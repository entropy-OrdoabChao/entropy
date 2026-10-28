#!/usr/bin/env python3
"""
precedent-cache
SQLite-backed hot cache of verified binding precedent.
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB_PATH = Path.home() / ".hermes" / "data" / "cache" / "precedent.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def conn():
    c = sqlite3.connect(str(DB_PATH))
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""
        CREATE TABLE IF NOT EXISTS precedent (
          id INTEGER PRIMARY KEY,
          jurisdiction TEXT NOT NULL,
          issue TEXT NOT NULL,
          case_name TEXT NOT NULL,
          citation TEXT NOT NULL,
          pinpoint TEXT,
          holding TEXT NOT NULL,
          binding_level TEXT,
          verified_at TEXT NOT NULL,
          verified_count INTEGER DEFAULT 1,
          last_used TEXT
        )
    """)
    c.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS precedent_fts
        USING fts5(issue, holding, content='precedent', content_rowid='id')
    """)
    c.execute("""
        CREATE TRIGGER IF NOT EXISTS precedent_ai AFTER INSERT ON precedent BEGIN
          INSERT INTO precedent_fts(rowid, issue, holding) VALUES (new.id, new.issue, new.holding);
        END
    """)
    c.execute("""
        CREATE TRIGGER IF NOT EXISTS precedent_ad AFTER DELETE ON precedent BEGIN
          INSERT INTO precedent_fts(precedent_fts, rowid, issue, holding) VALUES('delete', old.id, old.issue, old.holding);
        END
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_juris ON precedent(jurisdiction)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_used ON precedent(last_used)")
    return c


def cmd_lookup(args):
    c = conn()
    now = datetime.utcnow().isoformat()
    # FTS5 query — sanitize quotes
    query = args.issue.replace('"', '""')
    rows = c.execute("""
        SELECT p.id, p.case_name, p.citation, p.pinpoint, p.holding, p.binding_level, p.verified_count
        FROM precedent_fts
        JOIN precedent p ON p.id = precedent_fts.rowid
        WHERE precedent_fts MATCH ? AND p.jurisdiction = ?
        ORDER BY p.verified_count DESC, rank
        LIMIT 10
    """, (query, args.jurisdiction)).fetchall()

    if not rows:
        print(json.dumps({"hit": False, "results": []}))
        return

    ids = [r[0] for r in rows]
    c.executemany("UPDATE precedent SET last_used=? WHERE id=?", [(now, i) for i in ids])
    c.commit()

    results = [{
        "case": r[1], "citation": r[2], "pinpoint": r[3],
        "holding": r[4], "binding_level": r[5], "verified_count": r[6],
    } for r in rows]
    print(json.dumps({"hit": True, "count": len(results), "results": results}, indent=2))


def cmd_promote(args):
    c = conn()
    now = datetime.utcnow().isoformat()
    # Dedupe by (jurisdiction, citation)
    existing = c.execute(
        "SELECT id, verified_count FROM precedent WHERE jurisdiction=? AND citation=?",
        (args.jurisdiction, args.citation),
    ).fetchone()
    if existing:
        c.execute(
            "UPDATE precedent SET verified_count=?, last_used=? WHERE id=?",
            (existing[1] + 1, now, existing[0]),
        )
        c.commit()
        print(f"INCREMENTED: id={existing[0]} count={existing[1]+1}")
        return

    cur = c.execute("""
        INSERT INTO precedent (jurisdiction, issue, case_name, citation, pinpoint, holding, binding_level, verified_at, last_used)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (args.jurisdiction, args.issue, args.case, args.citation, args.pinpoint or "",
          args.holding, args.binding_level or "", now, now))
    c.commit()
    print(f"PROMOTED: id={cur.lastrowid}")


def cmd_list(args):
    c = conn()
    rows = c.execute(
        "SELECT case_name, citation, binding_level, verified_count, last_used FROM precedent WHERE jurisdiction=? ORDER BY verified_count DESC LIMIT ?",
        (args.jurisdiction, args.limit),
    ).fetchall()
    for r in rows:
        print(f"  [{r[2]}] {r[0]}, {r[1]} (used={r[3]}, last={r[4]})")


def cmd_stats(args):
    c = conn()
    total = c.execute("SELECT COUNT(*) FROM precedent").fetchone()[0]
    by_juris = c.execute("SELECT jurisdiction, COUNT(*) FROM precedent GROUP BY jurisdiction").fetchall()
    by_level = c.execute("SELECT binding_level, COUNT(*) FROM precedent GROUP BY binding_level").fetchall()
    hot = c.execute("SELECT COUNT(*) FROM precedent WHERE verified_count >= 3").fetchone()[0]
    print(f"Total entries: {total}")
    print(f"Hot entries (verified_count >= 3): {hot}")
    print("By jurisdiction:")
    for j, n in by_juris:
        print(f"  {j}: {n}")
    print("By binding level:")
    for l, n in by_level:
        print(f"  {l}: {n}")


def cmd_wipe(args):
    if not args.confirm:
        sys.exit("Refusing to wipe without --confirm")
    DB_PATH.unlink(missing_ok=True)
    print("WIPED")


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("lookup")
    pl.add_argument("--jurisdiction", required=True)
    pl.add_argument("--issue", required=True)

    pp = sub.add_parser("promote")
    pp.add_argument("--jurisdiction", required=True)
    pp.add_argument("--issue", required=True)
    pp.add_argument("--case", required=True)
    pp.add_argument("--citation", required=True)
    pp.add_argument("--pinpoint")
    pp.add_argument("--holding", required=True)
    pp.add_argument("--binding-level")

    pli = sub.add_parser("list")
    pli.add_argument("--jurisdiction", required=True)
    pli.add_argument("--limit", type=int, default=50)

    sub.add_parser("stats")

    pw = sub.add_parser("wipe")
    pw.add_argument("--confirm", action="store_true")

    args = p.parse_args()
    {"lookup": cmd_lookup, "promote": cmd_promote, "list": cmd_list, "stats": cmd_stats, "wipe": cmd_wipe}[args.cmd](args)


if __name__ == "__main__":
    main()
