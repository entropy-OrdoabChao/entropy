"""
PHASE_2A_PRIMARY_SOURCE_SCRAPERS
CourtListener API client + multi-source fallback.

Searches federal + state case law via REST API.
Token from env var COURTLISTENER_API_KEY.

Sources (in fallback order):
  1. CourtListener search API
  2. CourtListener citation lookup
  3. Case.law API (Harvard)
"""
from __future__ import annotations
import os
import re
import json
import requests
from typing import Optional

from core.logger import get_logger
from scrapers._base import html_to_text, save_primary_source

log = get_logger("scraper_cl")

API_KEY = os.environ.get("COURTLISTENER_API_KEY", "")
BASE = "https://www.courtlistener.com/api/rest/v4"
HEADERS = {"Authorization": f"Token {API_KEY}"} if API_KEY else {}
TIMEOUT = 30


def _get(url: str) -> Optional[dict]:
    """GET with auth headers. Returns parsed JSON or None."""
    if not API_KEY:
        log.error("auth", "COURTLISTENER_API_KEY not set")
        return None
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.error("request", f"CL request failed: {e}")
        return None


def search_opinions(query: str, court: str = "", n: int = 10) -> list[dict]:
    """Search CourtListener opinions. Returns list of result dicts."""
    url = f"{BASE}/search/?q={requests.utils.quote(query)}&type=o&format=json&page_size={n}"
    if court:
        url += f"&court={court}"
    data = _get(url)
    if not data:
        return []
    results = []
    for r in data.get("results", [])[:n]:
        snippet = r.get("snippet", "")
        if "<" in snippet:
            snippet = re.sub(r"<[^>]+>", "", snippet)[:300]
        results.append({
            "case_name": r.get("caseName", "Unknown"),
            "citation": r.get("citation", []),
            "court": r.get("court", ""),
            "date_filed": r.get("dateFiled", ""),
            "opinion_id": r.get("id"),
            "snippet": snippet,
            "cluster_id": r.get("cluster_id"),
        })
    log.info("search", f"CL search '{query[:40]}': {data.get('count', 0)} total, returned {len(results)}")
    return results


def fetch_opinion(opinion_id: int, save: bool = True) -> dict:
    """Fetch full opinion text by ID. Optionally save to primary_sources."""
    data = _get(f"{BASE}/opinions/{opinion_id}/?format=json")
    if not data:
        return {"error": f"Could not fetch opinion {opinion_id}"}

    text = data.get("plain_text") or data.get("html_with_citations") or ""
    if "<" in text:
        text = html_to_text(text)

    case_name = ""
    cluster_url = data.get("cluster", "")
    court_id = ""

    if cluster_url:
        fmt_url = cluster_url if "format=json" in cluster_url else cluster_url + "?format=json"
        cluster_data = _get(fmt_url)
        if cluster_data:
            case_name = cluster_data.get("case_name", "")
            court_id = cluster_data.get("court_id", "")

    result = {
        "opinion_id": opinion_id,
        "case_name": case_name,
        "court": court_id,
        "text_length": len(text),
        "cluster_url": cluster_url,
    }

    if save and text:
        source_id = f"cl_opinion_{opinion_id}"
        jurisdiction = "federal"
        if court_id and court_id.startswith("pa"):
            jurisdiction = "PA"
        sr = save_primary_source(
            source_id=source_id,
            jurisdiction=jurisdiction,
            source_type="case_law",
            title=case_name or f"Opinion {opinion_id}",
            text=text,
            url=f"https://www.courtlistener.com/opinion/{opinion_id}/",
            extra_frontmatter={"court": court_id, "opinion_id": opinion_id},
        )
        result["path"] = sr["path"]
        result["dedup_skipped"] = sr["dedup_skipped"]

    return result


def fetch_by_cluster(cluster_id: int, save: bool = True) -> dict:
    """Fetch opinion via cluster ID (when opinion_id is null)."""
    cluster_data = _get(f"{BASE}/clusters/{cluster_id}/?format=json")
    if not cluster_data:
        return {"error": f"Could not fetch cluster {cluster_id}"}

    case_name = cluster_data.get("case_name", "")
    court_id = cluster_data.get("court_id", "")
    date_filed = cluster_data.get("date_filed", "")

    sub_opinions = cluster_data.get("sub_opinions", [])
    if not sub_opinions:
        ops_url = f"{BASE}/opinions/?cluster={cluster_id}&format=json"
        ops_data = _get(ops_url)
        if ops_data and ops_data.get("results"):
            sub_opinions = [r.get("resource_uri", "") for r in ops_data["results"]]

    for op_url in sub_opinions[:1]:
        if isinstance(op_url, str) and op_url:
            m = re.search(r"/opinions/(\d+)/", op_url)
            if m:
                return fetch_opinion(int(m.group(1)), save=save)

    text = cluster_data.get("plain_text", "") or cluster_data.get("html_with_citations", "")
    if "<" in text:
        text = html_to_text(text)

    result = {"case_name": case_name, "court": court_id, "cluster_id": cluster_id, "text_length": len(text)}

    if save and text:
        source_id = f"cl_cluster_{cluster_id}"
        jurisdiction = "federal" if "scotus" in court_id.lower() or "dist" in court_id.lower() else "PA"
        sr = save_primary_source(
            source_id=source_id, jurisdiction=jurisdiction, source_type="case_law",
            title=case_name or f"Cluster {cluster_id}", text=text,
            url=f"https://www.courtlistener.com/opinion/{cluster_id}/",
            extra_frontmatter={"court": court_id, "cluster_id": cluster_id, "date_filed": date_filed},
        )
        result["path"] = sr["path"]
        result["dedup_skipped"] = sr["dedup_skipped"]

    return result


def search_and_save(query: str, court: str = "", n: int = 5) -> list[dict]:
    """Search + fetch + save top N opinions. Uses cluster_id when opinion_id is null."""
    hits = search_opinions(query, court, n)
    saved = []
    for h in hits:
        oid = h.get("opinion_id")
        cid = h.get("cluster_id")
        try:
            if oid:
                r = fetch_opinion(oid, save=True)
            elif cid:
                r = fetch_by_cluster(cid, save=True)
            else:
                continue
            r["search_hit"] = h
            if not r.get("error"):
                saved.append(r)
        except Exception as e:
            log.warning("fetch", f"Failed to fetch {h.get('case_name', '?')}: {e}")
    log.info("search_and_save", f"CL search_and_save: {len(saved)} opinions saved")
    return saved


def fetch_from_fallback(case_name: str, citation: str = "", save: bool = True) -> dict:
    """Try multiple sources when CourtListener search fails."""

    # Strategy 1: CourtListener citation lookup
    if citation:
        try:
            cit_match = re.match(r"(\d+)\s+(?:U\.?S\.?|S\.?\s*Ct\.?|F\.\d+|L\.?\s*Ed\.?)\s+(\d+)", citation)
            if cit_match:
                vol = cit_match.group(1)
                page = cit_match.group(2)
                url = f"{BASE}/citations/?volume={vol}&page={page}&format=json"
                data = _get(url)
                if data and data.get("results"):
                    for r in data["results"]:
                        clu = r.get("cluster_id") or r.get("cluster")
                        if clu:
                            cluster_id = clu if isinstance(clu, int) else None
                            if isinstance(clu, str):
                                m = re.search(r"/(\d+)/", clu)
                                if m:
                                    cluster_id = int(m.group(1))
                            if cluster_id:
                                log.info("citation_lookup", f"Found cluster {cluster_id} for {citation}")
                                return fetch_by_cluster(cluster_id, save=save)
        except Exception as e:
            log.info("citation_lookup", f"Citation lookup failed: {e}")

    # Strategy 2: Cornell LII (SCOTUS cases by volume/page)
    if citation:
        try:
            cit_match = re.match(r"(\d+)\s+(?:U\.?S\.?)\s+(\d+)", citation)
            if cit_match:
                vol = cit_match.group(1)
                page = cit_match.group(2)
                lii_url = f"https://www.law.cornell.edu/supremecourt/text/{vol}/{page}"
                resp = requests.get(lii_url, timeout=15)
                if resp.status_code == 200 and len(resp.text) > 5000:
                    # Extract title
                    title_match = re.search(r"<title>(.*?)\|", resp.text)
                    title = title_match.group(1).strip() if title_match else case_name
                    
                    # Extract body text
                    text = html_to_text(resp.text)
                    if len(text) > 500:
                        result = {
                            "case_name": title,
                            "court": "scotus",
                            "text_length": len(text),
                            "source": "cornell_lii",
                        }
                        if save:
                            source_id = f"lii_scotus_{vol}_{page}"
                            sr = save_primary_source(
                                source_id=source_id, jurisdiction="federal",
                                source_type="case_law", title=title, text=text,
                                url=lii_url,
                                extra_frontmatter={"citation": citation, "court": "scotus", "volume": vol, "page": page},
                            )
                            result["path"] = sr["path"]
                            result["dedup_skipped"] = sr["dedup_skipped"]
                        log.info("cornell_lii", f"Found: {title} ({len(text)} chars)")
                        return result
        except Exception as e:
            log.info("cornell_lii", f"Cornell LII failed: {e}")

    return {"error": f"Case not found in any source: {case_name} {citation}"}


def smart_search(query: str, citation: str = "", court: str = "", n: int = 3, save: bool = True) -> list[dict]:
    """Smart case search: tries CourtListener first, then fallbacks."""
    results = search_and_save(query, court=court, n=n)

    query_words = set(w.lower() for w in query.split() if len(w) > 2)
    good_results = []
    for r in results:
        case_name = (r.get("case_name", "") or r.get("search_hit", {}).get("case_name", "")).lower()
        match_count = sum(1 for w in query_words if w in case_name)
        if match_count >= 2:
            good_results.append(r)

    if good_results:
        return good_results

    log.info("smart_search", f"CourtListener results poor for '{query}', trying fallbacks")
    fallback = fetch_from_fallback(query, citation=citation, save=save)
    if not fallback.get("error"):
        return [fallback]

    return results


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: courtlistener.py search <query> [court] [n]")
        print("       courtlistener.py opinion <id>")
        print("       courtlistener.py smart <query> [citation]")
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "search":
        q = sys.argv[2]
        court = sys.argv[3] if len(sys.argv) > 3 else ""
        n = int(sys.argv[4]) if len(sys.argv) > 4 else 10
        for r in search_opinions(q, court, n):
            print(json.dumps(r, indent=2))
    elif cmd == "opinion":
        print(json.dumps(fetch_opinion(int(sys.argv[2])), indent=2))
    elif cmd == "smart":
        q = sys.argv[2]
        cit = sys.argv[3] if len(sys.argv) > 3 else ""
        for r in smart_search(q, citation=cit):
            print(json.dumps(r, indent=2, default=str))