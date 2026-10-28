"""
pipeline/prima_facie_evidence.py
================================
Walks the case docket looking for plaintiff exhibits and adversary
admissions that constitute prima facie evidence on the record.

The scorecard surfaces these so the court sees:
  - What's already in evidence supporting plaintiff
  - What defendants have failed to rebut
  - What defendants have inadvertently admitted

PUBLIC API
----------
gather_prima_facie_evidence(case_id) -> List[Dict]

Each entry: {dkt, source_doc, kind, summary, role}
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import re
import json
import logging
from pathlib import Path
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

log = logging.getLogger("prima_facie_evidence")

ROOT = _BEEZLE_ROOT


# Patterns of plaintiff prima facie evidence (case-agnostic markers)
EVIDENCE_PATTERNS = {
    "Certificate of Service": [
        r"certificate\s+of\s+service",
        r"proof\s+of\s+service",
        r"certified\s+mail\s+receipt",
        r"\bgreen\s+card\b",
    ],
    "1099-C / Debt Cancellation": [
        r"\b1099[- ]?C\b",
        r"cancellation\s+of\s+debt",
        r"debt\s+cancellation",
    ],
    "Validation Request / Response": [
        r"validation\s+of\s+debt",
        r"debt\s+validation",
        r"validation\s+response",
    ],
    "Mailbox / Receipt Evidence": [
        r"received\s+and\s+signed\s+for",
        r"signature\s+confirmation",
        r"return\s+receipt",
        r"USPS\s+tracking",
    ],
    "Loan / Contract": [
        r"loan\s+(?:contract|agreement)",
        r"promissory\s+note",
        r"negotiable\s+instrument",
        r"electronic\s+(?:funds?\s+)?transfer\s+(?:agreement|authorization)",
    ],
    "Admission Against Interest": [
        r"defendant\s+admit(?:s|ted|ting)",
        r"(?:we|they)\s+(?:do\s+not\s+)?dispute",
        r"undisputed",
        r"concede(?:s|d|ding)?",
        r"acknowledge(?:s|d|ment|ing)?",
    ],
    "Affidavit / Declaration": [
        r"sworn\s+affidavit",
        r"under\s+penalty\s+of\s+perjury",
        r"declaration\s+(?:of|under)",
        r"28\s+U\.\s*S\.\s*C\.\s*§+\s*1746",
    ],
    "Docket Entry Reference": [
        r"\b(?:ECF|Dkt\.?|Doc\.?)\s*(?:No\.?\s*)?(\d+(?:-\d+)?)",
    ],
}


DKT_PATTERN = re.compile(
    r"\b(?:ECF|Dkt\.?|Docket|Doc\.?|Document)\s*(?:No\.?)?\s*(\d+(?:-\d+)?)",
    re.IGNORECASE,
)


def _safe_cid(case_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", case_id)


def _extract_dkt(text: str, filename: str) -> str:
    """Extract a Dkt N reference from text or filename."""
    # Try filename first
    m = DKT_PATTERN.search(filename or "")
    if m:
        return f"Dkt {m.group(1)}"
    # Then text
    m = DKT_PATTERN.search(text or "")
    if m:
        return f"Dkt {m.group(1)}"
    # Bare leading numbers in filename
    if filename:
        bare = re.match(r"^(\d+(?:-\d+)?)\.(?:pdf|docx)",
                         filename.strip(), re.IGNORECASE)
        if bare:
            return f"Dkt {bare.group(1)}"
    return ""


def gather_prima_facie_evidence(
    case_id: str,
    max_results: int = 30,
) -> List[Dict[str, Any]]:
    """Walk the case docket for plaintiff exhibits + adversary admissions.

    Returns a list of evidence entries suitable for the scorecard:
      [{dkt, source_doc, kind, summary, role}]
    """
    canonical_id = _safe_cid(case_id)

    # Load case_map
    case_map_path = ROOT / "cases_index" / "case_map.json"
    if not case_map_path.exists():
        return []
    case_map = json.loads(case_map_path.read_text(encoding="utf-8"))
    entry = case_map.get(case_id)
    if not entry:
        for k, v in case_map.items():
            if k.endswith(case_id.split(":")[-1]):
                entry = v
                break
    if not entry:
        return []

    # Collect doc paths from plaintiff-likely buckets + exhibits
    docs = []
    for kind in ("exhibits", "my_drafts", "filings", "notes"):
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

    # Use the role classifier to focus on plaintiff exhibits
    try:
        from doc_role_classifier import classify_doc_with_evidence
    except Exception:
        classify_doc_with_evidence = None

    try:
        from pipeline.doc_reader import extract_text
    except Exception:
        extract_text = None

    results: List[Dict[str, Any]] = []
    seen_dkts = set()

    for d in docs:
        if len(results) >= max_results:
            break
        path = Path(d["path"])
        if not path.exists():
            continue
        filename = path.name

        # Classify role
        role = "unknown"
        if classify_doc_with_evidence:
            try:
                role = classify_doc_with_evidence(str(path)).get("role", "unknown")
            except Exception:
                pass

        # We're looking for plaintiff exhibits AND adversary admissions in
        # opposing filings. Skip if it's a judge doc.
        if role == "judge":
            continue

        # Get text
        text = ""
        if extract_text:
            try:
                text = extract_text(str(path)) or ""
            except Exception:
                continue
        if not text:
            continue
        text_lower = text.lower()

        # Search for evidence markers
        for kind, patterns in EVIDENCE_PATTERNS.items():
            if kind == "Docket Entry Reference":
                continue  # used for dkt extraction
            if kind == "Admission Against Interest" and role == "plaintiff":
                # An "admission" in YOUR filing isn't an admission against
                # adversary interest. Skip.
                continue
            for pat in patterns:
                m = re.search(pat, text_lower, re.IGNORECASE)
                if m:
                    dkt = _extract_dkt(text, filename)
                    if dkt and dkt in seen_dkts and kind != "Admission Against Interest":
                        # Avoid duplicating evidence from the same docket entry
                        break
                    snippet_start = max(0, m.start() - 60)
                    snippet_end = min(len(text), m.end() + 120)
                    snippet = text[snippet_start:snippet_end].replace("\n", " ").strip()
                    if len(snippet) > 200:
                        snippet = snippet[:197] + "..."
                    results.append({
                        "dkt": dkt or "—",
                        "source_doc": filename,
                        "kind": kind,
                        "summary": snippet,
                        "role": role,
                    })
                    if dkt:
                        seen_dkts.add(dkt)
                    break  # one finding per (doc, kind)

    return results


if __name__ == "__main__":
    import sys
    case_id = sys.argv[1] if len(sys.argv) > 1 else "fed:2:26-cv-00123"
    evs = gather_prima_facie_evidence(case_id)
    print(f"\nPrima facie evidence on docket for {case_id}: {len(evs)} entries")
    print("=" * 70)
    for e in evs[:20]:
        print(f"  {e['dkt']:<10} [{e['kind']:<30}] {e['source_doc'][:40]}")
        print(f"             role={e['role']}  {e['summary'][:100]}")
