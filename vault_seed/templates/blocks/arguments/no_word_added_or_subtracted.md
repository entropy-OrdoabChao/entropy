---
id: no_word_added_or_subtracted
title: NO-WORD-ADDED-OR-SUBTRACTED — Every Silent Insertion or Deletion Is a Facial Departure
applies_to_defects:
  - FATAL/CANDOR
  - SERIOUS/CANDOR
  - FATAL/CONSTRUCTION
  - SERIOUS/CONSTRUCTION
  - FATAL/AUTHORITY
  - SERIOUS/AUTHORITY
  - FATAL/CROSS_CASE_AUTHORITY
required_authorities:
  - Pa.R.P.C. 3.3 — Candor toward tribunal (no false statements of law)
  - FRCP 11(b)(2) — Certification that legal contentions are warranted by law
  - 28 U.S.C. § 1927 — Counsel liability for excessive costs
  - Gould v. Gould, 245 U.S. 151 (1917) — Doubt construed AGAINST government FOR citizen
required_facts:
  - case_id
  - caption
  - court
output_format: argument_section
mode: deterministic_if_then
---

## Every Silently Inserted Word or Deleted Limiter Is a Facial Departure

### A. The Rule

**IF** the opposing party renders a statutory or regulatory definition by silently inserting a word or deleting a limiter,
**AND** the inserted or deleted word changes the scope of the defined term,
**THEN** the rendering is a facial departure from the statutory text.

The opposing rendering is quoted next to the verbatim text to demonstrate the departure.

### B. This Cuts BOTH Ways

An opponent may silently **EXPAND** a definition by adding a word (e.g., adding "only" to narrow the class, or adding "including" to broaden it) or silently **SHRINK** a definition by deleting a limiter. Both are the same defect: a departure from the statutory text.

The opponent may also treat an "includes" definition as automatically expansive without a statutory expansion clause — the same defect as treating a "means" definition as non-exhaustive.

### C. Application: Operative-Text Comparison

> **Opponent's rendering:** "{{opponent_rendering}}"
>
> **Verbatim statutory text:** "{{verbatim_text}}"
>
> **Departure:** The opponent {{inserted/deleted}} the word "{{word}}" which {{expands/shrinks}} the defined class beyond what the statute authorizes.

### D. Authority: Candor Hooks

The opponent's failure to quote the verbatim text and flag the departure violates:
- **Pa.R.P.C. 3.3** — Candor toward tribunal (no false statements of law)
- **FRCP 11(b)(2)** — Certification that legal contentions are warranted by existing law
- **28 U.S.C. § 1927** — Counsel liability for excessive costs
