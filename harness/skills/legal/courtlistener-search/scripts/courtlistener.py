#!/usr/bin/env python3
"""CourtListener API client for case law search and retrieval."""
import sys, os, json, re
try:
    import urllib.request as req
except ImportError:
    print("ERROR: urllib required"); sys.exit(1)

API_KEY = os.environ.get("COURTLISTENER_API_KEY", "8e7b9e2c521ab2bb034a375d1c73702544339a00")
BASE = "https://www.courtlistener.com/api/rest/v4"
HEADERS = {"Authorization": f"Token {API_KEY}"}

def api_get(url):
    r = req.Request(url, headers=HEADERS)
    with req.urlopen(r, timeout=30) as resp:
        return json.loads(resp.read())

def search(query, court=None, n=10):
    url = f"{BASE}/search/?q={req.quote(query)}&type=o&format=json&page_size={n}"
    if court:
        url += f"&court={court}"
    data = api_get(url)
    print(f"Found: {data['count']} results\n")
    for r in data.get("results", [])[:n]:
        name = r.get("caseName", "Unknown")
        cite = r.get("citation", ["No citation"])
        date = r.get("dateFiled", "?")
        court_name = r.get("court", "?")
        rid = r.get("id", "?")
        print(f"  {name}")
        print(f"    Citation: {cite}")
        print(f"    Court: {court_name} | Filed: {date} | ID: {rid}")
        snippet = r.get("snippet", "")
        if snippet:
            clean = re.sub(r"<[^>]+>", "", snippet)[:200]
            print(f"    Snippet: {clean}")
        print()

def opinion(oid):
    data = api_get(f"{BASE}/opinions/{oid}/?format=json")
    text = data.get("plain_text") or data.get("html_with_citations") or "No text available"
    if "<" in text:
        text = re.sub(r"<[^>]+>", "", text)
    cluster_url = data.get("cluster", "")
    print(f"Opinion ID: {oid}")
    print(f"Cluster: {cluster_url}")
    print(f"Text length: {len(text)} chars")
    print(f"\n{'='*60}\n")
    print(text[:5000])
    if len(text) > 5000:
        print(f"\n... [{len(text)-5000} more characters]")

def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  courtlistener.py search <query> [court] [n_results]")
        print("  courtlistener.py opinion <opinion_id>")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "search":
        query = sys.argv[2] if len(sys.argv) > 2 else ""
        court = sys.argv[3] if len(sys.argv) > 3 else None
        n = int(sys.argv[4]) if len(sys.argv) > 4 else 10
        search(query, court, n)
    elif cmd == "opinion":
        oid = sys.argv[2]
        opinion(oid)
    else:
        print(f"Unknown command: {cmd}")

if __name__ == "__main__":
    main()
