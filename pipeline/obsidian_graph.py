# -*- coding: utf-8 -*-
"""
pipeline/obsidian_graph.py — Session 20 / Phase H
==================================================
Native Obsidian graph queries over the [[wikilinks]] in note bodies.

WHY THIS MODULE EXISTS
----------------------
ObsidianIndex (obsidian_sync_tools/obsidian_query.py) indexes by frontmatter
`cites:` lists only. Diagnostic in Session 20 Step 2 confirmed total_citations=0
across 65,669 nodes — almost no notes have `cites:` frontmatter, so the existing
trace_authority() walker returns empty.

The actual linkage in this vault is [[wikilinks]] in note bodies (50,491 of
66,778 notes have them, per Step 2 diagnostic). This module builds and caches
that linkage as a queryable graph.

DESIGN
------
1. Wikilink index: built once, cached to disk at obsidian_sync/_index/.
   ~4-minute cold build; <1 second to load cache afterwards.
2. Folder scope: every query defaults to excluding primary_sources/,
   unmatched_emails/, __pycache__/, _workers_v18f2/, dev/. Opt in per query.
3. Proposal pattern: writes go to _proposals/<ts>/, approval moves to
   _applied/<ts>/ and writes the actual note. Mirrors Phase G (Session 19).

PUBLIC API
----------
  obsidian_backlinks(note_path, folder_scope=None)
  obsidian_outgoing_links(note_path, resolve=True)
  obsidian_graph_walk(start, depth=2, edge_types=None, folder_scope=None,
                      max_nodes=200)
  obsidian_search_by_tag(tag, frontmatter_only=True, limit=500)
  obsidian_search_by_frontmatter(field, value, limit=500)
  obsidian_list_orphans(folder_scope=None, limit=100)
  obsidian_graph_stats(folder_scope=None)
  obsidian_propose_note(subdir, filename, content, frontmatter, links_to)
  obsidian_propose_link(from_note, to_note, edge_type, citation, scope=None)
  approve_obsidian_proposal(proposal_id)

  build_wikilink_index(folder_scope=None, force=False)
  get_wikilink_index(folder_scope=None, max_age_hours=24)

NEVER WRITES DIRECTLY TO THE LIVE VAULT EXCEPT VIA approve_obsidian_proposal.
"""

from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("obsidian_graph")

# ════════════════════════════════════════════════════════════════════
# CONSTANTS
# ════════════════════════════════════════════════════════════════════

_DEFAULT_ROOT = os.environ.get("BEEZLE_ROOT", str(_eroot()))
ROOT = Path(_DEFAULT_ROOT)
VAULT = ROOT / "obsidian_sync"
INDEX_DIR = VAULT / "_index"
PROPOSALS_DIR = VAULT / "_proposals"
APPLIED_DIR = PROPOSALS_DIR / "_applied"

# Folders excluded by default. Caller can opt-in via folder_scope.
_DEFAULT_EXCLUDE = (
    "primary_sources",      # 34K USC/CFR monoliths — opt-in only
    "unmatched_emails",     # 19K email feedstock — opt-in only
    "__pycache__",
    "_workers_v18f2",
    "dev",
    "_index",               # our own cache files
    "_proposals",           # staged not-yet-live notes
)

# Valid edge types for proposals
_EDGE_TYPES = (
    "cross_ref",        # internal same-statute reference
    "authority",        # CFR -> USC authority cite
    "defined_by",       # term imported from another section
    "implements",       # regulation implements statute
    "incorporates",     # by-reference cite to another reg
    "enforced_by",      # agency enforcement designation
    "interpreted_by",   # guidance/ruling interpreting reg
    "judicial_overlay", # case decision applying or limiting
    "unknown",          # legacy wikilink, no edge-type tag
)

# Wikilink regexes. Pattern 1: extract target+alias. Pattern 2: parse alias for edge_type:pin_cite.
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:\|([^\]]+))?\]\]")
_EDGE_ALIAS_RE = re.compile(r"^([a-z_]+):(.+)$")  # edge_type:pin_cite
_FRONTMATTER_TAG_RE = re.compile(r"^tags:\s*\[?([^\]]+)\]?\s*$", re.MULTILINE)


# ════════════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════════════

def _is_excluded(rel_path: str, folder_scope: Optional[List[str]]) -> bool:
    """True if path should be skipped under current scope."""
    parts = Path(rel_path).parts
    if not parts:
        return True
    top = parts[0]
    if folder_scope is not None:
        # Caller specified explicit scope: only include those folders
        return top not in folder_scope
    # Default: exclude noise folders
    return top in _DEFAULT_EXCLUDE


def _scope_hash(folder_scope: Optional[List[str]]) -> str:
    """Stable cache key for a scope spec."""
    if folder_scope is None:
        return "default"
    return hashlib.md5(",".join(sorted(folder_scope)).encode()).hexdigest()[:12]


# BEEZLE_FN_UNUSED 2026-06-13: _parse_wikilink (no static refs in pipeline/)
def _parse_wikilink(raw: str) -> Tuple[str, str, str, Optional[str]]:
    """Parse a [[target|edge_type:pin_cite]] form.

    Returns: (target, raw_alias, edge_type, pin_cite)
    """
    m = _WIKILINK_RE.match("[[" + raw + "]]")
    if m:
        target = m.group(1).strip()
        alias = (m.group(2) or "").strip()
    else:
        target = raw.strip()
        alias = ""

    edge_type = "unknown"
    pin_cite: Optional[str] = None
    if alias:
        em = _EDGE_ALIAS_RE.match(alias)
        if em:
            candidate = em.group(1)
            if candidate in _EDGE_TYPES:
                edge_type = candidate
                pin_cite = em.group(2).strip()
    return target, alias, edge_type, pin_cite


def _extract_wikilinks_with_edges(body: str) -> List[Dict[str, Any]]:
    """Pull [[wikilinks]] from body, parse edge_type:pin_cite when present."""
    results = []
    for m in _WIKILINK_RE.finditer(body):
        target = m.group(1).strip()
        alias = (m.group(2) or "").strip()
        edge_type = "unknown"
        pin_cite: Optional[str] = None
        if alias:
            em = _EDGE_ALIAS_RE.match(alias)
            if em and em.group(1) in _EDGE_TYPES:
                edge_type = em.group(1)
                pin_cite = em.group(2).strip()
        results.append({
            "target": target,
            "alias": alias,
            "edge_type": edge_type,
            "pin_cite": pin_cite,
        })
    return results


def _extract_frontmatter_tags(body: str) -> List[str]:
    """Get tags from frontmatter, supporting both YAML shapes.

    Flow form:  tags: [a, b, c]
    Block form: tags:
                - a
                - b

    Inline #hashtag from note bodies is intentionally NOT included —
    diagnostic showed vault has junk tags scraped from email subject lines.
    Handles CRLF by stripping \r from each line.
    """
    if not body.startswith("---"):
        return []
    end = body.find("\n---", 3)
    if end == -1:
        return []
    fm_block = body[3:end]
    tags = []
    in_tags_block = False
    for raw_line in fm_block.split("\n"):
        line = raw_line.rstrip("\r")
        stripped = line.strip()
        if not stripped:
            in_tags_block = False
            continue
        if stripped.startswith("tags:"):
            rest = stripped[5:].strip()
            if rest.startswith("[") and rest.endswith("]"):
                # Flow form: tags: [a, b, c]
                inner = rest[1:-1]
                tags.extend([t.strip().strip("'\"") for t in inner.split(",") if t.strip()])
                in_tags_block = False
            elif rest == "":
                # Block form opening: tags:
                in_tags_block = True
            else:
                # Single inline value: tags: foo  (rare but possible)
                tags.append(rest.strip("'\""))
                in_tags_block = False
        elif in_tags_block:
            # Continuation line — must be "- value" or end the block
            if stripped.startswith("- "):
                v = stripped[2:].strip().strip("'\"")
                if v:
                    tags.append(v)
            elif line.startswith(" ") or line.startswith("\t"):
                # indented non-list content; ignore but stay in block
                continue
            else:
                # New top-level key — block ended
                in_tags_block = False
    return [t for t in tags if t]


def _extract_frontmatter_field(body: str, field: str) -> Optional[str]:
    """Get scalar value of a single frontmatter field."""
    if not body.startswith("---"):
        return None
    end = body.find("\n---", 3)
    if end == -1:
        return None
    fm_block = body[3:end]
    pattern = re.compile(r"^" + re.escape(field) + r":\s*(.+)$", re.MULTILINE)
    m = pattern.search(fm_block)
    if m:
        return m.group(1).strip().strip("\\'\"")
    return None


_SLUG_ALIASES_CACHE = None
_PATH_SUFFIX_INDEX_CACHE = None


def _load_slug_aliases() -> dict:
    """Load the slug-alias map from obsidian_sync/_index/slug_aliases.json.

    Returns a dict with two keys:
      - 'aliases': {long_slug: short_path_prefix} for path-prefix substitution
      - 'case_name_aliases': {alt_name: canonical_name} for Cases/X normalization
    Empty dict if file missing or malformed (resolver falls back gracefully).
    """
    global _SLUG_ALIASES_CACHE
    if _SLUG_ALIASES_CACHE is not None:
        return _SLUG_ALIASES_CACHE
    import json as _json
    alias_file = INDEX_DIR / "slug_aliases.json"
    if not alias_file.exists():
        _SLUG_ALIASES_CACHE = {"aliases": {}, "case_name_aliases": {}}
        return _SLUG_ALIASES_CACHE
    try:
        data = _json.loads(alias_file.read_text(encoding="utf-8"))
        _SLUG_ALIASES_CACHE = {
            "aliases": data.get("aliases", {}),
            "case_name_aliases": data.get("case_name_aliases", {}),
        }
    except Exception:
        _SLUG_ALIASES_CACHE = {"aliases": {}, "case_name_aliases": {}}
    return _SLUG_ALIASES_CACHE


def _build_path_suffix_index(force: bool = False) -> dict:
    """Build a {path_suffix: [list of relative paths]} index over the vault.

    For each .md file at relative path 'a/b/c.md', registers suffixes:
      'c.md', 'b/c.md', 'a/b/c.md'.
    Used for Obsidian-style "shortest-path" resolution when literal paths fail.

    Cached in module global; pass force=True to rebuild.
    """
    global _PATH_SUFFIX_INDEX_CACHE
    if _PATH_SUFFIX_INDEX_CACHE is not None and not force:
        return _PATH_SUFFIX_INDEX_CACHE
    suffix_index: dict = {}
    excluded = set(_DEFAULT_EXCLUDE)
    for md_file in VAULT.rglob("*.md"):
        try:
            rel = md_file.relative_to(VAULT)
        except ValueError:
            continue
        parts = rel.parts
        # Skip excluded top-level folders
        if parts and parts[0] in excluded:
            continue
        rel_str = str(rel).replace("\\", "/")
        # Register every trailing-suffix variant
        for i in range(len(parts)):
            suffix = "/".join(parts[i:])
            suffix_index.setdefault(suffix, []).append(rel_str)
    _PATH_SUFFIX_INDEX_CACHE = suffix_index
    return _PATH_SUFFIX_INDEX_CACHE


def _resolve_link_target(target: str):
    """Resolve a wikilink target to a vault-relative path, or None if not found.

    Resolution strategies, tried in order:
      1. Literal: VAULT/{target}.md exists
      2. Literal with .md already in target
      3. Case-insensitive parent-folder match (existing behavior)
      4. NEW: slug-alias substitution from _index/slug_aliases.json
      5. NEW: path-suffix index lookup (Obsidian-style shortest-path match)
         Only returns a hit if the suffix lookup is unambiguous (1 result).
    """
    if not target:
        return None

    # Strip leading/trailing whitespace and any anchor fragments
    clean = target.strip()
    if "#" in clean:
        clean = clean.split("#", 1)[0]
    if "|" in clean:
        clean = clean.split("|", 1)[0]
    clean = clean.strip()
    if not clean:
        return None

    # ─── Strategy 0 (NEW, but fast): case_name_aliases for Cases/X normalization ──
    aliases = _load_slug_aliases()
    case_aliases = aliases.get("case_name_aliases", {})
    if clean in case_aliases:
        clean = case_aliases[clean]

    # ─── Strategy 1: literal path with .md appended ─────────────────────
    candidate = VAULT / f"{clean}.md"
    if candidate.exists() and candidate.is_file():
        try:
            return str(candidate.relative_to(VAULT)).replace("\\", "/")
        except ValueError:
            pass

    # ─── Strategy 2: literal path if it already ends with .md ───────────
    if clean.lower().endswith(".md"):
        candidate = VAULT / clean
        if candidate.exists() and candidate.is_file():
            try:
                return str(candidate.relative_to(VAULT)).replace("\\", "/")
            except ValueError:
                pass

    # ─── Strategy 3: case-insensitive parent traversal (legacy behavior) ─
    # Walk segments and try case-insensitive matching at each level
    try:
        parts = clean.split("/")
        current = VAULT
        for i, part in enumerate(parts):
            is_last = (i == len(parts) - 1)
            target_name = f"{part}.md" if is_last else part
            # Try exact first
            next_path = current / target_name
            if next_path.exists():
                current = next_path
                continue
            # Case-insensitive scan
            found = None
            if current.is_dir():
                for child in current.iterdir():
                    if child.name.lower() == target_name.lower():
                        found = child
                        break
            if found is None:
                current = None
                break
            current = found
        if current is not None and current.is_file():
            try:
                return str(current.relative_to(VAULT)).replace("\\", "/")
            except ValueError:
                pass
    except Exception:
        pass

    # ─── Strategy 4 (NEW): slug-alias substitution ──────────────────────
    # If target starts with a known long-slug, rewrite to canonical path and retry.
    path_aliases = aliases.get("aliases", {})
    for long_slug, short_path in path_aliases.items():
        if clean == long_slug or clean.startswith(long_slug + "/"):
            rewritten = short_path + clean[len(long_slug):]
            # Try literal lookup with the rewritten path
            cand = VAULT / f"{rewritten}.md"
            if cand.exists() and cand.is_file():
                try:
                    return str(cand.relative_to(VAULT)).replace("\\", "/")
                except ValueError:
                    pass
            # Also try the .md-already-appended variant
            if rewritten.lower().endswith(".md"):
                cand = VAULT / rewritten
                if cand.exists() and cand.is_file():
                    try:
                        return str(cand.relative_to(VAULT)).replace("\\", "/")
                    except ValueError:
                        pass

    # ─── Strategy 5 (NEW): path-suffix index lookup ─────────────────────
    # Try increasingly-shorter suffixes; only accept unambiguous (single-result) matches.
    suffix_index = _build_path_suffix_index()
    needle = f"{clean}.md"
    matches = suffix_index.get(needle, [])
    if len(matches) == 1:
        return matches[0]
    # Basename-only fallback: ONLY when the original target had no "/" in it.
    # When the target is a path-fragment (e.g. "Cases/Equifax"), a basename-only
    # match would resolve to a completely different folder's note — wrong.
    # Restrict basename fallback to pure filename references like "[[Equifax]]".
    if "/" not in clean:
        basename_matches = suffix_index.get(needle, [])
        if len(basename_matches) == 1:
            return basename_matches[0]

    return None


def _cache_path(folder_scope: Optional[List[str]]) -> Path:
    return INDEX_DIR / f"wikilink_index_{_scope_hash(folder_scope)}.json"


def build_wikilink_index(
    folder_scope: Optional[List[str]] = None,
    force: bool = False,
    verbose: bool = False,
) -> Dict[str, Any]:
    """One-time scan: for every note in scope, extract wikilinks from body.

    Returns: {
        "scope": folder_scope or "default",
        "built_at": ISO timestamp,
        "stats": {"files_scanned": N, "files_with_links": N, "edges": N,
                  "unresolved_targets": N, "duration_s": float},
        "outgoing": {rel_path: [{target, edge_type, pin_cite, resolved_to}, ...]},
        "incoming": {rel_path: [source_paths_that_link_here]},
        "orphans": [rel_paths with zero in+out],
    }

    Written to obsidian_sync/_index/wikilink_index_<scope_hash>.json
    """
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    cache = _cache_path(folder_scope)
    if cache.exists() and not force:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning(f"cache read failed, rebuilding: {e}")

    if verbose:
        print(f"[obsidian_graph] Building wikilink index, scope={folder_scope or 'default'}")

    t0 = time.time()
    outgoing: Dict[str, List[Dict[str, Any]]] = {}
    incoming: Dict[str, List[str]] = {}
    files_scanned = 0
    files_with_links = 0
    edge_count = 0
    unresolved = 0

    for md_path in VAULT.rglob("*.md"):
        try:
            rel = str(md_path.relative_to(VAULT))
        except ValueError:
            continue
        if _is_excluded(rel, folder_scope):
            continue
        files_scanned += 1
        try:
            body = md_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        links = _extract_wikilinks_with_edges(body)
        if not links:
            outgoing[rel] = []
            continue
        files_with_links += 1
        resolved_links = []
        for link in links:
            resolved = _resolve_link_target(link["target"])
            resolved_str = str(resolved) if resolved else None
            if resolved is None:
                unresolved += 1
            resolved_links.append({
                "target": link["target"],
                "edge_type": link["edge_type"],
                "pin_cite": link["pin_cite"],
                "resolved_to": resolved_str,
            })
            if resolved_str:
                incoming.setdefault(resolved_str, []).append(rel)
            edge_count += 1
        outgoing[rel] = resolved_links

        if verbose and files_scanned % 5000 == 0:
            print(f"  ...scanned {files_scanned}, edges so far: {edge_count}")

    # Orphans: notes in outgoing with no outgoing AND no incoming
    orphans = []
    for rel in outgoing:
        if not outgoing[rel] and not incoming.get(rel):
            orphans.append(rel)

    duration = time.time() - t0
    payload = {
        "scope": folder_scope or "default",
        "built_at": datetime.now(timezone.utc).isoformat(),
        "vault_root": str(VAULT),
        "stats": {
            "files_scanned": files_scanned,
            "files_with_links": files_with_links,
            "edges": edge_count,
            "unresolved_targets": unresolved,
            "orphans": len(orphans),
            "duration_s": round(duration, 2),
        },
        "outgoing": outgoing,
        "incoming": incoming,
        "orphans": orphans,
    }

    try:
        cache.write_text(json.dumps(payload), encoding="utf-8")
    except Exception as e:
        log.warning(f"cache write failed: {e}")

    if verbose:
        print(f"[obsidian_graph] Done in {duration:.1f}s. "
              f"scanned={files_scanned} edges={edge_count} orphans={len(orphans)}")
    return payload


def get_wikilink_index(
    folder_scope: Optional[List[str]] = None,
    max_age_hours: float = 24,
) -> Dict[str, Any]:
    """Load cached wikilink index. Rebuild if missing or stale."""
    cache = _cache_path(folder_scope)
    if cache.exists():
        try:
            payload = json.loads(cache.read_text(encoding="utf-8"))
            built = datetime.fromisoformat(payload["built_at"].replace("Z", "+00:00"))
            age_hours = (datetime.now(timezone.utc) - built).total_seconds() / 3600
            if age_hours < max_age_hours:
                return payload
        except Exception:
            pass
    return build_wikilink_index(folder_scope=folder_scope, force=True)


# ════════════════════════════════════════════════════════════════════
# PUBLIC QUERY API
# ════════════════════════════════════════════════════════════════════

# BEEZLE_FN_UNUSED 2026-06-13: obsidian_backlinks (no static refs in pipeline/)
def obsidian_backlinks(
    note_path: str,
    folder_scope: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """List notes that link TO note_path. Reads from cached index."""
    idx = get_wikilink_index(folder_scope=folder_scope)
    incoming = idx["incoming"].get(note_path, [])
    return {
        "note_path": note_path,
        "backlinks_count": len(incoming),
        "backlinks": incoming,
        "scope": idx["scope"],
    }


# BEEZLE_FN_UNUSED 2026-06-13: obsidian_outgoing_links (no static refs in pipeline/)
def obsidian_outgoing_links(
    note_path: str,
    folder_scope: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """List wikilinks FROM note_path, with edge types and resolution status."""
    idx = get_wikilink_index(folder_scope=folder_scope)
    out = idx["outgoing"].get(note_path)
    if out is None:
        return {
            "note_path": note_path,
            "error": "note not in index (may be excluded by scope, or not in vault)",
            "scope": idx["scope"],
        }
    return {
        "note_path": note_path,
        "outgoing_count": len(out),
        "resolved_count": sum(1 for o in out if o["resolved_to"]),
        "unresolved_count": sum(1 for o in out if not o["resolved_to"]),
        "outgoing": out,
        "scope": idx["scope"],
    }


# BEEZLE_FN_UNUSED 2026-06-13: obsidian_graph_walk (no static refs in pipeline/)
def obsidian_graph_walk(
    start: str,
    depth: int = 2,
    edge_types: Optional[List[str]] = None,
    folder_scope: Optional[List[str]] = None,
    max_nodes: int = 200,
) -> Dict[str, Any]:
    """BFS walk from `start` note, following resolved wikilinks.

    Args:
        start: relative note path (e.g. 'cases/fed_26-cv-00123/statutes_and_rules.md')
        depth: how many hops outward
        edge_types: optional filter, e.g. ['authority', 'implements']
        folder_scope: optional scope restriction
        max_nodes: hard cap on visited nodes

    Returns: {start, depth, visited, edges_traversed, nodes: [...], by_depth: {...}}
    """
    idx = get_wikilink_index(folder_scope=folder_scope)
    outgoing = idx["outgoing"]
    incoming = idx["incoming"]

    visited: Dict[str, int] = {start: 0}  # node -> depth seen
    edges_traversed: List[Dict[str, Any]] = []
    frontier = [start]

    for d in range(depth):
        next_frontier = []
        for node in frontier:
            out_links = outgoing.get(node, [])
            in_links = incoming.get(node, [])

            # Outgoing edges
            for link in out_links:
                if edge_types and link["edge_type"] not in edge_types:
                    continue
                resolved = link["resolved_to"]
                if resolved is None:
                    continue
                if resolved not in visited and len(visited) < max_nodes:
                    visited[resolved] = d + 1
                    next_frontier.append(resolved)
                    edges_traversed.append({
                        "from": node,
                        "to": resolved,
                        "edge_type": link["edge_type"],
                        "pin_cite": link["pin_cite"],
                        "direction": "outgoing",
                        "depth": d + 1,
                    })

            # Incoming edges (backlinks)
            for src in in_links:
                if src not in visited and len(visited) < max_nodes:
                    visited[src] = d + 1
                    next_frontier.append(src)
                    edges_traversed.append({
                        "from": src,
                        "to": node,
                        "edge_type": "unknown",  # caller would need src note to know edge type
                        "pin_cite": None,
                        "direction": "incoming",
                        "depth": d + 1,
                    })

            if len(visited) >= max_nodes:
                break
        frontier = next_frontier
        if not frontier:
            break

    by_depth: Dict[int, List[str]] = {}
    for node, d in visited.items():
        by_depth.setdefault(d, []).append(node)

    return {
        "start": start,
        "depth": depth,
        "edge_types_filter": edge_types,
        "visited_count": len(visited),
        "edges_traversed_count": len(edges_traversed),
        "capped": len(visited) >= max_nodes,
        "by_depth": {str(k): v for k, v in by_depth.items()},
        "edges": edges_traversed[:500],  # cap response payload
        "scope": idx["scope"],
    }


# BEEZLE_FN_UNUSED 2026-06-13: obsidian_search_by_tag (no static refs in pipeline/)
def obsidian_search_by_tag(
    tag: str,
    frontmatter_only: bool = True,
    folder_scope: Optional[List[str]] = None,
    limit: int = 500,
) -> Dict[str, Any]:
    """Find notes with a given tag.

    frontmatter_only=True (default): only structured `tags: [...]` field.
    frontmatter_only=False: also include inline #hashtag in body (noisier).
    """
    tag_norm = tag.lstrip("#").lower()
    matches = []
    scanned = 0

    for md_path in VAULT.rglob("*.md"):
        try:
            rel = str(md_path.relative_to(VAULT))
        except ValueError:
            continue
        if _is_excluded(rel, folder_scope):
            continue
        scanned += 1
        try:
            body = md_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        # Frontmatter tags
        fm_tags = _extract_frontmatter_tags(body)
        if tag_norm in [t.lower() for t in fm_tags]:
            matches.append({"path": rel, "source": "frontmatter", "all_tags": fm_tags})
            if len(matches) >= limit:
                break
            continue

        if not frontmatter_only:
            # Inline #hashtag in body
            inline = re.findall(r"#([A-Za-z][A-Za-z0-9_/-]+)", body)
            if tag_norm in [t.lower() for t in inline]:
                matches.append({"path": rel, "source": "inline", "all_tags": list(set(inline))[:20]})
                if len(matches) >= limit:
                    break

    return {
        "tag": tag,
        "frontmatter_only": frontmatter_only,
        "scanned": scanned,
        "match_count": len(matches),
        "matches": matches,
    }


# BEEZLE_FN_UNUSED 2026-06-13: obsidian_search_by_frontmatter (no static refs in pipeline/)
def obsidian_search_by_frontmatter(
    field: str,
    value: str,
    folder_scope: Optional[List[str]] = None,
    limit: int = 500,
) -> Dict[str, Any]:
    """Find notes where frontmatter field equals value (case-insensitive)."""
    value_norm = value.lower()
    matches = []
    scanned = 0

    for md_path in VAULT.rglob("*.md"):
        try:
            rel = str(md_path.relative_to(VAULT))
        except ValueError:
            continue
        if _is_excluded(rel, folder_scope):
            continue
        scanned += 1
        try:
            body = md_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        val = _extract_frontmatter_field(body, field)
        if val and value_norm in val.lower():
            matches.append({"path": rel, "field": field, "value": val})
            if len(matches) >= limit:
                break

    return {
        "field": field,
        "value": value,
        "scanned": scanned,
        "match_count": len(matches),
        "matches": matches,
    }


# BEEZLE_FN_UNUSED 2026-06-13: obsidian_list_orphans (no static refs in pipeline/)
def obsidian_list_orphans(
    folder_scope: Optional[List[str]] = None,
    limit: int = 100,
) -> Dict[str, Any]:
    """Notes with zero outgoing AND zero incoming wikilinks in current scope."""
    idx = get_wikilink_index(folder_scope=folder_scope)
    orphans = idx["orphans"][:limit]
    return {
        "scope": idx["scope"],
        "total_orphans": len(idx["orphans"]),
        "returned": len(orphans),
        "orphans": orphans,
    }


# BEEZLE_FN_UNUSED 2026-06-13: obsidian_graph_stats (no static refs in pipeline/)
def obsidian_graph_stats(
    folder_scope: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Summary stats for the wikilink graph in current scope."""
    idx = get_wikilink_index(folder_scope=folder_scope)
    stats = dict(idx["stats"])
    stats["scope"] = idx["scope"]
    stats["built_at"] = idx["built_at"]
    stats["cache_path"] = str(_cache_path(folder_scope))
    return stats


# ════════════════════════════════════════════════════════════════════
# PROPOSAL API
# ════════════════════════════════════════════════════════════════════

def _new_proposal_id(kind: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")[:21]
    return f"{kind}_{ts}"


# BEEZLE_FN_UNUSED 2026-06-13: obsidian_propose_note (no static refs in pipeline/)
def obsidian_propose_note(
    subdir: str,
    filename: str,
    content: str,
    frontmatter: Optional[Dict[str, Any]] = None,
    links_to: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Stage a new note for human approval. Does NOT write to live vault.

    Saves to _proposals/<proposal_id>/<subdir>/<filename> + manifest.json.
    Returns proposal_id for use in approve_obsidian_proposal.
    """
    if not filename.endswith(".md"):
        filename = filename + ".md"
    proposal_id = _new_proposal_id("note")
    proposal_dir = PROPOSALS_DIR / proposal_id
    proposal_dir.mkdir(parents=True, exist_ok=True)

    body_parts = []
    if frontmatter:
        body_parts.append("---")
        for k, v in frontmatter.items():
            if isinstance(v, list):
                body_parts.append(f"{k}:")
                for item in v:
                    body_parts.append(f"  - {item}")
            else:
                body_parts.append(f"{k}: {v}")
        body_parts.append("---")
        body_parts.append("")
    body_parts.append(content)
    if links_to:
        body_parts.append("")
        body_parts.append("## Linked")
        for link in links_to:
            body_parts.append(f"- [[{link}]]")
    full_body = "\n".join(body_parts)

    target_rel = Path(subdir) / filename
    staged_file = proposal_dir / target_rel
    staged_file.parent.mkdir(parents=True, exist_ok=True)
    staged_file.write_text(full_body, encoding="utf-8")

    manifest = {
        "proposal_id": proposal_id,
        "kind": "note",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "target_path": str(target_rel),
        "target_full_path": str(VAULT / target_rel),
        "staged_file": str(staged_file.relative_to(VAULT)),
        "frontmatter": frontmatter or {},
        "links_to": links_to or [],
        "content_chars": len(content),
        "status": "staged",
    }
    (proposal_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    return {
        "ok": True,
        "proposal_id": proposal_id,
        "kind": "note",
        "target_path": str(target_rel),
        "staged_file": str(staged_file.relative_to(VAULT)),
        "manifest_path": str((proposal_dir / "manifest.json").relative_to(VAULT)),
        "preview_first_200_chars": full_body[:200],
    }


# BEEZLE_FN_UNUSED 2026-06-13: obsidian_propose_link (no static refs in pipeline/)
def obsidian_propose_link(
    from_note: str,
    to_note: str,
    edge_type: str,
    citation: str,
    scope: Optional[str] = None,
) -> Dict[str, Any]:
    """Stage adding a wikilink from from_note → to_note with edge metadata.

    The actual link, when approved, becomes:
        [[to_note|edge_type:citation]]
    appended to a `## Linked Authorities` section at the end of from_note.
    """
    if edge_type not in _EDGE_TYPES:
        return {
            "ok": False,
            "error": f"edge_type must be one of {_EDGE_TYPES!r}",
            "got": edge_type,
        }
    from_full = VAULT / from_note
    if not from_full.exists():
        return {"ok": False, "error": f"from_note not found: {from_note}"}

    proposal_id = _new_proposal_id("link")
    proposal_dir = PROPOSALS_DIR / proposal_id
    proposal_dir.mkdir(parents=True, exist_ok=True)

    alias = f"{edge_type}:{citation}"
    if scope:
        alias = f"{edge_type}:{citation} ({scope})"
    link_text = f"[[{to_note}|{alias}]]"

    manifest = {
        "proposal_id": proposal_id,
        "kind": "link",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "from_note": from_note,
        "to_note": to_note,
        "edge_type": edge_type,
        "citation": citation,
        "scope": scope,
        "link_text": link_text,
        "status": "staged",
    }
    (proposal_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    return {
        "ok": True,
        "proposal_id": proposal_id,
        "kind": "link",
        "from_note": from_note,
        "to_note": to_note,
        "edge_type": edge_type,
        "link_text_to_be_added": link_text,
        "manifest_path": str((proposal_dir / "manifest.json").relative_to(VAULT)),
    }


def _detect_line_ending(raw_bytes: bytes) -> str:
    """Return '\\r\\n' if file uses CRLF, '\\n' otherwise. Preserves original convention on write."""
    if b"\r\n" in raw_bytes:
        return "\r\n"
    return "\n"


def _split_frontmatter_lexical(text: str):
    """Split a note into (fm_block_lines, body_str, has_real_fm).

    Returns the raw lines of the YAML block (NOT parsed) so they can be
    preserved byte-exact. Body is everything after the closing ---.

    has_real_fm is False if:
      - File doesn't start with ---
      - No closing --- found
      - First non-blank line inside the block starts with # (markdown H1 = malformed FM)
      - Block parses but doesn't yield a dict

    When has_real_fm is False, fm_block_lines is [] and body is the FULL text
    (preserves the malformed pseudo-FM as body content).
    """
    import yaml as _yaml
    if not text.startswith("---"):
        return [], text, False
    lines = text.split("\n")
    if len(lines) < 2:
        return [], text, False
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx is None:
        return [], text, False
    fm_block_lines = lines[1:end_idx]
    body = "\n".join(lines[end_idx + 1:])

    # Validation 1: first non-blank line must not start with # (would be a markdown H1)
    for ln in fm_block_lines:
        s = ln.strip()
        if not s:
            continue
        if s.startswith("#"):
            return [], text, False
        break

    # Validation 2: must parse as a YAML dict
    try:
        parsed = _yaml.safe_load("\n".join(fm_block_lines))
        if not isinstance(parsed, dict):
            return [], text, False
    except _yaml.YAMLError:
        return [], text, False

    return fm_block_lines, body, True


def _fm_existing_keys(fm_block_lines):
    """Return set of top-level keys already present in the lexical FM block."""
    keys = set()
    for line in fm_block_lines:
        # Top-level key: starts at column 0, has 'key:' or 'key: value'
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_\-]*)\s*:", line)
        if m:
            keys.add(m.group(1))
    return keys


def _serialize_new_keys(set_frontmatter):
    """Dump only the NEW keys to YAML text, return list of lines (no trailing newline)."""
    import yaml as _yaml
    if not set_frontmatter:
        return []
    yaml_text = _yaml.safe_dump(
        dict(set_frontmatter),  # ensure dict, not OrderedDict surprises
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    )
    return [ln for ln in yaml_text.rstrip("\n").split("\n")]


def _update_existing_keys(fm_block_lines, updates):
    """For each key in updates that already exists in fm_block_lines, replace its value lexically.

    Returns new fm_block_lines list. Keys not in fm_block_lines are NOT added here
    (caller handles those via _serialize_new_keys).

    Handles simple scalar updates only. If the existing key has a multi-line value
    (e.g. a list with - items, or |/> block scalar), we replace from the key line
    through the last continuation line. Continuation lines are those indented with
    spaces relative to column 0.
    """
    if not updates:
        return list(fm_block_lines)

    result = []
    i = 0
    while i < len(fm_block_lines):
        line = fm_block_lines[i]
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_\-]*)\s*:", line)
        if m and m.group(1) in updates:
            key = m.group(1)
            new_value = updates[key]
            # Skip continuation lines for this key
            j = i + 1
            while j < len(fm_block_lines):
                cont = fm_block_lines[j]
                if cont == "" or cont.startswith(" ") or cont.startswith("\t"):
                    j += 1
                else:
                    break
            # Insert replacement
            new_lines = _serialize_new_keys({key: new_value})
            result.extend(new_lines)
            i = j
            continue
        result.append(line)
        i += 1
    return result


def _remove_keys(fm_block_lines, keys_to_remove):
    """Remove top-level keys (and their continuation lines) from fm_block_lines."""
    if not keys_to_remove:
        return list(fm_block_lines)
    remove_set = set(keys_to_remove)
    result = []
    i = 0
    while i < len(fm_block_lines):
        line = fm_block_lines[i]
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_\-]*)\s*:", line)
        if m and m.group(1) in remove_set:
            # Skip this line and continuations
            j = i + 1
            while j < len(fm_block_lines):
                cont = fm_block_lines[j]
                if cont == "" or cont.startswith(" ") or cont.startswith("\t"):
                    j += 1
                else:
                    break
            i = j
            continue
        result.append(line)
        i += 1
    return result


def _rebuild_note(fm_block_lines, body, line_ending, had_real_fm):
    """Reconstruct the note text, preserving body and line endings exactly.

    fm_block_lines: list of raw FM lines (may be [] if no FM should be written)
    body: body string (with LF line endings; will be normalized at end)
    line_ending: '\\r\\n' or '\\n'
    had_real_fm: if True, body starts immediately after old FM block; if False,
                 the note may need extra blank-line separation from body
    """
    if not fm_block_lines:
        # No FM block — return body alone (with line endings normalized)
        out = body
    else:
        # Build: ---\nLINE\nLINE\n---\nBODY
        parts = ["---"]
        parts.extend(fm_block_lines)
        parts.append("---")
        # If body doesn't start with a blank line, add one
        body_part = body.lstrip("\n").lstrip("\r")
        joined = "\n".join(parts) + "\n\n" + body_part if body_part else "\n".join(parts) + "\n"
        out = joined
    if line_ending == "\r\n":
        out = out.replace("\r\n", "\n").replace("\n", "\r\n")
    return out


# BEEZLE_FN_UNUSED 2026-06-13: obsidian_propose_edit (no static refs in pipeline/)
def obsidian_propose_edit(
    note_path: str,
    set_frontmatter: Optional[Dict[str, Any]] = None,
    unset_frontmatter: Optional[List[str]] = None,
    body_replace: Optional[str] = None,
    body_append: Optional[str] = None,
    body_prepend: Optional[str] = None,
    body_replace_pattern: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Stage a lexically-preserving edit to an existing note for approval.

    Frontmatter edits use line-level injection/replacement — existing YAML lines
    are preserved byte-exact. New keys are appended. Updated keys are replaced
    lexically. Removed keys (and continuation lines) are deleted.

    If the file's existing FM block is malformed (e.g., contains markdown H1 like
    '# Title'), it's treated as body content and a fresh FM block is prepended.

    Operations: see module docstring. Body ops unchanged from prior version.
    """
    import hashlib
    import json as _json
    import random

    full_path = VAULT / note_path
    if not full_path.exists() or not full_path.is_file():
        return {"error": f"note_path does not exist: {note_path}"}

    ops_count = sum(1 for x in [set_frontmatter, unset_frontmatter, body_replace,
                                 body_append, body_prepend, body_replace_pattern] if x)
    if ops_count == 0:
        return {"error": "no operations specified"}

    raw = full_path.read_bytes()
    line_ending = _detect_line_ending(raw)
    text = raw.decode("utf-8", errors="replace")
    text_lf = text.replace("\r\n", "\n")
    before_md5 = hashlib.md5(raw).hexdigest()

    # ── Parse current state lexically ──
    fm_block_lines, body, had_real_fm = _split_frontmatter_lexical(text_lf)

    # ── Apply frontmatter operations ──
    set_fm = dict(set_frontmatter or {})
    unset_fm = list(unset_frontmatter or [])

    if not had_real_fm:
        # No real FM — build a fresh block from set_frontmatter only
        # (unset is a no-op since there's nothing to remove)
        new_fm_lines = _serialize_new_keys(set_fm) if set_fm else []
    else:
        # Real FM exists — split set_fm into updates vs additions
        existing_keys = _fm_existing_keys(fm_block_lines)
        updates = {k: v for k, v in set_fm.items() if k in existing_keys}
        additions = {k: v for k, v in set_fm.items() if k not in existing_keys}

        # 1. Update existing keys in place
        new_fm_lines = _update_existing_keys(fm_block_lines, updates)
        # 2. Remove unset keys
        new_fm_lines = _remove_keys(new_fm_lines, unset_fm)
        # 3. Append new keys at end
        if additions:
            addition_lines = _serialize_new_keys(additions)
            new_fm_lines = new_fm_lines + addition_lines

    # ── Apply body operations ──
    new_body = body
    if body_replace is not None:
        new_body = body_replace
    else:
        if body_replace_pattern is not None:
            find = body_replace_pattern.get("find", "")
            replace = body_replace_pattern.get("replace", "")
            if find and find in new_body:
                new_body = new_body.replace(find, replace, 1)
        if body_prepend is not None:
            sep = "\n\n" if new_body.lstrip() else ""
            new_body = body_prepend.rstrip("\n") + sep + new_body
        if body_append is not None:
            sep = "\n\n" if new_body.rstrip() else ""
            new_body = new_body.rstrip("\n") + sep + body_append.lstrip("\n")

    # ── Reconstruct ──
    new_text = _rebuild_note(new_fm_lines, new_body, line_ending, had_real_fm)
    new_bytes = new_text.encode("utf-8")
    after_md5 = hashlib.md5(new_bytes).hexdigest()

    # ── Idempotency ──
    if before_md5 == after_md5:
        return {"status": "no_change", "note_path": note_path, "before_md5": before_md5}

    # ── Stage proposal ──
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    rand = random.randint(10000, 99999)
    proposal_id = f"edit_{ts}_{rand}"
    proposal_dir = PROPOSALS_DIR / proposal_id
    proposal_dir.mkdir(parents=True, exist_ok=True)

    (proposal_dir / "before.md").write_bytes(raw)
    (proposal_dir / "after.md").write_bytes(new_bytes)

    # ── Build diff summary ──
    diff_parts = []
    if had_real_fm:
        existing_keys = _fm_existing_keys(fm_block_lines)
        fm_added = [k for k in set_fm if k not in existing_keys]
        fm_updated = [k for k in set_fm if k in existing_keys]
        fm_removed = [k for k in unset_fm if k in existing_keys]
        if fm_added:   diff_parts.append(f"+{len(fm_added)} FM keys: {fm_added}")
        if fm_updated: diff_parts.append(f"~{len(fm_updated)} FM keys: {fm_updated}")
        if fm_removed: diff_parts.append(f"-{len(fm_removed)} FM keys: {fm_removed}")
    else:
        if set_fm:
            diff_parts.append(f"+fresh FM block: {list(set_fm.keys())}")
    if body != new_body:
        diff_parts.append(f"body changed ({len(body)}→{len(new_body)} chars)")

    manifest = {
        "proposal_id": proposal_id,
        "kind": "edit",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "target_path": note_path,
        "target_full_path": str(full_path),
        "before_md5": before_md5,
        "after_md5": after_md5,
        "line_ending": "CRLF" if line_ending == "\r\n" else "LF",
        "had_real_fm": had_real_fm,
        "operations": {
            "set_frontmatter": set_frontmatter or {},
            "unset_frontmatter": unset_frontmatter or [],
            "body_replace": bool(body_replace is not None),
            "body_append": bool(body_append is not None),
            "body_prepend": bool(body_prepend is not None),
            "body_replace_pattern": body_replace_pattern or {},
        },
        "diff_summary": "; ".join(diff_parts) if diff_parts else "(no visible change)",
        "status": "staged",
    }
    (proposal_dir / "manifest.json").write_text(
        _json.dumps(manifest, indent=2, default=str), encoding="utf-8"
    )

    return {
        "proposal_id": proposal_id,
        "status": "staged",
        "target_path": note_path,
        "before_md5": before_md5,
        "after_md5": after_md5,
        "diff_summary": manifest["diff_summary"],
    }


def _apply_edit_proposal(proposal_dir: Path, manifest: Dict[str, Any]) -> Dict[str, Any]:
    """Apply an edit proposal staged by obsidian_propose_edit. Called by approve_obsidian_proposal."""
    import hashlib
    
    target_path = manifest["target_path"]
    target_full = Path(manifest["target_full_path"])
    expected_before_md5 = manifest["before_md5"]
    
    if not target_full.exists():
        return {"error": f"target file no longer exists: {target_path}"}
    
    # md5 guard: verify the file hasn't changed since proposal staged
    current_raw = target_full.read_bytes()
    current_md5 = hashlib.md5(current_raw).hexdigest()
    if current_md5 != expected_before_md5:
        return {
            "error": f"file changed since proposal staged. Expected md5 {expected_before_md5}, "
                     f"got {current_md5}. Aborting to prevent overwrite.",
            "proposal_id": manifest["proposal_id"],
            "target_path": target_path,
        }
    
    # Read after.md from the proposal dir
    after_file = proposal_dir / "after.md"
    if not after_file.exists():
        return {"error": f"after.md missing from proposal dir: {proposal_dir}"}
    
    new_bytes = after_file.read_bytes()
    after_md5_computed = hashlib.md5(new_bytes).hexdigest()
    if after_md5_computed != manifest["after_md5"]:
        return {
            "error": f"after.md md5 mismatch: expected {manifest['after_md5']}, got {after_md5_computed}",
        }
    
    # Write to live vault
    target_full.write_bytes(new_bytes)
    
    return {
        "status": "applied",
        "kind": "edit",
        "proposal_id": manifest["proposal_id"],
        "modified": target_path,
        "before_md5": expected_before_md5,
        "after_md5": after_md5_computed,
        "diff_summary": manifest.get("diff_summary", ""),
    }


# BEEZLE_FN_UNUSED 2026-06-13: approve_obsidian_proposal (no static refs in pipeline/)
def approve_obsidian_proposal(proposal_id: str) -> Dict[str, Any]:
    """Apply a staged Obsidian proposal to the live vault.
    
    Dispatches on manifest['kind']:
      - "note": create a new note (delegates to legacy applier)
      - "link": add a wikilink to an existing note
      - "edit": modify an existing note (frontmatter and/or body)
    
    After applying, moves the proposal dir to _proposals/_applied/ for audit trail.
    """
    import json as _json
    import shutil
    
    proposal_dir = PROPOSALS_DIR / proposal_id
    if not proposal_dir.exists():
        return {"error": f"proposal not found: {proposal_id}"}
    
    manifest_file = proposal_dir / "manifest.json"
    if not manifest_file.exists():
        return {"error": f"manifest.json missing: {proposal_id}"}
    
    try:
        manifest = _json.loads(manifest_file.read_text(encoding="utf-8"))
    except Exception as e:
        return {"error": f"manifest.json malformed: {e}"}
    
    kind = manifest.get("kind", "note")
    
    # Dispatch by kind
    if kind == "edit":
        result = _apply_edit_proposal(proposal_dir, manifest)
    elif kind == "link":
        result = _apply_link_proposal_legacy(proposal_dir, manifest)
    else:
        # Default: note creation (legacy behavior, preserved)
        result = _apply_note_proposal_legacy(proposal_dir, manifest)
    
    # If applied, move proposal dir to _applied/
    if result.get("status") == "applied":
        try:
            target_applied = APPLIED_DIR / proposal_id
            if target_applied.exists():
                shutil.rmtree(target_applied)
            shutil.move(str(proposal_dir), str(target_applied))
            result["moved_to"] = str(target_applied.relative_to(VAULT))
        except Exception as e:
            result["move_warning"] = f"could not move to _applied/: {e}"
    
    return result


def _apply_note_proposal_legacy(proposal_dir: Path, manifest: Dict[str, Any]) -> Dict[str, Any]:
    """Apply a 'note' kind proposal: copy staged note to live vault.
    
    This is the pre-Session 22 behavior, factored out for the dispatch refactor.
    """
    target_path = manifest["target_path"]
    target_full = Path(manifest["target_full_path"])
    staged_file_rel = manifest.get("staged_file", "")
    
    # Find the staged note inside proposal_dir (it's the .md mirroring target_path)
    staged_md = None
    for p in proposal_dir.rglob("*.md"):
        # Skip before.md/after.md from edit proposals
        if p.name in ("before.md", "after.md"):
            continue
        staged_md = p
        break
    
    if staged_md is None:
        return {"error": f"no staged .md found in {proposal_dir}"}
    
    # Don't clobber existing files for "note" kind
    if target_full.exists():
        return {
            "error": f"target already exists: {target_path}. "
                     f"Use kind=edit for modifying existing files.",
            "proposal_id": manifest["proposal_id"],
        }
    
    target_full.parent.mkdir(parents=True, exist_ok=True)
    target_full.write_bytes(staged_md.read_bytes())
    
    return {
        "status": "applied",
        "kind": "note",
        "proposal_id": manifest["proposal_id"],
        "applied_to": target_path,
    }


def _apply_link_proposal_legacy(proposal_dir: Path, manifest: Dict[str, Any]) -> Dict[str, Any]:
    """Apply a 'link' kind proposal: append wikilink to existing note.
    
    Pre-Session 22 behavior, factored out for the dispatch refactor.
    """
    from_note = manifest.get("from_note")
    to_note = manifest.get("to_note")
    edge_type = manifest.get("edge_type", "unknown")
    citation = manifest.get("citation", "")
    
    if not from_note or not to_note:
        return {"error": "manifest missing from_note or to_note"}
    
    from_full = VAULT / from_note
    if not from_full.exists():
        return {"error": f"from_note does not exist: {from_note}"}
    
    # Build the link snippet
    pin = f":{citation}" if citation else ""
    link_text = f"[[{to_note}|{edge_type}{pin}]]"
    
    # Append to from_note (preserving line endings)
    raw = from_full.read_bytes()
    line_ending = _detect_line_ending(raw)
    text = raw.decode("utf-8", errors="replace")
    text_lf = text.replace("\r\n", "\n")
    
    # Append under a "## Linked" section if absent, else just append
    if "## Linked" not in text_lf:
        text_lf = text_lf.rstrip() + "\n\n## Linked\n- " + link_text + "\n"
    else:
        text_lf = text_lf.rstrip() + "\n- " + link_text + "\n"
    
    if line_ending == "\r\n":
        text_out = text_lf.replace("\r\n", "\n").replace("\n", "\r\n")
    else:
        text_out = text_lf
    
    from_full.write_bytes(text_out.encode("utf-8"))
    
    return {
        "status": "applied",
        "kind": "link",
        "proposal_id": manifest["proposal_id"],
        "modified": from_note,
        "added_link": link_text,
    }


# ════════════════════════════════════════════════════════════════════
# CLI SELF-TEST
# ════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    print(f"VAULT: {VAULT}")
    print(f"INDEX_DIR: {INDEX_DIR}")
    print(f"PROPOSALS_DIR: {PROPOSALS_DIR}")
    print()
    print("Building wikilink index (default scope, may take ~4 min on cold start)...")
    payload = build_wikilink_index(verbose=True, force="--force" in sys.argv)
    print()
    print(json.dumps(payload["stats"], indent=2))
