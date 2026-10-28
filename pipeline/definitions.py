"""
pipeline/definitions.py
========================
Shared legal vocabulary for the audit pipeline.

Every violation type, every doctrine, every sanction trigger should
resolve to the same definition wherever it appears: block14 detector,
judicial_order_scanner, derived_violations module, scorecard renderer,
and motion drafter. Without a shared definition layer, the same term
("sua sponte", "bill of attainder", "immunity waiver") drifts between
modules and the final filing reads inconsistently.

PUBLIC API
----------
get_definition(term) -> Dict        # full definition
get_brief(term) -> str              # one-line gloss for the scorecard
list_terms() -> List[str]
explain_violation(violation_dict) -> str   # human-readable explanation
"""
from __future__ import annotations

import re
from typing import Dict, List, Any, Optional


# ============================================================================
# DEFINITIONS
# Each definition has:
#   term:          canonical name
#   brief:         one-sentence gloss for the scorecard
#   full:          full doctrinal definition for the motion
#   authority:     binding precedent or statute that establishes the term
#   triggers_when: observable conditions that establish a violation
#   remedy:        the procedural remedy when the violation is found
#   void_inducing: True if the violation makes the underlying order void
#                  under FRCP 60(b)(4)
# ============================================================================

DEFINITIONS: Dict[str, Dict[str, Any]] = {

    # -------- PROCEDURAL --------
    "sua_sponte_ruling": {
        "term": "Sua Sponte Ruling",
        "brief": "Court issues a substantive ruling on its own motion, without briefing or notice to the parties.",
        "full": (
            "A sua sponte ruling is one the court initiates without a motion "
            "from any party. Sua sponte rulings on the merits — without "
            "notice or opportunity to be heard — generally violate procedural "
            "due process under the Fifth and Fourteenth Amendments. The "
            "Third Circuit requires notice and opportunity to respond before "
            "sua sponte dismissal."
        ),
        "authority": (
            "Mathews v. Eldridge, 424 U.S. 319 (1976); "
            "Bryson v. Brand Insulations, Inc., 621 F.2d 556 (3d Cir. 1980) "
            "(notice required before sua sponte dismissal); "
            "Oatess v. Sobolevitch, 914 F.2d 428 (3d Cir. 1990)"
        ),
        "triggers_when": [
            "Court rules on a motion not filed by any party",
            "Court dismisses claims without prior briefing on the issue",
            "Court excludes a defendant without notice or hearing",
            "Order issues without responsive briefing on docket",
        ],
        "remedy": "Vacate under FRCP 60(b)(4); preserve for appellate review.",
        "void_inducing": True,
        "regex_triggers": [
            r"\bsua\s+sponte\b",
            r"\bon\s+(?:the\s+)?Court'?s?\s+own\s+motion\b",
            r"\bof\s+its\s+own\s+motion\b",
            r"\bwithout\s+(?:briefing|hearing|notice|response)\b.*\b(?:dismiss|exclud|strik)",
        ],
    },

    "prior_restraint_filing_ban": {
        "term": "Prior Restraint / Filing Injunction",
        "brief": "Court order forbidding a litigant from filing without prior leave — a First Amendment prior restraint.",
        "full": (
            "A filing injunction is a restraint imposed by a court on a "
            "litigant's right of access to the courts. Under the Third "
            "Circuit's Safir/Chipps test, such an injunction is "
            "constitutionally permissible only if: (1) the litigant received "
            "notice and opportunity to respond; (2) the court compiled an "
            "adequate factual record of frivolousness or harassment; (3) the "
            "court made specific findings; and (4) the injunction is "
            "narrowly tailored. Failure of any factor renders the order void."
        ),
        "authority": (
            "U.S. Const. amend. I; "
            "In re Safir, 2021 WL 5906089 (3d Cir.); "
            "Chipps v. U.S. Dist. Court, 882 F.2d 72 (3d Cir. 1989); "
            "Alexander v. United States, 509 U.S. 544 (1993); "
            "Nebraska Press Ass'n v. Stuart, 427 U.S. 539 (1976)"
        ),
        "triggers_when": [
            "Order tells litigant 'do not file' or 'shall not file'",
            "Order requires 'leave of court' to file future motions",
            "Order labels litigant 'vexatious' without Safir findings",
            "Order issues filing ban sua sponte (no motion, no briefing)",
        ],
        "remedy": (
            "Emergency motion to vacate as unconstitutional prior restraint; "
            "Petition for Writ of Mandamus under 28 U.S.C. § 1651."
        ),
        "void_inducing": True,
        "regex_triggers": [
            r"(?:do\s+not|shall\s+not|may\s+not|is\s+(?:hereby\s+)?prohibited\s+from)\s+fil(?:e|ing)",
            r"(?:filing\s+(?:injunction|restriction|ban|bar|prohibition))",
            r"(?:pre[-\s]?filing\s+(?:injunction|review|screening|restriction))",
            r"(?:leave\s+of\s+(?:court|this\s+court)\s+(?:before|prior\s+to|to)\s+fil(?:e|ing))",
            r"(?:no\s+(?:further|additional|new)\s+(?:actions?|complaints?|filings?|motions?))",
            r"\bvexatious\s+litigant\b",
        ],
    },

    # -------- LABELING / AD HOMINEM --------
    "sovereign_citizen_label": {
        "term": "Sovereign-Citizen Mislabeling",
        "brief": "Court characterizes a litigant as a sovereign citizen without evidentiary basis, using the label as a substitute for legal analysis.",
        "full": (
            "When a court labels a pro se litigant a 'sovereign citizen' "
            "without grounding the label in specific record evidence (e.g., "
            "filings invoking the redemption movement, the Strawman doctrine, "
            "or Russell-Jay Gould parse syntax grammar), the label functions "
            "as ad hominem dismissal and creates an appearance of "
            "prejudgment. Standing precedent prohibits use of a pejorative "
            "label as the substitute for merits analysis."
        ),
        "authority": (
            "U.S. Const. amend. V; "
            "Caperton v. A.T. Massey Coal Co., 556 U.S. 868 (2009) "
            "(appearance of bias); "
            "Liteky v. United States, 510 U.S. 540 (1994) "
            "(judicial rulings must rest on the record); "
            "Liljeberg v. Health Servs., 486 U.S. 847 (1988)"
        ),
        "triggers_when": [
            "Order labels litigant 'sovereign citizen' without filings invoking that movement",
            "Order dismisses claims on the basis of the label rather than the merits",
            "Order treats pro se litigant's terminology as sovereign-citizen rhetoric without grounding",
        ],
        "remedy": (
            "Move for reconsideration; demand record citations supporting "
            "the characterization; preserve for appellate review under "
            "Caperton / Liljeberg appearance-of-bias standard."
        ),
        "void_inducing": False,  # serious but not automatically void
        "regex_triggers": [
            r"\bsovereign\s+citizen\b",
            r"\bsovereign\s+citizen[- ]like\b",
            r"\bsov\s*cit\b",
            r"\bredemption\s+movement\b",
            r"\bstrawman\s+(?:theory|doctrine)\b",
        ],
    },

    "bill_of_attainder_mislabel": {
        "term": "Bill-of-Attainder Mischaracterization",
        "brief": "Court invokes the Bill of Attainder Clause where it does not apply (the clause restrains legislatures, not courts) or applies it backwards against the litigant.",
        "full": (
            "Article I, § 9 of the U.S. Constitution forbids Congress (and "
            "the states under Article I, § 10) from passing bills of "
            "attainder. The clause applies to LEGISLATIVE punishment without "
            "trial. Courts do not pass bills of attainder; the clause "
            "cannot be invoked by a court to characterize a litigant's "
            "filings, nor against a pro se plaintiff for seeking relief. "
            "Use of the term against a litigant evidences either factual or "
            "legal misapplication."
        ),
        "authority": (
            "U.S. Const. art. I, §§ 9, 10; "
            "United States v. Lovett, 328 U.S. 303 (1946); "
            "Selective Service System v. Minnesota PIRG, 468 U.S. 841 (1984); "
            "Nixon v. Adm'r of Gen. Servs., 433 U.S. 425 (1977)"
        ),
        "triggers_when": [
            "Court characterizes plaintiff's filings as a 'bill of attainder' against defendants",
            "Court applies the Bill of Attainder Clause without identifying a legislative act",
            "Court treats a civil complaint as 'punishment without trial'",
        ],
        "remedy": (
            "Move for reconsideration on the ground that the Bill of "
            "Attainder Clause does not apply to civil pleadings; cite "
            "Lovett's legislative-punishment requirement."
        ),
        "void_inducing": True,  # if dispositive of a ruling, that ruling rests on legal error
        "regex_triggers": [
            r"\bbill\s+of\s+attainder\b",
            r"\bart(?:icle|\.|s)?\s*I\b.*\battainder\b",
        ],
    },

    # -------- IMMUNITY --------
    "immunity_waiver_fraud_on_court": {
        "term": "Immunity Waiver — Fraud on the Court",
        "brief": "Judicial immunity does not protect a judge from declaratory or injunctive relief, nor from civil liability where the act was outside the judicial function.",
        "full": (
            "Judicial immunity is qualified, not absolute. It does not "
            "extend to: (1) acts taken in the clear absence of all "
            "jurisdiction (Bradley v. Fisher); (2) non-judicial acts such "
            "as administrative or ministerial functions (Forrester v. White, "
            "Mireles v. Waco); (3) declaratory or injunctive relief sought "
            "prospectively against a judge in official capacity (Pulliam v. "
            "Allen; Ex parte Young); and (4) fraud upon the court (Hazel-"
            "Atlas Glass Co. v. Hartford-Empire Co.; Universal Oil Prods. "
            "Co. v. Root Refining Co.)."
        ),
        "authority": (
            "Hazel-Atlas Glass Co. v. Hartford-Empire Co., 322 U.S. 238 (1944); "
            "Forrester v. White, 484 U.S. 219 (1988); "
            "Mireles v. Waco, 502 U.S. 9 (1991); "
            "Bradley v. Fisher, 80 U.S. 335 (1871); "
            "Pulliam v. Allen, 466 U.S. 522 (1984); "
            "Ex parte Young, 209 U.S. 123 (1908); "
            "Stump v. Sparkman, 435 U.S. 349 (1978) (boundaries)"
        ),
        "triggers_when": [
            "Judicial act taken in clear absence of jurisdiction",
            "Order rests on knowingly false statements of material fact",
            "Order issues on the basis of fabricated or doctored evidence",
            "Administrative/ministerial acts (scheduling, docketing) — immunity does not attach",
            "Order issues sua sponte on the merits — outside ordinary judicial process",
        ],
        "remedy": (
            "FRCP 60(d)(3) independent action (no time bar); civil "
            "liability under § 1983 for prospective relief; refer to "
            "Judicial Council under 28 U.S.C. § 351."
        ),
        "void_inducing": True,
        "regex_triggers": [
            r"\b(?:judicial\s+)?immunity\s+waiver\b",
            r"\bHazel[- ]Atlas\b",
            r"\bForrester\s+v\.?\s+White\b",
            r"\bPulliam\s+v\.?\s+Allen\b",
            r"\bclear\s+absence\s+of\s+(?:all\s+)?jurisdiction\b",
        ],
    },

    # -------- CIVIL RIGHTS --------
    "color_of_law_deprivation": {
        "term": "Deprivation of Rights Under Color of Law (§ 242 / § 1983)",
        "brief": "An act taken by a state actor that deprives a person of constitutional rights without due process is actionable under 42 U.S.C. § 1983 and may be criminal under 18 U.S.C. § 242.",
        "full": (
            "18 U.S.C. § 242 criminalizes willful deprivations of "
            "constitutional rights under color of any law, statute, "
            "ordinance, or regulation. 42 U.S.C. § 1983 provides the civil "
            "counterpart: any state actor who deprives a person of rights "
            "secured by the Constitution may be held liable. Acting under "
            "color of law means exercising power possessed by virtue of "
            "state law or made possible only because the actor is clothed "
            "with the authority of state law."
        ),
        "authority": (
            "18 U.S.C. § 242; 42 U.S.C. § 1983; "
            "Screws v. United States, 325 U.S. 91 (1945); "
            "Monroe v. Pape, 365 U.S. 167 (1961); "
            "West v. Atkins, 487 U.S. 42 (1988); "
            "Lugar v. Edmondson Oil Co., 457 U.S. 922 (1982)"
        ),
        "triggers_when": [
            "State actor (or party acting in concert) deprives a person of constitutional rights",
            "Deprivation occurs without procedural due process",
            "Conduct is willful in the § 242 sense (specific intent to deprive)",
        ],
        "remedy": (
            "File § 1983 complaint (civil); refer to U.S. Attorney for "
            "§ 242 review where appropriate."
        ),
        "void_inducing": False,
        "regex_triggers": [
            r"\bunder\s+color\s+of\s+(?:law|state\s+law|office|authority)\b",
            r"\b42\s+U\.\s*S\.\s*C\.\s*§+\s*1983\b",
            r"\b18\s+U\.\s*S\.\s*C\.\s*§+\s*242\b",
            r"\bdeprivation\s+of\s+(?:constitutional\s+)?rights\b",
        ],
    },

    # -------- CONSTITUTIONAL --------
    "procedural_due_process": {
        "term": "Procedural Due Process Violation",
        "brief": "Government deprived a person of a protected liberty or property interest without notice and a meaningful opportunity to be heard.",
        "full": (
            "Procedural due process under the Fifth and Fourteenth "
            "Amendments requires, at a minimum, notice reasonably calculated "
            "to apprise interested parties of the pendency of the action "
            "(Mullane) and a meaningful opportunity to be heard (Goldberg). "
            "The Mathews v. Eldridge balancing test weighs the private "
            "interest, the risk of erroneous deprivation, and the "
            "government's interest. A court order that issues without "
            "notice or opportunity to respond — particularly sua sponte "
            "rulings affecting substantial rights — violates this guarantee."
        ),
        "authority": (
            "U.S. Const. amends. V, XIV; "
            "Mullane v. Cent. Hanover Bank & Trust Co., 339 U.S. 306 (1950); "
            "Mathews v. Eldridge, 424 U.S. 319 (1976); "
            "Goldberg v. Kelly, 397 U.S. 254 (1970); "
            "Cleveland Bd. of Educ. v. Loudermill, 470 U.S. 532 (1985)"
        ),
        "triggers_when": [
            "Order issues without notice to affected party",
            "Order issues without opportunity to be heard",
            "Order rests on facts not in the record",
            "Order contradicts the docket without addressing the contradiction",
        ],
        "remedy": "Vacate under FRCP 60(b)(4); appeal; § 1983 for prospective relief.",
        "void_inducing": True,
        "regex_triggers": [
            r"\bprocedural\s+due\s+process\b",
            r"\bdue\s+process\s+(?:clause|violation|defect)\b",
            r"\bMathews\s+v\.?\s+Eldridge\b",
            r"\bnotice\s+and\s+(?:meaningful\s+)?opportunity\s+to\s+be\s+heard\b",
        ],
    },
}


def get_definition(term: str) -> Optional[Dict[str, Any]]:
    """Look up a definition by canonical key."""
    return DEFINITIONS.get(term)


# BEEZLE_FN_UNUSED 2026-06-13: get_brief (no static refs in pipeline/)
def get_brief(term: str) -> str:
    """One-line gloss suitable for the scorecard."""
    d = DEFINITIONS.get(term)
    return d["brief"] if d else ""


def list_terms() -> List[str]:
    return sorted(DEFINITIONS.keys())


def map_violation_type_to_definition(violation_type: str) -> Optional[str]:
    """Block14 violation_type → definitions key."""
    return {
        "PRIOR_RESTRAINT_FILING_BAN":          "prior_restraint_filing_ban",
        "DO_NOT_FILE_LANGUAGE":                "prior_restraint_filing_ban",
        "PROCEDURAL_DUE_PROCESS_VIOLATION":    "procedural_due_process",
        "NOTICE_INADEQUATE":                   "procedural_due_process",
        "EX_PARTE_RULING":                     "sua_sponte_ruling",
        "DENIED_OPPORTUNITY_TO_RESPOND":       "procedural_due_process",
        "SUA_SPONTE_RULING":                   "sua_sponte_ruling",
        "SOVEREIGN_CITIZEN_LABEL":             "sovereign_citizen_label",
        "BILL_OF_ATTAINDER_MISLABEL":          "bill_of_attainder_mislabel",
        "IMMUNITY_WAIVER_TRIGGERED":           "immunity_waiver_fraud_on_court",
        "DEPRIVATION_OF_RIGHTS_COLOR_OF_LAW":  "color_of_law_deprivation",
    }.get(violation_type)


# BEEZLE_FN_UNUSED 2026-06-13: explain_violation (no static refs in pipeline/)
def explain_violation(violation: Dict[str, Any]) -> str:
    """Human-readable one-paragraph explanation of a violation."""
    vt = violation.get("violation_type") or violation.get("type", "")
    def_key = map_violation_type_to_definition(vt)
    if not def_key:
        return ""
    d = DEFINITIONS[def_key]
    return d["brief"]


# BEEZLE_FN_UNUSED 2026-06-13: boundary_context (no static refs in pipeline/)
def boundary_context(term: str) -> Dict[str, Any]:
    """Return scope and limits for a term (for the motion's analysis sections).

    Useful to explain WHEN a doctrine applies and when it doesn't —
    e.g., the Bill of Attainder Clause restrains LEGISLATURES, so a court
    cannot invoke it to characterize a civil complaint.
    """
    d = DEFINITIONS.get(term)
    if not d:
        return {}
    return {
        "term":            d["term"],
        "applies_to":      d.get("triggers_when", []),
        "authority":       d.get("authority", ""),
        "void_inducing":   d.get("void_inducing", False),
        "remedy":          d.get("remedy", ""),
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        term = sys.argv[1]
        d = get_definition(term)
        if d:
            print(f"\n{d['term']}")
            print("=" * 70)
            print(f"\nBRIEF:  {d['brief']}")
            print(f"\nFULL:\n{d['full']}")
            print(f"\nAUTHORITY:\n{d['authority']}")
            print(f"\nTRIGGERS WHEN:")
            for t in d.get("triggers_when", []):
                print(f"  - {t}")
            print(f"\nREMEDY: {d['remedy']}")
            print(f"VOID-INDUCING: {d['void_inducing']}")
        else:
            print(f"Term not found: {term}")
            print(f"Available: {', '.join(list_terms())}")
    else:
        print("Defined terms:")
        for t in list_terms():
            d = DEFINITIONS[t]
            void = " †" if d["void_inducing"] else ""
            print(f"  {t:<35} {d['brief'][:60]}{void}")
