#!/usr/bin/env python3
"""
appellate-pipeline
Appellate-stage layer over the trial pipeline.
Standards of review, preservation, brief building, post-decision options.
"""
import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

DATA_ROOT = Path.home() / ".hermes" / "data" / "cases"


# ─────────────────────────── Standards of Review canon ───────────────────────────

STANDARDS_OF_REVIEW = {
    "de_novo": {
        "label": "De novo",
        "applies_to": [
            "questions of law",
            "statutory interpretation",
            "constitutional questions",
            "summary judgment",
            "motion to dismiss",
            "personal jurisdiction",
            "subject matter jurisdiction",
            "qualified immunity (legal question)",
        ],
        "controlling_case": "Salve Regina College v. Russell, 499 U.S. 225 (1991)",
        "burden": "none — appellate court substitutes judgment",
    },
    "clear_error": {
        "label": "Clear error",
        "applies_to": [
            "findings of fact after bench trial",
            "credibility determinations",
            "factual findings underlying mixed questions",
        ],
        "controlling_case": "United States v. U.S. Gypsum Co., 333 U.S. 364, 395 (1948)",
        "burden": "must leave 'definite and firm conviction that a mistake has been committed'",
    },
    "abuse_of_discretion": {
        "label": "Abuse of discretion",
        "applies_to": [
            "evidentiary rulings",
            "scheduling orders",
            "sanctions imposed",
            "attorney fees",
            "discovery rulings",
            "preliminary injunction",
            "case management orders",
        ],
        "controlling_case": "Cooter & Gell v. Hartmarx Corp., 496 U.S. 384 (1990)",
        "burden": "decision must be arbitrary, fanciful, or clearly unreasonable",
    },
    "substantial_evidence": {
        "label": "Substantial evidence",
        "applies_to": [
            "agency findings",
            "jury verdicts on factual questions",
            "social security determinations",
        ],
        "controlling_case": "Richardson v. Perales, 402 U.S. 389 (1971)",
        "burden": "such relevant evidence as a reasonable mind might accept as adequate",
    },
    "plain_error": {
        "label": "Plain error",
        "applies_to": ["unpreserved errors"],
        "controlling_case": "United States v. Olano, 507 U.S. 725 (1993)",
        "burden": "(1) error, (2) plain, (3) affects substantial rights, (4) seriously affects fairness",
    },
    "structural_error": {
        "label": "Structural error",
        "applies_to": [
            "biased judge",
            "denial of counsel",
            "denial of self-representation",
            "denial of public trial",
            "defective reasonable doubt instruction",
        ],
        "controlling_case": "Arizona v. Fulminante, 499 U.S. 279 (1991)",
        "burden": "automatic reversal — never harmless",
    },
}

# Issue keyword → standard mapping
ISSUE_PATTERNS = {
    r"summary judgment|12\(b\)\(6\)|motion to dismiss|judgment on the pleadings": "de_novo",
    r"statutory interpretation|construction of|meaning of.*statute": "de_novo",
    r"constitutional|due process|equal protection|first amendment": "de_novo",
    r"jurisdiction|standing|removal|remand": "de_novo",
    r"finding of fact|credibility|bench trial finding": "clear_error",
    r"evidentiary ruling|admitted evidence|excluded evidence|hearsay ruling": "abuse_of_discretion",
    r"sanctions|rule 11|attorney fees|fee award": "abuse_of_discretion",
    r"discovery|scheduling order|case management": "abuse_of_discretion",
    r"preliminary injunction|TRO|temporary restraining": "abuse_of_discretion",
    r"recusal|biased judge|judicial bias": "structural_error",
    r"denial of counsel|right to counsel": "structural_error",
    r"agency|substantial evidence": "substantial_evidence",
    r"unpreserved|not raised below|waived": "plain_error",
}


def case_dir(case_no):
    p = DATA_ROOT / re.sub(r"[^a-z0-9]+", "_", case_no.lower()).strip("_")
    p.mkdir(parents=True, exist_ok=True)
    return p


def map_issue_to_standard(issue_desc):
    """Match an issue description to its standard of review."""
    text = issue_desc.lower()
    for pattern, standard in ISSUE_PATTERNS.items():
        if re.search(pattern, text):
            return STANDARDS_OF_REVIEW[standard]
    # Default
    return STANDARDS_OF_REVIEW["abuse_of_discretion"]


# ─────────────────────────── Commands ───────────────────────────

def cmd_init(args):
    cd = case_dir(args.case)
    meta_path = cd / "meta.json"
    if not meta_path.exists():
        sys.exit(f"Appellate case not yet initialized via case-manager: {args.case}")

    appellate_state = {
        "appellate_no": args.case,
        "trial_case": args.trial_case,
        "circuit": args.circuit,
        "initialized_at": datetime.now().isoformat(),
        "stage": "briefing",
        "issues_logged": 0,
    }
    (cd / "appellate_state.json").write_text(json.dumps(appellate_state, indent=2))

    # Create standard appellate deadlines
    deadlines = [
        {
            "description": "Opening brief due",
            "due": "TBD — set after record on appeal filed",
            "source_rule": "FRAP 31(a)(1)",
            "priority": "critical",
            "status": "pending_schedule",
        },
        {
            "description": "Reply brief due",
            "due": "TBD — 21 days after appellee brief",
            "source_rule": "FRAP 31(a)(1)",
            "priority": "high",
            "status": "pending_schedule",
        },
    ]
    with (cd / "deadlines.jsonl").open("a") as f:
        for d in deadlines:
            d["created_at"] = datetime.now().isoformat()
            f.write(json.dumps(d) + "\n")

    print(f"INITIALIZED APPELLATE: {args.case} (from trial {args.trial_case})")
    print(f"  Circuit: {args.circuit}")
    print(f"  State file: {cd / 'appellate_state.json'}")


def cmd_map_standards(args):
    cd = case_dir(args.case)
    if args.issues_file:
        issues = json.loads(Path(args.issues_file).read_text())
    elif args.issue:
        issues = [{"description": args.issue}]
    else:
        sys.exit("Provide --issues-file or --issue")

    mapped = []
    for i in issues:
        std = map_issue_to_standard(i["description"])
        mapped.append({
            "issue": i["description"],
            "standard_of_review": std["label"],
            "burden": std["burden"],
            "controlling_case": std["controlling_case"],
            "applies_to": std["applies_to"],
        })

    issues_path = cd / "issues.json"
    existing = json.loads(issues_path.read_text()) if issues_path.exists() else {"issues": []}
    existing["issues"].extend(mapped)
    issues_path.write_text(json.dumps(existing, indent=2))

    print(f"MAPPED {len(mapped)} issue(s) to standards of review:")
    for m in mapped:
        print(f"\n  Issue: {m['issue']}")
        print(f"  Standard: {m['standard_of_review']}")
        print(f"  Burden: {m['burden']}")
        print(f"  Controlling: {m['controlling_case']}")


def cmd_preservation(args):
    cd = case_dir(args.case)
    preservation_path = cd / "preservation.json"
    existing = json.loads(preservation_path.read_text()) if preservation_path.exists() else {"issues": []}

    entry = {
        "issue": args.issue,
        "raised_below": args.raised_below or args.record_cite is not None,
        "record_cite": args.record_cite,
        "ruling_cite": args.ruling_cite,
        "preservation_status": "preserved" if args.record_cite else "needs_plain_error",
        "plain_error_required": not bool(args.record_cite),
        "logged_at": datetime.now().isoformat(),
    }

    if entry["plain_error_required"]:
        entry["plain_error_test"] = STANDARDS_OF_REVIEW["plain_error"]
        entry["risk"] = "HIGH — unpreserved issues are reviewed under Olano four-part test"

    existing["issues"].append(entry)
    preservation_path.write_text(json.dumps(existing, indent=2))

    print(f"PRESERVATION LOGGED:")
    print(f"  Issue: {args.issue}")
    print(f"  Status: {entry['preservation_status']}")
    if args.record_cite:
        print(f"  Record: {args.record_cite}")
    if args.ruling_cite:
        print(f"  Ruling: {args.ruling_cite}")
    if entry["plain_error_required"]:
        print(f"  ⚠ {entry['risk']}")


def cmd_brief(args):
    cd = case_dir(args.case)
    meta = json.loads((cd / "meta.json").read_text())
    state = json.loads((cd / "appellate_state.json").read_text())
    issues = json.loads((cd / "issues.json").read_text()) if (cd / "issues.json").exists() else {"issues": []}
    preservation = json.loads((cd / "preservation.json").read_text()) if (cd / "preservation.json").exists() else {"issues": []}

    brief = {
        "case_no": args.case,
        "caption": meta.get("caption"),
        "court": state.get("circuit"),
        "brief_type": args.type,
        "frap_28_structure": {
            "1_corporate_disclosure": {"rule": "FRAP 26.1", "content": "AGENT_FILL"},
            "2_table_of_contents": {"auto_generated": True},
            "3_table_of_authorities": {"auto_generated_from": "all cited cases"},
            "4_jurisdictional_statement": {
                "rule": "FRAP 28(a)(4)",
                "content": "AGENT_FILL — basis for district court jurisdiction, basis for appellate jurisdiction, timeliness of appeal, finality"
            },
            "5_statement_of_issues": {
                "issues": [i["issue"] for i in issues.get("issues", [])]
            },
            "6_statement_of_case": {"content": "AGENT_FILL — procedural history with record cites"},
            "7_statement_of_facts": {
                "rule": "FRAP 28(a)(6)",
                "requirement": "Every fact MUST have a record citation (App. X)",
                "content": "AGENT_FILL"
            },
            "8_summary_of_argument": {"content": "AGENT_FILL"},
            "9_argument": {
                "issues": [
                    {
                        "issue": i["issue"],
                        "standard_of_review": i["standard_of_review"],
                        "controlling_case_for_standard": i["controlling_case"],
                        "argument_skeleton": "AGENT_FILL using binding-precedent-enforcer + due-process-violation-engine"
                    }
                    for i in issues.get("issues", [])
                ]
            },
            "10_conclusion": {"content": "AGENT_FILL — specific relief requested"},
            "11_certificate_of_compliance": {
                "rule": "FRAP 32(g)",
                "word_limit_principal": 13000,
                "word_limit_reply": 6500,
            },
            "12_certificate_of_service": {"content": "AGENT_FILL"},
        },
        "preservation_summary": {
            "total_issues": len(issues.get("issues", [])),
            "preserved": sum(1 for p in preservation.get("issues", []) if p.get("preservation_status") == "preserved"),
            "needs_plain_error": sum(1 for p in preservation.get("issues", []) if p.get("plain_error_required")),
        },
        "trap_options": {
            "1_affirm": "court affirms per binding authority — no path",
            "2_reverse": "court reverses per binding authority — relief granted",
            "3_vacate_and_remand": "court vacates and remands — partial relief",
            "4_dismiss_appeal": "court dismisses for procedural defect — file motion to recall mandate",
        },
    }

    output = args.output or f"/tmp/{args.case}_brief.json"
    Path(output).write_text(json.dumps(brief, indent=2))
    print(f"BRIEF SKELETON: {output}")
    print(f"  Issues: {len(issues.get('issues', []))}")
    print(f"  Preserved: {brief['preservation_summary']['preserved']}")
    print(f"  Needs plain error: {brief['preservation_summary']['needs_plain_error']}")


def cmd_cumulative_error(args):
    cd = case_dir(args.case)
    issues = json.loads((cd / "issues.json").read_text()) if (cd / "issues.json").exists() else {"issues": []}
    ledger = []
    ledger_path = cd / "ledger.jsonl"
    if ledger_path.exists():
        ledger = [json.loads(l) for l in ledger_path.read_text().splitlines() if l.strip()]

    # Aggregate errors from issues + ledger
    total_errors = len(issues.get("issues", []))
    structural = sum(
        1 for i in issues.get("issues", [])
        if "structural" in i.get("standard_of_review", "").lower()
    )
    de_novo_issues = sum(
        1 for i in issues.get("issues", [])
        if "de novo" in i.get("standard_of_review", "").lower()
    )

    ledger_score = sum(e.get("score", 0) for e in ledger)

    analysis = {
        "case": args.case,
        "total_errors_identified": total_errors,
        "structural_errors": structural,
        "de_novo_issues": de_novo_issues,
        "trial_ledger_cumulative_score": ledger_score,
        "cumulative_error_doctrine_applies": total_errors >= 3,
        "controlling_case": "Donnelly v. DeChristoforo, 416 U.S. 637 (1974); United States v. Hands, 184 F.3d 1322 (11th Cir. 1999)",
        "argument": (
            "Even if individual errors are harmless, their cumulative effect denied a fair trial. "
            "Where the combined prejudice from multiple errors so infects the proceeding that the "
            "result is unreliable, reversal is required."
        ) if total_errors >= 3 else "Insufficient errors logged for cumulative error doctrine.",
        "structural_error_note": (
            "STRUCTURAL ERRORS PRESENT — automatic reversal under Arizona v. Fulminante, "
            "499 U.S. 279 (1991). Cumulative analysis unnecessary."
        ) if structural > 0 else None,
    }

    output = args.output or f"/tmp/{args.case}_cumulative_error.json"
    Path(output).write_text(json.dumps(analysis, indent=2))
    print(f"CUMULATIVE ERROR ANALYSIS: {output}")
    print(f"  Total errors: {total_errors}")
    print(f"  Structural: {structural}")
    print(f"  De novo: {de_novo_issues}")
    print(f"  Doctrine applies: {analysis['cumulative_error_doctrine_applies']}")


def cmd_post_decision(args):
    cd = case_dir(args.case)
    decision_text = Path(args.decision_file).read_text() if args.decision_file else ""
    text_lower = decision_text.lower()

    # Detect outcome
    outcome = "unknown"
    if "affirm" in text_lower:
        outcome = "affirmed"
    elif "revers" in text_lower:
        outcome = "reversed"
    elif "vacat" in text_lower:
        outcome = "vacated"
    elif "remand" in text_lower:
        outcome = "remanded"
    elif "dismiss" in text_lower:
        outcome = "dismissed"

    options = {
        "case": args.case,
        "decision_outcome": outcome,
        "post_decision_options": [],
    }

    if outcome in ("affirmed", "dismissed"):
        options["post_decision_options"].extend([
            {
                "option": "petition_for_rehearing",
                "rule": "FRAP 40",
                "deadline": "14 days from entry of judgment",
                "standard": "points of law or fact overlooked or misapprehended",
                "word_limit": 3900,
            },
            {
                "option": "petition_for_rehearing_en_banc",
                "rule": "FRAP 35",
                "deadline": "14 days from entry of judgment",
                "standard": "necessary to secure or maintain uniformity of court's decisions, or proceeding involves question of exceptional importance",
                "word_limit": 3900,
            },
            {
                "option": "petition_for_certiorari",
                "rule": "Sup. Ct. R. 13",
                "deadline": "90 days from entry of judgment or denial of rehearing",
                "standard": "Sup. Ct. R. 10 — circuit split, important federal question, conflict with SCOTUS precedent",
                "word_limit": 9000,
            },
        ])

    if outcome in ("reversed", "vacated", "remanded"):
        options["post_decision_options"].extend([
            {
                "option": "respond_to_opposing_petitions",
                "rule": "FRAP 35/40 response",
                "deadline": "varies",
            },
            {
                "option": "mandate_issuance_tracking",
                "rule": "FRAP 41",
                "note": "stay of mandate available pending cert petition",
            },
        ])

    output = args.output or f"/tmp/{args.case}_post_decision.json"
    Path(output).write_text(json.dumps(options, indent=2))
    print(f"POST-DECISION OPTIONS: {output}")
    print(f"  Outcome: {outcome}")
    print(f"  Options: {len(options['post_decision_options'])}")


# ─────────────────────────── Main ───────────────────────────

def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("init")
    pi.add_argument("--case", required=True)
    pi.add_argument("--trial-case", required=True)
    pi.add_argument("--circuit", required=True)

    pm = sub.add_parser("map-standards")
    pm.add_argument("--case", required=True)
    pm.add_argument("--issues-file")
    pm.add_argument("--issue")

    pp = sub.add_parser("preservation")
    pp.add_argument("--case", required=True)
    pp.add_argument("--issue", required=True)
    pp.add_argument("--raised-below", action="store_true")
    pp.add_argument("--record-cite")
    pp.add_argument("--ruling-cite")

    pb = sub.add_parser("brief")
    pb.add_argument("--case", required=True)
    pb.add_argument("--type", default="opening", choices=["opening", "answering", "reply", "amicus"])
    pb.add_argument("--output")

    pc = sub.add_parser("cumulative-error")
    pc.add_argument("--case", required=True)
    pc.add_argument("--output")

    pd = sub.add_parser("post-decision")
    pd.add_argument("--case", required=True)
    pd.add_argument("--decision-file")
    pd.add_argument("--output")

    args = p.parse_args()
    handlers = {
        "init": cmd_init, "map-standards": cmd_map_standards, "preservation": cmd_preservation,
        "brief": cmd_brief, "cumulative-error": cmd_cumulative_error, "post-decision": cmd_post_decision,
    }
    handlers[args.cmd](args)


if __name__ == "__main__":
    main()
