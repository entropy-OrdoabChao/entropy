#!/usr/bin/env python3
"""struct_family_apply.py — apply struct family proposals + orphan re-keys to the SANDBOX struct DB with an audit table.

Two inputs, one audit table, zero writes outside STRUCT_DB (default = the sandbox copy; refuses the live turbovec path):

  --proposals  output/research/struct_family_proposals.jsonl  (struct_family_propose.py; latest line per source wins)
        rule: confidence >= --min-conf (0.8) AND
              family is an EXISTING family          -> UPDATE chunk_struct SET family=? WHERE source=? AND family='unclassified'
              family is NEW:<name>                  -> applied as <name> ONLY if >= --new-min-sources (3) distinct sources agree
                                                       on that name at >= min-conf; otherwise left unclassified
  --orphans    output/research/orphan_source_map.jsonl         (orphan_source_recover.py; {chunk_id, source, method, ord?})
        rule: UPDATE chunk_struct SET source=?, family=family_of(source), title=..., ord=?(when known), ord_mode='recovered'
              WHERE chunk_id=? AND family='orphan'   (chunk_text.db is NOT touched — it stays the read-only store)

  struct_family_audit(source, old_family, new_family, confidence, why, ts, chunk_id, kind, method)   -- append-only
  Invariants checked after the write: COUNT(*) unchanged; no row with family NULL/''.

Run : cd ~/beezle/_sandbox_struct && env -u PYTHONPATH -u CUDA_VISIBLE_DEVICES /usr/bin/python3 \
          pipeline/struct_family_apply.py --orphans [--proposals] [--dry-run] [--min-conf 0.8] [--new-min-sources 3]
      (takes `cp -p struct_index.db struct_index.db.bak.<ts>` first unless --no-backup)
Verify: /usr/bin/python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" pipeline/struct_family_apply.py
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import argparse, json, os, shutil, sqlite3, sys, time
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from struct_index_build import family_of, FAM_INDEX, ORPHAN                             # noqa: E402

SANDBOX = HERE.parent
STRUCT_DB = Path(os.environ.get("STRUCT_DB", str(SANDBOX / "struct_index.db")))
OUT_DIR = Path(os.environ.get("STRUCT_RESEARCH_DIR", str(SANDBOX / "output" / "research")))
PROPOSALS = OUT_DIR / "struct_family_proposals.jsonl"
ORPHAN_MAP = OUT_DIR / "orphan_source_map.jsonl"
LIVE = (_eroot() / "corpus/struct_index.db").resolve()
EXPECTED_ROWS = 7_016_151


def load_jsonl(p: Path) -> list[dict]:
    out = []
    if p.exists():
        for ln in p.open():
            try: out.append(json.loads(ln))
            except json.JSONDecodeError: continue
    return out


def ensure_audit(con):
    con.execute("CREATE TABLE IF NOT EXISTS struct_family_audit(source TEXT, old_family TEXT, new_family TEXT, confidence REAL, "
                "why TEXT, ts TEXT, chunk_id TEXT, kind TEXT, method TEXT)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_sfa_source ON struct_family_audit(source)")


# proposal kinds -> builder kinds (the builder has one 'document' kind for every non-codified class)
BUILDER_KIND = {"regulation": "reg", "guidance": "document", "form": "document", "unclassified": "document"}


def load_merge_map(path: Path) -> dict[str, str]:
    """struct_family_merge.jsonl (struct_family_merge.py) -> {new_name: canonical_name}; canonical may be an EXISTING family."""
    out = {}
    for r in load_jsonl(path):
        if r.get("status", "").startswith("merge") and r.get("resolved_to"): out[r["from"]] = r["resolved_to"]
    return out


def rebuild_override_tables(con) -> dict:
    """The learning layer the builder consumes (orphan_exclusion_spec.md §last). Rebuilt from struct_family_audit in the same
    transaction as every apply/merge, so the tables are a pure function of the audit:
      source_family_override(source PK, family, kind, title, confidence, origin, ts)  <- latest source-level audit row per source
          whose new_family is a real family (not unclassified/orphan) — consulted by struct_index_build ONLY when family_of()
          returns 'unclassified' (deterministic table first; the LLM verdict fills only the hole it was asked about)
      source_chunk_override(chunk_id PK, source, ord, method)  <- the orphan re-key rows (A:*/B:* methods); the orphan build
          emits those chunks under the recovered source instead of re-filing them as orphans
    Also checks that chunk_struct agrees with the override (mismatches are reported, never silently fixed)."""
    con.execute("CREATE TABLE IF NOT EXISTS source_family_override(source TEXT PRIMARY KEY, family TEXT, kind TEXT, title TEXT, "
                "confidence REAL, origin TEXT, ts TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS source_chunk_override(chunk_id TEXT PRIMARY KEY, source TEXT, ord INTEGER, method TEXT)")
    con.execute("DELETE FROM source_family_override"); con.execute("DELETE FROM source_chunk_override")
    # latest source-level row per source (rowid order = append order)
    rows = con.execute("SELECT a.source, a.new_family, a.kind, a.confidence, a.method, a.ts FROM struct_family_audit a JOIN ("
                       "SELECT source, MAX(rowid) r FROM struct_family_audit WHERE chunk_id IS NULL GROUP BY source) m "
                       "ON a.rowid=m.r").fetchall()
    n_fam = 0
    for src, fam, kind, conf, method, ts in rows:
        if not fam or fam in ("unclassified", ORPHAN): continue
        if family_of(src)[0] != "unclassified": continue                        # deterministic table owns it: no override needed
        k = BUILDER_KIND.get(kind or "", kind) if fam not in FAM_INDEX else FAM_INDEX[fam][2]
        con.execute("INSERT OR REPLACE INTO source_family_override VALUES (?,?,?,?,?,?,?)", (src, fam, k or "document", "", conf, method, ts))
        n_fam += 1
    # orphan re-keys: ord from the recover map when known (chunk_struct.ord of a recovered row is that value; else NULL)
    ords = {}
    for r in load_jsonl(ORPHAN_MAP):
        if r.get("ord") is not None: ords[r["chunk_id"]] = int(r["ord"])
    n_chunk = 0
    for cid, src, method in con.execute("SELECT chunk_id, source, method FROM struct_family_audit WHERE chunk_id IS NOT NULL "
                                        "AND (method LIKE 'A:%' OR method LIKE 'B:%')").fetchall():
        con.execute("INSERT OR REPLACE INTO source_chunk_override VALUES (?,?,?,?)", (cid, src, ords.get(cid), method))
        n_chunk += 1
    mism = con.execute("SELECT COUNT(*) FROM source_family_override o WHERE NOT EXISTS (SELECT 1 FROM chunk_struct c WHERE c.source=o.source "
                       "AND c.family=o.family)").fetchone()[0]
    return {"source_family_override": n_fam, "source_chunk_override": n_chunk, "override_sources_not_in_chunk_struct_family": mism}


def counts(con) -> dict:
    r = con.execute("SELECT COUNT(*), COUNT(DISTINCT source) FROM chunk_struct WHERE family='unclassified'").fetchone()
    o = con.execute("SELECT COUNT(*) FROM chunk_struct WHERE family='orphan'").fetchone()[0]
    return {"unclassified_rows": r[0], "unclassified_sources": r[1], "orphan_rows": o,
            "total_rows": con.execute("SELECT COUNT(*) FROM chunk_struct").fetchone()[0]}


def apply_proposals(con, min_conf: float, new_min: int, dry: bool, merge_map: dict[str, str] | None = None) -> dict:
    """merge_map (struct_family_merge.jsonl): NEW:<name> is applied under its canonical name; when the canonical name is an
    EXISTING family the proposal is applied as that family. Quorum counts on canonical names."""
    merge_map = merge_map or {}
    latest = {}
    for p in load_jsonl(PROPOSALS): latest[p["source"]] = p
    hi = [p for p in latest.values() if p["confidence"] >= min_conf and p["family"] != "unclassified"]
    for p in hi:
        if p["family"].startswith("NEW:"):
            canon = merge_map.get(p["family"][4:], p["family"][4:])
            p["family"] = canon if canon in FAM_INDEX else "NEW:" + canon
    new_votes = Counter(p["family"] for p in hi if p["family"].startswith("NEW:"))
    accepted_new = {f for f, c in new_votes.items() if c >= new_min}
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    stat = Counter(); fam_rows = Counter(); skipped_new = Counter()
    for p in sorted(hi, key=lambda p: p["source"]):
        fam = p["family"]
        if fam.startswith("NEW:"):
            if fam not in accepted_new: skipped_new[fam] += 1; stat["new_below_quorum"] += 1; continue
            fam = fam[4:]
        elif fam not in FAM_INDEX or fam in ("unclassified", ORPHAN):
            stat["bad_family"] += 1; continue
        n = con.execute("SELECT COUNT(*) FROM chunk_struct WHERE source=? AND family='unclassified'", (p["source"],)).fetchone()[0]
        if not n: stat["already_done"] += 1; continue
        if not dry:
            con.execute("UPDATE chunk_struct SET family=? WHERE source=? AND family='unclassified'", (fam, p["source"]))
            con.execute("INSERT INTO struct_family_audit VALUES (?,?,?,?,?,?,?,?,?)",
                        (p["source"], "unclassified", fam, p["confidence"], p["why"], ts, None, p.get("kind"), "llm_proposal:" + p.get("model", "")))
        stat["sources_applied"] += 1; stat["rows_applied"] += n; fam_rows[fam] += n
    return {"stat": dict(stat), "proposed_sources": len(latest), "high_conf": len(hi), "accepted_new": sorted(accepted_new),
            "new_below_quorum": dict(skipped_new.most_common(40)), "rows_by_family": dict(fam_rows.most_common())}


def apply_orphans(con, dry: bool) -> dict:
    rows = load_jsonl(ORPHAN_MAP)
    best = {}
    for r in rows:                                                                   # A (hash equality) beats B (verbatim twin)
        cur = best.get(r["chunk_id"])
        if cur is None or r.get("confidence", 0) > cur.get("confidence", 0): best[r["chunk_id"]] = r
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    stat = Counter(); fam_rows = Counter()
    for cid, r in best.items():
        row = con.execute("SELECT family FROM chunk_struct WHERE chunk_id=?", (cid,)).fetchone()
        if not row: stat["chunk_not_in_struct"] += 1; continue
        if row[0] != ORPHAN: stat["not_orphan_anymore"] += 1; continue
        fam, title = family_of(r["source"])
        if not dry:
            if r.get("ord") is not None:
                con.execute("UPDATE chunk_struct SET source=?, family=?, title=?, ord=?, ord_mode='recovered' WHERE chunk_id=?",
                            (r["source"], fam, title, int(r["ord"]), cid))
            else:
                con.execute("UPDATE chunk_struct SET source=?, family=?, title=?, ord_mode='recovered' WHERE chunk_id=?",
                            (r["source"], fam, title, cid))
            con.execute("INSERT INTO struct_family_audit VALUES (?,?,?,?,?,?,?,?,?)",
                        (r["source"], ORPHAN, fam, r.get("confidence", 1.0), f"orphan re-key {r['method']}", ts, cid, None, r["method"]))
        stat["chunks_applied"] += 1; fam_rows[fam] += 1
    return {"stat": dict(stat), "mapped_chunks": len(best), "rows_by_family": dict(fam_rows.most_common())}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--proposals", action="store_true"); ap.add_argument("--orphans", action="store_true")
    ap.add_argument("--min-conf", type=float, default=0.8); ap.add_argument("--new-min-sources", type=int, default=3)
    ap.add_argument("--dry-run", action="store_true"); ap.add_argument("--no-backup", action="store_true")
    ap.add_argument("--merge-map", default=str(OUT_DIR / "struct_family_merge.jsonl"), help="struct_family_merge.jsonl ('' = none)")
    ap.add_argument("--rebuild-overrides", action="store_true", help="only rebuild source_family_override / source_chunk_override from the audit")
    a = ap.parse_args()
    if STRUCT_DB.resolve() == LIVE: sys.exit("REFUSED: STRUCT_DB is the live turbovec index; point STRUCT_DB at a sandbox copy")
    if a.rebuild_overrides:
        con = sqlite3.connect(str(STRUCT_DB), timeout=120); ensure_audit(con)
        ov = rebuild_override_tables(con); con.commit(); print("override tables:", json.dumps(ov)); return
    if not (a.proposals or a.orphans): sys.exit("nothing to do: pass --proposals and/or --orphans")
    merge_map = load_merge_map(Path(a.merge_map)) if a.merge_map else {}
    if merge_map: print(f"merge map: {len(merge_map)} NEW names -> canonical", flush=True)
    if not a.dry_run and not a.no_backup:
        bak = STRUCT_DB.with_name(STRUCT_DB.name + f".bak.{time.strftime('%Y%m%d%H%M%S')}")
        shutil.copy2(STRUCT_DB, bak); print(f"backup -> {bak}", flush=True)
    con = sqlite3.connect(str(STRUCT_DB), timeout=120)
    con.execute("PRAGMA journal_mode=WAL")
    ensure_audit(con)
    before = counts(con)
    print("before:", json.dumps(before), flush=True)
    out = {"before": before, "dry_run": a.dry_run}
    if a.orphans: out["orphans"] = apply_orphans(con, a.dry_run); print("orphans:", json.dumps(out["orphans"])[:1500], flush=True)
    if a.proposals: out["proposals"] = apply_proposals(con, a.min_conf, a.new_min_sources, a.dry_run, merge_map); print("proposals:", json.dumps(out["proposals"])[:3000], flush=True)
    if not a.dry_run:
        out["override_tables"] = rebuild_override_tables(con); print("override tables:", json.dumps(out["override_tables"]), flush=True)
        con.commit()
    after = counts(con)
    bad = con.execute("SELECT COUNT(*) FROM chunk_struct WHERE family IS NULL OR family=''").fetchone()[0]
    out["after"] = after; out["invariants"] = {"rows_unchanged": after["total_rows"] == before["total_rows"] == EXPECTED_ROWS,
                                               "total_rows": after["total_rows"], "rows_without_family": bad}
    print("after:", json.dumps(after)); print("invariants:", json.dumps(out["invariants"]))
    (OUT_DIR / f"struct_family_apply.{time.strftime('%Y%m%d%H%M%S')}{'.dry' if a.dry_run else ''}.json").write_text(json.dumps(out, indent=1))
    if not out["invariants"]["rows_unchanged"] or bad: sys.exit("INVARIANT VIOLATION — inspect the backup")


if __name__ == "__main__":
    main()
