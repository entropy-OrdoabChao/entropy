---
name: dismissal-error-statement
description: Write two-sentence dismissal-error statement with authority citation, save + link in vault.
version: 0.1.0
metadata:
  hermes:
    tags: [Dismissal, Error, Statement, Authority, Citation, Save, Link]
---

# Dismissal Error Statement

Write a two-sentence dismissal-error statement citing the controlling authority, save it to the case's insights directory, and link it in the Obsidian vault.

**Does NOT do:** Full-motion drafting, appeal strategy, or multi-sentence legal arguments. This is a single-purpose note for documenting dismissal errors.

## When to Use

- User asks to write a dismissal-error statement with authority citation
- User asks to "save + link" a dismissal error finding
- User asks to write TWO sentences about a dismissal being error
- User provides a slug for the case where the dismissal occurred

## Prerequisites

- Case slug known (e.g., `transunion-25-2960`)
- Obsidian vault at `{{ENTROPY_HOME}}/obsidian_sync/cases/`
- Case directory exists at `{{ENTROPY_HOME}}/obsidian_sync/cases/<slug>/`
- Insights subdirectory: `{{ENTROPY_HOME}}/obsidian_sync/cases/<slug>/insights/`

## How to Run

1. Identify the controlling authority for the dismissal error
2. Write two sentences stating the dismissal was error, citing that authority
3. Save to the case's insights directory as `dismissal_error_<identifier>.md`
4. Link in the vault using wikilink syntax

## Quick Reference

### File path
```
{{ENTROPY_HOME}}/obsidian_sync/cases/<slug>/insights/dismissal_error_<identifier>.md
```

### File format
```
# Dismissal Error — <Authority Name>

Slug: <case-slug>

[Two sentences: the dismissal was error because [reason], citing [authority]. The second sentence explains the legal consequence (e.g., void ab initio, lack of jurisdiction, etc.).]
```

### Wikilink format
```
[[dismissal_error_<identifier>]]
```

## Procedure

### Step 1: Identify the controlling authority

Search the corpus for the controlling authority using `search_files`. Look for:
- Local appellate rules (e.g., `3d Cir. L.A.R. 107.2`)
- FRAP rules (e.g., `FRAP 31`, `FRAP 10(e)(2)(B)`)
- Circuit-specific dismissal/reinstatement rules

Use `read_file` to confirm the authority is present in the corpus files.

### Step 2: Confirm the case slug

Search for the case slug using `search_files` with pattern matching on the slug in the case directory. Look for the slug in `CASE_REGISTRY.md` or `intake_analysis_log.md`.

### Step 3: Write the two-sentence statement

The two sentences follow this pattern:
1. First sentence: The dismissal was error because [specific reason], citing [authority].
2. Second sentence: The legal consequence (e.g., the order is void ab initio, the court lacked jurisdiction, etc.).

### Step 4: Save to the case insights directory

Create the file at `{{ENTROPY_HOME}}/obsidian_sync/cases/<slug>/insights/dismissal_error_<identifier>.md` using `write_file`.

The file format:
```
# Dismissal Error — <Authority Name>

Slug: <case-slug>

[Two sentences]
```

### Step 5: Link in the vault

In the user's reply, include a wikilink to the note:
```
[[dismissal_error_<identifier>]]
```

## Pitfalls

### Primary authority not in corpus

If the user cites authority that is not found in the corpus (e.g., `3d Cir. L.A.R. 107.2` returned no results from `search_files`), do NOT use unverified authority. Report the finding honestly and seek alternatives.

### Wrong slug

Always confirm the slug from the case directory (via `search_files` for `CASE_REGISTRY.md` or `intake_analysis_log.md`) before writing. Do not guess.

### File path confusion

The file path uses the slug from the case directory, NOT the case_id. For example:
- Case ID: `{{CASE_ID}}`
- Slug: `transunion-25-2960` (from the case directory name)

### Two sentences constraint

The user explicitly asks for TWO sentences. Do not expand to a multi-paragraph argument.

## Verification

The statement is verified when:
1. The file exists at the correct path: `read_file` on `{{ENTROPY_HOME}}/obsidian_sync/cases/<slug>/insights/dismissal_error_<identifier>.md`
2. The slug matches the case directory
3. The two sentences contain a citation to the controlling authority
4. The wikilink is provided in the reply