---
name: definition-lookup
description: Look up legal term definitions using the Beezle tiered lookup engine. Use for finding authoritative definitions of legal terms across statutes, regulations, and case law.
---

# Definition Lookup

## Purpose
Look up legal term definitions using the Beezle tiered lookup engine. Use for finding authoritative definitions of legal terms across statutes, regulations, and case law.

## When to use
- "Define X" / "what does X mean legally"
- "Find the definition of Y" / "look up Z"
- Any task requiring an authoritative definition of a legal term
- Building the legal comprehension graph (frontier terms)

## How to use
```bash
# Use the MCP tool directly
mcp_beezle_definition_lookup(term="adverse claim", statute_scope="UCC 3-603")
```

## Tiered Fallback
The engine uses priority-ranked sources:
1. Explicit candidates from caller (if provided)
2. CORE_DEFINITIONS (litigation terms catalog)
3. definitions_learned/ cache (prior cron-built records)
4. ChromaDB retrieval (statutes, dicts, case law - all corpora)

## Pitfalls
- **ChromaDB returns commentary, not statutory text** — ChromaDB indexing covers case law commentary, law review articles, and treatise excerpts — but the actual statutory text for UCC provisions and other statutes is NOT in ChromaDB. If ChromaDB search returns results from law review articles or court opinions, those are commentary, not the actual statutory language.
- **ChromaDB returns completely unrelated content** — When searching for terms like "consumer report" in ChromaDB, the embedding can match on a completely different domain (e.g., bank loan tables where "consumer" refers to consumer loans, not the FCRA term). This is NOT commentary — it's non-statutory financial data that shares a keyword. The ChromaDB result was bank loan data for "consumer report," not the FCRA definition. Always verify the result is actually the statute you're looking for, not just a keyword match in a different context.
- **MCP definition_lookup can return irrelevant results** — When `mcp_beezle_definition_lookup` returns a result with tier `chromadb_ranked`, the definition may be from a completely unrelated statute (e.g., searching for "negligent compliance" under FCRA returned NRCS contract text). Verify the `scope` field matches your target statute. If the scope doesn't match, fall back to primary sources directly.
- **Term not separately defined — unsealing pattern** — When a term is NOT separately defined in the target regulation/statute, its scope is governed by the controlling definition of the bounding term. Example: "fiduciary activities" is not separately defined in 12 C.F.R. Part 9; its scope is determined by the definition of "fiduciary capacity" in § 9.2(e), which covers trustee, executor, administrator, registrar, transfer agent, guardian, assignee, receiver, custodian under uniform gifts to minors act, investment adviser (for fee), investment discretion on behalf of another, or any similar capacity authorized by the OCC under 12 U.S.C. 92a. See `references/unsealed-terms-pattern.md`.
- **PA Code files aren't stored locally** — the `mcp_beezle_source_fetch` tool retrieves PA Code dynamically, but `read_file` will fail because the files don't exist as local files. Always use `source_fetch` for PA Code.
- **source_fetch returns paths that don't exist locally** — `mcp_beezle_source_fetch` reports a file path, text_length, and sha1, but `read_file` fails because the file was never written to disk. This is NOT a one-off index corruption — it happened with BOTH `cfr_12_part_9_sec_9.2.md` AND `usc_12_sec_92a.md` in the same session. The source_fetch tool fetches the content from the web and reports it as available locally, but the file is never actually persisted. Always attempt to read the returned path; if it fails, use `source_fetch` directly or the eCFR/USCode API instead.
- **eCFR API returns 406 Unsupported format** — The eCFR API at `https://www.ecfr.gov/api/v1/` rejects requests with 406 (Unsupported format) even with `Accept: application/json` headers. The API endpoint is not usable for programmatic access. Fetch the HTML page directly with a browser User-Agent header instead, or use `source_fetch` which handles the retrieval. See `references/ecfr-api-406.md`.
- **Definition-lookup tool vs skill** — the MCP tool `mcp_beezle_definition_lookup` exists and works; the skill is just a wrapper for how to use it.

## Related Tools
- `mcp_beezle_definition_validate` — validate a definition against banned behaviors
- `mcp_beezle_definition_validate_import` — validate that a definition isn't being illegally imported from one statute to another
- `mcp_beezle_check_definition_scope` — classify a statutory definition as closed (means) or expansive (includes)

## Support Files
- `references/unsealed-terms-pattern.md` — how to handle terms that are NOT separately defined but whose scope is governed by the bounding term's controlling definition
- `references/fcra_statutory_sources.md` — Primary source locations for FCRA statutory text (USCODE_LII directory and source_fetch)
- `references/ecfr-api-406.md` — The eCFR API endpoint returns 406 on all requests; use HTML page fetch with User-Agent or source_fetch instead
- `references/chromadb-fallback-sequence.md` — Ordered fallback sequence when ChromaDB-ranked results have wrong scope: source_fetch → read_file → search_chunks → direct URL fetch
