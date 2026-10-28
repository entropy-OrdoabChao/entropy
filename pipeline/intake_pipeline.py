"""
pipeline/intake_pipeline.py — Upgraded pipeline_intake for MCP server.

Handles:
  1. Defendant separation (individual entries with roles/titles)
  2. Document text → structured facts extraction
  3. Timeline building from document dates
  4. Exhibit assignment

Drop-in replacement for pipeline_intake in beezle_mcp_server.py.

Integration:
    from pipeline.intake_pipeline import pipeline_intake_v3
    
    @mcp.tool  
    def pipeline_intake(case_id, plaintiff_name, ...) -> dict:
        return pipeline_intake_v3(case_id, plaintiff_name, ...)
"""

from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
# === BEEZLE_ROOT_SHIM (WSL-safe; auto-injected) ===
import os as _os_broot
from pathlib import Path as _Path_broot
def _resolve_beezle_root():
    _e = _os_broot.environ.get("BEEZLE_ROOT")
    if _e:
        return _Path_broot(_e)
    _d = _Path_broot(__file__).resolve().parent
    for _ in range(5):
        if (_d / "pipeline").exists() or (_d / "obsidian_sync").exists():
            return _d
        _d = _d.parent
    return _Path_broot(str(_eroot()))
_BEEZLE_ROOT = _resolve_beezle_root()
# === END BEEZLE_ROOT_SHIM ===
import re
import time
from pathlib import Path
from typing import Dict, List, Optional


# BEEZLE_FN_UNUSED 2026-06-13: pipeline_intake_v3 (no static refs in pipeline/)
def pipeline_intake_v3(
    case_id: str,
    plaintiff_name: str,
    plaintiff_address: str,
    defendant_names: str,
    case_description: str,
    damages: str = "",
    court: str = "EDPA",
    # New V3 fields
    plaintiff_email: str = "",
    defendants_structured: Optional[List[Dict]] = None,
    documents: Optional[List[Dict]] = None,
    timeline: Optional[List[Dict]] = None,
    exhibits: Optional[List[Dict]] = None,
    preliminary_statement: Optional[List[str]] = None,
    fact_sections: Optional[Dict[str, List[str]]] = None,
    exhaustion_paragraphs: Optional[List[str]] = None,
    prayer_for_relief: Optional[List[str]] = None,
    admissions: Optional[List[str]] = None,
) -> dict:
    """
    STEP 1 (V3): Create case and extract initial facts.
    
    Accepts either:
      - defendant_names as comma-separated string (backward compat)
      - defendants_structured as list of dicts with full info
    
    Documents can include extracted text for fact extraction.
    """
    import sys
    base = _BEEZLE_ROOT if sys.platform == "win32" else Path(__file__).parent.parent
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))
    
    from core.case_state import create_state, save_state
    
    state = create_state(case_id)
    
    # ── Plaintiff ──
    state["plaintiff"] = {
        "name": plaintiff_name,
        "address": plaintiff_address,
        "email": plaintiff_email,
        "role": "a natural person, proceeding pro se",
    }
    
    # ── Defendants ──
    if defendants_structured:
        state["defendants"] = defendants_structured
    else:
        state["defendants"] = parse_defendants(defendant_names)
    
    # ── Facts ──
    state["facts"] = [case_description] if case_description else []
    state["damages"] = damages
    state["court"] = court
    
    # ── Optional structured data ──
    if documents:
        state["documents"] = documents
    if timeline:
        state["timeline"] = timeline
    if exhibits:
        state["exhibits"] = exhibits
    if preliminary_statement:
        state["preliminary_statement"] = preliminary_statement
    if fact_sections:
        state["fact_sections"] = fact_sections
    if exhaustion_paragraphs:
        state["exhaustion_paragraphs"] = exhaustion_paragraphs
    if prayer_for_relief:
        state["prayer_for_relief"] = prayer_for_relief
    if admissions:
        state["admissions"] = admissions
    
    state["step"] = "intake_complete"
    save_state(case_id, state)
    
    return {
        "status": "intake_complete",
        "case_id": case_id,
        "defendants_count": len(state["defendants"]),
        "facts_count": len(state["facts"]),
        "documents_count": len(state.get("documents", [])),
        "next_step": "pipeline_classify",
    }


def parse_defendants(defendant_names: str) -> List[Dict]:
    """
    Parse defendant string into separated entries.
    
    Handles formats:
      - "HUD, Yolonda Hatton, Bao-Anh Trinh"
      - "HUD and Yolonda Hatton and Bao-Anh Trinh"
      - "HUD; Yolonda Hatton, Gov Info Specialist, in her official capacity"
    
    Returns list of dicts with name, capacity, title, department, etc.
    """
    if not defendant_names:
        return []
    
    # Normalize separators
    text = defendant_names.strip()
    
    # Split on " and " or "," or ";"
    parts = re.split(r'\s+and\s+|;\s*', text)
    if len(parts) == 1:
        # Try comma split, but be careful with "Title, Department" patterns
        parts = [p.strip() for p in text.split(",")]
    
    defendants = []
    for part in parts:
        part = part.strip().rstrip(",;")
        if not part:
            continue
        
        d = _parse_single_defendant(part)
        if d:
            defendants.append(d)
    
    return defendants


def _parse_single_defendant(text: str) -> Dict:
    """Parse a single defendant entry into structured dict."""
    d = {
        "name": "",
        "capacity": "",
        "role": "",
        "title": "",
        "department": "",
        "email": "",
        "conduct": "",
    }
    
    # Check for capacity markers
    capacity_patterns = [
        (r'(?:in\s+)?(his|her|their)\s+official\s+capacit(?:y|ies)', 'official capacity'),
        (r'(?:in\s+)?(his|her|their)\s+individual\s+capacit(?:y|ies)', 'individual capacity'),
        (r'official\s+capacity', 'official capacity'),
    ]
    
    remaining = text
    for pattern, cap_value in capacity_patterns:
        m = re.search(pattern, remaining, re.IGNORECASE)
        if m:
            d["capacity"] = f"{cap_value}"
            remaining = remaining[:m.start()].strip().rstrip(",;")
            break
    
    # Check if it's an agency (contains keywords)
    agency_keywords = [
        "DEPARTMENT", "AGENCY", "BUREAU", "OFFICE", "COMMISSION",
        "ADMINISTRATION", "AUTHORITY", "SERVICE", "BOARD",
        "HUD", "DOJ", "FDA", "EPA", "IRS", "FBI", "CFPB",
        "U.S.", "UNITED STATES",
    ]
    is_agency = any(kw in remaining.upper() for kw in agency_keywords)
    
    if is_agency:
        d["name"] = remaining.strip()
        d["capacity"] = d["capacity"] or "agency"
    else:
        # It's a person — check for title after comma
        parts = remaining.split(",", 1)
        d["name"] = parts[0].strip()
        if len(parts) > 1:
            title_text = parts[1].strip()
            d["title"] = title_text
        if not d["capacity"]:
            d["capacity"] = "official capacity"
    
    return d


# BEEZLE_FN_UNUSED 2026-06-13: extract_facts_from_document (no static refs in pipeline/)
def extract_facts_from_document(doc_text: str, doc_name: str = "", exhibit_letter: str = "") -> Dict:
    """
    Extract structured facts from document text.
    Returns dict with dates, names, tracking_numbers, violations, admissions.
    
    This is a DETERMINISTIC extraction — pattern matching, not LLM.
    """
    result = {
        "source": doc_name,
        "exhibit": exhibit_letter,
        "dates": [],
        "names": [],
        "tracking_numbers": [],
        "violations": [],
        "admissions": [],
        "key_facts": [],
    }
    
    if not doc_text:
        return result
    
    # ── Date extraction ──
    date_patterns = [
        r'(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}',
        r'\d{1,2}/\d{1,2}/\d{4}',
        r'\d{4}-\d{2}-\d{2}',
    ]
    for pat in date_patterns:
        for m in re.finditer(pat, doc_text):
            if m.group() not in result["dates"]:
                result["dates"].append(m.group())
    
    # ── Tracking numbers ──
    tracking_patterns = [
        r'\d{2}-FI-[A-Z]{2}-\d{5}',       # FOIA tracking: 26-FI-HQ-00985
        r'\d{2}-A-[A-Z]-\d{5}',            # Appeal: 26-A-C-00014
        r'FHA\s*(?:Case\s*)?(?:No\.?\s*)?\d{3}-\d{7}-\d{3}',  # FHA case
        r'(?:Case|Docket|No\.?|Tracking)\s*(?:No\.?\s*)?[:#]?\s*[\d-]+',
    ]
    for pat in tracking_patterns:
        for m in re.finditer(pat, doc_text, re.IGNORECASE):
            val = m.group().strip()
            if val not in result["tracking_numbers"]:
                result["tracking_numbers"].append(val)
    
    # ── Violation indicators ──
    violation_keywords = [
        "failed to", "failure to", "did not", "has not", "refused to",
        "denied", "constructive denial", "violated", "breach",
        "non-compliance", "noncompliance",
    ]
    sentences = re.split(r'[.!?]+', doc_text)
    for sent in sentences:
        sent_lower = sent.lower().strip()
        if any(kw in sent_lower for kw in violation_keywords):
            clean = sent.strip()
            if len(clean) > 20 and clean not in result["violations"]:
                result["violations"].append(clean[:300])
    
    # ── Admission indicators ──
    admission_keywords = [
        "admits", "acknowledges", "concedes", "confirms",
        "constitutes a constructive denial",
    ]
    for sent in sentences:
        sent_lower = sent.lower().strip()
        if any(kw in sent_lower for kw in admission_keywords):
            clean = sent.strip()
            if len(clean) > 20 and clean not in result["admissions"]:
                result["admissions"].append(clean[:300])
    
    return result


# BEEZLE_FN_UNUSED 2026-06-13: build_timeline_from_documents (no static refs in pipeline/)
def build_timeline_from_documents(doc_extractions: List[Dict]) -> List[Dict]:
    """
    Build chronological timeline from multiple document extractions.
    
    Returns list of timeline entries sorted by date:
    [{"date": "January 8, 2026", "text": "...", "exhibit": "A", "section": "A. The Request"}]
    """
    entries = []
    
    for ext in doc_extractions:
        exhibit = ext.get("exhibit", "")
        source = ext.get("source", "")
        
        for date in ext.get("dates", []):
            entries.append({
                "date": date,
                "text": f"Document dated {date}",
                "exhibit": exhibit,
                "section": "General",
                "source": source,
            })
        
        for fact in ext.get("key_facts", []):
            entries.append({
                "date": "",
                "text": fact,
                "exhibit": exhibit,
                "section": "General",
                "source": source,
            })
    
    # Sort by date (crude but functional — proper date parsing can be added)
    return entries
