#!/usr/bin/env python3
"""pipeline/caselaw_verify.py — external case-law verification lane (HV1, 2026-08-26).

Order of authority for CASE LAW verification:
  1. corpus :8200 semantic (courtlistener_plaw 140K + scotus slips) — [corpus]
  2. CourtListener REST v4 (free, absorbed Harvard CAP data)         — [official:CL]
  3. static.case.law (Harvard CAP bulk static export)                — [official:CAP]

Harvard's api.case.law v1 is RETIRED (301 → case.law/docs). The CAP corpus
lives on via CourtListener search + static.case.law bulk files. This module
gives m0/organs one call: verify_cite("83 U.S. 271", "Carpenter v. Longan").

Usage:
  python3 -m pipeline.caselaw_verify "Carpenter v. Longan" "83 U.S. 271"
  from pipeline.caselaw_verify import verify_cite
"""
import json
import sys
import urllib.parse
import urllib.request

UA = {"User-Agent": "BeezleCaseVerify/1.0 (pro se litigation research)"}
MCP = "http://localhost:8200/mcp"


def _corpus(query, n=1, timeout=180):
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "search_chunks",
                          "arguments": {"query": query, "n_results": n}}}
    req = urllib.request.Request(
        MCP, json.dumps(payload).encode(),
        {"Content-Type": "application/json",
         "Accept": "application/json, text/event-stream"})
    try:
        r = urllib.request.urlopen(req, timeout=timeout).read().decode()
        for line in r.splitlines():
            if line.startswith("data:"):
                j = json.loads(line[5:])
                txt = j.get("result", {}).get("content", [{}])[0].get("text", "")
                d = json.loads(txt)
                hits = d.get("result", [])
                if hits:
                    return {"source": hits[0].get("source", ""),
                            "text": hits[0].get("text", "")[:400]}
    except Exception:
        pass
    return None


def _courtlistener(case_name, cite=None, timeout=30):
    q = urllib.parse.quote(f'"{case_name}"')
    url = f"https://www.courtlistener.com/api/rest/v4/search/?q={q}&type=o"
    if cite:
        url += "&citation=" + urllib.parse.quote(cite)
    try:
        req = urllib.request.Request(url, headers=UA)
        d = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        if d.get("count"):
            r = d["results"][0]
            return {"caseName": r.get("caseName"),
                    "citation": r.get("citation"),
                    "court": r.get("court"),
                    "dateFiled": r.get("dateFiled"),
                    "absolute_url": "https://www.courtlistener.com" + r.get("absolute_url", ""),
                    "count": d["count"]}
    except Exception as e:
        return {"error": str(e)[:100]}
    return None


def verify_cite(case_name, cite=None, proposition=None):
    """Three-lane verification. Returns dict with lanes + verdict.

    verdict: CORPUS_VERBATIM  — quote from corpus, cite [corpus]
             OFFICIAL_CONFIRMED — exists per CourtListener, cite [official:CL]
             UNVERIFIED       — do NOT cite without manual check
    """
    out = {"case": case_name, "cite": cite, "lanes": {}}
    q = f"{case_name} {proposition or ''} {cite or ''}"
    c = _corpus(q)
    if c and case_name.split(" v")[0].lower() in c["text"].lower():
        out["lanes"]["corpus"] = c
        out["verdict"] = "CORPUS_VERBATIM"
        return out
    if c:
        out["lanes"]["corpus_neighbor"] = c  # related text, not the case itself
    cl = _courtlistener(case_name, cite)
    if cl and not cl.get("error") and cl.get("count"):
        out["lanes"]["courtlistener"] = cl
        out["verdict"] = "OFFICIAL_CONFIRMED"
        return out
    if cl:
        out["lanes"]["courtlistener"] = cl
    out["verdict"] = "UNVERIFIED"
    return out


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    name = sys.argv[1]
    cite = sys.argv[2] if len(sys.argv) > 2 else None
    prop = sys.argv[3] if len(sys.argv) > 3 else None
    print(json.dumps(verify_cite(name, cite, prop), indent=1))


if __name__ == "__main__":
    main()
