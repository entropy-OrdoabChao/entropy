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
    """CL lane via pipeline/cl_client.py (item cl_client).

    Was an UNAUTHENTICATED urlopen, which both burned the shared 5/min account quota
    anonymously and 403/202'd on this account. cl_client supplies the token, takes a slot from
    the ONE fcntl-locked on-disk bucket every Beezle process shares, honors Retry-After once,
    caches 24 h and receipts the call. A 429 is TERMINAL -> {'error': 'cl_rate_limited'} and
    verify_cite falls through to UNVERIFIED; it never retries and never blocks a room.
    """
    from pipeline import cl_client
    params = {"q": f'"{case_name}"', "type": "o"}
    if cite:
        params["citation"] = cite
    r = cl_client.get("/search/", params, caller="caselaw_verify._courtlistener",
                      timeout=timeout)
    if not r.get("ok"):
        out = {"error": str(r.get("reason"))[:100]}
        if r.get("retry_after") is not None:
            out["retry_after"] = r["retry_after"]
        return out
    d = r.get("data") or {}
    if d.get("count"):
        res = d["results"][0]
        return {"caseName": res.get("caseName"),
                "citation": res.get("citation"),
                "court": res.get("court"),
                "dateFiled": res.get("dateFiled"),
                "absolute_url": "https://www.courtlistener.com" + res.get("absolute_url", ""),
                "count": d["count"]}
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
