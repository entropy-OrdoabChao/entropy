# -*- coding: utf-8 -*-
"""
ocr_engine.py
=============
OCR fallback for scanned PDFs. Respects config.yaml ocr settings:
  ocr:
    dpi: 300
    pdfplumber_first: true
    fallback_tesseract: true
    lang: "eng"

Public API:
    extract_text_smart(path, min_text_threshold=50) -> str
    ocr_pdf(path, dpi=300, lang="eng") -> str
    ocr_image(path, lang="eng") -> str
    is_ocr_available() -> bool

Dependencies (one-time install):
    pip install pytesseract pdf2image pillow --break-system-packages
    Tesseract binary: https://github.com/UB-Mannheim/tesseract/wiki
    Poppler (for pdf2image): https://github.com/oschwartz10612/poppler-windows
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

import logging
import os
import sys
from pathlib import Path
from typing import Optional

log = logging.getLogger("ocr_engine")

# ---------------------------------------------------------------------------
# Load OCR config from config.yaml
# ---------------------------------------------------------------------------
_CFG = {"dpi": 300, "pdfplumber_first": True, "fallback_tesseract": True, "lang": "eng"}
try:
    import yaml
    _yaml_path = _BEEZLE_ROOT / "config.yaml"
    if _yaml_path.exists():
        _raw = yaml.safe_load(_yaml_path.read_text(encoding="utf-8")) or {}
        if isinstance(_raw.get("ocr"), dict):
            _CFG.update({k: v for k, v in _raw["ocr"].items() if v is not None})
except Exception as e:
    log.debug("could not load ocr config from yaml: %s", e)


# ---------------------------------------------------------------------------
# Dependency detection — graceful degradation
# ---------------------------------------------------------------------------
_TESSERACT_OK = None
_PDF2IMAGE_OK = None
_PDFPLUMBER_OK = None
_DOCX_OK = None


def _check_tesseract() -> bool:
    global _TESSERACT_OK
    if _TESSERACT_OK is not None:
        return _TESSERACT_OK
    try:
        import pytesseract  # noqa: F401
        # Also check the binary
        try:
            import pytesseract as _pt
            _pt.get_tesseract_version()
            _TESSERACT_OK = True
        except Exception as e:
            log.warning("pytesseract installed but tesseract binary not found: %s", e)
            log.warning("Install: https://github.com/UB-Mannheim/tesseract/wiki")
            _TESSERACT_OK = False
    except ImportError:
        _TESSERACT_OK = False
    return _TESSERACT_OK


def _check_pdf2image() -> bool:
    global _PDF2IMAGE_OK
    if _PDF2IMAGE_OK is not None:
        return _PDF2IMAGE_OK
    try:
        import pdf2image  # noqa: F401
        _PDF2IMAGE_OK = True
    except ImportError:
        _PDF2IMAGE_OK = False
    return _PDF2IMAGE_OK


def _check_pdfplumber() -> bool:
    global _PDFPLUMBER_OK
    if _PDFPLUMBER_OK is not None:
        return _PDFPLUMBER_OK
    try:
        import pdfplumber  # noqa: F401
        _PDFPLUMBER_OK = True
    except ImportError:
        _PDFPLUMBER_OK = False
    return _PDFPLUMBER_OK


def _check_docx() -> bool:
    global _DOCX_OK
    if _DOCX_OK is not None:
        return _DOCX_OK
    try:
        import docx  # noqa: F401
        _DOCX_OK = True
    except ImportError:
        _DOCX_OK = False
    return _DOCX_OK


def is_ocr_available() -> bool:
    return _check_tesseract() and _check_pdf2image()


# ---------------------------------------------------------------------------
# Core extractors
# ---------------------------------------------------------------------------
def _extract_pdfplumber(path: Path) -> str:
    if not _check_pdfplumber():
        return ""
    import pdfplumber
    try:
        parts = []
        with pdfplumber.open(str(path)) as pdf:
            for i, page in enumerate(pdf.pages):
                try:
                    t = page.extract_text() or ""
                    if t.strip():
                        parts.append(t)
                except Exception as e:
                    log.debug("pdfplumber page %d in %s: %s", i, path.name, e)
        return "\n".join(parts)
    except Exception as e:
        log.debug("pdfplumber failed on %s: %s", path.name, e)
        return ""


def ocr_pdf(path: Path, dpi: Optional[int] = None, lang: Optional[str] = None) -> str:
    """OCR a full PDF by rasterizing each page via pdf2image, then tesseract."""
    if not _check_tesseract() or not _check_pdf2image():
        log.warning("OCR unavailable for %s (missing tesseract or pdf2image)", path.name)
        return ""

    import pytesseract
    from pdf2image import convert_from_path

    dpi = dpi or int(_CFG.get("dpi", 300))
    lang = lang or str(_CFG.get("lang", "eng"))

    try:
        pages = convert_from_path(str(path), dpi=dpi)
    except Exception as e:
        log.error("pdf2image failed on %s: %s (need poppler?)", path.name, e)
        return ""

    parts = []
    for i, page_img in enumerate(pages):
        try:
            t = pytesseract.image_to_string(page_img, lang=lang)
            parts.append(t)
        except Exception as e:
            log.debug("tesseract page %d on %s: %s", i, path.name, e)
    return "\n".join(parts)


def ocr_image(path: Path, lang: Optional[str] = None) -> str:
    if not _check_tesseract():
        return ""
    import pytesseract
    from PIL import Image
    lang = lang or str(_CFG.get("lang", "eng"))
    try:
        return pytesseract.image_to_string(Image.open(str(path)), lang=lang)
    except Exception as e:
        log.debug("ocr_image failed on %s: %s", path.name, e)
        return ""


def _extract_docx(path: Path) -> str:
    if not _check_docx():
        return ""
    import docx
    try:
        doc = docx.Document(str(path))
        return "\n".join(p.text for p in doc.paragraphs if p.text)
    except Exception as e:
        log.debug("docx failed on %s: %s", path.name, e)
        return ""


def _extract_text_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        log.debug("text read failed on %s: %s", path.name, e)
        return ""


def _extract_html(path: Path) -> str:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(raw, "html.parser").get_text("\n")
        except ImportError:
            import re as _re
            return _re.sub(r"<[^>]+>", " ", raw)
    except Exception as e:
        log.debug("html failed on %s: %s", path.name, e)
        return ""


def _extract_eml(path: Path) -> str:
    import email
    import email.policy
    try:
        raw = path.read_bytes()
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        headers = []
        for h in ("From", "To", "Subject", "Date"):
            v = msg.get(h)
            if v:
                headers.append(f"{h}: {v}")
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                if ctype == "text/plain":
                    body = part.get_content()
                    break
                elif ctype == "text/html" and not body:
                    body = part.get_content()
        else:
            body = msg.get_content()
        return "\n".join(headers) + "\n\n" + (body or "")
    except Exception as e:
        log.debug("eml failed on %s: %s", path.name, e)
        return ""


# ---------------------------------------------------------------------------
# Smart extractor — pdfplumber first, OCR fallback if low text yield
# ---------------------------------------------------------------------------
def extract_text_smart(
    path,
    min_text_threshold: int = 50,
    force_ocr: bool = False,
    ocr_pdfs: bool = True,
    ocr_images: bool = True,
) -> str:
    """
    Extract text from any supported file. Returns empty string on failure.

    For PDFs: tries pdfplumber first. If total text < min_text_threshold,
    falls back to tesseract OCR (full-page rasterize).
    For images: OCRs directly.
    For docx/txt/html/eml: standard parsers.
    """
    p = Path(path)
    if not p.exists():
        return ""

    ext = p.suffix.lower()

    if ext == ".pdf":
        text = ""
        if not force_ocr and _CFG.get("pdfplumber_first", True):
            text = _extract_pdfplumber(p)
        need_ocr = force_ocr or (len(text.strip()) < min_text_threshold)
        if need_ocr and ocr_pdfs and _CFG.get("fallback_tesseract", True):
            log.info("OCR fallback triggered for %s (pdfplumber yielded %d chars)",
                     p.name, len(text.strip()))
            ocr_text = ocr_pdf(p)
            if len(ocr_text.strip()) > len(text.strip()):
                return ocr_text
        return text

    if ext in (".docx", ".doc"):
        return _extract_docx(p)

    if ext in (".txt", ".md", ".text", ".log"):
        return _extract_text_file(p)

    if ext in (".html", ".htm"):
        return _extract_html(p)

    if ext in (".eml", ".msg"):
        return _extract_eml(p)

    if ext in (".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp"):
        if ocr_images:
            return ocr_image(p)
        return ""

    # Unknown extension — try as text
    return _extract_text_file(p)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print(f"OCR config:            {_CFG}")
    print(f"pytesseract installed: {_check_tesseract()}")
    print(f"pdf2image installed:   {_check_pdf2image()}")
    print(f"pdfplumber installed:  {_check_pdfplumber()}")
    print(f"python-docx installed: {_check_docx()}")
    print(f"OCR end-to-end ready:  {is_ocr_available()}")
    if len(sys.argv) > 1:
        test = Path(sys.argv[1])
        if test.exists():
            txt = extract_text_smart(test)
            print(f"\nExtracted {len(txt)} chars from {test.name}:")
            print(txt[:500])
