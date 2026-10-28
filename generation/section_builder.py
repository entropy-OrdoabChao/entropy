"""
generation/section_builder.py — Deterministic Section Builder.

Splits a legal document into discrete sections that can each be generated
in a small LLM call. This solves the "21k input, 10-minute timeout" problem
by breaking drafting into:

  Section 1: Preamble (caption already in docx, this is the intro paragraph)
  Section 2: Rule Framework (statutes and rules in play)
  Section 3..N: One section per conflict (rule → fact → application → result)
  Section N+1: Conclusion / WHEREFORE

Each section gets ~2000-4000 chars of input and produces ~500-1500 chars of output.
Each LLM call should complete in 30-90 seconds with thinking OFF.

NO LLM in this module. Only assembly/templating.
The LLM is called by the chunked_drafter, which uses sections from this module.
"""

from dataclasses import dataclass, field
from typing import Optional

from core.logger import get_logger

log = get_logger("SECTION")


# ══════════════════════════════════════════════════════════════════════
# DATACLASSES
# ══════════════════════════════════════════════════════════════════════

@dataclass
class DraftSection:
    name: str  # internal section ID (preamble, rule_framework, argument_1, etc.)
    title: str  # printable section title (I. INTRODUCTION, II. ARGUMENT, etc.)
    section_type: str  # preamble | rule_framework | argument | conclusion | header
    instructions: str  # what the LLM should write for this section
    inputs: dict = field(default_factory=dict)  # data the LLM needs for this section
    expected_length: int = 800  # rough output target in chars

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "section_type": self.section_type,
            "instructions": self.instructions,
            "inputs": self.inputs,
            "expected_length": self.expected_length,
        }


# ══════════════════════════════════════════════════════════════════════
# DOCUMENT-SPECIFIC SECTION PLANS
# ══════════════════════════════════════════════════════════════════════

DOCUMENT_PLANS = {
    "opposition_brief": [
        ("preamble", "I. INTRODUCTION"),
        ("question_presented", "II. STATEMENT OF THE QUESTION PRESENTED"),
        ("relevant_facts", "III. RELEVANT FACTS"),
        ("argument_intro", "IV. ARGUMENT"),
        ("conflicts", "ARGUMENT_BODY"),  # expands to one section per conflict
        ("conclusion", "V. CONCLUSION"),
    ],
    "reply_brief": [
        ("preamble", "I. INTRODUCTION"),
        ("conflicts", "II. REPLY TO OPPOSITION"),
        ("conclusion", "III. CONCLUSION"),
    ],
    "motion_to_strike": [
        ("preamble", "I. INTRODUCTION"),
        ("rule_framework", "II. APPLICABLE RULES"),
        ("conflicts", "III. ARGUMENT"),
        ("conclusion", "IV. RELIEF REQUESTED"),
    ],
    "motion_to_compel": [
        ("preamble", "I. INTRODUCTION"),
        ("rule_framework", "II. APPLICABLE RULES"),
        ("conflicts", "III. ARGUMENT"),
        ("conclusion", "IV. RELIEF REQUESTED"),
    ],
    "preliminary_objections": [
        ("preamble", "I. INTRODUCTION"),
        ("rule_framework", "II. APPLICABLE RULES"),
        ("conflicts", "III. OBJECTIONS"),
        ("conclusion", "IV. RELIEF REQUESTED"),
    ],
    "complaint": [
        ("preamble", "I. INTRODUCTION"),
        ("parties", "II. PARTIES"),
        ("jurisdiction", "III. JURISDICTION AND VENUE"),
        ("relevant_facts", "IV. FACTUAL ALLEGATIONS"),
        ("counts", "V. CAUSES OF ACTION"),
        ("conclusion", "VI. PRAYER FOR RELIEF"),
    ],
    "demand_letter": [
        ("preamble", "INTRODUCTION"),
        ("relevant_facts", "FACTS"),
        ("rule_framework", "LEGAL BASIS"),
        ("conclusion", "DEMAND"),
    ],
    "regulatory_complaint": [
        ("preamble", "I. COMPLAINANT INFORMATION"),
        ("relevant_facts", "II. STATEMENT OF FACTS"),
        ("rule_framework", "III. VIOLATIONS"),
        ("conclusion", "IV. REQUESTED RELIEF"),
    ],
    # Default fallback
    "motion": [
        ("preamble", "I. INTRODUCTION"),
        ("rule_framework", "II. APPLICABLE LAW"),
        ("conflicts", "III. ARGUMENT"),
        ("conclusion", "IV. RELIEF REQUESTED"),
    ],
    # ===PHASE_3F_STEP6C_CHUNKED_V1=== motion_to_vacate plan
    "motion_to_vacate": [
        ("preamble", "I. INTRODUCTION"),
        ("dispositive_anchor", "II. DISPOSITIVE ORDER AT ISSUE"),
        # ===PHASE_3F_JUDICIAL_DUTY_V1=== judicial_duty_slot
        ("judicial_duty_breach", "III. JUDICIAL DUTY BREACH"),
        # ===END PHASE_3F_JUDICIAL_DUTY_V1===
        ("rule_framework", "IV. STANDARD OF REVIEW"),
        ("conflicts", "V. ARGUMENT"),
        ("evidence_ignored", "VI. FAILURE TO ADDRESS RECORD EVIDENCE"),
        ("conclusion", "VII. RELIEF REQUESTED"),
    ],
    "motion_for_reconsideration": [
        ("preamble", "I. INTRODUCTION"),
        ("dispositive_anchor", "II. DISPOSITIVE ORDER AT ISSUE"),
        # ===PHASE_3F_JUDICIAL_DUTY_V1=== judicial_duty_slot_recon
        ("judicial_duty_breach", "III. JUDICIAL DUTY BREACH"),
        # ===END PHASE_3F_JUDICIAL_DUTY_V1===
        ("rule_framework", "IV. STANDARD OF REVIEW"),
        ("conflicts", "V. ARGUMENT"),
        ("evidence_ignored", "VI. FAILURE TO ADDRESS RECORD EVIDENCE"),
        ("conclusion", "VII. RELIEF REQUESTED"),
    ],
    # ===END PHASE_3F_STEP6C_CHUNKED_V1===
}


# ══════════════════════════════════════════════════════════════════════
# SECTION BUILDER
# ══════════════════════════════════════════════════════════════════════

def build_sections(
    doc_type: str,
    case_context: dict = None,
    facts: list = None,
    authority: str = "",
    conflicts: list = None,
    procedural: dict = None,
) -> list:
    """Build a list of DraftSection objects for the given document type.

    NO LLM. Pure deterministic assembly.

    Args:
        doc_type: e.g. 'opposition_brief', 'reply_brief', 'motion_to_strike'
        case_context: dict with court, plaintiff, defendant, etc.
        facts: list of fact strings
        authority: DB-retrieved authority text
        conflicts: list of Conflict dicts from conflict_engine
        procedural: dict from procedural_engine

    Returns:
        list of DraftSection
    """
    case_context = case_context or {}
    facts = facts or []
    conflicts = conflicts or []
    procedural = procedural or {}

    plan = DOCUMENT_PLANS.get(doc_type, DOCUMENT_PLANS["motion"])
    sections = []

    for section_id, section_title in plan:
        if section_id == "conflicts":
            # Expand into one section per conflict
            conflict_sections = _build_conflict_sections(
                conflicts, case_context, authority
            )
            sections.extend(conflict_sections)
            continue

        # ===PHASE_3F_STEP6D_RECONNECT_V1=== inject dispositive + evidence_ignored
        if doc_type in ("motion_to_vacate", "motion_for_reconsideration"):
            disp_text = case_context.get("dispositive_text", "") or ""
            disp_orders = case_context.get("dispositive_orders", []) or []
            record_gaps = case_context.get("record_evidence_gaps", []) or []

            if section_id == "dispositive_anchor":
                sec = _build_dispositive_anchor_section(
                    case_context, disp_text, disp_orders
                )
                if sec:
                    sections.append(sec)
                continue

            if section_id == "evidence_ignored":
                sec = _build_evidence_ignored_section(
                    case_context, record_gaps, disp_orders
                )
                if sec:
                    sections.append(sec)
                continue
            # ===PHASE_3F_JUDICIAL_DUTY_V1=== judicial_duty_breach_dispatch
            if section_id == "judicial_duty_breach":
                jdd = case_context.get("judicial_duty_defects", []) or []
                sec = _build_judicial_duty_breach_section(case_context, jdd)
                if sec:
                    sections.append(sec)
                continue
            # ===END PHASE_3F_JUDICIAL_DUTY_V1===
        # ===END PHASE_3F_STEP6D_RECONNECT_V1===

        section = _build_single_section(
            section_id=section_id,
            section_title=section_title,
            doc_type=doc_type,
            case_context=case_context,
            facts=facts,
            authority=authority,
            conflicts=conflicts,
            procedural=procedural,
        )
        if section:
            sections.append(section)

    log.stage("build_sections",
              f"{doc_type}: {len(sections)} sections planned")
    return sections


def _build_single_section(
    section_id: str,
    section_title: str,
    doc_type: str,
    case_context: dict,
    facts: list,
    authority: str,
    conflicts: list,
    procedural: dict,
) -> Optional[DraftSection]:
    """Build a single non-conflict section."""

    plaintiff = case_context.get("plaintiff", "Plaintiff")
    defendant = case_context.get("defendant", "Defendant")
    garnishee = case_context.get("garnishee", "")
    case_no = case_context.get("case_no", "")
    court = case_context.get("court", "")
    opposing_counsel = case_context.get("opposing_counsel", "")
    filing_title = case_context.get("current_filing_title", "")

    # ─── Preamble / Introduction ──────────────────────────────────────
    if section_id == "preamble":
        # Different intros for different doc types
        if doc_type in ("opposition_brief", "reply_brief"):
            instr = (
                f"Write a 2-paragraph introduction for {plaintiff}'s {doc_type.replace('_', ' ')}.\n"
                f"Paragraph 1: State what this filing is and what relief is sought.\n"
                f"Paragraph 2: Briefly preview the main arguments (without making them in full).\n"
                f"Be direct. No hedging. No 'may' or 'might'. Use the parties' real names."
            )
        elif doc_type in ("motion_to_strike", "motion_to_compel", "preliminary_objections"):
            instr = (
                f"Write a 1-2 paragraph introduction for {plaintiff}'s {doc_type.replace('_', ' ')}.\n"
                f"State the relief sought and the controlling rule briefly.\n"
                f"Direct, court-ready language. No hedging."
            )
        elif doc_type == "complaint":
            instr = (
                f"Write a 1-paragraph preliminary statement for {plaintiff}'s complaint against {defendant}.\n"
                f"Identify the cause of action and the relief sought."
            )
        else:
            instr = f"Write a brief 1-paragraph introduction for {doc_type.replace('_', ' ')}."

        return DraftSection(
            name="preamble",
            title=section_title,
            section_type="preamble",
            instructions=instr,
            inputs={
                "plaintiff": plaintiff,
                "defendant": defendant,
                "garnishee": garnishee,
                "case_no": case_no,
                "doc_type": doc_type,
                "filing_title": filing_title,
                "opposing_counsel": opposing_counsel,
            },
            expected_length=600,
        )

    # ─── Rule Framework ───────────────────────────────────────────────
    if section_id == "rule_framework":
        # Trim authority to fit
        auth_trimmed = authority[:3000] if authority else ""
        instr = (
            f"Write a brief 'Applicable Rules' section for this filing.\n"
            f"List the controlling statutes and rules, quoting MANDATORY language verbatim.\n"
            f"Use only the authority provided below. NO outside citations.\n"
            f"Format: list each rule with its citation, then a 1-sentence explanation."
        )
        return DraftSection(
            name="rule_framework",
            title=section_title,
            section_type="rule_framework",
            instructions=instr,
            inputs={"authority": auth_trimmed},
            expected_length=900,
        )

    # ─── Question Presented (briefs) ──────────────────────────────────
    if section_id == "question_presented":
        instr = (
            f"Write a single 'Question Presented' for this filing.\n"
            f"Format: 'Whether...' followed by 'Suggested answer: Yes/No.'\n"
            f"Keep to 2-3 sentences total."
        )
        return DraftSection(
            name="question_presented",
            title=section_title,
            section_type="question",
            instructions=instr,
            inputs={
                "plaintiff": plaintiff,
                "defendant": defendant,
                "filing_title": filing_title,
            },
            expected_length=300,
        )

    # ─── Relevant Facts ───────────────────────────────────────────────
    if section_id == "relevant_facts":
        # Use top 12 most specific facts
        top_facts = facts[:12] if facts else []
        instr = (
            f"Write a 'Relevant Facts' section using ONLY the facts below.\n"
            f"Number each fact. Use exact dates and amounts where given.\n"
            f"NO embellishment. NO interpretation. NO conclusions.\n"
            f"Maximum 1-2 sentences per numbered fact."
        )
        return DraftSection(
            name="relevant_facts",
            title=section_title,
            section_type="facts",
            instructions=instr,
            inputs={"facts": top_facts},
            expected_length=1000,
        )

    # ─── Parties (complaints) ─────────────────────────────────────────
    if section_id == "parties":
        instr = (
            f"Write a 'Parties' section identifying:\n"
            f"- {plaintiff} (Plaintiff)\n"
            f"- {defendant} (Defendant)\n"
            f"For each, state who they are and any relevant capacity (individual/corporate/state actor).\n"
            f"One paragraph per party."
        )
        return DraftSection(
            name="parties",
            title=section_title,
            section_type="parties",
            instructions=instr,
            inputs={"plaintiff": plaintiff, "defendant": defendant},
            expected_length=400,
        )

    # ─── Jurisdiction ──────────────────────────────────────────────────
    if section_id == "jurisdiction":
        instr = (
            f"Write a 'Jurisdiction and Venue' section.\n"
            f"State the jurisdictional basis and venue for {court}.\n"
            f"Cite the relevant rule (e.g., Pa.R.C.P. 1006 for venue, 42 Pa.C.S. § 5101 for jurisdiction).\n"
            f"One paragraph."
        )
        return DraftSection(
            name="jurisdiction",
            title=section_title,
            section_type="jurisdiction",
            instructions=instr,
            inputs={"court": court},
            expected_length=400,
        )

    # ─── Conclusion / WHEREFORE ───────────────────────────────────────
    if section_id == "conclusion":
        if doc_type in ("complaint",):
            instr = (
                f"Write a 'Prayer for Relief' / WHEREFORE clause.\n"
                f"Request: (a) judgment in favor of {plaintiff}, (b) damages, "
                f"(c) costs and attorney fees, (d) any other relief the court deems just.\n"
                f"Use formal WHEREFORE language."
            )
        elif doc_type in ("opposition_brief", "reply_brief"):
            instr = (
                f"Write a brief CONCLUSION section.\n"
                f"State that the relief sought by the opposing party should be denied.\n"
                f"Request {plaintiff}'s relief be granted.\n"
                f"Keep to 2-3 sentences."
            )
        elif doc_type == "regulatory_complaint":
            instr = (
                f"Write a 'Requested Relief' section.\n"
                f"Request investigation, enforcement action, and written response from the agency.\n"
                f"Note that private legal action is being pursued in parallel."
            )
        else:
            instr = (
                f"Write a WHEREFORE clause requesting the relief sought.\n"
                f"Direct, formal court language."
            )

        return DraftSection(
            name="conclusion",
            title=section_title,
            section_type="conclusion",
            instructions=instr,
            inputs={
                "plaintiff": plaintiff,
                "defendant": defendant,
                "doc_type": doc_type,
            },
            expected_length=400,
        )

    return None



# ===PHASE_3F_STEP6D_RECONNECT_V1=== seeded data + new section types

# Pre-seeded definitions for issue keys our audit produces.
# Each entry: violation_type → (definition_block, logic_chain_block)
# These are used as fallback when build_definition_set / build_logic_chain
# return empty (which they do for our issue keys).
_3F_SEEDED_DEFINITIONS = {
    "NON_BINDING_AS_CONTROLLING": (
        "Binding precedent: A judicial decision that lower courts within "
        "the same hierarchy MUST follow on identical legal questions. "
        "Persuasive authority: A judicial decision that a court MAY "
        "consider but is NOT bound to follow. Out-of-circuit federal "
        "decisions, sister-state decisions, and unpublished trial court "
        "decisions are persuasive only.",
        "IF a federal district court relies on out-of-circuit decisions "
        "presented as controlling, AND no Third Circuit or Supreme Court "
        "authority on point is cited, THEN the resulting ruling rests on "
        "an incorrect application of stare decisis."
    ),
    "TRIAL_AS_BINDING": (
        "Trial court decision: A ruling by a court of original "
        "jurisdiction. Trial court decisions are not binding precedent "
        "on coordinate or higher courts. Federal district court orders "
        "do not bind other federal district courts on questions of law.",
        "IF a filing cites trial court decisions as controlling "
        "authority, AND no appellate authority is provided, THEN the "
        "argument rests on procedurally defective use of authority."
    ),
    "TWOMBLY_IQBAL_MISAPPLIED": (
        "Rule 12(b)(6) standard (Twombly/Iqbal): A complaint must allege "
        "facts that, taken as true, state a claim that is plausible on "
        "its face. The court must (a) accept all factual allegations as "
        "true, (b) draw all reasonable inferences in plaintiff's favor, "
        "and (c) determine whether the alleged facts state a plausible "
        "claim. The court may NOT (a) weigh evidence, (b) resolve "
        "factual disputes, or (c) require evidentiary proof at the "
        "pleading stage.",
        "IF a court applies Rule 12(b)(6) by weighing evidence or "
        "resolving disputed facts against plaintiff, THEN the dismissal "
        "applies an incorrect legal standard requiring vacatur."
    ),
    "TWOMBLY_IQBAL": (
        "Rule 12(b)(6) standard (Twombly/Iqbal): A complaint must allege "
        "facts that state a plausible claim. The court accepts all "
        "factual allegations as true and draws all inferences in "
        "plaintiff's favor.",
        "IF a court applies Rule 12(b)(6) by weighing evidence at the "
        "pleading stage, THEN the dismissal is entered under an "
        "incorrect legal standard."
    ),
    "SERVICE_DEFECTIVE": (
        "Service of process under FRCP 4 and FRCP 5: All filings must be "
        "served on every party. Proof of service must be filed. A party "
        "is entitled to actual or reasonably calculated notice of "
        "proceedings affecting their rights (Mullane v. Central Hanover "
        "Bank, 339 U.S. 306).",
        "IF a defendant or filing was not properly served, AND the "
        "Court entered a ruling against the party not served, THEN the "
        "ruling is voidable for inadequate notice under FRCP 60(b)(4) "
        "and Mullane."
    ),
    "VERIFICATION_MISSING": (
        "Verification under Pa.R.C.P. 1024 / FRCP 11: A pleading must "
        "be verified by affidavit when required by rule. An unverified "
        "pleading where verification is required is a nullity.",
        "IF verification is required, AND the filing lacks a sworn "
        "verification, THEN the filing is procedurally defective and "
        "must be stricken."
    ),
    "DUE_PROCESS_NOTICE": (
        "Constitutional due process (U.S. Const. amend. XIV § 1; "
        "Mullane v. Central Hanover Bank, 339 U.S. 306, 314 (1950)): A "
        "party must receive notice reasonably calculated, under all the "
        "circumstances, to apprise interested parties of the pendency "
        "of the action and afford them an opportunity to present their "
        "objections.",
        "IF notice was inadequate, OR opportunity to be heard was "
        "denied, THEN the resulting judgment is void under FRCP "
        "60(b)(4) — not merely voidable. No balancing applies."
    ),
    "FILING_DEADLINE": (
        "FRCP 6 / FRAP timing: Filings must be made within the "
        "prescribed time. Late filings without leave are procedurally "
        "barred.",
        "IF a filing was made after the prescribed deadline, AND no "
        "leave to file out of time was granted, THEN the filing is "
        "procedurally defective."
    ),
}



# ===PHASE_3F_STEP6E_FIXES_V1=== additional seeded definitions
# Aligned to actual issue keys in audit data (see diagnostic D3)
_3F_SEEDED_DEFINITIONS_6E = {
    "TWOMBLY_IQBAL_MISAPPLICATION": (
        "Rule 12(b)(6) standard (Bell Atlantic Corp. v. Twombly, 550 U.S. "
        "544 (2007); Ashcroft v. Iqbal, 556 U.S. 662 (2009)): A complaint "
        "must allege facts that, taken as true, state a claim that is "
        "plausible on its face. The court must (a) accept all factual "
        "allegations as true, (b) draw all reasonable inferences in the "
        "non-movant's favor, and (c) determine whether the alleged facts "
        "state a plausible claim. The court may NOT (a) weigh evidence, "
        "(b) resolve disputed factual questions, or (c) require "
        "evidentiary proof at the pleading stage. Misapplication of "
        "Twombly/Iqbal that imposes a heightened pleading standard or "
        "weighs evidence is a misapplication of controlling law.",
        "IF a court applies Rule 12(b)(6) by weighing factual disputes, "
        "OR resolving disputed inferences against the non-movant, OR "
        "requiring proof at the pleading stage, THEN the dismissal "
        "applies an incorrect legal standard requiring vacatur under "
        "FRCP 60(b)(4)."
    ),
    "FILING_DEADLINE_VIOLATION": (
        "FRCP 6 timing rules: Filings must be made within the prescribed "
        "time computed under FRCP 6(a) and (d). FRCP 6(b) requires leave "
        "of court for filings made out of time. A filing made after the "
        "prescribed deadline without prior leave is procedurally "
        "defective and subject to being stricken under FRCP 12(f).",
        "IF a filing was made after the prescribed deadline, AND no "
        "extension under FRCP 6(b) was granted, THEN the filing is "
        "procedurally defective and the court should not have considered "
        "its substantive content."
    ),
    "NOTICE_INADEQUATE": (
        "Constitutional due process (U.S. Const. amend. XIV § 1): A "
        "party is entitled to notice reasonably calculated, under all "
        "the circumstances, to apprise interested parties of the "
        "pendency of the action and afford them an opportunity to "
        "present their objections. Mullane v. Central Hanover Bank & "
        "Trust Co., 339 U.S. 306, 314 (1950). Notice that fails to reach "
        "the party, OR fails to afford a meaningful opportunity to be "
        "heard, is constitutionally inadequate.",
        "IF notice of a proceeding was not actually delivered to the "
        "party, OR the party had no reasonable opportunity to be heard, "
        "THEN any judgment entered against that party is void under "
        "FRCP 60(b)(4) for lack of due process. No balancing applies."
    ),
    "CLAIMS_DISMISSED": (
        "Effect of dismissal: A dismissal under FRCP 12(b)(6) terminates "
        "the dismissed claims. Dismissals with prejudice operate as "
        "adjudications on the merits (FRCP 41(b)). The plaintiff loses "
        "the ability to litigate the dismissed claims absent vacatur, "
        "amendment, or appellate reversal.",
        "IF claims were dismissed under an incorrect legal standard, OR "
        "without adequate notice and opportunity to be heard, OR by a "
        "court that exceeded its authority, THEN the dismissal is "
        "subject to vacatur under FRCP 60(b)(4) (void) or FRCP 60(b)(6) "
        "(extraordinary circumstances)."
    ),
    "MTD_GRANTED_AGAINST_PLAINTIFF": (
        "Motion to Dismiss under FRCP 12(b)(6): A dispositive motion "
        "challenging the legal sufficiency of the complaint. When "
        "granted, the motion terminates the challenged claims. The "
        "court accepts well-pleaded factual allegations as true and "
        "draws all reasonable inferences in plaintiff's favor.",
        "IF a Motion to Dismiss was granted under an incorrect legal "
        "standard (Twombly/Iqbal misapplication, evidence weighed at "
        "pleading stage, inferences drawn against plaintiff), THEN the "
        "resulting dismissal rests on legal error and is subject to "
        "vacatur."
    ),
    "COURT_RULING_GRANTED": (
        "Court rulings on dispositive motions: A grant of a dispositive "
        "motion (motion to dismiss, motion for summary judgment, motion "
        "to strike, motion for judgment on the pleadings) terminates "
        "the targeted claims or party. The standard governing each "
        "motion is fixed by rule and binding precedent and the court "
        "must apply that standard correctly.",
        "IF a dispositive motion was granted under an incorrect legal "
        "standard, OR without consideration of record evidence, OR "
        "without proper notice to the opposing party, THEN the ruling "
        "is subject to vacatur under FRCP 60(b)(4) or FRCP 60(b)(6)."
    ),
    "SERVICE_DEFECTIVE": (
        "Service of process under FRCP 4 and FRCP 5: All filings must "
        "be served on every party. Proof of service must be filed. A "
        "party is entitled to actual or reasonably calculated notice of "
        "proceedings affecting their rights. Mullane v. Central Hanover "
        "Bank & Trust Co., 339 U.S. 306 (1950).",
        "IF a defendant or filing was not properly served, AND the "
        "Court entered a ruling against the party not served, THEN the "
        "ruling is void for inadequate notice under FRCP 60(b)(4) and "
        "Mullane."
    ),
    "VERIFICATION_MISSING": (
        "Verification under Pa.R.C.P. 1024 / FRCP 11: A pleading must "
        "be verified by affidavit when required by rule. An unverified "
        "pleading where verification is required is a nullity. Under "
        "FRCP 11(a), every pleading must be signed by an attorney of "
        "record or by the party personally; an unsigned pleading must "
        "be stricken unless promptly corrected.",
        "IF verification or signature is required, AND the filing lacks "
        "the required verification or signature, THEN the filing is "
        "procedurally defective and must be stricken under FRCP 12(f)."
    ),
}
# ===END PHASE_3F_STEP6E_FIXES_V1===

def _seeded_definitions(issue: str):
    """Fallback definitions/logic for issue keys we know about.

    Returns (definitions_text, logic_chain_text) or empty strings.
    Consults both 6d and 6e seed dictionaries.
    """
    if not issue:
        return ("", "")
    key = issue.upper().replace(" ", "_")

    # ===PHASE_3F_STEP6E_FIXES_V1=== check 6e dict first (more specific)
    seed = _3F_SEEDED_DEFINITIONS_6E.get(key)
    if seed:
        return seed
    # Try partial match in 6e
    for k, v in _3F_SEEDED_DEFINITIONS_6E.items():
        if k in key or key in k:
            return v
    # ===END PHASE_3F_STEP6E_FIXES_V1===

    seed = _3F_SEEDED_DEFINITIONS.get(key)
    if seed:
        defs, chain = seed
        return (defs, chain)
    # Try partial match in 6d
    for k, v in _3F_SEEDED_DEFINITIONS.items():
        if k in key or key in k:
            return v
    return ("", "")
    key = issue.upper().replace(" ", "_")
    seed = _3F_SEEDED_DEFINITIONS.get(key)
    if seed:
        defs, chain = seed
        return (defs, chain)
    # Try partial match
    for k, v in _3F_SEEDED_DEFINITIONS.items():
        if k in key or key in k:
            return v
    return ("", "")


def _build_dispositive_anchor_section(case_context: dict, dispositive_text: str,
                                        dispositive_orders: list) -> Optional[DraftSection]:
    """Build the II. DISPOSITIVE ORDER AT ISSUE section.

    The LLM's job: translate the structured dispositive_text into legal
    prose. NOT to invent or reason about anything.
    """
    if not dispositive_text or not dispositive_orders:
        return None

    plaintiff = case_context.get("plaintiff", "Plaintiff")
    instr = (
        f"Translate the dispositive order facts below into a clear "
        f"legal narrative section titled 'II. DISPOSITIVE ORDER AT ISSUE'. "
        f"State each order, the date, the ECF number, the defendants, "
        f"and the subject matter of the dismissal. Do NOT reason about "
        f"whether the dismissal was correct — that comes in later "
        f"sections. Do NOT invent any fact not below. Use {plaintiff} "
        f"as the plaintiff. Close with: 'Each order terminated "
        f"Plaintiff's claims as set forth therein.'"
    )
    return DraftSection(
        name="dispositive_anchor",
        title="II. DISPOSITIVE ORDER AT ISSUE",
        section_type="dispositive_anchor",
        instructions=instr,
        inputs={
            "dispositive_text":   dispositive_text,
            "dispositive_orders": dispositive_orders,
            "plaintiff":          plaintiff,
        },
        expected_length=800,
    )


def _build_evidence_ignored_section(case_context: dict,
                                      record_gaps: list,
                                      dispositive_orders: list) -> Optional[DraftSection]:
    """Build the V. FAILURE TO ADDRESS RECORD EVIDENCE section.

    The LLM's job: state that each piece of record evidence below was not
    addressed in the dispositive orders. Pure translation of structured gap
    list to prose.
    """
    if not record_gaps:
        return None

    plaintiff = case_context.get("plaintiff", "Plaintiff")
    instr = (
        f"Write the section 'V. FAILURE TO ADDRESS RECORD EVIDENCE'. "
        f"For each record evidence item below, state what it is and that "
        f"the dispositive order(s) failed to address it. Conclude that "
        f"a ruling entered without consideration of record evidence "
        f"deprives {plaintiff} of meaningful opportunity to be heard "
        f"under Mullane v. Central Hanover Bank. Do NOT invent evidence "
        f"not listed below."
    )
    return DraftSection(
        name="evidence_ignored",
        title="V. FAILURE TO ADDRESS RECORD EVIDENCE",
        section_type="evidence_ignored",
        instructions=instr,
        inputs={
            "record_gaps":        record_gaps,
            "dispositive_orders": dispositive_orders,
            "plaintiff":          plaintiff,
        },
        expected_length=900,
    )
# ===END PHASE_3F_STEP6D_RECONNECT_V1===



# ===PHASE_3F_JUDICIAL_DUTY_V1=== judicial_duty_breach_builder
def _build_judicial_duty_breach_section(case_context: dict,
                                          judicial_duty_defects: list) -> "Optional[DraftSection]":
    """Build the III. JUDICIAL DUTY BREACH section.

    The LLM's job: translate the list of court-authored doctrinal defects
    into legal prose. Each defect MUST be quoted verbatim from the order
    that committed it. The LLM does NOT decide which defects are real —
    they are deterministically pre-filtered by upstream
    _extract_judicial_duty_defects().

    judicial_duty_defects shape:
        [
            {
                "date":         "2025-09-19",
                "judge":        "the Judge, J.",
                "ecf":          "Document 121",
                "source_doc":   "judges memorandum.pdf",
                "defects": [
                    {
                        "violation_type": "TWOMBLY_IQBAL_MISAPPLICATION",
                        "severity":       "FATAL",
                        "rule_citation":  "FRCP 12(b)(6); Twombly; Iqbal",
                        "rule_text":      "...",
                        "quote":          "the Court need not accept as true...",
                        "page":           4,
                        "lines":          "187-190",
                        "fix":            "All well-pleaded factual allegations...",
                    },
                    ...
                ],
            },
            ...
        ]
    """
    if not judicial_duty_defects:
        return None

    plaintiff = case_context.get("plaintiff", "Plaintiff")
    # ===PHASE_3F_JUDICIAL_DUTY_V5_STRIP_CONFLICTING_INSTR===
    # Instructions intentionally minimal. The chunked_drafter input
    # formatter (section_type == 'judicial_duty_breach' branch) owns
    # the per-group paragraph contract, the verbatim-quote rules, and
    # the conclusion sentence. Two instruction sources contradicted
    # in v4c/v4e (per-defect vs per-group); this collapses to one.
    instr = (
        f"Write the section \'III. JUDICIAL DUTY BREACH\' for {plaintiff}\'s "
        f"motion to vacate. Follow the rules in the SECTION INPUT below "
        f"exactly. Do NOT paraphrase verbatim quotes. Do NOT invent defects."
    )
    # ===END PHASE_3F_JUDICIAL_DUTY_V5_STRIP_CONFLICTING_INSTR===
    return DraftSection(
        name="judicial_duty_breach",
        title="III. JUDICIAL DUTY BREACH",
        section_type="judicial_duty_breach",
        instructions=instr,
        inputs={
            "judicial_duty_defects": judicial_duty_defects,
            "plaintiff":             plaintiff,
        },
        expected_length=1200,
    )
# ===END PHASE_3F_JUDICIAL_DUTY_V1===

def _build_conflict_sections(
    conflicts: list,
    case_context: dict,
    authority: str,
) -> list:
    """Expand the conflicts list into one DraftSection per conflict.

    Deduplicates by issue — if conflict engine produced 4 verification
    conflicts, we collapse them into a single argument section that
    incorporates all the variant positions.
    """
    sections = []
    plaintiff = case_context.get("plaintiff", "Plaintiff")

    # ── DEDUPE by issue ───────────────────────────────────────────────
    grouped = {}  # issue → list of conflict dicts
    for c in conflicts:
        cd = c if isinstance(c, dict) else c.to_dict()
        issue = cd.get("issue", "general")
        grouped.setdefault(issue, []).append(cd)

    # Sort issues by severity / count for stable order
    ordered_issues = sorted(grouped.keys(), key=lambda k: (-len(grouped[k]), k))

    for i, issue in enumerate(ordered_issues[:6], 1):  # Cap at 6 unique issues
        bucket = grouped[issue]
        primary = bucket[0]

        # Combine all their positions into one
        positions = [b.get("their_position", "")[:300] for b in bucket if b.get("their_position")]
        their_position = " | ".join(p for p in positions if p)[:600]

        # Use the first non-empty rule_text
        rule_text = ""
        for b in bucket:
            rt = b.get("rule_text", "")
            if rt and rt != "(rule text not retrieved)":
                rule_text = rt[:500]
                break

        # Use the strongest application analysis
        applications = [b.get("application", "") for b in bucket if b.get("application")]
        application = " ".join(applications)[:600]

        result = primary.get("result", "non-compliant")
        status = primary.get("status", "violation")
        attack_vector = primary.get("attack_vector", "application")
        attack_summary = primary.get("attack_summary", "")
        rule_authority = primary.get("rule_authority", "")

        # ─── PHASE 3.5g: BUILD DEFINITIONS for this issue ─────────────
        definitions_text = ""
        try:
            from pipeline.definition_engine import build_definition_set, format_definition_block
            definitions = build_definition_set(
                issue=issue,
                db_authority=authority,
            )
            if definitions:
                definitions_text = format_definition_block(definitions)
        except Exception as e:
            pass  # Definitions are optional, don't crash drafting

        # ─── PHASE 3.5g: BUILD IF/THEN LOGIC CHAIN for this issue ─────
        logic_chain_text = ""
        try:
            from pipeline.if_then_engine import build_logic_chain, format_chain_block
            from pipeline.fact_polarity_engine import sanitize_rule_text

            # CRITICAL: sanitize rule_text first to strip template garbage
            # (BLAIR COUNTY, ARBITRATIONSCHEDULING, BY THE COURT, etc.)
            clean_rule = sanitize_rule_text(rule_text)

            chain = build_logic_chain(
                rule_text=clean_rule,
                rule_authority=rule_authority,
                facts=[],  # Facts come from case_context separately
                issue=issue,
                attack_vector=attack_vector,
                extraction={},
            )
            if chain:
                logic_chain_text = format_chain_block(chain)
        except Exception as e:
            pass  # Chain is optional

            # ===PHASE_3F_STEP6D_RECONNECT_V1=== seeded fallback
            if not definitions_text:
                seed_defs, _ = _seeded_definitions(issue)
                if seed_defs:
                    definitions_text = seed_defs
            if not logic_chain_text:
                _, seed_chain = _seeded_definitions(issue)
                if seed_chain:
                    logic_chain_text = seed_chain
            # ===END PHASE_3F_STEP6D_RECONNECT_V1===

        # Tailor instructions to the attack vector
        attack_guidance = _attack_vector_guidance(attack_vector)

        instr = (
            f"Write ONE argument section addressing the issue of: {issue}.\n"
            f"This section addresses {len(bucket)} related opposing argument(s) on this issue.\n"
            f"\n"
            f"ATTACK STRATEGY: {attack_vector.upper()}\n"
            f"{attack_guidance}\n"
            f"\n"
            f"You MUST use the locked DEFINITIONS and IF/THEN chain provided.\n"
            f"You are formatting deterministic logic, not reasoning from scratch.\n"
        )

        sections.append(DraftSection(
            name=f"argument_{i}",
            title=f"{i}. {issue.replace('_', ' ').title()}",
            section_type="argument",
            instructions=instr,
            inputs={
                "issue": issue,
                "their_position": their_position,
                "rule_text": rule_text,
                "application": application,
                "result": result,
                "status": status,
                "attack_vector": attack_vector,
                "attack_summary": attack_summary,
                "definitions_text": definitions_text,
                "logic_chain_text": logic_chain_text,
                "plaintiff": plaintiff,
            },
            expected_length=1100,
        ))

    return sections


# ══════════════════════════════════════════════════════════════════════
# ATTACK VECTOR GUIDANCE (advocacy directives per attack type)
# ══════════════════════════════════════════════════════════════════════

ATTACK_VECTOR_GUIDANCE = {
    "timing": (
        "The opposing party did not act within the time required by the rule. "
        "Argue that mandatory rules require contemporaneous compliance and that "
        "a late attempt at compliance does not retroactively cure the original "
        "defect. Cite the specific dates from the record."
    ),
    "scope": (
        "The opposing party is misapplying the rule. Argue that the cited rule "
        "either does not apply to this situation or is being read more broadly "
        "than its text supports. Distinguish the rule's actual scope from the "
        "opposing party's expansive reading."
    ),
    "authority": (
        "The opposing party's cited authority does not control. Argue that the "
        "cited cases are non-binding (lower court, dicta, or distinguishable on "
        "their facts) and that the actual controlling authority supports the "
        "plaintiff's position."
    ),
    "application": (
        "The facts of this case do not satisfy the rule the opposing party is "
        "trying to invoke. Argue that the record establishes specific elements "
        "of non-compliance and that the rule cannot be satisfied by the facts "
        "the opposing party presents."
    ),
    "procedure": (
        "The opposing party used the wrong procedural mechanism. Argue that the "
        "rules require a specific procedure that was not followed, and that the "
        "opposing party cannot substitute their preferred procedure for the "
        "rule's actual requirements."
    ),
    "burden": (
        "The opposing party has not carried their burden of proof. Argue that "
        "the burden of establishing the procedural propriety of their conduct "
        "lies with them, and that they have not met that burden on this record."
    ),
}


def _attack_vector_guidance(vector: str) -> str:
    """Return advocacy guidance for the given attack vector."""
    return ATTACK_VECTOR_GUIDANCE.get(vector, ATTACK_VECTOR_GUIDANCE["application"])


# ══════════════════════════════════════════════════════════════════════
# SECTION ASSEMBLY
# ══════════════════════════════════════════════════════════════════════

def merge_sections(rendered: dict, plan: list) -> str:
    """Merge rendered section outputs into a complete document.

    Args:
        rendered: dict mapping section name → generated text
        plan: list of DraftSection objects (preserves order)

    Returns:
        Complete document as plain text.
    """
    parts = []
    last_title = None

    for section in plan:
        text = rendered.get(section.name, "").strip()
        if not text:
            continue

        # For argument sections, group under one ARGUMENT header
        if section.section_type == "argument":
            if last_title != "ARGUMENT":
                parts.append("\nARGUMENT\n")
                last_title = "ARGUMENT"
            parts.append(f"\n{section.title}\n\n{text}\n")
        else:
            parts.append(f"\n{section.title}\n\n{text}\n")
            last_title = section.title

    return "\n".join(parts).strip()
