#!/usr/bin/env python3
"""cite_family — the DETERMINISTIC cite→family→source-key parser and the HEADER PIN test (S-prefetch-pin, 2026-11-13).

WHY (the defect this closes, backlog item 'PREFETCH/SKEPTIC — a cite is "resolved" when the pinned chunk merely MENTIONS it'):
room_prefetch pinned chunk e2f5a7004637a35d79f63e23434f644f for `24 C.F.R. § 203.431`.  That chunk is the TAIL of § 203.432 plus
the HEAD of § 203.433 — its only words about 203.431 are a cross-reference ("…until the notice required by § 203.431 is received
by HUD"). It was written to ground_pack.md as `GROUND+:` and every lane then quoted those words AS § 203.431.  Separately the
lanes cited `12 U.S.C. § 1748b` (Title 12 ch. 13 subch. VIII — ARMED SERVICES housing) as the FHA 203(b) authority and
`13 Pa.C.S. § 9308(e)` (UCC-9 attachment) for an MIP disclosure duty, and the skeptic graded both GROUNDED, because nothing in
the chain compared the TITLE/CHAPTER of the pinned source key against the cite the card named.

Two deterministic answers, no LLM, no network:
  parse(cite)   -> {family, title, part_or_chapter, section, source_key_prefixes[], section_key, pull_target, ...}
  header_matches(cite, chunk_text, source_key) -> {exact_header, mention_only, family_match, ...}

  exact_header  the chunk carries the cite's OWN SECTION HEADING at a boundary (start of text / line start / after a sentence
                end) in the form the corpus prints it — '§ 203.431 Sale of insured mortgage to approved mortgagee.' /
                '§ 1709. Insurance of mortgages' / '§ 3301. Person entitled to enforce instrument.' / 'Rule 1028. Preliminary
                Objections' — AND the heading is followed by BODY (>= MIN_BODY chars before the next section heading), so a
                TABLE OF CONTENTS run of headings is not mistaken for the operative text.
  mention_only  the cite's number appears, but never as its own heading: a cross-reference. NOT a resolution.
  family_match  the source key's family + title + part/chapter agree with the cite's.  12 U.S.C. § 1748b against a card naming
                24 C.F.R. part 203 is False; 13 Pa.C.S. § 3301 pinned on pa_cs_t13_ch93 is False (chapter 33 != chapter 93).

SOURCE-KEY SCHEMES ARE PROBED, NEVER GUESSED (chunk_text, 2026-11-13 — 10+ real keys per family in the sandbox REPORT.md):
  usc        usc_t12_s1709 · usc_t12_s1709a · usc_t12_s1709–1 · usc_t12_s1735f–10 (EN DASH) · usc_t12_s1748b · usc_t15_s1666i–2
             · usc_t12_s101 to 110 (range keys) · uscode-2023-title26 · uscode-2024-title12
  cfr        cfr_26_16050p · cfr_12_1024_servicing_error_info · cfr_12_part_1024 · CFR-2024-title24-vol2 ·
             cfr-2024-title24-vol2_1 (BOTH cases exist; sqlite range compare is case-sensitive) · ECFR-title24 · ECFR-title24_1 ·
             CFR-2014-title12-vol1-sec21-11.  NOTE: there is NO cfr_24_203 shelf — 24 C.F.R. part 203 lives inside the
             volume/eCFR dumps, which is exactly why a part-level key can never stand in for a section-level pin.
  pa_cs      pa_cs_t13 · pa_cs_t13_ch00 · pa_cs_t13_ch33 · pa_cs_t13_ch93 · pa_cs_t13_ch911 · pa_cs_t13_ch11 …
  pa_code    pa_code_t231_ch1019 · pa_code_t231_ch1028 · pa_code_t231_ch1035 · pa_code_t210_ch19 · pa_code_t210_ch25 …
  fed rules  frcp_rule_12 · frap_rule_28 · fre_rule_403 …
  phila      phila_cr_1028c_preliminary_objections · phila_cr_10281_notice_to_defend_form_contents · phila_cr_1018_caption …
  pa_ps      pa_73ps_fceua · pa_73ps_utpcpl
  case       family='case', source_key_prefixes=[] — a reporter cite has no statute key; the party/volume guard
             (fetch_bot._focus, room_prefetch.key_in_source) governs those, unchanged.
"""
from __future__ import annotations
import re
_WS = re.compile(r"\s+")   # hoisted: backslash inside an f-string expression is a SyntaxError on Python 3.11 (MCP server venv)

MIN_BODY = 120          # chars of body that must follow a heading before the next heading — the TOC test, measured on the
                        # part-203 table of contents (gaps ~40-60 chars) vs § 203.431's own body (>600 chars)
DASHES = "-–—"

# ---------------------------------------------------------------- cite patterns (deterministic; order = specificity)
_RX_USC = re.compile(r"\b(\d{1,2})\s+U\.?\s?S\.?\s?C\.?(?:A\.?)?\s*(?:§+\s*)?(\d+[a-zA-Z]?(?:[-–—]\d+[a-zA-Z]?)?)\b", re.I)
_RX_CFR = re.compile(r"\b(\d{1,2})\s+C\.?\s?F\.?\s?R\.?\s*(?:§+\s*)?(\d{1,4}\.\d+[A-Za-z]?(?:[-–—]\d+[A-Za-z]?)?|\d{1,4})\b", re.I)
_RX_PACS = re.compile(r"\b(\d{1,2})\s+Pa\.?\s*C\.?\s*S\.?(?:A\.?)?\s*(?:§+\s*)?(\d+(?:\.\d+)?)", re.I)
_RX_PS = re.compile(r"\b(\d{1,3})\s+P\.?\s?S\.?\s*§+\s*(\d+(?:[-.]\d+)*)", re.I)
_RX_PACODE = re.compile(r"\b(\d{1,3})\s+Pa\.?\s*Code\s*§+\s*(\d+(?:\.\d+)?)", re.I)
_RX_PARULE = re.compile(r"Pa\.\s*R\.\s*(C(?:iv)?|A|E|J\.?A|Crim)\.\s*P\.\s*(\d{1,4}(?:\.\d+)?)((?:\s*\([a-z0-9]+\))*)", re.I)
_RX_PHILA = re.compile(r"Phila\.?\s*(?:Civ\.?|Civil)\s*R\.?\s*\*?(\d{1,4}(?:\.\d+)?)((?:\s*\([a-z0-9]+\))*)", re.I)
_RX_FEDRULE = re.compile(r"Fed\.?\s*R\.?\s*(Civ|App|Evid|Crim|Bankr)\.?\s*P?\.?\s*(\d{1,4}(?:\.\d+)?)((?:\s*\([a-z0-9]+\))*)", re.I)
_RX_REPORTER = re.compile(r"\b(\d{1,4})\s+(U\.\s?S\.|S\.\s?Ct\.|F\.\s?(?:2d|3d|4th)|F\.\s?Supp\.?(?:\s?[23]d)?|A\.\s?[23]d|"
                          r"Pa\.(?:\s?(?:Super|Cmwlth|Commw)\.)?|L\.\s?Ed\.(?:\s?2d)?)\s+(\d{1,5})\b")

_PARULE_TITLE = {"c": ("pa_rcp", "231"), "civ": ("pa_rcp", "231"), "a": ("pa_rap", "210"), "e": ("pa_re", "225"),
                 "ja": ("pa_rja", "201"), "crim": ("pa_rcrimp", "234")}
_FEDRULE_FAM = {"civ": "frcp", "app": "frap", "evid": "fre", "crim": "frcrmp", "bankr": "frbp"}
_SECTION_FAMILIES = ("usc", "cfr", "pa_cs", "pa_code", "pa_ps")
_RULE_FAMILIES = ("pa_rcp", "pa_rap", "pa_re", "pa_rja", "pa_rcrimp", "frcp", "frap", "fre", "frcrmp", "frbp", "phila_civ_r")


def _dash_variants(s: str) -> list[str]:
    """'1735f-10' -> ['1735f-10','1735f–10','1735f—10'] — the corpus key and the printed heading use an EN DASH where the
    cite is typed with a hyphen (usc_t12_s1735f–10).  A hyphen-only compare reported that section's own shelf as ABSENT."""
    out = [s]
    for d in DASHES:
        out.append(re.sub(f"[{DASHES}]", d, s))
    return list(dict.fromkeys(out))


def _trunc(n: str) -> list[str]:
    """'1019' -> ['1019','101','10','1'] — the Pa. Code / Pa.R.A.P. chapter key ladder (pa_code_t231_ch1019, pa_code_t210_ch19)."""
    return [n[:i] for i in range(len(n), 0, -1)]


def parse(cite: str) -> dict:
    """ANY cite string -> {family, title, part_or_chapter, section, source_key_prefixes[], section_key, pull_target, kind, cite}.
    family='unknown' when nothing parses (the caller's own guard then decides; this function never guesses)."""
    c = _WS.sub( " ", (cite or "")).strip()
    base = {"cite": c, "family": "unknown", "title": "", "part_or_chapter": "", "section": "", "subsections": [],
            "kind": "unknown", "source_key_prefixes": [], "section_key": "", "pull_target": ""}
    if not c:
        return base
    subs = re.findall(r"\(([a-zA-Z0-9]{1,4})\)", c)

    m = _RX_PACODE.search(c)                                  # before Pa.C.S.: '231 Pa. Code § 1019' is a RULE shelf
    if m:
        t, sec = m.group(1), m.group(2)
        ch = sec.split(".")[0]
        fam = {"231": "pa_rcp", "210": "pa_rap", "225": "pa_re", "201": "pa_rja"}.get(t, "pa_code")
        return {**base, "family": "pa_code", "kind": "regulation", "title": t, "part_or_chapter": ch, "section": sec, "subsections": subs,
                "source_key_prefixes": [f"pa_code_t{t}_ch{x}" for x in _trunc(ch)] + [f"pa_code_t{t}"],
                "section_key": f"pa_code_t{t}_ch{ch}", "pull_target": f"pa_code:{t}:{sec}", "rule_family": fam}

    m = _RX_PACS.search(c)
    if m:
        t, sec = m.group(1), m.group(2)
        digits = sec.split(".")[0]
        ch = digits[:-2] if len(digits) > 2 else digits        # 3301 -> ch33, 9308 -> ch93, 5505 -> ch55 (probed real keys)
        pre = [f"pa_cs_t{t}_ch{ch}", f"pa_cs_t{t}_ch{digits[:3]}", f"pa_cs_t{t}_ch{digits}", f"pa_cs_t{t}"]
        return {**base, "family": "pa_cs", "kind": "statute", "title": t, "part_or_chapter": ch, "section": sec, "subsections": subs,
                "source_key_prefixes": list(dict.fromkeys(pre)), "section_key": f"pa_cs_t{t}_ch{ch}",
                "pull_target": f"pa_cs:{t}:{sec}", "marker": f"{t}c{sec.replace('.', '')}s"}

    m = _RX_USC.search(c)
    if m:
        t, sec = m.group(1), m.group(2).lower()
        keys = [f"usc_t{t}_s{v}" for v in _dash_variants(sec)]
        stem = re.sub(rf"[{DASHES}]\d+[a-z]?$", "", sec)
        keys += [f"usc_t{t}_s{stem}", f"uscode-2023-title{t}", f"uscode-2024-title{t}", f"usc_t{t}_s"]
        return {**base, "family": "usc", "kind": "statute", "title": t, "part_or_chapter": "", "section": sec, "subsections": subs,
                "source_key_prefixes": list(dict.fromkeys(keys)), "section_key": f"usc_t{t}_s{sec}",
                "pull_target": f"usc:{t}:{sec}"}

    m = _RX_CFR.search(c)
    if m:
        t, sec = m.group(1), m.group(2)
        part = sec.split(".")[0]
        norm = re.sub(r"[^a-z0-9]", "", sec.lower())           # 1.6050P-1 -> 16050p1 ; the ingest squashes punctuation
        keys = [f"cfr_{t}_{norm[:n]}" for n in range(len(norm), 3, -1)][:8]
        keys += [f"cfr_{t}_{part}", f"cfr_{t}_part_{part}", f"ecfr_{t}_{norm}"]
        for y in ("2024", "2023", "2025"):                      # BOTH cases: 'CFR-2024-title24-vol2' and 'cfr-2024-title24-vol2_1'
            keys += [f"CFR-{y}-title{t}-", f"cfr-{y}-title{t}-"]
        keys += [f"ECFR-title{t}", f"ecfr-title{t}"]
        return {**base, "family": "cfr", "kind": "regulation", "title": t, "part_or_chapter": part, "section": sec, "subsections": subs,
                "source_key_prefixes": list(dict.fromkeys(keys)), "section_key": f"cfr_{t}_{norm}",
                "pull_target": f"cfr:{t}:{sec}"}

    m = _RX_PS.search(c)
    if m:
        t, sec = m.group(1), m.group(2)
        return {**base, "family": "pa_ps", "kind": "statute", "title": t, "part_or_chapter": "", "section": sec, "subsections": subs,
                "source_key_prefixes": [f"pa_{t}ps_", "pa_statutes"], "section_key": f"pa_{t}ps_",
                "pull_target": f"pa_ps:{t}:{sec}"}

    m = _RX_PARULE.search(c)
    if m:
        fam, t = _PARULE_TITLE.get(re.sub(r"[^a-z]", "", m.group(1).lower()), ("pa_rcp", "231"))
        n = m.group(2)
        return {**base, "family": fam, "kind": "rule", "title": t, "part_or_chapter": n.split(".")[0], "section": n,
                "subsections": re.findall(r"\(([a-zA-Z0-9]{1,4})\)", m.group(3) or ""),
                "source_key_prefixes": [f"pa_code_t{t}_ch{x}" for x in _trunc(n.split('.')[0])] + [f"pa_code_t{t}"],
                "section_key": f"pa_code_t{t}_ch{n.split('.')[0]}", "pull_target": f"{fam}:{t}:{n}", "number": n}

    m = _RX_PHILA.search(c)
    if m:
        n = m.group(1); nod = n.replace(".", "")
        return {**base, "family": "phila_civ_r", "kind": "rule", "title": "phila", "part_or_chapter": nod, "section": n,
                "subsections": re.findall(r"\(([a-zA-Z0-9]{1,4})\)", m.group(2) or ""),
                "source_key_prefixes": [f"phila_cr_{nod}", f"phila_cr_{n}", "phila_cr_", "phila_"],
                "section_key": f"phila_cr_{nod}", "pull_target": f"phila_civ_r:phila:{n}", "number": n}

    m = _RX_FEDRULE.search(c)
    if m:
        fam = _FEDRULE_FAM.get(m.group(1).lower(), "frcp"); n = m.group(2)
        return {**base, "family": fam, "kind": "rule", "title": "", "part_or_chapter": "", "section": n,
                "subsections": re.findall(r"\(([a-zA-Z0-9]{1,4})\)", m.group(3) or ""),
                "source_key_prefixes": [f"{fam}_rule_{n}"], "section_key": f"{fam}_rule_{n}",
                "pull_target": f"{fam}::{n}", "number": n}

    m = _RX_REPORTER.search(c)
    if m:
        rep = re.sub(r"[^a-z0-9]", "", m.group(2).lower())
        return {**base, "family": "case", "kind": "case", "title": "", "part_or_chapter": rep, "section": "",
                "source_key_prefixes": [], "section_key": "", "pull_target": "",
                "volume": m.group(1), "reporter": rep, "page": m.group(3)}
    return base


# ---------------------------------------------------------------- source key -> family (the skeptic's drift test)
_KEY_RX = [
    ("usc", re.compile(r"^usc_t(\d{1,2})_s(.+)$", re.I), ("title", "section")),
    ("usc", re.compile(r"^uscode-\d{4}-title(\d{1,2})", re.I), ("title",)),
    ("cfr", re.compile(r"^cfr_(\d{1,2})_part_(\d{1,4})", re.I), ("title", "part_or_chapter")),
    ("cfr", re.compile(r"^cfr_(\d{1,2})_(\d{1,4})", re.I), ("title", "part_or_chapter")),
    ("cfr", re.compile(r"^e?cfr[-_](?:\d{4}[-_])?title[-_]?(\d{1,2})", re.I), ("title",)),
    ("pa_cs", re.compile(r"^pa_cs_t(\d{1,2})(?:_ch(\d{1,4}))?", re.I), ("title", "part_or_chapter")),
    ("pa_cs", re.compile(r"^pa_cons_stat_t(\d{1,2})", re.I), ("title",)),
    ("pa_code", re.compile(r"^pa_code_t(\d{1,3})_ch(\d{1,4})", re.I), ("title", "part_or_chapter")),
    ("pa_ps", re.compile(r"^pa_(\d{1,3})ps_", re.I), ("title",)),
    ("frcp", re.compile(r"^frcp_rule_(\d{1,4})", re.I), ("section",)),
    ("frap", re.compile(r"^frap_rule_(\d{1,4})", re.I), ("section",)),
    ("fre", re.compile(r"^fre_rule_(\d{1,4})", re.I), ("section",)),
    ("phila_civ_r", re.compile(r"^phila_cr_(\d{1,5})", re.I), ("part_or_chapter",)),
]
_PACODE_FAM = {"231": "pa_rcp", "210": "pa_rap", "225": "pa_re", "201": "pa_rja"}


def key_family(source_key: str) -> dict:
    """A REAL corpus source key -> {family, title, part_or_chapter, section} (probed schemes only; 'unknown' otherwise)."""
    s = (source_key or "").strip()
    out = {"family": "unknown", "title": "", "part_or_chapter": "", "section": "", "source_key": s}
    for fam, rx, fields in _KEY_RX:
        m = rx.match(s)
        if not m:
            continue
        got = {**out, "family": fam}
        for i, f in enumerate(fields):
            got[f] = m.group(i + 1) or ""
        if fam == "pa_code":
            got["rule_family"] = _PACODE_FAM.get(got["title"], "pa_code")
        return got
    return out


def _norm_sec(s: str) -> str:
    return re.sub(rf"[{DASHES}]", "-", (s or "").lower().strip())


def family_match(cite: str, source_key: str) -> dict:
    """Does the SOURCE KEY's family/title/part-or-chapter agree with the cite the card names?
    12 U.S.C. § 1748b vs a card naming 24 C.F.R. part 203 -> False.  13 Pa.C.S. § 3301 on pa_cs_t13_ch93 -> False (ch 33 != 93).
    An unparsed key cannot PROVE agreement, so it is False with why='key-family-unknown' (never a silent pass)."""
    p, k = parse(cite), key_family(source_key)
    if p["family"] == "unknown":
        return {"ok": False, "why": "cite-family-unknown", "cite_family": p["family"], "key_family": k["family"]}
    if p["family"] == "case":
        return {"ok": True, "why": "case cite — reporter/party guard governs, not a statute key", "cite_family": "case", "key_family": k["family"]}
    if k["family"] == "unknown":
        return {"ok": False, "why": "key-family-unknown", "cite_family": p["family"], "key_family": k["family"]}
    cf, kf = p["family"], k["family"]
    equiv = {"pa_rcp": "pa_code", "pa_rap": "pa_code", "pa_re": "pa_code", "pa_rja": "pa_code", "pa_code": "pa_code"}
    if equiv.get(cf, cf) != equiv.get(kf, kf):
        return {"ok": False, "why": f"family {cf} != key family {kf}", "cite_family": cf, "key_family": kf}
    if p["title"] and k["title"] and p["title"].lstrip("0") != k["title"].lstrip("0"):
        return {"ok": False, "why": f"title {p['title']} != key title {k['title']}", "cite_family": cf, "key_family": kf}
    if p["part_or_chapter"] and k["part_or_chapter"] and p["part_or_chapter"].lstrip("0") != k["part_or_chapter"].lstrip("0"):
        return {"ok": False, "why": f"part/chapter {p['part_or_chapter']} != key {k['part_or_chapter']}", "cite_family": cf, "key_family": kf}
    if cf in ("usc",) and k.get("section") and _norm_sec(p["section"]) != _norm_sec(k["section"]):
        # the title agrees, the section shelf does not — reported, but the HEADER test is what decides a pin
        return {"ok": True, "why": f"title ok; key section {k['section']} != cite section {p['section']}",
                "cite_family": cf, "key_family": kf, "section_key_match": False}
    return {"ok": True, "why": "family+title+part agree", "cite_family": cf, "key_family": kf,
            "section_key_match": bool(k.get("section")) and _norm_sec(p["section"]) == _norm_sec(k.get("section") or "")}


# ---------------------------------------------------------------- the HEADER PIN test
def header_re(p: dict):
    """The cite's OWN heading as the corpus prints it.  Section families REQUIRE the § sigil — the part-203 table of contents
    prints bare numbers ('203.427 Statute of limitations…') while the body prints '§ 203.431 Sale of insured mortgage…';
    requiring § is therefore the first TOC discriminator, and the body-gap test below is the second."""
    sec = str(p.get("section") or "")
    if not sec:
        return None
    if p["family"] in _RULE_FAMILIES:
        n = re.escape(str(p.get("number") or sec))
        return re.compile(rf"(?:Rule|R\.)\s+{n}(?:\.\d+)?\.?\s+[A-Z(\"]")
    pat = re.sub(rf"\\?[{DASHES}]", f"[{DASHES}]", re.escape(sec))
    return re.compile(rf"§+\s*{pat}\.?\s+[A-Z(\"]")


def any_heading_re(p: dict):
    """ANY heading of the same family — used to measure how much BODY follows the cite's heading (the TOC test)."""
    if p["family"] in _RULE_FAMILIES:
        return re.compile(r"(?:Rule|R\.)\s+\d{1,4}(?:\.\d+)?\.?\s+[A-Z(]")
    return re.compile(rf"§+\s*\d{{1,4}}(?:\.\d+)?[A-Za-z]?(?:[{DASHES}]\d+[a-z]?)?\.?\s+[A-Z(]")


def _boundary(text: str, i: int) -> bool:
    """A heading starts the chunk, a line, or a new sentence — not mid-sentence ('…required by § 203.431 is received by HUD')."""
    if i == 0:
        return True
    pre = text[max(0, i - 4):i]
    return bool(re.search(r"(?:\n|^)\s*$", pre) or re.search(r"[.!?;:’”\")\]]\s*$", pre))


def header_matches(cite: str, chunk_text: str, source_key: str = "") -> dict:
    """-> {exact_header, mention_only, family_match, header_index, header_text, body_chars, why, cite_family, key_family}

    exact_header: the cite's own heading at a boundary AND >= MIN_BODY chars of body before the next heading of that family
                  (or the chunk ends) — i.e. the OPERATIVE TEXT, not a table of contents and not a cross-reference.
    mention_only: the number appears, but no qualifying heading.  This is the state that used to be written as GROUND+.
    """
    t = re.sub(r"[ \t\u00a0]+", " ", chunk_text or "")
    p = parse(cite)
    fm = family_match(cite, source_key or "")
    out = {"exact_header": False, "mention_only": False, "family_match": bool(fm.get("ok")), "family_why": fm.get("why"),
           "header_index": -1, "header_text": "", "body_chars": 0, "why": "", "cite_family": p["family"],
           "key_family": fm.get("key_family"), "section_key_match": fm.get("section_key_match"), "pull_target": p.get("pull_target") or ""}
    if p["family"] in ("unknown", "case") or not p.get("section"):
        out["why"] = f"no section/number to header-test (family={p['family']})"
        return out
    hre, are = header_re(p), any_heading_re(p)
    sec = str(p.get("section") or "")
    sec_pat = re.sub(rf"\\?[{DASHES}]", f"[{DASHES}]", re.escape(sec))   # hoisted out of the rf-string: Python 3.11 (MCP venv) forbids a backslash inside an f-string expression
    lit = re.compile(rf"§*\s*{sec_pat}\b")
    best = None
    for m in hre.finditer(t):
        if not _boundary(t, m.start()):
            continue
        nxt = are.search(t, m.end())
        body = (nxt.start() if nxt else len(t)) - m.end()
        if best is None or body > best[1]:
            best = (m, body)
    if best:
        m, body = best
        out.update(header_index=m.start(), header_text=t[m.start():m.start() + 90].strip(), body_chars=body)
        if body >= MIN_BODY:
            out["exact_header"] = True
            out["why"] = f"heading at {m.start()} ({'chunk start' if m.start() == 0 else 'boundary'}), {body} body chars before the next heading"
            return out
        out["mention_only"] = True
        out["why"] = f"heading at {m.start()} but only {body} body chars before the next heading — table of contents, not the body"
        return out
    if lit.search(t):
        mm = lit.search(t)
        out["mention_only"] = True
        out["header_index"] = -1
        out["header_text"] = t[max(0, mm.start() - 30):mm.start() + 70].strip()
        out["why"] = "cite appears only inside running text (cross-reference), never as its own heading"
        return out
    out["why"] = "cite does not appear in this chunk at all"
    return out


def header_window(cite: str, chunk_text: str, win: int = 1200) -> str:
    """The excerpt a GROUND+ line may quote: it BEGINS at the cite's own heading ('§ 203.431 Sale of insured mortgage…').
    Returns '' when there is no qualifying heading — there is then nothing this cite may be quoted from."""
    t = chunk_text or ""
    p = parse(cite)
    hre = header_re(p)
    if not hre:
        return ""
    for m in hre.finditer(re.sub(r"[ \t\u00a0]+", " ", t)):
        if _boundary(re.sub(r"[ \t\u00a0]+", " ", t), m.start()):
            s = re.sub(r"[ \t\u00a0]+", " ", t)
            return s[m.start(): m.start() + win]
    return ""


def pick_pin(cite: str, candidates: list, text_of=None, win: int = 1200) -> dict:
    """THE PIN RULE, shared by fetch_bot.resolve and room_prefetch (one implementation, one answer).

    candidates: [{chunk_id, source, text, ...}] in the ladder's own order.  text_of(cand) -> FULL chunk text when the
    candidate's 'text' is already a window (the hits are truncated to 1500 chars by fetch_bot._hit, and a heading can sit
    past that cut — measured: § 203.431's heading is at offset 3534 of CFR-2024-title24-vol2's chunk).

    -> {pin_kind: exact_header|mention-only|none, pinned, pull_needed, pull_target, detail:[...]}
    """
    detail, exact, mention = [], [], []
    for h in candidates or []:
        full = None
        if text_of:
            try:
                full = text_of(h)
            except Exception:
                full = None
        txt = full or h.get("text") or ""
        hm = header_matches(cite, txt, h.get("source") or "")
        d = {"chunk_id": h.get("chunk_id"), "source": h.get("source"), **{k: hm[k] for k in
             ("exact_header", "mention_only", "family_match", "family_why", "header_index", "body_chars", "why")}}
        detail.append(d)
        if hm["exact_header"]:
            exact.append((h, txt, hm))
        elif hm["mention_only"]:
            mention.append((h, txt, hm))
    p = parse(cite)
    if exact:
        h, txt, hm = max(exact, key=lambda e: (bool(e[2]["family_match"]), e[2]["body_chars"]))
        pinned = {**h, "text": header_window(cite, txt, win) or txt, "pin_kind": "exact_header",
                  "header_text": hm["header_text"], "family_match": hm["family_match"]}
        return {"pin_kind": "exact_header", "pinned": pinned, "pull_needed": False, "pull_target": "", "detail": detail}
    if mention:
        return {"pin_kind": "mention-only", "pinned": None, "pull_needed": True,
                "pull_target": p.get("pull_target") or "", "detail": detail,
                "mention_chunks": [d["chunk_id"] for d in detail if d["mention_only"]][:5]}
    return {"pin_kind": "none", "pinned": None, "pull_needed": bool(p.get("pull_target")),
            "pull_target": p.get("pull_target") or "", "detail": detail}


def main(argv=None):
    import sys, json
    args = (argv if argv is not None else sys.argv[1:])
    if not args:
        print("usage: cite_family.py '<cite>' [chunk_text_file] [source_key]"); return 2
    cite = args[0]
    out = {"parse": parse(cite)}
    if len(args) > 1:
        txt = open(args[1], errors="ignore").read()
        out["header_matches"] = header_matches(cite, txt, args[2] if len(args) > 2 else "")
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
