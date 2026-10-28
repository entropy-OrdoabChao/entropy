# -*- coding: utf-8 -*-
"""
universal_intake.py
===================
Unified ingestion: emails + PC documents + case filings → classifier row schema.

Reuses existing infrastructure:
  - beezle_ingest.DEFAULT_SOURCE_DIRS (~/Documents/pdf/misc, ~/Documents/pdf/Personal 2)
  - pipeline.document_scanner.DocumentScanner for PC docs (SHA1 dedup, caching)
  - pipeline.ocr_engine for PDF OCR fallback
  - cases_index/case_map.json for existing case filings

Output: unified list of "rows" in the classifier's expected schema:
    {
      "message_id":      str,               # unique ID
      "source_type":     "email"|"doc"|"case_filing",
      "source_path":     str,               # disk path or email id
      "date":            str,
      "from":            str,               # sender / author / source
      "subject":         str,
      "agency":          dict,
      "violations":      list,
      "case_references": list,
      "matched_case":    str|null,
      "body_preview":    str,                # full text truncated
      "body_length":     int,
      "sha1":            str,               # content hash for dedup
      "file_metadata":   dict,               # ext, size, mtime, doc_type, etc.
    }

Public API:
    gather(sources=("email","docs","cases"), ocr=True, dedup=True) -> List[Dict]
    gather_emails() -> List[Dict]
    gather_pc_documents(ocr=True) -> List[Dict]
    gather_case_filings(ocr=True) -> List[Dict]

CLI:
    python -m pipeline.universal_intake --sources email docs cases --output output/intake.json
"""

from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import argparse
import hashlib
import json
import re
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = _eroot()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.ocr_engine import extract_text_smart, is_ocr_available  # noqa: E402

# Silence noisy pdfminer/pdfplumber warnings on malformed PDFs
for _noisy in ("pdfminer", "pdfminer.pdffont", "pdfminer.pdfinterp",
               "pdfminer.cmapdb", "pdfminer.psparser", "pdfminer.layout",
               "pdfplumber", "PIL"):
    logging.getLogger(_noisy).setLevel(logging.ERROR)

log = logging.getLogger("universal_intake")
if not log.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

# ---------------------------------------------------------------------------
# Source directory configuration
# ---------------------------------------------------------------------------
DEFAULT_DOC_SOURCE_DIRS = [
    Path.home() / "Documents" / "pdf" / "misc",
    Path.home() / "Documents" / "pdf" / "Personal 2",
]
EMAIL_SCAN_DIR = ROOT / "output" / "email_scans"
CASE_MAP_PATH  = ROOT / "cases_index" / "case_map.json"
CACHE_DIR      = ROOT / "text_cache"
OUTPUT_DIR     = ROOT / "output"
INTAKE_OUT     = OUTPUT_DIR / "universal_intake_rows.json"


# ---------------------------------------------------------------------------
# EMAIL SOURCE — just re-emits email_scan rows in canonical shape
# ---------------------------------------------------------------------------
_EMAIL_CKPT = None   # set lazily to ROOT/output/universal_intake_email_ckpt.json


def gather_emails(scan_file: Optional[Path] = None, incremental: bool = False) -> List[Dict[str, Any]]:
    if scan_file is None:
        if not EMAIL_SCAN_DIR.exists():
            log.warning("email_scans dir not found: %s", EMAIL_SCAN_DIR)
            return []
        files = sorted(EMAIL_SCAN_DIR.glob("email_scan_*.json"))
        if not files:
            log.warning("no email_scan_*.json found")
            return []
        scan_file = files[-1]

    log.info("gather_emails: %s", scan_file)
    data = json.loads(scan_file.read_text(encoding="utf-8"))
    items = data.get("results") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []

    rows: List[Dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        body = it.get("body_preview") or ""
        rows.append({
            "message_id":      it.get("message_id"),
            "source_type":     "email",
            "source_path":     f"email:{it.get('message_id')}",
            "date":            it.get("date"),
            "from":            it.get("from"),
            "subject":         it.get("subject"),
            "agency":          it.get("agency") or {},
            "violations":      it.get("violations") or [],
            "case_references": it.get("case_references") or [],
            "deadlines":       it.get("deadlines") or [],
            "matched_case":    it.get("matched_case"),
            "body_preview":    body,
            "body_length":     it.get("body_length") or len(body),
            "sha1":            _sha1(it.get("subject", "") + body),
            "file_metadata":   {
                "attachments": it.get("attachments") or [],
            },
        })
    if incremental:
        # process only emails not seen on a prior run (robust dedup by message_id/sha1)
        ckpt = ROOT / "output" / "universal_intake_email_ckpt.json"
        seen = set()
        if ckpt.exists():
            try:
                seen = set(json.loads(ckpt.read_text(encoding="utf-8")).get("seen", []))
            except Exception:
                seen = set()
        key = lambda r: r.get("message_id") or r.get("sha1")
        new_rows = [r for r in rows if key(r) not in seen]
        seen.update(key(r) for r in rows if key(r))
        try:
            ckpt.write_text(json.dumps({"seen": sorted(seen)}), encoding="utf-8")
        except Exception:
            pass
        log.info("gather_emails: incremental %d new / %d total", len(new_rows), len(rows))
        return new_rows
    log.info("gather_emails: %d rows", len(rows))
    return rows


# ---------------------------------------------------------------------------
# PC DOCUMENTS SOURCE — reuses DocumentScanner
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Evidence detection — process ONLY case-related files. Everything else
# (treatises, books, statutes, references) is skipped silently.
# ---------------------------------------------------------------------------
import time as _ev_time


# Filename patterns that ALWAYS skip — work product, manuals, templates from
# unrelated systems. These would otherwise false-match other keywords.
EVIDENCE_BLACKLIST = [
    # SAP / enterprise system docs
    "prod_man", "_sd_en_", "_wp_en_", "_fl_en_", "_admin_wp_",
    "@1179", "@1175", "@1177",
    "monte_carlo", "managing_ideas", "proposal_intro", "strat_port",
    "pm_admin", "odata_sd", "gate review", "gate_review",
    "smart_search", "automation_flows", "appr_wf",
    # Generic generated PDFs
    "generatepdf",
    # Backup/recovery files
    "autorecovered", "(autorecovered)",
    # Legal REFERENCE works (not evidence)
    "treatise", "treatise on", "encyclopedia", "encyclopaedia",
    "corpus juris", "corpusjuris", "corpus_juris",
    "blacks law dictionary", "blacks_law_dictionary", "black's law dictionary",
    "bouvier", "bouvier's", "bouviers law",
    "restatement of", "restatement (",
    "hornbook", "casebook", "case book",
    "nutshell", "nuts and bolts",
    "law review", "l rev", "l. rev", "lrev",
    "hofstra", "harvard law", "yale law", "stanford law", "columbia law",
    "amjur", "am.jur", "am jur",
    "redemption manual", "cracking the code", "claim-your-securities",
    "consuetudo", "vellex", "pomeroy", "cooley on",
    "blackstone", "commentaries on the laws",
    "scotus", "supreme court of the united states",
    # Legislative handbooks / state legislative
    "leg-handbook", "leg handbook", "legislative manual",
    # Daily papers / news archives
    "thedalyeagle", "the daily eagle", "newspaper archive",
]

def _is_blacklisted(path: Path) -> bool:
    """Hard skip — these are never evidence regardless of other matches."""
    name = path.name.lower()
    if name.startswith("~$"):  # Office lockfile
        return True
    for pat in EVIDENCE_BLACKLIST:
        if pat in name:
            return True
    return False


# Defendants from your 4 active cases + common adversaries
EVIDENCE_DEFENDANTS = [
    # Active case defendants
    "equifax", "amerisave", "lippi", "state farm", "statefarm",
    "planet home", "planet-home", "planethome",
    # Common credit/financial adversaries
    "experian", "transunion", "trans union", "trans-union",
    "capital one", "capitalone", "cap one", "cap-one", "capone",
    "chase", "wells fargo", "wellsfargo",
    "bank of america", "bankofamerica", "boa ", "citi", "citibank",
    "discover", "amex", "american express", "americanexpress",
    "synchrony", "barclays", "us bank", "usbank",
    "greensky", "green sky", "green-sky",
    # Mortgage / housing
    "rocket mortgage", "rocketmortgage", "quicken", "freedom mortgage",
    "mr cooper", "mrcooper", "nationstar", "ocwen", "phh",
    "lakeview", "shellpoint", "newrez", "selene",
    # Debt collectors
    "midland", "portfolio recovery", "portfoliorecovery",
    "encore capital", "lvnv", "cavalry", "asset acceptance",
    # Insurance
    "geico", "progressive", "allstate", "liberty mutual", "libertymutual",
    "nationwide", "travelers", "farmers",
    # Utilities (often have FCRA/billing disputes)
    "peco", "pgw", "philadelphia gas", "comcast", "verizon",
    "t-mobile", "tmobile", "at&t", "att ", "spectrum",
    # Retail credit
    "sears", "kohl's", "kohls", "macy's", "macys", "best buy",
    "home depot", "lowes", "lowe's",
    # Other
    "lendingtree", "credit karma", "creditkarma",
    "penn state",  # in 1099 c penn state.pdf
]

# Litigation keywords — anything that looks like court filing
EVIDENCE_LITIGATION_KEYWORDS = [
    "complaint", "motion", "mtd", "order", "opposition", "response",
    "summons", "subpoena", "exhibit", "affidavit", "declaration",
    "stipulation", "notice", "praecipe", "petition", "brief",
    "memorandum", "memo of law", "memo in support",
    "transcript", "deposition", "interrogator",
    "discovery", "subpoena", "answer", "counterclaim", "crossclaim",
    "judgment", "ruling", "decree", "injunction",
    "removal", "remand", "dismissal", "default",
    "ifp", "in forma pauperis",
    "filing", "filed", "docket",
    "rfa", "rfp", "rog ", "interrog",
    "service of process", "proof of service", "certificate of service",
]

# Agency / regulator keywords
EVIDENCE_AGENCY_KEYWORDS = [
    "cfpb", "consumer financial", "consumerfinance",
    "hud", "fair housing", "fairhousing",
    "ftc", "federal trade",
    "irs", "internal revenue",
    "sec ", "securities and exchange",
    "occ", "comptroller of",
    "doj", "department of justice",
    "attorney general", "ag complaint",
    "pa ag", "pennsylvania ag",
    "phrc", "human relations",
    "phila ccp", "philadelphia",
    "dept of labor", "department of labor",
    "eeoc", "equal employment",
]

# Generic legal/financial doc keywords (broad net)
EVIDENCE_DOC_KEYWORDS = [
    # Disputes / demands
    "demand", "dispute", "violation", "deceptive", "fraud",
    "validation of debt", "vod ",
    "cease and desist", "ceaseand",
    "certified mail", "certifiedmail",
    "billing dispute", "billing error",
    "qwr", "qualified written request",
    # Credit / FCRA
    "credit report", "creditreport", "tradeline",
    "adverse action", "adverseaction",
    "denial", "denied", "rejection",
    "fcra", "fdcpa", "ecoa", "tila", "respa", "glba",
    "1681", "1692", "1691",
    # Statutes / theories
    "rico", "color of law", "1983",
    "bad faith",
    # Financial records (THIS WAS MISSING)
    "1099",        # 1099-C cancellation, 1099-A acquisition
    "1099-c", "1099c", "1099-a", "1099-misc",
    "statement",   # bank/credit card statements
    "stmt",
    "billing", "invoice",
    "balance", "account",
    "charge-off", "chargeoff", "charge off",
    "collection notice", "collectionnotice",
    "default notice", "defaultnotice",
    "foreclosure", "deed in lieu", "short sale",
    "refund", "credit balance",
    "payoff", "pay-off", "payoff statement",
    "amortization",
    "escrow",
    # Tax docs
    "irs notice", "tax lien", "tax bill",
    "schedule c", "schedule e", "1040",
    # Legal templates / forms
    "certificate of dishonor", "certificateofdishonor",
    "notary certificate", "notarycertificate",
    "affidavit",
    "promissory note",
    "negotiable instrument",
    # Contracts / agreements
    "settlement", "release", "agreement",
    "contract", "lease",
    "deed", "mortgage note", "title",
    # Court / process
    "service", "subpoena", "summons",
]

# Docket number patterns — any of these in a path = case-related
DOCKET_REGEX = re.compile(
    # 2:26-cv-00123 or 2-26-cv-00123 (federal)
    r"(\d{1,2}[:-]\d{2,4}[-_]?cv[-_]?\d{4,6}"
    # cv-12345 standalone (must include 'cv')
    r"|cv[-_]\d{4,6}"
    # explicit "case no" prefix
    r"|case[-_ ]no[.-_ ]\s*\d{4,}"
    # SEC filing format: 0000927628-25-000092 (10-K, 8-K)
    r"|\d{10}-\d{2}-\d{6})",
    re.IGNORECASE,
)

# Active dockets from registry (loaded lazily)
_ACTIVE_DOCKETS = None


def _load_active_dockets():
    """Pull docket numbers from cases_index/case_registry.json."""
    global _ACTIVE_DOCKETS
    if _ACTIVE_DOCKETS is not None:
        return _ACTIVE_DOCKETS
    _ACTIVE_DOCKETS = []
    reg = ROOT / "cases_index" / "case_registry.json"
    if not reg.exists():
        return _ACTIVE_DOCKETS
    try:
        import json as _ej
        data = _ej.loads(reg.read_text(encoding="utf-8"))
        cases = data.get("cases") if isinstance(data, dict) else data
        if isinstance(cases, list):
            for c in cases:
                cid = c.get("case_id") or c.get("id")
                if cid:
                    _ACTIVE_DOCKETS.append(cid)
                    # Numeric portion
                    m = re.search(r"(\\d:\\d{2}-cv-\\d{4,5})", cid)
                    if m: _ACTIVE_DOCKETS.append(m.group(1))
                    m = re.search(r"(\\d{6,})", cid)
                    if m: _ACTIVE_DOCKETS.append(m.group(1))
        elif isinstance(data, dict):
            for cid in data.keys():
                _ACTIVE_DOCKETS.append(cid)
                m = re.search(r"(\\d:\\d{2}-cv-\\d{4,5})", cid)
                if m: _ACTIVE_DOCKETS.append(m.group(1))
                m = re.search(r"(\\d{6,})", cid)
                if m: _ACTIVE_DOCKETS.append(m.group(1))
    except Exception:
        pass
    return _ACTIVE_DOCKETS


def is_evidence_file(path: Path, mtime_cutoff_seconds: float = 0,
                      strict: bool = False):
    """
    Return (is_evidence: bool, reason: str).

    Checks in order:
      0. Blacklist — never evidence (SAP files, ~$ lockfiles, etc.)
      1. Active docket number in path
      2. Tightened docket regex pattern
      3. Defendant name in path
      4. Litigation keyword
      5. Agency keyword
      6. Doc keyword (financial/legal — only if not strict)
    
    mtime fallback REMOVED — was matching too many random recent files.
    """
    # 0. Blacklist first
    if _is_blacklisted(path):
        return False, "blacklist"

    name_lower = path.name.lower()
    parent_lower = str(path.parent).lower()
    full_lower = str(path).lower()

    # 1. Active docket in path or parent folder
    for docket in _load_active_dockets():
        if not docket:
            continue
        d_low = docket.lower()
        if d_low in full_lower:
            return True, f"docket:{docket}"
        # Also check parent folder names for case identifiers
        # e.g., file in folder named "Lippi" or "26-cv-00123"
        m = re.search(r"(\d:\d{2}-cv-\d{4,5})", d_low)
        if m and m.group(1) in full_lower:
            return True, f"docket:{m.group(1)}"

    # 2. Tightened docket regex
    if DOCKET_REGEX.search(name_lower):
        return True, "docket_pattern"

    # 3. Defendant names — must be word-boundary or in folder name
    for d in EVIDENCE_DEFENDANTS:
        if d in name_lower or d in parent_lower:
            return True, f"defendant:{d}"

    # 4. Litigation keyword
    for kw in EVIDENCE_LITIGATION_KEYWORDS:
        if kw in name_lower:
            return True, f"litigation:{kw}"

    # 5. Agency keyword
    for kw in EVIDENCE_AGENCY_KEYWORDS:
        if kw in name_lower:
            return True, f"agency:{kw}"

    if strict:
        return False, "no_match_strict"

    # 6. Doc keyword (financial / legal)
    for kw in EVIDENCE_DOC_KEYWORDS:
        if kw in name_lower:
            return True, f"doc_kw:{kw}"

    return False, "no_match"


def gather_pc_documents(
    source_dirs=None,
    ocr=True,
    use_scanner_cache=False,
    progress_every=10,
    pdf_timeout_sec=120,
    checkpoint=True,
    max_files=0,
    max_pdf_mb=20.0,            # skip PDFs > this (default 20 MB now — evidence rarely huge)
    evidence_only=True,         # ONLY process case-related files
    evidence_strict=False,      # if True, no doc-keyword fallback — must have docket/defendant
    mtime_months=18,            # files older than this skipped (0 = no age filter)
    newest_first=True,
):
    """
    Walk real document folders. Adds:
      - progress logging every N files
      - per-PDF timeout (skips PDFs that take longer than pdf_timeout_sec)
      - checkpoint file to resume between runs
      - max_files cap (0 = unlimited)
    """
    import json as _json
    import time as _time
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

    source_dirs = source_dirs or DEFAULT_DOC_SOURCE_DIRS
    source_dirs = [Path(d) for d in source_dirs if Path(d).exists()]
    if not source_dirs:
        log.warning("gather_pc_documents: no source dirs exist")
        return []

    log.info("gather_pc_documents: scanning %d dirs (ocr=%s, timeout=%ds)",
             len(source_dirs), ocr, pdf_timeout_sec)

    # Checkpoint setup
    ckpt_path = ROOT / "output" / "intake_checkpoint.json"
    done_shas = set()
    if checkpoint and ckpt_path.exists():
        try:
            ckpt_data = _json.loads(ckpt_path.read_text(encoding="utf-8"))
            done_shas = set(ckpt_data.get("done_shas", []))
            log.info("checkpoint: %d files already processed (will skip)", len(done_shas))
        except Exception as e:
            log.debug("checkpoint read failed: %s", e)

    # Try DocumentScanner only if explicitly requested (it does heavy
    # extraction up-front and can take an hour with no progress logging).
    scanner_docs = []
    if use_scanner_cache:
        log.info("use_scanner_cache=True: invoking DocumentScanner.scan() "
                 "(no progress until it returns)")
        try:
            from pipeline.document_scanner import DocumentScanner
            scanner = DocumentScanner(source_dirs=source_dirs, cache_dir=CACHE_DIR)
            scanner_docs = scanner.scan()
            log.info("DocumentScanner returned %d docs", len(scanner_docs))
        except Exception as e:
            log.warning("DocumentScanner failed (%s), falling back to direct walk", e)
            scanner_docs = []
    else:
        log.info("use_scanner_cache=False (direct walk for fast progress)")

    rows = []
    pool = ThreadPoolExecutor(max_workers=1) if ocr else None
    t_start = _time.time()
    skipped_ckpt = 0
    timed_out = 0

    def _ocr_with_timeout(pdf_path, timeout):
        """Run extract_text_smart with a hard timeout."""
        if pool is None:
            return extract_text_smart(pdf_path, min_text_threshold=50)
        future = pool.submit(extract_text_smart, pdf_path, 50)
        try:
            return future.result(timeout=timeout)
        except FuturesTimeout:
            future.cancel()
            return None  # signal timeout

    # Build the file list
    files_to_process = []
    if scanner_docs:
        for d in scanner_docs:
            meta = d.to_dict() if hasattr(d, "to_dict") else dict(d)
            files_to_process.append(("scanner", meta, None))
    else:
        log.info("walking source dirs (evidence-only=%s, strict=%s, mtime_months=%d): %s",
                 evidence_only, evidence_strict, mtime_months,
                 [str(d) for d in source_dirs])
        import time as _walk_t
        _t0 = _walk_t.time()

        # Compute mtime cutoff
        mtime_cutoff = 0.0
        if mtime_months and mtime_months > 0:
            mtime_cutoff = _walk_t.time() - (mtime_months * 30 * 86400)

        skipped_too_big   = 0
        skipped_not_evid  = 0
        skipped_too_old   = 0  # not used directly — folded into is_evidence
        evidence_reasons  = {}

        for src_dir in source_dirs:
            log.info("  walking %s ...", src_dir)
            count_in_dir = 0
            for p in src_dir.rglob("*"):
                if not p.is_file():
                    continue
                if p.suffix.lower() not in (".pdf", ".docx", ".doc", ".txt",
                                              ".md", ".html", ".htm", ".eml", ".msg"):
                    continue

                # Evidence filter
                if evidence_only:
                    is_evid, reason = is_evidence_file(
                        p, mtime_cutoff, strict=evidence_strict
                    )
                    if not is_evid:
                        skipped_not_evid += 1
                        continue
                    evidence_reasons[reason.split(":", 1)[0]] = \
                        evidence_reasons.get(reason.split(":", 1)[0], 0) + 1

                # Hard size cap
                try:
                    size = p.stat().st_size
                except Exception:
                    size = 0
                if p.suffix.lower() == ".pdf" and max_pdf_mb > 0:
                    if size > max_pdf_mb * 1024 * 1024:
                        skipped_too_big += 1
                        log.debug("skip oversized PDF (%.1f MB): %s",
                                  size/1024/1024, p.name)
                        continue

                files_to_process.append(("direct", None, p))
                count_in_dir += 1
                if count_in_dir % 50 == 0:
                    log.info("    %s: %d evidence files queued (%d non-evidence skipped, %d oversized)",
                             src_dir.name, count_in_dir,
                             skipped_not_evid, skipped_too_big)
            log.info("  done %s: %d evidence files (%d non-evidence skipped, %d oversized) in %.1fs",
                     src_dir.name, count_in_dir, skipped_not_evid,
                     skipped_too_big, _walk_t.time() - _t0)

        if evidence_reasons:
            log.info("  evidence match reasons:")
            for r, n in sorted(evidence_reasons.items(), key=lambda x: -x[1]):
                log.info("    %-15s %d files", r, n)

        # Sort newest-first
        if newest_first and files_to_process:
            try:
                files_to_process.sort(
                    key=lambda x: x[2].stat().st_mtime if x[2] else 0,
                    reverse=True,
                )
                log.info("  sorted %d files by mtime DESC (newest first)",
                         len(files_to_process))
            except Exception as e:
                log.debug("sort failed: %s", e)


    log.info("gather_pc_documents: %d candidate files", len(files_to_process))
    if max_files:
        files_to_process = files_to_process[:max_files]
        log.info("(capped at --max-files %d)", max_files)

    for i, (mode, meta, p) in enumerate(files_to_process, 1):
        try:
            if mode == "scanner":
                sha1 = meta.get("sha1")
                if checkpoint and sha1 and sha1 in done_shas:
                    skipped_ckpt += 1
                    continue
                text_path = meta.get("text_path")
                body = ""
                if text_path and Path(text_path).exists():
                    try:
                        body = Path(text_path).read_text(encoding="utf-8", errors="ignore")
                    except Exception:
                        body = ""
                if ocr and len(body.strip()) < 50:
                    pdf_path = Path(meta.get("path", ""))
                    if pdf_path.exists():
                        ocr_text = _ocr_with_timeout(pdf_path, pdf_timeout_sec)
                        if ocr_text is None:
                            log.warning("OCR timeout (%ds): %s", pdf_timeout_sec, pdf_path.name)
                            timed_out += 1
                        elif len(ocr_text.strip()) > len(body.strip()):
                            body = ocr_text
                            if text_path:
                                try:
                                    Path(text_path).write_text(body, encoding="utf-8")
                                except Exception:
                                    pass
                rows.append(_doc_to_row(meta, body))
                if sha1:
                    done_shas.add(sha1)
            else:  # direct
                sha1 = _sha1_file(p)
                if checkpoint and sha1 in done_shas:
                    skipped_ckpt += 1
                    continue
                body = ""
                if ocr:
                    ocr_text = _ocr_with_timeout(p, pdf_timeout_sec)
                    if ocr_text is None:
                        log.warning("OCR timeout (%ds): %s", pdf_timeout_sec, p.name)
                        timed_out += 1
                        body = ""
                    else:
                        body = ocr_text
                stat = p.stat()
                meta = {
                    "path":       str(p),
                    "filename":   p.name,
                    "extension":  p.suffix.lower(),
                    "size_bytes": stat.st_size,
                    "mtime":      stat.st_mtime,
                    "sha1":       sha1,
                    "dockets":    [],
                    "parties":    [],
                    "doc_type":   "unknown",
                    "author":     "unknown",
                    "is_reference":     False,
                    "is_personal_life": False,
                    "is_filer_case":   False,
                }
                rows.append(_doc_to_row(meta, body))
                done_shas.add(sha1)
        except Exception as e:
            log.error("file failed (%s): %s", p if mode == "direct" else meta.get("path"), e)

        # Progress
        if i % progress_every == 0 or i == len(files_to_process):
            elapsed = _time.time() - t_start
            rate = i / elapsed if elapsed > 0 else 0
            remaining = (len(files_to_process) - i) / rate if rate > 0 else 0
            log.info("  progress: %d/%d (%.1f files/sec, ~%dm %ds remaining, %d ckpt-skip, %d timeouts)",
                     i, len(files_to_process), rate,
                     int(remaining // 60), int(remaining % 60),
                     skipped_ckpt, timed_out)

            # Save checkpoint periodically
            if checkpoint:
                try:
                    ckpt_path.parent.mkdir(parents=True, exist_ok=True)
                    ckpt_path.write_text(_json.dumps({
                        "done_shas": sorted(done_shas),
                        "last_update": _time.time(),
                    }), encoding="utf-8")
                except Exception:
                    pass

    if pool:
        pool.shutdown(wait=False)

    elapsed = _time.time() - t_start
    log.info("gather_pc_documents: %d rows in %.1fs (%d skipped, %d timeouts)",
             len(rows), elapsed, skipped_ckpt, timed_out)
    return rows


def _doc_to_row(meta: Dict[str, Any], body: str) -> Dict[str, Any]:
    p = Path(meta.get("path", ""))
    return {
        "message_id":      f"doc:{meta.get('sha1', p.name)}",
        "source_type":     "doc",
        "source_path":     str(p),
        "date":            _fmt_mtime(meta.get("mtime")),
        "from":            meta.get("author") or "unknown",
        "subject":         p.stem,
        "agency":          {},  # docs don't have "agency" field
        "violations":      [],
        "case_references": meta.get("dockets") or [],
        "deadlines":       [],
        "matched_case":    (meta.get("dockets") or [None])[0] if meta.get("dockets") else None,
        "body_preview":    (body or "")[:10000],
        "body_length":     len(body or ""),
        "sha1":            meta.get("sha1"),
        "file_metadata": {
            "extension":        meta.get("extension"),
            "size_bytes":       meta.get("size_bytes"),
            "doc_type":         meta.get("doc_type"),
            "parties":          meta.get("parties") or [],
            "is_reference":     bool(meta.get("is_reference")),
            "is_personal_life": bool(meta.get("is_personal_life")),
            "is_filer_case":   bool(meta.get("is_filer_case")),
            "filename":         meta.get("filename"),
        },
    }


# ---------------------------------------------------------------------------
# CASE FILINGS SOURCE — walks case_map.json
# ---------------------------------------------------------------------------
def gather_case_filings(ocr: bool = True) -> List[Dict[str, Any]]:
    if not CASE_MAP_PATH.exists():
        log.warning("case_map.json not found: %s", CASE_MAP_PATH)
        return []

    log.info("gather_case_filings: %s", CASE_MAP_PATH)
    case_map = json.loads(CASE_MAP_PATH.read_text(encoding="utf-8"))

    rows: List[Dict[str, Any]] = []
    for case_id, entry in case_map.items():
        if not isinstance(entry, dict):
            continue
        for kind in ("filings", "orders", "drafts", "exhibits",
                     "mtds", "oppositions", "court_notices", "documents"):
            bucket = entry.get(kind)
            if isinstance(bucket, dict):
                bucket = list(bucket.values())
            if not isinstance(bucket, list):
                continue

            for d in bucket:
                if isinstance(d, str):
                    path_str = d
                    meta_extra = {}
                elif isinstance(d, dict):
                    path_str = d.get("path") or d.get("file") or d.get("filepath")
                    meta_extra = d
                else:
                    continue
                if not path_str:
                    continue

                p = Path(path_str)
                if not p.exists():
                    continue

                body = extract_text_smart(p, min_text_threshold=50) if ocr else ""
                try:
                    stat = p.stat()
                    sha1 = _sha1_file(p)
                except Exception:
                    stat = None
                    sha1 = None

                rows.append({
                    "message_id":      f"case:{case_id}:{sha1 or p.name}",
                    "source_type":     "case_filing",
                    "source_path":     str(p),
                    "date":            _fmt_mtime(stat.st_mtime) if stat else None,
                    "from":            meta_extra.get("author") or "court_or_party",
                    "subject":         meta_extra.get("title") or p.stem,
                    "agency":          {},
                    "violations":      meta_extra.get("violations") or [],
                    "case_references": [case_id],
                    "deadlines":       [],
                    "matched_case":    case_id,
                    "body_preview":    (body or "")[:10000],
                    "body_length":     len(body or ""),
                    "sha1":            sha1,
                    "file_metadata": {
                        "case_id":  case_id,
                        "kind":     kind,
                        "extension": p.suffix.lower(),
                        "size_bytes": stat.st_size if stat else None,
                        **{k: v for k, v in meta_extra.items()
                           if k not in ("path", "file", "filepath")},
                    },
                })
    log.info("gather_case_filings: %d rows across %d cases",
             len(rows), len(case_map))
    return rows


# ---------------------------------------------------------------------------
# UNIFIED GATHER
# ---------------------------------------------------------------------------
def gather(
    sources=("email", "docs", "cases"),
    ocr: bool = True,
    dedup: bool = True,
    output_path: Optional[Path] = None,
    incremental: bool = False,
) -> Dict[str, Any]:
    """Run all configured sources and return a single unified result."""
    if ocr:
        if is_ocr_available():
            log.info("OCR is available (pytesseract + pdf2image + tesseract binary)")
        else:
            log.warning("OCR requested but NOT available. Scanned PDFs will be skipped.")
            log.warning("Install: pip install pytesseract pdf2image pillow")
            log.warning("  + tesseract binary + poppler")

    all_rows: List[Dict[str, Any]] = []
    source_counts: Dict[str, int] = {}

    if "email" in sources:
        rows = gather_emails(incremental=incremental)
        all_rows.extend(rows)
        source_counts["email"] = len(rows)

    if "docs" in sources:
        extra = globals().get("_GATHER_PC_KWARGS", {}) or {}
        rows = gather_pc_documents(ocr=ocr, **extra)
        all_rows.extend(rows)
        source_counts["docs"] = len(rows)

    if "cases" in sources:
        rows = gather_case_filings(ocr=ocr)
        all_rows.extend(rows)
        source_counts["case_filings"] = len(rows)

    # Dedup by sha1 (skip emails since their sha1 is subject+body-based)
    if dedup:
        before = len(all_rows)
        seen = set()
        deduped = []
        for r in all_rows:
            key = r.get("sha1")
            if not key or key in seen:
                if not key:
                    # no sha1 — keep (probably email)
                    deduped.append(r)
                continue
            seen.add(key)
            deduped.append(r)
        log.info("dedup: %d → %d rows", before, len(deduped))
        all_rows = deduped

    summary = {
        "generated":     datetime.now().isoformat(),
        "sources":       list(sources),
        "source_counts": source_counts,
        "total_rows":    len(all_rows),
        "ocr_enabled":   bool(ocr),
        "ocr_available": is_ocr_available(),
        "rows":          all_rows,
    }

    if output_path is None:
        output_path = INTAKE_OUT
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    log.info("wrote %s (%d rows)", output_path, len(all_rows))
    return summary


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", errors="replace")).hexdigest()


def _sha1_file(p: Path) -> str:
    h = hashlib.sha1()
    try:
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""


def _fmt_mtime(mtime) -> Optional[str]:
    if not mtime:
        return None
    try:
        return datetime.fromtimestamp(mtime).isoformat()
    except Exception:
        return str(mtime)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--sources", nargs="+",
                   default=["email", "docs", "cases"],
                   choices=["email", "docs", "cases"])
    p.add_argument("--no-ocr", action="store_true")
    p.add_argument("--no-dedup", action="store_true")
    p.add_argument("--output", type=str, default=str(INTAKE_OUT))
    args = p.parse_args()

    gather(
        sources=tuple(args.sources),
        ocr=not args.no_ocr,
        dedup=not args.no_dedup,
        output_path=Path(args.output),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
