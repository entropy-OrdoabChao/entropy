"""m2 ORGAN bots — the m0 pattern applied per lane (B-m2-8).

m0 (`case_facts_refresh`) does not hand the model a packet and ask it to reason; Python SURFACES the items (the
movant's cites via `_CITE_RX`), PINS each one to its own corpus text (`_authority_own_text`), asks ONE bounded
question per item (`_holding_sentence_llm`: 180 tokens, own text in, one sentence out), GATES the answer in code,
and UPSERTS the result into CASE_FACTS. The free-reasoning fleet bots inverted that (65-79 KB packet, 20 cites,
5-8 min, no write-back) and dropped 37% of their authorities at the guard. An organ bot:

  item = one cite / one rule / one count         (deterministic surface, from the adverse filing)
  own text = the pinned corpus chunks for it     (deterministic, turbovec + pin, phase-1 — never model memory)
  one micro LLM call per item                    (<= 320 tokens, T=0, JSON, lane URL = M2_LANE)
  guard in code                                  (quote verbatim in own text, anachronism, record years, movant-relief)
  cell in the fleet schema  +  `## M2 LANE FINDINGS / ### <lane>` block upserted into CASE_FACTS (incremental)

  M2_ROOT=<sandbox> python3 -m pipeline.m2.organ.runner <run_dir> --slug <slug> --lanes authority-stack,stare-decisis
      [--lane-url http://localhost:8000/v1] [--model qwen3.8-27b] [--write-cf] [--redo]
  reads  <run_dir>/packet.json {reply_brief, ...}   writes <run_dir>/cells_<lane>.json (+ CASE_FACTS block with --write-cf)

Lanes here: authority-stack (cite; micro-LLM), stare-decisis (cite; PYTHON ONLY — binding class from the reporter, rule
quote from m0's `_binding_weight_rule`). A lane is a (surface, question, guard) triple; add lanes by adding a triple.
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import argparse, json, os, re, sys, time, urllib.request
from pathlib import Path

sys.path.insert(0, str(_epkg()))
import pipeline  # noqa
_root = os.environ.get("M2_ROOT")
if _root and f"{_root}/pipeline" not in pipeline.__path__:
    pipeline.__path__.insert(0, f"{_root}/pipeline")
from pipeline.m2.hermes_fleet import brief as B          # noqa: E402  cites_in_plus / pull_cites_in_context / m0_baseline
from pipeline import case_facts_refresh as CFR            # noqa: E402  _binding_weight_rule / _upsert_block / _SECTION_HEADERS

CASES = (_eroot() / "obsidian_sync/cases")
CF_HEADER = "## M2 LANE FINDINGS"

# ---------- deterministic knowledge the guard needs (small, enacted facts; extend, never infer) ----------
STATUTE_ENACTED = {"8371": 1990}                 # 42 Pa.C.S. § 8371 — Act 1990-6; a 1981 opinion cannot quote it
MOVANT_RELIEF = re.compile(r"\b(must|should) be (dismissed|stricken|sustained)\b|\bobjections? (must|should) be sustained\b", re.I)
_YEAR = re.compile(r"\b(19|20)\d{2}\b")
_PAREN_COURT = re.compile(r"\((Pa\.\s?Super\.|Pa\.\s?Cmwlth\.|Pa\.|3d Cir\.|E\.D\. Pa\.|M\.D\. Pa\.|W\.D\. Pa\.|[A-Z]\.D\. [A-Z][a-z]+\.)\s*(\d{4})\)")


def _norm(x: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (x or "").lower())


def _years_in_record(slug: str) -> set[str]:
    cf = CASES / slug / "CASE_FACTS.md"
    return set(m.group(0) for m in _YEAR.finditer(cf.read_text(encoding="utf-8", errors="ignore"))) if cf.exists() else set()


# ---------- surface: the movant's authorities, each with the sentence that cites it and its pinned own text ----------
def surface_cites(reply: str, per: int = 3) -> list[dict]:
    """One item per AUTHORITY. The name form ('Wenk v. State Farm') and the reporter form ('228 A.3d 540') of the same case
    are MERGED when the reporter follows the name in the same citation sentence — otherwise the same authority yields two
    cells and the reporter-only one can read NOT_IN_CORPUS while the name pinned (first run: 4 such pairs). Court + year come
    from the parenthetical after the reporter: the deciding court is a fact of the citation, never inferred."""
    pinned = B.pull_cites_in_context(reply, per=per)
    by = {}
    for h in pinned:
        label = h["source"].split(" chunk=")[0][5:]
        by.setdefault(label, []).append(h)
    flat = re.sub(r"\s+", " ", reply)
    raw = []
    for c, spec in B.cites_in_plus(reply):
        cf = re.sub(r"\s+", " ", c)
        positions = [m.start() for m in re.finditer(re.escape(cf), flat)]
        i = positions[0] if positions else -1
        raw.append({"cite": c, "kind": spec["kind"], "pos": positions, "chunks": [h for h in by.get(c, []) if h.get("chunk_id")]})
    # merge reporter -> preceding name within 140 chars
    names = [r for r in raw if r["kind"] != "reporter"]; reps = [r for r in raw if r["kind"] == "reporter"]
    for rp in reps:
        host = None
        for nm in names:
            if any(0 < rp_pos - n_pos <= 140 for n_pos in nm["pos"] for rp_pos in rp["pos"]):
                host = nm; break
        if host:
            host["reporter"] = rp["cite"]; host["pos"] = host["pos"] + rp["pos"]
            ids = {h["chunk_id"] for h in host["chunks"]}
            host["chunks"] += [h for h in rp["chunks"] if h["chunk_id"] not in ids]
        else:
            names.append(rp)
    out = []
    for r in names:
        anchor = r["pos"][0] if r["pos"] else -1
        ctx = flat[max(0, anchor - 350): anchor + 260].strip() if anchor >= 0 else ""
        court, year = None, None
        for ps in r["pos"]:
            m = _PAREN_COURT.search(flat[ps: ps + 260])
            if m: court, year = m.group(1), int(m.group(2)); break
        label = r["cite"] + (f", {r['reporter']}" if r.get("reporter") else "")
        out.append({"cite": label, "kind": r["kind"], "ctx": ctx, "chunks": r["chunks"], "court": court, "year": year})
    return out


# ---------- one bounded question ----------
def micro_llm(prompt: str, url: str, model: str, max_tokens: int = 320, timeout: int = 120) -> str:
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens,
                       "temperature": 0.0, "top_p": 1.0, "response_format": {"type": "json_object"},
                       "chat_template_kwargs": {"enable_thinking": False}}).encode()
    req = urllib.request.Request(url.rstrip("/") + "/chat/completions", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return (json.loads(r.read().decode())["choices"][0]["message"]["content"] or "").strip()


def _json(txt: str) -> dict:
    try: return json.loads(txt)
    except Exception:
        m = re.search(r"\{.*\}", txt, re.S)
        try: return json.loads(m.group(0)) if m else {}
        except Exception: return {}


# ---------- guard (code, not opinion) ----------
def guard(cell: dict, own_texts: list[str], record_years: set[str], year: int | None) -> dict:
    notes = []
    norm_own = [_norm(t) for t in own_texts]
    kept = []
    for a in cell.get("authorities", []):
        q = a.get("quote") or ""
        nq = _norm(q)
        if len(q.split()) < 8 or not any(nq[:100] in t for t in norm_own):
            notes.append(f"quote not verbatim in own text: {q[:60]!r}"); continue
        if year and any(k in q and year < y for k, y in STATUTE_ENACTED.items()):
            notes.append(f"anachronism: {year} opinion names a {STATUTE_ENACTED} statute"); continue
        a["verified"] = True; kept.append(a)
    cell["authorities"] = kept
    if MOVANT_RELIEF.search(cell.get("proposition") or ""):
        notes.append("proposition argues the movant's relief"); cell["proposition"] = ""
    bad_years = {y for y in _YEAR.findall(cell.get("proposition") or "") } - set()
    for y in set(m.group(0) for m in _YEAR.finditer(cell.get("proposition") or "")):
        if y not in record_years and not any(y in t for t in own_texts):
            notes.append(f"year {y} not in record/own text"); cell["proposition"] = ""
    if not kept and cell.get("status") == "GROUNDED":
        cell["status"] = "GAP"
    if not cell.get("proposition") and cell.get("status") not in ("NOT_IN_CORPUS", "SUPPORTS_MOVANT", "NOT_APPLICABLE"):
        cell["status"] = "GAP"
    cell["guard_notes"] = notes
    return cell


# ---------- lane: authority-stack (micro-LLM per cite) ----------
Q_AUTHORITY = """You verify ONE authority the opposing party (the movant, who filed preliminary objections) relies on.
AUTHORITY: {cite}
THE MOVANT'S SENTENCE CITING IT: "{ctx}"
THE AUTHORITY'S OWN TEXT (corpus chunks; the only text you may quote):
{own}

Answer JSON only, exactly these keys:
{{"stands_for": "yes|partly|no",
  "holding_quote": "<8-40 words copied VERBATIM from THE AUTHORITY'S OWN TEXT above that states what it actually holds on this point; empty string if none>",
  "finding": "<= 60 words: does the own text support what the movant says it holds? name the gap or overstatement if any>",
  "proposition": "<1-2 sentences for the party OPPOSING the preliminary objections, court-facing, no 'corpus/chunk/excerpt' words; empty if the authority genuinely supports the movant>"}}
Never quote the movant's sentence as the authority's holding. Never add a statute, date or amount not in the texts."""


def lane_authority_stack(items, slug, url, model, record_years):
    base = B.m0_baseline(slug, B.M0_SECTIONS["authority-stack"]) or ""
    cells, t0 = [], time.time()
    for it in items:
        c, chunks = it["cite"], it["chunks"]
        tag = "confirmed" if _norm(c.split(",")[0]) in _norm(base) else "new"
        level = 6 if it["kind"] in ("case", "reporter") else 5
        if not chunks:
            cells.append({"claim": c, "status": "NOT_IN_CORPUS", "level": level, "m0": tag, "finding": f"No corpus chunk names {c}; its holding cannot be verified and must not be characterized.",
                          "proposition": "", "authorities": [], "gap": f"NOT IN CORPUS by semantic+pin: {c}", "fix": "pull + ingest (legal-corpus-ingest) before relying on or rebutting it", "record_facts": [], "seconds": 0})
            continue
        own = "\n---\n".join(f"[{h['chunk_id']} part={h.get('part')} RECITAL={'yes' if h.get('recital') else 'no'}]\n{h['text']}" for h in chunks)
        t = time.time()
        try: ans = _json(micro_llm(Q_AUTHORITY.format(cite=c, ctx=it["ctx"][:600], own=own), url, model))
        except Exception as e: ans = {"error": str(e)[:120]}
        q = (ans.get("holding_quote") or "").strip()
        src = next((h["chunk_id"] for h in chunks if _norm(q)[:100] in _norm(h["text"])), chunks[0]["chunk_id"]) if q else chunks[0]["chunk_id"]
        sf = (ans.get("stands_for") or "").lower()
        status = "SUPPORTS_MOVANT" if sf == "yes" and not (ans.get("proposition") or "").strip() else ("GROUNDED" if sf else "GAP")
        cell = {"claim": c, "status": status, "level": level, "m0": tag,
                "finding": (ans.get("finding") or ans.get("error") or "").strip(), "proposition": (ans.get("proposition") or "").strip(),
                "authorities": ([{"cite": c, "quote": q, "source": src, "speaker": "court"}] if q else []),
                "gap": None if ans.get("stands_for") else "model returned no verdict", "fix": None, "record_facts": [],
                "stands_for": ans.get("stands_for"), "seconds": round(time.time() - t, 1)}
        cells.append(guard(cell, [h["text"] for h in chunks], record_years, it["year"]))
    return cells, round(time.time() - t0, 1)


# ---------- lane: stare-decisis (PYTHON ONLY — the binding class is a fact about the reporter, not an opinion) ----------
def _binding_class(it) -> tuple[str, bool]:
    c, court = it["cite"], it["court"] or ""
    if re.search(r"\bPa\.\s?Super\.", court) or re.search(r"\bPa\.\s?Super\.\s+\d", c): return ("Superior Court of Pennsylvania — binding on the Court of Common Pleas", True)
    if re.search(r"\bPa\.\s?Cmwlth\.", court): return ("Commonwealth Court of Pennsylvania — binding on the Court of Common Pleas", True)
    if court == "Pa." or re.search(r"\b\d+\s+Pa\.\s+\d+", c): return ("Supreme Court of Pennsylvania — binding", True)
    if re.search(r"U\.S\.|S\.\s?Ct\.", c): return ("Supreme Court of the United States — binding on federal questions only", True)
    if re.search(r"F\.\s?Supp|F\.\s?[234]d|WL\s+\d|Cir\.|D\. Pa\.", c + " " + court): return ("federal court — NOT binding on Pennsylvania state courts absent a U.S. Supreme Court pronouncement", False)
    if re.search(r"A\.\s?[23]d", c): return ("Pennsylvania appellate reporter (court not stated in the parenthetical) — verify the deciding court before weighting", True)
    return ("deciding court not stated in the citation — NOT DETERMINED", None)


def lane_stare_decisis(items, slug, record_years):
    t0 = time.time()
    try: rule_q, rule_cite = CFR._binding_weight_rule()
    except Exception: rule_q, rule_cite = "", ""
    base = B.m0_baseline(slug, B.M0_SECTIONS["stare-decisis"]) or ""
    cells = []
    for it in items:
        if it["kind"] not in ("case", "reporter"): continue
        cls, binding = _binding_class(it)
        tag = "confirmed" if _norm(it["cite"].split(",")[0]) in _norm(base) else "new"
        if binding is None:
            cells.append({"claim": it["cite"], "status": "GAP", "level": 7, "m0": tag, "finding": f"{it['cite']}: {cls}.", "proposition": "", "authorities": [],
                          "gap": "deciding court not in the citation; resolve from the pinned chunk's caption before weighting", "fix": None, "record_facts": [], "binding": None, "seconds": 0})
            continue
        auths = []
        if not binding and rule_q and len(rule_q.split()) >= 8:
            auths.append({"cite": rule_cite, "quote": rule_q, "source": "corpus:" + _norm(rule_cite)[:40], "verified": True, "speaker": "court"})
        prop = ("" if binding else f"{it['cite']} is a decision of a {cls.split(' — ')[0]}; it does not bind this Court, and the movant identifies no United States Supreme Court pronouncement adopting its proposition. The objection resting on it carries no binding authority.")
        cells.append({"claim": it["cite"], "status": "GROUNDED" if not binding else "NOT_APPLICABLE", "level": 7, "m0": tag,
                      "finding": f"{it['cite']}: {cls}." + ("" if it["chunks"] else " (not pinned in corpus — binding class from the reporter only)"),
                      "proposition": prop, "authorities": auths, "gap": None, "fix": None, "record_facts": [], "binding": binding, "seconds": 0})
    return cells, round(time.time() - t0, 1)


# ---------- write-back: fleet schema file + CASE_FACTS block (incremental) ----------
def cf_block(existing: str, lane: str, cells: list[dict]) -> str:
    sec = re.search(rf"^### {re.escape(lane)}\n(.*?)(?=^### |\Z)", existing, re.S | re.M)
    old = sec.group(1) if sec else ""
    lines = [f"### {lane}", ""]
    kept_old = [ln for ln in old.splitlines() if ln.strip()]
    seen = {ln.split("**")[1] for ln in kept_old if ln.count("**") >= 2}
    lines += kept_old
    for c in cells:
        if c["claim"] in seen or c["status"] in ("NOT_APPLICABLE",) or not (c.get("proposition") or c["status"] == "NOT_IN_CORPUS"): continue
        a = c["authorities"][0] if c["authorities"] else None
        q = f' — "{a["quote"]}" ({a["cite"]}) [corpus:{a["source"]}]' if a else ""
        lines.append(f"- **{c['claim']}** [{c['status']}; m0 {c['m0']}] {c['finding']}{q}" + (f" → {c['proposition']}" if c.get("proposition") else ""))
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("run_dir"); ap.add_argument("--slug", required=True)
    ap.add_argument("--lanes", default="authority-stack,stare-decisis"); ap.add_argument("--lane-url", default=os.environ.get("M2_LANE", "http://localhost:8000/v1"))
    ap.add_argument("--model", default=os.environ.get("M2_MODEL", "qwen3.8-27b")); ap.add_argument("--write-cf", action="store_true"); ap.add_argument("--redo", action="store_true")
    a = ap.parse_args(argv)
    run = Path(a.run_dir); p = json.loads((run / "packet.json").read_text(encoding="utf-8"))
    T0 = time.time()
    items = surface_cites(p.get("reply_brief", ""))
    t_surface = round(time.time() - T0, 1)
    print(f"[organ] surfaced {len(items)} authorities, {sum(1 for i in items if i['chunks'])} pinned in corpus ({t_surface}s incl. turbovec load)")
    years = _years_in_record(a.slug)
    report = {"surface_s": t_surface, "lanes": {}}
    blocks = {}
    for lane in [x.strip() for x in a.lanes.split(",") if x.strip()]:
        if lane == "authority-stack": cells, secs = lane_authority_stack(items, a.slug, a.lane_url, a.model, years)
        elif lane == "stare-decisis": cells, secs = lane_stare_decisis(items, a.slug, years)
        else: print(f"[organ] no organ for lane {lane} yet"); continue
        (run / f"cells_{lane}.json").write_text(json.dumps({"bot": lane, "organ": True, "cells": cells}, indent=1), encoding="utf-8")
        st = {}
        for c in cells: st[c["status"]] = st.get(c["status"], 0) + 1
        report["lanes"][lane] = {"cells": len(cells), "seconds": secs, "status": st, "verified_quotes": sum(len(c["authorities"]) for c in cells),
                                 "guard_notes": sum(len(c.get("guard_notes", [])) for c in cells)}
        print(f"[organ] {lane}: {len(cells)} cells in {secs}s {st} verified_quotes={report['lanes'][lane]['verified_quotes']} guard_notes={report['lanes'][lane]['guard_notes']}")
        blocks[lane] = cells
    if a.write_cf and blocks:
        cf = CASES / a.slug / "CASE_FACTS.md"
        doc = cf.read_text(encoding="utf-8", errors="ignore")
        m = re.search(rf"^{re.escape(CF_HEADER)}\n(.*?)(?=^## |\Z)", doc, re.S | re.M)
        existing = "" if a.redo else (m.group(1) if m else "")
        body = existing
        for lane, cells in blocks.items():
            blk = cf_block(existing, lane, cells)
            body = re.sub(rf"^### {re.escape(lane)}\n.*?(?=^### |\Z)", "", body, flags=re.S | re.M).rstrip() + "\n\n" + blk
        bak = cf.with_suffix(f".md.bak.{time.strftime('%Y%m%d_%H%M%S')}"); bak.write_text(doc, encoding="utf-8")
        ok = CFR._upsert_block(cf, CF_HEADER, CF_HEADER + "\n" + body.strip() + "\n")
        print(f"[organ] CASE_FACTS {CF_HEADER} upserted={ok} (backup {bak.name})")
    report["total_s"] = round(time.time() - T0, 1)
    (run / "ORGAN_REPORT.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(f"[organ] total {report['total_s']}s → {run}/ORGAN_REPORT.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
