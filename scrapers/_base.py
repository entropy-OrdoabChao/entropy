"""
PHASE_2A_PRIMARY_SOURCE_SCRAPERS
Shared base for all scrapers: save-to-obsidian, dedup, YAML frontmatter, HTML-to-text.

Output directory: <home>\obsidian_sync\primary_sources\<jurisdiction>\<source_id>.md
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import hashlib
import re
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional

from core.logger import get_logger

log = get_logger("scraper_base")

PRIMARY_SOURCES_DIR = (_eroot() / "obsidian_sync/primary_sources")


class _HTMLTextExtractor(HTMLParser):
    """Extract text from HTML, skipping scripts/styles/nav."""
    def __init__(self):
        super().__init__()
        self.text: list[str] = []
        self.skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "nav", "header", "footer", "noscript"):
            self.skip = True

    def handle_endtag(self, tag):
        if tag in ("script", "style", "nav", "header", "footer", "noscript"):
            self.skip = False
        if tag in ("p", "div", "br", "h1", "h2", "h3", "h4", "li", "tr", "dt", "dd", "td"):
            self.text.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.text.append(data)


def html_to_text(html: str) -> str:
    """Clean HTML to plain text."""
    parser = _HTMLTextExtractor()
    parser.feed(html)
    text = "".join(parser.text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def xml_to_text(xml_content: str) -> str:
    """Simple XML tag-strip to text."""
    text = re.sub(r"<[^>]+>", " ", xml_content)
    text = re.sub(r"\s+", " ", text)
    for ent, repl in [("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"')]:
        text = text.replace(ent, repl)
    return text.strip()


def safe_id(raw: str) -> str:
    """Sanitize a string for use as filename."""
    s = re.sub(r"[^\w\-.]", "_", raw)
    return re.sub(r"_+", "_", s).strip("_")[:120]


def text_sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def save_primary_source(
    source_id: str,
    jurisdiction: str,
    source_type: str,
    title: str,
    text: str,
    url: str = "",
    section: str = "",
    extra_frontmatter: Optional[dict] = None,
) -> dict:
    """Save fetched source as YAML-frontmatter markdown to obsidian_sync/primary_sources/.

    Returns {source_id, path, text_length, jurisdiction, type, sha1, dedup_skipped}.
    Never overwrites if SHA-1 matches existing file.
    """
    jur_dir = PRIMARY_SOURCES_DIR / safe_id(jurisdiction)
    jur_dir.mkdir(parents=True, exist_ok=True)

    fname = safe_id(source_id) + ".md"
    target = jur_dir / fname

    sha = text_sha1(text)

    # Dedup: if file exists and SHA matches, skip
    if target.exists():
        existing = target.read_text(encoding="utf-8")
        if sha in existing:
            log.info(f"dedup skip: {fname} (SHA match)")
            return {
                "source_id": source_id,
                "path": str(target),
                "title": title,
                "url": url,
                "text_length": len(text),
                "jurisdiction": jurisdiction,
                "type": source_type,
                "sha1": sha,
                "dedup_skipped": True,
            }

    # Build YAML frontmatter
    fm = {
        "source_id": source_id,
        "title": title,
        "jurisdiction": jurisdiction,
        "type": source_type,
        "section": section,
        "url": url,
        "sha1": sha,
        "fetched": datetime.utcnow().isoformat() + "Z",
        "text_length": len(text),
    }
    if extra_frontmatter:
        fm.update(extra_frontmatter)

    lines = ["---"]
    for k, v in fm.items():
        lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {title}")
    lines.append("")
    lines.append(text)

    target.write_text("\n".join(lines), encoding="utf-8")
    log.info(f"saved: {target} ({len(text):,} chars)")

    return {
        "source_id": source_id,
        "path": str(target),
        "title": title,
        "url": url,
        "text_length": len(text),
        "jurisdiction": jurisdiction,
        "type": source_type,
        "sha1": sha,
        "dedup_skipped": False,
    }


# ─── Direct DB Ingest ─────────────────────────────────────────────

def ingest_to_db(
    text: str,
    source_id: str,
    source_type: str = "statute",
    jurisdiction: str = "federal",
    title: str = "",
    section: str = "",
    extra_metadata: Optional[dict] = None,
    chunk_words: int = 800,
    overlap: int = 150,
    max_chunks: int = 50000,
) -> dict:
    """Chunk text and push directly to ChromaDB.

    Uses the same model/collection as bulk_ingest_to_chroma.py.
    Call this after save_primary_source() or instead of it.

    Returns: {chunks_indexed, source_id, source_type}
    """
    try:
        import hashlib as _hl
        import chromadb
        from sentence_transformers import SentenceTransformer
    except ImportError as e:
        log.info("ingest_to_db: missing dependency: " + str(e))
        return {"error": str(e), "chunks_indexed": 0}

    # Clean text
    clean = text.replace("\r\n", "\n").replace("\r", "\n")
    clean = re.sub(r"\n{3,}", "\n\n", clean)
    clean = clean.replace("\x0c", "\n").replace("\x00", "")

    # Chunk
    words = clean.split()
    chunks = []
    i = 0
    idx = 0
    while i < len(words) and idx < max_chunks:
        ct = " ".join(words[i:i + chunk_words])
        if len(ct.strip()) >= 100:
            chunk_id = _hl.md5((source_id + "_chunk_" + str(idx)).encode()).hexdigest()
            meta = {
                "source": source_id,
                "source_type": source_type,
                "jurisdiction": jurisdiction,
                "chunk_index": idx,
                "chunk_type": "text",
                "section": section,
                "citations": "",
            }
            if extra_metadata:
                meta.update(extra_metadata)
            chunks.append({"id": chunk_id, "text": ct, "meta": meta})
            idx += 1
        i += chunk_words - overlap

    if not chunks:
        return {"chunks_indexed": 0, "source_id": source_id}

    # Connect and ingest
    try:
        client = chromadb.HttpClient(host="127.0.0.1", port=8100)
        col = client.get_collection("legal_docs")
    except Exception as e:
        log.info("ingest_to_db: ChromaDB connection failed: " + str(e))
        return {"error": str(e), "chunks_indexed": 0}

    try:
        import torch
        device = "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        device = "cpu"

    model = SentenceTransformer("BAAI/bge-base-en-v1.5", device=device)

    inserted = 0
    batch_size = 500
    for bi in range(0, len(chunks), batch_size):
        batch = chunks[bi:bi + batch_size]
        try:
            texts = [c["text"] for c in batch]
            ids = [c["id"] for c in batch]
            metas = [c["meta"] for c in batch]
            vecs = model.encode(texts, batch_size=256, normalize_embeddings=True,
                                show_progress_bar=False, device=device).tolist()
            col.upsert(ids=ids, documents=texts, embeddings=vecs, metadatas=metas)
            inserted += len(batch)
        except Exception as e:
            log.info("ingest_to_db batch error: " + str(e))

    log.info("ingest_to_db: " + source_id + " -> " + str(inserted) + " chunks indexed")
    return {"chunks_indexed": inserted, "source_id": source_id, "source_type": source_type}

