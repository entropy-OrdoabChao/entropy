"""
void_determination.py — by-the-book FRCP 60(b)(4) void-judgment test.

A judgment is VOID (not merely voidable) under Fed. R. Civ. P. 60(b)(4) ONLY
in the circumstances the Supreme Court fixed in United Student Aid Funds, Inc.
v. Espinosa, 559 U.S. 260, 271 (2010):
  (1) the rendering court lacked SUBJECT-MATTER JURISDICTION;
  (2) the rendering court lacked PERSONAL JURISDICTION; or
  (3) the judgment was entered in a manner inconsistent with DUE PROCESS.

Standing is a component of subject-matter jurisdiction (Steel Co. v. Citizens
for a Better Env't, 523 U.S. 83, 94 (1998)); its absence is jurisdictional and
renders the judgment void. Concrete-injury standing: Lujan v. Defenders of
Wildlife, 504 U.S. 555 (1992); Spokeo, Inc. v. Robins, 578 U.S. 330 (2016);
TransUnion LLC v. Ramirez, 594 U.S. 413 (2021).

This module is AGNOSTIC: it reads the case's own computed verdicts (from
jurisdiction_check / applicability standing / the audit) and applies the fixed
test. It states no opinion; where a ground is established by a FAIL verdict, the
voidness follows from binding precedent, not from advocacy.

Returns the grounds found (each with its controlling authority) so the drafter
can argue void under the specific prong, citing the specific binding case.
"""
from __future__ import annotations
from typing import Any

# Controlling authority per ground (their rules — the court's own standard).
_AUTH = {
    "no_subject_matter_jurisdiction": [
        "Fed. R. Civ. P. 60(b)(4)",
        "United Student Aid Funds, Inc. v. Espinosa, 559 U.S. 260, 271 (2010)",
        "Steel Co. v. Citizens for a Better Env't, 523 U.S. 83, 94 (1998)",
    ],
    "no_standing": [
        "Fed. R. Civ. P. 60(b)(4)",
        "Steel Co. v. Citizens for a Better Env't, 523 U.S. 83, 94 (1998)",
        "Lujan v. Defenders of Wildlife, 504 U.S. 555, 560-61 (1992)",
        "Spokeo, Inc. v. Robins, 578 U.S. 330, 338-39 (2016)",
        "TransUnion LLC v. Ramirez, 594 U.S. 413 (2021)",
    ],
    "no_personal_jurisdiction": [
        "Fed. R. Civ. P. 60(b)(4)",
        "United Student Aid Funds, Inc. v. Espinosa, 559 U.S. 260, 271 (2010)",
        "Ins. Corp. of Ireland v. Compagnie des Bauxites de Guinee, 456 U.S. 694 (1982)",
    ],
    "due_process_defect": [
        "Fed. R. Civ. P. 60(b)(4)",
        "United Student Aid Funds, Inc. v. Espinosa, 559 U.S. 260, 271 (2010)",
        "U.S. Const. amend. XIV, § 1",
        "Mullane v. Cent. Hanover Bank & Tr. Co., 339 U.S. 306, 314 (1950)",
    ],
}

# Audit violation categories that establish a due-process defect (notice /
# opportunity to be heard). These are the registry's own category slugs.
_DUE_PROCESS_CATEGORIES = {
    "INADEQUATE_NOTICE",
    "PROCEDURAL_DUE_PROCESS",
    "DEFECTIVE_SERVICE",          # no notice -> due-process voidness
    "DENIED_OPPORTUNITY_TO_BE_HEARD",
}


def determine_void_grounds(
    jurisdiction: dict[str, Any] | None,
    standing_verdict: str | None,
    audit_defects: list[dict] | None,
) -> list[dict]:
    """Apply the Espinosa test to a case's computed verdicts.

    Args:
      jurisdiction: asdict(JurisdictionResult) — has subject_matter / personal
                    verdicts (+ _reason, _authority).
      standing_verdict: 'PASS' | 'FAIL' | 'UNCLEAR' | None — Article III standing.
      audit_defects: list of detected violation dicts (each may have 'category').

    Returns: list of grounds, each:
      {ground, reason, authority:[...]}
    Empty list => no void ground established => judgment not void (ffc applies).
    Only FAIL verdicts establish a ground; UNCLEAR does NOT (we do not assert
    voidness without a clear failure — airtight, not overreaching).
    """
    grounds: list[dict] = []
    j = jurisdiction or {}

    # (1) Subject-matter jurisdiction
    if j.get("subject_matter") == "FAIL":
        grounds.append({
            "ground": "no_subject_matter_jurisdiction",
            "reason": j.get("subject_matter_reason", "")
                      or "Rendering court lacked subject-matter jurisdiction.",
            "authority": (j.get("subject_matter_authority") or [])
                         + _AUTH["no_subject_matter_jurisdiction"],
        })

    # (1a) Standing — a component of SMJ; its absence is jurisdictional.
    if standing_verdict == "FAIL":
        grounds.append({
            "ground": "no_standing",
            "reason": "Plaintiff/movant lacked Article III standing; standing is a "
                      "component of subject-matter jurisdiction, the absence of "
                      "which renders any resulting judgment void.",
            "authority": _AUTH["no_standing"],
        })

    # (2) Personal jurisdiction
    if j.get("personal") == "FAIL":
        grounds.append({
            "ground": "no_personal_jurisdiction",
            "reason": j.get("personal_reason", "")
                      or "Rendering court lacked personal jurisdiction over a party.",
            "authority": (j.get("personal_authority") or [])
                         + _AUTH["no_personal_jurisdiction"],
        })

    # (3) Due-process defect — from the audit's detected violations.
    for d in (audit_defects or []):
        cat = str(d.get("category") or d.get("violation_type") or "").upper()
        if any(dp in cat for dp in _DUE_PROCESS_CATEGORIES):
            grounds.append({
                "ground": "due_process_defect",
                "reason": d.get("description") or d.get("explanation")
                          or "Judgment entered inconsistent with due process "
                             "(notice and opportunity to be heard).",
                "authority": _AUTH["due_process_defect"],
            })
            break  # one due-process ground suffices

    return grounds


def ffc_applies(
    jurisdiction: dict[str, Any] | None,
    standing_verdict: str | None,
    audit_defects: list[dict] | None,
) -> bool:
    """True if NO void ground is established (judgment stands / full faith &
    credit applies). False if ANY Rule 60(b)(4) ground is present (void).

    This is the value the decision engine consumes: ffc_applies=False fires
    the vacate_void_judgment rule (R002).
    """
    return len(determine_void_grounds(jurisdiction, standing_verdict, audit_defects)) == 0


if __name__ == "__main__":
    # self-test: the three Espinosa prongs + the clean case
    import json
    cases = {
        "no_smj (state tort in federal court, no diversity)": dict(
            jurisdiction={"subject_matter": "FAIL",
                          "subject_matter_reason": "State-law tort; no federal question and no diversity.",
                          "subject_matter_authority": ["28 U.S.C. 1331"],
                          "personal": "PASS"},
            standing_verdict="PASS", audit_defects=[]),
        "no_standing": dict(
            jurisdiction={"subject_matter": "PASS", "personal": "PASS"},
            standing_verdict="FAIL", audit_defects=[]),
        "due_process (defective service)": dict(
            jurisdiction={"subject_matter": "PASS", "personal": "PASS"},
            standing_verdict="PASS",
            audit_defects=[{"category": "DEFECTIVE_SERVICE",
                            "description": "Party never served; no notice."}]),
        "clean (no void ground)": dict(
            jurisdiction={"subject_matter": "PASS", "personal": "PASS"},
            standing_verdict="PASS", audit_defects=[]),
    }
    for name, kw in cases.items():
        g = determine_void_grounds(**kw)
        print(f"\n=== {name} ===")
        print(f"  ffc_applies (judgment stands)? {ffc_applies(**kw)}")
        print(f"  void grounds: {[x['ground'] for x in g]}")
        for x in g:
            print(f"    - {x['ground']}: {x['authority'][1] if len(x['authority'])>1 else x['authority'][0]}")
