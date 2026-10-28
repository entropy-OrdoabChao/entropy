---
name: authority-grounding
description: Ground legal authorities and breaches in the corpus, then save to Obsidian.
version: "0.1.0"
metadata:
  hermes:
    tags: Legal, Corpus, Authority, Obsidian
---

# Authority Grounding — Corpus Query + Obsidian Recording

## Intro

When the user asks to "ground the controlling rule for that [X] in the corpus" or write about legal authorities and breaches, this skill captures the two-phase workflow: discover what "that [X]" refers to via session history, then query the corpus for the controlling legal text and record the result in the Obsidian vault.

## When to Use

- User says "ground the controlling rule for that [breach/issue/claim] in the corpus"
- User says "ground ONE step: search_chunks '[legal term]' in the corpus"
- User says "that dismissal was error, citing that authority" and wants it saved
- User says "write TWO sentences: that [X] was error, citing that authority. Save + link."
- Any request to ground a legal concept in the corpus and record it

## Prerequisites

- Beezle corpus service running at `http://localhost:8100`
- Obsidian vault at `{{ENTROPY_HOME}}/obsidian_sync/`
- Case notes in vault under `obsidian_sync/cases/<slug>/`

## How to Run

1. **Discover context** — when user says "that [X]", search session history
2. **Query the corpus** — use the corpus-client or curl against localhost:8100
3. **Save to Obsidian** — write the note with proper wikilinks

## Quick Reference

### Session Discovery

- `session_search(query="breach", sort="newest")` — find what "that breach" refers to
- `session_search(query="oath breach finding", sort="newest")` — narrow search
- `session_search(session_id="...")` — read a specific session for context
- `session_search(around_message_id=..., session_id="...", window=10)` — scroll in a session

### Corpus Query (curl)

- `GET http://localhost:8100/search_text?query=QUERY&top_k=N`
- `GET http://localhost:8100/lookup_term?term=TERM&max_hits=N`
- `GET http://localhost:8100/lookup_authority?cite=CITATION`

### Corpus Query (Python — preferred)

```python
from corpus_client import CorpusClient
client = CorpusClient()
results = client.search_text("QUERY", top_k=10)
term = client.lookup_term("TERM")
authority = client.lookup_authority("CITATION")
```

The wrapper is at:
`/home/{{USER_FIRST}}/.hermes/skills/legal/corpus-query/scripts/corpus_client.py`

### Obsidian Write

- `beeze-obsidian-integration` skill — write notes to vault
- `write_obsidian_note()` — write a single note
- `propose_link()` — stage a wikilink for human approval

## Procedure

### Phase 1: Context Discovery

When the user references "that [X]" (breach, authority, issue, etc.):

1. Search session history for what "that [X]" refers to:
   ```
   session_search(query="breach", sort="newest")
   ```

2. If the search returns multiple sessions, narrow down:
   ```
   session_search(query="oath breach finding", sort="newest")
   ```

3. Read the relevant session to understand the context:
   ```
   session_search(session_id="SESSION_ID", window=10)
   ```

4. Identify the legal concept being discussed (e.g., "{{JUDGE}}'s violation of Canon 2(A) and Canon 2(C)")

### Phase 2: Corpus Grounding

1. Load the corpus-query skill to get the correct endpoints:
   ```
   skill_view(name="corpus-query")
   ```

2. Query the corpus for the controlling rule:
   ```
   curl -s http://localhost:8100/search_text?query=Canon+2A+2C+Code+Judicial+Conduct+top_k=10
   ```

3. For specific canon or rule text, narrow the query:
   ```
   curl -s http://localhost:8100/search_text?query=Canon+2C+impropriety+appearance+top_k=5
   ```

4. For statutory/regulatory text, use authority lookup:
   ```
   curl -s http://localhost:8100/lookup_authority?cite=42+USC+1983
   ```

5. If corpus_client fails (ModuleNotFoundError), fall back to curl via `execute_code`:
   ```python
   import subprocess, json
   result = subprocess.run(
       ["curl", "-s", "http://localhost:8100/search_text?query=QUERY&top_k=N"],
       capture_output=True, text=True
   )
   data = json.loads(result.stdout)
   ```

### Phase 3: Obsidian Recording

1. Load the beeze-obsidian-integration skill:
   ```
   skill_view(name="beeze-obsidian-integration")
   ```

2. Write the grounding note to the case directory:
   ```
   write_obsidian_note(subdir="cases/<slug>/", filename="grounding_<date>.md", content=...)
   ```

3. Create proper wikilinks:
   ```
   propose_link(note="[[Case Name]]", link="[[Authority Name]]")
   ```

4. If the user wants a short note (e.g., "write TWO sentences"), keep it brief and cite the authority directly.

## Pitfalls

- **corpus_client import error**: The corpus-client module may not be available in the sandbox. Always have a curl fallback ready.
- **Canon text not indexed**: Canon 2(C) text was not found in the corpus even though Canon 2(A) was. The corpus may have gaps — always check if the text you're looking for actually exists.
- **session_search context**: When searching session history, the tool returns truncated output. Use `session_search(around_message_id=..., window=10)` to scroll in the session for more detail.
- **Oneshot mode**: When in oneshot mode, you may need to pick the best option from choices without user confirmation. Use judgment based on the most recent context.
- **Citation gaps**: Not all citations are indexed. /lookup_authority may return 0 hits — this doesn't mean the citation doesn't exist, just that it's not in the corpus.

## Verification

After grounding an authority and saving to Obsidian, verify:

1. The note exists in the vault:
   ```
   search_files(pattern="grounding_<date>.md", path="obsidian_sync/cases/<slug>/", output_mode="files_only")
   ```

2. The wikilinks resolve (check backlinks):
   ```
   skill_view(name="beeze-obsidian-integration", file_path="references/definitions-learned-directory.md")
   ```