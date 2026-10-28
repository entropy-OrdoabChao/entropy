---
name: mcp-state-caseid-quirks
description: Case ID resolution quirks for Beezle MCP state cases. Use when `case_full_filing_package` or other MCP tools return "not found" for state docket IDs. Encodes the underscore vs colon routing bug.
version: 1.0.0
---

# MCP State Case ID Quirks

## The Bug

`mcp_beezle_case_full_filing_package` (and likely other MCP tools) resolve state case IDs with **underscore** between "phila" and "ccp":

- **Correct:** `{{CASE_ID}}` (underscore)
- **Broken:** `phila:ccp:{{CASE_ID}}` (colon)

The user-facing docket format and documentation use colon form. The registry stores underscore. The tool returns "case_id not found" when given colon form.

## Fix Pattern

**Always call `list_cases` first** when working with state case IDs before calling any filing-package or analysis tools. The list output shows the canonical_id in the correct form.

## Scope

- Federal cases (`fed:2:25-cv-NNNNN`): colon form works correctly.
- State cases (`phila_*`): underscore form required by MCP tools.
- This is a registry-level mismatch, not a user-facing convention.

## Session Log

- 2026-06-02: User asked for `phila:ccp:{{CASE_ID}}`, tool returned "not found". Discovered `{{CASE_ID}}` via `list_cases`.
