#!/usr/bin/env python3
"""m2 → m0's PEN. The bots COMPREHEND (study cells); m1_rebuttal WRITES (deterministic: it cannot invent a cite, a quote or
an admission). This bridge turns the cells into m1's two inputs and runs m1 unchanged:

    cells.jsonl  ──►  bank.json   (m1 bank entries: {pattern, tier, heading, text, cites, record_facts, source})
    adverse txt  ──►  matrix.json (m1 build_matrix: the opponent's own headings — the DRAFTING unit)
                      ──►  m1_rebuttal.assemble → voice_pass → ARGUMENT  ──►  m0 skeleton → full filing → m1_filing_gate → render

Nothing here writes prose. A cell's `finding` is the bot's verified comprehension of ONE authority/axis; the quote it
carries was verified against the chunk by the runner. Run:
    M2_ROOT=<home> python3 -m pipeline.m2.bank_bridge --slug <slug> --cells <cells.jsonl> --adverse <reply.txt> \
        --skeleton <m0 draft .md> --out <dir> [--voice --lane http://localhost:8000/v1 --court PA_CP_PHILA --type opposition_state]
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import argparse, importlib, json, os, re, sys
from pathlib import Path

ROOT = Path(os.environ.get("M2_ROOT", str(Path(__file__).resolve().parents[2])))


def _live(mod: str):
    import pipeline as _pkg
    live = str(ROOT / "pipeline")
    if live not in _pkg.__path__: _pkg.__path__.append(live)
    return importlib.import_module(f"pipeline.{mod}")


R = _live("m1_rebuttal")
sr = importlib.import_module("pipeline.m2.analysis_runner")

_STOP = set("plaintiff defendant plaintiffs defendants complaint against should claims claim assert asserts because court "
            "motion brief reply response objection objections preliminary purported argument properly understood".split())
_CASE = re.compile(r"\b([A-Z][A-Za-z'\u2019&.\-]+(?:\s+[A-Z][A-Za-z'\u2019&.\-]+){0,3})\s+v\.\s+([A-Z][A-Za-z'\u2019&.\-]+)")

# ---- S-no-hardcode (2026-11-14): the party tokens _heading_for() must ignore are THIS case's
# own parties (adverse names + the filer), from pipeline.case_profile — not a literal pair.
_SLUG = None                         # set from --slug in main()
_PT = {}


def _party_tokens() -> set:
    key = _SLUG or ""
    if key not in _PT:
        toks = set()
        try:
            from pipeline.case_profile import filer_tokens, profile as _cp
            toks |= filer_tokens()
            if key:
                for n in _cp(key).get("adverse_names") or []:
                    toks |= {t.lower() for t in re.split(r"[^A-Za-z]+", n) if len(t) >= 4}
        except Exception:
            pass
        _PT[key] = toks
    return _PT[key]


def _anchor(text: str, n: int = 4) -> str:
    """m1's own pattern discipline: a few long words of the heading, escaped, OR-joined."""
    words = [w for w in re.findall(r"[a-z]{5,}", text.lower()) if w not in _STOP]
    seen, out = set(), []
    for w in words:
        if w not in seen: seen.add(w); out.append(w)
    return "|".join(re.escape(w) for w in out[:n])


def _names(text: str) -> set[str]:
    out = {m.group(1).split()[-1].lower().strip("’'.,") for m in _CASE.finditer(text or "")}
    out |= {m.group(0).lower() for m in re.finditer(r"§\s*[\d.]+(?:\([a-z0-9]+\))*", text or "")}
    return out


def _heading_for(cell: dict, claim_title: str, pts: list[dict], m1_claims: dict) -> str | None:
    """Which opponent point does this cell answer? (1) m1 heading-claims ('The objection as to <X> is not well taken')
    → the point X; (2) authority cells → the point whose PASSAGE names that authority; (3) none → structural (once)."""
    ct = set(re.findall(r"[a-z]{4,}", claim_title.lower()))
    for p in pts:
        hw = [w for w in re.findall(r"[a-z]{4,}", p["title"].lower())]
        if len(hw) >= 5 and sum(1 for w in hw if w in ct) / len(hw) >= 0.6: return p["title"]
    n = _names(claim_title + " " + " ".join(a.get("cite", "") for a in cell.get("authorities") or []))
    mt = re.match(r"^([A-Z][A-Za-z'’]+)\s+(?:is|does|v\.)\b", claim_title)      # 'Cowden does not reach…' → cowden
    if mt: n.add(mt.group(1).lower().strip("’'"))
    # S-no-hardcode (2026-11-14): this dropped the literals {"progressive","filer"} — one case's
    # parties — so in every other case the parties' own names were scored as distinctive matches.
    # The party tokens to ignore are now the case's own (adverse + filer) from case_profile.
    n -= {"plaintiff", "defendant"} | _party_tokens()
    best, score = None, 0
    for p in pts:
        body = (p["title"] + " " + p.get("text", "")).lower()
        sc = sum(1 for x in n if x in body)
        if sc > score: best, score = p["title"], sc
    return best


_META = re.compile(r"^(the (claim|draft|corpus|excerpts?|surreply|sur-reply|response|pleading|document)\b|this (supports|confirms|is consistent|aligns)|"
                   r"the (opponent|defendant|movant)'?s? characteri[sz]ation|on (this|my) axis|the bot|level \d|\(level \d\))", re.I)
_META_IN = re.compile(r"\b(the corpus|corpus excerpt|the excerpts?|the draft'?s?|on this axis|hierarchy level|codification[_ ]only|NOT IN CORPUS|\[corpus\]|\[official[^\]]*\]|\[doc\]|"
                      r"the draft relies|the draft (does|cites|invokes|asserts)|the provided|provided (statutory |corpus )?(text|excerpt)|the mapping is|the pleading'?s? (averments? )?(is|are) (accurate|consistent)|the statement is (accurate|correct)|is accurate based on)\b", re.I)


_CITE_OK = re.compile(r"\bv\.\s|§|\b(Pa\.R\.C\.P\.|Pa\.R\.A\.P\.|Fed\. ?R\.|Phila\.|Am\.? ?Jur|AmJur|U\.S\.C\.|Pa\.C\.S\.|P\.S\.|C\.F\.R\.|Pa\. Code|Const\.|art\.|Art\.)", re.I)


def _is_cite(c: str) -> bool:
    c = str(c or "").strip()
    return bool(_CITE_OK.search(c)) and not re.search(r"_(chunk|vol|PDFA)|^[a-z0-9_]+$", c)


_ADVERSE_NOUNS: set = set()   # meta nouns that, in THIS run, name the opposing paper itself (set by cells_to_bank)


def filing_voice(text: str) -> str:
    """Only sentences a court may read. The bots' `finding` is work product ABOUT the draft; `proposition` is the rule
    stated. Drop any sentence that talks about the claim/draft/corpus/axis; strip tag markers. Deterministic — nothing rewritten.

    ADVERSE_NOUN_EXEMPT (2026-11-17): `_META` treats 'the response / the pleading / the document' as bot talk about our own
    draft. When the OPPOSING paper is itself a Response, every sentence stating what "The Response" says or omits — the
    record facts the reply stands on — was deleted, leaving only the 'VEHICLE:' tail. A meta noun that names the adverse
    instrument is filing voice."""
    out = []
    for sent in R._split_sents(" ".join(text.split())):
        s = sent.strip()
        if not s: continue
        m = _META.search(s)
        if m and _ADVERSE_NOUNS:
            noun = re.match(r"(?i)^the (\w+)", s)
            if noun and noun.group(1).lower() in _ADVERSE_NOUNS:
                m = None
        if m or _META_IN.search(sent): continue
        out.append(s)
    return " ".join(out)


# ---- GUARDRAIL (m0's cite_verifier_v2 + corpus phrase check) — a bot's `verified` flag is advisory only ----
_CITE_CACHE: dict = {}
_DB = None
def _db():
    global _DB
    if _DB is None:
        try:
            from pipeline.cite_verifier_v2 import _find_legaldb
            _DB = _find_legaldb()()
        except Exception as e:
            print(f"[guard] legal DB unavailable ({e}); cite-family check skipped", file=sys.stderr); _DB = False
    return _DB

def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", re.sub(r"\s+", " ", str(t or "").lower()))

_QCON = None
def _quote_in_source(quote: str, source: str) -> bool:
    """Corpus check scoped to the bot's named source_doc: the quote (normalized, up to 160 chars) must appear in a chunk
    OF THAT SOURCE. Reads turbovec chunk_text.db directly (source column, 0.0 s) -- the HTTP /search_text phrase path
    took 2+ s per quote and timed out the guard at ~100 quotes."""
    import os, sqlite3
    from pathlib import Path as _P
    global _QCON
    words = str(quote or "").split()
    if len(words) < 8 or not source: return False
    if _QCON is None:
        db = _P(os.environ.get("TURBOVEC_DIR", str(_eroot() / "corpus"))) / "chunk_text.db"
        try: _QCON = sqlite3.connect(f"file:{db}?mode=ro", uri=True, check_same_thread=False)
        except Exception: _QCON = False
    if not _QCON: return False
    nq = _norm(quote)[:160]
    src = source.split("_chunk_")[0]
    try:
        rows = _QCON.execute("select text from chunk_text where source=? or chunk_id=?", (src, source)).fetchall()
    except Exception: rows = []
    for (txt,) in rows:
        nt = _norm(txt)
        if nq[:100] in nt or (len(nq) > 60 and nq[-60:] in nt): return True
    return False

def _section_near_quote(quote: str, source: str, tok: str, tail: str) -> bool:
    """For a statute quoted through a case/recital: the chunk that holds the quote must also name the section."""
    import json as _j, urllib.parse, urllib.request
    q = " ".join(str(quote).split()[:10])
    url = "http://localhost:8100/search_text?query=%s&phrase=true&top_k=12&source=%s" % (urllib.parse.quote(q), urllib.parse.quote(source))
    try:
        with urllib.request.urlopen(url, timeout=25) as r: hits = _j.loads(r.read()).get("hits") or []
    except Exception: return False
    nq = _norm(quote)[:100]
    for h in hits:
        t = str(h.get("text") or "")
        if nq in _norm(t) and (tok in t or (len(tail) >= 3 and re.search(r"[§\s]" + re.escape(tail) + r"\b", t))):
            return True
    return False

_STAT_FAMILIES = [
    (re.compile(r"\b(\d+)\s*P\.?\s*S\.?", re.I),            lambda m: (f"pa_{m.group(1)}ps",)),
    (re.compile(r"\b(\d+)\s*Pa\.?\s*C\.?\s*S\.?", re.I),  lambda m: (f"pa_cs_t{m.group(1)}", f"pa_cs_{m.group(1)}")),
    (re.compile(r"\bPa\.?\s*R\.?\s*C\.?\s*P\.?", re.I),    lambda m: ("pa_code_t231", "pa_rcp")),
    (re.compile(r"\b(\d+)\s*Pa\.?\s*Code", re.I),           lambda m: (f"pa_code_t{m.group(1)}",)),
    (re.compile(r"\b(\d+)\s*U\.?\s*S\.?\s*C\.?", re.I),   lambda m: (f"uscode-2023-title{m.group(1)}", f"usc_t{m.group(1)}")),
    (re.compile(r"\bPhila\.?\s*Civ\.?\s*R\.?|\bPhila\.?\s*C\.?\s*R\.?\s*\*", re.I), lambda m: ("phila_civ_r", "phila_cr")),  # S-bridge-hash-source: Phila local rules (phila_civ_r_*, phila_cr_*)
]
def _source_doc_of(src: str) -> str:
    """S-bridge-hash-source (2026-11-16): the room names its source as the CHUNK ID (16/32-hex); the family check needs the
    chunk's source_doc. Resolve through chunk_text.db (the same table _quote_in_source reads) — before this every § 5505 /
    *208.3 / *210 row (source pa_cs_t42 / phila_cr_*) was dropped as 'section not found', emptying the brief's bank."""
    s = str(src or "")
    if not re.fullmatch(r"[0-9a-f]{16}|[0-9a-f]{32}", s):
        return s
    _quote_in_source("x " * 9, s)          # opens _QCON if needed (returns False harmlessly)
    if not _QCON:
        return s
    try:
        r = _QCON.execute("select source from chunk_text where chunk_id=? limit 1", (s,)).fetchone()
        return r[0] if r and r[0] else s
    except Exception:
        return s
def _statute_family_ok(cite: str, src: str) -> bool:
    """True when the bot's source is the enacted text of the code family the cite names."""
    sl = _source_doc_of(src).lower()
    for rx, fam in _STAT_FAMILIES:
        m = rx.search(cite)
        if m:
            return any(sl.startswith(f.lower()) for f in fam(m))
    return False

_SURNAME = re.compile(r"([A-Z][A-Za-z'\-]+)\s+v\.")
def guard_authorities(cell: dict) -> tuple[list, list]:
    """Returns (kept, dropped). An authority is kept only when (1) its quote is found verbatim in a corpus chunk and
    (2) that chunk's source_doc matches the cite: for a case cite the party surname is in the source id; for a
    statute/rule cite the source is one cite_verifier_v2 resolves the cite to (the section token appears in that doc).
    Quote-in-source-but-wrong-label (e.g. UIPA text filed under a UTPCPL cite) therefore fails."""
    kept, dropped = [], []
    for a in (cell.get("authorities") or []):
        cite, quote, src = str(a.get("cite") or ""), str(a.get("quote") or ""), str(a.get("source") or "")
        if not _quote_in_source(quote, src):
            dropped.append((cite, f"quote not verbatim in named source {src[:40]}")); continue
        m = _SURNAME.search(cite)
        if m:                                   # case cite: the source must BE that case
            sl = src.lower().replace("_", " ").replace("'", "")
            ok = m.group(1).lower().replace("'", "") in sl
            if not ok:                          # docvault ids carry the reporter cite (docvault_pull_a2d_787_376), not the name
                rp = re.search(r"(\d+)\s+([A-Z][\w.]*(?:\s?\d?d)?)\s+(\d+)", cite)
                if rp:
                    vol, rep, page = rp.group(1), re.sub(r"[^a-z0-9]", "", rp.group(2).lower()), rp.group(3)
                    ok = vol in sl and page in sl and rep in sl.replace(" ", "")
            why = f"quote source {src[:40]} is not the cited case"
        else:                                   # statute/rule cite: the quoting chunk must carry the section token
            sec = re.search(r"(\d+(?:\.\d+)?(?:-\d+(?:\.\d+)?)?)(?:\([^)]*\))*\s*$", cite.split("(as")[0].strip())
            tok = sec.group(1) if sec else ""
            tail = tok.split("-")[-1]
            # the source IS the enacted text of that code family (pa_73ps_* for 73 P.S., pa_cs_t42 for 42 Pa.C.S.,
            # pa_code_t231 for Pa.R.C.P. ...): verbatim-in-source already proves it; PA unconsolidated acts use INTERNAL
            # numbering ("Section 2"), so the P.S. token never appears in the chunk. Section-near-quote applies only
            # when the statute is quoted THROUGH a case or another document.
            ok = bool(tok) and (_statute_family_ok(cite, src) or tok in quote or tail in quote
                                or _section_near_quote(quote, src, tok, tail))
            why = f"section {tok} not found with the quote in {src[:40]} (mislabeled statute?)"
        if ok:
            a = dict(a); a["verified"] = True; a["guard_source"] = src; kept.append(a)
        else:
            dropped.append((cite, why))
    return kept, dropped


def cells_to_bank(cells: list[dict], pts: list[dict], adverse: str, m1_claims: dict) -> list[dict]:
    heads = [p["title"] for p in pts]
    """One bank entry per verified cell. tier: RECORD when the finding stands on a record fact, SETTLED when on verified
    authority. Cells that answer no heading become structural entries anchored on the heading whose passage names the
    same authority in the ADVERSE text (abandoned PO authorities), else the first heading (levers) — m1 states each once."""
    bank = []
    # the adverse paper's own instrument noun(s) are filing voice, not bot meta (see filing_voice)
    global _ADVERSE_NOUNS
    _title = " ".join((adverse or "")[:600].split()).lower()
    _ADVERSE_NOUNS = {n for n in ("response", "pleading", "document", "surreply", "sur-reply")
                      if re.search(r"\b%s\b" % re.escape(n), _title)}
    for c in cells:
        # RECORD_CELLS_ARE_GROUNDED (2026-11-17): a cell whose ground is the opponent's OWN paper ([doc:…] admission,
        # face contradiction — their COS "Pro-Se Plaintiff" beside two bar-numbered signatures) carries status RECORD
        # and no statute. This filter threw every one of them away (4 cells, 0 bank entries) — the whole
        # "prima facie from the opponent's writings" class. RECORD is a grounded status; its tier is RECORD, its role
        # impeachment, and it rides under its claim's heading.
        if c.get("status") not in ("GROUNDED", "CONFLICT", "RECORD"): continue
        _record_only = c.get("status") == "RECORD"
        auths, dropped = guard_authorities(c)
        for cite, why in dropped:
            print(f"[guard] DROP {c.get('bot')}/{c.get('claim')}: {cite[:60]} — {why}", file=sys.stderr)
        if dropped and not auths and c.get("status") == "GROUNDED":
            c = dict(c, status="GAP"); continue                       # nothing verified stands under it
        finding = filing_voice(str(c.get("proposition") or "")) or filing_voice(str(c.get("finding") or ""))
        if _record_only:
            # a RECORD cell must point at the opponent's paper or the record — a bare assertion is not a record fact
            _rf = " ".join(str(x) for x in (c.get("record_facts") or []))
            if not finding or not re.search(r"\[doc:|\[record:|JUDICIAL ADMISSION|ADMISSION:", _rf + " " + str(c.get("proposition") or "")):
                continue
        elif not finding or (not auths and c.get("status") != "GROUNDED"): continue
        title = str(c.get("title") or "")
        h = _heading_for(c, title, pts, m1_claims)
        if h is None and c.get("proposition"):      # e.g. a per-ORDER cell: route by the issue its proposition argues
            h = _heading_for(c, f"{title} {c['proposition']}", pts, m1_claims)
        # CLAIM_IS_THE_ADDRESS (2026-11-17): the room keys every cell to its point number (`claim` N = ADVERSE_POINTS point N);
        # a cell whose bot-written title ('CONTRADICTION: The Response's certificate…') shares no words with the point's
        # heading fell to "structural" and was never emitted. The claim number is the address — word overlap is the fallback.
        if h is None:
            try:
                _n = int(c.get("claim") or 0)
            except (TypeError, ValueError):
                _n = 0
            if 1 <= _n <= len(pts):
                h = pts[_n - 1]["title"]
        # the bank entry's `heading` is how m1 finds it (HEADING_IDENTITY); a cell routed to a point carries that point's title
        _entry_heading = (h or title)[:120]
        auths = [a for a in auths if _is_cite(a["cite"])]                 # a source KEY is not a citation
        cites = list(dict.fromkeys(a["cite"] for a in auths))
        quotes = [f"“{a['quote'].strip()}”" for a in auths
                  if len(str(a.get("quote") or "").split()) >= 8 and not _CASE.search(str(a["quote"])[:60]) and " v. " not in str(a["quote"])[:40]][:2]
        finding = re.sub(r"\b\w[\w.§ ]{0,40},\s*supra\.(?=\s|$)", "", finding).strip()   # 'Toy … construed, supra.' fragments
        text = finding
        if quotes and not any(q[1:40] in finding for q in quotes):
            text = f"{finding} The authority itself: {quotes[0]} ({cites[0]})."
        if c.get("status") == "CONFLICT" and c.get("gap"):
            g = filing_voice(str(c["gap"]))
            if g: text = f"{text} {g}"
        ours = R._our_heading(h) if h else title[:120]                      # ONE point per opponent point, headed in OUR voice;
        entry = {"pattern": _anchor(h) if h else _anchor(title) or "objection", "answer_heading": ours,   # authority findings fold under it
                 "tier": "RECORD" if (_record_only or (not auths and c.get("level") in ("", None, "record"))) else "SETTLED",
                 "heading": _entry_heading, "text": text, "cites": cites,
                 "record_facts": [], "source": f"m2:{c.get('bot')}", "m2_claim": c.get("claim"), "m2_level": c.get("level"),
                 **({"role": "impeachment"} if _record_only else {})}
        if not h:  # structural: answers no heading — m1 emits it once under its anchor
            entry["pattern"] = _anchor(heads[0]) if heads else entry["pattern"]
        bank.append(entry)
    # dedupe by (cite set, first 80 chars) — 13 bots on one claim say the same thing in different registers
    seen, out = set(), []
    for e in bank:
        # DEDUPE_ON_THE_VIOLATION (2026-11-17): the key was (cites, first 80 chars). A 'Rule: "<quote>" <cite>. Violation: …'
        # cell shares its first 80 chars with EVERY other cell quoting the same rule, so claim 2's *210(4) violation was
        # swallowed by claim 5's *210(4) fetch. Identity = the heading answered + the words AFTER the quoted rule.
        _body = re.sub(r"(?is)^\s*rule:\s*[\"“].*?[\"”]\s*[^.]{0,80}\.\s*(?:violation:\s*)?", "", e["text"]) or e["text"]
        k = (e.get("heading", "")[:60].lower(), tuple(sorted(e["cites"])), " ".join(_body.split())[:80].lower())
        if k in seen: continue
        seen.add(k); out.append(e)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", required=True); ap.add_argument("--cells", required=True); ap.add_argument("--adverse", required=True)
    ap.add_argument("--skeleton", required=True, help="m0 draft .md: everything but ## ARGUMENT is kept as the filing's anatomy")
    ap.add_argument("--cells-md", default="", help="the m1 draft whose claims key the cells (for heading-claim titles)")
    ap.add_argument("--points-md", default="", help="the drafting UNIT when the adverse paper is a COURT's order/opinion: "
                    "our own issues (**n. title** points). Default = the opponent's headings from --adverse")
    ap.add_argument("--out", required=True); ap.add_argument("--voice", action="store_true")
    ap.add_argument("--lane", default=os.environ.get("M2_LANE", "http://localhost:8000/v1"))
    ap.add_argument("--court", default="PA_CP_PHILA"); ap.add_argument("--type", default="opposition_state", dest="ftype")
    a = ap.parse_args(argv)
    global _SLUG
    _SLUG = a.slug                   # party tokens to ignore come from this case's profile
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    adverse = R._join_heading_lines(Path(a.adverse).read_text(encoding="utf-8", errors="replace"))
    # 1) MATRIX = the opponent's headings, m1's own extractor on the adverse text we were given
    if a.points_md:
        # A court order/opinion has no argument headings (adverse_points returns 0, or garbage on its I/A/1 markers):
        # the unit is the APPELLANT'S OWN issues, the same split that keyed the issue cells.
        pts = [{"title": c["title"], "text": c.get("text", "")}
               for c in sr.split_claims(Path(a.points_md).read_text(encoding="utf-8", errors="replace"))]
        built = f"m2 bank_bridge: our issues ({Path(a.points_md).name}) answering a court paper"
    else:
        dr = importlib.import_module("pipeline.m2.draft_runner")     # the reply's OWN points (intro accusation + argument headings)
        pts = dr.adverse_points(Path(a.adverse).read_text(encoding="utf-8", errors="replace"))
        built = "m2 bank_bridge: adverse filing headings"
    if not pts:
        sys.exit(f"[bridge] 0 drafting points from {a.points_md or a.adverse} — nothing to answer; pass --points-md for a court paper")
    heads = [" ".join(p["title"].split()) for p in pts]
    matrix = {"slug": a.slug, "built_from": built, "doctrine": "zero-waiver",
              "briefs": {a.slug: {"doc": Path(a.adverse).name, "headings_found": heads, "propositions_asserted": []}}}
    (out / "matrix.json").write_text(json.dumps(matrix, indent=1, ensure_ascii=False), encoding="utf-8")
    # 2) BANK = the cells
    cells = [json.loads(l) for l in open(a.cells, encoding="utf-8") if l.strip()]
    m1_claims = {c["id"]: c for c in sr.split_claims(Path(a.cells_md).read_text(encoding="utf-8", errors="replace"))} if a.cells_md else {}
    pts = [{"title": h, "text": p.get("text", "")} for h, p in zip(heads, pts)]
    bank = cells_to_bank(cells, pts, adverse, m1_claims)
    if os.environ.get("M2_UNION_M0", "1") == "1":                         # m0's build_bank = the base; m2 = the enhancement
        try:
            base = R.build_bank(a.slug)
            print(f"[bridge] union with m0 build_bank: {len(base)} base entries", flush=True)
            bank = base + bank
        except Exception as e:
            print(f"[bridge] build_bank unavailable ({e}); cells only", flush=True)
    (out / "bank.json").write_text(json.dumps(bank, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"[bridge] {len(heads)} headings; {len(bank)} bank entries from {len(cells)} cells "
          f"(SETTLED {sum(1 for e in bank if e['tier']=='SETTLED')}, RECORD {sum(1 for e in bank if e['tier']=='RECORD')}, "
          f"structural {sum(1 for e in bank if 'answer_heading' in e)})", flush=True)
    # 3) THE PEN: m1 assemble (unchanged) → optional voice_pass (polish only; fail-closed)
    arg_path = out / "ARGUMENT_m1.md"
    R.assemble(str(out / "matrix.json"), str(out / "bank.json"), str(arg_path), ana_path=str(ROOT / "obsidian_sync/cases" / a.slug / "CASE_FACTS.md"))
    arg = arg_path.read_text(encoding="utf-8")
    arg = re.sub(r"<!--.*?-->\n?", "", arg)                                   # m1's brief markers are work product
    body_by_n = {m.group(1): m.group(2) for m in re.finditer(r"\*\*(\d+)\. .*?\*\*(.*?)(?=\n\*\*\d+\. |\n\[\^|\Z)", arg, re.S)}
    def _fn(m):                                                             # footnote: each authority once, ONLY if the point names it
        n = re.match(r"\[\^(\d+)\]", m.group(1)).group(1); body = body_by_n.get(n, arg).lower()
        parts = [x.strip() for x in m.group(2).split(";")]
        keep = []
        for x in dict.fromkeys(parts):
            if not x or not _is_cite(x): continue
            key = (x.split(" v. ")[0].split()[-1] if " v. " in x else re.sub(r"[^a-z0-9§.]", "", x.lower())[:14]).lower().strip("’'.,")
            if key and key in body: keep.append(x)
        return m.group(1) + "; ".join(keep)
    arg = re.sub(r"(\[\^\d+\]: )(.+)", _fn, arg)
    arg = re.sub(r"(?m)^\[\^\d+\]: *\n", "", arg)
    sents, seen = [], set()                                                 # duplicated sentence within a point
    for para in arg.split("\n"):
        if para.startswith("**"):
            kept, seen = [], set()
            for st in R._split_sents(para):
                k = st.strip().lower()[:90]
                if k in seen: continue
                seen.add(k); kept.append(st.strip())
            para = " ".join(kept)
        sents.append(para)
    arg = "\n".join(sents)
    arg = arg.replace("# REBUTTAL MATRIX (zero-waiver: every point answered)\n", "")
    if a.voice:
        arg2, used = R.voice_pass(arg, llm_url=a.lane, court=a.court, filing_type=a.ftype)
        print(f"[bridge] voice_pass used_llm={used}", flush=True); arg = arg2 if used else arg
    (out / "ARGUMENT_m2.md").write_text("## ARGUMENT\n\n" + arg.strip() + "\n", encoding="utf-8")
    # 4) FULL FILING = m0's anatomy with this ARGUMENT
    sk = Path(a.skeleton).read_text(encoding="utf-8")
    i, j = sk.index("## ARGUMENT"), sk.index("## RELIEF")
    (out / "MOTION_m2.md").write_text(sk[:i] + "## ARGUMENT\n\n" + arg.strip() + "\n\n" + sk[j:], encoding="utf-8")
    print(f"[bridge] ARGUMENT {len(arg.split())} words; NOVEL (unanswered headings): {arg.count('[NOVEL')} → {out/'MOTION_m2.md'}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
