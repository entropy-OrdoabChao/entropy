#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""m0_exposure_map — ANALYSIS-side FULL-SPECTRUM violation organ (m0).

USER DIRECTIVE (2026-08-31): "the whole point of the m0 and hermes is it has
full legal access and can hit from all angles. this shows we understand law
fully and is exposure for when agencies that can do something can use our
cases. ... it still feels like we are using one slice of the pie."

WHAT IT DOES (case-agnostic):
  Reads a case's CASE_FACTS.md + stub evidence, then asks the m0 LLM — grounded
  by semantic corpus retrieval across ALL titles (state + federal, civil +
  criminal + regulatory) and the Obsidian graph's accumulated notes — to
  identify EVERY duty/prohibition the recorded conduct implicates. Each
  candidate violation is then VERIFIED against corpus text (deterministic
  guardrail: the statute must exist in corpus or it is named NOT IN CORPUS)
  and ROUTED by what it is FOR:

    PLEAD    — private right of action exists -> candidate count / amendment
    EVIDENCE — no private right, but the violation is proof for a pleaded
               count (e.g. UIPA unfair-claims acts as 8371 bad-faith evidence)
    REFER    — enforcement belongs to an agency (DOI, AG, CFPB, FTC, DOJ,
               county DA) -> documented exposure for the referral packet
    CRIMINAL — penal statutes implicated by the conduct (never pleaded by a
               private civil plaintiff; documented as exposure + DA/AG referral)

  A violation with no private right is NOT unusable — it is referral material
  and bad-faith/punitive-damages evidence. That routing IS the full pie.

OUTPUT: cases/<slug>/EXPOSURE_MAP.md (three-lane table + per-violation
  grounding) + one learnings.jsonl line; the connected-notes loop links it
  into the graph. Re-runnable; .bak before overwrite.

DISCIPLINE: corpus + written law decide; every entry [corpus: <source>] /
  [official:<cite>] / NOT IN CORPUS; no fabricated cites; the LLM proposes,
  the corpus-presence check disposes. Section-by-section (bounded batches),
  never one giant pass.

Usage:
  python3 -m pipeline.m0_exposure_map <case_dir> [--stub <stub.md>] [--dry-run]
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import argparse, datetime, json, os, re, sqlite3, sys, urllib.request
from pathlib import Path

CHUNK_DB = (_eroot() / "corpus/chunk_text.db")
MCP = os.environ.get("BEEZLE_MCP_URL", "http://localhost:8200/mcp")
LLM = os.environ.get("BEEZLE_LLAMA_URL", "http://localhost:8000/v1/chat/completions")
MODEL = os.environ.get("BEEZLE_CF_MODEL", "qwen3.8-27b")

ROUTES = ("PLEAD", "EVIDENCE", "REFER", "CRIMINAL")


# ---------------------------------------------------------------- retrieval
def _mcp_search(query: str, n: int = 6) -> list[dict]:
    """Semantic corpus lane (primary). Returns [] if :8200 down (guardrail
    lane below still verifies presence by direct sqlite)."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "search_chunks",
                                  "arguments": {"query": query, "n_results": n}}})
    req = urllib.request.Request(
        MCP, data=body.encode(), headers={"Content-Type": "application/json",
                                          "Accept": "application/json, text/event-stream"})
    try:
        raw = urllib.request.urlopen(req, timeout=120).read().decode()
        for line in raw.splitlines():
            if line.startswith("data:"):
                d = json.loads(line[5:])
                txt = d.get("result", {}).get("content", [{}])[0].get("text", "{}")
                return json.loads(txt).get("result", [])
    except Exception:
        pass
    return []


def _corpus_has(cite_tokens: list[str]) -> str | None:
    """Deterministic guardrail: does the corpus actually carry this statute's
    text? Returns the source name or None. Tokens tried in order."""
    con = sqlite3.connect(f"file:{CHUNK_DB}?mode=ro", uri=True, timeout=60)
    try:
        for tok in cite_tokens:
            row = con.execute(
                "SELECT source FROM chunk_text WHERE text LIKE ? LIMIT 1",
                (f"%{tok}%",)).fetchone()
            if row:
                return row[0]
    finally:
        con.close()
    return None


# ---------------------------------------------------------------- LLM lane
def _llm(prompt: str, max_tokens: int = 8000) -> str:
    body = json.dumps({"model": MODEL, "max_tokens": max_tokens,
                       "temperature": 0.1,
                       # suppress the reasoning channel: exposure listing is
                       # extraction, not derivation — thinking burned the whole
                       # budget twice (finish=length, content=None)
                       "chat_template_kwargs": {"enable_thinking": False},
                       "messages": [{"role": "user", "content": prompt}]})
    req = urllib.request.Request(LLM, data=body.encode(),
                                 headers={"Content-Type": "application/json"})
    raw = json.loads(urllib.request.urlopen(req, timeout=900).read())
    ch = raw["choices"][0]
    msg = ch.get("message", {})
    content = msg.get("content") or ""
    if not content:
        content = msg.get("reasoning") or msg.get("reasoning_content") or ""
        print(f"[exposure] WARN: empty content (finish={ch.get('finish_reason')}); "
              f"salvaging from reasoning channel ({len(content)} chars)")
    return content


PROMPT = """You are m0, a legal-analysis engine with full corpus access. Below are the
recorded FACTS of a case (all from the defendant's own documents) and corpus excerpts
retrieved for candidate legal angles.

TASK: identify EVERY statutory/regulatory/common-law duty or prohibition that the
recorded conduct plausibly implicates — state AND federal, civil AND criminal AND
regulatory. Think like a full-coverage compliance auditor, not an advocate: the goal
is the complete exposure surface, routed correctly. Do NOT limit yourself to claims
already pleaded.

For each violation output ONE json object in a json array:
  {"cite": "<exact citation>",
   "name": "<short name>",
   "conduct": "<the specific recorded fact(s) that implicate it, with dates>",
   "route": "PLEAD|EVIDENCE|REFER|CRIMINAL",
   "route_reason": "<private right of action? which agency enforces? how it proves a pleaded count?>",
   "enforcer": "<who can act: private plaintiff / PA DOI / PA AG / CFPB / FTC / DA / DOJ>"}

ROUTING RULES:
- PLEAD only if a private right of action exists for THIS plaintiff on THESE facts.
- A violation with no private right routes to EVIDENCE (if it proves an element of a
  pleaded count, e.g. statutory unfair-claims standards as bad-faith evidence) and/or
  REFER (the agency that can act).
- CRIMINAL statutes are never pleaded by a private civil plaintiff: route CRIMINAL,
  enforcer = DA/AG/DOJ, and note any civil-action subsection honestly.
- If unsure a statute exists, still list it — the pipeline verifies against corpus
  and marks NOT IN CORPUS honestly. Never invent subsection text.

FACTS:
{facts}

CORPUS EXCERPTS (grounding, not exhaustive):
{excerpts}

Return ONLY the json array."""


# ---------------------------------------------------------------- angles
# Bounded semantic probes across the WHOLE corpus — each angle is one bounded
# retrieval (section-by-section discipline), spanning state/federal/civil/
# criminal/regulatory. Case-agnostic phrasings; facts supply the specifics.
ANGLE_QUERIES = (
    "unfair insurance claims settlement practices statutory standards misrepresenting facts refusing to pay without investigation",
    "debt collection unfair practices collecting amount not owed disputed debt validation federal state",
    "consumer protection deceptive acts practices private action ascertainable loss",
    "theft receiving retaining property of another failure to return funds known legal obligation criminal",
    "insurance fraud false statements to regulator or insured criminal civil action",
    "unauthorized electronic fund transfer authorization revoked bank account debits consumer",
    "mail wire fraud scheme to defraud use of mails interstate communications",
    "regulatory duty to enforce insurance code commissioner powers penalties",
)


def gather(case_dir: Path, stub: Path | None) -> tuple[str, str]:
    facts = ""
    cf = case_dir / "CASE_FACTS.md"
    if cf.exists():
        t = cf.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"## OPERATIVE FACTS.*", t, re.S)
        if m:
            # IC prepends here too (v2.4): the OPERATIVE-FACTS branch must not
            # skip the identity section that sits above it in the file.
            _icm0 = re.search(r"^## INITIAL CLAIM.*?(?=^## |\Z)", t, re.M | re.S)
            _ic0 = (_icm0.group(0)[:4000] + "\n") if _icm0 else ""
            facts = _ic0 + m.group(0)[:9000 - len(_ic0)]
        else:
            # NO OPERATIVE FACTS section (hardening sprint 2026-08-31, user:
            # "it looks at the case but doesnt seem to look at the INITIAL
            # CLAIM AT STATE LEVEL"): a blind head-window feeds stale intake
            # logs and misses the claim core. Piece-wise subsection triage —
            # split to ###-level (## sections dilute; the claim analysis
            # hides inside huge sections), score by CLAIM-CORE terms weighted
            # 5x over generic litigation terms, absolute count not density,
            # take best until budget. Header always included (case type /
            # cause / claims live there).
            _CORE = re.compile(
                r"(?i)\b(chain of title|real party|security interest|quiet"
                r" title|standing|holder|note|assignment|indorse|endorse"
                r"|allonge|conveyance|deed|mortgage|claim|count|cause of"
                r" action|element)\b")
            _GEN = re.compile(
                r"(?i)\b(duty|breach|violat|fraud|conceal|damag|refund"
                r"|premium|escrow|dispute|garnish|judgment|dismiss|order"
                r"|motion|interest)\b")
            def _score(s):
                return 5 * len(_CORE.findall(s)) + len(_GEN.findall(s))
            parts = re.split(r"(?=^##+ )", t, flags=re.M)
            head = parts[0] if parts and not parts[0].startswith("#") else ""
            subs = [p for p in parts if p.startswith("#")]
            # keep the case-header section (Case Information etc.) first
            _hdr = next((p for p in subs if re.match(r"^## Case Information", p)), "")
            # INITIAL CLAIM is UNCONDITIONAL (v2.4, 2026-08-31): the section is
            # the case's identity — it never competes in the scoring race. The
            # ###-level split scatters its children, so collect the whole
            # family (the ## header and every ### until the next ##) verbatim.
            _icm = re.search(r"^## INITIAL CLAIM.*?(?=^## |\Z)", t, re.M | re.S)
            _ic = _icm.group(0)[:4500] if _icm else ""
            _ic_keys = {id(p) for p in subs
                        if _icm and p in _icm.group(0)}
            scored = sorted((p for p in subs
                             if p is not _hdr and id(p) not in _ic_keys),
                            key=_score, reverse=True)
            budget = 9000 - len(_hdr[:1200]) - len(_ic)
            picked, used = [], 0
            for s in scored:
                take = s[:2500]
                if used + len(take) > budget:
                    continue
                picked.append(take)
                used += len(take)
            facts = _hdr[:1200] + "\n" + _ic + "\n" + "\n".join(picked)
    if stub and stub.exists():
        t = stub.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"## NEW EVIDENCE BLOCK.*?(?=## Administrative|## PARTIES|$)", t, re.S)
        if m:
            facts += "\n\n" + m.group(0)[:6000]
    # CLAIM-CORE COMPLETENESS ASSERT (class rule, hardening sprint 2026-08-31,
    # user: windowing must never starve the INITIAL CLAIM — on ANY case).
    # The case file's own declared claim identity (Case Type / Cause / Nature
    # of Suit lines) must survive into the assembled window. Loud, never
    # silent: a missing claim-core is a FAILED gather, not a quiet periphery
    # map.
    if cf.exists():
        _decl = " ".join(re.findall(
            r"(?im)^.*(?:case type|cause|nature of suit|claim taxonomy)\s*[:*]+.*$",
            cf.read_text(encoding="utf-8", errors="replace")[:4000]))
        _terms = {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z ]{3,30}", _decl)
                  for w in [w.strip()] if len(w.split()) <= 3 and len(w) > 5
                  and w.lower() not in ("case type", "nature of suit", "cause")}
        _missing = sorted(t2 for t2 in _terms if t2 not in facts.lower())
        if _terms and len(_missing) == len(_terms):
            print(f"[exposure] CLAIM-CORE ASSERT FAILED: none of the declared "
                  f"claim terms {sorted(_terms)[:6]} reached the window — "
                  "gather() is starving the initial claim; refusing to map "
                  "the periphery as if it were the case")
            raise SystemExit(3)
        elif _missing:
            print(f"[exposure] claim-core partial: missing {_missing[:5]} "
                  f"(present: {len(_terms)-len(_missing)}/{len(_terms)})")
    ex = []
    for q in ANGLE_QUERIES:
        for h in _mcp_search(q, 3):
            src = h.get("source", "?")
            txt = re.sub(r"\s+", " ", h.get("text", ""))[:600]
            if h.get("score", 0) > 0.55:
                ex.append(f"[{src}] {txt}")
    return facts, "\n\n".join(ex[:22])


def verify(items: list[dict]) -> list[dict]:
    """Guardrail: corpus-presence check per cite; stamps grounding."""
    out = []
    for it in items:
        cite = (it.get("cite") or "").strip()
        if not cite or it.get("route") not in ROUTES:
            continue
        toks = [cite]
        m = re.search(r"§+\s*([\d.\-]+[a-z]?)", cite)
        num = m.group(1) if m else None
        if num:
            toks += [f"§ {num}", f"§{num}"]
        src = _corpus_has(toks)
        it["grounding"] = f"[corpus: {src}]" if src else "NOT IN CORPUS"
        out.append(it)
    return out


def render(case_dir: Path, items: list[dict], facts_len: int) -> Path:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    lanes = {r: [i for i in items if i["route"] == r] for r in ROUTES}
    L = [f"# EXPOSURE MAP — full-spectrum violation surface",
         f"_m0_exposure_map, {ts}. Every entry corpus-verified or marked NOT IN "
         f"CORPUS. Routing: PLEAD = private right of action on these facts; "
         f"EVIDENCE = proves a pleaded count; REFER = agency enforcement; "
         f"CRIMINAL = penal exposure (DA/AG/DOJ), never privately pleaded._", ""]
    ICONS = {"PLEAD": "⚖️ PLEADABLE (private right of action)",
             "EVIDENCE": "🧾 EVIDENTIARY (proves pleaded counts)",
             "REFER": "🏛️ AGENCY REFERRAL (they can act; we document)",
             "CRIMINAL": "🚨 CRIMINAL EXPOSURE (referral only)"}
    for r in ROUTES:
        L.append(f"## {ICONS[r]}")
        if not lanes[r]:
            L.append("- none identified this pass")
        for it in lanes[r]:
            L += [f"### {it['cite']} — {it.get('name','')}",
                  f"- **Conduct:** {it.get('conduct','')}",
                  f"- **Why this lane:** {it.get('route_reason','')}",
                  f"- **Enforcer:** {it.get('enforcer','')}",
                  f"- **Grounding:** {it.get('grounding','')}", ""]
        L.append("")
    out = case_dir / "EXPOSURE_MAP.md"
    if out.exists():
        bak = out.with_suffix(f".md.bak.{datetime.datetime.now():%Y%m%d_%H%M%S}")
        bak.write_text(out.read_text(encoding='utf-8'), encoding="utf-8")
    out.write_text("\n".join(L), encoding="utf-8")
    return out


def duty_engine_lane(facts: str) -> list[dict]:
    """PRIMARY lane: m0 LLM COMPREHENSION selects which entity types and
    triggering actions the recorded facts implicate — reasoning ON (this is
    derivation, the whole reason the 27B reasoning model holds the legal
    lane). The duty_maps YAML vocabulary is the deterministic GUARDRAIL: the
    LLM can only select actions that exist in the maps (no invented
    triggers), and run_duty_analysis resolves them to curated duties.
    AGNOSTIC: vocabulary comes from EVERY map; nothing case-hardcoded."""
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from duty_engine import run_duty_analysis, list_available_entity_types, load_duty_map
    except Exception as e:
        print(f"[exposure] duty_engine unavailable ({e}); LLM lane carries")
        return []
    # full vocabulary: entity -> its triggering-action set (guardrail domain)
    vocab = {}
    for ent in list_available_entity_types():
        try:
            m = load_duty_map(ent)
            acts = set()
            for d in (m.get("duties") or []):
                acts.update(d.get("triggering_actions") or [])
            if acts:
                vocab[ent] = sorted(acts)
        except Exception:
            continue
    if not vocab:
        return []
    # SECTION-BY-SECTION (locked discipline; user 2026-08-31: "the m0 does it
    # in pieces because of context — it will OOM"): one bounded selection call
    # PER ENTITY-BATCH (3 maps/call, ~small menu each, 4K output), facts
    # window bounded, results merged. Never one giant menu+facts pass.
    ents = sorted(vocab)
    selection = {}
    _f = facts[:6000]
    for bi in range(0, len(ents), 3):
        batch = ents[bi:bi + 3]
        menu = "\n".join(f"- {ent}: {', '.join(vocab[ent])}" for ent in batch)
        sel_prompt = (
            "You are m0. Below are recorded case FACTS and a MENU of entity "
            "types with their defined triggering actions (curated duty maps).\n"
            "Reason about WHO acted in the facts (every actor: the company, "
            "its agents, any court/judge/counsel/regulator conduct) and WHICH "
            "listed actions the recorded conduct constitutes. Select ONLY "
            "actions from the menu, under their own entity type. An actor can "
            "match several entity types. If none of these entity types fit "
            "the facts, return {}.\n"
            'Return ONLY a json object: {"<entity_type>": ["<action>", ...]}\n\n'
            "FACTS:\n" + _f + "\n\nMENU:\n" + menu)
        body = json.dumps({"model": MODEL, "max_tokens": 4000, "temperature": 0.1,
                           "messages": [{"role": "user", "content": sel_prompt}]})
        req = urllib.request.Request(LLM, data=body.encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            raw = json.loads(urllib.request.urlopen(req, timeout=600).read())
            ch = raw["choices"][0]
            msg = ch.get("message", {})
            sel_txt = msg.get("content") or ""
            if not sel_txt:
                # reasoning burn: retry ONCE with thinking suppressed — by the
                # time the menu is this small the reasoning adds nothing
                body2 = json.dumps({"model": MODEL, "max_tokens": 2500,
                                    "temperature": 0.1,
                                    "chat_template_kwargs": {"enable_thinking": False},
                                    "messages": [{"role": "user", "content": sel_prompt}]})
                req2 = urllib.request.Request(LLM, data=body2.encode(),
                                              headers={"Content-Type": "application/json"})
                raw2 = json.loads(urllib.request.urlopen(req2, timeout=600).read())
                msg2 = raw2["choices"][0].get("message", {})
                sel_txt = msg2.get("content") or msg2.get("reasoning") or ""
                print(f"[exposure] batch {batch}: content empty "
                      f"(finish={ch.get('finish_reason')}), no-think retry -> "
                      f"{len(sel_txt)} chars")
        except Exception as e:
            print(f"[exposure] selection batch {batch} failed ({e}); skipping")
            continue
        m2 = re.search(r"\{[^{}]*(?:\[[^\]]*\][^{}]*)*\}", sel_txt or "", re.S)
        if not m2:
            print(f"[exposure] batch {batch}: no json object in reply "
                  f"({len(sel_txt or '')} chars)")
            continue
        try:
            part = json.loads(m2.group(0))
        except json.JSONDecodeError as e:
            print(f"[exposure] batch {batch}: bad json ({e})")
            continue
        for ent, acts in (part or {}).items():
            if ent in vocab and acts:
                good = [a for a in acts if a in set(vocab[ent])]
                if good:
                    selection.setdefault(ent, [])
                    selection[ent] = sorted(set(selection[ent]) | set(good))
    if not selection:
        print("[exposure] LLM selected no valid entity/actions; duty lane empty")
        return []
    print("[exposure] m0 selection:", {k: len(v) for k, v in selection.items()})
    out, seen_duty = [], set()
    for ent, actions in selection.items():
        r = run_duty_analysis(entity_type=ent, actions=actions,
                              target_court=os.environ.get("BEEZLE_COURT", ""),
                              fact_pattern=facts[:1500])
        for d in r.matched_duties:
            if d.duty_id in seen_duty:
                continue
            seen_duty.add(d.duty_id)
            # route deterministically: statutory duties with private remedies ->
            # PLEAD/EVIDENCE; regulatory/industry -> REFER; fiduciary/
            # common-law -> EVIDENCE (they ground pleaded counts)
            remedies = " ".join(d.violation_remedy).lower()
            if d.duty_class == "statutory" and re.search(
                    r"attorney fee|punitive|interest|return of|statutory damages|private", remedies):
                route = "PLEAD"
            elif d.duty_class in ("regulatory", "industry_standard"):
                route = "REFER"
            else:
                route = "EVIDENCE"
            out.append({
                "cite": d.source.replace("-", " ").replace("Pa CS", "Pa.C.S. §").replace("P S", "P.S. §"),
                "name": d.name,
                "conduct": f"matched recorded conduct: {', '.join(d.matched_actions)}",
                "route": route,
                "route_reason": (f"{d.duty_class} duty [{d.source_title}]; remedies: "
                                 f"{'; '.join(d.violation_remedy) or 'grounds pleaded counts'}"),
                "enforcer": d.regulator or "private plaintiff / court",
                "grounding": f"[duty_map: {d.duty_id}]",
                "_burden_flip": d.burden_flip_questions[:2],
            })
    print(f"[exposure] duty_engine lane: {len(out)} duties across "
          f"{len(selection)} entity types")
    return out


def map_exposure(case_dir: Path, stub: Path | None = None) -> dict:
    """The organ's entry point (seat-able): gather → duty lane → LLM gap-fill lane → verify → dedupe.
    Returns {"items": [...], "facts_len": n}. Writes nothing; main() renders + logs."""
    facts, excerpts = gather(case_dir, stub)
    if not facts:
        return {"items": [], "facts_len": 0, "error": "no CASE_FACTS.md found"}
    print(f"[exposure] facts {len(facts)} chars; excerpts {len(excerpts)} chars")
    # LANE 1 (deterministic guardrail + seed): duty_engine YAML maps
    duty_items = duty_engine_lane(facts)
    # LANE 2 (comprehension): LLM fills angles the YAML maps don't cover
    known = "\n".join(f"- {d['cite']} ({d['route']})" for d in duty_items)
    resp = _llm(PROMPT.replace("{facts}", facts[:8000]).replace("{excerpts}", excerpts[:5000])
                + "\n\nALREADY MAPPED (do NOT repeat; find what these MISS — "
                  "criminal titles, federal statutes, EFT/banking, mail/wire, "
                  "collection-agent liability):\n" + known)
    m = re.search(r"\[\s*\{.*\}\s*\]", resp or "", re.S)
    llm_items = []
    if m:
        try:
            llm_items = json.loads(m.group(0))
        except json.JSONDecodeError as e:
            print(f"[exposure] WARN bad json from LLM lane: {e}")
    else:
        print("[exposure] WARN: LLM lane returned no json array (duty lane carries)")
    items = duty_items + verify(llm_items)
    # DEDUPE (v2.4): the LLM gap-fill lane re-nominates the same criminal/refer
    # cites per batch (2822 dry-run: 18 USC 1341 x4). Key = (route, cite);
    # first occurrence wins (duty-lane entries lead the list, so duty_map
    # grounding always outranks a bare corpus stamp for the same cite).
    _seen, _uniq = set(), []
    for it in items:
        k = (it["route"], re.sub(r"\s+", " ", it["cite"]).strip().lower())
        if k in _seen:
            continue
        _seen.add(k)
        _uniq.append(it)
    if len(_uniq) < len(items):
        print(f"[exposure] dedupe: {len(items)} -> {len(_uniq)}")
    items = _uniq
    print(f"[exposure] {len(items)} violations identified")
    for it in items:
        print(f"  [{it['route']:8}] {it['cite']} — {it.get('grounding')}")
    return {"items": items, "facts_len": len(facts)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("case_dir")
    ap.add_argument("--stub", default=None)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    case_dir = Path(a.case_dir)
    stub = Path(a.stub) if a.stub else None
    res = map_exposure(case_dir, stub)
    if res.get("error"):
        sys.exit("[exposure] " + res["error"])
    items, facts_len = res["items"], res["facts_len"]
    if a.dry_run:
        return
    out = render(case_dir, items, facts_len)
    print(f"[exposure] wrote {out}")
    # learning loop
    lj = Path("output/learnings.jsonl")
    with lj.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "text": (f"EXPOSURE MAP {case_dir.name}: {len(items)} violations "
                     f"routed — " + "; ".join(
                         f"{it['cite']}→{it['route']}" for it in items)),
            "tags": ["exposure-map", "full-spectrum", case_dir.name],
            "source": "pipeline.m0_exposure_map"}) + "\n")
    print("[exposure] learning appended")


if __name__ == "__main__":
    main()
