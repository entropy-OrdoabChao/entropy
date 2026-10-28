#!/usr/bin/env python3
"""m0_quality_review.py — the Am Jur LITMUS organ (m0 ANALYSIS side).

The LLM is the JUDGE here, per design doctrine: deterministic steps are guardrails,
never a replacement for comprehension. This organ reads an assembled draft and
judges it against the Am Jur Pleading & Practice Forms craft standard retrieved
from the corpus — structure, paragraph discipline, element pleading, prayer form,
verification — and returns a verdict + specific, actionable defects.

Usage:
  python3 -m pipeline.m0_quality_review <draft.md> [--type complaint] [--court PA_CP_PHILA]
  (env: BEEZLE_LLAMA_URL full-path chat endpoint; BEEZLE_CF_MODEL model id —
   same per-organ routing as case_facts_refresh)

Verdict JSON is printed and saved next to the draft as <stem>.quality.json.
Exit 0 = PASSES the litmus; exit 2 = defects found (caller decides severity).
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import json
import os
import re
import sqlite3
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHUNK_DB = (_eroot() / "corpus/chunk_text.db")

LLM_URL = os.environ.get("BEEZLE_LLAMA_URL", "http://127.0.0.1:8000/v1/chat/completions")
MODEL = os.environ.get("BEEZLE_CF_MODEL", "qwen3.8-27b-exl3")

# Craft sources in priority order — the FILING-craft shelf, not substantive titles.
CRAFT_SOURCES = (
    "treatise_american_jurisprudence_pleading_and_practice_forms_annotated",
    "amjur_legal_forms_summary",
    "amjur2d_affidavits",
    "amjur_proof_of_facts_expert_testimony",
)

# The whole Am Jur shelf counts as craft authority for the SEMANTIC lane —
# general pleading discipline lives in american_jurisprudence / amjur_and_dictionaries
# (Black's pleading definitions), not just the P&P Forms treatise.
_AMJUR_MARKERS = ("amjur", "american_jurisprudence", "am_jur", "am-jur",
                  "proof_of_facts", "legal_forms")

MCP_URL = os.environ.get("BEEZLE_MCP_URL", "http://localhost:8200/mcp")

_CRAFT_QUERIES = {
    "complaint": (
        "complaint form numbered allegations one material fact per paragraph "
        "caption parties counts incorporation by reference prayer for relief verification",
        "pleading sufficiency stating cause of action elements tied to facts "
        "not conclusory specificity of allegations",
    ),
    "petition": (
        "petition form allegations caption prayer verification",
        "pleading sufficiency elements specificity",
    ),
    "motion": (
        "motion form notice grounds proposed order certificate of service",
        "motion practice requisites stating grounds with particularity",
    ),
    "opposition": (
        "opposition response memorandum grounds answering motion",
        "responsive pleading meeting each ground",
    ),
}


def _craft_semantic(filing_type: str, budget: int) -> list[str]:
    """PRIMARY lane: semantic search_chunks via the :8200 corpus MCP —
    the same comprehension-grade retrieval m0 itself uses. Filtered to the
    Am Jur shelf so the judge reads craft authority, not substantive noise."""
    queries = _CRAFT_QUERIES.get(
        filing_type, ("pleading form allegations requisites", "pleading sufficiency"))
    ws = re.compile(r"\s+")
    out, used, seen = [], 0, set()
    for q in queries:
        if used >= budget:
            break
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "search_chunks",
                              "arguments": {"query": q, "n_results": 12}}}
        req = urllib.request.Request(
            MCP_URL, json.dumps(payload).encode(),
            {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream"})
        try:
            r = urllib.request.urlopen(req, timeout=120).read().decode()
        except Exception:
            return out  # lane down — caller falls back to LIKE guardrail
        for line in r.splitlines():
            if not line.startswith("data:"):
                continue
            try:
                j = json.loads(line[5:])
                txt = j.get("result", {}).get("content", [{}])[0].get("text", "")
                hits = json.loads(txt).get("result", [])
            except Exception:
                continue
            for h in hits:
                src = (h.get("source") or "").lower()
                if not any(m in src for m in _AMJUR_MARKERS):
                    continue
                seg = ws.sub(" ", h.get("text") or "")[:800]
                key = seg[:120]
                if not seg or key in seen or used >= budget:
                    continue
                seen.add(key)
                out.append(f"[{h.get('source')}] ...{seg}...")
                used += len(seg)
    return out


def _craft_excerpts(filing_type: str, budget: int = 9000) -> str:
    """Pull the craft-standard excerpts the judge reads beside the draft.
    Semantic :8200 lane PRIMARY (comprehension-grade, whole Am Jur shelf);
    keyword LIKE over the P&P shelf is the deterministic GUARDRAIL fallback."""
    sem = _craft_semantic(filing_type, budget)
    remaining = budget - sum(len(s) for s in sem)
    if sem and remaining < 1000:
        return "\n\n".join(sem)[:budget]
    # Guardrail lane: deterministic LIKE over the P&P shelf — fills whatever
    # budget the semantic lane left (or all of it if :8200 is down).
    kw = {
        "complaint": ("complaint", "allegation", "count", "prayer", "caption",
                      "numbered", "verification", "demand"),
        "petition": ("petition", "allegation", "prayer", "caption", "verification"),
        "motion": ("motion", "notice", "grounds", "order", "certificate"),
        "opposition": ("opposition", "response", "grounds", "memorandum"),
    }.get(filing_type, ("pleading", "allegation", "form"))
    out, used = list(sem), sum(len(s) for s in sem)
    try:
        db = sqlite3.connect(f"file:{CHUNK_DB}?mode=ro", uri=True)
        cur = db.cursor()
        ws = re.compile(r"\s+")
        for src in CRAFT_SOURCES:
            for k in kw:
                if used >= budget:
                    break
                cur.execute(
                    "SELECT text FROM chunk_text WHERE source=? AND text LIKE ? LIMIT 2",
                    (src, f"%{k}%"))
                for (t,) in cur.fetchall():
                    t2 = ws.sub(" ", t)
                    i = t2.lower().find(k)
                    seg = t2[max(0, i - 150):i + 650]
                    if seg and seg not in "".join(out):
                        out.append(f"[{src}] ...{seg}...")
                        used += len(seg)
        db.close()
    except Exception as e:
        out.append(f"(craft retrieval unavailable: {e})")
    return "\n\n".join(out)[:budget]


COURT_PROFILES = {
    "PA_CP_PHILA": (
        "COURT-SPECIFIC RULES THAT OVERRIDE GENERIC INSTINCTS (Pennsylvania Court of "
        "Common Pleas): (1) Pa.R.C.P. 1018.1 REQUIRES a Notice to Defend at the FRONT "
        "of every complaint, before the body — its presence at the top is CORRECT, do "
        "not flag it. (2) Pa.R.C.P. 1024 verification at the end is required — correct. "
        "(3) A definitions schedule (Schedule A) and a PROOF OF FACTS section are this "
        "filer's LOCKED house anatomy — judge their internal quality (clarity, accuracy) "
        "but do NOT direct their removal. (4) Fact pleading (Pa.R.C.P. 1019(a)) demands "
        "material facts, more than federal notice pleading — specificity is a virtue. "
        "(5a) STATUTE-ELEMENT DISCIPLINE: 42 Pa.C.S. § 8371 is statutory insurance BAD "
        "FAITH — its elements are lack of reasonable basis and knowing/reckless disregard. "
        "'Unfair or deceptive act or practice in trade or commerce' belongs to the UTPCPL "
        "(73 P.S. § 201-1 et seq.) ONLY. Never direct § 8371 counts to plead UTPCPL "
        "elements or vice versa. "
        "(5) 'Not to exceed $75,000' damage caps are deliberate jurisdictional strategy "
        "— never direct their removal The cap is deliberately NOT anchored to any single statute \u2014 it is a global ceiling on the whole judgment for federal-removal math; never direct adding a statutory basis to it. The WHEREFORE's first numbered item is the CEILING clause — it lumps 'statutory, treble, and punitive damages' deliberately because it fixes the global total, not the per-statute bases; the statute-by-statute remedy specification lives in the SUBSEQUENT prayer items (\u00a7 8371 item; UTPCPL \u00a7 201-9.2(a) item). Read the prayer as a whole before flagging: never direct restating per-statute remedies inside the ceiling clause when adjacent items already itemize them. The FLOOR-plus-CEILING pairing in that item ('not less than $161.36 ... provided, however ... not to exceed $75,000.00') is LOCKED strategy, not a contradiction: the floor is the minimum Defendant's own confirmation admits as due, the ceiling is the elected jurisdictional maximum for the whole action; a floor below a ceiling is arithmetically coherent — never flag their coexistence or direct removal of either. (7) Dated single-fact averments that plead one temporal relationship each (refusal date; days-after-receipt; days-before-issuance) are the required 1029(b) form here \u2014 do not direct merging or further splitting of them, and do not flag them on ANY axis (including readability): their staccato one-fact-per-paragraph rhythm IS the required form, not a style defect. (7b) The PARTIES entity-identification block (multiple corporate labels on one policy; the prior-case caption-correction fact; the ABC ENTITIES naming justification) is LOCKED strategy: the prior-action appearance fact (E.D. Pa. 2:26-cv-00789 caption correction) is PLAINTIFF'S OWN direct experience pleaded as a fact — never flag citing that case or direct its removal; never direct deleting the ABC ENTITIES justification paragraphs. Craft improvements INSIDE those paragraphs (specificity, factual phrasing) are fair game; existence/strategy is not. (6) FURTHER LOCKED anatomy (never flag for existing, "
        "only for internal defects like a broken cite or wrong exhibit): the DEFINITIONS & "
        "AUTHORITIES footnote block; one numbered paragraph PER cancellation confirmation "
        "(Pa.R.C.P. 1029(b) specific-denial strategy — never direct consolidation into a "
        "table); 'ABC ENTITIES 1-5' fictitious-defendant captioning (affiliate-entity "
        "practice); the Q&A-form PROOF OF FACTS section (a deliberate concession device, "
        "not improper pleading — never direct its removal); the signature block's "
        "sui juris + '13 Pa.C.S. § 1308 / U.C.C. § 1-308' reservation line (locked "
        "signature standard — never direct its removal or relocation); a blank "
        "'Case No. ________' (correct pre-filing — the court assigns it)."),
}

PROMPT = """You are a senior legal drafting reviewer. Judge the DRAFT below against the
Am Jur Pleading & Practice Forms craft standard (excerpts provided) and general
good-pleading discipline. You are the LITMUS — judge like an experienced editor
reading a filing the moment before it goes out the door.

Judge on these axes (score each 1-5, 5 = filing-grade):
1. structure: caption -> parties -> jurisdiction/venue -> facts -> counts -> prayer -> verification, in order, nothing missing for a {ftype}
2. paragraph_discipline: numbered paragraphs, ONE material fact each, dates/amounts/actors specific, no compound run-ons
3. element_pleading: every count states its elements and ties each element to a pleaded fact (not conclusory labels)
4. incorporation: counts properly incorporate prior paragraphs by reference
5. prayer: special prayer (specific relief) + general prayer ("such other relief..."), consistent with the counts
6. readability: reads like a lawyer wrote it — plain, declarative, professional; no template smell, no orphan headings, no unresolved placeholders
7. record_grounding: quotes and exhibit references woven into the narrative naturally, not bolted on
8. demand_discipline: WHERE the draft demands a response, compliance, or justification from any actor (demand tables, notices, challenges to orders), it must carry ALL THREE locked elements: (a) SUBSTANTIVE-FACTS requirement — responses must engage the specific documents/dates/amounts/instruments, unengaged facts stand admitted (FRCP 8(b)(6) / Pa.R.C.P. 1029(b)); (b) AUTHORITY-DISCLOSURE — the responder and any order relied on must identify the enacted authority (statute/PL origin, rule, or canon) for the act (5 U.S.C. 706(2)(C)), with page-and-line identification demanded for referral-style rulings; (c) WAIVER SYMMETRY — Johnson v. Zerbst definition quoted, unraised defenses waived symmetrically, no waiver presumed against plaintiff (1 Pa.C.S. 1308). Score 5 if all present where applicable (or draft has no demand posture — score 5 N/A); score <=3 if any missing where the posture calls for it.
9. defined_class: EVERY statute or rule the draft pleads must have its reach resolved to the parties — the draft shows (explicitly where coverage could be contested, implicitly where obvious) WHO the provision reaches per its own definitions or the general definitions (1 U.S.C. 1 / 1 Pa.C.S. 1991), and in WHAT CAPACITY each party is reached (individual, corporate entity, officer, fiduciary, surety). A count pleading a statute against a party with no visible defined-class fit, or resting on an unexamined jurisdictional assumption (no enacting authority + forum + capacity nexus), scores <=3. Score 5 if every pleaded provision's class/capacity is sound (or draft pleads no statutes — score 5 N/A).

Return ONLY JSON:
{{"verdict": "PASS" | "DEFECTS",
  "scores": {{"structure": n, "paragraph_discipline": n, "element_pleading": n,
             "incorporation": n, "prayer": n, "readability": n, "record_grounding": n,
             "demand_discipline": n, "defined_class": n}},
  "defects": [{{"axis": "...", "where": "quoted snippet or section name",
               "problem": "what fails the standard", "fix": "specific rewrite instruction"}}],
  "strengths": ["..."]}}

A verdict of PASS requires every axis >= 4 and zero defects of substance.
Do NOT invent facts, citations, or exhibits; judge only what is on the page.

=== COURT PROFILE (local rules that OVERRIDE generic instincts) ===
{court_profile}

=== AM JUR CRAFT EXCERPTS (the standard) ===
{craft}

=== DRAFT ({ftype}) ===
{draft}
"""


def _call_llm(prompt: str, timeout: int = 420) -> str:
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 6000,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    req = urllib.request.Request(
        LLM_URL, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        d = json.loads(resp.read())
    msg = d["choices"][0]["message"]
    return msg.get("content") or msg.get("reasoning") or ""


def review(draft_path: str, filing_type: str = "complaint",
           court: str = "") -> dict:
    p = Path(draft_path)
    dx = p.with_suffix(".docx")
    if dx.exists():
        try:
            import docx as _docx
            from docx.table import Table as _Table
            from docx.text.paragraph import Paragraph as _Para
            _d = _docx.Document(str(dx))
            _parts = []
            for _el in _d.element.body:
                if _el.tag.endswith("}p"):
                    _t = _Para(_el, _d).text
                    if _t.strip():
                        _parts.append(_t)
                elif _el.tag.endswith("}tbl"):
                    for _row in _Table(_el, _d).rows:
                        _cells = [c.text.strip() for c in _row.cells]
                        if any(_cells):
                            _parts.append(" | ".join(_cells))
            draft = "\n".join(_parts)
            p = dx  # judge the filing view, not the pre-render md
        except Exception:
            draft = p.read_text(encoding="utf-8", errors="replace")
    else:
        draft = p.read_text(encoding="utf-8", errors="replace")
    # bound the draft for one window; heads+tails if huge (the judge reads shape + samples)
    if len(draft) > 42000:
        # FIRST: elide locked-anatomy appendix sections (pinned from review anyway)
        # so the pleading body — counts, SOF, prayer — always stays whole.
        import re as _re
        for _sec in ("SCHEDULE A — DEFINITIONS", "PROOF OF FACTS",
                     "DEFINITIONS & AUTHORITIES", "INDEX OF EXHIBITS"):
            if len(draft) <= 30000:
                break
            _m = _re.search(r"^#+ .*" + _re.escape(_sec) + r".*$", draft, _re.M)
            if not _m:
                continue
            _start = _m.end()
            _nxt = _re.search(r"^#+ ", draft[_start:], _re.M)
            _end = _start + (_nxt.start() if _nxt else len(draft) - _start)
            _body = draft[_start:_end]
            if len(_body) > 800:
                draft = (draft[:_start]
                         + "\n\n[locked section present in the full draft; body elided "
                         "from the review window — do not flag its absence or judge it]\n\n"
                         + draft[_end:])
        if len(draft) > 42000:
            _cut = draft.rfind("\n", 0, 18000)
            _cut2 = draft.find("\n", len(draft) - 9000)
            draft = (draft[:_cut if _cut > 12000 else 18000]
                     + "\n\n[REVIEW WINDOW NOTE: the middle of the draft is elided here to fit "
                     "the review window. Text before/after this marker ends/starts at paragraph "
                     "boundaries of the FULL draft — the intervening numbered paragraphs EXIST "
                     "in the full draft; never report the numbering gap, the seam, or the elided "
                     "span as missing content or a structural defect.]\n\n"
                     + draft[_cut2 + 1 if _cut2 > -1 else len(draft) - 9000:])
    craft = _craft_excerpts(filing_type)
    prompt = PROMPT.format(ftype=filing_type, craft=craft, draft=draft,
                           court_profile=COURT_PROFILES.get(court, "(none — judge on general standards)"))
    raw = _call_llm(prompt)
    m = re.search(r"\{.*", raw, re.S)
    data = None
    if m:
        txt = m.group(0)
        # brace-balance salvage: close any truncated arrays/objects
        for tail in ("", "}", "]}", "}]}", "\"}]}", "\"}]}}"):
            try:
                data = json.loads(txt + tail)
                break
            except Exception:
                continue
        if data is None:
            # trim to last complete object in "defects" and retry
            cut = txt.rfind("},")
            if cut > 0:
                for tail in ("]}", "]}}"):
                    try:
                        data = json.loads(txt[:cut + 1] + tail)
                        break
                    except Exception:
                        continue
    if data is None:
        data = {"verdict": "ERROR", "raw": raw[:800]}
    data["draft"] = str(p)
    data["filing_type"] = filing_type
    data["court"] = court
    data["model"] = MODEL
    out = Path(str(p) + ".quality.json")
    out.write_text(json.dumps(data, indent=1), encoding="utf-8")
    v = data.get("verdict")
    scores = data.get("scores") or {}
    print(f"[m0_quality_review] {p.name}: {v} "
          f"({', '.join(f'{k}={vv}' for k, vv in scores.items())})")
    for d in (data.get("defects") or [])[:10]:
        print(f"  DEFECT [{d.get('axis')}] {d.get('problem')} -> {d.get('fix')}")
    return data


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 1
    ftype = "complaint"
    court = ""
    for i, a in enumerate(sys.argv[1:]):
        if a == "--type" and i + 2 <= len(sys.argv[1:]):
            ftype = sys.argv[1:][i + 1]
        if a == "--court" and i + 2 <= len(sys.argv[1:]):
            court = sys.argv[1:][i + 1]
    data = review(args[0], ftype, court)
    return 0 if data.get("verdict") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
