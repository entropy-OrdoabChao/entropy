"""
filing_queue.py — Filing Queue Manager

Manages pending drafts with deadlines. Tracks what needs to be filed,
when, and in what order. Integrates with document_lifecycle.py and
deadline_tracker.py.

State: JSON files in <home>/lifecycle/queue/
Each queue entry tracks one document that needs filing.

Priority logic:
  1. Overdue items (negative days_left) — CRITICAL
  2. Due within 3 days — URGENT
  3. Due within 14 days — SOON
  4. Due within 30 days — NORMAL
  5. No deadline — LOW

Queue entries are never deleted; they transition through statuses:
  pending → ready → filed → confirmed
  pending → cancelled (if no longer needed)
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import json
import hashlib
import logging
from datetime import datetime, timedelta
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Optional

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

log = logging.getLogger("filing_queue")

# ─── Paths ───
BASE_PATH = _BEEZLE_ROOT
QUEUE_DIR = BASE_PATH / "lifecycle" / "queue"
QUEUE_DIR.mkdir(parents=True, exist_ok=True)


class QueueStatus(str, Enum):
    PENDING = "pending"       # draft in progress or awaiting review
    READY = "ready"           # reviewed, ready to file
    FILED = "filed"           # submitted to court
    CONFIRMED = "confirmed"   # filing confirmation received
    CANCELLED = "cancelled"   # no longer needed


class Priority(str, Enum):
    CRITICAL = "critical"     # overdue
    URGENT = "urgent"         # <= 3 days
    SOON = "soon"             # <= 14 days
    NORMAL = "normal"         # <= 30 days
    LOW = "low"               # no deadline or > 30 days


@dataclass
class QueueEntry:
    queue_id: str
    doc_id: str
    case_id: str
    doc_type: str
    title: str
    court: str
    jurisdiction: str
    deadline: str             # ISO date
    status: str = QueueStatus.PENDING.value
    priority: str = Priority.NORMAL.value
    days_left: int = 999
    files: list[str] = field(default_factory=list)
    filing_notes: str = ""
    filed_at: str = ""
    filed_by: str = ""
    confirmation: str = ""
    created_at: str = ""
    updated_at: str = ""
    service_required: bool = True
    service_completed: bool = False
    service_method: str = ""   # ecf, mail, hand_delivery
    checklist: list[dict] = field(default_factory=list)  # pre-filing checklist items
    history: list[dict] = field(default_factory=list)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _queue_path(queue_id: str) -> Path:
    return QUEUE_DIR / f"{queue_id}.json"


def _save_entry(entry: QueueEntry) -> None:
    entry.updated_at = _now()
    _queue_path(entry.queue_id).write_text(
        json.dumps(asdict(entry), indent=2, default=str), encoding="utf-8"
    )


def _load_entry(queue_id: str) -> Optional[QueueEntry]:
    path = _queue_path(queue_id)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return QueueEntry(**data)


def _compute_priority(deadline: str) -> tuple[str, int]:
    """Compute priority and days_left from deadline string."""
    if not deadline:
        return Priority.LOW.value, 999

    try:
        dl_dt = datetime.fromisoformat(deadline)
        # Treat as date only if no time component
        if dl_dt.hour == 0 and dl_dt.minute == 0:
            dl_dt = dl_dt.replace(hour=23, minute=59, second=59)
        days = (dl_dt - datetime.now()).days
    except (ValueError, TypeError):
        return Priority.LOW.value, 999

    if days < 0:
        return Priority.CRITICAL.value, days
    elif days <= 3:
        return Priority.URGENT.value, days
    elif days <= 14:
        return Priority.SOON.value, days
    elif days <= 30:
        return Priority.NORMAL.value, days
    else:
        return Priority.LOW.value, days


def _gen_queue_id(case_id: str, doc_type: str) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    h = hashlib.md5(f"q:{case_id}:{doc_type}:{ts}".encode()).hexdigest()[:6]
    return f"q_{ts}_{h}"


def _default_checklist(doc_type: str, court: str) -> list[dict]:
    """Generate a pre-filing checklist based on document type and court."""
    items = []

    # Universal items
    items.append({"item": "Proofread final document", "checked": False})
    items.append({"item": "Verify all citations are accurate", "checked": False})
    items.append({"item": "Check page numbering", "checked": False})

    # Court-specific
    if "federal" in court.lower() or court in ("EDPA", "CA3"):
        items.append({"item": "Verify ECF formatting requirements", "checked": False})
        items.append({"item": "Check certificate of service", "checked": False})
        items.append({"item": "Confirm filing fee paid or IFP granted", "checked": False})
    elif "state" in court.lower() or "ccp" in court.lower() or "PA_CP" in court:
        items.append({"item": "Verify PACFile formatting", "checked": False})
        items.append({"item": "Include verification if required", "checked": False})
        items.append({"item": "Check service by first-class mail", "checked": False})

    # Type-specific
    if doc_type in ("complaint", "amended_complaint", "foia"):
        items.append({"item": "Attach all exhibits", "checked": False})
        items.append({"item": "Include civil cover sheet", "checked": False})
        items.append({"item": "Summons prepared", "checked": False})
    elif doc_type == "motion":
        items.append({"item": "Include proposed order", "checked": False})
        items.append({"item": "Check briefing schedule compliance", "checked": False})
    elif doc_type in ("reply_brief", "memorandum"):
        items.append({"item": "Check page/word limit compliance", "checked": False})
        items.append({"item": "Table of authorities current", "checked": False})

    return items


# ═══════════════════════════════════════════════════════
# QUEUE OPERATIONS
# ═══════════════════════════════════════════════════════

def enqueue_document(
    doc_id: str,
    case_id: str,
    deadline: str,
    court: str = "",
    jurisdiction: str = "",
    doc_type: str = "",
    title: str = "",
    files: list[str] = None,
    filing_notes: str = "",
) -> dict:
    """Add a document to the filing queue."""
    queue_id = _gen_queue_id(case_id, doc_type)
    priority, days_left = _compute_priority(deadline)

    entry = QueueEntry(
        queue_id=queue_id,
        doc_id=doc_id,
        case_id=case_id,
        doc_type=doc_type,
        title=title,
        court=court,
        jurisdiction=jurisdiction,
        deadline=deadline,
        priority=priority,
        days_left=days_left,
        files=files or [],
        filing_notes=filing_notes,
        created_at=_now(),
        checklist=_default_checklist(doc_type, court),
    )
    entry.history.append({"status": "pending", "at": _now(), "note": "Enqueued"})
    _save_entry(entry)
    log.info(f"Enqueued {queue_id} (case={case_id}, type={doc_type}, priority={priority}, days_left={days_left})")
    return asdict(entry)


def get_queue_entry(queue_id: str) -> dict:
    """Get a single queue entry by ID."""
    entry = _load_entry(queue_id)
    if entry is None:
        return {"error": f"Queue entry not found: {queue_id}"}
    # Refresh priority
    entry.priority, entry.days_left = _compute_priority(entry.deadline)
    _save_entry(entry)
    return asdict(entry)


def list_queue(
    status: str = "",
    case_id: str = "",
    priority: str = "",
    include_completed: bool = False,
) -> dict:
    """List filing queue entries with optional filters. Sorted by urgency."""
    results = []
    for path in QUEUE_DIR.glob("q_*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))

            # Refresh priority on read
            p, dl = _compute_priority(data.get("deadline", ""))
            data["priority"] = p
            data["days_left"] = dl

            # Filters
            if status and data.get("status") != status:
                continue
            if case_id and data.get("case_id") != case_id:
                continue
            if priority and data.get("priority") != priority:
                continue
            if not include_completed and data.get("status") in ("filed", "confirmed", "cancelled"):
                continue

            results.append(data)
        except Exception:
            continue

    # Sort by priority order, then days_left
    priority_order = {"critical": 0, "urgent": 1, "soon": 2, "normal": 3, "low": 4}
    results.sort(key=lambda x: (priority_order.get(x.get("priority", "low"), 5), x.get("days_left", 999)))

    return {"count": len(results), "queue": results}


def mark_ready(queue_id: str, notes: str = "") -> dict:
    """Mark a queued document as ready for filing."""
    entry = _load_entry(queue_id)
    if entry is None:
        return {"error": f"Queue entry not found: {queue_id}"}
    entry.status = QueueStatus.READY.value
    entry.filing_notes = notes or entry.filing_notes
    entry.history.append({"status": "ready", "at": _now(), "note": notes or "Marked ready"})
    _save_entry(entry)
    return asdict(entry)


def mark_queue_filed(queue_id: str, confirmation: str = "", filed_by: str = "user") -> dict:
    """Mark a queued document as filed."""
    entry = _load_entry(queue_id)
    if entry is None:
        return {"error": f"Queue entry not found: {queue_id}"}
    entry.status = QueueStatus.FILED.value
    entry.filed_at = _now()
    entry.filed_by = filed_by
    entry.confirmation = confirmation
    entry.history.append({"status": "filed", "at": _now(), "note": f"Filed: {confirmation}"})
    _save_entry(entry)
    return asdict(entry)


def update_checklist(queue_id: str, item_index: int, checked: bool) -> dict:
    """Toggle a checklist item for a queue entry."""
    entry = _load_entry(queue_id)
    if entry is None:
        return {"error": f"Queue entry not found: {queue_id}"}
    if item_index < 0 or item_index >= len(entry.checklist):
        return {"error": f"Invalid checklist index: {item_index}"}
    entry.checklist[item_index]["checked"] = checked
    _save_entry(entry)
    return asdict(entry)


def mark_service_complete(queue_id: str, method: str = "ecf") -> dict:
    """Mark service as completed for a queue entry."""
    entry = _load_entry(queue_id)
    if entry is None:
        return {"error": f"Queue entry not found: {queue_id}"}
    entry.service_completed = True
    entry.service_method = method
    entry.history.append({"status": "service", "at": _now(), "note": f"Service completed via {method}"})
    _save_entry(entry)
    return asdict(entry)


def cancel_queue_entry(queue_id: str, reason: str = "") -> dict:
    """Cancel a queue entry."""
    entry = _load_entry(queue_id)
    if entry is None:
        return {"error": f"Queue entry not found: {queue_id}"}
    entry.status = QueueStatus.CANCELLED.value
    entry.history.append({"status": "cancelled", "at": _now(), "note": reason or "Cancelled"})
    _save_entry(entry)
    return asdict(entry)


def get_queue_summary() -> dict:
    """Get a summary of the filing queue."""
    all_entries = list_queue(include_completed=True)
    entries = all_entries.get("queue", [])

    by_status = {}
    by_priority = {}
    overdue = []
    due_this_week = []

    for e in entries:
        s = e.get("status", "unknown")
        p = e.get("priority", "low")
        by_status[s] = by_status.get(s, 0) + 1
        by_priority[p] = by_priority.get(p, 0) + 1

        dl = e.get("days_left", 999)
        if dl < 0 and s not in ("filed", "confirmed", "cancelled"):
            overdue.append(e)
        elif 0 <= dl <= 7 and s not in ("filed", "confirmed", "cancelled"):
            due_this_week.append(e)

    return {
        "total": len(entries),
        "by_status": by_status,
        "by_priority": by_priority,
        "overdue_count": len(overdue),
        "overdue": overdue[:10],
        "due_this_week": due_this_week[:10],
        "generated_at": _now(),
    }


# ═══════════════════════════════════════════════════════
# MCP TOOL REGISTRATION
# ═══════════════════════════════════════════════════════


def dequeue_entry(queue_id: str, confirm: bool = False, reason: str = "manual_removal") -> dict:
    """Remove a filing queue entry by queue_id.

    Use for test artifacts, wrong enqueues, or filings that were dropped before
    being filed. Requires confirm=True to actually delete (safety gate).

    Returns:
        {ok: bool, queue_id, removed: bool, message, preview/audit_note}
    """
    path = _queue_path(queue_id)

    if not path.exists():
        return {
            "ok": False,
            "queue_id": queue_id,
            "removed": False,
            "message": f"queue_id not found in {QUEUE_DIR}",
            "path_tried": str(path),
        }

    # Load entry for preview / audit
    try:
        entry_data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return {
            "ok": False,
            "queue_id": queue_id,
            "removed": False,
            "message": f"failed to read queue file: {e}",
        }

    if not confirm:
        return {
            "ok": False,
            "queue_id": queue_id,
            "removed": False,
            "message": "confirmation required",
            "preview": {
                "title": entry_data.get("title"),
                "doc_id": entry_data.get("doc_id"),
                "case_id": entry_data.get("case_id"),
                "status": entry_data.get("status"),
                "files": entry_data.get("files", []),
                "deadline": entry_data.get("deadline"),
            },
            "next_action": "Call filing_dequeue again with confirm=True to delete this entry.",
        }

    # Do the delete
    try:
        path.unlink()
    except Exception as e:
        return {
            "ok": False,
            "queue_id": queue_id,
            "removed": False,
            "message": f"failed to delete queue file: {e}",
        }

    return {
        "ok": True,
        "queue_id": queue_id,
        "removed": True,
        "message": f"deleted queue entry {queue_id}",
        "audit_note": {
            "removed_at": _now(),
            "reason": reason,
            "entry_snapshot": {
                "doc_id": entry_data.get("doc_id"),
                "case_id": entry_data.get("case_id"),
                "title": entry_data.get("title"),
                "status": entry_data.get("status"),
            },
        },
    }

def register_filing_queue_tools(mcp_server) -> int:
    """Register Filing Queue tools with the Beezle MCP server."""

    @mcp_server.tool
    def filing_enqueue(
        doc_id: str,
        case_id: str,
        deadline: str,
        court: str = "",
        jurisdiction: str = "",
        doc_type: str = "",
        title: str = "",
        filing_notes: str = "",
    ) -> dict:
        """Add a document to the filing queue with a deadline.

        Creates a queue entry with auto-computed priority based on deadline
        proximity and generates a pre-filing checklist appropriate to the
        court and document type.

        Args:
            doc_id: Document lifecycle ID from lifecycle_create
            case_id: Canonical case ID
            deadline: Filing deadline as ISO date (e.g. '2026-05-15')
            court: Court type (federal_district, state_ccp)
            jurisdiction: Jurisdiction code (EDPA, PA_CP_PHILA)
            doc_type: complaint, motion, reply_brief, etc.
            title: Document title
            filing_notes: Any notes about filing requirements
        """
        return enqueue_document(doc_id, case_id, deadline, court, jurisdiction,
                                doc_type, title, filing_notes=filing_notes)

    @mcp_server.tool
    def filing_queue_list(
        status: str = "",
        case_id: str = "",
        priority: str = "",
    ) -> dict:
        """List pending filings sorted by urgency.

        Returns queue entries with auto-refreshed priority based on current
        date vs deadline. Critical items (overdue) appear first.

        Args:
            status: Filter by status (pending, ready, filed, confirmed, cancelled)
            case_id: Filter by case ID
            priority: Filter by priority (critical, urgent, soon, normal, low)
        """
        return list_queue(status, case_id, priority)

    @mcp_server.tool
    def filing_mark_ready(queue_id: str, notes: str = "") -> dict:
        """Mark a queued filing as ready to submit.

        Use after completing the pre-filing checklist review. The document
        moves from 'pending' to 'ready' status.
        """
        return mark_ready(queue_id, notes)

    @mcp_server.tool
    def filing_mark_filed(queue_id: str, confirmation: str = "") -> dict:
        """Mark a filing as submitted to the court.

        Args:
            queue_id: Queue entry ID
            confirmation: Filing confirmation number or receipt
        """
        return mark_queue_filed(queue_id, confirmation)

    @mcp_server.tool
    def filing_checklist(queue_id: str) -> dict:
        """Get the pre-filing checklist for a queued document.

        Returns the full queue entry including the checklist with checked/
        unchecked items, filing notes, and service requirements.
        """
        return get_queue_entry(queue_id)

    @mcp_server.tool
    def filing_queue_summary() -> dict:
        """Get a summary of the entire filing queue.

        Returns counts by status and priority, overdue items,
        and items due this week. Use this for a daily filing status check.
        """
        return get_queue_summary()

    @mcp_server.tool
    def filing_dequeue(queue_id: str, confirm: bool = False, reason: str = "manual_removal") -> dict:
        """Remove a filing queue entry by queue_id.

        Use for test artifacts or wrong enqueues. Requires confirm=True to actually delete.

        Args:
            queue_id: Queue entry ID (e.g. q_20260422_173438_89f029)
            confirm: Must be True to delete. Default False returns preview only.
            reason: Audit note for the removal.
        """
        return dequeue_entry(queue_id, confirm=confirm, reason=reason)


    return 7  # number of tools registered
