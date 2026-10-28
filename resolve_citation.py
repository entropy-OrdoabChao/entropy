"""
Phase 2: resolve_citation MCP tool — single tool, two modes (v2.2)

v2.2 changes:
  - NEW: case-namespace queries route through case_index table (SQLite)
  - NEW: pin-cite stripping ("Marbury, 5 U.S. at 177" → still matches Marbury)
  - NEW: party-name-only queries ("Marbury v. Madison" w/o reporter)
         cleanly return not_found, do NOT fall to Chroma
  - REMOVED: Chroma fallback for case-namespace queries (was the p99 killer)

Spec contract:
  - Lookup mode target: <50ms p50, <120ms p99   (across ALL namespaces)
  - Extract mode: idempotent (same chunk re-processed = 0 new edges)
  - Hard namespace filter: USC query NEVER returns CFR chunks
  - case_index must be built first via build_case_index.py
"""
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

STRUCTURE_DB = str(_ehermes() / "beezle_structure.db")
CHROMA_HOST  = "localhost"
CHROMA_PORT  = 8100
COLLECTION   = "legal_docs"
# v2.3: chunk TEXT also lives in SQLite — the beezle-corpus container's
# /corpus/chunk_text.db (5.8M rows: chunk_id PK, text, source, ...), mounted at
# ~/turbovec/chunk_text.db on the host. The MCP server's venv has no
# chromadb client, so include_text resolves text from SQLite FIRST (card
# t_5e3479c5); Chroma remains the legacy fallback for ids SQLite is missing.
CHUNK_TEXT_DB = str(_eroot() / "corpus/chunk_text.db")

PARSER_DIR = str(Path.home() / "beezle_phase1")
if PARSER_DIR not in sys.path:
    sys.path.insert(0, PARSER_DIR)
from citation_parser import (  # noqa: E402
    parse, parse_all, Citation, _REPORTER_RE, _normalize_reporter,
)

# Shared reporter normalization with build_case_index
REPORTER_CANONICAL = {
    "us": "U.S.", "sct": "S.Ct.", "led": "L.Ed.",
    "fsupp": "F.Supp.", "fed_reporter": "F.",
    "atlantic": "A.", "northeastern": "N.E.", "pacific": "P.",
    "southeastern": "S.E.", "southern": "So.",
    "southwestern": "S.W.", "northwestern": "N.W.",
}
_SERIES_RE = re.compile(r"(2d|3d|4th)", re.IGNORECASE)


def _canonical_reporter_with_series(raw: str, reporter_id: str) -> str:
    base = REPORTER_CANONICAL.get(reporter_id)
    if not base:
        return re.sub(r"\s+", "", raw)
    series_match = _SERIES_RE.search(raw)
    if series_match:
        return f"{base}{series_match.group(1).lower().replace('rd', 'd')}"
    return base


def parse_case_lookup_key(text: str) -> tuple[int, str, int] | None:
    """Extract (volume, reporter, page) from any case-citation form.

    Handles:
      - "5 U.S. 137"
      - "Marbury v. Madison, 5 U.S. 137"
      - "Marbury v. Madison, 5 U.S. 137 (1803)"
      - "Marbury, 5 U.S. at 177"   (pin cite; we match by vol/rep/page=137 if
                                    present elsewhere — pin cites alone need
                                    additional context which we lack here, so
                                    "at 177" is parsed as page=177)
    Returns None for party-name-only or unparseable input.
    """
    m = _REPORTER_RE.search(text)
    if not m:
        return None
    rep_entry = _normalize_reporter(m["reporter"])
    if not rep_entry or rep_entry["kind"] != "case":
        return None
    try:
        volume = int(m["volume"])
        page = int(m["page"])
    except (ValueError, KeyError):
        return None
    canonical_reporter = _canonical_reporter_with_series(
        m["reporter"], rep_entry["id"]
    )
    return (volume, canonical_reporter, page)


_SQL_CONN = None
_CHUNK_CONN = None
_CHROMA_COL = None
_SCHEMA_ENSURED = False


def _get_chunk_text_db() -> sqlite3.Connection | None:
    """Lazily open the beezle-corpus chunk_text DB (read-only).

    Returns None when the file is absent (server keeps working; text fetch
    degrades to the Chroma fallback) — never raises at import/lookup time.
    """
    global _CHUNK_CONN
    if _CHUNK_CONN is None:
        if not Path(CHUNK_TEXT_DB).exists():
            return None
        try:
            _CHUNK_CONN = sqlite3.connect(
                f"file:{CHUNK_TEXT_DB}?mode=ro", uri=True,
                check_same_thread=False)
            _CHUNK_CONN.row_factory = sqlite3.Row
        except sqlite3.OperationalError:
            return None
    return _CHUNK_CONN


def _fetch_chunk_texts(chunk_ids: list[str]) -> dict[str, str]:
    """chunk_id -> text, SQLite-first (card t_5e3479c5).

    The authority table's chroma_chunk_ids ARE chunk_text.chunk_id PKs (the
    corpus server keys its FTS/text layers on the same ids), so a direct
    PRIMARY KEY lookup resolves text in ~0.1ms per id with no chromadb client.
    Missing ids simply stay out of the returned dict (caller keeps Chroma as
    the legacy fallback for those).
    """
    conn = _get_chunk_text_db()
    out: dict[str, str] = {}
    if conn is None or not chunk_ids:
        return out
    placeholders = ",".join("?" * len(chunk_ids))
    try:
        rows = conn.execute(
            f"SELECT chunk_id, text FROM chunk_text WHERE chunk_id IN ({placeholders})",
            list(chunk_ids)).fetchall()
    except sqlite3.OperationalError:
        return out
    for r in rows:
        if r["text"]:
            out[r["chunk_id"]] = r["text"]
    return out


def _get_sql() -> sqlite3.Connection:
    global _SQL_CONN, _SCHEMA_ENSURED
    if _SQL_CONN is None:
        _SQL_CONN = sqlite3.connect(STRUCTURE_DB, check_same_thread=False,
                                    isolation_level=None)
        _SQL_CONN.row_factory = sqlite3.Row
        _SQL_CONN.execute("PRAGMA journal_mode=DELETE")
        _SQL_CONN.execute("PRAGMA synchronous=NORMAL")
    if not _SCHEMA_ENSURED:
        try:
            _SQL_CONN.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_edge_chunk_unique
                ON citation_edge(src_id, dst_id, treatment, source_chunk)
            """)
        except sqlite3.OperationalError:
            pass
        _SCHEMA_ENSURED = True
    return _SQL_CONN


def _get_chroma():
    global _CHROMA_COL
    if _CHROMA_COL is None:
        import chromadb
        client = chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
        _CHROMA_COL = client.get_collection(COLLECTION)
    return _CHROMA_COL


def _authority_id(c: Citation) -> str:
    return f"{c.namespace}:{c.canonical}"


def _filter_by_namespace(chunks: list[dict], required_ns: str) -> list[dict]:
    return [c for c in chunks
            if (c.get("metadata") or {}).get("corpus_namespace") == required_ns]


def _section_root(canonical: str) -> str:
    m = re.search(r"§\s*([\d.\-]+)", canonical)
    return m.group(1) if m else ""


def _build_lookup_result(row, c: Citation, limit: int, include_text: bool,
                        out: dict, t0: float) -> dict:
    """Common builder for any SQLite-hit result."""
    chunk_ids = (row["chroma_chunk_ids"] or "").split(",")[:limit]
    chunk_ids = [cid for cid in chunk_ids if cid]
    out["chunk_ids"] = chunk_ids
    out["authority_id"] = row["id"]
    out["source"] = "sqlite"

    if include_text and chunk_ids:
        # SQLite-first (card t_5e3479c5): resolve text from chunk_text.db, which
        # the MCP server's venv can read directly — no chromadb client needed.
        raw_chunks: list[dict] = []
        texts = _fetch_chunk_texts(chunk_ids)
        resolved: set[str] = set()
        for cid in chunk_ids:
            doc = texts.get(cid)
            if doc:
                resolved.add(cid)
                raw_chunks.append({
                    "id": cid,
                    "text": (doc[:500] + "...") if len(doc) > 500 else doc,
                    # corpus_namespace is required: _filter_by_namespace drops
                    # chunks whose metadata lacks it (parity with Chroma metas)
                    "metadata": {"source": "sqlite",
                                 "corpus_namespace": c.namespace},
                })
        missing = [cid for cid in chunk_ids if cid not in resolved]
        if missing:
            # Legacy fallback for ids SQLite is missing (Chroma for regs only,
            # per the module docstring) — and a hard error when the client is
            # absent, so the caller degrades to ids-only instead of crashing.
            try:
                page = _get_chroma().get(ids=missing,
                                         include=["documents", "metadatas"])
                for cid, doc, meta in zip(page["ids"], page["documents"],
                                          page["metadatas"]):
                    if doc:
                        raw_chunks.append({
                            "id": cid,
                            "text": (doc[:500] + "...") if len(doc) > 500 else doc,
                            "metadata": meta or {},
                        })
            except Exception as e:
                # preserve the SQLite-resolved text; note only the missing ids
                out["error"] = f"chroma_fetch_failed (fallback; {len(missing)} id(s) " \
                               f"ids-only): {e}"
                for cid in missing:
                    raw_chunks.append({"id": cid})
        out["chunks"] = _filter_by_namespace(raw_chunks, c.namespace)

    out["latency_ms"] = (time.perf_counter() - t0) * 1000
    return out


# ============================================================
# Mode A: LOOKUP
# ============================================================
def _lookup(citation: str, namespace_hint: str = "",
            limit: int = 20, include_text: bool = False) -> dict:
    """Citation string → matching chunks. SQLite-only paths."""
    t0 = time.perf_counter()
    out = {
        "namespace": None,
        "canonical": None,
        "authority_id": None,
        "chunk_ids": [],
        "chunks": [],
        "source": "not_found",
        "latency_ms": 0.0,
        "error": None,
    }

    c = parse(citation)
    if c is None:
        out["error"] = "unparseable"
        out["latency_ms"] = (time.perf_counter() - t0) * 1000
        return out

    if namespace_hint and namespace_hint != c.namespace:
        out["error"] = f"namespace_hint_mismatch: parsed={c.namespace} hint={namespace_hint}"

    out["namespace"] = c.namespace
    out["canonical"] = c.canonical
    out["authority_id"] = _authority_id(c)

    cur = _get_sql().cursor()

    # ============================================================
    # CASE NAMESPACE — dedicated path via case_index
    # ============================================================
    if c.namespace == "case":
        key = parse_case_lookup_key(citation)
        if key is None:
            # Party-name-only or unparseable as vol/rep/page
            # Stay in not_found; do NOT fall to Chroma (slow path eliminated)
            out["error"] = "case_lookup_requires_volume_reporter_page"
            out["latency_ms"] = (time.perf_counter() - t0) * 1000
            return out

        volume, reporter, page = key
        cur.execute("""
            SELECT a.id, a.citation, a.corpus_namespace, a.chunk_count,
                   a.chroma_chunk_ids, ci.canonical AS ci_canonical
            FROM case_index ci
            JOIN authority a ON a.id = ci.authority_id
            WHERE ci.volume = ? AND ci.reporter = ? AND ci.page = ?
            LIMIT 1
        """, (volume, reporter, page))
        row = cur.fetchone()

        if row is not None:
            # Update canonical to the indexed form (e.g. with party names)
            out["canonical"] = row["ci_canonical"] or out["canonical"]
            return _build_lookup_result(row, c, limit, include_text, out, t0)

        # No case_index hit → return not_found cleanly
        out["latency_ms"] = (time.perf_counter() - t0) * 1000
        return out

    # ============================================================
    # STATUTE / REGULATION NAMESPACES
    # ============================================================
    # Tier 1: SQLite exact authority by id
    cur.execute("""
        SELECT id, citation, corpus_namespace, chunk_count, chroma_chunk_ids
        FROM authority
        WHERE id = ?
    """, (out["authority_id"],))
    row = cur.fetchone()

    # Tier 2: SQLite by canonical + namespace
    if row is None:
        cur.execute("""
            SELECT id, citation, corpus_namespace, chunk_count, chroma_chunk_ids
            FROM authority
            WHERE citation = ? AND corpus_namespace = ?
            LIMIT 1
        """, (c.canonical, c.namespace))
        row = cur.fetchone()

    # Tier 2b: SQLite section-root prefix
    if row is None and c.namespace.startswith(("usc_", "cfr_", "pa_stat_")):
        section_root = _section_root(c.canonical)
        if section_root:
            title_match = re.match(r"^(\d+)\s+(USC|CFR)\s+§\s+", c.canonical)
            if title_match:
                title_num, code = title_match.groups()
                root = section_root.split("(")[0].split(".")[0].split("-")[0]
                like_pattern = f"{title_num} {code} § {root}%"
                cur.execute("""
                    SELECT id, citation, corpus_namespace, chunk_count, chroma_chunk_ids
                    FROM authority
                    WHERE corpus_namespace = ? AND citation LIKE ?
                    ORDER BY length(citation) ASC
                    LIMIT 1
                """, (c.namespace, like_pattern))
                row = cur.fetchone()

    if row is not None:
        return _build_lookup_result(row, c, limit, include_text, out, t0)

    # Tier 3: Chroma metadata fallback — STATUTES/REGS ONLY
    try:
        coll = _get_chroma()
        where_filter = {
            "$and": [
                {"corpus_namespace": c.namespace},
                {"has_citation": "True"},
            ]
        }
        includes = ["metadatas"]
        if include_text:
            includes.append("documents")
        page = coll.get(where=where_filter, limit=limit * 5, include=includes)

        candidates = []
        for i, cid in enumerate(page["ids"]):
            meta = page["metadatas"][i] if page["metadatas"] else {}
            entry = {"id": cid, "metadata": meta or {}}
            if include_text and page.get("documents"):
                doc = page["documents"][i]
                entry["text"] = (doc[:500] + "...") if doc and len(doc) > 500 else doc
            candidates.append(entry)

        candidates = _filter_by_namespace(candidates, c.namespace)

        section_root = _section_root(c.canonical)
        section_pattern = section_root.split("(")[0].split(".")[0].split("-")[0] if section_root else None
        if section_pattern:
            filtered = []
            for entry in candidates:
                meta_str = json.dumps(entry.get("metadata") or {}, default=str)
                if section_pattern in meta_str:
                    filtered.append(entry)
            candidates = filtered[:limit]
        else:
            candidates = candidates[:limit]

        if candidates:
            out["chunk_ids"] = [e["id"] for e in candidates]
            out["chunks"] = candidates if include_text else []
            out["source"] = "chroma_meta"
    except Exception as e:
        out["error"] = f"chroma_meta_failed: {e}"

    out["latency_ms"] = (time.perf_counter() - t0) * 1000
    return out


# ============================================================
# Mode B: EXTRACT
# ============================================================
def _extract(text: str, source_chunk_id: str,
             treatment: str = "cited") -> dict:
    t0 = time.perf_counter()
    out = {
        "citations_found": 0,
        "edges_written": 0,
        "edges_skipped": 0,
        "unparseable_count": 0,
        "extracted_citations": [],
        "source_chunk_id": source_chunk_id,
        "latency_ms": 0.0,
        "error": None,
    }

    if not text or not source_chunk_id:
        out["error"] = "text and source_chunk_id required"
        out["latency_ms"] = (time.perf_counter() - t0) * 1000
        return out

    cur = _get_sql().cursor()
    cur.execute("""
        SELECT authority_id FROM chunk_processed WHERE chunk_id = ?
    """, (source_chunk_id,))
    src_row = cur.fetchone()
    src_authority_id = src_row[0] if src_row and src_row[0] else None

    citations = parse_all(text)
    out["citations_found"] = len(citations)

    if not citations:
        out["latency_ms"] = (time.perf_counter() - t0) * 1000
        return out

    seen_auth_ids = set()
    deduped = []
    for c in citations:
        aid = _authority_id(c)
        if aid in seen_auth_ids:
            continue
        seen_auth_ids.add(aid)
        deduped.append((c, aid))

    placeholders = ",".join("?" * len(seen_auth_ids))
    cur.execute(f"""
        SELECT id FROM authority WHERE id IN ({placeholders})
    """, list(seen_auth_ids))
    in_db = {r[0] for r in cur.fetchall()}

    for c, aid in deduped:
        out["extracted_citations"].append({
            "matched": c.raw,
            "canonical": c.canonical,
            "namespace": c.namespace,
            "authority_id": aid,
            "in_db": aid in in_db,
        })

    if src_authority_id is None:
        out["latency_ms"] = (time.perf_counter() - t0) * 1000
        return out

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    written = 0
    skipped = 0

    cur.execute("BEGIN")
    try:
        for _, aid in deduped:
            if aid == src_authority_id:
                continue
            if aid not in in_db:
                continue
            cur.execute("""
                INSERT OR IGNORE INTO citation_edge
                    (src_id, dst_id, treatment, confidence,
                     source_chunk, extracted_by, created_at)
                VALUES (?, ?, ?, 0.5, ?, 'resolve_citation_v2.2', ?)
            """, (src_authority_id, aid, treatment, source_chunk_id, now))
            if cur.rowcount > 0:
                written += 1
            else:
                skipped += 1
        cur.execute("COMMIT")
    except Exception as e:
        cur.execute("ROLLBACK")
        out["error"] = f"sqlite_write_failed: {e}"

    out["edges_written"] = written
    out["edges_skipped"] = skipped
    out["latency_ms"] = (time.perf_counter() - t0) * 1000
    return out


def resolve_citation_impl(
    citation: str = "",
    namespace_hint: str = "",
    mode: str = "lookup",
    limit: int = 20,
    include_text: bool = False,
    text: str = "",
    source_chunk_id: str = "",
    treatment: str = "cited",
) -> dict:
    """Resolve citation to chunks (lookup) OR extract citations from chunk (extract).

    Performance contract:
        Lookup p50 < 50ms, p99 < 120ms across ALL namespaces (statutes/regs/cases)
        Case lookups require volume/reporter/page; party-name-only returns not_found.

    Hard guarantees:
        1. USC query NEVER returns CFR chunks
        2. Case-namespace queries NEVER hit Chroma (case_index only)
        3. Garbage input → {source: "not_found", error: "unparseable"}
        4. Extract on same chunk N times produces edges only on 1st call
    """
    if mode == "direct_lookup":
        # Phase 5.1 — deterministic citation lookup, no vector search
        if not citation:
            return {"error": "direct_lookup mode requires 'citation'", "source": "not_found"}
        try:
            import sys as _sys
            if str(_eroot()) not in _sys.path:
                _sys.path.insert(0, str(_epkg()))
            import sys as _sys
            _EXT4_MCP_MODULES = str(Path.home() / "beezle_phase5/mcp_modules")
            if _EXT4_MCP_MODULES not in _sys.path:
                _sys.path.insert(0, _EXT4_MCP_MODULES)
            from direct_lookup_v1 import direct_lookup
        except ImportError as e:
            return {"error": f"direct_lookup_v1 import failed: {e}",
                    "source": "not_found"}
        result = direct_lookup(citation, include_chunks=include_text or True)
        if not result.get("found"):
            return {
                "mode": "direct_lookup",
                "found": False,
                "query": citation,
                "match_type": result.get("match_type", "no_match"),
                "source": "not_found",
                "latency_ms": result.get("lookup_ms"),
            }
        return {
            "mode": "direct_lookup",
            "found": True,
            "query": citation,
            "match_type": result["match_type"],
            "authority_id": result["authority_id"],
            "namespace": result["corpus_namespace"],
            "canonical": result["citation"],
            "kind": result["kind"],
            "tier": result["tier"],
            "chunk_ids": result.get("chunk_ids", []) or [],
            "chunk_count": result["chunk_count"],
            "current_validity": result["current_validity"],
            "overruled_flag": result["overruled_flag"],
            "blacklisted": result["blacklisted"],
            "rank_multiplier": result["rank_multiplier"],
            "status_warning": result.get("status_warning"),
            "status_message": result.get("status_message"),
            "treatment_summary": result.get("treatment_summary", {}),
            "source": "direct_lookup",
            "latency_ms": result.get("lookup_ms"),
        }

    if mode == "lookup":
        if not citation:
            return {"error": "lookup mode requires 'citation'", "source": "not_found"}
        return _lookup(citation, namespace_hint, limit, include_text)

    if mode == "extract":
        if not text or not source_chunk_id:
            return {"error": "extract mode requires 'text' and 'source_chunk_id'"}
        return _extract(text, source_chunk_id, treatment)

    return {"error": f"unknown mode: {mode!r} (use 'lookup', 'direct_lookup', or 'extract')"}


# ============================================================
# INTEGRATION INSTRUCTIONS (unchanged from v2)
# ============================================================
"""
TO INTEGRATE INTO <home>/beezle_mcp_server.py:

  Same as v2:
  1. cp resolve_citation.py <home>/resolve_citation.py
  2. Add `sys.path.insert(0, str(_epkg()))` and
     `from resolve_citation import resolve_citation_impl` to imports
  3. Wrap in @mcp.tool() — see v2 docstring for boilerplate
  4. Restart beezle_mcp_server

PREREQUISITE: case_index must be built first:
    python3 ~/beezle_phase2/build_case_index.py
"""
