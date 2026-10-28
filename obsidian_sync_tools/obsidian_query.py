"""
obsidian_query.py — Beezle Obsidian Layer B Query Engine

Loads all nodes from <home>\\obsidian_sync\\beezle_vault\\v2\\ into memory
on first call, parses YAML frontmatter, and provides search/walk operations.

Pure deterministic. No LLM. Returns structured dicts for MCP tools to format.

Usage:
    from obsidian_query import ObsidianIndex
    idx = ObsidianIndex()  # loads ~2 sec, cached after
    results = idx.search("FCRA furnisher", source_type="case", court="EDPA")
    chain = idx.trace_authority("fcra-furnisher-liability", target_court="EDPA")
    node = idx.get_node("seamans-v-temple-univ-ca3")
"""

from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from threading import RLock
from typing import Optional

# VAULT_ROOT: env-driven with WSL-safe default. The old "beezle_vault/v2"
# subdir was a phantom — never materialized in the live vault. Point at the
# actual vault root, <home>/obsidian_sync/.
import os as _os
_DEFAULT_ROOT = _os.environ.get("BEEZLE_ROOT", str(_eroot()))
VAULT_ROOT = Path(_DEFAULT_ROOT) / "obsidian_sync"

# Existing dirs (will be discovered dynamically too)
KNOWN_DIRS = [
    "statutes", "cases", "regulations", "rules",
    "constitutional", "historical", "uncategorized",
    "pub_laws", "pa_bills", "concepts", "doctrines",
    "procedures", "remedies", "requirements", "agencies",
    "courts", "processes", "triggers", "ladders",
    "access", "barriers", "enforcement", "immunity_ex",
    "failure_modes", "chains", "patterns", "cross_ref",
]


def _parse_yaml_frontmatter(text: str) -> tuple[dict, str]:
    """
    Minimal YAML frontmatter parser. Handles:
    - Top-level key: value pairs
    - List values (lines starting with -)
    - String values
    Does NOT handle nested dicts (we don't use them).

    Returns (frontmatter_dict, body_text)
    """
    if not text.startswith("---"):
        return {}, text

    end = text.find("\n---", 3)
    if end == -1:
        return {}, text

    yaml_block = text[3:end].strip()
    body = text[end + 4:].lstrip()

    fm = {}
    current_key = None
    current_list = None

    for line in yaml_block.split("\n"):
        if not line.strip():
            continue
        # List item
        if line.startswith("  - ") or line.startswith("- "):
            value = line.split("- ", 1)[1].strip()
            if current_list is not None:
                current_list.append(value)
            continue
        # Top-level key
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            if not value:
                # List or empty
                current_list = []
                fm[key] = current_list
                current_key = key
            else:
                fm[key] = value
                current_list = None
                current_key = key

    return fm, body


class ObsidianIndex:
    _instance = None
    _lock = RLock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._loaded = False
            return cls._instance

    def __init__(self, vault_root: Optional[Path] = None, eager: bool = True):
        if self._loaded:
            return
        self.vault_root = Path(vault_root) if vault_root else VAULT_ROOT
        self.nodes: dict[str, dict] = {}              # canonical_id -> node dict
        self.by_type: dict[str, list[str]] = defaultdict(list)
        self.by_court: dict[str, list[str]] = defaultdict(list)
        self.by_jurisdiction: dict[str, list[str]] = defaultdict(list)
        self.by_citation: dict[str, list[str]] = defaultdict(list)  # cited_id -> [nodes that cite it]
        self.text_index: dict[str, list[str]] = defaultdict(list)   # word -> [canonical_ids]
        self.loaded_at: float = 0.0
        self.load_errors: list[str] = []
        self._loaded = True
        if eager:
            self.load()

    def load(self) -> dict:
        """Walk the vault and build all indexes. Returns stats."""
        with self._lock:
            t0 = time.time()
            self.nodes.clear()
            self.by_type.clear()
            self.by_court.clear()
            self.by_jurisdiction.clear()
            self.by_citation.clear()
            self.text_index.clear()
            self.load_errors.clear()

            if not self.vault_root.exists():
                self.load_errors.append(f"vault_root does not exist: {self.vault_root}")
                return self._stats()

            files_seen = 0
            for md_file in self.vault_root.rglob("*.md"):
                files_seen += 1
                try:
                    self._index_file(md_file)
                except Exception as e:
                    self.load_errors.append(f"{md_file.name}: {e}")

            self.loaded_at = time.time()
            self._files_seen = files_seen
            self._load_seconds = self.loaded_at - t0
            return self._stats()

    def _index_file(self, path: Path):
        text = path.read_text(encoding="utf-8", errors="replace")
        fm, body = _parse_yaml_frontmatter(text)

        canonical_id = fm.get("canonical_id") or path.stem
        if not canonical_id:
            return

        node = {
            "canonical_id": canonical_id,
            "type": fm.get("type", "unknown"),
            "court": fm.get("court", "UNKNOWN"),
            "jurisdiction": fm.get("jurisdiction", "other"),
            "confidence": fm.get("confidence", "low"),
            "source_authority": fm.get("source_authority", ""),
            "file_stem": fm.get("file_stem", path.stem),
            "binds_courts": fm.get("binds_courts", []) if isinstance(fm.get("binds_courts"), list) else [],
            "cites": fm.get("cites", []) if isinstance(fm.get("cites"), list) else [],
            "hierarchy_tier": fm.get("hierarchy_tier"),
            "binds": fm.get("binds"),
            "binding_strength": fm.get("binding_strength"),
            "interpretive_weight": fm.get("interpretive_weight"),
            "vault_path": str(path),
            "directory": path.parent.name,
        }

        self.nodes[canonical_id] = node
        self.by_type[node["type"]].append(canonical_id)
        self.by_court[node["court"]].append(canonical_id)
        self.by_jurisdiction[node["jurisdiction"]].append(canonical_id)

        # Build reverse-citation index
        for cited in node["cites"]:
            self.by_citation[cited].append(canonical_id)

        # Build word index from canonical_id (cheap, indexes structured names)
        words = re.findall(r"[a-z0-9]+", canonical_id.lower())
        for w in words:
            if len(w) >= 3:
                self.text_index[w].append(canonical_id)

    def _stats(self) -> dict:
        return {
            "vault_root": str(self.vault_root),
            "loaded": self._loaded,
            "loaded_at": self.loaded_at,
            "load_seconds": getattr(self, "_load_seconds", 0),
            "files_seen": getattr(self, "_files_seen", 0),
            "total_nodes": len(self.nodes),
            "by_type": {k: len(v) for k, v in self.by_type.items()},
            "by_court": {k: len(v) for k, v in self.by_court.items()},
            "by_jurisdiction": {k: len(v) for k, v in self.by_jurisdiction.items()},
            "total_citations": sum(len(v) for v in self.by_citation.values()),
            "load_errors": len(self.load_errors),
        }

    # ------------------------------------------------------------------
    # PUBLIC API
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        if not self.nodes:
            self.load()
        return self._stats()

    def get_node(self, canonical_id: str) -> Optional[dict]:
        if not self.nodes:
            self.load()
        return self.nodes.get(canonical_id)

    def search(
        self,
        query: str,
        source_type: Optional[str] = None,
        court: Optional[str] = None,
        jurisdiction: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        """
        Search nodes by free-text query against canonical_id.
        Filters by type, court, jurisdiction.
        Returns ranked hits (binding > persuasive > unknown).
        """
        if not self.nodes:
            self.load()

        query_words = [w.lower() for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) >= 3]
        if not query_words:
            return []

        # Score by # of matching words
        scores: dict[str, int] = defaultdict(int)
        for w in query_words:
            for cid in self.text_index.get(w, []):
                scores[cid] += 1

        # Filter and collect
        hits = []
        for cid, score in scores.items():
            node = self.nodes.get(cid)
            if not node:
                continue
            if source_type and node["type"] != source_type:
                continue
            if court and node["court"] != court and court not in node.get("binds_courts", []):
                continue
            if jurisdiction and node["jurisdiction"] != jurisdiction:
                continue
            hits.append((score, node))

        # Sort: score DESC, then binding strength
        def rank_key(item):
            score, node = item
            tier = node.get("hierarchy_tier")
            tier_rank = -100 if tier == 0 else (100 if tier == -1 else 0)
            type_rank = {
                "constitutional_provision": 0,
                "case": 1,
                "statute": 2,
                "regulation": 3,
                "rule": 4,
                "doctrine": 5,
                "concept": 6,
                "pub_law": 7,
                "historical_document": 8,
                "unknown": 99,
            }.get(node["type"], 50)
            return (-score, tier_rank, type_rank)

        hits.sort(key=rank_key)
        return [h[1] for h in hits[:limit]]

    def find_by_court(
        self,
        court: str,
        source_type: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        """Find all nodes that BIND a given court."""
        if not self.nodes:
            self.load()

        results = []
        for cid, node in self.nodes.items():
            if court in node.get("binds_courts", []) or node["court"] == court:
                if source_type and node["type"] != source_type:
                    continue
                results.append(node)
                if len(results) >= limit:
                    break
        return results

    def trace_authority(
        self,
        concept_or_id: str,
        target_court: str,
        max_depth: int = 3,
    ) -> dict:
        """
        Walk the graph from a concept/citation and find binding authority chains
        for a target court.

        Returns:
            {
                "query": concept_or_id,
                "target_court": target_court,
                "binding_statutes": [...],
                "binding_cases": [...],
                "binding_constitutional": [...],
                "supporting_doctrines": [...],
                "depth_explored": int,
            }
        """
        if not self.nodes:
            self.load()

        result = {
            "query": concept_or_id,
            "target_court": target_court,
            "binding_statutes": [],
            "binding_cases": [],
            "binding_constitutional": [],
            "binding_regulations": [],
            "binding_rules": [],
            "supporting_doctrines": [],
            "supporting_concepts": [],
            "depth_explored": 0,
        }

        # Find seed nodes that match the concept
        seeds = []
        # Direct ID match
        if concept_or_id in self.nodes:
            seeds.append(concept_or_id)
        # Word search
        seeds.extend([n["canonical_id"] for n in self.search(concept_or_id, limit=20)])
        # Citation match (someone cites this ID)
        if concept_or_id in self.by_citation:
            seeds.extend(self.by_citation[concept_or_id])

        seeds = list(dict.fromkeys(seeds))[:15]  # dedupe, cap

        # BFS walk
        visited: set[str] = set()
        frontier = list(seeds)

        for depth in range(max_depth):
            if not frontier:
                break
            result["depth_explored"] = depth + 1
            next_frontier = []

            for cid in frontier:
                if cid in visited:
                    continue
                visited.add(cid)
                node = self.nodes.get(cid)
                if not node:
                    continue

                # Is this node binding on target_court?
                is_binding = (
                    target_court in node.get("binds_courts", [])
                    or node["court"] == target_court
                    or node.get("hierarchy_tier") == 0  # Constitution binds all
                )

                node_type = node["type"]
                bucket = None
                if node_type == "case" and is_binding:
                    bucket = "binding_cases"
                elif node_type == "statute":
                    bucket = "binding_statutes"  # statutes always apply
                elif node_type == "constitutional_provision":
                    bucket = "binding_constitutional"
                elif node_type == "regulation":
                    bucket = "binding_regulations"
                elif node_type == "rule":
                    bucket = "binding_rules"
                elif node_type == "doctrine":
                    bucket = "supporting_doctrines"
                elif node_type == "concept":
                    bucket = "supporting_concepts"

                if bucket:
                    result[bucket].append({
                        "canonical_id": node["canonical_id"],
                        "court": node["court"],
                        "confidence": node["confidence"],
                        "binds_courts": node.get("binds_courts", []),
                        "cites_count": len(node.get("cites", [])),
                    })

                # Expand: add cited nodes and citing nodes
                for cited in node.get("cites", []):
                    if cited not in visited:
                        next_frontier.append(cited)
                for citer in self.by_citation.get(cid, []):
                    if citer not in visited:
                        next_frontier.append(citer)

            frontier = next_frontier

        # Cap each bucket
        for k in ["binding_cases", "binding_statutes", "binding_constitutional",
                  "binding_regulations", "binding_rules", "supporting_doctrines",
                  "supporting_concepts"]:
            result[k] = result[k][:10]

        result["total_nodes_visited"] = len(visited)
        return result

    def reload(self) -> dict:
        """Force re-read of vault."""
        return self.load()


# Module-level singleton accessor
_INDEX: Optional[ObsidianIndex] = None


def get_index(eager: bool = True) -> ObsidianIndex:
    global _INDEX
    if _INDEX is None:
        _INDEX = ObsidianIndex(eager=eager)
    return _INDEX


if __name__ == "__main__":
    # Self-test
    idx = ObsidianIndex()
    stats = idx.stats()
    print("=" * 60)
    print("ObsidianIndex self-test")
    print("=" * 60)
    print(json.dumps(stats, indent=2, default=str))
    print()
    print("Sample search for 'fcra':")
    for hit in idx.search("fcra", limit=5):
        print(f"  - {hit['canonical_id']} ({hit['type']}, {hit['court']})")
