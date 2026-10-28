#!/usr/bin/env python3
"""
Legal Scorecard Engine — Deterministic violation scoring.
No interpretation. Binary rule checks → weighted score → legal consequence.

Usage:
  python3 scorecard_engine.py                    # Interactive mode
  python3 scorecard_engine.py --json input.json  # From JSON file
  python3 scorecard_engine.py --case "case name"  # Label output
"""

import json, sys, os
from datetime import datetime

# ═══════════════════════════════════════════════════════════════════
# RULE DEFINITIONS — Each rule is a binary check tied to authority
# ═══════════════════════════════════════════════════════════════════

RULES = {
    "jurisdiction": {
        "weight": 3,
        "label": "Subject Matter Jurisdiction",
        "fatal": True,  # ANY failure here = VOID
        "rules": {
            "no_federal_question": {
                "label": "No federal question presented",
                "authority": "28 U.S.C. § 1331",
                "case": "Louisville & Nashville R.R. v. Mottley, 211 U.S. 149 (1908)",
                "test": "Complaint does not arise under federal law on its face"
            },
            "no_complete_diversity": {
                "label": "Complete diversity lacking",
                "authority": "28 U.S.C. § 1332",
                "case": "Strawbridge v. Curtiss, 7 U.S. 267 (1806)",
                "test": "Any plaintiff shares citizenship with any defendant"
            },
            "amount_not_met": {
                "label": "Amount in controversy not exceeded",
                "authority": "28 U.S.C. § 1332(a)",
                "case": "St. Paul Mercury v. Red Cab, 303 U.S. 283 (1938)",
                "test": "Legal certainty that amount does not exceed $75,000"
            },
            "no_supplemental_basis": {
                "label": "No supplemental jurisdiction for state claims",
                "authority": "28 U.S.C. § 1367",
                "case": "United Mine Workers v. Gibbs, 383 U.S. 715 (1966)",
                "test": "State claims do not share common nucleus of operative fact"
            }
        }
    },
    "removal": {
        "weight": 3,
        "label": "Removal Procedure",
        "fatal": True,
        "rules": {
            "removal_untimely": {
                "label": "Removal not filed within 30 days",
                "authority": "28 U.S.C. § 1446(b)",
                "case": "Murphy Bros. v. Michetti Pipe Stringing, 526 U.S. 344 (1999)",
                "test": "Notice of removal filed >30 days after service"
            },
            "forum_defendant_rule": {
                "label": "Forum defendant improperly removed",
                "authority": "28 U.S.C. § 1441(b)(2)",
                "case": "Shamrock Oil v. Sheets, 313 U.S. 100 (1941)",
                "test": "Defendant is citizen of forum state in diversity case"
            },
            "no_unanimous_consent": {
                "label": "Not all defendants joined in removal",
                "authority": "28 U.S.C. § 1446(b)(2)(A)",
                "case": "Shamrock Oil v. Sheets, 313 U.S. 100 (1941)",
                "test": "One or more defendants did not consent within 30 days"
            },
            "late_consent": {
                "label": "Consent filed after 30-day window",
                "authority": "28 U.S.C. § 1446(b)(2)(B)",
                "case": "",
                "test": "Co-defendant consent filed after removal deadline"
            },
            "one_year_bar": {
                "label": "Removal filed after one-year bar",
                "authority": "28 U.S.C. § 1446(c)(1)",
                "case": "",
                "test": "Diversity-based removal filed >1 year after commencement"
            }
        }
    },
    "procedure_federal": {
        "weight": 2,
        "label": "Federal Procedural Requirements",
        "fatal": False,
        "rules": {
            "insufficient_pleading": {
                "label": "Complaint fails plausibility standard",
                "authority": "Fed. R. Civ. P. 8(a)(2)",
                "case": "Ashcroft v. Iqbal, 556 U.S. 662 (2009)",
                "test": "Factual allegations do not state a plausible claim"
            },
            "no_short_plain_statement": {
                "label": "No short and plain statement of jurisdiction",
                "authority": "Fed. R. Civ. P. 8(a)(1)",
                "case": "",
                "test": "Complaint omits jurisdictional basis"
            },
            "improper_service": {
                "label": "Service not effected properly",
                "authority": "Fed. R. Civ. P. 4",
                "case": "",
                "test": "Service did not comply with Rule 4 requirements"
            },
            "no_rule_11_basis": {
                "label": "Filing lacks Rule 11 basis",
                "authority": "Fed. R. Civ. P. 11(b)",
                "case": "",
                "test": "Claims not warranted by existing law or good faith argument"
            }
        }
    },
    "procedure_pa": {
        "weight": 2,
        "label": "PA Procedural Requirements",
        "fatal": False,
        "rules": {
            "no_verification": {
                "label": "Required verification missing",
                "authority": "Pa.R.C.P. No. 1024",
                "case": "",
                "test": "Pleading contains averments of fact but is unverified"
            },
            "no_cert_of_service": {
                "label": "Certificate of service missing",
                "authority": "Pa.R.C.P. No. 440",
                "case": "",
                "test": "Filing lacks certificate of service"
            },
            "improper_service_pa": {
                "label": "Service not per PA rules",
                "authority": "Pa.R.C.P. No. 400-410",
                "case": "",
                "test": "Service did not comply with PA Rules"
            },
            "no_proposed_order": {
                "label": "Required proposed order not attached",
                "authority": "Local Rule",
                "case": "",
                "test": "Motion filed without required proposed order"
            }
        }
    },
    "due_process": {
        "weight": 3,
        "label": "Due Process",
        "fatal": True,
        "rules": {
            "no_notice": {
                "label": "Inadequate notice of proceedings",
                "authority": "U.S. Const. Amend. XIV; PA Const. Art. I § 11",
                "case": "Mullane v. Central Hanover, 339 U.S. 306 (1950)",
                "test": "Party did not receive constitutionally adequate notice"
            },
            "no_opportunity_to_be_heard": {
                "label": "Denied opportunity to be heard",
                "authority": "U.S. Const. Amend. XIV",
                "case": "Mathews v. Eldridge, 424 U.S. 319 (1976)",
                "test": "Party was denied meaningful opportunity to respond"
            },
            "biased_tribunal": {
                "label": "Tribunal lacked impartiality",
                "authority": "28 U.S.C. § 455",
                "case": "Liteky v. United States, 510 U.S. 540 (1994)",
                "test": "Judge had personal bias or financial interest"
            },
            "fraud_on_court": {
                "label": "Fraud upon the court",
                "authority": "Fed. R. Civ. P. 60(d)(3)",
                "case": "Hazel-Atlas Glass v. Hartford-Empire, 322 U.S. 238 (1944)",
                "test": "Officer of court made material misrepresentation"
            }
        }
    },
    "statutory_violations": {
        "weight": 2,
        "label": "Substantive Statutory Violations",
        "fatal": False,
        "rules": {
            "fdcpa_violation": {
                "label": "Fair Debt Collection Practices Act violation",
                "authority": "15 U.S.C. § 1692",
                "case": "Heintz v. Jenkins, 514 U.S. 291 (1995)",
                "test": "Debt collector engaged in prohibited practice"
            },
            "fcra_violation": {
                "label": "Fair Credit Reporting Act violation",
                "authority": "15 U.S.C. § 1681",
                "case": "Safeco v. Burr, 551 U.S. 47 (2007)",
                "test": "Furnisher or CRA violated FCRA obligations"
            },
            "respa_violation": {
                "label": "RESPA violation — QWR response failure",
                "authority": "12 U.S.C. § 2605(e)",
                "case": "",
                "test": "Servicer failed to respond to QWR within 30 days"
            },
            "bad_faith_insurance": {
                "label": "Bad faith insurance practices",
                "authority": "42 Pa.C.S.A. § 8371",
                "case": "Terletsky v. Prudential, 649 A.2d 680 (Pa. Super. 1994)",
                "test": "Insurer lacked reasonable basis for denying claim"
            },
            "utpcpl_violation": {
                "label": "Unfair Trade Practices violation",
                "authority": "73 P.S. § 201-1 et seq.",
                "case": "",
                "test": "Deceptive or unfair business practice"
            },
            "section_1983": {
                "label": "Deprivation of rights under color of law",
                "authority": "42 U.S.C. § 1983",
                "case": "Monroe v. Pape, 365 U.S. 167 (1961)",
                "test": "State actor deprived person of constitutional right"
            }
        }
    }
}

# ═══════════════════════════════════════════════════════════════════
# OUTCOME DETERMINATION — Deterministic, no interpretation
# ═══════════════════════════════════════════════════════════════════

def determine_outcome(total_score, fatal_categories_failed, violations):
    """
    Deterministic outcome based on score and fatal category triggers.
    Outcomes are tied to DOCTRINE, not just math.
    No interpretation. 1 + 1 = 2.
    """
    outcomes = []

    # Fatal category trigger — doctrinal mapping
    if fatal_categories_failed:
        for cat in fatal_categories_failed:
            if "Jurisdiction" in cat or "Removal" in cat:
                outcomes.append({
                    "determination": "VOID AB INITIO",
                    "doctrine": "Jurisdictional defect — court without jurisdiction acts as nullity",
                    "basis": f"Fatal defect in: {cat}",
                    "controlling_authority": "Steel Co. v. Citizens for Better Environment, 523 U.S. 83 (1998); "
                                             "Marbury v. Madison, 5 U.S. 137 (1803)",
                    "action": "Motion to Vacate (Rule 60(b)(4)) or Collateral Attack",
                    "time_limit": "NONE — void judgments may be challenged at any time",
                    "draft_language": "The order is void ab initio for want of subject matter jurisdiction."
                })
            elif "Due Process" in cat:
                outcomes.append({
                    "determination": "VOID AB INITIO",
                    "doctrine": "Constitutional defect — denial of due process renders proceeding void",
                    "basis": f"Fatal defect in: {cat}",
                    "controlling_authority": "Mullane v. Central Hanover, 339 U.S. 306 (1950); "
                                             "U.S. Const. Amend. XIV; PA Const. Art. I § 11",
                    "action": "Motion to Vacate (Rule 60(b)(4)) or Independent Action (Rule 60(d))",
                    "time_limit": "NONE — constitutional violations are not subject to time bars",
                    "draft_language": "The order is void ab initio as a denial of constitutional due process."
                })

    # Score-based outcomes (for non-fatal categories)
    non_fatal_score = sum(v["weight"] for v in violations
                         if not any(fc in v["category"] for fc in fatal_categories_failed))

    if not fatal_categories_failed:
        if non_fatal_score >= 8:
            outcomes.append({
                "determination": "REVERSIBLE ERROR — MULTIPLE GROUNDS",
                "doctrine": "Cumulative procedural errors requiring reversal",
                "basis": f"{non_fatal_score} weighted violations across {len(violations)} rules",
                "controlling_authority": "Applicable procedural rules — see individual violations",
                "action": "Appeal with multiple independent grounds",
                "time_limit": "30 days from entry of final order (federal); 30 days (PA)",
                "draft_language": "The order constitutes reversible error on multiple independent grounds."
            })
        elif non_fatal_score >= 5:
            outcomes.append({
                "determination": "REVERSIBLE ERROR",
                "doctrine": "Prejudicial procedural error",
                "basis": f"{non_fatal_score} weighted violations",
                "controlling_authority": "Applicable procedural rules — see individual violations",
                "action": "Motion for Reconsideration or Appeal",
                "time_limit": "Check applicable deadlines",
                "draft_language": "The order constitutes reversible error."
            })
        elif non_fatal_score >= 3:
            outcomes.append({
                "determination": "MATERIAL DEFECT — CURABLE",
                "doctrine": "Procedural irregularity subject to correction",
                "basis": f"{non_fatal_score} weighted violations",
                "controlling_authority": "Applicable procedural rules",
                "action": "Motion to Strike / Preliminary Objections",
                "time_limit": "20 days (PA Prelim. Obj.) / per local rules",
                "draft_language": "The filing contains material procedural defects requiring correction."
            })
        elif non_fatal_score >= 1:
            outcomes.append({
                "determination": "MINOR DEFECT",
                "doctrine": "Non-prejudicial irregularity",
                "basis": f"{non_fatal_score} weighted violation(s)",
                "controlling_authority": "N/A",
                "action": "Preserve for record; raise if strategically useful",
                "time_limit": "N/A",
                "draft_language": ""
            })
        else:
            outcomes.append({
                "determination": "NO MATERIAL DEFECT",
                "doctrine": "No procedural basis for challenge",
                "basis": "No violations detected",
                "controlling_authority": "N/A",
                "action": "No procedural attack available",
                "time_limit": "N/A",
                "draft_language": ""
            })

    return outcomes


def run_scorecard(answers):
    """
    Run the scorecard against provided answers.
    answers = {"category.rule_key": True/False, ...}
    """
    violations = []
    total_score = 0
    fatal_categories_failed = []
    category_results = {}

    for cat_key, cat_data in RULES.items():
        cat_violations = []
        cat_score = 0

        for rule_key, rule_data in cat_data["rules"].items():
            full_key = f"{cat_key}.{rule_key}"
            violated = answers.get(full_key, False)

            if violated:
                weighted = cat_data["weight"]
                cat_score += weighted
                total_score += weighted
                # Support record anchors: answers can be True or {"record_cite": "..."}
                record_cite = ""
                if isinstance(violated, dict):
                    record_cite = violated.get("record_cite", "")
                elif isinstance(answers.get(full_key + ".record_cite"), str):
                    record_cite = answers[full_key + ".record_cite"]
                violation = {
                    "key": full_key,
                    "label": rule_data["label"],
                    "record_cite": record_cite,
                    "authority": rule_data["authority"],
                    "case": rule_data.get("case", ""),
                    "weight": weighted,
                    "category": cat_data["label"]
                }
                violations.append(violation)
                cat_violations.append(violation)

        if cat_violations and cat_data.get("fatal"):
            fatal_categories_failed.append(cat_data["label"])

        category_results[cat_key] = {
            "label": cat_data["label"],
            "weight": cat_data["weight"],
            "fatal": cat_data.get("fatal", False),
            "violations": cat_violations,
            "score": cat_score,
            "total_rules": len(cat_data["rules"]),
            "violated_count": len(cat_violations)
        }

    outcomes = determine_outcome(total_score, fatal_categories_failed, violations)

    return {
        "timestamp": datetime.now().isoformat(),
        "total_score": total_score,
        "total_violations": len(violations),
        "fatal_categories_failed": fatal_categories_failed,
        "violations": violations,
        "category_results": category_results,
        "outcomes": outcomes
    }


def format_report(result, case_name=""):
    """Format scorecard as court-ready text report."""
    lines = []
    lines.append("=" * 70)
    lines.append("LEGAL VIOLATION SCORECARD")
    if case_name:
        lines.append(f"Case: {case_name}")
    lines.append(f"Generated: {result['timestamp']}")
    lines.append("=" * 70)

    for cat_key, cat in result["category_results"].items():
        lines.append("")
        fatal_tag = " [FATAL]" if cat["fatal"] else ""
        lines.append(f"CATEGORY: {cat['label']}{fatal_tag} (weight: {cat['weight']}x)")
        lines.append("-" * 50)

        for rule_key, rule_data in RULES[cat_key]["rules"].items():
            full_key = f"{cat_key}.{rule_key}"
            violated = any(v["key"] == full_key for v in cat["violations"])
            status = "FAILED" if violated else "PASS"
            mark = "X" if violated else " "
            lines.append(f"  [{mark}] {rule_data['label']}")
            lines.append(f"      Authority: {rule_data['authority']}")
            if violated and rule_data.get("case"):
                lines.append(f"      Case: {rule_data['case']}")

        lines.append(f"  Category Score: {cat['score']} "
                      f"({cat['violated_count']}/{cat['total_rules']} rules violated)")

    lines.append("")
    lines.append("=" * 70)
    lines.append(f"TOTAL VIOLATIONS: {result['total_violations']}")
    lines.append(f"WEIGHTED SCORE: {result['total_score']}")

    if result["fatal_categories_failed"]:
        lines.append(f"FATAL CATEGORIES: {', '.join(result['fatal_categories_failed'])}")

    lines.append("")
    lines.append("DETERMINATION:")
    for outcome in result["outcomes"]:
        lines.append(f"  {outcome['determination']}")
        if outcome.get("doctrine"):
            lines.append(f"  Doctrine: {outcome['doctrine']}")
        lines.append(f"  Basis: {outcome['basis']}")
        if outcome.get("controlling_authority"):
            lines.append(f"  Controlling Authority: {outcome['controlling_authority']}")
        lines.append(f"  Action: {outcome['action']}")
        lines.append(f"  Time Limit: {outcome['time_limit']}")
        if outcome.get("draft_language"):
            lines.append(f"  Required Draft Language: \"{outcome['draft_language']}\"")

    lines.append("=" * 70)

    # Authority summary with record cites
    if result["violations"]:
        lines.append("")
        lines.append("CONTROLLING AUTHORITY FOR EACH VIOLATION:")
        for i, v in enumerate(result["violations"], 1):
            lines.append(f"  {i}. {v['label']}")
            lines.append(f"     {v['authority']}")
            if v.get("case"):
                lines.append(f"     {v['case']}")
            if v.get("record_cite"):
                lines.append(f"     Record: {v['record_cite']}")

    return "\n".join(lines)


def interactive_mode():
    """Walk through each rule interactively."""
    print("=" * 60)
    print("LEGAL SCORECARD — Interactive Mode")
    print("Answer Y/N for each rule violation")
    print("=" * 60)

    case_name = input("\nCase name (optional): ").strip()
    answers = {}

    for cat_key, cat_data in RULES.items():
        print(f"\n{'─'*40}")
        fatal_tag = " [FATAL]" if cat_data.get("fatal") else ""
        print(f"Category: {cat_data['label']}{fatal_tag}")
        print(f"{'─'*40}")

        for rule_key, rule_data in cat_data["rules"].items():
            full_key = f"{cat_key}.{rule_key}"
            print(f"\n  {rule_data['label']}")
            print(f"  Authority: {rule_data['authority']}")
            print(f"  Test: {rule_data['test']}")

            while True:
                ans = input("  Violated? (y/n/skip): ").strip().lower()
                if ans in ("y", "yes", "1"):
                    answers[full_key] = True
                    break
                elif ans in ("n", "no", "0", "s", "skip", ""):
                    answers[full_key] = False
                    break

    result = run_scorecard(answers)
    report = format_report(result, case_name)
    print("\n" + report)

    # Save report
    outfile = f"/tmp/scorecard_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    with open(outfile, "w") as f:
        f.write(report)
    print(f"\nSaved to: {outfile}")

    # Also save JSON
    jsonfile = outfile.replace(".txt", ".json")
    with open(jsonfile, "w") as f:
        json.dump({"answers": answers, "result": result}, f, indent=2)
    print(f"JSON saved to: {jsonfile}")


def from_json(filepath, case_name=""):
    """Run scorecard from JSON file."""
    with open(filepath) as f:
        data = json.load(f)

    answers = data if isinstance(data, dict) and not data.get("answers") else data.get("answers", data)
    result = run_scorecard(answers)
    report = format_report(result, case_name)
    print(report)

    outfile = f"/tmp/scorecard_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    with open(outfile, "w") as f:
        f.write(report)
    print(f"\nSaved to: {outfile}")


def list_rules():
    """Print all available rules."""
    total = 0
    for cat_key, cat_data in RULES.items():
        fatal_tag = " [FATAL]" if cat_data.get("fatal") else ""
        print(f"\n{cat_data['label']}{fatal_tag} (weight: {cat_data['weight']}x)")
        for rule_key, rule_data in cat_data["rules"].items():
            print(f"  {cat_key}.{rule_key}")
            print(f"    {rule_data['label']} — {rule_data['authority']}")
            total += 1
    print(f"\nTotal rules: {total}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        if sys.argv[1] == "--json":
            case = sys.argv[3] if len(sys.argv) > 3 and sys.argv[2] == "--case" else ""
            if not case and len(sys.argv) > 3:
                case = sys.argv[3]
            from_json(sys.argv[2], case)
        elif sys.argv[1] == "--list":
            list_rules()
        elif sys.argv[1] == "--case":
            interactive_mode()
        else:
            print("Usage:")
            print("  scorecard_engine.py              # Interactive mode")
            print("  scorecard_engine.py --json file   # From JSON")
            print("  scorecard_engine.py --list        # List all rules")
    else:
        interactive_mode()
