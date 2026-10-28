"""
deadline_tracker.py — Statute of Limitations & Deadline Computation

Deterministic deadline calculator for legal filings.
Sources deadline rules from:
  1. Federal Rules of Civil Procedure (FRCP)
  2. Pennsylvania Rules of Civil Procedure (Pa.R.C.P.)
  3. Federal Rules of Appellate Procedure (FRAP)
  4. Specific statutes (FOIA, FCRA, FDCPA, etc.)
  5. Court orders (manually entered)
  6. ChromaDB legal corpus (fallback lookup)

All computation is deterministic. No LLM calls.

Handles:
  - Statute of limitations (from accrual/discovery date)
  - Response deadlines (from service date)
  - Appeal deadlines (from order/judgment date)
  - Discovery deadlines (from case management order)
  - Tolling (equitable tolling, minor tolling, disability)
  - Weekday/holiday adjustment (FRCP Rule 6, Pa.R.C.P. 106)

State: JSON files in <home>/lifecycle/deadlines/
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import json
import hashlib
import logging
from datetime import datetime, date, timedelta
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

log = logging.getLogger("deadline_tracker")

# ─── Paths ───
BASE_PATH = _eroot()
DEADLINES_DIR = BASE_PATH / "lifecycle" / "deadlines"
DEADLINES_DIR.mkdir(parents=True, exist_ok=True)


# ═══════════════════════════════════════════════════════
# DEADLINE RULES DATABASE
# ═══════════════════════════════════════════════════════

# Format: (days, calendar_or_court, source_rule, description)
# "calendar" = calendar days, "court" = court/business days

RESPONSE_DEADLINES: dict[str, dict[str, tuple[int, str, str, str]]] = {
    # Federal rules
    "federal": {
        "answer_complaint":         (21, "calendar", "FRCP 12(a)(1)(A)(i)", "Answer to complaint"),
        "answer_complaint_us_gov":  (60, "calendar", "FRCP 12(a)(2)", "Answer when US is defendant"),
        "answer_complaint_waiver":  (60, "calendar", "FRCP 12(a)(1)(A)(ii)", "Answer after waiver of service"),
        "motion_to_dismiss":        (21, "calendar", "FRCP 12(a)(1)(A)(i)", "Motion to dismiss"),
        "reply_to_motion":          (14, "calendar", "FRCP 6(c)(1)", "Reply to motion (after response)"),
        "response_to_motion":       (21, "calendar", "Local Rule 7.1(c)", "Response/opposition to motion"),
        "appeal_final_judgment":    (30, "calendar", "FRAP 4(a)(1)(A)", "Notice of appeal from final judgment"),
        "appeal_us_party":          (60, "calendar", "FRAP 4(a)(1)(B)", "Notice of appeal when US is party"),
        "cross_appeal":             (14, "calendar", "FRAP 4(a)(3)", "Cross-appeal after notice of appeal"),
        "appellate_brief":          (40, "calendar", "FRAP 31(a)(1)", "Opening appellate brief"),
        "appellee_brief":           (30, "calendar", "FRAP 31(a)(1)", "Appellee's response brief"),
        "reply_brief_appeal":       (21, "calendar", "FRAP 31(a)(1)", "Reply brief on appeal"),
        "cert_petition":            (90, "calendar", "Sup. Ct. R. 13.1", "Petition for certiorari"),
        "rule_59_motion":           (28, "calendar", "FRCP 59(b)", "Motion for new trial"),
        "rule_60_motion":           (365, "calendar", "FRCP 60(c)(1)", "Motion for relief from judgment"),
        "discovery_response":       (30, "calendar", "FRCP 33(b)(2), 34(b)(2)", "Response to interrogatories/RFP"),
        "rfa_response":             (30, "calendar", "FRCP 36(a)(3)", "Response to requests for admission"),
        "summary_judgment":         (21, "calendar", "FRCP 56(b)", "Opposition to summary judgment"),
        "preliminary_objections":   (14, "calendar", "FRCP 12(f)", "Response to preliminary objections"),
    },
    # Pennsylvania state rules
    "pa_state": {
        "answer_complaint":         (20, "calendar", "Pa.R.C.P. 1026(a)", "Answer to complaint"),
        "preliminary_objections":   (20, "calendar", "Pa.R.C.P. 1028(c)(2)", "Preliminary objections"),
        "answer_po":                (20, "calendar", "Pa.R.C.P. 1028(c)(2)", "Response to preliminary objections"),
        "motion_response":          (20, "calendar", "Pa.R.C.P. 208.3(b)", "Response to motion"),
        "appeal_ccp_to_superior":   (30, "calendar", "Pa.R.A.P. 903(a)", "Appeal from CCP to Superior Court"),
        "appeal_superior_to_supreme": (30, "calendar", "Pa.R.A.P. 1113(a)", "Petition for allowance of appeal"),
        "discovery_response":       (30, "calendar", "Pa.R.C.P. 4006(a)(2)", "Response to interrogatories"),
        "new_trial_motion":         (10, "calendar", "Pa.R.C.P. 227.1(c)(2)", "Post-trial motions"),
        "reconsideration":          (30, "calendar", "42 Pa.C.S. 5505", "Petition to open/modify judgment"),
        "praecipe_default":         (10, "calendar", "Pa.R.C.P. 237.1(a)(2)", "After 10-day notice, enter default"),
        "garnishment_answer":       (30, "calendar", "Pa.R.C.P. 3146(a)", "Garnishee answer"),
    },
}

STATUTE_OF_LIMITATIONS: dict[str, dict[str, tuple[int, str, str, str]]] = {
    "federal": {
        "foia":                 (20, "business", "5 U.S.C. 552(a)(6)(A)(i)", "FOIA agency response deadline"),
        "foia_appeal":          (20, "business", "5 U.S.C. 552(a)(6)(A)(ii)", "FOIA administrative appeal"),
        "foia_lawsuit":         (730, "calendar", "5 U.S.C. 552(a)(4)(B)", "FOIA federal suit (no fixed SOL, but 6yr 28 USC 2401)"),
        "fcra":                 (730, "calendar", "15 U.S.C. 1681p", "FCRA claim (2 years from discovery)"),
        "fdcpa":                (365, "calendar", "15 U.S.C. 1692k(d)", "FDCPA claim (1 year from violation)"),
        "tila":                 (365, "calendar", "15 U.S.C. 1640(e)", "TILA claim (1 year)"),
        "respa":                (365, "calendar", "12 U.S.C. 2614", "RESPA section 8 (1 year)"),
        "respa_escrow":         (1095, "calendar", "12 U.S.C. 2614", "RESPA escrow (3 years)"),
        "section_1983":         (730, "calendar", "42 U.S.C. 1983 + state SOL", "Section 1983 (borrow state personal injury SOL)"),
        "bivens":               (730, "calendar", "Bivens (borrow state SOL)", "Bivens action"),
        "ftca":                 (730, "calendar", "28 U.S.C. 2401(b)", "Federal Tort Claims Act (2 years)"),
        "ftca_admin":           (180, "calendar", "28 U.S.C. 2675(a)", "FTCA admin claim (must exhaust first)"),
        "title_vii":            (300, "calendar", "42 U.S.C. 2000e-5(e)", "Title VII (300 days to EEOC)"),
        "ada":                  (300, "calendar", "42 U.S.C. 12117(a)", "ADA employment (300 days to EEOC)"),
        "antitrust":            (1460, "calendar", "15 U.S.C. 15b", "Clayton Act (4 years)"),
        "securities_fraud":     (730, "calendar", "28 U.S.C. 1658(b)", "Securities fraud (2yr discovery, 5yr repose)"),
        "rico":                 (1460, "calendar", "Agency Holding Corp. v. Malley-Duff", "Civil RICO (4 years)"),
    },
    "pa_state": {
        "personal_injury":      (730, "calendar", "42 Pa.C.S. 5524(2)", "Personal injury (2 years)"),
        "breach_contract":      (1460, "calendar", "42 Pa.C.S. 5525(a)(1)", "Contract (4 years)"),
        "fraud":                (730, "calendar", "42 Pa.C.S. 5524(7)", "Fraud (2 years from discovery)"),
        "product_liability":    (730, "calendar", "42 Pa.C.S. 5524(2)", "Product liability (2 years)"),
        "defamation":           (365, "calendar", "42 Pa.C.S. 5523(1)", "Defamation (1 year)"),
        "property_damage":      (730, "calendar", "42 Pa.C.S. 5524(3)", "Property damage (2 years)"),
        "med_mal":              (730, "calendar", "42 Pa.C.S. 5524(2)", "Medical malpractice (2 years)"),
        "utpcpl":               (2190, "calendar", "73 P.S. 201-9.2", "UTPCPL (6 years)"),
        "wage_claim":           (1095, "calendar", "43 P.S. 260.9a(c)", "Wage Payment Act (3 years)"),
        "bad_faith_insurance":  (730, "calendar", "42 Pa.C.S. 8371 + 5524", "Bad faith insurance (2 years)"),
        "wrongful_death":       (730, "calendar", "42 Pa.C.S. 5524(2)", "Wrongful death (2 years)"),
        "quiet_title":          (7300, "calendar", "42 Pa.C.S. 5530(a)(3)", "Quiet title / adverse possession (21 years)"),
    },
}

# Federal holidays (fixed + computed for 2026)
FEDERAL_HOLIDAYS_2026 = {
    date(2026, 1, 1),    # New Year
    date(2026, 1, 19),   # MLK
    date(2026, 2, 16),   # Presidents Day
    date(2026, 5, 25),   # Memorial Day
    date(2026, 6, 19),   # Juneteenth
    date(2026, 7, 3),    # Independence Day observed (July 4 is Saturday)
    date(2026, 9, 7),    # Labor Day
    date(2026, 10, 12),  # Columbus Day
    date(2026, 11, 11),  # Veterans Day
    date(2026, 11, 26),  # Thanksgiving
    date(2026, 12, 25),  # Christmas
}


# ═══════════════════════════════════════════════════════
# DEADLINE COMPUTATION
# ═══════════════════════════════════════════════════════

def _is_court_day(d: date) -> bool:
    """Check if a date is a court business day (not weekend, not federal holiday)."""
    if d.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    if d in FEDERAL_HOLIDAYS_2026:
        return False
    return True


def _add_calendar_days(start: date, days: int) -> date:
    """Add calendar days, then if result falls on weekend/holiday, push to next court day."""
    result = start + timedelta(days=days)
    # FRCP Rule 6(a)(1)(C): if last day is weekend/holiday, push to next business day
    while not _is_court_day(result):
        result += timedelta(days=1)
    return result


def _add_business_days(start: date, days: int) -> date:
    """Add business/court days (skip weekends and federal holidays)."""
    current = start
    added = 0
    while added < days:
        current += timedelta(days=1)
        if _is_court_day(current):
            added += 1
    return current


def compute_deadline(
    start_date: str,
    rule_key: str,
    jurisdiction: str = "federal",
    tolling_days: int = 0,
) -> dict:
    """
    Compute a filing/response deadline from a start date and rule.

    Args:
        start_date: ISO date string (date of service, accrual, order, etc.)
        rule_key: Key from RESPONSE_DEADLINES or STATUTE_OF_LIMITATIONS
        jurisdiction: 'federal' or 'pa_state'
        tolling_days: Additional days for equitable tolling, disability, etc.

    Returns:
        dict with deadline date, rule citation, computation breakdown
    """
    try:
        start = date.fromisoformat(start_date)
    except (ValueError, TypeError):
        return {"error": f"Invalid start_date: {start_date}"}

    # Look up rule
    rule = None
    rule_source = ""

    response_rules = RESPONSE_DEADLINES.get(jurisdiction, {})
    sol_rules = STATUTE_OF_LIMITATIONS.get(jurisdiction, {})

    if rule_key in response_rules:
        rule = response_rules[rule_key]
        rule_source = "response_deadline"
    elif rule_key in sol_rules:
        rule = sol_rules[rule_key]
        rule_source = "statute_of_limitations"
    else:
        # Try the other jurisdiction
        alt = "pa_state" if jurisdiction == "federal" else "federal"
        alt_response = RESPONSE_DEADLINES.get(alt, {})
        alt_sol = STATUTE_OF_LIMITATIONS.get(alt, {})
        if rule_key in alt_response:
            rule = alt_response[rule_key]
            rule_source = "response_deadline"
            jurisdiction = alt
        elif rule_key in alt_sol:
            rule = alt_sol[rule_key]
            rule_source = "statute_of_limitations"
            jurisdiction = alt

    if rule is None:
        return {
            "error": f"Unknown rule_key: {rule_key}",
            "available_keys": {
                "response_deadlines": list(response_rules.keys()),
                "statutes_of_limitations": list(sol_rules.keys()),
            },
        }

    days, day_type, citation, description = rule
    total_days = days + tolling_days

    if day_type == "business":
        deadline_date = _add_business_days(start, total_days)
    else:
        deadline_date = _add_calendar_days(start, total_days)

    days_from_now = (deadline_date - date.today()).days

    return {
        "start_date": start_date,
        "rule_key": rule_key,
        "jurisdiction": jurisdiction,
        "rule_source": rule_source,
        "citation": citation,
        "description": description,
        "base_days": days,
        "day_type": day_type,
        "tolling_days": tolling_days,
        "total_days": total_days,
        "deadline": deadline_date.isoformat(),
        "days_from_now": days_from_now,
        "is_overdue": days_from_now < 0,
        "is_urgent": 0 <= days_from_now <= 7,
        "weekend_adjusted": deadline_date.weekday() != (start + timedelta(days=total_days)).weekday(),
    }


def compute_multiple_deadlines(
    case_id: str,
    start_date: str,
    jurisdiction: str = "federal",
    rule_keys: list[str] = None,
    tolling_days: int = 0,
) -> dict:
    """
    Compute all relevant deadlines for a case from a single trigger date.

    If rule_keys is empty, computes ALL deadlines for the jurisdiction.
    Results are sorted by deadline date (earliest first).
    """
    rules = RESPONSE_DEADLINES.get(jurisdiction, {})
    sol = STATUTE_OF_LIMITATIONS.get(jurisdiction, {})

    keys = rule_keys or list(rules.keys()) + list(sol.keys())
    results = []

    for key in keys:
        r = compute_deadline(start_date, key, jurisdiction, tolling_days)
        if "error" not in r:
            results.append(r)

    results.sort(key=lambda x: x.get("deadline", "9999"))

    return {
        "case_id": case_id,
        "start_date": start_date,
        "jurisdiction": jurisdiction,
        "tolling_days": tolling_days,
        "deadlines": results,
        "count": len(results),
        "next_deadline": results[0] if results else None,
    }


def list_available_rules(jurisdiction: str = "") -> dict:
    """List all available deadline rules."""
    result = {}
    jurisdictions = [jurisdiction] if jurisdiction else ["federal", "pa_state"]

    for j in jurisdictions:
        rules = {}
        for key, (days, dtype, citation, desc) in RESPONSE_DEADLINES.get(j, {}).items():
            rules[key] = {"days": days, "type": dtype, "citation": citation, "description": desc, "category": "response"}
        for key, (days, dtype, citation, desc) in STATUTE_OF_LIMITATIONS.get(j, {}).items():
            rules[key] = {"days": days, "type": dtype, "citation": citation, "description": desc, "category": "sol"}
        result[j] = rules

    return result


# ═══════════════════════════════════════════════════════
# DEADLINE TRACKING (persistent)
# ═══════════════════════════════════════════════════════

@dataclass
class TrackedDeadline:
    deadline_id: str
    case_id: str
    rule_key: str
    citation: str
    description: str
    start_date: str
    deadline: str
    jurisdiction: str
    status: str = "active"       # active, met, missed, extended, waived
    days_from_now: int = 0
    doc_id: str = ""             # linked lifecycle document
    queue_id: str = ""           # linked filing queue entry
    notes: str = ""
    created_at: str = ""
    updated_at: str = ""
    history: list[dict] = field(default_factory=list)


def _dl_path(deadline_id: str) -> Path:
    return DEADLINES_DIR / f"{deadline_id}.json"


def _save_deadline(dl: TrackedDeadline) -> None:
    dl.updated_at = datetime.now().isoformat(timespec="seconds")
    dl.days_from_now = (date.fromisoformat(dl.deadline) - date.today()).days
    _dl_path(dl.deadline_id).write_text(
        json.dumps(asdict(dl), indent=2, default=str), encoding="utf-8"
    )


def _load_deadline(deadline_id: str) -> Optional[TrackedDeadline]:
    path = _dl_path(deadline_id)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return TrackedDeadline(**data)


def track_deadline(
    case_id: str,
    rule_key: str,
    start_date: str,
    jurisdiction: str = "federal",
    tolling_days: int = 0,
    notes: str = "",
    doc_id: str = "",
) -> dict:
    """Compute a deadline and begin tracking it."""
    computed = compute_deadline(start_date, rule_key, jurisdiction, tolling_days)
    if "error" in computed:
        return computed

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    h = hashlib.md5(f"dl:{case_id}:{rule_key}:{ts}".encode()).hexdigest()[:6]
    deadline_id = f"dl_{ts}_{h}"

    dl = TrackedDeadline(
        deadline_id=deadline_id,
        case_id=case_id,
        rule_key=rule_key,
        citation=computed["citation"],
        description=computed["description"],
        start_date=start_date,
        deadline=computed["deadline"],
        jurisdiction=jurisdiction,
        days_from_now=computed["days_from_now"],
        doc_id=doc_id,
        notes=notes,
        created_at=datetime.now().isoformat(timespec="seconds"),
    )
    dl.history.append({"action": "created", "at": dl.created_at, "note": f"Computed from {rule_key}"})
    _save_deadline(dl)

    return {**asdict(dl), "computation": computed}


def extend_deadline(deadline_id: str, new_deadline: str, reason: str = "") -> dict:
    """Extend a tracked deadline (e.g., by stipulation or court order)."""
    dl = _load_deadline(deadline_id)
    if dl is None:
        return {"error": f"Deadline not found: {deadline_id}"}

    old = dl.deadline
    dl.deadline = new_deadline
    dl.status = "active"
    dl.history.append({
        "action": "extended",
        "at": datetime.now().isoformat(timespec="seconds"),
        "note": f"Extended from {old} to {new_deadline}: {reason}",
    })
    _save_deadline(dl)
    return asdict(dl)


def mark_deadline_met(deadline_id: str, doc_id: str = "", note: str = "") -> dict:
    """Mark a deadline as met (filing completed)."""
    dl = _load_deadline(deadline_id)
    if dl is None:
        return {"error": f"Deadline not found: {deadline_id}"}

    dl.status = "met"
    if doc_id:
        dl.doc_id = doc_id
    dl.history.append({
        "action": "met",
        "at": datetime.now().isoformat(timespec="seconds"),
        "note": note or "Deadline met",
    })
    _save_deadline(dl)
    return asdict(dl)


def waive_deadline(deadline_id: str, reason: str = "") -> dict:
    """Mark a deadline as waived."""
    dl = _load_deadline(deadline_id)
    if dl is None:
        return {"error": f"Deadline not found: {deadline_id}"}

    dl.status = "waived"
    dl.history.append({
        "action": "waived",
        "at": datetime.now().isoformat(timespec="seconds"),
        "note": reason or "Waived",
    })
    _save_deadline(dl)
    return asdict(dl)


def list_tracked_deadlines(
    case_id: str = "",
    status: str = "active",
    days_ahead: int = 90,
) -> dict:
    """List tracked deadlines with optional filters."""
    results = []
    now = date.today()

    for path in DEADLINES_DIR.glob("dl_*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))

            # Refresh days_from_now
            try:
                dl_date = date.fromisoformat(data.get("deadline", ""))
                data["days_from_now"] = (dl_date - now).days
            except (ValueError, TypeError):
                data["days_from_now"] = 999

            # Auto-mark missed deadlines
            if data.get("status") == "active" and data["days_from_now"] < 0:
                data["status"] = "missed"
                # Update on disk
                path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

            # Filters
            if case_id and data.get("case_id") != case_id:
                continue
            if status and data.get("status") != status:
                continue
            if status == "active" and data["days_from_now"] > days_ahead:
                continue

            results.append(data)
        except Exception:
            continue

    results.sort(key=lambda x: x.get("days_from_now", 999))
    return {"count": len(results), "deadlines": results}


def get_deadline_summary(case_id: str = "") -> dict:
    """Summary of all tracked deadlines across cases."""
    all_dl = list_tracked_deadlines(case_id=case_id, status="", days_ahead=9999)
    entries = all_dl.get("deadlines", [])

    by_status = {}
    overdue = []
    next_7 = []
    next_30 = []

    for e in entries:
        s = e.get("status", "unknown")
        by_status[s] = by_status.get(s, 0) + 1

        dfn = e.get("days_from_now", 999)
        if s == "active" or s == "missed":
            if dfn < 0:
                overdue.append(e)
            elif dfn <= 7:
                next_7.append(e)
            elif dfn <= 30:
                next_30.append(e)

    return {
        "total": len(entries),
        "by_status": by_status,
        "overdue": overdue,
        "due_next_7_days": next_7,
        "due_next_30_days": next_30,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


# ═══════════════════════════════════════════════════════
# CHROMADB FALLBACK LOOKUP
# ═══════════════════════════════════════════════════════

def lookup_deadline_from_db(query: str, jurisdiction: str = "PA") -> dict:
    """
    Fallback: query ChromaDB for deadline/SOL info when a rule_key isn't
    in the hardcoded tables. Returns raw DB hits for Hermes to interpret.
    """
    try:
        from pipeline.db_connector import LegalDB
        db = LegalDB()
        hits = db.query_text(
            f"{query} deadline statute of limitations time limit days {jurisdiction}",
            n_results=5,
        )
        results = []
        for h in hits:
            results.append({
                "source": h.get("metadata", {}).get("source", "unknown"),
                "text": h.get("document", "")[:400],
                "distance": h.get("distance", 0),
            })
        return {
            "query": query,
            "jurisdiction": jurisdiction,
            "results": results,
            "note": "These are raw DB hits. Verify the specific deadline rule before relying on it.",
        }
    except Exception as e:
        return {"error": f"DB lookup failed: {e}"}


# ═══════════════════════════════════════════════════════
# MCP TOOL REGISTRATION
# ═══════════════════════════════════════════════════════

def register_deadline_tools(mcp_server) -> int:
    """Register Deadline Tracker tools with the Beezle MCP server."""

    @mcp_server.tool
    def deadline_compute(
        start_date: str,
        rule_key: str,
        jurisdiction: str = "federal",
        tolling_days: int = 0,
    ) -> dict:
        """Compute a legal deadline from a start date and rule.

        Deterministic computation using FRCP, Pa.R.C.P., FRAP, and statutory
        rules. Handles weekend/holiday adjustment per Rule 6.

        Args:
            start_date: ISO date (e.g. '2026-04-15') — date of service,
                        accrual, order, etc.
            rule_key: Deadline rule (e.g. 'answer_complaint', 'appeal_final_judgment',
                      'fcra', 'fdcpa'). Call deadline_rules to see all available keys.
            jurisdiction: 'federal' or 'pa_state'
            tolling_days: Extra days for equitable tolling or extensions
        """
        return compute_deadline(start_date, rule_key, jurisdiction, tolling_days)

    @mcp_server.tool
    def deadline_compute_all(
        case_id: str,
        start_date: str,
        jurisdiction: str = "federal",
        tolling_days: int = 0,
    ) -> dict:
        """Compute ALL relevant deadlines for a case from a trigger date.

        Returns every applicable deadline sorted by date (earliest first).
        Useful for building a complete case calendar from the date of service
        or date of the triggering event.

        Args:
            case_id: Case identifier for tracking
            start_date: ISO date of the triggering event
            jurisdiction: 'federal' or 'pa_state'
            tolling_days: Additional tolling days
        """
        return compute_multiple_deadlines(case_id, start_date, jurisdiction,
                                          tolling_days=tolling_days)

    @mcp_server.tool
    def deadline_rules(jurisdiction: str = "") -> dict:
        """List all available deadline rules and statutes of limitations.

        Returns every rule key with its days, citation, and description.
        Use this to find the correct rule_key for deadline_compute.

        Args:
            jurisdiction: 'federal', 'pa_state', or empty for both
        """
        return list_available_rules(jurisdiction)

    @mcp_server.tool
    def deadline_track(
        case_id: str,
        rule_key: str,
        start_date: str,
        jurisdiction: str = "federal",
        tolling_days: int = 0,
        notes: str = "",
    ) -> dict:
        """Compute and persistently track a deadline.

        Creates a tracked deadline record that auto-updates status (active →
        missed if overdue). Links to document lifecycle and filing queue.

        Args:
            case_id: Case identifier
            rule_key: Deadline rule key
            start_date: ISO date of triggering event
            jurisdiction: 'federal' or 'pa_state'
            tolling_days: Additional tolling days
            notes: Any context or notes
        """
        return track_deadline(case_id, rule_key, start_date, jurisdiction,
                              tolling_days, notes)

    @mcp_server.tool
    def deadline_extend(deadline_id: str, new_deadline: str, reason: str = "") -> dict:
        """Extend a tracked deadline (by stipulation or court order).

        Args:
            deadline_id: Tracked deadline ID
            new_deadline: New deadline as ISO date
            reason: Reason for extension (e.g. 'stipulation', 'court order')
        """
        return extend_deadline(deadline_id, new_deadline, reason)

    @mcp_server.tool
    def deadline_list(
        case_id: str = "",
        status: str = "active",
        days_ahead: int = 90,
    ) -> dict:
        """List all tracked deadlines sorted by urgency.

        Auto-marks overdue deadlines as 'missed'. Returns active deadlines
        within the specified lookahead window.

        Args:
            case_id: Filter by case (empty = all)
            status: active, met, missed, extended, waived (empty = all)
            days_ahead: How far ahead to look (default 90 days)
        """
        return list_tracked_deadlines(case_id, status, days_ahead)

    @mcp_server.tool
    def deadline_summary(case_id: str = "") -> dict:
        """Get a deadline health summary across all cases.

        Returns overdue items, deadlines due in 7 and 30 days,
        and counts by status. Use for daily deadline monitoring.

        Args:
            case_id: Filter to one case (empty = all cases)
        """
        return get_deadline_summary(case_id)

    @mcp_server.tool
    def deadline_lookup(query: str, jurisdiction: str = "PA") -> dict:
        """Look up deadline rules in ChromaDB when the hardcoded tables
        don't have the specific rule you need.

        Queries the 2.9M-chunk legal corpus for deadline-related provisions.
        Returns raw DB hits — verify before relying on them.

        Args:
            query: Natural language query (e.g. 'RESPA notice of error deadline')
            jurisdiction: Jurisdiction filter
        """
        return lookup_deadline_from_db(query, jurisdiction)

    return 8  # number of tools registered
