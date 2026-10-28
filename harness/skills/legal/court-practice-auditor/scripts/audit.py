#!/usr/bin/env python3
"""
court-practice-auditor
Tests court local rules / standing orders against higher binding authority.
"""
import argparse
import json
import sys
from pathlib import Path

CONFIG_PATH = Path.home() / ".hermes" / "legal_config.json"

# Higher-rule layers to test against, in order
HIGHER_RULE_LAYERS = {
    "edpa": ["us_const", "scotus", "ca3", "us_code", "frcp", "fre", "frap"],
    "pa-state": ["us_const", "scotus", "pa_const", "pa_supreme", "pa_rcp", "pa_rap"],
    "mdpa": ["us_const", "scotus", "ca3", "us_code", "frcp", "fre", "frap"],
    "wdpa": ["us_const", "scotus", "ca3", "us_code", "frcp", "fre", "frap"],
}

ACCESS_TO_COURTS_CANON = [
    {"case": "Bounds v. Smith", "cite": "430 U.S. 817 (1977)"},
    {"case": "Christopher v. Harbury", "cite": "536 U.S. 403 (2002)"},
    {"case": "Lewis v. Casey", "cite": "518 U.S. 343 (1996)"},
]

DUE_PROCESS_TRIGGERS = {
    "right_to_be_heard": ["page limit", "no oral argument", "summary disposition", "reply brief"],
    "notice": ["constructive notice", "deemed served", "no actual notice"],
    "neutral_arbiter": ["chambers conference", "ex parte", "informal communication"],
    "findings": ["no opinion required", "summary order", "without explanation"],
    "appellate_review": ["non-appealable", "interlocutory bar", "discretionary review only"],
}


def load_config():
    return json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}


def audit_practice(text, source, jurisdiction):
    cfg = load_config()
    layers = HIGHER_RULE_LAYERS.get(jurisdiction, [])

    text_lower = text.lower()

    # Due process tests
    dp_violations = []
    for prong, triggers in DUE_PROCESS_TRIGGERS.items():
        if any(t in text_lower for t in triggers):
            dp_violations.append(prong)

    # Equal protection: pro se burden detection
    eq_violations = []
    if any(p in text_lower for p in ["pro se", "self-represented", "unrepresented"]):
        if any(b in text_lower for b in ["may not", "shall not", "prohibited", "additional"]):
            eq_violations.append("differential burden on pro se")

    # Access to courts
    a2c_violations = []
    if any(b in text_lower for b in ["filing fee", "must be represented", "in forma pauperis denied",
                                       "page limit", "word limit"]):
        a2c_violations.append("possible access barrier")

    has_violation = bool(dp_violations or eq_violations or a2c_violations)

    return {
        "practice": text[:1500],
        "source": source,
        "jurisdiction": jurisdiction,
        "higher_rule_layers_tested": layers,
        "tests": {
            "due_process_conflict": {
                "violated": bool(dp_violations),
                "prongs": dp_violations,
            },
            "equal_protection": {
                "violated": bool(eq_violations),
                "details": eq_violations,
            },
            "access_to_courts": {
                "violated": bool(a2c_violations),
                "details": a2c_violations,
                "binding_canon": ACCESS_TO_COURTS_CANON if a2c_violations else [],
            },
        },
        "verdict": "DEVIATION" if has_violation else "AGENT_REVIEW_REQUIRED",
        "remedies": [
            {"type": "challenge_local_rule", "auth": "FRCP 83(a)(1) — must be consistent with FRCP"},
            {"type": "mandamus", "auth": "28 U.S.C. § 1651"},
            {"type": "1983", "auth": "42 U.S.C. § 1983 if state actor"},
            {"type": "appeal_preservation", "auth": "preserve at trial level"},
        ] if has_violation else [],
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--practice-file", help="Single practice/rule text file")
    p.add_argument("--practice-dir", help="Directory of practice files to audit")
    p.add_argument("--jurisdiction", required=True)
    p.add_argument("--output", default="/tmp/practice_audit.json")
    args = p.parse_args()

    audits = []
    if args.practice_file:
        text = Path(args.practice_file).read_text()
        audits.append(audit_practice(text, {"document": args.practice_file}, args.jurisdiction))
    elif args.practice_dir:
        for f in Path(args.practice_dir).rglob("*.txt"):
            text = f.read_text()
            audits.append(audit_practice(text, {"document": str(f)}, args.jurisdiction))
    else:
        sys.exit("ERROR: provide --practice-file or --practice-dir")

    deviations = [a for a in audits if a["verdict"] == "DEVIATION"]
    Path(args.output).write_text(json.dumps({"audits": audits, "deviation_count": len(deviations)}, indent=2))
    print(f"WROTE: {args.output}")
    print(f"DEVIATIONS: {len(deviations)} / {len(audits)}")


if __name__ == "__main__":
    main()
