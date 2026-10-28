#!/usr/bin/env python3
"""
document_role_classifier.py — classify legal documents by authorship role.

Purpose: stop the judicial_order_scanner from flagging YOUR OWN memoranda of
law as containing judicial misconduct. The scanner's job is to find misconduct
BY the court (and optionally opposing counsel), not in your own filings.

Roles returned:
  judicial_output           — order/opinion/memorandum BY the court
  opposing_party_filing     — filing BY adverse counsel/defendant
  pro_se_plaintiff_filing   — filing BY the user (Filer)
  exhibit_or_evidence       — attached document, not a party filing
  correspondence            — email, letter
  transcript                — court reporter output
  unknown                   — no distinctive markers

Classification stages (high → low confidence):
  1. Filename heuristics (covers ~80% of typical case folders)
  2. Content markers in first 2000 chars
  3. Plaintiff-name match from case yaml caption (disambiguates ECF entries)

CLI:
  document_role_classifier.py classify <path> [--case-id CASE_ID]
  document_role_classifier.py audit <case_id>
  document_role_classifier.py list-rules
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import re
import sys
import json
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BEEZLE_ROOT = _eroot()
CASES_DIR = BEEZLE_ROOT / "obsidian_sync" / "cases"

logger = logging.getLogger("document_role_classifier")

VALID_ROLES = {
    "judicial_output",
    "opposing_party_filing",
    "pro_se_plaintiff_filing",
    "exhibit_or_evidence",
    "correspondence",
    "transcript",
    "unknown",
}


# ─────────────────────────────────────────────────────────────────────────────
# Result type
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RoleClassification:
    role: str
    confidence: str       # high | med | low
    method: str           # filename_*, content_*, case_yaml_*
    matched_pattern: Optional[str] = None
    extracted_text_snippet: Optional[str] = None
    file_path: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# Filename pattern rules
# ─────────────────────────────────────────────────────────────────────────────

# Each rule: (pattern, role, confidence, name)
# Patterns matched against lowercase filename
FILENAME_RULES_PRO_SE_PLAINTIFF = [
    (r"plaintiff'?s?[\s_-]", "filename_plaintiff_possessive"),
    (r"\bmemorandum of law in support of plaintiff", "filename_plaintiff_memo_of_law"),
    (r"\bamended complaint", "filename_amended_complaint"),
    (r"^complaint[\s_.\-]", "filename_complaint"),
    (r"\bcomplaint[\s_.\-]*(signed|final|draft|\d)", "filename_complaint_qualified"),
    (r"^rebuttal[\s_]", "filename_rebuttal"),
    (r"\brebuttal to judge", "filename_rebuttal_to_judge"),
    (r"\bresponse to (motion|order|judge|opposition)", "filename_response_to"),
    (r"\breply to (motion|opposition)", "filename_reply_to_motion"),
    (r"\bmemorandum in opposition", "filename_memo_in_opposition"),
    (r"\bmemorandum of negotiable", "filename_memo_negotiable_instruments"),
    # "opposition to motion" intentionally removed — ambiguous;
    # rely on Stage 0 named-defendant pre-check or content classifier instead
    (r"\bmotion to remand", "filename_motion_to_remand"),
    (r"\bmotion for (default|reconsideration|leave|extension)", "filename_motion_for"),
    (r"\bpetition for", "filename_petition_for"),
    (r"\baffidavit of (plaintiff|service)", "filename_affidavit_plaintiff"),
    (r"^legal memorandum", "filename_legal_memorandum"),
    (r"\bdisacharge\b", "filename_discharge_misspelled"),  # user-specific marker
    (r"\bin support of plaintiff", "filename_in_support_of_plaintiff"),
]

FILENAME_RULES_JUDICIAL_OUTPUT = [
    (r"\bjudge['']?s?\s+(order|memorandum|opinion|ruling)", "filename_judges_x"),
    (r"^judge order[\s_-]?\d?", "filename_judge_order_prefix"),
    (r"\bjudicial order", "filename_judicial_order"),
    (r"\border on motion", "filename_order_on_motion"),
    (r"\bmemorandum and[\s_/]?or opinion", "filename_memo_and_or_opinion"),
    (r"\bopinion of the court", "filename_opinion_of_court"),
    (r"\bcourt order", "filename_court_order"),
    (r"\bappeal order", "filename_appeal_order"),
    (r"\bruling on", "filename_ruling_on"),
    (r"\bbench (order|ruling)", "filename_bench_ruling"),
    (r"\bdismissal order", "filename_dismissal_order"),
    (r"\bshow cause", "filename_show_cause"),
    (r"\bscheduling order", "filename_scheduling_order"),
    (r"\bopinion[\s_.\-]", "filename_opinion_prefix"),
    (r"\bclerk['']?s? entry", "filename_clerks_entry"),
    (r"\bdocket entry", "filename_docket_entry"),
]

FILENAME_RULES_OPPOSING_PARTY = [
    (r"defendant'?s? (motion|answer|response|reply|brief|opposition)", "filename_defendants_x"),
    (r"\bmotion to dismiss\b(?! by plaintiff)", "filename_motion_to_dismiss"),
    (r"\banswer to (complaint|amended)", "filename_answer_to_complaint"),
    (r"\bdefense (brief|memorandum)", "filename_defense_brief"),
    # Pro Hac Vice admissions — always filed BY opposing counsel
    (r"\b(admission|admitt(?:ed|ance))\s+pro\s+hac\s+vice", "filename_pro_hac_vice"),
    (r"\bpro\s+hac\s+vice\s+(by|of|for)", "filename_pro_hac_vice_by"),
    # Generic appearance/entry of appearance by counsel
    (r"\b(entry\s+of\s+appearance|notice\s+of\s+appearance)\b", "filename_entry_of_appearance"),
]

FILENAME_RULES_TRANSCRIPT = [
    (r"transcript(?![a-z])", "filename_transcript"),
    (r"\bhearing[\s_-]?transcript", "filename_hearing_transcript"),
    (r"\bdeposition(?![a-z])", "filename_deposition"),
    (r"^tr\d+", "filename_transcript_abbreviation"),
]

FILENAME_RULES_CORRESPONDENCE = [
    (r"^email\b", "filename_email_prefix"),
    (r"^letter\b", "filename_letter_prefix"),
    (r"\bcorrespondence\b", "filename_correspondence"),
    (r"^trackletter", "filename_trackletter"),
]

FILENAME_RULES_EXHIBIT = [
    (r"^exhibit[\s_-]", "filename_exhibit_prefix"),
    (r"\bexh[\s_.\-]\d+", "filename_exh_numbered"),
    (r"^ex\.[\s_]", "filename_ex_abbrev"),
    (r"\b1099[\s_-]?[a-z]\b", "filename_1099_form"),
    (r"\bw-?2\b|\bw-?9\b", "filename_tax_form"),
    (r"\bschedule\s+[a-z]\b", "filename_schedule_form"),
]

# Gmail ECF notification rules — these are tricky
# They're typically named like:
# "Gmail - Activity in Case X-cv-Y NAME v. OTHERS Order on Motion for Z.pdf"
GMAIL_ECF_PATTERN = re.compile(r"gmail.*activity in case.*", re.IGNORECASE)


# ─────────────────────────────────────────────────────────────────────────────
# Content marker rules
# ─────────────────────────────────────────────────────────────────────────────

CONTENT_MARKERS_JUDICIAL = [
    "it is hereby ordered",
    "it is so ordered",
    "by the court:",
    "ordered that:",
    "ordered, adjudged",
    "for the foregoing reasons",
    "the court finds",
    "the court holds",
    "the court concludes",
    "memorandum opinion",
    "memorandum and order",
    "/s/ honorable",
    "/s/ hon. ",
    "/s/ judge",
    "u.s. district judge",
    "united states district judge",
    "circuit judge",
    "associate justice",
    "before:",  # appellate panel header
    "now, therefore, it is ordered",
    "and it is further ordered",
]

CONTENT_MARKERS_PRO_SE_PLAINTIFF = [
    "plaintiff respectfully",
    "plaintiff submits",
    "plaintiff hereby",
    "plaintiff moves",
    "plaintiff requests",
    "plaintiff prays",
    "plaintiff, pro se",
    "comes now plaintiff",
    "comes now the plaintiff",
    "pro se plaintiff",
    "i, plaintiff",
    "i am the plaintiff",
    "respectfully submitted,\nplaintiff",
    "respectfully submitted,\n/s/",
]

CONTENT_MARKERS_OPPOSING_PARTY = [
    "defendant respectfully",
    "defendant submits",
    "defendant hereby",
    "defendant moves",
    "defendants respectfully",
    "defendants move",
    "the undersigned counsel for defendant",
    "counsel for defendant",
]


# ─────────────────────────────────────────────────────────────────────────────
# Case yaml lookup
# ─────────────────────────────────────────────────────────────────────────────

def _slug_for_case(case_id: str) -> Optional[str]:
    """case_id 'fed:2:26-cv-00123' → 'fed_26-cv-00123'."""
    parts = case_id.split(":")
    if not parts:
        return None
    if case_id.startswith("fed:"):
        return "fed_" + parts[-1]
    if case_id.startswith("phila_ccp:"):
        return "phila_ccp_" + parts[-1]
    if case_id.startswith("pa_super:"):
        return "pa_super_" + parts[-1]
    return None


def _get_plaintiff_info(case_id: str) -> Tuple[Optional[str], bool]:
    """Returns (plaintiff_name, is_pro_se) from CASE_GROUND_TRUTH.yaml."""
    slug = _slug_for_case(case_id)
    if not slug:
        return (None, False)
    yaml_path = CASES_DIR / slug / "meta" / "CASE_GROUND_TRUTH.yaml"
    if not yaml_path.exists():
        return (None, False)
    try:
        import yaml
        d = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        caption = d.get("caption", {}) or {}
        return (caption.get("plaintiff"), bool(caption.get("pro_se")))
    except Exception as e:
        logger.debug(f"yaml read err for {case_id}: {e}")
        return (None, False)


# ─────────────────────────────────────────────────────────────────────────────
# Text extraction helper
# ─────────────────────────────────────────────────────────────────────────────

def _extract_text_safe(path: Path, max_chars: int = 2000) -> str:
    """Read first N chars of a document. Supports .pdf, .docx, .md, .txt."""
    if not path.exists() or not path.is_file():
        return ""
    suffix = path.suffix.lower()
    try:
        if suffix in (".md", ".txt"):
            return path.read_text(encoding="utf-8", errors="replace")[:max_chars]
        if suffix == ".pdf":
            try:
                import pdfplumber
                with pdfplumber.open(path) as pdf:
                    text = ""
                    for page in pdf.pages[:2]:
                        text += (page.extract_text() or "") + "\n"
                        if len(text) >= max_chars:
                            break
                    return text[:max_chars]
            except ImportError:
                try:
                    from pypdf import PdfReader
                    r = PdfReader(str(path))
                    text = ""
                    for p in r.pages[:2]:
                        text += (p.extract_text() or "") + "\n"
                        if len(text) >= max_chars:
                            break
                    return text[:max_chars]
                except Exception:
                    return ""
        if suffix == ".docx":
            try:
                from docx import Document
                doc = Document(str(path))
                text = "\n".join(p.text for p in doc.paragraphs[:50])
                return text[:max_chars]
            except Exception:
                return ""
    except Exception as e:
        logger.debug(f"text extract err {path}: {e}")
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# Main classifier
# ─────────────────────────────────────────────────────────────────────────────

def classify(file_path: Path, case_id: Optional[str] = None,
              probe_content: bool = True) -> RoleClassification:
    """Classify a document by authorship role.

    Args:
      file_path:    path to document
      case_id:      optional case id (e.g., 'fed:2:26-cv-00123') for name disambiguation
      probe_content: if False, only use filename (faster but less accurate)
    """
    file_path = Path(file_path)
    fname = file_path.name
    fname_lower = fname.lower()

    # ─── Stage 1: filename heuristics ────────────────────────────────────────

    # PRE-CHECK 0: named defendant + filing verb → opposing_party_filing
    # Runs FIRST so "AmeriSave Opposition to Motion to Remand" and
    # "State Farm Reply to Motion" don\'t fall through to plaintiff patterns.
    _DEFENDANT_NAMES_RE = re.compile(
        r"\b(amerisave|capital[\s_-]?one|state[\s_-]?farm|transunion|trans[\s_-]?union|"
        r"equifax|experian|greensky|green[\s_-]?sky|avant|allstate|all[\s_-]?state|"
        r"lippi|nationwide|geico|progressive|chase|wells[\s_-]?fargo|"
        r"bank[\s_-]?of[\s_-]?america|citibank|citi|discover|amex|"
        r"american[\s_-]?express|synchrony|navient|sallie[\s_-]?mae)"
        r"[\s_,.-]+"
        r"(opposition|response|reply|brief|memo|memorandum|motion|answer|notice|filing)"
    )
    if _DEFENDANT_NAMES_RE.search(fname_lower):
        return RoleClassification(
            role="opposing_party_filing",
            confidence="high",
            method="filename_match",
            matched_pattern="precheck_named_defendant_filing",
            file_path=str(file_path),
        )

    # PRE-CHECK: "Response/Rebuttal/Reply TO a judicial thing" is plaintiff filing
    # Must check before generic judicial patterns to avoid "judges order" hijacking
    # "Rebuttal to judges order"
    if re.search(r"\b(rebuttal|response|reply|opposition|objection)\s+to\s+"
                  r"(judge|judicial|court|order|ruling|opinion|memorandum)",
                  fname_lower):
        return RoleClassification(
            role="pro_se_plaintiff_filing",
            confidence="high",
            method="filename_match",
            matched_pattern="response_to_judicial",
            file_path=str(file_path),
        )

    # Gmail ECF notifications — check next (these are court output)
    if GMAIL_ECF_PATTERN.search(fname_lower):
        if re.search(r"order on motion|order \(memorandum|memorandum and[\s_/]?or opinion",
                      fname_lower):
            return RoleClassification(
                role="judicial_output",
                confidence="high",
                method="filename_match",
                matched_pattern="gmail_ecf_order_notification",
                file_path=str(file_path),
            )
        return RoleClassification(
            role="judicial_output",
            confidence="med",
            method="filename_match",
            matched_pattern="gmail_ecf_notification_generic",
            file_path=str(file_path),
        )

    # Judicial output — check BEFORE plaintiff patterns so "Order on Motion for X"
    # doesn't get caught by plaintiff's "motion for X" rule
    for pattern, name in FILENAME_RULES_JUDICIAL_OUTPUT:
        if re.search(pattern, fname_lower):
            return RoleClassification(
                role="judicial_output",
                confidence="high",
                method="filename_match",
                matched_pattern=name,
                file_path=str(file_path),
            )

    # Pro se plaintiff filings — strong signal
    for pattern, name in FILENAME_RULES_PRO_SE_PLAINTIFF:
        if re.search(pattern, fname_lower):
            return RoleClassification(
                role="pro_se_plaintiff_filing",
                confidence="high",
                method="filename_match",
                matched_pattern=name,
                file_path=str(file_path),
            )

    # Opposing party — strong signal
    for pattern, name in FILENAME_RULES_OPPOSING_PARTY:
        if re.search(pattern, fname_lower):
            return RoleClassification(
                role="opposing_party_filing",
                confidence="high",
                method="filename_match",
                matched_pattern=name,
                file_path=str(file_path),
            )

    # Transcripts
    for pattern, name in FILENAME_RULES_TRANSCRIPT:
        if re.search(pattern, fname_lower):
            return RoleClassification(
                role="transcript",
                confidence="high",
                method="filename_match",
                matched_pattern=name,
                file_path=str(file_path),
            )

    # Correspondence
    for pattern, name in FILENAME_RULES_CORRESPONDENCE:
        if re.search(pattern, fname_lower):
            return RoleClassification(
                role="correspondence",
                confidence="high",
                method="filename_match",
                matched_pattern=name,
                file_path=str(file_path),
            )

    # Exhibits
    for pattern, name in FILENAME_RULES_EXHIBIT:
        if re.search(pattern, fname_lower):
            return RoleClassification(
                role="exhibit_or_evidence",
                confidence="med",
                method="filename_match",
                matched_pattern=name,
                file_path=str(file_path),
            )

    # ─── Stage 2: content markers (for ECF entries and ambiguous filenames) ──

    if not probe_content:
        return RoleClassification(
            role="unknown",
            confidence="low",
            method="filename_only_no_match",
            file_path=str(file_path),
        )

    text = _extract_text_safe(file_path, max_chars=2000)
    text_lower = text.lower()

    if not text_lower:
        return RoleClassification(
            role="unknown",
            confidence="low",
            method="content_extraction_failed",
            file_path=str(file_path),
        )

    # ─── PRE-CHECK: counsel signature block overrides judicial markers ──────
    # A file like h73ELbL6J.pdf contains the Judge's order on page 1
    # (which the first-2000-char read catches) but the bulk of the text
    # (pages 2-8) is Example Trust Co's opposition brief. If a counsel
    # signature block appears anywhere in the probed text, this is NOT
    # judicial output — it's opposing counsel's filing.
    #
    # This catches the pattern where a judicial order is combined with
    # an opposing party's brief in a single PDF (common with ECF filings).
    counsel_overrides = [
        r"(counsel|attorney\s+general|attorneys?)\s+for\s+(defendant|garnishee|respondent|plaintiff\s+opposing)",
        r"on\s+behalf\s+of\s+(defendant|garnishee|respondent)",
        r"represents?\s+(defendant|garnishee|respondent)",
        r"by:\s*/s/[A-Z][a-z]+\s+[A-Z][a-z]+\s*\n\s*[A-Z][A-Z\s]+(?:LLP|PC|P\.?A\.?L\.?C\.?|LLC)\s*\n\s*By:\s*/s/",
        r"counsel\s+for\s+the\s+(garnishee|defendant|respondent|opposing\s+party)",
        r"attorneys?\s+for\s+the\s+(garnishee|defendant|respondent)",
        # Catch "Counsel for [Entity Name]" — any named entity signature block
        # indicates this is a party filing, not judicial output.
        r"counsel\s+for\s+[A-Z][A-Za-z\s]+(?:Company|Corporation|LLC|Insurance|Trust|Bank|Group)",
        r"attorneys?\s+for\s+[A-Z][A-Za-z\s]+(?:Company|Corporation|LLC|Insurance|Trust|Bank|Group)",
    ]
    for pat in counsel_overrides:
        if re.search(pat, text_lower):
            return RoleClassification(
                role="opposing_party_filing",
                confidence="high",
                method="content_counsel_override",
                matched_pattern=f"counsel_sig: {pat!r}",
                extracted_text_snippet=text[:200],
                file_path=str(file_path),
            )

    for marker in CONTENT_MARKERS_JUDICIAL:
        if marker in text_lower:
            return RoleClassification(
                role="judicial_output",
                confidence="high",
                method="content_marker",
                matched_pattern=f"judicial: {marker!r}",
                extracted_text_snippet=text[:200],
                file_path=str(file_path),
            )

    for marker in CONTENT_MARKERS_PRO_SE_PLAINTIFF:
        if marker in text_lower:
            return RoleClassification(
                role="pro_se_plaintiff_filing",
                confidence="high",
                method="content_marker",
                matched_pattern=f"plaintiff: {marker!r}",
                extracted_text_snippet=text[:200],
                file_path=str(file_path),
            )

    for marker in CONTENT_MARKERS_OPPOSING_PARTY:
        if marker in text_lower:
            return RoleClassification(
                role="opposing_party_filing",
                confidence="high",
                method="content_marker",
                matched_pattern=f"defendant: {marker!r}",
                extracted_text_snippet=text[:200],
                file_path=str(file_path),
            )

    # ─── Stage 3: plaintiff-name match (last resort) ─────────────────────────
    if case_id:
        plaintiff_name, is_pro_se = _get_plaintiff_info(case_id)
        if plaintiff_name and is_pro_se:
            pn_lower = plaintiff_name.lower()
            # First check for opposing-counsel signature blocks; if present,
            # this is NOT a plaintiff filing even if the name appears in the caption.
            opposing_signals = [
                "counsel for defendant", "attorneys for defendant",
                "on behalf of defendant", "represents defendant",
                "for defendant ", "for the defendant",
                "pro hac vice", "admission of",
                "esquire (admitted", "esquire (pro hac",
            ]
            has_opposing_signal = any(s in text_lower for s in opposing_signals)
            # Look for plaintiff name in content with filing-language nearby
            if not has_opposing_signal and pn_lower in text_lower:
                # Check for filing language within ±200 chars of name
                idx = text_lower.find(pn_lower)
                window = text_lower[max(0, idx-200):idx+200]
                if any(t in window for t in [
                    "respectfully", "moves", "submits", "petitions",
                    "states", "alleges", "avers", "/s/",
                ]):
                    return RoleClassification(
                        role="pro_se_plaintiff_filing",
                        confidence="med",
                        method="case_yaml_plaintiff_name_match",
                        matched_pattern=f"plaintiff_name='{plaintiff_name}'",
                        extracted_text_snippet=text[idx:idx+200] if idx >= 0 else text[:200],
                        file_path=str(file_path),
                    )

    return RoleClassification(
        role="unknown",
        confidence="low",
        method="no_distinctive_markers",
        extracted_text_snippet=text[:200],
        file_path=str(file_path),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Audit: classify all documents in a case folder
# ─────────────────────────────────────────────────────────────────────────────

def audit_case(case_id: str) -> Dict[str, Any]:
    """Walk a case folder, classify every document, return summary."""
    slug = _slug_for_case(case_id)
    if not slug:
        return {"error": f"unknown case_id format: {case_id}"}
    case_dir = CASES_DIR / slug
    if not case_dir.exists():
        return {"error": f"case dir not found: {case_dir}"}

    results: List[Dict[str, Any]] = []
    for ext in (".pdf", ".docx", ".doc", ".md", ".txt"):
        for f in case_dir.rglob(f"*{ext}"):
            if "_audits" in f.parts or "chain_outputs" in f.parts:
                continue
            if "_extractor_runs" in f.parts:
                continue
            rc = classify(f, case_id=case_id)
            results.append({
                "file": str(f.relative_to(case_dir)),
                "role": rc.role,
                "confidence": rc.confidence,
                "method": rc.method,
                "pattern": rc.matched_pattern,
            })

    # Summary
    by_role: Dict[str, int] = {}
    for r in results:
        by_role[r["role"]] = by_role.get(r["role"], 0) + 1

    would_scan = sum(1 for r in results if r["role"] == "judicial_output")
    would_skip = len(results) - would_scan

    return {
        "case_id": case_id,
        "case_dir": str(case_dir),
        "total_documents": len(results),
        "by_role": by_role,
        "would_scan_for_judicial_violations": would_scan,
        "would_skip": would_skip,
        "documents": results,
    }


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


# === BEEZLE_CLASSIFY_SHIM_V1 ===
# Backwards-compatibility alias for legacy callers that import
# `classify_doc_with_evidence`. The real function is `classify()` above.
# This wrapper accepts the extra kwargs older callers pass (defendants=,
# plaintiff_name=, **kw), ignores them, and returns dataclasses.asdict()
# with LEGACY role names so callers using string comparisons against
# old labels keep working:
#     judicial_output         -> judge
#     opposing_party_filing   -> opposing_counsel
#     pro_se_plaintiff_filing -> plaintiff
# Other roles (exhibit_or_evidence, correspondence, transcript, unknown)
# pass through unchanged.

_BEEZLE_LEGACY_ROLE_ALIAS = {
    "judicial_output":         "judge",
    "opposing_party_filing":   "opposing_counsel",
    "pro_se_plaintiff_filing": "plaintiff",
}


def classify_doc_with_evidence(file_path, case_id=None, defendants=None,
                                plaintiff_name=None, probe_content=True, **kw):
    """Compat shim around classify(). Returns dict (not dataclass) with
    LEGACY role names so existing string comparisons in callers still work.
    """
    from dataclasses import asdict
    try:
        result = classify(file_path, case_id=case_id, probe_content=probe_content)
        d = asdict(result)
        new_role = d.get("role")
        if new_role in _BEEZLE_LEGACY_ROLE_ALIAS:
            d["role"] = _BEEZLE_LEGACY_ROLE_ALIAS[new_role]
            d["_canonical_role"] = new_role
        return d
    except Exception as _e:
        return {"role": "unknown", "confidence": "low", "method": "shim_error",
                 "matched_pattern": None, "extracted_text_snippet": None,
                 "file_path": str(file_path), "error": str(_e)}
# === END BEEZLE_CLASSIFY_SHIM_V1 ===

if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_class = sub.add_parser("classify", help="Classify a single document")
    p_class.add_argument("path")
    p_class.add_argument("--case-id", default=None)
    p_class.add_argument("--no-content", action="store_true",
                          help="Use filename only (faster)")
    p_class.add_argument("--json", action="store_true")

    p_audit = sub.add_parser("audit", help="Classify all documents in a case")
    p_audit.add_argument("case_id")
    p_audit.add_argument("--json", action="store_true")
    p_audit.add_argument("--show-all", action="store_true",
                          help="Show every document (default: summary + non-judicial only)")

    sub.add_parser("list-rules", help="List all classification rules")

    args = ap.parse_args()

    if args.cmd == "classify":
        rc = classify(Path(args.path), case_id=args.case_id,
                       probe_content=not args.no_content)
        if args.json:
            print(json.dumps(asdict(rc), indent=2, default=str))
        else:
            print(f"\n  file:        {args.path}")
            print(f"  role:        {rc.role}")
            print(f"  confidence:  {rc.confidence}")
            print(f"  method:      {rc.method}")
            if rc.matched_pattern:
                print(f"  pattern:     {rc.matched_pattern}")
            if rc.extracted_text_snippet:
                snippet = rc.extracted_text_snippet.replace("\n", " ")[:120]
                print(f"  snippet:     {snippet}...")

    elif args.cmd == "audit":
        result = audit_case(args.case_id)
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            if "error" in result:
                print(f"  ✗ {result['error']}", file=sys.stderr); sys.exit(1)
            print(f"\n══ Audit: {result['case_id']} ══")
            print(f"  Total docs:               {result['total_documents']}")
            print(f"  Would SCAN (judicial):    {result['would_scan_for_judicial_violations']}")
            print(f"  Would SKIP (everything else): {result['would_skip']}")
            print(f"\n  Breakdown by role:")
            for role, count in sorted(result["by_role"].items(),
                                       key=lambda x: -x[1]):
                marker = "✓" if role == "judicial_output" else "·"
                print(f"    {marker} {role:30s}  {count:4d}")
            if args.show_all:
                print(f"\n  All documents:")
                for d in result["documents"]:
                    marker = "✓SCAN" if d["role"] == "judicial_output" else "·SKIP"
                    print(f"    {marker}  [{d['confidence']:4s}] {d['role']:28s}  {d['file']}")
            else:
                # Show non-judicial only (these are the ones we're FIXING)
                non_jud = [d for d in result["documents"]
                            if d["role"] != "judicial_output"]
                if non_jud:
                    print(f"\n  Will be SKIPPED (no longer scanned for judicial violations):")
                    for d in non_jud[:30]:
                        print(f"    · [{d['confidence']:4s}] {d['role']:28s}  {d['file']}")
                    if len(non_jud) > 30:
                        print(f"    · ...+{len(non_jud)-30} more")

    elif args.cmd == "list-rules":
        print("\n  Pro se plaintiff filename patterns:")
        for p, n in FILENAME_RULES_PRO_SE_PLAINTIFF:
            print(f"    · {n:40s}  /{p}/")
        print("\n  Judicial output filename patterns:")
        for p, n in FILENAME_RULES_JUDICIAL_OUTPUT:
            print(f"    · {n:40s}  /{p}/")
        print("\n  Opposing party filename patterns:")
        for p, n in FILENAME_RULES_OPPOSING_PARTY:
            print(f"    · {n:40s}  /{p}/")
        print("\n  Content markers (judicial):")
        for m in CONTENT_MARKERS_JUDICIAL:
            print(f"    · {m!r}")
        print("\n  Content markers (pro se plaintiff):")
        for m in CONTENT_MARKERS_PRO_SE_PLAINTIFF:
            print(f"    · {m!r}")
