#!/usr/bin/env python3
"""
corpus_client.py — Python wrapper for the beezle-corpus Docker API (port 8100).

Usage:
    from corpus_client import CorpusClient
    client = CorpusClient()
    results = client.search_text("FCRA remedy", top_k=10)
    term = client.lookup_term("furnisher")
    authority = client.lookup_authority("42 USC 1983")
    health = client.status()

Endpoints:
    GET /search_text?query=X&top_k=N — free-text search (FTS)
    GET /lookup_term?term=X&max_hits=N — exact + alias term lookup
    GET /lookup_authority?cite=X — authority by citation
    GET /status — health report
"""

import urllib.request
import urllib.parse
import json
import time
from typing import Optional, List, Dict, Any


class CorpusClient:
    """Client for the beezle-corpus Docker service at port 8100."""

    BASE_URL = "http://localhost:8100"

    def __init__(self, base_url: Optional[str] = None):
        if base_url:
            self.BASE_URL = base_url

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Make a GET request to the corpus service."""
        url = f"{self.BASE_URL}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)

        start = time.time()
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
                elapsed = time.time() - start
                return {**data, "_latency_ms": round(elapsed * 1000, 2)}
        except Exception as e:
            elapsed = time.time() - start
            return {
                "error": str(e),
                "_latency_ms": round(elapsed * 1000, 2),
                "_status": "error"
            }

    def search_text(self, query: str, top_k: int = 5) -> Dict[str, Any]:
        """Free-text search across the corpus (FTS).

        Args:
            query: search string (space-separated, no + — use spaces)
            top_k: number of results (1-50, default 5)

        Returns:
            {"query": "...", "count": N, "hits": [...], "_latency_ms": N}
        """
        return self._get("/search_text", {"query": query, "top_k": top_k})

    def lookup_term(self, term: str, max_hits: int = 10) -> Dict[str, Any]:
        """Exact + alias term lookup.

        Args:
            term: exact or partial term to look up
            max_hits: number of results (1-50, default 10)

        Returns:
            {"term": "...", "count": N, "hits": [...], "_latency_ms": N}
        """
        return self._get("/lookup_term", {"term": term, "max_hits": max_hits})

    def lookup_authority(self, cite: str) -> Dict[str, Any]:
        """Authority by citation.

        Args:
            cite: legal citation (e.g., "42 USC 1983", "15 USC 1681")

        Returns:
            {"cite": "...", "count": N, "hits": [...], "_latency_ms": N}
        """
        return self._get("/lookup_authority", {"cite": cite})

    def status(self) -> Dict[str, Any]:
        """Health report (chunk_db, vectors, doctrine_index).

        Returns:
            {"service": "beezle-corpus", "chunk_db": {...}, "vectors": {...},
             "doctrine_index": {...}, "_latency_ms": N}
        """
        return self._get("/status")

    def search_with_expansion(self, query: str, top_k: int = 10,
                              expansions: Optional[List[str]] = None) -> Dict[str, Any]:
        """Search with query expansion — tries multiple queries if the first returns few results.

        Args:
            query: base search query
            top_k: number of results per query (1-50, default 10)
            expansions: optional list of expansion keywords to try

        Returns:
            {"query": "...", "count": N, "hits": [...], "expansions_tried": [...],
             "_latency_ms": N}
        """
        all_hits = []
        seen_ids = set()
        expansions_tried = []

        # Try base query first
        result = self.search_text(query, top_k)
        if "hits" in result:
            for hit in result["hits"]:
                hit_id = hit.get("chunk_id", "")
                if hit_id and hit_id not in seen_ids:
                    seen_ids.add(hit_id)
                    all_hits.append(hit)
            if len(all_hits) >= top_k:
                return {**result, "hits": all_hits[:top_k], "expansions_tried": [query],
                        "_latency_ms": result.get("_latency_ms", 0)}

        # Try expansions
        if expansions:
            for expansion in expansions:
                if len(all_hits) >= top_k:
                    break
                result = self.search_text(f"{query} {expansion}", top_k)
                expansions_tried.append(f"{query} {expansion}")
                if "hits" in result:
                    for hit in result["hits"]:
                        hit_id = hit.get("chunk_id", "")
                        if hit_id and hit_id not in seen_ids:
                            seen_ids.add(hit_id)
                            all_hits.append(hit)

        total_latency = sum(
            r.get("_latency_ms", 0) for r in [self.search_text(query, top_k)] +
            ([self.search_text(f"{query} {e}", top_k) for e in expansions] if expansions else [])
        )

        return {
            "query": query,
            "count": len(all_hits),
            "hits": all_hits[:top_k],
            "expansions_tried": expansions_tried,
            "_latency_ms": total_latency
        }

    def search_multiple_terms(self, terms: List[str], top_k: int = 5) -> Dict[str, Any]:
        """Search for multiple terms and merge results.

        Args:
            terms: list of search terms
            top_k: number of results per term (1-50, default 5)

        Returns:
            {"queries": [...], "total_count": N, "hits": [...], "_latency_ms": N}
        """
        all_hits = []
        seen_ids = set()
        total_latency = 0

        for term in terms:
            result = self.search_text(term, top_k)
            total_latency += result.get("_latency_ms", 0)
            if "hits" in result:
                for hit in result["hits"]:
                    hit_id = hit.get("chunk_id", "")
                    if hit_id and hit_id not in seen_ids:
                        seen_ids.add(hit_id)
                        all_hits.append(hit)

        return {
            "queries": terms,
            "total_count": len(all_hits),
            "hits": all_hits[:top_k],
            "_latency_ms": total_latency
        }


# Convenience functions for quick use
def search_text(query: str, top_k: int = 5) -> Dict[str, Any]:
    """Quick free-text search."""
    return CorpusClient().search_text(query, top_k)

def lookup_term(term: str, max_hits: int = 10) -> Dict[str, Any]:
    """Quick term lookup."""
    return CorpusClient().lookup_term(term, max_hits)

def lookup_authority(cite: str) -> Dict[str, Any]:
    """Quick authority lookup."""
    return CorpusClient().lookup_authority(cite)

def status() -> Dict[str, Any]:
    """Quick health check."""
    return CorpusClient().status()
