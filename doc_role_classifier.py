"""
doc_role_classifier.py  (v4 — plaintiff Proposed ORDER fix + tighter judge markers)
====================================================================================
V4 fixes (based on observed misclassifications in v3 output):

  1. "Proposed ORDER.docx" was getting JUDGE role because the bare
     "order " marker matched first. Fix:
       - Add "proposed_order" to PLAINTIFF markers (BEFORE the order check)
       - Move JUDGE markers to require a date or docket qualifier
         (so just "order" is no longer enough)

  2. "Motion to reconsider remand -signed.pdf" was getting OPPOSING via
     PDF fallback. Fix: add "motion to reconsider" + "motion to remand"
     + " -signed" to plaintiff markers.

  3. Added: "praecipe_to_enter" (plaintiff), "amended_motion" (plaintiff),
     "exhibit_to_complaint" (plaintiff).

Self-test count grew from 11 to 18 (passes 18/18).
"""
from core import identity as _ident
from pathlib import Path
from typing import Optional, List, Dict


# JUDGE markers — REQUIRE a date/docket qualifier so "Proposed ORDER" doesn't match
JUDGE_MARKERS = [
    "memorandum_opinion", "memorandum_order", "report_and_recommendation",
    "r_and_r", "r&r", "rr_",
    "_order_2024", "_order_2025", "_order_2026", "_order_2027",
    "order_2024", "order_2025", "order_2026", "order_2027",
    "minute_entry", "minute_order",
    "scheduling_order", "case_management_order",
    "judgment", "_decree", "court_opinion",
    "clerk_entry", "clerk_notice",
    "_judge_", 
    "judges_memorandum", "judges memorandum",
    "ecf_141", "ecf 141",
]

PLAINTIFF_MARKERS = [
    # === V4 PRIORITY ADDITIONS (must beat the JUDGE "order" check) ===
    "proposed_order", "proposed order",
    "proposed_judgment", "proposed judgment",
    "motion_to_reconsider", "motion to reconsider",
    "motion_to_remand", "motion to remand",
    "motion_for_remand", "motion for remand",
    "amended_motion", "amended motion",
    "_signed_", " -signed", "-signed.",  # plaintiff signed docs
    "praecipe_to_enter", "praecipe to enter",
    "praecipe_for_judgment_plaintiff",
    "exhibit_to_complaint", "exhibit to complaint",

    # Original markers
    "complaint", "amended_complaint", "second_amended", "third_amended",
    "first_amended_complaint",
    "_ifp", "in_forma_pauperis", "ifp_application", "motion_ifp",
    "notice_of_appeal", "appellant_brief",
    "pro_se", "pro-se", "_prose",
    "motion_for_default", "request_for_default",
    "motion_to_vacate", "motion_for_reconsideration",
    "motion_for_leave", "motion_for_leave_to_file",
    "motion_to_strike", "motion_to_remand",
    "objection_to_r_r", "objection_to_report",
    "reply_in_support", "reply_brief_plaintiff",
    "plaintiff_response", "plaintiff_reply",
    "proof_of_service", "service_proof",
    "declaration_of_" + _ident.party_key(), "affidavit_of_" + _ident.party_key(),
    "notice_of_immunity",

    # V2 expansions
    "rebuttal", "rebuttle", "rebut_",
    "rebuttal to order", "rebuttal to judges order", "rebuttal to judge",
    "sur_reply", "sur reply", "sur-reply", "surreply",
    "validation_of_debt", "debt_validation", "debt validation",
    "validation of debt", "validation_response",
    "supplemental_declaration", "supplemental declaration",
    "declaration_of_clarification", "declaration for the public record",
    "legal_affidavit", "verified_affidavit",
    "legal affidavit federal", "verified affidavit",
    "affidavit_of_truth", "affidavit of truth",
    "petition_for_clarification", "petition for clarification",
    "preservation_of_rights", "preservation of rights", "petition for preservation",
    "plaintiff_s_petition", "plaintiff's petition", "plaintiff_petition",
    "conditional_acceptance", "conditional acceptance",
    "notice_of_legal_instruction", "notice of legal instruction",
    "legal_instruction_and_escalation", "legal instruction and escalation",
    "notice of escalation",
    "cover_letter", "cover letter",
    "exhibit_a_certificates_of_service", "exhibit a certificates of service",
    "certificates_of_service", "certificate_of_service_plaintiff",
    "motion_to_compel", "motion to compel",
    "motion_for_summary_judgment_plaintiff",
    "motion_to_certify", "motion to certify",
    "motion_for_default_judgment",
    "motion_for_frcp_54b", "motion for frcp 54b",
    "writ_of_mandamus", "writ of mandamus",
    "opposition_to_capital_one", "opposition to capital one",
    "opposition_to_equifax", "opposition to equifax",
    "opposition_to_experian", "opposition to experian",
    "opposition_to_transunion", "opposition to transunion",
    "opposition_to_transunions", "opposition to transunions",
    "opposition_to_avant", "opposition to avant",
    "opposition_to_extension", "opposition to extension",
    "opposition_to_motion_to_dismiss", "opposition to motion to dismiss",
    "opposition_to_exquifax", "opposition to exquifax",
    "memorandum_of_law_in_support_of_plaintiff",
    "memorandum of law in support of plaintiff",
    "memorandum in support of plaintiff",
    "complete_filing_package", "complete filing package",
    "complete_court_filing_package", "complete court filing package",
    "updated_filing_with_exhibit", "updated filing with exhibit",
    "supplemental_evidence",
    "filling_credit_bureau", "filling credit bureau",
    "filing_credit_bureau", "filing credit bureau",
    "credit_bureau_lawsuit_final", "credit bureau lawsuit final",
    "credit_bureau_appeals",
    "appellate_brief", "appellate brief",
    "appellate_brief_appeals",
    "appellant",
    "civil_appeal_information_statement",
    *_ident.name_tokens(),
    "plaintiff_s_", "plaintiff's_", "plaintiff_motion_for",
    "legal_memorandum", "legal memorandum",
    "motion_for_protective_order", "motion for protective order",
]

OPPOSING_MARKERS = [
    "_motion_to_dismiss_", "motion to dismiss",
    "_mtd_", "_mtd.", "_mtd ",
    "response_in_opposition",
    "memorandum_in_opposition",
    "answer_to_complaint", "answer_and_affirmative",
    "defendants_motion", "defendant_motion",
    "defendants_reply", "defendant_reply",
    "memorandum_in_support_of_motion_to_dismiss",
    "in_support_of_its_motion_to_dismiss",
    "in support to its motion to dismiss",
    " reply ", " reply.", "_reply.",
    "praecipe_for_default_defendant", "praecipe for default",
    "defendants_brief", "defendant_brief",
    # V4: removal-related opposing markers
    "notice_of_removal", "notice of removal",
    "defendants_notice_of_removal",
]

AMBIGUOUS_MARKERS = [
    "motion", "response", "reply", "memorandum", "brief",
    "answer", "objection", "letter",
]

GENERIC_DEFENDANT_FRAGMENTS = [
    "experian", "equifax", "transunion", "trans_union", "trans-union",
    "discover", "synchrony", "capital_one", "capital-one", "capitalone",
    "wells_fargo", "wellsfargo", "wells-fargo",
    "lippi", "avant", "lendly", "lending_club", "lendingclub",
    "credit_one", "creditone", "credit-one",
    "midland", "portfolio_recovery", "lvnv",
    "amex", "american_express",
    "verizon", "comcast",
]


def _norm(s):
    return (str(s).lower()
            .replace(" ", "_").replace("-", "_").replace(".", "_"))


def _is_judge_order(name_norm: str) -> bool:
    """Only treat 'order' as judicial when paired with a date/Dkt number.
    Excludes 'proposed_order', 'order_to_show_cause' as caught by plaintiff markers earlier.
    """
    # Pure 'order_2024' / 'order_<date>' patterns
    if name_norm.startswith("order_2024") or name_norm.startswith("order_2025") or \
       name_norm.startswith("order_2026") or name_norm.startswith("order_2027"):
        return True
    # ECF-numbered docket entry "ecf_141" already in JUDGE_MARKERS
    return False


def _contains_marker(name_norm, name_raw_lower, marker):
    m_norm = _norm(marker)
    m_raw  = marker.lower()
    return (m_norm in name_norm) or (m_raw in name_raw_lower)


def classify_doc(path, defendants=None, plaintiff_name=None):
    return classify_doc_with_evidence(path, defendants, plaintiff_name)["role"]


def classify_doc_with_evidence(path, defendants=None, plaintiff_name=None):
    name_raw = str(path).split("/")[-1].split("\\")[-1]
    name_raw_lower = name_raw.lower()
    name_norm = _norm(name_raw)
    full_norm = _norm(path)

    # 1. PLAINTIFF markers — checked FIRST, before any "order" check
    for marker in PLAINTIFF_MARKERS:
        if _contains_marker(name_norm, name_raw_lower, marker) or \
           _contains_marker(full_norm, str(path).lower(), marker):
            return {"role": "plaintiff", "matched_marker": marker,
                    "confidence": "high",
                    "reason": f"plaintiff-filing marker {marker!r}"}

    # 2. JUDGE markers (now tightened — no bare "order")
    for marker in JUDGE_MARKERS:
        if _contains_marker(name_norm, name_raw_lower, marker):
            return {"role": "judge", "matched_marker": marker,
                    "confidence": "high",
                    "reason": f"judicial marker {marker!r}"}
    if _is_judge_order(name_norm):
        return {"role": "judge", "matched_marker": "order_<date>",
                "confidence": "high", "reason": "date-qualified order pattern"}

    # 3. OPPOSING markers
    for marker in OPPOSING_MARKERS:
        if _contains_marker(name_norm, name_raw_lower, marker):
            return {"role": "opposing_counsel", "matched_marker": marker,
                    "confidence": "high",
                    "reason": f"opposing-side marker {marker!r}"}

    # 4. Defendant fragment
    def_list = []
    if defendants:
        def_list.extend(_norm(d) for d in defendants)
    def_list.extend(GENERIC_DEFENDANT_FRAGMENTS)
    for frag in def_list:
        if frag in name_norm:
            return {"role": "opposing_counsel", "matched_marker": frag,
                    "confidence": "medium",
                    "reason": f"defendant fragment {frag!r}"}

    # 5. Plaintiff name fragment
    if plaintiff_name:
        pn_norm = _norm(plaintiff_name)
        last = pn_norm.split("_")[-1]
        if pn_norm in name_norm or (len(last) >= 4 and last in name_norm):
            return {"role": "plaintiff", "matched_marker": last,
                    "confidence": "medium",
                    "reason": "plaintiff name fragment"}

    # 6. Extension fallback (.docx -> plaintiff, .pdf -> opposing)
    if name_raw_lower.endswith(".docx") or name_raw_lower.endswith(".doc"):
        return {"role": "plaintiff", "matched_marker": "_extension_docx",
                "confidence": "medium",
                "reason": ".docx files default to plaintiff"}
    if name_raw_lower.endswith(".pdf"):
        return {"role": "opposing_counsel",
                "matched_marker": "_extension_pdf",
                "confidence": "low",
                "reason": ".pdf defaults to opposing"}

    # 7. Ambiguous → unknown
    for marker in AMBIGUOUS_MARKERS:
        if marker in name_norm:
            return {"role": "unknown", "matched_marker": marker,
                    "confidence": "low",
                    "reason": f"ambiguous marker {marker!r}"}
    return {"role": "unknown", "matched_marker": "",
            "confidence": "low", "reason": "no marker matched"}


if __name__ == "__main__":
    tests = [
        # V4 PRIORITY FIXES
        ("plaintiff", "Proposed ORDER.docx"),
        ("plaintiff", "Proposed ORDER .pdf"),
        ("plaintiff", "(clean) Proposed Order (6 May 2025).docx"),
        ("plaintiff", "Proposed ORDER - Copy.docx"),
        ("plaintiff", "Motion to reconsider remand -signed.pdf"),
        ("plaintiff", "Motion to reconsider.docx"),
        ("plaintiff", "Motion for Remand.docx"),
        # Should remain judge (date-qualified)
        ("judge", "Order_2026-05-11.pdf"),
        ("judge", "order_20260511.pdf"),
        ("judge", "judges memorandum.pdf"),
        ("judge", "ecf_141.pdf"),
        # Should remain opposing
        ("opposing_counsel", "avant motion to dismiss.pdf"),
        ("opposing_counsel", "notice of removal.pdf"),
        ("opposing_counsel", "show_temp (15).pdf"),  # .pdf fallback
        # Should remain plaintiff
        ("plaintiff", "legal memorandum.docx"),
        ("plaintiff", "Motion to Compel.docx"),
        ("plaintiff", "random_unknown_file.docx"),
        ("plaintiff", "Validation of Debt - Filer.pdf"),
    ]
    print("V4 classifier self-test")
    print("=" * 80)
    correct = wrong = 0
    for expected, fn in tests:
        r = classify_doc_with_evidence(
            fn,
            defendants=["Experian", "Equifax", "TransUnion", "Avant", "Example Card Issuer"],
            plaintiff_name="Filer")
        actual = r["role"]
        ok = actual == expected
        if ok: correct += 1
        else: wrong += 1
        flag = "OK " if ok else "FAIL"
        print(f"  [{flag}] expected={expected:<18} got={actual:<18} | {fn[:55]}")
        if not ok:
            print(f"         matched={r['matched_marker']!r} reason={r['reason']}")
    print(f"\n{correct}/{correct+wrong} correct")
