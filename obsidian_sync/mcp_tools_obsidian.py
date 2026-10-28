"""
mcp_tools_obsidian — Layer B Obsidian / authority-vault MCP tools.

Backs onto pipeline.authority_resolver and the 58k-citation index at
obsidian_sync/primary_sources/_index/.

Exposes 5 tools when register_obsidian_tools(mcp) is called:
  - obsidian_index_stats: index health, coverage by jurisdiction/authority
  - trace_legal_authority: resolve a citation string to vault nodes
  - search_obsidian: keyword search across resolved citations
  - list_authorities_by_jurisdiction: nodes for a given jurisdiction
  - list_authorities_by_level: nodes for a given authority_level

Each tool is a thin, deterministic wrapper. No network calls. No LLM.
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

from typing import Optional
import sys
from pathlib import Path

_BEEZLE_ROOT = _eroot()
if str(_BEEZLE_ROOT) not in sys.path:
    sys.path.insert(0, str(_BEEZLE_ROOT))

from pipeline import authority_resolver as _ar


def register_obsidian_tools(mcp) -> int:
    """Attach Layer B tools to the FastMCP instance. Return tool count."""

    @mcp.tool
    def obsidian_index_stats() -> dict:
        """Return health/coverage stats for the Obsidian primary-sources citation index.

        Index lives at obsidian_sync/primary_sources/_index/ and is built by
        pipeline.authority_resolver.build_index. Returns build timestamp,
        node counts, coverage_pct, breakdown by jurisdiction and
        authority_level. Use this before trace_legal_authority to confirm
        the index is present and sized as expected.
        """
        try:
            return _ar.index_stats()
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

    @mcp.tool
    def trace_legal_authority(
        citation: str,
        jurisdiction: Optional[str] = None,
        limit: int = 10,
    ) -> dict:
        """Resolve a legal citation to matching nodes in the primary-sources vault.

        citation: a citation string. Examples that resolve well:
          - "15 U.S.C. § 1681"   (FCRA)
          - "42 U.S.C. § 1983"
          - "Pa.R.C.P. 4019"
          - "12 U.S.C. § 2605"   (RESPA)
        Citations that won't resolve unless the underlying opinion has been
        ingested: "Westinghouse", "Calderon v. Thompson, 523 U.S. 538".

        jurisdiction: optional filter — 'federal', 'pennsylvania', 'PA'.
        limit: max nodes returned (default 10).

        Returns {citation, jurisdiction, count, nodes}. Each node has
        path, title, jurisdiction, authority_level, matched_token.
        Empty nodes list means the citation isn't in the indexed vault;
        consult obsidian_index_stats to see what jurisdictions are covered.
        """
        try:
            nodes = _ar.resolve_citation(citation, jurisdiction=jurisdiction, limit=limit) or []
            return {
                "citation": citation,
                "jurisdiction": jurisdiction,
                "count": len(nodes),
                "nodes": nodes,
            }
        except Exception as e:
            return {
                "citation": citation,
                "jurisdiction": jurisdiction,
                "count": 0,
                "nodes": [],
                "error": f"{type(e).__name__}: {e}",
            }

    @mcp.tool
    def search_obsidian(query: str, limit: int = 20) -> dict:
        """Keyword-search the citation index by substring match.

        query: any substring. Matches against the index's citation keys
        (e.g. '1681', 'Pa.R.C.P.', '42 U.S.C.'). Returns up to `limit`
        unique citations and a count of how many nodes each maps to.
        For full node detail on a specific citation, follow up with
        trace_legal_authority.
        """
        try:
            import json
            idx_path = _BEEZLE_ROOT / "obsidian_sync/primary_sources/_index/by_citation.json"
            if not idx_path.exists():
                return {"query": query, "count": 0, "matches": [],
                        "error": "by_citation.json not found — run authority_resolver.build_index"}
            with idx_path.open("r", encoding="utf-8") as f:
                idx = json.load(f)
            q = query.lower().strip()
            matches = []
            for cite_key, node_paths in idx.items():
                if q in cite_key.lower():
                    matches.append({"citation": cite_key, "node_count": len(node_paths)})
                    if len(matches) >= limit:
                        break
            matches.sort(key=lambda m: -m["node_count"])
            return {"query": query, "count": len(matches), "matches": matches[:limit]}
        except Exception as e:
            return {"query": query, "count": 0, "matches": [],
                    "error": f"{type(e).__name__}: {e}"}

    @mcp.tool
    def list_authorities_by_jurisdiction(jurisdiction: str, limit: int = 50) -> dict:
        """List vault node paths for a given jurisdiction.

        jurisdiction: 'federal', 'pennsylvania', 'PA', 'unknown'. Use
        obsidian_index_stats to see counts per jurisdiction first; the
        'unknown' bucket holds the most nodes because many files lack
        explicit frontmatter. Returns up to `limit` paths.
        """
        try:
            paths = _ar.list_jurisdiction(jurisdiction) or []
            return {"jurisdiction": jurisdiction, "total": len(paths),
                    "paths": paths[:limit], "limited_to": limit}
        except Exception as e:
            return {"jurisdiction": jurisdiction, "total": 0, "paths": [],
                    "error": f"{type(e).__name__}: {e}"}

    @mcp.tool
    def list_authorities_by_level(authority_level: str, limit: int = 50) -> dict:
        """List vault node paths for a given authority_level.

        authority_level: 'AGENCY_GUIDANCE', 'CONSTITUTION', 'COURT_RULE',
        'TREATY', 'CASE_LAW', 'UNKNOWN'. Check obsidian_index_stats for
        counts. Returns up to `limit` paths.
        """
        try:
            paths = _ar.list_authority_level(authority_level) or []
            return {"authority_level": authority_level, "total": len(paths),
                    "paths": paths[:limit], "limited_to": limit}
        except Exception as e:
            return {"authority_level": authority_level, "total": 0, "paths": [],
                    "error": f"{type(e).__name__}: {e}"}

    return 5
