---
name: short-demand-letter
description: "Draft a single-wrong demand letter in 5 sentences: fact, legal hook, remedy, deadline, consequence."
version: 0.1.0
metadata:
  hermes:
    tags: Demand, Legal, Email-Reply, Short-Form
---

# Short Demand Letter

A single-wrong demand letter — one fact, one legal hook, one remedy, one deadline, one consequence. No filler.

## When to use

- Email reply to opposing counsel or party
- Short-form demand (<=1 page)
- Single wrong — one concrete violation
- When a full demand letter would be overkill but you still need a grounded, enforceable notice

## When NOT to use

- Multiple distinct wrongs (write separate letters)
- Complex factual disputes requiring discovery
- Government agency (use formal notice format instead)

## 5-Sentence Structure

1. **Fact** — ONE concrete wrong (who did what, when, on what basis). Include docket/exhibit citation.
2. **Legal hook** — Binding rule + citation. Verbatim quote or precise paraphrase.
3. **Remedy** — Specific remedy demanded.
4. **Deadline** — Calendar date. No "within X days" — use "by [date]".
5. **Consequence** — Consequence if ignored, with citation to the authority that supports it.

## Rules

- ONE wrong only. If there are multiple wrongs, write separate letters.
- Every legal claim must be corpus-grounded. If `search_chunks` returns nothing, write `[CORPUS GROUNDING REQUIRED — verify before sending]` instead of guessing.
- No qualifiers: "may," "might," "arguably," "it appears."
- No disclaimers or suggestions to consult counsel.

## Workflow

1. Identify the ONE wrong from the case — search the Obsidian vault or case DB for the specific defect.
2. Ground the legal hook in the corpus using `search_chunks` — verify the citation before writing.
3. Draft the 5 sentences following the structure above.
4. Verify each legal claim with `search_chunks` — fix any unsupported line.
5. Save as a reusable template note in the Obsidian vault if not already saved.
6. Wikilink from the case notes where applicable.

## Source template

`{{ENTROPY_HOME}}/obsidian_sync/templates/short-demand-email-reply.md`

## Verification

Check the vault: `ls {{ENTROPY_HOME}}/obsidian_sync/templates/short-demand-email-reply.md`
