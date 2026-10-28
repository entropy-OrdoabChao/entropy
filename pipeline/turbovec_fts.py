"""turbovec_fts.py — torch-free, numpy-free FTS/chunk access to ~/turbovec/chunk_text.db.

Shared by the daemon (pipeline/turbovec_daemon.py) and the client fallback (pipeline/retrieval_client.py), so
both answer the same shape from the same SQL.  Every query is bounded: a sqlite interrupt timer fires at
`timeout_s` and the call returns {"error": ...} instead of grinding (the 2026-10-12 class: a 300-char MATCH
ran for minutes at 1300% CPU).

Schema of record (read-only):
  chunk_text(chunk_id TEXT PK, text, source, jurisdiction, citations) WITHOUT ROWID, index ix_ct_source(source)
  chunk_text_fts(text, source_id UNINDEXED)  -- fts5 porter unicode61; source_id == chunk_text.chunk_id
  chunk_part(chunk_id, part)                 -- optional side DB chunk_part.db (majority/dissent/...)
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import os, re, sqlite3, threading
from pathlib import Path

TV_DIR = Path(os.environ.get("TURBOVEC_DIR", str(_eroot() / "corpus")))
CHUNK_DB = TV_DIR / "chunk_text.db"
PART_DB = TV_DIR / "chunk_part.db"
HOLDING_PARTS = {"majority", "unknown", None}
_LOCAL = threading.local()
_STOP = set("the a an of to in and or for on by with that this is are be as at from it its shall may any such".split())


def _con() -> sqlite3.Connection:
    con = getattr(_LOCAL, "con", None)
    if con is None:
        con = sqlite3.connect(f"file:{CHUNK_DB}?mode=ro", uri=True, timeout=30)
        _LOCAL.con = con
    return con


def _pcon():
    if not PART_DB.exists():
        return None
    pc = getattr(_LOCAL, "pcon", None)
    if pc is None:
        pc = sqlite3.connect(f"file:{PART_DB}?mode=ro", uri=True, timeout=30)
        _LOCAL.pcon = pc
    return pc


def part_of(chunk_id: str):
    pc = _pcon()
    if pc is None or not chunk_id:
        return None
    r = pc.execute("SELECT part FROM chunk_part WHERE chunk_id=?", (chunk_id,)).fetchone()
    return r[0] if r else None


def _bounded(fn, timeout_s: float):
    """Run fn(con) with a sqlite interrupt after timeout_s seconds."""
    con = _con()
    t = threading.Timer(timeout_s, con.interrupt)
    t.daemon = True
    t.start()
    try:
        return fn(con)
    except sqlite3.OperationalError as e:
        if "interrupted" in str(e).lower():
            return {"error": f"sqlite interrupted after {timeout_s}s"}
        return {"error": f"sqlite: {e}"}
    finally:
        t.cancel()


def _prefix_ok(source: str, source_prefix) -> bool:
    if not source_prefix:
        return True
    s = (source or "").lower()
    pres = [source_prefix] if isinstance(source_prefix, str) else list(source_prefix)
    return any(s.startswith(p.lower()) for p in pres if p)


def chunk(chunk_id: str, timeout_s: float = 20.0) -> dict:
    """Round-trip one chunk by id → {chunk_id, source, text, part} or {error}."""
    if not chunk_id:
        return {"error": "chunk_id required"}

    def q(con):
        r = con.execute("SELECT chunk_id, text, source, jurisdiction FROM chunk_text WHERE chunk_id=?", (chunk_id,)).fetchone()
        if not r:
            return {"error": f"chunk not found: {chunk_id}", "chunk_id": chunk_id}
        return {"chunk_id": r[0], "text": r[1] or "", "source": r[2] or "?", "jurisdiction": r[3], "part": part_of(r[0])}
    return _bounded(q, timeout_s)


def fts_phrase(phrase: str, source_prefix=None, limit: int = 40, timeout_s: float = 25.0, text_chars: int = 1200) -> dict:
    """Exact FTS5 phrase hit list (porter-stemmed).  Returns {"hits":[{chunk_id, source, text, part}], "mode":"fts"}."""
    ph = re.sub(r'["\']', " ", phrase or "").strip()
    if len(ph.split()) < 2:
        return {"error": "phrase too short (need >= 2 words)", "hits": []}
    pool = max(limit, 200) if source_prefix else limit

    def q(con):
        rows = con.execute("SELECT source_id FROM chunk_text_fts WHERE chunk_text_fts MATCH ? LIMIT ?", (f'"{ph}"', pool)).fetchall()
        hits = []
        for (sid,) in rows:
            r = con.execute("SELECT text, source FROM chunk_text WHERE chunk_id=?", (sid,)).fetchone()
            if not r or not _prefix_ok(r[1], source_prefix):
                continue
            hits.append({"chunk_id": sid, "source": r[1] or "?", "text": (r[0] or "")[:text_chars], "part": part_of(sid)})
            if len(hits) >= limit:
                break
        return {"hits": hits, "mode": "fts", "phrase": ph}
    return _bounded(q, timeout_s)


def fts_semantic_fallback(query: str, k: int = 6, source_prefix=None, timeout_s: float = 25.0) -> dict:
    """FTS-only stand-in for semantic(): bm25-ranked AND of the query's content words, relaxing to the longest
    half when AND is empty.  Same hit shape as semantic() with mode='fts_fallback' so callers never branch."""
    words = [w for w in re.findall(r"[A-Za-z0-9§]{3,}", query or "") if w.lower() not in _STOP][:12]
    if not words:
        return {"error": "query has no searchable words", "hits": [], "mode": "fts_fallback"}
    pool = max(k * 10, 60) if source_prefix else k * 4

    def q(con):
        def run(ws):
            m = " AND ".join(f'"{w}"' for w in ws)
            return con.execute("SELECT source_id, bm25(chunk_text_fts) FROM chunk_text_fts WHERE chunk_text_fts MATCH ? "
                               "ORDER BY rank LIMIT ?", (m, pool)).fetchall()
        rows = run(words)
        if not rows and len(words) > 3:
            rows = run(words[: max(3, len(words) // 2)])
        hits, seen = [], set()
        for sid, sc in rows:
            r = con.execute("SELECT text, source FROM chunk_text WHERE chunk_id=?", (sid,)).fetchone()
            if not r or not r[0] or not _prefix_ok(r[1], source_prefix):
                continue
            key = r[0][:80]
            if key in seen:
                continue
            seen.add(key)
            part = part_of(sid)
            if part not in HOLDING_PARTS:
                continue
            hits.append({"chunk_id": sid, "source": r[1] or "?", "part": part or "-", "score": round(-float(sc), 3), "text": r[0][:1200]})
            if len(hits) >= k:
                break
        return {"hits": hits, "mode": "fts_fallback", "query": query}
    return _bounded(q, timeout_s)
