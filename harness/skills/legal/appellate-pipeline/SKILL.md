---
name: appellate-pipeline
description: Appellate-stage layer over the existing trial pipeline. Adds standards of review, FRAP/state appellate rules, preservation analysis, record citations, appellate brief structure, oral argument prep, and post-decision options (rehearing, en banc, cert). Reuses all 8 due process / binding precedent / ledger skills with appellate-specific overrides.
version: 1.0.0
author: Hermes Legal
metadata:
  hermes:
    tags: [legal, appellate, frap, standards-of-review, brief, ca3, oral-argument]
    related_skills: [binding-precedent-enforcer, due-process-violation-engine, mandatory-vs-practice-comparator, the-trap-builder, case-manager, docket-violation-ledger]
prerequisites:
  files: [~/.hermes/legal_config.json]
---

# Appellate Pipeline

## Purpose
Trial-stage skills focus on FRCP, district court, and pre-judgment work. Appellate work has different controlling rules, different remedies, and a fundamentally different posture: you're attacking a record, not building one.

This skill is the appellate-stage wrapper over the existing pipeline. It does not replace any skill — it routes around them with appellate-specific overrides.

## Key differences from trial pipeline

| Dimension | Trial | Appellate |
|---|---|---|
| Controlling rules | FRCP, FRE, local | FRAP, local appellate, state app rules |
| Document type | Motions, complaints | Briefs, petitions, motions |
| Authority hierarchy | Same | Same — but circuit binding tightens |
| Standard of review | N/A | Critical — defines burden |
| Record | Being built | Fixed and cited |
| Preservation | N/A | Make-or-break threshold |
| Sanctions | Rule 11 | FRAP 38 (frivolous appeal) |
| Remedies | Judgment, injunction | Reverse, vacate, remand, dismiss appeal |
| Post-decision | Reconsideration | Rehearing, rehearing en banc, cert |

## Standards of Review (canonical)

The skill maps every issue to its standard of review:

| Standard | When | Burden |
|---|---|---|
| **De novo** | Pure questions of law, statutory interpretation, constitutional issues, summary judgment | None — appellate court substitutes judgment |
| **Clear error** | Findings of fact after bench trial | Must leave "definite and firm conviction that a mistake has been committed" — *United States v. U.S. Gypsum Co.*, 333 U.S. 364, 395 (1948) |
| **Abuse of discretion** | Discretionary rulings (evidence, scheduling, sanctions, attorney fees) | Decision must be "arbitrary, fanciful, or clearly unreasonable" |
| **Substantial evidence** | Agency findings, jury verdicts | Whether reasonable mind might accept evidence as adequate |
| **Plain error** | Unpreserved errors | (1) error, (2) plain, (3) affects substantial rights, (4) seriously affects fairness — *United States v. Olano*, 507 U.S. 725 (1993) |
| **Harmless error** | Preserved error vs. judgment effect | Whether the error affected the outcome |
| **Structural error** | Specific constitutional violations | Never harmless — automatic reversal — *Arizona v. Fulminante*, 499 U.S. 279 (1991) |

## Appealability Barriers

Before proceeding to standards of review, confirm the order is appealable. If the order is interlocutory and not certified:

**See:** `references/interlocutory-appeals.md` for the *Westinghouse*/*Pruden* framework and strategic surrender analysis.

## Preservation Analysis

For every appellate issue, the skill tests:
1. **Was the issue raised below?** — citation to record
2. **Was the issue ruled on?** — citation to order
3. **If not raised** → plain error analysis required
4. **If raised but not ruled on** → forfeiture vs. waiver distinction
5. **Constitutional issues** — *Yee v. Escondido*, 503 U.S. 519 (1992) preservation rules

## Pipeline (appellate)

### A1 — Identify appellate posture
- Notice of appeal date
- Final judgment vs. interlocutory (28 U.S.C. § 1292)
- Standing on appeal — aggrievement
- Timeliness — FRAP 4 / state equivalent

**No Appeal Available? Strategic Surrender Analysis:**
If the order is interlocutory (denial of motion to vacate, leave to amend granted) and no Rule 54(b) certification available:
- **Immediate appeal is not viable** under §1291 (not a final decision)
- **Rule 54(b) unavailable** when claims dismissed without prejudice (not final judgment)
- **Strategic Option:** Let the leave-to-amend deadline expire → dismissal with prejudice becomes appealable final judgment
- **Binding Authority:** *In re Westinghouse Secs. Litig.*, 90 F.3d 696, 704 (3d Cir. 1996); *Pruden v. SCI Camp Hill*, 252 F. App'x 436, 438 (3d Cir. 2007)
- **Risk:** Appellate court may affirm based on procedural default (refusal to amend) without reaching merits
- **Use when:** Legal error in underlying order is clear and reversible; procedural posture blocks other options
- **Trigger:** Any order that (1) denies motion to reconsider/vacate AND (2) grants leave to amend by future date

### A2 — Map controlling appellate rules
- FRAP for federal circuits
- Local appellate rules
- State appellate rules
- Standing orders of the panel (if assigned)

### A3 — Identify standard of review for EACH issue
- Apply canonical mapping above
- Cite the case establishing the standard for that exact issue type
- Output: issue → standard → burden → controlling case

### A4 — Preservation audit
- For every issue, locate the record citation where it was raised
- Locate the trial court ruling
- If not preserved → preserve via plain error or constitutional exception
- HARD STOP if no preservation theory exists

### A5 — Run trial pipeline on each issue
Reuse existing skills with `--appellate` flag:
- `binding-precedent-enforcer --appellate --court ca3`
- `mandatory-vs-practice-comparator` (test trial court's compliance with FRCP/local)
- `due-process-violation-engine` (structural error has highest value here)
- `precedent-cache lookup` (cache-first)

### A6 — Cumulative error doctrine
Even if individual errors are harmless, their combined effect may compel reversal. Aggregate every error from the docket-violation-ledger.

### A7 — Brief structure (FRAP 28)
1. Corporate disclosure (FRAP 26.1)
2. Table of contents
3. Table of authorities
4. Jurisdictional statement
5. Statement of issues
6. Statement of the case
7. Statement of facts (with record citations)
8. Summary of argument
9. Standard of review (per issue)
10. Argument
11. Conclusion
12. Certificate of compliance (FRAP 32)
13. Certificate of service
14. Addendum

### A8 — Oral argument prep
- Hot bench questions: identify panel's likely concerns from each judge's prior opinions
- Killer cases on each side
- Concession points (what you can give up)
- Time management

### A9 — Post-decision options
- Rehearing (FRAP 40) — within 14 days
- Rehearing en banc (FRAP 35) — exceptional importance
- Mandate stay (FRAP 41)
- Certiorari (Sup. Ct. R. 13) — 90 days

## Usage

### Initialize appellate proceeding from existing trial case
```bash
~/.hermes/skills/legal/case-manager/scripts/case.sh promote-to-appeal \
  --case "2:25-cv-12345" \
  --appellate-no "26-1234" \
  --appellate-court ca3 \
  --notice-of-appeal-filed 2026-04-15

~/.hermes/skills/legal/appellate-pipeline/scripts/appeal.sh init \
  --case "26-1234" \
  --trial-case "2:25-cv-12345" \
  --circuit ca3
```

### Map issues to standards of review
```bash
~/.hermes/skills/legal/appellate-pipeline/scripts/appeal.sh map-standards \
  --case "26-1234" \
  --issues-file /tmp/issues.json
```

### Preservation audit
```bash
~/.hermes/skills/legal/appellate-pipeline/scripts/appeal.sh preservation \
  --case "26-1234" \
  --issue "trial court denied motion to compel without findings" \
  --record-cite "App. 87-89"
```

### Build appellate brief
```bash
~/.hermes/skills/legal/appellate-pipeline/scripts/appeal.sh brief \
  --case "26-1234" \
  --type opening \
  --output /tmp/opening_brief.json
```

### Cumulative error analysis
```bash
~/.hermes/skills/legal/appellate-pipeline/scripts/appeal.sh cumulative-error \
  --case "26-1234"
```

### Post-decision routing
```bash
~/.hermes/skills/legal/appellate-pipeline/scripts/appeal.sh post-decision \
  --case "26-1234" \
  --decision-file /tmp/opinion.txt
```

## Integration with existing skills

The appellate pipeline does NOT replace any existing skill. It:
1. Reads the trial-court ledger (`docket-violation-ledger`) and converts each entry into a preserved appellate issue
2. Calls `binding-precedent-enforcer` with appellate-court jurisdiction binding
3. Calls `due-process-violation-engine` — structural errors are now automatic reversal triggers
4. Calls `the-trap-builder` — but with appellate options:
   - Affirm per binding authority
   - Reverse per binding authority
   - Vacate and remand
   - Refuse to address (mandamus from Supreme Court / en banc)
5. Calls `case-manager` to track appellate-specific deadlines (briefing schedule, oral argument)
6. Promotes verified holdings to `precedent-cache` with `binding_level=appellate`

## Storage
Reuses `~/.hermes/data/cases/{case_slug}/` from case-manager. Adds:
```
~/.hermes/data/cases/{appellate_slug}/
├── meta.json                  # appellate stage
├── trial_case.json            # link back to trial
├── issues.json                # appellate issues w/ standards of review
├── preservation.json          # per-issue preservation map
├── record_citations.json      # appendix references
├── briefs/
│   ├── opening_brief.docx
│   ├── reply_brief.docx
│   └── ...
└── (all standard case-manager files)
```

## Rules
1. Every appellate issue MUST have a standard of review cited to controlling case
2. Every fact in a brief MUST have a record citation (App. X, JA Y)
3. Unpreserved issues MUST go through plain error analysis or be dropped
4. Cumulative error analysis runs automatically when ≥ 3 issues are logged
5. Standard FRAP word/page limits enforced
6. Standing orders of the assigned panel override default circuit rules
7. Different standards of review apply to different issues — never use one standard for the whole brief
