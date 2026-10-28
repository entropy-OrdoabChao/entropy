# CHECK — pleading_sufficiency

PASS = run over Progressive's reply brief (intake/progressive_reply_brief_to_response_in_opposition_20261006.pdf.doctext.txt)
every FAULT carries the opponent's exact words + a verified rule quote; a paragraph that meets the rule is NOT flagged.
Pair: a bare conclusion ("Plaintiff fails to state a claim") vs a paragraph that pleads a dated fact.

## How to run
`python3 -m pipeline.m2.analysis_runner <filing.txt> --bots pleading_sufficiency --facts cases/<slug>/CASE_FACTS.md`

## Pair — deemed admissions (S-amjur-gaps 2026-10-20)
FAULT: opponent's pleading contests a fact it already admitted by not answering requests for admission in time → flag, quote the request + the lapse date.
NOT A FAULT: a timely served written answer or objection to the request.
