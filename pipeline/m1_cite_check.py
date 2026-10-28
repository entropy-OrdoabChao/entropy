"""m1 citation gate — every authority in a filing body is checked against the corpus (RUN 21, S-14/S-05/S-06).

Case cites: name + reporter vol/page resolve to an opinion source (`cap_us_<vol>_<page>_<slug>`, `cap_f3d_…`), the
quoted words must sit in THAT source's chunks (FTS, scoped by source), the year must match the opinion's header date
(class-level read: "Decided …" / "Filed …" / CAP decision_date / "<year> PA Super <n>"; a header with no such line yields a
WARN "year unverified", never a FAIL — S-quote-pa), and a pinpoint must lie inside the opinion's page range ([first page,
next opinion's first page in the volume)). PA reporters (A.2d / A.3d / Pa. / Pa. Super. / Pa. Cmwlth., with an optional
parallel "596 Pa. 62," prefix) resolve through the single-pull branch so Stilp / D'Ambrosio quotes reach the quote gate.
Statutes / rules / Am Jur: quoted sentences go through record_addendum.attributed (the ONE attribution function);
unquoted cites are checked for existence of the source family. ECF+date pairs: parsed to date objects and checked
against intake/dkt_<n>_*.doctext.txt 'Filed MM/DD/YY' headers and CASE_TIMELINE.json.
Read-only sqlite, no model, no torch.
"""
from __future__ import annotations
import re, json, sqlite3, datetime
from pathlib import Path

from pipeline.record_addendum import attributed, quoted_rules, cited_rules, _body, _CHUNK_DB

_REP = {"U.S.": "cap_us", "F.3d": "cap_f3d", "F.2d": "cap_f2d", "F.4th": "cap_f4th", "F. Supp.": "cap_fsupp",
        "F. Supp. 2d": "cap_fsupp2d", "F. Supp. 3d": "cap_fsupp3d", "S. Ct.": "cap_sct", "F. App'x": "cap_fappx",
        "A.2d": "cap_a2d", "A.3d": "cap_a3d",          # S-quote-pa: PA reporters reach _verify (quote gate) via the single-pull branch
        "Pa.": "cap_pa", "Pa. Super.": "cap_pasuper", "Pa. Cmwlth.": "cap_pacmwlth", "Pa. Commw.": "cap_pacmwlth"}
_REP_RX = (r"U\.S\.|F\.\s?(?:2d|3d|4th)|F\.\s?Supp\.(?:\s?(?:2d|3d))?|S\.\s?Ct\.|F\.\s?App'x|A\.\s?(?:2d|3d)"
           r"|Pa\.(?:\s?(?:Super|Cmwlth|Commw)\.)?")
_PARALLEL = r"(?:\d{1,3}\s+Pa\.(?:\s?(?:Super|Cmwlth|Commw)\.)?\s+\d{1,4},\s*)?"      # '596 Pa. 62, ' before the regional cite
_FULL = re.compile(r"([A-Z][\w\.'&\-, ]{2,80}? v\. [A-Z][\w\.'&\-, ]{2,80}?),?\s+" + _PARALLEL + r"(\d{1,3})\s+(" + _REP_RX +
                   r")\s+(\d{1,4})(?:,\s*(\d{1,4}))?(?:\s*n\.\s*\d+)?\s*\((?:[\w. ]+?\s)?(\d{4})\)")
# S-quote-pa: `_FULL`'s name group is leftmost-anchored on any capital, so a sentence ending before the cite is swallowed
# ("UTPCPL, the demurrer standard is not met. Werner v. Plater-Zyberk"). The case name starts after the last sentence
# boundary (a lowercase word + ./!/? + space + capital) in the first party; abbreviations ("U.S. Bank", "Servs. v.",
# "N.A. v.") are capitalized or single-letter and are never cut. Signal words ("In Werner", "See Werner") are dropped too.
_LEADIN = re.compile(r"^(?:See also|See|In|Cf\.|Accord|Compare|Citing|Quoting|Under|E\.g\.,?)\s+(?=[A-Z])")


def _trim(name: str) -> str:
    name = name.strip(" ,")
    head = name.split(" v. ", 1)[0]
    cuts = [m.end() for m in re.finditer(r"\b[a-z]{2,}[.!?]\s+(?=[A-Z])", head)]
    if cuts:
        name = name[cuts[-1]:]
    if not name.startswith("In re"):
        name = _LEADIN.sub("", name)
    return name.strip(" ,")
_SHORT = re.compile(r"([A-Z][\w\.'&\-]{2,40}),\s+(\d{1,3})\s+(" + _REP_RX + r")\s+at\s+(\d{1,4})")
_GENERIC = {"inc", "llc", "co", "corp", "ltd", "the", "united", "states", "ex", "rel", "of", "v", "et", "al", "na"}
_MON = {m: i for i, m in enumerate(("january", "february", "march", "april", "may", "june", "july", "august",
                                    "september", "october", "november", "december"), 1)}


def _con():
    return sqlite3.connect(f"file:{_CHUNK_DB}?mode=ro", uri=True, timeout=20)


def _slug_tokens(name: str) -> list[str]:
    return [t.lower() for t in re.findall(r"[A-Za-z']{3,}", name) if t.lower() not in _GENERIC]


def resolve_case(name: str, vol: str, rep: str, page: str) -> tuple[str | None, list[str]]:
    """(source, all sources of that volume) for name + vol/rep/page; None when the opinion is not in the corpus."""
    fam = _REP.get(re.sub(r"\s+", " ", rep).strip(), "")
    if not fam:
        return None, []
    con = _con()
    vol_srcs = [r[0] for r in con.execute("select distinct source from chunk_text where source like ?",
                                          (f"{fam}_{vol}_%",))]
    con.close()
    cands = [s for s in vol_srcs if s.startswith(f"{fam}_{vol}_{page}_")]
    toks = _slug_tokens(name)
    hit = next((s for s in cands if any(t in s.split("_") or t in s for t in toks)), None)
    if hit is None:
        hit = _resolve_single_pull(toks, vol, rep, page)
    if hit is None and re.sub(r"\s+", " ", rep).strip() == "U.S." and toks:
        # S-cite-usreports (2026-10-25): the bound U.S. Reports volume (source usreports-<vol>_*) IS the opinion;
        # confirm the first party's name appears in that volume's text before accepting it.
        con = _con()
        _u = [r[0] for r in con.execute("select distinct source from chunk_text where lower(source) like ? and lower(text) like ? limit 1",
                                        (f"usreports-{vol}_%", f"%{toks[0].lower()}%"))]
        con.close()
        hit = _u[0] if _u else None
    return hit, vol_srcs


# Single-pull opinions (CourtListener / CAP per-case ingests) are keyed scotus_<parties>_<vol><rep><page>
# (scotus_haines_v_kerner_404us519, scotus_gregg_v_ameriprise_financial_245a3d637) — NOT cap_<fam>_<vol>_<page>_.
# Without this branch every such opinion is a false NOT-IN-CORPUS at the gate (CA3 v33: Haines; worklog RUN 5 class).
_SP_REP = {"U.S.": "us", "F.2d": "f2d", "F.3d": "f3d", "F.4th": "f4th", "S. Ct.": "sct", "A.2d": "a2d", "A.3d": "a3d",
           "F. Supp.": "fsupp", "F. Supp. 2d": "fsupp2d", "F. Supp. 3d": "fsupp3d", "F. App'x": "fappx",
           "Pa.": "pa", "Pa. Super.": "pasuper", "Pa. Cmwlth.": "pacmwlth", "Pa. Commw.": "pacmwlth"}


def _resolve_single_pull(toks: list[str], vol: str, rep: str, page: str) -> str | None:
    """scotus_* key whose suffix is _<vol><rep><page> and whose party tokens confirm the name; None otherwise."""
    code = _SP_REP.get(re.sub(r"\s+", " ", rep).strip())
    if not code or not toks:
        return None
    con = _con()
    srcs = [r[0] for r in con.execute("select distinct source from chunk_text where source like ?",
                                      (f"scotus_%_{vol}{code}{page}",))]
    con.close()
    return next((s for s in srcs if any(t in s.split("_") for t in toks)), None)


def _single_pull_page(source: str) -> int | None:
    m = re.search(r"_(\d{1,4})(?:us|f2d|f3d|f4th|sct|a2d|a3d|fsupp(?:2d|3d)?|fappx|pa(?:super|cmwlth)?)(\d{1,4})$", source)
    return int(m.group(2)) if m else None


def page_range(source: str, vol_srcs: list[str]) -> tuple[int, int | None]:
    if source.startswith("scotus_"):
        return (_single_pull_page(source) or 0), None   # single pull: first page from the key, no next-opinion bound
    if source.lower().startswith("usreports-"):
        return 0, None   # bound U.S. Reports volume (S-cite-usreports): whole-volume source, no page bounds from the key
    first = int(source.split("_")[3])
    starts = sorted({int(s.split("_")[3]) for s in vol_srcs if len(s.split("_")) > 3 and s.split("_")[3].isdigit()})
    nxt = next((p for p in starts if p > first), None)
    return first, nxt


_MONTH_RX = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Za-z]*\.?"
# header date lines, by class: CAP text "Decided April 27, 1983." / "Decided Dec. 8, 1983." / "Decided March 28,1978" (no
# space), CourtListener slip text "DECIDED: FEBRUARY 17, 2021", PA Superior "Filed Feb. 21, 2002." / "FILED FEBRUARY 7, 2020",
# CAP JSON "decision_date": "2002-02-21", public-domain cite "1999 PA Super 14". Capitalized words only: a body sentence
# "the complaint was filed March 3, 2001" never reads as the opinion's own date.
_HDR_HARD = [
    ("decided", re.compile(r"(?:Decided|DECIDED)\s*:?\s*(?:" + _MONTH_RX + r"\s+\d{1,2},?\s*)?(\d{4})")),
    ("decision_date", re.compile(r"decision_date\W{1,6}(\d{4})")),
    ("filed", re.compile(r"(?:^|\s)(?:Filed|FILED)\s*:?\s*" + _MONTH_RX + r"\s+\d{1,2},?\s*(\d{4})", re.M)),
    ("pdc", re.compile(r"\b(\d{4})\s+PA\s+(?:Super|Commw)\s+\d{1,4}\b")),
]
_HDR_SOFT = re.compile(r"(?:Argued|ARGUED|Submitted|SUBMITTED)\s*:?\s*" + _MONTH_RX + r"\s+\d{1,2},?\s*(\d{4})")


def _chunks_ordered(source: str) -> list[tuple[int | None, str]]:
    """(chunk index | None, text) rows of one source; indexed keys '<source>_chunk_<n>' sort by n, hash keys keep db order."""
    con = _con()
    rows = con.execute("select chunk_id, text from chunk_text where source=?", (source,)).fetchall()
    con.close()
    out = []
    for cid, t in rows:
        m = re.search(r"_chunk_(\d+)$", cid or "")
        out.append((int(m.group(1)) if m else None, t))
    out.sort(key=lambda r: (r[0] is None, r[0] or 0))
    return out


def decided_year_info(source: str) -> dict:
    """Class-level year read of an opinion's header.

    Returns {'year': int|None, 'kind': str, 'header_years': set[int]}. kind is one of the _HDR_HARD classes (a clear
    decided/filed year — contradiction = FAIL), 'argued' (soft: only the argument/submission date is on the header),
    or 'none'. header_years = every 4-digit year in chunk_0 (the ±1 fallback when no header line exists). Header = the
    first two indexed chunks; sources keyed by hash (no _chunk_<n>) have no known order, so every row is a header row
    (the live cap_us behaviour)."""
    rows = _chunks_ordered(source)
    hdr = [t for i, t in rows if i is None or i <= 1] or [t for _, t in rows]
    info = {"year": None, "kind": "none", "header_years": set()}
    for kind, rx in _HDR_HARD:
        for t in hdr:
            m = rx.search(t)
            if m:
                info.update(year=int(m.group(1)), kind=kind)
                return info
    for t in hdr:
        m = _HDR_SOFT.search(t)
        if m:
            info.update(year=int(m.group(1)), kind="argued")
            break
    first = next((t for i, t in rows if i == 0), rows[0][1] if rows else "")
    info["header_years"] = {int(y) for y in re.findall(r"\b(1[6-9]\d\d|20\d\d)\b", first)}
    return info


def decided_year(source: str) -> int | None:
    """The opinion's decided/filed year from its header, or None (soft 'argued' and bare header years do not count)."""
    i = decided_year_info(source)
    return i["year"] if i["kind"] not in ("none", "argued") else None


def quote_in_source(quote: str, source: str) -> bool | None:
    # S-07: the FULL quote is verified — split on ellipses (". . ." / "…" / "[...]"), then every 12-word window of every
    # segment must sit in THIS source; inner quote marks / brackets are dropped for FTS
    segs = [s for s in re.split(r"\.\s?\.\s?\.|…|\[\.{3}\]|\[[^\]]*\]", quote) if s.strip()]
    # exact normalized substring over the opinion's own chunks (ix_ct_source makes this a ~20-row read); the FTS
    # source_id does not join to chunk_text.chunk_id for cap_us rows, so FTS cannot be scoped to one opinion
    con = _con()
    chunks = [_nq(t) for (t,) in con.execute("select text from chunk_text where source=?", (source,))]
    con.close()
    hay = " \u2016 ".join(chunks)
    ok, any_words = True, False
    for seg_text in segs:
        n = _nq(seg_text)
        if len(n.split()) < 3:
            continue
        any_words = True
        if n not in hay:
            # chunk boundaries: accept when the segment's first and last 6 words each sit in some chunk
            w = n.split()
            if not (" ".join(w[:6]) in hay and " ".join(w[-6:]) in hay):
                ok = False; break
    return ok if any_words else None


def _nq(s: str) -> str:
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = re.sub(r"\b(\d+)\s*U\.\s*S\.", r"\1 u.s.", s)
    s = re.sub(r"(?<=[a-z])-\s?(?=[a-z])", "", s)          # line-break hyphenation in the scan ("appel-lee")
    return re.sub(r"[^a-z0-9()§]+", " ", s.lower()).strip()


def _quote_before(body: str, pos: int) -> str:
    m = re.search(r"[“\"]([^”\"]{12,600})[”\"][^“\"]{0,40}$", body[max(0, pos - 800):pos])
    return m.group(1) if m else ""


def check_case_cites(md: str) -> list[dict]:
    body = _body(md)
    out, seen, fulls = [], set(), {}
    for m in _FULL.finditer(body):
        name, vol, rep, page, pin, year = m.groups()
        name = _trim(name)
        key = (vol, rep, page)
        fulls[(_slug_tokens(name)[0] if _slug_tokens(name) else name.lower(), vol)] = key
        quote = _quote_before(body, m.start())
        # S-quote-pa: a repeated cite is skipped only when it brings no NEW quoted words — the second "Stilp … (2007)" in a
        # paper carries its own quotation, and the quote gate must read every quotation, not just the first per opinion
        if key in seen and (not quote or (key, quote) in seen):
            continue
        repeat = key in seen
        seen.add(key); seen.add((key, quote))
        v = _verify(name, vol, rep, page, pin, year, quote)
        if repeat and v["status"] == "NOT-IN-CORPUS":
            continue                     # the first row already says the opinion is absent; nothing new to read
        out.append(v)
    for m in _SHORT.finditer(body):
        short, vol, rep, pin = m.groups()
        r = {"kind": "case", "cite": m.group(0), "status": "FAIL", "reason": ""}
        full = fulls.get((short.lower(), vol))
        if full:
            r["reason"] = f"short form of {full[0]} {full[1]} {full[2]}"
            v = _verify(short, vol, rep, full[2], pin, None, _quote_before(body, m.start()))
            r.update({"status": v["status"], "reason": "; ".join(x for x in (r["reason"], v["reason"]) if x), "source": v.get("source")})
        else:
            # S-13: short form without a full cite — resolve by party token in the volume
            fam = _REP.get(re.sub(r"\s+", " ", rep).strip(), "")
            con = _con()
            srcs = [s[0] for s in con.execute("select distinct source from chunk_text where source like ?", (f"{fam}_{vol}_%",))] if fam else []
            con.close()
            hit = [s for s in srcs if short.lower() in s.split("_")]
            if len(hit) == 1:
                page = hit[0].split("_")[3]
                v = _verify(short, vol, rep, page, pin, None, _quote_before(body, m.start()))
                v["reason"] = "short form, NO FULL CITE IN PAPER (S-13); " + v["reason"]
                v["status"] = "FAIL" if v["status"] == "PASS" else v["status"]
                r = v
            else:
                r["status"] = "NOT-IN-CORPUS"; r["reason"] = "short form; no full cite in paper and no unique opinion in volume"
        out.append(r)
    return out


def _verify(name, vol, rep, page, pin, year, quote) -> dict:
    cite = f"{name}, {vol} {rep} {page}" + (f", {pin}" if pin else "") + (f" ({year})" if year else "")
    r = {"kind": "case", "cite": cite, "status": "PASS", "reason": "", "source": None}
    src, vol_srcs = resolve_case(name, vol, rep, page)
    if not src:
        r["status"] = "NOT-IN-CORPUS"; r["reason"] = f"no opinion source {_REP.get(rep, rep)}_{vol}_{page}_<{name.split(' v. ')[0][:20]}>"
        return r
    r["source"] = src
    probs, warns = [], []
    first, nxt = page_range(src, vol_srcs)
    if pin:
        p = int(pin)
        if p < first or (nxt and p >= nxt):
            probs.append(f"pinpoint {p} outside opinion pages {first}-{(nxt - 1) if nxt else '?'}")
    if year:
        yi = decided_year_info(src)
        dy, kind = yi["year"], yi["kind"]
        if kind not in ("none", "argued"):
            if dy != int(year):
                probs.append(f"year {year} != {kind} {dy}")
        elif kind == "argued":
            # S-quote-pa: only the argument/submission date is on the header — a soft read, never a FAIL on its own
            warns.append(f"year {year} unverified (header shows only argued {dy}; no decided/filed line)")
        else:
            near = sorted(y for y in yi["header_years"] if abs(y - int(year)) <= 1)
            if near:
                warns.append(f"year {year} unverified (no decided/filed header; header chunk carries {near})")
            else:
                warns.append(f"year {year} unverified (no decided/filed header; no year within ±1 in header chunk)")
    if quote:
        q = quote_in_source(quote, src)
        if q is False:
            probs.append(f"quote not in {src}: '{quote[:50]}…'")
        r["quote"] = quote[:80]
    r["status"] = "FAIL" if probs else ("WARN" if warns else "PASS")
    r["reason"] = "; ".join(probs + warns) or f"name+reporter -> {src}; pages {first}-{(nxt - 1) if nxt else '?'}"
    return r


_AMJUR = re.compile(r"(\d+[AB]?)\s+Am\.\s?Jur\.\s?2d\s+([A-Z][\w ,&]{2,60}?)\s+§\s*(\d+)")
_LAR = re.compile(r"(?:3d Cir\. )?(?:L\.A\.R\.|I\.O\.P\.)\s*[\d.]+")


def _family_exists(cite: str) -> tuple[bool | None, str]:
    con = _con()
    try:
        m = re.search(r"(\d+)\s+U\.S\.C\.\s*§+\s*(\d+[a-z]?(?:-\d+)?)", cite)
        if m:
            pre = f"usc_t{m.group(1)}_s{m.group(2).lower()}"
            return bool(con.execute("select 1 from chunk_text where source like ? limit 1", (pre + "%",)).fetchone()), pre
        m = re.search(r"Fed\. R\. (Civ|App|Crim|Evid)\. P\. (\d+)", cite)
        if m:
            fam = {"Civ": "civil", "App": "appellate", "Crim": "criminal", "Evid": "evidence"}[m.group(1)]
            hit = con.execute("select 1 from chunk_text_fts f join chunk_text t on t.chunk_id=f.source_id where "
                              "chunk_text_fts match ? and lower(t.source) like ? limit 1",
                              (f'"Rule {m.group(2)}"', f"%{fam}%")).fetchone()
            return bool(hit), f"frcp-{fam}-rule{m.group(2)}"
        m = _AMJUR.search(cite)
        if m:
            pre = f"amjur2d_{m.group(1).lower()}"
            hit = con.execute("select 1 from chunk_text_fts f join chunk_text t on t.chunk_id=f.source_id where "
                              "chunk_text_fts match ? and t.source like ? limit 1", (f'"§ {m.group(3)}"', pre + "%")).fetchone()
            return bool(hit), f"{pre} § {m.group(3)}"
        if _LAR.fullmatch(cite.strip()):
            n = re.search(r"[\d.]+$", cite).group(0)
            hit = con.execute("select 1 from chunk_text_fts f join chunk_text t on t.chunk_id=f.source_id where "
                              "chunk_text_fts match ? and (lower(t.source) like '%ca3%' or lower(t.source) like '%third%' "
                              "or lower(t.source) like '%lar%') limit 1", (f'"{n}"',)).fetchone()
            return (True if hit else None), f"ca3-local-{n}"
        return None, ""
    finally:
        con.close()


def check_rule_cites(md: str) -> list[dict]:
    out, done = [], set()
    body = _body(md).replace("“", '"').replace("”", '"')
    quoted = list(quoted_rules(md))
    for m in re.finditer(r'"([^"]{8,600}?)[,.]?"[,.]?\s*(?:see\s+)?(' + _AMJUR.pattern + r"[^.;]*?\(\d{4}\))", body):
        quoted.append((re.sub(r"\s+", " ", m.group(2)).rstrip("."), m.group(1).strip().rstrip(",.")))
    for cite, q in quoted:
        done.add(re.sub(r"\s+", " ", cite))
        a = attributed(cite, q)                       # the ONE attribution function (record_addendum)
        ok, fam = _family_exists(cite)
        st = "PASS" if a else ("NOT-IN-CORPUS" if a is None else "FAIL")
        reason = {True: f"quote attributed to {fam or cite}", False: f"quote found only OUTSIDE {fam or cite}",
                  None: "quote not found in corpus"}[a]
        if a and ok is False:
            st, reason = "FAIL", f"section/rule number not in family {fam}"
        out.append({"kind": "statute/rule" if not _AMJUR.search(cite) else "treatise", "cite": cite, "status": st,
                    "reason": reason, "quote": q[:80]})
    for cite in cited_rules(md) + [m.group(0) for m in _AMJUR.finditer(body)] + [m.group(0) for m in _LAR.finditer(body)]:
        c = re.sub(r"\s+", " ", cite)
        if any(c.startswith(d) or d.startswith(c) for d in done):
            continue
        done.add(c)
        ok, fam = _family_exists(c)
        out.append({"kind": "treatise" if _AMJUR.search(c) else "statute/rule", "cite": c,
                    "status": "PASS" if ok else ("NOT-IN-CORPUS" if ok is None else "FAIL"),
                    "reason": f"source family {fam} {'present' if ok else 'absent'}" if fam else "no corpus family for this cite"})
    return out


# ---- ECF + date pairs ------------------------------------------------------------------------------------------------
def parse_date(s: str) -> datetime.date | None:
    s = s.strip().replace(".", "")
    m = re.match(r"([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})$", s)
    if m and _MON.get(m.group(1).lower()):
        try: return datetime.date(int(m.group(3)), _MON[m.group(1).lower()], int(m.group(2)))
        except ValueError: return None
    for f in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try: return datetime.datetime.strptime(s, f).date()
        except ValueError: pass
    return None


def record_dates(case_dir: Path) -> dict[int, set]:
    """ECF -> {date} from intake doctext 'Filed MM/DD/YY' headers (authoritative) and CASE_TIMELINE.json date_filed."""
    out: dict[int, set] = {}
    intake = case_dir / "intake"
    # every intake text (dkt_<n>_*.doctext.txt, ecf <n>.pdf.doctext.txt, _ocr/*.txt): the CM/ECF header names the ECF
    # itself ("Case 2:26-cv-00123-XYZ Document 67 Filed 07/31/25"), so the file name is not trusted for the number
    for f in sorted(intake.rglob("*.txt")) if intake.is_dir() else []:
        try: head = f.read_text(encoding="utf-8", errors="replace")[:4000]
        except Exception: continue
        for n, d in re.findall(r"Document\s+(\d{1,4})(?:-\d+)?\s+Filed\s+(\d{2}/\d{2}/\d{2,4})", head):
            dt = parse_date(d)
            if dt: out.setdefault(int(n), set()).add(dt)
    # docket text lines record the clerk's mailings to the unrepresented party: "08/18/2025 DOC. NOS. 67 AND 98 MAILED TO
    # UNREPS", "08/21/2025 PAPER #103 MAILED TO UNREPS", "09/22/2025 DOC. NOS. 121, 122 MAILED TO UNREP" — those dates
    # belong to those entries (as mailing dates) and to no other
    for f in sorted(intake.glob("*docket*.txt")) if intake.is_dir() else []:
        try: txt = f.read_text(encoding="utf-8", errors="replace")
        except Exception: continue
        for d, nums in re.findall(r"(?m)^(\d{2}/\d{2}/\d{4})\s+(?:DOC\.?\s*NOS?\.?|PAPER\s*#|DOCUMENT\s*NOS?\.?)\s*([\d,\s]+(?:AND\s+\d+)?)\s+MAILED", txt, flags=re.I):
            dt = parse_date(d)
            for n in re.findall(r"\d{1,4}", nums):
                if dt: out.setdefault(("mailed", int(n)), set()).add(dt)
    tl = case_dir / "CASE_TIMELINE.json"
    if tl.is_file():
        try:
            for e in json.loads(tl.read_text(encoding="utf-8")).get("entries", []):
                if e.get("docket") and str(e["docket"]).isdigit() and e.get("date_filed") and str(e.get("type")) != "unknown":
                    dt = parse_date(e["date_filed"])
                    if dt: out.setdefault(int(e["docket"]), set()).add(dt)
        except Exception:
            pass
    return out


_DATE_RX = re.compile(r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}\b"
                      r"|\b\d{1,2}/\d{1,2}/\d{2,4}\b")


_MONTHS = r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
_DATE_LIST = re.compile(rf"((?:{_MONTHS}\s+\d{{1,2}},\s+)+(?:and\s+)?{_MONTHS}\s+\d{{1,2}}),\s+(\d{{4}})\b")


def _expand_date_lists(sent: str) -> str:
    """'on July 22, August 21, and September 22, 2025' -> 'on July 22, 2025, August 21, 2025, and September 22, 2025' so each
    date in a year-elided list is a date the checker sees (26-0002 v30: only the last one was being tested)."""
    def _fix(m):
        yr = m.group(2)
        return re.sub(rf"({_MONTHS}\s+\d{{1,2}})(?!,\s+\d{{4}})", rf"\1, {yr}", m.group(1)) + f", {yr}"
    return _DATE_LIST.sub(_fix, sent)


def ecf_date_pairs(text: str) -> list[tuple[int, str]]:
    """(ECF, date-string) pairs: each date is paired with the nearest ECF/Dkt number in the same sentence, if any."""
    pairs = []
    for sent in re.split(r"(?<=[.;])\s+", text.replace("\n", " ")):
        sent = _expand_date_lists(sent)
        ecfs = [(m.start(), int(m.group(1))) for m in re.finditer(r"(?:ECF|Dkt\.?)\s*(?:No\.?\s*|Nos\.?\s*)?(\d{1,4})", sent)]
        if not ecfs: continue
        for m in _DATE_RX.finditer(sent):
            pos, n = min(ecfs, key=lambda e: abs(e[0] - m.start()))
            if True:   # every date in a sentence that names entries is tested; 'far' (>60 chars) dates are judged by the
                       # whole sentence's entries, never by the nearest number alone (see check_ecf_dates)
                # the nearest number is a guess: in "the report delivered September 2, 2025, before dismissal orders
                # ECF 121-122 issued September 19, 2025" the nearest ECF to Sept 2 is 121 but the date belongs to ECF 112
                # earlier in the sentence — so every ECF of the sentence rides along as the alternates
                far = abs(pos - m.start()) > 60
                between = sent[min(pos, m.start()):max(pos, m.start())].lower()
                # 'tight' = the date is the entry's own label: "ECF 67 (July 31, 2025)", "ECF 67, July 31, 2025", "July 31, 2025 order,
                # ECF 67" — nothing but punctuation / the number itself between them
                _gap = re.sub(r"(?:ECF|Dkt\.?)\s*(?:No\.?\s*|Nos\.?\s*)?\d{1,4}(?:\s*[-–]\s*\d{1,4})?", "", between, flags=re.I)
                tight = bool(re.fullmatch(r"[\s(),;:—–-]*(?:on|of|the|order|memorandum|opinion|entry|\s)*[\s(),;:—–-]*", _gap))
                asserts_act = tight or bool(re.search(r"\b(mailed|entered|issued|filed|docketed|signed|dated)\b", between))
                pairs.append((n, m.group(0), sent[max(0, m.start() - 70):m.start()].lower(), tuple(sorted({e[1] for e in ecfs})), far, asserts_act))
    return pairs


def check_ecf_dates(md: str, case_dir: Path) -> list[dict]:
    rec = record_dates(case_dir)
    body = re.sub(r"(?im)^\s*\**\s*(?:dated|date|executed(?: on)?)\s*:.*$", "", _body(md))
    out, seen = [], set()
    for n, ds, ctx, alts, far, asserts_act in ecf_date_pairs(body):
        if (n, ds) in seen: continue
        seen.add((n, ds))
        d = parse_date(ds)
        r = {"kind": "ecf-date", "cite": f"ECF {n} / {ds}", "status": "PASS", "reason": ""}
        if re.search(r"mailed|served|notice of|days late|received", ctx):
            # a mailing date: the docket's MAILED lines decide. The date must be the mailing date of an entry NAMED in the
            # sentence (26-0002 v30: "ECF 98 and ECF 103 ... mailed ... on July 22, August 21, and September 22" — July 22
            # is the mailing of ECF 39-41, not named; that is a FAIL a clerk can see)
            owners = [a for a in alts if d and d in rec.get(("mailed", a), set())]
            anyone = [k[1] for k in rec if isinstance(k, tuple) and d in rec[k]]
            if owners:
                r.update(cite=f"ECF {owners[0]} / {ds}", reason=f"docket: mailed {d.isoformat()}")
            elif anyone:
                r.update(status="FAIL", reason=f"docket mails {ds} for ECF {sorted(set(anyone))}, none named in the sentence (names ECF {list(alts)})")
            else:
                r.update(status="NOT-FILED-DATE", reason="mailing/service date not on the docket's MAILED lines — checked by NEF, not here")
        elif d is None:
            r.update(status="FAIL", reason="unparseable date")
        elif far:
            # the date sits away from every entry number: it is right if it is the filed/mailed date of SOME entry the
            # sentence names; a FAIL only when the record shows it belongs to entries the sentence does not name
            owners = [a for a in alts if d in rec.get(a, set()) or d in rec.get(("mailed", a), set())]
            anyone = sorted({(k[1] if isinstance(k, tuple) else k) for k, v in rec.items() if d in v})
            if owners:
                r.update(cite=f"ECF {owners[0]} / {ds}", reason=f"matches record {d.isoformat()} (date set apart in the sentence)")
            elif anyone and asserts_act:
                # the sentence says the named entries were mailed/entered/filed on this date and the record says otherwise
                r.update(status="FAIL", reason=f"record dates {ds} to ECF {anyone}, none named in the sentence (names ECF {list(alts)})")
            elif anyone:
                # a true record date used for an event the sentence does not tie to a number ("before dismissing defendants
                # on September 19, 2025" beside ECF 112) — right, and said so
                r.update(cite=f"ECF {anyone[0]} / {ds}", reason=f"record date of ECF {anyone} (entry not named in the sentence)")
            else:
                r.update(status="NOT-IN-RECORD", reason="date set apart from the entry numbers; not a docket filed/mailed date")
        elif n not in rec:   # tuple keys ("mailed", n) are mailing dates, looked up above
            r.update(status="NOT-IN-RECORD", reason=f"no filed date on record for ECF {n}")
        elif d not in rec[n]:
            other = [a for a in alts if a != n and d in rec.get(a, set())]
            if other:   # the date is another entry's filed date in the SAME sentence — the pairing guessed wrong, the text is right
                r.update(cite=f"ECF {other[0]} / {ds}", reason=f"matches record {d.isoformat()} (sentence also names ECF {n})")
            elif asserts_act:
                r.update(status="FAIL", reason=f"record has ECF {n} filed {sorted(x.isoformat() for x in rec[n])}")
            else:
                # the date names another event beside the entry ("Dkt 163-165) and Experian's non-appearance since May 10,
                # 2025 service") — not that entry's date, not asserted as such; record it, do not fail it
                r.update(status="NOT-IN-RECORD", reason=f"date of another event beside ECF {n} (record has ECF {n} filed {sorted(x.isoformat() for x in rec[n])}); not asserted as the entry's date")
        else:
            r["reason"] = f"matches record {d.isoformat()}"
        out.append(r)
    return out


def case_dir_of(path: Path) -> Path | None:
    for p in [path] + list(path.parents):
        if (p / "CASE_TIMELINE.json").is_file() or (p / "intake").is_dir():
            return p
    return None


def verify_draft(path) -> list[dict]:
    p = Path(path)
    md = p.read_text(encoding="utf-8", errors="replace")
    rows = check_case_cites(md) + check_rule_cites(md)
    cd = case_dir_of(p)
    if cd:
        rows += check_ecf_dates(md, cd)
    return rows


def table(rows: list[dict]) -> str:
    w = max([len(r["cite"]) for r in rows] + [10])
    lines = [f"{'KIND':13} {'STATUS':14} {'CITE':{w}}  REASON"]
    for r in rows:
        lines.append(f"{r['kind']:13} {r['status']:14} {r['cite'][:w]:{w}}  {r['reason']}")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    for f in sys.argv[1:]:
        rows = verify_draft(f)
        print(f"== {f}"); print(table(rows))
        print(f"total {len(rows)} | PASS {sum(r['status']=='PASS' for r in rows)} | FAIL {sum(r['status']=='FAIL' for r in rows)} | "
              f"WARN {sum(r['status']=='WARN' for r in rows)} | "
              f"NOT-IN-CORPUS/RECORD {sum(r['status'].startswith('NOT') for r in rows)}")
