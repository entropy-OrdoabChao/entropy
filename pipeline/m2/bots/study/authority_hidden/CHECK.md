# CHECK — authority_hidden

PASS = each claim returns either a quoted binding holding or a pull_candidates list with reporter cites from the treatise. Pair: a claim where Am Jur names the SCOTUS case (pull_candidates filled) vs one already cited in the draft (GROUNDED).

## How to run
`python3 -m pipeline.m2.study_runner <draft.md> --bots authority_hidden`
Parity before promotion: equal-or-better on 3 consecutive real matters + skeptic stamp.
