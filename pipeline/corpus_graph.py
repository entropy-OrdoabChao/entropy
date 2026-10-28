#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pipeline/corpus_graph.py — DETERMINISTIC corpus citation + definition graph (no LLM).

Builds, in one pass, the structural backbone the flywheel used to accrete term-by-term:
  chunk-source  --cites-->  {statute, case, CFR, constitution, public-law, court-rule}   (from chunk TEXT)
  term          --defined-in-->  home authority                                          (from defs_index.headwords)

Every edge is a LITERAL citation in the source text (or a headword's own cite) — it cannot hallucinate a
connection. Citations are CANONICALIZED (spacing/case/§) so "589 U. S. 399" and "589 U.S. 399" collapse to
one node. The interpretive term->duty->court's-duty->lever layer stays with fable_connection_hunter (LLM)
as ongoing hardening; this module does the structure only.

Persists to corpus_graph.db (nodes, edges). Query API used by the issue-spotter and m0 grounding.

CLI:
  python -m pipeline.corpus_graph --build [--slice N]     # build (whole corpus, or first N chunks)
  python -m pipeline.corpus_graph --query "42 U.S.C. 1983"
  python -m pipeline.corpus_graph --stats
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import os, re, sys, sqlite3, argparse
from pathlib import Path

CHUNK_DB = str(_eroot() / "corpus/chunk_text.db")
DEFS_DB  = str(_eroot() / "corpus/defs_index.db")
GRAPH_DB = os.environ.get("CORPUS_GRAPH_DB", str(_eroot() / "corpus/corpus_graph.db"))

# ── extraction patterns (absolute cites only — deterministic, high precision) ──
_PATTERNS = {
    "USC":      r"\b\d{1,2}\s+U\.?\s?S\.?\s?C\.?\s+(?:§\s*)?\d+[A-Za-z0-9\-]*",
    "CFR":      r"\b\d{1,2}\s+C\.?\s?F\.?\s?R\.?\s+(?:§\s*)?\d+(?:\.\d+)*",
    "PaCS":     r"\b\d{1,2}\s+Pa\.?\s?C\.?\s?S\.?\s+(?:§\s*)?\d+",
    "CASE":     r"\b\d{1,3}\s+(?:U\.?\s?S\.?|S\.?\s?Ct\.?|F\.?\s?[23]d|F\.?\s?Supp\.?\s?[23]?d?|A\.?\s?[23]d)\s+\d{1,4}",
    "CONST":    r"\bU\.?\s?S\.?\s+Const\.?[^.]{0,40}|\bart(?:icle)?\.?\s+[IVX]{1,4}\b|\b(?:First|Second|Third|Fourth|Fifth|Sixth|Seventh|Eighth|Ninth|Tenth|Eleventh|Twelfth|Thirteenth|Fourteenth|Fifteenth)\s+Amendment\b",
    "PUBL":     r"\bPub\.?\s?L\.?\s?(?:No\.?\s?)?\d+[-–]\d+|\b\d+\s+Stat\.?\s+\d+",
    "PA_RULE":  r"\bPa\.?\s?R\.?\s?[CAE]\.?\s?P?\.?\s?\d+[0-9.]*",
    "FED_RULE": r"\bFed\.?\s?R\.?\s?(?:Civ|App|Crim|Evid)\.?\s?P?\.?\s?\d+[0-9.()a-z]*",
}
_COMPILED = {k: re.compile(v, re.I) for k, v in _PATTERNS.items()}

# ── canonicalization (words, not rocket science) ──
def canon_cite(s: str, typ: str) -> str:
    s = re.sub(r"\s+", " ", s or "").strip(" .,")
    s = re.sub(r"\bU\.?\s*S\.?\s*C\.?", "U.S.C.", s, flags=re.I) if typ in ("USC",) else s
    if typ == "CASE":
        s = re.sub(r"\bU\.?\s*S\.?\b", "U.S.", s, flags=re.I)
        s = re.sub(r"\bS\.?\s*Ct\.?\b", "S. Ct.", s, flags=re.I)
        s = re.sub(r"\bF\.?\s*(2|3)\s*d\b", r"F.\1d", s, flags=re.I)
        s = re.sub(r"\bF\.?\s*Supp\.?\s*(2|3)?\s*d?\b", lambda m: "F. Supp." + (f" {m.group(1)}d" if m.group(1) else ""), s, flags=re.I)
        s = re.sub(r"\bA\.?\s*(2|3)\s*d\b", r"A.\1d", s, flags=re.I)
    if typ in ("USC", "PaCS", "CFR") and "§" not in s:
        s = re.sub(r"(U\.S\.C\.|Pa\.C\.S\.|C\.F\.R\.)\s+", r"\1 § ", s)
    if typ == "CFR":
        s = re.sub(r"\bC\.?\s*F\.?\s*R\.?", "C.F.R.", s, flags=re.I)
    if typ == "PaCS":
        s = re.sub(r"\bPa\.?\s*C\.?\s*S\.?", "Pa.C.S.", s, flags=re.I)
    if typ == "PUBL":
        s = re.sub(r"\bPub\.?\s?L\.?\s?(?:No\.?\s?)?", "Pub. L. ", s, flags=re.I)
        s = s.replace("–", "-")
        s = re.sub(r"\bStat\.?", "Stat.", s, flags=re.I)
    s = re.sub(r"\.{2,}", ".", s)          # collapse double periods ("S. Ct.." -> "S. Ct.")
    s = re.sub(r"§\s*§", "§", s)           # collapse doubled section marks
    s = re.sub(r"\s+", " ", s).strip()
    return s

# ── source-node canonicalization: chunk source label -> a human node label ──
def canon_source(src: str) -> str:
    s = (src or "").strip()
    m = re.match(r"(?i)uscode.*title[-_ ]?(\d+)", s)
    if m: return f"U.S.C. Title {m.group(1)}"
    m = re.match(r"(?i)e?cfr.*title[-_ ]?(\d+)", s)
    if m: return f"C.F.R. Title {m.group(1)}"
    m = re.match(r"(?i)us?reports?[-_ ]?(\d+)", s)
    if m: return f"U.S. Reports vol. {m.group(1)}"
    m = re.match(r"(?i)plaw[-_]?(\d+)[-_]?publ?[-_]?(\d+)", s)
    if m: return f"Pub. L. {m.group(1)}-{m.group(2)}"
    m = re.match(r"(?i)pa[-_ ]?code[-_ ]?t(\d+)", s)
    if m: return f"Pa. Code Title {m.group(1)}"
    return s

# ── source SELF-identity resolution: what authority IS this chunk (so edges get DIRECTION A->B) ──
_SEC_RE = re.compile(r"§\s*(\d+[A-Za-z0-9\-]*(?:\([a-z0-9]+\))*)")
def self_authority(source: str, text: str):
    """Best-effort DETERMINISTIC id of the authority a chunk IS (not what it cites). (auth, type) or (None,None).
    Statutes/CFR: title from the source label + the chunk's own § header. Cases: the source's U.S. Reports
    volume + the page cite present in the chunk. Deterministic; skips when it can't resolve cleanly."""
    s = (source or "").lower()
    head = (text or "")[:500]
    mt = re.search(r"title[-_ ]?(\d+)", s)
    if mt and ("uscode" in s or re.search(r"\busc\b", s)):
        sec = _SEC_RE.search(head)
        if sec:
            return f"{mt.group(1)} U.S.C. § {sec.group(1)}", "USC"
    if mt and "cfr" in s:
        sec = _SEC_RE.search(head)
        if sec:
            return f"{mt.group(1)} C.F.R. § {sec.group(1)}", "CFR"
    # CAP-ingested case sources carry the cite IN the label ('scotus_<name>_550us544', '..._827f3d1295')
    if any(x in s for x in ("scotus", "_v_", "case", "opinion", "usreports")):
        ml = re.search(r"(\d{1,4})(us|sct|f2d|f3d|fsupp2d|fsupp|a2d|a3d)(\d{1,4})", s)
        if ml:
            rep = {"us": "U.S.", "sct": "S. Ct.", "f2d": "F.2d", "f3d": "F.3d",
                   "fsupp": "F. Supp.", "fsupp2d": "F. Supp. 2d", "a2d": "A.2d", "a3d": "A.3d"}.get(ml.group(2), "")
            if rep:
                return canon_cite(f"{ml.group(1)} {rep} {ml.group(3)}", "CASE"), "CASE"
    mv = re.search(r"usreports?[-_]?(\d+)", s) or re.search(r"\b(\d+)us\d", s)
    if mv:
        vol = mv.group(1)
        cm = re.search(rf"\b{vol}\s+U\.?\s?S\.?\s+(\d+)", text or "")
        if cm:
            return canon_cite(f"{vol} U.S. {cm.group(1)}", "CASE"), "CASE"
    return None, None

# ── canon2: stronger canonical id — merges CONST variants (ART I / ARTICLE I) + normalizes rule/statute
#    forms so the term/definition layer and the citation layer share ONE namespace (bridges the island) ──
_ORD = {"i": "I", "ii": "II", "iii": "III", "iv": "IV", "v": "V", "vi": "VI", "vii": "VII"}
def canon2(s: str) -> str:
    s = re.sub(r"\s+", " ", (s or "")).strip(" .,;")
    m = re.search(r"(?:U\.?\s?S\.?\s*Const\.?,?\s*)?art(?:icle)?\.?\s+([IVXivx]{1,5})\b", s, re.I)
    if m:
        return "U.S. Const. art. " + _ORD.get(m.group(1).lower(), m.group(1).upper())
    m = re.search(r"(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|eleventh|twelfth|thirteenth|fourteenth|fifteenth)\s+amendment", s, re.I)
    if m:
        return m.group(1).capitalize() + " Amendment"
    m = re.search(r"(\d{1,3})\s+(u\.?\s?s\.?|s\.?\s?ct\.?|f\.?\s?[23]d|f\.?\s?supp\.?\s?[23]?d?|a\.?\s?[23]?d?)\s+(\d{1,4})", s, re.I)
    if m:
        rep = m.group(2).lower().replace(" ", "").replace(".", "")
        rep = {"us": "U.S.", "sct": "S. Ct.", "f2d": "F.2d", "f3d": "F.3d", "fsupp": "F. Supp.",
               "fsupp2d": "F. Supp. 2d", "fsupp3d": "F. Supp. 3d", "a2d": "A.2d", "a3d": "A.3d", "a": "A."}.get(rep, m.group(2).upper())
        return f"{m.group(1)} {rep} {m.group(3)}"
    # rules: FRCP/FRAP/FRE/Fed. R. Civ. P. -> one canonical id (bridges definition-layer rule cites)
    m = re.search(r"(?:Fed\.?\s?R\.?\s?(Civ|App|Crim|Evid)\.?\s?P?\.?|(FRCP|FRAP|FRE|FRCrP))\s*(\d+[\w.()]*)", s, re.I)
    if m:
        kind = (m.group(1) or "").lower()
        fam = {"civ": "Civ. P.", "app": "App. P.", "crim": "Crim. P.", "evid": "Evid."}.get(kind) \
            or {"frcp": "Civ. P.", "frap": "App. P.", "fre": "Evid.", "frcrp": "Crim. P."}.get((m.group(2) or "").lower(), "Civ. P.")
        return f"Fed. R. {fam} {m.group(3)}"
    m = re.search(r"(\d{1,2})\s+(U\.?S\.?C\.?|C\.?F\.?R\.?|Pa\.?C\.?S\.?)\s*(?:§\s*)?([\w.\-]+)", s, re.I)
    if m:
        base = {"usc": "U.S.C.", "cfr": "C.F.R.", "pacs": "Pa.C.S."}.get(m.group(2).lower().replace(".", "").replace(" ", ""), m.group(2))
        return f"{m.group(1)} {base} § {m.group(3)}"
    return s

def _connect(db=GRAPH_DB):
    con = sqlite3.connect(db, timeout=120)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    return con

def _init(con):
    con.execute("CREATE TABLE IF NOT EXISTS edges (src TEXT, src_node TEXT, dst_type TEXT, dst TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS terms (term TEXT, cite TEXT, kind TEXT, score INTEGER)")
    # colinks = the LINKED graph: cite <-> cite co-occurrence (both endpoints in ONE namespace, traversable).
    # Directed pairs stored both ways so a single-direction query returns all neighbors.
    con.execute("CREATE TABLE IF NOT EXISTS colinks (a TEXT, b TEXT)")
    # dedges = DIRECTED graph: authority a --cites--> authority b (self resolved from the chunk it IS)
    con.execute("CREATE TABLE IF NOT EXISTS dedges (a TEXT, a_type TEXT, b TEXT, b_type TEXT)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_edges_dst  ON edges(dst)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_edges_src  ON edges(src_node)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_terms_term ON terms(term)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_terms_cite ON terms(cite)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_colinks_a  ON colinks(a)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_dedges_a   ON dedges(a)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_dedges_b   ON dedges(b)")
    con.commit()

def build(slice_n: int = 0, resume: bool = True, slice_size: int = 25000, progress_every: int = 200000):
    """RESUMABLE build. Processes chunk_text in keyset-paginated slices (WHERE chunk_id > last_id), and
    writes each slice's edges/colinks/dedges + the checkpoint (last_id, counters) in ONE atomic commit.
    So an interruption (crash OR restart) loses at most the current slice and resumes from last_id — no
    duplicates. resume=True (default) continues; resume=False or slice_n>0 (test) starts fresh."""
    src = sqlite3.connect(CHUNK_DB); src.execute("PRAGMA query_only=1")
    g = _connect(); _init(g)
    g.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)"); g.commit()
    def _get(k, d=""):
        r = g.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone(); return r[0] if r else d
    def _set(k, v):
        g.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
    fresh = (not resume) or bool(slice_n) or (_get("last_id", "") == "" and _get("done", "0") == "0")
    if fresh:
        for tb in ("edges", "terms", "colinks", "dedges", "meta"):
            g.execute(f"DELETE FROM {tb}")
        g.commit()
    last_id = _get("last_id", "")
    n = int(_get("done", "0") or 0)
    edges = int(_get("edges_n", "0") or 0)
    colinks = int(_get("colinks_n", "0") or 0)
    dcount = int(_get("dcount", "0") or 0)
    if n:
        print(f"  [resume] continuing from {n:,} chunks (last_id={last_id[:24]})", flush=True)
    limit_total = int(slice_n) if slice_n else None
    while True:
        page = src.execute(
            "SELECT chunk_id, source, text FROM chunk_text WHERE chunk_id > ? ORDER BY chunk_id LIMIT ?",
            (last_id, slice_size)).fetchall()
        if not page:
            break
        batch, cobatch, dbatch = [], [], []
        for chunk_id, source, text in page:
            n += 1
            t = text or ""
            snode = canon_source(source or "?")
            chunk_cites = {}                      # dst -> type (dedup, keep type)
            for typ, rx in _COMPILED.items():
                for m in rx.finditer(t):
                    dst = canon_cite(m.group(0), typ)
                    if len(dst) < 4:
                        continue
                    batch.append((source or "?", snode, typ, dst)); edges += 1
                    chunk_cites.setdefault(dst, typ)
            cc = sorted(chunk_cites)              # UNDIRECTED: co-occurring cites are linked both ways
            if 2 <= len(cc) <= 12:
                for i in range(len(cc)):
                    for j in range(len(cc)):
                        if i != j:
                            cobatch.append((cc[i], cc[j])); colinks += 1
            self_a, self_t = self_authority(source or "", t)   # DIRECTED: self --cites--> each dst
            if self_a:
                for dst, dt in chunk_cites.items():
                    if dst != self_a:
                        dbatch.append((self_a, self_t, dst, dt)); dcount += 1
            last_id = chunk_id
        # ONE atomic transaction per slice: edges + colinks + dedges + checkpoint commit together
        if batch:   g.executemany("INSERT INTO edges VALUES (?,?,?,?)", batch)
        if cobatch: g.executemany("INSERT INTO colinks VALUES (?,?)", cobatch)
        if dbatch:  g.executemany("INSERT INTO dedges VALUES (?,?,?,?)", dbatch)
        _set("last_id", last_id); _set("done", n); _set("edges_n", edges)
        _set("colinks_n", colinks); _set("dcount", dcount)
        g.commit()
        if n % progress_every < slice_size:
            print(f"  ...{n:,} chunks, {edges:,} edges, {colinks:,} colinks, {dcount:,} directed [ckpt]", flush=True)
        if len(page) < slice_size or (limit_total and n >= limit_total):
            break
    src.close()
    # definitional layer — term -> home authority from defs_index.headwords (deterministic, idempotent refill)
    g.execute("DELETE FROM terms"); trows = 0
    try:
        d = sqlite3.connect(DEFS_DB)
        rows = list(d.execute("SELECT term, cite, kind, score FROM headwords"))
        g.executemany("INSERT INTO terms VALUES (?,?,?,?)", rows); trows = len(rows); d.close()
    except Exception as e:
        print("  [terms] skipped:", e)
    _set("complete", "1"); g.commit()
    print(f"BUILT: {n:,} chunks -> {edges:,} citation edges, {trows:,} term->authority edges -> {GRAPH_DB}")
    g.close()

# ── query API (used by the issue-spotter + m0 grounding) ──
def authorities_in(text: str) -> list:
    """Canon cites literally present in a block of text (dedup)."""
    out = set()
    for typ, rx in _COMPILED.items():
        for m in rx.finditer(text or ""):
            c = canon_cite(m.group(0), typ)
            if len(c) >= 4:
                out.add((typ, c))
    return sorted(out)

def cited_by(cite: str, k: int = 12) -> list:
    """Which corpus sources cite this authority (the 'who relies on X' edge)."""
    g = _connect()
    rows = g.execute("SELECT DISTINCT src_node FROM edges WHERE dst=? LIMIT ?", (cite, k)).fetchall()
    g.close()
    return [r[0] for r in rows]

def co_cited(cite: str, k: int = 12) -> list:
    """Authorities LINKED to this one — cites that co-occur with it, weighted by frequency. Traverses the
    colinks (cite<->cite) graph: both endpoints are cites in one namespace, so this is a real neighbor walk."""
    g = _connect()
    rows = g.execute("SELECT b, COUNT(*) c FROM colinks WHERE a=? GROUP BY b ORDER BY c DESC LIMIT ?",
                     (cite, k)).fetchall()
    g.close()
    return [(r[0], r[1]) for r in rows]

def co_cited_docs(cite: str, k: int = 12, min_weight: int = 2) -> list:
    """DOCUMENT-LEVEL co-citation, weighted by how many documents co-cite the pair — the DENSE cross-
    reference set (everything the same related documents also cite). Noise-filtered: co-cited by MANY
    documents = truly related; one shared document = incidental (dropped by min_weight). Cached per
    (cite,k,min_weight) in doclink_cache so the first ~10s scan is paid once. Never raises."""
    g = _connect()
    g.execute("CREATE TABLE IF NOT EXISTS doclink_cache (cite TEXT, k INT, mw INT, dst TEXT, w INT)")
    g.execute("CREATE INDEX IF NOT EXISTS ix_dlc ON doclink_cache(cite,k,mw)")
    cached = g.execute("SELECT dst,w FROM doclink_cache WHERE cite=? AND k=? AND mw=? ORDER BY w DESC",
                       (cite, k, min_weight)).fetchall()
    if cached:
        g.close(); return [(r[0], r[1]) for r in cached]
    raw = g.execute(
        "SELECT dst, COUNT(DISTINCT src) w FROM edges WHERE src IN "
        "(SELECT DISTINCT src FROM edges WHERE dst=?) AND dst!=? GROUP BY dst HAVING w>=? "
        "ORDER BY w DESC LIMIT ?", (cite, cite, min_weight, k * 4)).fetchall()
    # MERGE variant nodes (Art. I / Article I -> one) via canon2, then take top-k — the dedup taking effect
    self_c = canon2(cite)
    merged = {}
    for d, w in raw:
        c = canon2(d)
        if c == self_c:
            continue
        merged[c] = merged.get(c, 0) + w
    rows = sorted(merged.items(), key=lambda x: -x[1])[:k]
    if rows:
        g.executemany("INSERT INTO doclink_cache VALUES (?,?,?,?,?)",
                      [(cite, k, min_weight, d, w) for d, w in rows]); g.commit()
    g.close()
    return rows

def co_cited_deep(cite: str, hops: int = 2, k: int = 15) -> list:
    """MULTI-HOP document-level neighbors (doctrine chain): 1-hop neighbors + their neighbors, with weight
    decay per hop, merged and re-ranked. Reaches the broader doctrine neighborhood a single hop misses."""
    seen = {cite: 0.0}
    frontier = [(cite, 1.0)]
    for _h in range(max(hops, 1)):
        nxt = []
        for node, mult in frontier:
            for dst, w in co_cited_docs(node, k=k, min_weight=2):
                score = w * mult
                seen[dst] = seen.get(dst, 0.0) + score
                nxt.append((dst, mult * 0.35))       # decay deeper hops
        frontier = nxt[:k * 3]
    seen.pop(cite, None)
    return sorted(seen.items(), key=lambda x: -x[1])[:k]

def build_canon_index():
    """One-time (fast, safe): map canon2(authority) -> each raw variant, so the definition layer and any
    variant node forms can be resolved into the citation graph. Rebuilds the canon_index table."""
    g = _connect()
    g.execute("CREATE TABLE IF NOT EXISTS canon_index (canon TEXT, raw TEXT)")
    g.execute("DELETE FROM canon_index")
    rows = [(canon2(r[0]), r[0]) for r in g.execute("SELECT DISTINCT dst FROM edges")]
    g.executemany("INSERT INTO canon_index VALUES (?,?)", rows)
    g.execute("CREATE INDEX IF NOT EXISTS ix_canon ON canon_index(canon)")
    g.commit(); n = len(rows); g.close()
    return n

def neighbors_for_term(term: str, k: int = 12) -> dict:
    """BRIDGE the definition island into the citation graph: a defined TERM -> its home authority ->
    (canon2) -> the authorities the law cross-references with it. Returns {term, home_cite, canon,
    in_graph, neighbors}. So a definition reaches the cases/statutes that actually use its authority."""
    g = _connect()
    hr = g.execute("SELECT cite FROM terms WHERE term LIKE ? ORDER BY score DESC LIMIT 1", (f"%{term.lower()}%",)).fetchone()
    if not hr:
        g.close(); return {"term": term, "home_cite": None, "in_graph": False, "neighbors": []}
    home = hr[0]; canon = canon2(home)
    # find graph nodes that canonicalize to the same authority
    variants = [r[0] for r in g.execute("SELECT DISTINCT raw FROM canon_index WHERE canon=? LIMIT 20", (canon,)).fetchall()]
    g.close()
    neigh = {}
    for raw in (variants or [home]):
        for dst, w in co_cited_docs(raw, k=k, min_weight=2):
            neigh[dst] = neigh.get(dst, 0) + w
    out = sorted(neigh.items(), key=lambda x: -x[1])[:k]
    treatise_cases = term_supporting_cases(term, k=k)   # cases Black's/CJS/Am Jur cite for this term
    return {"term": term, "home_cite": home, "canon": canon,
            "in_graph": bool(out) or bool(treatise_cases),
            "neighbors": out, "treatise_cases": treatise_cases}

def term_supporting_cases(term: str, k: int = 10, max_chunks: int = 12) -> list:
    """Bridge a defined TERM to the CASES the treatises (Black's / Corpus Juris Secundum / Am Jur / Pomeroy)
    cite in defining it — even dictionary terms whose short gloss has no cites. FTS the headword across the
    treatise corpus, aggregate the case cites in the entry's chunks, rank by frequency. Verified: 'real
    party in interest' -> 277 U.S. 157, 48 S. Ct. 438 (from CJS). Bouvier 1856 excepted (pre-modern cite
    style). Returns [(case_cite, freq)]. Never raises."""
    try:
        import sqlite3
        from collections import Counter
        g = _connect()
        g.execute("CREATE TABLE IF NOT EXISTS term_cases_cache (term TEXT, k INT, cite TEXT, w INT)")
        g.execute("CREATE INDEX IF NOT EXISTS ix_tcc ON term_cases_cache(term,k)")
        hit = g.execute("SELECT cite,w FROM term_cases_cache WHERE term=? AND k=? ORDER BY w DESC",
                        (term.lower(), k)).fetchall()
        if hit:
            g.close(); return [(r[0], r[1]) for r in hit]
        c = sqlite3.connect(CHUNK_DB); c.execute("PRAGMA query_only=1")
        # FTS phrase match (fast) JOINed to chunk_text, filtered to treatise sources — 0.05-1.4s vs ~15s LIKE
        try:
            rows = c.execute(
                "SELECT ct.text FROM chunk_text_fts f JOIN chunk_text ct ON ct.chunk_id=f.source_id "
                "WHERE f.chunk_text_fts MATCH ? AND (ct.source LIKE '%black%' OR ct.source LIKE '%juris%' "
                "OR ct.source LIKE '%amjur%' OR ct.source LIKE '%american_juris%' OR ct.source LIKE '%pomeroy%') "
                "LIMIT ?", ('"' + term.replace('"', '') + '"', max_chunks)).fetchall()
        except Exception:
            rows = c.execute(
                "SELECT text FROM chunk_text WHERE (source LIKE '%black%' OR source LIKE '%juris%' "
                "OR source LIKE '%amjur%' OR source LIKE '%american_juris%' OR source LIKE '%pomeroy%') "
                "AND text LIKE ? LIMIT ?", (f"%{term}%", max_chunks)).fetchall()
        c.close()
        rx = _COMPILED["CASE"]; cnt = Counter()
        for (txt,) in rows:
            for m in rx.finditer(txt or ""):
                cc = canon_cite(m.group(0), "CASE")
                if len(cc) >= 5:
                    cnt[cc] += 1
        out = cnt.most_common(k)
        if out:
            g.executemany("INSERT INTO term_cases_cache VALUES (?,?,?,?)",
                          [(term.lower(), k, cc, w) for cc, w in out]); g.commit()
        g.close()
        return out
    except Exception:
        return []

def cites(authority: str, k: int = 15) -> list:
    """DIRECTED: authorities that THIS authority cites (out-edges), weighted. The 'A cites B' walk."""
    g = _connect()
    rows = g.execute("SELECT b, COUNT(*) c FROM dedges WHERE a=? GROUP BY b ORDER BY c DESC LIMIT ?",
                     (authority, k)).fetchall()
    g.close()
    return [(r[0], r[1]) for r in rows]

def cited_by_directed(authority: str, k: int = 15) -> list:
    """DIRECTED: authorities that cite THIS authority (in-edges), weighted. The 'who relies on B' walk."""
    g = _connect()
    rows = g.execute("SELECT a, COUNT(*) c FROM dedges WHERE b=? GROUP BY a ORDER BY c DESC LIMIT ?",
                     (authority, k)).fetchall()
    g.close()
    return [(r[0], r[1]) for r in rows]

def term_home(term: str, k: int = 5) -> list:
    """Deterministic term -> home authority (from defs_index.headwords), highest score first."""
    g = _connect()
    rows = g.execute("SELECT cite, kind, score FROM terms WHERE term LIKE ? ORDER BY score DESC LIMIT ?",
                     (f"%{term.lower()}%", k)).fetchall()
    g.close()
    return [{"cite": r[0], "kind": r[1], "score": r[2]} for r in rows]

def stats() -> dict:
    g = _connect()
    try:
        e = g.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        d = g.execute("SELECT COUNT(DISTINCT dst) FROM edges").fetchone()[0]
        t = g.execute("SELECT COUNT(*) FROM terms").fetchone()[0]
        by = dict(g.execute("SELECT dst_type, COUNT(*) FROM edges GROUP BY dst_type ORDER BY 2 DESC").fetchall())
        cl = g.execute("SELECT COUNT(*) FROM colinks").fetchone()[0]
        cln = g.execute("SELECT COUNT(DISTINCT a) FROM colinks").fetchone()[0]
        de = g.execute("SELECT COUNT(*) FROM dedges").fetchone()[0]
        den = g.execute("SELECT COUNT(DISTINCT a) FROM dedges").fetchone()[0]
    except Exception:
        e = d = t = cl = cln = de = den = 0; by = {}
    g.close()
    return {"edges": e, "distinct_authorities": d, "term_edges": t, "by_type": by,
            "colinks": cl, "linked_cite_nodes": cln, "directed_edges": de, "directed_source_authorities": den}

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--slice", type=int, default=0)
    ap.add_argument("--query", default="")
    ap.add_argument("--stats", action="store_true")
    a = ap.parse_args()
    if a.build:
        build(slice_n=a.slice)
    elif a.query:
        print("co_cited (undirected):", co_cited(a.query))
        print("cites (directed out): ", cites(a.query))
        print("cited_by (directed in):", cited_by_directed(a.query))
    elif a.stats:
        print(stats())
    else:
        ap.print_help()
