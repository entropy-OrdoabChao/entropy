# CHECK — authority_holding

PASS = each opponent cite returns holding-quote vs attributed-quote with MATCH / MISSTATED. Pair: Danson as the opponent reads it vs as it reads (surreply point 3).

## How to run
`python3 -m pipeline.m2.study_runner <draft.md> --bots authority_holding`
Parity before promotion: equal-or-better on 3 consecutive real matters + skeptic stamp.
- Every quoted holding carries `speaker: court|party` — `party` whenever the excerpt is RECITAL=yes or the quoted sentence sits inside an argument summary; a `party` quote is never reported as the holding.
