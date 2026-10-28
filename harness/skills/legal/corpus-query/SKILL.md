---
name: corpus-query
description: Query the beezle-corpus Docker service for the 35.8GB legal text corpus
when_to_use: When you need to find legal text, definitions, or authority from the corpus DB
grounding: grounded
related_skills: [corpus-query-timeout-handling, corpus-fallback-strategy, beezle-legal-research, corpus-grounder]
---

# Corpus Query — Beezle Corpus DB

## Overview

The corpus lives inside the beezle-corpus Docker container on port 8100. It provides three endpoints.

**Corpus size: 36,683 MB (35.8 GB)** (verified 2026-07-31 via `/status`).

## Endpoints

### 1. Free-text search (FTS)

    GET http://localhost:8100/search_text?query=QUERY&top_k=N

- query: search string (space-separated, no + — use spaces)
- top_k: number of results (1-50, default 5)
- Returns: {"query": "...", "count": N, "hits": [...]}
- Each hit has: term, text (excerpt), source, score, chunk_id, metadata

### 2. Term + alias lookup

    GET http://localhost:8100/lookup_term?term=TERM&max_hits=N

- term: exact or partial term to look up
- max_hits: number of results (1-50, default 10)
- Returns: {"term": "...", "count": N, "hits": [...]}
- Best for: finding definitions of legal terms

### 3. Authority by citation

    GET http://localhost:8100/lookup_authority?cite=CITATION

- cite: legal citation (e.g., "42 USC 1983", "15 USC 1681")
- Returns: {"cite": "...", "count": N, "hits": [...]}
- Best for: finding the actual statutory/regulatory text for a citation

## Key constraints

- Never query without a specific term/citation — the corpus is 35.8 GB, broad
  queries return noise
- Use top_k=5 by default — more results = more context = slower
- FTS is the most powerful — free-text search across all corpus text
- /lookup_authority may return 0 hits — not all citations are indexed yet
- Always verify results — check source field to know if it is from FTS, vector,
  or doctrine index
- **MCP vs HTTP distinction**: `search_chunks` is MCP-only (HTTP endpoint returns 404).
  The three HTTP endpoints (`/search_text`, `/lookup_term`, `/lookup_authority`) are
  the ONLY HTTP endpoints. Do NOT curl `search_chunks` — use the Beezle MCP client.
- **`lookup_authority` is unreliable**: Returns 0 hits for major citations including
  42 USC 1983 and 15 USC 1681 (verified 2026-07-30). Not all citations are indexed.
  For citation-based lookups, prefer `search_text` with the citation as query, or
  use MCP `resolve_citation`.
- **Beezle-legal-research conflict**: That skill says "NEVER curl localhost:8100" —
  this is wrong. The HTTP endpoints are valid and live (verified 2026-07-30).
  Use HTTP for simple lookups; use MCP `search_chunks` for semantic search.
  Both are correct — they serve different purposes.

## Workflow pattern

1. Start with the corpus — before loading any MCP tools, query the corpus for
   the legal text
2. Then use MCP tools — once you have the corpus data, use Beezle MCP for
   workflow (case analysis, filing, etc.)
3. Cross-reference — if corpus says one thing and MCP says another, investigate

## Example queries

- Definition of furnisher under FCRA:
  GET /lookup_term?term=furnisher
- FCRA statutory text:
  GET /search_text?query=FCRA 15 USC section 1681
- Section 1983 citation:
  GET /lookup_authority?cite=42 USC 1983
- Bad faith remedy:
  GET /search_text?query=42 PaCS section 8371 bad faith remedy
- Garnishment procedure:
  GET /search_text?query=garnishment procedure Pa.R.C.P.

## Python wrapper (faster)

Instead of typing curl commands, use the Python wrapper:

    from corpus_client import CorpusClient
    client = CorpusClient()
    results = client.search_text("FCRA remedy", top_k=10)
    term = client.lookup_term("furnisher")
    authority = client.lookup_authority("42 USC 1983")

The wrapper is at:
`/home/{{USER_FIRST}}/.hermes/skills/legal/corpus-query/scripts/corpus_client.py`

It also includes:
- `search_with_expansion()` — tries multiple queries if the first returns few results
- `search_multiple_terms()` — searches for multiple terms and merges results
- `status()` — health check

## Quick reference: definition extraction

For extracting legal definitions from ChromaDB and writing them to the Obsidian vault, see:
`references/chromadb-definition-extraction-quickref.md`

For the full workflow with definition file format, see:
`beeze-legal-construction/references/chromadb-definition-extraction.md`

## Query expansion patterns

The corpus may not have everything indexed. Use query expansion to fill gaps:

    # Try base query + expansions if few results
    client.search_with_expansion("FCRA remedy", top_k=10, expansions=["damages", "injunctive relief", "attorney fees"])

    # Search for multiple terms
    client.search_multiple_terms(["FCRA", "furnisher", "consumer"], top_k=5)

## Court Local Rules Ingest Pattern (EDPA 2026-07-04)

When a court's local rules are NOT in the corpus (confirmed by **`search_chunks` via MCP** returning only noise), the ingest pattern is:

**NOTE:** `search_chunks` is MCP-only (HTTP endpoint returns 404). Do NOT curl it. Use the Beezle MCP client.

1. **Fetch the official PDF** from the court's website (e.g., paed.uscourts.gov → Local Rules page)
2. **Extract text** via `pdftotext` on the local copy
3. **Split by rule boundaries** using regex: `re.split(r'(?=Rule\s+\d+)', text)` — each chunk becomes one rule
4. **Insert into chunk_text.db** with `source=edpa_local_rules`, `jurisdiction=EDPA`, and `citations` as JSON array of rule citations
5. **Turbovec ingest**: `python3 -m pipeline.turbovec_ingest` — adds vectors to new index
6. **Verify**: `python3 -m pipeline.turbovec_ingest --search "Local Rule 7.1"` — confirm high-confidence hit

**CRITICAL**: SQL insert uses `INSERT OR IGNORE` to avoid duplicates on re-runs. Always verify post-insert count before declaring success.

**Where this applies**: Any court local rules not in corpus — EDPA, other federal districts, state court local rules.

## Support Files

- `references/chromadb-api-fallback-pattern.md` — Diagnostic steps for when the raw ChromaDB HTTP API returns 404 but Beezle MCP tools may still work
- `references/search_chunks-reality-check.md` — Live-verified: `search_chunks` is MCP-only (HTTP 404). Do NOT curl it.

<!-- BEEZLE-BRIDGE-START -->
## 🔗 Wired to Beezle (synced 2026-07-30)

*This section is auto-generated by `_sync_hermes_beezle_v2.py`. Everything
above this marker is Hermes's own work and is preserved verbatim. The
Beezle modules below are the production implementations that execute what
this skill describes.*

**Category**: `infrastructure`

**Primary Beezle module**: `pipeline.corpus_lookup`

**Direct invocation**:

```bash
cd {{ENTROPY_HOME}}
python3 -m pipeline.corpus_lookup
```

**Also uses**:

- `pipeline.corpus_ingest`
- `pipeline.retriever`
- `pipeline.turbovec_search`



**Registry entry**: NOT IN CORPUS — `{{ENTROPY_HOME}}/skills/registry.json` key `corpus-query` does not exist (verified 2026-07-24). Remove this claim.

**Master dispatcher**: `pipeline.chain_runner` (122 KB) — the production
orchestrator that calls all skills in sequence.

**Self-learning hook**: if this skill produces a filed motion, the outcome
is logged to `emma_intel.db.motion_outcomes` via `pipeline.outcome_writer`.
That table feeds back into `pipeline.learning_loop`.

**Source-of-truth principle**: Hermes documents what this skill DOES.
Beezle's pipeline IS what this skill does. Do not duplicate logic in
this SKILL.md — if behavior needs to change, edit the Beezle module.
<!-- BEEZLE-BRIDGE-END -->

