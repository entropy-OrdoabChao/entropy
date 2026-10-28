"""
generation/finalize_brief.py — Block 8

Takes a freshly-generated draft brief (from draft_for_case) and produces
a court-ready PDF bundle. Automates everything in pre_filing_checklist
EXCEPT external citation verification (Shepardizing) and physical mailing.

11 AUTOMATIONS:
  1. Strip verification checklist banner
  2. Add II.C heading manually if missing
  3. Update Dated: line to today
  4. Caption verification (read from registry)
  5. Citation format conversion (internal IDs → court format)
  6. Constitutional canonical insertion (when constitutional fires)
  7. Claim ordering per registry claim_priority
  8. Exhibit bundling (brief + scorecard + matrix → bundle)
  9. Certificate of Service (auto-generated from registry counsel_of_record)
 10. PDF conversion via docx2pdf
 11. Format compliance check

Plus optional --audit flag: runs Hermes as readability lint pass.
NEVER rewrites — flags only. Output: readability_report.txt.

USAGE:
  python -m generation.finalize_brief phila_ccp:260100000
  python -m generation.finalize_brief phila_ccp:260100000 --audit
  python -m generation.finalize_brief phila_ccp:260100000 --no-pdf  (skip PDF step)
  python -m generation.finalize_brief phila_ccp:260100000 --brief <path/to/draft.docx>

OUTPUT:
  <home>\\output\\filed_ready\\<safe_id>_<timestamp>\\
    reply_brief.docx              (cleaned)
    reply_brief.pdf               (if PDF enabled)
    scorecard_exhibit.docx + .pdf
    violations_matrix.docx + .pdf
    certificate_of_service.docx + .pdf
    readability_report.txt        (if --audit)
    READY_TO_FILE.txt             (manifest)
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
import argparse
import json
import logging
import shutil
import sys
import re
from datetime import datetime
from pathlib import Path

# Add repo root to sys.path for sibling imports
_REPO = _BEEZLE_ROOT
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from docx import Document
from docx.shared import Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH

from generation._finalize_canonicals import (
    EXACT_CITATION_REWRITES,
    CITATION_REGEX_REWRITES,
    CONSTITUTIONAL_CANONICALS,
    CONSTITUTIONAL_BY_SOURCE,
    reformat_citation,
)

log = logging.getLogger("finalize_brief")

REGISTRY      = (_BEEZLE_ROOT / "cases_index" / "case_registry.json")
OUTPUT_BASE   = (_BEEZLE_ROOT / "output" / "filed_ready")
DRAFTS_DIR    = (_BEEZLE_ROOT / "output" / "reply_briefs")

# Hermes endpoint for --audit (no rewrite, lint only)
HERMES_URL    = "http://localhost:8765/audit"


# ═════════════════════════════════════════════════════════════════════════
# UTILITIES
# ═════════════════════════════════════════════════════════════════════════

def _safe_id(canonical_id: str) -> str:
    return canonical_id.replace(":", "_").replace("/", "_")


def _load_registry_entry(canonical_id: str) -> dict:
    if not REGISTRY.exists():
        raise FileNotFoundError(f"registry missing: {REGISTRY}")
    reg = json.loads(REGISTRY.read_text(encoding="utf-8"))
    if canonical_id not in reg:
        raise KeyError(f"case not in registry: {canonical_id}")
    return reg[canonical_id]


def _find_latest_brief(canonical_id: str) -> Path:
    """Locate the most recent draft brief docx for this case."""
    sid = _safe_id(canonical_id)
    candidates = sorted(
        DRAFTS_DIR.glob(f"reply_brief_{sid}_*.docx"),
        key=lambda p: p.stat().st_mtime, reverse=True
    )
    if not candidates:
        raise FileNotFoundError(f"no draft brief found for {canonical_id}")
    return candidates[0]


def _find_companion(canonical_id: str, prefix: str, timestamp: str = None) -> Path | None:
    """Locate scorecard or matrix companion file for a brief."""
    sid = _safe_id(canonical_id)
    if timestamp:
        path = DRAFTS_DIR / f"{prefix}_{sid}_{timestamp}.docx"
        if path.exists():
            return path
    # Fall back to most recent companion
    candidates = sorted(
        DRAFTS_DIR.glob(f"{prefix}_{sid}_*.docx"),
        key=lambda p: p.stat().st_mtime, reverse=True
    )
    return candidates[0] if candidates else None


def _extract_timestamp(brief_path: Path) -> str:
    m = re.search(r"_(\d{8}_\d{6})\.docx$", brief_path.name)
    return m.group(1) if m else datetime.now().strftime("%Y%m%d_%H%M%S")


# ═════════════════════════════════════════════════════════════════════════
# AUTOMATION 1: Strip verification checklist banner
# ═════════════════════════════════════════════════════════════════════════

def strip_verification_checklist(doc: Document) -> int:
    """Remove all paragraphs from VERIFICATION CHECKLIST onward."""
    body = doc.element.body
    paragraphs = list(doc.paragraphs)
    strip_from = None
    for i, p in enumerate(paragraphs):
        if "VERIFICATION CHECKLIST" in p.text.upper():
            strip_from = i
            break
    if strip_from is None:
        return 0
    removed = 0
    for p in paragraphs[strip_from:]:
        p._element.getparent().remove(p._element)
        removed += 1
    return removed


# ═════════════════════════════════════════════════════════════════════════
# AUTOMATION 2: Add II.C heading if missing
# ═════════════════════════════════════════════════════════════════════════

def ensure_section_iic_heading(doc: Document) -> bool:
    """Insert 'II.C. RELIEF DEMANDED ON THE CLAIMS' heading before the
    'PLAINTIFF HAS ESTABLISHED' ALL CAPS punchline if not already present."""
    paragraphs = list(doc.paragraphs)
    punchline_idx = None
    for i, p in enumerate(paragraphs):
        if p.text.strip().startswith("PLAINTIFF HAS ESTABLISHED"):
            punchline_idx = i
            break
    if punchline_idx is None:
        return False
    # Check 5 paragraphs above for existing heading
    for j in range(max(0, punchline_idx - 5), punchline_idx):
        t = paragraphs[j].text.strip().upper()
        if "II.C" in t and "RELIEF DEMANDED" in t:
            return False  # already there

    # Insert new paragraph BEFORE punchline
    punchline_para = paragraphs[punchline_idx]
    new_p = punchline_para.insert_paragraph_before("II.C. RELIEF DEMANDED ON THE CLAIMS")
    # Bold + size 13
    for run in new_p.runs:
        run.font.bold = True
        run.font.size = Pt(13)
    if not new_p.runs:
        run = new_p.add_run("")
        run.font.bold = True
    return True


# ═════════════════════════════════════════════════════════════════════════
# AUTOMATION 3: Update Dated: line
# ═════════════════════════════════════════════════════════════════════════

def update_dated_line(doc: Document) -> bool:
    """Replace 'Dated: <anything>' with today's formatted date."""
    today = datetime.now().strftime("%B %d, %Y")
    for p in doc.paragraphs:
        if p.text.strip().startswith("Dated:"):
            for run in p.runs:
                run.text = ""
            if p.runs:
                p.runs[0].text = f"Dated: {today}"
            else:
                p.add_run(f"Dated: {today}")
            return True
    return False


# ═════════════════════════════════════════════════════════════════════════
# AUTOMATION 4: Caption verification (warn only)
# ═════════════════════════════════════════════════════════════════════════

def verify_caption(doc: Document, registry_entry: dict) -> list[str]:
    """Check first ~10 paragraphs for caption fields. Return list of warnings."""
    warnings = []
    head_text = "\n".join(p.text for p in doc.paragraphs[:15])
    expected_caption = registry_entry.get("case_caption", "")
    if expected_caption and expected_caption.upper() not in head_text.upper():
        warnings.append(
            f"caption '{expected_caption}' not found in first 15 paragraphs"
        )
    return warnings


# ═════════════════════════════════════════════════════════════════════════
# AUTOMATION 5: Citation format conversion
# ═════════════════════════════════════════════════════════════════════════

def convert_citation_formats(doc: Document) -> int:
    """Apply EXACT_CITATION_REWRITES + CITATION_REGEX_REWRITES across all
    paragraphs. Returns number of paragraphs modified."""
    count = 0
    for p in doc.paragraphs:
        original = p.text
        rewritten = reformat_citation(original)
        if rewritten != original:
            # Apply by clearing all runs and rebuilding the paragraph text
            for run in p.runs:
                run.text = ""
            if p.runs:
                p.runs[0].text = rewritten
            else:
                p.add_run(rewritten)
            count += 1
    # Also process tables
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    original = p.text
                    rewritten = reformat_citation(original)
                    if rewritten != original:
                        for run in p.runs:
                            run.text = ""
                        if p.runs:
                            p.runs[0].text = rewritten
                        else:
                            p.add_run(rewritten)
                        count += 1
    return count


# ═════════════════════════════════════════════════════════════════════════
# AUTOMATION 6: Constitutional canonical insertion
# ═════════════════════════════════════════════════════════════════════════

def insert_constitutional_canonicals(doc: Document) -> int:
    """When constitutional duties fire, insert canonical case parentheticals
    (Mullane, Mathews, etc.) below the punchline.

    Detects: paragraph starting with 'PLAINTIFF HAS ESTABLISHED N CONSTITUTIONAL'
    Inserts: a new paragraph below it with 'See, e.g., Mullane v. Central
    Hanover Bank & Trust Co., 339 U.S. 306 (1950); Mathews v. Eldridge,
    424 U.S. 319 (1976); ...'
    """
    paragraphs = list(doc.paragraphs)
    for i, p in enumerate(paragraphs):
        text = p.text.strip()
        if not text.startswith("PLAINTIFF HAS ESTABLISHED"):
            continue
        m = re.match(r"PLAINTIFF HAS ESTABLISHED (\d+) CONSTITUTIONAL", text)
        if not m or m.group(1) == "0":
            continue
        # Check if next paragraph already has 'See, e.g., Mullane'
        if i + 1 < len(paragraphs) and "Mullane" in paragraphs[i + 1].text:
            return 0
        # Insert canonical citation paragraph
        cites = [
            "Mullane v. Central Hanover Bank & Trust Co., 339 U.S. 306 (1950)",
            "Mathews v. Eldridge, 424 U.S. 319 (1976)",
            "Fuentes v. Shevin, 407 U.S. 67 (1972)",
            "International Shoe Co. v. Washington, 326 U.S. 310 (1945)",
            "Burger King Corp. v. Rudzewicz, 471 U.S. 462 (1985)",
        ]
        cite_text = "See, e.g., " + "; ".join(cites) + "."

        # Insert AFTER the punchline (before the next paragraph)
        if i + 1 < len(paragraphs):
            anchor = paragraphs[i + 1]
            new_p = anchor.insert_paragraph_before(cite_text)
        else:
            new_p = doc.add_paragraph(cite_text)
        for run in new_p.runs:
            run.font.italic = True
            run.font.size = Pt(11)
        return 1
    return 0


# ═════════════════════════════════════════════════════════════════════════
# AUTOMATION 7: Claim ordering per registry
# ═════════════════════════════════════════════════════════════════════════

def reorder_claims(doc: Document, registry_entry: dict) -> int:
    """Reorder Section II.A subsection paragraphs per registry claim_priority.

    Detects: subsections under Section II.A starting with 'II.A.N. CLAIM_NAME'
    Reorders them per the claim_priority list.

    NOTE: this is the most fragile automation. If brief structure changes,
    this needs updating. Returns count of subsections reordered (0 = none
    moved or no changes needed).
    """
    priority = registry_entry.get("claim_priority", [])
    if not priority:
        return 0
    # Find Section II.A subsection boundaries
    paragraphs = list(doc.paragraphs)
    section_starts = []
    section_end = None
    for i, p in enumerate(paragraphs):
        t = p.text.strip()
        if re.match(r"^II\.A\.\d+\.?\s+", t):
            section_starts.append(i)
        elif section_starts and (t.startswith("II.B") or t.startswith("II.C")
                                 or t.startswith("III.")):
            section_end = i
            break
    if not section_starts or section_end is None:
        log.info("claim reorder: no II.A subsections found, skipping")
        return 0
    # We have subsection start positions but reordering paragraphs in-place
    # in python-docx is fragile. For now, only LOG what would be reordered.
    # Real reorder requires deepcopy + tree manipulation.
    log.info(f"claim reorder: detected {len(section_starts)} II.A subsections, "
             f"priority list has {len(priority)} entries (logging only)")
    return 0


# ═════════════════════════════════════════════════════════════════════════
# AUTOMATION 9: Certificate of Service generation
# ═════════════════════════════════════════════════════════════════════════

def build_certificate_of_service(canonical_id: str, registry_entry: dict,
                                 filing_date: str) -> Document:
    """Build a separate COS docx from registry counsel_of_record."""
    counsel = registry_entry.get("counsel_of_record") or []
    case_caption = registry_entry.get("case_caption", "")
    docket = registry_entry.get("docket", canonical_id)
    court  = registry_entry.get("filing_court_address", "")

    cos = Document()
    title = cos.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = title.add_run("CERTIFICATE OF SERVICE")
    run.font.bold = True
    run.font.size = Pt(13)
    cos.add_paragraph()

    if court:
        cap = cos.add_paragraph()
        cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
        cap.add_run(court).font.size = Pt(11)
        cos.add_paragraph()

    if case_caption:
        cap2 = cos.add_paragraph()
        cap2.alignment = WD_ALIGN_PARAGRAPH.CENTER
        cap2.add_run(case_caption).font.bold = True
        cos.add_paragraph(f"Docket No.: {docket}").alignment = WD_ALIGN_PARAGRAPH.CENTER
        cos.add_paragraph()

    body = cos.add_paragraph(
        f"I, Filer Babah Filer, hereby certify that on {filing_date}, "
        f"I served a true and correct copy of the foregoing Plaintiff's Reply "
        f"in Support of Motion for Reconsideration on counsel of record by "
        f"first-class United States mail, postage prepaid, addressed as follows:"
    )

    cos.add_paragraph()
    if not counsel:
        cos.add_paragraph("[NO COUNSEL OF RECORD POPULATED IN REGISTRY — "
                          "UPDATE counsel_of_record FIELD BEFORE FILING]")
    else:
        for c in counsel:
            cos.add_paragraph(c.get("name", ""))
            if c.get("firm"):
                cos.add_paragraph(c["firm"])
            for line in (c.get("address", "") or "").split("\n"):
                if line.strip():
                    cos.add_paragraph(line)
            if c.get("email"):
                cos.add_paragraph(f"Email: {c['email']}")
            if c.get("role"):
                p = cos.add_paragraph(f"({c['role']})")
                for run in p.runs:
                    run.font.italic = True
            cos.add_paragraph()

    cos.add_paragraph()
    cos.add_paragraph()
    sig = cos.add_paragraph()
    sig.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    sig.add_run("/s/ Filer Babah Filer").font.bold = True
    sig2 = cos.add_paragraph()
    sig2.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    sig2.add_run("Filer Babah Filer\nPlaintiff, pro se\n2522 S Darien St\n"
                 "Anytown, ST 00000\n(555) 555-0100")
    cos.add_paragraph()
    cos.add_paragraph(f"Dated: {filing_date}").alignment = WD_ALIGN_PARAGRAPH.RIGHT
    return cos


# ═════════════════════════════════════════════════════════════════════════
# AUTOMATION 10: PDF conversion
# ═════════════════════════════════════════════════════════════════════════

# === FINALIZE_V2_SAFE_FIXES ===

def strip_engine_metadata(doc) -> int:
    """FIX 1: Strip engine metadata leakage (Domain/Engine confidence lines).

    Removes paragraphs that contain ONLY engine internal metadata:
      - "Domain(s): contract, insurance. Engine confidence: low."
      - "Engine confidence: high."
      - "Domain(s): xxx."
    These should NEVER appear in a filed brief — they signal uncertainty
    to the court.
    """
    import re as _re
    # Match patterns that indicate engine metadata. The line must be
    # ONLY metadata (no other content).
    metadata_patterns = [
        _re.compile(r"^\s*Domain\(s\):.*?Engine confidence:\s*\w+\s*\.?\s*$", _re.IGNORECASE),
        _re.compile(r"^\s*Domain\(s\):.*?\.?\s*$", _re.IGNORECASE),
        _re.compile(r"^\s*Engine confidence:\s*\w+\s*\.?\s*$", _re.IGNORECASE),
    ]
    paragraphs = list(doc.paragraphs)
    removed = 0
    for p in paragraphs:
        text = p.text.strip()
        if not text:
            continue
        for pat in metadata_patterns:
            if pat.match(text):
                p._element.getparent().remove(p._element)
                removed += 1
                break
    return removed


def dedup_appendix_a(doc) -> int:
    """FIX 2: Dedup Appendix A production-demand bullet list.

    Locates the APPENDIX A section, walks its bullet items, removes
    duplicates while preserving first-occurrence order. Only deduplicates
    bullets WITHIN Appendix A (not anywhere else).
    """
    paragraphs = list(doc.paragraphs)
    appendix_start = None
    appendix_end = None
    for i, p in enumerate(paragraphs):
        text = p.text.strip().upper()
        if text.startswith("APPENDIX A"):
            appendix_start = i
        elif appendix_start is not None and (
            text.startswith("RESPECTFULLY SUBMITTED")
            or text.startswith("/S/")
            or "DATED:" in text
            or text.startswith("APPENDIX B")
        ):
            appendix_end = i
            break
    if appendix_start is None:
        return 0
    if appendix_end is None:
        appendix_end = len(paragraphs)

    # Track seen bullets (normalized — lowercase, stripped of bullet marker)
    seen = set()
    removed = 0
    for p in paragraphs[appendix_start + 1:appendix_end]:
        text = p.text.strip()
        if not text:
            continue
        # Normalize: strip leading bullet markers and lowercase for matching
        normalized = text.lstrip("•·-* \t").strip().lower()
        if not normalized:
            continue
        # Only dedup if it looks like a bullet item (starts with bullet char)
        is_bullet = text.startswith(("•", "·", "-", "*"))
        if not is_bullet:
            continue
        if normalized in seen:
            p._element.getparent().remove(p._element)
            removed += 1
        else:
            seen.add(normalized)
    return removed


def soften_non_binding_language(doc) -> int:
    """FIX 3: Replace 'non-binding' with 'not controlling on this Court'.

    Done as a text-level rewrite (not in citation rewriter) because this
    is a stylistic/strategic phrasing fix, not a citation format fix.
    """
    replacements = [
        ("non-binding on this court under standard hierarchy principles",
         "not controlling on this Court and at most persuasive under standard hierarchy principles"),
        ("non-binding on this court",
         "not controlling on this Court"),
        ("facially non-binding",
         "facially not controlling"),
    ]
    count = 0
    for p in doc.paragraphs:
        original = p.text
        if not original:
            continue
        rewritten = original
        for old, new in replacements:
            if old.lower() in rewritten.lower():
                # Case-preserving replace (find original casing)
                idx = rewritten.lower().find(old.lower())
                while idx != -1:
                    rewritten = rewritten[:idx] + new + rewritten[idx + len(old):]
                    idx = rewritten.lower().find(old.lower(), idx + len(new))
        if rewritten != original:
            for run in p.runs:
                run.text = ""
            if p.runs:
                p.runs[0].text = rewritten
            else:
                p.add_run(rewritten)
            count += 1
    # Also process tables (scorecard exhibit may have it in cells)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    original = p.text
                    if not original:
                        continue
                    rewritten = original
                    for old, new in replacements:
                        if old.lower() in rewritten.lower():
                            idx = rewritten.lower().find(old.lower())
                            while idx != -1:
                                rewritten = rewritten[:idx] + new + rewritten[idx + len(old):]
                                idx = rewritten.lower().find(old.lower(), idx + len(new))
                    if rewritten != original:
                        for run in p.runs:
                            run.text = ""
                        if p.runs:
                            p.runs[0].text = rewritten
                        else:
                            p.add_run(rewritten)
                        count += 1
    return count


def insert_state_action_anchor(doc) -> bool:
    """FIX 4: Insert state-action anchor sentence before constitutional duties.

    Locates the line "U.S. Const. amend. XIV § 1 — Fourteenth Amendment
    Procedural Due Process" (first occurrence) and inserts an anchor
    paragraph IMMEDIATELY ABOVE it that explains the state-action linkage.

    Without this anchor, the constitutional argument can be dismissed for
    failing to identify state action (the COURT's enforcement of an
    unservced-on judgment IS the state action).
    """
    # BLOCK_6_13_POSTURE_LOCK — posture-dispatched anchor text
    try:
        from generation.posture_templates import STATE_ACTION_ANCHOR as _SA, get as _pget
        _posture_for_anchor = getattr(doc, "_beezle_posture", None) or "merits"
        anchor_text = _pget(_SA, _posture_for_anchor)
    except Exception:
        anchor_text = (
            "The deprivation arises from state action taken without "
            "procedurally compliant notice and opportunity to be heard. The "
            "constitutional duties enumerated below therefore apply."
        )
    paragraphs = list(doc.paragraphs)
    for i, p in enumerate(paragraphs):
        text = p.text.strip()
        # Trigger: first bullet line that starts with the 14A constitutional duty
        if (text.startswith("• U.S. Const. amend. XIV")
                or text.startswith("- U.S. Const. amend. XIV")
                or text.startswith("U.S. Const. amend. XIV") and "Fourteenth Amendment" in text):
            # Check if anchor already inserted (5 paragraphs above)
            for j in range(max(0, i - 5), i):
                if "state action" in paragraphs[j].text.lower():
                    return False  # already there
            # Insert anchor before this paragraph
            new_p = p.insert_paragraph_before(anchor_text)
            for run in new_p.runs:
                run.font.italic = False
                from docx.shared import Pt as _Pt
                run.font.size = _Pt(11)
            return True
    return False
# === END FINALIZE_V2_SAFE_FIXES ===


# === LIBREOFFICE_PDF_V1 ===
# LibreOffice headless conversion. Replaces docx2pdf (Word COM is broken
# on Python 3.14 + Office Click-to-Run with error 37373 "command not
# available"). LibreOffice has no COM dependency, no Word required.
import subprocess
import os

# Auto-discover LibreOffice install path
_SOFFICE_CANDIDATES = [
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\LibreOffice\program\soffice.exe"),
    # WSL reaching the Windows-installed LibreOffice (the pipeline runs in WSL; the converter is on Windows):
    "/mnt/c/Program Files/LibreOffice/program/soffice.exe",
    "/mnt/c/Program Files (x86)/LibreOffice/program/soffice.exe",
    "/usr/bin/soffice",
    "/usr/bin/libreoffice",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
]


def _win_path(p) -> str:
    """Translate a WSL /mnt/c/... path to a Windows C:\\... path (for a Windows soffice.exe invoked from WSL).
    No-op if wslpath is unavailable or the path is already Windows-style."""
    s = str(p)
    if not s.startswith("/mnt/"):
        return s
    try:
        return subprocess.run(["wslpath", "-w", s], capture_output=True, text=True,
                              timeout=10).stdout.strip() or s
    except Exception:
        return s


def _find_soffice() -> str | None:
    for cand in _SOFFICE_CANDIDATES:
        if cand and os.path.isfile(cand):
            return cand
    return None


def convert_to_pdf(docx_path: Path) -> Path | None:
    """Convert docx to PDF via LibreOffice headless.

    LibreOffice always names its output <input_basename>.pdf in the
    --outdir directory. We let it do that, then return the PDF path.
    """
    soffice = _find_soffice()
    if not soffice:
        log.error("LibreOffice (soffice.exe) not found. Install via "
                  "'winget install -e --id TheDocumentFoundation.LibreOffice'")
        return None

    out_dir = docx_path.parent
    pdf_path = docx_path.with_suffix(".pdf")

    try:
        # LibreOffice headless mode. --convert-to pdf converts in place.
        # Timeout 60s per file (LibreOffice is faster than Word; 30s usually
        # enough but some complex docs with many tables can take longer).
        # A Windows soffice.exe invoked from WSL needs Windows-style path arguments (it can't read /mnt/c/...).
        _win = soffice.lower().endswith(".exe")
        _outarg = _win_path(out_dir) if _win else str(out_dir)
        _docarg = _win_path(docx_path) if _win else str(docx_path)
        # ISOLATED user profile (F6 2026-10-21): with the default profile locked by any other soffice, LibreOffice
        # exits 0 and writes nothing ("ran but PDF missing").
        _prof = "file:///C:/Users/Public/lo_profile_conv" if _win else "file:///tmp/lo_profile_conv"
        result = subprocess.run(
            [
                soffice,
                f"-env:UserInstallation={_prof}",
                "--headless", "--norestore",
                "--convert-to", "pdf",
                "--outdir", _outarg,
                _docarg,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            log.error(f"LibreOffice failed for {docx_path.name}: "
                      f"rc={result.returncode}, stderr={result.stderr.strip()[:200]}")
            return None
        if pdf_path.exists():
            log.info(f"PDF: {pdf_path.name} ({pdf_path.stat().st_size:,} bytes)")
            return pdf_path
        else:
            log.error(f"LibreOffice ran but PDF missing: {pdf_path}")
            return None
    except subprocess.TimeoutExpired:
        log.error(f"LibreOffice timeout for {docx_path.name} (>120s)")
        return None
    except Exception as e:
        log.error(f"LibreOffice exception for {docx_path.name}: {e}")
        return None
# === END LIBREOFFICE_PDF_V1 ===


# ═════════════════════════════════════════════════════════════════════════
# AUTOMATION 11: Format compliance check
# ═════════════════════════════════════════════════════════════════════════

def check_format_compliance(doc: Document) -> list[str]:
    """Check margins, font, page count. Returns warnings."""
    warnings = []
    # Margins
    for section in doc.sections:
        try:
            top = section.top_margin.inches if section.top_margin else None
            left = section.left_margin.inches if section.left_margin else None
            if top and abs(top - 1.0) > 0.05:
                warnings.append(f"top margin {top}\" (expected 1.0\")")
            if left and abs(left - 1.25) > 0.05 and abs(left - 1.0) > 0.05:
                warnings.append(f"left margin {left}\" (expected 1.0-1.25\")")
        except Exception:
            pass
    # Page count estimate (paragraphs / 30 ≈ pages for body text)
    para_count = len([p for p in doc.paragraphs if p.text.strip()])
    est_pages = max(1, para_count // 30)
    if est_pages > 25:
        warnings.append(f"estimated {est_pages} pages (court limit may be 25)")
    return warnings


# ═════════════════════════════════════════════════════════════════════════
# --AUDIT: Hermes lint pass (no rewrite, only flags)
# ═════════════════════════════════════════════════════════════════════════

HERMES_AUDIT_PROMPT = """\
TASK: Review the following legal brief for readability and clarity.

RULES:
- DO NOT rewrite or paraphrase.
- DO NOT change legal conclusions.
- DO NOT soften mandatory language (e.g., MUST, SHALL, NO DISCRETION).
- DO NOT suggest replacing ALL CAPS with mixed case if used for void/constitutional emphasis.
- ONLY return a bullet list of issues with paragraph references.

CHECK FOR:
1. Incomplete or truncated sentences
2. Broken IF/AND/THEN logic chains
3. Sections that are hard to follow
4. Excessive ALL CAPS that may reduce credibility (note ONLY non-mandatory uses)
5. Missing transitions between sections

OUTPUT FORMAT:
- One bullet per issue, with paragraph number
- Example: "Paragraph 47: IF/AND/THEN chain ends mid-sentence"
- If no issues found, output: "No readability issues detected."
"""


def run_hermes_audit(brief_path: Path, out_path: Path) -> bool:
    """Send brief content to Hermes for lint pass. Save report to out_path."""
    try:
        import requests
    except ImportError:
        log.warning("requests not installed; skipping audit")
        return False

    try:
        doc = Document(str(brief_path))
        text = "\n".join(
            f"[{i}] {p.text.strip()}"
            for i, p in enumerate(doc.paragraphs) if p.text.strip()
        )
    except Exception as e:
        log.error(f"could not read brief for audit: {e}")
        return False

    payload = {
        "system": HERMES_AUDIT_PROMPT,
        "user":   f"BRIEF CONTENT:\n\n{text}",
        "max_tokens":  1500,
        "temperature": 0.0,
    }

    try:
        resp = requests.post(HERMES_URL, json=payload, timeout=120)
        resp.raise_for_status()
        report = resp.json().get("response", "[no response]")
    except Exception as e:
        log.warning(f"Hermes audit unavailable: {e}")
        report = (f"# Audit Skipped\n\nHermes endpoint unreachable: {e}\n"
                  f"Run audit manually by submitting brief text to Hermes "
                  f"with the following system prompt:\n\n{HERMES_AUDIT_PROMPT}")

    out_path.write_text(report, encoding="utf-8")
    log.info(f"audit report: {out_path}")
    return True


# ═════════════════════════════════════════════════════════════════════════
# MAIN PIPELINE
# ═════════════════════════════════════════════════════════════════════════

def finalize(canonical_id: str, brief_override: Path = None,
             do_pdf: bool = True, do_audit: bool = False) -> Path:
    """Main pipeline. Returns the output bundle directory."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    print("=" * 76)
    print(f"FINALIZE BRIEF — {canonical_id}")
    print("=" * 76)

    registry_entry = _load_registry_entry(canonical_id)
    # Tolerant lookup — registry may use different field naming conventions
    _caption = (registry_entry.get('case_caption')
                or registry_entry.get('case_name')
                or registry_entry.get('caption')
                or registry_entry.get('display_name')
                or '<no caption in registry>')
    _court = (registry_entry.get('court')
              or registry_entry.get('court_id')
              or registry_entry.get('court_name')
              or registry_entry.get('jurisdiction')
              or '<no court in registry>')
    log.info(f"case: {_caption}")
    log.info(f"court: {_court}")

    brief_path = brief_override or _find_latest_brief(canonical_id)
    log.info(f"source brief: {brief_path.name}")
    timestamp = _extract_timestamp(brief_path)

    # Build output bundle dir
    bundle_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    bundle_dir = OUTPUT_BASE / f"{_safe_id(canonical_id)}_{bundle_ts}"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    log.info(f"output bundle: {bundle_dir}")

    # ─── Process brief ──
    out_brief = bundle_dir / "reply_brief.docx"
    shutil.copy2(brief_path, out_brief)
    doc = Document(str(out_brief))

    # AUTOMATION 1
    n = strip_verification_checklist(doc)
    log.info(f"[1/11] verification checklist: stripped {n} paragraphs")

    # AUTOMATION 2
    added = ensure_section_iic_heading(doc)
    log.info(f"[2/11] II.C heading: {'inserted' if added else 'already present'}")

    # AUTOMATION 3
    updated = update_dated_line(doc)
    log.info(f"[3/11] Dated: line: {'updated' if updated else 'NOT FOUND'}")

    # AUTOMATION 4
    cap_warnings = verify_caption(doc, registry_entry)
    if cap_warnings:
        for w in cap_warnings:
            log.warning(f"[4/11] caption: {w}")
    else:
        log.info("[4/11] caption verified")

    # === EARLY_SAVE_V1: persist progress in case later automation fails ===
    doc.save(str(out_brief))
    log.info(f"early save: brief persisted after automation 4")
    doc = Document(str(out_brief))  # reload

    # AUTOMATION 5
    n_rewrites = convert_citation_formats(doc)
    log.info(f"[5/11] citation formats: rewrote {n_rewrites} paragraphs")

    # AUTOMATION 6
    n_canon = insert_constitutional_canonicals(doc)
    log.info(f"[6/11] constitutional canonicals: inserted {n_canon}")

    # AUTOMATION 7
    n_reorder = reorder_claims(doc, registry_entry)
    log.info(f"[7/11] claim ordering: {n_reorder} reordered (logging only)")

    # === FINALIZE_V2_SAFE_FIXES: 4 court-presentation fixes ===
    n_meta = strip_engine_metadata(doc)
    log.info(f"[v2.1] engine metadata stripped: {n_meta} lines")

    n_dup = dedup_appendix_a(doc)
    log.info(f"[v2.2] Appendix A deduplicated: {n_dup} duplicates removed")

    n_soft = soften_non_binding_language(doc)
    log.info(f"[v2.3] non-binding language softened: {n_soft} paragraphs")

    # BLOCK_6_13_1_POSTURE_WIRING — derive posture from registry directly (no 'case' var)
    try:
        import json as _bz_json
        from pipeline.posture_engine import derive_posture as _bz_dp
        _bz_reg_path = _BEEZLE_ROOT / "cases_index" / "case_registry.json"
        _bz_reg = _bz_json.loads(_bz_reg_path.read_text(encoding="utf-8"))
        _bz_entry = _bz_reg.get(canonical_id, {}) if isinstance(_bz_reg, dict) else {}
        _bz_facts = _bz_entry.get("engine_facts", []) or []
        _bz_role  = _bz_entry.get("role", "plaintiff") or "plaintiff"
        doc._beezle_posture = _bz_dp(_bz_entry, _bz_role, _bz_facts)
        log.info(f"[v2.4-posture] posture: {doc._beezle_posture}")
        # Also push to violations_matrix module for any matrix re-rendering
        try:
            from generation import violations_matrix as _bz_vm
            _bz_vm.set_posture(doc._beezle_posture)
        except Exception:
            pass
    except Exception as _pe:
        doc._beezle_posture = "merits"
        log.warning(f"[v2.4-posture] posture derivation failed: {_pe}")
    anchored = insert_state_action_anchor(doc)
    log.info(f"[v2.4] state-action anchor: {'inserted' if anchored else 'not applicable'}")
    # === END FINALIZE_V2_SAFE_FIXES ===

    doc.save(str(out_brief))
    log.info(f"finalized brief saved: {out_brief.name}")

    # ─── Companions: scorecard + matrix ──
    out_files = [out_brief]
    sc_src = _find_companion(canonical_id, "scorecard_exhibit", timestamp)
    if sc_src:
        out_sc = bundle_dir / "scorecard_exhibit.docx"
        shutil.copy2(sc_src, out_sc)
        # Apply citation format conversion to scorecard too
        sc_doc = Document(str(out_sc))
        convert_citation_formats(sc_doc)
        soften_non_binding_language(sc_doc)
        sc_doc.save(str(out_sc))
        out_files.append(out_sc)
        log.info(f"[8a/11] scorecard exhibit copied")

    vm_src = _find_companion(canonical_id, "violations_matrix", timestamp)
    if vm_src:
        out_vm = bundle_dir / "violations_matrix.docx"
        shutil.copy2(vm_src, out_vm)
        vm_doc = Document(str(out_vm))
        convert_citation_formats(vm_doc)
        soften_non_binding_language(vm_doc)
        vm_doc.save(str(out_vm))
        out_files.append(out_vm)
        log.info(f"[8b/11] violations matrix copied")

    # ─── Certificate of Service ──
    today = datetime.now().strftime("%B %d, %Y")
    cos = build_certificate_of_service(canonical_id, registry_entry, today)
    out_cos = bundle_dir / "certificate_of_service.docx"
    cos.save(str(out_cos))
    out_files.append(out_cos)
    log.info(f"[9/11] certificate of service generated")

    # ─── Audit (optional, before PDF) ──
    if do_audit:
        report_path = bundle_dir / "readability_report.txt"
        run_hermes_audit(out_brief, report_path)
        log.info("[--audit] readability report saved")

    # ─── PDF conversion ──
    pdf_files = []
    if do_pdf:
        for f in out_files:
            pdf = convert_to_pdf(f)
            if pdf:
                pdf_files.append(pdf)
        log.info(f"[10/11] PDF conversion: {len(pdf_files)}/{len(out_files)} succeeded")
    else:
        log.info("[10/11] PDF conversion: skipped (--no-pdf)")

    # ─── Format compliance ──
    fmt_warnings = check_format_compliance(Document(str(out_brief)))
    if fmt_warnings:
        for w in fmt_warnings:
            log.warning(f"[11/11] format: {w}")
    else:
        log.info("[11/11] format compliance: clean")

    # ─── Manifest ──
    manifest_path = bundle_dir / "READY_TO_FILE.txt"
    lines = []
    lines.append(f"BEEZLE FILED-READY BUNDLE")
    lines.append(f"Generated: {datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"Case: {canonical_id}")
    _m_caption = (registry_entry.get('case_caption')
                  or registry_entry.get('case_name')
                  or registry_entry.get('caption')
                  or registry_entry.get('display_name') or '')
    _m_court = (registry_entry.get('court')
                or registry_entry.get('court_id')
                or registry_entry.get('court_name')
                or registry_entry.get('jurisdiction') or '')
    lines.append(f"Caption: {_m_caption}")
    lines.append(f"Court: {_m_court}")
    lines.append(f"Source brief: {brief_path.name}")
    lines.append("")
    lines.append("FILES IN BUNDLE:")
    for f in sorted(bundle_dir.iterdir()):
        size = f.stat().st_size
        lines.append(f"  {f.name}  ({size:,} bytes)")
    lines.append("")
    lines.append("MANUAL STEPS REMAINING:")
    lines.append("  1. Verify counsel_of_record placeholders replaced in COS")
    lines.append("  2. Verify all citations against CourtListener / Lexis / Westlaw")
    lines.append("  3. Print all PDFs")
    lines.append("  4. Mail to counsel of record per COS")
    lines.append("  5. File brief + exhibits at court (or e-file if available)")
    if cap_warnings or fmt_warnings:
        lines.append("")
        lines.append("WARNINGS:")
        for w in cap_warnings + fmt_warnings:
            lines.append(f"  - {w}")
    manifest_path.write_text("\n".join(lines), encoding="utf-8")

    print()
    print("=" * 76)
    print(f"BUNDLE READY: {bundle_dir}")
    print("=" * 76)
    return bundle_dir


def main():
    p = argparse.ArgumentParser()
    p.add_argument("canonical_id", help="e.g., phila_ccp:260100000")
    p.add_argument("--brief", default=None,
                   help="path to specific brief docx (else uses latest)")
    p.add_argument("--no-pdf", action="store_true",
                   help="skip PDF conversion (for fast iteration)")
    p.add_argument("--audit", action="store_true",
                   help="run Hermes readability lint pass")
    args = p.parse_args()

    brief_override = Path(args.brief) if args.brief else None
    finalize(args.canonical_id, brief_override,
             do_pdf=not args.no_pdf, do_audit=args.audit)


if __name__ == "__main__":
    main()
