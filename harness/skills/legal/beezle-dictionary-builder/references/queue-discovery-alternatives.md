# Queue Discovery Alternatives

## Problem
The skill's Step 1 recommends PowerShell `Get-ChildItem` commands for queue discovery. On Windows, the `terminal` tool often returns exit code 126 (command not found), making PowerShell commands unreliable.

## Solution: MCP-Based Discovery

Use `mcp_beezle_list_obsidian_notes` to enumerate the queries directory. This is the preferred method when terminal fails.

### Command
```python
mcp_beezle_list_obsidian_notes(subdir="queries/definitions")
```

### Parsing the Result
Returns JSON with structure:
```json
{
  "vault_root": "{{ENTROPY_HOME}}\obsidian_sync",
  "subdir": "queries/definitions",
  "count": 13,
  "notes": [
    "queries/definitions/federal_reserve_note__treasury.md",
    "queries/definitions/processed/federal_reserve_note__treasury.md",
    "queries/definitions/judicial_immunity__federal.md",
    ...
  ]
}
```

### Filtering for Pending Items
1. Filter `notes` array for entries NOT containing `processed/` in the path
2. For each candidate, read the file with `mcp_beezle_read_obsidian_note`
3. Check YAML frontmatter for `status: processed`
4. Keep only files where `status` is missing or not `processed`

### Example Workflow
```
# List all files
mcp_beezle_list_obsidian_notes(subdir="queries/definitions")

# Filter candidates (exclude processed/ subfolder)
candidates = [n for n in result["notes"] if "processed/" not in n]

# For each candidate, verify not already processed
for candidate in candidates:
    content = mcp_beezle_read_obsidian_note(note_path=candidate)
    if "status: processed" not in content["content"]:
        pending_queue.append(candidate)
```

## When to Use Each Method

| Method | Use When |
|--------|----------|
| PowerShell `Get-ChildItem` | Terminal is available and reliable (rare on Windows) |
| `mcp_beezle_list_obsidian_notes` | Terminal fails (exit 126), or for batch processing multiple queue items |

## Session Example (2026-05-13)
All 5 non-processed files in queue (`federal_reserve_note`, `judicial_immunity`, `lawful_money`, `negotiable_instrument`, `real_party_in_interest`, `standing`) were already marked `status: processed` from previous cron runs. Using `mcp_beezle_read_obsidian_note` on each confirmed cache hits, resulting in `[SILENT]` response.
