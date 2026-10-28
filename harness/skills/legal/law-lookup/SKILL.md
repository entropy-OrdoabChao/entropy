---
name: law-lookup
description: Retrieve verbatim statute text from Beezle ChromaDB and CourtListener via law_fetcher.
when_to_use: User asks "what is <statute>?" or any question requiring verbatim statute text.
---

# law-lookup

Deterministic legal retrieval. Do NOT paraphrase; return verbatim text.

## How to use

Run law_fetcher.py with the citation as a single argument. Return its stdout unchanged.

Command: python3 ~/.hermes/skills/legal/law-lookup/scripts/law_fetcher.py "<citation>"

Example: python3 ~/.hermes/skills/legal/law-lookup/scripts/law_fetcher.py "5 USC 3331"

The script queries ChromaDB on http://127.0.0.1:8100 and CourtListener v4. Output is structured JSON. Pass it through to the user without interpretation. Do NOT read the script file itself.