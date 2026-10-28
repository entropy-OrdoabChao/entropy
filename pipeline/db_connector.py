"""
pipeline/db_connector.py Ã¢â‚¬â€ ChromaDB connector for the legal database.

Connects to the EXISTING ChromaDB Docker container (port 8100).
Collection: legal_docs (2.4M+ chunks, 768-dim bge-base-en-v1.5 embeddings)

DATABASE PROTECTION RULES:
  - DO NOT delete collections
  - DO NOT modify structure
  - DO NOT overwrite data
  - DO NOT re-index unless explicitly instructed
  - DO NOT recreate embeddings
  - READ-ONLY queries only
"""

import time
from typing import Optional

from core.logger import get_logger

log = get_logger("DB")

# Ã¢â€â‚¬Ã¢â€â‚¬ Configuration Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬

CHROMA_HOST = "127.0.0.1"  # BLOCK_8_1_IPV4_LOCK
CHROMA_PORT = 8100
COLLECTION_NAME = "legal_docs"

# === THE DB IS THE BEEZLE CORPUS ===
# :8100 runs the beezle-corpus HTTP service (docker/beezle-corpus), NOT ChromaDB. This connector
# speaks the corpus API (/search_text, /status) — the old chromadb client 404'd on /api/v2/auth/identity.
import os as _os
import requests as _rq
CORPUS_URL = _os.environ.get("BEEZLE_CORPUS_URL", f"http://{CHROMA_HOST}:{CHROMA_PORT}")


def _corpus_search(query: str, top_k: int = 10) -> list[dict]:
    """Query the beezle corpus, mapping its hits to the legacy LegalDB hit-dict shape so all callers
    of query_text() keep working unchanged."""
    try:
        r = _rq.get(f"{CORPUS_URL}/search_text", params={"query": query, "top_k": top_k}, timeout=30)
        r.raise_for_status()
        hits = r.json().get("hits", []) or []
    except Exception as e:
        log.error("corpus_search", "beezle-corpus query failed", error=str(e))
        return []
    out = []
    for h in hits:
        meta = h.get("metadata") or {}
        score = h.get("score")
        out.append({
            "id": h.get("chunk_id") or h.get("id") or "",
            "text": h.get("text", ""),
            "distance": (1.0 - score) if isinstance(score, (int, float)) else 0.0,
            "source": h.get("source") or meta.get("source", "unknown"),
            "section": meta.get("section", ""),
            "chunk_type": meta.get("chunk_type", "general"),
            "citations": h.get("authorities") or meta.get("citations", ""),
            "doc_id": meta.get("doc_id", ""),
            "has_definitions": str(meta.get("has_defs", "False")) == "True",
            "metadata": meta,
        })
    return out
EMBEDDING_MODEL = "BAAI/bge-base-en-v1.5"  # 768-dim Ã¢â‚¬â€ MUST match what DB was built with




# ============================================================================
# v18e2 Speedup: Module-level embedder singleton
# Loads BAAI/bge-base-en-v1.5 once, reused across ALL LegalDB instances.
# Without this, each agent that creates a fresh LegalDB() reloads the model
# (~3-5s wasted per fresh instance).
# ============================================================================
_V18E2_MODULE_EMBEDDER = None


def _v18e2_get_module_embedder():
    """Module-level singleton accessor for the embedder."""
    global _V18E2_MODULE_EMBEDDER
    if _V18E2_MODULE_EMBEDDER is not None:
        return _V18E2_MODULE_EMBEDDER

    try:
        from sentence_transformers import SentenceTransformer
        log.stage("v18e2_embedder", f"Loading {EMBEDDING_MODEL} singleton (one-time)")
        model = SentenceTransformer(EMBEDDING_MODEL)
        # GPU-PROBE HARDENING (2026-10-24): the former `torch.cuda.is_available()` check here did nothing (pass) but
        # opened /dev/dxg; with the WSL bridge wedged it hung the job in D state. Embedder stays on CPU; no CUDA probe.
        # Lane liveness, when needed, is HTTP: curl -s --max-time 3 localhost:8000/v1/models (local-gpu) / :8080 (local-gpu).
        _V18E2_MODULE_EMBEDDER = model
        return model
    except ImportError:
        log.error("v18e2_embedder", "sentence-transformers not installed")
        raise


class LegalDB:
    """Read-only interface to the ChromaDB legal database."""

    def __init__(self, host: str = CHROMA_HOST, port: int = CHROMA_PORT):
        self.host = host
        self.port = port
        self._client = None
        self._collection = None
        self._embedder = None

    def _connect(self):
        """No-op — the beezle corpus is a stateless HTTP service; no client/collection to hold."""
        return

    def _get_embedder(self):
        """Lazy-load the embedding model â€” v18e2 uses module singleton."""
        if self._embedder is not None:
            return self._embedder
        self._embedder = _v18e2_get_module_embedder()
        return self._embedder

    def _embed_query(self, text: str) -> list[float]:
        """Embed a query string using the correct model (768-dim)."""
        model = self._get_embedder()
        vec = model.encode(text, normalize_embeddings=True)
        return vec.tolist()

    def health_check(self) -> dict:
        """Check the beezle-corpus service (/status)."""
        try:
            r = _rq.get(f"{CORPUS_URL}/status", timeout=8)
            r.raise_for_status()
            s = r.json()
            cdb = s.get("chunk_db", {}) or {}
            return {
                "status": "ok" if cdb.get("available") else "degraded",
                "service": s.get("service", "beezle-corpus"),
                "chunk_db_mb": cdb.get("size_mb"),
                "query_table": s.get("query_table") or cdb.get("query_table"),
            }
        except Exception as e:
            return {"status": "offline", "error": str(e)}

    def query_text(self, query_text: str, n_results: int = 10,
                   where: Optional[dict] = None) -> list[dict]:
        """Query by text Ã¢â‚¬â€ embeds with bge-base-en-v1.5 (768-dim) to match DB."""
        # beezle corpus does FTS server-side — no client-side embedding needed
        t0 = time.time()
        hits = _corpus_search(query_text, top_k=n_results)
        log.stage("query_text", f"Retrieved {len(hits)} from beezle corpus in {int((time.time()-t0)*1000)}ms")
        return hits

    def query_embedding(self, embedding: list[float], n_results: int = 10,
                        where: Optional[dict] = None) -> list[dict]:
        """The beezle corpus exposes FTS, not a vector endpoint. Vector queries are not supported;
        callers should use query_text(). Returns [] rather than crash."""
        log.error("query_embedding", "beezle corpus has no vector endpoint — use query_text()")
        return []

    def get_count(self) -> int:
        """Approx corpus size (MB) from /status; corpus does not expose an exact row count cheaply."""
        h = self.health_check()
        return int(h.get("chunk_db_mb") or 0)

    def get_collection_info(self) -> dict:
        """Corpus status as collection-info."""
        return {"name": "beezle_corpus", **self.health_check()}

    def _format_results(self, raw_result: dict) -> list[dict]:
        """Convert ChromaDB result format to flat list of hit dicts."""
        hits = []
        ids = raw_result.get("ids", [[]])[0]
        docs = raw_result.get("documents", [[]])[0]
        metas = raw_result.get("metadatas", [[]])[0] or []
        dists = raw_result.get("distances", [[]])[0]

        for i in range(len(ids)):
            meta = metas[i] if (i < len(metas) and metas[i] is not None) else {}
            hit = {
                "id": ids[i],
                "text": docs[i] if i < len(docs) else "",
                "distance": dists[i] if i < len(dists) else 0,
                "source": meta.get("source", "unknown"),
                "section": meta.get("section", ""),
                "chunk_type": meta.get("chunk_type", "general"),
                "citations": meta.get("citations", ""),
                "doc_id": meta.get("doc_id", ""),
                "has_definitions": meta.get("has_defs", "False") == "True",
                "metadata": meta,
            }
            hits.append(hit)

        return hits


# Ã¢â€â‚¬Ã¢â€â‚¬ Singleton Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬

_instance: Optional[LegalDB] = None

def get_db(host: str = CHROMA_HOST, port: int = CHROMA_PORT) -> LegalDB:
    """Get or create the DB singleton."""
    global _instance
    if _instance is None:
        _instance = LegalDB(host, port)
    return _instance

