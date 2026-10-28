---
name: corpus-claim-verification
description: Verify legal claims from drafts against the corpus.
version: 0.1.0
metadata:
  hermes:
    tags: Legal, Corpus, Verification, Claims
---

# Corpus Claim Verification

Verify legal claims (statutes, rules, case law) from draft filings against the Beezle corpus. Detects which claims are grounded in the corpus and which need fixing.

## When to Use

- User asks to verify claims from a draft filing
- User says "check if the corpus supports these claims"
- After drafting, before finalizing a filing
- User says "verify each legal claim with search_text"

## Prerequisites

- Beezle corpus API running on `localhost:8100`
- DRAFT files in `obsidian_sync/cases/<case>/drafts/<bundle>/motions/<chain>/<name>_DRAFT.md`
- Python `requests` library available

## How to Run

1. Invoke through the `terminal` tool: `cd {{ENTROPY_HOME}} && python3 <script>`
2. Or invoke through the `execute_code` tool with inline code
3. Use `read_file` to read DRAFT files before extracting claims

## Quick Reference

- Corpus base: `http://localhost:8100`
- Search endpoint (GET): `GET /search_text?query=<term>&top_k=<N>`
- Search endpoint (POST): `POST /search` with body `{"queries": [query]}` (list of strings)
- Term lookup: `GET /lookup_term?term=<term>&max_hits=<N>`
- Authority lookup: `GET /lookup_authority?cite=<cite>`
- Categories: FOUND, WEAK, NOT_FOUND

## Procedure

1. **Find the DRAFT files** — use `search_files` or `terminal` to locate `_DRAFT.md` files in the case directory.

2. **Extract all legal claims** — read each DRAFT file with `read_file` and collect:
   - Statutes (e.g., "42 U.S.C. 1983", "42 Pa.C.S. 2503(6)")
   - Rules (e.g., "FRCP 60(d)(3)", "Pa.R.C.P. 1024")
   - Case citations (e.g., "Hazel-Atlas Glass Co. v. Hartford-Empire Co., 322 U.S. 238 (1944)")
   - Deduplicate across all drafts

3. **Verify each claim** — for each unique claim, query the corpus:
   - For statutes/rules: try `lookup_authority` first, then `search_text`
   - For case law: use `search_text` or `lookup_term`
   - Check at least 5 hits, not just the first one

4. **Categorize results**:
   - **FOUND**: Corpus returned relevant, supporting text matching the claim's purpose
   - **WEAK**: Corpus returned hits but the context is wrong (e.g., term appears in unrelated document)
   - **NOT_FOUND**: Corpus returned no hits at all

5. **Fix unsupported claims** — for WEAK and NOT_FOUND items, either:
   - Remove the unsupported line from the draft
   - Replace with a supported authority from the corpus
   - Flag for manual research

6. **Write a correction note** — for each GAP (where recall was wrong or missing), write a one-line grounded correction with a link to the source.

## Pitfalls

- **FALSE_POSITIVE_HITS**: The most critical pitfall. FTS search returns the term but in a wrong context (e.g., "Pa.R.C.P. 403" matched a transportation document, not the rule). Do NOT declare a claim NOT_FOUND just because the first hit is unrelated. Check more hits.
- **WEAK classification is often wrong**: A WEAK hit may be a FALSE_POSITIVE_HIT — the corpus actually has the right citation, just not as the top result.
- **Corpus timeout**: `search_text` can timeout on complex terms (30+ second timeout recommended).
- **Different indexing**: Some claims may be indexed differently than expected — try multiple search strategies.
- **Don't dismiss hits**: If the corpus returns ANY hits for a claim, the claim exists in the corpus. The question is relevance, not existence.
- **The GAP**: If you call something NOT_FOUND but re-verification shows hits exist, your error was not checking enough hits to find the relevant context.

## Verification

Run `search_text` for the same term and confirm the hit count matches what was previously reported. If the count changed, your earlier classification was wrong.
