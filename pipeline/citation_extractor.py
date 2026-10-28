"""
pipeline/citation_extractor.py
===============================
Full-document citation extraction.

The v4 scorecard had empty 'Issuing Court', 'Defendant Filing', and
'Citation Inventory' tables because citation extraction ran only on the
~200-character QUOTE snippets attached to detected violations. Block14
quotes rarely contain a full reporter cite.

This module scans the ENTIRE text of each adversary filing for citations
matching the standard reporter patterns. Output is grouped by source
filing, allowing the scorecard to produce the same 92-row Citation
Inventory the May-19 scorecard had.

PUBLIC API
----------
extract_citations_from_case(case_id, court_id="EDPA") -> Dict[str, Any]

Returns:
  {
    "all_citations": [
      {citation, reporter_type, issuing_court, binding, severity,
       source_filing, source_path, dkt}, ...
    ],
    "by_filing": {filename: [citation, ...]},
    "by_court":  {court_label: count},
    "by_severity": {FATAL: n, SERIOUS: n, OK: n},
    "documents_scanned": int,
  }
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import re
import json
import logging
from pathlib import Path
from collections import defaultdict, Counter
from typing import Dict, List, Any, Optional

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

log = logging.getLogger("citation_extractor")

# === BEEZLE_PATCH_PATH_RESOLVE_V1 ===
try:
    from pipeline._path_resolver import resolve_path as _resolve_path
except Exception:
    def _resolve_path(raw):
        try:
            p = Path(raw) if raw else None
            return p if (p is not None and p.exists()) else None
        except Exception:
            return None
# === END BEEZLE_PATCH_PATH_RESOLVE_V1 ===

ROOT = _BEEZLE_ROOT


# ---------------------------------------------------------------------------
# Citation patterns
# ---------------------------------------------------------------------------
CITATION_PATTERNS = [
    # SCOTUS — bind on this court
    (re.compile(r"\b\d+\s*U\.\s*S\.\s*\d+\b"),                "scotus"),
    (re.compile(r"\b\d+\s*S\.\s*Ct\.\s*\d+\b"),                "scotus"),
    (re.compile(r"\b\d+\s*L\.\s*Ed\.\s*(?:2d\s*)?\d+\b"),      "scotus"),

    # Third Circuit precedential — bind on EDPA
    # (we detect F.3d/F.2d generally, then refine based on parenthetical)
    (re.compile(r"\b\d+\s*F\.\s*[23]d\s*\d+\b"),                "circuit_reporter"),
    (re.compile(r"\b\d+\s*F\.4th\s*\d+\b"),                     "circuit_reporter"),

    # Unpublished circuit dispositions — non-binding under IOP 5.7
    (re.compile(r"\b\d+\s*F\.\s*App'?x\.?\s*\d+\b"),            "unpublished_circuit"),
    (re.compile(r"\b\d+\s*Fed\.\s*App'?x\.?\s*\d+\b"),          "unpublished_circuit"),

    # District court reporters — non-binding (persuasive at best)
    (re.compile(r"\b\d+\s*F\.\s*Supp\.?\s*[23]?d?\.?\s*\d+\b"), "district_reporter"),

    # Westlaw / LEXIS unpublished — non-binding
    (re.compile(r"\b\d{4}\s*WL\s*\d+\b"),                       "westlaw_unpublished"),
    (re.compile(r"\bU\.S\.\s*Dist\.\s*LEXIS\s*\d+\b"),          "district_lexis_unpublished"),
    (re.compile(r"\b\d{4}\s*U\.\s*S\.\s*App\.\s*LEXIS\s*\d+\b"), "circuit_lexis"),

    # Pennsylvania state courts
    (re.compile(r"\bPa\.\s*Super\.\s*(?:Ct\.\s*)?\d{4}",       re.IGNORECASE), "pa_superior"),
    (re.compile(r"\bPa\.\s*Commw\.\s*(?:Ct\.\s*)?\d{4}",       re.IGNORECASE), "pa_commwlth"),
    (re.compile(r"\b\d+\s*Pa\.\s+\d+\b"),                       "pa_supreme"),
    (re.compile(r"\b\d+\s*A\.\s*[23]d\s*\d+\b"),                "atlantic_reporter"),
]


COURT_LABEL = {
    "scotus":                       ("Supreme Court of the United States",        "YES — binding"),
    "circuit_reporter":             ("U.S. Court of Appeals (sister circuit)",    "NO — non-binding"),
    "third_circuit":                ("U.S. Court of Appeals (Third Circuit)",     "YES — binding"),
    "unpublished_circuit":          ("U.S. Court of Appeals (non-precedential)",  "NO — non-binding"),
    "district_reporter":            ("U.S. District Court (outside Third Cir.)",  "NO — non-binding"),
    "district_lexis_unpublished":   ("U.S. District Court (unpublished)",         "NO — non-binding"),
    "circuit_lexis":                ("U.S. Court of Appeals (unpublished)",       "NO — non-binding"),
    "westlaw_unpublished":          ("an unidentified court",                     "NO — non-binding"),
    "atlantic_reporter":            ("Atlantic Reporter (state)",                 "NO — non-binding"),
    "pa_superior":                  ("Pennsylvania Superior Court",               "NO — non-binding"),
    "pa_commwlth":                  ("Pennsylvania Commonwealth Court",           "NO — non-binding"),
    "pa_supreme":                   ("Pennsylvania Supreme Court",                "NO — non-binding"),
}


def _classify_circuit_citation(text: str, match_start: int, match_end: int, court_id: str = "EDPA") -> str:
    """Look at the parenthetical after an F.3d/F.2d/F.4th cite to determine if it's 3d Cir. or sister circuit.

    Returns: 'third_circuit' if controlling circuit; 'circuit_reporter' if sister.
    """
    # Look at the next 80 chars for a parenthetical
    tail = text[match_end:match_end + 80]
    # Match controlling circuit for the court
    controlling = {
        "EDPA":  r"3d\s+Cir|Third\s+Circuit",
        "WDPA":  r"3d\s+Cir|Third\s+Circuit",
        "MDPA":  r"3d\s+Cir|Third\s+Circuit",
        "DNJ":   r"3d\s+Cir|Third\s+Circuit",
        "DDE":   r"3d\s+Cir|Third\s+Circuit",
        "SDNY":  r"2d\s+Cir|Second\s+Circuit",
        "EDNY":  r"2d\s+Cir|Second\s+Circuit",
    }
    pat = controlling.get(court_id, r"3d\s+Cir|Third\s+Circuit")
    if re.search(pat, tail, re.IGNORECASE):
        return "third_circuit"
    return "circuit_reporter"


def _severity(reporter_type: str) -> str:
    """Map reporter type to scorecard severity."""
    if reporter_type in ("scotus", "third_circuit"):
        return "OK"
    if reporter_type in ("unpublished_circuit", "district_reporter",
                          "district_lexis_unpublished", "circuit_lexis",
                          "westlaw_unpublished"):
        return "FATAL"
    return "SERIOUS"


# ---------------------------------------------------------------------------
# Docket extraction (mirrors renderer)
# ---------------------------------------------------------------------------
DKT_PATTERN = re.compile(
    r"\b(?:ECF|Dkt\.?|Docket|Doc\.?|Document)\s*(?:No\.?)?\s*(\d+(?:-\d+)?)",
    re.IGNORECASE,
)


def _extract_dkt(text: str, filename: str) -> str:
    m = DKT_PATTERN.search(filename or "")
    if m:
        return f"Dkt {m.group(1)}"
    bare = re.match(r"^(\d+(?:-\d+)?)\.(?:pdf|docx)", (filename or "").strip(), re.IGNORECASE)
    if bare:
        return f"Dkt {bare.group(1)}"
    m = DKT_PATTERN.search(text or "")
    if m:
        return f"Dkt {m.group(1)}"
    return ""


# ---------------------------------------------------------------------------
# Main extractor
# ---------------------------------------------------------------------------
def extract_citations_from_doc(text: str, filename: str = "", court_id: str = "EDPA",
                                 source_path: str = "") -> List[Dict[str, Any]]:
    """Extract every citation from a full document's text."""
    if not text:
        return []
    cites: List[Dict[str, Any]] = []
    seen_in_doc = set()  # dedupe identical citations within the same filing
    dkt = _extract_dkt(text, filename)
    for pat, reporter_type in CITATION_PATTERNS:
        for m in pat.finditer(text):
            cite_text = m.group(0).strip()
            key = (cite_text, reporter_type)
            if key in seen_in_doc:
                continue
            seen_in_doc.add(key)

            # Refine circuit citations using the parenthetical
            actual_type = reporter_type
            if reporter_type == "circuit_reporter":
                actual_type = _classify_circuit_citation(text, m.start(), m.end(), court_id)

            court_label, binding = COURT_LABEL.get(
                actual_type, ("an unidentified court", "NO — non-binding"))
            sev = _severity(actual_type)
            cites.append({
                "citation": cite_text,
                "reporter_type": actual_type,
                "issuing_court": court_label,
                "binding": binding,
                "severity": sev,
                "source_filing": filename,
                "source_path": source_path,
                "dkt": dkt,
            })
    return cites


def extract_citations_from_case(case_id: str, court_id: str = "EDPA",
                                  adversary_only: bool = True) -> Dict[str, Any]:
    """Walk every adversary filing in the case and extract citations."""
    # Load case_map
    case_map_path = ROOT / "cases_index" / "case_map.json"
    if not case_map_path.exists():
        return _empty_result()
    case_map = json.loads(case_map_path.read_text(encoding="utf-8"))
    entry = case_map.get(case_id)
    if not entry:
        for k, v in case_map.items():
            if k.endswith(case_id.split(":")[-1]):
                entry = v; break
    if not entry:
        return _empty_result()

    # Defendants list (for classifier)
    defendants = entry.get("all_defendants") or []

    # Doc paths
    docs = []
    for kind in ("orders", "filings", "motions_to_dismiss", "mtds",
                 "oppositions", "court_notices", "exhibits", "documents"):
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
                        docs.append({"path": p, "kind": kind})

    # Role-filter
    try:
        from pipeline.document_role_classifier import classify_doc_with_evidence  # BEEZLE_CLASSIFY_IMPORT_FIX_V1
    except Exception:
        classify_doc_with_evidence = None

    adv_docs = []
    for d in docs:
        if adversary_only and classify_doc_with_evidence:
            role = classify_doc_with_evidence(
                d["path"], defendants=defendants, plaintiff_name="Filer"
            ).get("role", "unknown")
            d["role"] = role
            if role == "plaintiff":
                continue
        adv_docs.append(d)

    log.info("citation_extractor scanning %d adversary docs", len(adv_docs))

    # Read text & extract citations
    try:
        from pipeline.doc_reader import extract_text
    except Exception:
        log.error("doc_reader unavailable")
        return _empty_result()

    all_citations: List[Dict[str, Any]] = []
    by_filing: Dict[str, List[Dict[str, Any]]] = {}
    by_court = Counter()
    by_severity = Counter()
    docs_with_cites = 0

    # === BEEZLE_CITATION_PATH_DIAG_V1 ===
    _diag_missing = 0
    _diag_empty   = 0
    _diag_exception = 0
    _diag_samples = []  # === BEEZLE_PATCH_CIT_DIAG_RESOLVE_V1 ===
    for d in adv_docs:
        path = _resolve_path(d["path"])
        if path is None:
            _diag_missing += 1
            if len(_diag_samples) < 5:
                _diag_samples.append(str(d["path"])[:200])
            continue
        try:
            text = extract_text(str(path)) or ""
        except Exception as _e:
            _diag_exception += 1
            log.debug("extract_text raised on %s: %s", path, _e)
            continue
        if not text:
            _diag_empty += 1
            continue
    # back-compat: original loop continues below this marker; the actual
    # citation extraction is re-entered in the loop below.
    # === END BEEZLE_CITATION_PATH_DIAG_V1 ===
    if _diag_missing or _diag_empty or _diag_exception:
        log.info("  diag: missing_path=%d, extract_empty=%d, extract_raised=%d (of %d adv_docs)",
                 _diag_missing, _diag_empty, _diag_exception, len(adv_docs))
        for _s in (_diag_samples if "_diag_samples" in dir() else []):
            log.info("    unresolved: %s", _s)
    # === BEEZLE_PATCH_CIT_MAIN_RESOLVE_V1 ===
    for d in adv_docs:
        path = _resolve_path(d["path"])
        if path is None:
            continue
        try:
            text = extract_text(str(path)) or ""
        except Exception:
            continue
        if not text:
            continue
        cites = extract_citations_from_doc(text, filename=path.name,
                                            court_id=court_id,
                                            source_path=str(path))
        if not cites:
            continue
        docs_with_cites += 1
        all_citations.extend(cites)
        by_filing[path.name] = cites
        for c in cites:
            by_court[c["issuing_court"]] += 1
            by_severity[c["severity"]] += 1

    log.info("  extracted %d citations across %d docs", len(all_citations), docs_with_cites)

    return {
        "all_citations": all_citations,
        "by_filing": by_filing,
        "by_court": dict(by_court),
        "by_severity": dict(by_severity),
        "documents_scanned": len(adv_docs),
        "documents_with_citations": docs_with_cites,
    }


def _empty_result() -> Dict[str, Any]:
    return {
        "all_citations": [],
        "by_filing": {},
        "by_court": {},
        "by_severity": {},
        "documents_scanned": 0,
        "documents_with_citations": 0,
    }


if __name__ == "__main__":
    import sys
    case_id = sys.argv[1] if len(sys.argv) > 1 else "fed:2:26-cv-00123"
    court_id = sys.argv[2] if len(sys.argv) > 2 else "EDPA"
    result = extract_citations_from_case(case_id, court_id=court_id)
    print(f"\nCitation extraction for {case_id} ({court_id})")
    print(f"Documents scanned: {result['documents_scanned']}")
    print(f"Documents with citations: {result['documents_with_citations']}")
    print(f"Total citations: {len(result['all_citations'])}")
    print(f"By severity: {result['by_severity']}")
    print()
    print("By issuing court:")
    for court, n in sorted(result["by_court"].items(), key=lambda kv: -kv[1]):
        print(f"  {court:<45} {n}")
    print()
    print("First 10 citations:")
    for c in result["all_citations"][:10]:
        print(f"  {c['dkt']:<10} {c['severity']:<8} {c['citation'][:55]:<55} {c['source_filing'][:30]}")
