#!/usr/bin/env python3
"""
query.py v0.2 — runtime lookup API for the Judicial Accountability Registry.

Bundle 2 additions:
  - category filter (statutory, public_pressure, criminal_referral, etc.)
  - federal_judge union expansion (auto-includes all federal_*_judge subtypes)
  - accountability_options() now groups by category in output
  - bond_status filter (skip repealed bonds in active recovery queries)
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import sys
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

_THIS = Path(__file__).resolve()
if str(_THIS.parent) not in sys.path:
    sys.path.insert(0, str(_THIS.parent))

from schema import (  # noqa: E402
    Registry, Forum, Bond, Writ, load_and_validate
)

DEFAULT_REGISTRY = (_eroot() / "pipeline/accountability_registry/registry.yaml")

# Union expansion: querying 'federal_judge' also matches the specific subtypes
FEDERAL_JUDGE_UNION = {
    "federal_judge",
    "federal_district_judge", "federal_circuit_judge",
    "federal_magistrate_judge", "federal_bankruptcy_judge",
    "federal_senior_judge", "scotus_justice",
    "court_of_federal_claims_judge", "tax_court_judge",
}


@lru_cache(maxsize=1)
def load_registry(path: Optional[str] = None) -> Registry:
    p = Path(path) if path else DEFAULT_REGISTRY
    if not p.exists():
        raise FileNotFoundError(f"registry not found: {p}")
    return load_and_validate(p)


def _expand_target(target_role: str) -> set:
    """If target is 'federal_judge', also match specific subtypes (and vice versa)."""
    if target_role == "federal_judge":
        return FEDERAL_JUDGE_UNION
    if target_role in FEDERAL_JUDGE_UNION:
        # Specific subtype — also match generic 'federal_judge' for backward compat
        return {target_role, "federal_judge"}
    return {target_role}


def forums_for_target(target_role: str,
                      jurisdiction: Optional[str] = None,
                      geography: Optional[str] = None,
                      category: Optional[str] = None,
                      exclude_categories: Optional[List[str]] = None) -> List[Forum]:
    """
    Return forums that can act against the given target role.

    target_role: a SCOPE_TARGETS value, e.g. 'federal_district_judge'
    jurisdiction: optional filter, e.g. 'federal' or 'pa_state'
    geography: optional state abbreviation, e.g. 'PA'
    category: optional filter, e.g. 'statutory' to exclude public_pressure
    exclude_categories: optional list to exclude, e.g. ['limitation_note']
    """
    reg = load_registry()
    target_set = _expand_target(target_role)
    out = []
    for f in reg.forums:
        if not (target_set & set(f.scope)):
            continue
        if jurisdiction and f.jurisdiction != jurisdiction:
            continue
        if geography and f.geography and geography not in f.geography:
            continue
        if category and f.category != category:
            continue
        if exclude_categories and f.category in exclude_categories:
            continue
        out.append(f)
    return out


def forum_by_id(forum_id: str) -> Optional[Forum]:
    reg = load_registry()
    for f in reg.forums:
        if f.id == forum_id:
            return f
    return None


def all_forums(jurisdiction: Optional[str] = None,
               category: Optional[str] = None) -> List[Forum]:
    reg = load_registry()
    out = list(reg.forums)
    if jurisdiction:
        out = [f for f in out if f.jurisdiction == jurisdiction]
    if category:
        out = [f for f in out if f.category == category]
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Bonds
# ─────────────────────────────────────────────────────────────────────────────

def bonds_for_role(bonded_role: str,
                   jurisdiction: Optional[str] = None,
                   active_only: bool = True) -> List[Bond]:
    reg = load_registry()
    out = []
    for b in reg.bonds:
        if b.bonded_role != bonded_role:
            continue
        if jurisdiction and b.jurisdiction != jurisdiction:
            continue
        if active_only and b.bond_status != "active":
            continue
        out.append(b)
    return out


def bond_by_id(bond_id: str) -> Optional[Bond]:
    reg = load_registry()
    for b in reg.bonds:
        if b.id == bond_id:
            return b
    return None


def all_bonds(jurisdiction: Optional[str] = None,
              status: Optional[str] = None) -> List[Bond]:
    reg = load_registry()
    out = list(reg.bonds)
    if jurisdiction:
        out = [b for b in out if b.jurisdiction == jurisdiction]
    if status:
        out = [b for b in out if b.bond_status == status]
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Writs
# ─────────────────────────────────────────────────────────────────────────────

def writ_by_id(writ_id: str) -> Optional[Writ]:
    reg = load_registry()
    for w in reg.writs:
        if w.id == writ_id:
            return w
    return None


def writs_for_forum(forum: str) -> List[Writ]:
    reg = load_registry()
    return [w for w in reg.writs if w.forum == forum]


def all_writs() -> List[Writ]:
    return list(load_registry().writs)


# ─────────────────────────────────────────────────────────────────────────────
# Combined "what can I do?" query — now category-grouped
# ─────────────────────────────────────────────────────────────────────────────

def accountability_options(target_role: str,
                           jurisdiction: Optional[str] = None,
                           geography: Optional[str] = None,
                           include_public_pressure: bool = True) -> Dict[str, Any]:
    """
    For a target role + jurisdiction, return all available paths grouped by category:
      - statutory complaints (judicial council, bar, OPR)
      - criminal referral paths (18 USC §§ 241/242)
      - in-case recusal paths (§§ 144/455)
      - impeachment intake
      - public-pressure NGOs (optional)
      - limitation notes (no-forum-exists documentation)
      - bonds for financial recovery
      - writs for judicial escalation
    """
    forums = forums_for_target(target_role, jurisdiction, geography)

    grouped: Dict[str, List[Dict]] = {}
    for f in forums:
        if f.category == "public_pressure" and not include_public_pressure:
            continue
        grouped.setdefault(f.category, []).append(asdict(f))

    # Bonds
    bonds = bonds_for_role(target_role, jurisdiction, active_only=True)
    repealed_bonds = bonds_for_role(target_role, jurisdiction, active_only=False)
    repealed_bonds = [b for b in repealed_bonds if b.bond_status != "active"]

    # Writs
    writ_pool = []
    if jurisdiction == "federal":
        writ_pool.extend(writs_for_forum("scotus"))
        writ_pool.extend(writs_for_forum("3d_circuit"))
    elif jurisdiction == "pa_state":
        writ_pool.extend(writs_for_forum("pa_supreme"))
        writ_pool.extend(writs_for_forum("pa_superior"))
        writ_pool.extend(writs_for_forum("pa_commonwealth"))

    return {
        "target_role": target_role,
        "jurisdiction": jurisdiction,
        "geography": geography,
        "forums_by_category": grouped,
        "active_bonds": [asdict(b) for b in bonds],
        "repealed_bonds": [asdict(b) for b in repealed_bonds],
        "writs": [asdict(w) for w in writ_pool],
        "summary": {
            "total_forums": sum(len(v) for v in grouped.values()),
            "categories": sorted(grouped.keys()),
            "active_bond_count": len(bonds),
            "repealed_bond_count": len(repealed_bonds),
            "writ_count": len(writ_pool),
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# ChromaDB-first source lookup (unchanged from v0.1)
# ─────────────────────────────────────────────────────────────────────────────

def chroma_lookup(query: str, n_results: int = 5,
                  collection_name: str = "legal_docs") -> List[Dict[str, Any]]:
    try:
        import chromadb
    except ImportError:
        return []
    try:
        client = chromadb.HttpClient(host="localhost", port=8100)
        coll = client.get_collection(collection_name)
        res = coll.query(query_texts=[query], n_results=n_results)
        out = []
        docs = res.get("documents", [[]])[0]
        metas = res.get("metadatas", [[]])[0]
        dists = res.get("distances", [[]])[0]
        for i, d in enumerate(docs):
            out.append({
                "text": d,
                "metadata": metas[i] if i < len(metas) else {},
                "distance": dists[i] if i < len(dists) else None,
            })
        return out
    except Exception as e:
        print(f"⚠ chroma_lookup failed: {e}", file=sys.stderr)
        return []


def scrape_or_lookup_source(query: str,
                            primary_url: Optional[str] = None,
                            min_chroma_results: int = 3) -> Dict[str, Any]:
    chroma_hits = chroma_lookup(query)
    if len(chroma_hits) >= min_chroma_results:
        return {"source": "chromadb", "results": chroma_hits, "fetch_needed": False}
    return {
        "source": "web_needed", "fetch_url": primary_url,
        "chroma_partial": chroma_hits, "fetch_needed": True,
    }


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    if len(sys.argv) < 2:
        print("usage: query.py <command> [args...]")
        print("commands:")
        print("  stats")
        print("  forums <target_role> [jurisdiction] [geography] [category]")
        print("  bonds <bonded_role> [jurisdiction]")
        print("  writ <writ_id>")
        print("  options <target_role> <jurisdiction> [geography]")
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "stats":
        from schema import stats as _stats
        print(json.dumps(_stats(load_registry()), indent=2))
    elif cmd == "forums":
        role = sys.argv[2]
        jx = sys.argv[3] if len(sys.argv) > 3 else None
        geo = sys.argv[4] if len(sys.argv) > 4 else None
        cat = sys.argv[5] if len(sys.argv) > 5 else None
        result = [asdict(f) for f in forums_for_target(role, jx, geo, cat)]
        print(json.dumps(result, indent=2))
    elif cmd == "bonds":
        role = sys.argv[2]
        jx = sys.argv[3] if len(sys.argv) > 3 else None
        result = [asdict(b) for b in bonds_for_role(role, jx)]
        print(json.dumps(result, indent=2))
    elif cmd == "writ":
        wid = sys.argv[2]
        w = writ_by_id(wid)
        print(json.dumps(asdict(w) if w else None, indent=2))
    elif cmd == "options":
        role = sys.argv[2]
        jx = sys.argv[3]
        geo = sys.argv[4] if len(sys.argv) > 4 else None
        print(json.dumps(accountability_options(role, jx, geo), indent=2))
    else:
        print(f"unknown command: {cmd}")
        sys.exit(1)
