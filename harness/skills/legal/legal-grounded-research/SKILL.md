---
name: legal-grounded-research
description: End-to-end legal research using BOTH the corpus and the Obsidian graph in an iterative loop — query corpus for primary authority, walk Obsidian links from those authorities, cross-reference findings, identify gaps, do targeted follow-ups, and synthesize into a grounded answer with dual citations.
when_to_use: When the user asks a legal question that requires grounding in primary law AND related knowledge — research questions, "what is the law on X," "find authority for Y," or any query where the answer lives across the corpus-graph boundary.
---

# legal-grounded-research — dual-source legal research

**Purpose:** Answer legal questions end-to-end by iterating between the corpus (primary law) and the Obsidian graph (related knowledge), producing a grounded answer with dual citations.

**Core principle:** Never trust a single source. Corpus gives you the law; Obsidian gives you the context. Cross-reference both. If they conflict, flag it.

---

## When to use this skill

- "What is the law on [topic]?" — research question
- "Find authority for [claim]" — need primary law + supporting context
- "Does [statute/regulation] apply to [situation]?" — needs both text and interpretation
- "What are the remedies under [statute]?" — needs statutory text + case law + regulatory guidance
- "Is [practice] legal?" — needs primary law + judicial overlay + regulatory guidance

## When NOT to use this skill

- Simple definition lookups → use `definition-lookup` or `corpus-query` directly
- Case-specific analysis (audit, scorecard) → use `case-analyzer` or `docket-violation-ledger`
- Drafting a filing → use `production-drafter` or `legal-case-workflow`
- Questions answered by corpus alone (e.g., "what does 15 USC 1681 say?") → use `corpus-access` directly

---

## The 4-phase loop

### PHASE 1: Seed — get primary authority from the corpus

**Goal:** Find the controlling law.

1. Query the corpus for the primary authority using `search_chunks()` (Beezle MCP) — the primary tool. Use `search_chunks(query, n_results=10)` for the core topic.
2. Also query `lookup_authority` for specific citations if the user provided them.
3. Also query `lookup_term` for key defined terms in the question.
4. **Verify positive-law status** on any U.S.C. title cited — is it POSITIVE LAW or non-positive? Non-positive means the Statutes at Large / Public Law controls on conflict.
5. Identify the **controlling authority** — the statute, regulation, or case that directly answers the question.

**Output:** List of primary authorities with sources, scores, and text excerpts. Flag any non-positive-law citations.

### PHASE 2: Expand — walk the Obsidian graph from the authorities

**Goal:** Find related knowledge the corpus alone won't surface.

1. For each primary authority found, walk the Obsidian graph:
   ```
   obsidian_graph_walk(start="<authority_slug>", depth=2, max_nodes=100)
   ```
   **NOTE:** The `source` field from corpus results is NOT a reliable slug — it may be a URL, filename, or arbitrary string. Do NOT blindly use it as a slug. Instead, construct the slug from the citation itself: e.g., "15_USC_1681" from "15 USC 1681", "12_CFR_204" from "12 CFR 204", "Smith_v_Jones_2020" from a case citation. If the slug doesn't resolve, the graph walk will return empty — that's expected.
2. For depth-1 neighbors, inspect outgoing links to see what authorities they cite:
   ```
   obsidian_outgoing_links(note_path="<neighbor_slug>")
   ```
3. For any interesting edge types (`judicial_overlay`, `interpreted_by`, `implemented_by`, `authority`), read the neighbor note:
   ```
   read_obsidian_note(subdir="<folder>", filename="<note>")
   ```
4. **Cross-reference:** For up to 5 of the most important Obsidian findings, verify against the corpus. Do NOT verify every single finding — that's a secondary explosion risk. Pick the 5 most relevant findings (by edge type priority: `judicial_overlay`, `interpreted_by`, `implemented_by`, `authority`, `defined_by`) and check those. If those 5 agree with the corpus, assume the rest do too.

**Output:** List of related knowledge — cases, regulations, guidance — linked from the primary authorities. Flag any conflicts between corpus and Obsidian.

### PHASE 3: Gap analysis — find what's missing

**Goal:** Identify what the corpus + graph haven't covered yet.

1. Compare the Obsidian graph findings against the corpus results:
   - Does the graph have authorities the corpus didn't surface? → **Gap 1: corpus blind spot** — do a targeted corpus query for those authorities.
   - Does the corpus have authorities the graph didn't link to? → **Gap 2: graph gap** — NOTE: do NOT propose wikilinks for this unless the gap is critical to the answer. The graph and corpus use different indexing methods (wikilinks vs. semantic vectors), so gaps are expected and usually unfixable. Only propose a link if the gap is blocking a key finding.
   - Are there key concepts mentioned in neither source? → **Gap 3: missing concept** — query the corpus directly for that concept.
2. Check for **missing definitions** — are key terms in the primary authority not defined in Obsidian? Query `lookup_term` for those terms. **Only flag as missing if the term is central to the user's question** — don't flag every undefined term.
3. Check for **missing judicial overlays** — does the graph have the statute but no case law interpreting it? Query `search_chunks` for case law.

**Output:** List of gaps with type (corpus blind spot / graph gap / missing concept) and recommended action.

### PHASE 4: Synthesize — produce a grounded answer

**Goal:** Answer the user's question with dual citations.

1. For each finding, cite BOTH sources where available:
   - Corpus source: `search_chunks` result with `source` field + excerpt
   - Obsidian source: `[[slug]]` with edge type (e.g., `[[15_USC_1681|judicial_overlay:Smith v. Jones]]`)
   - **CRITICAL:** If only ONE source is available, cite that one. Do NOT fabricate a second citation. Empty sections are acceptable — the output format below shows what to include when you HAVE content, not what to invent when you don't.
2. Structure the answer:
   - **Primary holding** — the direct answer from the corpus
   - **Supporting authorities** — related law from the graph
   - **Interpretations** — how courts/agencies have interpreted the law (from graph)
   - **Gaps** — what couldn't be answered (be honest about missing info)
   - **Conflicts** — where corpus and graph disagree
3. Include a **confidence assessment**:
   - HIGH: corpus + graph agree, both have primary authority
   - MEDIUM: corpus has primary authority, graph has supporting context
   - LOW: only one source available, or they conflict
4. If gaps were identified in Phase 3, do **targeted follow-up queries** and repeat Phases 2-4. **HARD LIMIT: max 2 iterations.** If gaps remain after 2 iterations, report them as irrecoverable and stop — do not continue looping.

---

## Tool reference

### Corpus tools (from `corpus-access` and `corpus-query`)

| Tool | What it does |
|---|---|
| `search_chunks(query, n_results)` | Primary corpus search — semantic + FTS over 3.17M chunks — NOTE: the exact parameter names are undocumented in the corpus-access skill; `query` is the search text, `n_results` is the number of results to return (use 5-10) |
| `lookup_authority(cite)` | Look up a specific legal citation (statute, regulation) |
| `lookup_term(term)` | Look up a defined term in the corpus dictionaries |
| `positive_law.check(authority)` | Check if a U.S.C. title is positive law — NOTE: this tool is NOT available via Beezle MCP; instead use the corpus-access skill's guidance to check positive-law status by querying the corpus for the title's positive-law note |

### Obsidian graph tools (from `obsidian-graph`)

| Tool | What it does |
|---|---|
| `obsidian_graph_walk(start, depth, max_nodes)` | BFS walk through resolved wikilinks from a seed |
| `obsidian_outgoing_links(note_path)` | List wikilinks FROM a note |
| `obsidian_backlinks(note_path)` | List notes that link TO a note |
| `obsidian_search_by_tag(tag)` | Find notes with a given tag |
| `obsidian_search_by_frontmatter(field, value)` | Find notes by frontmatter field |
| `read_obsidian_note(subdir, filename)` | Read a specific note |
| `obsidian_propose_link(from, to, edge_type, citation)` | Stage a new wikilink for human approval |

### Text-similarity search (from `obsidian_bridge`)

| Tool | What it does |
|---|---|
| `find_related(query, limit)` | Text-similarity search over note bodies |
| `search_obsidian(query)` | Keyword search over vault citation index |

---

## Edge type priority for graph walks

When walking the graph, prioritize these edge types (they have textual basis):

1. `judicial_overlay` — case law interpreting the statute
2. `interpreted_by` — agency guidance interpreting a regulation
3. `implemented_by` — regulation implementing a statute
4. `authority` — CFR → USC enabling-statute citation
5. `defined_by` — term imported from another section's definitions

Treat `unknown` edges with caution — they may be broken or stale links.

---

## Conflict resolution rules

When corpus and Obsidian disagree:

1. **Corpus text > Obsidian interpretation** — if the corpus says one thing and Obsidian says another, the corpus text controls. Note the conflict.
2. **Obsidian case law > corpus summary** — if Obsidian has a case that the corpus summary doesn't mention, the case may be more specific. Query the corpus directly for that case.
3. **Both > neither** — if they agree, that's HIGH confidence. If they disagree, flag it and note both positions.

---

## Output format

Always produce the answer in this structure (include sections only if you have content for them — don't invent content to fill empty sections):

```
## Answer to: [user's question]

### Primary Holding
[Direct answer from corpus with dual citation]

### Supporting Authorities
[Related law from graph with dual citation]

### Interpretations
[How courts/agencies have interpreted the law]

### Gaps
[What couldn't be answered — be honest]

### Conflicts
[Where corpus and graph disagree — if any]

### Confidence: HIGH / MEDIUM / LOW
[Why]
```

---

## Pitfalls

- **Don't assume the graph has everything** — the Obsidian vault has ~15,800 broken wikilinks. Walk the graph but verify each hit against the corpus.
- **Don't skip positive-law checks** — if you cite a non-positive-law U.S.C. title, the Statutes at Large controls. Always verify.
- **Don't over-walk the graph** — depth 2 is usually enough. Deeper walks return noise.
- **Don't fabricate edge types** — if the primary text doesn't support `judicial_overlay` or `implemented_by`, use `unknown` and note it.
- **Don't ignore corpus blind spots** — the corpus may not have everything. If the graph has something the corpus doesn't, flag it as a gap.
- **Don't use `primary_sources/` by default** — 34K USC/CFR notes will drown signal. Opt in only when the question specifically needs USC/CFR text.
