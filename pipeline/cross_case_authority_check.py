# -*- coding: utf-8 -*-
"""
cross_case_authority_check.py
==============================
Validator: detects when opposing counsel cites orders/cases from your OTHER
related cases without disclosure (Pa.R.P.C. 3.3 candor violation territory).

Typical scenario:
  - You have fed:2:26-cv-00789 (federal bad faith case, judge issued void order)
  - You filed mandamus challenging that order
  - In phila_ccp:260100000 (Lippi state case), opposing counsel cites that
    same federal order to support their motion to strike
  - They didn't disclose the mandamus to the state court
  - That's a candor violation + reliance on void authority

This validator:
  1. Loads case registry
  2. For target case, gets related_cases list
  3. Builds match patterns for each related case (docket aliases, caption,
     party combinations)
  4. Scans opposing-party filings in target case for those patterns
  5. Returns ProceduralDefect-shaped output that plugs into existing
     defect aggregation

Usage:
    from pipeline.cross_case_authority_check import scan
    result = scan("phila_ccp:260100000")
    # result["defects"] = [...]
    # result["total"], result["fatal_count"], etc.

Run standalone:
    python pipeline/cross_case_authority_check.py phila_ccp:260100000
"""

from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import json
import logging
import re
import sys
from dataclasses import dataclass, field, asdict
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

_ROOT_WSL = _eroot()
_ROOT_WIN = _BEEZLE_ROOT
ROOT = _ROOT_WSL if _ROOT_WSL.exists() else _ROOT_WIN
REGISTRY = ROOT / "cases_index" / "case_registry.json"
VAULT = ROOT / "obsidian_sync" / "cases"

log = logging.getLogger("cross_case_authority")
if not log.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(handler)
log.setLevel(logging.INFO)


@dataclass
class CrossCaseDefect:
    """Single defect representing cross-case authority misuse."""
    category: str = "CROSS_CASE_AUTHORITY"
    severity: str = "FATAL"
    defect: str = ""
    authority: list = field(default_factory=list)
    motion: str = ""
    source: str = ""        # filing where the misuse was found
    related_case: str = ""  # the related case that was improperly cited
    matched_text: str = ""  # the actual text that triggered the match


# Standard authorities for cross-case candor violations
CANDOR_AUTHORITIES_PA = [
    "Pa.R.P.C. 3.3 (candor toward the tribunal)",
    "Pa.R.C.P. 1023.1 (signing pleadings; sanctions for frivolous filings)",
    "Pa.R.C.P. 1028(a)(2) (motion to strike for non-conforming pleading)",
]

CANDOR_AUTHORITIES_FED = [
    "FRCP 11 (sanctions; representations to the court)",
    "Fed. R. Prof. C. 3.3 (candor toward the tribunal)",
    "L.R. Civ. P. 7.1 (motion practice; truthful representations)",
]

VOID_ORDER_AUTHORITIES = [
    "FRCP 60(b)(4) (void judgment)",
    "28 U.S.C. § 1651 (All Writs Act)",
    "Pa.R.C.P. 237.3 (relief from void judgment)",
]


def load_registry() -> dict:
    """Load the case registry."""
    if not REGISTRY.exists():
        log.error("Registry not found: " + str(REGISTRY))
        return {}
    try:
        return json.loads(REGISTRY.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        log.error("Registry malformed: " + str(e))
        return {}


def build_match_patterns(case_data: dict) -> list:
    """
    Build regex patterns that would match references to this case.

    Combines:
      - canonical_id ("fed:2:26-cv-00789")
      - all docket_aliases ("2:26-cv-00789", "06077", "2025CV06077", etc.)
      - state_origin_docket if present
      - caption with "v." normalization

    Returns list of compiled regex patterns.
    """
    patterns = []
    seen = set()

    # canonical_id
    canon = case_data.get("canonical_id", "")
    if canon:
        patterns.append(re.escape(canon))
        seen.add(canon.lower())
        # Also a colon-stripped variant
        no_colons = canon.replace(":", "[\\-:]?")
        if no_colons not in seen:
            patterns.append(no_colons)
            seen.add(no_colons)

    # docket_aliases
    for alias in case_data.get("docket_aliases", []) or []:
        a = str(alias).strip()
        if a and a.lower() not in seen:
            patterns.append(re.escape(a))
            seen.add(a.lower())

    # state_origin_docket
    if case_data.get("state_origin_docket"):
        a = str(case_data["state_origin_docket"]).strip()
        if a and a.lower() not in seen:
            patterns.append(re.escape(a))
            seen.add(a.lower())

    # Caption — extract first defendant name (most distinctive)
    caption = (case_data.get("caption") or "").strip()
    if caption and " VS " in caption.upper():
        parts = re.split(r"\s+VS\s+", caption, flags=re.IGNORECASE)
        if len(parts) >= 2:
            first_def = parts[1].split()[0].strip(",").strip()
            if first_def and len(first_def) > 3 and first_def.lower() not in seen:
                # Match patterns like "Filer v. State Farm" or "Filer v State Farm"
                pat = r"filer\s+v\.?\s+" + re.escape(first_def)
                patterns.append(pat)
                seen.add(first_def.lower())

    return patterns


def find_opposing_filings(case_id: str, case_data: dict) -> list:
    """
    Find opposing-party filings in this case's vault folder.

    Returns list of (filename, full_text) tuples.
    """
    # Find the vault folder for this case
    # It's keyed by the slug, not the canonical_id
    candidates = []

    # Try common slug patterns
    canon = case_data.get("canonical_id", case_id)
    caption = case_data.get("caption", "")
    parties = case_data.get("parties", [])

    # Slug starts with canon's prefix-form
    slug_prefix = canon.replace(":", "_")

    # Look in VAULT for any folder that starts with this prefix
    if VAULT.exists():
        for folder in VAULT.iterdir():
            if folder.is_dir() and folder.name.startswith(slug_prefix):
                candidates.append(folder)

    if not candidates:
        log.warning("  No vault folder found for " + case_id)
        return []

    case_folder = candidates[0]
    log.info("  Vault folder: " + case_folder.name)

    # Look for opposing-party filings (they typically have "defendant", "opposition",
    # "motion to strike", "answer", "reply" in the filename)
    opposing_keywords = [
        "defendant", "opposition", "opposing", "answer", "reply",
        "motion to dismiss", "motion to strike", "memorandum",
        "lippi", "amerisave", "equifax", "state farm", "example trust co",
        "planet home", "allstate", "progressive",
        "state_farm",
    ]

    found_files = []
    for ext in ["*.pdf", "*.docx", "*.txt", "*.md"]:
        for fp in case_folder.rglob(ext):
            name_lower = fp.name.lower()
            if any(kw in name_lower for kw in opposing_keywords):
                # Try to read it
                try:
                    if fp.suffix.lower() == ".txt" or fp.suffix.lower() == ".md":
                        text = fp.read_text(encoding="utf-8", errors="ignore")
                    elif fp.suffix.lower() == ".pdf":
                        # Try pdfplumber if available
                        try:
                            import pdfplumber
                            text_parts = []
                            with pdfplumber.open(fp) as pdf:
                                for page in pdf.pages:
                                    pt = page.extract_text() or ""
                                    text_parts.append(pt)
                            text = "\n".join(text_parts)
                        except Exception:
                            text = ""  # PDF unreadable, skip
                    elif fp.suffix.lower() == ".docx":
                        try:
                            from docx import Document
                            doc = Document(fp)
                            text = "\n".join(p.text for p in doc.paragraphs)
                        except Exception:
                            text = ""
                    else:
                        text = ""

                    if text and len(text) > 100:  # skip empty/tiny files
                        found_files.append((fp.name, text))
                except Exception as e:
                    log.warning("  Could not read " + fp.name + ": " + str(e))

    log.info("  Found " + str(len(found_files)) + " opposing-party filing(s) to scan")
    return found_files


def scan(case_id: str) -> dict:
    """
    Main entry point. Returns ProceduralAttackBuilder-compatible shape.

    {
        "total": int,
        "fatal_count": int,
        "by_category": {"CROSS_CASE_AUTHORITY": N},
        "by_severity": {"FATAL": N},
        "summary": "...",
        "defects": [<CrossCaseDefect dicts>]
    }
    """
    log.info("Scanning " + case_id + " for cross-case authority misuse")

    registry = load_registry()
    if not registry:
        return _empty_result(case_id, "registry not loaded")

    if case_id not in registry:
        # Try resolving — case_id might be a slug
        log.warning("Case " + case_id + " not in registry as canonical")
        return _empty_result(case_id, "case not in registry")

    target_case = registry[case_id]

    # 1. Get related cases
    related_ids = target_case.get("related_cases", []) or []
    if not related_ids:
        log.info("  No related_cases — nothing to cross-check")
        return _empty_result(case_id, "no related cases")

    log.info("  Related cases: " + str(related_ids))

    # 2. Build patterns for each related case
    related_patterns = {}  # related_case_id -> [patterns]
    for rid in related_ids:
        if rid in registry:
            rdata = registry[rid]
            patterns = build_match_patterns(rdata)
            related_patterns[rid] = patterns
            log.info("  Patterns for " + rid + ": " + str(len(patterns)))

    if not related_patterns:
        return _empty_result(case_id, "no usable patterns from related cases")

    # 3. Find opposing-party filings in target case
    filings = find_opposing_filings(case_id, target_case)
    if not filings:
        return _empty_result(case_id, "no opposing filings to scan")

    # 4. Scan each filing for matches against related case patterns
    defects = []
    is_federal = case_id.startswith("fed:")
    candor_auths = CANDOR_AUTHORITIES_FED if is_federal else CANDOR_AUTHORITIES_PA

    for filename, text in filings:
        text_lower = text.lower()
        for related_id, patterns in related_patterns.items():
            for pat in patterns:
                try:
                    matches = list(re.finditer(pat, text, re.IGNORECASE))
                    for m in matches:
                        # Get surrounding context (60 chars each side)
                        start = max(0, m.start() - 60)
                        end = min(len(text), m.end() + 60)
                        context = text[start:end].replace("\n", " ").strip()

                        defect = CrossCaseDefect(
                            severity="FATAL",
                            category="CROSS_CASE_AUTHORITY",
                            defect=(
                                "Opposing counsel cited or referenced related case "
                                + related_id + " in this filing without disclosure of "
                                "its procedural status (potential mandamus/appeal pending). "
                                "Use of orders or proceedings from related cases without "
                                "candor regarding their challenged status violates "
                                "Pa.R.P.C. 3.3 / Fed. R. Prof. C. 3.3."
                            ),
                            authority=candor_auths + VOID_ORDER_AUTHORITIES,
                            motion=(
                                "Motion to Strike + Sanctions for Lack of Candor; "
                                "Motion for Reconsideration based on improper authority"
                            ),
                            source=filename,
                            related_case=related_id,
                            matched_text=context[:200],
                        )
                        defects.append(defect)
                except re.error as e:
                    log.warning("  Pattern error for " + pat + ": " + str(e))

    # 5. Aggregate
    total = len(defects)
    fatal_count = sum(1 for d in defects if d.severity == "FATAL")

    if total == 0:
        summary = "No cross-case authority misuse detected"
    else:
        summary = (
            str(total) + " cross-case authority defect(s): "
            "FATAL=" + str(fatal_count) + ". Opposing counsel referenced "
            "related cases (" + ", ".join(related_patterns.keys()) + ") "
            "in this case without procedural disclosure."
        )

    log.info("  " + summary)

    return {
        "total": total,
        "fatal_count": fatal_count,
        "by_category": {"CROSS_CASE_AUTHORITY": total} if total else {},
        "by_severity": {"FATAL": fatal_count} if fatal_count else {},
        "summary": summary,
        "defects": [asdict(d) for d in defects],
        "case_id": case_id,
        "related_cases_checked": list(related_patterns.keys()),
    }


def _empty_result(case_id: str, reason: str) -> dict:
    return {
        "total": 0,
        "fatal_count": 0,
        "by_category": {},
        "by_severity": {},
        "summary": "No cross-case scan performed: " + reason,
        "defects": [],
        "case_id": case_id,
        "related_cases_checked": [],
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python cross_case_authority_check.py <case_id>")
        print("Example: python cross_case_authority_check.py phila_ccp:260100000")
        sys.exit(1)

    case_id = sys.argv[1]
    result = scan(case_id)

    print()
    print("=" * 70)
    print("CROSS-CASE AUTHORITY SCAN: " + case_id)
    print("=" * 70)
    print("Total defects: " + str(result["total"]))
    print("Fatal: " + str(result["fatal_count"]))
    print("Summary: " + result["summary"])
    print()
    if result["defects"]:
        print("=== DEFECTS ===")
        for i, d in enumerate(result["defects"], 1):
            print()
            print("[" + str(i) + "] [" + d["severity"] + "/" + d["category"] + "]")
            print("    Defect:        " + d["defect"][:200])
            print("    Source filing: " + d["source"])
            print("    Related case:  " + d["related_case"])
            print("    Matched:       " + d["matched_text"])
            print("    Authority:     " + ", ".join(d["authority"][:3]))
            print("    Motion:        " + d["motion"])
