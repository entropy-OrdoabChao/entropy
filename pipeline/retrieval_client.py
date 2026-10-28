"""retrieval_client.py — torch-free client for the turbovec daemon (pipeline/turbovec_daemon.py).

Drop-in for the per-process embedder: m2 analysis_runner, m0_authority_spine, relay-room ground_pack /
cells_to_ground / card_gate call these four functions and NEVER import torch / sentence_transformers.

  semantic(query, k=6, source_prefix=None)  -> {"hits":[{chunk_id, source, part, score, text}], "mode": embed|fts_fallback|client_fts}
  fts_phrase(phrase, source_prefix=None, limit=40) -> {"hits":[{chunk_id, source, text, part}], "mode":"fts"}
  chunk(chunk_id)                           -> {chunk_id, source, text, part} | {"error":...}
  health()                                  -> daemon health dict | {"ok":False,"error":...,"mode":"down"}

NEVER HANGS: connect timeout 2 s, request timeout 60 s (semantic; health 5 s).  When the daemon is down/slow the call falls back to the
FTS-only sqlite path in pipeline/turbovec_fts.py (same shapes, mode='client_fts'); if that fails too the
return is {"error": ...}.  Nothing here raises to the caller.
Env: TURBOVEC_SOCK (/tmp/turbovec.sock) · TURBOVEC_TCP (host:port, tried after the socket) ·
     TURBOVEC_CONNECT_TIMEOUT_S (2) · TURBOVEC_REQUEST_TIMEOUT_S (60) · TURBOVEC_CLIENT_NO_FALLBACK=1 (tests).
"""
from __future__ import annotations
import os, json, socket, time

SOCK = os.environ.get("TURBOVEC_SOCK", "/tmp/turbovec.sock")
TCP = os.environ.get("TURBOVEC_TCP", "")
CONNECT_TIMEOUT_S = float(os.environ.get("TURBOVEC_CONNECT_TIMEOUT_S", "2"))
REQUEST_TIMEOUT_S = float(os.environ.get("TURBOVEC_REQUEST_TIMEOUT_S", "60"))   # semantic: cold index pages in slowly; connect stays 2 s
_NO_FALLBACK = os.environ.get("TURBOVEC_CLIENT_NO_FALLBACK", "") == "1"
_LAST_DOWN = {"t": 0.0, "err": ""}
_DOWN_CACHE_S = float(os.environ.get("TURBOVEC_DOWN_CACHE_S", "5"))   # after a failed connect, skip retries briefly


def _connect():
    errs = []
    if SOCK:
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.settimeout(CONNECT_TIMEOUT_S); s.connect(SOCK); return s
        except OSError as e:
            errs.append(f"unix {SOCK}: {e}")
    if TCP:
        try:
            host, _, port = TCP.rpartition(":")
            return socket.create_connection((host or "127.0.0.1", int(port)), timeout=CONNECT_TIMEOUT_S)
        except OSError as e:
            errs.append(f"tcp {TCP}: {e}")
    raise ConnectionError("; ".join(errs) or "no endpoint configured")


def request(req: dict, timeout_s: float | None = None) -> dict:
    """One JSON request → response dict.  Returns {"error": ..., "down": True} on any transport failure."""
    if time.time() - _LAST_DOWN["t"] < _DOWN_CACHE_S:
        return {"error": f"daemon down (cached): {_LAST_DOWN['err']}", "down": True}
    s = None
    try:
        s = _connect()
        s.settimeout(timeout_s or REQUEST_TIMEOUT_S)
        s.sendall((json.dumps(req) + "\n").encode("utf-8"))
        buf = b""
        while not buf.endswith(b"\n"):
            part = s.recv(1 << 16)
            if not part:
                break
            buf += part
        if not buf:
            return {"error": "empty response", "down": True}
        return json.loads(buf.decode("utf-8", "replace"))
    except socket.timeout:
        return {"error": f"daemon request timed out after {timeout_s or REQUEST_TIMEOUT_S}s", "down": True, "timeout": True}
    except (ConnectionError, OSError, ValueError) as e:
        if isinstance(e, ConnectionError):
            _LAST_DOWN.update(t=time.time(), err=str(e))
        return {"error": f"daemon unreachable: {e}", "down": True}
    finally:
        if s is not None:
            try: s.close()
            except OSError: pass


def _fallback(fn, *a, **kw):
    if _NO_FALLBACK:
        return None
    try:
        from pipeline import turbovec_fts as F
        return getattr(F, fn)(*a, **kw)
    except Exception as e:
        return {"error": f"client FTS fallback failed: {e}"}


def health() -> dict:
    r = request({"op": "health"}, timeout_s=5)
    if r.get("down"):
        return {"ok": False, "mode": "down", "error": r.get("error")}
    return r


def semantic(query, k: int = 6, source_prefix=None) -> dict:
    r = request({"op": "semantic", "query": query, "k": k, "source_prefix": source_prefix})
    if r.get("down"):
        fb = _fallback("fts_semantic_fallback", query if isinstance(query, str) else " ".join(query), k, source_prefix)
        if fb is None:
            return r
        fb["mode"] = "client_fts"; fb["daemon_error"] = r.get("error"); return fb
    return r


def fts_phrase(phrase: str, source_prefix=None, limit: int = 40) -> dict:
    r = request({"op": "fts_phrase", "phrase": phrase, "source_prefix": source_prefix, "limit": limit})
    if r.get("down"):
        fb = _fallback("fts_phrase", phrase, source_prefix, limit)
        if fb is None:
            return r
        fb["mode"] = "client_fts"; fb["daemon_error"] = r.get("error"); return fb
    return r


def chunk(chunk_id: str) -> dict:
    r = request({"op": "chunk", "chunk_id": chunk_id})
    if r.get("down"):
        fb = _fallback("chunk", chunk_id)
        if fb is None:
            return r
        fb["mode"] = "client_fts"; return fb
    return r


def embed(texts, is_query: bool = False) -> dict:
    """Raw normalised bge vectors from the daemon (no local fallback — there is no torch here).
    {"vectors": [[...]], "dim": 768} or {"error": ..., "vectors": None}; callers must handle vectors=None."""
    r = request({"op": "embed", "texts": texts, "is_query": is_query})
    if r.get("down"):
        return {"error": r.get("error"), "vectors": None, "mode": "down"}
    return r


def hits(resp: dict) -> list:
    """Normalise any response to the list analysis_runner._retrieve returns ([{error}] on failure)."""
    if not isinstance(resp, dict):
        return [{"error": "bad response"}]
    if resp.get("error") and "hits" not in resp:
        return [{"error": resp["error"]}]
    return resp.get("hits") or []


if __name__ == "__main__":
    import sys
    op = sys.argv[1] if len(sys.argv) > 1 else "health"
    arg = " ".join(sys.argv[2:])
    out = {"health": lambda: health(), "semantic": lambda: semantic(arg), "fts_phrase": lambda: fts_phrase(arg), "chunk": lambda: chunk(arg)}[op]()
    print(json.dumps(out, indent=1, default=str)[:4000])
