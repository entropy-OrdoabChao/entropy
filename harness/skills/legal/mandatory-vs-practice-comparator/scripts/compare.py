#!/usr/bin/env python3
"""
mandatory-vs-practice-comparator
Extracts mandatory duties from statute/rule text and tests court practice for deviations.
"""
import argparse
import json
import re
import sys
from pathlib import Path

MANDATORY_VERBS = [
    r"\bshall\b",
    r"\bmust\b",
    r"\brequired\b",
    r"\bprohibited\b",
    r"\bmay not\b",
    r"\bis entitled to\b",
    r"\bhas the right to\b",
    r"\bnecessary\b",
]
DISCRETIONARY_VERBS = [r"\bmay\b", r"\bshould\b", r"\bappropriate\b", r"\bdiscretion\b"]

DUTY_OWNER_HINTS = {
    "court": ["the court", "court shall", "court must", "the judge"],
    "clerk": ["the clerk", "clerk shall", "clerk must"],
    "party": ["the party", "movant", "petitioner", "respondent", "plaintiff", "defendant"],
}


def extract_duties(text):
    duties = []
    sentences = re.split(r"(?<=[.;])\s+", text)
    duty_id = 0
    for s in sentences:
        for verb_pat in MANDATORY_VERBS:
            m = re.search(verb_pat, s, re.IGNORECASE)
            if m:
                duty_id += 1
                # Identify owner
                owner = "unspecified"
                s_lower = s.lower()
                for k, hints in DUTY_OWNER_HINTS.items():
                    if any(h in s_lower for h in hints):
                        owner = k
                        break
                duties.append({
                    "duty_id": duty_id,
                    "verbatim": s.strip(),
                    "duty_type": "MANDATORY",
                    "owed_by": owner,
                    "verb": m.group(0),
                })
                break
    return duties


def detect_deviation(duty, practice_text):
    """
    Crude heuristic: if the duty's key noun is absent from practice, suspect deviation.
    Hermes (the agent) should refine this with semantic comparison.
    """
    practice_lower = practice_text.lower()
    duty_lower = duty["verbatim"].lower()

    # Heuristic markers of non-compliance
    omission_markers = [
        ("findings of fact", "no findings"),
        ("conclusions of law", "no conclusions"),
        ("hearing", "without hearing"),
        ("notice", "without notice"),
        ("opportunity to be heard", "denied opportunity"),
        ("written order", "summary"),
        ("statement of reasons", "without explanation"),
    ]
    for required, omission in omission_markers:
        if required in duty_lower and (omission in practice_lower or required not in practice_lower):
            return omission

    # Negative compliance indicators
    if "summary denial" in practice_lower or "without explanation" in practice_lower:
        return "summary action without compliance with duty"

    return None


def compare(statute_name, statute_text, practice_text, jurisdiction):
    duties = extract_duties(statute_text)
    deviations = []

    for d in duties:
        dev = detect_deviation(d, practice_text)
        if dev:
            deviations.append({
                "duty_id": d["duty_id"],
                "duty_verbatim": d["verbatim"],
                "deviation": dev,
                "consequence_statutory": f"violation of {statute_name} — appellate reversal",
                "consequence_constitutional": "potential due process violation — see due-process-violation-engine",
                "binding_authority_enforcing_shall": [
                    "Lexecon Inc. v. Milberg Weiss, 523 U.S. 26 (1998)",
                    "Anderson v. Yungkau, 329 U.S. 482 (1947)",
                    "Kingdomware Techs. v. United States, 579 U.S. 162 (2016)",
                ],
            })

    return {
        "statute": statute_name,
        "jurisdiction": jurisdiction,
        "verbatim_text": statute_text[:2000],
        "duties_extracted": duties,
        "practice_observed": practice_text[:2000],
        "deviations": deviations,
        "deviation_count": len(deviations),
        "court_must_explain_or_be_reversed": len(deviations) > 0,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--statute", required=True, help="Citation, e.g. 'Fed. R. Civ. P. 52(a)'")
    p.add_argument("--statute-text-file", required=True)
    p.add_argument("--practice-file", required=True)
    p.add_argument("--jurisdiction", required=True)
    p.add_argument("--output", default="/tmp/mandatory_compare.json")
    args = p.parse_args()

    statute_text = Path(args.statute_text_file).read_text()
    practice_text = Path(args.practice_file).read_text()

    result = compare(args.statute, statute_text, practice_text, args.jurisdiction)
    Path(args.output).write_text(json.dumps(result, indent=2))
    print(f"WROTE: {args.output}")
    print(f"DUTIES: {len(result['duties_extracted'])}")
    print(f"DEVIATIONS: {result['deviation_count']}")


if __name__ == "__main__":
    main()
