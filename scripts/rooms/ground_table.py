"""GROUND TABLE — deterministic pre-pass for a room (the guardrail; built ONCE, shared by every bot).
Extracts every citation in the adverse filing, resolves each to its verbatim corpus chunk via the
Beezle MCP search_chunks (chunk_id first-class), and writes ground_table.md. Bots never re-search
what is on the table.
usage: ground_table.py <adverse_filing.md> <out.md>"""
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import re, sys, json, urllib.request

MCP = "http://127.0.0.1:8200/mcp"
PATS = [
    r"[A-Z][A-Za-z'’.&-]+(?: [A-Z][A-Za-z'’.&-]+)* v\. [A-Z][A-Za-z'’.&,-]+(?: [A-Za-z'’.&,-]+){0,6},? \d+ (?:A\.\d?d|F\.\d?d|F\. Supp\. \d?d|U\.S\.|Pa\.|Pa\. Super\.) \d+",
    r"\d+ Pa\.C\.S\.(?:A\.)? ?§+ ?[\d.]+(?:\([a-z0-9]+\))*",
    r"\d+ P\.S\. ?§+ ?[\d.-]+(?:\([a-z0-9]+\))*",
    r"Pa\. ?R\. ?C\. ?P\. ?(?:No\. )?\d+(?:\([a-z0-9]+\))*",
    r"Pa\. ?R\. ?A\. ?P\. ?\d+(?:\([a-z0-9]+\))*",
    r"\d+ U\.S\.C\. ?§+ ?[\d]+[a-z]?(?:\([a-z0-9]+\))*",
]

def mcp_search(q, n=3):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "search_chunks", "arguments": {"query": q, "n_results": n}}}
    req = urllib.request.Request(MCP, json.dumps(body).encode(),
                                 {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    raw = urllib.request.urlopen(req, timeout=180).read().decode(errors="ignore")
    for line in raw.splitlines():
        if line.startswith("data:"):
            r = json.loads(line[5:])["result"]
            sc = r.get("structuredContent") or json.loads(r["content"][0]["text"])
            return sc.get("result", [])
    return []

def key_tokens(cite):
    m = re.match(r"(.+?) v\. (.+?),? \d", cite)
    if m: return [m.group(1).split()[-1].lower(), m.group(2).split()[0].lower().strip(",")]
    m = re.search(r"§+ ?([\d.-]+)|(?:P\.|R\. ?C\. ?P\.|R\. ?A\. ?P\.) ?(?:No\. )?(\d+)", cite)   # statute/rule: the section number only
    return [(m.group(1) or m.group(2)).lower()] if m else [cite.split()[0].lower()]

FTS = "http://127.0.0.1:8100/search_text"
def fts(q, k=10):
    import urllib.parse
    return json.load(urllib.request.urlopen(FTS + "?" + urllib.parse.urlencode({"query": q, "top_k": k}), timeout=90)).get("hits", [])

def opinion_chunks(source):
    """every chunk of one opinion straight from the text layer (chunk_id = <source>_chunk_N)."""
    try:
        import sqlite3, sys as _s; _s.path.insert(0, str(_eroot()))
        from pipeline.case_comprehension import CHUNK_DB
        c = sqlite3.connect(str(CHUNK_DB)); c.execute("PRAGMA busy_timeout=30000")
        rows = c.execute("SELECT chunk_id, text FROM chunk_text WHERE source=? LIMIT 400", (source,)).fetchall(); c.close()
        return [(cid, t) for cid, t in rows if t and len(t) > 200]
    except Exception:
        return []


def resolve(cite, ctx=""):  # ctx = the passage in the filing just before the cite
    pa = bool(re.search(r"Pa\.C\.S|P\.S\.|Pa\. ?R\.", cite))
    toks = key_tokens(cite)
    if pa:  # statute / rule: literal text search (FTS) on the section — semantic search on a bare cite returns noise
        sec = toks[0]
        for h in fts(f"§ {sec}", 12) + fts(f"{sec}", 12):
            t = (h.get("text") or ""); src = (h.get("source") or h.get("chunk_id") or "")
            if re.search(rf"§+ ?{re.escape(sec)}\b", t) and "pa_" in src.lower() + " " + json.dumps(h.get("metadata") or {}).lower():
                return {"chunk_id": h.get("chunk_id") or src, "text": t}, False
        # no enacted-text family holds the section: find the chunk that QUOTES it ("§ N provides as follows: ...") and say so
        qhits = mcp_search(f"{cite} provides as follows", 20) + mcp_search(f"{cite} {ctx}".strip(), 12)
        def qscore(h):
            t = h.get("text") or ""; src = (h.get("chunk_id") or "")
            if not re.search(rf"(§+ ?{re.escape(sec)}\b|Section {re.escape(sec)}\b)", t): return -1
            return (10 if src.startswith("pa_") else 0) + (6 if re.search(r"provides(?: as follows| that|:)|reads as follows", t) else 0) + (3 if re.search(r"\bIn an action\b|\bshall\b|\bmay\b", t) else 0)
        qhits = [h for h in qhits if qscore(h) >= 0 and (h.get("chunk_id") or "").startswith(("pa_", "scotus_", "paco"))]   # PA cite → PA source only, never usc_/CFR/plaw
        if qhits:
            best = max(qhits, key=qscore)
            if best["chunk_id"].startswith("pa_"): return best, False
            best = dict(best); best["chunk_id"] += f"   (section QUOTED in this source — enacted text of {cite} NOT IN CORPUS as a statute family: ingest gap)"
            return best, False
    def pick(hits):
        if pa:  # a Pennsylvania cite must resolve to a Pennsylvania source, never usc_/courtlistener_plaw
            hits = [h for h in hits if (h.get("chunk_id") or "").startswith(("pa_", "scotus_", "paco"))] or hits
            hits.sort(key=lambda h: 0 if (h.get("chunk_id") or "").startswith("pa_") else 1)
        norm = lambda x: re.sub(r"[^a-z0-9]", "", x.lower())
        own = [h for h in hits if all(norm(k) in norm(h.get("chunk_id") or "") for k in toks)]
        if own:   # pass 1: the case itself. Semantic search returns a handful of its chunks; the holding is usually not among them —
            # so go to the DB for ALL chunks of that opinion and pick deterministically: the holding chunk + the passage the filing relies on.
            cw = set(re.findall(r"[a-z]{5,}", ctx.lower()))
            full = opinion_chunks((own[0].get("chunk_id") or "").split("#")[0])
            if full:
                pin = re.search(r"\d+ [A-Z][A-Za-z.0-9]* ?(?:\d[a-z]+ )?\d+, (\d+)", cite)   # "106 A.3d 48, 68" → 68
                def fscore(cid, t):
                    tl = t.lower()
                    cap = bool(re.search(r"\b(argued|decided|submitted)\b.{0,80}\b(argued|decided|filed)\b", tl[:600])) or bool(re.search(r"\bappell(ee|ant)\b.{0,200}\bno\. ?\d+", tl[:500])) or cid.endswith("_chunk_0")
                    return (len(cw & set(re.findall(r"[a-z]{5,}", tl))) + (8 if re.search(r"\bwe (hold|held|conclude|therefore hold)\b", tl) else 0)
                            + (6 if pin and re.search(rf"(\*|\[|\bat )\s?{pin.group(1)}\b", t) else 0) - (20 if cap else 0))
                ranked = sorted(full, key=lambda r: -fscore(*r))
                hold = next((r for r in ranked if re.search(r"\bwe (hold|held|conclude|therefore hold)\b", r[1].lower()) and fscore(*r) > 0), None)
                top = ranked[0]
                def window(t):   # excerpt centred on the holding sentence when the chunk is long
                    m = re.search(r"\bwe (hold|held|conclude|therefore hold)\b", t.lower())
                    if not m or len(t) <= 2000: return t
                    a = max(0, m.start() - 700); return ("…" if a else "") + t[a:a + 1900] + ("…" if a + 1900 < len(t) else "")
                b = {"chunk_id": top[0], "text": window(top[1])}
                if hold and hold[0] != top[0]:
                    b["text"] = window(top[1])[:1100] + f"\n[HOLDING — same opinion, chunk {hold[0]}]: " + window(hold[1])[:900]
                    b["chunk_id"] = f"{top[0]} + {hold[0]}"
                return b
            def score(h):
                t = (h.get("text") or "").lower()
                caption = bool(re.search(r"\b(argued|decided|submitted)\b.{0,80}\b(argued|decided|filed)\b", t[:600])) or bool(re.search(r"\bappell(ee|ant)\b.{0,200}\bno\. ?\d+", t[:500]))
                return len(cw & set(re.findall(r"[a-z]{5,}", t))) + (8 if re.search(r"\bwe (hold|held|conclude)", t) else 0) - (20 if caption else 0)
            own.sort(key=lambda h: -score(h))
            b = dict(own[0])
            if score(b) < -10: b["chunk_id"] += "   (CAPTION chunk only — opinion body not retrieved; bots may run ONE search for the holding and post GROUND+)"
            return b
        for h in hits:   # pass 2: text match (statutes, or the case under a different slug)
            t = (h.get("text") or "").lower(); src = (h.get("chunk_id") or "").lower()
            if all(k in t or k in src for k in toks): h["chunk_id"] = h["chunk_id"] + "   (CITING SOURCE, not the case itself)"; return h
        return None
    hits = mcp_search(cite, 6) + (mcp_search(f"{cite} {ctx}".strip(), 8) if ctx else [])
    nm = re.match(r"(?:See |see also |cf\. )?(.+?) v\. ", cite)
    if nm: hits += mcp_search(f"{nm.group(1)} we hold that", 20) + mcp_search(f"{nm.group(1)} {ctx[-260:]}", 12)   # the holding chunk, not the caption   # bare cite finds the case's own slug; cite+passage finds it when the name is garbled
    best = pick(hits)
    if not best:   # second query, phrased like the text itself
        hits2 = mcp_search(f"{cite} provides that", 8); best = pick(hits2); hits = hits + hits2
    if best: return best, False
    return (hits[0] if hits else None), True

def build(src, out):
    txt = open(src, errors="ignore").read()
    cites = []
    for p in PATS:
        for m in re.finditer(p, txt):
            c = re.sub(r"\s+", " ", m.group(0)).strip(" ,")
            if c not in cites: cites.append(c)
    # reporter-cite fallback: case names the name pattern missed (apostrophes, line breaks, "Nat'l")
    for m in re.finditer(r"\d+ (?:A\.\d?d|F\.\d?d|U\.S\.) \d+", txt):
        rep = m.group(0)
        if any(rep in c for c in cites): continue
        back = re.sub(r"\s+", " ", txt[max(0, m.start() - 900):m.start()].replace("\f", " "))
        nms = re.findall(r"([A-Z][A-Za-z'’.&-]*(?: [A-Za-z'’.&-]+){0,4} v\. [A-Z][A-Za-z'’.&-]+(?: [A-Za-z'’.&-]+){0,5})", back)
        nm = nms[-1] if nms else None   # last case name in the window (pinpoint cites follow a quote, not the name)
        cites.append(f"{nm.strip()}, {rep}" if nm else rep)
    if re.search(r"\bUTPCPL\b", txt) and not any("201-2" in c for c in cites):
        cites.append("73 P.S. § 201-2 (UTPCPL definitions: unfair methods of competition and unfair or deceptive acts or practices)")
    flat = re.sub(r"\s+", " ", txt.replace("\f", " "))
    def context(c):
        rep = re.search(r"\d+ (?:A\.\d?d|F\.\d?d|U\.S\.|Pa\.) \d+", c)
        j = flat.find(rep.group(0)) if rep else flat.find(c)
        # the filing's description of the authority is the parenthetical AFTER the cite plus the sentence before it
        return flat[max(0, j - 160):j + 420] if j >= 0 else ""
    rows = []; missing = []
    for c in cites:
        try: best, weak = resolve(c, context(c))
        except Exception as e: best, weak = None, True; print("ERR", c, e, file=sys.stderr)
        if best:
            rows.append((c, best["chunk_id"], weak, re.sub(r"[ \t]+", " ", best["text"])[:2100]))
        else: missing.append(c)
    with open(out, "w") as f:
        f.write(f"# GROUND TABLE — authorities cited in {src.split('/')[-1]} (deterministic; built once)\n")
        f.write("Rule for every bot: these chunks are the authority text. Quote ONLY from the excerpt below, cite the chunk_id as given. Do not search for anything listed here.\n\n")
        for i, (c, cid, weak, ex) in enumerate(rows, 1):
            f.write(f"## A{i}. {c}\nchunk_id: {cid}{'   (WEAK MATCH — verify before relying)' if weak else ''}\n> {ex}\n\n")
        f.write("## NOT RESOLVED (search returned nothing usable — treat as NOT IN CORPUS unless a bot proves otherwise with a chunk_id)\n")
        for c in missing: f.write(f"- {c}\n")
    return {"cites": len(cites), "resolved": len(rows), "weak": sum(r[2] for r in rows), "missing": len(missing), "file": out}

if __name__ == "__main__":
    print(json.dumps(build(sys.argv[1], sys.argv[2])))
