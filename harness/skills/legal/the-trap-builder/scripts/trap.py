#!/usr/bin/env python3
"""
the-trap-builder
Generates the final filing appendix that enumerates the court's three lawful options.
"""
import argparse
import json
from pathlib import Path


def load(path):
    return json.loads(Path(path).read_text()) if path and Path(path).exists() else {}


def build_trap(chain, wall, mandatory, dpv, jurisdiction):
    # Extract binding cites from precedent wall
    binding_cites = []
    if isinstance(wall, list):
        for w in wall:
            for entry in w.get("binding_chain", []) or []:
                if isinstance(entry, dict):
                    binding_cites.append({
                        "case": entry.get("case"),
                        "cite": entry.get("citation"),
                        "binds": entry.get("binds", []),
                    })

    # Extract mandatory duties the court would have to violate
    duties_to_violate = []
    if isinstance(mandatory, dict):
        for d in mandatory.get("deviations", []):
            duties_to_violate.append(d.get("duty_verbatim"))

    # Extract due process prongs
    dp_prongs = []
    if isinstance(dpv, dict):
        for f in dpv.get("findings", []):
            if f.get("violation_type"):
                dp_prongs.append(f.get("due_process_prong"))

    return {
        "title": "APPENDIX: COURT'S LAWFUL OPTIONS UNDER BINDING AUTHORITY",
        "jurisdiction": jurisdiction,
        "options": [
            {
                "option": 1,
                "label": "RULE PER BINDING AUTHORITY",
                "required_ruling": "grant relief sought (see Conclusion of attached filing)",
                "supporting_authority": binding_cites,
                "consequence": "no reversible error; record reflects compliance with binding precedent",
            },
            {
                "option": 2,
                "label": "RULE AGAINST BINDING AUTHORITY",
                "required_ruling": "deny relief",
                "court_must": [
                    "explain in writing why each cited binding holding does not control",
                    "explain why each mandatory statutory duty does not apply",
                    "make findings of fact and conclusions of law per Fed. R. Civ. P. 52(a)",
                ] + [f"contradict mandatory duty: {d}" for d in duties_to_violate],
                "triggers": [
                    {"type": "reversible_error", "scope": "preserved by this filing"},
                    {"type": "structural_error", "scope": "if Caperton/Tumey applies"},
                    {"type": "1983_action", "auth": "42 U.S.C. § 1983"},
                    {"type": "judicial_misconduct_complaint", "auth": "28 U.S.C. § 351"},
                ],
                "preserved_issues": dp_prongs or ["due process violation", "abuse of discretion"],
            },
            {
                "option": 3,
                "label": "REFUSE TO RULE",
                "consequence": "mandamus jurisdiction attaches in supervising appellate court",
                "authority": [
                    "28 U.S.C. § 1651 (All Writs Act)",
                    "Cheney v. U.S. Dist. Court, 542 U.S. 367 (2004)",
                    "In re School Asbestos Litig., 977 F.2d 764 (3d Cir. 1992)",
                ],
                "filing_ready": True,
            },
        ],
        "bias_exposure_summary": (
            "An adverse ruling on the merits requires the court to articulate written findings "
            "explaining its deviation from each cited binding authority. Failure to do so violates "
            "Fed. R. Civ. P. 52(a) and the procedural due process requirement of a decision on the record."
        ),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--chain")
    p.add_argument("--precedent-wall", required=True)
    p.add_argument("--mandatory-duties")
    p.add_argument("--dpv-report")
    p.add_argument("--jurisdiction", required=True)
    p.add_argument("--output", default="/tmp/trap_appendix.json")
    args = p.parse_args()

    trap = build_trap(
        load(args.chain),
        load(args.precedent_wall),
        load(args.mandatory_duties),
        load(args.dpv_report),
        args.jurisdiction,
    )
    Path(args.output).write_text(json.dumps(trap, indent=2))
    print(f"WROTE: {args.output}")


if __name__ == "__main__":
    main()
