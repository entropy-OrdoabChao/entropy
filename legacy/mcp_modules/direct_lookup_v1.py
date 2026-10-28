#!/usr/bin/env python3
"""
direct_lookup_v1.py — deterministic citation lookup.

Purpose: when Hermes (or any caller) has a citation string and wants
the authoritative record, this function returns it WITHOUT vector search.

This is the "I know what I'm looking for" retrieval path. It is:
  - Deterministic (same input → same output, always)
  - Fast (< 5ms p99)
  - Hallucination-free (only returns what's in the DB)
  - Direction-aware (returns the overruled flag set by Phase 4.5)

Citation normalization handles common variations:
  "133 U.S. 333"  ↔ "133 U. S. 333" (Westlaw space style)
  "42 U.S.C. § 1983" ↔ "42 USC § 1983"
  "12 C.F.R. § 24.1" ↔ "12 CFR § 24.1"
  "26 USC §6330" ↔ "26 USC § 6330" (space after §)
  Pa.R.C.P. 234.2 ↔ Pa. R. C. P. 234.2

Returns:
{
    "found": bool,
    "authority_id": str | None,
    "citation": str,                  # canonical form from DB
    "match_type": "exact" | "normalized" | "fuzzy" | None,
    "kind": "case" | "statute" | "regulation" | "pa_statute" | "pa_rule",
    "tier": int,
    "current_validity": str,
    "overruled_flag": bool,           # from rank_meta if available
    "rank_multiplier": float | None,
    "blacklisted": bool,
    "chunk_ids": list[str],           # for chunk fetch downstream
    "chunk_count": int,
    "treatment_summary": {            # what happened to this authority
        "followed_count": int,
        "relied_upon_count": int,
        "criticized_count": int,
        "overruled_by": list[str],    # citations that overruled this one
        "superseded_by": list[str],
        "abrogated_by": list[str],
    },
    "lookup_ms": float,
}

Drop-in module for beezle_mcp_server.py. Standalone CLI for testing.

Usage:
    python3 direct_lookup_v1.py "133 U.S. 333"
    python3 direct_lookup_v1.py "42 USC § 1983"
    python3 direct_lookup_v1.py --test    # run all built-in tests
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Optional

# ============================================================
# Config
# ============================================================
DB_PATH = "/home/{{USER_FIRST}}/.hermes/beezle_structure.db"
RANK_META_PATH = "/home/{{USER_FIRST}}/turbovec/rank_meta.db"


# ============================================================
# Citation normalization
# ============================================================
def normalize_citation(raw: str) -> list[str]:
    """Generate a list of canonical variants for fuzzy matching.

    Returns variants ordered from "most likely canonical" to "looser".
    Strips Bluebook spaces, U.S./USC ambiguity, optional § spaces.
    """
    if not raw:
        return []

    cit = raw.strip()
    variants = [cit]

    # Whitespace normalization
    cit_collapsed = re.sub(r"\s+", " ", cit)
    if cit_collapsed != cit:
        variants.append(cit_collapsed)

    # Westlaw "U. S." → "U.S."
    if " U. S. " in cit_collapsed:
        variants.append(cit_collapsed.replace(" U. S. ", " U.S. "))
    if " U.S. " in cit_collapsed:
        variants.append(cit_collapsed.replace(" U.S. ", " U. S. "))

    # USC / U.S.C.
    if "U.S.C." in cit_collapsed:
        variants.append(cit_collapsed.replace("U.S.C.", "USC"))
    if " USC " in cit_collapsed and "U.S.C." not in cit_collapsed:
        variants.append(cit_collapsed.replace(" USC ", " U.S.C. "))

    # CFR / C.F.R.
    if "C.F.R." in cit_collapsed:
        variants.append(cit_collapsed.replace("C.F.R.", "CFR"))
    if " CFR " in cit_collapsed and "C.F.R." not in cit_collapsed:
        variants.append(cit_collapsed.replace(" CFR ", " C.F.R. "))

    # § with no following space
    if "§" in cit_collapsed:
        variants.append(re.sub(r"§\s*(\S)", r"§ \1", cit_collapsed))
        variants.append(re.sub(r"§\s+(\S)", r"§\1", cit_collapsed))

    # Federal Reporter "F.2d" / "F. 2d"
    if "F.2d" in cit_collapsed:
        variants.append(cit_collapsed.replace("F.2d", "F. 2d"))
    if "F. 2d" in cit_collapsed:
        variants.append(cit_collapsed.replace("F. 2d", "F.2d"))
    if "F.3d" in cit_collapsed:
        variants.append(cit_collapsed.replace("F.3d", "F. 3d"))
    if "F. 3d" in cit_collapsed:
        variants.append(cit_collapsed.replace("F. 3d", "F.3d"))

    # Pa.C.S. variants
    if "Pa.C.S." in cit_collapsed:
        variants.append(cit_collapsed.replace("Pa.C.S.", "Pa. C. S."))

    # Pa.R.C.P. variants
    if "Pa.R.C.P." in cit_collapsed:
        variants.append(cit_collapsed.replace("Pa.R.C.P.", "Pa. R. C. P."))

    # Dedup while preserving order
    seen = set()
    ordered_unique = []
    for v in variants:
        if v not in seen:
            seen.add(v)
            ordered_unique.append(v)
    return ordered_unique


# ============================================================
# Database lookup
# ============================================================
def lookup_authority(citation_query: str,
                     db: sqlite3.Connection) -> tuple[Optional[dict], str]:
    """Find authority by citation. Returns (row_dict, match_type)."""
    variants = normalize_citation(citation_query)

    # Try each variant as exact match
    for i, variant in enumerate(variants):
        row = db.execute("""
            SELECT id, citation, corpus_namespace, kind, tier,
                   current_validity, chunk_count, chroma_chunk_ids
            FROM authority
            WHERE citation = ?
        """, (variant,)).fetchone()
        if row:
            match_type = "exact" if i == 0 else "normalized"
            return dict(zip(
                ["id", "citation", "corpus_namespace", "kind", "tier",
                 "current_validity", "chunk_count", "chroma_chunk_ids"],
                row,
            )), match_type

    # Fuzzy: case-insensitive LIKE on whitespace-collapsed
    # Last resort. Only matches if there's exactly one result.
    collapsed = re.sub(r"\s+", " ", citation_query.strip())
    if not collapsed:
        return None, "no_match"
    rows = db.execute("""
        SELECT id, citation, corpus_namespace, kind, tier,
               current_validity, chunk_count, chroma_chunk_ids
        FROM authority
        WHERE LOWER(REPLACE(REPLACE(citation, '  ', ' '), ' ', '')) =
              LOWER(REPLACE(?, ' ', ''))
        LIMIT 2
    """, (collapsed,)).fetchall()
    if len(rows) == 1:
        return dict(zip(
            ["id", "citation", "corpus_namespace", "kind", "tier",
             "current_validity", "chunk_count", "chroma_chunk_ids"],
            rows[0],
        )), "fuzzy"
    return None, "no_match"


def lookup_rank_meta(authority_id: str,
                     rm: sqlite3.Connection) -> Optional[dict]:
    """Get rank_meta record. None if not in current build."""
    row = rm.execute("""
        SELECT overruled_flag, blacklisted, rank_multiplier,
               followed_count, relied_upon_count, criticized_count,
               tier_weight, validity_factor
        FROM authority_rank
        WHERE authority_id = ?
          AND build_id = (SELECT build_id FROM build_meta WHERE is_current = 1)
    """, (authority_id,)).fetchone()
    if not row:
        return None
    return dict(zip(
        ["overruled_flag", "blacklisted", "rank_multiplier",
         "followed_count", "relied_upon_count", "criticized_count",
         "tier_weight", "validity_factor"],
        row,
    ))


def lookup_treatment_summary(authority_id: str,
                              db: sqlite3.Connection) -> dict:
    """Get treatment relationships involving this authority."""
    # What overruled/superseded/abrogated this authority?
    rows = db.execute("""
        SELECT h.signal_phrase, h.confidence, h.dst_id, h.effect_target,
               (SELECT citation FROM authority WHERE id = h.dst_id) AS dst_cit,
               (SELECT citation FROM authority WHERE id = h.src_id) AS src_cit
        FROM edge_treatment_history h
        WHERE (h.src_id = ? OR h.dst_id = ?)
          AND h.treatment IN ('overruled', 'superseded', 'abrogated', 'limited', 'modified')
          AND h.confidence >= 0.90
    """, (authority_id, authority_id)).fetchall()

    overruled_by = []
    superseded_by = []
    abrogated_by = []
    for signal, conf, dst_id, effect_target, dst_cit, src_cit in rows:
        # effect_target='src' means src is the affected side
        # So if our authority_id is src, the OTHER side (dst) did the action
        # If our authority_id is dst, then src did the action
        is_affected = False
        other = None
        if effect_target == "src":
            # src is affected, dst is the actor
            is_affected = True
            other = dst_cit
        elif effect_target == "dst":
            # dst is affected, src is the actor
            is_affected = True
            other = src_cit

        if not is_affected or not other:
            continue

        if "overruled" in signal:
            overruled_by.append(other)
        elif "superseded" in signal:
            superseded_by.append(other)
        elif "abrogated" in signal:
            abrogated_by.append(other)

    # Inbound treatment counts (followed/relied/criticized as dst)
    counts = {}
    for treat in ("followed", "relied_upon", "criticized"):
        n = db.execute("""
            SELECT COUNT(*) FROM edge_treatment_history
            WHERE dst_id = ? AND treatment = ?
        """, (authority_id, treat)).fetchone()[0]
        counts[f"{treat}_count"] = n

    return {
        "overruled_by": sorted(set(overruled_by)),
        "superseded_by": sorted(set(superseded_by)),
        "abrogated_by": sorted(set(abrogated_by)),
        **counts,
    }


# ============================================================
# Public API
# ============================================================
def direct_lookup(citation: str, include_chunks: bool = True) -> dict:
    """Direct lookup by citation string. Returns full authority record.

    Args:
        citation: citation string (any common variant)
        include_chunks: if True, includes chroma chunk_ids for downstream fetch

    Returns: result dict (see module docstring)
    """
    t0 = time.time()

    try:
        db = sqlite3.connect(DB_PATH, timeout=10)
        db.execute("PRAGMA query_only = 1")
    except Exception as e:
        return {
            "found": False,
            "error": f"db_open_failed: {e}",
            "lookup_ms": (time.time() - t0) * 1000,
        }

    try:
        rm = sqlite3.connect(RANK_META_PATH, timeout=10)
        rm.execute("PRAGMA query_only = 1")
    except Exception:
        rm = None  # rank_meta is optional

    auth, match_type = lookup_authority(citation, db)

    if auth is None:
        db.close()
        if rm:
            rm.close()
        return {
            "found": False,
            "query": citation,
            "match_type": "no_match",
            "lookup_ms": (time.time() - t0) * 1000,
        }

    # Pull rank metadata if available
    rank = None
    if rm is not None:
        try:
            rank = lookup_rank_meta(auth["id"], rm)
        except Exception:
            rank = None
        rm.close()

    # Treatment summary
    treatment = lookup_treatment_summary(auth["id"], db)

    # Chunk ids if requested
    chunk_ids = []
    if include_chunks and auth["chroma_chunk_ids"]:
        seen = set()
        for cid in auth["chroma_chunk_ids"].split(","):
            cid = cid.strip()
            if cid and cid not in seen:
                seen.add(cid)
                chunk_ids.append(cid)

    db.close()

    result = {
        "found": True,
        "query": citation,
        "match_type": match_type,
        "authority_id": auth["id"],
        "citation": auth["citation"],
        "kind": auth["kind"],
        "corpus_namespace": auth["corpus_namespace"],
        "tier": auth["tier"],
        "current_validity": auth["current_validity"],
        "chunk_count": auth["chunk_count"],
        "chunk_ids": chunk_ids if include_chunks else None,
        "overruled_flag": bool(rank["overruled_flag"]) if rank else False,
        "blacklisted": bool(rank["blacklisted"]) if rank else False,
        "rank_multiplier": rank["rank_multiplier"] if rank else None,
        "treatment_summary": treatment,
        "lookup_ms": round((time.time() - t0) * 1000, 2),
    }

    # Human-readable status for Hermes
    if result["overruled_flag"]:
        result["status_warning"] = "STALE_LAW"
        if treatment["overruled_by"]:
            result["status_message"] = (
                f"This authority was overruled by: "
                f"{', '.join(treatment['overruled_by'])}"
            )
        elif treatment["superseded_by"]:
            result["status_message"] = (
                f"This authority was superseded by: "
                f"{', '.join(treatment['superseded_by'])}"
            )
        elif treatment["abrogated_by"]:
            result["status_message"] = (
                f"This authority was abrogated by: "
                f"{', '.join(treatment['abrogated_by'])}"
            )
        else:
            result["status_message"] = "This authority is flagged as stale/overruled."
    elif result["blacklisted"]:
        result["status_warning"] = "BLACKLISTED"
        result["status_message"] = "This authority is in the system blacklist."
    else:
        result["status_warning"] = None
        result["status_message"] = "Good law (subject to validity_checked_at)."

    return result


# ============================================================
# Self-tests
# ============================================================
def run_self_tests():
    """Built-in tests covering normalization, overruled lookup, etc."""
    tests = [
        # (query, expected found, expected overruled_flag, description)
        ("133 U.S. 333",       True,  True,  "Sparf — known overruled"),
        ("133 U. S. 333",      True,  True,  "Sparf with Westlaw spacing"),
        ("42 USC § 1983",      True,  False, "Civil rights statute — good law"),
        ("42 U.S.C. § 1983",   True,  False, "Same statute, alt formatting"),
        ("12 CFR § 0",      True,  False, "Regulation — good law"),
        ("18 Pa.C.S. § 2903",  True,  False, "PA statute"),
        ("Pa.R.C.P. .",    True,  False, "PA rule"),
        ("999 U.S. 99999",     False, False, "Nonexistent — should not be found"),
    ]

    print("=" * 70)
    print("direct_lookup_v1 SELF-TESTS")
    print("=" * 70)

    passed = 0
    failed = []
    for query, want_found, want_overruled, desc in tests:
        result = direct_lookup(query, include_chunks=False)
        found = result.get("found", False)
        overruled = result.get("overruled_flag", False)

        ok = (found == want_found) and (overruled == want_overruled or not want_found)
        status = "✓" if ok else "✗"
        print(f"  {status} '{query}'  {desc}")
        print(f"      found={found} (want {want_found})  "
              f"overruled={overruled} (want {want_overruled if want_found else 'n/a'})  "
              f"match={result.get('match_type', 'n/a')}  "
              f"lookup_ms={result.get('lookup_ms', 0):.2f}")
        if found:
            print(f"      citation={result.get('citation')}  "
                  f"kind={result.get('kind')}  "
                  f"tier={result.get('tier')}")
            if overruled:
                msg = result.get("status_message", "")
                print(f"      status: {msg}")
        if ok:
            passed += 1
        else:
            failed.append((query, desc))

    print()
    print(f"Self-tests: {passed}/{len(tests)} passed")
    if failed:
        print("Failures:")
        for q, d in failed:
            print(f"  ✗ '{q}' ({d})")
        return False
    return True


# ============================================================
# CLI
# ============================================================
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("citation", nargs="?", help="Citation to look up")
    ap.add_argument("--test", action="store_true", help="Run self-tests")
    ap.add_argument("--no-chunks", action="store_true",
                    help="Omit chunk_ids from output (faster, smaller)")
    args = ap.parse_args()

    if args.test:
        return 0 if run_self_tests() else 1

    if not args.citation:
        ap.print_help()
        return 2

    result = direct_lookup(args.citation, include_chunks=not args.no_chunks)
    print(json.dumps(result, indent=2))
    return 0 if result.get("found") else 3


if __name__ == "__main__":
    sys.exit(main())
