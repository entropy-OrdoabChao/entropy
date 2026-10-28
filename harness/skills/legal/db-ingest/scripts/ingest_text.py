#!/usr/bin/env python3
"""
Ingest legal text into the Beezle corpus (chunk_text.db) — ChromaDB-FREE.

Writes ADDITIVELY to the host chunk_text.db that beezle-corpus serves read-only/live:
  chunk_text(chunk_id PK, text, source, jurisdiction, citations)
  chunk_text_fts (FTS5, the /search_text index)        <- everything
  chunk_text_dict_fts (FTS5)                            <- definitions (Black's-style)
Dedups by chunk_id (INSERT OR IGNORE), so re-running is safe. No ChromaDB, no embedding
required for keyword search (set INGEST_EMBED=1 to also compute vectors later).

Usage (unchanged CLI):
  ingest_text.py --file /path/file.(txt|md|pdf) --source name --type definition [--term X]
  ingest_text.py --dir  /path/dir --type definition        # ingests .txt/.md/.pdf
  ingest_text.py --text "..." --source name --type definition --term estoppel
  ingest_text.py --count
"""
import argparse, hashlib, sys, os, re, sqlite3
from pathlib import Path

CHUNK_DB = os.environ.get("CHUNK_TEXT_DB", "/home/{{USER_FIRST}}/turbovec/chunk_text.db")
EMBED = os.environ.get("INGEST_EMBED", "0") == "1"      # FTS needs no embedding; opt-in for vectors
REPO = "{{ENTROPY_HOME}}"


class ChunkTextDB:
    """Direct chunk_text.db backend — the ChromaDB bypass. Mimics the chroma collection API."""
    def __init__(self, path):
        self.path = path

    def count(self):
        con = sqlite3.connect(self.path, timeout=120)
        n = con.execute("SELECT count(*) FROM chunk_text").fetchone()[0]
        con.close()
        return n

    def upsert(self, ids, documents, embeddings=None, metadatas=None):
        con = sqlite3.connect(self.path, timeout=120)
        cur = con.cursor()
        added = 0
        metadatas = metadatas or [{}] * len(ids)
        for cid, text, meta in zip(ids, documents, metadatas):
            meta = meta or {}
            src = meta.get("source", "") or ""
            ctype = meta.get("chunk_type", "") or ""
            cites = meta.get("citations", "") or ""
            juris = meta.get("jurisdiction", "") or "US"
            cur.execute(
                "INSERT OR IGNORE INTO chunk_text(chunk_id,text,source,jurisdiction,citations) VALUES(?,?,?,?,?)",
                (cid, text, src, juris, cites))
            if cur.rowcount:                                  # only index genuinely-new chunks
                cur.execute("INSERT INTO chunk_text_fts(text,source_id) VALUES(?,?)", (text, cid))
                if ctype == "definition":
                    cur.execute("INSERT INTO chunk_text_dict_fts(text,source_id,source) VALUES(?,?,?)",
                                (text, cid, src))
                added += 1
        con.commit()
        con.close()
        return added


def get_client():
    db = CHUNK_DB
    class _C:
        def get_collection(self, name):   return ChunkTextDB(db)
        def create_collection(self, name, metadata=None): return ChunkTextDB(db)
    return _C()


def get_model():
    if not EMBED:
        return None
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer("BAAI/bge-base-en-v1.5")


def _extract(path: Path) -> str:
    suf = path.suffix.lower()
    if suf == ".pdf":
        sys.path.insert(0, REPO)
        from pipeline.doc_reader import extract_text
        t = extract_text(str(path))
        return t if isinstance(t, str) else (t.get("text", "") if isinstance(t, dict) else "")
    raw = path.read_text(encoding="utf-8", errors="replace")
    if suf == ".md":
        raw = re.sub(r"^---.*?---", "", raw, flags=re.S).strip()      # strip YAML frontmatter
    return raw


def chunk_text(text, doc_id, chunk_size=400, overlap=50):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    words = text.split()
    chunks, i, idx = [], 0, 0
    while i < len(words):
        ct = " ".join(words[i:i + chunk_size])
        if len(ct.strip()) >= 50:
            chunks.append({"id": hashlib.md5(f"{doc_id}_{idx}".encode()).hexdigest(), "text": ct, "index": idx})
            idx += 1
        i += chunk_size - overlap
    return chunks


def ingest_chunks(col, model, chunks, source, chunk_type, section="", term=""):
    if not chunks:
        return 0
    total = 0
    for i in range(0, len(chunks), 100):
        batch = chunks[i:i + 100]
        texts = [c["text"] for c in batch]
        ids = [c["id"] for c in batch]
        metas = [{"source": source, "chunk_type": chunk_type, "chunk_index": c["index"],
                  "section": section, "citations": "", "term": term} for c in batch]
        vectors = model.encode(texts, normalize_embeddings=True, show_progress_bar=False).tolist() if model else None
        total += col.upsert(ids=ids, documents=texts, embeddings=vectors, metadatas=metas)
    return total


def ingest_definition(col, model, text, source, term):
    cid = hashlib.md5(f"{source}_{term}".encode()).hexdigest()
    vec = [model.encode(text, normalize_embeddings=True).tolist()] if model else None
    return col.upsert(ids=[cid], documents=[text], embeddings=vec,
                      metadatas=[{"source": source, "chunk_type": "definition", "term": term}])


def _ingest_path(col, model, p: Path, args):
    text = _extract(p)
    if len(text.strip()) < 50:
        print(f"  skip {p.name} (<50 chars)")
        return 0
    if args.type == "definition" and args.term:
        n = ingest_definition(col, model, text, args.source or p.stem, args.term)
    else:
        chunks = chunk_text(text, p.stem, args.chunk_size, args.overlap)
        n = ingest_chunks(col, model, chunks, args.source or p.stem, args.type, args.section, args.term)
    print(f"  {p.name}: +{n} new chunk(s)")
    return n


def main():
    ap = argparse.ArgumentParser(description="Ingest legal text into chunk_text.db (ChromaDB-free)")
    ap.add_argument("--text", default=""); ap.add_argument("--file", default="")
    ap.add_argument("--dir", default=""); ap.add_argument("--source", default="manual")
    ap.add_argument("--type", default="text"); ap.add_argument("--section", default="")
    ap.add_argument("--term", default=""); ap.add_argument("--chunk-size", type=int, default=400)
    ap.add_argument("--overlap", type=int, default=50); ap.add_argument("--count", action="store_true")
    ap.add_argument("--collection", default="legal_docs")
    args = ap.parse_args()

    col = get_client().get_collection(args.collection)
    if args.count:
        print(f"chunk_text rows: {col.count()}"); return
    before = col.count()
    model = get_model()
    total = 0

    if args.text:
        if args.type == "definition" and args.term:
            total = ingest_definition(col, model, args.text, args.source, args.term)
        else:
            total = ingest_chunks(col, model, chunk_text(args.text, args.source, args.chunk_size, args.overlap),
                                  args.source, args.type, args.section, args.term)
        print(f"text: +{total}")
    elif args.file:
        p = Path(args.file)
        if not p.exists():
            print(f"not found: {args.file}"); sys.exit(1)
        total = _ingest_path(col, model, p, args)
    elif args.dir:
        d = Path(args.dir)
        if not d.is_dir():
            print(f"not a dir: {args.dir}"); sys.exit(1)
        files = sorted([f for f in d.iterdir() if f.suffix.lower() in (".txt", ".md", ".pdf")
                        and not f.name.startswith(".") and "LEARNING_LOG" not in f.name])
        print(f"{len(files)} file(s) in {args.dir}")
        for f in files:
            try:
                total += _ingest_path(col, model, f, args)
            except Exception as e:
                print(f"  ERR {f.name}: {e}")
    else:
        print("Provide --text, --file, --dir, or --count"); sys.exit(1)

    print(f"\nNew chunks added: {total}   (chunk_text rows {before} -> {col.count()})")


if __name__ == "__main__":
    main()
