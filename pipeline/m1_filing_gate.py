#!/usr/bin/env python3
"""m1_filing_gate.py — deterministic COURT-RULES COMPLIANCE GATE (m1 lane).

USER DIRECTIVE (2026-08-26): filings must match ALL court filing rules and
format. This gate runs BEFORE any draft enters a mailing batch:

  1. LIMITS  — word/page limits from generation.md_to_docx.COURT_RULES
               (FRAP 32(a)(7) for 3D_CIR incl. 13,000/6,500; EDPA pages).
  2. BLOCKS  — required structural blocks for the (court, filing_type) from
               the filing-requirements DB + COURT_RULES: caption, signature
               block, certificate of service, certificate of compliance
               (appellate, when word-limited), corporate disclosure (FRAP 26.1
               posture), tables (TOC/TOA for principal briefs).
  3. GLOBAL  — the DB's global rules that are draft-checkable: R2 (COS vs
               proof-of-service wording), R6 (no pre-certification of
               service), R7 (no Notice to Defend in federal).
  4. FORMAT  — renderer params (font/size/spacing/margins) confirmed against
               COURT_RULES so md_to_docx renders compliant by construction.
  5. SUBSTANCE — a rule-mandated section must also DISCHARGE its mandate: a
               heading over an empty body is the same defect as a missing
               heading (2026-09-27 run 8b: 0 assembled points -> empty
               ARGUMENT -> "PASS 0 findings"). The floor and the form probes
               come from the contract's own `requires` text (the rule verbatim
               from corpus), not from a per-court keyword table.

Output: verdict PASS / FAIL(reasons) + the exact renderer profile to use.
Never auto-fixes — reports, so the fix happens in the pipeline (no sidecar
mutations). Exit code 1 on FAIL so batch scripts can gate on it.
"""
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import json, os, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQ_DB = Path(str(_ehermes() / "skills/legal/filing-requirements-checker/data/filing_requirements.json"))

# S-gate-form-only (2026-10-25, user-locked): the filing gate checks COURT FORM ONLY -- caption / docket,
# rule-mandated sections (presence, floor, order), rule-mandated attachments ([PROPOSED] ORDER, VERIFICATION,
# CERTIFICATE OF SERVICE), signature block, word/page limits, scaffold + provenance leaks. CONTENT checks
# (date arithmetic, cite verification, cite-forum coherence, self-label, waiver coverage) belong to the m2
# skeptic, which already runs; carried here they failed court-compliant drafts for reasons the court never
# reads for. FORM_ONLY=True drops them from the default findings; the code stays so the skeptic (or an
# operator audit) can flip it. Never re-create these checks elsewhere -- flip the flag.
FORM_ONLY = True

def _court_rules(court):
    # aliases + forum-profile courts resolve through md_to_docx's one resolver (F2)
    from generation.md_to_docx import _court_rules_or_die
    try:
        return _court_rules_or_die(court) or {}
    except ValueError:
        return {}

# Profile keys are written by rule_requirements under the id the operator typed (e.g. "3D_CIR")
# while check() canonicalizes to "CA3". Look up by every alias so an existing contract is found.
_PROFILE_ALIASES = {"CA3": ("CA3", "3D_CIR"), "3D_CIR": ("3D_CIR", "CA3")}


def _prof_entry(prof, court):
    for k in _PROFILE_ALIASES.get(court, (court,)):
        if prof.get(k):
            return prof[k]
    return {}


def _mandatory_sections(court, kind):
    """The court's own rule-mandated sections, from the style profile (DATA).

    Populated by pipeline/rule_requirements.py, which reads the rule verbatim from corpus
    and anchors every derived section in that text. Empty list = this court declares no
    section contract, and the gate's other checks apply unchanged.
    """
    try:
        prof_path = _profiles_path()
        if not prof_path.is_file():
            return []
        prof = json.loads(prof_path.read_text(encoding="utf-8")) or {}
        return (_prof_entry(prof, court).get(kind) or {}).get("mandatory_sections") or []
    except Exception:
        return []


_ATT_STOP = {"the", "and", "of", "to", "for", "with", "all", "any", "such", "copies", "copy",
             "items", "item", "each", "shall", "which"}


def _att_tokens(label):
    """Significant word stems of a filer's label. Plural/singular fold ("EXHIBITS"→"exhibit")."""
    out = []
    for w in re.findall(r"[a-z0-9]+", label.lower()):
        if w in _ATT_STOP or len(w) < 3:
            continue
        out.append(w[:-1] if w.endswith("s") and len(w) > 4 else w)
    return out


def _attachment_evidenced(label, headings, sibling_names):
    """True when ONE heading in the draft, or ONE sibling file in the packet dir, carries
    EVERY significant word of the label (any alternative when the label says "X OR Y").
    'Any word matches' was the 2026-09-27 hole: "...PRELIMINARY OBJECTIONS" was satisfied by
    the opposition's own filename and a packet with zero exhibits passed."""
    alts = [a.strip() for a in re.split(r"\s+OR\s+", label, flags=re.I) if a.strip()] or [label]
    hay = [re.sub(r"[\[\]()*_]", " ", h).lower() for h in headings] + \
          [re.sub(r"[_\-.]", " ", n).lower() for n in sibling_names]
    for alt in alts:
        toks = _att_tokens(alt)
        if not toks:
            continue
        for h in hay:
            words = {w[:-1] if w.endswith("s") and len(w) > 4 else w
                     for w in re.findall(r"[a-z0-9]+", h)}
            if all(t in words for t in toks):
                return True
    return False


def _profiles_path():
    """WHERE the rule contract lives. DATA is not code: a sandboxed copy of this module sits at
    <sandbox>/pipeline/, so `Path(__file__).parent.parent/output/audit/...` resolves into the
    sandbox and the contract silently reads as EMPTY -- which surfaces as "NO RULE CONTRACT" on
    every state draft (2026-09-28: the engineer probe hit exactly this). Resolution order:
    M1_STYLE_PROFILES (explicit) -> the module's own tree -> the working tree (live repo).
    Live behaviour is unchanged: for pipeline/ the module tree IS the repo."""
    import os
    env = os.environ.get("M1_STYLE_PROFILES")
    if env:
        return Path(env)
    rel = Path("output") / "audit" / "m1_style_profiles.json"
    here = Path(__file__).resolve().parent.parent / rel
    if here.is_file():
        return here
    cwd = Path.cwd() / rel
    if cwd.is_file():
        return cwd
    return here          # non-existent: callers report it, never guess a contract


_MIN_SECTION_WORDS = 15   # default floor for a rule-mandated section; per-section override
                          # via the contract's own "min_words" (DATA, from rule_requirements).


def _heading_level(head_line):
    m = re.match(r"\s{0,3}(#{1,6})", head_line or "")
    return len(m.group(1)) if m else 1


def _section_body(text, m):
    """The BODY of the section whose heading matched at `m`: everything up to the next heading of
    the SAME OR HIGHER level. Subsections belong to their parent, so an ARGUMENT written entirely
    under '### Part I' headings still counts as argued."""
    lvl = _heading_level(m.group(0))
    nxt = len(text)
    for mm in re.finditer(r"(?m)^\s{0,3}(#{1,6})\s*\S", text):
        if mm.start() <= m.start():
            continue
        if len(mm.group(1)) <= lvl:
            nxt = mm.start()
            break
    body = text[m.end():nxt]
    body = re.sub(r"<!--.*?-->", " ", body, flags=re.S)
    return body


_CITE_RX = re.compile(
    r"\b\d+\s+(?:A\.\s?\d?d|Pa\.(?:\s?(?:Super|Cmwlth)\.)?|F\.\s?(?:Supp\.(?:\s?\d?d)?|App'x|\d?d)"
    r"|U\.S\.|S\.\s?Ct\.)"                      # reporter citation
    r"|\bPa\.\s?(?:R\.\s?C\.\s?P|R\.\s?A\.\s?P|C\.\s?S)"   # PA rule / statute
    r"|\bFed\.\s?R\.\s?(?:Civ|App|Evid)\.\s?P"             # federal rule
    r"|\b\d+\s+U\.S\.C\."                                   # federal statute
    r"|\u00a7\s?\d"                                         # section symbol + number
    r"|\bPhila\.\s?Civ\.\s?R", re.I)


def _mandate_probes(requires):
    """Discharge probes read OFF THE RULE'S OWN WORDS in the contract's `requires` text.

    This is NOT a per-court keyword table: the operative words come from the rule verbatim as
    `pipeline/rule_requirements.py` derived it from corpus, so a new forum whose rule says
    "in question form" or "citation of the authorities relied on" is covered with no code change.
    Deterministic = guardrail only; nothing here judges the QUALITY of the content."""
    r = (requires or "").lower()
    probes = []
    if "question form" in r or "yes or no" in r:
        probes.append(("stated in question form (rule's own words)",
                       lambda b: "?" in b))
    if "citation of the authorities" in r or "authorities relied on" in r:
        probes.append(("citation of the authorities relied on (rule's own words)",
                       lambda b: bool(_CITE_RX.search(b))))
    return probes


def _court_examined(court):
    """True when the rule organ has written ANY profile entry for this court -- i.e. its
    rules were read, even if they mandate no sections for this kind."""
    try:
        prof_path = _profiles_path()
        if not prof_path.is_file():
            return False
        prof = json.loads(prof_path.read_text(encoding="utf-8")) or {}
        ent = _prof_entry(prof, court)
        return any(isinstance(v, dict) and v for k, v in ent.items() if not k.startswith("_"))
    except Exception:
        return False


def _court_channel(court):
    """How this court receives filings (DATA: m1_style_profiles.json <court>._channel = efile|mail)."""
    try:
        prof = json.loads(_profiles_path().read_text(encoding="utf-8")) or {}
        return str((prof.get(court) or {}).get("_channel") or "")
    except Exception:
        return ""


def _mandatory_attachments(court, kind):
    """Documents the forum requires to be filed TOGETHER WITH this filing (DATA, from
    rule_requirements). Empty = the court declares none for this kind."""
    try:
        prof_path = _profiles_path()
        if not prof_path.is_file():
            return []
        prof = json.loads(prof_path.read_text(encoding="utf-8")) or {}
        _k = (kind or "").lower()
        _non_brief = any(t in _k for t in ("affidavit", "declaration", "verification", "sworn",
                                           "certificate", "cover_sheet", "coversheet", "ifp",
                                           "complaint"))   # a first pleading never takes a brief's attachments
        for k in ((kind,) if _non_brief else (kind, "brief_or_opposition")):
            v = ((prof.get(court) or {}).get(k) or {}).get("mandatory_attachments")
            if v:
                return v
        return []
    except Exception:
        return []


# ---------------------------------------------------------------------------------------------
# CLASS GUARD A — INTERNAL PROVENANCE TAGS (2026-10-24, CA3 26-0000/26-0002 v34 II.1: the body of the filing
# read '28 U.S.C. § 1446(b)(2)(A) [corpus:usc_t28_s1446_c0].'). The relay-room proposal form deliberately emits
# 'Rule: “<words>” <cite> [corpus:<chunk_id>]' -- legitimate UPSTREAM provenance. A court never reads it: the
# finisher/drafter STRIP every tag (keeping them in a sidecar), and this gate FAILS any body that still carries one.
# Covered: [corpus:…] [corpus: …] [doc] [doc:…] [official:…] [record:…] [source:…] [chunk:…] [NOT IN CORPUS],
# bare chunk ids ('amjur2d_32A_volume_32a#ffff418347786dab', '507bv#a7423581a5', 'usc_t28_s1446_c0').
PROVENANCE_TAG_RX = re.compile(
    r"\[\s*(?:corpus|doc|official|record|source|chunk|chunk_id)\s*(?::[^\]\n]*)?\]"
    r"|\[\s*NOT IN CORPUS\s*\]"
    r"|(?<![\w/])[A-Za-z0-9_.\-]{2,}#[0-9a-f]{8,}\b"
    r"|\b(?:usc|cfr|pa_code|frcp|frap|amjur2d|docvault|scotus|cap|courtlistener)_[a-z0-9]+(?:_[a-z0-9]+)+\b",
    re.I)


def strip_provenance_tags(md):
    """CLASS RULE: no internal provenance tag survives into text a court reads. Returns (clean_md, tags) where tags is
    a list of {tag, line, before} for the sidecar/provenance log. Orphan punctuation left by the removal is healed
    on the touched lines only ('§ 1446(b)(2)(A) .' -> '§ 1446(b)(2)(A).', '( )' -> '', doubled spaces -> one)."""
    out, tags = [], []
    for i, line in enumerate(md.split("\n"), 1):
        hits = list(PROVENANCE_TAG_RX.finditer(line))
        if not hits:
            out.append(line)
            continue
        for h in hits:
            tags.append({"tag": h.group(0), "line": i, "before": line.strip()[:240]})
        new = PROVENANCE_TAG_RX.sub("", line)
        new = re.sub(r"\(\s*\)", "", new)               # tag was the only thing inside parens
        new = re.sub(r"[ \t]+([.,;:)\]])", r"\1", new)  # '§ 1446(b)(2)(A) .' -> '§ 1446(b)(2)(A).'
        new = re.sub(r"([(\[])[ \t]+", r"\1", new)
        new = re.sub(r"[ \t]{2,}", " ", new).rstrip()
        out.append(new)
    return "\n".join(out), tags


# ---------------------------------------------------------------------------------------------
# CLASS GUARD B — DAY-COUNT ARITHMETIC (2026-10-24, CA3 26-0000/26-0002 v34 II.1: 'TransUnion removed alone on
# June 2, 2025, 38 days after service on all six defendants' -- service 05/10/2025, removal 06/02/2025 = 23 days;
# the 38 belonged to ECF 28 (06/17/2025) in CASE_FACTS and was transplanted onto the removal date). CLASS RULE:
# a count of days may appear in a filing only when it is reproduced from two dated record facts; the preferred
# form puts both dates on the page ('served May 10, 2025; removed June 2, 2025 -- 23 days').
_DC_MON = {m: i for i, m in enumerate(("january", "february", "march", "april", "may", "june", "july", "august",
                                       "september", "october", "november", "december"), 1)}
_DC_MON.update({k[:3]: v for k, v in list(_DC_MON.items())})
_DC_DATE_RX = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December|"
    r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.?\s+(\d{1,2}),?\s+(\d{4})\b"      # May 10, 2025
    r"|\b(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})\b"                                              # 05/10/2025, 5/10/25
    r"|\b(\d{4})-(\d{2})-(\d{2})\b"                                                        # 2025-05-10
    r"|\b(\d{1,2})-([A-Z]{3})-(\d{4})\b")                                                  # 02-JUN-2025 (CCP docket)
_DC_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
             "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
             "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
             "sixty": 60, "ninety": 90}
_DC_NUM = r"(\d{1,4}|" + "|".join(_DC_WORDS) + r")"
_DC_COUNT_RX = re.compile(
    r"(?i)\b(within\s+)?" + _DC_NUM + r"(?:\s*(?:to|-|–)\s*" + _DC_NUM + r")?"
    r"[\s-]+days?\b(?:\s+(after|before|from|following|later\s+than|prior\s+to|since|past|beyond|late))?")
_DC_ABBR = re.compile(r"(?:\b(?:v|No|Nos|Cir|Fed|Civ|App|Dkt|Ex|Exs|Id|Inc|Co|Corp|Ltd|R|P|U\.S|U\.S\.C|L\.A\.R|I\.O\.P|al|Mr|Ms|Dr|St|Supp)"
                      r"|\b[A-Z])\.$")
_DC_STOP = {"the", "and", "of", "to", "on", "in", "by", "all", "any", "its", "his", "her", "their", "that", "this",
            "with", "for", "was", "were", "had", "has", "been", "from", "after", "before", "following", "later",
            "than", "entry", "date", "day", "days", "filed", "filing", "court", "order", "ecf", "dkt", "no",
            "appellant", "plaintiff", "defendant", "defendants", "six", "each", "every"}
_DC_SCORE_STOP = {"the", "and", "was", "were", "with", "for", "that", "this", "its", "day", "days", "after", "before",
                  "from", "following", "later", "than", "alone"}
_DC_STEM_ALIASES = {"service": ("service", "served", "serve"), "served": ("service", "served", "serve"),
                    "removal": ("removal", "removed"), "removed": ("removal", "removed"),
                    "judgment": ("judgment",), "mailed": ("mailed", "mailing", "mail"), "mailing": ("mailed", "mailing", "mail"),
                    "notice": ("notice", "noticed"), "entered": ("entered", "entry"), "appeal": ("appeal", "appealed")}


def _dc_parse(m):
    import datetime as _dt
    try:
        if m.group(1):
            return _dt.date(int(m.group(3)), _DC_MON[m.group(1).lower()[:3]], int(m.group(2)))
        if m.group(4):
            y = int(m.group(6)); y = y + 2000 if y < 100 else y
            return _dt.date(y, int(m.group(4)), int(m.group(5)))
        if m.group(7):
            return _dt.date(int(m.group(7)), int(m.group(8)), int(m.group(9)))
        if m.group(10):
            return _dt.date(int(m.group(12)), _DC_MON[m.group(11).lower()], int(m.group(10)))
    except (ValueError, KeyError):
        return None
    return None


def _dc_dates_in(s):
    out = []
    for m in _DC_DATE_RX.finditer(s):
        d = _dc_parse(m)
        if d:
            out.append((d, m.group(0), m.start()))
    return out


def _dc_sentences(text):
    """Sentence split that keeps a citation whole ('28 U.S.C. § 1446', 'Fed. R. Civ. P. 55(a)')."""
    out = []
    for part in re.split(r"(?<=[.;!?])\s+(?=[A-Z*“\"(])|\n+", text.replace("\r", "")):
        part = part.strip()
        if not part:
            continue
        if out and (_DC_ABBR.search(out[-1]) or re.match(r"[A-Z][a-z]{1,6}\.(?:,?\s|$)", part)):
            out[-1] += " " + part
        else:
            out.append(part)
    return out


_DC_RECORD_CACHE = {}
_DC_INTAKE_NAME_HINTS = ("docket", "activity in case", "notice of removal", "timeline")


def _dc_record_texts(case_facts_md, intake_dir, max_files=400, max_bytes=2_000_000):
    """(label, text) for every record text the arithmetic may be reproduced from: CASE_FACTS.md plus the DOCKET
    text twins under intake/ (*.txt / *.md whose name says docket / CM-ECF 'Activity in Case' / notice of removal /
    timeline). Bounded + cached per process so the gate stays deterministic and fast on /mnt/c."""
    key = (str(case_facts_md or ""), str(intake_dir or ""))
    if key in _DC_RECORD_CACHE:
        return _DC_RECORD_CACHE[key]
    out = []
    try:
        if case_facts_md and Path(case_facts_md).is_file():
            out.append((Path(case_facts_md).name, Path(case_facts_md).read_text(encoding="utf-8", errors="replace")))
    except Exception:
        pass
    try:
        if intake_dir and Path(intake_dir).is_dir():
            n = 0
            for p in sorted(Path(intake_dir).rglob("*")):
                if p.suffix.lower() not in (".txt", ".md") or not p.is_file():
                    continue
                if not any(h in p.name.lower() for h in _DC_INTAKE_NAME_HINTS):
                    continue
                try:
                    if p.stat().st_size > max_bytes:
                        continue
                    out.append((p.name, p.read_text(encoding="utf-8", errors="replace")))
                except Exception:
                    continue
                n += 1
                if n >= max_files:
                    break
    except Exception:
        pass
    _DC_RECORD_CACHE[key] = out
    return out


def _dc_event_stems(phrase):
    toks = [w for w in re.findall(r"[a-z]{4,}", (phrase or "").lower()) if w not in _DC_STOP]
    stems = []
    for w in toks[:4]:
        stems.extend(_DC_STEM_ALIASES.get(w, (w[:6],)))
    return list(dict.fromkeys(stems))


def _dc_record_dates_for(stems, records, limit=200):
    """Dates on record lines that carry any of the event stems: [(date, label:lineno, line)]."""
    out = []
    if not stems:
        return out
    rx = re.compile("|".join(re.escape(s) for s in stems), re.I)
    for label, text in records:
        for ln, line in enumerate(text.split("\n"), 1):
            if not rx.search(line):
                continue
            for d, raw, _p in _dc_dates_in(line):
                out.append((d, f"{label}:{ln}", line.strip()[:160]))
                if len(out) >= limit:
                    return out
    return out


def day_count_check(body_md, case_facts_md=None, intake_dir=None):
    """CLASS RULE (gate): every 'N days after/before/from/following <event>' in the body must be reproducible --
    either both dates sit in the same sentence and differ by N, or the sentence's one date and a dated record
    fact for <event> (CASE_FACTS.md / intake docket OCR) differ by N. Quoted rule text ('within 30 days after')
    is not a record claim and is skipped; 'within N days' with no dates on the page is a deadline statement, not
    arithmetic, and is skipped. Returns a list of finding strings ('' findings = PASS)."""
    findings = []
    text = re.sub(r"<!--.*?-->", " ", body_md or "", flags=re.S)
    text = re.sub(r"[“\"][^”\"]{12,}[”\"]", " “…” ", text)      # a quoted rule is not an arithmetic claim
    records = None
    for sent in _dc_sentences(text):
        for m in _DC_COUNT_RX.finditer(sent):
            within = bool(m.group(1))
            n1 = _DC_WORDS.get(m.group(2).lower(), None) if not m.group(2).isdigit() else int(m.group(2))
            n2 = (_DC_WORDS.get(m.group(3).lower(), None) if not m.group(3).isdigit() else int(m.group(3))) if m.group(3) else None
            direction = (m.group(4) or "").lower()
            if n1 is None:
                continue
            lo, hi = (min(n1, n2), max(n1, n2)) if n2 is not None else (n1, n1)
            dates = _dc_dates_in(sent)
            claim = sent.strip()[:200]
            fits = lambda k: lo <= k <= hi if not within else k <= hi
            # (1) arithmetic on the page: two dates in the sentence
            if len(dates) >= 2:
                diffs = sorted({abs((a[0] - b[0]).days) for i, a in enumerate(dates) for b in dates[i + 1:]})
                if any(fits(k) for k in diffs):
                    continue
                findings.append(f"DAY-COUNT NOT REPRODUCED: '{m.group(0).strip()}' but the dates on the page "
                                f"({', '.join(d[1] for d in dates)}) differ by {diffs} day(s) -- in: {claim}")
                continue
            if within and not dates:
                continue                                        # deadline statement, not a record count
            if not direction and not dates:
                continue                                        # bare 'N days' with no event and no date
            if not dates:
                findings.append(f"DAY-COUNT NOT REPRODUCIBLE: '{m.group(0).strip()}' names no dated event on the "
                                f"page; state both dates (served <date>; removed <date> -- N days) -- in: {claim}")
                continue
            # (2) one date on the page: the other must be a dated record fact for the event named after the
            #     direction word ('38 days after SERVICE on all six defendants' -> record lines naming service)
            anchor = min(dates, key=lambda d: abs(d[2] - m.start()))
            tail = sent[m.end():m.end() + 80]
            stems = _dc_event_stems(tail)
            if not stems:
                head = sent[max(0, m.start() - 80):m.start()]
                stems = _dc_event_stems(head)
            if records is None:
                records = _dc_record_texts(case_facts_md, intake_dir)
            if not records:
                findings.append(f"DAY-COUNT UNCHECKED: '{m.group(0).strip()}' (anchor {anchor[1]}) -- no CASE_FACTS.md / "
                                f"intake record texts to reproduce it from -- in: {claim}")
                continue
            cands = _dc_record_dates_for(stems, records)
            match = [(d, src) for d, src, _l in cands if fits(abs((anchor[0] - d).days)) and d != anchor[0]]
            if match:
                continue
            # report the record's dated events for the stem, best-attested first: lines that share the most context
            # words with the claim ('service on all six defendants' -> CASE_FACTS 'affidavit of service (05/10/2025,
            # all six)'), CASE_FACTS (curated record) before raw docket OCR, then nearest count. The message then
            # says what the record actually gives ('2025-05-10 (CASE_FACTS.md:641) -> 23 days').
            _ctx = {w for w in re.findall(r"[a-z]{3,}", (sent[max(0, m.start() - 80):m.end() + 80]).lower())
                    if w not in _DC_SCORE_STOP}
            _byd = {}
            for d, src, _line in cands:
                if d == anchor[0]:
                    continue
                _sc = len(_ctx & set(re.findall(r"[a-z]{3,}", _line.lower()))) + (3 if src.startswith("CASE_FACTS") else 0)
                _e = _byd.setdefault(d, [0, src, _sc])
                _e[0] += 1
                if _sc > _e[2]:
                    _e[1], _e[2] = src, _sc
            seen = sorted(((abs((anchor[0] - d).days), d.isoformat(), src, cnt, sc) for d, (cnt, src, sc) in _byd.items()),
                          key=lambda t: (-t[4], -min(t[3], 5), t[0]))
            near = "; ".join(f"{d} ({src}) -> {k} days" for k, d, src, cnt, sc in seen[:4]) or "none"
            findings.append(f"DAY-COUNT NOT REPRODUCIBLE: '{m.group(0).strip()}' with anchor {anchor[1]} -- record dates for "
                            f"'{'/'.join(stems[:3])}' give {near}; {m.group(0).split()[0] if not within else hi} not reproducible "
                            f"from the record; state both dates -- in: {claim}")
    return findings


# ---------------------------------------------------------------------------------------------
# CLASS GUARD C — FORUM COHERENCE (S-m1-application-crosscase, 2026-10-24). CA3 26-0003 FRAP 26(b) motion
# (--court 3D_CIR) PASSED while its RELIEF SOUGHT was the phila_ccp_260100000 PA Superior Court prayer
# (Pa.R.A.P. 1935/1931/2185/1701, "received by the Prothonotary"), its preamble Pa.R.A.P. 123, its verification
# 18 Pa.C.S. § 4904 — another matter's paper under this caption. CLASS RULE: a filing's PROCEDURAL authority is
# the forum sovereign's own; a foreign court-rule / verification-statute / officer marker is a FAIL unless the
# STUB ITSELF cites that same provision (stub-cites exception: the filer's 1 Pa.C.S. § 1308 reservation in a
# federal signature is pleaded on purpose). DATA: court family -> foreign markers; a new forum is a row.
FORUM_COURT_FAMILY = {
    "FED": ("3D_CIR", "CA3", "EDPA", "SCOTUS", "MDPA", "WDPA", "DNJ"),
    "PA": ("PA_SUP", "PA_SUPER", "PA_SUPREME", "PA_CMWLTH", "PA_CP_PHILA", "PA_CP"),
}
# family -> [(label, regex of a FOREIGN marker)]; each match is one cite, normalized for the stub exception
FORUM_FOREIGN_MARKERS = {
    "FED": [
        ("Pa.R.A.P.", r"Pa\.\s?R\.\s?A\.\s?P\.\s*\d+(?:\.\d+)?(?:\([a-z0-9]+\))*"),
        ("Pa.R.C.P.", r"Pa\.\s?R\.\s?C\.\s?P\.\s*(?:No\.\s*)?\d+(?:\.\d+)?(?:\([a-z0-9]+\))*"),
        ("Pa.R.Crim.P.", r"Pa\.\s?R\.\s?Crim\.\s?P\.\s*\d+"),
        ("Pa.C.S.", r"\d+\s*Pa\.\s?C\.\s?S\.(?:A\.)?\s*§{0,2}\s*\d+(?:\.\d+)?(?:\([a-z0-9]+\))*"),
        ("Pa. Code", r"\d+\s*Pa\.\s?Code\s*§?\s*\d+(?:\.\d+)?"),
        ("Phila.Civ.R.", r"Phila\.\s?(?:Civ\.|Cty\.)\s?R\.\s*\*?\d+"),
        ("Prothonotary", r"\bProthonotary(?: of the (?:Superior|Supreme|Commonwealth) Court)?\b"),
    ],
    "PA": [
        ("Fed. R. App. P.", r"(?:Fed\.\s?R\.\s?App\.\s?P\.|\bFRAP)\s*\d+(?:\([a-z0-9]+\))*"),
        ("Fed. R. Civ. P.", r"(?:Fed\.\s?R\.\s?Civ\.\s?P\.|\bFRCP)\s*\d+(?:\([a-z0-9]+\))*"),
        ("3d Cir. L.A.R.", r"(?:3d\s*Cir\.\s*)?L\.\s?A\.\s?R\.\s*\d+(?:\.\d+)?"),
        ("E.D. Pa. L.R.", r"E\.\s?D\.\s?Pa\.\s*(?:Local\s*)?(?:Civ\.\s*)?R\.\s*\d+(?:\.\d+)?"),
        ("28 U.S.C. § 1746", r"28\s*U\.\s?S\.\s?C\.\s*§?\s*1746"),
    ],
}


# the filer's own reservation-of-rights cites, carried in the signature block in every forum (data, not code)
FORUM_SIGNATURE_RESERVATIONS = ("1 Pa.C.S. § 1308", "1 Pa.C.S. §1308", "1 Pa.C.S. 1308")


def forum_family_of(court):
    k = str(court or "").strip().upper().replace("-", "_").replace(" ", "_")
    for fam, ids in FORUM_COURT_FAMILY.items():
        if k in ids:
            return fam
    if k.startswith("PA_") or "PHILA" in k:
        return "PA"
    if "CIR" in k or "SCOTUS" in k or k.startswith(("CA", "ED", "MD", "WD", "ND", "SD")):
        return "FED"
    return None


def _fc_norm(cite):
    """One cite -> comparison key: case-folded, no spaces/§, trailing punctuation dropped."""
    return re.sub(r"[\s§.]+", "", str(cite)).lower().rstrip(";,:)")


def locate_stub(draft_path):
    """The m0 stub beside the draft: the sibling .md (not a *_DRAFT/_FILING/_M1_* artifact) whose stem prefixes the
    draft name and that carries a stub block (## RELIEF SHAPE / ## CONTROLLING AUTHORITY / # STUB). None if absent.
    Override: $M1_STUB_PATH."""
    env = os.environ.get("M1_STUB_PATH")
    if env and Path(env).exists():
        return Path(env)
    dp = Path(draft_path)
    if not dp.parent.is_dir():
        return None
    stem = re.sub(r"_(?:M1_)?(?:DRAFT|FILING)(?:_v\d+)?$", "", dp.stem)
    cands = []
    for p in dp.parent.glob("*.md"):
        if p.name == dp.name or re.search(r"_(?:DRAFT|FILING)\b|_M1_|QUARANTINE|_ADDENDUM", p.name):
            continue
        try:
            head = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if re.search(r"(?m)^##\s+(?:RELIEF SHAPE|CONTROLLING AUTHORITY)\b|^#\s+STUB\b", head):
            cands.append((0 if stem.startswith(p.stem) or p.stem.startswith(stem) else 1, len(p.stem), p))
    if not cands:
        return None
    cands.sort(key=lambda t: (t[0], -t[1]))
    return cands[0][2]


def forum_coherence_check(body_md, court, stub_text=None):
    """CLASS GUARD C. Returns findings: one per foreign procedural cite + line, unless the stub cites it."""
    fam = forum_family_of(court)
    if not fam or fam not in FORUM_FOREIGN_MARKERS:
        return []
    stub_keys = set()
    # 2026-10-24 data-level exemption: the filer's standing signature reservation (1 Pa.C.S. § 1308 — the
    # "sui juris" block the project's locked anatomy puts in EVERY filing, any forum) is the filer's own
    # reservation of rights, not an imported procedural rule; it never counts as a foreign-forum cite.
    for _ex in FORUM_SIGNATURE_RESERVATIONS:
        stub_keys.add(_fc_norm(_ex))
    if stub_text:
        for _lbl, _rx in FORUM_FOREIGN_MARKERS[fam]:
            for m in re.finditer(_rx, stub_text):
                stub_keys.add(_fc_norm(m.group(0)))
    out, seen = [], set()
    # the body a court reads: HTML comments out, nothing else excluded (a COS or signature cite counts too)
    text = re.sub(r"<!--.*?-->", "", body_md, flags=re.S)
    for i, line in enumerate(text.split("\n"), 1):
        for _lbl, _rx in FORUM_FOREIGN_MARKERS[fam]:
            for m in re.finditer(_rx, line):
                cite = m.group(0).strip()
                key = _fc_norm(cite)
                if key in stub_keys or (i, key) in seen:
                    continue
                seen.add((i, key))
                out.append(f"FORUM COHERENCE: foreign procedural authority in a {court} ({fam}) filing — "
                           f"'{cite}' [{_lbl}] at line {i}, not cited by the stub")
    return out


def _case_dir_above(path, max_up=8):
    p = Path(path).resolve()
    for q in [p] + list(p.parents)[:max_up]:
        if (q / "CASE_FACTS.md").is_file() or (q / "CASE_TIMELINE.json").is_file() or (q / "intake").is_dir():
            return q
    return None


def waiver_findings(text, orders_matrix, issues_md=None, addendum_json=None, addendum_md=None):
    """WAIVER check — reads the all-orders matrix (<ALL_ORDERS_UNITS.m2>/cells.jsonl) + OUR_ISSUES.md through
    record_addendum.issue_coverage (the ONE tabulation). An issue is NAMED when its number sits in a Point/sub-issue list
    (`Issue 3`, `Issues 2, 5 and 7`, `**3.**`, `(3)` at line start) OR >=3 distinctive title tokens share one sentence
    OR the addendum's Part V row carries it (the <stem>_ADDENDUM.md text twin / sidecar JSON beside the draft). An order is NAMED when `ECF n` appears in the body
    or in the addendum (Part II lists every order; Part V the uncovered ones). Silence = waiver = FAIL."""
    from pipeline import record_addendum as _ra
    mx = Path(orders_matrix)
    issues_md = Path(issues_md) if issues_md else mx.parent / "OUR_ISSUES.md"
    out = []
    if not (mx / "cells.jsonl").exists():   # stage 0 not finished: naming is still checked (issues + orders come from the files), departures are not
        out.append(f"WAIVER CHECK PARTIAL: no cells.jsonl in {mx} — issue/order NAMING checked from OUR_ISSUES.md + ALL_ORDERS_UNITS.md; "
                   "rule departures and order->issue ties await stage 0 (the all-orders discretion_closer matrix)")
    cov = _ra.issue_coverage(mx, issues_md, _ra._case_dir(mx) or mx, mx.parent.parent, append=False, verify=lambda q, c="": {"status": "skip"})
    body = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    low = body.lower()
    sents = [set(re.findall(r"[a-z][a-z0-9'’\-]{3,}", s)) for s in re.split(r"(?<=[.;:])\s+|\n", low)]
    nums = set()
    for m in re.finditer(r"\bissues?\s*(?:nos?\.?|numbers?|#)?\s*((?:\d{1,2}\s*(?:,|and|&|/|–|-|to)?\s*)+)", body, re.I):
        a, _b = _ra.issue_numbers_in(m.group(0), {int(k): v for k, v in cov["issues"].items()})
        nums |= set(a)
    for m in re.finditer(r"(?m)^\s*(?:\*\*|#+\s*|\()?(\d{1,2})[.)]", body):   # Point / sub-issue list numbering
        nums.add(int(m.group(1)))
    add = {}
    if addendum_json and Path(addendum_json).exists():
        try:
            add = json.loads(Path(addendum_json).read_text(encoding="utf-8")) or {}
        except Exception:
            add = {}
    add_issues = {int(k) for k in (((add.get("issues_preserved") or {}).get("issues")) or [])} if add else set()
    add_part_v = bool(add.get("issues_preserved"))
    add_ecfs = set()
    if addendum_md and Path(addendum_md).exists():   # the addendum's text twin beside the draft: Part V rows + Part II orders
        amd = Path(addendum_md).read_text(encoding="utf-8", errors="replace")
        pv = amd.split("ISSUES PRESERVED", 1)[1] if "ISSUES PRESERVED" in amd else ""
        add_issues |= {int(x) for x in re.findall(r"(?m)^Issue (\d{1,2}) \u2014", pv)}
        add_part_v = add_part_v or bool(pv.strip())
        add_ecfs = {int(x) for x in re.findall(r"\bECF(?:\s*No\.?)?\s*(\d{1,4})\b", amd, re.I)}
    ecfs_named = {int(x) for x in re.findall(r"\bECF(?:\s*No\.?)?\s*(\d{1,4})\b", body, re.I)} | add_ecfs
    for n, t in cov["issues"].items():
        n = int(n)
        toks = _ra.title_tokens(t)
        by_title = len(toks) >= 3 and any(len(toks & s) >= 3 for s in sents)
        if not (n in nums or by_title or n in add_issues or add_part_v):
            out.append(f"WAIVER: issue {n} ({t[:90]}) not named in body or addendum")
    for r in cov["orders"]:
        if not (set(r["ecfs"]) & ecfs_named) and not add_part_v:
            out.append(f"WAIVER: order ECF {r['ecf']} ({r['date']}) not named")
    for m in cov["missing_from_units"]:
        if m["ecf"] not in ecfs_named and not add_part_v:
            out.append(f"WAIVER: order ECF {m['ecf']} ({m['date']}) not named (and absent from ALL_ORDERS_UNITS.md)")
    return out


def check(draft_path, court, filing_type, is_word_limited_form=True, docket=None, serve_list=None, orders_matrix=None, issues_md=None, stub_path=None):
    text = Path(draft_path).read_text(errors="replace")
    # CANONICAL COURT ID. Every lookup below (COURT_RULES, REQ_DB bucket, style profile)
    # keys on the canonical code. An unresolvable court is a FINDING, not an empty contract.
    _court_in = court
    try:
        from pipeline.court_hierarchy import canonical_court_id
        court = canonical_court_id(court) or court
    except Exception:
        pass
    words = len(re.sub(r"<!--.*?-->", "", text, flags=re.S).split())
    rules = _court_rules(court)
    req = {}
    try:
        db = json.loads(REQ_DB.read_text())
        # S-gate-forum-bucket (2026-10-21): an unlisted court fell to "federal_district" and an N.J. Superior Court
        # complaint was failed for a federal summons / verification / IFP. Unlisted state forum -> no DB list; the
        # forum's own rule contract (2c, rule_requirements) is the requirement source.
        bucket = {"3D_CIR": "circuit_court", "EDPA": "federal_district",
                  "PA_CP_PHILA": "pa_state", "PA_SUP": "pa_state", "SCOTUS": "scotus"}.get(court)
        if bucket is None:
            bucket = ("pa_state" if str(court).startswith("PA_")
                      else "federal_district" if (rules or {}).get("is_federal") else None)
        req = ((db.get(bucket) or {}).get(filing_type) or {}) if bucket else {}
    except Exception:
        pass
    findings, profile = [], {}
    # 1. LIMITS
    lim = (rules.get("limits") or {}).get(filing_type) or {}
    if lim.get("words") and words > lim["words"]:
        findings.append(f"OVER WORD LIMIT: {words} > {lim['words']} ({court} {filing_type})")
    profile["limit"] = lim; profile["word_count"] = words
    # 2. BLOCKS
    low = text.lower()
    # every paper served on parties needs a COS -- EXCEPT an initial complaint, which is served as original process
    # with the summons after filing (F6 2026-10-21); a pre-filing COS on it is itself a false certification
    need_cos = filing_type != "complaint"
    if not need_cos and "certificate of service" in low:
        findings.append("FALSE COS: an initial complaint is served by summons as original process, not by certificate")
    if need_cos and "certificate of service" not in low:
        findings.append("MISSING: Certificate of Service block")
    if rules.get("is_appellate") and is_word_limited_form and lim.get("words"):
        if "certificate of compliance" not in low:
            findings.append("MISSING: Certificate of Compliance (FRAP 32(g) — word-limited form)")
    if rules.get("is_appellate") and filing_type == "principal":
        for tbl in ("table of contents", "table of authorities"):
            if tbl not in low:
                findings.append(f"MISSING: {tbl.title()} (principal brief)")
    if not re.search(r"respectfully submitted|/s/|signature", low):
        findings.append("MISSING: signature block")
    for artifact in (req.get("required") or req.get("artifacts") or []):
        a = str(artifact).lower().replace("_", " ")
        if a in ("cover sheet", "civil cover sheet"):  # not draft-side
            continue
        if a == "signature":  # semantic check, not literal word (sig = /s/ + Respectfully submitted)
            if not re.search(r"respectfully submitted|/s/", low):
                findings.append("DB-REQUIRED artifact not evidenced in draft: signature")
            continue
        # S-gate-bracket-heading (2026-10-25): the finisher's own heading is '[PROPOSED] ORDER'; the literal
        # 'proposed order' test failed every post-order motion the finisher built correctly.
        _lowb = re.sub(r"[\[\]]", "", low)
        if a not in _lowb and a.replace(" ", "") not in _lowb.replace(" ", ""):
            findings.append(f"DB-REQUIRED artifact not evidenced in draft: {artifact}")
    # 2a-ii. STANDALONE PAPERS IN THE RENDERED .docx (OJR rejection 2026-10-27, E-File #2610062538: "ENSURE THE PROPOSED ORDER IS
    # ON ITS OWN PAGE WITH THE CASE CAPTION AND CASE NUMBER"). Presence of the words was never the court's test; the page was.
    # Checked on the .docx next to the draft when it exists: each standalone heading must be preceded by a page break and the
    # case number must appear between that break and the heading (the re-emitted caption).
    try:
        findings += _standalone_paper_findings(draft_path, court)
    except Exception as _e:
        findings.append(f"standalone-paper check unavailable: {_e!r}"[:160])
    # 2b. COURT-RULE MANDATORY SECTIONS (rule_requirements contract, 2026-09-27)
    # The gate previously knew only a static artifact list, so a brief could omit every
    # section the forum's own rules mandate and still PASS. Phila.Civ.R. *210 requires five
    # separately and distinctly titled sections IN ORDER; a federal-shaped brief filed in
    # the Court of Common Pleas violates the rule on its face. Contract comes from
    # m1_style_profiles.json (derived from corpus by pipeline/rule_requirements.py) --
    # DATA, so a new court needs no code change here.
    _contract_kind = None
    # The brief contracts are fallbacks for BRIEFS. An affidavit, verification, certificate or
    # cover sheet is not "a brief or memorandum of law" (*210 applies to briefs only), so it
    # is measured against its own contract if one exists, never against the brief's sections.
    _ft = (filing_type or "").lower()
    _non_brief = any(k in _ft for k in ("affidavit", "declaration", "verification",
                                       "sworn", "certificate", "cover_sheet", "coversheet", "ifp"))
    _kinds = (filing_type,) if _non_brief else (filing_type, "brief_or_opposition", "opposition_or_motion")
    for _kind in _kinds:
        _heads = _mandatory_sections(court, _kind)
        if _heads:
            _contract_kind = _kind
            _seen = []
            for _h in _heads:
                _head = str(_h.get("heading") or "").strip()
                _rule = str(_h.get("rule") or "").strip()
                if not _head:
                    continue
                _pat = re.compile(r"(?mi)^\s{0,3}#{1,4}\s*(?:[IVXLC]+\.\s*)?" +
                                  re.escape(_head) + r"\s*$")
                _m = _pat.search(text)
                if not _m:
                    findings.append(f"MISSING RULE-MANDATED SECTION: {_head}"
                                    + (f" ({_rule})" if _rule else ""))
                else:
                    _seen.append((_m.start(), _head, _rule))
                    # HOLLOW SECTION. Presence of the heading is not compliance: the rule mandates
                    # the section's CONTENT ("state the operative facts", "state the reason(s)...").
                    # 2026-09-27 run 8b: 0 rebuttal points assembled -> an 875-word brief with an
                    # EMPTY ARGUMENT passed as "PASS 0 findings" because the five *210 headings were
                    # there. A heading over nothing is the same defect as no heading at all.
                    _req = str(_h.get("requires") or "").strip()
                    _body = _section_body(text, _m)
                    _bw = len(_body.split())
                    try:
                        _floor = int(_h.get("min_words") or _MIN_SECTION_WORDS)
                    except Exception:
                        _floor = _MIN_SECTION_WORDS
                    if _bw < _floor:
                        findings.append(
                            f"HOLLOW RULE-MANDATED SECTION: {_head} has {_bw} word(s), "
                            f"floor {_floor}" + (f" ({_rule})" if _rule else "")
                            + (f" -- requires: {_req}" if _req else ""))
                    else:
                        # The rule's own words may mandate a FORM as well as content.
                        for _label, _probe in _mandate_probes(_req):
                            try:
                                _ok = bool(_probe(_body))
                            except Exception:
                                _ok = True   # a broken probe never fails a draft
                            if not _ok:
                                findings.append(
                                    f"RULE-MANDATED SECTION DOES NOT DISCHARGE ITS MANDATE: "
                                    f"{_head} -- {_label}"
                                    + (f" ({_rule})" if _rule else ""))
            # ORDER is part of the mandate ("in the order listed"): a present-but-misplaced
            # section is still a defect on the face of the filing.
            _order = [h for _p, h, _r in sorted(_seen)]
            _want = [str(h.get("heading") or "").strip() for h in _heads
                     if str(h.get("heading") or "").strip() in _order]
            if _order and _order != _want:
                findings.append(f"RULE-MANDATED SECTION ORDER wrong: found {_order} "
                                f"expected {_want}")
            break
    # FAIL CLOSED: a state trial court with no section contract at all means the rule
    # organ never ran for this (court, kind), or the court id did not resolve. Silence here
    # is how five missing *210 sections passed as "0 findings".
    if _contract_kind is None and not _court_examined(court):
        # Distinguish "rules examined, none mandate sections" (a profile entry exists --
        # legitimate for e.g. E.D. Pa. L.R. 7.1) from "this court was never looked at".
        findings.append(f"NO RULE CONTRACT for court={court!r} (input {_court_in!r}) "
                        f"kind={filing_type!r}: run pipeline.rule_requirements first")
    # 2c. COURT-RULE MANDATORY ATTACHMENTS (rule_requirements contract). The draft must
    # EVIDENCE each item the forum requires to be filed together with it: a same-named
    # heading in the .md, or a sibling file in the packet dir whose name carries the item.
    _atts = _mandatory_attachments(court, _contract_kind or filing_type)
    if _atts:
        _pdir = Path(draft_path).resolve().parent
        # Only siblings that belong to THIS document count: same leading packet index
        # ("07_..." for 07_brief_...). The packet dir is shared by every instrument in the
        # chain; the complaint's own "10_..._EXHIBITS.pdf" satisfied the opposition's
        # EXHIBITS requirement on 2026-09-27 -- a packet with zero exhibits for THIS filing.
        _self = Path(draft_path).name
        _tok = _self.split("_", 1)[0] if "_" in _self else ""
        _own = _tok + "_" if _tok.isdigit() else None      # "07_" packets only; an unnumbered draft shares its dir with its own exhibits
        # A PLACEHOLDER is not evidence: m1_finisher writes "<LABEL>_REQUIRED_BY_<rule>.TODO" for an item it
        # cannot build, and until 2026-10-27 that filename satisfied this very check (the Phila cover sheet
        # "passed" on its own TODO marker). Only real artifacts count; empty files never do.
        _PLACEHOLDER = re.compile(r"\.(todo|missing|placeholder|stub)$|_REQUIRED_BY_", re.I)
        _sibs = [p.name for p in _pdir.iterdir()
                 if p.name != _self and (_own is None or p.name.startswith(_own))
                 and not _PLACEHOLDER.search(p.name) and p.stat().st_size > 0] \
            if _pdir.is_dir() else []
        # FILING CHANNEL (DATA): a court whose e-filing system GENERATES an item (Phila FJD builds the
        # Petition/Motion Cover Sheet from the wizard) declares it in the profile as
        # satisfied_by: "efiling_system"; it is then not a defect when the channel is efile.
        _channel = (os.environ.get("BEEZLE_FILING_CHANNEL") or _court_channel(court) or "mail").lower()
        _heads = re.findall(r"(?m)^\s{0,3}#{1,4}\s*(.+?)\s*$", text)
        # exhibit ITEM lines are attachment evidence too (the application lane lists exhibits as numbered
        # paragraphs "35. **Exhibit A** — <label> Source: <file>", not as headings)
        _heads += [m.group(1) for m in re.finditer(r"(?m)^\s*\d+\.\s+\*\*Exhibit\s+[A-Z]\d?\*\*\s*[—-]\s*(.+?)\s*$", text)]
        for _a in _atts:
            _item = str(_a.get("item") or "").strip()
            _label = str(_a.get("label") or "").strip() or _item
            if not _item:
                continue
            # CONDITIONAL attachments are not defects. Rule 123(a) requires supporting
            # papers to be filed *"If an application is supported by briefs, verified
            # statements, or other papers"* -- the duty is contingent on the filer having
            # them. Checked unconditionally, that requirement can never be satisfied by any
            # application that rests on its own averments, so the gate would FAIL every
            # Pa.R.A.P. 123 filing forever and the operator learns to ignore the gate. A
            # gate that cries wolf on a compliant paper is worse than no gate.
            _req = str(_a.get("requires") or "")
            if re.search(r"\bif the (?:application|filing|document) is supported by\b", _req, re.I) \
                    or re.search(r"\bif (?:any|so|supported)\b", _req, re.I):
                continue
            if str(_a.get("satisfied_by") or "").lower() == "efiling_system" and _channel == "efile":
                continue
            if not _attachment_evidenced(_label, _heads, _sibs):
                findings.append(f"MISSING RULE-MANDATED ATTACHMENT: {_label}"
                                + (f" [{_item}]" if _label != _item else "")
                                + (f" ({_a.get('rule')})" if _a.get("rule") else "")
                                + (f" -- {_a.get('requires')}" if _a.get("requires") else ""))

    # 3. GLOBAL draft-checkable rules
    if rules.get("is_federal") and "notice to defend" in low:
        findings.append("R7 VIOLATION: 'Notice to Defend' is a PA-state block — remove in federal filing")
    if re.search(r"certif\w+ of service.*?(will be|to be) (served|mailed)", low, re.S):
        findings.append("R6 WARNING: COS pre-certifies future service — certify actual service/mailing date")
    # 3b. INTERNAL DATE CONSISTENCY (class rule: one event, one date). A full date that appears in the brief with
    # one year in the FACTS and a different year for the SAME month/day elsewhere is a contradiction on the face of
    # the paper (seen: vehicle sale "March 21, 2019" in FACTS vs "3/21/2023" in ARGUMENT — the 2019 date was the
    # autopay e-signature pulled into CASE_FACTS by mistake). Agnostic: month/day collision across years, any case.
    _MON = {m: i for i, m in enumerate(("january", "february", "march", "april", "may", "june", "july", "august",
                                        "september", "october", "november", "december"), 1)}
    _seen = {}
    # S-gate-foreign-forum-authority (2026-10-21): a NJ Superior Court complaint PASSED while its jurisdiction
    # paragraph cited 42 Pa.C.S. § 931 / Pa.R.C.P. 2179 and its signature 28 U.S.C. § 1654. Jurisdiction, venue
    # and appearance must be the forum sovereign's own law.
    _cu = str(court).upper()
    from pipeline import forum_profile as _fp
    _sovk = _fp.sovereign_of(_cu)
    if re.search(r"\[NO FORUM (?:JURISDICTION|VENUE) SOURCE", text):
        findings.append("NO FORUM JURISDICTION SOURCE: jurisdiction/venue not built for this court")
    _jv = re.search(r"(?ms)^##\s*JURISDICTION AND VENUE\s*$(.*?)(?=^##\s)", text)
    _FOREIGN = {"NJ": r"Pa\.\s?C\.\s?S\.|Pa\.\s?R\.\s?C\.\s?P\.|\bP\.\s?S\.\s*§|Commonwealth of Pennsylvania|Philadelphia County",
                "PA": r"N\.\s?J\.\s?S\.\s?A\.|\bR\.\s?4:\d|Superior Court of New Jersey"}
    # baseline patterns UNION every other sovereign's citation markers read from corpus [forum_profile]
    _fpat = "|".join(x for x in (_FOREIGN.get(_sovk), _fp.foreign_markers(_sovk) if _sovk else None) if x)
    if _sovk and _jv and _fpat:
        _fm = re.search(_fpat, _jv.group(1))
        if _fm:
            findings.append(f"FOREIGN-FORUM AUTHORITY in JURISDICTION AND VENUE ({court}): '{_fm.group(0)}'")
    if _sovk and re.search(r"Appearing personally under 28 U\.S\.C\. § 1654", text):
        findings.append("FEDERAL APPEARANCE STATUTE in a state-court signature (28 U.S.C. § 1654)")
    if _sovk:
        for _os, _orec in (_fp.load().get("sovereigns") or {}).items():
            _oc = ((_orec.get("fields") or {}).get("reservation_1308") or {}).get("cite")
            if _os != _sovk and _oc and _oc in text:
                findings.append(f"FOREIGN-FORUM AUTHORITY in signature reservation: {_oc} in a {_sovk} filing")
    # execution dates ("Dated: October 21, 2026" in the signature / COS blocks) are the day of signing, not a
    # pleaded event -- a signing on the anniversary of an event is not a contradiction (nj_doi 2026-10-21).
    _ev_text = re.sub(r"(?im)^\s*\**\s*(?:dated|date|executed(?: on)?)\s*:.*$", "", text)
    for m in re.finditer(r"\b([A-Z][a-z]+)\.? (\d{1,2}), (\d{4})\b|\b(\d{1,2})/(\d{1,2})/(\d{4})\b", _ev_text):
        if m.group(1):
            mo = _MON.get(m.group(1).lower()); d, y = int(m.group(2)), int(m.group(3))
        else:
            mo, d, y = int(m.group(4)), int(m.group(5)), int(m.group(6))
        if mo:
            # the EVENT is the docket entry the date is attached to (nearest ECF/Dkt number in the sentence), not the
            # bare month/day: two different orders a year apart on the same day (ECF 67 7/31/2025, judgment ECF 180
            # 7/31/2026) are two events, not a contradiction (CA3 26-0002 screen, 2026-10-23)
            _sent = _ev_text[max(0, _ev_text.rfind(". ", 0, m.start())): _ev_text.find(". ", m.end()) if _ev_text.find(". ", m.end()) > 0 else len(_ev_text)]
            _ecfs = tuple(sorted(set(re.findall(r"(?:ECF|Dkt\.?)\s*(?:No\.?\s*)?(\d{1,4})", _sent))))
            # S-gate-date-key-state (2026-10-25): in a state forum the event is keyed by the confirmation/control number,
            # and a date introduced by 'effective'/'eff.' is a policy-term boundary (six back-dated cancellation terms
            # all fall on 3/21 and 9/21 of different years) — a different event class, not one event with two dates.
            # The record's own phrasing is "Cancel effective: 09/21/2025" / "Expiration Date: 03/21/2025" (verbatim
            # exhibit quotes) — the introducer may carry a colon, and an expiration is the same term-boundary class.
            _ecfs = _ecfs + tuple(sorted(set(re.findall(r"(?:Control|Confirmation|Conf\.?|No\.)\s*(?:No\.?\s*)?(\d{5,8})", _sent))))
            if re.search(r"(?i)\b(?:effective|eff\.|expiration|expires?)\s*:?\s*(?:date\s*:?)?\s*$", _ev_text[max(0, m.start()-20):m.start()]):
                _ecfs = _ecfs + ("eff:%d" % y,)
            _seen.setdefault((mo, d, _ecfs), set()).add(y)
    for (mo, d, _ecfs), ys in _seen.items():
        if len(ys) > 1 and (mo, d) != (1, 1) and not FORM_ONLY:   # S-gate-form-only: content check -> m2 skeptic
            findings.append(f"DATE CONTRADICTION: {mo}/{d} appears with years {sorted(ys)} — one event, one date; fix the FACTS source")
    # 3a. ECF-DATE (S-06, RUN 21): every (ECF, date) pair in the body is parsed to a date and must equal the entry's
    #     filed date on the record (intake CM/ECF 'Document <n> Filed MM/DD/YY' headers + CASE_TIMELINE.json);
    #     CITE (S-14/S-05): every case / statute / rule / treatise cite verified against the corpus (name+reporter ->
    #     opinion source, quote in that source, year = Decided, pinpoint inside the opinion's pages; rules via
    #     record_addendum.attributed). Verifier failure is reported, never silently skipped.
    #     S-gate-form-only: CONTENT check -- off the default path (the finisher's own _gate_cites is the hard stop
    #     on WRONG cites at emit time; the m2 skeptic owns cite/record verification).
    if not FORM_ONLY:
        try:
            from pipeline import m1_cite_check as _cc
            _cd = _cc.case_dir_of(Path(draft_path))
            _rows = _cc.check_case_cites(text) + _cc.check_rule_cites(text) + (_cc.check_ecf_dates(text, _cd) if _cd else [])
            for _r in _rows:
                if _r["status"] == "FAIL":
                    _id = "ECF-DATE MISMATCH" if _r["kind"] == "ecf-date" else "CITE FAIL"
                    findings.append(f"{_id}: {_r['cite']} — {_r['reason']}")
                elif _r["status"] == "NOT-IN-CORPUS":
                    findings.append(f"CITE NOT IN CORPUS: {_r['cite']} — {_r['reason']}")
            if not _cd:
                findings.append("ECF-DATE UNCHECKED: no case dir (CASE_TIMELINE.json / intake) above the draft")
        except Exception as _e:
            findings.append(f"CITE GATE UNAVAILABLE: {_e.__class__.__name__}: {_e}")
    # 3b. CLASS GUARDS (added 2026-10-16 after CA3 26-0003: IFP motion captioned to the wrong docket, COS
    #     asserting service a one-recipient mailing never made, m0 stub appendix printed into a filed packet)
    _cap = text[:2500]
    if not FORM_ONLY:   # S-gate-form-only: SELF-LABEL is a content/voice check -> m2 skeptic
        for _m in re.finditer(r"(?i)\bpro[\s-]*se\b|\bpro[\s-]+per\b|\bself-represented\b", text):  # S-gate-selflabel: \'proper\' is not \'pro per\'
            findings.append(f"SELF-LABEL: '{_m.group(0)}' at char {_m.start()} — filer signs sui juris (28 U.S.C. § 1654 / 42 Pa.C.S. § 2501); state the court's duty, not the filer's status")
            break
    if docket:
        _dk = re.findall(r"\bNo\.?\s*(\d{1,2}-\d{3,5}|\d{1,2}:\d{2}-[a-z]{2}-\d{3,6})", _cap, re.I)
        if docket not in _cap:
            findings.append(f"WRONG DOCKET: caption does not carry target docket {docket} (found {_dk[:3]})")
        elif _dk and _dk[0] != docket:
            findings.append(f"WRONG DOCKET: first docket number in caption is {_dk[0]}, target is {docket}")
    _cos_m = list(re.finditer(r"(?mi)^#{1,6}\s*certificate of service\b.*$", text))
    _cos_i = _cos_m[-1].start() if _cos_m else low.rfind("certificate of service")
    _cos = text[_cos_i:] if _cos_i >= 0 else ""
    if serve_list:
        _miss = [n for n in serve_list if n.split(",")[0].strip().lower() not in _cos.lower()]
        if _miss:
            findings.append(f"COS INCOMPLETE: {len(_miss)} required recipient(s) absent from the Certificate of Service: {_miss[:6]}")
        if re.search(r"all (?:parties|counsel) of record", _cos, re.I) and len(serve_list) > 1:
            findings.append("COS VAGUE: 'all parties of record' — a COS must NAME each recipient so the mailing can be checked against it")
    _leak = re.findall(r"(?i)source stub|for drafter reference|locked elements|do not embellish|note to the drafter|drafting note", text)
    if _leak:
        findings.append(f"LEAK: drafter scaffold text in the filing: {sorted(set(x.lower() for x in _leak))}")
    # 3c. CLASS GUARD A — PROVENANCE TAGS. '[corpus:usc_t28_s1446_c0]' after the statute cite in CA3 26-0000 v34
    #     II.1 (2026-10-24). Upstream provenance is legitimate in cells / proposals / OUR_ISSUES; in a court paper it
    #     is a leak. The finisher strips (strip_provenance_tags) and logs; anything that survives FAILs here.
    _ptags = [t for t in strip_provenance_tags(text)[1]]
    if _ptags:
        _ex = "; ".join(f"line {t['line']}: {t['tag']}" for t in _ptags[:6])
        findings.append(f"PROVENANCE TAG LEAK: {len(_ptags)} internal tag(s) in the filing body ({_ex}) — "
                        f"strip at finish (sidecar keeps them); a court never reads [corpus:]/[doc]/[official:]/chunk ids")
    # 3d. CLASS GUARD B — DAY-COUNT ARITHMETIC. 'removed alone on June 2, 2025, 38 days after service' (record:
    #     service 05/10/2025 -> 23 days; CA3 26-0000/26-0002 v34 II.1). Every 'N days after/before <event>' must be
    #     reproduced from two dated record facts (both on the page, or one on the page + CASE_FACTS/intake docket).
    #     S-gate-form-only: CONTENT check -> m2 skeptic.
    if not FORM_ONLY:
        try:
            _cdir = _case_dir_above(draft_path)
            findings += day_count_check(text, (_cdir / "CASE_FACTS.md") if _cdir else None,
                                        (_cdir / "intake") if _cdir else None)
        except Exception as _e:
            findings.append(f"DAY-COUNT GATE UNAVAILABLE: {_e.__class__.__name__}: {_e}")
    # 3e. CLASS GUARD C — FORUM COHERENCE (S-m1-application-crosscase 2026-10-24): foreign procedural authority
    #     (Pa.R.A.P./Pa.C.S./Prothonotary in a FED filing; FRAP/FRCP/§ 1746 in a PA filing) FAILs unless the stub cites it.
    #     S-gate-form-only: cite-forum CONTENT check -> m2 skeptic.
    if not FORM_ONLY:
        try:
            _sp = Path(stub_path) if stub_path else locate_stub(draft_path)
            findings += forum_coherence_check(text, court, _sp.read_text(encoding="utf-8", errors="replace") if _sp and _sp.exists() else None)
        except Exception as _e:
            findings.append(f"FORUM COHERENCE GATE UNAVAILABLE: {_e.__class__.__name__}: {_e}")
    # 4. FORMAT profile for the renderer
    profile["renderer"] = {k: rules.get(k) for k in
                           ("font", "size_pt", "line_spacing", "margin_in", "caption_template", "page_numbers")}
    # 9. WAIVER (--orders-matrix, or $M1_ORDERS_MATRIX from run_screen.sh stage 0): every issue of record and every
    #    adverse order must be NAMED in the body or in the record addendum's Part V — grouping is fine, silence is not.
    #    S-gate-form-only: coverage is a CONTENT check -> m2 skeptic.
    _omx = orders_matrix or os.environ.get("M1_ORDERS_MATRIX")
    if _omx and not FORM_ONLY:
        try:
            _dp = Path(draft_path)
            _stem = re.sub(r"_(DRAFT|FILING)$", "", _dp.stem)
            findings += waiver_findings(text, _omx, issues_md, _dp.with_name(_stem + "_ADDENDUM.json"), _dp.with_name(_stem + "_ADDENDUM.md"))
        except Exception as _e:
            findings.append(f"WAIVER CHECK UNAVAILABLE: {_e.__class__.__name__}: {_e}")
    verdict = "PASS" if not findings else "FAIL"
    return {"verdict": verdict, "findings": findings, "profile": profile,
            "court": court, "filing_type": filing_type, "file": str(draft_path)}

def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("draft"); ap.add_argument("--court", required=True)
    ap.add_argument("--type", required=True, dest="ftype")
    ap.add_argument("--docket", default=None, help="target docket no.; caption must carry it first")
    ap.add_argument("--serve-list", default=None, help="file: one required COS recipient per line")
    ap.add_argument("--orders-matrix", default=None, help="<ALL_ORDERS_UNITS.m2> dir (stage 0 matrix): WAIVER check, every issue + every order named")
    ap.add_argument("--issues", default=None, help="OUR_ISSUES.md (default: sibling of --orders-matrix)")
    ap.add_argument("--stub", default=None, help="m0 stub (FORUM COHERENCE stub-cites exception; default: sibling stub beside the draft)")
    a = ap.parse_args()
    sl = [l.strip() for l in open(a.serve_list) if l.strip()] if a.serve_list else None
    r = check(a.draft, a.court, a.ftype, docket=a.docket, serve_list=sl, orders_matrix=a.orders_matrix, issues_md=a.issues, stub_path=a.stub)
    print(json.dumps(r, indent=1))
    sys.exit(0 if r["verdict"] == "PASS" else 1)

if __name__ == "__main__":
    main()


def _standalone_paper_findings(draft_path, court) -> list:
    """Rendered-docx check: every standalone paper ([PROPOSED] ORDER, CERTIFICATE OF SERVICE, VERIFICATION per the court's
    md_to_docx rules) starts after a page break and its page carries the case number. Returns gate findings (strings)."""
    import re, zipfile
    from pathlib import Path
    dp = Path(str(draft_path))
    cands = [dp.with_suffix(".docx"), dp.parent / "FILING.docx"] + sorted(dp.parent.glob("PACKET/00_*.docx"))
    docx = next((c for c in cands if c.exists()), None)
    if docx is None: return []
    try:
        from pipeline import md_to_docx as M
        rules = M._court_rules_or_die(court)
        names = tuple(x.upper().replace("[", "").replace("]", "") for x in rules.get("standalone_sections", M.STANDALONE_SECTIONS_DEFAULT))
    except Exception:
        names = ("PROPOSED ORDER", "ORDER", "CERTIFICATE OF SERVICE", "VERIFICATION")
    x = zipfile.ZipFile(docx).read("word/document.xml").decode("utf-8", "ignore")
    blocks = re.findall(r"<w:(?:p|tbl)[ >].*?</w:(?:p|tbl)>", x, re.S)
    txt = lambda b: re.sub(r"<[^>]+>", "", b).strip()
    out = []
    for i, b in enumerate(blocks):
        t = re.sub(r"[\[\]]", "", txt(b)).strip().upper()
        if t not in names or i == 0: continue
        # walk back to the previous page break; the caption (case number) must sit between it and the heading
        j = i - 1; seen = ""
        while j >= 0 and 'w:type="page"' not in blocks[j]:
            seen = txt(blocks[j]) + " " + seen; j -= 1
            if i - j > 40: break
        if j < 0 or 'w:type="page"' not in blocks[j]:
            out.append(f"STANDALONE PAPER not on its own page: '{t}' has no page break before it ({docx.name})")
        elif not re.search(r"(No\.|Case No|Civil Action|Docket)\s*:?\s*\S*\d", seen):
            out.append(f"STANDALONE PAPER missing caption/case number on its page: '{t}' ({docx.name})")
    return out

