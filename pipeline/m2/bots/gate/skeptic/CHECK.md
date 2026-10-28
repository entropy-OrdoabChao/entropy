# CHECK — skeptic

PASS = stamps clean rows, returns rows with unverified quotes, never stamps a row whose quote the runner marked unverified. Pair: a row with verified quotes (STAMP) vs a row with a plausible but unquoted cite (RETURN).

## How to run
`python3 -m pipeline.m2.study_runner <draft.md> --bots skeptic`
Parity before promotion: equal-or-better on 3 consecutive real matters + skeptic stamp.
