#!/usr/bin/env python3
"""Search ChromaDB legal_docs collection from command line."""
import sys
import chromadb
from sentence_transformers import SentenceTransformer

def main():
    if len(sys.argv) < 2:
        print("Usage: search_chromadb.py <query> [n_results] [source_filter]")
        print("  query: search text")
        print("  n_results: number of results (default: 8)")
        print("  source_filter: filter by source name substring")
        sys.exit(1)

    query = sys.argv[1]
    n_results = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    source_filter = sys.argv[3] if len(sys.argv) > 3 else None

    client = chromadb.HttpClient(host="localhost", port=8100)
    col = client.get_collection("legal_docs")
    print(f"Collection: {col.count()} chunks")

    model = SentenceTransformer("BAAI/bge-base-en-v1.5")
    vec = model.encode(query).tolist()

    kwargs = {
        "query_embeddings": [vec],
        "n_results": n_results,
        "include": ["documents", "metadatas", "distances"]
    }
    if source_filter:
        kwargs["where"] = {"source": {"$contains": source_filter}}

    results = col.query(**kwargs)

    for i, (doc, meta, dist) in enumerate(zip(
        results["documents"][0],
        results["metadatas"][0],
        results["distances"][0]
    )):
        print(f"\n{'='*60}")
        print(f"Result {i+1} | Distance: {dist:.4f} | Source: {meta.get('source', '?')}")
        print(f"{'='*60}")
        print(doc[:800])

if __name__ == "__main__":
    main()
