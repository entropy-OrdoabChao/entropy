---
name: legal-research
description: Legal research and analysis — querying the Beezle corpus, Obsidian vault, and legal databases to produce comprehensive legal research reports on any topic.
status: active
---

# Beezle Legal Research

## Purpose
Query the Beezle legal corpus, Obsidian vault, and legal databases to produce comprehensive legal research reports on any topic.

## What it does
- Semantic search over indexed legal corpus (ChromaDB at http://localhost:8100)
- Keyword search over vault citation index
- File-based search for UCC definitions, IRS research, case law
- Synthesis of findings into structured research reports

## Core tools needed
- `chromadb-search` skill — query ChromaDB at http://localhost:8100
- `search_files()` — find files by name or content in the corpus
- `read_file()` — read specific definition files (UCC, IRS, etc.)
- `trace_legal_authority()` — resolve citations to vault nodes
- `resolve_citation()` — resolve citation to chunks
- `search_obsidian()` — keyword search citation index
- `definition_lookup()` — look up legal term definitions
- `query_legal_db()` — query legal database (requires chromadb installed)

## Sources to check first
1. **UCC definitions**: `{{ENTROPY_HOME}}/definitions/_ucc_ingested/` — already ingested UCC sections
2. **IRS research**: `{{ENTROPY_HOME}}/obsidian_sync/learning/irs/` — Form 1099-C, tax research
3. **Treasury lane**: `{{ENTROPY_HOME}}/obsidian_sync/learning/treasury/` — negotiable instruments, lawful money
4. **Existing debt discharge**: `{{ENTROPY_HOME}}/obsidian_sync/studies/debt_discharge/` — prior research
5. **Vault index**: `obsidian_index_stats()` — check if citation index is built before searching
6. **Beezle DB**: `query_legal_db()` — query the legal database for statutes, rules, case law

## Pitfalls
- **ChromaDB may not be running**: Check at http://localhost:8100 before querying. If down, fall back to file-based search.
- **query_legal_db() requires chromadb installed**: If pip install chromadb fails, use file-based search instead.
- **Citation index may be stale**: Run `obsidian_index_stats()` to verify before relying on `search_obsidian()`.
- **Existing research files may be incomplete**: Always check for prior research before starting from scratch.

## Output
Structured legal research report with primary source citations, analysis, and practical applications.

## Workflow
1. Identify the legal topic and key concepts
2. Check for existing research (search files, vault)
3. Query ChromaDB for semantic search
4. Search file-based sources (UCC definitions, IRS research, etc.)
5. Resolve key citations to vault nodes
6. Synthesize findings into structured report
7. Include practical applications and strategy

## Support files
- `references/debt-discharge-research.md` — Debt discharge law research (UCC Article 3, accord and satisfaction, 1099-C, negotiable instruments, HDC doctrine)
