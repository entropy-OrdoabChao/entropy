#!/usr/bin/env python3
"""fetch_bot.py — THE corpus retrieval specialist (S-fetch-bot, 2026-10-24).  Torch-free; daemon + read-only sqlite.

The user's room design: few bots, small context, ONE retrieval bot that owns every corpus pull so "NOT IN CORPUS" stops
being a lane's guess and becomes this bot's verified finding (or its pull).  Two faces: (1) this deterministic tool,
(2) the Hermes profile asp-fetch whose skill runs `fetch_bot.py resolve` and posts the verbatim excerpt.

  resolve(request) -> {status: FOUND | PULLED | PULLED_STAGED | NOT_FOUND, hits:[{chunk_id, source, family, text, score}],
                       cite, kind, families, verified_quote?, escalation:[{step, name, query, n, ...}], ingest_manifest?, commands?}

ESCALATION LADDER (every step logged in `escalation`, in order; a step that FINDS stops the ladder):
  1 pins      exact source-key pins: analysis_runner._exact_statute / _exact_rule_or_reporter / _pull_cites_tv (the m2 pin path)
              + a PA statute section-marker check (pa_cs_t42 chunk carrying "42c5505s") so the operative section ranks first.
  2 semantic  analysis_runner._retrieve over the cite's natural families (statute -> its code family, case -> reporter family,
              question -> ALL registered families, k=12); cite hits must pass analysis_runner._pins (party/section in slug or head).
  3 fts       phrase search: daemon fts_phrase (RC) first, then sqlite chunk_text_fts MATCH the way card_gate._fts does it;
              phrases = the distinctive words / party names / "vol rep page".
  4 pull      case cite only, USER-LOCKED ORDER: Harvard CAP static bulk (given cite) -> CAP (parallel cites) -> Wikisource ->
              CourtListener v4 (auth, exact-citation query) LAST — pipeline/case_fetch.py's own rungs, each logged, each result
              NAME-GUARDED (corpus_gap_bot.name_guard: lead party in the caption, else REJECTED).  Text is STAGED under corpus_sources/ with the skill's naming
              (cap_us_<vol>_<page>_<slug> / scotus_<slug>_<cite>), chunked with case_fetch._chunks, FTS-verified in the staged text,
              manifest written.  Rows are written to chunk_text + chunk_text_fts ONLY with --write-db (the skill's ingest_case
              pattern) -> PULLED; default -> PULLED_STAGED + the commands the parent runs.
              statute / rule cite: no network pull here — the family census + the lane command (pipeline.ingest_pa_rcp_chapter ...).
  5 not_found only after 1-4, carrying every query used.

CLI:  fetch_bot.py resolve '<cite or question>' [--json] [--write-db] [--k N]
      fetch_bot.py pack --units ADVERSE_POINTS.md --lanes a,b,c --facts CASE_FACTS.md --out pack.md [--budget 24576]
      fetch_bot.py commit <manifest.json>      (writes the staged chunks of a PULLED_STAGED manifest into both tables)

Pack functions unit_queries / unit_families / pack_excerpts / build_unit_pack / _retrieve_batched are adapted WITH ATTRIBUTION from
<home>/_sandbox_m2_unit_pack/pipeline/m2/analysis_runner.py (S-m2-unit-pack).  Measured defect there: ONE union pull
ranked across 34 families let amjur/usreports crowd out small shelves.  Fix here: one pull PER FAMILY-SET (per lane), then a
round-robin merge so every family set keeps its best hit before any set gets its second (tests/regress_fetch_bot.py asserts it).
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import os, re, sys, json, time, sqlite3, hashlib, zipfile, io, urllib.request, urllib.parse
_WS = re.compile(r"\s+")   # hoisted: a backslash inside an f-string expression is a SyntaxError on Python 3.11 (the MCP server venv)
from pathlib import Path

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")          # never load torch here (analysis_runner is a daemon client)
ROOT = Path(os.environ.get("M2_ROOT", str(Path(__file__).resolve().parents[2])))
REPO = _eroot()
for p in (str(ROOT), str(REPO)):
    if p not in sys.path: sys.path.insert(0, p)
from pipeline.m2 import analysis_runner as AR          # noqa: E402  (torch-free; RC socket client inside)
from pipeline import retrieval_client as RC             # noqa: E402

TV_DB = Path(os.environ.get("TURBOVEC_DIR", str(_eroot() / "corpus"))) / "chunk_text.db"
STAGE_DIR = Path(os.environ.get("FETCH_STAGE_DIR", str(REPO / "corpus_sources" / "fetch_bot")))
MANIFEST_DIR = Path(os.environ.get("FETCH_MANIFEST_DIR", str(REPO / "output" / "plaw_backfill")))
CL_BASE = os.environ.get("COURTLISTENER_BASE", "https://www.courtlistener.com/api/rest/v4")
CAP_BASE = os.environ.get("CAP_STATIC_BASE", "https://static.case.law")
UA = "beezle-fetch-bot/1.0"
K_DEFAULT = 12
PACK_BUDGET = 24 * 1024
WINDOW = 1200
HTTP_GET = None          # test seam: callable(url, headers) -> bytes|str ; None = real urllib
_SLEEP = float(os.environ.get("FETCH_CL_SLEEP_S", "2.5"))   # CourtListener rate limit (skill: ~2.5 s between calls)

INGEST_CMD = str(_eroot() / "corpus/venv/bin/python -m pipeline.turbovec_ingest --sources-from {manifest}")
RESTART_CMD = "bash scripts/turbovec_daemon.sh restart"
COMMIT_CMD = "/usr/bin/python3 pipeline/m2/fetch_bot.py commit {manifest}"

# ------------------------------------------------------------------ request classification
_REP = re.compile(r"(\d{1,4})\s+(U\.\s?S\.|S\.\s?Ct\.|F\.\s?(?:2d|3d|4th)|F\.\s?Supp\.?\s?(?:2d|3d)?|A\.\s?(?:2d|3d)|Pa\.(?:\s?(?:Super|Cmwlth|Commw)\.)?|L\.\s?Ed\.\s?(?:2d)?)\s+(\d{1,4})\b", re.I)
_PACS = re.compile(r"(\d{1,2})\s+Pa\.\s*C\.S\.(?:A\.)?\s*(?:§+\s*)?(\d+(?:\.\d+)?)", re.I)
_USC = re.compile(r"\b(\d{1,2})\s+U\.?\s?S\.?\s?C\.?(?:A\.?)?\s*(?:§+\s*)?(\d+[a-zA-Z]?(?:-\d+[a-zA-Z]?)?)\b", re.I)
# 11-13 (retriever_fts): the § and the periods are now OPTIONAL in both.  A chat/cron query is typed "13 Pa.C.S. 3301" /
# "12 USC 1735f-10" far more often than "13 Pa. C.S. § 3301"; the strict forms classified those as `question`, which sent
# the cite to an unscoped semantic pull over EVERY family — the same hole the bare-CFR form had.  The section digits are
# still mandatory, so "42 Pa.C.S. Ch. 55" and a reporter cite ("505 U.S. 833" — no C) still do not match.
_PS = re.compile(r"(\d{1,3})\s*P\.S\.\s*§+\s*(\d+(?:[.-]\d+)*(?:\.\d+)?)", re.I)   # 11-09: dash form kept — '73 P.S. § 201-2' was cut to '§ 201' → zero pins for every UTPCPL cite
_CFR = re.compile(r"\b(\d{1,2})\s+C\.?\s?F\.?\s?R\.?(?:\s*§+)?\s*(\d{1,4}\.\d+[A-Za-z]?(?:-\d+[A-Za-z]?)?|\d{1,4})\b", re.I)   # 11-13: the chat/cron forms the old
#   pattern dropped on the floor — bare "24 CFR 203.431" (no periods) and a lettered/dashed Treasury section ("26 CFR 1.6050P-1") both classified as `question`,
#   so the whole cite class fell through to an unscoped semantic pull (the retriever_fts defect: § 203.431's body is in the corpus and never surfaced).
_PARULE = re.compile(r"Pa\.\s*R\.\s*(C(?:iv)?|A|E|J\.?A|Crim)\.\s*P\.\s*(\d{1,4})(?:\.\d+)?((?:\s*\([a-z0-9]+\))*)", re.I)
_FEDRULE = re.compile(r"Fed\.\s*R\.\s*(Civ|App|Evid|Crim|Bankr)\.?\s*(?:P\.?)?\s*(\d{1,4})", re.I)
_AMJUR = re.compile(r"(\d{1,3}[A-Z]?)\s+Am\.?\s?Jur\.?\s?2d\s*(?:[A-Za-z &,'-]+?)?\s*§+\s*(\d+)", re.I)
_CASE = AR._CASE
_REP_FAM = {"us": "usreports", "sct": "usreports", "led": "usreports", "f2d": "ca3_", "f3d": "ca3_", "f4th": "ca3_", "fsupp": "fsupp",
            "a2d": "pa_cases", "a3d": "pa_cases", "pa": "pa_cases", "pasuper": "pa_cases", "pacmwlth": "pa_cases", "pacommw": "pa_cases"}
# raw source prefixes for families FAMILIES.yaml does not register (case shelves) — data, not code
_RAW_PREFIX = {"pa_cases": ["scotus_", "pa_super_", "pa_cmwlth_", "docvault_pull_a2d", "docvault_pull_a3d", "docvault_pull_pa_", "cap_pa_", "cap_a2d_", "cap_a3d_"],
               "ca3_": ["ca3_", "docvault_pull_f2d", "docvault_pull_f3d", "cap_f2d_", "cap_f3d_"],
               "fsupp": ["cap_fsupp", "docvault_pull_fsupp", "fsupp_"], "pa_73ps_": ["pa_73ps_", "pa_statutes", "ps_"], "cfr_": ["cfr_", "ecfr_"],
               "usc_": ["usc_t", "usc_", "uscode-"], "pa_cs_": ["pa_cs_t"]}
_STOP = set("the a an of to in on for and or is are was were be been by with as at from that this which who whom whose what when where why how standard rule law court courts pennsylvania federal does do did under all any".split())
_CAP_SLUG = {"us": "us", "f2d": "f2d", "f3d": "f3d", "a2d": "a2d", "a3d": "a3d", "sct": "sct", "pa": "pa", "fsupp": "f-supp", "fsupp2d": "f-supp-2d",
             "pasuper": "pa-super", "pacmwlth": "pa-commw", "pacommw": "pa-commw"}


def _rep_key(rep: str) -> str:
    return re.sub(r"[^a-z0-9]", "", rep.lower())


def _classify(request: str) -> dict:
    """-> {kind: case|statute|rule|amjur|question, cite, families:[registry names or raw-prefix names], tokens:[...], parts}."""
    r = re.sub(r"\s+", " ", request or "").strip()
    reg = AR._fam_registry()
    m = _PACS.search(r)
    if m:
        fam = f"pa_cs_{m.group(1)}_"
        return {"kind": "statute", "cite": m.group(0), "families": [fam if fam in reg else "pa_cs_"], "title": m.group(1), "section": m.group(2),
                "marker": f"{m.group(1)}c{m.group(2).replace('.', '')}s", "tokens": [m.group(2)]}
    m = _USC.search(r)
    if m:
        fam = f"usc_{m.group(1)}_"
        return {"kind": "statute", "cite": m.group(0), "families": [fam if fam in reg else "usc_"], "title": m.group(1), "section": m.group(2), "tokens": [m.group(2)]}
    m = _PS.search(r)
    if m: return {"kind": "statute", "cite": m.group(0), "families": ["pa_73ps_"] + ([f"pa_{m.group(1)}ps_"] if m.group(1) != "73" else []),   # 11-10: the title's own P.S. shelf (pa_40ps_*) is in scope, not only 73
                  "title": m.group(1), "section": m.group(2), "tokens": [m.group(2)]}
    m = _CFR.search(r)
    if m: return {"kind": "statute", "cite": m.group(0), "families": ["cfr_"], "title": m.group(1), "section": m.group(2), "tokens": [m.group(2)]}
    m = _PARULE.search(r)
    if m:
        fam = {"c": "pa_rcp", "civ": "pa_rcp", "a": "pa_rap", "e": "pa_re", "ja": "pa_rja", "crim": "pa_rcrimp"}.get(m.group(1).lower().replace(".", ""), "pa_rcp")
        return {"kind": "rule", "cite": m.group(0), "families": [fam], "number": m.group(2), "subs": re.findall(r"\(([a-z0-9]+)\)", m.group(3) or ""), "tokens": [m.group(2)]}
    m = _FEDRULE.search(r)
    if m:
        fam = {"civ": "frcp", "app": "frap", "evid": "fre", "crim": "frcrmp", "bankr": "frbp"}[m.group(1).lower()]
        return {"kind": "rule", "cite": m.group(0), "families": [fam], "number": m.group(2), "tokens": [m.group(2)]}
    m = _AMJUR.search(r)
    if m: return {"kind": "amjur", "cite": m.group(0), "families": [f"amjur_{m.group(1).lower()}"], "volume": m.group(1), "section": m.group(2), "tokens": [m.group(2)],
                  "words": _distinct(r[m.end():])}
    m = _REP.search(r)
    cm = _CASE.search(r)
    if m or cm:
        parties = []
        if cm: parties = [AR._trim_leading(cm.group(1)), AR._trim_party(cm.group(2))]
        rk = _rep_key(m.group(2)) if m else ""
        fam = _REP_FAM.get(rk, "usreports") if m else "usreports"
        cite = (f"{' v. '.join(parties)}, " if parties else "") + (f"{m.group(1)} {_WS.sub('', m.group(2))} {m.group(3)}" if m else "")
        toks = AR._cite_tokens(" v. ".join(parties))[1] if parties else []
        return {"kind": "case", "cite": cite.strip(" ,"), "families": [fam], "parties": parties, "tokens": toks,
                "vol": m.group(1) if m else "", "rep": rk, "page": m.group(3) if m else ""}
    fams = [k for k, v in reg.items() if not k.startswith("_") and not (isinstance(v, dict) and v.get("absent"))]
    return {"kind": "question", "cite": r, "families": fams, "tokens": [], "words": _distinct(r)}


def classify(request: str) -> dict:
    """_classify + `words`: the request's distinctive words outside the cite itself (Explanatory Comment / traditional remedies / holding words)."""
    req = _classify(request)
    rest = re.sub(r"\s+", " ", (request or "")).replace(req.get("cite") or "", " ") if req["kind"] != "question" else ""
    req["words"] = list(dict.fromkeys((req.get("words") or []) + _distinct(rest)))
    return req


def _distinct(text: str, n: int = 6) -> list[str]:
    ws = [w for w in re.findall(r"[A-Za-z][A-Za-z'-]{2,}", text or "") if w.lower() not in _STOP]
    return list(dict.fromkeys(w for w in ws))[:n]


def _prefixes(families: list[str]) -> list[str]:
    out = []
    for f in families:
        out += AR._fam_prefixes([f]) or _RAW_PREFIX.get(f, [f])
    return list(dict.fromkeys(out))


def _family_of(source: str, families: list[str]) -> str:
    s = (source or "").lower()
    for f in families:
        if AR._fam_match(s, f) or any(s.startswith(p.lower()) for p in _RAW_PREFIX.get(f, [])):
            return f
    return "unscoped"


# ------------------------------------------------------------------ hits
def _hit(cid, source, text, fam, score=0.0, step="", part=None) -> dict:
    return {"chunk_id": cid, "source": source, "family": fam, "text": (text or "")[:1500], "score": round(float(score or 0), 3), "step": step, "part": part or "-"}


def _con():
    return sqlite3.connect(f"file:{TV_DB}?mode=ro", uri=True, timeout=120)


_SRC_INDEX: list[str] = []


def _sources(con) -> list[str]:
    """Every distinct source key (indexed GROUP BY, ~0.4 s once per process) — the deterministic slug index for party-name cites."""
    global _SRC_INDEX
    if not _SRC_INDEX:
        _SRC_INDEX = [r[0] for r in con.execute("SELECT source FROM chunk_text GROUP BY source").fetchall() if r[0]]
    return _SRC_INDEX


def _case_sources(con, parties: list[str], cap: int = 3) -> list[str]:
    """'Rancosky v. Washington Nat’l Ins. Co.' → sources whose slug reads <p1>_v_<p2…> (alnum-only compare so O’Malley ≡ o_malley)."""
    if len(parties) < 2: return []
    a = re.sub(r"[^a-z0-9]+", "_", parties[0].lower()).strip("_").split("_"); b = re.sub(r"[^a-z0-9]+", "", parties[1].lower())
    a = [w for w in a if len(w) > 2 and w not in ("the", "inc", "co", "corp", "llc", "ltd", "appellant", "appellants", "appellee", "appellees")]
    if not a or not b: return []
    rx = re.compile(rf"(^|_){re.escape(a[-1])}_v_(.*)$")
    out = []
    for s in _sources(con):
        m = rx.search(s.lower())
        if m and re.sub(r"[^a-z0-9]", "", m.group(2)).startswith(b[:5]): out.append(s)
    return sorted(out, key=lambda s: (not s.startswith("scotus_"), s))[:cap]


def _rows(con, cids: list[str]) -> list[tuple]:
    out = []
    for c in cids:
        r = con.execute("SELECT chunk_id, source, text FROM chunk_text WHERE chunk_id=?", (c,)).fetchone()
        if r: out.append(r)
    return out


# ------------------------------------------------------------------ focus: the operative words first, windowed
_VOLSCAN = re.compile(r"(USREPORTS-\d+|_PDFA$|volume_\d+)", re.I)


def _terms(req: dict) -> list[str]:
    t = [w.lower() for w in (req.get("parties") or [])] + list(req.get("tokens") or []) + [w.lower() for w in (req.get("words") or [])]
    if req.get("section"): t += [f"§ {req['section']}", req["section"]]
    if req.get("number"): t += [f"rule {req['number']}", req["number"]] + [f"({s})" for s in (req.get("subs") or [])]
    if req.get("marker"): t.append(req["marker"])
    return list(dict.fromkeys(x for x in t if x))


def _registry_keyed(source: str, vol: str, page: str) -> bool:
    """cite_registry says this source IS (vol, page) — the key the corpus carries for the case (docvault_pull_a2d_787_376 has no party in its slug)."""
    try:
        from pipeline.m2 import struct_pins as _SP
        reg = _SP._registry()
        return bool(reg and reg.execute("SELECT 1 FROM cite_registry WHERE source=? AND vol=? AND page=? LIMIT 1", (source, int(vol), int(page))).fetchone())
    except Exception:
        return False


def _focus(req: dict, hits: list[dict], con, log: list, per: int = 3) -> list[dict]:
    """Re-rank pinned hits by how many of the request's operative terms the text carries; for a VOLUME SCAN source (USREPORTS-455_PDFA:
    the pin lands on the table of cases) pull the same source's chunks where every party token co-occurs; window each text on the terms."""
    terms = _terms(req)
    if not terms or not hits: return hits[:per]
    toks = [t for t in (req.get("tokens") or []) if t]
    if req["kind"] == "case" and toks:
        for src in dict.fromkeys(h["source"] for h in hits if _VOLSCAN.search(h.get("source") or "")):
            cond = " AND ".join("instr(lower(text), ?)>0" for _ in toks)
            rows = con.execute(f"SELECT chunk_id, source, text FROM chunk_text WHERE source=? AND {cond} LIMIT 12", (src, *toks)).fetchall()
            for r in rows:
                if r[0] not in {h["chunk_id"] for h in hits}: hits.append(_hit(r[0], r[1], r[2], _family_of(r[1], req["families"]), 1.0, "pins:volume_cooccur"))
            log.append({"step": 1, "name": "pins:volume_cooccur", "query": f"{src} ∧ {toks}", "n": len(rows)})
    def score(h):
        t = (h.get("text") or "").lower()
        idx = -1 if (t.count(" . .") > 5 or t.count(" v. ") > 8) else 0                 # a table-of-cases / index page is not the opinion
        return sum(1 for x in terms if x.lower() in t) + (0.5 if any(x in t for x in toks) else 0) + idx
    if req["kind"] == "case" and toks:                                       # a reporter pin in the right VOLUME but the wrong pages is not FOUND:
        vol, page = str(req.get("vol") or ""), str(req.get("page") or "")
        keyed = {s for s in {h.get("source") or "" for h in hits}            # retrieval-100 (11-09): a source KEYED by (vol, page) — the reporter
                 if vol and page and (re.search(rf"_{vol}[a-z0-9]*?{page}$", s.lower()) or _registry_keyed(s, vol, page))}   # key in its slug or in
        def evidence(h):                                                     # cite_registry — is proven by the key; party tokens are the test for
            t = (h.get("text") or "").lower(); s = (h.get("source") or "").lower()   # volume scans / slug hits ('Angela Bruno' is not in a holding chunk)
            if (h.get("source") or "") in keyed: return True
            index_page = t.count(" . .") > 5 or t.count(" v. ") > 8
            anchor = bool(req.get("page")) and bool(re.search(rf"\b{re.escape(str(req.get('vol')))}\s+u\.?\s?s\.?\s+{re.escape(str(req.get('page')))}\b", t))
            return (toks[0] in s) or any(x in s for x in toks[1:]) or (not index_page and all(x in t for x in toks)) or (not index_page and anchor)
        before = len(hits); hits = [h for h in hits if evidence(h)]
        log.append({"step": 1, "name": "pins:party_evidence", "query": toks, "n": len(hits), "dropped": before - len(hits)})
        if not hits: return []
    try:                                                                      # 11-10: the struct_pins unit anchor ('Section 611-A.') — a P.S. cite's
        from pipeline.m2 import struct_pins as _SP                            # own number never appears in the act text, so _best_window has no
        anchor = getattr(_SP, "last_anchor", None) if hits and hits[0].get("step", "").startswith("pins:") else None   # term to score; the heading does
    except Exception:
        anchor = None
    hits.sort(key=lambda h: -(score(h) + (1.0 if anchor and anchor.search(h.get("text") or "") else 0.0)))   # the chunk carrying the heading leads
    for h in hits[:per]:
        full = h.get("text") or ""; mk = req.get("marker")
        rh = re.search(rf"(?:^|\n|\.\s+)Rule\s+{re.escape(str(req.get('number')))}\.\s+[A-Z]", full) if req["kind"] == "rule" and req.get("number") else None
        ra = anchor.search(full) if anchor else None
        if mk and mk in full:                                                 # PA statute: the window starts at the section marker itself
            i = full.index(mk); h["text"] = full[max(0, i - 80): i + WINDOW]
        elif rh:                                                              # 11-09: a rule chunk opens on ITS OWN heading ('Rule 1028. Preliminary
            h["text"] = full[rh.start(): rh.start() + WINDOW]                 # Objections'), not on the amendment-history note _best_window scored higher
        elif ra and ra.start() > 0:                                           # 11-10: unit heading mid-chunk → the window opens on the heading
            h["text"] = full[ra.start(): ra.start() + WINDOW]
        else:
            h["text"] = AR._best_window(full, terms, WINDOW) or full
        h["focus"] = [x for x in terms if x.lower() in (h.get("text") or "").lower()]
    return hits[:per]


# ------------------------------------------------------------------ ladder steps
def step_pins(req: dict, con, log: list, per: int = 3) -> list[dict]:
    """1: the m2 pin path.  Returns hits on the cite's OWN source key (wide collect, then _focus -> per); [] when the pins find nothing.
    FOLD 11-09: under M2_PATH_TO_LAW=1 the corpus-graph unit hub is consulted FIRST inside AR._exact_statute / AR._exact_rule_or_reporter
    (path_to_law.hub_rows_for_cite → the unit's own chunk ids); no hub → this rung runs byte-identically. One cite→chunk path, not two."""
    cite, kind, fams = req["cite"], req["kind"], req["families"]
    out, seen = [], set(); want, per = per, max(per * 4, 12)
    if kind == "statute" and req.get("marker"):                    # PA section marker: the operative section first
        for src in _prefixes(fams):
            for r in con.execute("SELECT chunk_id, source, text FROM chunk_text WHERE source=? AND instr(text, ?)>0 LIMIT ?", (src, req["marker"], per)).fetchall():
                if r[0] not in seen: seen.add(r[0]); out.append(_hit(r[0], r[1], r[2], _family_of(r[1], fams), 1.0, "pins:marker"))
        log.append({"step": 1, "name": "pins:marker", "query": req["marker"], "n": len(out)})
        if out: return _focus(req, out, con, log, want)
    if kind in ("statute", "rule", "case"):
        rows = AR._exact_statute(cite, "", per, con=con) if kind == "statute" else []
        try:                                                           # 11-10: which struct_pins rung keyed the hit — 'unit' (direct) or
            from pipeline.m2 import struct_pins as _SP                 # 'ps_crosswalk' (P.S. § → act section via the mined crosswalk table)
            rung = getattr(_SP, "last_rung", "") or ""
        except Exception:
            rung = ""
        for h in rows:
            cid = h.get("chunk_id")
            if cid and cid not in seen and AR._chunk_exists(con, cid):
                r = _rows(con, [cid])
                if r: seen.add(cid); out.append(_hit(cid, r[0][1], r[0][2], _family_of(r[0][1], fams), 1.0, "pins:ps_crosswalk" if rung == "ps_crosswalk" else "pins:exact_statute"))
        log.append({"step": 1, "name": "pins:_exact_statute", "query": cite, "n": len(out), **({"rung": rung} if rung else {})})
        if rung == "ps_crosswalk" and out:
            log.append({"step": 1, "name": "pins:ps_crosswalk", "query": cite, "n": len(out)})
        if out: return _focus(req, out, con, log, want)
        rr = AR._exact_rule_or_reporter(con, cite) or []
        for r in rr[:per]:
            cid, text, src = r[0], r[1], r[2]
            if cid not in seen: seen.add(cid); out.append(_hit(cid, src, text, _family_of(src, fams), 1.0, "pins:_exact_rule_or_reporter"))
        log.append({"step": 1, "name": "pins:_exact_rule_or_reporter", "query": cite, "n": len(out)})
        if out: return _focus(req, out, con, log, want)
        if kind == "case" and req.get("parties"):                 # 11-09 (retrieval-100): party-name cite with no reporter → the case's OWN
            for src in _case_sources(con, req["parties"]):         # source by slug ('rancosky_v_washington' → scotus_rancosky_v_…), holding
                rows = con.execute("SELECT chunk_id, source, text FROM chunk_text WHERE source=? LIMIT 60", (src,)).fetchall()   # chunk first
                try:
                    from pipeline.m2 import path_to_law as _PTL
                    rows = [(c, s, t) for c, t, s in _PTL.holding_rank([(c, t, s) for c, s, t in rows])]
                except Exception:
                    pass
                for r in rows[:per]:
                    if r[0] not in seen: seen.add(r[0]); out.append(_hit(r[0], r[1], r[2], _family_of(r[1], fams), 1.0, "pins:case_slug"))
            log.append({"step": 1, "name": "pins:case_slug", "query": req["parties"], "n": len(out)})
            if out: return _focus(req, out, con, log, want)
    if kind == "amjur":
        for src in _prefixes(fams):
            for r in con.execute("SELECT chunk_id, source, text FROM chunk_text WHERE source LIKE ? AND instr(text, ?)>0 LIMIT ?", (src + "%", f"§ {req['section']}", per)).fetchall():
                if r[0] not in seen: seen.add(r[0]); out.append(_hit(r[0], r[1], r[2], _family_of(r[1], fams), 1.0, "pins:amjur_section"))
        log.append({"step": 1, "name": "pins:amjur_section", "query": f"{_prefixes(fams)} § {req['section']}", "n": len(out)})
    return _focus(req, out, con, log, want) if out else []


def step_semantic(req: dict, con, log: list, k: int = K_DEFAULT) -> list[dict]:
    """2: analysis_runner._retrieve over the natural families; cite hits must pin (AR._pins)."""
    cite, kind, fams = req["cite"], req["kind"], req["families"]
    qs = [cite] if kind == "question" else [cite, f"{cite} held", f"{' '.join(req.get('parties') or req.get('tokens') or [cite])} opinion"]
    reg_fams = [f for f in fams if f in AR._fam_registry()] if kind != "question" else []
    hits = [h for h in AR._retrieve(qs, reg_fams, k=k) if not h.get("error")]
    err = next((h["error"] for h in AR._retrieve(qs, reg_fams, k=k) if h.get("error")), "") if not hits else ""
    out = []
    if kind != "question":
        kk, toks, fam = AR._cite_tokens(cite)
        for h in hits:
            if AR._pins(kk, toks, fam, h, cite=cite) and AR._chunk_exists(con, h.get("chunk_id")):
                out.append(_hit(h["chunk_id"], h["source"], h["text"], _family_of(h["source"], fams), h.get("score"), "semantic:pinned", h.get("part")))
    else:
        out = [_hit(h["chunk_id"], h["source"], h["text"], _family_of(h["source"], fams), h.get("score"), "semantic", h.get("part")) for h in hits if h.get("chunk_id")]
    log.append({"step": 2, "name": "semantic", "query": qs, "families": reg_fams or "ALL", "k": k, "raw": len(hits), "n": len(out), **({"error": err} if err else {})})
    return out


def _phrases(req: dict) -> list[str]:
    kind = req["kind"]
    if kind == "case":
        ps = []
        if req.get("parties"): ps.append(" v. ".join(req["parties"]))
        if req.get("vol"): ps.append(f"{req['vol']} {req['rep'].upper().replace('US', 'U.S.')} {req['page']}" if req["rep"] == "us" else f"{req['vol']} {req['page']}")
        return ps
    if kind == "statute": return [f"§ {req['section']}", req["section"]]
    if kind == "rule": return [f"Rule {req['number']}"]
    if kind == "amjur": return [f"§ {req['section']}"] + ([" ".join(req["words"][:4])] if req.get("words") else [])
    return [" ".join(req["words"][:4])] if req.get("words") else [req["cite"]]


def _fts_sqlite(con, phrase: str, prefixes: list[str], limit: int, pool: int = 0) -> list[tuple]:
    """card_gate._fts shape: tokens -> FTS5 phrase MATCH over chunk_text_fts(text, source_id); source filter applied on the row.
    `pool` = how many MATCH candidates to pull before the source filter (default limit*4 — the room's rung, unchanged).
    A source-key sweep needs a WIDER pool: the filter runs on the row, so a candidate list shorter than the phrase's whole
    match set can drop the cite's own shelf entirely (measured: '203 431' matches 37 chunks corpus-wide — a pool of 32 is luck)."""
    toks = re.sub(r"[^\w\s§]", " ", phrase).split()
    if not toks: return []
    try:
        ids = [r[0] for r in con.execute("SELECT source_id FROM chunk_text_fts WHERE chunk_text_fts MATCH ? LIMIT ?", ('"' + " ".join(toks) + '"', pool or limit * 4)).fetchall()]
    except Exception:
        return []
    rows = _rows(con, ids)
    if prefixes: rows = [r for r in rows if any((r[1] or "").lower().startswith(p.lower()) for p in prefixes)]
    return rows[:limit]


def _sec_rx(sec: str) -> str:
    """The section number as a dash-agnostic regex: the corpus prints 'usc_t12_s1735f–10' / '§ 1735f–10' with an
    EN DASH where the cite is typed '1735f-10' with a hyphen.  Same section — a hyphen-only match reported the
    body as ABSENT (measured on 12 U.S.C. § 1735f-10, whose own shelf was hit #1 and scored as a miss)."""
    return re.sub(r"\\?-", "[-–—]", re.escape(sec))


def _head_re(req: dict):
    """The cite's OWN heading, the way the corpus prints it: '§ 203.431 Sale of insured mortgage…' / 'Rule 1028. Preliminary
    Objections'.  A chunk carrying the heading IS the operative text; a chunk that merely repeats the number is a cross-reference."""
    sec = str(req.get("section") or "")
    if req["kind"] == "rule" and req.get("number"):
        return re.compile(rf"(?:^|\n|\.\s+)Rule\s+{re.escape(str(req['number']))}\.\s+[A-Z]")
    return re.compile(rf"§+\s*{_sec_rx(sec)}\.?\s+[A-Z(]") if sec else None
    # the trailing '\.?': the U.S. Code prints '§ 6103. Confidentiality…' while the C.F.R. prints
    # '§ 203.431 Sale…' — without it usc_t26_s6103_c0's own heading scored as a bare mention and an
    # uscode-2023-title26 bulk chunk led instead.


def _lit_rank(req: dict, hits: list[dict]) -> list[dict]:
    """Rank an FTS hit set so the LITERAL section ranks first (the retriever_fts lock): heading > bare number mention,
    the cite's own title shelf (…title24…/_t42) > another title's, a canonical shelf key > a duplicate-ingest '_1' twin,
    and a TABLE OF CONTENTS chunk ('203.430 Sale of interests… 203.431 Sale of insured mortgage…') is NOT the operative
    text — measured: the part-203 TOC chunk outranked § 203.431's own body on the first build of this step.
    Reorders only — nothing is dropped, so the receipt still describes everything swept."""
    sec = str(req.get("section") or req.get("number") or "")
    if not sec or not hits: return hits
    head = _head_re(req); title = str(req.get("title") or "")
    lit = re.compile(rf"§+\s*{_sec_rx(sec)}\b|\b{_sec_rx(sec)}\b")
    tre = re.compile(rf"(?:title|^t|_t|-t){re.escape(title)}\b") if title else None
    def sc(h):
        t = h.get("text") or ""; s = (h.get("source") or "").lower(); v = 0.0
        if (h.get("step") or "").startswith("source_key"): v += 2.0        # the section's OWN shelf key — exact, not a guess
        if head and head.search(t): v += 3.0 + (0.5 if head.search(t[:400]) else 0.0)
        elif lit.search(t): v += 1.0
        if tre and tre.search(s): v += 1.0
        if s.startswith("courtlistener_") or "compiled_rules" in s: v -= 0.4   # a court-site copy of the rule is not the
        if re.search(r"_\d+$", s): v -= 0.25                       # official code shelf; prefer pa_code_t231_* when both hit
        toc = len(re.findall(r"\b\d{2,4}\.\d+\s+[A-Z]", t)) >= 4 or t.count(" . .") > 5 \
            or len(re.findall(r"§+\s*[\d.]+[A-Za-z\-–]*\.\s+[A-Z]", t)) >= 4          # 'pa_cs_t13_ch00': a chapter's own
        if toc: v -= 1.5                                                               # section list, not § 3301's body
        return (-(v + float(h.get("score") or 0) / 10.0),)
    return sorted(hits, key=sc)


def step_fts(req: dict, con, log: list, limit: int = 8) -> list[dict]:
    """3: daemon fts_phrase first, sqlite FTS (card_gate._fts way) when the daemon returns none.
    11-13 (retriever_fts): RANK THEN TRUNCATE — a section/rule cite collects a wider candidate set, _lit_rank puts the
    operative heading chunk first, and only then is it cut to `limit`.  Cutting first returned whatever order the FTS
    index happened to hold (the part-203 table of contents), burying the section's own body.  A case/question cite has
    no section to rank, so _lit_rank is a no-op and the returned set is the same first-N it always was."""
    fams = req["families"]; prefixes = _prefixes(fams) if req["kind"] != "question" else None
    cand = max(limit * 6, 48) if (req.get("section") or req.get("number")) else limit
    out, seen = [], set()
    for ph in _phrases(req):
        resp = RC.fts_phrase(ph, prefixes, cand)
        hits = [h for h in RC.hits(resp) if h.get("chunk_id") and not h.get("error")]
        log.append({"step": 3, "name": "fts:daemon", "query": ph, "source_prefix": prefixes, "n": len(hits), "mode": resp.get("mode") if isinstance(resp, dict) else "?"})
        if not hits:
            rows = _fts_sqlite(con, ph, prefixes or [], cand)
            hits = [{"chunk_id": r[0], "source": r[1], "text": r[2]} for r in rows]
            log.append({"step": 3, "name": "fts:sqlite", "query": ph, "source_prefix": prefixes, "n": len(hits)})
        for h in hits:
            if h["chunk_id"] in seen: continue
            if req["kind"] == "case" and req.get("tokens"):                                  # name-guard: a citing case is not the case
                head = (h.get("text") or "")[:600].lower(); src = (h.get("source") or "").lower()
                if not (req["tokens"][0] in src or all(t in head for t in req["tokens"])): continue
            seen.add(h["chunk_id"]); out.append(_hit(h["chunk_id"], h.get("source"), _section_window(req, h.get("text")), _family_of(h.get("source"), fams), 0.5, "fts"))
        if out: break
    return _lit_rank(req, out)[:limit]


def _section_window(req: dict, text: str | None, win: int = WINDOW) -> str:
    """Open the excerpt ON the cite (heading first, then the literal number) — a truncation of a big CFR chunk
    otherwise cuts the operative sentence off the end of the window.  No section/number to open on (a case or a
    bare question) => the text is returned UNTOUCHED, so the room's case lane keeps its current excerpt."""
    t = text or ""
    head = _head_re(req); sec = str(req.get("section") or req.get("number") or "")
    if not sec: return t
    m = head.search(t) if head else None
    if not m: m = re.search(rf"§+\s*{_sec_rx(sec)}\b|\b{_sec_rx(sec)}\b", t)
    i = max(0, m.start() - 80) if m else 0
    return t[i: i + win]


def _key_stems(req: dict) -> list[str]:
    """The SECTION'S OWN SOURCE KEY, the way the corpus names it — derived from (title, section), never scanned:
        12 U.S.C. § 1735f-10  -> usc_t12_s1735f-10 / usc_t12_s1735f–10 (EN dash is the corpus's own form)
        26 U.S.C. § 6103      -> usc_t26_s6103                (39 chunks; the cite's own shelf)
        13 Pa.C.S. § 3301     -> pa_cs_t13…                   (910 chunks under pa_cs_t13*)
        26 C.F.R. § 1.6050P-1 -> cfr_26_16050p1 -> trimmed to cfr_26_16050p  (the key the ingest actually wrote)
    Returned as RANGE stems (`source >= stem AND source < stem+\\uffff`), which is an indexed lookup — never a
    LIKE '%…%' scan of the 60 GB table (a hard rule: that is a timeout, and a timeout is not an absence)."""
    t, sec = str(req.get("title") or ""), str(req.get("section") or "")
    fams = [f.lower() for f in (req.get("families") or [])]
    if not t or not sec: return []
    low = sec.lower(); out = []
    if any(f.startswith("usc_") for f in fams):
        for d in (low, low.replace("-", "–"), low.replace("-", "—")):
            out += [f"usc_t{t}_s{d}", f"uscode-2023-title{t}", f"uscode-2024-title{t}"]
    if any(f.startswith("pa_cs_") for f in fams):
        out += [f"pa_cs_t{t}", f"pa_cons_stat_t{t}"]
    if any(f.startswith("cfr_") for f in fams):
        norm = re.sub(r"[^a-z0-9]", "", low)
        out += [f"cfr_{t}_{norm[:n]}" for n in range(len(norm), 3, -1)][:6]      # the ingest squashes punctuation and
        out += [f"ecfr_{t}_{norm}", f"cfr-{t}-{norm}"]                            # sometimes drops the trailing part
    if any(f.endswith("ps_") for f in fams):
        out += [f"pa_{t}ps_", "pa_statutes"]
    return list(dict.fromkeys(out))


def step_source_key(req: dict, con, log: list, per: int = 6) -> list[dict]:
    """1b: the SOURCE-KEY rung — the cite's own shelf by key, with the section as the in-chunk needle.
    step_pins (AR._exact_statute) answered 0 for '26 USC 6103' and '13 Pa.C.S. 3301' even though usc_t26_s6103
    (39 chunks) and pa_cs_t13 (910) are both in the corpus, so the sweep cannot rely on it alone.
    Needles carry the DASH VARIANTS and the result is _lit_rank'ed, because a chapter shelf answers with its own
    table of contents ('§ 3301. Person entitled to enforce… § 3302. Holder in due course…') before the body."""
    sec = str(req.get("section") or "")
    base = [n for n in (f"§ {sec}", f"§{sec}", req.get("marker") or "", sec) if n]
    needles = list(dict.fromkeys([v for n in base for v in (n, n.replace("-", "–"), n.replace("-", "—"))]))
    out, seen = [], set()
    for stem in _key_stems(req):
        for needle in needles:
            try:
                rows = con.execute("SELECT chunk_id, source, text FROM chunk_text WHERE source >= ? AND source < ? AND instr(text, ?)>0 LIMIT ?",
                                   (stem, stem + "\uffff", needle, per * 4)).fetchall()
            except Exception as e:
                log.append({"step": 1, "name": "source_key:error", "query": stem, "error": str(e)[:120]}); continue
            if not rows: continue
            log.append({"step": 1, "name": "source_key", "query": f"{stem}* ∧ {needle!r}", "n": len(rows)})
            for r in rows:
                if r[0] in seen: continue
                seen.add(r[0]); out.append(_hit(r[0], r[1], _section_window(req, r[2]), _family_of(r[1], req["families"]), 1.0, "source_key"))
            break                                                   # this stem answered; the next needle would only re-find it
        if len(out) >= per: break
    if not out: log.append({"step": 1, "name": "source_key", "query": _key_stems(req)[:8], "n": 0})
    return _lit_rank(req, out)[:per]


def fts_sweep(request: str, limit: int = 8, con=None, pool: int = 240) -> dict:
    """THE reusable CORPUS SWEEP (11-13, item retriever_fts) — fetch_bot's source-key + FTS rungs as a standalone step
    so the MCP tool, the chat and every cron get the same sweep the room gets, with a RECEIPT.

    search_chunks stopped at semantic: '24 CFR 203.431 sale of insured mortgage…' returned CFR-2024-title24 only as the
    0.62 _auth_pull fallback while § 203.431's own body sat in chunk_text_fts (source CFR-2024-title24-vol2).  Here the
    cite's family -> source prefixes (_prefixes, the registry's own naming) -> the section's own source key
    (step_source_key) -> FTS5 phrase MATCH -> literal-section rank (_lit_rank).

    -> {cite, kind, families_swept, source_prefixes_swept, source_keys_swept, fts_used, sweep_complete, phrases,
        hits:[...], escalation:[...]}

    NOT IN CORPUS is a LEGAL answer ONLY when sweep_complete is true, i.e. families_swept covers the cite's whole class
    (user memory lock: 'NOT-IN-CORPUS only after ALL families').  fts_used=false / sweep_complete=false means NOT SWEPT,
    which is not an absence finding.
    """
    req = classify(request)
    own = con is None; con = con or _con()
    log: list = []
    try:
        fams = list(req.get("families") or [])
        prefixes = _prefixes(fams) if req["kind"] != "question" else []
        phrases = _phrases(req)
        cand = max(limit * 6, 48)                           # RANK THEN TRUNCATE: cutting to `limit` in FTS-index order
        hits, seen = [], set()                              # returned the part-203 TOC and buried § 203.431's own body
        for h in step_source_key(req, con, log, per=limit):               # 1b: the section's OWN shelf, by key
            if h["chunk_id"] not in seen: seen.add(h["chunk_id"]); hits.append(h)
        for ph in phrases:                                  # EVERY phrase, EVERY family — step_fts stops at the first phrase that
            rows = _fts_sqlite(con, ph, prefixes, cand, pool=pool)    # yields rows, which is right for a lane but would make the
            for r in rows:                                            # receipt promise coverage the sweep never ran
                if r[0] in seen: continue
                seen.add(r[0]); hits.append(_hit(r[0], r[1], _section_window(req, r[2]), _family_of(r[1], fams), 0.5, "fts:source_key"))
            log.append({"step": 3, "name": "fts:sweep", "query": ph, "source_prefix": prefixes[:8], "pool": pool, "n": len(rows)})
        for h in step_fts(req, con, log, limit=limit):       # the room's own rung (daemon first) — merged, never replaced
            if h["chunk_id"] not in seen: seen.add(h["chunk_id"]); hits.append(h)
        hits = _lit_rank(req, hits)[:limit]
        g = pin_gate(req, hits, con)                          # 11-13 (prefetch_pin): WHAT the sweep's best hit actually is
        return {"cite": req["cite"], "kind": req["kind"], "phrases": phrases, "hits": hits, "escalation": log,
                "source_keys_swept": _key_stems(req)[:12], "pin_detail": (g.get("detail") or [])[:8],
                "pull_needed": bool(g.get("pull_needed")), "pull_target": g.get("pull_target") or "",
                **_receipt(req, log, pin_kind=g.get("pin_kind") or "none")}
    finally:
        if own: con.close()


def _receipt(req: dict, log: list, pin_kind: str = "none") -> dict:
    """The SWEEP RECEIPT — what was actually swept, so a NOT-IN-CORPUS claim is auditable instead of asserted.
    families_swept = the cite class's families the pin/FTS source-key steps ran over; fts_used = the phrase sweep ran;
    sweep_complete = a real cite + an executed FTS sweep over that cite's whole family class.
    11-13 (prefetch_pin): + pin_kind = exact_header | mention-only | none — WHAT the pinned chunk actually is.  A receipt that
    says FOUND without saying which of the three it pinned is how a cross-reference ('…the notice required by § 203.431…')
    shipped as the operative text of § 203.431 in the closure run."""
    fts_used = any(e.get("step") == 3 for e in log)
    swept = fts_used or any(e.get("step") == 1 for e in log)
    fams = list(req.get("families") or []) if swept else []
    pres = _prefixes(fams) if (fams and req["kind"] != "question") else []
    return {"families_swept": fams, "source_prefixes_swept": pres[:24], "fts_used": fts_used,
            "sweep_complete": bool(fts_used and fams and req["kind"] != "question"),
            "pin_kind": pin_kind,
            "pin_rule": "a GROUND+ quote requires pin_kind=exact_header (the cite's own section heading + body); "
                        "mention-only = a cross-reference, which is NOT a resolution",
            "not_in_corpus_rule": "NOT IN CORPUS is legal only when sweep_complete=true (families_swept covers the cite class)"}


# ------------------------------------------------------------------ THE PIN GATE (S-prefetch-pin, 2026-11-13)
# Closure run planet_preservice_closure_20261113_200843: chunk e2f5a7004637a35d79f63e23434f644f was written to ground_pack.md
# as `GROUND+: 24 C.F.R. § 203.431` though its only words about 203.431 are '…until the notice required by § 203.431 is
# received by HUD' — the tail of § 203.432 plus the head of § 203.433.  Every lane then quoted that cross-reference AS the
# section.  A hit is a PIN only when it carries the cite's OWN HEADING with body behind it (pipeline/cite_family.py).
try:
    from pipeline import cite_family as CFAM            # deterministic; no torch, no network, no db
except Exception:                                      # pragma: no cover - a missing module must not break the ladder
    CFAM = None


def _full_text(con, chunk_id: str) -> str:
    """The FULL chunk text.  _hit truncates to 1500 chars and the rungs window before that, but a section heading can sit
    past the cut — measured: § 203.431's heading is at offset 3534 of CFR-2024-title24-vol2's chunk ce0979911199."""
    try:
        r = con.execute("SELECT text FROM chunk_text WHERE chunk_id=?", (chunk_id,)).fetchone()
        return r[0] if r and r[0] else ""
    except Exception:
        return ""


def pin_gate(req: dict, hits: list, con) -> dict:
    """-> cite_family.pick_pin + {gated}: {pin_kind: exact_header|mention-only|none, pinned, pull_needed, pull_target, detail}.
    Only a SECTION/RULE cite is gated (it has a heading to match); a case/question cite keeps the party/volume guard it had."""
    if not CFAM or req.get("kind") not in ("statute", "rule") or not (req.get("section") or req.get("number")):
        return {"pin_kind": "none", "pinned": None, "pull_needed": False, "pull_target": "", "detail": [], "gated": False}
    g = CFAM.pick_pin(req.get("cite") or "", hits or [],
                      text_of=lambda h: _full_text(con, h.get("chunk_id") or "") or (h.get("text") or ""), win=WINDOW)
    g["gated"] = True
    return g



# ------------------------------------------------------------------ step 4: pull (case) / statute lane report
# USER-LOCKED ORDER (twice): Harvard CAP static bulk FIRST (given cite -> parallel cites) -> Wikisource -> CourtListener (auth) LAST.
# The rungs are pipeline/case_fetch.py's own functions (from_cap / _parallel_cites / from_wikisource / from_courtlistener — the same
# ladder fetch_opinion() runs and corpus_gap_bot.fill_gap reuses); fetch_bot only LOGS each rung and NAME-GUARDS the result.
def _cf():
    from pipeline import case_fetch
    return case_fetch


def _name_guard(requested: str, text: str, name: str) -> tuple[bool, str]:
    """corpus_gap_bot.name_guard when importable (lead-party-in-caption rule); else the same rule locally."""
    try:
        from pipeline.corpus_gap_bot import name_guard
        return name_guard(requested, text, name)
    except Exception:
        pass
    lead = next((w.lower().strip(".,'") for w in re.split(r"\s+v\.?\s+", requested)[0].split() if len(w) > 3 and w.lower() not in _STOP), "")
    head = ((name or "") + " " + (text or "")[:1200]).lower()
    cap = re.split(r"\b(?:supreme court|court of appeals|argued|decided|submitted|filed|opinion by|no\.\s*\d)\b", head, maxsplit=1)[0]
    return (bool(lead) and lead in cap), (f"lead {lead!r} in caption" if lead and lead in cap else f"lead {lead!r} absent from caption")


def _slug(n: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", (n or "").lower())).strip("_")[:60]


def pull_rungs(req: dict, log: list) -> tuple[str, str, str, str]:
    """-> (text, resolved_name, provider, origin) or ('', '', '', '').  Every rung logged: cap:given, cap:parallel:<cite>, wikisource,
    courtlistener (query named).  A rung's text must pass the name guard or it is REJECTED and the ladder continues."""
    CF = _cf()
    name = " v. ".join(req.get("parties") or []) or req["cite"]
    rep_txt = {"us": "U.S.", "f2d": "F.2d", "f3d": "F.3d", "a2d": "A.2d", "a3d": "A.3d", "sct": "S. Ct.", "pa": "Pa."}.get(req.get("rep"), req.get("rep") or "")
    cite = f"{req.get('vol')} {rep_txt} {req.get('page')}".strip() if req.get("vol") else ""
    parsed = CF.parse_cite(cite) if cite else None

    def accept(rung, txt, nm, origin):
        if not txt:
            log.append({"step": 4, "name": rung, "query": origin, "n": 0}); return None
        ok, why = _name_guard(name, txt, nm or "")
        log.append({"step": 4, "name": rung, "query": origin, "n": 1, "chars": len(txt), "case_name": nm, "name_guard": why, **({} if ok else {"rejected": True})})
        return (txt, nm or name, rung, origin) if ok else None

    if parsed:
        r = accept("cap:given", *CF.from_cap(*parsed), f"{CAP_BASE}/{parsed[1]}/{parsed[0]}.zip#first_page={parsed[2]}")
        if r: return r
        alts = CF._parallel_cites(name, cite)
        log.append({"step": 4, "name": "cap:parallel_cites", "query": f'"{cite}"', "n": len(alts), "cites": alts})
        for alt in alts:
            p2 = CF.parse_cite(alt)
            if not p2 or p2 == parsed: continue
            r = accept(f"cap:parallel:{alt}", *CF.from_cap(*p2), f"{CAP_BASE}/{p2[1]}/{p2[0]}.zip#first_page={p2[2]}")
            if r: return r
    else:
        log.append({"step": 4, "name": "cap:given", "query": cite or None, "n": 0, "note": "no parseable reporter cite — CAP skipped"})
    r = accept("wikisource", *CF.from_wikisource(name), f"https://en.wikisource.org/wiki/{urllib.parse.quote(name.replace(' ', '_'))}")
    if r: return r
    q = f'"{cite}"' if cite else f'"{name}"'
    r = accept("courtlistener", *CF.from_courtlistener(name, cite), f"{CL_BASE}/search/?type=o&q={q}")
    if r: return r
    return "", "", "", ""


def _chunks(text: str) -> list[str]:
    try:
        CF = _cf()
        cs = CF._chunks(CF._clean(text))
        if cs: return cs
    except Exception:
        pass
    out, buf = [], ""                                                                         # fallback: ~1400-char sentence-boundary split
    for para in re.split(r"\n\s*\n", text):
        buf = (buf + "\n\n" + para).strip()
        while len(buf) > 1400:
            cut = buf.rfind(". ", 900, 1400); cut = cut + 1 if cut > 0 else 1400
            out.append(buf[:cut].strip()); buf = buf[cut:].strip()
    if buf: (out.append(buf) if not out or len(buf) > 60 else out.__setitem__(-1, out[-1] + " " + buf))
    return out


def step_pull(req: dict, log: list, write_db: bool = False) -> dict:
    """4 (case): CAP(given) -> CAP(parallel) -> Wikisource -> CourtListener; name-guarded; stage + manifest (+ rows with write_db)."""
    text, name, provider, origin = pull_rungs(req, log)
    if not text:
        queries = [e.get("query") for e in log if e.get("step") == 4 and e.get("query")]
        return {"status": "NOT_FOUND", "pull": {"rungs": [e["name"] for e in log if e.get("step") == 4], "queries": queries,
                                                 "rejected": [e for e in log if e.get("step") == 4 and e.get("rejected")]}}
    vol, rep, page = req.get("vol") or "", req.get("rep") or "", req.get("page") or ""
    cite_str = f"{vol} {rep.upper().replace('US', 'U.S.')} {page}".strip()
    quality = "CAP born-digital (Harvard), not OCR" if provider.startswith("cap") else f"{provider} text"
    src = f"cap_us_{vol}_{page}_{_slug(name)}" if rep == "us" and provider.startswith("cap") else f"scotus_{_slug(name)}_{re.sub(r'[^a-z0-9]+', '', cite_str.lower())}"
    sub = "us_reports" if rep == "us" else ("pennsylvania_cases" if rep in ("a2d", "a3d", "pa") else "caselaw")
    sdir = STAGE_DIR / sub; sdir.mkdir(parents=True, exist_ok=True)
    raw = sdir / f"{src}.txt"; raw.write_text(text, encoding="utf-8")
    cs = _chunks(text)
    assert cs and max(len(c) for c in cs) <= 4000, "chunker produced an oversized chunk"                # skill: assert on the OUTPUT
    probe_words = [t for t in (req.get("tokens") or []) if t]
    probes = {w: sum(1 for c in cs if w in c.lower()) for w in probe_words}
    verified = next((c for c in cs if probe_words and all(w in c.lower() for w in probe_words[:1])), cs[0])
    man = {src: {"name": name, "cite": cite_str, "court": "", "decided": "", "source": src, "chunks": len(cs), "chars": len(text), "origin": origin, "provider": provider,
                 "raw": str(raw), "jurisdiction": "US" if rep == "us" else "pa_state", "pulled": time.strftime("%Y-%m-%d %H:%M"), "quality": quality,
                 "probes": probes, "db_rows": 0, "fts_rows_matching_lead": 0, "entity_or_pdf_garbage": sum(1 for c in cs if "endobj" in c or "%PDF" in c),
                 "rungs": [e["name"] for e in log if e.get("step") == 4], "staged_by": "fetch_bot"}}
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    mpath = MANIFEST_DIR / f"fetch_bot_{src}_{time.strftime('%Y%m%d_%H%M%S')}_manifest.json"
    mpath.write_text(json.dumps(man, indent=1), encoding="utf-8")
    log.append({"step": 4, "name": "stage", "source": src, "raw": str(raw), "chunks": len(cs), "manifest": str(mpath), "provider": provider})
    res = {"ingest_manifest": str(mpath), "source": src, "raw": str(raw), "chunks": len(cs), "provider": provider, "verified_quote": verified[:600],
           "hits": [_hit(f"{src}_chunk_{i}", src, c, req["families"][0], 1.0, "pull:staged") for i, c in enumerate(cs[:3])]}
    if write_db:
        n = commit_manifest(mpath, log)
        return {"status": "PULLED", **res, "db_rows": n, "commands": [INGEST_CMD.format(manifest=mpath), RESTART_CMD]}
    return {"status": "PULLED_STAGED", **res, "commands": [COMMIT_CMD.format(manifest=mpath), INGEST_CMD.format(manifest=mpath), RESTART_CMD]}


def commit_manifest(mpath, log: list | None = None, db: Path | None = None) -> int:
    """Write a staged manifest's chunks into chunk_text AND chunk_text_fts (case_fetch.ingest_case pattern: both tables, idempotent)."""
    db = db or TV_DB
    man = json.loads(Path(mpath).read_text(encoding="utf-8")); total = 0
    con = sqlite3.connect(str(db), timeout=120); con.execute("PRAGMA busy_timeout=120000")
    for src, e in man.items():
        cs = _chunks(Path(e["raw"]).read_text(encoding="utf-8"))
        # Idempotency guard (case_fetch.ingest_case pattern): only a source ALREADY in chunk_text needs the re-commit DELETEs.
        # chunk_text_fts.source_id is UNINDEXED, so the LIKE delete is a full scan of the FTS table (>300 s on the 60 GB db);
        # a fresh source (the normal pull) must never pay it.  Measured 2026-10-24: 5 new sources hung the commit >300 s.
        if con.execute("SELECT 1 FROM chunk_text WHERE source=? LIMIT 1", (src,)).fetchone():
            con.execute("DELETE FROM chunk_text WHERE source=?", (src,))
            con.execute("DELETE FROM chunk_text_fts WHERE source_id LIKE ? AND source_id NOT LIKE ?", (src + "_chunk_%", src + "_%_chunk_%"))
        rows = [(f"{src}_chunk_{i}", c, src, e.get("jurisdiction") or "US", e.get("cite") or "") for i, c in enumerate(cs)]
        con.executemany("INSERT OR IGNORE INTO chunk_text (chunk_id,text,source,jurisdiction,citations) VALUES (?,?,?,?,?)", rows)
        con.executemany("INSERT INTO chunk_text_fts (text, source_id) VALUES (?,?)", [(c, f"{src}_chunk_{i}") for i, c in enumerate(cs)])
        con.commit(); total += len(cs); e["db_rows"] = len(cs)
    con.close(); Path(mpath).write_text(json.dumps(man, indent=1), encoding="utf-8")
    if log is not None: log.append({"step": 4, "name": "commit", "manifest": str(mpath), "db_rows": total, "db": str(db)})
    return total


def statute_lane(req: dict, con, log: list) -> dict:
    """4 (statute/rule): what the family holds + the command that fills it (no network here)."""
    prefixes = _prefixes(req["families"]); census = {}
    for p in prefixes[:6]:
        try:
            census[p] = [r[0] for r in con.execute("SELECT source FROM chunk_text WHERE source >= ? AND source < ? GROUP BY source LIMIT 60", (p, p + "\uffff")).fetchall()]
        except Exception as e:
            census[p] = f"error {e}"
    cmd = ""
    if req["kind"] == "rule" and req["families"] == ["pa_rcp"]:
        n = req["number"]; ch = "100" if int(n) < 200 else str((int(n) // 100) * 100)
        cmd = f"/usr/bin/python3 -m pipeline.ingest_pa_rcp_chapter {ch}   # Pa.R.C.P. {n} lives in Ch. {ch}; --dry first"
    elif req["kind"] == "rule" and req["families"][0] in ("pa_rap", "pa_rja", "pa_re"):
        t = {"pa_rap": 210, "pa_rja": 201, "pa_re": 225}[req["families"][0]]
        cmd = f"/usr/bin/python3 -m pipeline.ingest_pa_rcp_chapter --title {t} --all --dry"
    elif req["kind"] == "statute":
        cmd = "pipeline/official_source_puller.py pull (USC: uscode.house.gov; eCFR API with --compressed; Pa.C.S.: legis.state.pa.us) — convert PDF first, pass --raw"
    log.append({"step": 4, "name": "statute_lane", "families": req["families"], "census_keys": {k: (len(v) if isinstance(v, list) else v) for k, v in census.items()}, "command": cmd})
    return {"census": census, "command": cmd}


# ------------------------------------------------------------------ resolve
def resolve(request: str, k: int = K_DEFAULT, write_db: bool = False, con=None, pull: bool = True) -> dict:
    t0 = time.time(); log: list = []
    req = classify(request)
    own = con is None; con = con or _con()
    res = {"request": request, "cite": req["cite"], "kind": req["kind"], "families": req["families"], "escalation": log}
    mentions: list = []; pin_log: list = []            # 11-13 (prefetch_pin): candidates that only CROSS-REFERENCE the cite

    def found(hits, pin_kind: str = "none"):
        """FOUND = the AUTHORITY is pinned. The request's extra words (an Explanatory Comment phrase, a holding's words) are reported
        verified only when a hit carries them — else `words_unverified` names them (the lane must not quote them)."""
        joined = " ".join((h.get("text") or "").lower() for h in hits)
        missing = [w for w in (req.get("words") or []) if w.lower() not in joined]
        return {**res, "status": "FOUND", "hits": hits, "verified_quote": hits[0]["text"][:600], "words_unverified": missing,
                "resolved": "exact-header" if pin_kind == "exact_header" else "yes", "pin_kind": pin_kind,
                "pinned": hits[0].get("chunk_id"), "pull_needed": False, "pin_log": pin_log,
                **_receipt(req, log, pin_kind=pin_kind), "secs": round(time.time() - t0, 1)}

    def gate(hits, rung: str):
        """-> (hits_to_return | None, pin_kind).  A SECTION/RULE cite may only stop the ladder on an exact_header pin; a
        mention-only rung is REMEMBERED and the ladder continues (the section's own shelf often answers one rung later)."""
        g = pin_gate(req, hits, con)
        pin_log.append({"rung": rung, "pin_kind": g["pin_kind"], "n": len(hits or []), "detail": (g.get("detail") or [])[:6]})
        log.append({"step": 1, "name": f"pin_gate:{rung}", "query": req.get("cite"), "n": len(hits or []), "pin_kind": g["pin_kind"]})
        if not g.get("gated"):
            return hits, "none"
        if g["pin_kind"] == "exact_header":
            p = g["pinned"] or {}
            rest = [h for h in (hits or []) if h.get("chunk_id") != p.get("chunk_id")]
            return [p] + rest, "exact_header"
        have = {m.get("chunk_id") for m in mentions}
        mentions.extend(h for h in (hits or []) if h.get("chunk_id") not in have)
        return None, g["pin_kind"]

    try:
        hits = step_pins(req, con, log, per=3) if req["kind"] != "question" else []
        if hits:
            ok, pk = gate(hits, "pins")
            if ok: return found(ok, pk)
        if req["kind"] == "statute":                         # 1b (11-13, retriever_fts): the section's OWN source key —
            hits = step_source_key(req, con, log, per=3)     # AR._exact_statute answers 0 for '26 USC 6103' though
            if hits:                                         # usc_t26_s6103 holds 39 chunks
                ok, pk = gate(hits, "source_key")
                if ok: return found(ok, pk)
        hits = step_semantic(req, con, log, k=k)
        if hits:
            ok, pk = gate(hits, "semantic")
            if ok: return found(ok, pk)
        hits = step_fts(req, con, log)
        if hits:
            ok, pk = gate(hits, "fts")
            if ok: return found(ok, pk)
        extra = {}
        if req["kind"] == "case" and pull:
            p = step_pull(req, log, write_db=write_db)
            if p["status"] != "NOT_FOUND": return {**res, **p, "pin_kind": "none", **_receipt(req, log), "secs": round(time.time() - t0, 1)}
            extra = {"pull": p.get("pull")}
        elif req["kind"] in ("statute", "rule"):
            extra = {"statute_lane": statute_lane(req, con, log)}
        queries = [e.get("query") for e in log if e.get("query")]
        tgt = (CFAM.parse(req.get("cite") or "").get("pull_target") if CFAM else "") or ""
        if mentions:
            # MENTION_ONLY: every candidate merely cross-references the cite.  NOT a resolution — hits is EMPTY so no caller
            # can quote these words as the section; `mention_hits` keeps them for the audit, `pull_target` names what to pull.
            g = pin_gate(req, mentions, con)
            log.append({"step": 5, "name": "mention_only", "queries": queries, "pull_target": tgt})
            return {**res, "status": "MENTION_ONLY", "resolved": "mention-only", "pinned": None, "pin_kind": "mention-only",
                    "pull_needed": True, "pull_target": tgt, "hits": [], "mention_hits": mentions[:3],
                    "mention_detail": (g.get("detail") or [])[:6], "pin_log": pin_log, "queries": queries, **extra,
                    **_receipt(req, log, pin_kind="mention-only"), "secs": round(time.time() - t0, 1)}
        log.append({"step": 5, "name": "not_found", "queries": queries})
        # 11-13 (retriever_fts): a NOT_FOUND is an ABSENCE CLAIM — it ships with the sweep receipt or it is not a finding.
        return {**res, "status": "NOT_FOUND", "hits": [], "queries": queries, "resolved": "no", "pinned": None,
                "pin_kind": "none", "pull_needed": bool(tgt), "pull_target": tgt, "pin_log": pin_log, **extra,
                **_receipt(req, log, pin_kind="none"), "secs": round(time.time() - t0, 1)}
    finally:
        if own: con.close()


# ------------------------------------------------------------------ PACK (adapted with attribution from _sandbox_m2_unit_pack analysis_runner.py)
# lane -> family set (data; a lane may also be a pipeline/m2/bots/analysis/<bot>/JOB.yaml name, read when present)
LANE_FAMILIES = {
    "procedural": ["pa_rcp", "frcp", "amjur_pleading", "amjur_dismissal"], "definitions": ["glossary", "dictionary_act", "pa_cs_1_", "doctrine"],
    "verbatim-rule": ["pa_rcp", "pa_rap", "frcp", "frap"], "construction": ["canons", "pa_cs_1_", "amjur_"], "authority-stack": ["usreports", "amjur_stare_decisis", "amjur_courts"],
    "stare-decisis": ["usreports", "amjur_stare_decisis"], "jurisdiction": ["pa_cs_42_", "amjur_courts", "amjur_federal_courts", "us_const"],
    "duty": ["amjur_officers", "amjur_clerks", "pa_cs_42_"], "remedy": ["pa_cs_42_", "pa_rcp", "amjur_costs", "usreports"],
    "posture-vehicle": ["pa_rcp", "pa_rap", "pa_cs_42_"], "pseudolaw-rebut": ["usreports", "doctrine", "us_const"], "case-law": ["usreports", "pa_cases"],
    "pa-state-law": ["pa_cs_42_", "pa_cs_1_", "pa_rcp", "pa_const"], "federal-statute": ["usc_15_", "stat_", "dictionary_act"], "fed-rules": ["frcp", "frap", "fre"],
    "regulations": ["cfr_"], "capacity": ["doctrine", "glossary", "pa_cs_15_"], "money-flow": ["usc_15_", "pa_cs_15_", "amjur_instruments"],
    "void-order": ["usreports", "pa_cs_42_", "amjur_courts"], "dismissal-posture": ["pa_rcp", "amjur_dismissal", "pa_rap"], "fetch": [],
}


def lane_families(lane: str) -> list[str]:
    """unit_families (attributed): forum-filtered family set of ONE lane (a bot JOB.yaml when the lane is a bot name)."""
    try:
        for b in AR.load_bots(only=[lane]) or []:
            if b.get("families"): return AR.forum_families(list(b["families"]))
    except Exception:
        pass
    reg = AR._fam_registry()
    fams = [f for f in LANE_FAMILIES.get(lane, ["usreports", "doctrine"]) if f in reg or f in _RAW_PREFIX]
    return AR.forum_families(fams) or fams


def unit_claims(units_text: str) -> list[dict]:
    """ADVERSE_POINTS.md -> claims via analysis_runner.split_claims (the m2 unit splitter)."""
    cl = AR.split_claims(units_text or "")
    return cl or [{"id": 1, "title": (units_text or "")[:80], "text": units_text or ""}]


def unit_queries(claims: list[dict], lane: str) -> list[str]:
    """unit_queries (attributed): claim title + first 300 chars per unit, then the lane's standing queries; distinct."""
    qs = []
    for c in claims: qs += [c.get("title") or "", (c.get("text") or "")[:300]]
    qs += [f"{lane.replace('-', ' ')} {c.get('title') or ''}" for c in claims[:4]]
    return list(dict.fromkeys(q for q in qs if isinstance(q, str) and q.strip()))


def _retrieve_batched(qs: list[str], families: list[str], k: int) -> list[dict]:
    """_retrieve_batched (attributed): AR._retrieve per MAX_Q batch, merged by chunk_id keeping the best score."""
    best: dict = {}
    for i in range(0, len(qs), AR.MAX_Q):
        batch = qs[i:i + AR.MAX_Q]
        for h in AR._retrieve(batch, families, min(200, max(k, 4 * len(batch)))):
            if h.get("error"): return [h]
            cid = h.get("chunk_id") or f"?{len(best)}"
            if cid not in best or (h.get("score") or 0) > (best[cid].get("score") or 0): best[cid] = h
    return sorted(best.values(), key=lambda h: -(h.get("score") or 0))


def per_lane_pulls(claims: list[dict], lanes: list[str], k: int = 4) -> dict[str, list[dict]]:
    """THE FIX: one pull PER FAMILY (lane × family), never one union pull ranked across every family — a lane whose set mixes a
    big shelf (amjur/usreports) with a thin one (pa_cs_42_) would otherwise lose the thin one inside its own pull too.
    Returns {"<lane>|<family>": hits} (each hit tagged fam + lane)."""
    out = {}
    for lane in lanes:
        for fam in lane_families(lane):
            hits = [h for h in _retrieve_batched(unit_queries(claims, lane), [fam], k) if not h.get("error")]
            for h in hits: h["fam"] = _family_of(h.get("source"), [fam]); h["lane"] = lane
            out[f"{lane}|{fam}"] = [h for h in hits if h.get("fam") != "unscoped"] or hits[:1]   # in-family first; one unscoped stand-in when the shelf is thin
    return out


def pack_excerpts(by_lane: dict[str, list[dict]], pinned: list[dict], budget: int) -> list[dict]:
    """pack_excerpts (attributed, fixed): ROUND-ROBIN across the (lane, family) groups — every family's best before any family's second;
    pinned chunks dropped; a hit that does not fit is skipped, not truncated.  Deterministic (lane order, score order inside)."""
    pinned_ids = {p.get("chunk_id") for p in pinned if p.get("chunk_id")}; seen = set(pinned_ids)
    groups = {}
    for lane, hits in by_lane.items():
        g = []
        for h in sorted(hits, key=lambda h: -(h.get("score") or 0)):
            cid = h.get("chunk_id")
            if not cid or cid in seen or "text" not in h: continue
            seen.add(cid); g.append(h)
        groups[lane] = g
    pick, used = [], 0
    while any(groups.values()):
        progressed = False
        for lane in list(groups):
            g = groups[lane]
            if not g: continue
            h = g.pop(0); n = len(h.get("text") or "")
            if used + n > budget: continue
            pick.append(h); used += n; progressed = True
        if not progressed and not any(groups.values()): break
    return pick


def record_facts(facts_text: str, limit: int = 2500) -> str:
    """CASE_FACTS.md 'Record facts' lines (heading match, else dated bullet lines)."""
    t = facts_text or ""
    m = re.search(r"(?im)^#+\s*record facts[^\n]*\n(.*?)(?=^#|\Z)", t, re.S)
    body = m.group(1) if m else "\n".join(l for l in t.splitlines() if re.match(r"\s*[-*]\s", l) and re.search(r"\d{4}|\d{1,2}/\d{1,2}", l))
    return body.strip()[:limit]


def build_pack(units_text: str, lanes: list[str], facts_text: str, budget: int = PACK_BUDGET, k: int = 8, con=None) -> dict:
    """build_unit_pack (attributed): pins for every cite the units name + record facts + per-lane family pulls, round-robin merged."""
    claims = unit_claims(units_text)
    own = con is None; con = con or _con()
    try:
        cites = list(dict.fromkeys(c for cl in claims for c in AR.cites_in(cl.get("text") or "")))
        pinned, pin_log = [], []
        for c in cites:
            req = classify(c); lg = []
            hs = step_pins(req, con, lg, per=1)
            pin_log.append({"cite": c, "found": bool(hs), "log": lg})
            for h in hs[:1]: pinned.append({**h, "cite": c})
        facts = record_facts(facts_text)
        by_lane = per_lane_pulls(claims, lanes, k=max(2, k // 2))
        head_len = sum(len(p.get("text") or "") + 120 for p in pinned) + len(facts) + 400
        axis = pack_excerpts(by_lane, pinned, max(0, budget - head_len))
        return {"claims": [c.get("id") for c in claims], "cites": cites, "pinned": pinned, "pin_log": pin_log, "facts": facts, "lanes": lanes,
                "lane_families": {l: lane_families(l) for l in lanes}, "by_lane_n": {l: len(v) for l, v in by_lane.items()}, "axis": axis,
                "lanes_with_excerpt": sorted({h.get("lane") for h in axis}), "families_with_excerpt": sorted({h.get("fam") for h in axis}),
                "excerpt_chars": sum(len(h.get("text") or "") for h in axis), "budget": budget}
    finally:
        if own: con.close()


def render_pack(pack: dict) -> str:
    L = ["# RETRIEVAL PACK (fetch — shared ground for every lane of this leg; cite chunks as [corpus:<chunk_id>])",
         f"lanes: {', '.join(pack['lanes'])} | families: " + "; ".join(f"{l}=[{','.join(f)}]" for l, f in pack["lane_families"].items()), ""]
    L.append("## PINNED AUTHORITIES (named in the units; exact source-key pins)")
    for p in pack["pinned"]:
        L.append(f"- {p.get('cite')} → [corpus:{p['chunk_id']}] source={p['source']} [{p['family']}]\n  “{_WS.sub(' ', (p.get('text') or '')[:700]).strip()}”")
    for e in pack["pin_log"]:
        if not e["found"]: L.append(f"- {e['cite']} → NOT PINNED (pins ran: {[x.get('name') for x in e['log']]}; ask @fetch to escalate)")
    L += ["", "## RECORD FACTS (CASE_FACTS.md)", pack["facts"] or "(none)", "", "## FAMILY EXCERPTS (one pull per lane family-set, round-robin merged)"]
    for i, h in enumerate(pack["axis"], 1):
        L.append(f"[{i}] [{h.get('fam')}] lane={h.get('lane')} source={h.get('source')} chunk={h.get('chunk_id')} part={h.get('part') or '-'} score={h.get('score')}\n{h.get('text')}")
    out = "\n".join(L)
    if len(out.encode()) > pack["budget"]:                                           # hard cap: trim excerpts from the tail
        while pack["axis"] and len(out.encode()) > pack["budget"]:
            pack["axis"].pop(); out = render_pack(pack); break
    return out


# ------------------------------------------------------------------ CLI
def _card(res: dict) -> str:
    st = res["status"]
    if st in ("FOUND", "PULLED", "PULLED_STAGED"):
        h = res["hits"][0] if res.get("hits") else {}
        lines = [f"FETCH: {st} | {res['cite']} | kind={res['kind']} families={res['families']} | step={h.get('step')} | pin_kind={res.get('pin_kind')}"]
        for h in res.get("hits", [])[:3]:
            lines.append(f"  [corpus:{h['chunk_id']}] source={h['source']} [{h['family']}]\n  “{_WS.sub( ' ', (h.get('text') or '')[:500]).strip()}”")
        if res.get("words_unverified"):
            lines.append(f"  WORDS NOT IN THE PINNED CHUNKS (do not quote them; NOT IN CORPUS for this authority): {res['words_unverified']}")
        if st != "FOUND":
            lines.append(f"  STAGED manifest={res.get('ingest_manifest')} chunks={res.get('chunks')} — parent runs:\n    " + "\n    ".join(res.get("commands", [])))
        return "\n".join(lines)
    if st == "MENTION_ONLY":
        # 11-13 (prefetch_pin): the corpus only CROSS-REFERENCES this cite.  Not a resolution — nothing here may be quoted as the section.
        lines = [f"FETCH: MENTION_ONLY | {res['cite']} | kind={res['kind']} families={res['families']} | pin_kind=mention-only "
                 f"| pull={res.get('pull_target')}",
                 "  the cite appears only inside running text (a cross-reference). NOT the section's body — do NOT quote these words as the rule."]
        for d in (res.get("mention_detail") or [])[:3]:
            lines.append(f"  rejected [corpus:{d.get('chunk_id')}] source={d.get('source')} family_match={d.get('family_match')} | {d.get('why')}")
        lines.append(f"  pull the section: /usr/bin/python3 scripts/rooms/room_prefetch.py <inputs> --pull-section {res.get('pull_target')}")
        return "\n".join(lines)
    q = res.get("queries") or []
    extra = f" pull_rungs={res['pull'].get('rungs')} rejected={[(e.get('name'), e.get('name_guard')) for e in res['pull'].get('rejected') or []]}" if res.get("pull") else ""
    extra += f" statute_lane_cmd={res['statute_lane'].get('command')!r}" if res.get("statute_lane") else ""
    return f"FETCH: NOT_FOUND | {res['cite']} | kind={res['kind']} families={res['families']} | queries={q}{extra}"


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0]); sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("resolve"); r.add_argument("request"); r.add_argument("--json", action="store_true"); r.add_argument("--write-db", action="store_true")
    r.add_argument("--k", type=int, default=K_DEFAULT); r.add_argument("--no-pull", action="store_true")
    p = sub.add_parser("pack"); p.add_argument("--units", required=True); p.add_argument("--lanes", required=True); p.add_argument("--facts", required=True)
    p.add_argument("--out", required=True); p.add_argument("--budget", type=int, default=PACK_BUDGET); p.add_argument("--k", type=int, default=8)
    c = sub.add_parser("commit"); c.add_argument("manifest")
    a = ap.parse_args(argv)
    if a.cmd == "resolve":
        res = resolve(a.request, k=a.k, write_db=a.write_db, pull=not a.no_pull)
        print(json.dumps(res, ensure_ascii=False, indent=1) if a.json else _card(res))
        return 0 if res["status"] in ("FOUND", "PULLED", "PULLED_STAGED") else (3 if res["status"] == "MENTION_ONLY" else 2)
    if a.cmd == "pack":
        pk = build_pack(Path(a.units).read_text(encoding="utf-8"), [x for x in a.lanes.split(",") if x], Path(a.facts).read_text(encoding="utf-8"), budget=a.budget, k=a.k)
        txt = render_pack(pk); Path(a.out).write_text(txt, encoding="utf-8")
        print(json.dumps({"out": a.out, "bytes": len(txt.encode()), "pinned": len(pk["pinned"]), "excerpts": len(pk["axis"]), "by_lane": pk["by_lane_n"],
                          "lanes_with_excerpt": sorted({h.get("lane") for h in pk["axis"]})})); return 0
    if a.cmd == "commit":
        print(json.dumps({"db_rows": commit_manifest(a.manifest), "then": [INGEST_CMD.format(manifest=a.manifest), RESTART_CMD]})); return 0


if __name__ == "__main__":
    sys.exit(main())
