#!/usr/bin/env python3
"""
backfill_namespace_v2.py — Phase 1 backfill, optimized for ext4

ARCHITECTURE:
  Phase A: Pre-collect all has_citation=true chunk IDs into a flat list
           (paginated where= query → ID list cache file)
  Phase B: Bulk-fetch chunks BY ID (8887/sec measured) + parse + write SQLite
           NO Chroma writes. Single SQLite transaction per 10k chunks.

WHY NO CHROMA WRITES:
  - Chroma updates are slow due to payload size (full metadata round-trip)
  - SQLite IS the authoritative citation index for Phase 2+
  - The 6,795 chunks already updated stay updated; new chunks just get
    indexed in SQLite without round-tripping through Chroma

USAGE:
  # Phase A only — build ID cache (fast, idempotent)
  python3 backfill_namespace_v2.py --collect-ids

  # Phase B only — parse + SQLite from cached IDs
  python3 backfill_namespace_v2.py --process

  # Both phases (default)
  python3 backfill_namespace_v2.py

  # Resume — skip chunks already in beezle_structure.db
  python3 backfill_namespace_v2.py --resume

  # Limit (for testing on subset)
  python3 backfill_namespace_v2.py --limit 10000

OUTPUTS:
  /tmp/has_citation_ids.txt              — cached IDs
  /tmp/phase1_v2_progress.log            — live progress log (flushed every batch)
  /tmp/phase1_v2_report.json             — final summary
"""
import argparse
import json
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

try:
    import chromadb
except ImportError:
    sys.exit("ERROR: chromadb not installed. Run from hermes-agent venv.")

# Import the local parser
sys.path.insert(0, str(Path(__file__).parent))
from citation_parser import parse, parse_all, path_parts, Citation


STRUCTURE_DB = Path("/home/{{USER_FIRST}}/.hermes/beezle_structure.db")
IDS_CACHE    = Path("/tmp/has_citation_ids.txt")
LOG_PATH     = Path("/tmp/phase1_v2_progress.log")
REPORT_PATH  = Path("/tmp/phase1_v2_report.json")


# ============================================================
# SQLite schema (must match v1)
# ============================================================
SCHEMA_DDL = """
PRAGMA journal_mode = DELETE;
PRAGMA synchronous  = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS authority (
    id                   TEXT PRIMARY KEY,
    citation             TEXT NOT NULL,
    corpus_namespace     TEXT NOT NULL,
    kind                 TEXT NOT NULL,
    tier                 INTEGER NOT NULL,
    branch               TEXT NOT NULL,
    track                TEXT NOT NULL DEFAULT 'na',
    court_level          TEXT,
    current_validity     TEXT NOT NULL DEFAULT 'unknown',
    validity_checked_at  TEXT,
    chroma_chunk_ids     TEXT,
    chunk_count          INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auth_namespace   ON authority(corpus_namespace);
CREATE INDEX IF NOT EXISTS idx_auth_tier_branch ON authority(tier, branch);
CREATE INDEX IF NOT EXISTS idx_auth_citation    ON authority(citation);

CREATE TABLE IF NOT EXISTS authority_path (
    authority_id  TEXT NOT NULL,
    depth         INTEGER NOT NULL,
    label         TEXT NOT NULL,
    code          TEXT NOT NULL,
    PRIMARY KEY (authority_id, depth),
    FOREIGN KEY (authority_id) REFERENCES authority(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_path_code ON authority_path(code, depth);

CREATE TABLE IF NOT EXISTS citation_edge (
    src_id        TEXT NOT NULL,
    dst_id        TEXT NOT NULL,
    treatment     TEXT NOT NULL,
    confidence    REAL NOT NULL DEFAULT 0.5,
    source_chunk  TEXT,
    extracted_by  TEXT,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (src_id, dst_id, treatment)
);
CREATE INDEX IF NOT EXISTS idx_edge_dst ON citation_edge(dst_id);
CREATE INDEX IF NOT EXISTS idx_edge_src ON citation_edge(src_id);

-- Per-chunk index: tracks which chunks we've already processed (for resume)
CREATE TABLE IF NOT EXISTS chunk_processed (
    chunk_id     TEXT PRIMARY KEY,
    authority_id TEXT,
    processed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunk_authority ON chunk_processed(authority_id);

CREATE TABLE IF NOT EXISTS phase_log (
    phase       TEXT NOT NULL,
    action      TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    stats       TEXT,
    PRIMARY KEY (phase, action, started_at)
);
"""


def log(msg):
    """Flush-immediately logger."""
    line = f"[{datetime.utcnow().isoformat()}Z] {msg}"
    print(line, flush=True)
    with LOG_PATH.open("a") as f:
        f.write(line + "\n")


def open_structure_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(STRUCTURE_DB))
    conn.execute("PRAGMA journal_mode=DELETE")
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "delete", f"FATAL: journal_mode is {mode!r}, not 'delete'"
    conn.executescript(SCHEMA_DDL)
    conn.commit()
    return conn


# ============================================================
# Phase A — Pre-collect all has_citation=true IDs
# ============================================================
def phase_a_collect_ids(chroma_host: str, chroma_port: int, collection: str,
                        batch_size: int = 10000) -> list[str]:
    """
    Paginate where=has_citation=True to dump all IDs to a flat file.
    This is the slow part of the old approach, but we only do it ONCE here
    and cache the results.

    On ext4, this runs at ~2000-5000/sec for filtered+id-only reads.
    Expected: ~3-5 minutes for ~500k chunks.
    """
    log(f"Phase A: connecting to chromadb at {chroma_host}:{chroma_port}")
    client = chromadb.HttpClient(host=chroma_host, port=chroma_port)
    coll = client.get_collection(collection)
    log(f"Phase A: collection has {coll.count():,} total docs")

    if IDS_CACHE.exists():
        log(f"Phase A: cache already exists at {IDS_CACHE} — reading existing IDs")
        with IDS_CACHE.open() as f:
            ids = [line.strip() for line in f if line.strip()]
        log(f"Phase A: {len(ids):,} IDs loaded from cache")
        return ids

    log(f"Phase A: starting ID collection (no cache yet)")
    all_ids: list[str] = []
    offset = 0
    t0 = time.time()

    for try_val in ("True", "true", True):
        try:
            test = coll.get(where={"has_citation": try_val}, limit=1, include=[])
            if test.get("ids"):
                has_citation_val = try_val
                break
        except Exception as e:
            log(f"  has_citation={try_val!r} failed: {e}")
    else:
        log("ERROR: no chunks matched has_citation in any form")
        return []
    log(f"Phase A: using has_citation={has_citation_val!r}")

    while True:
        t_batch = time.time()
        try:
            page = coll.get(
                where={"has_citation": has_citation_val},
                limit=batch_size,
                offset=offset,
                include=[],  # IDs only — fastest
            )
        except Exception as e:
            log(f"  page at offset={offset} FAILED: {e}")
            break
        ids = page.get("ids", [])
        all_ids.extend(ids)
        offset += batch_size
        rate = len(all_ids) / (time.time() - t0)
        log(f"  collected {len(all_ids):>7,} so far ({time.time()-t0:.1f}s elapsed, "
            f"rate={rate:.0f}/sec, last batch {time.time()-t_batch:.1f}s)")
        if len(ids) < batch_size:
            break

    log(f"Phase A: complete — {len(all_ids):,} IDs in {time.time()-t0:.1f}s "
        f"(avg {len(all_ids)/max(1, time.time()-t0):.0f}/sec)")
    with IDS_CACHE.open("w") as f:
        for cid in all_ids:
            f.write(cid + "\n")
    log(f"Phase A: cached to {IDS_CACHE}")
    return all_ids


# ============================================================
# Phase B — Bulk-fetch by ID + parse + SQLite write (no Chroma writes)
# ============================================================
def extract_citation_candidates(meta: dict) -> list[str]:
    """Same logic as v1, but doc text is not used (we don't include='documents')."""
    cands = []
    for k in ("citations_extracted", "citations", "known_statute_names",
              "known_statutes", "defined_terms", "section"):
        v = meta.get(k)
        if v and isinstance(v, str):
            parts = [p.strip() for p in re.split(r"[,;|]", v) if p.strip()]
            cands.extend(parts[:5])
    return cands


def parse_chunk(meta: dict) -> tuple[Citation | None, str]:
    cands = extract_citation_candidates(meta)
    for cand in cands:
        c = parse(cand)
        if c:
            return c, cand
    return None, ""


def phase_b_process(ids: list[str], chroma_host: str, chroma_port: int,
                    collection: str, batch_size: int = 5000,
                    transaction_size: int = 10000,
                    resume: bool = False,
                    limit: int | None = None) -> dict:
    """
    Bulk-fetch chunks by ID (8887/sec measured), parse, write SQLite.
    """
    if limit:
        ids = ids[:limit]
    log(f"Phase B: processing {len(ids):,} chunk IDs")

    client = chromadb.HttpClient(host=chroma_host, port=chroma_port)
    coll = client.get_collection(collection)
    sql_conn = open_structure_db()

    # Resume support
    already_done = set()
    if resume:
        log("Phase B: building resume skip-set from chunk_processed table...")
        cur = sql_conn.cursor()
        cur.execute("SELECT chunk_id FROM chunk_processed")
        already_done = {r[0] for r in cur.fetchall()}
        log(f"Phase B: {len(already_done):,} chunks already processed, will skip")
        ids = [cid for cid in ids if cid not in already_done]
        log(f"Phase B: {len(ids):,} chunks remaining")

    # Phase log entry
    phase_started = datetime.utcnow().isoformat() + "Z"
    sql_conn.execute("""
        INSERT INTO phase_log (phase, action, started_at, stats)
        VALUES ('phase1_v2', 'process', ?, ?)
    """, (phase_started, json.dumps({"input_ids": len(ids), "resume": resume})))
    sql_conn.commit()

    # Buffers for batched SQLite writes
    pending_authorities = {}    # auth_id -> (row_tuple, path_rows, chunk_ids[])
    pending_chunks = []         # (chunk_id, authority_id) pairs

    # Metrics
    report = {
        "started_at": phase_started,
        "chunks_total": 0,
        "parse_ok": 0,
        "parse_failed": 0,
        "namespace_counts": Counter(),
        "kind_counts": Counter(),
        "branch_counts": Counter(),
        "parser_method_counts": Counter(),
        "sqlite_authorities": 0,
        "sqlite_chunks_indexed": 0,
        "batches": 0,
    }

    t0 = time.time()

    def flush(sql_conn, pending_authorities, pending_chunks):
        """Write accumulated authorities + chunk_processed rows in one transaction."""
        now = datetime.utcnow().isoformat() + "Z"
        cur = sql_conn.cursor()
        try:
            cur.execute("BEGIN")
            for auth_id, data in pending_authorities.items():
                row = data["auth_row"]
                chunk_ids_csv = ",".join(data["chunk_ids"][:200])
                cur.execute("""
                    INSERT INTO authority
                        (id, citation, corpus_namespace, kind, tier, branch, track,
                         court_level, current_validity, validity_checked_at,
                         chroma_chunk_ids, chunk_count, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        chroma_chunk_ids = (
                            CASE
                                WHEN length(authority.chroma_chunk_ids) > 10000
                                  THEN authority.chroma_chunk_ids
                                ELSE authority.chroma_chunk_ids || ',' || excluded.chroma_chunk_ids
                            END
                        ),
                        chunk_count = authority.chunk_count + excluded.chunk_count,
                        updated_at  = excluded.updated_at
                """, (row[0], row[1], row[2], row[3], row[4], row[5], row[6],
                      row[7], row[8], row[9], chunk_ids_csv, len(data["chunk_ids"]),
                      now, now))
                for path_row in data["path_rows"]:
                    cur.execute("""
                        INSERT OR IGNORE INTO authority_path
                            (authority_id, depth, label, code)
                        VALUES (?, ?, ?, ?)
                    """, path_row)
            # Insert chunk_processed in bulk
            cur.executemany("""
                INSERT OR REPLACE INTO chunk_processed
                    (chunk_id, authority_id, processed_at)
                VALUES (?, ?, ?)
            """, [(cid, aid, now) for cid, aid in pending_chunks])
            cur.execute("COMMIT")
        except Exception as e:
            cur.execute("ROLLBACK")
            raise

    # ----- Main loop -----
    for batch_start in range(0, len(ids), batch_size):
        batch_ids = ids[batch_start:batch_start + batch_size]
        t_batch = time.time()

        # FAST PATH: get(ids=batch) — 8,887/sec measured on ext4
        try:
            page = coll.get(ids=batch_ids, include=["metadatas"])
        except Exception as e:
            log(f"  batch at {batch_start} FAILED: {e}")
            continue

        for cid, meta in zip(page["ids"], page["metadatas"]):
            report["chunks_total"] += 1
            c, matched = parse_chunk(meta or {})
            if not c:
                report["parse_failed"] += 1
                pending_chunks.append((cid, None))  # mark as processed even if no parse
                continue

            report["parse_ok"] += 1
            report["namespace_counts"][c.namespace] += 1
            report["kind_counts"][c.kind] += 1
            report["branch_counts"][c.branch] += 1
            report["parser_method_counts"][c.parser_method] += 1
            pp = path_parts(c)

            auth_id = f"{c.namespace}:{c.canonical}"
            if auth_id not in pending_authorities:
                pending_authorities[auth_id] = {
                    "auth_row": (
                        auth_id, c.canonical, c.namespace, c.kind, c.tier,
                        c.branch, c.track, c.court_level or None,
                        "unknown", None, cid, 1,
                        datetime.utcnow().isoformat() + "Z",
                        datetime.utcnow().isoformat() + "Z"
                    ),
                    "path_rows": [(auth_id, d, label, code) for d, label, code in pp],
                    "chunk_ids": [cid],
                }
            else:
                pending_authorities[auth_id]["chunk_ids"].append(cid)
            pending_chunks.append((cid, auth_id))

        report["batches"] += 1

        # Flush every transaction_size chunks
        if len(pending_chunks) >= transaction_size:
            flush(sql_conn, pending_authorities, pending_chunks)
            report["sqlite_authorities"] += len(pending_authorities)
            report["sqlite_chunks_indexed"] += len(pending_chunks)
            pending_authorities = {}
            pending_chunks = []

        # Progress log every ~10k chunks
        if report["chunks_total"] % 10000 < batch_size:
            rate = report["chunks_total"] / (time.time() - t0)
            eta_sec = (len(ids) - report["chunks_total"]) / max(1, rate)
            log(f"  {report['chunks_total']:>7,}/{len(ids):,} chunks "
                f"({100*report['chunks_total']/len(ids):.1f}%) "
                f"rate={rate:.0f}/sec  eta={eta_sec/60:.1f}min  "
                f"parsed={report['parse_ok']:,}/{report['parse_failed']:,}")

    # Final flush
    if pending_chunks:
        flush(sql_conn, pending_authorities, pending_chunks)
        report["sqlite_authorities"] += len(pending_authorities)
        report["sqlite_chunks_indexed"] += len(pending_chunks)

    elapsed = time.time() - t0
    report["duration_sec"] = elapsed
    report["rate_chunks_per_sec"] = report["chunks_total"] / max(1, elapsed)

    # Update phase_log
    sql_conn.execute("""
        UPDATE phase_log SET finished_at = ?, stats = ?
        WHERE phase = 'phase1_v2' AND started_at = ?
    """, (datetime.utcnow().isoformat() + "Z",
          json.dumps({
              "chunks_total": report["chunks_total"],
              "parse_ok": report["parse_ok"],
              "parse_failed": report["parse_failed"],
              "sqlite_authorities": report["sqlite_authorities"],
              "duration_sec": elapsed,
          }),
          phase_started))
    sql_conn.commit()
    sql_conn.close()

    return report


def write_report(report: dict, out_path: Path):
    """Persist report with counters serialized."""
    for k in ("namespace_counts", "kind_counts", "branch_counts", "parser_method_counts"):
        if isinstance(report.get(k), Counter):
            report[k] = dict(report[k])
    out_path.write_text(json.dumps(report, indent=2, default=str))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--collect-ids", action="store_true",
                    help="Only run Phase A (collect IDs to cache)")
    ap.add_argument("--process", action="store_true",
                    help="Only run Phase B (process cached IDs)")
    ap.add_argument("--resume", action="store_true",
                    help="Skip chunks already in chunk_processed table")
    ap.add_argument("--limit", type=int, default=None,
                    help="Process at most N chunks (testing)")
    ap.add_argument("--batch-size", type=int, default=5000,
                    help="Chroma fetch batch size (default 5000)")
    ap.add_argument("--collect-batch-size", type=int, default=10000,
                    help="Phase A pagination batch (default 10000)")
    ap.add_argument("--transaction-size", type=int, default=10000,
                    help="Chunks per SQLite commit (default 10000)")
    ap.add_argument("--chroma-host", default="localhost")
    ap.add_argument("--chroma-port", type=int, default=8100)
    ap.add_argument("--collection", default="legal_docs")
    args = ap.parse_args()

    # Truncate log file at start of run (keep history in phase_log table instead)
    LOG_PATH.write_text("")
    log("=" * 70)
    log("Phase 1 v2 — Optimized backfill (ext4)")
    log("=" * 70)

    do_a = args.collect_ids or not args.process
    do_b = args.process or not args.collect_ids

    ids = []
    if do_a:
        ids = phase_a_collect_ids(args.chroma_host, args.chroma_port,
                                  args.collection, args.collect_batch_size)
    if do_b:
        if not ids and IDS_CACHE.exists():
            log(f"Loading IDs from cache {IDS_CACHE}")
            with IDS_CACHE.open() as f:
                ids = [line.strip() for line in f if line.strip()]
        if not ids:
            log("FATAL: no IDs to process. Run with --collect-ids first.")
            sys.exit(1)

        log("=" * 70)
        log("Phase B starting")
        log("=" * 70)
        report = phase_b_process(
            ids, args.chroma_host, args.chroma_port, args.collection,
            batch_size=args.batch_size,
            transaction_size=args.transaction_size,
            resume=args.resume,
            limit=args.limit,
        )

        log("=" * 70)
        log("PHASE 1 v2 FINAL REPORT")
        log("=" * 70)
        log(f"  duration         : {report['duration_sec']:.1f}s "
            f"({report['duration_sec']/60:.1f}min)")
        log(f"  rate             : {report['rate_chunks_per_sec']:.0f} chunks/sec")
        log(f"  chunks scanned   : {report['chunks_total']:,}")
        log(f"  parse OK         : {report['parse_ok']:,} "
            f"({100*report['parse_ok']/max(1, report['chunks_total']):.1f}%)")
        log(f"  parse failed     : {report['parse_failed']:,}")
        log(f"  authorities      : {report['sqlite_authorities']:,}")
        log(f"  chunks indexed   : {report['sqlite_chunks_indexed']:,}")
        log("")
        log("  Top namespaces:")
        for ns, n in sorted(report["namespace_counts"].items(), key=lambda x: -x[1])[:15]:
            log(f"    {n:>7,}  {ns}")

        write_report(report, REPORT_PATH)
        log(f"  full report → {REPORT_PATH}")


if __name__ == "__main__":
    main()
