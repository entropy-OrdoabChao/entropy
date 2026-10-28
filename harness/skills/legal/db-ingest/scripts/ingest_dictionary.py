#!/usr/bin/env python3
"""
Ingest legal dictionary into ChromaDB.
Parses dictionary-format text (term followed by definition) into one chunk per definition.

Expects format:
  TERM. Definition text here spanning
  one or more lines.

  ANOTHER TERM. Another definition here.

Usage:
  ingest_dictionary.py --file /path/to/dictionary.txt --source blacks_law_4th
  ingest_dictionary.py --file /path/to/dictionary.txt --source bouviers
  ingest_dictionary.py --dry-run --file /path/to/dictionary.txt
"""

import argparse, hashlib, re, sys
from pathlib import Path

def parse_dictionary(text):
    """
    Parse dictionary text into (term, definition) pairs.
    Handles multiple common dictionary formats:
    - TERM. Definition...
    - TERM — Definition...
    - TERM: Definition...
    - TERM\nDefinition on next line...
    """
    entries = []
    # Split on blank-line-separated blocks
    blocks = re.split(r"\n\s*\n", text)

    for block in blocks:
        block = block.strip()
        if not block or len(block) < 10:
            continue

        # Try to extract term from start of block
        # Pattern 1: TERM. definition (all caps term followed by period)
        m = re.match(r"^([A-Z][A-Z\s,'-]{1,80})\.\s*(.+)", block, re.DOTALL)
        if m:
            term = m.group(1).strip().rstrip(",").strip()
            defn = m.group(2).strip()
            entries.append((term, f"{term}. {defn}"))
            continue

        # Pattern 2: TERM — definition
        m = re.match(r"^([A-Z][A-Z\s,'-]{1,80})\s*[—–-]\s*(.+)", block, re.DOTALL)
        if m:
            term = m.group(1).strip().rstrip(",").strip()
            defn = m.group(2).strip()
            entries.append((term, f"{term}. {defn}"))
            continue

        # Pattern 3: TERM: definition
        m = re.match(r"^([A-Z][A-Z\s,'-]{1,80}):\s*(.+)", block, re.DOTALL)
        if m:
            term = m.group(1).strip().rstrip(",").strip()
            defn = m.group(2).strip()
            entries.append((term, f"{term}. {defn}"))
            continue

        # Pattern 4: First line is the term (all caps), rest is definition
        lines = block.split("\n")
        if len(lines) >= 2 and lines[0].strip().isupper() and len(lines[0].strip()) < 80:
            term = lines[0].strip().rstrip(".")
            defn = " ".join(l.strip() for l in lines[1:])
            entries.append((term, f"{term}. {defn}"))
            continue

    return entries

def ingest_definitions(entries, source, collection_name="legal_docs", dry_run=False):
    """Ingest parsed definitions into ChromaDB."""
    if dry_run:
        print(f"DRY RUN — would ingest {len(entries)} definitions")
        for i, (term, defn) in enumerate(entries[:10]):
            print(f"  {i+1}. {term}: {defn[:100]}...")
        if len(entries) > 10:
            print(f"  ... and {len(entries) - 10} more")
        return

    import chromadb
    from sentence_transformers import SentenceTransformer

    client = chromadb.HttpClient(host="localhost", port=8100)
    try:
        col = client.get_collection(collection_name)
    except:
        col = client.create_collection(name=collection_name, metadata={"hnsw:space": "cosine"})

    model = SentenceTransformer("BAAI/bge-base-en-v1.5")

    batch_size = 100
    total = 0

    for i in range(0, len(entries), batch_size):
        batch = entries[i:i + batch_size]
        texts = [defn for _, defn in batch]
        terms = [term for term, _ in batch]
        ids = [hashlib.md5(f"{source}_{term}".encode()).hexdigest() for term in terms]
        metas = [{
            "source": source,
            "chunk_type": "definition",
            "chunk_index": 0,
            "section": "",
            "citations": "",
            "term": term.lower()
        } for term in terms]

        vectors = model.encode(texts, normalize_embeddings=True, show_progress_bar=False).tolist()
        col.upsert(ids=ids, documents=texts, embeddings=vectors, metadatas=metas)
        total += len(batch)

        if (i // batch_size) % 10 == 0:
            print(f"  Ingested {total}/{len(entries)} definitions...")

    print(f"\nTotal ingested: {total} definitions")
    print(f"Collection now has: {col.count()} chunks")

def main():
    parser = argparse.ArgumentParser(description="Ingest legal dictionary into ChromaDB")
    parser.add_argument("--file", required=True, help="Path to dictionary text file")
    parser.add_argument("--source", default="legal_dictionary", help="Source identifier (e.g., blacks_law_4th, bouviers)")
    parser.add_argument("--collection", default="legal_docs", help="ChromaDB collection name")
    parser.add_argument("--dry-run", action="store_true", help="Parse only, don't ingest")

    args = parser.parse_args()

    filepath = Path(args.file)
    if not filepath.exists():
        print(f"File not found: {args.file}")
        sys.exit(1)

    print(f"Reading {filepath.name}...")
    text = filepath.read_text(encoding="utf-8", errors="replace")
    print(f"File size: {len(text):,} characters")

    print("Parsing definitions...")
    entries = parse_dictionary(text)
    print(f"Found {len(entries)} definitions")

    if not entries:
        print("No definitions found. Check file format.")
        print("Expected: TERM. Definition text...")
        sys.exit(1)

    ingest_definitions(entries, args.source, args.collection, args.dry_run)

if __name__ == "__main__":
    main()
