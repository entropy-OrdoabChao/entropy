#!/usr/bin/env python3
"""
phase1_verify.py — Phase 1 acceptance test

The ONE test that matters: does Phase 1 actually kill the Title 26
USC/CFR conflation bug?

Three checks:
  1. Resolve "26 USC § 6330(b)(2)"     → all chunks have corpus_namespace=usc_t26
  2. Resolve "26 CFR § 301.6330-1"     → all chunks have corpus_namespace=cfr_t26
  3. INTERSECTION of (1) and (2)        → MUST be empty

If any check fails, Phase 1 is broken regardless of parser self-test passing.
Run this AFTER backfill_namespace.py --write-chroma --write-sqlite.

Exit codes:
   0 = all checks pass
   1 = USC namespace contamination
   2 = CFR namespace contamination
   3 = overlap between USC and CFR result sets
   4 = backfill not yet run (no namespace data found)
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

try:
    import chromadb
except ImportError:
    sys.exit("ERROR: chromadb not installed.")

sys.path.insert(0, str(Path(__file__).parent))
from citation_parser import parse


STRUCTURE_DB = Path("/home/{{USER_FIRST}}/.hermes/beezle_structure.db")


def fetch_by_namespace(coll, namespace: str, limit: int = 200) -> dict:
    """Get up to N chunks for a corpus_namespace."""
    try:
        return coll.get(
            where={"corpus_namespace": namespace},
            limit=limit,
            include=["metadatas"],
        )
    except Exception as e:
        return {"ids": [], "metadatas": [], "_error": str(e)}


def check_namespace_purity(result: dict, expected_ns: str) -> tuple[int, int, list]:
    """Return (n_correct, n_wrong, examples_of_wrong)."""
    correct, wrong, wrong_examples = 0, 0, []
    for cid, m in zip(result.get("ids", []), result.get("metadatas", [])):
        actual_ns = m.get("corpus_namespace", "")
        if actual_ns == expected_ns:
            correct += 1
        else:
            wrong += 1
            if len(wrong_examples) < 5:
                wrong_examples.append({
                    "chunk_id": cid,
                    "expected": expected_ns,
                    "actual": actual_ns,
                    "citation_canonical": m.get("citation_canonical", "(missing)"),
                })
    return correct, wrong, wrong_examples


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--chroma-host", default="localhost")
    ap.add_argument("--chroma-port", type=int, default=8100)
    ap.add_argument("--collection", default="legal_docs")
    ap.add_argument("--limit", type=int, default=200,
                    help="Max chunks to fetch per namespace")
    ap.add_argument("--out", default="/tmp/phase1_verify_report.json")
    args = ap.parse_args()

    print("=" * 70)
    print("PHASE 1 ACCEPTANCE TEST — Title 26 USC vs CFR disambiguation")
    print("=" * 70)
    print()

    # 0. Verify parser still works on the canonical inputs
    print("[0] Parser sanity check ...")
    p_usc = parse("26 USC § 6330(b)(2)")
    p_cfr = parse("26 CFR § 301.6330-1")
    assert p_usc and p_usc.namespace == "usc_t26", f"parser broken for USC: {p_usc}"
    assert p_cfr and p_cfr.namespace == "cfr_t26", f"parser broken for CFR: {p_cfr}"
    print(f"    USC → {p_usc.namespace}  canonical={p_usc.canonical!r}")
    print(f"    CFR → {p_cfr.namespace}  canonical={p_cfr.canonical!r}")
    assert p_usc.namespace != p_cfr.namespace, "parser produces same namespace for USC and CFR!"
    print("    OK\n")

    # 1. Connect
    client = chromadb.HttpClient(host=args.chroma_host, port=args.chroma_port)
    coll = client.get_collection(args.collection)

    # 2. Check backfill ran at all
    print("[1] Checking that Phase 1 backfill has populated namespace metadata ...")
    sample = fetch_by_namespace(coll, "usc_t26", limit=1)
    if not sample["ids"]:
        sample = fetch_by_namespace(coll, "cfr_t26", limit=1)
    if not sample["ids"]:
        print("    NO chunks found with corpus_namespace=usc_t26 OR cfr_t26.")
        print("    Backfill not yet run? Exiting code 4.")
        Path(args.out).write_text(json.dumps({
            "result": "BACKFILL_NOT_RUN",
            "checks": [],
        }, indent=2))
        sys.exit(4)
    print("    OK — namespace metadata present\n")

    # 3. Fetch both sets
    print(f"[2] Fetching up to {args.limit} chunks with corpus_namespace=usc_t26 ...")
    usc_result = fetch_by_namespace(coll, "usc_t26", limit=args.limit)
    print(f"    found {len(usc_result['ids'])} chunks")
    print(f"[3] Fetching up to {args.limit} chunks with corpus_namespace=cfr_t26 ...")
    cfr_result = fetch_by_namespace(coll, "cfr_t26", limit=args.limit)
    print(f"    found {len(cfr_result['ids'])} chunks")
    print()

    # 4. Purity checks
    print("[4] Checking namespace purity ...")
    usc_correct, usc_wrong, usc_examples = check_namespace_purity(usc_result, "usc_t26")
    cfr_correct, cfr_wrong, cfr_examples = check_namespace_purity(cfr_result, "cfr_t26")
    print(f"    usc_t26  : {usc_correct} correct, {usc_wrong} wrong")
    print(f"    cfr_t26  : {cfr_correct} correct, {cfr_wrong} wrong")
    print()

    # 5. THE bug check — set intersection
    print("[5] Computing INTERSECTION of usc_t26 and cfr_t26 chunk sets ...")
    usc_ids = set(usc_result.get("ids", []))
    cfr_ids = set(cfr_result.get("ids", []))
    overlap = usc_ids & cfr_ids
    print(f"    |usc_t26|    = {len(usc_ids)}")
    print(f"    |cfr_t26|    = {len(cfr_ids)}")
    print(f"    intersection = {len(overlap)}    (MUST be 0)")
    if overlap:
        print(f"    OVERLAP CHUNKS: {list(overlap)[:10]}")
    print()

    # 6. Sample SQLite consistency
    sqlite_usc, sqlite_cfr = 0, 0
    sqlite_overlap_ids = []
    if STRUCTURE_DB.exists():
        print(f"[6] Cross-checking SQLite structural index ({STRUCTURE_DB}) ...")
        c = sqlite3.connect(str(STRUCTURE_DB))
        c.row_factory = sqlite3.Row
        r = c.execute(
            "SELECT COUNT(*) AS n FROM authority WHERE corpus_namespace='usc_t26'"
        ).fetchone()
        sqlite_usc = r["n"]
        r = c.execute(
            "SELECT COUNT(*) AS n FROM authority WHERE corpus_namespace='cfr_t26'"
        ).fetchone()
        sqlite_cfr = r["n"]
        print(f"    authority table usc_t26 rows: {sqlite_usc}")
        print(f"    authority table cfr_t26 rows: {sqlite_cfr}")
        # Are any authority IDs duplicated across namespaces?
        dup = c.execute("""
            SELECT a.id FROM authority a JOIN authority b
              ON a.citation = b.citation
             AND a.corpus_namespace != b.corpus_namespace
             AND a.corpus_namespace = 'usc_t26'
             AND b.corpus_namespace = 'cfr_t26'
        """).fetchall()
        sqlite_overlap_ids = [r["id"] for r in dup]
        print(f"    authority rows with SAME citation in BOTH namespaces: {len(sqlite_overlap_ids)}")
        c.close()
        print()
    else:
        print(f"[6] SQLite index not present — skipping cross-check\n")

    # 7. Verdict
    print("=" * 70)
    checks = []
    rc = 0
    if usc_wrong:
        checks.append(("USC purity", "FAIL",
                       f"{usc_wrong} chunks with corpus_namespace=usc_t26 had wrong namespace"))
        rc = 1
    else:
        checks.append(("USC purity", "PASS", f"{usc_correct} chunks all correctly namespaced"))

    if cfr_wrong:
        checks.append(("CFR purity", "FAIL",
                       f"{cfr_wrong} chunks with corpus_namespace=cfr_t26 had wrong namespace"))
        rc = rc or 2
    else:
        checks.append(("CFR purity", "PASS", f"{cfr_correct} chunks all correctly namespaced"))

    if overlap:
        checks.append(("USC/CFR chunk overlap", "FAIL",
                       f"{len(overlap)} chunks appeared in both result sets"))
        rc = rc or 3
    else:
        checks.append(("USC/CFR chunk overlap", "PASS",
                       "zero overlap — disambiguation working"))

    for name, result, detail in checks:
        marker = "✓" if result == "PASS" else "✗"
        print(f"  [{marker}] {name:30} {result:5} — {detail}")
    print()

    if rc == 0:
        print("  RESULT: ACCEPTANCE PASSED — Title 26 disambiguation bug is dead.")
    else:
        print(f"  RESULT: ACCEPTANCE FAILED (exit code {rc}). Phase 1 broken.")
    print("=" * 70)

    # Write JSON report
    Path(args.out).write_text(json.dumps({
        "result": "PASS" if rc == 0 else "FAIL",
        "exit_code": rc,
        "parser_sanity": {
            "usc": p_usc.to_dict() if p_usc else None,
            "cfr": p_cfr.to_dict() if p_cfr else None,
        },
        "chroma": {
            "usc_correct": usc_correct, "usc_wrong": usc_wrong,
            "cfr_correct": cfr_correct, "cfr_wrong": cfr_wrong,
            "usc_examples_wrong": usc_examples,
            "cfr_examples_wrong": cfr_examples,
            "overlap_count": len(overlap),
            "overlap_chunk_ids": list(overlap)[:20],
        },
        "sqlite": {
            "usc_authority_rows": sqlite_usc,
            "cfr_authority_rows": sqlite_cfr,
            "citation_overlap_count": len(sqlite_overlap_ids),
            "citation_overlap_ids": sqlite_overlap_ids[:20],
        },
        "checks": [
            {"name": n, "result": r, "detail": d} for n, r, d in checks
        ],
    }, indent=2))
    print(f"\nReport → {args.out}")
    sys.exit(rc)


if __name__ == "__main__":
    main()
