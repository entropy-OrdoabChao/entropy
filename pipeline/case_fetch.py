"""pipeline/case_fetch.py — acquire full case opinions for citation verification / rebuttal grounding.

Purpose
-------
When a cited case (ours OR opposing counsel's) is not already in the corpus, pull its FULL opinion
from a free primary source and ingest it PERMANENTLY, so the flow never re-pulls and
`pipeline.claim_support_check` can grade the cite against the case's own text (SUPPORTED / OVERSTATED /
WRONG_POINT / UNBACKED). This is the case-law analogue of `pipeline.rules_index` + `needs_rule.log`.

Source chain (mix-and-match, no single point of rate-limit):
  1. Caselaw Access Project  (static.case.law)  — Harvard public-domain bulk, static CDN, NO auth/NO limit.
  2. Wikisource              (en.wikisource.org) — older public-domain opinions, clean text.
  3. CourtListener           (courtlistener.com) — public opinion page scrape; broad but RATE-LIMITED,
                                                   so it is the LAST-RESORT fallback only.

Ingest is additive + idempotent (skips a case already in the corpus) and writes to BOTH the long-term
store (`chunk_text`) and the FTS index (`chunk_text_fts`) that claim_support_check searches. Semantic
embedding is a separate step (`python -m pipeline.turbovec_ingest`) run after a batch. Misses are logged
to output/audit/needs_case.log for later acquisition from another source.

CLI:
  python -m pipeline.case_fetch --cite "533 U.S. 167" --name "Duncan v. Walker"
  python -m pipeline.case_fetch --selftest
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import os, re, json, time, sqlite3, subprocess, urllib.parse
from pathlib import Path

# Reuse the canonical corpus path (avoids path drift with the rest of the pipeline).
try:
    from pipeline.turbovec_ingest import CHUNK_DB  # ~/turbovec/chunk_text.db
except Exception:
    CHUNK_DB = (_eroot() / "corpus/chunk_text.db")

ROOT = Path(os.environ.get("BEEZLE_ROOT", str(_eroot())))
NEEDS_CASE_LOG = (_eroot() / "output/audit/needs_case.log")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"

# reporter string (as printed) -> CAP reporter slug
_REPORTER = {
    "u.s.": "us", "us": "us",
    "f.": "f", "f": "f",
    "f.2d": "f2d", "f2d": "f2d",
    "f.3d": "f3d", "f3d": "f3d",
    "f.4th": "f4th", "f4th": "f4th",
    "f.supp": "f-supp", "f.supp.": "f-supp",
    "f.supp.2d": "f-supp-2d", "f.supp.3d": "f-supp-3d",
    "s.ct.": "s-ct", "s.ct": "s-ct", "sct": "s-ct",
    "l.ed.": "l-ed", "l.ed": "l-ed", "l.ed.2d": "l-ed-2d", "l.ed.2d.": "l-ed-2d",
    # regional reporters (Atlantic) + PA/NJ state reporters
    "a.": "a", "a.2d": "a2d", "a2d": "a2d", "a.3d": "a3d", "a3d": "a3d",
    "pa.": "pa", "pa": "pa", "pa.super.": "pa-super", "pa.commw.": "pa-commw",
    "n.j.": "nj", "nj": "nj", "n.j.super.": "nj-super", "n.j.eq.": "nj-eq",
    "n.j.l.": "nj-l", "n.j.misc.": "nj-misc",
    "n.y.": "ny", "n.y.2d": "ny2d", "n.y.3d": "ny3d",
    "cal.": "cal", "cal.2d": "cal2d", "cal.3d": "cal3d", "cal.4th": "cal4th",
    "so.2d": "so2d", "so.3d": "so3d", "n.e.2d": "ne2d", "n.w.2d": "nw2d",
    "s.e.2d": "se2d", "s.w.2d": "sw2d", "s.w.3d": "sw3d", "p.2d": "p2d", "p.3d": "p3d",
}

def parse_cite(cite: str):
    """'533 U.S. 167' -> (533, 'us', 167). Returns None if unparseable."""
    m = re.match(r"\s*(\d{1,3})\s+([A-Za-z.]+(?:\s?2d|\s?3d)?)\.?\s+(\d{1,4})", cite.strip())
    if not m:
        return None
    vol, rep, page = m.group(1), m.group(2).lower().replace(" ", ""), m.group(3)
    slug = _REPORTER.get(rep) or _REPORTER.get(rep.rstrip("."))
    if not slug:
        return None
    return int(vol), slug, int(page)

def _curl(url: str, timeout: int = 45) -> str:
    try:
        r = subprocess.run(["curl", "-sSL", "--max-time", str(timeout), "-A", UA, url],
                           capture_output=True, text=True, timeout=timeout + 8)
        return r.stdout or ""
    except Exception:
        return ""

# ---- source 1: Caselaw Access Project -------------------------------------------------
def from_cap(vol: int, rep: str, page: int):
    """CAP static bulk. Tries the per-case JSON path first (fast when it exists), then the
    VOLUME ZIP — the route the skill locks and the only one that works for most reporters
    (per-case paths 404 broadly). Matches by first_page inside the zip."""
    for ordn in ("01", "02", "03"):
        raw = _curl(f"https://static.case.law/{rep}/{vol}/cases/{page:04d}-{ordn}.json")
        if not raw.strip().startswith("{"):
            continue
        try:
            d = json.loads(raw)
        except Exception:
            continue
        cites = " ".join(str(c.get("cite", "")) for c in (d.get("citations") or []) if isinstance(c, dict))
        if str(page) not in cites or str(vol) not in cites:
            continue
        cb = d.get("casebody") or {}
        ops = cb.get("opinions") if isinstance(cb, dict) else None
        body = "\n\n".join(o.get("text", "") for o in (ops or []) if isinstance(o, dict) and o.get("text"))
        full = (((cb.get("head_matter") or "") if isinstance(cb, dict) else "") + "\n\n" + body).strip()
        if full and len(full) > 300:
            return full, d.get("name_abbreviation") or ""
    return _from_cap_zip(vol, rep, page)


def _from_cap_zip(vol: int, rep: str, page: int):
    """Volume-zip route: https://static.case.law/<slug>/<vol>.zip -> case with first_page==page."""
    import zipfile, io, urllib.request as _u
    try:
        req = _u.Request(f"https://static.case.law/{rep}/{vol}.zip",
                         headers={"User-Agent": UA})
        z = zipfile.ZipFile(io.BytesIO(_u.urlopen(req, timeout=300).read()))
    except Exception:
        return None, None
    for nm in z.namelist():
        if not nm.endswith(".json"):
            continue
        try:
            d = json.loads(z.read(nm))
        except Exception:
            continue
        if not isinstance(d, dict) or str(d.get("first_page")) != str(page):
            continue
        cb = d.get("casebody") or {}
        ops = cb.get("opinions") if isinstance(cb, dict) else None
        head = (cb.get("head_matter") or "") if isinstance(cb, dict) else ""
        body = "\n\n".join(o.get("text", "") for o in (ops or []) if isinstance(o, dict) and o.get("text"))
        full = (head + "\n\n" + body).strip()
        if full and len(full) > 300:
            return full, d.get("name_abbreviation") or ""
    return None, None

# ---- source 2: Wikisource -------------------------------------------------------------
def from_wikisource(name: str):
    title = urllib.parse.quote(name.replace(" ", "_"))
    raw = _curl(f"https://en.wikisource.org/wiki/{title}")
    if not raw or "Wikisource does not have" in raw or "<title>" not in raw:
        return None, None
    t = re.sub(r"<script.*?</script>", " ", raw, flags=re.S)
    t = re.sub(r"<style.*?</style>", " ", t, flags=re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"\s+", " ", t)
    return (t, name) if len(t) > 2000 else (None, None)

# ---- source 3: CourtListener (public page scrape, LAST RESORT, rate-limited) -----------
def _cl_key():
    """CourtListener API key: env first, then config.yaml. Anonymous calls 401/202 —
    a lane that skips the key reports every case as a MISS (false NOT-IN-CORPUS)."""
    k = os.environ.get("COURTLISTENER_API_KEY")
    if k:
        return k.strip()
    try:
        t = (_eroot() / "config.yaml").read_text(encoding="utf-8", errors="ignore")
        m = re.search(r"courtlistener[^\n]*\n(?:.*\n){0,6}?.*?([A-Za-z0-9]{40})", t, re.I)
        if m:
            return m.group(1)
        m = re.search(r"\b([A-Za-z0-9]{40})\b", t)
        return m.group(1) if m else None
    except Exception:
        return None


def _cl_get(url: str, key: str):
    import urllib.request as _u
    try:
        r = _u.Request(url, headers={"Authorization": f"Token {key}", "User-Agent": UA})
        return _u.urlopen(r, timeout=90).read().decode("utf-8", "ignore")
    except Exception:
        return ""


def from_courtlistener(name: str, cite: str):
    """AUTHENTICATED REST v4: search -> cluster -> sub_opinions -> plain_text/html.

    The old public-HTML scrape is DEAD (courtlistener.com now returns HTTP 202 with an
    empty body to non-browser clients), which silently turned this fallback into a
    guaranteed miss. Post-CAP-cutoff opinions (roughly 2020+) live ONLY here.
    """
    key = _cl_key()
    if not key:
        return None, None
    q = urllib.parse.quote(f'"{cite}"' if cite else f'"{name}"')
    raw = _cl_get(f"https://www.courtlistener.com/api/rest/v4/search/?type=o&q={q}", key)
    try:
        results = (json.loads(raw) or {}).get("results") or []
    except Exception:
        return None, None
    if not results:
        return None, None
    top = results[0]
    cid = top.get("cluster_id")
    if not cid:
        return None, None
    craw = _cl_get(f"https://www.courtlistener.com/api/rest/v4/clusters/{cid}/", key)
    try:
        subs = (json.loads(craw) or {}).get("sub_opinions") or []
    except Exception:
        subs = []
    text = ""
    for su in subs:
        oid = su.rstrip("/").split("/")[-1] if isinstance(su, str) else su
        oraw = _cl_get(f"https://www.courtlistener.com/api/rest/v4/opinions/{oid}/", key)
        try:
            o = json.loads(oraw) or {}
        except Exception:
            continue
        t = o.get("plain_text") or ""
        if not t:
            h = (o.get("html_with_citations") or o.get("html")
                 or o.get("html_lawbox") or o.get("html_columbia") or "")
            t = re.sub(r"<[^>]+>", " ", h)
        text += " " + t
    text = re.sub(r"\s+", " ", text).strip()
    return (text, top.get("caseName") or name) if len(text) > 2000 else (None, None)

# ---- helpers --------------------------------------------------------------------------
def _slug(n: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", n.lower()).strip("_")[:48]

def _clean(t: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", t)).strip()

def _split_long(p: str, size: int):
    """Break a single oversized paragraph at SENTENCE boundaries.

    A paragraph longer than `size` must still be cut, or it rides into the corpus whole.
    Live failure: Mallory v. Norfolk Southern (600 U.S. 122) landed as ONE 128,867-char
    chunk because the fetched text had no blank lines at all -- the paragraph split found
    nothing to break on and the size cap never applied. An embedder reads only the first
    few hundred tokens, so ~99% of that opinion was invisible to semantic search while
    still occupying a retrieval slot.
    """
    out = []
    while len(p) > size:
        cut = p.rfind(". ", int(size * 0.6), size)
        if cut < 0:
            cut = p.rfind(" ", int(size * 0.6), size)
        if cut < 0:
            cut = size
        else:
            cut += 1
        out.append(p[:cut].strip())
        p = p[cut:].strip()
    if p:
        out.append(p)
    return out


def _chunks(t: str, size: int = 1400):
    """Chunk an opinion to ~`size` chars, breaking at paragraph then sentence bounds.

    Splitting on blank lines ALONE is not enough: several fetch routes return text with
    single newlines or no newlines (HTML-to-text, some CourtListener plain_text), so
    `re.split(r"\\n\\s*\\n", ...)` returns one giant paragraph and every chunk cap is a
    no-op. Any paragraph over the cap is therefore sentence-split before it is emitted.
    """
    out, buf, n = [], [], 0
    for p in re.split(r"\n\s*\n", t):
        p = p.strip()
        if not p:
            continue
        for piece in (_split_long(p, size) if len(p) > size else [p]):
            if n + len(piece) > size and buf:
                out.append(" ".join(buf)); buf, n = [], 0
            buf.append(piece); n += len(piece)
    if buf:
        out.append(" ".join(buf))
    # MERGE short pieces instead of DISCARDING them. The >60 filter exists to drop
    # navigation/boilerplate noise, but once _split_long cuts a long paragraph the FINAL
    # piece can legitimately be tiny (a 1400-char cut leaves whatever remains), and
    # dropping it silently loses law: re-chunking source '48-29' lost exactly 49 chars,
    # one discarded tail fragment. A short piece that FOLLOWS real text belongs to that
    # text -- append it. Only leading short pieces with nothing to attach to are dropped.
    kept = []
    for c in out:
        if len(c) <= 60:
            if kept:
                kept[-1] = kept[-1] + " " + c
            continue
        kept.append(c)
    return kept

def _log_miss(name, cite):
    try:
        NEEDS_CASE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(NEEDS_CASE_LOG, "a", encoding="utf-8") as f:
            f.write(f"{name}\t{cite}\n")
    except Exception:
        pass

def _parallel_cites(name: str, cite: str, key=None):
    """Discover PARALLEL citations for a case (e.g. 431 A.2d 966 -> 494 Pa. 501).

    A case is often absent from the reporter you were handed but present in CAP under its
    parallel reporter. D'Ambrosio is the worked example: NOT in a2d/431 at all, but in
    pa/494. Without this, a pull reports a false MISS on a case the corpus could have had.
    """
    key = key or _cl_key()
    if not key:
        return []
    q = urllib.parse.quote(f'"{cite}"' if cite else f'"{name}"')
    raw = _cl_get(f"https://www.courtlistener.com/api/rest/v4/search/?type=o&q={q}", key)
    try:
        results = (json.loads(raw) or {}).get("results") or []
    except Exception:
        return []
    if not results:
        return []
    out = []
    for c in (results[0].get("citation") or []):
        c = str(c).strip()
        if c and c.lower() != (cite or "").strip().lower():
            out.append(c)
    return out


def fetch_opinion(name: str, cite: str):
    """Return (text, resolved_name, provider) or (None, None, None).

    Order: CAP (given cite) -> CAP (PARALLEL cites) -> Wikisource -> CourtListener (authenticated).
    """
    parsed = parse_cite(cite) if cite else None
    if parsed:
        txt, nm = from_cap(*parsed)
        if txt:
            return txt, (nm or name), "CAP"
    # same case, different reporter — CAP coverage varies by reporter
    for alt in _parallel_cites(name, cite):
        p2 = parse_cite(alt)
        if not p2 or p2 == parsed:
            continue
        txt, nm = from_cap(*p2)
        if txt:
            return txt, (nm or name), f"CAP:{alt}"
    txt, nm = from_wikisource(name)
    if txt:
        return txt, (nm or name), "WIKISOURCE"
    txt, nm = from_courtlistener(name, cite)
    if txt:
        return txt, (nm or name), "COURTLISTENER"
    return None, None, None

def ingest_case(name: str, cite: str, db: Path = None):
    """Fetch + ingest a single case into chunk_text AND chunk_text_fts. Idempotent.
    Returns {status, source, chunks, provider}."""
    db = db or CHUNK_DB
    src = f"scotus_{_slug(name)}_{re.sub(r'[^a-z0-9]+','', cite.lower())}"
    con = sqlite3.connect(str(db), timeout=60)
    con.execute("PRAGMA busy_timeout=60000")
    present = con.execute("SELECT 1 FROM chunk_text WHERE source=? LIMIT 1", (src,)).fetchone()
    if present:
        con.close()
        return {"status": "already_present", "source": src, "chunks": 0, "provider": None}
    text, resolved, provider = fetch_opinion(name, cite)
    if not text:
        con.close(); _log_miss(name, cite)
        return {"status": "miss", "source": src, "chunks": 0, "provider": None}
    cs = _chunks(_clean(text))
    if not cs:
        con.close(); _log_miss(name, cite)
        return {"status": "empty", "source": src, "chunks": 0, "provider": provider}
    rows = [(f"{src}_chunk_{i}", c, src, "US", cite) for i, c in enumerate(cs)]
    con.executemany("INSERT OR IGNORE INTO chunk_text (chunk_id,text,source,jurisdiction,citations) "
                    "VALUES (?,?,?,?,?)", rows)
    # keep the FTS index (what claim_support_check searches) in sync — no trigger exists
    con.executemany("INSERT INTO chunk_text_fts (text, source_id) VALUES (?,?)",
                    [(c, f"{src}_chunk_{i}") for i, c in enumerate(cs)])
    con.commit(); con.close()
    return {"status": "ingested", "source": src, "chunks": len(cs), "provider": provider}

def backfill_fts(prefixes, db: Path = None):
    """One-pass backfill of chunk_text_fts for sources added without FTS rows (this session's ingests)."""
    db = db or CHUNK_DB
    con = sqlite3.connect(str(db), timeout=120)
    con.execute("PRAGMA busy_timeout=120000")
    globs = " OR ".join(["source GLOB ?"] * len(prefixes))
    params = [p + "*" for p in prefixes]
    rows = con.execute(f"SELECT chunk_id, text, source FROM chunk_text WHERE {globs}", params).fetchall()
    # skip sources already represented in FTS (idempotent) — check one probe chunk per source
    by_src = {}
    for cid, txt, src in rows:
        by_src.setdefault(src, []).append((cid, txt))
    added = 0
    for src, items in by_src.items():
        probe = con.execute("SELECT 1 FROM chunk_text_fts WHERE source_id=? LIMIT 1", (items[0][0],)).fetchone()
        if probe:
            continue
        con.executemany("INSERT INTO chunk_text_fts (text, source_id) VALUES (?,?)",
                        [(txt, cid) for cid, txt in items])
        added += len(items)
    con.commit(); con.close()
    return added

# ── BINDING-AWARE AUTHORITY (Harvard-first) — reusable by the drafter cite-check AND the issue-spotter.
#    Case-agnostic: works for ANY cite, not hardcoded to any case. CAP (free/no-limit) supplies the COURT
#    (which from_cap threw away) so binding can be decided deterministically; CAP-miss -> CourtListener. ──
_CIRCUIT_WORDS = {
    "first": "1", "second": "2", "third": "3", "fourth": "4", "fifth": "5", "sixth": "6",
    "seventh": "7", "eighth": "8", "ninth": "9", "tenth": "10", "eleventh": "11",
    "d.c.": "dc", "district of columbia": "dc",
}
_FORUM_CIRCUIT = {  # forum label -> its controlling federal circuit
    "edpa": "3", "e.d. pa": "3", "e.d.pa": "3", "3d_cir": "3", "ca3": "3", "third": "3",
    "wdpa": "3", "mdpa": "3", "njd": "3", "d. del": "3", "d.del": "3", "pa_": "3",
}

def _forum_circuit(target_court):
    t = (target_court or "").lower()
    for k, v in _FORUM_CIRCUIT.items():
        if k in t:
            return v
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s*cir", t)
    return m.group(1) if m else ""

def cap_meta(cite):
    """CAP json -> (court_name, name_abbrev, full_text) or None. Same source as from_cap, keeps the COURT."""
    p = parse_cite(cite)
    if not p:
        return None
    vol, rep, page = p
    for ordn in ("01", "02", "03"):
        raw = _curl(f"https://static.case.law/{rep}/{vol}/cases/{page:04d}-{ordn}.json")
        if not raw.strip().startswith("{"):
            continue
        try:
            d = json.loads(raw)
        except Exception:
            continue
        cites = " ".join(str(c.get("cite", "")) for c in (d.get("citations") or []))
        if str(page) not in cites or str(vol) not in cites:
            continue
        court = ((d.get("court") or {}).get("name") or "")
        cb = d.get("casebody") or {}
        txt = "\n\n".join(o.get("text", "") for o in (cb.get("opinions") or []) if o.get("text"))
        return court, (d.get("name_abbreviation") or ""), txt
    return None

def _court_binding(court, cite, target_circuit):
    c = (court or "").lower()
    if "supreme court of the united states" in c:
        return "BINDING", "SCOTUS"
    if "court of appeals" in c:
        for w, num in _CIRCUIT_WORDS.items():
            if w in c:
                return ("BINDING" if num == target_circuit else "NON-BINDING"), f"CA{num}"
        return "NON-BINDING", "circuit?"
    if "district court" in c:
        return "NON-BINDING", "district"
    rep = (parse_cite(cite) or (0, "", 0))[1]
    if rep in ("f", "f2d", "f3d"):
        return "NON-BINDING", "circuit(reporter)"
    if "supp" in rep:
        return "NON-BINDING", "district(reporter)"
    return "UNKNOWN", (c[:40] or "?")

AUTH_CACHE = os.environ.get("AUTH_CACHE_DB", str(_eroot() / "corpus/authority_cache.db"))
_GRAPH_DB_FOR_AUTH = os.environ.get("CORPUS_GRAPH_DB", str(_eroot() / "corpus/corpus_graph.db"))

def _auth_cache_conn():
    con = sqlite3.connect(AUTH_CACHE, timeout=30)
    con.execute("CREATE TABLE IF NOT EXISTS auth (cite TEXT, court_tc TEXT, found INT, name TEXT, "
                "court TEXT, binding TEXT, lvl TEXT, PRIMARY KEY(cite, court_tc))")
    return con

def _corpus_knows(cite):
    """The pull engine ingests every fetched case + the graph indexes every cited authority, so a cite
    that is already a graph node is a REAL authority — no network needed to confirm existence."""
    try:
        g = sqlite3.connect(_GRAPH_DB_FOR_AUTH, timeout=10)
        r = g.execute("SELECT 1 FROM edges WHERE dst=? LIMIT 1", (cite,)).fetchone()
        g.close()
        return bool(r)
    except Exception:
        return False

def case_authority(name, cite, target_court="EDPA"):
    """CORPUS-FIRST, Harvard-second authority check for ANY case cite. Returns
    {found, provider, court, level, binding, name, text_len}. Order: (1) persistent cache — every cite
    is resolved ONCE, then instant; (2) the corpus/graph — a cite already indexed is real (no network);
    (3) network CAP (Harvard) -> CourtListener only for a genuinely-unknown cite, then cached. A real
    cite the corpus knows is NEVER reported fabricated on a network rate-limit. Never raises."""
    tc = _forum_circuit(target_court)
    out = {"found": False, "provider": None, "court": "", "binding": "UNKNOWN",
           "level": "", "name": name, "text_len": 0}
    # (1) persistent cache — pulled/verified once, never re-pulled
    try:
        con = _auth_cache_conn()
        row = con.execute("SELECT found,name,court,binding,lvl FROM auth WHERE cite=? AND court_tc=?",
                          (cite, target_court)).fetchone()
        con.close()
        if row:
            return {"found": bool(row[0]), "provider": "cache", "court": row[2] or "",
                    "name": row[1] or name, "binding": row[3] or "UNKNOWN", "level": row[4] or "", "text_len": 0}
    except Exception:
        pass
    corpus_real = _corpus_knows(cite)
    # (2)/(3) resolve court+binding: CAP (Harvard) then CourtListener — only reached on a cache miss
    court, resolved, txt = "", "", ""
    m = cap_meta(cite)
    if m:
        court, resolved, txt = m
        out["provider"] = "CAP"
    else:
        try:
            from pipeline import cite_authority_check as _cac
            p = parse_cite(cite)
            if p:
                r = _cac.lookup_courtlistener(p[1], p[0], p[2])
                if isinstance(r, dict) and r.get("found"):
                    court = r.get("court") or ""
                    resolved = r.get("case_name") or name
                    out["provider"] = "COURTLISTENER"
        except Exception:
            pass
    if out["provider"]:
        out["found"] = True
        out["court"] = court
        out["name"] = resolved or name
        out["text_len"] = len(txt or "")
        out["binding"], out["level"] = _court_binding(court, cite, tc)
    elif corpus_real:
        # network missed/rate-limited but the corpus knows the cite is real — never call it fabricated
        out["found"] = True
        out["provider"] = "corpus"
        out["binding"] = "UNKNOWN"
    # cache the resolution (found or not) so we don't re-pull
    try:
        con = _auth_cache_conn()
        con.execute("INSERT OR REPLACE INTO auth VALUES (?,?,?,?,?,?,?)",
                    (cite, target_court, int(out["found"]), out["name"], out["court"], out["binding"], out["level"]))
        con.commit(); con.close()
    except Exception:
        pass
    # INGEST-ON-VERIFY: on a FRESH network pull of a real case, persist its opinion text to the corpus (FTS)
    # in the BACKGROUND, so the stands-for gate retrieves it by fast FTS next time instead of slow turbovec-
    # semantic. ingest_case is GPU-free + idempotent; a daemon thread keeps verification non-blocking. This is
    # what keeps the gate fast for EVERY future draft automatically (the user never has to ingest by hand).
    if out.get("provider") in ("CAP", "COURTLISTENER") and out.get("found"):
        try:
            import threading as _th
            _th.Thread(target=lambda: ingest_case(out.get("name") or name, cite), daemon=True).start()
        except Exception:
            pass
    return out

def _selftest():
    r = ingest_case("Duncan v. Walker", "533 U.S. 167")
    print("selftest ingest_case:", r)
    assert r["status"] in ("ingested", "already_present"), r
    assert parse_cite("533 U.S. 167") == (533, "us", 167)
    assert parse_cite("288 F.2d 545") == (288, "f2d", 545)
    print("OK")

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--cite", default="")
    ap.add_argument("--name", default="")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        _selftest()
    elif a.cite or a.name:
        print(json.dumps(ingest_case(a.name or a.cite, a.cite), indent=2))
    else:
        ap.print_help()
