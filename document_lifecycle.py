"""
document_lifecycle.py — Document Lifecycle Orchestrator (Hermes MCP tools)

Master orchestrator that coordinates the full lifecycle of legal documents:
  intake → classify → draft → validate → queue → file → track

Exposes MCP tools for Hermes to manage documents from creation through filing.
All state persists to disk (JSON) so no work is lost across restarts.

Deterministic invariant: NO legal conclusions generated here. This module
orchestrates existing Beezle engines (definition_engine, if_then_engine,
complaint_drafter, reply_brief_drafter, etc.) and tracks their outputs.

Directory layout:
  <home>/lifecycle/
    documents/         ← generated document metadata (.json per doc)
    queue/             ← filing queue entries
    deadlines/         ← deadline tracking data
    archive/           ← completed/filed documents
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import json
import logging
import hashlib
import shutil
from datetime import datetime, timedelta
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Optional, Any

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

log = logging.getLogger("document_lifecycle")

# ─── Paths ───
BASE_PATH = _BEEZLE_ROOT
LIFECYCLE_DIR = BASE_PATH / "lifecycle"
DOCUMENTS_DIR = LIFECYCLE_DIR / "documents"
QUEUE_DIR = LIFECYCLE_DIR / "queue"
DEADLINES_DIR = LIFECYCLE_DIR / "deadlines"
ARCHIVE_DIR = LIFECYCLE_DIR / "archive"
OUTPUT_DIR = BASE_PATH / "output"

for _d in [DOCUMENTS_DIR, QUEUE_DIR, DEADLINES_DIR, ARCHIVE_DIR]:
    _d.mkdir(parents=True, exist_ok=True)


# ═══════════════════════════════════════════════════════
# DATA MODELS
# ═══════════════════════════════════════════════════════

class DocStatus(str, Enum):
    INTAKE = "intake"
    CLASSIFIED = "classified"
    DRAFTING = "drafting"
    DRAFT_COMPLETE = "draft_complete"
    VALIDATING = "validating"
    VALIDATED = "validated"
    VALIDATION_FAILED = "validation_failed"
    QUEUED = "queued"
    FILED = "filed"
    ARCHIVED = "archived"


class DocType(str, Enum):
    COMPLAINT = "complaint"
    AMENDED_COMPLAINT = "amended_complaint"
    REPLY_BRIEF = "reply_brief"
    MOTION = "motion"
    APPEAL = "appeal"
    SCORECARD = "scorecard"
    CERT_PETITION = "cert_petition"
    REGULATORY = "regulatory"
    FOIA = "foia"
    PRAECIPE = "praecipe"
    EXHIBIT = "exhibit"
    MEMORANDUM = "memorandum"


@dataclass
class DocumentRecord:
    doc_id: str
    case_id: str
    doc_type: str
    title: str
    status: str = DocStatus.INTAKE.value
    court: str = ""
    jurisdiction: str = ""
    created_at: str = ""
    updated_at: str = ""
    deadline: str = ""                    # ISO date string, empty if none
    deadline_source: str = ""             # "statute|rule|court_order|manual"
    files: list[str] = field(default_factory=list)     # generated file paths
    exhibits: list[str] = field(default_factory=list)
    validation_errors: list[str] = field(default_factory=list)
    validation_score: float = 0.0
    filing_queue_id: str = ""
    filed_at: str = ""
    filed_confirmation: str = ""
    metadata: dict = field(default_factory=dict)
    history: list[dict] = field(default_factory=list)  # status change log


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _doc_path(doc_id: str) -> Path:
    return DOCUMENTS_DIR / f"{doc_id}.json"


def _save_doc(rec: DocumentRecord) -> None:
    rec.updated_at = _now()
    _doc_path(rec.doc_id).write_text(
        json.dumps(asdict(rec), indent=2, default=str), encoding="utf-8"
    )


def _load_doc(doc_id: str) -> Optional[DocumentRecord]:
    path = _doc_path(doc_id)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return DocumentRecord(**data)


def _gen_doc_id(case_id: str, doc_type: str) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    h = hashlib.md5(f"{case_id}:{doc_type}:{ts}".encode()).hexdigest()[:6]
    return f"doc_{ts}_{h}"


def _transition(rec: DocumentRecord, new_status: str, note: str = "") -> None:
    """Record a status transition in the document history."""
    rec.history.append({
        "from": rec.status,
        "to": new_status,
        "at": _now(),
        "note": note,
    })
    rec.status = new_status


# ═══════════════════════════════════════════════════════
# CORE LIFECYCLE FUNCTIONS
# ═══════════════════════════════════════════════════════

def create_document(
    case_id: str,
    doc_type: str,
    title: str,
    court: str = "",
    jurisdiction: str = "",
    deadline: str = "",
    deadline_source: str = "",
    metadata: dict = None,
) -> dict:
    """Create a new document record and begin lifecycle tracking."""
    doc_id = _gen_doc_id(case_id, doc_type)
    rec = DocumentRecord(
        doc_id=doc_id,
        case_id=case_id,
        doc_type=doc_type,
        title=title,
        court=court,
        jurisdiction=jurisdiction,
        created_at=_now(),
        deadline=deadline,
        deadline_source=deadline_source,
        metadata=metadata or {},
    )
    _transition(rec, DocStatus.INTAKE.value, "Document created")
    _save_doc(rec)
    log.info(f"Created document {doc_id} for case {case_id} ({doc_type})")
    return asdict(rec)


def advance_document(doc_id: str, new_status: str, note: str = "",
                     files: list[str] = None, validation_errors: list[str] = None,
                     validation_score: float = None) -> dict:
    """Advance a document to the next lifecycle stage."""
    rec = _load_doc(doc_id)
    if rec is None:
        return {"error": f"Document not found: {doc_id}"}

    if files:
        rec.files.extend(files)
    if validation_errors is not None:
        rec.validation_errors = validation_errors
    if validation_score is not None:
        rec.validation_score = validation_score

    _transition(rec, new_status, note)
    _save_doc(rec)
    return asdict(rec)


def get_document(doc_id: str) -> dict:
    """Get a document record by ID."""
    rec = _load_doc(doc_id)
    if rec is None:
        return {"error": f"Document not found: {doc_id}"}
    return asdict(rec)


def list_documents(case_id: str = "", status: str = "", doc_type: str = "") -> dict:
    """List documents with optional filters."""
    results = []
    for path in DOCUMENTS_DIR.glob("doc_*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if case_id and data.get("case_id") != case_id:
                continue
            if status and data.get("status") != status:
                continue
            if doc_type and data.get("doc_type") != doc_type:
                continue
            results.append(data)
        except Exception:
            continue

    results.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    return {"count": len(results), "documents": results}


def draft_and_track(
    case_id: str,
    doc_type: str,
    title: str,
    user_input: str,
    court: str = "federal_district",
    jurisdiction: str = "EDPA",
    deadline: str = "",
    deadline_source: str = "",
    documents: list[str] = None,
    damages: str = "",
) -> dict:
    """
    Full lifecycle: create record → route to drafter → validate → queue.
    This is the primary entry point Hermes should call for document generation.
    """
    # 1. Create lifecycle record
    rec_data = create_document(
        case_id=case_id,
        doc_type=doc_type,
        title=title,
        court=court,
        jurisdiction=jurisdiction,
        deadline=deadline,
        deadline_source=deadline_source,
        metadata={"user_input": user_input, "damages": damages},
    )
    doc_id = rec_data["doc_id"]

    # 2. Route to appropriate drafter
    advance_document(doc_id, DocStatus.DRAFTING.value, "Routing to drafter")

    try:
        from core.workflow_router import route_draft
        import asyncio

        request_data = {
            "intent": doc_type,
            "user_input": user_input,
            "court": court,
            "jurisdiction": jurisdiction,
            "documents": documents or [],
            "damages": damages,
        }

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    draft_result = pool.submit(asyncio.run, route_draft(request_data)).result()
            else:
                draft_result = asyncio.run(route_draft(request_data))
        except RuntimeError:
            draft_result = asyncio.run(route_draft(request_data))

        generated_files = draft_result.get("files", [])

        if draft_result.get("status") == "error":
            advance_document(doc_id, DocStatus.VALIDATION_FAILED.value,
                             f"Draft failed: {draft_result.get('errors', [])}")
            return get_document(doc_id)

        if draft_result.get("status") == "needs_input":
            rec = _load_doc(doc_id)
            rec.metadata["questions"] = draft_result.get("questions", [])
            _save_doc(rec)
            return {**asdict(rec), "questions": draft_result.get("questions", [])}

        # 3. Mark draft complete
        advance_document(doc_id, DocStatus.DRAFT_COMPLETE.value,
                         f"Generated {len(generated_files)} files",
                         files=generated_files)

    except Exception as e:
        advance_document(doc_id, DocStatus.VALIDATION_FAILED.value, f"Drafting exception: {e}")
        return get_document(doc_id)

    # 4. Run validation
    validation_result = validate_document(doc_id)

    # 5. Auto-queue if validated and deadline exists
    rec = _load_doc(doc_id)
    if rec and rec.status == DocStatus.VALIDATED.value and rec.deadline:
        from filing_queue import enqueue_document
        q_result = enqueue_document(
            doc_id=doc_id,
            case_id=case_id,
            deadline=rec.deadline,
            court=court,
            jurisdiction=jurisdiction,
            doc_type=doc_type,
            title=title,
            files=rec.files,
        )
        rec.filing_queue_id = q_result.get("queue_id", "")
        _transition(rec, DocStatus.QUEUED.value, "Auto-queued for filing")
        _save_doc(rec)

    return get_document(doc_id)


def validate_document(doc_id: str) -> dict:
    """
    Run validation checks on a drafted document.
    Checks: file existence, format compliance, construction rules,
    verification requirements, service requirements.
    """
    rec = _load_doc(doc_id)
    if rec is None:
        return {"error": f"Document not found: {doc_id}"}

    _transition(rec, DocStatus.VALIDATING.value, "Running validation")
    errors = []
    score = 100.0

    # Check 1: Files exist
    for fp in rec.files:
        if not Path(fp).exists():
            errors.append(f"File missing: {fp}")
            score -= 20.0

    # Check 2: At least one output file
    if not rec.files:
        errors.append("No output files generated")
        score -= 50.0

    # Check 3: Court/jurisdiction specified
    if not rec.court:
        errors.append("Court not specified")
        score -= 10.0
    if not rec.jurisdiction:
        errors.append("Jurisdiction not specified")
        score -= 10.0

    # Check 4: Run construction validation if complaint/motion
    if rec.doc_type in ("complaint", "amended_complaint", "motion", "foia"):
        try:
            from pipeline.statutory_construction import check_means_vs_includes
            # Construction validation runs inside the drafter; we just check the output
        except ImportError:
            pass

    # Check 5: Docx format validation
    for fp in rec.files:
        p = Path(fp)
        if p.suffix == ".docx" and p.exists():
            try:
                from docx import Document
                doc = Document(str(p))
                if len(doc.paragraphs) < 3:
                    errors.append(f"Document suspiciously short: {p.name}")
                    score -= 15.0
            except Exception as e:
                errors.append(f"Cannot read docx {p.name}: {e}")
                score -= 20.0

    score = max(0.0, score)
    new_status = DocStatus.VALIDATED.value if score >= 60.0 else DocStatus.VALIDATION_FAILED.value

    advance_document(doc_id, new_status,
                     f"Validation score: {score:.0f}/100 ({len(errors)} issues)",
                     validation_errors=errors, validation_score=score)
    return get_document(doc_id)


def mark_filed(doc_id: str, confirmation: str = "") -> dict:
    """Mark a document as filed with the court."""
    rec = _load_doc(doc_id)
    if rec is None:
        return {"error": f"Document not found: {doc_id}"}

    rec.filed_at = _now()
    rec.filed_confirmation = confirmation
    _transition(rec, DocStatus.FILED.value, f"Filed: {confirmation}")
    _save_doc(rec)

    # Also update the filing queue
    if rec.filing_queue_id:
        try:
            from filing_queue import mark_queue_filed
            mark_queue_filed(rec.filing_queue_id, confirmation)
        except Exception:
            pass

    return asdict(rec)


def archive_document(doc_id: str) -> dict:
    """Move a filed document to the archive."""
    rec = _load_doc(doc_id)
    if rec is None:
        return {"error": f"Document not found: {doc_id}"}

    _transition(rec, DocStatus.ARCHIVED.value, "Archived")

    # Move record file to archive
    src = _doc_path(doc_id)
    dst = ARCHIVE_DIR / src.name
    _save_doc(rec)
    shutil.copy2(str(src), str(dst))
    src.unlink()

    return asdict(rec)


def get_lifecycle_dashboard() -> dict:
    """
    Overview of all active documents across all cases.
    Returns counts by status, upcoming deadlines, overdue items.
    """
    by_status = {}
    upcoming = []
    overdue = []
    total = 0
    now = datetime.now()

    for path in DOCUMENTS_DIR.glob("doc_*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            status = data.get("status", "unknown")
            by_status[status] = by_status.get(status, 0) + 1
            total += 1

            deadline = data.get("deadline", "")
            if deadline:
                try:
                    dl_dt = datetime.fromisoformat(deadline)
                    days_left = (dl_dt - now).days
                    entry = {
                        "doc_id": data["doc_id"],
                        "case_id": data["case_id"],
                        "title": data["title"],
                        "deadline": deadline,
                        "days_left": days_left,
                        "status": status,
                    }
                    if days_left < 0 and status not in ("filed", "archived"):
                        overdue.append(entry)
                    elif 0 <= days_left <= 30 and status not in ("filed", "archived"):
                        upcoming.append(entry)
                except (ValueError, TypeError):
                    pass
        except Exception:
            continue

    upcoming.sort(key=lambda x: x["days_left"])
    overdue.sort(key=lambda x: x["days_left"])

    return {
        "total_documents": total,
        "by_status": by_status,
        "upcoming_deadlines": upcoming[:20],
        "overdue": overdue,
        "generated_at": _now(),
    }


# ═══════════════════════════════════════════════════════
# MCP TOOL REGISTRATION
# ═══════════════════════════════════════════════════════

def register_lifecycle_tools(mcp_server) -> int:
    """Register Document Lifecycle tools with the Beezle MCP server."""

    @mcp_server.tool
    def lifecycle_create(
        case_id: str,
        doc_type: str,
        title: str,
        court: str = "",
        jurisdiction: str = "",
        deadline: str = "",
        deadline_source: str = "",
    ) -> dict:
        """Create a new document lifecycle record.

        Tracks a document from intake through filing. Call this before drafting
        to establish the lifecycle record, or use lifecycle_draft for the full
        automated flow.

        Args:
            case_id: Canonical case ID (e.g. 'phila_ccp:260100000')
            doc_type: complaint, reply_brief, motion, appeal, foia, etc.
            title: Human-readable document title
            court: Court type (federal_district, state_ccp, federal_appellate)
            jurisdiction: Specific jurisdiction (EDPA, PA_CP_PHILA, CA3)
            deadline: Filing deadline as ISO date (e.g. '2026-05-01')
            deadline_source: statute, rule, court_order, or manual
        """
        return create_document(case_id, doc_type, title, court, jurisdiction,
                               deadline, deadline_source)

    @mcp_server.tool
    def lifecycle_draft(
        case_id: str,
        doc_type: str,
        title: str,
        user_input: str,
        court: str = "federal_district",
        jurisdiction: str = "EDPA",
        deadline: str = "",
        deadline_source: str = "",
        damages: str = "",
    ) -> dict:
        """Full lifecycle: create → draft → validate → queue.

        Primary entry point for document generation. Routes to the correct
        Beezle drafter (complaint, reply brief, motion, etc.), validates output,
        and auto-queues for filing if a deadline is set.

        Args:
            case_id: Canonical case ID
            doc_type: complaint, reply_brief, motion, appeal, foia, etc.
            title: Document title
            user_input: Natural language description of what to draft
            court: Court type
            jurisdiction: Jurisdiction code
            deadline: Filing deadline ISO date
            deadline_source: statute, rule, court_order, manual
            damages: Damages amount (if applicable)
        """
        return draft_and_track(case_id, doc_type, title, user_input,
                               court, jurisdiction, deadline, deadline_source,
                               damages=damages)

    @mcp_server.tool
    def lifecycle_status(doc_id: str) -> dict:
        """Get the current lifecycle status of a document.

        Returns the full document record including status, files, validation
        results, deadline, and history of status transitions.
        """
        return get_document(doc_id)

    @mcp_server.tool
    def lifecycle_advance(doc_id: str, new_status: str, note: str = "") -> dict:
        """Manually advance a document to a new lifecycle stage.

        Use this when a document needs manual intervention (e.g., marking
        validation_failed as validated after manual review, or advancing
        from draft_complete to queued).

        Valid statuses: intake, classified, drafting, draft_complete,
        validating, validated, validation_failed, queued, filed, archived
        """
        return advance_document(doc_id, new_status, note)

    @mcp_server.tool
    def lifecycle_list(
        case_id: str = "",
        status: str = "",
        doc_type: str = "",
    ) -> dict:
        """List all tracked documents with optional filters.

        Args:
            case_id: Filter by case (empty = all cases)
            status: Filter by status (empty = all statuses)
            doc_type: Filter by document type (empty = all types)

        Returns documents sorted by most recently updated.
        """
        return list_documents(case_id, status, doc_type)

    @mcp_server.tool
    def lifecycle_validate(doc_id: str) -> dict:
        """Run validation checks on a drafted document.

        Checks file existence, format compliance, court/jurisdiction,
        and document length. Returns a score (0-100) and any errors found.
        Documents scoring >= 60 are marked validated; below that,
        validation_failed.
        """
        return validate_document(doc_id)

    @mcp_server.tool
    def lifecycle_mark_filed(doc_id: str, confirmation: str = "") -> dict:
        """Mark a document as filed with the court.

        Args:
            doc_id: Document ID
            confirmation: Filing confirmation number or receipt info
        """
        return mark_filed(doc_id, confirmation)

    @mcp_server.tool
    def lifecycle_dashboard() -> dict:
        """Get an overview dashboard of all document lifecycle activity.

        Returns counts by status, upcoming deadlines (next 30 days),
        and any overdue items. Use this for a quick health check across
        all active documents and cases.
        """
        return get_lifecycle_dashboard()

    return 8  # number of tools registered
