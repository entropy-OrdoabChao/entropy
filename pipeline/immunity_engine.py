"""
pipeline/immunity_engine.py — Immunity Waiver Analysis Engine

DETERMINISTIC. NO LLM. NO INTERPRETATION.

For every government defendant, determines:
  - WHAT immunity applies (sovereign, qualified, absolute, none)
  - WHETHER immunity has been waived (by statute)
  - WHAT exceptions to the waiver exist
  - WHETHER the defendant acted outside authority (ultra vires)
  - WHAT causes of action survive immunity

Every conclusion traced to source text.

Usage:
    from pipeline.immunity_engine import analyze_immunity, check_waiver

    result = analyze_immunity(defendant_type="federal_officer", conduct="denied FOIA request")
"""

import re
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Literal
from core.logger import get_logger

log = get_logger("immunity_engine")

# ─── Data Structures ────────────────────────────────────────────────

ImmunityType = Literal["sovereign", "qualified", "absolute",
                        "statutory", "none", "unknown"]

WaiverStatus = Literal["waived", "intact", "partial", "excepted", "unknown"]

DefendantType = Literal["federal_government", "federal_agency", "federal_officer",
                         "state_government", "state_agency", "state_officer",
                         "local_government", "local_officer",
                         "judge", "prosecutor", "legislator",
                         "private_entity", "unknown"]


@dataclass
class ImmunityWaiver:
    """A specific statutory waiver of immunity."""
    statute: str                 # e.g., "28 U.S.C. § 1346(b)"
    waiver_type: str             # e.g., "FTCA", "APA", "Tucker Act"
    scope: str                   # what the waiver covers
    exceptions: List[str] = field(default_factory=list)
    conditions: List[str] = field(default_factory=list)
    source_text: str = ""


@dataclass
class ImmunityException:
    """An exception that restores immunity despite a waiver."""
    exception_name: str
    statute: str
    description: str
    source_text: str = ""


@dataclass
class ImmunityAnalysis:
    """Complete immunity analysis for a defendant."""
    defendant_type: DefendantType
    immunity_type: ImmunityType
    waiver_status: WaiverStatus
    waivers: List[ImmunityWaiver] = field(default_factory=list)
    exceptions: List[ImmunityException] = field(default_factory=list)
    causes_of_action: List[str] = field(default_factory=list)
    ultra_vires: bool = False
    oath_required: bool = False
    oath_statute: str = ""
    bond_required: bool = False
    explanation_chain: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_frontmatter(self) -> dict:
        return {
            "defendant_type": self.defendant_type,
            "immunity_type": self.immunity_type,
            "waiver_status": self.waiver_status,
            "has_waivers": len(self.waivers) > 0,
            "causes_of_action": self.causes_of_action,
            "ultra_vires": self.ultra_vires,
        }


# ─── Immunity Database (Deterministic Lookup) ────────────────────────

# Statutory waivers of sovereign immunity
FEDERAL_WAIVERS = [
    ImmunityWaiver(
        statute="28 U.S.C. § 1346(b)",
        waiver_type="FTCA",
        scope="Tort claims against United States for negligent/wrongful acts of government employees acting within scope",
        exceptions=[
            "28 U.S.C. § 2680(a) — Discretionary function",
            "28 U.S.C. § 2680(b) — Loss of postal matter",
            "28 U.S.C. § 2680(c) — Tax/customs assessment",
            "28 U.S.C. § 2680(e) — Admiralty claims (separate statute)",
            "28 U.S.C. § 2680(h) — Intentional torts (with law enforcement exception)",
            "28 U.S.C. § 2680(j) — Combatant activities",
            "28 U.S.C. § 2680(k) — Foreign country claims",
        ],
        conditions=[
            "Must exhaust administrative remedies first",
            "Must file administrative claim within 2 years",
            "Must file suit within 6 months of denial",
        ],
    ),
    ImmunityWaiver(
        statute="28 U.S.C. § 1491",
        waiver_type="Tucker Act",
        scope="Claims against US founded on Constitution, statute, regulation, or contract (monetary only)",
        conditions=["Court of Federal Claims for claims over $10,000"],
    ),
    ImmunityWaiver(
        statute="28 U.S.C. § 1346(a)(2)",
        waiver_type="Little Tucker Act",
        scope="Claims against US up to $10,000 in district court",
    ),
    ImmunityWaiver(
        statute="5 U.S.C. § 702",
        waiver_type="APA",
        scope="Non-monetary relief against federal agencies — review of final agency action",
        conditions=[
            "Final agency action required",
            "No other adequate remedy in court",
            "Must be within zone of interests",
        ],
    ),
    ImmunityWaiver(
        statute="5 U.S.C. § 552(a)(4)(B)",
        waiver_type="FOIA",
        scope="District court jurisdiction to enjoin agency withholding records",
        conditions=["Must exhaust administrative remedies"],
    ),
    ImmunityWaiver(
        statute="5 U.S.C. § 552a(g)",
        waiver_type="Privacy Act",
        scope="Civil action against agency for Privacy Act violations",
    ),
    ImmunityWaiver(
        statute="42 U.S.C. § 1983",
        waiver_type="Section 1983",
        scope="Deprivation of rights under color of state law — damages and injunctive relief",
        conditions=["State action requirement", "Does not apply to federal officers"],
    ),
    ImmunityWaiver(
        statute="Bivens v. Six Unknown Named Agents, 403 U.S. 388 (1971)",
        waiver_type="Bivens",
        scope="Constitutional tort claims against federal officers in individual capacity",
        conditions=[
            "No alternative statutory remedy",
            "No special factors counseling hesitation",
            "Limited to 4th, 5th, 8th Amendment contexts",
        ],
    ),
]

# Absolute immunity categories
ABSOLUTE_IMMUNITY = {
    "judge": {
        "scope": "Judicial acts within jurisdiction",
        "exception": "Acts outside judicial capacity or in complete absence of jurisdiction",
        "source": "Stump v. Sparkman, 435 U.S. 349 (1978)",
    },
    "prosecutor": {
        "scope": "Acts intimately associated with the judicial phase of the criminal process",
        "exception": "Investigative or administrative functions",
        "source": "Imbler v. Pachtman, 424 U.S. 409 (1976)",
    },
    "legislator": {
        "scope": "Legislative acts within sphere of legitimate legislative activity",
        "exception": "Acts outside legislative function",
        "source": "U.S. Const. Art. I, § 6, Cl. 1; Gravel v. United States, 408 U.S. 606 (1972)",
    },
}

# Oath requirements
OATH_STATUTES = {
    "federal": "5 U.S.C. § 3331",
    "constitutional": "U.S. Const. Art. VI, Cl. 3",
    "state": "varies by state constitution",
}


# ─── Core Analysis ───────────────────────────────────────────────────

def analyze_immunity(defendant_type: str, conduct: str = "",
                     statutes_involved: list = None) -> ImmunityAnalysis:
    """
    Analyze immunity for a given defendant type and conduct.

    Args:
        defendant_type: one of DefendantType values
        conduct: description of the conduct at issue
        statutes_involved: list of statute citations involved

    Returns:
        ImmunityAnalysis with full chain of reasoning.
    """
    statutes_involved = statutes_involved or []
    result = ImmunityAnalysis(
        defendant_type=defendant_type,
        immunity_type="unknown",
        waiver_status="unknown",
    )

    chain = result.explanation_chain

    # Step 1: Classify immunity type
    if defendant_type in ("federal_government", "federal_agency"):
        result.immunity_type = "sovereign"
        chain.append(f"Defendant is {defendant_type} → sovereign immunity applies (default)")
        chain.append("Sovereign immunity must be waived by Congress — checking waivers")
        _check_federal_waivers(result, conduct, statutes_involved)

    elif defendant_type == "federal_officer":
        result.immunity_type = "qualified"
        chain.append("Defendant is federal officer → qualified immunity analysis")
        result.oath_required = True
        result.oath_statute = OATH_STATUTES["federal"]
        chain.append(f"Oath required: {result.oath_statute}")
        _check_qualified_immunity(result, conduct)
        _check_bivens(result, statutes_involved)

    elif defendant_type in ("state_government", "state_agency"):
        result.immunity_type = "sovereign"
        chain.append("Defendant is state entity → 11th Amendment sovereign immunity")
        chain.append("Checking Congressional abrogation and Ex parte Young exception")
        _check_state_immunity(result, statutes_involved)

    elif defendant_type == "state_officer":
        result.immunity_type = "qualified"
        chain.append("Defendant is state officer → qualified immunity + § 1983 analysis")
        result.oath_required = True
        result.oath_statute = OATH_STATUTES["state"]
        _check_qualified_immunity(result, conduct)
        _check_1983(result, statutes_involved)

    elif defendant_type in ("local_government", "local_officer"):
        result.immunity_type = "qualified"
        chain.append("Local government/officer → no sovereign immunity (Monell)")
        chain.append("Qualified immunity may apply to individual officers")
        _check_1983(result, statutes_involved)

    elif defendant_type == "judge":
        result.immunity_type = "absolute"
        chain.append("Defendant is judge → absolute immunity for judicial acts")
        _check_absolute_immunity(result, "judge", conduct)

    elif defendant_type == "prosecutor":
        result.immunity_type = "absolute"
        chain.append("Defendant is prosecutor → absolute immunity for prosecutorial acts")
        _check_absolute_immunity(result, "prosecutor", conduct)

    elif defendant_type == "legislator":
        result.immunity_type = "absolute"
        chain.append("Defendant is legislator → absolute immunity for legislative acts")
        _check_absolute_immunity(result, "legislator", conduct)

    elif defendant_type == "private_entity":
        result.immunity_type = "none"
        result.waiver_status = "waived"
        chain.append("Defendant is private entity → no immunity")

    return result


# BEEZLE_FN_UNUSED 2026-06-13: check_waiver (no static refs in pipeline/)
def check_waiver(text: str, citation: str = "") -> dict:
    """
    Quick check: does this statutory text contain an immunity waiver?
    Returns dict suitable for vault frontmatter.
    """
    text_lower = text.lower()
    waiver_indicators = {
        "sovereign_immunity_waiver": bool(re.search(
            r"(?:united\s+states|government)\s+(?:shall\s+be|is)\s+(?:liable|subject\s+to\s+suit)|"
            r"(?:consent|waive)(?:s|d)?\s+(?:to\s+(?:be\s+)?su(?:ed|it)|sovereign\s+immunity)|"
            r"(?:jurisdiction|right)\s+to\s+(?:bring|maintain|commence)\s+(?:a\s+)?(?:civil\s+)?action\s+against",
            text_lower
        )),
        "private_right_of_action": bool(re.search(
            r"(?:any|a)\s+person\s+(?:may|shall\s+have\s+the\s+right\s+to)\s+(?:bring|commence|maintain|file)",
            text_lower
        )),
        "fee_shifting": bool(re.search(
            r"(?:attorney|counsel)(?:'?s?)?\s+fees?",
            text_lower
        )),
    }
    return waiver_indicators


# ─── Internal Analysis Functions ──────────────────────────────────────

def _check_federal_waivers(result: ImmunityAnalysis, conduct: str,
                           statutes: list):
    """Check all federal sovereign immunity waivers."""
    chain = result.explanation_chain
    found_waiver = False

    for waiver in FEDERAL_WAIVERS:
        # Skip state-only waivers for federal defendants
        if waiver.waiver_type == "Section 1983":
            continue

        # Check if any involved statute matches this waiver
        waiver_applies = False
        if any(waiver.statute.split("§")[0].strip() in s for s in statutes):
            waiver_applies = True
        elif waiver.waiver_type == "FTCA" and any(
            kw in conduct.lower() for kw in ["tort", "negligent", "wrongful", "injury"]
        ):
            waiver_applies = True
        elif waiver.waiver_type == "APA" and any(
            kw in conduct.lower() for kw in ["agency action", "final action", "arbitrary", "capricious"]
        ):
            waiver_applies = True
        elif waiver.waiver_type == "FOIA" and "foia" in conduct.lower():
            waiver_applies = True
        elif waiver.waiver_type == "Privacy Act" and "privacy" in conduct.lower():
            waiver_applies = True

        if waiver_applies:
            result.waivers.append(waiver)
            chain.append(f"WAIVER FOUND: {waiver.waiver_type} ({waiver.statute})")
            chain.append(f"  Scope: {waiver.scope}")
            for exc in waiver.exceptions:
                chain.append(f"  Exception: {exc}")
                result.exceptions.append(ImmunityException(
                    exception_name=exc.split("—")[1].strip() if "—" in exc else exc,
                    statute=exc.split("—")[0].strip() if "—" in exc else "",
                    description=exc,
                ))
            for cond in waiver.conditions:
                chain.append(f"  Condition: {cond}")
            result.causes_of_action.append(waiver.waiver_type)
            found_waiver = True

    if found_waiver:
        result.waiver_status = "waived"
        if result.exceptions:
            result.waiver_status = "partial"
            chain.append("Waiver is PARTIAL — exceptions may apply")
    else:
        result.waiver_status = "intact"
        chain.append("No applicable waiver found — sovereign immunity intact")


def _check_qualified_immunity(result: ImmunityAnalysis, conduct: str):
    """Check qualified immunity two-prong test."""
    chain = result.explanation_chain
    chain.append("Qualified immunity two-prong test:")
    chain.append("  1. Did the official violate a constitutional right?")
    chain.append("  2. Was the right 'clearly established' at the time?")
    chain.append("  → Both prongs must be met to overcome qualified immunity")
    chain.append("  → Court may address prongs in either order (Pearson v. Callahan, 555 U.S. 223)")

    # Ultra vires check
    ultra_vires_indicators = [
        "without authority", "exceeded authority", "outside jurisdiction",
        "unauthorized", "ultra vires", "no legal authority",
        "without statutory basis", "beyond scope",
    ]
    if any(ind in conduct.lower() for ind in ultra_vires_indicators):
        result.ultra_vires = True
        chain.append("ULTRA VIRES DETECTED — officer acted outside authority")
        chain.append("  → Qualified immunity does not protect ultra vires acts")


def _check_absolute_immunity(result: ImmunityAnalysis, role: str, conduct: str):
    """Check absolute immunity and its exceptions."""
    chain = result.explanation_chain
    imm = ABSOLUTE_IMMUNITY.get(role, {})

    chain.append(f"Absolute immunity scope: {imm.get('scope', 'N/A')}")
    chain.append(f"Exception: {imm.get('exception', 'N/A')}")
    chain.append(f"Source: {imm.get('source', 'N/A')}")

    # Check for acts outside authority
    outside_indicators = [
        "administrative", "investigative", "ministerial",
        "without jurisdiction", "outside jurisdiction",
        "non-judicial", "non-prosecutorial", "non-legislative",
        "personal", "private capacity",
    ]
    if any(ind in conduct.lower() for ind in outside_indicators):
        result.ultra_vires = True
        result.waiver_status = "excepted"
        chain.append(f"EXCEPTION TRIGGERED — conduct appears outside {role} function")
        chain.append(f"  → Absolute immunity does NOT apply to acts outside official function")
        result.causes_of_action.append(f"§ 1983 (if state) or Bivens (if federal) — {role} acting outside function")
    else:
        result.waiver_status = "intact"
        chain.append(f"Conduct appears within {role} function — absolute immunity likely applies")
        chain.append(f"  Alternative: Judicial misconduct complaint under 28 U.S.C. §§ 351-364")
        result.causes_of_action.append("28 U.S.C. §§ 351-364 — Judicial Conduct and Disability Act")
        result.causes_of_action.append("28 U.S.C. § 455 — Disqualification")


def _check_bivens(result: ImmunityAnalysis, statutes: list):
    """Check Bivens availability for federal officers."""
    chain = result.explanation_chain
    bivens = next((w for w in FEDERAL_WAIVERS if w.waiver_type == "Bivens"), None)
    if bivens:
        result.waivers.append(bivens)
        chain.append(f"Bivens action available: {bivens.scope}")
        for cond in bivens.conditions:
            chain.append(f"  Condition: {cond}")
        result.causes_of_action.append("Bivens (constitutional tort)")


def _check_1983(result: ImmunityAnalysis, statutes: list):
    """Check § 1983 availability."""
    chain = result.explanation_chain
    sec_1983 = next((w for w in FEDERAL_WAIVERS if w.waiver_type == "Section 1983"), None)
    if sec_1983:
        result.waivers.append(sec_1983)
        result.waiver_status = "waived"
        chain.append(f"42 U.S.C. § 1983: {sec_1983.scope}")
        chain.append("  § 1983 text: 'Every person who... shall be liable'")
        chain.append("  Note: 'Every person' = text says liable, immunity is judicial overlay")
        result.causes_of_action.append("42 U.S.C. § 1983")


def _check_state_immunity(result: ImmunityAnalysis, statutes: list):
    """Check state sovereign immunity under 11th Amendment."""
    chain = result.explanation_chain
    chain.append("11th Amendment bars suits against states in federal court (default)")
    chain.append("Exceptions:")
    chain.append("  1. Congressional abrogation under § 5 of 14th Amendment")
    chain.append("  2. Ex parte Young — injunctive relief against state officer")
    chain.append("  3. State consent to suit")

    # Check for Ex parte Young
    result.causes_of_action.append("Ex parte Young, 209 U.S. 123 (1908) — injunctive relief against officer")
    result.waiver_status = "partial"


# BEEZLE_FN_UNUSED 2026-06-13: format_immunity_report (no static refs in pipeline/)
def format_immunity_report(analysis: ImmunityAnalysis) -> str:
    """Format immunity analysis as a readable report."""
    lines = [f"IMMUNITY ANALYSIS — {analysis.defendant_type}"]
    lines.append(f"  Type: {analysis.immunity_type}")
    lines.append(f"  Status: {analysis.waiver_status}")
    if analysis.ultra_vires:
        lines.append(f"  *** ULTRA VIRES — acting outside authority ***")
    if analysis.oath_required:
        lines.append(f"  Oath required: {analysis.oath_statute}")

    if analysis.waivers:
        lines.append(f"\n  WAIVERS:")
        for w in analysis.waivers:
            lines.append(f"    {w.waiver_type} ({w.statute}): {w.scope[:80]}")

    if analysis.causes_of_action:
        lines.append(f"\n  AVAILABLE CAUSES OF ACTION:")
        for c in analysis.causes_of_action:
            lines.append(f"    → {c}")

    lines.append(f"\n  REASONING CHAIN:")
    for step in analysis.explanation_chain:
        lines.append(f"    {step}")

    return "\n".join(lines)
