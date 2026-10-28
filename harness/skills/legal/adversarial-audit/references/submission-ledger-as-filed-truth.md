---
date: 2026-07-03
type: reference
source: adversarial-audit-cron
---

# Submission Ledger as FILED Truth

## CRITICAL (2026-06-29): A drafted packet does NOT equal a filed document.

A packet at `output/filings/<case>/packet_*/` may exist with a .docx file but was never submitted to the court. Always check the ledger (not just the packet directory) before declaring a document filed.

## How to refresh + read the ledger

```bash
cd {{ENTROPY_HOME}} && PYTHONPATH={{ENTROPY_HOME}} python3 -m pipeline.submission_reconciler
```

Then read:
- `output/filings/reconciliation_report.md` — human-readable summary
- `output/filings/our_filings_<slug>.json` — structured data

## What the ledger tells you

- A doc in the ledger → **FILED**, its response deadline is **CLOSED** → NEVER flag it stuck/overdue
- A document absent from the ledger is **NOT FILED** — it may be stuck in the packet directory awaiting submission
- Only treat a doc as stuck/overdue if it is **absent from the ledger** AND past a real due date

## Ledger refresh is nightly

The ledger also refreshes nightly (`submission_reconcile_nightly.sh`). See the `filing-status-from-ledger` skill.

## Key finding from 2026-07-03 audit

- Queue item `q_20260626_101930` ({{CASE_ID}} reply brief) was marked READY_TO_FILE but NOT in ledger
- Queue item `q_20260623_183949` ({{CASE_ID}}) had only checklist.md — broken packet
- Both items were NOT FILED despite appearing ready in the queue