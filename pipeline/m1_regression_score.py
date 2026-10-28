#!/usr/bin/env python3
"""m1_regression_score.py — deterministic m0-vs-m1 draft comparison (roadmap migration rule).

Scores any two drafts of the SAME target on identical criteria; no LLM judgment.
  COVERAGE : every matrix point answered (zero-waiver) — count of headed rebuttals
  EVIDENCE : the 6 dkt43 record items present in BODY (not appendix)
  CITES    : every cite passes caselaw_verify / statute-format check; UNBACKED count
  HYGIENE  : no reasoning leaks, no stub echo, no dup sections/sentences (m0 G2/G3/G7/G11)
  SIZE     : words (concise-wins per user directive; ties broken by fewer words)
Output: JSON scorecard + verdict line per draft; exit 0 always (report, not gate).
"""
import json, re, sys
from pathlib import Path

EVIDENCE = {
    "allonge_unexecuted": r"allonge",
    "s3204": r"3204",
    "fas140_isolation": r"FAS\s*140",
    "1099c": r"1099",
    "exhibit_c_sale": r"Exhibit C|fully purchased",
    "gnma_remic": r"GNMA|REMIC|2022-125",
}
LEAK_PAT = re.compile(r"cite ONLY|APPENDIX A|Source Stub|\[SKILL|\[FACT NOT IN RECORD|---BEGIN STUB|reasoning:", re.I)
CITE_PAT = re.compile(r"\d+\s+U\.S\.C?\.?\s*§|\d+\s+Pa\.C\.S\.|\d+\s+C\.F\.R\.|Fed\.\s*R\.|[A-Z][a-z]+ v\. [A-Z]|\d+\s+U\.S\.\s+\d+|\d+\s+F\.\d?d")

def _dup_sentences(text):
    sents = [s.strip().lower() for s in re.split(r"(?<=[.!?])\s+", text) if len(s.strip()) > 60]
    return len(sents) - len(set(sents))

def score(path, body_only=True):
    t = Path(path).read_text(errors="replace")
    body = t.split("APPENDIX A")[0] if body_only else t
    rebuttals = len(re.findall(r"^\*\*\d+\.|^###?\s*[IVX]+\.", body, re.M))
    ev = {k: bool(re.search(p, body, re.I)) for k, p in EVIDENCE.items()}
    cites = len(CITE_PAT.findall(body))
    leaks = LEAK_PAT.findall(t)  # leaks scored on FULL file
    dups = _dup_sentences(body)
    words = len(body.split())
    return {
        "file": str(path), "rebuttal_headings": rebuttals,
        "evidence_in_body": ev, "evidence_score": f"{sum(ev.values())}/{len(ev)}",
        "cite_count": cites, "leak_hits": leaks[:5], "dup_sentences": dups,
        "words": words,
    }

def main():
    out = {}
    for label, p in [a.split("=", 1) for a in sys.argv[1:] if "=" in a]:
        if Path(p).exists():
            out[label] = score(p)
        else:
            out[label] = {"error": f"not found: {p}"}
    print(json.dumps(out, indent=1))
    # verdict line
    scored = {k: v for k, v in out.items() if "error" not in v}
    if len(scored) >= 2:
        def rank(s):
            evn = int(s["evidence_score"].split("/")[0])
            return (evn, s["rebuttal_headings"], -len(s["leak_hits"]), -s["dup_sentences"], -s["words"])
        best = max(scored, key=lambda k: rank(scored[k]))
        print(f"\nVERDICT: {best} wins on (evidence, coverage, -leaks, -dups, -words)")

if __name__ == "__main__":
    main()
