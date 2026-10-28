# -*- coding: utf-8 -*-
"""
case_audit_engine.py
====================
Runs judicial_order_scanner across all orders/documents for a case
and feeds results into Agent 10 (immunity/criminal) via chain_runner.

Outputs per-case void/voidable analysis to output/case_audits/<case>/.

Place in: <home>\\pipeline\\case_audit_engine.py

Usage:
    python -m pipeline.case_audit_engine --case fed:2:26-cv-00123
    python -m pipeline.case_audit_engine --all
"""

from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import argparse
import json
import logging
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

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

ROOT = _BEEZLE_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.chain_runner import ChainRunner, _safe_import  # noqa: E402

log = logging.getLogger("case_audit_engine")
if not log.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

AUDIT_DIR = ROOT / "output" / "case_audits"
AUDIT_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Case → document resolution
# ---------------------------------------------------------------------------
def _load_case_map() -> Dict[str, Any]:
    path = ROOT / "cases_index" / "case_map.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.error("case_map.json load failed: %s", e)
        return {}


def _load_registry() -> Dict[str, Any]:
    path = ROOT / "cases_index" / "case_registry.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if isinstance(data, dict) and "cases" in data and isinstance(data["cases"], list):
        return {c.get("case_id") or c.get("id"): c for c in data["cases"]}
    return data if isinstance(data, dict) else {}


def _resolve_case_documents(case_id: str, case_map: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Resolve documents for a case from case_map.json.
    Returns list of dicts with at least {path, kind}.
    """
    docs: List[Dict[str, Any]] = []
    entry = case_map.get(case_id) if isinstance(case_map, dict) else None
    if not entry:
        # case_map might key by dockets (2:26-cv-00123) instead of fed:...
        for k, v in case_map.items():
            if k.endswith(case_id.split(":")[-1]) or case_id.endswith(k):
                entry = v
                break

    if not entry:
        return docs

    for kind in ("orders", "filings", "mtds", "oppositions", "court_notices",
                 "drafts", "exhibits", "documents"):
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
    return docs


def _read_text(path: Path, max_chars: int = 200_000) -> str:
    if not path.exists():
        return ""
    try:
        if path.suffix.lower() == ".pdf":
            # Use doc_reader if available
            dr = _safe_import("pipeline.doc_reader", ["read_pdf", "read_document", "read"])
            for fn_name in ("read_document", "read_pdf", "read"):
                fn = dr.get(fn_name)
                if fn:
                    try:
                        txt = fn(str(path))
                        if isinstance(txt, str):
                            return txt[:max_chars]
                    except Exception:
                        pass
            return ""
        return path.read_text(encoding="utf-8", errors="ignore")[:max_chars]
    except Exception as e:
        log.debug("read %s failed: %s", path, e)
        return ""


# ---------------------------------------------------------------------------
# Per-order scan + Agent 10
# ---------------------------------------------------------------------------
# === BEEZLE ROLE-AWARE AUDIT PATCH ===
def _scan_and_classify(text: str, source_doc: str = "document", role: str = "unknown") -> Dict[str, Any]:
    """
    Patched: correctly destructures scan_order's dict return, passes role.
    Returns a uniform dict with real violation entries (not field-name dicts).
    """
    jos = _safe_import("pipeline.judicial_order_scanner",
                       ["scan_order", "JudicialViolation"])

    scan_result: Dict[str, Any] = {}
    if jos.get("scan_order") and text:
        try:
            # scan_order returns a DICT (not a list)
            scan_result = jos["scan_order"](
                text,
                source_doc=source_doc,
                role=role,
                enrich_db=False,  # skip DB enrichment in bulk audit for speed
            ) or {}
            if not isinstance(scan_result, dict):
                scan_result = {}
        except Exception as e:
            log.debug("scan_order failed for %s: %s", source_doc, e)
            scan_result = {}

    # Extract real violations list from the scan_result dict
    violations = scan_result.get("violations", []) or []
    if not isinstance(violations, list):
        violations = []

    serialized = []
    severity_counts = Counter()
    void_count = 0
    voidable_count = 0

    for v in violations:
        if hasattr(v, "to_dict") and callable(v.to_dict):
            d = v.to_dict()
        elif hasattr(v, "__dict__"):
            d = dict(v.__dict__)
        elif isinstance(v, dict):
            d = v
        else:
            continue  # skip non-violation entries

        sev = (d.get("severity") or d.get("level") or "").lower()
        severity_counts[sev or "unknown"] += 1
        if sev in ("constitutional", "void") or d.get("void") is True:
            void_count += 1
        elif sev in ("fatal", "voidable") or d.get("voidable") is True:
            voidable_count += 1
        serialized.append(d)

    return {
        "violations":      serialized,
        "violation_count": len(serialized),
        "void":            void_count,
        "voidable":        voidable_count,
        "severity_counts": dict(severity_counts),
        "role":            role,
        "source_doc":      source_doc,
    }
# === END BEEZLE ROLE-AWARE AUDIT PATCH ===


def audit_case(case_id: str, runner: Optional[ChainRunner] = None) -> Dict[str, Any]:
    """Audit one case: scan all orders, feed into Agent 10 per order."""
    case_map = _load_case_map()
    registry = _load_registry()
    docs     = _resolve_case_documents(case_id, case_map)

    log.info("case %s: %d documents", case_id, len(docs))

    runner = runner or ChainRunner(vault=False, log_each=False)

    per_doc_reports: List[Dict[str, Any]] = []
    total_violations = 0
    total_void       = 0
    total_voidable   = 0

    # --- BEEZLE: load case context for doc classification ---
    case_defendants = []
    case_plaintiff = None
    try:
        reg_entry = registry.get(case_id) or {}
        case_defendants = reg_entry.get("defendants") or []
        if case_defendants and isinstance(case_defendants[0], dict):
            case_defendants = [x.get("name", "") for x in case_defendants]
        case_plaintiff = reg_entry.get("plaintiff") or reg_entry.get("plaintiff_name")
        # Also try ground truth yaml
        if not case_defendants or not case_plaintiff:
            import yaml as _yaml
            slug = re.sub(r"^fed:\d+:", "fed_", case_id).replace(":", "_")
            gt_p = ROOT / "obsidian_sync" / "cases" / slug / "meta" / "CASE_GROUND_TRUTH.yaml"
            if gt_p.exists():
                gt = _yaml.safe_load(gt_p.read_text(encoding="utf-8")) or {}
                if not case_defendants:
                    defs = gt.get("defendants") or gt.get("parties", {}).get("defendants") or []
                    if defs and isinstance(defs[0], dict):
                        defs = [x.get("name", "") for x in defs]
                    case_defendants = defs
                if not case_plaintiff:
                    p = gt.get("plaintiff") or gt.get("parties", {}).get("plaintiff")
                    if isinstance(p, dict):
                        p = p.get("name")
                    case_plaintiff = p
    except Exception as _e:
        log.debug("case context load failed: %s", _e)

    # Import classifier lazily
    try:
        import sys as _sys
        if str(ROOT) not in _sys.path:
            _sys.path.insert(0, str(ROOT))
        from doc_role_classifier import classify_doc_with_evidence as _classify
    except Exception as _e:
        log.warning("doc_role_classifier not available: %s; defaulting role=unknown", _e)
        _classify = lambda p, **kw: {"role": "unknown", "matched_marker": "", "confidence": "low", "reason": "classifier unavailable"}

    for d in docs:
        path = Path(d["path"])
        text = _read_text(path)
        if not text:
            continue

        # Classify document role
        role_ev = _classify(
            str(path),
            defendants=case_defendants,
            plaintiff_name=case_plaintiff,
        )
        role = role_ev.get("role", "unknown")
        d["role"] = role
        d["role_confidence"] = role_ev.get("confidence")
        d["role_matched_marker"] = role_ev.get("matched_marker")

        scan = _scan_and_classify(text, source_doc=path.name, role=role)

        # Feed into Agent 10 only (immunity + criminal classification)
        try:
            state = runner.run(
                {
                    "text":           text[:50_000],
                    "case_number":    case_id,
                    "violation_type": "JUDICIAL_ORDER_AUDIT",
                    "document_path":  str(path),
                    "document_kind":  d.get("kind"),
                },
                case_id=case_id,
                only=[1, 10],
            )
            a10 = state.agent_json(10)
        except Exception as e:
            log.debug("agent_10 failed for %s: %s", path, e)
            a10 = {"error": str(e)}

        per_doc_reports.append({
            "path":   str(path),
            "kind":   d.get("kind"),
            "role":   role,
            "role_confidence": d.get("role_confidence"),
            "role_matched_marker": d.get("role_matched_marker"),
            "scan":   scan,
            "agent10": a10,
        })
        total_violations += scan["violation_count"]
        total_void       += scan["void"]
        total_voidable   += scan["voidable"]

    # Summary
    cat_counter = Counter()
    for r in per_doc_reports:
        for v in r["scan"]["violations"]:
            c = v.get("category") or v.get("type") or v.get("violation_type") or "unknown"
            cat_counter[str(c)] += 1

    # Per-role aggregations (judge / opposing_counsel / plaintiff / unknown)
    by_role = Counter()
    violations_by_role = Counter()
    void_by_role = Counter()
    voidable_by_role = Counter()
    for r in per_doc_reports:
        rrole = r.get("role") or r.get("scan", {}).get("role") or "unknown"
        by_role[rrole] += 1
        violations_by_role[rrole] += r["scan"].get("violation_count", 0)
        void_by_role[rrole] += r["scan"].get("void", 0)
        voidable_by_role[rrole] += r["scan"].get("voidable", 0)

    adversary_docs = by_role.get("judge", 0) + by_role.get("opposing_counsel", 0)
    adversary_violations = violations_by_role.get("judge", 0) + violations_by_role.get("opposing_counsel", 0)
    adversary_void = void_by_role.get("judge", 0) + void_by_role.get("opposing_counsel", 0)
    adversary_voidable = voidable_by_role.get("judge", 0) + voidable_by_role.get("opposing_counsel", 0)

    summary = {
        "generated":        datetime.now().isoformat(),
        "case_id":          case_id,
        "case_title":       (registry.get(case_id) or {}).get("title"),
        "documents_scanned": len(per_doc_reports),
        "total_violations": total_violations,
        "void_orders":      total_void,
        "voidable_orders":  total_voidable,
        "by_category":      dict(cat_counter),
        # BEEZLE: role-aware aggregates
        "by_role": {
            "docs":       dict(by_role),
            "violations": dict(violations_by_role),
            "void":       dict(void_by_role),
            "voidable":   dict(voidable_by_role),
        },
        "adversary_summary": {
            "documents":       adversary_docs,
            "violations":      adversary_violations,
            "void_orders":     adversary_void,
            "voidable_orders": adversary_voidable,
        },
        "reports":          per_doc_reports,
    }

    # Persist
    safe_cid = re.sub(r"[^A-Za-z0-9_-]", "_", case_id)
    out_dir = AUDIT_DIR / safe_cid
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "audit.json").write_text(
        json.dumps(summary, indent=2, default=str), encoding="utf-8"
    )
    (out_dir / "SUMMARY.md").write_text(_render_summary_md(summary), encoding="utf-8")
    log.info("case %s audit: %d docs, %d violations (%d void, %d voidable)",
             case_id, len(per_doc_reports), total_violations, total_void, total_voidable)
    return summary


def _render_summary_md(s: Dict[str, Any]) -> str:
    lines = [
        f"# Case Audit — {s.get('case_title') or s['case_id']}",
        f"- case_id: `{s['case_id']}`",
        f"- generated: {s['generated']}",
        f"- documents scanned: {s['documents_scanned']}",
        f"- total violations: **{s['total_violations']}**",
        f"- void orders: **{s['void_orders']}**",
        f"- voidable orders: **{s['voidable_orders']}**",
        "",
        "## Violations by category",
    ]
    for cat, n in sorted(s["by_category"].items(), key=lambda x: -x[1]):
        lines.append(f"- {cat}: {n}")
    lines += ["", "## Per-document breakdown"]
    for r in s["reports"]:
        lines.append(
            f"- `{r['kind']}` {Path(r['path']).name}: "
            f"{r['scan']['violation_count']} violations "
            f"(void={r['scan']['void']}, voidable={r['scan']['voidable']})"
        )
    return "\n".join(lines)


def audit_all() -> Dict[str, Any]:
    registry = _load_registry()
    out = {}
    runner = ChainRunner(vault=False, log_each=False)
    for cid in registry.keys():
        if not cid:
            continue
        try:
            out[cid] = audit_case(cid, runner=runner)
        except Exception as e:
            log.error("audit %s failed: %s", cid, e)
            out[cid] = {"error": str(e)}
    master_path = AUDIT_DIR / "MASTER_AUDIT.json"
    master_path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    log.info("wrote master audit: %s", master_path)
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--case", type=str, default=None)
    p.add_argument("--all",  action="store_true")
    args = p.parse_args()

    if args.all:
        audit_all()
    elif args.case:
        audit_case(args.case)
    else:
        p.error("specify --case <id> or --all")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
