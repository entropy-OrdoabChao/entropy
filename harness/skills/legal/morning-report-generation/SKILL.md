---
type: legal
description: Generate morning report from kanban, cron, new claims, and draft data
name: morning-report-generation
---

# Morning Report Generation

## Steps

1. **Gather kanban data**: `hermes kanban stats` and `hermes kanban list --status blocked` and `hermes kanban list --status running`
2. **Gather cron failures**: `hermes cron list` and filter for error/timeout/failed
3. **Gather new claims**: Read `{{ENTROPY_HOME}}/output/new_claims_queue.json` for needs_owner_review, promoted, and skipped items
4. **Gather drafts**: Check `{{ENTROPY_HOME}}/output/new_claim_drafts/` for recent drafts
5. **Gather lessons**: Check `{{ENTROPY_HOME}}/obsidian_sync/learning/` for recent lessons
6. **Write report**: Create file at `{{ENTROPY_HOME}}/obsidian_sync/00_system/morning_report.md`

## Report Structure

- System Status (MCP, Gateway, LLM, Docker, Disk)
- Kanban Task Classification (blocked vs running, DIAGNOSTIC vs LEGITIMATE)
- Tasks Completed (past 6 hours)
- Drafts Produced (past 6 hours)
- Lessons Logged (past 6 hours)
- Scores (past 6 hours)
- Cron Job Failures (classification: ILLUSORY vs LEGITIMATE)
- New Claims Queue (needs_owner_review, promoted, skipped, staleness check)
- Issues Requiring Attention (severity, auto-repair possible?)
- Issues Noted (no action needed)

## Key Patterns

- MCP tools work via native stdio even when Docker is down
- Cron model drift errors are ILLUSORY — LLM is available, job pinned to wrong model
- Docker/container checks not performed in headless cron mode
- New claims queue may have missing defendant data (data gap)
