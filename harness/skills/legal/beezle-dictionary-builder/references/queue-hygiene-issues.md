# Dictionary-Builder Queue Hygiene Issues

## Stale Processed Files in Queue (2026-06-16)

The dictionary-builder heartbeat session processes terms from the queue but
finds they are already marked `status: processed` — the queue has 15 items
but most are stale duplicates.

**Symptoms**:
- Session processes 5 terms, all return Tier 2 cache hits
- No new definitions written
- Session returns [SILENT] because no new terms need processing
- Queue has 15 items total — many are stale duplicates

**Root Cause**:
- Tier 2 cache hits mark files as processed but don't remove them from the queue
- The queue is a flat directory — no mechanism to distinguish active vs stale entries
- Queue status check files (`queue_status_check.md`, `status_check.md`) are also in the queue directory

**Fix**:
1. Add a `last_processed` timestamp to processed markers
2. Add a queue cleanup cron job that removes files older than N days with `status: processed`
3. Remove `queue_status_check.md` and `status_check.md` from the queue directory

**Status**: 3-day streak, no fix applied.
