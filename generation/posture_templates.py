"""
posture_templates.py — BLOCK_6_13_POSTURE_LOCK

Single source of truth for posture-dependent language.
All renderers (drafter, finalizer, matrix) import from here.
NO LLM. Update one dict to change wording everywhere.
"""
from __future__ import annotations

# -------- Section II.A.5 — per-claim due-process relief footer --------
DUE_PROCESS_RELIEF_FOOTER = {
    "enforcing_judgment": (
        "ON THIS CLAIM, THE COURT MUST STRIKE DEFECTIVE ENFORCEMENT-PHASE "
        "FILINGS AND COMPEL PROCEDURALLY COMPLIANT NOTICE AND RESPONSE. "
        "Any enforcement-phase deprivation taken without procedurally compliant "
        "notice is void as against Plaintiff and cannot defeat Plaintiff's "
        "valid judgment. NO DISCRETION APPLIES on the rule-bound enforcement "
        "obligations identified above."
    ),
    "attacking_judgment": (
        "ON THIS CLAIM, THE COURT MUST GRANT VACATUR. The controlling "
        "authorities establish a void judgment and/or due process deprivation. "
        "NO DISCRETION APPLIES; the Court is bound to set aside the prior "
        "judgment and restore the moving party to the position pre-deprivation."
    ),
    "merits": (
        "On this claim, the Court shall enforce the duties identified above; "
        "defendant carries the burden of showing compliance."
    ),
}

# -------- Section II.B — post-doctrine paragraph --------
II_B_DOCTRINE_PARAGRAPH = {
    "enforcing_judgment": (
        "Because Defendants' enforcement-phase conduct rests on defective "
        "procedure and on non-binding authority asserted as if controlling, "
        "any enforcement-phase order entered on that basis is void as against "
        "Plaintiff and confers no authority to withhold property owed to "
        "Plaintiff under the underlying judgment."
    ),
    "attacking_judgment": (
        "Because the prior judgment rests on defective procedure and on "
        "non-binding authority asserted as if controlling, that judgment is "
        "void as a matter of law and must be vacated. No discretionary "
        "balancing applies; a void judgment confers no rights and supports "
        "no enforcement."
    ),
    "merits": (
        "Because defendant relies on non-binding authority on the issues "
        "presented, that reliance fails as a matter of law and cannot support "
        "the relief defendant seeks."
    ),
}

# -------- Section II.C — top-of-block summary --------
II_C_SUMMARY = {
    "enforcing_judgment": (
        "PLAINTIFF HAS ESTABLISHED {joined}. ANY ENFORCEMENT-PHASE "
        "DEPRIVATION TAKEN WITHOUT PROCEDURALLY COMPLIANT NOTICE IS VOID "
        "AS AGAINST PLAINTIFF AND CANNOT DEFEAT PLAINTIFF'S VALID JUDGMENT. "
        "THE COURT MUST STRIKE DEFECTIVE ENFORCEMENT FILINGS, COMPEL "
        "COMPLIANT RESPONSE, AND RESTORE TO PLAINTIFF ANY PROPERTY "
        "WITHHELD WITHOUT DUE PROCESS. NO DISCRETION APPLIES; THE COURT "
        "HAS NO POWER TO EXCUSE NON-COMPLIANCE WITH THE PROCEDURAL AND "
        "CONSTITUTIONAL REQUIREMENTS IDENTIFIED ABOVE."
    ),
    "attacking_judgment": (
        "PLAINTIFF HAS ESTABLISHED {joined}. THE PRIOR JUDGMENT IS VOID "
        "AS A MATTER OF LAW. THE COURT MUST VACATE. NO DISCRETION APPLIES; "
        "THE COURT HAS NO POWER TO EXCUSE NON-COMPLIANCE WITH PROCEDURAL "
        "DUE PROCESS OR WITH THE VOIDNESS DOCTRINES IDENTIFIED ABOVE."
    ),
    "merits": (
        "PLAINTIFF HAS ESTABLISHED {joined}. THE COURT SHALL ENFORCE THE "
        "DUTIES IDENTIFIED ABOVE AS WRITTEN."
    ),
}

# -------- Section I — posture clarifier (one sentence, top of brief) --------
POSTURE_CLARIFIER = {
    "enforcing_judgment": (
        "Plaintiff does not challenge the validity of the underlying judgment, "
        "which was properly entered in Plaintiff's favor. Plaintiff challenges "
        "only Defendants' failure to comply with procedural and constitutional "
        "requirements in the enforcement and garnishment phase."
    ),
    "attacking_judgment": (
        "The moving party challenges the validity of the prior judgment as "
        "void for lack of due process. The relief sought is vacatur and "
        "renewed proceedings on proper service."
    ),
    "merits": "",
}

# -------- finalize_brief.py — state-action anchor --------
STATE_ACTION_ANCHOR = {
    "enforcing_judgment": (
        "The deprivation arises from Defendants' enforcement-phase conduct "
        "and from any judicial action taken on that conduct without "
        "procedurally compliant notice and opportunity to be heard. "
        "Judicial enforcement-phase action constitutes state action for "
        "purposes of Fourteenth Amendment due process analysis. The "
        "constitutional duties enumerated below therefore apply directly to "
        "the enforcement-phase deprivation, not to the underlying judgment."
    ),
    "attacking_judgment": (
        "The deprivation arises from this Court's entry and continued "
        "enforcement of a judgment obtained without constitutionally "
        "adequate notice and opportunity to be heard. That judicial action "
        "constitutes state action for purposes of Fourteenth Amendment "
        "due process analysis. The constitutional duties enumerated below "
        "therefore apply directly to the judgment under review."
    ),
    "merits": (
        "The deprivation, if any, arises from state action taken without "
        "procedurally compliant notice and opportunity to be heard. The "
        "constitutional duties enumerated below therefore apply."
    ),
}

# -------- violations_matrix.py — per-duty CONST relief --------
MATRIX_CONST_RELIEF = {
    "enforcing_judgment": (
        ">>> RELIEF MANDATED: DUE PROCESS VIOLATION ESTABLISHED IN THE "
        "ENFORCEMENT PHASE. ANY ENFORCEMENT-PHASE DEPRIVATION TAKEN WITHOUT "
        "PROCEDURALLY COMPLIANT NOTICE IS VOID AS AGAINST PLAINTIFF. THE "
        "COURT MUST STRIKE DEFECTIVE ENFORCEMENT FILINGS AND RESTORE TO "
        "PLAINTIFF ANY PROPERTY WITHHELD WITHOUT DUE PROCESS."
    ),
    "attacking_judgment": (
        ">>> RELIEF MANDATED: DUE PROCESS VIOLATION ESTABLISHED. THE "
        "JUDGMENT IS VOID. THE COURT MUST VACATE AND RESTORE THE MOVING "
        "PARTY TO POSITION PRE-DEPRIVATION."
    ),
    "merits": (
        ">>> RELIEF MANDATED: DUE PROCESS VIOLATION ESTABLISHED. THE COURT "
        "MUST PROVIDE PROCEDURALLY COMPLIANT NOTICE AND OPPORTUNITY TO BE "
        "HEARD BEFORE ANY DEPRIVATION ISSUES."
    ),
}

# -------- violations_matrix.py — per-duty VOID relief (FRCP 60(b)(4) etc) --------
MATRIX_VOID_RELIEF = {
    "enforcing_judgment": (
        ">>> RELIEF MANDATED: ANY ENFORCEMENT-PHASE ORDER ENTERED WITHOUT "
        "PROCEDURALLY COMPLIANT NOTICE IS VOID AS AGAINST PLAINTIFF. THE "
        "COURT MUST STRIKE THE DEFECTIVE ENFORCEMENT-PHASE FILING. NO "
        "DISCRETION APPLIES."
    ),
    "attacking_judgment": (
        ">>> RELIEF MANDATED: THE JUDGMENT IS VOID. THE COURT MUST VACATE. "
        "NO DISCRETION APPLIES."
    ),
    "merits": (
        ">>> RELIEF MANDATED: The cited authority establishes voidness on "
        "the grounds stated; the Court shall act accordingly upon proper motion."
    ),
}

# -------- violations_matrix.py — section summary for CONST tier --------
MATRIX_CONST_SECTION_SUMMARY = {
    "enforcing_judgment": (
        ">>> SECTION RELIEF SUMMARY: {count} CONSTITUTIONAL VIOLATION(S) "
        "ESTABLISHED IN THE ENFORCEMENT PHASE. ANY ENFORCEMENT-PHASE "
        "DEPRIVATION ON THIS BASIS IS VOID AS AGAINST PLAINTIFF. THE "
        "COURT HAS NO DISCRETION TO EXCUSE."
    ),
    "attacking_judgment": (
        ">>> SECTION RELIEF SUMMARY: {count} CONSTITUTIONAL VIOLATION(S) "
        "ESTABLISHED. THE JUDGMENT IS VOID AS A MATTER OF LAW. THE COURT "
        "HAS NO DISCRETION TO EXCUSE."
    ),
    "merits": (
        ">>> SECTION RELIEF SUMMARY: {count} CONSTITUTIONAL VIOLATION(S) "
        "ESTABLISHED. THE COURT SHALL PROVIDE COMPLIANT PROCESS."
    ),
}

# -------- violations_matrix.py — consolidated relief vacatur lines --------
MATRIX_CONSOLIDATED_VACATUR = {
    "enforcing_judgment": (
        "  - STRIKE all enforcement-phase filings made without procedurally "
        "compliant notice, based on {count} established constitutional due "
        "process violation(s)."
    ),
    "attacking_judgment": (
        "  - VACATE the judgment based on {count} established constitutional "
        "due process violation(s)."
    ),
    "merits": "",
}

MATRIX_CONSOLIDATED_VOID = {
    "enforcing_judgment": (
        "  - STRIKE any enforcement-phase order entered without procedurally "
        "compliant authority, based on {count} void-doctrine ground(s) established."
    ),
    "attacking_judgment": (
        "  - VACATE the judgment based on {count} void-judgment doctrine(s) "
        "established."
    ),
    "merits": "",
}


def get(template_dict: dict, posture: str) -> str:
    """Safe lookup with merits fallback."""
    return template_dict.get(posture) or template_dict.get("merits", "")


# === BLOCK_6_13_2_POSTURE_FINAL_POLISH — additional posture templates ===

# Posture-aware label rewrites for raw engine_facts that surface in II.A.5
# triggering-facts list. Keys are lowercased fact strings.
TRIGGER_LABEL_REWRITE = {
    "enforcing_judgment": {
        "no proper service": "no proper service of garnishment process / interrogatories",
        "no notice": "no procedurally compliant notice in enforcement proceedings",
        "default judgment without proper service": "garnishee defaulted without procedurally compliant service",
        "service defective": "defective service of enforcement-phase process",
        "lack of jurisdiction": "no statutory or rule-based authority for enforcement-phase action taken",
        "due process violation": "deprivation of judgment proceeds without procedurally compliant process",
    },
    "attacking_judgment": {
        # raw labels are correct for collateral-attack posture
    },
    "merits": {},
}

# Closing line of consolidated relief block in violations_matrix
MATRIX_CLOSING_LINE = {
    "enforcing_judgment": (
        "The mandatory relief identified above is non-discretionary on the "
        "facts established. The Court is bound to deny any relief premised on "
        "procedurally defective enforcement actions and to enforce Plaintiff's "
        "valid judgment as entered."
    ),
    "attacking_judgment": (
        "The mandatory relief identified above is non-discretionary on the "
        "facts established. The Court is bound to vacate any void judgment "
        "and to enforce each procedural rule and statutory remedy as written."
    ),
    "merits": (
        "The relief identified above is rule-bound. The Court shall enforce "
        "each procedural rule and statutory remedy as written."
    ),
}

# Fallback mandatory-relief line (rare: posture says enforcing but no const/void
# duties fired). Keeps the section meaningful instead of printing an empty header.
MATRIX_MANDATORY_FALLBACK = {
    "enforcing_judgment": (
        "  - STRIKE all enforcement-phase filings made without procedurally "
        "compliant notice, and COMPEL compliant response under Pa.R.C.P. 1024 / "
        "3145 / 3146."
    ),
    "attacking_judgment": "",
    "merits": "",
}
# === END BLOCK_6_13_2_POSTURE_FINAL_POLISH ===
