"""
pipeline/audit_writer.py
=========================

Live audit data writer.

Runs every pipeline call. Refreshes procedural violations by scanning
opposing filings via block14_pinpointer.scan_document_for_violations.
Preserves existing stare-decisis data already in the audit JSON.

Output: output/audit/data_<safe_case_id>.json with merged shape:
    {
        "case_id": "...",
        "case_court": "...",
        "title": "...",
        "scorecard": { "violations": [...] }    ← preserved if present (stare-decisis)
        "procedural_violations": [...]          ← NEW, refreshed every run
        "audit_generated": "<iso ts>"
    }
"""
from __future__ import annotations
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
import json
from datetime import datetime
import re
from pathlib import Path

from core.logger import get_logger

log = get_logger("audit_writer")

AUDIT_DIR = (_BEEZLE_ROOT / "output" / "audit")
# CANONICAL map is cases_index/; the legacy cases/_consolidated/ path no longer
# exists, so reading it left case_info empty and WIPED procedural_violations.
CASE_MAP_PATH = (_BEEZLE_ROOT / "cases_index" / "case_map.json")
if not CASE_MAP_PATH.exists():
    CASE_MAP_PATH = (_BEEZLE_ROOT / "cases" / "_consolidated" / "case_map.json")


def _to_wsl(p):
    """case_map stores Windows paths (C:\\...). Under WSL, Path('C:\\..') does
    not resolve, so opposing filings were silently skipped -> 0 procedural.
    Convert to /mnt/c/... when running under WSL; leave untouched on Windows."""
    import re as _re
    s = str(p).replace("\\", "/")
    if _re.match(r"^[A-Za-z]:/", s) and Path("/mnt").exists():
        return "/mnt/" + s[0].lower() + s[2:]
    return s


def _safe_id(case_id: str) -> str:
    """Convert canonical case_id to a filesystem-safe slug."""
    return case_id.replace(":", "_").replace("/", "_")


def _resolve_case_in_map(case_id: str, case_map: dict) -> str | None:
    """Find this case_id's key in case_map.json."""
    if not case_map:
        return None
    docket = case_id.split(":", 1)[-1] if ":" in case_id else case_id
    for ckey, cinfo in case_map.items():
        cm_id = str(cinfo.get("case_id", ""))
        if docket and (docket in cm_id or docket in ckey):
            return ckey
        if case_id == cm_id or case_id == ckey:
            return ckey
    return None


def _scan_opposing_filings(case_info: dict, court: str, case_slug: str = "") -> list[dict]:
    """Run block14_pinpointer on every opposing filing in this case.

    Returns list of dicts (ViolationPin.to_dict() output) with extra
    fields: file (basename), doc_category.

    PULL FIX (2026-07-31): case_map's static doc lists miss NEW docket documents that docket_email_fetch
    drops into the case INTAKE (e.g. a just-entered memorandum/opinion). When case_slug is given, the intake
    dir's court documents are also scanned so the audit reflects the LATEST orders — otherwise m0 drafts off
    a stale record (it cited old cases instead of the memorandum's).
    """
    try:
        from pipeline.block14_pinpointer import scan_document_for_violations
        from pipeline.judicial_order_scanner import scan_order as _scan_judicial
        from pipeline.doc_reader import extract_text
    except Exception as e:
        log.error("audit_writer", f"detector import failed: {e}")
        return []

    # === RECIPROCITY_AUDIT_V1 (2026-08-25) — LLM comprehension layer ===
    # The flip block14's regex cannot catch: a rule invoked against the pro se
    # party while the enforcing filer is excused from the identical showing.
    # Fail-safe: if the LLM/reasoning layer is unavailable, _recip_scan
    # returns [] and the deterministic scan proceeds unchanged.
    def _recip_scan(text: str, doc_name: str) -> list:
        try:
            from pipeline.reasoning_layer import (
                ReasoningEngine, reciprocity_asymmetries_to_violations)
            eng = ReasoningEngine()
            tr = eng.run_audit("reciprocity_audit", doc_name, text, case_slug or "")
            asyms = (tr.filtered_result or {}).get("asymmetries", [])
            return reciprocity_asymmetries_to_violations(asyms, source_doc=doc_name)
        except Exception as _re:
            log.warning(f"reciprocity scan unavailable on {doc_name}: {_re}")
            return []
    # === END RECIPROCITY_AUDIT_V1 ===

    results = []
    doc_categories = ["orders", "motions_to_dismiss", "oppositions",
                      "court_notices", "answers", "briefs"]

    for doc_key in doc_categories:
        for fpath_str in (case_info.get(doc_key) or []):
            fp = Path(_to_wsl(fpath_str))
            if not fp.exists() or fp.stat().st_size < 100:
                continue
            try:
                text = extract_text(fp)
                if not text or len(text) < 200:
                    continue
                vios = scan_document_for_violations(
                    text, fp.stem[:60], court=court,
                )
                for v in vios:
                    d = v.to_dict()
                    d["file"] = fp.name
                    d["doc_category"] = doc_key
                    results.append(d)
                # RECIPROCITY_AUDIT_V1: LLM asymmetry pass on the same document
                for rd in _recip_scan(text, fp.stem[:60]):
                    rd["file"] = fp.name
                    rd["doc_category"] = doc_key
                    results.append(rd)
                # Also scan via judicial_order_scanner for constitutional/procedural violations
                try:
                    judicial_vios = _scan_judicial(
                        text=text,
                        source_doc=fp.name,
                        role="plaintiff",
                        enrich_db=False,
                    )
                    if judicial_vios.get("violations"):
                        for jv in judicial_vios["violations"]:
                            jv["file"] = fp.name
                            jv["doc_category"] = doc_key + "_judicial"
                            results.append(jv)
                except Exception as _jv_e:
                    log.warning(f"judicial_order_scanner failed on {fp.name}: {_jv_e}")
            except Exception as e:
                log.warning(f"scan failed on {fp.name}: {e}")

    # INTAKE FOLD-IN: also scan the case intake's court documents (the docket_email_fetch downloads), which
    # case_map does not list. Targets order/opinion/memorandum text; runs the same block14 + judicial scanners.
    if case_slug:
        idir = _resolve_intake_dir(case_slug)
        if idir:
            _seen = {r.get("file") for r in results}
            _court_doc = re.compile(r"(dkt[_ ]?\d+|order|memorand|opinion|judgment|ruling)", re.I)
            for fp in sorted(idir.glob("*.txt")):
                if not _court_doc.search(fp.name) or fp.name in _seen:
                    continue
                if not fp.exists() or fp.stat().st_size < 100:
                    continue
                try:
                    text = fp.read_text(encoding="utf-8", errors="ignore")
                    if not text or len(text) < 200:
                        continue
                    for v in scan_document_for_violations(text, fp.stem[:60], court=court):
                        d = v.to_dict(); d["file"] = fp.name; d["doc_category"] = "intake_orders"
                        results.append(d)
                    # RECIPROCITY_AUDIT_V1: LLM asymmetry pass on intake court docs
                    for rd in _recip_scan(text, fp.stem[:60]):
                        rd["file"] = fp.name; rd["doc_category"] = "intake_orders"
                        results.append(rd)
                    try:
                        jv = _scan_judicial(text=text, source_doc=fp.name, role="plaintiff", enrich_db=False)
                        # _scan_judicial returns a dict normally but a LIST on
                        # role_guard skip paths — normalize both shapes
                        _jlist = (jv.get("violations") or []) if isinstance(jv, dict) \
                            else (jv or [])
                        for j in _jlist:
                            if not isinstance(j, dict):
                                continue
                            j["file"] = fp.name; j["doc_category"] = "intake_orders_judicial"
                            results.append(j)
                    except Exception as _je:
                        log.warning(f"intake judicial scan failed on {fp.name}: {_je}")
                except Exception as _ie:
                    log.warning(f"intake scan failed on {fp.name}: {_ie}")

    # ── INSTRUMENT COMPARATOR (cross-production; deterministic) ──
    # Single-doc scans cannot see two productions of the same note disagreeing
    # (e.g. allonge present in one, absent in the enforcer's own packet).
    if case_slug:
        try:
            from pipeline.instrument_comparator import scan_case_intake as _ic_scan
            _idir = _resolve_intake_dir(case_slug)
            if _idir:
                for _c in _ic_scan(_idir):
                    _c["file"] = "cross-production"
                    _c["doc_category"] = "instrument_comparison"
                    results.append(_c)
        except Exception as _ic_e:
            log.warning(f"instrument comparator failed: {_ic_e}")
        try:
            from pipeline.case_fact_comparator import scan_case_facts as _cf_scan
            _idir = _resolve_intake_dir(case_slug)
            if _idir:
                _extra = [d for d in _idir.glob("*/") if d.is_dir()]
                for _c in _cf_scan(_idir, case_slug, extra_dirs=_extra):
                    _c["file"] = "cross-filing"
                    _c["doc_category"] = "fact_comparison"
                    results.append(_c)
        except Exception as _cf_e:
            log.warning(f"case-fact comparator failed: {_cf_e}")

    # ── STATEMENT LEDGER (memory organ; deterministic) ──
    # Captures EVERY statement (roles, facts, court directives, claims, chat
    # findings) with authorship, and computes the UNREBUTTED queue — adverse
    # statements no pro-se filing has answered. m0 reads the queue; the user
    # never has to point at an unanswered order again.
    try:
        from pipeline.statement_ledger import write_ledger as _sl_write
        # organs below live on obsidian_sync/cases/<slug>; the case id
        # (fed:2:26-cv-00456) is NOT the dir name (fed_26-cv-00456). Match by
        # docket tail against the organ's own CASES_DIR — deterministic.
        from pipeline.statement_ledger import CASES_DIR as _SL_CASES
        _tail = case_slug.split(":")[-1]
        _dslug = case_slug
        _best = -1
        for _cd in sorted(_SL_CASES.iterdir()):
            if not (_cd.is_dir() and _cd.name.endswith(_tail)):
                continue
            # disambiguate: pick the candidate with the most intake sidecars
            _n = len(list((_cd / "intake").glob("*.txt")))                 if (_cd / "intake").is_dir() else 0
            if _n > _best:
                _best = _n
                _dslug = _cd.name
        _led = _sl_write(_dslug)
        # UNREBUTTED adverse statements JOIN THE VIOLATIONS STREAM m0 drafts
        # from (results -> procedural_violations -> data_<case>.json). The
        # ledger artifacts (.json/.md) are still written for the vault.
        for _u in _led.get("unrebutted", [])[:40]:
            results.append({
                "violation_type": "UNREBUTTED_ADVERSE_STATEMENT",
                "rule_citation": ("Fed. R. Evid. 801(d)(2); the record speaks "
                                  "unanswered \u2014 silence risks concession"),
                "rule_text": ("An adverse statement (court directive or "
                              "opponent claim) sits in the record with no "
                              "responsive pro-se filing detected."),
                "source_doc": _u.get("source", "")[:90],
                "file": _u.get("source", "")[:90],
                "doc_category": "statement_ledger",
                "quote": _u.get("text", "")[:300],
                "explanation": (f"[{_u.get('author')}/{_u.get('kind')}] adverse "
                                "statement UNREBUTTED on the record: it must be "
                                "answered, distinguished, or neutralized in the "
                                "next filing."),
                "fix": ("Answer it in the next filing: rebut with record proof, "
                        "distinguish, or move against it; then the ledger marks "
                        "it ADDRESSED."),
                "severity": "SERIOUS",
            })
    except Exception as e:
        log.warning(f"statement_ledger fold failed: {e}")

    # ── DEFINITION GATE (term-of-art vs colloquial; deterministic) ──
    try:
        from pipeline.definition_gate import scan_case as _dg_scan
        try:
            _dg_slug = _dslug
        except NameError:
            _dg_slug = case_slug
        _dgr = _dg_scan(_dg_slug)
        # definition pins are violation-shaped (DEFINITION_DIVERGENCE, hooked
        # in all mirrors) \u2014 they JOIN results so m0 drafts from them.
        for _dp in _dgr["pins"][:80]:
            _dp["file"] = _dp.get("source_doc", "")
            _dp["doc_category"] = "definition_gate"
            results.append(_dp)
    except Exception as e:
        log.warning(f"definition_gate fold failed: {e}")

    # ── PRINCIPLE SCANNERS (13 principle categories; deterministic) ──
    # Emitters for the principle-audit categories: rescission-ignored,
    # conditions-precedent, waiver/forfeiture, burden inversion, law-of-case,
    # candor (derived), statutory damages (derived). Pins JOIN results — the
    # m0 stream — never a side key. Sandbox-proven V13.
    if case_slug:
        try:
            from pipeline.principle_scanners import run_all as _ps_run
            try:
                _ps_slug = _dslug  # directory slug computed by the ledger fold
            except NameError:
                _ps_slug = case_slug
            for _pp in _ps_run(_ps_slug, results):
                _pp.setdefault("doc_category", "principle_scanners")
                results.append(_pp)
        except Exception as _ps_e:
            log.warning(f"principle scanners failed: {_ps_e}")

    # ── CANONS + MAXIMS (comprehension organ; deterministic) ──
    if case_slug:
        try:
            from pipeline.canons_maxims import run_all as _cm_run
            try:
                _cm_slug = _dslug
            except NameError:
                _cm_slug = case_slug
            for _cp in _cm_run(_cm_slug, results):
                results.append(_cp)
        except Exception as _cm_e:
            log.warning(f"canons_maxims failed: {_cm_e}")



    # ── CROSS-PARTY ROLE REGISTER (trick_catcher; deterministic) ──
    # Opposing parties' own statements of WHO IS WHAT (holder/owner/creditor/
    # servicer) cross-referenced per loan (aliases collapsed). Mutually
    # exclusive claims become CROSS_PARTY_ROLE_CONFLICT pins — contradictions
    # built from the parties' own words.
    if case_slug:
        try:
            from pipeline.trick_catcher import (
                extract_role_assertions as _ra, find_role_conflicts as _rc)
            _idir = _resolve_intake_dir(case_slug)
            if _idir:
                _asserts = []
                for _fp in sorted(_idir.glob("*.txt"))[:400]:
                    try:
                        if _fp.stat().st_size < 400:
                            continue
                        _asserts.extend(_ra(
                            _fp.read_text(encoding="utf-8", errors="replace"),
                            _fp.name[:80], case_slug))
                    except Exception:
                        continue
                for _c in _rc(_asserts):
                    _c["file"] = "cross-party"
                    _c["doc_category"] = "role_register"
                    results.append(_c)
        except Exception as _tc_e:
            log.warning(f"role register failed: {_tc_e}")

    return results


def _scan_intake_hierarchy(case_slug: str, court: str) -> list[dict]:
    """Stare-decisis (binding-hierarchy) audit of the case INTAKE's court documents. Folds the SCORECARD the
    same way _scan_opposing_filings folds procedural_violations — so a NEW memorandum's out-of-circuit cites
    (non-binding presented as controlling) reach the audit, else m0 cites the wrong cases. Agnostic; the
    scorecard builder (baseline_loop) scans only stale case_map paths and never sees new intake. Empty on any
    failure (additive, never fatal)."""
    out = []
    try:
        from pipeline.hierarchy_enforcer import HierarchyEnforcer
    except Exception as e:
        log.warning(f"hierarchy_enforcer import failed: {e}")
        return out
    idir = _resolve_intake_dir(case_slug)
    if not idir:
        return out
    he = HierarchyEnforcer(court)
    _court_doc = re.compile(r"(dkt[_ ]?\d+|order|memorand|opinion|judgment|ruling)", re.I)
    for fp in sorted(idir.glob("*.txt")):
        if not _court_doc.search(fp.name):
            continue
        try:
            for v in he.audit_file(str(fp)):
                d = v.to_dict()
                d["file"] = fp.name
                d["source_file"] = str(fp)
                d["category"] = d.get("violation_type")  # scorecard keys on 'category'
                out.append(d)
        except Exception as _he:
            log.warning(f"intake hierarchy scan failed on {fp.name}: {_he}")
    return out


def _dedup_with_counts(violations: list[dict]) -> list[dict]:
    """Group identical (violation_type, rule_citation, file) and add count."""
    seen = {}
    deduped = []
    for v in violations:
        key = (v.get("violation_type", ""), v.get("rule_citation", ""), v.get("file", ""))
        if key in seen:
            seen[key]["count"] = seen[key].get("count", 1) + 1
        else:
            entry = dict(v)
            entry["count"] = 1
            seen[key] = entry
            deduped.append(entry)
    return deduped


def _ingest_evidence_class(case_id: str) -> list[dict]:
    """Ingest scored evidence entries from intake/ documents.

    Scans the case's intake directory for:
    1. GNMA REMIC trust files (FDS/RLY/ANC/SRF/FRR/MTF) — score trust existence
    2. 1099-C returns — score against 26 U.S.C. § 6050P(a) filing requirements
    3. Servicing-transfer docs — score against 12 U.S.C. § 2605(b) notice requirements

    Returns a list of scored evidence dicts with schema:
        {
            "evidence_type": "gnma_remic" | "1099C" | "servicing_transfer",
            "file": "filename",
            "score": float (0.0 - 1.0),
            "severity": "FATAL" | "SERIOUS" | "MODERATE" | "MINOR",
            "rule_citation": "26 U.S.C. § 6050P(a)" | "12 U.S.C. § 2605(b)" | "GNMA REMIC",
            "violation_type": "string",
            "description": "string",
            "defendant": "string | null",
            "evidence_value": "string | null",
        }
    """
    # Remic file type codes: FDS, RLY, ANC, SRF, FRR, MTF
    REMIC_PATTERNS = ["FDS", "RLY", "ANC", "SRF", "FRR", "MTF"]
    REMIC_SCORE = 0.85  # Trust existence is high-value evidence for RESA claim

    # 1099-C threshold: $600+ cancellation of indebtedness triggers § 6050P
    _1099_C_THRESHOLD = 600.0

    # 12 U.S.C. § 2605(b) — notice must be given before servicing transfer
    # Missing notice = SERIOUS violation; notice given after transfer = FATAL violation

    # Resolve the case's intake directory
    case_slug = case_id.split(":", 1)[-1] if ":" in case_id else case_id
    intake_dir = _resolve_intake_dir(case_slug)
    if not intake_dir or not intake_dir.exists():
        log.debug(f"_ingest_evidence_class: no intake dir for {case_slug}")
        return []

    results = []
    try:
        files = [f for f in intake_dir.iterdir() if f.is_file()]
    except Exception as e:
        log.warning(f"_ingest_evidence_class: could not list {intake_dir}: {e}")
        return []

    # ─── 1. GNMA REMIC trust files ────────────────────────────────────────
    found_remic = []
    for f in files:
        fn = f.name.upper()
        for pat in REMIC_PATTERNS:
            if pat in fn:
                found_remic.append(f)
                break

    if found_remic:
        trust_id = _extract_remic_trust_id(found_remic)
        file_type_count = len(set(_classify_remic_file(f) for f in found_remic))
        # Score based on file type diversity (more types = stronger evidence of a real trust)
        score = min(1.0, 0.5 + (file_type_count / len(REMIC_PATTERNS)) * 0.5)
        results.append({
            "evidence_type": "gnma_remic",
            "file": found_remic[0].name,
            "score": score,
            "severity": "SERIOUS" if score >= 0.7 else "MODERATE",
            "rule_citation": "GNMA REMIC",
            "violation_type": "REMIC_TRUST_EXISTS",
            "description": f"GNMA REMIC trust {trust_id} exists ({file_type_count} file types). "
                           "Trust is collateralized by cancelled debts — evidence of RESA violation.",
            "defendant": None,
            "evidence_value": trust_id,
        })

    # ─── 2. 1099-C returns — score against 26 U.S.C. § 6050P(a) ──────────
    found_1099c = []
    for f in files:
        fn = f.name.upper()
        if "1099" in fn and ("C" in fn or "FORM 1099" in fn or "CANCELLATION" in fn):
            found_1099c.append(f)

    for f in found_1099c:
        # Determine severity by checking if the file has OCR text (indicates it's a real filed form)
        fn = f.name.upper()
        is_form = "FORM 1099" in fn or "1099" in fn and "C" in fn
        has_ocr = f.name.endswith(".ocr.txt") or ".ocr.txt" in f.name
        score = 0.5 if has_ocr else 0.3 if is_form else 0.15

        # Determine defendant from filename context
        defendant = _extract_defendant_from_filename(f.name)

        results.append({
            "evidence_type": "1099C",
            "file": f.name,
            "score": score,
            "severity": "SERIOUS" if score >= 0.5 else "MODERATE",
            "rule_citation": "26 U.S.C. § 6050P(a)",
            "violation_type": "1099C_CANCELLED_DEBT",
            "description": f"1099-C filed — cancellation of debt of $600+. "
                           "Triggers § 6050P(a) reporting duty. Evidence of RESA violation.",
            "defendant": defendant,
            "evidence_value": "CANCELLED_DEBT",
        })

    # ─── 3. Servicing-transfer docs — score against 12 U.S.C. § 2605(b) ───
    found_svc = []
    for f in files:
        fn = f.name.upper()
        if any(k in fn for k in ["SERVICING", "TRANSFER", "SVC", "NOTICE OF SERVICING"]):
            # Exclude non-doc files (e.g. "entry of appearance" has "appearance" not "servicing")
            found_svc.append(f)

    for f in found_svc:
        fn = f.name.upper()
        is_notice = "NOTICE" in fn and "SERVICING" in fn
        is_challenge = any(k in fn for k in ["CHALLENGE", "COMPLAINT", "OBJECTION", "OPPOSITION"])
        score = 0.9 if is_notice else 0.6 if is_challenge else 0.35

        results.append({
            "evidence_type": "servicing_transfer",
            "file": f.name,
            "score": score,
            "severity": "FATAL" if is_notice else "SERIOUS",
            "rule_citation": "12 U.S.C. § 2605(b)",
            "violation_type": "SERVICING_TRANSFER",
            "description": f"Servicing transfer document found. "
                           f"{'Notice of transfer — § 2605(b) notice required before transfer.' if is_notice else 'Challenge to transfer — evidence of unauthorized transfer.'}",
            "defendant": _extract_defendant_from_filename(f.name),
            "evidence_value": "NOTICE_BEFORE_TRANSFER" if is_notice else "UNAUTHORIZED_TRANSFER",
        })

    return results


def _resolve_intake_dir(case_slug: str) -> Path | None:
    """Resolve the intake directory for a case. AGNOSTIC: tries multiple slug forms so ANY case_id form
    resolves to its real obsidian folder — e.g. 'fed:2:26-cv-00123' -> 'fed_26-cv-00123',
    'phila_ccp:260100000' -> 'phila_ccp_260100000'. Previously only the exact slug was tried, so a colon
    form silently returned None and new intake (a just-filed order) was never scanned."""
    raw = case_slug or ""
    tail = raw.split(":", 1)[-1] if ":" in raw else raw
    safe = raw.replace(":", "_").replace("/", "_")
    forms = []
    for f in (raw, tail, safe, safe.replace("fed_2_", "fed_"), tail.replace(":", "_")):
        if f and f not in forms:
            forms.append(f)
    for f in forms:
        for c in (_BEEZLE_ROOT / "obsidian_sync" / "cases" / f / "intake",
                  _BEEZLE_ROOT / "cases" / f / "intake"):
            if c.exists() and c.is_dir():
                return c
    return None


def _classify_remic_file(f: Path) -> str:
    """Classify a GNMA REMIC file by its type code."""
    fn = f.name.upper()
    for pat in ["FDS", "RLY", "ANC", "SRF", "FRR", "MTF"]:
        if pat in fn:
            return pat
    return "UNKNOWN"


def _extract_remic_trust_id(files: list[Path]) -> str:
    """Extract the REMIC trust identifier from file names."""
    for f in files:
        fn = f.name.upper()
        # Match patterns like "GNMA-2022-125" or "GNMA-2022-125"
        m = _re.search(r"(GNMA-\d{4}-\d+)", fn)
        if m:
            return m.group(1)
        # Match patterns like "GNMA 2022-125"
        m = _re.search(r"(GNMA\s+\d{4}-\d+)", fn)
        if m:
            return m.group(1)
    return "UNKNOWN"


def _extract_defendant_from_filename(filename: str) -> str | None:
    """Extract defendant name from a filename. Best-effort heuristic."""
    fn = filename.upper()
    defendant_map = {
        "AMERISAVE": "Amerisave Mortgage Corporation",
        "Dovenmuehle": "DMI (Dovenmuehle Mortgage Inc)",
        "BNY": "Bank of New York Mellon",
        "WELLS FARGO": "Wells Fargo Bank, N.A.",
        "TRUMP": "Trump",
    }
    for key, val in defendant_map.items():
        if key in fn:
            return val
    return None


def _newest_intake_mtime(case_id: str) -> float:
    """mtime of the newest file under the case's intake dir. The freshness contract: an audit written
    AFTER the newest intake file already reflects everything on disk. FAIL-SAFE: if no intake dir can
    be resolved under any slug form, return +inf so the caller NEVER treats the audit as fresh (a
    wrong 'fresh' answer would silently skip scanning a new filing; a wrong 'stale' answer just costs
    one redundant scan)."""
    raw = case_id or ""
    forms = []
    # canonical "fed:2:26-cv-00123" -> obsidian slug forms; also accept already-safe ids
    tail = raw.split(":", 1)[-1] if ":" in raw else raw
    safe = raw.replace(":", "_").replace("/", "_")
    for f in (tail, safe, safe.replace("fed_2_", "fed_"), raw, tail.replace(":", "_")):
        if f and f not in forms:
            forms.append(f)
    d = None
    for f in forms:
        d = _resolve_intake_dir(f)
        if d:
            break
    if not d:
        return float("inf")
    newest = 0.0
    for p in d.rglob("*"):
        try:
            if p.is_file():
                m = p.stat().st_mtime
                if m > newest:
                    newest = m
        except Exception:
            continue
    return newest if newest > 0 else float("inf")


def build_audit_data(case_id: str, court: str = "EDPA",
                      caption: str = "", persist: bool = True,
                      skip_if_fresh: bool = False) -> dict:
    """Build (or refresh) the audit data for a case.

    1. Load existing audit JSON if present (preserves stare-decisis data).
    2. Scan opposing filings via block14_pinpointer (FRESH each call).
    3. Merge: existing scorecard preserved, procedural_violations refreshed.
    4. Optionally persist to disk.
    5. Return the merged dict.

    Args:
        case_id: Canonical case_id (e.g. "phila_ccp:260100000").
        court: Court code (PA_CP_PHILA, EDPA, etc.). Used by the detector.
        caption: Display title (best-effort).
        persist: If True, write back to output/audit/data_<safe>.json.
        skip_if_fresh: OPT-IN dedup (GO step 2, 2026-07-08): if the existing audit JSON is NEWER than
            the newest file in the case's intake dir, return it as-is instead of re-scanning every
            filing. Default False preserves the refresh-every-run invariant; reply flows that build
            the audit more than once per session pass True (a new filing always has a newer intake
            mtime, so it always forces the rebuild).

    Returns:
        Merged dict containing case info, preserved scorecard, fresh procedural.
    """
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)

    safe = _safe_id(case_id)
    audit_path = AUDIT_DIR / f"data_{safe}.json"

    if skip_if_fresh and audit_path.exists():
        try:
            if audit_path.stat().st_mtime > _newest_intake_mtime(case_id):
                log.info(f"audit fresh (newer than newest intake file) — skipping re-scan for {case_id}")
                return json.loads(audit_path.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning(f"freshness check failed ({e}); doing full rebuild")

    # 1. Preserve existing data if any
    existing = {}
    if audit_path.exists():
        try:
            existing = json.loads(audit_path.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning(f"could not parse existing audit at {audit_path}: {e}")

    # 2. Find this case in case_map
    case_info = {}
    try:
        if CASE_MAP_PATH.exists():
            cm = json.loads(CASE_MAP_PATH.read_text(encoding="utf-8"))
            ckey = _resolve_case_in_map(case_id, cm)
            if ckey:
                case_info = cm.get(ckey, {})
    except Exception as e:
        log.warning(f"case_map read failed: {e}")

    # 3. Scan opposing filings
    procedural_violations = []
    if case_info:
        scanned = _scan_opposing_filings(case_info, court=court, case_slug=case_id)
        procedural_violations = _dedup_with_counts(scanned)
        log.info(f"detected {len(procedural_violations)} unique procedural violations")
    else:
        log.warning(f"no case_info found for {case_id}; procedural detection skipped")

    # 4. Merge
    merged = dict(existing)  # start from existing (preserves scorecard)
    merged["case_id"] = existing.get("case_id") or case_id.split(":", 1)[-1]
    merged["case_court"] = existing.get("case_court") or court
    if caption and not merged.get("title"):
        merged["title"] = caption
    merged["procedural_violations"] = procedural_violations
    merged["audit_generated"] = datetime.now().isoformat(timespec="seconds")

    # ─── Phase B3: enrich with posture + case_type from registry ─────
    try:
        from pipeline.case_loader import load_case as _b3_load_case
        from pipeline.reasoning_orchestrator import detect_posture as _b3_detect
        _b3_case = _b3_load_case(case_id)
        if _b3_case:
            merged["posture"] = _b3_detect(_b3_case)
            merged["case_type"] = _b3_case.get("case_type", "")
            merged["status"] = _b3_case.get("status", "")
    except Exception as _b3_e:
        log.warning(f"posture writeback failed: {_b3_e}")
    # ─── end Phase B3 ─────────────────────────────────────────────────

    # ─── Phase 3G: HYBRID ECF stamping (LLM-reasoned, deterministically anchored) ─
    # Precedence per violation, never fabricated (R7):
    #   1. _3f_ecf  — the Phase 3F LLM extractor's reasoned number (own-vs-cited,
    #      reads garbled/image headers). The SMART layer.
    #   2. deterministic CM/ECF header scan (ecf_resolver, pdftotext + case-number
    #      validation). The fast/high-confidence ANCHOR and fallback.
    # Records source_ecf_basis (llm | header) for auditability. Makes the docket
    # number authoritative in the single source of truth (drafter + exhibits agree).
    try:
        import re as _ecf_re
        from pipeline.ecf_resolver import build_ecf_map as _b_ecf, resolve as _r_ecf
        _emap = _b_ecf(case_id)

        def _llm_ecf(_v):
            _raw = _v.get("_3f_ecf")
            if isinstance(_raw, int):
                return _raw
            if isinstance(_raw, str):
                _m = _ecf_re.search(r"\d+", _raw)  # handles "Document 20" / "20"
                if _m:
                    return int(_m.group(0))
            return None

        def _stamp(_v, _name):
            if not isinstance(_v, dict) or _v.get("source_ecf"):
                return 0
            _n = _llm_ecf(_v)
            if _n is not None:
                _v["source_ecf"] = _n; _v["source_ecf_basis"] = "llm"; return 1
            _n = _r_ecf(_name or "", _emap) if _emap else None
            if _n is not None:
                _v["source_ecf"] = _n; _v["source_ecf_basis"] = "header"; return 1
            return 0

        _stamped = 0
        for _v in merged.get("procedural_violations", []):
            _stamped += _stamp(_v, _v.get("source_doc") or _v.get("file"))
        _sc = merged.get("scorecard") or {}
        for _v in (_sc.get("violations", []) if isinstance(_sc, dict) else []):
            _stamped += _stamp(_v, _v.get("source_file"))
        log.info(f"Phase 3G: stamped source_ecf on {_stamped} violations "
                 f"(llm-preferred, header-anchored)")
    except Exception as _ecf_e:
        log.warning(f"Phase 3G ECF stamp skipped: {_ecf_e}")
    # ─── end Phase 3G ─────────────────────────────────────────────────

    # INTAKE STARE-DECISIS FOLD: the scorecard (non-binding-as-controlling / trial-court-as-appellate) is
    # PRESERVED here, built by baseline_loop scanning stale case_map paths — so a NEW memorandum's out-of-
    # circuit cites never reach it. Audit the intake's court docs and merge into the scorecard (dedup).
    try:
        _intake_sd = _scan_intake_hierarchy(case_id, court)
        if _intake_sd:
            _sc = merged.get("scorecard")
            if not isinstance(_sc, dict):
                _sc = merged["scorecard"] = {"violations": []}
            _vios = _sc.setdefault("violations", [])
            _seen = {(v.get("citation"), v.get("source_file") or v.get("file")) for v in _vios}
            _added = 0
            for v in _intake_sd:
                k = (v.get("citation"), v.get("source_file") or v.get("file"))
                if k not in _seen:
                    _vios.append(v); _seen.add(k); _added += 1
            log.info(f"intake stare-decisis fold: +{_added} scorecard violations from case intake")
    except Exception as _sde:
        log.warning(f"intake stare-decisis fold skipped: {_sde}")

    # 4.5 Jurisdictional-propriety lever (SMJ / removal / void-judgment) — additive, never fatal.
    try:
        from pipeline.jurisdiction_check import analyze as _jx_analyze
        _jx = _jx_analyze(case_id)
        merged["jurisdictional_defects"] = _jx.get("jurisdictional_defects", [])
        merged["_jx_summary"] = {k: _jx.get(k) for k in ("removed", "federal_claim_on_face", "llc_defendants")}
    except Exception as _jxe:
        log.warning(f"jurisdiction_check skipped: {_jxe}")

    # 4.6 Justiciability (ripeness/mootness) — the two thinnest triad legs; additive.
    try:
        from pipeline.justiciability_detect import detect as _just_detect
        _jtxt = " ".join(str(merged.get(k, "")) for k in
                         ("opposing_facts", "plaintiff_facts", "record_contradictions", "title"))
        _jf = _just_detect(_jtxt)
        if _jf:
            merged.setdefault("procedural_violations", []).extend(_jf)
    except Exception as _je:
        log.warning(f"justiciability_detect skipped: {_je}")

    # 5. Persist
    if persist:
        try:
            audit_path.write_text(
                json.dumps(merged, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            log.info(f"audit data written to {audit_path.name}")
        except Exception as e:
            log.error("audit_writer", f"persist failed: {e}")

        # 5b. Authority-triad synthesis — read the just-written jurisdictional_defects +
        # procedural_violations and reason capacity+standing+jurisdiction as ONE threshold
        # (any leg failed -> no authority -> void). Additive key `authority_triad`.
        try:
            from pipeline.authority_triad import write_audit as _auth_triad
            _auth_triad(case_id)
        except Exception as _ate:
            log.warning(f"authority_triad skipped: {_ate}")

        # 5c. Jurisdictional-MODE analysis (10th leg) — read the ORDERS + OPPOSING drafts (never the
        # litigant's own filings) and prove any law->equity/admiralty switch or immunity gap, element by
        # element (LAW rule -> RECORD conduct -> violation -> lever). Additive key `jurisdiction_mode`.
        try:
            from pipeline.jurisdiction_mode import write_audit as _jmode
            _jmode(case_id)
        except Exception as _jme:
            log.warning(f"jurisdiction_mode skipped: {_jme}")

        # Analysis-layer producer: record litigation posture (default status, admissions
        # by conduct, contested-void orders, clerk non-filing, SJ basis) into the audit so
        # m0 + the dependency/posture/conflict engines consume ONE source and nothing slips.
        try:
            from pipeline.litigation_posture import extract as _posture
            _posture(case_id, write=True)
        except Exception as e:
            log.error("audit_writer", f"litigation_posture skipped: {e}")

        # Analysis-layer producer: VERIFY (never assume) the judicial officer(s) presiding —
        # signer read off the record + each order's filer, checked against the researched
        # registry (FJC/congress.gov/state roster). Writes judicial_officers into the audit.
        # Analysis-side only — NOT wired into the drafter.
        try:
            from pipeline.judicial_officer_verifier import verify as _verify_officers
            _verify_officers(case_id, write=True)
        except Exception as e:
            log.error("audit_writer", f"judicial_officer_verify skipped: {e}")

        # Attribute each violation to its ACTOR (judge / opposing counsel / litigant) so the breach
        # records attribute correctly, then expose the JUDGE'S oath breach (only the judge's own
        # orders) and route OPPOSING-COUNSEL violations to their own Rule 11 / bar record. The
        # litigant's own filings are excluded from both. Analysis-side only — NOT the drafter.
        try:
            from pipeline.violation_attribution import attribute_violations
            attribute_violations(case_id, write_back=True)
        except Exception as e:
            log.error("audit_writer", f"violation_attribution skipped: {e}")

        # AUTHORITY BANK GATE (2026-08-26, user: gates move UP into analysis so it is airtight):
        # cite-verify + term-of-art discipline run AT AUDIT TIME on the just-written stream —
        # every case-law authority stamped verified/unconfirmed/UNBACKED (UNBACKED demoted to
        # authority_rejected so no drafter can cite it), terms-of-art flagged, one organ record
        # (AUTHORITY_BANK_GATE) pinned into procedural_violations. Downstream drafters (m0/m1)
        # inherit a pre-verified stream instead of gating per-draft.
        try:
            from pipeline.authority_bank_gate import gate_case as _bank_gate
            _bank_gate(case_id, write=True)
        except Exception as e:
            log.error("audit_writer", f"authority_bank_gate skipped: {e}")
        # ─── Claim-fidelity gate: does the authority support the PROPOSITION it is cited for? ──
        # authority_bank_gate answers "does this cite EXIST and resolve" (existence).
        # This answers "does the cited case actually hold what we say it holds" (fidelity) --
        # the WRONG_POINT / OVERSTATED class that an existence check passes clean, because a
        # real citation attached to a proposition it does not carry is perfectly resolvable.
        # It belongs HERE, at m0 analysis time, not in the m1 tail: stamped on the record, no
        # drafter can weave a demoted authority, and the fix holds for every future filing off
        # this audit instead of being re-litigated per draft.
        try:
            from pipeline.claim_fidelity_gate import gate_case as _fid_gate, gate_bank as _fid_bank
            _fc = _fid_gate(case_id, write=True) or _fid_bank(case_id, write=True)
            if _fc:
                log.info(f"claim_fidelity_gate: {_fc}")
        except Exception as e:
            log.error("audit_writer", f"claim_fidelity_gate skipped: {e}")
        try:
            from pipeline.judicial_oath_breach import build_breach_record, build_opposing_counsel_record
            build_breach_record(case_id, write=True)
            build_opposing_counsel_record(case_id, write=True)
        except Exception as e:
            log.error("audit_writer", f"oath_breach skipped: {e}")

        # ─── Evidence ingestion layer: score intake documents against statutory duties ──
        try:
            scored_evidence = _ingest_evidence_class(case_id)
            if scored_evidence:
                log.info(f"evidence_ingest: {len(scored_evidence)} scored entries for {case_id}")
            # Merge into the merged dict so it's persisted
            merged["evidence_ingest"] = {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "entries": scored_evidence,
                "summary": {
                    "total": len(scored_evidence),
                    "by_type": {et: sum(1 for e in scored_evidence if e["evidence_type"] == et)
                                for et in set(e["evidence_type"] for e in scored_evidence)} if scored_evidence else {},
                    "fatal": sum(1 for e in scored_evidence if e.get("severity") == "FATAL"),
                    "serious": sum(1 for e in scored_evidence if e.get("severity") == "SERIOUS"),
                    "moderate": sum(1 for e in scored_evidence if e.get("severity") == "MODERATE"),
                    "minor": sum(1 for e in scored_evidence if e.get("severity") == "MINOR"),
                },
            }
        except Exception as e:
            log.error("audit_writer", f"evidence_ingest skipped: {e}")

    return merged


# BEEZLE_FN_UNUSED 2026-06-13: get_or_build_audit (no static refs in pipeline/)
def get_or_build_audit(case_id: str, court: str = "EDPA",
                       caption: str = "") -> dict:
    """Convenience: returns the audit dict, refreshing procedural violations."""
    return build_audit_data(case_id, court=court, caption=caption, persist=True)
