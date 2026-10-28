# learnings.jsonl Write Pattern

**Trigger**: Intake processing or any cron job that produces durable findings

**Doctrine**: CRON_DOCTRINE.md Rule 7 — write to topic log AND append one line to learnings.jsonl

**Format**:
```json
{"text": "<SHORT grounded finding + which active case(s) it bears on>", "cite": "<controlling authority + corpus coordinate: source doc / chunk id>", "tags": ["tag1", "tag2"], "source": "<job-name>", "cases": ["case-id-1", "case-id-2"]}
```

**CRITICAL**: The JSONL line is what `pipeline/connected_notes.py` turns into a linked atomic note in the graph. A note written only under `studies/` or `comprehension/` without the JSONL line risks staying blind.

**Always name the active case(s) the finding applies to** — read meta/CASE_GROUND_TRUTH.yaml to get the canonical case ID.

**PATTERN STATUS**: PROVEN WORKFLOW — Mandatory for all cron jobs producing durable findings
