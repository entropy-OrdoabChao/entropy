# Corpus API — search_chunks Reality Check

**Date verified:** 2026-07-16
**Test method:** Live curl to port 8100

## Finding

`search_chunks` is **MCP-only**. The HTTP endpoint at `http://127.0.0.1:8100/search_chunks` returns `{"detail":"Not Found"}` (404).

## Valid HTTP Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/status` | GET | Health check |
| `/search_text?query=TERM` | GET | FTS search |
| `/lookup_term?term=TERM` | GET | Term lookup |
| `/lookup_authority?cite=CITE` | GET | Authority lookup |

## Implications

- Do NOT curl `search_chunks` — it will fail with 404.
- Use Beezle MCP client for `search_chunks`.
- For HTTP fallback, use `/search_text` (but note: broad queries return noise; specific citation-style queries work well).
- Multiple skills were updated to reflect this: `corpus-grounding`, `corpus-query`, `corpus-access`.

## References

- [`corpus-grounding/SKILL.md`](../corpus-grounding/SKILL.md) — patched with MCP-only clarification
- [`corpus-query/SKILL.md`](../corpus-query/SKILL.md) — patched with MCP-only note
- [`corpus-access/SKILL.md`](../corpus-access/SKILL.md) — patched with Pitfall note
