"""m2 Hermes fleet — phase-1 packet builder. ONE deterministic retrieval pass (turbovec in-process, pinned named cites,
part-filtered) writes a per-bot packet so a Hermes child VERIFIES and REASONS instead of searching 30x (agent bots
measured 50-70 min each when they searched; the script runner's cells cost 5 s because retrieval was phase 1).

  M2_ROOT=<sandbox> python3 -m pipeline.m2.hermes_fleet.brief <run_dir> [--bots a,b] [--k 6]
    reads  <run_dir>/packet.json  {reply_brief, po_verified_record, record_slice}
    writes <run_dir>/packets/<bot>.md + <run_dir>/packets/INDEX.json (bot -> packet path, goal line, out file)
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import argparse, json, os, re, sys
from pathlib import Path

sys.path.insert(0, str(_epkg()))
import pipeline  # noqa
_root = os.environ.get("M2_ROOT")
if _root and f"{_root}/pipeline" not in pipeline.__path__:
    pipeline.__path__.insert(0, f"{_root}/pipeline")
from pipeline.m2 import analysis_runner as AR  # noqa: E402
from pipeline.case_profile import profile, CaseProfileGap  # noqa: E402

HERE = Path(__file__).parent


def _asset(name: str) -> Path:
    """An asset that lives NEXT TO this module in the live tree.

    `Path(__file__).parent / name` breaks the moment this module is loaded from a sandbox
    overlay (the overlay carries the changed .py, not the unchanged .md), so importing brief.py
    under scripts/sandbox_overlay.py died at import time and no gate could ever exercise it.
    Resolution order: beside __file__, then the live package dir, then M2_ROOT."""
    cands = [HERE / name]
    for base in (p for p in pipeline.__path__ if p):
        cands.append(Path(base) / "m2" / "hermes_fleet" / name)
    for c in cands:
        if c.is_file():
            return c
    raise SystemExit(f"[brief] asset not found: {name} (looked in {[str(c) for c in cands]})")


RULES = _asset("CELL_RULES.md").read_text(encoding="utf-8")

# bot -> (panel, unit, the one question, retrieval queries beyond the named cites, corpus families)
BOTS = {
    "authority-stack": ("GROUND", "cite", "for EACH authority the adverse filing cites: real + binding in this forum + stands for the proposition it is assigned? One cell per cite.", [], ['usreports','cap_us_','ca3_','pa_','courtlistener_','doctrine']),
    "construction": ("GROUND", "term", "for each contested term: which construction canon governs and how it resolves the reading. One cell per term.", ["gist of the action nature of the duty", "statutory construction plain meaning consumer transaction purchases", "bad faith statute legislative response common law"], ['sutherland','pa_cs_1_','canons','amjur_73','usreports','pa_']),
    "definitions": ("GROUND", "term", "controlling term-of-art definition (statute first, then binding case, then treatise); means = closed, includes = open only with an expansion clause. One cell per term.", ["definition trade commerce sale", "definition bad faith insurer", "definition consumer transaction"], ['plaw_','comps_','usc_','pa_cs_','pa_73ps_','stat_','blacks','bouvier']),
    "verbatim-rule": ("GROUND", "rule", "verbatim operative rule text (shall/may/void) for every statute or rule the adverse filing invokes, with its enacted source. One cell per rule.", ["preliminary objections limited to the following grounds", "unfair trade practices private action ascertainable loss", "action arising under an insurance policy bad faith interest punitive"], ['pa_cs_','pa_73ps_','pa_code','pa_rcp','pa_bulletin','plaw_','usc_']),
    "duty": ("DEFECT", "claim", "what DUTY the record + law establish that the adverse filing denies or ignores (statutory, implied covenant, social/tort, counsel's candor). One cell per duty.", ["implied covenant good faith fair dealing insurer", "candor toward the tribunal", "social duty imposed by law regardless of contract"], ['amjur_','cjs_','restatement','duty_map','pa_','usreports']),
    "procedural": ("DEFECT", "filing", "every facial procedural defect in the adverse filing: demurrer standard, cognizable grounds, new matter in a reply, what it failed to answer (silence = waiver). One cell per defect.", ["preliminary objections demurrer all material facts admitted as true", "any doubt resolved in favor of overruling", "reply brief new matter waiver"], ['pa_rcp','pa_rap','pa_re','pa_rja','pa_bulletin','pa_code','pa_']),
    "stare-decisis": ("DEFECT", "cite", "for each authority the adverse filing cites: non-binding cited as controlling, holding overstated, or binding authority omitted. One cell per cite.", ["federal decisions not binding on Pennsylvania state courts", "Superior Court binding on courts of common pleas"], ['usreports','cap_us_','ca3_','pa_','courtlistener_','doctrine']),
    "dismissal-posture": ("STRATEGY", "count", "for each count attacked: the binding authority that restores it on these pleaded facts, and leave to amend in the alternative. One cell per count.", ["leave to amend pleading liberally granted", "unjust enrichment pleaded in the alternative"], ['pa_','usreports','cap_us_','courtlistener_','pa_rcp']),
    "posture-vehicle": ("STRATEGY", "case", "after this adverse filing, Plaintiff's correct vehicle (sur-reply by leave, motion, nothing) and which vehicles are BLOCKED, from the rule text. One cell per vehicle.", ["preliminary objections response reply brief local rule", "sur-reply leave of court motion practice"], ['pa_rcp','pa_code','pa_bulletin','phila','pa_']),
    "remedy": ("STRATEGY", "count", "for each count attacked: the relief the law compels if the pleaded facts are admitted — overrule; alternatively leave to amend; the statutory remedies (treble damages, fees, interest, punitive). One cell per count. The proposition states OUR relief.", ["treble damages attorney fees private action", "interest prime rate plus three percent punitive damages costs"], ['usc_','pa_cs_','pa_73ps_','plaw_','amjur_22','amjur_25','pa_']),
    "pa-state-law": ("STRATEGY", "cite", "PA statute / Pa. Code / Pa.R.C.P. text by source name for every PA authority in play, quoted from the enacted text. One cell per source.", ["Pa.R.C.P. 1028 preliminary objections", "Pa.R.C.P. 1033 amendment", "unfair insurance practices act cancellation refund"], ['pa_cs_','pa_73ps_','pa_code','pa_rcp','pa_bulletin','pa_40ps']),
    "jurisdiction": ("THRESHOLD", "case", "subject-matter jurisdiction over every count and Plaintiff's standing (ascertainable loss) on this record; what the adverse filing's silence on jurisdiction means. One cell per question.", ["courts of common pleas unlimited original jurisdiction", "ascertainable loss of money or property standing"], ['pa_cs_42','pa_cs_','pa_','usreports','amjur_']),
    "capacity": ("THRESHOLD", "party", "the capacity each party acts in on THIS record (insured/purchaser, insurer, collector as agent) and whether appearing sui juris changes capacity or the pleading standard. One cell per party.", ["any person who purchases or leases goods or services primarily for personal family or household purposes", "insurer insured action arising under an insurance policy"], ['amjur_','cjs_','restatement','duty_map','pa_cs_15_','pa_cs_40_','usc_15_','pa_']),
    "case-law": ("THRESHOLD", "proposition", "for each proposition our opposition needs: the binding case and its quoted holding. One cell per proposition.", ["gist of the action doctrine duty social policy", "UTPCPL catch-all deceptive conduct likelihood of confusion", "bad faith statute remedy survives contract"], ['pa_','usreports','cap_us_','ca3_','courtlistener_']),
}


_REPORTER = __import__("re").compile(r"\b(\d{1,3})\s+(A\.\s?[23]d|A\.|Pa\.\s?Super\.|Pa\.|F\.\s?[234]d|F\.\s?Supp\.\s?[23]d|U\.S\.|S\.\s?Ct\.)\s+(\d{1,4})\b")


def _norm(x: str) -> str:
    return __import__("re").sub(r"[\s.'’_-]", "", (x or "").lower())


def cites_in_plus(text: str) -> list[tuple[str, dict]]:
    """Every authority the adverse filing names: AR.cites_in (case names / statutes / rules) PLUS reporter cites
    ('106 A.3d 48') — the reply wrote 'Bruno’s holding … 106 A.3d 48, 68' with no 'v.', so the name regex missed it and
    the bots quoted Bruno from the reply brief. Returns (label, pin-spec)."""
    out, seen = [], set()
    for c in AR.cites_in(text):
        kind, toks, fam = AR._cite_tokens(c)
        key = tuple(_norm(t) for t in toks) or (_norm(c),)
        if key in seen: continue          # 'Cowden v. Aetna' + 'See Cowden v. Aetna Cas.' are one authority
        seen.add(key); out.append((c, {"kind": kind, "toks": toks, "fam": fam}))
    for m in _REPORTER.finditer(text):
        slug = _norm(m.group(1) + m.group(2) + m.group(3))           # 106a3d48 — how corpus slugs carry the reporter
        if slug in seen: continue
        seen.add(slug); out.append((f"{m.group(1)} {m.group(2)} {m.group(3)}", {"kind": "reporter", "slug": slug}))
    return out[:16]


def _pins_plus(spec: dict, h: dict) -> bool:
    src = _norm(h.get("source") or ""); head = _norm((h.get("text") or "")[:400])
    if spec["kind"] == "reporter":
        return spec["slug"] in src
    if spec["kind"] == "case":                 # apostrophe-safe (D'Ambrosio), and a lone first party never pins alone when
        toks = [_norm(t) for t in spec["toks"]]  # two are known (Hart v. Arnold pinned every 'Hart v. United States')
        if not toks: return False
        if len(toks) >= 2: return all(t in src for t in toks) or all(t in head for t in toks) or (toks[0] in src and toks[1] in head)
        return toks[0] in src or toks[0] in head
    return AR._pins(spec["kind"], spec["toks"], spec["fam"], h)


def pull_cites_in_context(text: str, per: int = 3) -> list[dict]:
    """Phase-1 pin, surgical: for each authority the adverse filing names, retrieve with the SENTENCE that cites it (the
    proposition the opponent assigns to it) AND with the cite alone (two retrievals, union — one merged list let the
    context hits crowd out the cite hits), keep only chunks that pin to that authority."""
    import re as _re
    out, seen = [], set()
    for c, spec in cites_in_plus(text):
        i = text.find(c); ctx = text[max(0, i - 350): i + len(c) + 150] if i >= 0 else ""
        ctx = _re.sub(r"\s+", " ", ctx).strip()
        hits = [h for h in AR._retrieve([ctx or c], [], k=12) + AR._retrieve([c, f"{c} held"], [], k=12) if not h.get("error")]
        pinned, ids = [], set()
        for h in hits:
            if h.get("chunk_id") in seen or h.get("chunk_id") in ids or not _pins_plus(spec, h): continue
            ids.add(h["chunk_id"]); pinned.append(h)
            if len(pinned) >= per: break
        for h in pinned:
            seen.add(h["chunk_id"])
            out.append({"chunk_id": h["chunk_id"], "source": f"cite:{c} chunk={h['chunk_id']}", "part": h.get("part"),
                        "recital": bool(AR._RECITAL.search(h.get("text") or "")), "text": (h.get("text") or "")[:1400]})
        if not pinned:
            out.append({"chunk_id": None, "source": f"cite:{c}", "text": f"(NOT IN CORPUS by semantic+pin: {c} — no chunk names this authority; say so, do not characterize its holding)"})
    return out


M0_SECTIONS = {'authority-stack': ('WHAT THEIR AUTHORITIES ACTUALLY SAY', "PROVENANCE OF THE MOVANT'S AUTHORITIES"), 'construction': ('Premise Audit', 'DISTINCTIONS ON THIS RECORD'), 'definitions': ('Premise Audit', 'DISTINCTIONS ON THIS RECORD'), 'verbatim-rule': ("RULE DEFECTS IN THE MOVANT'S FILING", 'AUTHORITY THE ANALYSIS MUST CARRY'), 'duty': ('AM JUR ELEMENT CHAINS', 'WHEN THE BREACH BEGAN'), 'procedural': ("RULE DEFECTS IN THE MOVANT'S FILING", 'SWORN FACTS UNANSWERED'), 'stare-decisis': ('NON-BINDING AUTHORITY THE MOVANT RELIES ON', 'OMITTED CONTROLLING AUTHORITY'), 'dismissal-posture': ('Defects and Levers', 'Conclusion'), 'posture-vehicle': ('Defects and Levers', 'Conclusion'), 'remedy': ('AM JUR ELEMENT CHAINS', 'Defects and Levers'), 'pa-state-law': ("RULE DEFECTS IN THE MOVANT'S FILING", 'AUTHORITY THE ANALYSIS MUST CARRY'), 'jurisdiction': ('AM JUR ELEMENT CHAINS', 'RECORD FACTS ANSWERING THE OBJECTIONS'), 'capacity': ('AM JUR ELEMENT CHAINS', 'RECORD FACTS ANSWERING THE OBJECTIONS'), 'case-law': ('AUTHORITY THE ANALYSIS MUST CARRY', 'OMITTED CONTROLLING AUTHORITY')}
M0_MODULES = {'authority-stack': 'case_facts_refresh._verify_their_characterizations, cite_verifier_v2, authority_validity', 'construction': 'statutory_construction, canons_maxims, premise_audit', 'definitions': 'definition_engine/definition_gate, term_of_art_scanner, dictionary_resolver', 'verbatim-rule': 'case_facts_refresh._quoted_rules_audit, rules_index, statutory_terms_reader', 'duty': 'duty_engine, fiduciary engine', 'procedural': 'm0_rebuttal_gate, case_facts_refresh (SWORN FACTS UNANSWERED), procedural scan', 'stare-decisis': 'hierarchy_enforcer, binding_authority_lookup, court_hierarchy, stare_decisis', 'dismissal-posture': 'dismissal_analyzer, m0_posture_fit', 'posture-vehicle': 'litigation_posture, auto_reply (vehicle pick), m0_reply_gate', 'remedy': 'remedy_engine', 'pa-state-law': 'rules_index, pa statutes spine (m0_authority_spine)', 'jurisdiction': 'jurisdiction_check, four_gates, justiciability_detect', 'capacity': 'statute_capacity, capacity engine', 'case-law': 'authority_discovery, m0_authority_spine, caselaw_verify'}
CASES = (_eroot() / "obsidian_sync/cases")


def m0_baseline(slug: str, headings: tuple, cap: int = 9000) -> str:
    """m0's own verified analysis of THIS adverse filing (CASE_FACTS section ADVERSE-FILING DEFECTS + LEVERS, written
    by case_facts_refresh): the subsections this bot mirrors. The baseline scored 13/18 - the bot starts here."""
    import re as _re
    cf = CASES / slug / "CASE_FACTS.md"
    if not cf.exists(): return ""
    m = _re.search(r"^## ADVERSE-FILING DEFECTS & LEVERS(.*?)(?=^## |\Z)", cf.read_text(encoding="utf-8", errors="ignore"), _re.S | _re.M)
    if not m: return ""
    parts = _re.split(r"^(?=### |\*\*(?:Premise Audit|Defects and Levers|Conclusion|Filing analyzed))", m.group(1), flags=_re.M)
    out = []
    for h in headings:
        for prt in parts:
            if h.lower() in prt.strip().split("\n")[0].lower():
                out.append(prt.strip()[:cap]); break
    return "\n\n".join(out)


def _excerpt_block(hits: list[dict], cap: int) -> str:
    out = []
    for h in hits:
        if h.get("error"): continue
        src = h.get("source") or h.get("chunk_id") or "?"
        hdr = f"[source={src} part={h.get('part') or 'unknown'}{' RECITAL=yes' if h.get('recital') else ''}]"
        out.append(f"{hdr}\n{(h.get('text') or '')[:cap].strip()}")
    return "\n\n".join(out)


def case_header(slug: str, packet: dict) -> str:
    """The 'which matter is this' header every bot packet opens with.

    This was four lines of one matter's caption, posture and opponent-point gist, hardcoded —
    so a packet built for ANY other case told every bot it was opposing that matter's
    preliminary objections. Caption / court / docket / posture now come from the case's own
    profile, and the adverse filing's own points come from packet.json ('opponent_points'),
    which is where per-run content belongs. Nothing is defaulted from another matter."""
    try:
        pr = profile(slug)
    except CaseProfileGap as e:
        raise SystemExit(f"[brief] {e}")
    court = pr.get("court_name") or pr.get("court") or ""
    docket = pr.get("docket") or ""
    cap = pr.get("caption_short") or pr.get("caption") or pr.get("slug") or slug
    role = (pr.get("filer_role") or "Filer")
    cap_note = pr.get("filer_capacity") or ""
    bits = [f"Case: {cap}"]
    if court:
        bits.append(court)
    if docket:
        bits.append(f"No. {docket}")
    head = ", ".join(bits) + "."
    posture = (packet.get("posture") or "").strip()
    pts = (packet.get("opponent_points") or "").strip()
    lines = [head, f"{role}{f' ({cap_note})' if cap_note else ''} is the filer of record."]
    if posture:
        lines.append(posture)
    if pts:
        lines.append(f"Opponent points: {pts}")
    else:
        lines.append("Opponent points: (packet.json carries no 'opponent_points' — read them "
                     "off the adverse filing below; do not assume a posture).")
    return "\n".join(lines)


def build(run_dir: Path, bots: list[str], k: int, case: str = "") -> dict:
    p = json.loads((run_dir / "packet.json").read_text(encoding="utf-8"))
    # Which matter this run is for, in order of authority: the CLI, then the packet, then the
    # environment. NEVER a default to a real case — a silent default is how one matter's
    # CASE_FACTS ends up in another matter's bot packets.
    raw = (case or p.get("slug") or p.get("case_id") or p.get("case")
           or os.environ.get("BEEZLE_CASE") or os.environ.get("M2_CASE") or "").strip()
    if not raw:
        raise SystemExit(f"[brief] no case: pass --case, or set \"slug\" in "
                         f"{run_dir / 'packet.json'}, or export BEEZLE_CASE. "
                         f"Refusing to guess a matter.")
    try:
        slug = profile(raw)["slug"]
    except CaseProfileGap as e:
        raise SystemExit(f"[brief] {e}")
    hdr = case_header(slug, p)
    reply, record, slice_ = p.get("reply_brief", ""), p.get("po_verified_record", ""), p.get("record_slice", "")[:8000]   # lane-survival budget (long prefill x4 killed a lane)
    pinned = pull_cites_in_context(reply + "\n" + record, per=3)   # the opponent's own authorities, pinned IN CONTEXT, once
    pdir = run_dir / "packets"; pdir.mkdir(exist_ok=True)
    index = {}
    for bot in bots:
        panel, unit, question, queries, fams = BOTS[bot]
        sem = AR.retrieve(queries, fams, k=k) if queries else []
        notes = AR.graph_notes(f"{question} {' '.join(queries)}", k=4, max_chars=2400)      # the vault: what we already found, linked
        body = f"""# m2 bot packet — {bot} (panel {panel}, unit {unit})
{hdr}

## Your one question
{question}

## M0 BASELINE - verify, extend, correct (this is the floor; m0 organs: {M0_MODULES[bot]})
m0 already analyzed this filing (subsections below, from CASE_FACTS section ADVERSE-FILING DEFECTS & LEVERS). Do NOT
re-derive it. For each m0 point in your lane: (a) VERIFY it against the excerpts -> cell with `m0: confirmed`; (b) CORRECT it
where the excerpt says otherwise -> cell with `m0: corrected` + what was wrong; (c) ADD what m0 missed -> cell with `m0: new`.
{m0_baseline(slug, M0_SECTIONS[bot]) or '(m0 wrote no subsection for this lane - every cell is m0: new)'}

## CELL RULES (hard; the skeptic rejects on these)
{RULES}
## Output
Write `{run_dir}/cells_{bot}.json` = {{"bot": "{bot}", "cells": [ {{claim, status: GROUNDED|CONFLICT|GAP|NOT_APPLICABLE, level(int 1-7),
finding(<=80 words), m0: confirmed|corrected|new, proposition(1-3 court-facing sentences stating OUR relief), authorities:[{{cite, quote(verbatim >=8 words
from an excerpt below), source(excerpt source id), verified(bool), speaker: court|party}}], gap, fix, record_facts:[verbatim
record lines]}} ]}} and return the same JSON. Verify a quote (optional, <=8 calls total) ONLY with the semantic tool mcp__beezle__search_chunks(query=<the proposition in
plain words + the case name>, n_results=5) and check the hit's `source` names the authority. NEVER curl /search_text (keyword
FTS — it returns junk and produces false GAPs). FORBIDDEN: web_search/web_extract/websites; scripts/mcp.sh does not exist.

## The adverse filing (reply brief)
{reply}

## PO verified record (the POs' propositions, verified)
{record}

## Record slice (CASE_FACTS — quote lines verbatim as record_facts)
{slice_}

## EXCERPTS A — the opponent's named authorities, pinned (semantic + pin; part-filtered)
{_excerpt_block(pinned, 900)}

## EXCERPTS B — retrieved for your question (semantic, holdings only, your corpus families: {', '.join(fams)})
{_excerpt_block(sem, 900) or '(none — your question is answered from EXCERPTS A and the record)'}

## PRIOR GROUNDED NOTES (Obsidian graph — verified earlier; cite their [[slug]]; re-verify any quote before citing it)
{notes or '(no prior notes for this question)'}
"""
        (pdir / f"{bot}.md").write_text(body, encoding="utf-8")
        index[bot] = {"packet": str(pdir / f"{bot}.md"), "panel": panel, "out": str(run_dir / f"cells_{bot}.json"),
                      "chars": len(body), "pinned": len(pinned), "semantic": len([h for h in sem if not h.get("error")]), "notes": len(notes)}
        print(f"[brief] {bot:18s} {panel:9s} packet {len(body)//1000:3d} KB  pinned {len(pinned)}  semantic {index[bot]['semantic']}  vault-notes {len(notes)} chars")
    (pdir / "INDEX.json").write_text(json.dumps(index, indent=1), encoding="utf-8")
    return index


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("run_dir"); ap.add_argument("--bots", default=""); ap.add_argument("--k", type=int, default=6)
    ap.add_argument("--case", default="", help="case id / slug / docket (overrides packet.json 'slug'); no default")
    a = ap.parse_args(argv)
    bots = [b.strip() for b in a.bots.split(",") if b.strip()] or list(BOTS)
    bad = [b for b in bots if b not in BOTS]
    if bad: print(f"[brief] unknown bots: {bad}", file=sys.stderr); return 2
    build(Path(a.run_dir), bots, a.k, a.case); return 0


if __name__ == "__main__":
    sys.exit(main())
