#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pipeline/rules_index.py — cite-keyed RULE index (the airtight definitions-index pattern applied to
RULES). Crack-the-legal-code finding, 2026-07-08: the decisive procedural anchors (admission-by-silence,
waiver, findings duties, judicial notice, uncontested-grant) exist in the ingested corpus but are NOT
deterministically retrievable — semantic queries land on wrong-provenance chunks (IRS manual, mislabeled
dictionary text) and FTS returns noise. Definitions solved this with a headword index (defs_index.db);
rules get the same: cite -> verbatim rule text + source.

AGNOSTIC BY DESIGN: no hardcoded rule list and no hardcoded source list. The builder scans chunk_text
for RULE-SHAPED text (a rule/section heading followed by operative text) and derives each rule's SYSTEM
from markers IN THE TEXT/SOURCE themselves (Pa.R.C.P. / Pa.R.A.P. / Fed. R. Civ. P. / Local Rule /
C.F.R. / Pa.C.S. / U.S.C. ...). What the corpus holds is what the index holds; a lookup miss appends to
output/audit/needs_rule.log — the same auto-extending gap-discovery contract as needs_hook.log.

DB: ~/turbovec/rules_index.db (additive; the corpus itself is immutable and untouched).

CLI:  python3 -m pipeline.rules_index --build
      python3 -m pipeline.rules_index --lookup "Pa.R.C.P. 1029"
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import re, sqlite3, sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(_epkg()))

DB = str(_eroot() / "corpus/rules_index.db")
NEEDS_RULE = (_eroot() / "output/audit/needs_rule.log")

# system markers are DISCOVERED in text/source, listed here only as recognizers (regex -> canonical
# system tag). Adding a new system = the corpus containing its marker; unknown markers land as "other".
_SYSTEM_MARKERS = [
    (r"Pa\.?\s*R\.?\s*C\.?\s*P\.?|Pennsylvania Rules? of Civil Procedure|231 Pa\. Code", "pa.r.c.p."),
    (r"Pa\.?\s*R\.?\s*A\.?\s*P\.?|Pennsylvania Rules? of Appellate Procedure|210 Pa\. Code", "pa.r.a.p."),
    (r"Pa\.?\s*R\.?\s*E\.?\b|Pennsylvania Rules? of Evidence|225 Pa\. Code", "pa.r.e."),
    (r"Fed\.?\s*R\.?\s*Civ\.?\s*P\.?|Federal Rules? of Civil Procedure", "fed.r.civ.p."),
    (r"Fed\.?\s*R\.?\s*App\.?\s*P\.?|FRAP|Federal Rules? of Appellate Procedure", "fed.r.app.p."),
    (r"Fed\.?\s*R\.?\s*Evid\.?|Federal Rules? of Evidence|FRE\b", "fed.r.evid."),
    (r"Local\s+(?:Civil\s+)?Rule|Loc\.?\s*R\.?|L\.?\s*Civ\.?\s*R|LAR\b", "local.rule"),
    (r"\bC\.?F\.?R\.?\b|Code of Federal Regulations", "cfr"),
    (r"Pa\.?\s*C\.?\s*S\.?\b|Pennsylvania Consolidated Statutes", "pa.c.s."),
    (r"U\.?\s*S\.?\s*C\.?\b|United States Code", "usc"),
    (r"Pa\.?\s*O\.?\s*C\.?|Orphans.{0,3}Court", "pa.o.c."),
    (r"T\.?C\.?\s*Rule|Tax Court Rule", "tax.ct."),
]
# a rule-shaped heading: "Rule 1029. Denials..." / "Rule 7.1 Motion Practice" / "§ 1921. Legislative intent"
_HEAD = re.compile(r"(?:(?<=\n)|^)\s*(?:Rule|RULE|§|Section|Sec\.)\s+(\d+(?:\.\d+)*(?:\([a-z0-9]\))*[a-zA-Z]?)"
                   r"[.—–\-:\s]+([A-Z“\"(][^\n]{4,120})\n?")


def _system_of(text: str, source: str) -> str:
    blob = (source or "") + " " + (text or "")[:800]
    for pat, tag in _SYSTEM_MARKERS:
        if re.search(pat, blob):
            return tag
    return "other"


def _norm_num(num: str) -> str:
    return re.sub(r"\s+", "", (num or "").lower())


# source NAME encodes the rule for the per-rule dumps (the reliable signal — the whole source IS that rule)
_SRC_RULE = [
    (re.compile(r"(?:courtlistener_)?frcp_rule_(\d+)", re.I), "fed.r.civ.p."),
    (re.compile(r"(?:courtlistener_)?frap_rule_(\d+)", re.I), "fed.r.app.p."),
    (re.compile(r"(?:courtlistener_)?fre_rule_(\d+)", re.I), "fed.r.evid."),
]
_TOC_ROW = re.compile(r"\d+(?:\.\d+)?\.\s+[A-Z]")          # "1029. Denials." list entries
_HISTORY = re.compile(r"^\s*(?:The provisions of this Rule|Source\b|\(?Editor'?s Note|Official Note)")


# Advisory-committee notes / history prose masquerade as operative text (they quote "shall/may" while
# DISCUSSING a rule). They must NEVER outrank the operative rule text. Agnostic across systems: federal
# advisory-committee notes ("former Equity Rule"), PA official notes, reporter commentary.
_COMMENTARY = re.compile(r"advisory committee|former equity rule|notes of decisions|committee note|"
                         r"\bas amended\b|this rule is consistent|notes of advisory|explanatory comment|"
                         r"source note|history of rule", re.I)


def _is_toc_or_history(body: str) -> bool:
    if _HISTORY.match(body):
        return True
    if len(_TOC_ROW.findall(body[:400])) >= 3:            # 3+ "NNNN. Title." = a table of contents
        return True
    # history/source note density (adopted...effective...Pa.B.)
    if body.count("effective") + body.count("Pa.B.") >= 3 and "shall" not in body.lower():
        return True
    return False


def _operative_rank(body: str) -> int:
    """Signed adjustment: how much a chunk looks like OPERATIVE rule text vs commentary. Strongly negative for
    advisory-note prose; positive when it leads with real 'Rule N. Title. (a) Capitalized' subsection structure.
    So the index stores — and lookup prefers — the operative rule, never its committee notes, for ANY system."""
    lead = body[:900]
    adj = 0
    if _COMMENTARY.search(lead):
        adj -= 1_000_000
    if re.search(r"\([a-z]\)\s+[A-Z]", lead):             # a real '(a) Capitalized' subsection heading up top
        adj += 20_000
    return adj


def _src_rule(source: str):
    for rx, sysm in _SRC_RULE:
        m = rx.search(source or "")
        if m:
            return sysm, m.group(1)
    return None, None


def build_index(verbose: bool = True, limit: int | None = None) -> int:
    """Scan chunk_text for rule-shaped chunks and index (system, number) -> verbatim text. Streaming
    (fetchmany) so the 3M-row table never loads at once. Idempotent: DROP + rebuild."""
    from pipeline.case_comprehension import CHUNK_DB
    src = sqlite3.connect(str(CHUNK_DB))
    out = sqlite3.connect(DB)
    out.execute("DROP TABLE IF EXISTS rules")
    out.execute("CREATE TABLE rules (system TEXT, num TEXT, title TEXT, body TEXT, source TEXT, "
                "PRIMARY KEY (system, num, source))")
    cur = src.execute("SELECT text, source FROM chunk_text WHERE text LIKE '%Rule %' OR text LIKE '%§%'")
    n_chunks = n_rules = 0
    # per-rule-dump accumulator: source-name encodes the rule -> keep the LONGEST operative chunk as body
    src_best = {}   # (system, num) -> (best_len, body, source)
    while True:
        rows = cur.fetchmany(4000)
        if not rows:
            break
        for text, source in rows:
            n_chunks += 1
            t = text or ""
            # pa_consolidated_statutes encodes the CODE TITLE in a compact marker ("75c1734s" = 75 Pa.C.S.
            # § 1734). Title-scope the source ("pa_consolidated_title75") so lookup can disambiguate
            # 75 Pa.C.S. from 42 Pa.C.S. (parallel to USCODE-LII-titleN). Without this, every Pa.C.S. section
            # collided or tagged 'other' and could not be found. Agnostic — any title the corpus holds.
            _pacs_title = None
            _pacs_sec = None
            _src_low = (source or "").lower()
            _m_srcpa = re.search(r"pa_cons_stat_t0*(\d+)_s([0-9a-z_]+)", _src_low)
            if _m_srcpa:                        # scheme-1: title + section live IN the source name (reliable)
                _pacs_title = _m_srcpa.group(1)
                _pacs_sec = _m_srcpa.group(2).replace("_", ".")
            elif "pa_consolidated" in _src_low:  # flat scheme: TTcSSSS<status> marker (42c8542v = 42 Pa.C.S. §8542)
                _mk = re.search(r"\b(\d{1,3})c\d{2,}[a-z]\b", t)   # status letter is s/v/h, not always 's'
                if _mk:
                    _pacs_title = _mk.group(1)
            # (1) source-name-derived rule (reliable per-rule dumps: PA scheme-1, USCODE per-section, FRCP/FRAP/FRE)
            ssys, snum = _src_rule(source or "")
            _scope_title = _pacs_title or ""      # title slot for the key/source (Pa.C.S. or U.S.C. title)
            _eff = f"pa_consolidated_title{_pacs_title}" if _pacs_title else (source or "")[:80]
            if not ssys and _pacs_sec:            # PA Consolidated Statutes scheme-1 source IS this section
                ssys, snum = "pa.c.s.", _pacs_sec
            if not ssys:                           # USCODE per-section source: ...title<N>...sec<MMMM> (title-scoped)
                _m_usc = re.search(r"uscode-\d+-title(\d+)-.*-sec(\d+[a-z]?\d*)", _src_low)
                if _m_usc:
                    ssys, snum, _scope_title = "usc", _m_usc.group(2), _m_usc.group(1)
                else:
                    _m_usc2 = re.match(r"usc_t(\d+)_s([0-9a-z]+)", _src_low)   # clean USLM ingest scheme
                    if _m_usc2:
                        ssys, snum, _scope_title = "usc", _m_usc2.group(2), _m_usc2.group(1)
                    else:
                        _m_ps = re.match(r"pa_ps_t(\d+)_s([0-9_]+)", _src_low)  # PA Purdon's (40 P.S. UIPA)
                        if _m_ps:
                            ssys, snum, _scope_title = "p.s.", _m_ps.group(2).replace("_", "."), _m_ps.group(1)
            if ssys and snum:
                op = len(t) if any(w in t.lower() for w in ("shall", "must", " may ", "admitted",
                                                             "denied", "means", " is ", " are ")) else 0
                if op:
                    # Prefer the OPERATIVE rule chunk (leads with 'Rule N. (a) ...') over committee-note prose.
                    # Where the corpus only holds fragmented/commentary chunks for a rule (some long federal
                    # rules), the best operative chunk still wins; the verifier's subsection honesty guard then
                    # downgrades any residual mismatch to UNVERIFIED — never a false UNBACKED.
                    op += _operative_rank(t)
                    if t.lstrip()[:1] == "§":     # a multi-chunk section's FIRST chunk (leads with '§ N.') must
                        op += 500_000             # win over its continuations, so the body starts at the heading
                    # Title-scope the key so 75 Pa.C.S. §1712 ≠ 42 Pa.C.S. §1712 and 28 U.S.C. §2071 ≠ 5 U.S.C. §2071.
                    key = (ssys, _norm_num(snum), _scope_title)
                    if op > src_best.get(key, (-10**12,))[0]:
                        src_best[key] = (op, t[:12000], _eff)   # full rule (multi-subsection)
            # (2) heading-scan for multi-rule sources (consolidated files, pa_code chapters)
            for m in _HEAD.finditer(t):
                num, title = m.group(1), m.group(2).strip()
                nxt = _HEAD.search(t, m.end())
                body = t[m.start():nxt.start() if nxt else m.start() + 12000].strip()   # full rule, all subsections
                if len(body) < 120:            # heading with no operative text = citation-in-passing
                    continue
                if _is_toc_or_history(body):    # skip tables of contents + adoption/source notes
                    continue
                if _COMMENTARY.search(body[:900]) and not re.search(r"\([a-z]\)\s+[A-Z]", body[:900]):
                    continue                    # skip pure advisory-committee-note chunks (no operative subsection)
                system = _system_of(t, source)
                eff_source = (source or "")[:80]
                if _pacs_title:                         # Pa.C.S. section from a consolidated dump -> title-scope
                    system = "pa.c.s."
                    eff_source = f"pa_consolidated_title{_pacs_title}"
                try:
                    out.execute("INSERT OR IGNORE INTO rules VALUES (?,?,?,?,?)",
                                (system, _norm_num(num), title[:150], body[:12000], eff_source))
                    n_rules += 1
                except Exception:
                    continue
            # (3) flat PA consolidated dump: newlines were collapsed at ingest so the heading-scan misses the
            #     sections. The 'TTcSSSS<status>' marker (42c8542v) reliably gives the chunk's TITLE (42); the
            #     SECTION comes from the '§ NNNN. Heading' in the collapsed text (marker sits at a section's
            #     END, so aligning bodies to it is off-by-one). Title-from-marker + section-from-heading makes
            #     every PA section across ALL titles resolvable, not just Title 75's clean per-section scheme.
            if _src_low == "pa_consolidated_statutes":
                _mkt = re.search(r"\b(\d{1,3})c\d{2,}[a-z]\b", t)
                if _mkt:
                    _ttl = _mkt.group(1)
                    _heads = list(re.finditer(r"§\s*(\d+[A-Za-z]?(?:\.\d+)*)\.\s+([A-Z][^.]{3,90}?)\.", t))
                    for _hi, _hm in enumerate(_heads):
                        _sec = _hm.group(1)
                        _end = _heads[_hi + 1].start() if _hi + 1 < len(_heads) else min(len(t), _hm.start() + 12000)
                        _body = t[_hm.start():_end]
                        if len(_body) < 120:
                            continue
                        key = ("pa.c.s.", _norm_num(_sec), _ttl)
                        op = len(_body) + _operative_rank(_body)
                        if op > src_best.get(key, (-10**12,))[0]:
                            src_best[key] = (op, _body[:12000], f"pa_consolidated_title{_ttl}")
        if verbose and n_chunks % 200000 < 4000:
            print(f"  scanned ~{n_chunks} candidate chunks, {n_rules} rule rows", flush=True)
    # flush the source-name-derived rows (authoritative — whole source IS that rule)
    for (system, num, _ttl), (_ln, body, source) in src_best.items():
        try:
            out.execute("INSERT OR REPLACE INTO rules VALUES (?,?,?,?,?)",
                        (system, num, f"{system} Rule {num}"[:150], body, source))
            n_rules += 1
        except Exception:
            continue
    out.commit()
    total = out.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
    if verbose:
        print(f"rules_index: {total} rows from {n_chunks} candidate chunks -> {DB}")
    out.close(); src.close()
    return total


_CITE = re.compile(r"(?i)^\s*(?P<sys>[a-z. ]*?)\s*(?:Rule|R\.|§|Section|Sec\.)?\s*"
                   r"(?P<num>\d+(?:\.\d+)*(?:\([a-z0-9]\))*)\s*$")


def _slice_to_subsection(body: str, sub: str) -> str:
    """Return the operative slice of a rule body for a requested subsection (e.g. '(b)') — from that
    subsection's marker to the next same-level marker. This is why the full (untruncated) body must be stored:
    a cite like 'Pa.R.A.P. 1925(b)' must judge against the (b) text, not the (a) start. If the marker isn't
    found, return the full body (a superset is safer than a wrong slice)."""
    if not sub or not body:
        return body
    lab = sub.strip("() ").lower()
    if not lab:
        return body
    m = re.search(r"(?:^|[\s\n(])\(" + re.escape(lab) + r"\)\s", body, re.I)   # '(b) ' at a boundary
    if not m:
        return body
    start = m.start()
    nxt = re.search(r"[\s\n]\([a-z0-9]{1,3}\)\s", body[m.end():], re.I)          # next same-level subsection
    end = (m.end() + nxt.start()) if nxt else len(body)
    sliced = body[start:end].strip()
    # keep a short rule-title lead so the verifier still sees WHICH rule this is
    head = body[:80].split("(")[0].strip()
    return (head + " " + sliced) if head and head.lower() not in sliced.lower()[:80] else sliced


def lookup(cite: str, limit: int = 3) -> List[Dict[str, Any]]:
    """Deterministic cite lookup: 'Pa.R.C.P. 1029(b)' / 'FRE 201' / 'LR 7.1(c)' -> verbatim rule rows.
    System is matched from the cite's own markers; a bare number searches all systems. A miss logs to
    needs_rule.log (auto-extending gap discovery — the ingest target list writes itself)."""
    raw = (cite or "").strip()
    sys_tag = _system_of(raw, "")
    # C1+C2 FIX: parse the SECTION number AFTER the code marker for a TITLE-prefixed statute, not the leading
    # TITLE. '12 CFR 1024.38' -> 1024.38 (was '12'); '42 Pa.C.S. 5505' -> 5505; '15 U.S.C. § 1681n' -> 1681n.
    # For court rules the first number IS the rule number.
    _CODE_MK = r"(?:U\.?\s?S\.?\s?C\.?(?:\s?A\.?)?|C\.?\s?F\.?\s?R\.?|Pa\.?\s?C\.?\s?S\.?(?:\s?A\.?)?|\bP\.?\s?S\.?)"
    # Always try the code-marker parse first (a title-prefixed statute has its SECTION after the code, even
    # when _system_of returns 'other' for it, e.g. '73 P.S. § 201-9.2'); court rules fall to the first number.
    m = re.search(_CODE_MK + r"\s*§?\s*(\d+[A-Za-z]?(?:[.\-]\d+)*)(\([a-z0-9]\))?", raw, re.I)
    if not m:
        m = re.search(r"(\d+(?:\.\d+)*)(\([a-z0-9]\))?", raw)
    if not m:
        return []
    num = _norm_num(m.group(1))
    sub = _norm_num(m.group(1) + (m.group(2) or ""))
    _subreq = (m.group(2) or "")   # the requested subsection label, e.g. '(b)', for slicing the returned body
    # source classes that carry each system's OFFICIAL text (rank boost, discovered naming)
    _CLASS = {"pa.r.c.p.": "pa_code", "pa.r.a.p.": "pa_code", "pa.r.e.": "pa_code",
              "pa.c.s.": "pa_consolidated", "local.rule": "local_rules", "cfr": "cfr",
              "usc": "usc", "fed.r.civ.p.": "frcp", "fed.r.app.p.": "frap", "fed.r.evid.": "fre"}
    try:
        con = sqlite3.connect(DB)
        rows = []
        for key in (sub, num):
            # Float clean Cornell-LII rows to the front and widen the window: a section number (e.g. §5103,
            # §411) recurs across many U.S. Code titles' print rows, so a blind LIMIT could fetch 12 braided
            # rows and never surface the one clean LII row for rank() to pick. Ordering guarantees it is seen.
            q = ("SELECT system, num, title, body, source FROM rules WHERE num=? "
                 + ("AND system=? " if sys_tag != "other" else "")
                 + "ORDER BY CASE WHEN lower(source) LIKE 'uscode-lii-%' THEN 0 ELSE 1 END LIMIT 40")
            args = ([key, sys_tag] if sys_tag != "other" else [key])
            rows = con.execute(q, args).fetchall()
            if rows:
                break
        con.close()
        if not rows:
            fb = _corpus_fallback(sys_tag, m.group(1), sub)
            if fb:
                return fb
            _log_needs_rule(raw)
            return []
        cls = _CLASS.get(sys_tag, "")
        # TITLE-DISAMBIGUATION: a codified cite carries its TITLE before the code marker ('31 U.S.C. 3123',
        # '12 CFR 1024.38'). The index stores each title's section under a title-scoped source
        # (USCODE-2023-title31), so §3123 from Titles 7/26/31 coexist. Boost the row whose source matches the
        # cite's title so 31 U.S.C. 3123 (public-debt pledge) never resolves to 7 U.S.C. 3123 (Agriculture).
        # No-op for court rules (no leading code title) -> ranking there is byte-identical to before.
        _utm = re.search(r"(\d+)\s*(?:U\.?\s?S\.?\s?C|C\.?\s?F\.?\s?R|Pa\.?\s?C\.?\s?S)", raw, re.I)
        _utitle = _utm.group(1) if _utm else None
        def rank(r):
            body = r[3] or ""
            src = (r[4] or "").lower()
            toc_penalty = 1 if body.count("....") >= 2 else 0          # dot-leader TOC lines
            comm_penalty = 1 if _COMMENTARY.search(body[:900]) else 0  # advisory notes lose to operative text
            op_boost = 1 if re.search(r"\([a-z]\)\s+[A-Z]", body[:900]) else 0
            src_boost = 1 if (cls and cls in src) else 0
            # TIERED source ranking for codified cites, so every observed failure mode resolves cleanly:
            #   tier 0 = a dedicated clean domain ingest (fcra-*, federal-rules-*, pa_code_*, frcp/fre) —
            #            keeps 15 U.S.C. 1681n on its clean FCRA source, never displaced by raw print text;
            #   tier 1 = clean Cornell-LII USC text (USCODE-LII-titleN) whose title matches — the readable,
            #            single-column statute (31 U.S.C. 3123 -> the public-debt pledge in full prose);
            #   tier 2 = clean LII text, title not asserted in the cite;
            #   tier 3 = the raw two-column USCODE-2023 PRINT row whose title matches (fallback for a section
            #            not yet scraped clean) — still beats a mis-indexed encyclopedia row (cjs §61);
            #   tier 4 = everything else (wrong-title USCODE, encyclopedia/junk rows).
            _DEDICATED = ("fcra", "fdcpa", "federal-rules", "frcp", "frap", "fre_rule",
                          "pa_code", "pa_consolidated", "restyledmaster", "pa_rules", "pa_legal", "pa_ps_t")
            is_lii = src.startswith("uscode-lii-title")
            is_print = bool(re.match(r"uscode-(?:19|20)\d\d-title", src))   # any USCODE year (2011–2023), not just 2023
            titled = bool(_utitle)
            title_ok = bool((is_lii or is_print) and titled and ("title" + _utitle) in src)
            is_pacs_titled = src.startswith("pa_consolidated_title")
            pacs_flat = ("pa_consolidated" in src) and not is_pacs_titled   # untitled flat dump
            pacs_title_ok = is_pacs_titled and titled and ("title" + _utitle) in src
            pacs_title_wrong = is_pacs_titled and titled and ("title" + _utitle) not in src
            is_usc_clean = bool(re.match(r"usc_t\d+_s", src))               # clean USLM per-section ingest
            usc_clean_ok = is_usc_clean and titled and bool(re.match(r"usc_t" + re.escape(_utitle or "_") + r"_s", src))
            usc_clean_wrong = is_usc_clean and titled and not usc_clean_ok
            # A wrong-title USC row must NEVER beat a right-title one: '18 U.S.C. 8' must not resolve to a
            # Title 1 s8 row just because that row is clean. When the cite names a title, only title-matching
            # USC rows are "good"; a title MISMATCH is demoted below everything real (tier 5), so a real miss
            # falls through to _corpus_fallback / needs_rule instead of returning a confidently-wrong section.
            if pacs_title_wrong:
                tier = 5                                # right section, WRONG Pa.C.S. title -> below real rows
            elif pacs_title_ok or (is_pacs_titled and not titled):
                tier = 0                                # title-matched (or cite asserted no title) Pa.C.S. row
            elif pacs_flat and titled:
                tier = 3                                # untitled flat PA row: below the titled match, above junk
            elif usc_clean_wrong:
                tier = 5                                # clean USLM section, WRONG U.S.C. title -> below real rows
            elif usc_clean_ok or (is_usc_clean and not titled):
                tier = 0                                # clean per-section U.S. Code (uscode.house.gov USLM), title-matched
            elif any(p in src for p in _DEDICATED):
                tier = 0
            elif is_lii and title_ok:
                tier = 1
            elif is_print and title_ok:
                tier = 2
            elif is_lii and not titled:
                tier = 1
            elif is_print and not titled:
                tier = 2
            elif (is_lii or is_print) and titled:      # right section number, WRONG title -> demote hard
                tier = 5
            else:
                tier = 4                                # non-USC junk / encyclopedia
            return (tier, -src_boost, comm_penalty, toc_penalty, -op_boost, -len(body))
        ranked = sorted(rows, key=rank)
        clean = [r for r in ranked if rank(r)[0] < 5]      # drop wrong-title collisions (tier 5)
        if not clean:
            # only a confidently-WRONG-title section matched (e.g. '23 Pa.C.S. § 3301' colliding onto Title 75's
            # § 3301 driving law). A clean miss -> fallback/needs_rule beats returning the wrong statute as law.
            fb = _corpus_fallback(sys_tag, m.group(1), sub)
            if fb:
                return fb
            _log_needs_rule(raw)
            return []
        rows = clean[:limit]
        return [{"system": r[0], "num": r[1], "title": r[2],
                 "body": _slice_to_subsection(r[3] or "", _subreq), "source": r[4]} for r in rows]
    except Exception:
        return []


# source families that hold each rule system's official text (for the direct-corpus fallback)
# PA Code Title scheme = the authoritative enumeration blueprint (225=Evidence, 231=Civil Procedure,
# 210=Appellate, 234=Criminal Procedure). The corpus ingest did NOT use a uniform 'pa_code_tNNN' source
# name, so each family lists the REAL discovered source-name patterns (SQLite LIKE) in priority order. PA
# families exclude federal bleed downstream (a Pa.R.E. query must never resolve to Federal Rules of Evidence).
_FAMILY = {
    "pa.r.c.p.":    ["pa_code_t231", "%rules-of-civil-procedure%", "%civil-procedure%"],
    "pa.r.a.p.":    ["pa_code_t210", "%rules-of-appellate%", "%appellate-procedure%"],
    "pa.r.e.":      ["PA_rules-of-evidence", "restyledmasterevid", "pa_code_t225"],
    "pa.r.crim.p.": ["pa_code_t234", "%rules-of-criminal%"],
    "pa.o.c.":      ["pa_code_t231", "%orphans%"],
    "pa.c.s.":      ["pa_consolidated"],
    # FEDERAL families (data-driven from the real corpus source names). Federal queries exclude PA sources
    # downstream, so Fed.R.Evid. 201 resolves to federal-rules-of-evidence, never PA_rules-of-evidence.
    "fed.r.civ.p.": ["federal-rules-of-civil-procedure", "frcp_rule_", "%federal-rules-of-civil%"],
    "fed.r.evid.":  ["federal-rules-of-evidence", "%federal-rules-of-evidence%"],
    "fed.r.app.p.": ["federal-rules-of-appellate-procedure", "%federal-rules-of-appellate%"],
}


def _corpus_fallback(sys_tag: str, num: str, sub: str):
    """When the pre-built index misses (multi-rule chapter files split a rule's body across chunks, or a
    family like Pa.R.E. was ingested under a non-uniform source name), pull the operative chunk straight
    from chunk_text.db in the right source family. Tries each real source pattern for the family and, for a
    PA family, excludes federal-rule chunks so Pa.R.E. 201 never resolves to Federal Rules of Evidence 201.
    Returns index-shaped rows or []."""
    _is_statute = sys_tag in ("cfr", "usc", "pa.c.s.", "p.s.")
    fams = _FAMILY.get(sys_tag)
    if not fams and not _is_statute:
        return []
    if isinstance(fams, str):
        fams = [fams]
    _is_pa = sys_tag.startswith("pa")
    _is_fed = sys_tag.startswith("fed")
    try:
        from pipeline.case_comprehension import CHUNK_DB
        c = sqlite3.connect(str(CHUNK_DB))
        rows = []
        if _is_statute:
            # C1+C2 STATUTE BRANCH: §-codified statutes are written in the corpus as "§ {num}" (e.g.
            # "§ 1024.38", "23 Pa.C.S.A. § 5505"). Sources are messy, so probe the SECTION HEADER and keep
            # only chunks carrying the right CODE MARKER (a CFR query keeps CFR chunks, a USC query keeps USC,
            # never cross-namespace). Returns the operative section text, or []. This is what makes RESPA/TILA
            # (12 CFR 1024/1026), FCRA (15 U.S.C. 1681*) and 42 Pa.C.S. cites resolvable without a re-ingest.
            _MARKS = {"cfr": ["c.f.r", " cfr", "code of federal reg"],
                      "usc": ["u.s.c", "united states code"],
                      "pa.c.s.": ["pa.c.s", "pa. c.s", "consolidated statutes"],
                      "p.s.": ["p.s.", "p. s.", "purdon"]}
            marks = _MARKS.get(sys_tag, [])
            cand = c.execute("SELECT source, text FROM chunk_text WHERE text LIKE ? LIMIT 60",
                             (f"%§ {num}%",)).fetchall()

            def _ok(t):
                tl = (t or "").lower()[:1400]
                if not any(mk in tl for mk in marks):
                    return False
                if sys_tag in ("cfr", "usc") and ("pa.c.s" in tl or "purdon" in tl):
                    return False   # federal statute query must not resolve to a PA statute chunk
                if sys_tag in ("pa.c.s.", "p.s.") and ("u.s.c" in tl or "c.f.r" in tl):
                    return False   # PA statute query must not resolve to a federal chunk
                return True

            srows = [r for r in cand if _ok(r[1])]
            c.close()
            if not srows:
                return []
            srows.sort(key=lambda r: ((r[1] or "").find(f"§ {num}") if (r[1] or "").find(f"§ {num}") >= 0 else 10**6))
            best = srows[0]
            i = max(0, (best[1] or "").find(f"§ {num}"))
            return [{"system": sys_tag, "num": _norm_num(sub), "title": f"{sys_tag} § {num} (corpus)",
                     "body": best[1][i:i + 2000].strip(), "source": (best[0] or "")[:80] + " [fallback]"}]
        for fam in fams:
            patt = fam if "%" in fam else fam + "%"
            # prefer a chunk that NAMES the rule and carries operative language
            r = c.execute(
                "SELECT source, text FROM chunk_text WHERE source LIKE ? AND text LIKE ? "
                "AND (text LIKE '%shall%' OR text LIKE '%denied%' OR text LIKE '%admit%' "
                "OR text LIKE '%means%' OR text LIKE '%must%' OR text LIKE '%notice%') LIMIT 6",
                (patt, f"%Rule {num}.%")).fetchall()
            if not r:
                r = c.execute("SELECT source, text FROM chunk_text WHERE source LIKE ? AND text LIKE ? LIMIT 4",
                              (patt, f"%Rule {num}.%")).fetchall()
            if _is_pa:   # never let a PA rule query resolve to a federal rule chunk
                r = [x for x in r if "federal" not in (x[0] or "").lower()
                     and "fed. r." not in (x[1] or "")[:250].lower()]
            elif _is_fed:  # never let a FEDERAL rule query resolve to a PA rule chunk
                r = [x for x in r if not (x[0] or "").lower().startswith("pa")
                     and "pa_rules" not in (x[0] or "").lower()
                     and "pennsylvania" not in (x[1] or "")[:250].lower()]
            if r:
                rows = r
                break
        c.close()
        if not rows:
            return []
        # pick the row where 'Rule <num>.' is closest to the start (the rule's own section, not a cross-ref)
        def score(r):
            i = r[1].find(f"Rule {num}.")
            return i if i >= 0 else 10 ** 6
        best = sorted(rows, key=score)[0]
        i = max(0, best[1].find(f"Rule {num}."))
        body = best[1][i:i + 2000].strip()
        return [{"system": sys_tag, "num": _norm_num(sub), "title": f"{sys_tag} Rule {num} (corpus)",
                 "body": body, "source": (best[0] or "")[:80] + " [fallback]"}]
    except Exception:
        return []


def _log_needs_rule(cite: str) -> None:
    try:
        NEEDS_RULE.parent.mkdir(parents=True, exist_ok=True)
        from datetime import datetime as _dt
        recent = ""
        if NEEDS_RULE.exists():
            recent = "\n".join(NEEDS_RULE.read_text(encoding="utf-8").splitlines()[-50:])
        if f"cite={cite}" in recent:
            return
        with NEEDS_RULE.open("a", encoding="utf-8") as f:
            f.write(f"{_dt.now().isoformat(timespec='seconds')}  cite={cite}\n")
    except Exception:
        pass


if __name__ == "__main__":
    if "--build" in sys.argv:
        build_index()
    elif "--lookup" in sys.argv:
        q = sys.argv[sys.argv.index("--lookup") + 1]
        hits = lookup(q)
        if not hits:
            print(f"(no indexed rule for {q!r} — logged to needs_rule.log)")
        for h in hits:
            print(f"[{h['system']} {h['num']}] {h['title']}  ({h['source']})")
            print("   " + " ".join(h["body"].split())[:220])
    else:
        print(__doc__)
