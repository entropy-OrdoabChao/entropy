# CHECK — capacity

PASS = every count names obligor + obligee + capacity with the instrument clause and the rule quoted. Pair: insurer as party to the policy (GROUNDED) vs adjuster named without a duty source (GAP).

## How to run
`python3 -m pipeline.m2.study_runner <draft.md> --bots capacity`
Parity before promotion: equal-or-better on 3 consecutive real matters + skeptic stamp.

## Pair — official vs individual capacity (S-amjur-gaps 2026-10-20)
FAULT: damages under 1983 against an official "in his official capacity" → that is the State (Will) — flag.
NOT A FAULT: the same official named "individually" for his own act.

## Pair — anticipated immunity (S-m2-sovereign-immunity-anticipation 2026-10-20)
FAULT: a negligence / failure-to-supervise claim against a State agency that never addresses the tort-claims act's immunities (e.g. N.J.S.A. 59:2-3 discretion, 59:2-4 / 59:3-5 failure to enforce any law, 59:2-10 entity not liable for employee fraud/malice) -> GAP: list each immunity, its exception, and the defendant left liable.
NOT A FAULT: the claim names the employee individually for actual fraud / actual malice / willful misconduct and pleads the exception (N.J.S.A. 59:3-14(a)) with dated acts.
