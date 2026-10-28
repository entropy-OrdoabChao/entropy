# eCFR API 406

**Symptom:** The eCFR API at `https://www.ecfr.gov/api/v1/` returns `406 Unsupported format` on all requests, even with `Accept: application/json` headers.

**Cause:** The API endpoint does not accept the request format being sent.

**Workaround:** Use one of these approaches:
1. Fetch the HTML page directly with `requests.get()` and a browser User-Agent header (e.g., `Mozilla/5.0`), then parse the HTML to find the section text.
2. Use `mcp_beezle_source_fetch(source="cfr", params={...})` which handles the retrieval internally.
3. Use `mcp_beezle_search_chunks` to find the text in the indexed corpus.
