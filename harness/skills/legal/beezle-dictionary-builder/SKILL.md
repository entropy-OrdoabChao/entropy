---
name: beezle-dictionary-builder
category: legal
description: Process legal term definition requests from queries/definitions/ queue using 4-tier lookup engine; write authoritative definitions or NOT_FOUND stubs
triggers: [cron_job, manual_queue_processing, term_definition_request]
dependencies: [mcp_beezle_definition_lookup, mcp_beezle_search_chunks, mcp_beezle_write_obsidian_note]
---

# beezle-dictionary-builder

**Purpose:** Process legal term definition requests from `queries/definitions/` queue using Beezle's 4-tier lookup engine. Write authoritative definitions to `definitions_learned/` or NOT_FOUND stubs. Move processed queries to `processed/` subfolder.

**Trigger:** Scheduled cron job, manual request to process `queries/definitions/` queue, or any task requiring term definition lookup.

## Workflow

### 1. Queue Discovery
**Primary:** PowerShell `Get-ChildItem` (see example below). **Warning:** Terminal often fails on Windows (exit 126).

**Alternative:** If terminal fails, use `mcp_beezle_list_obsidian_notes(subdir="queries/definitions")` and filter results for files NOT containing `processed/` in path AND without `status: processed` in frontmatter. See `references/queue-discovery-alternatives.md`.

```powershell
# List pending requests: exclude processed subfolder AND files already marked status: processed
Get-ChildItem "{{ENTROPY_HOME}}\obsidian_sync\queries\definitions\*.md" -ErrorAction SilentlyContinue | 
Where-Object { $_.Name -notmatch "processed" } | 
ForEach-Object { 
    $content = Get-Content $_.FullName -Raw
    if ($content -notmatch '^status:\s*processed') { $_ }
}
```

### 2. Parse Request (per file)
Read YAML frontmatter:
- `term`: The legal term to define (e.g., "negotiable instrument", "standing")
- `scope`: Jurisdictional scope (e.g., "federal", "ucc", "treasury", "civil_procedure")
- `requested_by`: Username who created the request
- `requested_at`: ISO timestamp

### 3. 4-Tier Lookup Sequence
Call `mcp_beezle_definition_lookup` with `term` and `statute_scope`:

**Tier 1:** `chromadb_ranked` — ChromaDB vector search over ingested legal corpus (SCOTUS, statutes, Black's Law Dictionary, etc.). Returns `has_term: true/false`, `priority_rank` (1=highest), `source`.

**Tier 2:** `definitions_learned_cache` — If Tier 1 fails or returns `has_term: false`, check if a definition was previously cached for this term/scope combination.

**Tier 3:** `web_fallback` — If Tiers 1-2 fail, scrape Cornell LII (for UCC/statutes) or Black's Law Dictionary online. Validate before write-back using `scraper-validation` skill.

**Tier 4:** `NOT_FOUND` — If all tiers fail, create NOT_FOUND stub.

### 4. Write Decision

**Valid Definition Found:**
- Write to: `{{ENTROPY_HOME}}\obsidian_sync\definitions_learned\{term_snake}__{scope}.md`
- Format: YAML frontmatter (term, scope, source, source_type, priority_rank, chunk_id, built_at, built_by) + markdown body with quoted definition and provenance section
- **Locked status:** Other Beezle agents must consult this entry before drafting; do not modify without re-running lookup and confirming new chunk ranks equal or higher

**NOT_FOUND (No definition in corpus):**
- Write to: `{{ENTROPY_HOME}}\obsidian_sync\definitions_learned\{term_snake}__{scope}__NOT_FOUND.md`
- Document: "No authoritative definition found in legal corpus (statutes, regulations, case law, Black's Law, Bouvier's)."
- Include retrieval details: candidates examined, best match score, source examined
- **Flag:** This term should not be used in drafting until valid definition discovered

**DB Error (Ingestion corruption):**
- Write NOT_FOUND stub with `tier: db_error`
- Include error message (e.g., `"'int' object has no attribute 'strip'"`)
- **Flag:** Requires ChromaDB re-ingestion or manual research

**Queue Hygiene**

**Tier 1/3 (New Definition Written):**
- After successfully writing to `definitions_learned/`, move the query file to `queries/definitions/processed/` to clear the queue
- Terminal commands: `powershell -Command "Move-Item 'source.md' 'destination/'"`
- **Note:** Terminal often returns exit code 126 on Windows. If terminal fails, overwrite the query file with a processed marker (see Tier 2 below)

**Tier 2 (Cache Hit - No New File Written):**
- No new definition file is created. Overwrite the query file with a processed marker using `mcp_beezle_write_obsidian_note` (overwrite: true)
- Do not move the file — moving is unnecessary when no definition file was created
- Content includes: `status: processed`, `tier: definitions_learned_cache`, `cached_at` timestamp, reference to existing definition
- **Why Overwrite:** Terminal is unreliable on Windows; Tier 2 consumes no external API quota; no benefit to file system operations when definition already exists

**Corpus Gaps:**
- See `references/corpus-gaps.md` for known terms that return NOT_FOUND due to ingestion bugs or scope mismatches
- See `references/queue-hygiene-issues.md` for stale processed file issues in the queue

## Pitfalls

**Terminal Unavailable:**
- `terminal` tool may return exit code 126 (command not found) or fail on Windows. Do not rely on it for queue hygiene.
- Use the overwrite workaround: `mcp_beezle_write_obsidian_note` with `overwrite: true` to write processed marker to original query file.

**False Positives in ChromaDB:**
- `has_term: false` means the chunk does not contain the requested term — treat as NOT_FOUND, do not write definition
- `priority_rank: 99` indicates low-quality match; verify before using
- Source type `unknown` requires validation before write-back

**Scope Mismatch:**
- Terms may exist in corpus but under different scope (e.g., "lawful money" exists in federal case law but query uses "treasury" scope)
- Solution: If lookup fails with scope X, check if term exists in broader "federal" scope, but document the scope mismatch

**Cron Job Constraints:**
- Process one term per run (avoid rate limiting ChromaDB). **Exception:** Cache hits (Tier 2) consume no external API quota and can be batched — process all pending cache hits in a single run to reduce overhead.
- If queue is empty, return `[SILENT]` — do not generate empty reports
- Terminal commands require `powershell -Command` wrapper on Windows
- **Terminal Unreliable:** Terminal often returns exit code 126 (command not found) on Windows; rely on `mcp_beezle_write_obsidian_note` overwrite workaround for queue hygiene

**NOT_FOUND Stubs Are Permanent:**
- Once a NOT_FOUND stub is cached, re-running lookup may return the cached NOT_FOUND (tier 2 hit) instead of re-querying DB
- To force re-search: Delete the NOT_FOUND stub file first, or use `force_recalc: true` if tool supports it

**Corpus Gaps:**
- `judicial immunity` (federal): SCOTUS case law not ingested properly into ChromaDB
- `standing` (federal): DB error on lookup — ingestion pipeline bug
- `federal reserve note` (treasury): Term not defined in Treasury regulations; exists only in Federal Reserve Act (not in scope)

## Tool Calls

**Primary:** `mcp_beezle_definition_lookup` (term, statute_scope)
**Fallback:** `mcp_beezle_search_chunks` (query, n_results=5) — for manual corpus inspection when automatic lookup fails
**Write:** `mcp_beezle_write_obsidian_note` (path, content) — write definition or NOT_FOUND stub
**Queue Hygiene:** `terminal` (powershell Move-Item/Remove-Item) — move processed files; OR `mcp_beezle_write_obsidian_note` (overwrite: true) — mark as processed if terminal fails

## Verification

After processing N terms:
1. Verify `definitions_learned/` contains N new files (or N NOT_FOUND stubs)
2. Verify `queries/definitions/` has N fewer files (moved to `processed/`)
3. Spot-check one definition file: Confirm YAML frontmatter matches lookup result (source, rank, chunk_id)
4. Verify NOT_FOUND stubs contain the specific error or corpus gap explanation

**Next Session:** Process next pending term from queue. If queue empty, return `[SILENT]`.