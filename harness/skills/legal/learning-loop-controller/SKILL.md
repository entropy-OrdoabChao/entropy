---
name: learning-loop-controller
description: Self-learning loop. After every legal task, detects authority gaps, auto-fetches missing statutes/rules/binding precedent (including from other jurisdictions under Full Faith and Credit), stages them in a review queue, ingests on approval, and re-validates the chain. Supports dynamic jurisdiction discovery — not hardcoded.
version: 1.1.0
author: Hermes Legal
metadata:\n  hermes:\n    tags: [legal, self-learning, ingest, gap-detection, full-faith-credit, jurisdiction-discovery, mcp-tools]\n    related_skills: [law-lookup, courtlistener-search, db-ingest, binding-precedent-enforcer, doctrine-note-validation]
prerequisites:
  files: [~/.hermes/legal_config.json]
  skills: [law-lookup, courtlistener-search, db-ingest]
---

# Learning Loop Controller

## Purpose
Make Hermes get smarter after every task. Detect what was missing, fetch it, stage it for review, ingest on approval, re-validate.

Three-phase cycle:
1. **Detect** — scan element results for `MISSING AUTHORITY` and `NOT ESTABLISHED` flags
2. **Fetch** — auto-retrieve from law-lookup, CourtListener, jurisdiction-specific sources, and other jurisdictions under Full Faith and Credit (U.S. Const. art. IV § 1 + 28 U.S.C. § 1738)
3. **Stage → Approve → Ingest → Re-validate**

## Full Faith and Credit support
When primary jurisdiction lacks binding authority on a question, the loop searches sister-state authority for persuasive value AND federal authority for binding value. New jurisdictions can be discovered and added to `legal_config.json` dynamically — no pre-seeding required.

Authorities for cross-jurisdictional reach:
- **U.S. Const. art. IV § 1** — Full Faith and Credit Clause
- **28 U.S.C. § 1738** — federal implementing statute
- **Allen v. McCurry**, 449 U.S. 90 (1980) — preclusion across jurisdictions

## MCP Tool Availability and Fallback Workflows

### Known Tool Status (as of May 2026)

The following MCP-based research tools are EXPECTED but may NOT be available:

| Tool | Status | Fallback |
|------|--------|----------|
| `chromadb-search` | **NOT FOUND** | Manual browser + `db-ingest` |
| `beezle-research` | **NOT FOUND** | Manual browser + web extract |

### When MCP Tools Fail

If `learning-loop-controller` or related skills fail with "NOT FOUND" errors:

1. **Do NOT panic** — MCP tools work via native stdio even when Docker is down
2. **Document the failure** — note tool name and error in session log
3. **Activate fallback workflow** — manual browser research
4. **Ingest results** — use `db-ingest` skill after manual research

### Manual Browser Research Workflow

When MCP tools are unavailable, use this fallback pattern:

```
1. Navigate to authoritative source (Cornell LII, IRS.gov, etc.)
2. Extract relevant statutory/regulatory text
3. Document source URL and date accessed
4. Use db-ingest to add to ChromaDB
5. Proceed with task using retrieved authority
```

**Key sources for manual research:**
- **Statutes:** https://www.law.cornell.edu/uscode/text/26
- **Forms:** https://www.irs.gov/pub/irs-pdf/
- **Instructions:** https://www.irs.gov/instructions/
- **Courtlistener:** https://www.courtlistener.com/

### IRS Research Example

When researching IRS tax recovery topics (May 2026):

**Expected tools (NOT AVAILABLE):**
- `chromadb-search` for database lookup
- `beezle-research` for Beezle MCP research

**Fallback executed:**
1. Manual browser navigation to 6+ sources
2. Cornell Law LII for 26 USC § 7422, § 6330
3. IRS.gov for Form 1040-X, Form 843, Form 1099-C
4. IRS IRB (Internal Revenue Bulletin) for notices
5. Taxpayer Advocate Service pages
6. Compiled to comprehensive report in Obsidian

**Output:** `{{ENTROPY_HOME}}\obsidian_sync\studies\irs_tax_recovery\IRS_TAX_RECOVERY_RESEARCH_REPORT.md`

### Pattern Recognition

If MCP tools fail repeatedly:
1. Check MCP server status via `check-mcp-status`
2. Verify Docker containers: `docker ps`
3. Check Beezle MCP config in `config.yaml`
4. Document pattern in `~/.hermes/lessons/`
5. Consider manual workflow as primary for certain tasks

## When to Use
- Step 16 of SOUL.md pipeline (after EVERY legal task)
- Manually after a DB search returns insufficient authority
- When testing whether sister-state law fills a PA gap
- **When MCP research tools fail** — activate manual browser workflow

## Usage
```bash
# Run after a task — detect gaps, fetch, stage for review
~/.hermes/skills/legal/learning-loop-controller/scripts/loop.sh \
  --task-context /tmp/task_context.json \
  --gaps /tmp/hermes_gaps.jsonl \
  --jurisdiction edpa \
  --auto-fetch \
  --stage-only

# After review, ingest staged authority
~/.hermes/skills/legal/learning-loop-controller/scripts/loop.sh \
  --ingest-staged

# Discover new jurisdiction
~/.hermes/skills/legal/learning-loop-controller/scripts/loop.sh \
  --discover-jurisdiction "ndca" \
  --register

# Re-validate chain after ingestion
~/.hermes/skills/legal/learning-loop-controller/scripts/loop.sh \
  --revalidate /tmp/logic_chain.json
```

## Phases

### Phase A — Gap Detection
Reads:
- `legal-analysis` element results (Step 9 output)
- `binding-precedent-enforcer` walls with empty `binding_chain`
- `definition-lookup` results marked `NO_STATUTORY_DEFINITION`

Output: `~/.hermes/data/learning/gaps_{date}.jsonl`

### Phase B — Auto-Fetch (parallel)
For each gap:
1. `law-lookup` (USC, CFR, state codes by jurisdiction)
2. `courtlistener-search` (binding court for jurisdiction + sister states)
3. Sister-jurisdiction search under Full Faith and Credit
4. Court website for local rules / standing orders
5. Federal Register for regulatory amendments
6. **If MCP tools fail** — manual browser research to authoritative sources

### Phase C — Stage / Approve / Ingest
- Fetched authority lands in `~/.hermes/data/learning/staged/`
- Each item has metadata: source, hash, fetch date, gap it addresses
- Review with `--list-staged` 
- Approve with `--approve <id>` or `--approve-all`
- Ingest via `db-ingest` skill into ChromaDB with proper tags

### Phase D — Re-validate
After ingestion, re-runs `legal-analysis` on the original elements. If a previously MISSING element now has authority → updates the chain. If still MISSING → escalates with exact .gov URL needed.

## Storage
```
~/.hermes/data/learning/
├── gaps_{YYYYMMDD}.jsonl       # detected gaps log
├── staged/                      # fetched but un-ingested authority
│   ├── {hash}.txt
│   └── {hash}.meta.json
├── ingested/                    # archive of approved items
└── rejected/                    # items reviewed and rejected
```

## Output Format (gap detection)
```json
{
  "gap_id": "sha256_hash",
  "element": "string",
  "jurisdiction": "edpa",
  "missing_authority_type": "binding_precedent | statute | regulation | local_rule",
  "search_attempted": ["chromadb", "courtlistener", "law_fetcher"],
  "fetch_status": "pending | fetched | not_found | staged",
  "fetched_sources": [
    {"source": "courtlistener", "url": "...", "hash": "..."}
  ],
  "approval_status": "pending | approved | rejected"
}
```

## Rules
1. Never auto-ingest without staging first (default behavior)
2. Always hash fetched content to detect duplicates against existing DB
3. Never overwrite existing authority — flag conflict and run `authority-hierarchy-check`
4. Sister-state authority is tagged `persuasive` not `binding` unless Full Faith and Credit applies
5. New jurisdictions added to config require `--register` flag (explicit)
6. Re-validation MUST use the new authority — never silently skip
7. **Document MCP tool failures** — log to session and lessons directory
8. **Activate manual fallback** — don't wait for MCP tools to recover

## References
- IRS Tax Recovery Research: `{{ENTROPY_HOME}}\obsidian_sync\studies\irs_tax_recovery\IRS_TAX_RECOVERY_RESEARCH_REPORT.md`
- MCP Tool Status: Document in `~/.hermes/lessons/` for recurring failures

## Alternative Validation Path: Doctrine Note Validation
For tightening Obsidian doctrine notes (under `_hermes_learnings/notes/`) toward precise pinpoint cites, use the `doctrine-note-validation` skill instead of Phase D's element-level re-validation. This is the preferred path when the goal is narrowing draft arguments rather than validating legal-analysis element results.


## Active retrieval (use the shadow-study skill) — raises accuracy
Before recording a learning, run the shadow-study loop instead of passively retrieving:
1. RECALL from memory FIRST — write what you think the term/rule/doctrine means + your CONFIDENCE (high/med/low). Do NOT search yet. Label it a PREDICTION (not grounded).
2. VERIFY — now search_chunks / read_graph and compare.
3. Record ONLY the GAP where memory diverged (wrong / missing / low-confidence-but-actually-X). That gap is the hallucination point; the cited gap-correction is the high-value learning — not a re-summary of what you already knew.
Keep it one small topic per cycle and LINK the gap-note. See the shadow-study skill.
