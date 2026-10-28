# lookup_authority Reliability — Verified Failures

Verified 2026-07-30: `lookup_authority` returns 0 hits for major citations that should be indexed.

## Failed lookups (0 hits each)
- 42 USC 1983
- 15 USC 1681

## Implication
`lookup_authority` is NOT reliable for citation-based lookups. Not all citations are indexed, and major ones fail.

## Alternatives
- Use `search_text` with the citation as the query string
- Use MCP `resolve_citation` for authoritative resolution
- Use MCP `trace_legal_authority` for Obsidian vault resolution
