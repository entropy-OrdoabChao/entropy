"""
BLOCK_9A_HERMES_SKILLS_PORT_1_OF_2
Persistent judicial violation tracker. Ported from hermes appellate-violation-tracker skill.

Storage: <home>\obsidian_sync\violation_tracker.json
- Every violation is logged with judge, case, description, authority, severity, timestamp
- Reports queryable by judge, case, or all
- Read-only at runtime; writes only via explicit log() calls
- No interpretation. Just record + retrieve.
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import json
from pathlib import Path
from datetime import datetime
from typing import Optional, Literal

from core.logger import get_logger

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

log = get_logger("violation_tracker")

TRACKER_PATH = (_BEEZLE_ROOT / "obsidian_sync" / "violation_tracker.json")
SEVERITY = Literal["MINOR", "MODERATE", "SERIOUS", "EGREGIOUS"]


def _load() -> dict:
    if not TRACKER_PATH.exists():
        return {"version": "1.0", "violations": []}
    try:
        return json.loads(TRACKER_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        log.error(f"tracker corrupt: {e}; starting fresh")
        return {"version": "1.0", "violations": []}


def _save(data: dict) -> None:
    TRACKER_PATH.parent.mkdir(parents=True, exist_ok=True)
    TRACKER_PATH.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def log_violation(
    judge: str,
    case: str,
    violation: str,
    authority: str,
    severity: str = "MODERATE",
    source: str = "manual",
    extra: Optional[dict] = None,
) -> dict:
    """Log a single violation. Returns the entry."""
    data = _load()
    entry = {
        "id": len(data["violations"]) + 1,
        "ts": datetime.utcnow().isoformat() + "Z",
        "judge": judge.strip(),
        "case": case.strip(),
        "violation": violation.strip(),
        "authority": authority.strip(),
        "severity": severity.upper(),
        "source": source,
        "extra": extra or {},
    }
    data["violations"].append(entry)
    _save(data)
    log.info(f"logged: {judge} / {case} / {severity} / {violation[:60]}")
    return entry


def log_from_scorecard(judge: str, case: str, scorecard_path: str | Path) -> list[dict]:
    """Bulk-log violations from a scorecard JSON output."""
    sp = Path(scorecard_path)
    if not sp.exists():
        log.warning(f"scorecard not found: {sp}")
        return []
    sc = json.loads(sp.read_text(encoding="utf-8"))
    entries = []
    # Accept either {violations:[...]} or {rules:[{violated:bool,...}]}
    items = sc.get("violations") or [r for r in sc.get("rules", []) if r.get("violated")]
    for item in items:
        entries.append(log_violation(
            judge=judge,
            case=case,
            violation=item.get("description") or item.get("rule") or item.get("name", "unspecified"),
            authority=item.get("authority", "unspecified"),
            severity=item.get("severity", "MODERATE"),
            source=f"scorecard:{sp.name}",
            extra={"weight": item.get("weight"), "outcome": item.get("outcome")},
        ))
    log.info(f"logged {len(entries)} from {sp.name}")
    return entries


def report_judge(judge: str) -> dict:
    data = _load()
    matches = [v for v in data["violations"] if v["judge"].lower() == judge.lower()]
    cases = sorted({v["case"] for v in matches})
    by_severity: dict[str, int] = {}
    for v in matches:
        by_severity[v["severity"]] = by_severity.get(v["severity"], 0) + 1
    return {
        "judge": judge,
        "total_violations": len(matches),
        "cases_affected": len(cases),
        "case_list": cases,
        "by_severity": by_severity,
        "violations": matches,
    }


def report_case(case: str) -> dict:
    data = _load()
    matches = [v for v in data["violations"] if v["case"].lower() == case.lower()]
    judges = sorted({v["judge"] for v in matches})
    return {
        "case": case,
        "total_violations": len(matches),
        "judges_involved": judges,
        "violations": matches,
    }


def report_all() -> dict:
    data = _load()
    judges: dict[str, int] = {}
    cases: dict[str, int] = {}
    for v in data["violations"]:
        judges[v["judge"]] = judges.get(v["judge"], 0) + 1
        cases[v["case"]] = cases.get(v["case"], 0) + 1
    return {
        "total_violations": len(data["violations"]),
        "unique_judges": len(judges),
        "unique_cases": len(cases),
        "by_judge": dict(sorted(judges.items(), key=lambda x: -x[1])),
        "by_case": dict(sorted(cases.items(), key=lambda x: -x[1])),
    }


if __name__ == "__main__":
    import sys, argparse
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    pl = sub.add_parser("log")
    pl.add_argument("--judge", required=True)
    pl.add_argument("--case", required=True)
    pl.add_argument("--violation")
    pl.add_argument("--authority", default="unspecified")
    pl.add_argument("--severity", default="MODERATE")
    pl.add_argument("--scorecard", help="Bulk import from scorecard JSON")
    pr = sub.add_parser("report")
    pr.add_argument("--judge")
    pr.add_argument("--case")
    pr.add_argument("--all", action="store_true")
    args = ap.parse_args()
    if args.cmd == "log":
        if args.scorecard:
            print(json.dumps(log_from_scorecard(args.judge, args.case, args.scorecard), indent=2, default=str))
        else:
            print(json.dumps(log_violation(args.judge, args.case, args.violation, args.authority, args.severity), indent=2, default=str))
    else:
        if args.all:
            print(json.dumps(report_all(), indent=2, default=str))
        elif args.judge:
            print(json.dumps(report_judge(args.judge), indent=2, default=str))
        elif args.case:
            print(json.dumps(report_case(args.case), indent=2, default=str))
        else:
            print(json.dumps(report_all(), indent=2, default=str))