#!/usr/bin/env python3
"""
turbovec_search.py — semantic search over the TurboVec authority corpus.

Embeds a query with BAAI/bge-base-en-v1.5 (the model that built
authority_vectors_768.npy) and cosine-matches against the 108720-authority
matrix. Returns REAL authorities from authority_ids.json. Retrieval only —
nothing generated. A similarity floor drops weak matches so no off-point or
noise citation is ever returned.
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import json
import threading
from pathlib import Path

TURBOVEC_DIR = (_eroot() / "corpus")
VECTORS_PATH = TURBOVEC_DIR / "authority_vectors_768.npy"
IDS_PATH = TURBOVEC_DIR / "authority_ids.json"
EMBEDDING_MODEL = "BAAI/bge-base-en-v1.5"          # MUST match corpus build
BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "
DEFAULT_MIN_SIM = 0.45                              # anti-hallucination floor

_LOCK = threading.Lock()
_VECS = None        # normalized (N,768) float32
_IDS = None         # list of dicts
_MODEL = None


def _load():
    global _VECS, _IDS, _MODEL
    if _VECS is not None:
        return
    with _LOCK:
        if _VECS is not None:
            return
        import numpy as np
        ids = json.loads(IDS_PATH.read_text(encoding="utf-8"))
        vecs = np.load(VECTORS_PATH, mmap_mode="r").astype("float32")
        # L2-normalize for cosine via dot product
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        vecs = vecs / norms
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer(EMBEDDING_MODEL)
        _IDS, _VECS, _MODEL = ids, vecs, model


def _is_excluded(rec) -> bool:
    """Drop overruled/blacklisted authorities if the corpus flags them."""
    for k in ("overruled", "blacklisted", "excluded"):
        if rec.get(k):
            return True
    status = str(rec.get("status", "")).lower()
    return status in ("overruled", "blacklisted")


def search_authorities(query_text: str, top_k: int = 4,
                       min_sim: float = DEFAULT_MIN_SIM) -> list[dict]:
    """Return up to top_k real authorities most similar to query_text.
    Each result: {citation, authority_id, similarity}. Empty list if no match
    clears the similarity floor (honest empty rather than a weak/wrong cite)."""
    if not query_text or not query_text.strip():
        return []
    try:
        _load()
        import numpy as np
        q = _MODEL.encode([BGE_QUERY_PREFIX + query_text],
                          normalize_embeddings=True)[0].astype("float32")
        sims = _VECS @ q                       # cosine (both normalized)
        # take a few extra to allow exclusion filtering
        k = min(top_k * 4, sims.shape[0])
        idx = np.argpartition(-sims, k - 1)[:k]
        idx = idx[np.argsort(-sims[idx])]
        out = []
        for i in idx:
            s = float(sims[i])
            if s < min_sim:
                break                          # sorted desc — nothing better remains
            rec = _IDS[int(i)]
            if _is_excluded(rec):
                continue
            out.append({
                "citation": rec.get("citation") or rec.get("authority_id", ""),
                "authority_id": rec.get("authority_id", ""),
                "similarity": round(s, 4),
            })
            if len(out) >= top_k:
                break
        return out
    except Exception as e:
        # fail honest: no enrichment rather than wrong enrichment
        import logging
        logging.getLogger("turbovec_search").warning("search failed: %s", e)
        return []


if __name__ == "__main__":
    import sys
    q = sys.argv[1] if len(sys.argv) > 1 else "prior restraint on filing motions injunction"
    for r in search_authorities(q):
        print(f"  {r['similarity']:.3f}  {r['citation']}")
