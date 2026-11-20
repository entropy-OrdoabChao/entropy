"""Deterministic card gate — THE single quote verifier for the room pipeline (cells_to_ground.reverify delegates here).
A quoted authority on a CARD is VERIFIED only when the FULL quoted sentence (whitespace/quote-mark normalised, every
elided segment) is a substring of ONE corpus chunk whose source belongs to the cited authority's family
(usc_t<T>_s<sec><letter> / amjur2d_<vol> / federal rule text carrying 'Rule <n>' / reporter slug or both party names).
A match of only a leading/trailing/middle window is status 'partial' and NEVER counts as verified (S-07).
[doc] quotes (record facts) are checked against the room's input files only.
Backend: sqlite FTS5 over chunk_text.db (read-only, no torch, no HTTP). CARD_GATE_HTTP=1 adds the corpus HTTP search
as an extra candidate finder (candidates are still full-text checked here).
usage: card_gate.py <cards.md> [input_dir]   (rewrites in place, prints a tally)"""
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import re, sys, json, glob, sqlite3, os, urllib.request, urllib.parse, importlib.util

DB = os.environ.get("CHUNK_DB", "file:~/turbovec/chunk_text.db?mode=ro")
CORPUS = "http://127.0.0.1:8100/search_text"
Q = re.compile(r'[“"]([^”"]{4,400})[”"]')
_con = None


def _db():
    global _con
    if _con is None:
        _con = sqlite3.connect(DB, uri=True, timeout=90)
        _con.execute("PRAGMA busy_timeout=90000")
    return _con


def norm(s):
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"').replace("—", "-").replace("–", "-").replace("\u00a0", " ")
    return re.sub(r"\s+", " ", s).strip().lower()


def segments(quote):
    """an elided quote ("A ... B") must match segment by segment; each segment ≥ 4 words is checked, all must hold."""
    segs = [x.strip(" ,;:") for x in re.split(r"\s*(?:\.\.\.|…|\[\.\.\.\]|\[…\])\s*", quote) if x.strip()]
    segs = [x for x in segs if len(x.split()) >= 4] or segs
    return segs


def _in_text(q, text):
    """FULL-segment substring match, tolerant of bracketed alterations and punctuation differences."""
    if q in text: return True
    strip = lambda x: re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", "", re.sub(r"\[[^\]]*\]", "", x))).strip()
    qs, ts = strip(q), strip(text)
    return len(qs) > 20 and qs in ts


# ---------------------------------------------------------------- authority family (class rule, no per-case list)
_USC = re.compile(r"(\d+)\s*U\.?S\.?C\.?A?\.?\s*§+\s*(\d+[a-z]?(?:-\d+[a-z]?)?)")
_RULE = re.compile(r"Fed\. ?R\. ?(Civ|App|Crim|Evid)\. ?P?\.? ?(\d+)")
_AMJUR = re.compile(r"(\d+[A-Z]?) Am\. ?Jur\. ?2d")
_REP = re.compile(r"(\d+)\s+(U\.S\.|F\.\d?d|F\. ?Supp\.? ?\d?d?|S\. ?Ct\.|A\.\d?d|Pa\.)\s+(\d+)")
_RULEFAM = {"Civ": r"federal.?rules.?of.?civil|frcp|\d+bv$", "App": r"federal.?rules.?of.?appellate|frap|\d+bv$",
            "Crim": r"federal.?rules.?of.?criminal|frcrmp", "Evid": r"federal.?rules.?of.?evidence|fre\b"}


def family_ok(cite, src, cid, text):
    """Does chunk (src, cid, text) belong to the authority family of `cite`?  Returns the match kind, '' on mismatch,
    or 'family-unparsed' when the cite names no family this gate knows (forum/local rules, treatises other than Am Jur)."""
    cite = cite or ""; S, C, T = (src or "").lower(), (cid or "").lower(), text or ""
    m = _USC.search(cite)
    if m:
        key = f"usc_t{m.group(1)}_s{m.group(2).lower()}"
        rx = rf"(^|[^a-z0-9]){re.escape(key)}([^a-z0-9]|$)"
        return "usc-key" if re.search(rx, S) or re.search(rx, C) else ""
    m = _RULE.search(cite)
    if m:
        if not re.search(_RULEFAM[m.group(1)], S): return ""
        return "rule+number" if re.search(rf"\bRule {m.group(2)}\b", T) else ""
    m = _AMJUR.search(cite)
    if m: return "amjur-volume" if f"amjur2d_{m.group(1).lower()}" in S else ""
    m = _REP.search(cite)
    if m:
        v, pg = m.group(1), m.group(3)
        if re.search(rf"(?<!\d){v}[_-]?(us|f\d?d|fsupp\d?|sct|a\d?d|pa)?[_-]?{pg}(?!\d)", S): return "reporter-slug"
        parties = re.match(r"\s*(?:In re |Ex parte )?([A-Z][\w'.-]+)[^,]*?\bv\.\s+([A-Z][\w'.-]+)", cite)
        if parties and all(re.search(rf"\b{re.escape(p)}\b", T) for p in parties.groups()): return "both-parties"
        return ""
    return "family-unparsed"


# ---------------------------------------------------------------- S-family-check (2026-11-13, class — BACKLOG "a cite is
# 'resolved' when the pinned chunk merely MENTIONS it"): the PINNED SOURCE must BE the cite the card NAMES, not a chunk that
# happens to carry the words. family_ok() above answers "is this chunk in the cite's family" for a QUOTE; the pair below is
# the DETERMINISTIC GRADING check: parse the cite the card names and the source key of the chunk it attaches, and refuse the
# card when their family / title / chapter / section CONTRADICT each other (24 C.F.R. § 203.431 pinned to usc_t12_s1748b;
# 13 Pa.C.S. § 9308(e) pinned to pa_cs_t7). Nothing here is case-specific and nothing guesses: a part that does not parse on
# either side is None and NEVER produces a mismatch ("unchecked"), so a forum/local rule or a treatise is never failed blind.
# cite_family(cite) is the contract pipeline/cite_family.py publishes (family/title/chapter/section); when that live module
# exists it is preferred, so the two children's parsers can never disagree.
_EXT_CITE_FAMILY = "unset"


def _ext_cite_family():
    """pipeline/cite_family.py (another organ of the same backlog item) when it exists AND honours the contract; else None."""
    global _EXT_CITE_FAMILY
    if _EXT_CITE_FAMILY == "unset":
        _EXT_CITE_FAMILY = None
        for root in (str(Path.home() / "beezle"), str(_eroot())):
            p = os.path.join(root, "pipeline", "cite_family.py")
            if not os.path.exists(p): continue
            try:
                spec = importlib.util.spec_from_file_location("_cite_family_ext", p)
                if spec is None or spec.loader is None: break
                m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
                fn = getattr(m, "cite_family", None) or getattr(m, "family", None) or getattr(m, "_family", None)
                probe = fn("12 U.S.C. § 1748b") if fn else None
                if isinstance(probe, dict) and {"family", "title", "chapter", "section"} <= set(probe):
                    _EXT_CITE_FAMILY = fn
            except Exception:
                _EXT_CITE_FAMILY = None
            break
    return _EXT_CITE_FAMILY


_RULE_TITLE = {"Pa.R.C.P": "231", "Pa.R.A.P": "210", "Pa.R.E": "225", "Pa.R.Crim.P": "234", "Pa.R.J.A": "201",
               "Pa.R.D.E": "204", "Pa.R.Civ.P": "231", "Pa.R.J.C.P": "237"}
_F_USC = re.compile(r"(\d+)\s*U\.?\s?S\.?\s?C\.?(?:A\.?)?\s*§*\s*(\d+[A-Za-z]?(?:[-–]\d+[a-z]?)?)?", re.I)
_F_CFR = re.compile(r"(\d+)\s*C\.?\s?F\.?\s?R\.?\s*§*\s*(\d+)(?:\.(\d+[a-z]?))?", re.I)
_F_PACS = re.compile(r"(\d+)\s*Pa\.?\s?C\.?\s?S\.?(?:A\.?)?\s*§*\s*(\d+)?", re.I)
_F_PS = re.compile(r"(\d+)\s*P\.\s?S\.\s*§*\s*([\d.]+)?", re.I)
_F_PACODE = re.compile(r"(\d+)\s*Pa\.?\s?Code\s*§*\s*([\d.]+)?", re.I)
_F_PARULE = re.compile(r"(Pa\.\s?R\.\s?[A-Za-z.]*?P|Pa\.\s?R\.\s?E|Pa\.\s?R\.\s?J\.\s?A)\.?\s*§*\s*(\d+(?:\.\d+)?)", re.I)
_F_FRULE = re.compile(r"Fed\.?\s?R\.?\s?(Civ|App|Crim|Evid|Bankr)\.?\s?P?\.?\s*(\d+)", re.I)
_F_LOCAL = re.compile(r"(Phila\.?\s?Civ\.?\s?R\.?|L\.?R\.?|Local Rule)\s*\*?\s*(\d+)", re.I)
_F_AMJUR = re.compile(r"(\d+[A-Z]?)\s*Am\.?\s?Jur\.?\s?2d", re.I)


def _blank(**kw):
    d = {"family": None, "title": None, "chapter": None, "section": None}
    d.update(kw); return d


def cite_family(cite):
    """A CITE as written → {family, title, chapter, section} (None where the cite does not say). Chapter is derived only
    where the numbering system states it: a CFR section is <part>.<sec> (part = chapter); a Pa.C.S. section is
    <chapter><2 digits> (§ 9308 → ch. 93, the UCC-9 chapters). Unknown citation form → family None (never a mismatch)."""
    ext = _ext_cite_family()
    if ext:
        try:
            r = ext(cite or "")
            if isinstance(r, dict): return {k: (r.get(k) or None) for k in ("family", "title", "chapter", "section")}
        except Exception:
            pass
    c = re.sub(r"\s+", " ", cite or "").strip()
    if not c: return _blank()
    m = _F_CFR.search(c)
    if m: return _blank(family="cfr", title=m.group(1), chapter=m.group(2), section=(f"{m.group(2)}.{m.group(3)}" if m.group(3) else None))
    m = _F_USC.search(c)
    if m:
        sec = (m.group(2) or "").lower() or None
        return _blank(family="usc", title=m.group(1), section=sec)
    m = _F_PACS.search(c)
    if m:
        sec = m.group(2)
        ch = sec[:-2] if sec and len(sec) > 2 else None
        return _blank(family="pacs", title=m.group(1), chapter=ch, section=sec)
    m = _F_PS.search(c)
    if m: return _blank(family="pastat", title=m.group(1), section=m.group(2))
    m = _F_PACODE.search(c)
    if m: return _blank(family="pacode", title=m.group(1), section=m.group(2))
    m = _F_PARULE.search(c)
    if m:
        key = re.sub(r"[\s]", "", m.group(1)).rstrip(".")
        return _blank(family="pacode", title=_RULE_TITLE.get(key), section=m.group(2))
    m = _F_FRULE.search(c)
    if m: return _blank(family="frule", chapter=m.group(1).lower(), section=m.group(2))
    m = _F_AMJUR.search(c)
    if m: return _blank(family="amjur", title=m.group(1).lower())
    # a forum/local rule names no code family this gate can key on (same case as family_ok's 'family-unparsed') → unchecked
    if _F_LOCAL.search(c): return _blank()
    if _REP.search(c) or " v. " in c: return _blank(family="case")
    return _blank()


_S_USC = re.compile(r"^usc_t(\d+)_s([0-9a-z\u2013–-]+)", re.I)
_S_USC_LII = re.compile(r"^USCODE-[A-Z]+-title(\d+)", re.I)
_S_CFR_GOV = re.compile(r"^CFR-\d{4}-title(\d+)-vol\d+(?:-sec(\d+)-(\d+[a-z]?))?", re.I)
_S_CFR_K = re.compile(r"^cfr_(\d+)(?:_(\d+))?(?:_(\d+[a-z]?))?", re.I)
_S_PACS = re.compile(r"^pa_cs_t(\d+)(?:_ch(\d+))?", re.I)
_S_PACODE = re.compile(r"^pa_code_t(\d+)(?:_ch(\d+))?", re.I)
_S_PS = re.compile(r"^pa_(\d+)ps", re.I)
_S_AMJUR = re.compile(r"^amjur2d_(\w+)", re.I)


def source_family(src):
    """A corpus SOURCE KEY → {family, title, chapter, section}. The key is the corpus' own name for the authority, so this is
    a lookup, not an inference; an unrecognised key (comps-*, a .md doc, a reporter slug) → family None = unchecked."""
    s = (src or "").split("#")[0].strip()
    s = re.sub(r"_c\d+$", "", s)                      # chunk suffix (usc_t12_s1748b_c0)
    if not s: return _blank()
    m = _S_USC.match(s)
    if m:
        sec = re.split(r"[,\s]", m.group(2))[0].lower().replace("\u2013", "-")
        return _blank(family="usc", title=m.group(1), section=sec or None)
    m = _S_USC_LII.match(s)
    if m: return _blank(family="usc", title=m.group(1))
    m = _S_CFR_GOV.match(s)
    if m: return _blank(family="cfr", title=m.group(1), chapter=m.group(2), section=(f"{m.group(2)}.{m.group(3)}" if m.group(3) else None))
    m = _S_CFR_K.match(s)
    if m: return _blank(family="cfr", title=m.group(1), chapter=m.group(2), section=(f"{m.group(2)}.{m.group(3)}" if m.group(2) and m.group(3) else None))
    m = _S_PACS.match(s)
    if m: return _blank(family="pacs", title=m.group(1), chapter=m.group(2))
    if s.startswith("pa_consolidated_statutes"): return _blank(family="pacs")
    m = _S_PACODE.match(s)
    if m: return _blank(family="pacode", title=m.group(1))     # Pa. Code chapter numbering differs per title — title only
    m = _S_PS.match(s)
    if m: return _blank(family="pastat", title=m.group(1))
    m = _S_AMJUR.match(s)
    if m: return _blank(family="amjur", title=m.group(1).lower())
    return _blank()


def _desc(cite, f):
    bits = [f["family"] or "?"]
    if f["title"]: bits.append("t" + str(f["title"]))
    if f["chapter"]: bits.append("ch" + str(f["chapter"]))
    if f["section"]: bits.append("§" + str(f["section"]))
    return f"{cite} [{' '.join(bits)}]" if cite else " ".join(bits)


def family_check(cite, src):
    """(verdict, why) for a cite ↔ pinned-source pair. verdict: 'ok' | 'unchecked' | 'mismatch'.
    MISMATCH only on a positive contradiction, part by part (family, then title, then chapter, then section) — a part
    either side leaves unparsed can never fail. why = \"family-mismatch: card names X, chunk is Y\"."""
    cf, sf = cite_family(cite), source_family(src)
    if not cf["family"] or not sf["family"]:
        return "unchecked", f"family unparsed (cite={cf['family'] or '?'}, source={sf['family'] or '?'})"
    bad = None
    if cf["family"] != sf["family"]: bad = "family"
    elif cf["title"] and sf["title"] and str(cf["title"]) != str(sf["title"]): bad = "title"
    # chapter is compared only for Pa.C.S., the one family whose corpus keys state it unambiguously (pa_cs_t13_ch92 holds
    # § 92xx, ch93 holds § 93xx — probed, not assumed). A CFR ingest key (cfr_26_16050p) does NOT encode part/section, so a
    # chapter compare there would fail a correct pin; CFR drift is caught by the section compare on CFR-…-sec<part>-<sec>.
    elif cf["family"] == "pacs" and cf["chapter"] and sf["chapter"] and str(cf["chapter"]) != str(sf["chapter"]): bad = "chapter"
    elif cf["section"] and sf["section"] and cf["section"].lower() != sf["section"].lower(): bad = "section"
    if bad:
        return "mismatch", f"family-mismatch ({bad}): card names {_desc(cite, cf)}, chunk is {_desc((src or '').split('#')[0], sf)}"
    return "ok", f"{cf['family']} t{cf['title'] or '?'} agrees"


_SRC_CACHE = {}


def chunk_source(cid, hint_map=None, cache=None):
    """chunk id (or '<source_key>#<id>') → the chunk's SOURCE KEY. The DB is asked FIRST and its answer wins: a lane may
    write a key that does not exist in the corpus (pa_code_t2319 for pa_code_t231_ch1019), and trusting that prefix would
    report a family mismatch against a pin that is in fact correct. Ladder: chunk_text on the id (exact, then prefix — lanes
    truncate ids to 8-10 hex) → the pack's own 'source=' hints → the written prefix, only if it parses as a key. '' when
    nothing resolves, which family_check reads as 'unchecked' and never fails. Results cached (optional caller dict)."""
    cid = (cid or "").strip()
    if not cid: return ""
    cache = _SRC_CACHE if cache is None else cache
    if cid in cache: return cache[cid]
    head, _, idpart = cid.partition("#")
    out = ""
    for probe in [x for x in (idpart, head) if x]:
        try:
            r = _db().execute("SELECT source FROM chunk_text WHERE chunk_id=?", (probe,)).fetchone()
            if not r and re.fullmatch(r"[0-9a-f]{8,}", probe):
                r = _db().execute("SELECT source FROM chunk_text WHERE chunk_id>=? AND chunk_id<? LIMIT 1", (probe, probe + "z")).fetchone()
            if r and r[0]: out = r[0]; break
        except Exception:
            pass
    if not out and hint_map:
        out = hint_map.get(cid) or hint_map.get(idpart) or hint_map.get(head) or ""
        if not out:
            for k, v in hint_map.items():                    # the lane may quote a truncated id of a hinted chunk
                if k and (k.startswith(idpart or head) or (idpart or head).startswith(k)) and min(len(k), len(idpart or head)) >= 8:
                    out = v; break
    if not out and head and source_family(head)["family"]: out = head
    cache[cid] = out
    return out


# ---------------------------------------------------------------- candidates + verdict
def _fts(phrase, limit=40):
    toks = re.sub(r"[^\w\s§]", " ", phrase).split()
    if not toks: return []
    try:
        return [r[0] for r in _db().execute("SELECT source_id FROM chunk_text_fts WHERE chunk_text_fts MATCH ? LIMIT ?",
                                            ('"' + " ".join(toks) + '"', limit)).fetchall()]
    except Exception:
        return []


def _chunk(cid):
    try: r = _db().execute("SELECT source, text FROM chunk_text WHERE chunk_id=?", (cid,)).fetchone()
    except Exception: r = None
    return (r[0], r[1]) if r else (None, None)


def _http_candidates(q, k=20):
    try:
        url = CORPUS + "?" + urllib.parse.urlencode({"query": q, "top_k": k})
        return [(h.get("chunk_id") or h.get("source"), h.get("source"), h.get("text", "")) for h in json.load(urllib.request.urlopen(url, timeout=60)).get("hits", [])]
    except Exception:
        return []


def _candidates(seg):
    """(cid, source, text) candidates: full-phrase FTS first, then lead-8-words FTS, then optional HTTP."""
    seen, out = set(), []
    for phrase, lim in ((seg, 40), (" ".join(seg.split()[:8]), 60)):
        for cid in _fts(phrase, lim):
            if cid in seen: continue
            src, txt = _chunk(cid)
            if txt is not None: seen.add(cid); out.append((cid, src, txt))
    if os.environ.get("CARD_GATE_HTTP") == "1":
        for cid, src, txt in _http_candidates(seg):
            if cid and cid not in seen: seen.add(cid); out.append((cid, src, txt))
    return out


def _windows(seg):
    w = seg.split()
    wins = [" ".join(w[:18]), " ".join(w[-18:])]
    if len(seg) > 60: wins.append(seg[len(seg)//2 - 30: len(seg)//2 + 30])
    return [x for x in wins if len(x.split()) >= 4]


def verify_phrase(quote, cite="", cid=""):
    """-> {"status": verified|partial|unverified, "chunk_id", "source", "how"}.
    verified : every segment of the quote is a full substring of a chunk in the cited family (same chunk preferred).
    partial  : only a window (first/last 18 words or a 60-char middle) of some segment was found — NOT verified.
    unverified: nothing found, or found only in a chunk of another authority family (how says which).
    cid (S-prefetch 2026-11-10): the chunk_id the card itself names (GROUND+ line / authority field '— <chunk_id>'): checked FIRST by
    direct lookup — FTS candidate recall missed 9 of 47 prefetch quotes (§/-- tokens in statute chunks) whose words ARE in that chunk."""
    segs = [norm(s) for s in segments(quote)] or [norm(quote)]
    if cid:
        src, txt = _chunk(cid)
        if txt is not None and all(_in_text(seg, norm(txt)) for seg in segs):
            fam = family_ok(cite, src, cid, txt)
            if fam: return {"status": "verified", "chunk_id": cid, "source": src, "how": f"chunk_id on card; {fam}"}
    hits, wrong = [], None
    for seg in segs:
        full = None
        cands = _candidates(seg)
        for cid, src, txt in cands:
            if not _in_text(seg, norm(txt)): continue
            fam = family_ok(cite, src, cid, txt)
            if fam: full = (cid, src, fam); break
            wrong = wrong or (cid, src)
        if full:
            hits.append(full); continue
        # no full in-family match: a window may be there — reported as partial, never verified
        for win in _windows(seg):
            pool = cands or [(c, s, t) for c in _fts(win, 40) for (s, t) in [_chunk(c)] if t is not None]
            for cid, src, txt in pool:
                if win in norm(txt) and family_ok(cite, src, cid, txt):
                    return {"status": "partial", "chunk_id": cid, "source": src, "how": "window only — full sentence not in chunk"}
        if wrong: return {"status": "unverified", "chunk_id": wrong[0], "source": wrong[1], "how": "phrase found, but in another authority's chunk"}
        return {"status": "unverified", "chunk_id": None, "source": None, "how": "no exact corpus phrase"}
    cids = {h[0] for h in hits}
    note = hits[0][2] + ("" if len(hits) == 1 else ("; elided quote, all segments in one chunk" if len(cids) == 1 else "; segments in different chunks — check context"))
    return {"status": "verified", "chunk_id": hits[0][0], "source": hits[0][1], "how": note}


def in_corpus(quote, cite=""):
    """back-compat: (found, where) — found only when verify_phrase says verified."""
    v = verify_phrase(quote, cite)
    return (True, v["chunk_id"]) if v["status"] == "verified" else (False, None)


def in_docs(quote, docs):
    segs = [norm(x) for x in segments(quote)]
    for name, text in docs.items():
        if all(_in_text(q, text) for q in segs): return name
    return None


# ---------------------------------------------------------------- S-cite-drift-normalize (2026-10-24, run 42): the ONE cite pinner
# pipeline/m2/cite_normalize.py (sibling tree first, live tree second). A case name that resolves to exactly one pinned corpus
# source gets the pin's reporter cite; 0 -> FETCH; >1 -> exact match or FETCH. The gate ANNOTATES the card line
# ('GATE: cite normalized OLD -> NEW [source]' / 'GATE: cite FETCH ...'); cards_to_cells rewrites the cell. CITE_NORMALIZE=off disables.
_CN = None
_RESOLVER = None


def cite_normalizer():
    """-> (module, Resolver) | (None, None)."""
    global _CN, _RESOLVER
    if _RESOLVER is not None or _CN is False:
        return (_CN or None), _RESOLVER
    if os.environ.get("CITE_NORMALIZE", "").lower() in ("off", "0", "none"):
        _CN = False; return None, None
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(os.path.dirname(os.path.dirname(here)), "pipeline", "m2", "cite_normalize.py"),
                 str(_eroot() / "pipeline/m2/cite_normalize.py")):
        if os.path.exists(cand):
            try:
                spec = importlib.util.spec_from_file_location("cite_normalize", cand); mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod); R = mod.Resolver(); R.keys(); _CN, _RESOLVER = mod, R
                return _CN, _RESOLVER
            except Exception as e:
                print(f"card_gate: cite normalizer unavailable ({e!r}) -- cites NOT pinned", file=sys.stderr)
                break
    _CN = False
    return None, None


def normalize_note(cite):
    """-> (new_cite, 'GATE: cite normalized OLD -> NEW [source]' | 'GATE: cite FETCH (...)' | '')."""
    cn, R = cite_normalizer()
    if not cn or not cite or " v. " not in cite: return cite, ""
    try:
        new, res = cn.normalize_cite(cite, R)
    except Exception as e:
        return cite, f"GATE: cite normalizer error ({str(e)[:60]})"
    if res["action"] == "normalized": return new, f"GATE: cite normalized {res['old']} -> {res['new']} [{res['source']}]"
    if res["action"] == "fetch": return cite, f"GATE: cite FETCH ({res['why']})"
    return cite, ""


def cite_of(auth):
    """the citation part of an authority field: text before the first quote mark / em-dash."""
    return re.split(r"\s+[—-]\s+|[“\"]", auth or "", 1)[0].strip().strip("[]")


def gate(path, input_dir=None):
    docs = {}
    if input_dir:
        for f in glob.glob(input_dir + "/*.md") + glob.glob(input_dir + "/*.txt"):
            docs[f.split("/")[-1]] = norm(open(f, errors="ignore").read())
    out = []; tally = {"cards": 0, "quoted": 0, "doc": 0, "corpus": 0, "partial": 0, "forced_open": 0, "unquoted_authority": 0}
    for line in open(path, errors="ignore").read().splitlines():
        if not line.startswith("CARD:"):
            out.append(line); continue
        tally["cards"] += 1
        parts = [p.strip() for p in line.split(" | ")]
        parts = [p for p in parts if not p.startswith("GATE:")]          # re-gating: drop stale notes
        # tolerant layout: axis | finding | [where] | [authority] | STATUS [| PROPOSITION: …] — bots drop optional fields
        si = next((i for i in range(len(parts) - 1, 1, -1) if re.match(r"(SUSTAINED|OPEN|REJECTED|NOT IN CORPUS)\b", parts[i].upper())), None)
        if si is None:
            out.append(line); continue
        auth = parts[si - 1] if si >= 3 else ""; status = parts[si]
        parts_status_idx = si
        if "NOT IN CORPUS" in auth or auth in ("—", "-", ""):
            out.append(" | ".join(parts)); continue
        allq = Q.findall(auth) + [q for q in Q.findall(line) if q not in Q.findall(auth)]   # authority-field quotes first
        quotes = [q for q in allq if len(q.split()) >= 4]
        short = [q for q in allq if len(q.split()) < 4]   # 1-3 word quotes (a typo, a term) are checked against the inputs only
        if not quotes and short:
            d = next((in_docs(q, docs) for q in short if in_docs(q, docs)), None)
            if d: tally["doc"] += 1; out.append(" | ".join(parts) + f" | GATE: short quote verified ({d})"); continue
        if not quotes:
            tally["unquoted_authority"] += 1
            if status.upper().startswith("SUSTAINED"):
                parts[parts_status_idx] = "OPEN"; tally["forced_open"] += 1; out.append(" | ".join(parts) + " | GATE: authority carries no quote"); continue
            out.append(" | ".join(parts)); continue
        tally["quoted"] += 1
        cite = cite_of(auth)
        # S-prefetch: the chunk_id the card names after its quote ('cite — "words" — <chunk_id>' or '[corpus:<chunk_id>]') → direct check first
        cm = re.search(r"\[corpus:([A-Za-z0-9_.:-]+)\]", line) or re.search(r"[\"”]\s*[—-]\s*([A-Za-z0-9_.:-]{8,})\s*$", auth)
        card_cid = cm.group(1) if cm else ""
        ok = False; where = None; best = None
        for qt in quotes:
            d = in_docs(qt, docs)
            if d: ok = True; where = d; tally["doc"] += 1; break
            v = verify_phrase(qt, cite, card_cid)
            if v["status"] == "verified": ok = True; where = v["chunk_id"]; tally["corpus"] += 1; break
            if best is None or (v["status"] == "partial" and best["status"] != "partial"): best = v
        _, cn_note = normalize_note(cite)
        cn_note = f" | {cn_note}" if cn_note else ""
        if ok:
            out.append(" | ".join(parts) + f" | GATE: quote verified ({where})" + cn_note)
        else:
            parts[parts_status_idx] = "OPEN"; tally["forced_open"] += 1
            if best and best["status"] == "partial":
                tally["partial"] += 1
                out.append(" | ".join(parts) + f" | GATE: quote NOT verified — partial match only ({best['chunk_id']}); full sentence not in chunk" + cn_note)
            elif best and best.get("chunk_id"):
                out.append(" | ".join(parts) + f" | GATE: quote NOT verified — found only in another authority's chunk ({best['chunk_id']})" + cn_note)
            else:
                out.append(" | ".join(parts) + " | GATE: quote NOT found in corpus or inputs" + cn_note)
    open(path, "w").write("\n".join(out) + "\n")
    return tally


if __name__ == "__main__":
    print(json.dumps(gate(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)))
