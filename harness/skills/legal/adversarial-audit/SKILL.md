---
name: adversarial-audit
description: Adversarial review of legal drafts from opposing counsel, judge, and appellate reviewer perspectives before filing.
category: legal
triggers:
  - "adversarial audit"
  - "opposing counsel review"
  - "pre-filing adversarial review"
  - "simulate opposition"
  - "audit before filing"
---

# ADVERSARIAL AUDIT

**Pre-filing adversarial review of legal drafts.** Before ANY filing goes to the print queue, simulate the three strongest oppositions: opposing counsel, the judge, and an appellate reviewer.

## Why this matters

Drafts that pass internal validation often fail adversarial scrutiny. The validation engine scores format compliance, not argument strength. An adversarial audit catches:

1. **Opposing counsel attacks** — misstated legal basis, wrong procedural vehicle, arguments already rejected by the court
2. **Judge attacks** — same judge who already rejected the argument, arguments with no new evidence or new authority, futile filings
3. **Appellate reviewer attacks** — arguments that won't survive appellate review, missing appellate standards, arguments that create more problems than they solve

## Execution

### Step 1: Enumerate all drafts
- `mcp_beezle_lifecycle_list` → all documents across all statuses
- `mcp_beezle_lifecycle_list(status="validated")` → validated documents
- `mcp_beezle_lifecycle_list(status="drafting")` → drafting documents
- `mcp_beezle_filing_queue_list` → queue items

### Step 2: Get case context per draft
- `mcp_beezle_case_ground_truth(case_id)` → case posture, defendants, deadlines, pending motions
- `mcp_beezle_case_diagnose(case_id)` → defect patterns, judge info, recommended filings
- `mcp_beezle_get_case_score(canonical_id)` → quick health check (score 0-100, violation counts)

### Step 3: Adversarial review per draft
For each draft, simulate three perspectives:

**Opposing counsel:**
- Does the motion misstate the legal basis? (e.g., FRCP 12(d) doesn't authorize striking citations)
- Is the procedural vehicle wrong? (e.g., PA state court has no FRCP 12(d) equivalent)
- **FRCP vs Pa.R.C.P. mismatch:** FRCP rules cited in PA Common Pleas are WRONG — use Pa.R.C.P. equivalents (e.g., FRCP 60(d)(3) → Pa.R.C.P. 238; FRCP 12(d) → Pa.R.C.P. 1028). This is a FATAL error.
- Are arguments already rejected by the court? (no new evidence, no new authority)

**Judge:**
- Is this the same judge who already rejected these arguments? (futile filing)
- Does the motion present new evidence or new legal authority?
- Does the judge have documented bias against the plaintiff? (see bias violation categories)

**Appellate reviewer:**
- Will this argument survive appellate review?
- Are the appellate standards met? (e.g., 28 U.S.C. § 1292(b) certification standard)
- Does the motion create more problems than it solves?

### Step 5: Write audit report
- Write full audit report to `obsidian_sync/cases/_hermes/audits/adversarial_audit_<date>.md`
- Write redraft tasks to `obsidian_sync/cases/_hermes/tasks/redraft_tasks_<date>.md`
- Write cron log to `obsidian_sync/cases/_hermes/logs/adversarial_audit_log_<date>.md`

## Pitfalls

1. **Pipeline produces NO output files** — All "validated" documents are empty. The validation engine reports "No output files generated" — this is a systematic pipeline failure, not a content issue. Never trust "validated" status without verifying actual files exist.
2. **Scoring engine is broken** — All scores = 0, all ranks = "?". Cannot use scores for prioritization.
3. **Audit engine is broken** — Scorecard not found for all cases. Cannot use audit data for prioritization.
4. **All documents are overdue** — No deadlines have been updated since creation.
5. **Manual advancement of documents** — Documents are being manually advanced to "validated" status without actual filings, creating false confidence in the filing queue.
6. **READY_TO_FILE ≠ FILED** — A packet marked READY_TO_FILE may never have been submitted to court. Always check the submission ledger (`pipeline.submission_reconciler`) before declaring a document filed. The ledger is the source of truth for filed status.
7. **Broken packets** — Queue items may contain only `checklist.md` with no actual filing document. Verify all expected files (motion.docx, scorecard, certificate_of_service) exist before proceeding.
8. **Do NOT audit scorecard-only files** — Scorecards are exhibits, not filings. Skip them in the audit.
9. **Do NOT audit drafts already in the submission ledger** — They are FILED. The ledger includes ECF notification data — if a document appears there, it has been submitted to court regardless of what `filing_queue_list` shows.
10. **Always check certificate of service** — Placeholder text (e.g., "[TO BE COMPLETED]", "[TO BE CONFIRMED]") is a REVISION finding. Verify counsel_of_record placeholders are replaced.
11. **Check if draft addresses ALL active defendants** — Missing a defendant is a FATAL finding. Cross-reference with `case_ground_truth` defendant list.
12. **Check statute of limitations** for new claims in amended complaints.
13. **Case folders can be empty** — `{{CASE_ID}}`, `{{CASE_ID}}`, and `{{CASE_ID}}` all have 0 items in their case directories. An empty case folder does NOT mean no case data exists. Check `CASE_GROUND_TRUTH.yaml` first.
14. **Beezle MCP registry and case_map.json are SEPARATE** — A case can be in one but not the other. Always check Beezle MCP registry first (`mcp_beezle_list_cases`), use case_map.json as fallback only.
15. **Most recent files in output directories are often scorecards, not filings** — When searching for "most recent drafts," filter for actual filing documents (motions, briefs, complaints), not scorecards or scorecard_exhibit files.
16. **adversarial-audit skill may not appear in available_skills** — The skill exists at `legal/adversarial-audit` but may not be listed in the available_skills index. If `skill_view(adversarial-audit)` fails, check `/home/{{USER_FIRST}}/.hermes/skills/legal/adversarial-audit/SKILL.md` directly.

## Output Structure