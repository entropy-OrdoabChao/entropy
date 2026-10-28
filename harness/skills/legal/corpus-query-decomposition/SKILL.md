---
name: corpus-query-decomposition
description: Systematic approach to corpus searches when initial queries return zero or irrelevant hits.
version: 0.1.0
metadata:
  hermes:
    tags: Corpus, Search, Decomposition, FTS
---

# Corpus Query Decomposition — Zero-Hit Recovery

When a targeted corpus search returns zero hits or irrelevant results, this skill captures the systematic decomposition strategy: break the query into component parts, search each independently, then synthesize findings.

## When to Use

- User asks to ground a legal concept in the corpus and the initial search returns zero hits
- User asks to find a specific regulation, section, or doctrine in the corpus
- Any corpus query that returns no results or clearly wrong results
- When you need to verify whether a legal concept exists in the corpus at all

## Prerequisites

- Beezle corpus DB running on port 8100
- corpus-query skill loaded (for API reference)
- Python with `requests` library available

## How to Run

Invoke through the `terminal` tool using `curl` to the corpus query API, or use the Python wrapper from `corpus-client`.

## Quick Reference

### Corpus Query API Endpoints

- `GET http://localhost:8100/search_text?query=QUERY&top_k=N` — FTS search
- `GET http://localhost:8100/lookup_term?term=TERM&max_hits=N` — term/definition lookup
- `GET http://localhost:8100/lookup_authority?cite=CITATION` — authority by citation

### Python Wrapper

```
from corpus_client import CorpusClient
client = CorpusClient()
results = client.search_text("query", top_k=10)
```

Wrapper path: `corpus-query/scripts/corpus_client.py`

## Procedure

1. **Load corpus-query skill** — run `skill_view(name="corpus-query")` to get the latest API reference.

2. **Execute initial targeted search** — use the full query from the user. Format with `+` for required terms, space for OR:

```
curl -s "http://localhost:8100/search_text?query=FHA+subrogation+24+CFR+203+double+recovery&top_k=10" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'Hits: {d.get(\"count\",0)}')" 2>&1
```

3. **If zero hits, decompose the query** — break into component searches:

   a. Search each major term separately:
   ```
   curl -s "http://localhost:8100/search_text?query=FHA+subrogation&top_k=10" | python3 -c "..."
   ```
   ```
   curl -s "http://localhost:8100/search_text?query=24+CFR+203&top_k=10" | python3 -c "..."
   ```
   ```
   curl -s "http://localhost:8100/search_text?query=double+recovery&top_k=10" | python3 -c "..."
   ```

   b. Search adjacent term combinations:
   ```
   curl -s "http://localhost:8100/search_text?query=double+recovery+FHA&top_k=10" | python3 -c "..."
   ```
   ```
   curl -s "http://localhost:8100/search_text?query=subrogation+Mortgage+Insurance+Fund&top_k=10" | python3 -c "..."
   ```

   c. Try broader search for the regulatory part:
   ```
   curl -s "http://localhost:8100/search_text?query=24+CFR+203&top_k=10" | python3 -c "..."
   ```

4. **Review results for relevance** — for each hit, check:
   - Does the text excerpt actually relate to the query concept?
   - Or is it a false positive from a coincidental term match?
   - Use the `score` field and `source` field to gauge quality

5. **Try alternative search strategies** if decomposition still yields nothing:
   - Use `lookup_term` for definitional concepts:
   ```
   curl -s "http://localhost:8100/lookup_term?term=SUBROGATION&max_hits=10"
   ```
   - Use `lookup_authority` for citation-based concepts:
   ```
   curl -s "http://localhost:8100/lookup_authority?cite=24 CFR 203"
   ```
   - Expand to related regulatory parts (e.g., try Part 36 for VA loans if Part 203 for FHA yields nothing)

6. **Report honestly** — if no relevant hits after decomposition, state that the concept is absent from the corpus. Do not fabricate results.

## Pitfalls

- **False positives from 24 CFR**: Many hits on "24 CFR" are from unrelated parts (e.g., Part 1007 for HERA crisis, Part 36 for VA loans). Always check the source/chunk to verify relevance.
- **FHA vs VA confusion**: The corpus contains both FHA (24 CFR Part 203) and VA (38 CFR Part 36) subrogation. These are different programs — verify which you're querying.
- **CURL formatting**: Use `+` not `%20` for spaces in curl URLs. Use spaces for OR terms.
- **Zero hits ≠ no concept**: The corpus may have the concept but not indexed in a way the FTS can find. Try the Python wrapper's `search_with_expansion()` for broader matching.
- **Don't over-decompose**: If a 2-term search returns hits, don't break it further unless the results are irrelevant.

## Verification

After executing the decomposition, verify the corpus service is still responding:

```
curl -s "http://localhost:8100/search_text?query=test&top_k=1" | python3 -c "import sys,json; d=json.load(sys.stdin); print('Corpus OK' if d.get('count',0) >= 0 else 'Corpus error')" 2>&1
```
