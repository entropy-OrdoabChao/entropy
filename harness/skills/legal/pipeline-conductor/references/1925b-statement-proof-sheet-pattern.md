# 1925(b) Statement Proof Sheet Pattern

## Problem
When a PA R.A.P. 1925(b) Statement of Errors is drafted and filed, the proof sheet lists **UNBACKED cites** — citations in the Statement that cannot be verified against the corpus. These need corpus grounding before the Statement is reliable.

## Pattern (2026-07-23, {{CASE_SLUG}})
The proof sheet (`proof_sheet_1925b_dkt88.md`) listed 6 UNBACKED cites:

| Citation | Issue | Resolution |
|----------|-------|------------|
| Pa.R.A.P. 1925(b) | Text discusses Rule 1925(a) and sanctions, not preservation | Search for Rule 1925(b) preservation text in corpus |
| Pa.R.C.P. 3111(b) | Text cites Pa.R.C.P. 1019, not 3111(b) | Replace with Pa.R.C.P. 1019 |
| Pa.R.C.P. 3111(b) | Text discusses Rule 3111.1, not 3111(b) | Replace with Pa.R.C.P. 3111.1 |
| Pa.R.A.P. 1925(b)(4)(vii) | Text cites 1925 but does not address mutuality or voidness | Find corpus text for Rule 1925(b)(4)(vii) |
| Pa.R.C.P. 1024 | Text defines verification, not default judgment or deemed admissions | Find corpus text for Pa.R.C.P. 1024 |
| Pa.R.A.P. 1925(b) | Text discusses Rule 1925(a) opinion, not service requirements | Find corpus text for Rule 1925(b) service |

## Detection
When a 1925(b) Statement is filed:
1. Check for `proof_sheet_1925b_dkt<NNN>.md` in the drafts folder
2. If UNBACKED cites exist, search the corpus for each citation
3. Replace UNBACKED cites with corpus-verified text
4. If a citation is NOT IN CORPUS, flag as such and note the gap

## PITFALL
The proof sheet only lists UNBACKED cites — it does NOT list verified cites. Do NOT assume the Statement is complete just because the proof sheet is short. A short proof sheet may mean the drafter used only a few citations, not that all citations are verified.

## Location
Proof sheets are typically at: `drafts/_replies/chain_reply_<NNN>/proof_sheet_1925b_dkt<NNN>.md`
