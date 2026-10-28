# ChromaDB Definition Extraction — 2026-06-22

## Purpose
Quick reference for extracting legal definitions from ChromaDB and writing them to the Obsidian vault.

## Workflow (summary)

1. **Query ChromaDB** — `mcp_beezle_search_chunks(query="term definition", n_results=5)`
2. **If empty, try `mcp_beezle_resolve_citation()`** — with `include_text=true`
3. **Write to Obsidian** — `{{ENTROPY_HOME}}/obsidian_sync/definitions_learned/<term>.md`
4. **Verify** — `ls {{ENTROPY_HOME}}/obsidian_sync/definitions_learned/ | wc -l`

## Pitfall
ChromaDB text content may be empty from `mcp_beezle_resolve_citation()` — use `mcp_beezle_search_chunks()` instead for text.

## See also
- `beeze-legal-construction/references/chromadb-definition-extraction.md` — full workflow with definitions
