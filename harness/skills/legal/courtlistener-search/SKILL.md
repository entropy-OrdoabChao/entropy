---
name: courtlistener-search
description: Search federal and state case law via the CourtListener API. Use when the user needs case citations, judicial opinions, or controlling authority not in the local database.
version: 1.0.0
metadata:
  hermes:
    tags: [legal, research, case-law, courtlistener, api]
    requires_toolsets: [terminal]
required_environment_variables:
  - name: COURTLISTENER_API_KEY
    prompt: "CourtListener API key"
    help: "Get one at https://www.courtlistener.com/help/api/"
    required_for: "Case law search"
---

# CourtListener Case Law Search

Search and retrieve federal and state court opinions via the CourtListener REST API. Use when the local ChromaDB doesn't have the needed case law or when you need current/recent opinions.

## When to Use

- User needs a specific case by name or citation (e.g., "find Iqbal v. Ashcroft")
- User needs controlling authority for a legal argument
- User needs recent opinions not yet in the local database
- User asks about a judge's ruling history
- Doctrine expansion — finding cases that cite a specific statute

## API Details

- Base URL: `https://www.courtlistener.com/api/rest/v3/`
- Auth: Token header `Authorization: Token <key>`
- Rate limit: 5000 requests/day for authenticated users
- Key endpoints: `/search/`, `/opinions/`, `/clusters/`, `/dockets/`

## Quick Reference

| Task | Endpoint |
|------|----------|
| Search opinions | `/search/?q=<query>&type=o` |
| Get opinion text | `/opinions/<id>/` |
| Get case cluster | `/clusters/<id>/` |
| Search by citation | `/search/?q=<citation>&type=o` |
| Search by court | `/search/?q=<query>&court=scotus&type=o` |
| Search by date | `/search/?q=<query>&filed_after=2020-01-01&type=o` |

## Procedure

### Search for Cases

```bash
python3 ~/.hermes/skills/legal/courtlistener-search/scripts/courtlistener.py search "qualified immunity 42 USC 1983"
```

### Get Full Opinion Text

```bash
python3 ~/.hermes/skills/legal/courtlistener-search/scripts/courtlistener.py opinion <opinion_id>
```

### Manual curl Search

```bash
curl -s -H "Authorization: Token ${COURTLISTENER_API_KEY}" \
  "https://www.courtlistener.com/api/rest/v3/search/?q=iqbal+ashcroft&type=o&format=json" \
  | python3 -c "
import sys,json
d = json.load(sys.stdin)
print(f'Found: {d[\"count\"]} results')
for r in d['results'][:5]:
    print(f\"  {r.get('caseName','?')} — {r.get('citation',['?'])} ({r.get('dateFiled','?')})\")
    print(f\"    Court: {r.get('court','?')}\")
    print(f\"    ID: {r.get('id','?')}\")
"
```

### Search by Specific Court

```bash
# SCOTUS only
curl -s -H "Authorization: Token ${COURTLISTENER_API_KEY}" \
  "https://www.courtlistener.com/api/rest/v3/search/?q=due+process&court=scotus&type=o&format=json"

# Third Circuit
curl -s -H "Authorization: Token ${COURTLISTENER_API_KEY}" \
  "https://www.courtlistener.com/api/rest/v3/search/?q=garnishment&court=ca3&type=o&format=json"

# PA state courts
curl -s -H "Authorization: Token ${COURTLISTENER_API_KEY}" \
  "https://www.courtlistener.com/api/rest/v3/search/?q=bad+faith+insurance&court=pa&type=o&format=json"
```

### Ingest Results into ChromaDB

After finding relevant cases, ingest the opinion text into the local database for future retrieval:

```bash
python3 ~/.hermes/skills/legal/courtlistener-search/scripts/courtlistener.py ingest <opinion_id>
```

## Court Codes

| Court | Code |
|-------|------|
| Supreme Court | scotus |
| Third Circuit | ca3 |
| E.D. Pa. | paed |
| M.D. Pa. | pamd |
| W.D. Pa. | pawd |
| PA Supreme Court | pa |
| PA Superior Court | pasuperct |
| PA Commonwealth | pacommwct |

## Pitfalls

- Rate limit: 5000/day. Don't loop through hundreds of results.
- Opinion text may be HTML — strip tags before using.
- Some older opinions have OCR artifacts.
- Not all opinions have full text — check `html_with_citations` or `plain_text` fields.
- The `search` endpoint returns metadata only — you must fetch `/opinions/<id>/` for full text.
- **AUTH REQUIRED FOR OPINION TEXT**: The `/search/` endpoint works without authentication (returns metadata only), but fetching opinion text via `/opinions/<id>/` returns 401 Unauthorized without a valid API key. Always verify the API key is set before attempting to fetch opinion text. If no key is available, use the search results for metadata/citation only.

## Verification

A successful search returns results with `count > 0` and each result has a `caseName`, `citation`, and `id`. Opinion fetch returns text in `html_with_citations` or `plain_text` field.
