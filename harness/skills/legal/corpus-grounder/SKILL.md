---
name: corpus-grounder
description: Search the Beezle corpus for authoritative legal text.
version: 0.1.0
grounding: partial
author: Beezle
license: MIT
metadata:
  hermes:
    tags: [Legal, Corpus, Grounding, Search, Statute]
    related_skills: [legal-grounded-research, corpus-query, beezle-irs-research]
---

# Beezle Corpus Grounding

Ground any legal claim, definition, or proposition by searching the Beezle corpus for authoritative text. Use this whenever the user asks to prove, verify, or ground something in the corpus — not for broad legal research (use `legal-grounded-research`), but for targeted text retrieval.

## Core Principle

The corpus has TWO distinct search endpoints with different capabilities. Using the wrong one wastes turns.

## Procedure

### Step 1: Try the brain first

Check `definitions_learned` in Obsidian for existing knowledge before hitting the corpus:

```
list_obsidian_notes(subdir="definitions_learned")
```

If relevant notes exist, `read_obsidian_note` on them. Skip to Step 6.

### Step 2: Try source_fetch for known statute paths

Use the `source_fetch` tool to fetch specific provisions by path. This is the fastest path when you know the section number.

**CRITICAL: source_fetch returns success metadata but does NOT write files to disk. Always verify:**

```
# After source_fetch returns success:
read_file("{{ENTROPY_HOME}}/obsidian_sync/primary_sources/federal/<path>.md")
```

If the file is empty or missing, the fetch silently failed — do NOT proceed using only the metadata. Try the corpus search fallback (Step 3).

### Step 3: Search for actual text — use the right endpoint

**For verbatim statutory/regulatory text** (e.g., "What does 26 USC 3401(c) say?"):

```
terminal("curl -s 'http://localhost:8100/search_text?query=<query>&limit=5'")
```

Search for the exact wording you want — use key phrases from the provision you're looking for. This endpoint returns actual text, not metadata.

**For finding chunks that mention a provision** (e.g., "Show me what the corpus says about employee definition"):

```
terminal("curl -s 'http://localhost:8100/search_text?query=<query>&top_k=10'")
```

Use `search_text` (NOT `search_chunks`) — the `search_chunks` HTTP endpoint returns 404. Use Beezle MCP `search_chunks` if you need semantic search; the HTTP endpoint is dead. This returns chunks that mention the provision but may not contain its actual text. Use to find related content, not specific statutory text.

### Step 4: Search the Obsidian brain for primary sources

If the corpus search returns insufficient text, check the Obsidian primary sources:

```
list_obsidian_notes(subdir="primary_sources/federal")
```

Look for files matching: `26usc*.md`, `26cfr*.md`, `irc*.md`, `form_843*.md`. Read the best 1-2.

### Step 5: Search ChromaDB

If the above paths are dry, use ChromaDB:

```
retrieve_with_expansion(query="<query topic>", jurisdiction="federal", final_k=10)
discover_authority(query="<query topic>", subject_areas=["tax"], include_case_law=true)
```

Pull 1-2 strongest chunks. Distance < 0.5 = strong match. Distance > 0.7 = ignore.

### Step 6: Write the finding

Write to `obsidian_sync/learning/<domain>/<slug>.md` using `write_file`:

```markdown
---
topic: <topic>
generated_at: <ISO timestamp>
status: complete
---

# Finding: <topic>

## Question
<one-line restatement>

## Binding Authority
- **Statute**: 26 USC § XXXX(x) — "<exact quoted text under 15 words>"
- **Regulation**: 26 CFR § 31.XXXX-X(x) — "<exact quoted text under 15 words>"
- **Case**: <Case v. Case>, <cite> (<court> <year>) — holding paraphrased

## Plain-English Walkthrough
<3-5 sentences>

## Source Anchors
- source_fetch path: <path>
- Corpus search endpoint: <search_chunks or search_text>
- Corpus chunk id: <id> (if applicable)
- ChromaDB distance: <0.XXX> (if applicable)
```

### Step 7: Update memory

```
memory_set(
    agent_slug="<domain>-specialist",
    frontmatter={
        last_topic: "<slug>",
        last_run_iso: "<ISO timestamp>",
        status: "complete" | "no_query"
    },
    append_log="Wrote learning/<domain>/<slug>.md"
)
```

### Step 8: Final response

```
Cycle complete.
Wrote: obsidian_sync/learning/<domain>/<slug>.md
Citations: 26 USC § XXXX, 26 CFR § 31.XXXX, <case>
```

## Pitfalls

- **source_fetch success ≠ file written**: This is the #1 trap. The tool returns metadata about what it "fetched" but the file is never written to disk. Always `read_file` the path after a successful fetch.
- **search_chunks returns mentions, not text**: If you need the actual text of a provision, use `search_text` — not `search_chunks`. The chunks endpoint returns chunks that mention the provision, not its actual text.
- **search_chunks HTTP endpoint is dead**: The HTTP endpoint `http://localhost:8100/search_chunks` returns 404. Use Beezle MCP `search_chunks` for semantic search; the HTTP endpoint is never valid.
- **Quotes ≤ 15 words**: Beyond that, paraphrase. Copyright matters.
- **One query per cycle**: Don't try to process the whole queue.
- **ChromaDB gibberish**: If ChromaDB returns gibberish, log it and do not write a learning note from bad data.
- **Brain vs DB conflicts**: Brain wins (it was vetted previously).

## Verification

After completing the grounding cycle, verify the output file exists:

```
read_file("{{ENTROPY_HOME}}/obsidian_sync/learning/<domain>/<slug>.md")
```

Confirm the frontmatter shows `status: complete` and the body contains actual authoritative text (not "[citation needed]" for every section).
