#!/usr/bin/env python3
"""
backfill_namespace.py — Phase 1

Targeted backfill of Chroma metadata + SQLite structural index using
citation_parser.py. Solves the Title 26 USC/CFR conflation bug.

DESIGN: NO IMPLICIT WRITES.
  --dry-run is the default. Reports only.
  --write-chroma  must be passed explicitly to mutate Chroma metadata.
  --write-sqlite  must be passed explicitly to write beezle_structure.db.

Backup MUST be run first: bash phase1_backup.sh

USAGE:
  # 1. Sample dry run (default — just inspect)
  python3 backfill_namespace.py --limit 1000

  # 2. Sample commit (after reviewing dry-run output)
  python3 backfill_namespace.py --limit 1000 --write-chroma --write-sqlite

  # 3. Full corpus commit (only after sample sign-off)
  python3 backfill_namespace.py --full --write-chroma --write-sqlite

OUTPUT:
  /tmp/phase1_backfill_report.json
  Stdout: parse success rate, namespace distribution, top failures,
          5-chunk spot check, hierarchy depth distribution.
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


# ============================================================
# SQLite — structural index DDL
# ============================================================
SCHEMA_DDL = """
PRAGMA journal_mode = DELETE;
PRAGMA synchronous  = NORMAL;
PRAGMA foreign_keys = ON;

-- Authority: every statute/reg/case is a node
CREATE TABLE IF NOT EXISTS authority (
    id                   TEXT PRIMARY KEY,                -- f"{namespace}:{canonical}"
    citation             TEXT NOT NULL,                   -- canonical form
    corpus_namespace     TEXT NOT NULL,                   -- usc_t26 | cfr_t26 | case | pa_*
    kind                 TEXT NOT NULL,                   -- statute|regulation|case|pa_statute|pa_rule
    tier                 INTEGER NOT NULL,
    branch               TEXT NOT NULL,                   -- federal|state|local|tribal
    track                TEXT NOT NULL DEFAULT 'na',      -- art_i|art_iii|na
    court_level          TEXT,                            -- scotus|circuit|district|...
    current_validity     TEXT NOT NULL DEFAULT 'unknown', -- good_law|overturned|...
    validity_checked_at  TEXT,
    chroma_chunk_ids     TEXT,                            -- comma-separated chunk IDs
    chunk_count          INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auth_namespace   ON authority(corpus_namespace);
CREATE INDEX IF NOT EXISTS idx_auth_tier_branch ON authority(tier, branch);
CREATE INDEX IF NOT EXISTS idx_auth_citation    ON authority(citation);

-- Hierarchical path: Title → Subtitle → ... → Subclause
CREATE TABLE IF NOT EXISTS authority_path (
    authority_id  TEXT NOT NULL,
    depth         INTEGER NOT NULL,
    label         TEXT NOT NULL,
    code          TEXT NOT NULL,
    PRIMARY KEY (authority_id, depth),
    FOREIGN KEY (authority_id) REFERENCES authority(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_path_code ON authority_path(code, depth);

-- Citation graph (populated in Phase 3, table created now for completeness)
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

-- Phase metadata
CREATE TABLE IF NOT EXISTS phase_log (
    phase       TEXT NOT NULL,
    action      TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    stats       TEXT,
    PRIMARY KEY (phase, action, started_at)
);
"""


def open_structure_db() -> sqlite3.Connection:
    """Open beezle_structure.db with mandatory DELETE journal mode (WSL2 9p safety)."""
    conn = sqlite3.connect(str(STRUCTURE_DB))
    # Hard-assert journal mode
    conn.execute("PRAGMA journal_mode=DELETE")
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "delete", (
        f"FATAL: journal_mode is {mode!r}, not 'delete'. "
        f"WSL2 9p requires DELETE. Refusing to write."
    )
    conn.executescript(SCHEMA_DDL)
    conn.commit()
    return conn


# ============================================================
# Source-of-citation strategy
# ============================================================
def extract_citation_candidates(meta: dict, doc: str) -> list[str]:
    """
    Return ordered list of citation text candidates to try.
    Prefers metadata fields (already curated) over document scan.
    """
    cands = []
    # Existing metadata fields, in priority order
    for k in ("citations_extracted", "citations", "known_statute_names",
              "known_statutes", "defined_terms", "section"):
        v = meta.get(k)
        if v and isinstance(v, str):
            # Split CSV-style values; some entries are comma-separated lists
            parts = [p.strip() for p in re.split(r"[,;|]", v) if p.strip()]
            cands.extend(parts[:5])  # cap per field
    # Document text scan as fallback — first 500 chars
    if doc:
        cands.append(doc[:500])
    return cands


def parse_first_match(meta: dict, doc: str) -> tuple[Citation | None, str, list[str]]:
    """
    Try each candidate; return (Citation, parser_method, raw_candidates_tried).
    """
    cands = extract_citation_candidates(meta, doc)
    for cand in cands:
        c = parse(cand)
        if c:
            return c, c.parser_method, cands
    return None, "", cands


# ============================================================
# Main backfill
# ============================================================
def fetch_sample(coll, limit: int, full: bool, batch_size: int = 1000):
    """
    Yield batches of (ids, metas, docs). With --full, walks entire corpus.
    """
    if full:
        # Iterate all chunks where has_citation
        offset = 0
        total_seen = 0
        # First try filter; if Chroma rejects, fall back to scan-and-filter
        try:
            while True:
                page = coll.get(
                    where={"has_citation": "True"},
                    limit=batch_size,
                    offset=offset,
                    include=["metadatas", "documents"],
                )
                if not page["ids"]:
                    break
                yield page["ids"], page["metadatas"], page["documents"]
                total_seen += len(page["ids"])
                offset += batch_size
                if total_seen >= 5_000_000:  # safety cap
                    break
        except Exception as e:
            print(f"  filter failed ({e}); scanning all chunks instead")
            total = coll.count()
            for off in range(0, total, batch_size):
                page = coll.get(limit=batch_size, offset=off,
                                include=["metadatas", "documents"])
                ids, metas, docs = [], [], []
                for cid, m, d in zip(page["ids"], page["metadatas"], page["documents"]):
                    if str(m.get("has_citation", "")).lower() == "true":
                        ids.append(cid); metas.append(m); docs.append(d)
                if ids:
                    yield ids, metas, docs
    else:
        # Sample mode — single page
        for val in ("True", "true", True):
            try:
                page = coll.get(
                    where={"has_citation": val},
                    limit=limit,
                    include=["metadatas", "documents"],
                )
                if page["ids"]:
                    yield page["ids"], page["metadatas"], page["documents"]
                    return
            except Exception:
                continue
        # Fallback: random sample, filter client-side
        page = coll.get(limit=limit * 3, include=["metadatas", "documents"])
        ids, metas, docs = [], [], []
        for cid, m, d in zip(page["ids"], page["metadatas"], page["documents"]):
            if str(m.get("has_citation", "")).lower() == "true":
                ids.append(cid); metas.append(m); docs.append(d)
                if len(ids) >= limit:
                    break
        yield ids, metas, docs


def process_batch(ids, metas, docs, report, want_chroma_update, want_sqlite_write,
                  coll, sql_conn, max_failure_samples=20, max_spotchecks=5):
    """Parse + optionally write."""
    chroma_updates = []
    sqlite_authorities = {}   # id → (citation_row, path_rows)

    for cid, meta, doc in zip(ids, metas, docs):
        report["chunks_total"] += 1
        c, method, cands = parse_first_match(meta, doc)
        if not c:
            report["parse_failed"] += 1
            if len(report["failure_samples"]) < max_failure_samples:
                report["failure_samples"].append({
                    "chunk_id": cid,
                    "tried_candidates": cands[:3],
                    "doc_preview": (doc or "")[:200],
                })
            continue

        report["parse_ok"] += 1
        report["namespace_counts"][c.namespace] += 1
        report["kind_counts"][c.kind] += 1
        report["branch_counts"][c.branch] += 1
        report["parser_method_counts"][method] += 1
        pp = path_parts(c)
        report["hierarchy_depth"][len(pp)] += 1

        # Spot checks — first 5
        if len(report["spotchecks"]) < max_spotchecks:
            report["spotchecks"].append({
                "chunk_id": cid,
                "raw_candidate": cands[0] if cands else "",
                "parsed": c.to_dict(),
                "path_parts": pp,
            })

        # Build Chroma metadata patch (additive — never deletes existing fields)
        now = datetime.utcnow().isoformat() + "Z"
        new_meta = dict(meta)
        new_meta.update({
            "corpus_namespace":   c.namespace,
            "citation_canonical": c.canonical,
            "citation_parts":     f"title={c.title or ''};code={c.code or ''};"
                                  f"section={c.section or ''};subsec={c.subsec};"
                                  f"para={c.para};subpara={c.subpara};clause={c.clause};"
                                  f"volume={c.volume or ''};reporter={c.reporter or ''};"
                                  f"page={c.page or ''}",
            "tier":               c.tier,
            "branch":             c.branch,
            "track":              c.track,
            "court_level":        c.court_level or "",
            "parse_confidence":   c.confidence,
            "parse_method":       method,
            "phase1_backfilled":  "true",
            "phase1_backfill_at": now,
        })
        chroma_updates.append((cid, new_meta))

        # Build SQLite rows
        auth_id = f"{c.namespace}:{c.canonical}"
        if auth_id not in sqlite_authorities:
            sqlite_authorities[auth_id] = {
                "auth_row": (
                    auth_id, c.canonical, c.namespace, c.kind, c.tier, c.branch,
                    c.track, c.court_level or None, "unknown", None,
                    cid, 1, now, now
                ),
                "path_rows": [(auth_id, d, label, code) for d, label, code in pp],
                "chunk_ids": [cid],
            }
        else:
            sqlite_authorities[auth_id]["chunk_ids"].append(cid)

    # --- Writes (conditional) ---
    if want_chroma_update and chroma_updates:
        # Chroma update is atomic per call; do batches of 500
        for i in range(0, len(chroma_updates), 500):
            sub = chroma_updates[i:i+500]
            cids = [x[0] for x in sub]
            ms   = [x[1] for x in sub]
            coll.update(ids=cids, metadatas=ms)
        report["chroma_updated"] += len(chroma_updates)

    if want_sqlite_write and sqlite_authorities:
        cur = sql_conn.cursor()
        for auth_id, data in sqlite_authorities.items():
            row = data["auth_row"]
            chunk_ids = data["chunk_ids"]
            chunk_ids_csv = ",".join(chunk_ids[:200])  # cap to keep row small
            cur.execute("""
                INSERT INTO authority
                    (id, citation, corpus_namespace, kind, tier, branch, track,
                     court_level, current_validity, validity_checked_at,
                     chroma_chunk_ids, chunk_count, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    chroma_chunk_ids = (
                        CASE
                            WHEN length(authority.chroma_chunk_ids) > 10000 THEN authority.chroma_chunk_ids
                            ELSE authority.chroma_chunk_ids || ',' || excluded.chroma_chunk_ids
                        END
                    ),
                    chunk_count = authority.chunk_count + excluded.chunk_count,
                    updated_at  = excluded.updated_at
            """, (row[0], row[1], row[2], row[3], row[4], row[5], row[6],
                  row[7], row[8], row[9], chunk_ids_csv, len(chunk_ids),
                  row[12], row[13]))
            for path_row in data["path_rows"]:
                cur.execute("""
                    INSERT OR IGNORE INTO authority_path
                        (authority_id, depth, label, code)
                    VALUES (?, ?, ?, ?)
                """, path_row)
            report["sqlite_authorities"] += 1
        sql_conn.commit()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", default=False,
                    help="(default behavior) parse + report only, write nothing")
    ap.add_argument("--write-chroma", action="store_true", default=False,
                    help="EXPLICIT opt-in: mutate Chroma metadata")
    ap.add_argument("--write-sqlite", action="store_true", default=False,
                    help="EXPLICIT opt-in: write beezle_structure.db")
    ap.add_argument("--limit", type=int, default=1000,
                    help="Sample size (ignored if --full)")
    ap.add_argument("--full", action="store_true",
                    help="Process entire has_citation=true corpus")
    ap.add_argument("--chroma-host", default="localhost")
    ap.add_argument("--chroma-port", type=int, default=8100)
    ap.add_argument("--collection", default="legal_docs")
    ap.add_argument("--out", default="/tmp/phase1_backfill_report.json")
    args = ap.parse_args()

    # Determine write intent — default is dry-run unless explicit flags
    want_chroma = args.write_chroma
    want_sqlite = args.write_sqlite
    is_dry      = not (want_chroma or want_sqlite)
    if is_dry:
        print("DRY RUN — no writes (default). Pass --write-chroma and/or "
              "--write-sqlite to enable.")

    # Connect to Chroma
    print(f"[phase1] connecting to chromadb at {args.chroma_host}:{args.chroma_port} ...")
    client = chromadb.HttpClient(host=args.chroma_host, port=args.chroma_port)
    coll = client.get_collection(args.collection)
    pre_count = coll.count()
    print(f"[phase1] collection has {pre_count:,} docs")

    # Open structural DB (creates schema if missing, even for dry run — schema is read by phase1_verify)
    sql_conn = open_structure_db()
    print(f"[phase1] structural DB ready at {STRUCTURE_DB} (journal_mode=DELETE verified)")

    # Initialize report
    report = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "dry_run": is_dry,
        "write_chroma": want_chroma,
        "write_sqlite": want_sqlite,
        "limit": args.limit,
        "full": args.full,
        "chunks_total": 0,
        "parse_ok": 0,
        "parse_failed": 0,
        "namespace_counts": Counter(),
        "kind_counts": Counter(),
        "branch_counts": Counter(),
        "parser_method_counts": Counter(),
        "hierarchy_depth": Counter(),
        "failure_samples": [],
        "spotchecks": [],
        "chroma_updated": 0,
        "sqlite_authorities": 0,
    }

    # Phase log entry
    phase_started = datetime.utcnow().isoformat() + "Z"
    sql_conn.execute("""
        INSERT INTO phase_log (phase, action, started_at, stats)
        VALUES (?, ?, ?, ?)
    """, ("phase1_backfill",
          "dry_run" if is_dry else f"write_chroma={want_chroma}_sqlite={want_sqlite}",
          phase_started,
          json.dumps({"limit": args.limit, "full": args.full})))
    sql_conn.commit()

    # Process
    t0 = time.time()
    batches_done = 0
    for ids, metas, docs in fetch_sample(coll, args.limit, args.full):
        process_batch(ids, metas, docs, report,
                      want_chroma_update=want_chroma,
                      want_sqlite_write=want_sqlite,
                      coll=coll, sql_conn=sql_conn)
        batches_done += 1
        if batches_done % 10 == 0:
            print(f"  ... {report['chunks_total']:,} chunks scanned, "
                  f"{report['parse_ok']:,} parsed OK, "
                  f"{report['parse_failed']:,} failed")
    elapsed = time.time() - t0

    # Update phase log
    sql_conn.execute("""
        UPDATE phase_log SET finished_at = ?, stats = ?
        WHERE phase = 'phase1_backfill' AND started_at = ?
    """, (datetime.utcnow().isoformat() + "Z",
          json.dumps({
              "chunks_total": report["chunks_total"],
              "parse_ok": report["parse_ok"],
              "parse_failed": report["parse_failed"],
              "chroma_updated": report["chroma_updated"],
              "sqlite_authorities": report["sqlite_authorities"],
          }),
          phase_started))
    sql_conn.commit()
    sql_conn.close()

    # ============ REPORT ============
    total = max(1, report["chunks_total"])
    rate = report["parse_ok"] * 100.0 / total
    print()
    print("=" * 70)
    print("PHASE 1 BACKFILL REPORT")
    print("=" * 70)
    print(f"  duration            : {elapsed:.1f}s")
    print(f"  chunks scanned      : {report['chunks_total']:,}")
    print(f"  parse OK            : {report['parse_ok']:,}")
    print(f"  parse FAILED        : {report['parse_failed']:,}")
    print(f"  parse success rate  : {rate:.1f}%   (target: ≥90%)")
    print(f"  chroma updates      : {report['chroma_updated']:,}")
    print(f"  sqlite authorities  : {report['sqlite_authorities']:,}")
    print()
    print(f"  Top namespaces:")
    for ns, n in report["namespace_counts"].most_common(15):
        print(f"    {n:>6,}  {ns}")
    print()
    print(f"  By kind:           {dict(report['kind_counts'])}")
    print(f"  By branch:         {dict(report['branch_counts'])}")
    print(f"  By parser_method:  {dict(report['parser_method_counts'])}")
    print()
    print(f"  Hierarchy depth distribution:")
    for d in sorted(report["hierarchy_depth"]):
        bar = "█" * min(40, report["hierarchy_depth"][d] * 40 // max(report["hierarchy_depth"].values()))
        print(f"    depth={d}: {report['hierarchy_depth'][d]:>5,}  {bar}")
    print()
    print(f"  SPOT CHECKS (5 random parsed citations):")
    for sc in report["spotchecks"]:
        print(f"    chunk={sc['chunk_id']}")
        print(f"      raw      : {sc['raw_candidate'][:80]!r}")
        print(f"      parsed   : namespace={sc['parsed']['namespace']:14} "
              f"canonical={sc['parsed']['canonical']!r}")
        print(f"      tier={sc['parsed']['tier']} branch={sc['parsed']['branch']} "
              f"track={sc['parsed']['track']} court_level={sc['parsed']['court_level']!r}")
        print(f"      path     : {sc['path_parts']}")
    print()
    if report["failure_samples"]:
        print(f"  TOP {len(report['failure_samples'])} PARSE FAILURES:")
        for fs in report["failure_samples"]:
            print(f"    chunk={fs['chunk_id']}")
            print(f"      tried: {[c[:60] for c in fs['tried_candidates']]}")
            print(f"      doc  : {fs['doc_preview'][:80]!r}")
    print()
    print("=" * 70)

    # Persist JSON report
    report["namespace_counts"]    = dict(report["namespace_counts"])
    report["kind_counts"]         = dict(report["kind_counts"])
    report["branch_counts"]       = dict(report["branch_counts"])
    report["parser_method_counts"]= dict(report["parser_method_counts"])
    report["hierarchy_depth"]     = dict(report["hierarchy_depth"])
    Path(args.out).write_text(json.dumps(report, indent=2, default=str))
    print(f"Report → {args.out}")

    if is_dry:
        print()
        print("This was a DRY RUN. To commit the sample:")
        print(f"  python3 {sys.argv[0]} --limit {args.limit} --write-chroma --write-sqlite")
    elif not args.full:
        print()
        print("Sample committed. Verify with phase1_verify.py, then run full corpus:")
        print(f"  python3 {sys.argv[0]} --full --write-chroma --write-sqlite")


if __name__ == "__main__":
    main()
