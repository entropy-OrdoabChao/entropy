# FCRA Statutory Text Sources

When building definitions for FCRA statutes, the actual statutory text is NOT in ChromaDB. Use these primary sources:

## USCODE_LII directory (preferred)
- `{{ENTROPY_HOME}}/obsidian_sync/primary_sources/USCODE_LII/title15/USCODE-title15-sec1681o.txt` — § 1681o (negligent noncompliance)
- `{{ENTROPY_HOME}}/obsidian_sync/primary_sources/USCODE_LII/title15/USCODE-title15-sec1681s-2.txt` — § 1681s-2 (furnisher duties)
- `{{ENTROPY_HOME}}/obsidian_sync/primary_sources/USCODE_LII/title15/USCODE-title15-sec1681n.txt` — § 1681n (willful noncompliance)

## mcp_beezle_source_fetch (dynamic)
- `source="usc", params={"title": 15, "section": "1681o"}` — fetches from uscode.house.gov

## Note
- The primary source index may reference files that don't exist (e.g., `usc_15_sec_1681o.md` showed as 4130 bytes but was empty). Always verify by reading the file.
- USCODE_LII files are the most reliable — they contain the full statutory text with editorial notes.
