---
name: pipeline-conductor
description: Universal legal filing pipeline. Email > Download > Intake > Review > Draft > Exhibits > Scorecard > Print list. Court rules only change drafting format.
version: 2.0.0
metadata:
  hermes:
    tags: [legal, conductor, pipeline, universal]
    category: legal
---

# Universal Filing Pipeline

## THE PIPELINE (same for ALL courts, ALL cases)

### Step 1: EMAIL
Pull new emails. Match to cases by sender domain and case number.

#### Non-ECF Signals (Step 1)
- **LetterStream "Job Pending"** from `support@letterstream.com` — User started a mailing but never completed it. Group by case_id + job_type, collect all Job IDs, report as actionable items for the user to complete or abandon.

### Step 2: DOWNLOAD  
If ECF/PACER notification: download the new filing PDF.

### Step 3: INTAKE
Process into obsidian_sync/cases/<slug>/intake/. OCR if needed. Extract text.

### Step 0: SUBMISSION LEDGER CHECK (CRITICAL — 2026-07-03)
Before flagging ANY packet as "stuck" or "overdue," verify against the submission ledger:
- Refresh + run: `cd {{ENTROPY_HOME}} && PYTHONPATH={{ENTROPY_HOME}} python3 -m pipeline.submission_reconciler`
- Read `output/filings/reconciliation_report.md` and `output/filings/our_filings_<slug>.json`
- **A packet on disk does NOT equal a filed document.** A packet at `output/filings/<case>/packet_*/` may exist with a .docx but never be submitted.
- Only flag a doc as stuck/overdue if it is **absent from the ledger** AND past a real due date.
- See `references/submission-ledger-verification-20260703.md` for the full pattern.

### Step 4: REVIEW
- Read CASE_GROUND_TRUTH.yaml (NOT CASE_FACTS.md — deprecated; look in `meta/` subdirectory if not at case root)
- Read ALL intake/ files (including docket for ECF numbers)
- Read ALL prior drafts/ (unfiled motions still pending)
- Find violations using mcp_beezle_search_chunks
- Update CASE_GROUND_TRUTH.yaml with new findings
- Determine what response is needed

#### PACER/ECF Download Verification (CRITICAL — 2026-06-23)
Before proceeding with drafting, verify PACER/ECF order PDFs referenced in CASE_FACTS.md exist in intake/:
- Search CASE_FACTS.md for `intake/` file references (e.g., `intake/8coAF8FdJ.pdf`)
- Check if each referenced file exists in the intake/ folder
- If a file is MISSING: document as "PACER/ECF download required — [filename] not in intake"
- Do NOT draft responses to orders that haven't been downloaded yet
- Download missing PACER/ECF PDFs via PACER/ECF portal before continuing

### Step 5: DRAFT
- Generate the correct motion/brief/response
- Use court-specific formatting rules (FRCP for federal, Pa.R.C.P. for state)
- Reference exhibits by Dkt number from intake/docket
- Save to obsidian_sync/cases/<slug>/drafts/ using write_file
- NEVER output only to chat â€" ALWAYS save file

### Step 6: EXHIBITS  
- Pull evidence from intake/ folder
- Label as Exhibit A, B, C using docket entry numbers
- Compile exhibit appendix with continuous page numbering

### Step 7: SCORECARD
- Generate violation scorecard with ALL docket references
- Include ECF file numbers from intake/docket.pdf
- Flag FATAL vs SERIOUS vs INFORMATIONAL
- Save to obsidian_sync/cases/<slug>/drafts/scorecard_YYYYMMDD.md

### Step 8: PRINT LIST
Output a simple list:
READY TO PRINT:

cases/fed_transunion/drafts/Motion_to_Strike.md
cases/fed_transunion/drafts/scorecard.md
cases/fed_transunion/drafts/Exhibit_A.pdf


## WHAT CHANGES PER COURT
ONLY the drafting format in Step 5:
- Federal (EDPA): FRCP, Local Rules, 28 USC
- PA State (CCP): Pa.R.C.P.
- Third Circuit: FRAP, LAR
- NJ: NJ Court Rules

Everything else is identical.

## SUPPORT FILES
- `references/pacer-ecf-verification-20260623.md` — Session findings for PACER/ECF download verification (2026-06-23)
- `references/submission-ledger-verification-20260703.md` — Full pattern for submission ledger verification
- `references/case-folder-splitting-pattern.md` — When a case gets a new order file, it may land in a separate folder (e.g., phila_ccp_? vs {{CASE_SLUG}}) — same case, different slug
- `references/1925b-statement-proof-sheet-pattern.md` — 1925(b) Statement proof sheet UNBACKED cite pattern (2026-07-23)

## RULES
- NEVER skip steps
- NEVER draft before review
- NEVER output to chat without saving file
- ALWAYS read full intake/ before drafting
- ALWAYS include docket numbers in scorecards
- ALWAYS list what needs printing at the end

## PITFALL: Case folder splitting (2026-07-23)
When a case receives a new order file, the intake may land in a separate folder from the canonical case folder. The new folder will have NO CASE_GROUND_TRUTH.yaml or CASE_FACTS.md. Before failing the pipeline, read the intake files to find the case ID, search existing cases for a match, and treat the split folder as part of the existing case. See `references/case-folder-splitting-pattern.md` for the full pattern.

## PITFALL: Slug variants (2026-07-23)
The same case may appear under multiple slugs: `{{CASE_SLUG}}` vs `fed_2_{{CASE_ID}}`, `{{CASE_SLUG}}` vs `fed_2_{{CASE_ID}}`, `{{CASE_SLUG}}` vs `fed_2_{{CASE_ID}}`. The ledger tracks both variants. When checking the ledger, search for both slug forms — a case may have a ledger entry under a different slug than the folder name.
