---
name: statutory-construction-analyzer
description: Apply rules of statutory construction to determine the scope and
  meaning of statutory language. Closed vs. expansive definitions, mandatory
  vs. discretionary duties, and other construction rules.
when_to_use: |
  When you need to interpret a statute's meaning — especially when the text is
  ambiguous or when opposing counsel is arguing for a narrow reading. NOTE: For
  comprehensive construction analysis (Canon 0-7, UCC patterns, Treasury ops),
  prefer beeze-legal-construction. This skill covers the 7 core canons only.
grounding: grounded
related_skills:
  - beeze-legal-construction
  - construction-spec
  - corpus-text-lookup
  - legal-soul
---

# Statutory Construction Analyzer

## Overview

Rules of statutory construction are the tools for determining what a statute
actually means when the text is ambiguous. This skill applies those rules.

## Key Construction Rules

### 1. "Means" vs. "Includes" — Closed vs. Expansive

- "means" = closed definition (only what is listed)
- "includes" = **NOT automatically expansive** — only expansive where the statute ITSELF supplies an expansion clause (e.g. 26 U.S.C. 7701(c) "shall not be deemed to exclude"; "including but not limited to"). Absent such a clause the enumerated items DEFINE THE CLASS — they are the floor, and reaching beyond them must be argued under the canons (ejusdem generis / expressio unius), never assumed. "Includes only" = closed.

### 2. Expressio Unius — "The expression of one thing is the exclusion of another"

- If a statute lists specific items, it excludes items not listed
- Example: "shall include A, B, and C" excludes D

### 3. Ejusdem Generis — "Of the same kind"

- General words following specific words are limited to the same kind
- Example: "cars, trucks, and other vehicles" — other vehicles means other
  motor vehicles, not bicycles

### 4. Mandatory vs. Discretionary

- "shall" = mandatory duty
- "may" = discretionary power
- This distinction is critical for determining if a remedy is available

### 5. Last Antecedent Rule

- A limiting clause modifies only the immediately preceding word/phrase
- Example: "A or B, but not C" — C modifies B, not A

### 6. Mutual Exclusion

- If a statute defines two terms that are mutually exclusive, something
  cannot be both at the same time
- Example: if "employee" and "independent contractor" are mutually exclusive,
  a person is one or the other, not both

### 7. Surplusage Rule

- Every word in a statute should be given meaning — avoid reading words out
- If a word seems redundant, it likely has a distinct purpose

## Workflow

### Step 1: Query the corpus for the statute text

    mcp_beezle_search_chunks(query="STATUTE section TEXT", n_results=5)

### Step 2: Identify construction issues

Look for:
- "means" vs. "includes" — closed vs. expansive
- "shall" vs. "may" — mandatory vs. discretionary
- Lists of specific items followed by general terms
- Conditional language ("if", "when", "provided that")
- Cross-statute definitions

### Step 3: Apply construction rules

For each issue identified, apply the relevant construction rule.

### Step 4: Cross-reference with corpus definitions

- `mcp_beezle_search_chunks()` — search the corpus for statutory/regulatory text
- `mcp_beezle_definition_lookup()` — search the corpus for term definitions
- `mcp_beezle_resolve_citation()` — look up authoritative legal text by citation

## Example: FCRA "furnisher" definition

1. `mcp_beezle_definition_lookup(term="furnisher")` — find corpus occurrences
2. Identify: "furnisher" is defined as "means" or "includes"?
3. If "means" — closed definition, only what is listed
4. If "includes" — expansive, can include similar entities not listed
5. Check if opposing counsel is arguing for a narrow reading
6. Apply construction rule to show the broader reading is correct

## Grounding status

**Verified 2026-09-17** (supersedes the 2026-07-23 zero-hit finding — the corpus gained a dedicated
canons source and Black's Law entries since). The canons ARE now in the corpus:

- **Dedicated source** `rules_of_statutory_construction_canons` — ejusdem generis (incl. Norfolk &
  Western v. Am. Train Dispatchers, 499 U.S. 117 (1991)), expressio unius, noscitur a sociis.
- **Black's Law Dictionary** (`frb_chicago_fractional`, `blacks_law_8th/9th`) — expressio unius,
  rule of the last antecedent (Blackstone's dean/prebendary example).
- **Case law** — last antecedent discussed in *United States v. Hayes*, 555 U.S. 415 (quoting
  Barnhart v. Thomas, 540 U.S. 20, 26); noscitur a sociis in *Washington State Dept. of Soc. &
  Health Servs. v. Guardianship Estate of Keffeler*, 537 U.S. 371, 384 (2003).

When using these canons:
1. Ground the specific statutory text via `mcp_beezle_search_chunks()` and cite the actual statute
2. The canons are interpretive tools — cite the underlying statutory text as the authority; the
   canon sources above confirm the canons exist but are not themselves dispositive authority
3. The corpus can confirm the actual text; it cannot confirm the canon's application to your facts

**Includes rule grounding**: The locked discipline (2026-07-05) that "includes" is NOT automatically
expansive is grounded in the production module `pipeline.statutory_construction._has_expansion_clause()`
(line 161) and Canon 1b of `beeze-legal-construction`. The exemplar expansion clause, 26 U.S.C.
7701(c) ("shall not be deemed to exclude"), IS in the corpus: `usc_t26_s7701_full` and
`USCODE-LII-title26` (both return the operative text — verified 2026-09-17).

## Pitfalls

- Don't assume "shall" is always mandatory — context matters
- Don't assume "includes" is always expansive — it can be used as "means"
- Don't ignore cross-statute definitions — the definition may be in a different
  title or section
- When in doubt, query the corpus for the exact statutory text rather than
  relying on memory

<!-- BEEZLE-BRIDGE-START -->
## 🔗 Wired to Beezle (synced 2026-06-12)

*This section is auto-generated by `_sync_hermes_beezle_v2.py`. Everything
above this marker is Hermes's own work and is preserved verbatim. The
Beezle modules below are the production implementations that execute what
this skill describes.*

**Category**: `knowledge-discovery`

**Primary Beezle module**: `pipeline.statutory_construction`

**Direct invocation**:

```bash
cd {{ENTROPY_HOME}}
python3 -m pipeline.statutory_construction
```

**Also uses**:

- `pipeline.statutes_registry`



**Registry entry**: `{{ENTROPY_HOME}}/skills/registry.json` (key: `statutory-construction-analyzer`)

**Master dispatcher**: `pipeline.chain_runner` (122 KB) — the production
orchestrator that calls all skills in sequence.

**Self-learning hook**: if this skill produces a filed motion, the outcome
is logged to `emma_intel.db.motion_outcomes` via `pipeline.outcome_writer`.
That table feeds back into `pipeline.learning_loop`.

**Source-of-truth principle**: Hermes documents what this skill DOES.
Beezle's pipeline IS what this skill does. Do not duplicate logic in
this SKILL.md — if behavior needs to change, edit the Beezle module.
<!-- BEEZLE-BRIDGE-END -->
## Linked
- [[beeze-legal-construction/SKILL.md|cross_ref:Comprehensive construction analysis — means vs includes, Canon 0 operative-text discipline, UCC patterns, Treasury operations]]
- [[corpus/corpus-text-lookup/SKILL.md|cross_ref:Corpus grounding]]
- [[system/evolution_log.md|cross_ref:Evolution log]]
