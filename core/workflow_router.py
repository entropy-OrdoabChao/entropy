"""
Beezle AI — Hermes Workflow Router
Single entry point for all document generation.

Hermes calls: POST /api/draft
Body: {
    "intent": "complaint|reply|motion|appeal|scorecard",
    "court": "federal_district|state_ccp|federal_appellate|scotus",
    "jurisdiction": "EDPA|PA_CP_PHILA|CA3|SCOTUS",
    "documents": ["path1.pdf", "path2.pdf"],
    "user_input": "natural language description",
    "damages": "2000000",
    "plaintiff": {...},
    "case_id": "optional"
}

Returns: {
    "files": ["complaint.docx", "exhibit_a.docx", ...],
    "status": "success",
    "pipeline": "complaint_federal"
}
"""

import sys, json, logging
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field

sys.path.insert(0, str(Path(__file__).parent.parent))

log = logging.getLogger("workflow_router")


# ═══════════════════════════════════════════════════════
# DOCUMENT TYPE CLASSIFIER
# ═══════════════════════════════════════════════════════

DOC_PATTERNS = {
    "court_order": ["ORDER", "AND NOW, this", "BY THE COURT", "ORDERED that", "hereby ORDERED"],
    "motion_to_dismiss": ["motion to dismiss", "FRCP 12(b)", "Rule 12(b)(6)", "failure to state a claim"],
    "opposition": ["opposition to", "response to plaintiff", "opposes plaintiff"],
    "complaint": ["COMPLAINT FOR", "CAUSES OF ACTION", "PRAYER FOR RELIEF", "COUNT I"],
    "foia_request": ["Freedom of Information", "FOIA", "5 U.S.C. § 552"],
    "foia_response": ["FOIA Request No.", "tracking number", "fee waiver"],
    "foia_appeal": ["administrative appeal", "FOIA appeal", "constructive denial"],
    "acknowledgment": ["acknowledge receipt", "assigned tracking", "acknowledgment"],
    "verification": ["affirmation", "declaration", "under penalty of perjury", "notarized"],
    "email": ["From:", "To:", "Subject:", "gmail", "@hud.gov", "@"],
    "memorandum": ["MEMORANDUM OF LAW", "MEMORANDUM IN SUPPORT"],
    "notice": ["NOTICE OF", "notice is hereby given"],
    "certificate": ["CERTIFICATE OF SERVICE", "hereby certify"],
    "appeal_brief": ["APPELLATE BRIEF", "Statement of Errors", "appellate jurisdiction"],
}


def classify_document(text: str, filename: str = "") -> str:
    """Classify a document by its content."""
    text_upper = text[:3000].upper()
    fn_lower = filename.lower()

    scores = {}
    for doc_type, patterns in DOC_PATTERNS.items():
        score = 0
        for p in patterns:
            if p.upper() in text_upper or p.lower() in fn_lower:
                score += 1
        if score > 0:
            scores[doc_type] = score

    if scores:
        return max(scores, key=scores.get)
    return "unknown"


def classify_documents(file_paths: List[str]) -> List[Dict]:
    """Classify multiple documents and assign exhibit letters."""
    from pipeline.doc_reader import extract_text

    results = []
    for i, fp in enumerate(file_paths):
        p = Path(fp)
        if not p.exists():
            continue
        try:
            text = extract_text(p)
            doc_type = classify_document(text or "", p.name)
            letter = chr(65 + i)  # A, B, C...
            results.append({
                "path": str(p),
                "filename": p.name,
                "doc_type": doc_type,
                "exhibit_letter": letter,
                "text_preview": (text or "")[:500],
                "size": p.stat().st_size,
            })
        except Exception as e:
            log.warning(f"Failed to classify {fp}: {e}")

    return results


# ═══════════════════════════════════════════════════════
# INTENT CLASSIFIER
# ═══════════════════════════════════════════════════════

INTENT_KEYWORDS = {
    "complaint": ["complaint", "sue", "file suit", "bring action", "cause of action", "draft a complaint", "new case", "initiate"],
    "amended_complaint": ["amended complaint", "amend complaint", "first amended", "second amended"],
    "reply_brief": ["reply", "reply brief", "response to motion", "opposition brief", "respond to"],
    "motion": ["motion to", "file a motion", "motion for", "strike", "dismiss", "reconsider", "compel"],
    "appeal": ["appeal", "notice of appeal", "appellate brief", "statement of errors", "appellate"],
    "scorecard": ["scorecard", "audit", "violations", "scan for violations"],
    "cert_petition": ["cert petition", "certiorari", "supreme court petition", "scotus"],
    "regulatory": ["regulatory complaint", "agency complaint", "cfpb", "occ", "fincen"],
}

COURT_KEYWORDS = {
    "federal_district": ["federal court", "district court", "EDPA", "eastern district", "U.S. district"],
    "state_ccp": ["court of common pleas", "state court", "CCP", "phila", "philadelphia court"],
    "federal_appellate": ["third circuit", "court of appeals", "appellate", "CA3", "circuit court"],
    "scotus": ["supreme court", "SCOTUS", "certiorari", "cert petition"],
}


def classify_intent(user_input: str) -> str:
    """Determine what type of document the user wants."""
    text = user_input.lower()
    scores = {}
    for intent, keywords in INTENT_KEYWORDS.items():
        score = sum(1 for k in keywords if k.lower() in text)
        if score > 0:
            scores[intent] = score
    return max(scores, key=scores.get) if scores else "complaint"


def classify_court(user_input: str, jurisdiction: str = "") -> str:
    """Determine which court system."""
    text = (user_input + " " + jurisdiction).lower()
    scores = {}
    for court, keywords in COURT_KEYWORDS.items():
        score = sum(1 for k in keywords if k.lower() in text)
        if score > 0:
            scores[court] = score
    return max(scores, key=scores.get) if scores else "federal_district"


# ═══════════════════════════════════════════════════════
# PIPELINE ROUTER
# ═══════════════════════════════════════════════════════

PIPELINE_MAP = {
    # (intent, court) → pipeline function name
    ("complaint", "federal_district"): "pipeline_complaint_federal",
    ("complaint", "state_ccp"): "pipeline_complaint_state",
    ("amended_complaint", "federal_district"): "pipeline_complaint_federal",
    ("reply_brief", "federal_district"): "pipeline_reply_brief",
    ("reply_brief", "state_ccp"): "pipeline_reply_brief",
    ("motion", "federal_district"): "pipeline_motion",
    ("motion", "state_ccp"): "pipeline_motion",
    ("appeal", "federal_appellate"): "pipeline_appeal",
    ("appeal", "state_ccp"): "pipeline_appeal_state",
    ("scorecard", "federal_district"): "pipeline_scorecard",
    ("scorecard", "state_ccp"): "pipeline_scorecard",
    ("cert_petition", "scotus"): "pipeline_cert",
}


async def route_draft(request_data: Dict) -> Dict:
    """
    Main entry point. Hermes calls this with user intent + documents.
    Returns paths to generated files.
    """
    from datetime import datetime
    from core.path_manager import OUTPUT_DIR

    user_input = request_data.get("user_input", "")
    documents = request_data.get("documents", [])
    intent = request_data.get("intent") or classify_intent(user_input)
    court = request_data.get("court") or classify_court(user_input, request_data.get("jurisdiction", ""))
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    result = {
        "intent": intent,
        "court": court,
        "pipeline": PIPELINE_MAP.get((intent, court), f"pipeline_{intent}"),
        "files": [],
        "status": "success",
        "errors": [],
        "classified_documents": [],
    }

    # Classify uploaded documents
    if documents:
        result["classified_documents"] = classify_documents(documents)

    # Route to correct pipeline
    pipeline_name = result["pipeline"]

    try:
        if pipeline_name == "pipeline_complaint_federal":
            files = await _run_complaint(request_data, result, court="federal", ts=ts)
            result["files"] = files

        elif pipeline_name == "pipeline_complaint_state":
            files = await _run_complaint(request_data, result, court="state", ts=ts)
            result["files"] = files

        elif pipeline_name == "pipeline_reply_brief":
            files = await _run_reply_brief(request_data, result, ts=ts)
            result["files"] = files

        elif pipeline_name == "pipeline_scorecard":
            files = await _run_scorecard(request_data, result, ts=ts)
            result["files"] = files

        elif pipeline_name == "pipeline_appeal":
            files = await _run_appeal(request_data, result, ts=ts)
            result["files"] = files

        else:
            result["status"] = "needs_input"
            result["questions"] = _get_missing_questions(intent, request_data)

    except Exception as e:
        result["status"] = "error"
        result["errors"].append(str(e))
        log.error(f"Pipeline {pipeline_name} failed: {e}")

    return result


# ═══════════════════════════════════════════════════════
# PIPELINE IMPLEMENTATIONS
# ═══════════════════════════════════════════════════════

async def _run_complaint(data: Dict, result: Dict, court: str, ts: str) -> List[str]:
    """Generate complaint + exhibits."""
    from core.path_manager import OUTPUT_DIR
    from generation.complaint_drafter import build_complaint_from_facts

    classified = result.get("classified_documents", [])
    plaintiff = data.get("plaintiff", {})
    defendants = data.get("defendants", [])

    # Build exhibits from classified documents
    exhibits = []
    for doc in classified:
        exhibits.append({
            "letter": doc["exhibit_letter"],
            "description": f"{doc['doc_type'].replace('_', ' ').title()} — {doc['filename']}",
            "date": "",
        })

    # Use LLM to extract claims from documents if claims_data not provided
    claims_data = data.get("claims_data", [])

    if not claims_data:
        # Fallback: need LLM extraction or return needs_input
        result["status"] = "needs_input"
        result["questions"] = [
            "What specific legal violations occurred?",
            "What statutes were violated?",
            "What relief are you seeking?",
        ]
        return []

    # Determine district
    if court == "federal":
        court_name = "IN THE UNITED STATES DISTRICT COURT"
        district = data.get("district", "FOR THE EASTERN DISTRICT OF PENNSYLVANIA")
    else:
        court_name = "IN THE COURT OF COMMON PLEAS"
        district = data.get("district", "PHILADELPHIA COUNTY")

    out_path = OUTPUT_DIR / f"complaint_{ts}.docx"

    path = build_complaint_from_facts(
        plaintiff_name=plaintiff.get("name", ""),
        plaintiff_address=plaintiff.get("address", ""),
        defendants=defendants,
        court=court_name,
        district=district,
        complaint_title=data.get("title", "COMPLAINT"),
        facts=[],
        claims_data=claims_data,
        exhibits=exhibits,
        output_path=out_path,
    )

    return [str(path)]


async def _run_reply_brief(data: Dict, result: Dict, ts: str) -> List[str]:
    """Route to existing Analysis pipeline (beezle.py run)."""
    # The Analysis tab already does this via POST /api/run
    result["redirect"] = "/api/run"
    result["message"] = "Use the Analysis tab to generate reply briefs. Upload the opponent filing and click Analyze."
    return []


async def _run_scorecard(data: Dict, result: Dict, ts: str) -> List[str]:
    """Route to existing scorecard pipeline."""
    result["redirect"] = "/api/audit_per_case"
    result["message"] = "Use the Scorecard tab to run per-case audits."
    return []


async def _run_appeal(data: Dict, result: Dict, ts: str) -> List[str]:
    """Route to cert engine if available."""
    result["redirect"] = "/api/run"
    result["message"] = "Appeal generation uses the Analysis pipeline with appellate posture."
    return []


def _get_missing_questions(intent: str, data: Dict) -> List[str]:
    """Return questions Hermes should ask based on intent."""
    base = []

    if not data.get("plaintiff"):
        base.append("What is the plaintiff's full name and address?")

    if not data.get("defendants"):
        base.append("Who are the defendants? Include their names and roles.")

    if intent == "complaint":
        base.extend([
            "What specific legal violations occurred?",
            "What statutes were violated?",
            "What damages are you seeking?",
            "In which court should this be filed (federal/state)?",
        ])
    elif intent == "motion":
        base.extend([
            "What type of motion (to dismiss, to strike, for reconsideration)?",
            "What is the case number?",
        ])
    elif intent == "appeal":
        base.extend([
            "What order are you appealing?",
            "What errors did the lower court make?",
        ])

    return base


# ═══════════════════════════════════════════════════════
# EXHIBIT AUTO-BUILDER
# ═══════════════════════════════════════════════════════

def build_exhibit_appendix(classified_docs: List[Dict], output_path: Path) -> Path:
    """
    Build an exhibit appendix .docx that contains copies of each document.
    Each exhibit gets a cover page + the extracted text content.
    """
    from docx import Document
    from docx.shared import Pt
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from pipeline.doc_reader import extract_text

    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Times New Roman"
    style.font.size = Pt(12)

    for i, cdoc in enumerate(classified_docs):
        if i > 0:
            doc.add_page_break()

        # Cover page
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        r = p.add_run(f"EXHIBIT {cdoc['exhibit_letter']}")
        r.bold = True
        r.font.size = Pt(16)

        doc.add_paragraph("")

        p2 = doc.add_paragraph()
        p2.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p2.add_run(cdoc.get("description", cdoc["filename"]))

        doc.add_paragraph("")
        doc.add_paragraph(f"Document Type: {cdoc['doc_type'].replace('_', ' ').title()}")
        doc.add_paragraph(f"Source: {cdoc['filename']}")
        doc.add_paragraph("")
        doc.add_paragraph("\u2500" * 60)
        doc.add_paragraph("")

        # Extract and insert content
        try:
            fp = Path(cdoc["path"])
            if fp.exists():
                text = extract_text(fp)
                if text:
                    # Split into paragraphs and add
                    for para_text in text.split("\n"):
                        if para_text.strip():
                            doc.add_paragraph(para_text.strip())
        except Exception:
            doc.add_paragraph("[Content could not be extracted]")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))
    return output_path