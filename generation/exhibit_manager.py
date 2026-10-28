"""
BLOCK_9A_HERMES_SKILLS_PORT_1_OF_2
Exhibit manager. Ported from hermes exhibit-manager skill.

Reuses Beezle's existing pipeline.doc_reader.extract_text() — no new OCR dependency.
- Catalog uploaded evidence (PDF/DOCX/TXT/MD/images by metadata only)
- Auto-label A, B, C, ...
- Generate exhibit index (markdown)
- Insert inline references in drafts (e.g. "(Ex. A at 3)")
- Build appendix manifest with continuous page numbering

Storage:
  - Per-case exhibit registry: <home>\cases\<case_id>\exhibits.json
  - Output index/appendix written to output/filed_ready/<case>/
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import json
import hashlib
from pathlib import Path
from datetime import datetime
from typing import Optional
from dataclasses import dataclass, field, asdict

from core.logger import get_logger
from pipeline.doc_reader import extract_text  # Beezle's existing pdfplumber-based extractor

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

log = get_logger("exhibit_manager")

EXHIBIT_LABELS = [chr(c) for c in range(ord("A"), ord("Z") + 1)]


@dataclass
class Exhibit:
    label: str                  # "A", "B", ...
    title: str
    source_path: str
    sha1: str
    file_type: str              # "pdf" | "docx" | "txt" | "image" | "other"
    page_count: int = 0
    text_excerpt: str = ""      # first 500 chars for searchability
    description: str = ""
    added_at: str = ""
    page_start: int = 0         # continuous numbering (set by build_appendix)
    page_end: int = 0


def _registry_path(case_id: str) -> Path:
    safe = case_id.replace(":", "_").replace("/", "_")
    p = Path(str(_BEEZLE_ROOT / "cases" / f"{safe}" / "exhibits.json"))
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_registry(case_id: str) -> list[dict]:
    rp = _registry_path(case_id)
    if not rp.exists():
        return []
    try:
        return json.loads(rp.read_text(encoding="utf-8"))
    except Exception as e:
        log.error(f"registry corrupt for {case_id}: {e}")
        return []


def _save_registry(case_id: str, exhibits: list[dict]) -> None:
    _registry_path(case_id).write_text(json.dumps(exhibits, indent=2, default=str), encoding="utf-8")


def _file_sha1(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _detect_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".pdf": return "pdf"
    if ext in (".docx", ".doc"): return "docx"
    if ext in (".txt", ".md"): return "txt"
    if ext in (".png", ".jpg", ".jpeg", ".gif", ".tif", ".tiff", ".bmp"): return "image"
    return "other"


def _next_label(existing: list[dict]) -> str:
    used = {e["label"] for e in existing}
    for L in EXHIBIT_LABELS:
        if L not in used:
            return L
    # fall through past Z: AA, AB, ...
    n = len(existing)
    return EXHIBIT_LABELS[n // 26 - 1] + EXHIBIT_LABELS[n % 26]


def add_exhibit(
    case_id: str,
    source_path: str | Path,
    title: Optional[str] = None,
    description: str = "",
) -> dict:
    """Catalog a single exhibit. Returns the Exhibit as dict."""
    sp = Path(source_path)
    if not sp.exists():
        raise FileNotFoundError(f"exhibit source not found: {sp}")
    registry = _load_registry(case_id)

    sha = _file_sha1(sp)
    if any(e["sha1"] == sha for e in registry):
        log.info(f"exhibit dedup skip: {sp.name} (sha1 match)")
        return next(e for e in registry if e["sha1"] == sha)

    ftype = _detect_type(sp)
    excerpt = ""
    page_count = 0
    if ftype in ("pdf", "docx", "txt"):
        try:
            text = extract_text(str(sp)) or ""
            excerpt = text[:500].strip()
            # rough page count for PDF
            if ftype == "pdf":
                try:
                    import pdfplumber
                    with pdfplumber.open(str(sp)) as pdf:
                        page_count = len(pdf.pages)
                except Exception:
                    page_count = 0
        except Exception as e:
            log.warning(f"extract failed for {sp.name}: {e}")

    ex = Exhibit(
        label=_next_label(registry),
        title=title or sp.stem,
        source_path=str(sp.resolve()),
        sha1=sha,
        file_type=ftype,
        page_count=page_count,
        text_excerpt=excerpt,
        description=description,
        added_at=datetime.utcnow().isoformat() + "Z",
    )
    registry.append(asdict(ex))
    _save_registry(case_id, registry)
    log.info(f"added Ex. {ex.label}: {ex.title} ({ftype}, {page_count}p)")
    return asdict(ex)


def list_exhibits(case_id: str) -> list[dict]:
    return _load_registry(case_id)


def build_index_markdown(case_id: str) -> str:
    """Generate a markdown exhibit index suitable for prepending to a brief."""
    exhibits = _load_registry(case_id)
    if not exhibits:
        return "# Exhibit Index\n\n*(no exhibits filed)*\n"
    lines = ["# Exhibit Index", "", f"*Case: {case_id}*", "",
             "| Ex. | Title | Type | Pages | Description |",
             "|-----|-------|------|-------|-------------|"]
    for e in exhibits:
        desc = (e.get("description") or e.get("text_excerpt", "")[:80]).replace("\n", " ").replace("|", "/")
        lines.append(f"| {e['label']} | {e['title']} | {e['file_type']} | {e.get('page_count',0)} | {desc} |")
    return "\n".join(lines) + "\n"


def build_appendix_manifest(case_id: str, brief_page_count: int = 0) -> dict:
    """
    Assign continuous page numbers across all exhibits.
    brief_page_count: number of pages in the main brief (exhibits start after).
    Returns: {total_pages, manifest:[{label,title,page_start,page_end,source_path}]}
    """
    exhibits = _load_registry(case_id)
    cursor = brief_page_count + 1 if brief_page_count > 0 else 1
    manifest = []
    for e in exhibits:
        pc = e.get("page_count") or 1
        e["page_start"] = cursor
        e["page_end"] = cursor + pc - 1
        manifest.append({
            "label": e["label"],
            "title": e["title"],
            "page_start": e["page_start"],
            "page_end": e["page_end"],
            "source_path": e["source_path"],
        })
        cursor += pc
    _save_registry(case_id, exhibits)
    return {
        "case_id": case_id,
        "brief_pages": brief_page_count,
        "total_pages": cursor - 1,
        "manifest": manifest,
    }


def insert_inline_refs(text: str, case_id: str) -> str:
    """Replace [[Ex.A]] / [[Ex.B at 3]] markers with formal "(Ex. A)" / "(Ex. A at 3)" form."""
    import re
    exhibits = {e["label"]: e for e in _load_registry(case_id)}
    def _sub(m: "re.Match[str]") -> str:
        label = m.group(1).upper()
        page = m.group(2)
        if label not in exhibits:
            return m.group(0)  # leave unknown markers untouched
        return f"(Ex. {label} at {page})" if page else f"(Ex. {label})"
    return re.sub(r"\[\[Ex\.\s*([A-Z]+)(?:\s+at\s+(\d+))?\]\]", _sub, text)