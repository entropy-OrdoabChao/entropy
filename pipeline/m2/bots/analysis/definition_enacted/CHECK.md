# CHECK — definition_enacted

PASS = for each defined term in the specimen the bot returns the statute's definitions clause verbatim, its means/includes scope, and flags any cross-statute import. Pair: 'consumer' under FCRA (closed, GROUNDED) vs 'bad faith' under 42 Pa.C.S. 8371 (no statutory definition → GAP, hands to definition_judicial).

## How to run
`python3 -m pipeline.m2.study_runner <draft.md> --bots definition_enacted`
Parity before promotion: equal-or-better on 3 consecutive real matters + skeptic stamp.
