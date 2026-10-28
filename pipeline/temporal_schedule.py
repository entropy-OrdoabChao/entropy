"""
pipeline/temporal_schedule.py v2 — Temporal filing schedule engine.

v2 changes from v1:
  + party_role filter — engine accepts user_role (default "plaintiff") and
    skips rules whose party_role doesn't match (Bug 3 fix)
  + maps_to dispatch — rules declare which case-anchor fields satisfy their
    trigger directly (eliminates noise from TRIGGER_MAP misfires)
  + caption.defendants[].dismissed parsing — extracts "ECF 122 (09/19/2025)"
    into entry_of_judgment (Bug 1 fix for 02822)
  + latest_order.kind heuristic stronger — sets entry_of_judgment when
    kind matches dismiss/grant/MTD-disposition (Bug 2 fix for 04767)
  + maps_to=[] rules are NEVER auto-computed — they require explicit
    anchors and remain silent if not provided (suppresses defendant rules
    and SOL rules when the engine can't compute them safely)

Reads case yaml to determine current procedural posture and computes live
deadlines for every applicable rule in temporal_rules.yaml.

Key entry points
────────────────
load_rules() -> dict
get_case_anchors(case_id) -> dict
compute_deadlines(case_id, user_role="plaintiff") -> list[dict]
format_deadlines_block(case_id, user_role="plaintiff") -> str

This module is read-only. It NEVER modifies yaml.
"""

from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

# v1.2: case state precondition filter
try:
    from pipeline.case_state import derive_case_state
except Exception:
    derive_case_state = None

try:
    import yaml
except ImportError:
    yaml = None

log = logging.getLogger("temporal_schedule")

ROOT = Path(__file__).resolve().parent.parent
RULES_PATH = Path(__file__).resolve().parent / "temporal_rules.yaml"

_RULES_CACHE: Optional[Dict[str, Any]] = None

# Dismissal/judgment keywords used across multiple parsers
_DISMISS_KEYWORDS = ("dismiss", "granted", "final judgment", "judgment entered",
                     "judgment as", "entered judgment", "mtd granted",
                     "mtd_granted")


# ═════════════════════════════════════════════════════════════════════════
# RULE LOADER
# ═════════════════════════════════════════════════════════════════════════

def load_rules(force_reload: bool = False) -> Dict[str, Any]:
    """Load + cache temporal_rules.yaml."""
    global _RULES_CACHE
    if _RULES_CACHE is not None and not force_reload:
        return _RULES_CACHE
    if yaml is None:
        log.error("PyYAML not installed — temporal_schedule disabled")
        return {"rules": [], "federal_holidays_2025": [], "federal_holidays_2026": [],
                "pa_holidays_2025": [], "pa_holidays_2026": []}
    if not RULES_PATH.exists():
        log.error("temporal_rules.yaml not found at %s", RULES_PATH)
        return {"rules": [], "federal_holidays_2025": [], "federal_holidays_2026": [],
                "pa_holidays_2025": [], "pa_holidays_2026": []}
    _RULES_CACHE = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
    log.info("temporal_schedule v2: loaded %d rules", len(_RULES_CACHE.get("rules", [])))
    return _RULES_CACHE


# BEEZLE_FN_UNUSED 2026-06-13: get_rules_by_forum (no static refs in pipeline/)
def get_rules_by_forum(forum: str) -> List[Dict[str, Any]]:
    return [r for r in load_rules().get("rules", []) if r.get("forum") == forum]


# BEEZLE_FN_UNUSED 2026-06-13: get_rule (no static refs in pipeline/)
def get_rule(rule_id: str) -> Optional[Dict[str, Any]]:
    for r in load_rules().get("rules", []):
        if r.get("id") == rule_id:
            return r
    return None


# ═════════════════════════════════════════════════════════════════════════
# DAY COUNTING ENGINE
# ═════════════════════════════════════════════════════════════════════════

def _parse_date(v: Union[str, date, datetime, None]) -> Optional[date]:
    """Normalize various date encodings to date object."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        v = v.strip().split("T")[0]
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
            try:
                return datetime.strptime(v, fmt).date()
            except ValueError:
                continue
    return None


def _extract_date_from_text(text: str) -> Optional[date]:
    """Find an ISO or US date inside free text. e.g. 'ECF 122 (09/19/2025)' → 2025-09-19"""
    if not isinstance(text, str):
        return None
    # ISO: YYYY-MM-DD
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    # US: MM/DD/YYYY or MM/DD/YY
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b", text)
    if m:
        try:
            yr = int(m.group(3))
            if yr < 100:
                yr += 2000
            return date(yr, int(m.group(1)), int(m.group(2)))
        except ValueError:
            pass
    return None


def _get_holidays(counting: str, year: int) -> List[date]:
    rules = load_rules()
    out = []
    fed_key = f"federal_holidays_{year}"
    pa_key = f"pa_holidays_{year}"
    for h in rules.get(fed_key, []):
        d = _parse_date(h)
        if d:
            out.append(d)
    if counting == "PA_106":
        for h in rules.get(pa_key, []):
            d = _parse_date(h)
            if d:
                out.append(d)
    return out


def _is_court_holiday(d: date, counting: str) -> bool:
    if counting == "literal":
        return False
    if d.weekday() >= 5:
        return True
    holidays = _get_holidays(counting, d.year)
    return d in holidays


def add_days(trigger: date, days: int, counting: str = "FRCP_6a") -> date:
    """Apply day-counting rule per FRCP 6(a) / FRAP 26(a) / PA Rule 106 / literal."""
    if counting == "literal":
        return trigger + timedelta(days=days)
    end = trigger + timedelta(days=days)
    while _is_court_holiday(end, counting):
        end = end + timedelta(days=1)
    return end


# ═════════════════════════════════════════════════════════════════════════
# CASE CONTEXT EXTRACTION
# ═════════════════════════════════════════════════════════════════════════

def _slug_for_case(case_id: str) -> Optional[str]:
    parts = case_id.split(":")
    if not parts:
        return None
    last = parts[-1]
    if case_id.startswith("fed:"):
        return f"fed_{last}"
    if case_id.startswith("phila_ccp:"):
        return f"phila_ccp_{last}"
    return last


def _infer_forum(case_id: str) -> str:
    if case_id.startswith("fed:"):
        return "federal_trial"
    if case_id.startswith("phila_ccp:"):
        return "pa_trial"
    if case_id.startswith("pa_super:"):
        return "pa_superior"
    if case_id.startswith("pa_supreme:"):
        return "pa_supreme"
    if case_id.startswith("scotus:"):
        return "scotus"
    if case_id.startswith("tax:"):
        return "tax_court"
    return "federal_trial"


def get_case_anchors(case_id: str) -> Dict[str, Any]:
    """Extract trigger dates from CASE_GROUND_TRUTH.yaml for one case.

    Returns dict with:
      forum, filed, latest_order, entry_of_judgment, appeal_filed,
      appeal_decided, pending_motions, explicit_deadlines, raw, anchor_sources

    anchor_sources: dict mapping each anchor field to the yaml-path it was
    extracted from (for audit/debug).
    """
    slug = _slug_for_case(case_id)
    if not slug:
        return {}
    yaml_path = ROOT / "obsidian_sync" / "cases" / slug / "meta" / "CASE_GROUND_TRUTH.yaml"
    if not yaml_path.exists() or yaml is None:
        return {"forum": _infer_forum(case_id), "raw": {}}

    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}

    out: Dict[str, Any] = {
        "case_id": case_id,
        "raw": data,
        "forum": _infer_forum(case_id),
        "filed": _parse_date(data.get("filed")),
        "latest_order": None,
        "entry_of_judgment": None,
        "appeal_filed": None,
        "appeal_decided": None,
        "pending_motions": data.get("pending_motions", []) or [],
        "explicit_deadlines": [],
        "anchor_sources": {},
    }
    src = out["anchor_sources"]

    if out["filed"]:
        src["filed"] = "yaml.filed"

    # latest_order can be top-level dict OR scalar OR inferred from procedural_history
    lo = data.get("latest_order")
    if isinstance(lo, dict):
        out["latest_order"] = _parse_date(lo.get("date"))
        if out["latest_order"]:
            src["latest_order"] = "yaml.latest_order.date"
        # Check kind / notes / disposition for dismissal signal
        kind = (lo.get("kind") or lo.get("type") or lo.get("disposition") or "")
        notes = (lo.get("notes") or "")
        combined = (str(kind) + " " + str(notes)).lower()
        if any(kw in combined for kw in _DISMISS_KEYWORDS):
            out["entry_of_judgment"] = out["latest_order"]
            src["entry_of_judgment"] = "yaml.latest_order (dismiss-keyword match)"
    elif isinstance(lo, (str, date, datetime)):
        out["latest_order"] = _parse_date(lo)
        if out["latest_order"]:
            src["latest_order"] = "yaml.latest_order (scalar)"

    # PATCH (Bug 1): Parse caption.defendants[].dismissed for embedded dates
    # Example: dismissed: "ECF 122 (09/19/2025)" → 2025-09-19
    if not out["entry_of_judgment"]:
        caption = data.get("caption", {}) or {}
        defendants = caption.get("defendants", []) or []
        dismissal_dates = []
        if isinstance(defendants, list):
            for d in defendants:
                if not isinstance(d, dict):
                    continue
                dismissed = d.get("dismissed")
                if not dismissed or dismissed == "null":
                    continue
                if isinstance(dismissed, (date, datetime)):
                    dismissal_dates.append(_parse_date(dismissed))
                elif isinstance(dismissed, str):
                    parsed = _extract_date_from_text(dismissed)
                    if parsed:
                        dismissal_dates.append(parsed)
        dismissal_dates = [d for d in dismissal_dates if d]
        if dismissal_dates:
            # Use earliest dismissal as entry_of_judgment anchor — that's when
            # the 28-day FRCP 59(e) and 1-year FRCP 60(b) clocks start
            out["entry_of_judgment"] = min(dismissal_dates)
            src["entry_of_judgment"] = f"yaml.caption.defendants[].dismissed (earliest of {len(dismissal_dates)})"

    # Procedural_history fallback for latest_order AND entry_of_judgment
    proc = data.get("procedural_history", []) or []
    if isinstance(proc, list) and proc:
        dates_with_events = []
        for e in proc:
            if isinstance(e, dict):
                d = _parse_date(e.get("date"))
                if d:
                    dates_with_events.append((d, e))
        if dates_with_events:
            dates_with_events.sort(key=lambda x: x[0], reverse=True)
            # Most-recent date populates latest_order if not already set
            if not out["latest_order"]:
                out["latest_order"] = dates_with_events[0][0]
                src["latest_order"] = "yaml.procedural_history (most recent)"
            # Find earliest dismissal event for entry_of_judgment if not set
            if not out["entry_of_judgment"]:
                dismissal_events = []
                for d, e in dates_with_events:
                    evt = (e.get("event") or "").lower()
                    if any(kw in evt for kw in _DISMISS_KEYWORDS):
                        # Skip 3d Cir appellate dismissals (those are appeal_decided)
                        if "3d cir" in evt or "circuit" in evt or "court of appeals" in evt:
                            continue
                        dismissal_events.append(d)
                if dismissal_events:
                    out["entry_of_judgment"] = min(dismissal_events)
                    src["entry_of_judgment"] = "yaml.procedural_history (dismiss-keyword event)"
            # Find appellate disposition for appeal_decided
            for d, e in dates_with_events:
                evt = (e.get("event") or "").lower()
                if any(kw in evt for kw in ("3d cir", "circuit", "court of appeals", "appeal dismiss", "appeal denied", "appellate")):
                    out["appeal_decided"] = d
                    src["appeal_decided"] = "yaml.procedural_history (appellate event)"
                    break

    # Explicit deadlines from yaml
    for dl in data.get("deadlines", []) or []:
        if isinstance(dl, dict):
            d = _parse_date(dl.get("date"))
            if d:
                out["explicit_deadlines"].append({
                    "date": d,
                    "label": dl.get("event") or dl.get("notes") or dl.get("label") or "?",
                    "source": "yaml.deadlines[]",
                })

    # Appeals
    appeals = data.get("appeals", []) or []
    if isinstance(appeals, list) and appeals:
        for a in appeals:
            if not isinstance(a, dict):
                continue
            kd = a.get("key_dates", {}) or {}
            if isinstance(kd, dict):
                for k in ("notice_filed", "filed", "reinstated"):
                    v = _parse_date(kd.get(k))
                    if v:
                        out["appeal_filed"] = v
                        src["appeal_filed"] = f"yaml.appeals[].key_dates.{k}"
                        break
                for k in ("decided", "dismissed"):
                    v = _parse_date(kd.get(k))
                    if v and not out.get("appeal_decided"):
                        out["appeal_decided"] = v
                        src["appeal_decided"] = f"yaml.appeals[].key_dates.{k}"

    return out


# ═════════════════════════════════════════════════════════════════════════
# DEADLINE COMPUTATION
# ═════════════════════════════════════════════════════════════════════════

# Anchor fields the engine knows about
KNOWN_ANCHORS = ["entry_of_judgment", "appeal_decided", "latest_order",
                 "appeal_filed", "filed"]


def compute_deadlines(case_id: str, today: Optional[date] = None,
                       user_role: str = "plaintiff") -> List[Dict[str, Any]]:
    """Compute live deadlines for a case.

    user_role filters rules by party_role:
      - plaintiff: shows rules with party_role in {'plaintiff', 'either'}
      - defendant: shows rules with party_role in {'defendant', 'either'}
      - both:      shows all

    Each rule with non-empty maps_to is evaluated against the case's anchor
    dict. The first matching anchor in maps_to wins. Rules with empty maps_to
    are skipped silently (require explicit context the engine can't infer).

    status ∈ {'EXPIRED', 'IMMINENT'(≤7d), 'UPCOMING'(≤30d), 'OPEN'(>30d or null)}
    """
    if today is None:
        today = date.today()

    anchors = get_case_anchors(case_id)

    # v1.2: derive case state for precondition filtering
    _case_state_for_filter = None
    if derive_case_state is not None:
        try:
            import yaml as _yaml
            from pathlib import Path as _Path
            _slug = _slug_for_case(case_id)
            if _slug:
                _yp = _Path(str(_eroot() / "obsidian_sync/cases")) / _slug / 'meta' / 'CASE_GROUND_TRUTH.yaml'
                if _yp.exists():
                    _yd = _yaml.safe_load(_yp.read_text(encoding='utf-8'))
                    _case_state_for_filter = derive_case_state(_yd or {})
        except Exception as _e:
            import logging as _logging
            _logging.getLogger('temporal_schedule').warning(f'state derivation failed: {_e}')
            _case_state_for_filter = None
    if not anchors:
        return []
    forum = anchors.get("forum", "federal_trial")

    # Forum cascade
    applicable_forums = {forum}
    if anchors.get("entry_of_judgment") or anchors.get("appeal_decided"):
        if forum == "federal_trial":
            applicable_forums.add("federal_appellate")
        if forum == "pa_trial":
            applicable_forums.add("pa_superior")
            applicable_forums.add("pa_commonwealth")
        if forum == "federal_appellate":
            applicable_forums.add("scotus")
        if forum in ("pa_superior", "pa_commonwealth"):
            applicable_forums.add("pa_supreme")
    if anchors.get("appeal_decided"):
        # Once appellate has decided, SCOTUS / PA Supreme become reachable
        if forum == "federal_trial":
            applicable_forums.add("scotus")
        if forum == "pa_trial":
            applicable_forums.add("pa_supreme")

    # Role filter setup
    role_filter = {user_role}
    role_filter.add("either")
    if user_role == "both":
        role_filter = {"plaintiff", "defendant", "either"}

    results: List[Dict[str, Any]] = []

    for rule in load_rules().get("rules", []):
        # v1.2: skip rules whose requires_state doesn't match current case state
        _req_states = rule.get('requires_state') if isinstance(rule, dict) else None
        if _req_states and _case_state_for_filter is not None:
            if _case_state_for_filter not in _req_states:
                continue
        # Forum filter
        if rule.get("forum") not in applicable_forums:
            continue
        # Party-role filter (Bug 3 fix)
        if rule.get("party_role", "either") not in role_filter:
            continue

        # Find anchor: rule's maps_to declares which case fields satisfy it.
        # Empty maps_to → skip silently (engine can't safely compute).
        maps_to = rule.get("maps_to", []) or []
        anchor_date = None
        anchor_field = None
        for field_name in maps_to:
            d = anchors.get(field_name)
            if d:
                anchor_date = d
                anchor_field = field_name
                break

        days = rule.get("days")

        # Rules with days=null are "reasonable time" / "any time" — list as OPEN
        # but only if at least one anchor exists in maps_to
        if days is None:
            if anchor_date:
                results.append({
                    "rule_id": rule.get("id"),
                    "citation": rule.get("citation"),
                    "name": rule.get("name"),
                    "anchor_date": anchor_date.isoformat() if anchor_date else None,
                    "anchor_field": anchor_field,
                    "trigger": rule.get("trigger"),
                    "deadline": None,
                    "days_remaining": None,
                    "status": "OPEN",
                    "jurisdictional": rule.get("jurisdictional", False),
                    "waivable": rule.get("waivable", True),
                    "party_role": rule.get("party_role", "either"),
                    "notes": rule.get("notes", ""),
                })
            continue

        if not anchor_date:
            continue

        deadline = add_days(anchor_date, days, rule.get("counting", "FRCP_6a"))
        days_remaining = (deadline - today).days

        if days_remaining < 0:
            status = "EXPIRED"
        elif days_remaining <= 7:
            status = "IMMINENT"
        elif days_remaining <= 30:
            status = "UPCOMING"
        else:
            status = "OPEN"

        results.append({
            "rule_id": rule.get("id"),
            "citation": rule.get("citation"),
            "name": rule.get("name"),
            "anchor_date": anchor_date.isoformat(),
            "anchor_field": anchor_field,
            "trigger": rule.get("trigger"),
            "deadline": deadline.isoformat(),
            "days_remaining": days_remaining,
            "status": status,
            "jurisdictional": rule.get("jurisdictional", False),
            "waivable": rule.get("waivable", True),
            "party_role": rule.get("party_role", "either"),
            "notes": rule.get("notes", ""),
        })

    def sort_key(r):
        dr = r.get("days_remaining")
        if dr is None:
            return (3, 0)
        if dr < 0:
            return (2, -dr)
        return (1, dr)
    results.sort(key=sort_key)

    return results


# ═════════════════════════════════════════════════════════════════════════
# FORMATTING
# ═════════════════════════════════════════════════════════════════════════

def format_deadlines_block(case_id: str, today: Optional[date] = None,
                            include_open: bool = False,
                            user_role: str = "plaintiff") -> str:
    deadlines = compute_deadlines(case_id, today=today, user_role=user_role)
    anchors = get_case_anchors(case_id)
    today_str = (today or date.today()).isoformat()

    lines = [f"## Filing Schedule — {case_id}", ""]
    lines.append(f"**Computed:** {today_str}  ")
    lines.append(f"**Forum:** {anchors.get('forum', '?')}  ")
    lines.append(f"**User role:** {user_role}  ")
    if anchors.get("filed"):
        lines.append(f"**Filed:** {anchors['filed'].isoformat()}  ")
    if anchors.get("latest_order"):
        lines.append(f"**Latest order:** {anchors['latest_order'].isoformat()}  ")
    if anchors.get("entry_of_judgment"):
        lines.append(f"**Entry of judgment:** {anchors['entry_of_judgment'].isoformat()}  ")
    if anchors.get("appeal_filed"):
        lines.append(f"**Appeal filed:** {anchors['appeal_filed'].isoformat()}  ")
    if anchors.get("appeal_decided"):
        lines.append(f"**Appeal decided:** {anchors['appeal_decided'].isoformat()}  ")
    lines.append("")

    if not deadlines:
        lines.append("_No computable deadlines from current yaml anchors._")
        lines.append("")
        if anchors.get("explicit_deadlines"):
            lines.append("### Explicit Deadlines (from yaml `deadlines[]`)")
            lines.append("")
            for ed in anchors["explicit_deadlines"]:
                d = ed["date"]
                dr = (d - (today or date.today())).days
                lines.append(f"- **{d.isoformat()}**: {ed['label']}")
                lines.append(f"  - {dr} days {'remaining' if dr >= 0 else 'OVERDUE'}")
        return "\n".join(lines)

    groups = {"EXPIRED": [], "IMMINENT": [], "UPCOMING": [], "OPEN": []}
    for d in deadlines:
        groups[d["status"]].append(d)

    for status_label, items in [
        ("🚨 EXPIRED", groups["EXPIRED"]),
        ("⚠️  IMMINENT (≤7 days)", groups["IMMINENT"]),
        ("⏳ UPCOMING (≤30 days)", groups["UPCOMING"]),
    ]:
        if not items:
            continue
        lines.append(f"### {status_label}")
        lines.append("")
        for d in items:
            jur = " **[JURISDICTIONAL]**" if d.get("jurisdictional") else ""
            lines.append(f"- **{d['name']}** ({d['citation']}){jur}")
            lines.append(f"  - Anchor: {d['anchor_date']} ({d['anchor_field']} → {d['trigger']})")
            if d.get("deadline"):
                dr = d["days_remaining"]
                dr_str = f"{dr} days remaining" if dr >= 0 else f"{-dr} days OVERDUE"
                lines.append(f"  - Deadline: {d['deadline']} ({dr_str})")
            if d.get("notes"):
                lines.append(f"  - Notes: {d['notes']}")
        lines.append("")

    if include_open and groups["OPEN"]:
        lines.append("### Open (no fixed deadline)")
        lines.append("")
        for d in groups["OPEN"]:
            lines.append(f"- **{d['name']}** ({d['citation']})")
            if d.get("anchor_field"):
                lines.append(f"  - Anchor: {d['anchor_date']} ({d['anchor_field']})")
            if d.get("notes"):
                lines.append(f"  - Notes: {d['notes']}")
        lines.append("")

    if anchors.get("explicit_deadlines"):
        lines.append("### Explicit Deadlines (from yaml `deadlines[]`)")
        lines.append("")
        for ed in anchors["explicit_deadlines"]:
            d = ed["date"]
            dr = (d - (today or date.today())).days
            lines.append(f"- **{d.isoformat()}**: {ed['label']}")
            lines.append(f"  - {dr} days {'remaining' if dr >= 0 else 'OVERDUE'}")
        lines.append("")

    return "\n".join(lines)


# ═════════════════════════════════════════════════════════════════════════
# CLI
# ═════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        cid = sys.argv[1]
        role = sys.argv[2] if len(sys.argv) > 2 else "plaintiff"
        print(format_deadlines_block(cid, include_open=True, user_role=role))
    else:
        rules = load_rules()
        print(f"Loaded {len(rules.get('rules', []))} rules from {RULES_PATH}")
        for r in rules.get("rules", []):
            mt = ",".join(r.get("maps_to", []) or []) or "—"
            print(f"  {r['id']:<35s} {r.get('forum','?'):<20s} {r.get('party_role','?'):<10s} maps_to=[{mt}]")
