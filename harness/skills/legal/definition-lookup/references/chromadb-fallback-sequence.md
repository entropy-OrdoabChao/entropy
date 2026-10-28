# ChromaDB Fallback Sequence for definition_lookup

When `mcp_beezle_definition_lookup` returns ChromaDB-ranked content with a `scope` that does NOT match your target statute/domain, the definition is irrelevant — the embedding matched on a common phrase, not the correct statute.

## Fallback sequence (in order)

### 1. source_fetch → read_file (for statutes/regulations)
`mcp_beezle_source_fetch` can retrieve the primary statute directly. In some cases the file IS locally readable via `read_file` (e.g., `/obsidian_sync/primary_sources/federal/usc_26_sec_6331.md`). Try reading the returned path — if it works, you have the authoritative text.

### 2. source_fetch (read-only, if file not readable)
If `read_file` fails on the source_fetch path, use `source_fetch` directly for the content. The pitfall is that `source_fetch` reports a path but the file may not exist locally.

### 3. search_chunks with source-specific query
Query the corpus with a query that includes the specific statute citation (e.g., "IRC 6331(b) seizure" rather than just "seizure"). This narrows results to the correct source.

### 4. Direct statute URL fetch
If the corpus has no relevant results, fetch from the authoritative source:
- USC: https://uscode.house.gov/view.xhtml?req=granuleid:USC-prelim-title26-section6331
- CFR: eCFR HTML page (API returns 406)
- PA Code: via `mcp_beezle_source_fetch` for PA Code

## Example: "seizure" under IRC
`mcp_beezle_definition_lookup(term="seizure", statute_scope="IRC 6331")` returned maritime territorial waters content — scope was wrong. Fallback: `source_fetch(source="usc", params={"title": 26, "section": "6331"})` → read_file → found IRC §6331(b): "The term 'levy' as used in this title includes the power of distraint and seizure by any means."

## When ChromaDB-ranked results ARE relevant
Before falling through, check the `scope` field in the ChromaDB result. If it matches your target statute, the result may be correct — but verify by reading the source text to confirm the definition is accurate.
