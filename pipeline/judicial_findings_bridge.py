"""
pipeline/judicial_findings_bridge.py
=====================================
Bridge between the judicial_order_scanner module and the scorecard.

The judicial_order_scanner detects PRIOR_RESTRAINT_FILING_BAN,
DO_NOT_FILE_LANGUAGE, immunity waivers, due process violations, etc.
in judge-authored orders. But its output is structured differently from
block14's RULE_VIOLATION_PATTERNS — it goes into a separate audit chain
and never makes it into the scorecard's by_file / violations lists.

This bridge:
  1. Walks every JUDGE-classified document in the case docket
  2. Runs the judicial_order_scanner on each
  3. Converts each finding to a block14-shaped violation dict
  4. Returns the merged list for the scorecard

PUBLIC API
----------
collect_judicial_findings(case_id) -> List[Dict]

Each finding shape:
  {
    "violation_type": str (matches block14 keys),
    "severity": "FATAL" | "SERIOUS" | "STANDARD",
    "rule": str,
    "source_doc": str,
    "quote": str,
    "derived": False,  # these are directly observed, not inferred
    "from_scanner": True,
  }
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import re
import json
import logging
from pathlib import Path
from typing import Dict, List, Any

log = logging.getLogger("judicial_findings_bridge")

# Cross-platform root resolution (WSL/Linux uses /mnt/c, native Windows uses C:\)
import sys as _sys

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
ROOT = _eroot() if _sys.platform != "win32" else _BEEZLE_ROOT


def _to_native_path(p: str) -> str:
    """Convert Windows path (C:\…) to current-platform path.

    On WSL/Linux, rewrites <home>\... → <home>/... and
    forward-slashes the backslashes. On native Windows, returns p unchanged.
    """
    if _sys.platform == "win32":
        return p
    if not isinstance(p, str):
        return p
    if len(p) >= 3 and p[1:3] == ":\\" or (len(p) >= 2 and p[1] == ":"):
        drive = p[0].lower()
        rest = p[2:].lstrip("\\/").replace("\\", "/")
        return f"/mnt/{drive}/{rest}"
    return p.replace("\\", "/")


# Scanner finding category → block14 violation_type
SCANNER_TO_BLOCK14 = {
    "PRIOR_RESTRAINT_FILING_BAN":    "PRIOR_RESTRAINT_FILING_BAN",
    "DO_NOT_FILE_LANGUAGE":          "DO_NOT_FILE_LANGUAGE",
    "EX_PARTE_RULING":               "EX_PARTE_RULING",
    "DENIED_OPPORTUNITY_TO_RESPOND": "DENIED_OPPORTUNITY_TO_RESPOND",
    "BIAS_JUDICIAL":                 "BIAS_JUDICIAL",
    "VOID_JUDGMENT_RULE_60B4":       "VOID_JUDGMENT_RULE_60B4",
}


def _safe_cid(case_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", case_id)


def collect_judicial_findings(case_id: str) -> List[Dict[str, Any]]:
    """Walk the docket, run judicial_order_scanner on judge docs, return
    block14-shaped violations.
    """
    # Load case_map
    case_map_path = ROOT / "cases_index" / "case_map.json"
    if not case_map_path.exists():
        return []
    case_map = json.loads(case_map_path.read_text(encoding="utf-8"))
    entry = case_map.get(case_id)
    if not entry:
        for k, v in case_map.items():
            if k.endswith(case_id.split(":")[-1]):
                entry = v; break
    if not entry:
        return []

    # Gather doc paths (convert Windows paths to current platform)
    docs = []
    for kind in ("orders", "filings", "court_notices", "documents", "motions_to_dismiss"):
        bucket = entry.get(kind) or []
        if isinstance(bucket, dict):
            bucket = list(bucket.values())
        if isinstance(bucket, list):
            for d in bucket:
                if isinstance(d, str):
                    docs.append(_to_native_path(d))
                elif isinstance(d, dict):
                    p = d.get("path") or d.get("file") or d.get("filepath")
                    if p: docs.append(_to_native_path(p))

    # Intake-folder fallback: case_map.json may be missing recently-added files.
    # Walk obsidian_sync/cases/<slug>/intake/ for files matching JUDGE_FILENAME_KEYWORDS
    # below and union them into docs[]. This is read-only — does NOT modify case_map.json.
    try:
        slug = case_id.split(":")[-1]  # e.g. "26-cv-00456"
        # Try standard slug variants
        slug_candidates = [
            f"fed_{slug}",
            f"fed_{slug.replace('cv-', 'cv-')}",
            slug,
        ]
        # If case_id is phila_ccp:NNN, slug variants differ
        if case_id.startswith("phila_ccp:"):
            slug_candidates = [f"phila_ccp_{slug}", slug]
        intake_dir = None
        for cand in slug_candidates:
            test = ROOT / "obsidian_sync" / "cases" / cand / "intake"
            if test.exists():
                intake_dir = test
                break
        if intake_dir:
            existing_names = {Path(d).name.lower() for d in docs}
            for f in intake_dir.iterdir():
                if not f.is_file():
                    continue
                if f.suffix.lower() not in (".pdf", ".docx", ".htm", ".html", ".txt"):
                    continue
                if f.name.lower() in existing_names:
                    continue
                docs.append(_to_native_path(str(f)))
    except Exception as e:
        log.debug("intake-folder fallback failed: %s", e)

    # Classifier
    try:
        from pipeline.document_role_classifier import classify_doc_with_evidence  # BEEZLE_CLASSIFY_IMPORT_FIX_V1
    except Exception:
        classify_doc_with_evidence = None

    # Filename-pattern fast-path: any of these strings in path → treat as judge order.
    # This catches Amerisave/StateFarm dismissal orders that doc_role_classifier
    # mis-tags as opposing_counsel, plus all the "ecf_NN.pdf" docket-order files.
    JUDGE_FILENAME_KEYWORDS = [
        # Generic judicial keywords
        "memorandum", "judges memorandum", "judge order", "judges order",
        "original dismissal", "dismissal order", "order dated",
        "rejected motion to vacat", "rejected motion to recuse",
        "appeal order", "judicial order", "court order",
        "judges order", "court ordered", "set_reset scheduling",
        "order (memorandum", "order on motion", "usca_order",
        # 02822 (TransUnion FCRA)
        "ecf 16", "ecf 21", "ecf 37", "ecf 41", "ecf 54", "ecf 67",
        "ecf 98", "ecf 103", "ecf 112", "ecf 122", "ecf 126", "ecf 130",
        "ecf 141",
        # 04767 (Amerisave/quiet title) — show_temp ecf 39/40, dkt 21-30, dkt_54
        "ecf 39", "ecf 40",
        "dkt 21", "dkt 22", "dkt 24", "dkt 25", "dkt 26", "dkt 27",
        "dkt 29", "dkt 30", "dkt 40", "dkt 41", "dkt_54", "dkt 54",
        "show_temp ecf",
    ]
    def _is_judge_by_filename(p: str) -> bool:
        pl = p.lower().replace("\\", "/")
        return any(kw in pl for kw in JUDGE_FILENAME_KEYWORDS)

    judge_docs = []
    for d in docs:
        # Try filename override first — overrides mis-classification.
        if _is_judge_by_filename(d):
            judge_docs.append(d)
            continue
        if classify_doc_with_evidence:
            try:
                role = classify_doc_with_evidence(d).get("role", "unknown")
            except Exception:
                role = "unknown"
            if role == "judge":
                judge_docs.append(d)
        else:
            # heuristic fallback (kept for environments without classifier)
            dl = d.lower()
            if any(kw in dl for kw in ["memorandum", "order_2024", "order_2025",
                                          "order_2026", "ecf_141", "judges"]):
                judge_docs.append(d)

    log.info("bridge: %d judge docs to scan", len(judge_docs))

    # Read text + apply scanner
    try:
        from pipeline.doc_reader import extract_text
    except Exception:
        return []
    try:
        # Real export is scan_order (per pipeline/judicial_order_scanner.py line 919).
        # Alias to the local name used below to preserve the rest of the flow.
        from pipeline.judicial_order_scanner import scan_order as scan_judicial_order
    except Exception as _imp_err:
        log.warning("judicial_order_scanner unavailable, using inline fallback: %s", _imp_err)
        scan_judicial_order = None

    results: List[Dict[str, Any]] = []
    for d in judge_docs:
        path = Path(d)
        if not path.exists():
            continue
        try:
            text = extract_text(str(path)) or ""
        except Exception:
            continue
        if not text:
            continue

        if scan_judicial_order:
            try:
                scan = scan_judicial_order(text, source_doc=path.name, enrich_db=True)  # TurboVec semantic enrichment (real authority, similarity-floored)
                # scanner returns a list of findings or a dict with 'violations'
                if isinstance(scan, dict):
                    findings = scan.get("violations") or []
                elif isinstance(scan, list):
                    findings = scan
                else:
                    findings = []
            except Exception as e:
                log.debug("scanner failed on %s: %s", path.name, e)
                findings = []
        else:
            findings = _inline_scan(text, path.name)

        for f in findings:
            # JudicialViolation.to_dict() emits violation_id; legacy callers
            # used violation_type / type. Try all three.
            if hasattr(f, "to_dict"):
                f = f.to_dict()
            vt = (f.get("violation_id") or f.get("violation_type")
                  or f.get("type", ""))
            if not vt:
                continue
            # Pass-through: if vt is in the legacy map, use the mapped label;
            # otherwise emit vt verbatim so the scorecard still sees the finding.
            mapped = SCANNER_TO_BLOCK14.get(vt, vt)
            _enr = f.get("db_enrichment") or {}
            results.append({
                "violation_type": mapped,
                "severity":       f.get("severity", "SERIOUS"),
                "rule":           f.get("rule") or f.get("rule_citation") or "",
                "source_doc":     path.name,
                "quote":          (f.get("quote") or f.get("text") or "")[:250],
                "derived":        False,
                "from_scanner":   True,
                "category":       f.get("category", ""),
                "display_name":   f.get("display_name", ""),
                "constitutional_basis": f.get("constitutional_basis", ""),
                # carry TurboVec enrichment through (was dropped in conversion)
                "db_enrichment":  _enr,
                "supporting_authorities": _enr.get("supporting_authorities", []),
            })

        # Also run our inline scan for the four new Dkt141 patterns,
        # regardless of whether the scanner module fires
        inline = _inline_scan(text, path.name)
        for f in inline:
            results.append(f)

    log.info("bridge: collected %d findings", len(results))
    return results


# ---------------------------------------------------------------------------
# Inline scanner — uses our definitions module for triggers
# ---------------------------------------------------------------------------
def _inline_scan(text: str, source_doc: str) -> List[Dict[str, Any]]:
    """Fallback scanner using the definitions module's regex triggers.
    Catches the four Dkt141-specific patterns even if the judicial_order_scanner
    module is unavailable or its output shape varies.
    """
    try:
        from pipeline.definitions import DEFINITIONS
    except Exception:
        return []

    findings: List[Dict[str, Any]] = []
    # Walk each definition that has regex_triggers
    for def_key, d in DEFINITIONS.items():
        triggers = d.get("regex_triggers", [])
        if not triggers:
            continue
        for pat in triggers:
            for m in re.finditer(pat, text, re.IGNORECASE | re.MULTILINE):
                # Build a snippet
                start = max(0, m.start() - 50)
                end = min(len(text), m.end() + 120)
                snippet = text[start:end].replace("\n", " ").strip()
                if len(snippet) > 200:
                    snippet = snippet[:197] + "..."
                # Map to block14 violation type
                vt = _def_key_to_violation_type(def_key)
                if not vt:
                    continue
                findings.append({
                    "violation_type": vt,
                    "severity": "FATAL" if d.get("void_inducing") else "SERIOUS",
                    "rule": d.get("authority", "")[:120],
                    "source_doc": source_doc,
                    "quote": snippet,
                    "derived": False,
                    "from_scanner": True,
                })
                break  # one finding per pattern per doc
    return findings


def _def_key_to_violation_type(def_key: str) -> str:
    return {
        "sua_sponte_ruling":              "SUA_SPONTE_RULING",
        "prior_restraint_filing_ban":     "PRIOR_RESTRAINT_FILING_BAN",
        "sovereign_citizen_label":        "SOVEREIGN_CITIZEN_LABEL",
        "bill_of_attainder_mislabel":     "BILL_OF_ATTAINDER_MISLABEL",
        "immunity_waiver_fraud_on_court": "IMMUNITY_WAIVER_TRIGGERED",
        "color_of_law_deprivation":       "DEPRIVATION_OF_RIGHTS_COLOR_OF_LAW",
        "procedural_due_process":         "PROCEDURAL_DUE_PROCESS_VIOLATION",
    }.get(def_key, "")


if __name__ == "__main__":
    import sys
    case_id = sys.argv[1] if len(sys.argv) > 1 else "fed:2:26-cv-00123"
    findings = collect_judicial_findings(case_id)
    print(f"\nJudicial findings for {case_id}: {len(findings)}")
    print("=" * 70)
    # Count by type
    by_type = {}
    for f in findings:
        by_type[f["violation_type"]] = by_type.get(f["violation_type"], 0) + 1
    for vt, n in sorted(by_type.items(), key=lambda kv: -kv[1]):
        print(f"  {vt:<40} {n}")
    print()
    print("First 10 entries:")
    for f in findings[:10]:
        print(f"  [{f['severity']:<7}] {f['violation_type']:<35} {f['source_doc'][:30]}")
        if f.get("quote"):
            print(f"            quote: {f['quote'][:100]}")


# ---------------------------------------------------------------------------
# Opposing-counsel pass (Section H) — added by _patch_counsel_findings.py
# ---------------------------------------------------------------------------
# BEEZLE_FN_UNUSED 2026-06-13: collect_counsel_findings (no static refs in pipeline/)
def collect_counsel_findings(case_id: str):
    """Emit LAWYER_RULE_VIOLATION findings from opposing-counsel filings that
    cite >=5 non-binding authorities as controlling (Pa. R.P.C. 3.3 candor).
    Reads the pre-extracted audit scorecard.violations (not a live rescan).
    Only opposing_counsel docs (role-classified) — never plaintiff's own."""
    import json as _json, re as _re
    from pathlib import Path as _P
    from collections import defaultdict as _dd

    RPC_THRESHOLD = 5

    safe = case_id.replace(":", "_").replace("/", "_")
    base = _P(__file__).resolve().parent.parent
    audit = base / "output" / "audit" / f"data_{safe}.json"
    if not audit.exists():
        alt = base / "output" / "audit" / f"data_{safe.replace('fed_2_', 'fed_')}.json"
        audit = alt if alt.exists() else audit
    if not audit.exists():
        return []

    try:
        data = _json.loads(audit.read_text(encoding="utf-8"))
    except Exception:
        return []

    sv = (data.get("scorecard") or {}).get("violations") or []
    if not sv:
        return []

    def _basename(s):
        return str(s or "").replace("\\", "/").split("/")[-1]

    by_doc = _dd(list)
    for v in sv:
        src = v.get("source_file") or v.get("source_doc") or ""
        by_doc[_basename(src)].append(v)

    # role classifier — the authoritative opposing-counsel gate
    try:
        from pipeline.document_role_classifier import classify_doc_with_evidence  # BEEZLE_CLASSIFY_IMPORT_FIX_V1
    except Exception:
        classify_doc_with_evidence = None

    def _is_opposing_counsel(fname):
        if classify_doc_with_evidence:
            try:
                return classify_doc_with_evidence(fname).get("role") == "opposing_counsel"
            except Exception:
                pass
        # conservative fallback: defendant-name reply/MTD, explicitly NOT plaintiff opposition
        fl = fname.lower()
        if "opposition" in fl or "plaintiff" in fl:
            return False
        return any(t in fl for t in ["in support to its motion", "reply", "motion to dismiss", "mtd"])

    findings = []
    noted_docs = []
    for fname, vs in by_doc.items():
        if not _is_opposing_counsel(fname):
            continue
        nonbinding = [x for x in vs if (x.get("severity") or "").upper() in ("FATAL", "SERIOUS")]
        n = len(nonbinding)
        if n == 0:
            continue
        if n >= RPC_THRESHOLD:
            sample = "; ".join(x.get("citation", x.get("evidence", "")) for x in nonbinding[:5] if x.get("citation") or x.get("evidence"))
            findings.append({
                "violation_type": "LAWYER_RULE_VIOLATION",
                "severity": "SERIOUS",
                "rule": "Pa. R.P.C. 3.3 (Candor Toward the Tribunal); Pa. R.P.C. 3.1 (Meritorious Claims)",
                "source_doc": fname,
                "quote": (f"Opposing counsel's filing cites {n} facially non-binding "
                          f"authorities while purporting to establish the controlling "
                          f"legal standard. R.P.C. 3.3(a)(2) requires disclosure of "
                          f"controlling adverse authority."),
                "derived": True,
                "from_scanner": False,
                "from_counsel_pass": True,
                "evidence": [
                    f"{n} non-binding citations in opposing-counsel filing",
                    f"Sample: {sample[:120]}" if sample else "(citation detail in audit data)",
                    "R.P.C. 3.3 requires candor as to controlling authority",
                    f"Threshold for R.P.C. 3.3 inference: {RPC_THRESHOLD}+ non-binding in one brief",
                ],
            })
        else:
            noted_docs.append((fname, n))

    # one non-accusatory note for sub-threshold counsel docs
    if noted_docs:
        detail = "; ".join(f"{fn} ({c})" for fn, c in noted_docs)
        findings.append({
            "violation_type": "LAWYER_RULE_VIOLATION",
            "severity": "STANDARD",
            "rule": "Observation — below R.P.C. 3.3 candor threshold",
            "source_doc": "(multiple opposing-counsel filings)",
            "quote": (f"Additional opposing-counsel filings cited non-binding authority "
                      f"below the {RPC_THRESHOLD}-cite candor threshold (noted, not charged): {detail}"),
            "derived": True,
            "from_scanner": False,
            "from_counsel_pass": True,
            "noted_only": True,
            "evidence": [f"Sub-threshold non-binding counsel filings: {detail}"],
        })

    return findings
