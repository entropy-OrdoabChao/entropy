---
name: precedent-cache
description: Hot cache of verified binding precedent keyed by (jurisdiction, issue). Cache-first retrieval. Promotes results from binding-precedent-enforcer into the cache. Cuts retrieval from 60s+ to <100ms on cache hits.
version: 1.0.0
author: Hermes Legal
metadata:
  hermes:
    tags: [legal, performance, cache, precedent, fast-path]
    related_skills: [binding-precedent-enforcer, learning-loop-controller]
---

# Precedent Cache

## Purpose
Once a binding holding has been verified for a (jurisdiction, issue) pair, it should never be re-fetched. This cache stores verified holdings in SQLite indexed by jurisdiction + issue keywords.

## Storage
`~/.hermes/data/cache/precedent.db` (SQLite, FTS5-enabled)

Schema:
```sql
CREATE TABLE precedent (
  id INTEGER PRIMARY KEY,
  jurisdiction TEXT NOT NULL,
  issue TEXT NOT NULL,
  case_name TEXT NOT NULL,
  citation TEXT NOT NULL,
  pinpoint TEXT,
  holding TEXT NOT NULL,
  binding_level TEXT,
  verified_at TEXT NOT NULL,
  verified_count INTEGER DEFAULT 1,
  last_used TEXT
);
CREATE VIRTUAL TABLE precedent_fts USING fts5(issue, holding, content=precedent);
```

## Usage

```bash
# Lookup (cache-first)
~/.hermes/skills/legal/precedent-cache/scripts/cache.sh lookup \
  --jurisdiction edpa --issue "procedural due process notice"

# Promote a verified holding into the cache
~/.hermes/skills/legal/precedent-cache/scripts/cache.sh promote \
  --jurisdiction edpa --issue "procedural due process notice" \
  --case "Mullane v. Central Hanover Bank" \
  --citation "339 U.S. 306 (1950)" --pinpoint "314" \
  --holding "An elementary and fundamental requirement of due process..." \
  --binding-level scotus

# List cache contents for a jurisdiction
~/.hermes/skills/legal/precedent-cache/scripts/cache.sh list --jurisdiction edpa

# Stats
~/.hermes/skills/legal/precedent-cache/scripts/cache.sh stats

# Wipe (rare — full re-verification triggered next pipeline run)
~/.hermes/skills/legal/precedent-cache/scripts/cache.sh wipe --confirm
```

## Cache hit policy
1. `binding-precedent-enforcer` calls `cache.sh lookup` BEFORE any DB or CourtListener call
2. On hit → return cached holdings, increment `last_used`, `verified_count`
3. On miss → run normal retrieval, then call `cache.sh promote` on verified results
4. Entries with `verified_count >= 3` and `last_used` within 90 days are "hot"
5. Cache survives DB rebuilds (independent SQLite file)

## Pipeline integration
Add to SOUL.md Step 6.5 (binding-precedent-enforcer):
```
First call: precedent-cache lookup
On cache miss: full retrieval + promote on success
```

## Performance target
- Cache hit: <100ms
- Cache miss: ~5–60s (existing retrieval path)
- Expected hit rate after 50 cases: ~70% on common due process issues
