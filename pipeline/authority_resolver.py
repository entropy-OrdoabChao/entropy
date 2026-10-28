"""
pipeline/authority_resolver.py
================================
Citation → primary_source node resolver.

Builds a JSON index over obsidian_sync/primary_sources/ (~34k rule nodes)
so that a citation like "15 U.S.C. § 1681" can be resolved to all rule nodes
that cite or interpret it, with O(1) lookup.

PUBLIC API
----------
build_index(force: bool = False) -> dict
    One-time indexer. Scans every .md file under primary_sources/,
    extracts citations from body text using 20-pattern regex set,
    and writes JSON indices to obsidian_sync/primary_sources/_index/.

resolve_citation(citation: str, jurisdiction: Optional[str] = None,
                 limit: int = 10) -> list[dict]
    Citation lookup. Returns list of matching rule nodes with metadata:
      [{path, title, jurisdiction, authority_level, snippet, citation_count}]

list_jurisdiction(jurisdiction: str) -> list[str]
    All rule node paths for a jurisdiction.

list_authority_level(level: str) -> list[str]
    All rule node paths for a given authority_level frontmatter value.

index_stats() -> dict
    Returns summary of current index.

INDEX FILES
-----------
obsidian_sync/primary_sources/_index/
  ├── by_citation.json       {"15 U.S.C. § 1681": ["federal/path1.md", ...]}
  ├── by_jurisdiction.json   {"federal": [paths], "PA": [paths], ...}
  ├── by_authority_level.json {"BINDING_SCOTUS": [paths], ...}
  ├── node_metadata.json     {"federal/path1.md": {title, citations, ...}}
  └── manifest.json          {build_at, total_nodes, indexed_nodes, ...}
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Iterator

import yaml

log = logging.getLogger("authority_resolver")

ROOT = _eroot()
PRIMARY_SOURCES = ROOT / "obsidian_sync" / "primary_sources"
INDEX_DIR = PRIMARY_SOURCES / "_index"

# ── Citation pattern set ─────────────────────────────────────────────
# Each pattern extracts a citation; the matched substring is normalized
# below (canonical_citation).  Patterns are ordered: more specific first.

CITATION_PATTERNS = [
    # ── FEDERAL STATUTES ──
    ("USC",       re.compile(r"\b(\d{1,2})\s*U\.?\s*S\.?\s*C\.?\s*§?\s*(\d+[a-zA-Z]?(?:\(\w+\))*)", re.I)),
    ("CFR",       re.compile(r"\b(\d{1,2})\s*C\.?\s*F\.?\s*R\.?\s*§?\s*(\d+(?:\.\d+)?)", re.I)),

    # ── FEDERAL RULES ──
    ("FRCP",      re.compile(r"\b(?:Fed\.?\s*R\.?\s*Civ\.?\s*P\.?|FRCP)\s*(\d+(?:\(\w+\))*)", re.I)),
    ("FRAP",      re.compile(r"\b(?:Fed\.?\s*R\.?\s*App\.?\s*P\.?|FRAP)\s*(\d+(?:\(\w+\))*)", re.I)),
    ("FRE",       re.compile(r"\b(?:Fed\.?\s*R\.?\s*Evid\.?|FRE)\s*(\d+)", re.I)),
    ("FRCrP",     re.compile(r"\b(?:Fed\.?\s*R\.?\s*Crim\.?\s*P\.?|FRCrP)\s*(\d+)", re.I)),
    ("LAR",       re.compile(r"\b(\d\w*\s*Cir\.?)?\s*L\.?A\.?R\.?\s*(\d+(?:\.\d+)?)", re.I)),

    # ── CASE REPORTERS ──
    ("USRptr",    re.compile(r"\b(\d+)\s+U\.?\s*S\.?\s+(\d+)")),
    ("SCt",       re.compile(r"\b(\d+)\s+S\.?\s*Ct\.?\s+(\d+)")),
    ("LEd",       re.compile(r"\b(\d+)\s+L\.?\s*Ed\.?\s*(\d?d?)\s+(\d+)")),
    ("F4th",      re.compile(r"\b(\d+)\s+F\.\s*4th\s+(\d+)")),
    ("F3d",       re.compile(r"\b(\d+)\s+F\.\s*3d\s+(\d+)")),
    ("F2d",       re.compile(r"\b(\d+)\s+F\.\s*2d\s+(\d+)")),
    ("FAppx",     re.compile(r"\b(\d+)\s+F\.\s*App'?x\.?\s+(\d+)")),
    ("FSupp",     re.compile(r"\b(\d+)\s+F\.\s*Supp\.?\s*(\d?d?)\s+(\d+)")),
    ("WL",        re.compile(r"\b(\d{4})\s+WL\s+(\d+)")),
    ("Atl",       re.compile(r"\b(\d+)\s+A\.\s*(2d|3d)?\s+(\d+)")),

    # ── PENNSYLVANIA ──
    ("PaCS",      re.compile(r"\b(\d+)\s*Pa\.?\s*C\.?\s*S\.?\s*§?\s*(\d+)", re.I)),
    ("PaRCP",     re.compile(r"\bPa\.?\s*R\.?\s*C\.?\s*P\.?\s*(\d+)", re.I)),
    ("PaRAP",     re.compile(r"\bPa\.?\s*R\.?\s*A\.?\s*P\.?\s*(\d+)", re.I)),
    ("PaCode",    re.compile(r"\b(\d+)\s+Pa\.?\s*Code\s*§?\s*(\d+\.?\d*)?", re.I)),
]


def _normalize_citation(pattern_name: str, match: re.Match) -> str:
    """Canonicalize a citation into a stable string key."""
    g = match.groups()
    if pattern_name == "USC":
        return f"{g[0]} U.S.C. § {g[1]}"
    if pattern_name == "CFR":
        return f"{g[0]} C.F.R. § {g[1]}"
    if pattern_name == "FRCP":
        return f"Fed. R. Civ. P. {g[0]}"
    if pattern_name == "FRAP":
        return f"Fed. R. App. P. {g[0]}"
    if pattern_name == "FRE":
        return f"Fed. R. Evid. {g[0]}"
    if pattern_name == "FRCrP":
        return f"Fed. R. Crim. P. {g[0]}"
    if pattern_name == "LAR":
        circuit = (g[0] or "").strip()
        rule = g[1]
        if circuit:
            return f"{circuit} L.A.R. {rule}"
        return f"L.A.R. {rule}"
    if pattern_name == "USRptr":
        return f"{g[0]} U.S. {g[1]}"
    if pattern_name == "SCt":
        return f"{g[0]} S. Ct. {g[1]}"
    if pattern_name == "LEd":
        suffix = g[1] or ""
        return f"{g[0]} L. Ed. {suffix} {g[2]}".replace("  ", " ").strip()
    if pattern_name in ("F4th", "F3d", "F2d"):
        rep = {"F4th": "F.4th", "F3d": "F.3d", "F2d": "F.2d"}[pattern_name]
        return f"{g[0]} {rep} {g[1]}"
    if pattern_name == "FAppx":
        return f"{g[0]} F. App'x {g[1]}"
    if pattern_name == "FSupp":
        suffix = g[1] or ""
        return f"{g[0]} F. Supp. {suffix} {g[2]}".replace("  ", " ").strip()
    if pattern_name == "WL":
        return f"{g[0]} WL {g[1]}"
    if pattern_name == "Atl":
        series = g[1] or ""
        return f"{g[0]} A.{series} {g[2]}".replace("A. ", "A. ").strip()
    if pattern_name == "PaCS":
        return f"{g[0]} Pa.C.S. § {g[1]}"
    if pattern_name == "PaRCP":
        return f"Pa.R.C.P. {g[0]}"
    if pattern_name == "PaRAP":
        return f"Pa.R.A.P. {g[0]}"
    if pattern_name == "PaCode":
        if g[1]:
            return f"{g[0]} Pa. Code § {g[1]}"
        return f"{g[0]} Pa. Code"
    return match.group(0).strip()


def _extract_citations(body: str) -> dict[str, list[str]]:
    """Extract all citations from a node body. Returns {pattern_name: [canonical_cite, ...]}"""
    hits = {}
    for name, rx in CITATION_PATTERNS:
        out = []
        for m in rx.finditer(body):
            try:
                canonical = _normalize_citation(name, m)
                if canonical and len(canonical) < 200:
                    out.append(canonical)
            except (IndexError, AttributeError):
                continue
        if out:
            hits[name] = out
    return hits


def _read_node(path: Path) -> tuple[dict, str]:
    """Read a primary_source node. Returns (frontmatter_dict, body_text)."""
    try:
        txt = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {}, ""
    if not txt.startswith("---"):
        return {}, txt
    try:
        end = txt.index("\n---", 4)
        fm_str = txt[4:end]
        body = txt[end + 4:]
        fm = yaml.safe_load(fm_str) or {}
        if not isinstance(fm, dict):
            fm = {}
        return fm, body
    except (ValueError, yaml.YAMLError):
        return {}, txt


def _iter_primary_sources() -> Iterator[Path]:
    """Yield every .md file under primary_sources/, skipping _index/."""
    for dirpath, dirnames, files in os.walk(PRIMARY_SOURCES):
        # Skip the index directory itself
        if "_index" in dirpath:
            continue
        if "_index" in dirnames:
            dirnames.remove("_index")
        for f in files:
            if f.endswith(".md"):
                yield Path(dirpath) / f


def build_index(force: bool = False, verbose: bool = True) -> dict:
    """Build the citation index for primary_sources/. One-time pass."""
    manifest_path = INDEX_DIR / "manifest.json"
    if manifest_path.exists() and not force:
        old = json.loads(manifest_path.read_text())
        if verbose:
            log.info(f"Index already exists (built {old.get('build_at')}). Use force=True to rebuild.")
        return {"status": "exists", "manifest": old}

    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    by_citation: dict[str, list[str]] = {}
    by_jurisdiction: dict[str, list[str]] = {}
    by_authority_level: dict[str, list[str]] = {}
    node_metadata: dict[str, dict] = {}

    total = 0
    indexed = 0
    total_cite_hits = 0

    for path in _iter_primary_sources():
        total += 1
        rel = str(path.relative_to(PRIMARY_SOURCES))
        fm, body = _read_node(path)
        if not body:
            continue

        cap_body = body[:50000]
        cites_by_pattern = _extract_citations(cap_body)
        unique_cites = set()
        for pattern_cites in cites_by_pattern.values():
            unique_cites.update(pattern_cites)

        jur = (fm.get("jurisdiction") or "unknown").strip()
        if jur not in by_jurisdiction:
            by_jurisdiction[jur] = []
        by_jurisdiction[jur].append(rel)

        auth_level = (
            (fm.get("conformity") or {}).get("authority_level")
            if isinstance(fm.get("conformity"), dict)
            else fm.get("authority_level")
        )
        if auth_level:
            auth_key = str(auth_level).upper()
            if auth_key not in by_authority_level:
                by_authority_level[auth_key] = []
            by_authority_level[auth_key].append(rel)

        if unique_cites:
            indexed += 1
            for c in unique_cites:
                if c not in by_citation:
                    by_citation[c] = []
                by_citation[c].append(rel)
            total_cite_hits += len(unique_cites)

        # Compact node metadata — only store nodes with at least 1 citation
        if unique_cites or fm.get("title"):
            node_metadata[rel] = {
                "title": (fm.get("title") or "")[:200],
                "jurisdiction": jur,
                "type": fm.get("type"),
                "authority_level": auth_level,
                "citations": sorted(unique_cites),
                "citation_count": len(unique_cites),
            }

        if verbose and total % 5000 == 0:
            elapsed = time.time() - t0
            log.info(f"  …{total} scanned ({elapsed:.1f}s, {total/elapsed:.0f}/s)")

    # Sort citation buckets by node count desc for stability
    for c in by_citation:
        by_citation[c] = sorted(set(by_citation[c]))

    elapsed = time.time() - t0
    manifest = {
        "build_at": datetime.now(timezone.utc).isoformat(),
        "build_duration_sec": round(elapsed, 1),
        "total_nodes_scanned": total,
        "nodes_with_citations": indexed,
        "coverage_pct": round(indexed * 100 / total, 1) if total else 0,
        "unique_citations": len(by_citation),
        "total_citation_hits": total_cite_hits,
        "jurisdictions": {k: len(v) for k, v in by_jurisdiction.items()},
        "authority_levels": {k: len(v) for k, v in by_authority_level.items()},
        "patterns_used": [name for name, _ in CITATION_PATTERNS],
    }

    if verbose:
        log.info(f"Index built in {elapsed:.1f}s — {indexed}/{total} nodes ({manifest['coverage_pct']}%), {len(by_citation)} unique citations")

    # Write atomically
    (INDEX_DIR / "by_citation.json").write_text(json.dumps(by_citation, indent=2), encoding="utf-8")
    (INDEX_DIR / "by_jurisdiction.json").write_text(json.dumps(by_jurisdiction, indent=2), encoding="utf-8")
    (INDEX_DIR / "by_authority_level.json").write_text(json.dumps(by_authority_level, indent=2), encoding="utf-8")
    (INDEX_DIR / "node_metadata.json").write_text(json.dumps(node_metadata, indent=2), encoding="utf-8")
    (INDEX_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return {"status": "built", "manifest": manifest}


# ── Cached index loader (singleton) ───────────────────────────────────
_index_cache: dict = {}


def _load_index() -> dict:
    """Lazy-load the index. Returns dict with by_citation, by_jurisdiction, etc."""
    global _index_cache
    if _index_cache:
        return _index_cache

    manifest_path = INDEX_DIR / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"Citation index not built. Run: "
            f"python3 -c 'from pipeline.authority_resolver import build_index; build_index(verbose=True)'"
        )

    _index_cache = {
        "manifest":           json.loads((INDEX_DIR / "manifest.json").read_text(encoding="utf-8")),
        "by_citation":        json.loads((INDEX_DIR / "by_citation.json").read_text(encoding="utf-8")),
        "by_jurisdiction":    json.loads((INDEX_DIR / "by_jurisdiction.json").read_text(encoding="utf-8")),
        "by_authority_level": json.loads((INDEX_DIR / "by_authority_level.json").read_text(encoding="utf-8")),
        "node_metadata":      json.loads((INDEX_DIR / "node_metadata.json").read_text(encoding="utf-8")),
    }
    return _index_cache


def _citation_variants(citation: str) -> list[str]:
    """Generate fuzzy variants of a citation to try against the index.

    Handles section-symbol variation, period normalization, parenthetical stripping.
    """
    c = citation.strip()
    variants = {c}
    # Variant: normalize whitespace
    variants.add(re.sub(r"\s+", " ", c))
    # Variant: section symbol vs no symbol
    variants.add(c.replace("§", "Section").strip())
    variants.add(c.replace("Section", "§").strip())
    # Variant: strip subdivision parens (e.g., "§ 1681(a)" → "§ 1681")
    no_paren = re.sub(r"\(\w+\)+", "", c).strip()
    if no_paren != c:
        variants.add(no_paren)
    # Variant: remove all dots
    no_dots = c.replace(".", "")
    variants.add(no_dots)
    return list(variants)


def resolve_citation(citation: str,
                     jurisdiction: Optional[str] = None,
                     limit: int = 10) -> list[dict]:
    """Resolve a citation string to matching rule nodes.

    Args:
        citation: e.g. "15 U.S.C. § 1681" or "FRCP 12(b)(6)"
        jurisdiction: optional filter ('federal', 'PA', 'pennsylvania', 'unknown')
        limit: max results

    Returns: list of {path, title, jurisdiction, type, authority_level,
                       citation_count, citations} dicts.
    """
    idx = _load_index()
    by_cite = idx["by_citation"]
    node_meta = idx["node_metadata"]

    # Try exact match first, then variants
    matching_paths: list[str] = []
    seen = set()

    for variant in _citation_variants(citation):
        if variant in by_cite:
            for p in by_cite[variant]:
                if p not in seen:
                    seen.add(p)
                    matching_paths.append(p)

    # Substring fallback (only if no exact/variant hits) — slow but small key set
    if not matching_paths:
        norm = re.sub(r"\s+", " ", citation.lower())
        for indexed_cite, paths in by_cite.items():
            if norm in re.sub(r"\s+", " ", indexed_cite.lower()):
                for p in paths:
                    if p not in seen:
                        seen.add(p)
                        matching_paths.append(p)

    results = []
    for path in matching_paths:
        meta = node_meta.get(path, {})
        if jurisdiction and meta.get("jurisdiction") != jurisdiction:
            continue
        results.append({
            "path": path,
            "abs_path": str(PRIMARY_SOURCES / path),
            "title": meta.get("title", ""),
            "jurisdiction": meta.get("jurisdiction"),
            "type": meta.get("type"),
            "authority_level": meta.get("authority_level"),
            "citation_count": meta.get("citation_count", 0),
            "citations_sample": meta.get("citations", [])[:5],
        })
        if len(results) >= limit:
            break
    return results


# BEEZLE_FN_UNUSED 2026-06-13: list_jurisdiction (no static refs in pipeline/)
def list_jurisdiction(jurisdiction: str) -> list[str]:
    """Return all rule node paths for a jurisdiction."""
    idx = _load_index()
    return idx["by_jurisdiction"].get(jurisdiction, [])


# BEEZLE_FN_UNUSED 2026-06-13: list_authority_level (no static refs in pipeline/)
def list_authority_level(level: str) -> list[str]:
    """Return all rule node paths for a given authority_level frontmatter value."""
    idx = _load_index()
    return idx["by_authority_level"].get(level.upper(), [])


def index_stats() -> dict:
    """Summary of current index state."""
    idx = _load_index()
    return idx["manifest"]


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if len(sys.argv) > 1 and sys.argv[1] == "build":
        force = "--force" in sys.argv
        r = build_index(force=force, verbose=True)
        print(json.dumps(r["manifest"], indent=2))
    elif len(sys.argv) > 1 and sys.argv[1] == "stats":
        print(json.dumps(index_stats(), indent=2))
    elif len(sys.argv) > 2 and sys.argv[1] == "resolve":
        cite = " ".join(sys.argv[2:])
        results = resolve_citation(cite)
        print(f"Found {len(results)} matches for: {cite}")
        for r in results:
            print(f"  {r['path']:60s}  [{r['jurisdiction']}]  {r['title'][:50]}")
    else:
        print("Usage:")
        print("  python -m pipeline.authority_resolver build [--force]")
        print("  python -m pipeline.authority_resolver stats")
        print("  python -m pipeline.authority_resolver resolve '15 U.S.C. § 1681'")
