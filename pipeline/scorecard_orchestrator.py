"""
pipeline/scorecard_orchestrator.py  (v3 — passes ground truth to renderer)
==========================================================================
Same as v2 but passes the full case_ground_truth dict to the renderer so
case caption renders correctly (no more raw dict in header).
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import sys
import json
import re
import logging
from pathlib import Path
from datetime import datetime
from collections import Counter
from typing import Dict, Any

# ROOT derived from this file location so it resolves under WSL (<home>), native Linux, or Windows — not a hardcoded Windows path.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

log = logging.getLogger("scorecard_orchestrator")
if not log.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

# === BEEZLE_PATCH_PATH_RESOLVE_V1 (+ WSL fix) ===
def _to_wsl(raw):
    """case_map stores Windows paths (C:\\...); convert to /mnt/c/... under WSL so
    docs actually resolve (bare Path('C:\\..') fails in WSL -> 0 docs scanned)."""
    s = str(raw).replace("\\", "/")
    if re.match(r"^[A-Za-z]:/", s) and Path("/mnt").exists():
        return "/mnt/" + s[0].lower() + s[2:]
    return s

try:
    from pipeline._path_resolver import resolve_path as __rp
    def _resolve_path(raw):
        r = __rp(raw)
        if r is not None:
            return r
        p = Path(_to_wsl(raw)) if raw else None
        return p if (p is not None and p.exists()) else None
except Exception:
    def _resolve_path(raw):
        try:
            p = Path(_to_wsl(raw)) if raw else None
            return p if (p is not None and p.exists()) else None
        except Exception:
            return None
# === END BEEZLE_PATCH_PATH_RESOLVE_V1 ===


def _safe_cid(case_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", case_id)


def _canonical_id(case_id: str) -> str:
    return _safe_cid(case_id)


def _output_dir(canonical_id: str) -> Path:  # === BEEZLE_PATCH_OUTPUT_PATH_V1 ===
    # Scorecards land alongside drafts under obsidian_sync/cases/<slug>/.
    # Try canonical_id first, then the legacy fed_2_ -> fed_ form to match
    # existing slug conventions (e.g. fed_26-cv-00123).
    for slug in (canonical_id, canonical_id.replace("fed_2_", "fed_")):
        cand = ROOT / "obsidian_sync" / "cases" / slug
        if cand.exists():
            d = cand / "scorecards"
            d.mkdir(parents=True, exist_ok=True)
            return d
    # No existing case folder — create under canonical_id
    d = ROOT / "obsidian_sync" / "cases" / canonical_id / "scorecards"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _audit_dir(canonical_id: str) -> Path:
    d = ROOT / "output" / "case_audits" / canonical_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _classify_role(path_str, defendants=None, plaintiff_name=None):
    try:
        from doc_role_classifier import classify_doc_with_evidence
        ev = classify_doc_with_evidence(
            path_str, defendants=defendants, plaintiff_name=plaintiff_name)
        return ev.get("role", "unknown")
    except Exception as e:
        log.debug("classify failed for %s: %s", path_str, e)
        return "unknown"


def _empty_counts():
    return {"documents_scanned": 0, "documents_with_violations": 0,
            "total_violations": 0, "fatal": 0, "serious": 0, "standard": 0,
            "by_pattern": {}, "by_role": {}}


def _load_case_ground_truth(case_id):
    canonical_id = _canonical_id(case_id)
    candidates = [
        ROOT / "obsidian_sync" / "cases" / canonical_id.replace("fed_2_", "fed_") / "meta" / "CASE_GROUND_TRUTH.yaml",
        ROOT / "obsidian_sync" / "cases" / canonical_id / "meta" / "CASE_GROUND_TRUTH.yaml",
    ]
    for cand in candidates:
        if cand.exists():
            try:
                import yaml
                return yaml.safe_load(cand.read_text(encoding="utf-8"))
            except Exception:
                pass
    return {}


def _scan_case(case_id, court_id="EDPA"):
    canonical_id = _canonical_id(case_id)
    log.info("scoring case %s -> %s", case_id, canonical_id)

    gt = _load_case_ground_truth(case_id)
    defendants = []
    if gt:
        d = gt.get("defendants") or (gt.get("parties") or {}).get("defendants") or []
        if isinstance(d, list):
            for item in d:
                if isinstance(item, str):
                    defendants.append(item)
                elif isinstance(item, dict):
                    n = item.get("name") or item.get("short_name")
                    if n:
                        defendants.append(n)
        elif isinstance(d, dict):
            defendants = list(d.keys())

    case_map_path = ROOT / "cases_index" / "case_map.json"
    if not case_map_path.exists():
        log.error("case_map.json not found at %s", case_map_path)
        return _empty_scan(case_id, canonical_id, court_id)

    case_map = json.loads(case_map_path.read_text(encoding="utf-8"))
    entry = case_map.get(case_id)
    if not entry:
        for k, v in case_map.items():
            if k.endswith(case_id.split(":")[-1]):
                entry = v
                break
    if not entry:
        log.error("no case_map entry for %s", case_id)
        return _empty_scan(case_id, canonical_id, court_id)

    if not defendants:
        defendants = entry.get("all_defendants") or []

    plaintiff_name = "Filer"
    if gt:
        pn = gt.get("plaintiff", {})
        if isinstance(pn, dict):
            plaintiff_name = pn.get("name") or plaintiff_name
        elif isinstance(pn, str):
            plaintiff_name = pn

    docs = []
    for kind in ("orders", "filings", "motions_to_dismiss", "mtds",
                 "oppositions", "court_notices", "exhibits", "documents",
                 "my_drafts"):
        bucket = entry.get(kind) or []
        if isinstance(bucket, dict):
            bucket = list(bucket.values())
        if isinstance(bucket, list):
            for d in bucket:
                if isinstance(d, str):
                    docs.append({"path": d, "kind": kind})
                elif isinstance(d, dict):
                    p = d.get("path") or d.get("file") or d.get("filepath")
                    if p:
                        docs.append({"path": p, "kind": kind, **d})

    log.info("  found %d candidate docs", len(docs))
    log.info("  defendants: %s", defendants[:5])
    log.info("  plaintiff:  %s", plaintiff_name)

    adversary_docs = []
    role_counts = Counter()
    for d in docs:
        role = _classify_role(d["path"], defendants=defendants, plaintiff_name=plaintiff_name)
        role_counts[role] += 1
        d["role"] = role
        if role != "plaintiff":
            adversary_docs.append(d)

    log.info("  role split: %s", dict(role_counts))
    log.info("  scanning %d adversary docs", len(adversary_docs))

    try:
        from pipeline.block14_pinpointer import scan_document_for_violations
        from pipeline.doc_reader import extract_text
    except ImportError as e:
        log.error("cannot import: %s", e)
        return _empty_scan(case_id, canonical_id, court_id)

    all_violations = []
    by_file = {}
    by_pattern = Counter()
    sev_counts = Counter()

    # === BEEZLE_PATCH_ORCH_SCAN_RESOLVE_V1 ===
    _unresolved_samples = []
    _resolved_count = 0
    for d in adversary_docs:
        path = _resolve_path(d["path"])
        if path is None:
            if len(_unresolved_samples) < 5:
                _unresolved_samples.append(str(d["path"])[:200])
            continue
        _resolved_count += 1
        try:
            text = extract_text(str(path)) or ""
        except Exception:
            continue
        if not text:
            continue
        try:
            pins = scan_document_for_violations(
                text=text, source_doc=path.name, court=court_id)
        except Exception:
            continue
        if not pins:
            continue
        for pin in pins:
            if hasattr(pin, "__dict__"):
                pin_d = dict(pin.__dict__)
            elif isinstance(pin, dict):
                pin_d = pin
            else:
                continue
            pin_d["role"] = d["role"]
            pin_d["source_doc"] = pin_d.get("source_doc") or path.name
            pin_d["source_path"] = str(path)
            all_violations.append(pin_d)
            by_file.setdefault(path.name, []).append(pin_d)
            ptype = pin_d.get("violation_type") or pin_d.get("type") or "UNKNOWN"
            by_pattern[ptype] += 1
            sev = (pin_d.get("severity") or "").upper()
            sev_counts[sev] += 1

    log.info("  %d violations across %d files", len(all_violations), len(by_file))
    if _unresolved_samples:
        log.info("  resolved %d / %d adversary docs; unresolved samples:",
                 _resolved_count, len(adversary_docs))
        for _s in _unresolved_samples:
            log.info("    unresolved: %s", _s)
    log.info("  severity: %s", dict(sev_counts))

    counts = {
        "documents_scanned": len(adversary_docs),
        "documents_with_violations": len(by_file),
        "total_violations": len(all_violations),
        "fatal": sev_counts.get("FATAL", 0),
        "serious": sev_counts.get("SERIOUS", 0),
        "standard": sev_counts.get("STANDARD", 0),
        "by_pattern": dict(by_pattern),
        "by_role": dict(role_counts),
    }

    # Caption / judge / court
    case_caption = None
    judge = ""
    case_court = ""
    if gt:
        case_caption = gt.get("case_caption") or gt.get("caption") or gt
        j = gt.get("judge", {})
        if isinstance(j, dict):
            judge = j.get("name") or ""
        elif isinstance(j, str):
            judge = j
        c = gt.get("court", {})
        if isinstance(c, dict):
            case_court = c.get("name") or c.get("full_name") or ""
        elif isinstance(c, str):
            case_court = c

    return {
        "case_id": case_id,
        "canonical_id": canonical_id,
        "generated": datetime.now().isoformat(),
        "court_id": court_id,
        "case_caption": case_caption,
        "case_ground_truth": gt,
        "judge": judge,
        "case_court": case_court,
        "violations": all_violations,
        "by_file": by_file,
        "counts": counts,
    }


def _empty_scan(case_id, canonical_id, court_id):
    return {
        "case_id": case_id,
        "canonical_id": canonical_id,
        "generated": datetime.now().isoformat(),
        "court_id": court_id,
        "case_caption": "",
        "case_ground_truth": {},
        "judge": "",
        "case_court": "",
        "violations": [],
        "by_file": {},
        "counts": _empty_counts(),
    }


def _write_audit_artifact(scan_result):
    canonical_id = scan_result["canonical_id"]
    audit_path = _audit_dir(canonical_id) / "audit.json"
    existing = {}
    if audit_path.exists():
        try:
            existing = json.loads(audit_path.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
    reports = []
    for filename, pins in scan_result["by_file"].items():
        path = pins[0].get("source_path", filename)
        severity_counts = Counter((p.get("severity") or "").upper() for p in pins)
        reports.append({
            "path": path,
            "kind": "adversary",
            "role": pins[0].get("role", "unknown"),
            "scan": {
                "violations": pins,
                "violation_count": len(pins),
                "void": severity_counts.get("VOID", 0),
                "voidable": severity_counts.get("FATAL", 0),
                "severity_counts": dict(severity_counts),
                "source_doc": filename,
            },
        })
    out = {
        **existing,
        "generated": scan_result["generated"],
        "case_id": scan_result["case_id"],
        "canonical_id": canonical_id,
        "documents_scanned": scan_result["counts"]["documents_scanned"],
        "total_violations": scan_result["counts"]["total_violations"],
        "by_category": scan_result["counts"]["by_pattern"],
        "by_role": scan_result["counts"]["by_role"],
        "adversary_summary": {
            "fatal":    scan_result["counts"]["fatal"],
            "serious":  scan_result["counts"]["serious"],
            "standard": scan_result["counts"]["standard"],
        },
        "reports": reports,
    }
    audit_path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    log.info("wrote audit artifact: %s (%d bytes)", audit_path, audit_path.stat().st_size)
    return audit_path


def _render_scorecard(scan_result):
    canonical_id = scan_result["canonical_id"]
    out_dir = _output_dir(canonical_id)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"scorecard_exhibit_{canonical_id}_{ts}.docx"

    from generation.scorecard_renderer_v2 import render_scorecard_v2
    result_path = render_scorecard_v2(
        scan_result=scan_result,
        output_path=out_path,
        case_caption=scan_result.get("case_caption"),
        judge=scan_result.get("judge") or None,
        case_court=scan_result.get("case_court") or None,
        case_ground_truth=scan_result.get("case_ground_truth"),
    )
    log.info("scorecard rendered: %s", result_path)
    return Path(result_path)


def build_scorecard_for_case(case_id, court_id="EDPA", refresh=True, exhibit_label="A"):
    canonical_id = _canonical_id(case_id)
    # FRESHNESS CONTRACT (GO step 3, 2026-07-08): refresh=True now means "ensure current", not
    # "always re-scan". If the case's audit artifact postdates the newest intake file, the scan it
    # holds already reflects everything on disk — reuse it (04767 was re-scored 3x in 37 min doing
    # the identical block14 scan audit_writer had just run). refresh="force" = unconditional re-scan.
    if refresh is True:
        try:
            from pipeline.audit_writer import _newest_intake_mtime
            _art = _audit_dir(canonical_id) / "audit.json"
            if _art.exists() and _art.stat().st_mtime > _newest_intake_mtime(canonical_id):
                log.info("scorecard: audit artifact fresh (newer than newest intake) — reusing scan")
                refresh = False
        except Exception:
            pass  # any doubt -> full re-scan (safe default)
    if refresh:
        scan_result = _scan_case(case_id, court_id=court_id)
        _write_audit_artifact(scan_result)
    else:
        audit_path = _audit_dir(canonical_id) / "audit.json"
        if not audit_path.exists():
            scan_result = _scan_case(case_id, court_id=court_id)
            _write_audit_artifact(scan_result)
        else:
            data = json.loads(audit_path.read_text(encoding="utf-8"))
            by_file = {}
            for rep in data.get("reports", []):
                fn = rep.get("scan", {}).get("source_doc") or Path(rep.get("path", "?")).name
                by_file[fn] = rep.get("scan", {}).get("violations", [])
            scan_result = {
                "case_id": case_id,
                "canonical_id": canonical_id,
                "generated": data.get("generated", datetime.now().isoformat()),
                "court_id": court_id,
                "case_caption": None,
                "case_ground_truth": _load_case_ground_truth(case_id),
                "judge": "",
                "case_court": "",
                "violations": [v for pins in by_file.values() for v in pins],
                "by_file": by_file,
                "counts": {
                    "documents_scanned":         data.get("documents_scanned", 0),
                    "documents_with_violations": len(by_file),
                    "total_violations":          data.get("total_violations", 0),
                    "fatal":    data.get("adversary_summary", {}).get("fatal", 0),
                    "serious":  data.get("adversary_summary", {}).get("serious", 0),
                    "standard": data.get("adversary_summary", {}).get("standard", 0),
                    "by_pattern": data.get("by_category", {}),
                    "by_role":    data.get("by_role", {}),
                },
            }

    out_path = _render_scorecard(scan_result)
    counts = scan_result["counts"]
    total = counts["total_violations"]
    fatal = counts["fatal"]
    judicial_notice_line = (
        f"TAKE JUDICIAL NOTICE under Federal Rule of Evidence 201(b)(2) of "
        f"the accompanying Scorecard Exhibit {exhibit_label}, which tabulates "
        f"the {total} defects (including {fatal} fatal-severity defects) "
        f"identified in the adversary filings of record."
    )
    return {
        "ok": True,
        "out_path": str(out_path),
        "canonical_id": canonical_id,
        "case_id": case_id,
        "court_id": court_id,
        "counts": counts,
        "exhibit_label": exhibit_label,
        "judicial_notice_line": judicial_notice_line,
    }


def main(argv=None):
    argv = argv or sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print("Usage: python pipeline\\scorecard_orchestrator.py <case_id> [court_id]")
        print("       python pipeline\\scorecard_orchestrator.py --all")
        return 1
    if argv[0] == "--all":
        case_ids = ["fed:2:26-cv-00123", "fed:2:26-cv-00456",
                    "fed:2:26-cv-00789", "fed:2:26-cv-03268",
                    "phila_ccp_260100000"]
    else:
        case_ids = [argv[0]]
    court_id = argv[1] if len(argv) > 1 else "EDPA"
    for cid in case_ids:
        print(f"\n=== {cid} ===")
        result = build_scorecard_for_case(cid, court_id=court_id, refresh=True)
        if result.get("ok"):
            print(f"  out_path: {result['out_path']}")
            c = result["counts"]
            print(f"  scanned:  {c['documents_scanned']} adversary docs")
            print(f"  total:    {c['total_violations']} ({c['fatal']} fatal, {c['serious']} serious)")
            print(f"  by_role:  {c['by_role']}")
            print(f"  patterns: {c['by_pattern']}")
        else:
            print(f"  FAILED: {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
