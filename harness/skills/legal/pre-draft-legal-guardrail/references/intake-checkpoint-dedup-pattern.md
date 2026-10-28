# Intake Checkpoint Dedup Pattern (PROVEN)

**Purpose:** Avoid reprocessing files that were already analyzed in prior cron runs.

**Mechanism:** `intake_checkpoint.json` at `{{ENTROPY_HOME}}/output/intake_checkpoint.json`

```json
{
  "done_shas": ["sha1hex1", "sha1hex2", ...],
  "last_update": 1778692673.769381
}
```

**Execution:**
1. Read checkpoint: `json.load(open("{{ENTROPY_HOME}}/output/intake_checkpoint.json"))`
2. For each file in intake dir, compute SHA-1: `hashlib.sha1(f.read()).hexdigest()`
3. Files whose SHA is in `done_shas` → SKIP (already processed)
4. Files whose SHA is NOT in `done_shas` → process, then add SHA to checkpoint

**Why SHA-1 (not filename):** Filenames can be reused, renamed, or duplicated across runs. SHA-1 is the only reliable dedup key.

**Known state (2026-07-02):** 1331 files marked done, last update ~2026-07-02.

**Pitfall:** Do NOT trust `intake_analysis_log.md` file counts — they are stale (Pattern 19). Always verify against actual directory listing + checkpoint SHA set.
