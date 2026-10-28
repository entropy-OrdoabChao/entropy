# CHECK — violation

PASS = every claim's `proposition` is two sentences: `Rule:` with verbatim mandatory words + a verified citation, then
`Violation:` with the record act and its ECF/Dkt number and date. No third sentence, no characterization.

## How to run
`python3 -m pipeline.m2.analysis_runner <issues.md> --bots violation`
Litmus: `python3 -m pipeline.m2.litmus_check <LITMUS.json> <cells.jsonl>` — the violation rows must carry the litmus
record markers (ECF numbers) and rule cites without hand-injected cells.

## Pair — mandatory vs discretionary
FAULT (a row): "Rule: “the clerk must enter the party's default.” Fed. R. Civ. P. 55(a). Violation: default motions
against a defendant that never appeared were denied (ECF 41, 98)."
NOT A ROW (GAP): "Rule: “the court may set aside an entry of default for good cause.” Fed. R. Civ. P. 55(c)." — `may`
leaves discretion; it is the opponent's rule, not a violation.

## Pair — section number
RIGHT: § 1030 for "a designation of the final judgment is sufficient to bring up for review all interlocutory orders"
(the section body heading is `§ 1030. —Prejudgment orders.`). WRONG: § 1031 (that page's running header).
