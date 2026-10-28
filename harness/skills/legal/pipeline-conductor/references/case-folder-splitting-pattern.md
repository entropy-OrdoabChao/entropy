# Case Folder Splitting Pattern

## Problem
When a case receives a new order file, the intake may land in a separate folder from the canonical case folder. The pipeline-conductor must recognize these as the same case.

## Example (2026-07-23)
- **Canonical case**: `{{CASE_SLUG}}` ({{USER_LAST}} v. {{OPPOSING_PARTY}}) — has CASE_GROUND_TRUTH.yaml, CASE_FACTS.md, etc.
- **Split folder**: `phila_ccp_?` — has intake/ with new {{JUDGE}} order files but NO CASE_GROUND_TRUTH.yaml
- **Same case**: Both folders reference "{{USER_LAST}} v. {{OPPOSING_PARTY}} et al. Case ID: {{CASE_ID}}" — confirmed by matching case ID in the order text

## Detection
When a new case folder appears without CASE_GROUND_TRUTH.yaml or CASE_FACTS.md:
1. Read the intake files to find the case ID (usually in the order text or cover sheet)
2. Search existing cases for matching case ID
3. If found, treat the new folder as a split of the existing case — merge intake, use existing CASE_GROUND_TRUTH.yaml
4. If no match found, create a new CASE_GROUND_TRUTH.yaml

## PITFALL
The pipeline-conductor's Step 4 (REVIEW) reads CASE_GROUND_TRUTH.yaml. If the case folder is split, the new folder will have no CASE_GROUND_TRUTH.yaml and the pipeline will fail to proceed. Always check for case ID matches when a folder lacks ground truth files.

## Another Example
- `fed_2_{{CASE_ID}}` and `{{CASE_SLUG}}` — same case, different slug formats
- `fed_2_{{CASE_ID}}` and `{{CASE_SLUG}}` — same case, different slug formats
- `fed_2_{{CASE_ID}}` and `{{CASE_SLUG}}` — same case, different slug formats

The ledger uses both slug variants (e.g., `our_filings_{{CASE_SLUG}}.json` and `fed_2_25_cv_02822`).
