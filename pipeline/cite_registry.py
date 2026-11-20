"""cite_registry.py — ONE lookup for "which document in the corpus IS this citation" (S-corpus-registry, 2026-11-01).
Problem: sources are slugged six ways (cap_us_550_544_x, scotus_x_550us544, docvault_pull_f3d_617_688, docvault_pull_<hash>,
577BV, 1000365223_004); resolving a reporter cite was a slug-regex guess and the `citations` column is unindexed (LIKE = minutes).
This builds ~/turbovec/cite_registry.db: cite_registry(reporter, vol, page, source, caption, court_code, year, provenance)
UNIQUE(reporter, vol, page, source), from (a) slug patterns, (b) docvault_pull citations column, (c) the opinion head of chunk 0
(caption + court). Rules/statutes: rule_registry(family, title, source) from pa_/usc_/CFR-/ECFR-/cfr_/FR-/pa_code/NNN_NNNN slugs (see RULE_PATS).
Read-only on chunk_text.db. Incremental: --build (resumes at build_state.last_source); --build-rules = rule_registry-only full pass (2 s, idempotent).
Query: lookup(rep) / lookup_rule(family, title).
usage: cite_registry.py --build [--limit N]   |   cite_registry.py --build-rules   |   cite_registry.py --lookup "617 F.3d 688"   |   --lookup-rule C.F.R. 12
"""
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import re, os, sys, json, time, sqlite3, argparse
CHUNK_DB = str(_eroot() / "corpus/chunk_text.db")
REG_DB = os.environ.get("CITE_REGISTRY_DB", str(_eroot() / "corpus/cite_registry.db"))

REP_CANON = {"us": "U.S.", "f3d": "F.3d", "f2d": "F.2d", "f4th": "F.4th", "fsupp": "F. Supp.", "fsupp2d": "F. Supp. 2d", "fsupp3d": "F. Supp. 3d",
             "a2d": "A.2d", "a3d": "A.3d", "sct": "S. Ct.", "pa": "Pa.", "pasuper": "Pa. Super.", "nj": "N.J.", "njsuper": "N.J. Super.",
             "njeq": "N.J. Eq.", "fappx": "F. App'x", "led2d": "L. Ed. 2d", "pacmwlth": "Pa. Cmwlth."}
SLUG_PATS = [  # (regex over source, reporter-key group, vol group, page group)
    (r"^cap_([a-z]+?)_(\d+)_(\d+)_", 1, 2, 3),                       # cap_us_550_544_bell_atlantic...
    (r"^docvault_pull_([a-z0-9]+)_(\d+)_(\d+)$", 1, 2, 3),            # docvault_pull_f3d_617_688
    (r"_(\d+)(us|f3d|f2d|a2d|a3d|fsupp3d|fsupp2d|fsupp|sct)(\d+)$", 2, 1, 3),   # scotus_x_550us544 / kelly_159fsupp3d562
    (r"^(\d+)(us|f3d|f2d|a2d|a3d)(\d+)[_-]", 2, 1, 3),               # 594us2r59 style (vol+rep+page prefix)
]
COURT_RX = [(r"supreme court of the united states", "SCOTUS"), (r"(?:third|3d|3rd) cir", "CA3"), (r"court of appeals.{0,30}(?:fifth|5th) circuit", "CA5"),
            (r"court of appeals.{0,30}(?:seventh|7th) circuit", "CA7"), (r"court of appeals.{0,30}(?:eleventh|11th) circuit", "CA11"),
            (r"(?:\b(?:first|second|fourth|sixth|eighth|ninth|tenth|d\.c\.|federal|1st|2d|4th|6th|8th|9th|10th)\s+cir|court of appeals)", "OTHER_CIRCUIT"), (r"eastern district of pennsylvania", "ED_PA"), (r"district court", "OTHER_DISTRICT"),
            (r"superior court of pennsylvania", "PA_SUPER"), (r"commonwealth court of pennsylvania", "PA_CMWLTH"), (r"supreme court of pennsylvania", "PA_SUPREME"),
            (r"supreme court of new jersey", "NJ_SUPREME"), (r"superior court of new jersey", "NJ_SUPER"), (r"court of common pleas", "PA_CP_OTHER")]
RULE_PATS = [(r"^pa_(\d+)ps_", "P.S."), (r"^pa_rcp_?(\d+)", "Pa.R.C.P."), (r"^pa_rap_?(\d+)", "Pa.R.A.P."), (r"^usc_t(\d+)", "U.S.C."), (r"^cfr_t(\d+)", "C.F.R."),
             (r"^pa_code_t?(\d+)", "Pa. Code"), (r"^frcp_?(\d+)", "Fed. R. Civ. P."), (r"^frap_?(\d+)", "FRAP"),
             # ---- REGULATIONS (S-corpus-registry-cfr, 2026-11-09). The `^cfr_t` pattern above never matched a real key, so the
             # registry held ZERO C.F.R. rows while the corpus holds the whole CFR. Real slug shapes (chunk_text.db census 2026-11-09):
             #   CFR-<yyyy>-title<N>-vol<N>[-sec…]   govinfo annual edition (495 keys / 364k chunks; lower-case `cfr-` twins are 200-cap copies)
             #   ECFR-title<N>                        eCFR (98 keys / 535k; `ecfr-` twins 200-cap)      cfr_<title>_<part>_<slug>  hand pulls (78)
             #   courtlistener_cfr_<t>_part_<p> (32)  doi_btfa_<t>cfr<part> (3)  <t>cfr<part>… (5cfr1315, 31cfr208final)  title<roman>-<t>CFR-pt<p>
             # Patterns are appended AFTER the legacy ones, so no source that matched before resolves differently (first match wins).
             # `title` = CFR title number. An optional 3rd element is a transform applied to group(1).
             (r"(?i)^cfr[-_]\d{4}[-_]title(\d+)", "C.F.R."), (r"(?i)^ecfr[-_]title(\d+)", "C.F.R."), (r"^cfr_(\d+)_", "C.F.R."),
             (r"^courtlistener_cfr_(\d+)_", "C.F.R."), (r"^doi_btfa_(\d+)cfr", "C.F.R."), (r"(?i)^(\d+)cfr\d", "C.F.R."), (r"(?i)^title[ivx]+-(\d+)cfr-", "C.F.R."),
             (r"(?i)^uscourts_cris_ecfr_(\d+)_", "C.F.R."),
             # Federal Register issue FR-<yyyy>-<mm>-<dd> (and lower-case twin): `title` = FR volume = year - 1935 (1 FR = 1936; 83 FR = 2018).
             (r"(?i)^fr-(\d{4})-\d\d-\d\d", "Fed. Reg.", lambda y: str(int(y) - 1935)),
             # Pa. Code pacodeandbulletin PDFs keyed <title>_<chapter>[a-z][_N] zero-padded (031_0089 = 31 Pa. Code ch. 89; 007_0003a = 7 Pa. Code ch. 3a;
             # `_1` suffix = duplicate ingest twin): 826 keys, titles 7/10/31/34/37/40/201/204/207/210. `title` de-zeroed to match the pa_code_t<T> rows.
             (r"^(\d{3})_\d{4}[a-z]?(?:_\d+)?$", "Pa. Code", lambda t: str(int(t))),
             # Pennsylvania Bulletin issue slices: <vol>-<issue>[_pN] / <vol>_<issue>_pN (1,988 keys / 905k chunks — NOT CFR title-part splits: sampled text is
             # DEP permit notices / Pa.B. address lists) and <vol>_<issue>_cts (1,252 keys). `title` = Pa.B. volume.
             (r"^(\d{1,2})[-_]\d{1,4}(?:_p\d+)?$", "Pa. Bull."), (r"^(\d{2})_\d{2}_cts$", "Pa. Bull.")]


def rule_match(src):
    """-> (family, title) for a source slug via RULE_PATS (first match wins), or None. Shared by build() and build_rules()."""
    for rx, fam, *fn in RULE_PATS:
        m = re.match(rx, src)
        if m:
            t = m.group(1)
            return fam, (fn[0](t) if fn else t)
    return None

def reg():
    r = sqlite3.connect(REG_DB); r.execute("PRAGMA journal_mode=WAL")
    r.execute("CREATE TABLE IF NOT EXISTS cite_registry(reporter TEXT, vol INTEGER, page INTEGER, source TEXT, caption TEXT, court_code TEXT, year TEXT, provenance TEXT, UNIQUE(reporter, vol, page, source))")
    r.execute("CREATE INDEX IF NOT EXISTS ix_reg_cite ON cite_registry(reporter, vol, page)")
    r.execute("CREATE TABLE IF NOT EXISTS rule_registry(family TEXT, title TEXT, source TEXT, UNIQUE(family, title, source))")
    r.execute("CREATE TABLE IF NOT EXISTS build_state(k TEXT PRIMARY KEY, v TEXT)")
    return r

def canon_rep(s):
    k = re.sub(r"[^a-z0-9]", "", s.lower().replace("f-supp", "fsupp").replace("supp", "supp"))
    return REP_CANON.get(k, "")

def parse_slug(src):
    for rx, gr, gv, gp in SLUG_PATS:
        m = re.search(rx, src)
        if m:
            rep = canon_rep(m.group(gr))
            if rep: return rep, int(m.group(gv)), int(m.group(gp)), "slug"
    return None

def parse_cite_str(s):
    m = re.match(r"\s*(\d+)\s+(U\.S\.|S\. ?Ct\.|F\.\s?(?:2d|3d|4th)|F\. ?Supp\.(?: [23]d)?|A\.\s?[23]d|Pa\.(?: Super\.| Cmwlth\.)?|N\.J\.(?: Super\.| Eq\.)?|L\. Ed\. 2d|F\. App'x)\s+(\d+)", s or "")
    if not m: return None
    rep = re.sub(r"\s+", " ", m.group(2)).replace("F.Supp.", "F. Supp.").replace("S.Ct.", "S. Ct.")
    return rep, int(m.group(1)), int(m.group(3))

REP_COURT = {"U.S.": "SCOTUS", "S. Ct.": "SCOTUS", "L. Ed. 2d": "SCOTUS", "Pa.": "PA_SUPREME", "Pa. Super.": "PA_SUPER", "Pa. Cmwlth.": "PA_CMWLTH",
             "N.J.": "NJ_SUPREME", "N.J. Super.": "NJ_SUPER", "N.J. Eq.": "NJ_EQ"}
def slug_caption(src):
    """cap_us_502_16_zatko_v_california → 'Zatko v. California' (the slug IS the caption for CAP sources)."""
    m = re.match(r"^cap_[a-z]+?_\d+_\d+_(.+)$", src)
    if not m: return ""
    return re.sub(r"\b(\w)", lambda k: k.group(1).upper(), m.group(1).replace("_v_", " v. ").replace("_", " ")).replace(" V. ", " v. ")

def head_meta(con, src, rep=""):
    """caption / court / year. Deterministic first (slug caption; reporter → court), head text only where the reporter does not fix the court
    (F.3d / F. Supp.: which circuit/district). Year only when it sits next to 'decided'/'argued'/'filed' — else blank, never a guess."""
    rows = con.execute("SELECT chunk_id, text, citations FROM chunk_text WHERE source=? ORDER BY chunk_id LIMIT 12", (src,)).fetchall()
    cit = (rows[0][2] or "") if rows else ""
    txt = " ".join(r[1] or "" for r in rows[:2])[:3000] if rows else ""; low = txt.lower()
    court = REP_COURT.get(rep, "")
    if not court:   # circuit reporters: the court header may sit in any early chunk; a circuit match outranks a 'district court' mention
        low12 = " ".join(r[1] or "" for r in rows).lower()
        hits = [c for rx, c in COURT_RX if re.search(rx, low12)]
        court = next((c for c in hits if c.startswith(("CA", "OTHER_CIRCUIT"))), hits[0] if hits else "")
        if rep in ("F.3d", "F.2d", "F.4th", "F. App'x") and court in ("OTHER_DISTRICT", "ED_PA", ""): court = "OTHER_CIRCUIT" if court else ""
        elif rep in ("F. Supp.", "F. Supp. 2d", "F. Supp. 3d") and court.startswith(("CA", "OTHER_CIRCUIT")): court = next((c for c in hits if "DISTRICT" in c or c == "ED_PA"), "OTHER_DISTRICT")
    cap = slug_caption(src)
    if not cap and ";" in cit: cap = cit.split(";", 1)[1].strip()[:120]
    if not cap:
        pre = re.split(r"\b(?:See|Id\.|Cf\.|\d+ (?:U\.S\.|F\.\d?d|A\.\d?d) \d+)", txt[:700])[0]
        m = re.search(r"([A-Z][A-Za-z'’.&, -]{2,80}? v\. [A-Z][A-Za-z'’.&, -]{2,80})", pre)
        cap = re.sub(r"\s+", " ", m.group(1)).strip(" ,") if m else ""
    y = re.search(r"(?:decided|argued|filed|submitted)[^0-9]{0,40}(1[89]\d\d|20\d\d)", low[:2500]); year = y.group(1) if y else ""
    return cap, court, year, cit

def build(limit=0):
    con = sqlite3.connect(f"file:{CHUNK_DB}?mode=ro", uri=True); con.execute("PRAGMA busy_timeout=120000")
    r = reg(); t0 = time.time(); n_src = n_cite = n_rule = 0
    last = (r.execute("SELECT v FROM build_state WHERE k='last_source'").fetchone() or [""])[0]
    cur = con.execute("SELECT DISTINCT source FROM chunk_text WHERE source > ? ORDER BY source", (last,))
    batch = []
    for (src,) in cur:
        n_src += 1
        p = parse_slug(src); prov = "slug"
        if not p and src.startswith("docvault_pull_"):
            cap, court, year, cit = head_meta(con, src); pc = parse_cite_str(cit); court = court or REP_COURT.get(pc[0], "") if pc else court
            if pc: p = (pc[0], pc[1], pc[2], "citations-column"); batch.append((p[0], p[1], p[2], src, cap or cit.split(";")[-1].strip()[:120], court, year, p[3]))
        elif p:
            cap, court, year, _ = head_meta(con, src, p[0]) if src.startswith(("cap_", "docvault_pull_", "scotus_", "courtlistener_")) else ("", REP_COURT.get(p[0], ""), "", "")
            batch.append((p[0], p[1], p[2], src, cap, court, year, p[3]))
        rm = rule_match(src)
        if rm: r.execute("INSERT OR IGNORE INTO rule_registry VALUES(?,?,?)", (rm[0], rm[1], src)); n_rule += 1
        if len(batch) >= 500:
            r.executemany("INSERT OR IGNORE INTO cite_registry VALUES(?,?,?,?,?,?,?,?)", batch); n_cite += len(batch); batch = []
            r.execute("INSERT OR REPLACE INTO build_state VALUES('last_source', ?)", (src,)); r.commit()
            print(f"  {n_src} sources, {n_cite} cites, {n_rule} rules, {round(time.time() - t0)}s @ {src[:40]}", flush=True)
        if limit and n_src >= limit: break
    if batch: r.executemany("INSERT OR IGNORE INTO cite_registry VALUES(?,?,?,?,?,?,?,?)", batch); n_cite += len(batch)
    r.execute("INSERT OR REPLACE INTO build_state VALUES('last_source', ?)", (src if n_src else last,)); r.commit()
    tot = r.execute("SELECT count(*), count(DISTINCT reporter||vol||page) FROM cite_registry").fetchone()
    print(json.dumps({"sources_scanned": n_src, "cites_added": n_cite, "rules_added": n_rule, "registry_rows": tot[0], "distinct_cites": tot[1], "secs": round(time.time() - t0, 1)}))

def lookup(rep_str):
    p = parse_cite_str(rep_str)
    if not p: return []
    r = reg()
    rows = r.execute("SELECT reporter, vol, page, source, caption, court_code, year, provenance FROM cite_registry WHERE reporter=? AND vol=? AND page=?", p).fetchall()
    return [dict(zip(("reporter", "vol", "page", "source", "caption", "court_code", "year", "provenance"), x)) for x in rows]

def lookup_rule(family, title):
    """rule_registry sources for (family, title) — e.g. lookup_rule('C.F.R.', '12') -> every source slug holding 12 C.F.R. text."""
    return [x[0] for x in reg().execute("SELECT source FROM rule_registry WHERE family=? AND title=? ORDER BY source", (family, str(title))).fetchall()]

def build_rules():
    """Rule-registry-only pass over EVERY distinct source (the covering index makes this ~2 s; no head_meta reads). Needed because
    build() is incremental on `last_source`, so sources scanned before a RULE_PATS extension never get re-visited. INSERT OR IGNORE +
    UNIQUE(family, title, source) keeps it idempotent; existing rows are never updated or deleted. Prints per-family before/after."""
    con = sqlite3.connect(f"file:{CHUNK_DB}?mode=ro", uri=True); con.execute("PRAGMA busy_timeout=120000")
    r = reg(); t0 = time.time()
    before = dict(r.execute("SELECT family, count(*) FROM rule_registry GROUP BY family").fetchall())
    rows = [(rm[0], rm[1], src) for (src,) in con.execute("SELECT DISTINCT source FROM chunk_text WHERE source IS NOT NULL AND source != ''") if (rm := rule_match(src))]
    r.executemany("INSERT OR IGNORE INTO rule_registry VALUES(?,?,?)", rows); r.commit()
    after = dict(r.execute("SELECT family, count(*) FROM rule_registry GROUP BY family").fetchall())
    out = {"matched_sources": len(rows), "before": before, "after": after, "added": {k: after[k] - before.get(k, 0) for k in after if after[k] != before.get(k, 0)}, "secs": round(time.time() - t0, 1)}
    print(json.dumps(out, indent=1)); return out

def register(rep_str, source, caption="", court_code="", year="", provenance="ingest"):
    p = parse_cite_str(rep_str)
    if not p: return False
    r = reg(); r.execute("INSERT OR IGNORE INTO cite_registry VALUES(?,?,?,?,?,?,?,?)", (p[0], p[1], p[2], source, caption, court_code, year, provenance)); r.commit(); return True

def rederive_courts(reporters=("F.3d", "F.2d", "F.4th", "F. App'x", "F. Supp.", "F. Supp. 2d", "F. Supp. 3d")):
    """re-derive court_code for rows whose reporter does not fix the court (head-order fix, 2026-11-01)."""
    con = sqlite3.connect(f"file:{CHUNK_DB}?mode=ro", uri=True); r = reg(); n = 0
    for rep, vol, page, src in r.execute("SELECT reporter, vol, page, source FROM cite_registry WHERE reporter IN (%s)" % ",".join("?" * len(reporters)), reporters).fetchall():
        _, court, _, _ = head_meta(con, src, rep)
        r.execute("UPDATE cite_registry SET court_code=? WHERE reporter=? AND vol=? AND page=? AND source=?", (court, rep, vol, page, src)); n += 1
    r.commit(); return n

# =====================================================================================================================
# G0c — RULE / SECTION granularity (S-corpus-registry-units, 2026-11-09).  rule_registry stays TITLE-level and its existing
# rows are never touched (INSERT OR IGNORE only, new families only). Units live in a NEW table:
#   rule_units(family, title, unit, source, chunk_ids, heading, n_chunks)  UNIQUE(family, title, unit, source)
# One row = one rule / section in one source, with the chunk ids that hold its heading (verbatim text) first, then chunks that
# merely mention it (cross-refs / notes), comma-joined, capped at UNIT_CHUNK_CAP. Zero LLM: regex over chunk_text.db only.
#   Pa.C.S.   pa_cs_t<T>[_ch<X>]            `§ NNNN. Heading.`   (§ is often the HTML entity &#167; in these chunks)
#   Pa.R.C.P. pa_code_t231_ch<N<8000> + pa_rcp_*   `Rule NNNN[.N]. Heading.`   (231 Pa. Code ch. 8000+ = Orphans' Court → Pa.O.C.R.)
#   Pa.R.A.P. pa_code_t210_ch<N<=41>        same heading regex (ch. 63/65/69 = court IOPs, not Pa.R.A.P.)
#   Pa.R.E.   pa_code_t225_*                 NOT IN CORPUS as of 2026-11-09 (0 sources) — reported, not invented
#   FRCP/FRAP/FRE  frcp_rule_<N>[_M] etc.    one source per rule; unit from the slug; heading chunk `Rule N. Title.` first
#   U.S.C.    usc_t<T>_s<sec>                unit from the slug (en-dash → hyphen); chunk ids = every chunk of the source
#   C.F.R.    slugs that carry a part number  unit = 'part <p>'
#   P.S.      pa_73ps_utpcpl → 201-<n>, pa_40ps_uipa → 1171.<n>, pa_73ps_fceua → 2270.<n>   (`Section n. Title.--` + TOC map)
UNIT_CHUNK_CAP = 12
RX_SEC = re.compile(r"(?:§|&#167;)\s*(\d{1,5}(?:\.\d+)?)\.\s{1,3}([A-Z\"“][^.\n]{2,120}?)\.(?=\s|$)")
RX_RULE = re.compile(r"\bRule\s+(\d{1,4}(?:\.\d+)?)\.\s+([A-Z\[][^.\n]{2,100}?)\.(?=\s|$)")
RX_PS_HEAD = re.compile(r"Section\s+(\d+(?:\.\d+)?)\.\s{1,3}([A-Z][^.]{2,140}?)\.--")            # headings may wrap a line (`;\n`)
RX_PS_TOC = re.compile(r"^\s*Section\s+(\d+(?:\.\d+)?)\.\s+([A-Z][^.]{2,140}?)\.\s*$", re.M)
RX_PS_START = re.compile(r"^\s*([A-Z][^.]{2,140}?)\.--")
_ws = lambda h: re.sub(r"\s+", " ", h).strip()
HEAD_SKIP = ("Source", "Official Note", "See ", "The provisions", "Explanatory", "Note", "Comment")
PS_UNIT_TPL = {"pa_73ps_utpcpl": "201-{n}", "pa_40ps_uipa": "1171.{n}", "pa_73ps_fceua": "2270.{n}"}
CFR_PART_PATS = [r"^cfr_(\d+)_(\d+[a-z]?)_", r"^courtlistener_cfr_(\d+)_part_(\w+)$", r"^doi_btfa_(\d+)cfr(\d+)", r"(?i)^(\d+)cfr(\d+)", r"(?i)^title[ivx]+-(\d+)cfr-pt(\d+)"]

def _units_table(r):
    r.execute("CREATE TABLE IF NOT EXISTS rule_units(family TEXT, title TEXT, unit TEXT, source TEXT, chunk_ids TEXT, heading TEXT, n_chunks INTEGER, UNIQUE(family, title, unit, source))")
    r.execute("CREATE INDEX IF NOT EXISTS ix_units_fam ON rule_units(family, title, unit)")

def _norm_unit(u):
    return u.replace("–", "-").replace("_", ".")

def _scan_headings(con, source, rx, mention_tpl=None, cap=UNIT_CHUNK_CAP):
    """-> {unit: (heading, [heading chunk ids] + [mention chunk ids])} for one source. Heading chunks first (sorted), then chunks that
    mention the unit (regex mention_tpl.format(u=unit)) — so a rule whose heading sits in one chunk still carries its notes/cross-refs."""
    rows = con.execute("SELECT chunk_id, text FROM chunk_text WHERE source=?", (source,)).fetchall()
    heads, texts = {}, {}
    for cid, t in rows:
        t = t or ""; texts[cid] = t
        for m in rx.finditer(t):
            h = m.group(2).strip()
            if h.startswith(HEAD_SKIP) or h.startswith("("): continue
            u = m.group(1); e = heads.setdefault(u, [h, set()]); e[1].add(cid)   # first heading seen is kept
    out = {}
    for u, (h, cids) in heads.items():
        ids = sorted(cids)
        if mention_tpl:
            mrx = re.compile(mention_tpl.format(u=re.escape(u)))
            ids += sorted(c for c, t in texts.items() if c not in cids and mrx.search(t))
        out[u] = (h, ids[:cap])
    return out

def _ps_units(con, source, tpl):
    rows = con.execute("SELECT chunk_id, text FROM chunk_text WHERE source=?", (source,)).fetchall()
    toc, toc_chunks = {}, set()
    for cid, t in rows:
        for m in RX_PS_TOC.finditer(t or ""): toc.setdefault(_ws(m.group(2)).lower(), m.group(1)); toc_chunks.add(cid)
    out = {}
    for cid, t in rows:
        t = t or ""
        for m in RX_PS_HEAD.finditer(t):
            e = out.setdefault(m.group(1), [_ws(m.group(2)), []]); e[1].append(cid)
        ms = RX_PS_START.match(t)                                     # heading split across a chunk boundary: chunk opens with `Title.--`
        if ms and _ws(ms.group(1)).lower() in toc:
            n = toc[_ws(ms.group(1)).lower()]; e = out.setdefault(n, [_ws(ms.group(1)), []])
            if cid not in e[1]: e[1].append(cid)
    # the table-of-contents chunk(s) trail every unit: they name the section and give the seat a 2nd id (marked toc in the hub body)
    return {tpl.format(n=n): (h, (ids + sorted(c for c in toc_chunks if c not in ids))[:UNIT_CHUNK_CAP]) for n, (h, ids) in out.items()}

def build_units(families=None, verbose=True):
    """Populate rule_units (+ rule_registry rows for the NEW families Pa.C.S. / FRCP / FRAP / FRE only). Idempotent: INSERT OR IGNORE
    on the UNIQUE key; existing rule_registry families are proven unchanged by the before/after counts it prints."""
    con = sqlite3.connect(f"file:{CHUNK_DB}?mode=ro", uri=True); con.execute("PRAGMA busy_timeout=120000")
    r = reg(); _units_table(r); t0 = time.time()
    before = dict(r.execute("SELECT family, count(*) FROM rule_registry GROUP BY family").fetchall())
    ubefore = dict(r.execute("SELECT family, count(*) FROM rule_units GROUP BY family").fetchall())
    cens = con.execute("SELECT source, count(*) FROM chunk_text WHERE source IS NOT NULL AND source != '' GROUP BY source").fetchall()
    srcs = [s for s, _ in cens]
    want = set(families) if families else None
    rows, reg_rows = [], []
    def add(fam, title, unit, src, ids, head):
        rows.append((fam, str(title), _norm_unit(unit), src, ",".join(ids), head[:160], len(ids)))
    # --- Pa.C.S. (title rows + § units)
    if not want or "Pa.C.S." in want:
        for s in srcs:
            m = re.match(r"^pa_cs_t(\d+)(?:_ch\w+)?$", s)
            if not m: continue
            reg_rows.append(("Pa.C.S.", m.group(1), s))
            for u, (h, ids) in _scan_headings(con, s, RX_SEC).items(): add("Pa.C.S.", m.group(1), u, s, ids, h)
    # --- Pa.R.C.P. / Pa.O.C.R. (231) and Pa.R.A.P. (210)
    if not want or {"Pa.R.C.P.", "Pa.R.A.P.", "Pa.O.C.R."} & want:
        for s in srcs:
            m = re.match(r"^pa_code_t(231|210)_ch(\d+)$", s); fam = t = None
            if m:
                t, ch = m.group(1), int(m.group(2))
                fam = ("Pa.O.C.R." if ch >= 8000 else "Pa.R.C.P.") if t == "231" else ("Pa.R.A.P." if ch <= 41 else None)
            elif s.startswith("pa_rcp_"): t, fam = "231", "Pa.R.C.P."
            if not fam: continue
            for u, (h, ids) in _scan_headings(con, s, RX_RULE, mention_tpl=r"\bRule\s+{u}\b").items(): add(fam, t, u, s, ids, h)
    # --- FRCP / FRAP / FRE (one source per rule)
    if not want or {"FRCP", "FRAP", "FRE"} & want:
        for s in srcs:
            m = re.match(r"^(frcp|frap|fre)_rule_(\d+(?:_\d+)?)$", s)
            if not m: continue
            fam, u = m.group(1).upper(), _norm_unit(m.group(2))
            reg_rows.append((fam, u, s))
            rws = con.execute("SELECT chunk_id, text FROM chunk_text WHERE source=?", (s,)).fetchall()
            hrx = re.compile(r"\bRule\s+" + re.escape(u) + r"\.\s+([A-Z][^.\n]{2,220}?)\.(?=\s|$)")
            heads = [(c, hrx.search(t or "")) for c, t in rws]; heads = [(c, mm) for c, mm in heads if mm]
            ids = sorted(c for c, _ in heads) + sorted(c for c, _ in rws if c not in {h[0] for h in heads})
            add(fam, "", u, s, ids[:UNIT_CHUNK_CAP], heads[0][1].group(1) if heads else "")
    # --- U.S.C. sections from slugs (covering-index group_concat; no text read)
    if not want or "U.S.C." in want:
        q = con.execute("SELECT source, group_concat(chunk_id) FROM chunk_text WHERE source >= 'usc_t' AND source < 'usc_u' GROUP BY source")
        for s, ids in q:
            m = re.match(r"^usc_t(\d+)_s(.+)$", s)
            if m: add("U.S.C.", m.group(1), m.group(2), s, sorted((ids or "").split(","))[:UNIT_CHUNK_CAP], "")
    # --- C.F.R. parts from slugs that carry them
    if not want or "C.F.R." in want:
        for s in srcs:
            for p in CFR_PART_PATS:
                m = re.match(p, s)
                if m:
                    ids = [c for (c,) in con.execute("SELECT chunk_id FROM chunk_text WHERE source=? ORDER BY chunk_id LIMIT ?", (s, UNIT_CHUNK_CAP))]
                    add("C.F.R.", m.group(1), f"part {m.group(2)}", s, ids, ""); break
    # --- P.S. acts with a known section→codification map
    if not want or "P.S." in want:
        for s, tpl in PS_UNIT_TPL.items():
            if s not in dict(cens): continue
            t = re.match(r"^pa_(\d+)ps_", s).group(1)
            for u, (h, ids) in _ps_units(con, s, tpl).items(): add("P.S.", t, u, s, ids, h)
    r.executemany("INSERT OR IGNORE INTO rule_registry VALUES(?,?,?)", reg_rows)
    r.executemany("INSERT OR IGNORE INTO rule_units VALUES(?,?,?,?,?,?,?)", rows); r.commit()
    after = dict(r.execute("SELECT family, count(*) FROM rule_registry GROUP BY family").fetchall())
    uafter = dict(r.execute("SELECT family, count(*) FROM rule_units GROUP BY family").fetchall())
    out = {"rule_registry": {"before": before, "after": after, "changed": {k: (before.get(k, 0), after.get(k, 0)) for k in set(before) | set(after) if before.get(k, 0) != after.get(k, 0)}},
           "rule_units": {"before": ubefore, "after": uafter, "rows_seen": len(rows)}, "secs": round(time.time() - t0, 1)}
    if verbose: print(json.dumps(out, indent=1))
    return out

def lookup_unit(family, unit, title=None):
    """rule_units rows for a rule/section — lookup_unit('Pa.R.C.P.', '1028') / lookup_unit('Pa.C.S.', '8371', '42') / lookup_unit('U.S.C.', '1332', '28')."""
    r = reg(); _units_table(r)
    q, args = "SELECT family, title, unit, source, chunk_ids, heading, n_chunks FROM rule_units WHERE family=? AND unit=?", [family, _norm_unit(str(unit))]
    if title is not None: q += " AND title=?"; args.append(str(title))
    return [dict(zip(("family", "title", "unit", "source", "chunk_ids", "heading", "n_chunks"), x)) | {"chunk_ids": x[4].split(",") if x[4] else []} for x in r.execute(q + " ORDER BY source", args).fetchall()]


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--build", action="store_true"); ap.add_argument("--limit", type=int, default=0); ap.add_argument("--lookup"); ap.add_argument("--rederive-courts", action="store_true")
    ap.add_argument("--build-units", action="store_true", help="G0c: rule_units (rule/section granularity) + Pa.C.S./FRCP/FRAP/FRE title rows; idempotent")
    ap.add_argument("--lookup-unit", nargs="+", metavar="X", help="FAMILY UNIT [TITLE]  e.g. --lookup-unit Pa.R.C.P. 1028 | --lookup-unit Pa.C.S. 8371 42")
    ap.add_argument("--build-rules", action="store_true", help="rule_registry-only full pass (idempotent; backfills families added to RULE_PATS)")
    ap.add_argument("--lookup-rule", nargs=2, metavar=("FAMILY", "TITLE"), help="e.g. --lookup-rule C.F.R. 12")
    a = ap.parse_args()
    if a.build: build(a.limit)
    if a.build_rules: build_rules()
    if a.build_units: build_units()
    if a.lookup_unit: print(json.dumps(lookup_unit(*a.lookup_unit), indent=1))
    if a.lookup: print(json.dumps(lookup(a.lookup), indent=1))
    if a.lookup_rule: print(json.dumps(lookup_rule(*a.lookup_rule), indent=1))
    if a.rederive_courts: print("rederived", rederive_courts())
