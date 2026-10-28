"""
corpus_client_resilient.py
--------------------------

Drop-in wrapper around the original Hermes corpus_client.py that adds:
  - 2-attempt cap (configurable)
  - Exponential backoff
  - Auto-broaden query on empty result (drops to first N words)
  - Fallback declaration: if all attempts return empty, raise CorpusEmpty
    so the caller can route to Black's / Bouvier's / web search instead
    of looping forever on the same dead query.

Designed to fix the "infinite loop on `you`" bug observed 2026-06-12.

Usage:
    from corpus_client_resilient import ResilientCorpusClient

    client = ResilientCorpusClient(max_attempts=2)
    try:
        results = client.search_text("FCRA remedy", top_k=10)
    except CorpusEmpty:
        # caller should pivot to fallback source
        ...
"""

import time
from typing import Optional


class CorpusEmpty(Exception):
    """Raised when the corpus has been queried with reasonable variants
    and produced no useful results. Caller should pivot, not retry."""


class ResilientCorpusClient:
    def __init__(self, base_url="http://localhost:8100",
                 max_attempts=2, base_delay=0.5, broadener_min_words=2):
        self.base_url = base_url
        self.max_attempts = int(max_attempts)
        self.base_delay = float(base_delay)
        self.broadener_min_words = int(broadener_min_words)

        # Lazy import — the original client may not be in path
        try:
            from corpus_client import CorpusClient
            self._client = CorpusClient(base_url=base_url)
        except ImportError:
            import requests
            self._requests = requests
            self._client = None

    def _raw_search(self, query, top_k=10):
        if self._client is not None:
            return self._client.search_text(query, top_k=top_k)
        # Fallback: raw HTTP
        r = self._requests.get(
            f"{self.base_url}/search_text",
            params={"query": query, "top_k": top_k},
            timeout=15,
        )
        r.raise_for_status()
        return r.json()

    def search_text(self, query, top_k=10):
        """Search with retry, broadening, and bounded attempts.

        Returns the result dict on hit. Raises CorpusEmpty if exhausted."""
        tried = []
        current_query = query

        for attempt in range(self.max_attempts):
            tried.append(current_query)
            try:
                data = self._raw_search(current_query, top_k=top_k)
            except Exception as e:
                # network / server error — sleep and retry
                if attempt < self.max_attempts - 1:
                    time.sleep(self.base_delay * (attempt + 1))
                    continue
                raise CorpusEmpty(
                    f"corpus query errored {self.max_attempts}x: {e} (tried: {tried})"
                ) from e

            results = data.get("hits") or []
            if results:
                return data

            # Empty result — try broadening if we can
            words = current_query.split()
            if len(words) > self.broadener_min_words and attempt < self.max_attempts - 1:
                current_query = " ".join(words[:self.broadener_min_words])
                time.sleep(self.base_delay * (attempt + 1))
                continue

            # No more broadening; this attempt counts but we keep going
            if attempt < self.max_attempts - 1:
                time.sleep(self.base_delay * (attempt + 1))
                continue

        # All attempts exhausted with no results
        raise CorpusEmpty(
            f"corpus has no results for query (after {self.max_attempts} attempts). "
            f"Tried: {tried}. Caller should fall back to Black's / Bouvier's / web."
        )

    def search_text_or_none(self, query, top_k=10):
        """Same as search_text but returns None instead of raising."""
        try:
            return self.search_text(query, top_k=top_k)
        except CorpusEmpty:
            return None


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python3 corpus_client_resilient.py <query> [top_k]")
        sys.exit(1)
    q = sys.argv[1]
    k = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    client = ResilientCorpusClient()
    try:
        data = client.search_text(q, top_k=k)
        print(f"OK — {len(data.get('hits', []))} result(s)")
        for r in data.get("hits", [])[:3]:
            text = (r.get("text") or "")[:120]
            print(f"  - {text}...")
    except CorpusEmpty as e:
        print(f"EMPTY — {e}")
        sys.exit(2)
