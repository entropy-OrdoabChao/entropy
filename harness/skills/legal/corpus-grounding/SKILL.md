---
name: corpus-grounding
description: Find and quote authoritative legal text from the corpus for a specific legal claim step.
version: 0.1.0
grounding: partial
metadata:
  hermes:
    tags: [Legal, Research, Corpus, Authority]
    related_skills: [corpus-access, corpus-query, corpus-text-lookup]
---

# Corpus Grounding — Legal Step Authority Finder

When the user asks to "ground ONE step" (or similar — "search_chunks", "find authority for"), this skill captures the workflow of taking a specific legal claim/argument step and finding authoritative text in the corpus to support it.

## When to Use

- User says "Ground ONE step: [query]" — the canonical trigger
- User asks to find authority for a specific legal proposition
- User wants to verify a legal claim against the corpus before drafting
- User needs a quote + source for a specific legal concept

## Prerequisites

- Load `corpus-query` for endpoint docs; `corpus-access` for the authoritative
  grounding path (see below).

## Procedure

1. **Load skills** — `corpus-access` first (authoritative path), then `corpus-query` for endpoint details.

2. **Use `search_chunks` via Beezle MCP as the primary grounding tool.** This is the primary path — semantic + FTS hybrid over the chunk index. IMPORTANT: `search_chunks` is MCP-only (HTTP endpoint returns 404). Use the Beezle MCP client, not raw HTTP.

3. **If MCP is unavailable**, fall back to HTTP endpoints (`/search_text`, `/lookup_term`, `/lookup_authority`) per `corpus-query` — see that skill for endpoint details. Do NOT rely on the Python wrapper as a primary path; it is a convenience shim.

4. **Ad-hoc `/search_text` (FTS)** — generally noisier than `search_chunks` for broad queries, but WORKS WELL for specific citation-style queries (e.g., "1099-C"). Use only when MCP is unavailable and the query is highly specific.

5. **Verify results** — check the `source` field to confirm it's from the FTS index or vector index, not taxonomy noise.

6. **Report the ground** — quote the best hit's text, cite the source field and chunk_id.

7. **If no relevant results after 2 refinements** — report "absent" from the corpus.

## Query Refinement Patterns

- Broad query returning noise → add specific legal terms (e.g., "1099-C" → "cancellation indebtedness IRC 108")
- Zero results → try broader terms (e.g., "TFM SAR review agency accounting" → "SAR review")
- Authority lookup returns 0 → try alternative citation formats (IRC 61, 26 USC 61, etc.)

## Verification

After grounding, always verify:
1. Source field is not a random taxonomy hit
2. The excerpt actually addresses the step being grounded, not just matching keywords

## Related Skills

- `corpus-access` — authoritative grounding path (two grounded checks: positive-law status + party capacity)
- `corpus-query` — HTTP endpoint reference (search_text, lookup_term, lookup_authority)
- `corpus-query-timeout-handling` — retry logic, timeout detection, cron safety (300s limit)
- `corpus-text-lookup` — statutory text retrieval

## Pitfalls

- The Python client at `/home/{{USER_FIRST}}/.hermes/skills/legal/corpus-query/scripts/corpus_client.py` is a convenience wrapper, NOT a primary path. Use MCP tools or HTTP endpoints.
- Corpus queries hit the 36.7 GB chunk_text.db (size verified via /status). The corpus does NOT cache results.
- `search_chunks` is MCP-only — the HTTP endpoint returns 404. Do NOT curl it.
- `/search_text` returns noise on broad queries; use specific citation-style queries for FTS.
- `/lookup_authority` may return 0 hits — not all citations are indexed.
- Always use spaces in the query, NOT + — curl will encode + as a space anyway.
- Cron jobs have 300s timeout — if grounding takes too long, break into smaller queries. See `corpus-query-timeout-handling` for retry patterns.
- The corpus doesn't contain everything — if you can't find it after 2 refinements, report absent.
- **Two grounded checks** (per `corpus-access`): always verify positive-law status and party capacity for any cited authority.

## Linked
- [[corpus-access/SKILL.md|cross_ref:Authoritative grounding path]]
- [[corpus-query/SKILL.md|cross_ref:Endpoint reference]]
- [[corpus-text-lookup/SKILL.md|cross_ref:Statutory text retrieval]]
