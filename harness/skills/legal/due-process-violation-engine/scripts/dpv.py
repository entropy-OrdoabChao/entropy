#!/usr/bin/env python3
"""
due-process-violation-engine
Tests court actions against the binding due process canon.
Outputs structured violations with binding authority + remedy mapping.
"""
import argparse
import json
import sys
from pathlib import Path

CONFIG_PATH = Path.home() / ".hermes" / "legal_config.json"

DUE_PROCESS_CANON = {
    "notice": [
        {
            "case": "Mullane v. Central Hanover Bank",
            "cite": "339 U.S. 306 (1950)",
            "rule": "notice reasonably calculated to apprise interested parties",
            "violation_class": "fundamental",
        }
    ],
    "hearing": [
        {
            "case": "Goldberg v. Kelly",
            "cite": "397 U.S. 254 (1970)",
            "rule": "evidentiary hearing required before deprivation of statutory entitlement",
            "violation_class": "fundamental",
        },
        {
            "case": "Mathews v. Eldridge",
            "cite": "424 U.S. 319 (1976)",
            "rule": "three-factor balancing test for procedural due process",
            "violation_class": "fundamental",
        },
    ],
    "neutral_arbiter": [
        {
            "case": "Caperton v. A.T. Massey Coal Co.",
            "cite": "556 U.S. 868 (2009)",
            "rule": "due process requires recusal when probability of bias is too high",
            "violation_class": "structural",
        },
        {
            "case": "Tumey v. Ohio",
            "cite": "273 U.S. 510 (1927)",
            "rule": "judge with pecuniary interest violates due process — structural",
            "violation_class": "structural",
        },
        {
            "case": "In re Murchison",
            "cite": "349 U.S. 133 (1955)",
            "rule": "judge cannot serve as both prosecutor and judge",
            "violation_class": "structural",
        },
    ],
    "right_to_be_heard": [
        {
            "case": "Hamdi v. Rumsfeld",
            "cite": "542 U.S. 507 (2004)",
            "rule": "meaningful opportunity to contest factual basis of deprivation",
            "violation_class": "fundamental",
        }
    ],
    "findings_on_record": [
        {
            "case": "Fed. R. Civ. P. 52(a)",
            "cite": "FRCP 52(a)",
            "rule": "court must find facts specially and state conclusions of law separately",
            "violation_class": "fundamental",
        }
    ],
    "appellate_review": [
        {
            "case": "Griffin v. Illinois",
            "cite": "351 U.S. 12 (1956)",
            "rule": "no procedural barrier to meaningful appellate review",
            "violation_class": "fundamental",
        }
    ],
}

REMEDY_MAP = {
    "structural": [
        {"type": "vacatur", "auth": "Arizona v. Fulminante, 499 U.S. 279 (1991)"},
        {"type": "mandamus", "auth": "28 U.S.C. § 1651"},
        {"type": "1983_action", "auth": "42 U.S.C. § 1983"},
        {"type": "appeal_structural_error", "auth": "preserved automatically"},
    ],
    "fundamental": [
        {"type": "motion_reconsideration", "auth": "Fed. R. Civ. P. 59(e)"},
        {"type": "appeal_reversible_error", "auth": "preserved by objection"},
        {"type": "1983_action", "auth": "42 U.S.C. § 1983 (if state actor)"},
    ],
    "harmless": [
        {"type": "preserve_for_appeal", "auth": "Fed. R. Evid. 103"},
    ],
}


def load_config():
    return json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}


def test_action(action_text, record, jurisdiction):
    """
    Test a court action against the canon.
    Returns a list of candidate violations the agent must verify against the record.
    """
    findings = []
    text_lower = action_text.lower()

    triggers = {
        "notice": ["no notice", "without notice", "ex parte", "did not serve", "unserved"],
        "hearing": ["denied hearing", "no hearing", "summarily", "without argument", "without oral"],
        "neutral_arbiter": ["financial interest", "stock", "campaign contribution", "personal", "prosecutor and judge"],
        "right_to_be_heard": ["denied opportunity", "struck", "refused to consider", "ignored argument"],
        "findings_on_record": ["no findings", "without explanation", "without reasons", "summary denial"],
        "appellate_review": ["denied appeal", "barred review", "no record"],
    }

    for prong, keywords in triggers.items():
        if any(kw in text_lower for kw in keywords):
            for canon_entry in DUE_PROCESS_CANON.get(prong, []):
                vc = canon_entry["violation_class"]
                findings.append({
                    "court_action": action_text[:500],
                    "due_process_prong": prong,
                    "binding_authority_violated": f"{canon_entry['case']}, {canon_entry['cite']}",
                    "rule": canon_entry["rule"],
                    "record_evidence": "AGENT_MUST_FILL_FROM_RECORD",
                    "violation_type": vc,
                    "preservation_required": ["contemporaneous objection", "post-ruling motion"],
                    "remedies": REMEDY_MAP[vc],
                    "court_must_explain": (
                        "court must articulate findings explaining the deviation OR be reversed"
                        if vc != "harmless"
                        else "preserve only"
                    ),
                })

    if not findings:
        findings.append({
            "court_action": action_text[:500],
            "due_process_prong": "none_detected",
            "note": "no triggers matched — agent should manually inspect for non-keyword violations",
        })

    return findings


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--action", required=True, help="Path to text file containing the court action/order")
    p.add_argument("--record", help="Path to JSON case record (optional)")
    p.add_argument("--jurisdiction", default="edpa")
    p.add_argument("--output", default="/tmp/dpv_report.json")
    args = p.parse_args()

    action_text = Path(args.action).read_text()
    record = json.loads(Path(args.record).read_text()) if args.record else {}

    findings = test_action(action_text, record, args.jurisdiction)

    report = {
        "jurisdiction": args.jurisdiction,
        "violations_found": len([f for f in findings if f.get("violation_type")]),
        "structural_count": len([f for f in findings if f.get("violation_type") == "structural"]),
        "fundamental_count": len([f for f in findings if f.get("violation_type") == "fundamental"]),
        "findings": findings,
    }

    Path(args.output).write_text(json.dumps(report, indent=2))
    print(f"WROTE: {args.output}")
    print(f"VIOLATIONS: {report['violations_found']} (structural={report['structural_count']}, fundamental={report['fundamental_count']})")


if __name__ == "__main__":
    main()
