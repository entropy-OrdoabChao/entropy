---
name: corpus-access
description: The CORRECT way to ground any legal claim in Beezle's corpus (3.5M chunks) — use this instead of ad-hoc SQL LIKE queries or the noisy /search_text endpoint. Use whenever you need a statute, rule, case, regulation, treatise, definition, IRS form, Public Law, or the positive-law status of a title.
grounding: partial
related_skills: [legal-soul, corpus-text-lookup, corpus-text-query, corpus-query, corpus-fallback-strategy, beeze-legal-construction]
---

# corpus-access — ground in the corpus, never from memory

Beezle's corpus is the ground truth. Do NOT answer law from training; retrieve it and cite the source. The recurring failure was using ad-hoc `LIKE '%...%'` queries (slow, over-match) or `:8100 /search_text` with natural-language (returns noise). Use the tools below instead.

## The structure (so you know what's there)
- **3,517,284 chunks** in `chunk_text.db` (host `/home/{{USER_FIRST}}/turbovec/`, 38G), embedded (dim 768 bge, 4-bit quantized — see `index_meta.json`; current delta 108,720 vectors).
- FTS: `chunk_text_fts` (statutes/rules/cases) + `chunk_text_dict_fts` (dictionaries: **Black's 8th/9th/2nd, Bouvier 1856** — for definitions). Join: `chunk_text.chunk_id = chunk_text_fts.source_id`.
- Coverage incl. US Code (all enacted titles + LII fills), CFR, Federal Rules, PA Consolidated Statutes (46 titles), **Public Laws / Statutes at Large** (`PLAW-*`), IRS IRM + TFM + forms, SEC/OCC/FDIC/FinCEN, SCOTUS reporters, Cornell LII mirrors (UCC, etc.).

## How to retrieve (the ONLY correct paths)
1. **`search_chunks(query, n_results)`** (Beezle MCP) — the primary tool. Full-corpus hybrid: semantic over the whole 3.5M index + the re-embed delta + authoritative statute/rule/instruction text. Returns `{source, text, score}`. Cite each `source`.
2. **`read_graph(query, k)`** (Beezle MCP) — read Beezle's accumulated Obsidian understanding BY MEANING (prior grounded notes). Build on it, cite `[[slug]]`, but verify against `search_chunks`.
3. For a **defined term** (natural person vs corporation, "includes" vs "means", etc.): `search_chunks` with the term — the dictionaries (Black's/Bouvier) are in the corpus. The **Dictionary Act, 1 U.S.C. § 1** is referenced in the corpus but its operative text ("The word 'include' … shall be considered as used either in the singular or plural") is NOT reliably retrievable via FTS (bare-cite misses; `USCODE-LII-title1` has only 39 TOC-level chunks, `USC-title1` has 12). Use `search_chunks` semantically with "Dictionary Act include means" or query Black's directly.

## Two grounded checks to ALWAYS run on a cited authority
- **Positive-law status** (`generation.positive_law.note_for_authority`): is the cited U.S.C. title POSITIVE LAW or non-positive? Non-positive ⇒ the Code is only *prima facie* evidence (1 U.S.C. § 204(a)); the enacted **Statutes at Large / Public Law** controls on conflict — demand it (the `PLAW-*` are in the corpus).
- **Party capacity**: is the party a **natural person** (constitutional protections) or an **artificial person / corporation** (statutory/commercial)? Apply the **"includes" vs "means"** rule (1 U.S.C. § 1; Black's) and flag any term used to misclassify a human as a commercial "person."

## Pitfalls
- **`search_chunks` is MCP-only** — the HTTP endpoint returns 404. Do NOT curl it. Always use the Beezle MCP client. If MCP server (localhost:8101) is down, fall back to direct SQLite FTS on `chunk_text_fts` (join on `chunk_id = source_id`).
- **Dictionary Act (1 U.S.C. § 1)**: Operative text NOT reliably retrievable via FTS. `USCODE-LII-title1` (39 chunks) and `USC-title1` (12 chunks) are TOC-level only. Use semantic `search_chunks` with "Dictionary Act include means" or query Black's dictionary entries directly. (Corrected 2026-09-18; prior "corrected 2026-07-05" claim was premature.)
- **UCC uniform text**: UCC 3-301, 9-203, 3-104, 3-302 ARE indexed via Cornell LII sources (`cornell_lii_ucc_3-301`, `cornell_lii_ucc_9-203`, `cornell_lii_ucc_3-104`, `cornell_lii_ucc_3-302`). Use source-name fetch first; content-FTS as fallback. (Corrected 2026-09-18; prior "NOT clearly indexed" claim was stale.)
- **Positive-law title gaps**: Some U.S.C. titles are non-positive law and the corpus may not have the Public Law anchor. Always verify positive-law status.
- **`12 USC 5534` (CFPB founding statute)**: Bare-cite FTS returns 0 hits, but substantive text ("Bureau Established … independent bureau in the Federal Reserve System") IS retrievable via semantic `search_chunks` with "Bureau Established independent bureau Federal Reserve System". Same miss class as noted in corpus-text-query and corpus-text-lookup. (Corrected 2026-09-18; prior "NOT IN CORPUS" verdict was a bare-cite miss.)

## Rules
- Report what the corpus says; quote + cite. If something is NOT in the corpus, say so — do not supply it from memory as if grounded.
- Prefer primary law (statute/rule/case) over secondary; for non-positive titles, anchor to the Public Law.
