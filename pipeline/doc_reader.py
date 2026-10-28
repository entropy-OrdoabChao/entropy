"""
pipeline/doc_reader.py — Extract text from uploaded documents.

Supports: PDF (via pdfplumber), DOCX (via python-docx), TXT, MD
Falls back gracefully when optional deps are missing.
"""

import os
from pathlib import Path
from typing import Optional

from core.logger import get_logger

log = get_logger("DOC_READER")



def _to_wsl_path(p):
    """Translate a stored Windows path (C:\\Users\\...) to its WSL mount
    (/mnt/c/Users/...) when running under WSL. No-op if already POSIX or the
    Windows path happens to resolve."""
    try:
        s = str(p)
    except Exception:
        return p
    import os as _os, re as _re
    if _os.path.exists(s):
        return s
    m = _re.match(r'^([A-Za-z]):[\\/](.*)$', s)
    if m:
        drive = m.group(1).lower()
        rest = m.group(2).replace('\\', '/')
        wsl = f"/mnt/{drive}/{rest}"
        return wsl
    return s


# Extraction cache (GO step 1, 2026-07-08): the same PDFs were re-parsed up to 4x per analysis run
# (81 extract calls in one reanalysis, heavy dup). Two tiers: an in-process memo keyed by (path, mtime)
# and an mtime-checked `.doctext.txt` sidecar (mirrors auto_reply's `.ocr.txt` pattern) for the
# expensive formats (pdf/docx). Plain text/md/html stay uncached (already a cheap read).
_MEMO = {}
_CACHE_EXTS = (".pdf", ".docx", ".doc")


def extract_text(file_path: str) -> str:
    file_path = _to_wsl_path(file_path)  # WSL: C:\\ -> /mnt/c/
    """Extract text from a document file.

    Args:
        file_path: Path to the document

    Returns:
        Extracted text string
    """
    path = Path(file_path)
    if not path.exists():
        log.error("extract", f"File not found: {file_path}")
        return ""

    ext = path.suffix.lower()
    size = path.stat().st_size

    mtime = path.stat().st_mtime
    memo_key = (str(path), mtime)
    if ext in _CACHE_EXTS:
        hit = _MEMO.get(memo_key)
        if hit is not None:
            return hit
        sidecar = path.with_suffix(path.suffix + ".doctext.txt")
        try:
            if sidecar.exists() and sidecar.stat().st_mtime >= mtime:
                text = sidecar.read_text(encoding="utf-8", errors="replace")
                if text.strip():
                    _MEMO[memo_key] = text
                    return text
        except Exception:
            pass

    log.info(f"Reading {path.name} ({size} bytes, {ext})")

    try:
        if ext in (".txt", ".md", ".text"):
            return _read_text(path)
        elif ext == ".pdf":
            text = _read_pdf(path)
        elif ext in (".docx", ".doc"):
            text = _read_docx(path)
        elif ext in (".html", ".htm"):
            return _read_html(path)
        else:
            log.warning("extract", f"Unsupported extension {ext}, trying as text")
            return _read_text(path)
    except Exception as e:
        log.error("extract", f"Failed to read {path.name}", error=str(e))
        return ""

    # cache successful expensive extractions (never cache error placeholders)
    if text and text.strip() and not text.startswith("["):
        _MEMO[memo_key] = text
        try:
            sidecar = path.with_suffix(path.suffix + ".doctext.txt")
            sidecar.write_text(text, encoding="utf-8")
        except Exception:
            pass  # read-only dir etc. — memo still covers this process
    return text


def _read_text(path: Path) -> str:
    """Read plain text file."""
    text = path.read_text(encoding="utf-8", errors="replace")
    log.stage("extract", f"Text file: {len(text)} chars")
    return text


def _read_pdf(path: Path) -> str:
    """Read PDF using pdfplumber (text-native) with OCR fallback."""
    try:
        import pdfplumber
    except ImportError:
        log.warning("extract", "pdfplumber not installed, install with: pip install pdfplumber")
        return f"[PDF extraction requires pdfplumber: pip install pdfplumber]\nFile: {path.name}"

    text_parts = []
    _got_text = False
    try:
        with pdfplumber.open(str(path)) as pdf:
            n_pages = len(pdf.pages)
            for i, page in enumerate(pdf.pages):
                page_text = page.extract_text() or ""
                if page_text.strip():
                    text_parts.append(f"--- Page {i+1} ---\n{page_text}")
                    _got_text = True
                else:
                    text_parts.append(f"--- Page {i+1} --- [no extractable text - may need OCR]")

        # OCR FALLBACK (2026-07-10): a scanned PDF yields no pdfplumber text. Instead of leaving a
        # placeholder (which starved the drafter), run the REAL OCR engine — the same pipeline.ocr_engine
        # that auto_reply already uses — so the reader never returns empty text. This is the straight-line
        # pair to the register+extract step wired into docket_email_fetch.
        if not _got_text:
            try:
                from pipeline.ocr_engine import extract_text_smart
                ocr_text = extract_text_smart(str(path)) or ""
                if ocr_text.strip():
                    log.stage("extract", f"PDF OCR fallback: {n_pages} pages, {len(ocr_text)} chars")
                    return ocr_text
            except Exception as _oe:
                log.warning("extract", f"OCR fallback unavailable: {_oe}")

        full_text = "\n\n".join(text_parts)
        log.stage("extract", f"PDF: {n_pages} pages, {len(full_text)} chars")
        return full_text

    except Exception as e:
        log.error("extract", f"PDF extraction failed: {path.name}", error=str(e))
        return f"[PDF extraction error: {e}]"


def _read_docx(path: Path) -> str:
    """Read DOCX using python-docx."""
    try:
        from docx import Document
    except ImportError:
        log.warning("extract", "python-docx not installed, install with: pip install python-docx")
        return f"[DOCX extraction requires python-docx: pip install python-docx]\nFile: {path.name}"

    try:
        doc = Document(str(path))
        paragraphs = []
        for para in doc.paragraphs:
            text = para.text.strip()
            if text:
                paragraphs.append(text)

        # Also extract from tables
        for table in doc.tables:
            for row in table.rows:
                row_text = " | ".join(cell.text.strip() for cell in row.cells if cell.text.strip())
                if row_text:
                    paragraphs.append(row_text)

        full_text = "\n\n".join(paragraphs)
        log.stage("extract", f"DOCX: {len(paragraphs)} paragraphs, {len(full_text)} chars")
        return full_text

    except Exception as e:
        log.error("extract", f"DOCX extraction failed: {path.name}", error=str(e))
        return f"[DOCX extraction error: {e}]"


def _read_html(path: Path) -> str:
    """Read HTML with basic tag stripping."""
    import re
    raw = path.read_text(encoding="utf-8", errors="replace")
    # Strip tags
    text = re.sub(r"<[^>]+>", " ", raw)
    text = re.sub(r"\s+", " ", text).strip()
    log.stage("extract", f"HTML: {len(text)} chars after stripping")
    return text
