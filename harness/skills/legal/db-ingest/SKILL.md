---
name: db-ingest
description: Ingest new legal sources into the Beezle corpus chunk_text.db (ChromaDB-free) during research. Handles text, definitions, statutes, regulations, case law, and bulk file ingestion. Use whenever you find law that should be in the database for future retrieval.
version: 1.0.0
metadata:
  hermes:
    tags: [legal, ingestion, chromadb, database, storage, pipeline]
    requires_toolsets: [terminal]
---

# Database Ingestion

Add new legal sources to the Beezle corpus chunk_text.db (ChromaDB-free) during research or bulk processing. Supports individual text snippets, definitions, full documents, and bulk file ingestion. Everything ingested becomes searchable for future queries.

## When to Use

- Found a statute/regulation via CourtListener that's not in the DB
- Downloaded new regulatory body rules (OCC, CFPB, Fed Reserve)
- Ingesting Black's Law / Bouvier's dictionaries
- User provides case documents to add to the knowledge base
- Any time you find useful legal text that should be retrievable later

## Quick Reference

| Task | Command |
|------|---------|
| Ingest single text | `ingest_text.py --text "..." --source "name" --type "statute"` |
| Ingest definition | `ingest_text.py --text "..." --source "blacks_law" --type "definition" --term "estoppel"` |
| Ingest file | `ingest_text.py --file /path/to/file.txt --source "name" --type "statute"` |
| Ingest directory | `ingest_text.py --dir /path/to/dir/ --source "cfr_title_12" --type "regulation"` |
| Check DB count | `ingest_text.py --count` |
| Bulk dictionary | `ingest_dictionary.py --file /path/to/dictionary.txt --source "blacks_law_4th"` |

## Procedure

### 1. Ingest Individual Text

```bash
python3 ~/.hermes/skills/legal/db-ingest/scripts/ingest_text.py \
  --text "15 USC 1692a(6): The term 'debt collector' means any person who uses any instrumentality of interstate commerce or the mails in any business the principal purpose of which is the collection of any debts, or who regularly collects or attempts to collect, directly or indirectly, debts owed or due or asserted to be owed or due another." \
  --source "usc15_1692a" \
  --type "statute" \
  --section "1692a(6)"
```

### 2. Ingest a Definition

```bash
python3 ~/.hermes/skills/legal/db-ingest/scripts/ingest_text.py \
  --text "ESTOPPEL. A bar or impediment raised by the law, which precludes a man from alleging or from denying a certain fact or state of facts, in consequence of his previous allegation or denial or conduct or admission." \
  --source "blacks_law_4th" \
  --type "definition" \
  --term "estoppel"
```

### 3. Ingest from File

```bash
# Single file
python3 ~/.hermes/skills/legal/db-ingest/scripts/ingest_text.py \
  --file /mnt/c/AI_AGENT/documents/processed/occ_regs.txt \
  --source "occ_regulations" \
  --type "regulation"

# Directory of files
python3 ~/.hermes/skills/legal/db-ingest/scripts/ingest_text.py \
  --dir /mnt/c/AI_AGENT/documents/processed/ \
  --source "federal_law" \
  --type "statute"
```

### 4. Ingest Case Law from CourtListener

After finding a useful case:

```bash
# Get the opinion text
python3 ~/.hermes/skills/legal/courtlistener-search/scripts/courtlistener.py opinion <opinion_id> > /tmp/opinion.txt

# Ingest it
python3 ~/.hermes/skills/legal/db-ingest/scripts/ingest_text.py \
  --file /tmp/opinion.txt \
  --source "courtlistener_<opinion_id>" \
  --type "opinion"
```

### 5. Bulk Dictionary Ingestion

For Black's Law or Bouvier's (expects one definition per entry, separated by blank lines):

```bash
python3 ~/.hermes/skills/legal/db-ingest/scripts/ingest_dictionary.py \
  --file /mnt/c/AI_AGENT/dictionaries/blacks_law_4th.txt \
  --source "blacks_law_4th"
```

## Metadata Schema

Every chunk in the Beezle corpus chunk_text.db (ChromaDB-free) has these metadata fields:

| Field | Description | Values |
|-------|-------------|--------|
| `source` | Source identifier | `"usc15_1692"`, `"blacks_law_4th"`, `"occ_regs"` |
| `chunk_type` | Type of content | `"statute"`, `"regulation"`, `"rule"`, `"opinion"`, `"definition"`, `"constitutional"`, `"text"` |
| `chunk_index` | Position in document | `0, 1, 2, ...` |
| `section` | Section/subsection | `"1692a(6)"`, `"Rule 12(b)(1)"` |
| `citations` | Extracted citations | `""` (populated by enrichment) |
| `term` | For definitions only | `"estoppel"`, `"debt collector"` |

## Chunking Rules

| Content Type | Chunk Size | Overlap | Strategy |
|-------------|-----------|---------|----------|
| Statutes | 400 words | 50 | Split by section when possible |
| Regulations | 400 words | 50 | Split by section/part |
| Case opinions | 800 words | 150 | Standard overlap |
| Definitions | Per-definition | 0 | One chunk per definition (atomic) |
| Rules of procedure | Per-rule | 0 | One chunk per rule when possible |
| Constitutional | Per-section | 0 | One chunk per section/amendment |

## Ingestion Priority List

### HIGH (ingest next)

| Source | Location | Type | Est. Chunks |
|--------|----------|------|-------------|
| Black's Law Dictionary (4th ed) | archive.org | definition | ~12,000 |
| Bouvier's Law Dictionary | archive.org | definition | ~6,500 |
| OCC Regulations (12 CFR 1-199) | ecfr.gov | regulation | ~50,000 |
| CFPB Regulations (12 CFR 1000+) | ecfr.gov | regulation | ~30,000 |

### MEDIUM

| Source | Location | Type | Est. Chunks |
|--------|----------|------|-------------|
| Fed Reserve Regs (B, E, X, Z) | ecfr.gov | regulation | ~20,000 |
| FTC Regulations (16 CFR) | ecfr.gov | regulation | ~15,000 |
| PA Code Title 42 (Judiciary) | pacodeandbulletin.gov | statute | ~10,000 |
| FRAP (Appellate Rules) | uscourts.gov | rule | ~500 |

### LOW (on demand)

| Source | Notes |
|--------|-------|
| Am. Jur. 2d | Copyrighted — ingest ONLY public domain excerpts from opinions |
| C.J.S. | Copyrighted — ingest ONLY public domain excerpts from opinions |
| Restatement | Copyrighted — ingest court adoptions, not text |
| SEC Regulations (17 CFR) | Only if securities cases arise |
| EPA Regulations (40 CFR) | Only if environmental cases arise |

## Verification

After ingestion, verify:

```bash
# Check total count
python3 ~/.hermes/skills/legal/db-ingest/scripts/ingest_text.py --count

# Search for what was just ingested
python3 ~/.hermes/skills/legal/chromadb-search/scripts/search_chromadb.py "[ingested term or citation]" 3
```

## Pitfalls

- Always set correct `chunk_type` metadata — this enables filtered searches
- Definitions should be atomic (one per chunk) not split across chunks
- Check for duplicates before ingesting — search first
- Large files (>100MB) should use the `txt-ingest` Docker container instead
- The embedding model MUST match what was used for existing data: `BAAI/bge-base-en-v1.5`
