#!/usr/bin/env python3
"""m1_rebuttal.py — FAST deterministic rebuttal assembler (m1 lane).

PRINCIPLE: the audit engine + corpus + neutralization tables already hold
the verdicts and authorities. m0's LLM re-derives them at ~20min/section.
m1 ASSEMBLES them in seconds and calls the LLM only for points classified
NOVEL — usually 0-3 per brief.

TIER DISPATCH (per opposing point):
  MOOT     — point depends on a threshold issue resolved against them
             (jurisdiction/standing/finality). One sentence + cross-ref.
  SETTLED  — point is answered by enacted text or binding precedent in
             the bank. One declarative sentence + footnote cite. NO LLM.
  RECORD   — point is defeated by a record fact (their own document).
             Element -> record fact -> cite. NO LLM.
  NOVEL    — genuinely contested reasoning. Queued for ONE batched LLM
             call at the end (all novel points in a single prompt).

INPUT (all precomputed, no re-derivation):
  --matrix   output/audit/26-0001_rebuttal_matrix.json   (their headings)
  --bank     rebuttal bank JSON: [{pattern, tier, threshold_ref?, text,
             cites:[...], record_facts:[...]}] — built from neutralization
             tables + audit verdicts + corpus verbatim pulls
  --out      assembled markdown

Zero-waiver preserved: EVERY heading gets a headed rebuttal — the tiers
control DEPTH, never coverage (m0 rule 12, executed deterministically).
"""
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import json, re, sys, datetime
from pathlib import Path
from pipeline._sentsplit import split_sentences as _split_sents
import re
import re as _re

# ---------------- VOICE PASS (the middle ground) ----------------
# Architecture (user-locked 2026-08-26): audit engine = 100% LLM (analysis);
# drafting = deterministic CONTENT (m1 assembles facts/cites/tiers — the
# unfalsifiable layer) + ONE light LLM pass over PROSE ONLY so the filing
# does not read as template/AI writing. The voice pass may rephrase flow,
# transitions, and sentence rhythm; it may NOT add, drop, or alter any
# citation, dollar amount, date, section number, party name, or defined
# term. verify_voice() enforces that deterministically — if invariants
# break, the assembled original is kept (fail-closed).

INVARIANT_PAT = re.compile(
    r"\d+\s+U\.S\.C?\.?\s*§+\s*[\w().-]+"      # USC cites
    r"|\d+\s+C\.F\.R\.\s*§*\s*[\w().-]+"        # CFR cites
    r"|1[0-9]\s+Pa\.C\.S\.\s*§+\s*[\w().-]+"    # Pa.C.S.
    r"|\d+\s+F\.\s*(?:2d|3d|4th|Supp\.?\s*[23]d)\s+\d+"  # reporters
    r"|\d+\s+U\.S\.\s+\d+"                      # U.S. reports
    r"|\d{4}\s+WL\s+\d+"                        # WL
    r"|\$[\d,]+(?:\.\d{2})?"                    # dollar amounts
    r"|Fed\.\s*R\.\s*(?:Civ|App)\.\s*P\.\s*[\w().]+"  # rules
    r"|\bFAS\s*\d+\b|\b1099-C\b|\bGNMA\b|\bREMIC\b"   # record terms
)

def _invariants(text):
    return sorted(INVARIANT_PAT.findall(text))

def verify_voice(original, rewritten):
    """Deterministic gate: the voice pass changed prose only."""
    a, b = _invariants(original), _invariants(rewritten)
    sa, sb = set(a), set(b)
    if sa != sb:                       # an authority/amount/date/heading lost or invented → reject
        return False, {"missing": sorted(sa - sb)[:10], "added": sorted(sb - sa)[:10]}
    if a != b:                         # same set, different repetition counts: prose merged repeats — allowed, logged
        from collections import Counter
        ca, cb = Counter(a), Counter(b)
        print(f"  [voice-verify] repetition changed (kept): {[(k, ca[k], cb[k]) for k in ca if ca[k] != cb[k]][:6]}")
    return True, {}

def _server_model(llm_url):
    """Auto-discover the served model id (vLLM 404s on wrong model names)."""
    import urllib.request
    try:
        with urllib.request.urlopen(llm_url.rstrip("/") + "/models", timeout=10) as r:
            data = json.loads(r.read())
        return (data.get("data") or [{}])[0].get("id") or "default"
    except Exception:
        return "default"

def _style_contract(court=None, filing_type=None):
    """Load the measured style profile (lawyer exemplars + Am Jur + court rules)
    and render it as voice-pass constraints. Data-driven: output/audit/m1_style_profiles.json."""
    try:
        prof = json.loads((Path(__file__).resolve().parent.parent /
                           "output" / "audit" / "m1_style_profiles.json").read_text())
        block = prof.get(court or "", {})
        spec = next(iter(block.values()), {}) if block else {}
        if not spec:
            return ""
        cc = spec.get("citation_conventions", {})
        return (
            "STYLE CONTRACT (measured from opposing counsel's filed briefs and "
            "Am Jur Pleading & Practice Forms — follow it, do not invent style): "
            f"Register: {spec.get('prose_register','professional')}. "
            f"Headings: {spec.get('heading_style',{}).get('argument_heads','Roman numeral full-sentence contentions')}. "
            f"Citations: first cite full with pinpoint; subsequent '{cc.get('subsequent','Id. / short form')}'; "
            f"record cites {cc.get('record_cites','(ECF No. __)')}; "
            "signals See/quoting/citing used sparingly and precisely. "
            "Every factual sentence keeps its record anchor. ")
    except Exception:
        return ""

def voice_pass(md_text, llm_url="http://localhost:8000/v1", court=None, filing_type=None):
    """One batched call: rewrite prose flow, everything substantive locked.
    Returns (text, used_llm) — fail-closed to the assembled original."""
    try:
        import urllib.request
        prompt = (
            "Rewrite the following rebuttal section so it reads as fluent, "
            "confident legal prose by a practicing appellate lawyer — vary "
            "sentence rhythm, add natural transitions between points, keep "
            "the professional register. "
            + _style_contract(court, filing_type) +
            "HARD CONSTRAINTS: keep every bold "
            "point heading, every citation, statute section, dollar amount, "
            "date, party name, footnote marker, and defined term EXACTLY as "
            "written and in the same point order; do not add authorities, "
            "facts, or arguments; do not lengthen by more than 15%. "
            "FACTS, NOT VERDICTS (Pa.R.C.P. 1019(a) discipline): in factual "
            "averments, never characterize conduct with conclusory labels — "
            "no 'without any reasonable basis', 'inequitable', 'constitutes a "
            "violation/unfair practice', 'renders it', 'demonstrates bad "
            "faith'. State only WHAT the documents show (who wrote what, "
            "when, contradicting what) and let the court draw the "
            "conclusion; conclusory phrasing belongs ONLY inside a count's "
            "element recitation quoting its authority. Output "
            "only the rewritten markdown.\n\n" + md_text)
        body = json.dumps({"model": _server_model(llm_url), "temperature": 0.4,
                           "messages": [{"role": "user", "content": prompt}]}).encode()
        req = urllib.request.Request(llm_url.rstrip("/") + "/chat/completions",
                                     data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=900) as r:
            out = json.loads(r.read())["choices"][0]["message"]["content"]
        ok, diff = verify_voice(md_text, out)
        if ok:
            return out, True
        print(f"  [voice-verify] REJECTED rewrite (invariants broke: {diff}) — keeping assembled text")
        return md_text, False
    except Exception as e:
        print(f"  [voice] unavailable ({e}) — deterministic text stands")
        return md_text, False


def classify(heading, bank):
    """Match an opposing heading to the strongest bank entry. Deterministic:
    first MOOT match wins, then RECORD, then SETTLED; no match -> NOVEL."""
    h = heading.lower()
    matches = []
    for e in bank:
        pat = e.get("pattern", "")
        try:
            if pat and re.search(pat, h, re.I):
                matches.append(e)
        except re.error:
            continue
    for tier in ("MOOT", "RECORD", "SETTLED"):
        for e in matches:
            if e.get("tier") == tier:
                return e
    return None


# ---------------------------------------------------------------- draft hygiene
# m0's analysis is WORK PRODUCT: it carries interrogatory scaffolding ("Q10 — ..."),
# ledger labels and instruction echoes that must never reach a filed document.
_QSCAFFOLD = re.compile(
    r"(?:^|\s)Q\d{1,2}\s*[—–-]\s*[A-Z][^.:]{0,60}:\s*", re.M)
# m0 annotates authorities with DIRECTIVES TO THE DRAFTER -- "(MOVANT'S -- answer it:
# distinguish it ...)", "(OURS -- pleaded in the Complaint; never drop it)", and trailing
# "-- state the proposition it supports ...". Those are work-product instructions. Emitted
# into a brief they read as the pleader talking to himself. Strip STRUCTURALLY: any
# parenthetical whose first words mark it as an instruction, plus a trailing em-dash
# clause in the imperative mood. Not a list of this case's strings.
_INSTRUCTION_ECHO = re.compile(
    r"\((?:build the table[^)]*|state the rule exactly[^)]*|do not narrate[^)]*)\)"
    r"|\s*\((?:MOVANT'?S|OURS|THEIRS)\b[^)]*\)"
    r"|\s*[\u2014-]{1,2}\s*(?:state|answer|distinguish|confine|cite|show|address|use|"
    r"do not|never|always)\b[^.;]*(?=[.;]|$)",
    re.I)

# CONCESSION_SCRUB_V1 — structural, not case-specific.
# m0's analysis states the law from BOTH sides (that is its job). A sentence saying
# one of our own theories is precluded/barred/foreclosed is analysis, not advocacy:
# in a filed brief it concedes the count the movant is attacking. Such a sentence is
# kept ONLY when it is attributed to the movant ("Defendant argues X is barred") or
# carries a defeating qualifier ("...but the statute supplies the remedy" is still a
# concession of the first clause, so attribution is the only safe keeper).
_PRECLUSION = re.compile(
    r"\b(?:is|are|was|were)\s+(?:expressly\s+|categorically\s+)?"
    r"(?:precluded|barred|foreclosed|unavailable|not\s+available|not\s+cognizable|"
    r"not\s+recognized|abrogated)\b"
    # ACTIVE VOICE: "Pennsylvania does not recognize X", "the statute does not permit X".
    # The passive form above misses these, and an unattributed active-voice denial
    # concedes the count just as completely. Scoped to LAW-SOURCE subjects: when the
    # subject is a case, an objection, or the record ("the cited case does not support
    # the proposition"), the sentence attacks THEIR position and must survive.
    r"|\b(?:pennsylvania|the\s+commonwealth|this\s+Commonwealth|the\s+statute|"
    r"the\s+section|the\s+act|the\s+code|the\s+rule|the\s+law|the\s+doctrine|"
    r"common\s+law|section\s+\d[\w.()\u00a7-]*|\u00a7\s*\d[\w.()-]*)\s+"
    r"(?:do|does|did)\s+not\s+(?:recognize|permit|allow|provide\s+for|afford|"
    r"authorize|create|confer|contemplate)\b"
    r"|\bthere\s+is\s+no\s+(?:such\s+)?(?:cause\s+of\s+action|claim|remedy|private\s+"
    r"right\s+of\s+action)\b"
    r"|\bno\s+(?:such\s+)?(?:cause\s+of\s+action|claim|remedy)\s+(?:exists|lies)\b",
    re.I)
_ATTRIBUTED = re.compile(
    r"\b(?:movant|defendant|respondent|opposing\s+counsel|petitioner)\b[^.]{0,80}?"
    r"\b(?:argues?|contends?|asserts?|maintains?|urges?|says?|claims?|posits?)\b"
    r"|\baccording to\b|\bwould have (?:it|this Court)\b|\btheir (?:position|theory)\b",
    re.I)

def _scrub_concessions(text: str) -> str:
    """Drop sentences that concede a theory is precluded unless attributed to the movant."""
    if not text or not _PRECLUSION.search(text):
        return text
    out = []
    for sent in _split_sents(text):
        if _PRECLUSION.search(sent) and not _ATTRIBUTED.search(sent):
            continue
        out.append(sent)
    return " ".join(out).strip()


def _fix_proposition(t: str) -> str:
    """"... for the proposition that E.D. Pa. Jan. 31, 2012." states no proposition.

    The bank sometimes carries a court-and-date (or a bare participle) where the held
    proposition belongs. Naming the authority without a proposition is better than
    naming a date as though it were one.
    """
    court = (r"(?:E\.D\.|M\.D\.|W\.D\.|N\.D\.)\s*(?:Pa|N\.Y|Cal)\.?|"
             r"\d(?:st|nd|rd|th)\s+Cir\.|Pa\.\s*(?:Super|Cmwlth)\.")
    t = re.sub(r"(?:[A-Z][^.]{0,60}?\s)?(?:advances|cites|offers)\s+this authority for the "
               r"proposition that\s+(?:" + court + r")[^.]{0,60}?\.(?:\s*\d{1,2},?\s*\d{4}\.)?\s*",
               "The movant advances this authority without identifying a holding that "
               "reaches the claim as pleaded. ", t)
    # a participle where a clause belongs: "for the proposition that explaining X"
    t = re.sub(r"for the proposition that\s+(?:explaining|noting|holding|stating)\s+",
               "for the proposition, as it reads it, that ", t)
    return t


def _flatten_tables(t: str) -> str:
    """A markdown table pasted into a paragraph renders as pipe soup in the .docx.

    The analysis writes tables; a prose point cannot carry one. Convert an inline table
    to the sentence it stands for, dropping the separator row and the header labels.
    """
    if "|" not in t:
        return t
    # separator row is the signature of a table that got flattened into prose
    t = re.sub(r"\|\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|", " ", t)
    # Rows read as sentences keyed by the header: "Count | Remedy | Attacked?" with row
    # "II | Restitution | Yes" states that as to Count II the remedy is restitution and it
    # is attacked. Emit that, not a semicolon dump of every cell.
    # A flattened table has no newlines: rows are separated by "| |". Split on that
    # boundary, then read each row's cells against the header row.
    span = re.search(r"\|(?:[^|\n]{0,60}\|){3,}", t)
    if span:
        raw = span.group(0)
        chunks = [c for c in re.split(r"\|\s*\|", raw) if c.strip(" |")]
        grid = [[c.strip() for c in ch.split("|") if c.strip()] for ch in chunks]
        grid = [g for g in grid if g]
        sents = []
        if len(grid) >= 2:
            hdr = grid[0]
            for row in grid[1:]:
                if len(row) != len(hdr):
                    continue
                cells = {h.rstrip("?:").strip().lower(): c
                         for h, c in zip(hdr[1:], row[1:]) if c}
                # A yes/no "attacked" column is a FINDING, not a field to recite: say
                # whether the movant attacked the count, in a clause a court can read.
                att = (cells.pop("attacked", "") or "").strip().lower()
                rem = (cells.pop("remedy", "") or "").strip()
                lead = f"Count {row[0]}" if not row[0].lower().startswith("count") else row[0]
                if att in ("yes", "y", "true"):
                    verb = "is attacked by the movant"
                elif att in ("no", "n", "false"):
                    verb = "is left standing"
                else:
                    verb = ""
                bits = []
                if rem:
                    bits.append(f"carries {rem[0].lower() + rem[1:]}")
                if verb:
                    bits.append(verb)
                rest = [f"{h}: {c}" for h, c in cells.items() if c]
                tail = ("; ".join(rest) + ".") if rest else ""
                if bits:
                    sents.append(f"{lead} " + " and ".join(bits) + ". " + tail)
                elif tail:
                    sents.append(f"As to {lead} — " + tail)
        t = (t[:span.start()].rstrip() + " " + " ".join(sents) + " "
             + t[span.end():].lstrip()) if sents else (
             t[:span.start()].rstrip() + " " + t[span.end():].lstrip())
    t = re.sub(r"\s*\|\s*", " ", t)
    return re.sub(r"\s{2,}", " ", t).strip()


_MONTHS = ("January|February|March|April|May|June|July|August|September|October|"
           "November|December")


_DANGLING = (r"(?:the )?(?:defenses? of )?failure to|of the|and the|to the|that the|"
             r"in the|on the|for the|with the|as to|such as|including|whether")


def _close_truncated(t: str) -> str:
    """A quotation or sentence that stops mid-clause must not ship.

    Two shapes came out of the bank: a quoted rule ending "...the defenses of failure
    to." and a body sentence cut mid-word ("turned on the absence of a recognized tort
    remedy — i 42 Pa.C.S."). Both assert something the source does not say. Close the
    quote with an ellipsis where the text was cut, and drop a dangling tail.
    """
    # 0. An UNTERMINATED quotation is the worst shape of all: the opening mark swallows
    # everything after it, so holdings from other cases read as part of the quoted rule
    # (measured: a Rule 1032(a) quote ran 1,207 chars and absorbed Mitchell and Bruno).
    # Steps 1-2 below only see BALANCED quotes, so this must run first. Close the quote
    # at the point the source text was actually cut; the remainder resumes as prose.
    if t.count('"') % 2 == 1:
        _open = t.rfind('"')
        _head, _tail = t[:_open + 1], t[_open + 1:]
        _cut = re.search(r"(?:" + _DANGLING + r")\.(\s|$)", _tail, re.I)
        if _cut:
            _quoted = _tail[:_cut.start()].rstrip(" ,;")
            _rest = _tail[_cut.end():].lstrip()
            t = _head + _quoted + ' . . . ."' + (" " + _rest if _rest else "")
        else:
            # no dangling marker: close at the last sentence end inside the run
            _end = max(_tail.rfind(". "), _tail.rfind(".\n"))
            if _end > 0:
                t = _head + _tail[:_end + 1].rstrip() + '"' + " " + _tail[_end + 2:].lstrip()

    # 1. quoted text ending on a dangling function word -> mark the elision
    def _q(m):
        inner = m.group(1).rstrip()
        if re.search(r"(?:" + _DANGLING + r")\.?$", inner, re.I):
            inner = re.sub(r"[,;]?\s*(?:" + _DANGLING + r")\.?$", "", inner, flags=re.I)
            return '"' + inner.rstrip(" ,;") + ' . . . ."'
        return m.group(0)
    t = re.sub(r'"([^"]{40,})"', _q, t)
    t = re.sub(r"\u201c([^\u201d]{40,})\u201d",
               lambda m: _q('"' + m.group(1) + '"').replace('"', "\u201c", 1)[:-1] + "\u201d"
               if re.search(r"(?:" + _DANGLING + r")\.?$", m.group(1).rstrip(), re.I)
               else m.group(0), t)
    # 2. a one/two-letter orphan word immediately before a citation or sentence end
    t = re.sub(r"\s+—\s+[a-z]{1,2}\s+(?=\d+\s+(?:Pa\.C\.S|P\.S|U\.S\.C))", ". ", t)
    t = re.sub(r"(?<=[a-z])\s+—\s+[a-z]{1,2}\.\s", ". ", t)
    return t


def _dedupe_adjacent_cite(t: str) -> str:
    """"X v. Y, 431 A.2d 966. X v. Y, 431 A.2d 966 (Pa. 1981), addressed..." names the
    same case twice in a row. Keep the fuller form (the one carrying the court/year).
    """
    def _one(m):
        first, second = m.group(1), m.group(2)
        # only collapse when it is the SAME case. two different cases cited back to back
        # is normal writing, and dropping one would delete authority from the brief.
        def _key(x):
            nm = x.split(" v. ")[0].strip().replace("\u2019", "'").lower()
            rep = re.search(r"\d+\s+[A-Z][\w.]*\.?\s?\d*d?\s+\d+", x)
            return (nm, rep.group(0) if rep else "")
        k1, k2 = _key(first), _key(second)
        if k1[0] != k2[0] or k1[1] != k2[1]:
            return m.group(0)
        return second if len(second) >= len(first) else first
    name = r"[A-Z][\w\u2019'.&-]+(?:\s+[A-Z\u2019'&][\w\u2019'.&-]*)*\s+v\.\s+[^,;]{2,70}"
    rep = r"\d+\s+[A-Z][\w.]*\.?\s?\d*d?\s+\d+"
    pat = re.compile(r"(" + name + r",\s*" + rep + r")\.\s+(" + name + r",\s*" + rep
                     + r"(?:\s*\([^)]{2,30}\))?)")
    t = pat.sub(_one, t)

    # second shape: "X v. A, X v. B, 431 A.2d 966" -- the first name carries no reporter
    # cite at all, so the pattern above cannot see it. keep the variant that the
    # reporter citation actually belongs to (the second).
    def _one2(m):
        a, b = m.group(1), m.group(2)
        na = a.split(" v. ")[0].strip().replace("\u2019", "'").lower()
        nb = b.split(" v. ")[0].strip().replace("\u2019", "'").lower()
        return m.group(0) if na != nb else b
    name2 = r"[A-Z][\w\u2019'.&-]+(?:\s+[A-Z\u2019'&][\w\u2019'.&-]*)*\s+v\.\s+[^,;]{2,70}"
    t = re.sub(r"(" + name2 + r"),\s*(" + name2 + r",\s*\d+\s+[A-Z][\w.]*\.?\s?\d*d?\s+\d+)",
               _one2, t)
    return t


def _dedupe_dates(t: str) -> str:
    """One date, one format. A run that lists 02/16/2026 and then February 16, 2026
    states the same fact twice and invites an argument about which controls."""
    import datetime as _dt

    def _norm(tok: str):
        tok = tok.strip(" ,;")
        for fmt in ("%m/%d/%Y", "%B %d, %Y", "%b %d, %Y"):
            try:
                return _dt.datetime.strptime(tok, fmt).date()
            except ValueError:
                continue
        return None

    pat = re.compile(r"(?:\d{2}/\d{2}/\d{4}|(?:" + _MONTHS + r")\s+\d{1,2},\s*\d{4})")

    def _fix(m):
        span = m.group(0)
        toks = pat.findall(span)
        if len(toks) < 3:
            return span
        seen, keep = set(), []
        for tok in toks:
            d = _norm(tok)
            key = d.isoformat() if d else tok
            if key in seen:
                continue
            seen.add(key)
            keep.append(tok)
        if len(keep) == len(toks):
            return span
        tail = span[span.rfind(toks[-1]) + len(toks[-1]):]
        return ", ".join(keep) + tail

    return pat.sub(lambda m: m.group(0), re.sub(
        r"(?:(?:\d{2}/\d{2}/\d{4}|(?:" + _MONTHS + r")\s+\d{1,2},\s*\d{4})[,;]?\s*){3,}",
        _fix, t))


def _strip_orphan_parenthetical(t: str) -> str:
    """Drop a citation tail that lost its case name: "2007) (noting that ...".

    A bare year with a closing paren is the tail of a citation whose head is gone; it
    carries no authority a court can check, and the bank repeated it verbatim.
    """
    t = re.sub(r"(?:(?<=\.)|(?<=^))\s*\d{4}\)\s*\((?:noting|citing|quoting|explaining)[^)]{0,400}\)?"
               r"[^.]{0,120}\.", " ", t)
    t = re.sub(r"\s*\b\d{4}\)\s*\((?:noting|citing|quoting|explaining)\s", " (", t)
    return re.sub(r"\s{2,}", " ", t)


def _repair_cite_fragment(t: str) -> str:
    """A body that opens mid-citation ("(Pa. Super. 2002), addressed ...") lost its case
    name to the heading. Drop the orphaned court-and-year parenthetical so the sentence
    starts as a sentence; the citation itself is preserved in the point's footnote."""
    t = (t or "").lstrip()
    t = re.sub(r"^\(?\s*(?:Pa\.|Pa\. Super\.|Pa\. Cmwlth\.|3d Cir\.|E\.D\. Pa\.|"
               r"[A-Z][\w. ]{0,18})\s*\d{4}\)\s*,?\s*", "", t)
    t = re.sub(r"^\d{4}\)\s*\(?[^)]{0,40}\)?\s*,?\s*", "", t)
    t = re.sub(r"^[,;:.]\s*", "", t)
    # a bare verb needs its subject back: "addressed X" -> "That decision addressed X"
    if re.match(r"^(?:address|held|reject|involv|concern|turn|appli|consider|"
                r"affirm|revers|dismiss|constru|not[e]?)[a-z]*\b", t, re.I):
        t = "That decision " + t[0].lower() + t[1:]
    if t and t[0].islower():
        t = t[0].upper() + t[1:]
    return t


def _sanitize(text: str) -> str:
    """Strip m0 work-product scaffolding + self-conceding analysis from filing text."""
    if not text:
        return text
    t = _QSCAFFOLD.sub(" ", text)
    t = _INSTRUCTION_ECHO.sub("", t)
    t = re.sub(r"\s*;?\s*if the (?:opinion|case|authority) is not in corpus[^.;]*[.;]?",
               "", t, flags=re.I)
    t = re.sub(r"\s*\bNOT IN CORPUS\b\s*", " ", t)
    t = _scrub_concessions(t)
    # sentence-final duplication only: "776. . Werner" / "776.. Werner"
    t = re.sub(r"(?<=[A-Za-z0-9\)\]])\.\s*\.(?!\.)(?=\s*(?:\*\*|\s[A-Z]))", ".", t)
    # a 2-dot run that is really an omission marker becomes a proper ellipsis
    t = re.sub(r"(?<=[,;:])\s*\.\.(?!\.)", " ...", t)
    t = re.sub(r"\s\.\.(?!\.)\s", " ... ", t)
    t = re.sub(r"\s*\(\s*\)", "", t)          # empty parens left by a scrub
    t = re.sub(r"\s*\[\s*\]", "", t)
    t = _flatten_tables(t)                      # a table cannot ride inside a paragraph
    t = _dedupe_adjacent_cite(t)                # the same cite twice in a row is one
    t = _dedupe_dates(t)                        # one date, one format
    t = _strip_orphan_parenthetical(t)          # "2007) (noting ..." has no case name
    t = _fix_proposition(t)                     # a court+date is not a proposition
    # A body that opens mid-citation belongs to the SAME class as the orphan parenthetical
    # above, so it must be repaired on every path text takes -- not at one call site.
    # Measured: _repair_cite_fragment was invoked once (the reply-brief body) while _sanitize
    # runs at 8+ sites, leaving 8 of 49 bank entries opening mid-sentence in the packet.
    t = _repair_cite_fragment(t)
    t = _close_truncated(t)                     # no quote/sentence dies mid-clause
    # A spaced ellipsis (Bluebook R5.3 -- four spaced periods marking an omission at the
    # end of a quoted sentence) is DELIBERATE punctuation, not stray whitespace. The two
    # cleanups below would collapse it to "....", so protect it across them.
    _ELLIP = "\x00ELLIPSIS\x00"
    t = t.replace(". . . .", _ELLIP).replace(". . .", _ELLIP + "3")
    t = re.sub(r"\s+([,.;:])", r"\1", t)        # space before punctuation
    t = re.sub(r"([,;:])\1+", r"\1", t)
    t = re.sub(r"\s{2,}", " ", t).strip()
    return t.replace(_ELLIP + "3", ". . .").replace(_ELLIP, ". . . .")


# A section title must read as OUR proposition. When the feed could not derive one
# it falls back to a generic prefix plus the movant's heading, which is their framing
# and frequently a mid-sentence fragment. Repair is GRAMMATICAL, not case-specific:
# find the thing the objection is aimed at (a count, a claim, a statute) and state
# that the objection fails as to it. No case names, no claim names, no filing types.
_FALLBACK_PREFIX = re.compile(r"^Response to the objection concerning\s+", re.I)
# what an objection can be aimed at, in descending specificity — all structural,
# none case-specific.
_TARGET_PATTERNS = (
    re.compile(r"\bCounts?\s+(?:[IVXLC]+|\d+)(?:\s*(?:,|and)\s*(?:[IVXLC]+|\d+))*", re.I),
    re.compile(r"\b\d+\s+(?:U\.S\.C\.|Pa\.C\.S\.|P\.S\.)\s+§+\s*[\w.()-]+", re.I),
    re.compile(r"\b(?:Pa\.R\.C\.P\.|Fed\.\s*R\.\s*Civ\.\s*P\.)\s*[\w.()-]+", re.I),
    re.compile(r"\bthe\s+(?:Complaint|Petition|Answer|Counterclaim)\b", re.I),
)

# A title that is only a case name is not a proposition. The party name itself may
# carry commas and periods ("PPG Indus., Inc. v. Generon IGS"), so the span after
# "v." cannot forbid commas -- it must be non-greedy up to an optional reporter cite.
# An argumentative verb anywhere means the title IS ours, so it is left alone.
_TITLE_VERB = re.compile(
    r"(?i)\b(?:does not|do not|is dist|are dist|supports?|binds?|reach(?:es)?|defeats?|"
    r"states?|pleads?|well taken|sufficient|governs?|applies|waive[sd]?|concedes?|"
    r"forecloses?|controls?|requires?|permits?|bars?|precludes?|establishes?)\b")
# the party name may itself carry commas AND periods ("PPG Indus., Inc."), so the
# head cannot be a run of comma-free capitalised words either.
_CASE_NAME_TITLE = re.compile(
    r"^\s*[A-Z][\w.,'\u2019&-]*(?:\s+[A-Za-z][\w.,'\u2019&-]*)*?\s+v\.\s+"
    r".{2,70}?(?:,\s*\d+\s+[A-Z][\w.]*\.?\s?\d*d?\s+\d+)?"
    r"(?:\s*\u2014[^.]{0,80})?\s*\.?\s*$")


def _proposition_from_body(body: str, case: str) -> str:
    """Our proposition for a point whose title is only the movant's case name.

    Reads what the point actually argues and titles it accordingly; never invents a
    holding. Case-agnostic: keys on argumentative shape, not on any named authority.
    """
    b = " ".join((body or "").split())
    nm = (case or "").split(" v.")[0].strip(" ,") or "That authority"
    # keep an abbreviation's period ("PPG Indus., Inc." not "PPG Indus., Inc"), but do
    # not leave a bare sentence period hanging on an ordinary word.
    if nm.endswith(".") and not re.search(r"\b[A-Z][a-z]{0,4}\.$", nm):
        nm = nm.rstrip(".")
    tests = (
        (r"does not (?:reach|control|govern|apply to) (?:the|this) claim",
         f"{nm} does not reach the claim as pleaded"),
        (r"turned on|addressed (?:only )?whether|procedural posture|distinguish",
         f"{nm} is distinguishable on this record"),
        (r"PERSUASIVE ONLY|does not bind|not binding|federal court sitting",
         f"{nm} does not bind this Court on a question of Pennsylvania law"),
        (r"binds this Court|is binding|stands for",
         f"{nm} supports the claim as pleaded"),
        (r"Pleaded in the Complaint",
         f"{nm} is pleaded in the Complaint and the movant does not address it"),
        (r"no longer|superseded|abrogated|overruled",
         f"{nm} no longer states the governing rule"),
    )
    for pat, title in tests:
        if re.search(pat, b, re.I):
            return title
    return f"{nm} does not defeat the claim as pleaded"


def _normalize_title(title: str, their_heading: str = "", body: str = "") -> str:
    t = (title or "").strip().rstrip(".")
    # a bare case name is not a proposition -- restate it as OUR point. an
    # argumentative verb means the title is already ours, so leave it alone.
    if ("v." in t and not _TITLE_VERB.search(t)
            and _CASE_NAME_TITLE.match(t.rstrip(".") + ".")):
        return _proposition_from_body(body, t)
    if not _FALLBACK_PREFIX.search(t):
        return t
    src = (_FALLBACK_PREFIX.sub("", t) or their_heading or "").strip()
    for pat in _TARGET_PATTERNS:
        m = pat.search(src)
        if m:
            tgt = " ".join(m.group(0).split())
            plural = bool(re.match(r"(?i)counts\s", tgt)) and (
                "," in tgt or re.search(r"(?i)\band\b", tgt))
            verb = "are" if plural else "is"
            return f"The objection as to {tgt} {verb} not well taken"
    # nothing structural named — keep a COMPLETE clause rather than a fragment
    frag = re.split(r",\s+|\s+(?:because|where|since|that|which)\s+", src)[0]
    frag = frag.strip(" ,;:—-\"“”").rstrip(".")
    return f"The objection as to {frag} is not well taken" if frag else t


# An authority statement belongs in FULL once. Restating the same black-letter
# paragraph under four headings reads as padding; the short-form cite carries it
# afterward (the footnote already preserves the citation under every section).
def _short_form(text: str) -> str:
    m = re.match(r"\s*([A-Z][A-Za-z.'’\- ]+?\s+v\.\s+[A-Za-z.'’\-, ]+?),?\s*\d", text or "")
    if m:
        return f"{m.group(1).strip()}, supra."
    m2 = re.search(r"(\d+\s+Pa\.C\.S\.\s+§\s*\d+[\w.()-]*)", text or "")
    if m2:
        return f"{m2.group(1)}, supra."
    return ""




# ---------------------------------------------------------------------------
# FEED BUILDER (absorbed from the former pipeline/m1_feed.py)
#
# The rebuttal lane needs a matrix (their headings) and a bank (our answers) built
# from the CURRENT analysis. Keeping that builder in a separate module meant the
# lane could assemble from a STALE feed without complaint -- and it did: a draft
# kept a date the analysis had already corrected. One organ now owns both steps.
# ---------------------------------------------------------------------------

# Paths the absorbed builder needs (these lived in the former m1_feed module).
_FEED_ROOT = _eroot()
CASES = _FEED_ROOT / "obsidian_sync" / "cases"
OUT = _FEED_ROOT / "output" / "audit"

_HEADING = re.compile(
    r"^\s*(?:(?:[IVXLC]+|[A-Z]|\d+)[.)]\s+)(?P<h>[A-Z][^\n]{8,340})$"
    r"|^\s*(?P<h2>[A-Z][A-Z \-'&,;:]{12,110})$",
    re.M)
_TOPIC = re.compile(
    r"unjust enrichment|conversion|breach of contract|bad faith|gist of the action|"
    r"unfair trade practices|UTPCPL|consumer protection|fiduciary|constructive trust|"
    r"statute of limitations|standing|jurisdiction|waiver|demurrer|preliminary objection|"
    r"failure to state|pleading|damages|remedy|treble|punitive|first-party|third-party|"
    r"post-formation|exclusion|expressio", re.I)
_NOT_ARGUMENT = re.compile(
    r"^(IN THE|COURT OF|CIVIL|NO\.|FILED|BY THE COURT|RESPECTFULLY|"
    r"CERTIFICATE OF SERVICE|VERIFICATION|CASE ID|CONTROL NO|E-FILE|ORDER$|NOW,|"
    r"BRIEF IN SUPPORT|MEMORANDUM|TABLE OF|EXHIBIT|APPENDIX)", re.I)
_BARE_LABEL = re.compile(
    r"^(INTRODUCTION|CONCLUSION|ARGUMENT|STANDARD OF REVIEW|PRELIMINARY STATEMENT|"
    r"STATEMENT OF (?:THE )?(?:CASE|FACTS)|FACTS|SUMMARY)\.?$", re.I)
_COUNSEL = re.compile(r"\b(?:PA|N\.?J\.?)\s*I\.?D\.?\s*(?:No\.?)?\s*\d|Esquire|, Esq\b", re.I)

def _newest_adverse(slug: str) -> str:
    idir = CASES / slug / "intake"
    if not idir.is_dir():
        return ""
    cands = list(idir.glob("*.doctext.txt")) + list(idir.glob("*.ocr.txt"))
    if not cands:
        return ""
    return max(cands, key=lambda p: p.stat().st_mtime).read_text(
        encoding="utf-8", errors="ignore")


# Bullets an analysis file marks as verified-FALSE. m0 writes a "DROPPED after verification
# (do not plead)" block listing claims that looked like levers and are not -- e.g. "no proposed
# order" when their proposed order is page 1. Feeding those to the drafter pleads a falsehood in
# the filer's own name, so the block is a KILL LIST, not content.
# m0 marks a verified-false claim three different ways across its analysis files:
#   "## DROPPED after verification (do not plead)"
#   "## RETRACTED — do not plead this"
#   "## CORRECTION" / "## DO NOT PLEAD"
# Matching only one of them let a retraction in a sibling file feed the drafter the very
# claim it retracted. Match the CONCEPT (dropped/retracted/corrected/do-not-plead), not
# one file's wording.
_DROP_HEAD = re.compile(
    r"(?im)^#{1,6}\s*(?:DROPPED|RETRACTED|WITHDRAWN|CORRECTION|DO\s+NOT\s+PLEAD)\b.*$")

# A verified-false claim also appears INLINE as a bullet or numbered item that says so
# ("- \"No proposed order\" — FALSE. ...", "1. **`theirdefect` ... is FALSE.**"). Those are
# not under any heading, so the block strip cannot reach them: drop the individual line.
_FALSE_LINE = re.compile(
    r"(?im)^\s*(?:[-*]|\d+\.)\s+(?=[^\n]*\b(?:is\s+)?FALSE\b)[^\n]*$")


def _strip_dropped(text: str) -> str:
    """Remove any 'DROPPED after verification (do not plead)' block from analysis prose.

    The block runs to the next heading of the same-or-higher level, or to end of file.
    """
    m = _DROP_HEAD.search(text)
    if not m:
        return text
    level = len(re.match(r"\s*(#+)", m.group(0)).group(1))
    rest = text[m.end():]
    nxt = re.search(r"(?m)^#{1,%d}\s" % level, rest)
    return text[:m.start()] + (rest[nxt.start():] if nxt else "")


def _unwrap_soft_breaks(text: str) -> str:
    """Join lines a markdown author wrapped mid-sentence, so extraction sees whole sentences.

    m0 hard-wraps analysis prose at ~95 chars: "...they quote policy-based duty language\n
    (\"the right under the policy...\")". Any bullet/paragraph split on single newlines then
    yields a body that opens mid-sentence, and that fragment gets STORED in the bank.

    A line break is SOFT when the next line continues the sentence: it does not start a list
    marker, heading, table row, or blockquote, and the current line does not end a sentence.
    """
    out = []
    for line in text.split("\n"):
        stripped = line.strip()
        starts_block = bool(re.match(r"^(?:[-*+]\s|\d+[.)]\s|#{1,6}\s|>|\||```)", stripped))
        if (out and stripped and not starts_block
                and out[-1].strip()
                and not re.search(r"[.!?:;\"\u201d)]$", out[-1].rstrip())
                and not re.match(r"^(?:[-*+]\s|\d+[.)]\s|#{1,6}\s|>|\||```)", out[-1].strip())):
            out[-1] = out[-1].rstrip() + " " + stripped
        else:
            out.append(line)
    return "\n".join(out)


def _strip_false_lines(text: str) -> str:
    """Drop individual bullets that declare a claim FALSE (outside any DROPPED block)."""
    return _FALSE_LINE.sub("", text)


# m0 also writes the instruction INLINE, with no heading and no "FALSE":
#   '... the rule says "conform." A typo, not a substantive ground. Per prior instruction:
#    do not build.'
# That is a directive to the drafter, not an argument for the brief.
_NO_PLEAD_LINE = re.compile(
    r"(?im)^[^\n]*\b(?:do not (?:plead|build|use|raise)|not a substantive ground|"
    r"per prior instruction)\b[^\n]*$")


def _strip_no_plead_lines(text: str) -> str:
    """Drop lines carrying m0's inline do-not-plead instruction."""
    return _NO_PLEAD_LINE.sub("", text)


def _restore_sentence_open(body: str, title: str) -> str:
    """Give a body a grammatical subject when the stripped prefix WAS its subject.

    Dropping the redundant "Hart v. Arnold, 884 A.2d 316," prefix leaves "addressed the
    interplay between..." -- a sentence with no subject. The case name is in the heading, so
    name the decision rather than repeating the full citation.
    """
    b = body.lstrip()
    if not b:
        return body
    # only a lowercase verb/conjunction opening is a fragment; a quote or capital is fine
    if not (b[0].islower() or b[0] == ","):
        return body
    short = re.split(r",\s*\d", title)[0].strip().rstrip(".,")
    short = re.sub(r"\s*\u2014.*$", "", short).strip()
    subject = f"{short} " if short and " v. " in short else "That decision "
    return subject + b.lstrip(", ")


# m0 writes whole SECTIONS addressed to the drafter, not to the court: "## Provenance note for
# the drafter", "## Note to the drafter", "## Drafting note". Their sentences are instructions
# ("Cite it as affirmed ... rather than attributing the sentence to the Supreme Court"), so any
# organ mining them for content emits work product. Same class as _NO_PLEAD_LINE, section scope.
_DRAFTER_NOTE_HEAD = re.compile(
    r"(?im)^#{1,6}\s*(?:provenance\s+note|note\s+to\s+the\s+drafter|drafting\s+note|"
    r"drafter[''\u2019]?s?\s+note|(?:appendix\s+\w+\s*[-\u2014:]*\s*)?source\s+stub|locked\s+elements|"
    r"what\s+happened\s*\(record\s+facts|stub:\s)\b[^\n]*$")


def _strip_drafter_notes(text: str) -> str:
    """Remove sections m0 addressed to the drafter rather than to the court."""
    out, skip = [], False
    for line in text.split("\n"):
        if re.match(r"(?m)^#{1,6}\s", line):
            skip = bool(_DRAFTER_NOTE_HEAD.match(line))
        if not skip:
            out.append(line)
    return "\n".join(out)


_LEVER_HEAD = re.compile(r"(?m)^#{1,6}\s*L\d+\b")


def _lever_blocks(text: str) -> list:
    """[(label, block_text)] for each of m0's '## Ln - ...' lever sections."""
    out = []
    for hm in re.finditer(r"(?m)^#{1,6}\s*(L\d+)\b([^\n]*)$", text):
        nxt = re.search(r"(?m)^#{1,6}\s", text[hm.end():])
        block = text[hm.end():hm.end() + (nxt.start() if nxt else len(text))]
        label = hm.group(2).lstrip(" \u2014-\u2013:").strip()
        out.append((label, block))
    return out


def _lever_norm(s: str) -> str:
    """Comparison form: markdown stripped, whitespace collapsed, lowercased.

    The finding has been through _clean_prose (asterisks gone); the lever block has not.
    Comparing the two raw made every emphasised lever look like a non-lever.
    """
    return " ".join(_clean_prose(s).split()).lower()


def _lever_finding(ana: str, body: str) -> bool:
    """True when this finding's text came from inside a lever section."""
    probe = _lever_norm(body)[:60]
    if not probe:
        return False
    return any(probe in _lever_norm(b) for _lbl, b in _lever_blocks(ana))


def _lever_label(ana: str, body: str) -> str:
    """m0's own lever heading text, used as the point's heading."""
    probe = _lever_norm(body)[:60]
    for lbl, b in _lever_blocks(ana):
        if probe and probe in _lever_norm(b):
            return lbl
    return ""


def _under_lever_heading(text: str, pos: int) -> bool:
    """True when offset `pos` sits under an m0 lever heading ('## L3 - ...').

    m0 numbers its graded levers L1..Ln. The nearest preceding heading decides: a bullet
    under a lever heading carries m0's grade, so the concession regex must not re-judge it.
    """
    heads = [(m.start(), bool(_LEVER_HEAD.match(m.group(0))))
             for m in re.finditer(r"(?m)^#{1,6}\s*[^\n]*$", text[:pos])]
    return heads[-1][1] if heads else False


def _analysis(slug: str) -> str:
    """m0's verified adverse-filing analysis for this case -- ONE source.

    CASE_FACTS.md § ADVERSE-FILING DEFECTS & LEVERS is the only thing the drafter reads.
    m0 is the single analysis tool; m1 assembles what m0 wrote and nothing else.

    UNFORKED 2026-09-27: a second source (analysis/*.md) was added the same morning so a
    hand-written Wenk lever would reach the draft. That folder also held Hermes' own
    working notes ("WRONG CASE", "DROP that prong", "reframe, do not drop"), which then
    rendered as argument in the brief. The five scrub passes written to filter them back
    out are no longer called. If m0's record is missing a lever, the fix is in m0
    (case_facts_refresh), never a side channel into m1.
    """
    cf = CASES / slug / "CASE_FACTS.md"
    if not cf.exists():
        return ""
    t = cf.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"^##\s*ADVERSE-FILING DEFECTS & LEVERS(.*?)(?=^##\s|\Z)", t, re.S | re.M)
    return m.group(1) if m else ""


def _clip_text(text: str, limit: int = 1100) -> str:
    """Clip to a budget on a SENTENCE boundary.

    `text[:1100]` severed a word and shipped it: "...during a dispute — i" (the start of
    "in this case"). No later pass can repair that -- the words are gone -- so the cut
    itself has to be clean. Prefer the last sentence end inside the budget; fall back to
    the last whole word.
    """
    text = text.strip()
    if len(text) <= limit:
        return text
    window = text[:limit]
    # last sentence terminator that is not part of an abbreviation/citation
    best = -1
    for m in re.finditer(r"[.!?][\"\u201d)]?\s", window):
        # An ELLIPSIS is not a sentence end. m0 writes elided rule text as
        # "...the defenses of failure to..."; cutting there severed the quote mid-clause
        # and left the quotation mark OPEN, so the following authority's sentences were
        # swept inside it and attributed to the rule. Never cut at an ellipsis.
        if re.search(r"\.\s*\.\s*\.?$|\u2026$", window[max(0, m.start() - 3):m.start() + 1]):
            continue
        tail = window[max(0, m.start() - 6):m.start() + 1]
        if re.search(r"\b(?:v|Inc|Co|Corp|Ltd|Nat|Mut|Cas|Ins|Pa|Super|Cmwlth|Cir|"
                     r"No|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Oct|Nov|Dec|e\.g|i\.e|"
                     r"\u00a7\u00a7?|[A-Z])\.$", tail):
            continue
        best = m.start() + 1
    def _quote_balanced(s: str) -> bool:
        """Even count of double-quote marks (straight or curly pairs)."""
        return (s.count('"') + s.count("\u201c") + s.count("\u201d")) % 2 == 0

    if best > limit * 0.5 and not _quote_balanced(window[:best]):
        # Cutting here would ship an UNCLOSED quotation. Back up to the last boundary
        # that leaves quotes balanced rather than emit a quote the source never closed.
        _b = -1
        for _m in re.finditer(r"[.!?][\"\u201d)]?\s", window[:best]):
            if _quote_balanced(window[:_m.start() + 1]):
                _b = _m.start() + 1
        best = _b
    if best > limit * 0.5:
        return window[:best].rstrip()
    cut = window.rfind(" ")
    return (window[:cut] if cut > limit * 0.5 else window).rstrip(" ,;:\u2014-") + "..."


def _cites_in(s: str) -> list:
    return sorted(set(c.strip() for c in re.findall(
        r"\b\d{1,3}\s+[A-Z][A-Za-z.]*\.?\s?(?:2d|3d|4th)?\s+\d{1,4}"
        r"|\b\d{1,2}\s+(?:P\.S\.|Pa\.C\.S\.|U\.S\.C\.)\s*§*\s*[\d.\-()]+"
        r"|Pa\.R\.(?:C\.P|A\.P|E)\.?\s*\d+(?:\.\d+)?(?:\([a-z0-9]\))*", s)))[:6]


# ── POSTURE GATE ───────────────────────────────────────────────────────────────
# m0's analysis is a NEUTRAL AUDIT: it scores every authority in both directions,
# which is correct for analysis and fatal for a brief. Lifted verbatim into the
# bank, findings like "Progressive's objection to Count III is well-grounded" and
# "Wenk: HOLDS-AS-CITED" became OUR argument — a draft that conceded four of six
# counts in the client's own voice.
#
# Same discipline as reasoning_layer's direction gate (against_pro_se vs by_pro_se):
# a finding that runs FOR the movant is not a defect to plead against ourselves.
# Here it is dropped from the bank; m0 keeps it in the analysis where it belongs
# (knowing the opponent's strong points is what makes the response honest).
#
# AGNOSTIC: keyed on argumentative POSTURE words, never on party names.
#
# USER-LOCKED DOCTRINE this enforces (litigation-docket-operations skill §1, and the
# m0 rebuttal rule 10 "ANSWER FROM OUR PLEADING, NOT THEIRS"): orders and motions are
# INPUTS TO ANALYZE, never law to obey, and the movant's framing is argument — never a
# fact about the law. A bank entry that adopts the movant's or the court's stance puts
# the opponent's case in our own brief.
_FOR_MOVANT = re.compile(
    r"\bwell[- ]grounded\b"
    r"|\bcorrectly (?:applies|applied|cites|cited|states|argues|characteriz\w+)\b"
    r"|\bobjection(?:s)? (?:is|are) (?:well|sound|valid|proper|meritorious)"
    r"|\bHOLDS[- ]AS[- ]CITED\b"
    r"|\bis (?:a )?settled proposition\b"
    r"|\b(?:movant|defendant)['’]?s? (?:position|argument|objection)s? (?:is|are) "
    r"(?:correct|sound|valid|supported)\b"
    r"|\bproperly (?:dismissed|barred|precluded)\b"
    r"|\bplaintiff(?:['’]s)? claim (?:is|are) (?:barred|precluded|deficient)\b"
    # ORDERS ARE INPUTS, NEVER LAW TO OBEY — any sentence asserting a court HAS ruled
    # is excluded from the bank outright. "the court's actual order granting..." slipped
    # past a narrower pattern and reached a draft.
    r"|\bcourt['’]?s? (?:actual |own )?(?:order|ruling|decision|opinion)\b"
    r"[^.\n]{0,40}\b(?:grant\w*|deni\w*|dismiss\w*|sustain\w*)\b"
    r"|\bthe court (?:has |had )?(?:granted|denied|dismissed|ruled|held|found|sustained)\b"
    r"|\b(?:order|ruling) (?:granting|denying|dismissing|sustaining)\b",
    re.I)

# A finding that DEFEATS the movant — kept even if it also contains audit vocabulary.
_FOR_US = re.compile(
    r"\bexpressio unius\b"
    r"|\bpersuasive only\b|\bdoes not bind\b|\bnot binding\b"
    r"|\bSTRETCHED\b|\bCONTRARY\b|\boverstat(?:es|ed|ement)\b"
    r"|\bremedy[- ]stripping\b|\bimplicit(?:ly)? conce(?:des|ssion)\b"
    r"|\bwaived\b|\bnot in the enacted text\b|\benacted text does not\b"
    r"|\bmay not add\b|\bnot among them\b"
    r"|\bnon[- ]responsive\b|\bmoot for want\b"
    r"|\bfails to (?:address|engage|distinguish)\b"
    r"|\bnever (?:cites|mentions|addresses|distinguishes)\b"
    r"|\bomit(?:s|ted)\b"
    r"|\bis not (?:a )?(?:judicial )?ruling\b|\bno ruling entered\b"
    r"|\brequested relief\b|\bproposed order\b",
    re.I)

# A statement ACCEPTING the movant's limiting rule as law. These carry no concession
# vocabulary — they read as neutral description — so the weighted filter missed them
# and one reached a draft: "The UTPCPL limitation to pre-formation conduct is specific
# to insurance contracts." Stating the opponent's asserted restriction as a fact about
# the law IS the concession, whatever its tone.
_ACCEPTS_LIMITATION = re.compile(
    r"\blimitation\b[^.\n]{0,60}\bis\b[^.\n]{0,40}\b(?:specific|limited|confined|"
    r"applicable|applies)\b"
    r"|\b(?:doctrine|rule|statute|provision|distinction)\b[^.\n]{0,50}"
    r"\b(?:is|are)\b[^.\n]{0,30}\b(?:specific to|limited to|confined to|"
    r"restricted to|only applies to|applies only)\b"
    r"|\bdoes not (?:apply|reach|extend) to\b(?![^.\n]{0,60}"
    r"\b(?:this|these|the present|our|plaintiff))"
    r"|\bapplies only to\b",
    re.I)


# A sentence asserting a court HAS ACTED is excluded ABSOLUTELY — it can never be
# outvoted by pro-us signals in the same passage. "Orders are inputs to analyze, never
# law to obey" (user-locked); a brief that recites a ruling that was never entered
# fabricates the record against its own client.
_RULING_ASSERTED = re.compile(
    r"\bcourt['’]?s? (?:actual |own )?(?:order|ruling|decision)\b"
    r"[^.\n]{0,40}\b(?:grant\w*|deni\w*|dismiss\w*|sustain\w*)\b"
    r"|\bthe court (?:has |had )?(?:granted|denied|dismissed|ruled|held|sustained)\b"
    r"|\b(?:order|ruling) (?:granting|denying|dismissing|sustaining)\b",
    re.I)


def _serves_our_side(text: str) -> bool:
    """Keep a finding only if it helps the non-movant. When a passage carries both
    (an audit line that concedes then distinguishes), the pro-us signal must be the
    stronger one — otherwise it reads as concession in a brief."""
    if not text:
        return False
    # hard exclusion first — no vote, no balance
    if _RULING_ASSERTED.search(text):
        return False
    # accepting the movant's limiting rule is a concession even when phrased neutrally,
    # UNLESS the same sentence rejects it (enacted text / expressio unius / not binding)
    if _ACCEPTS_LIMITATION.search(text) and not _FOR_US.search(text):
        return False
    pro = len(_FOR_US.findall(text))
    con = len(_FOR_MOVANT.findall(text))
    if con and not pro:
        return False
    if con and pro:
        return pro > con
    return True




def _clean_prose(s: str) -> str:
    """Strip analysis scaffolding so the bank carries FILING VOICE, not m0's worksheet.
    The first build emitted 'Q2_our_pleading:** The objections target...' into the draft."""
    s = re.sub(r"\*{0,2}Q\d+_[a-z_]+:?\*{0,2}\s*", "", s)
    s = re.sub(r"^\s*[-*]\s*", "", s)
    # m0 writes '**Label:**' run-ons that survive as '*Label:**' once the leading
    # asterisk is consumed by the surrounding markdown — a court sees raw punctuation.
    s = re.sub(r"\*+\s*([A-Z][^*\n:]{2,60}):\*+\s*", r"\1: ", s)
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    s = re.sub(r"\*+", "", s)
    # analysis list markers left dangling at the end of a lifted paragraph ("... 9.")
    s = re.sub(r"(?<=[.!?])\s+\d{1,2}\.\s*$", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _tidy_heading(h: str) -> str:
    """A section title must read as a clean proposition. Headings lifted from an OCR'd
    brief carry footnote digits, orphan quote marks and truncation."""
    h = re.sub(r"[\u00b9\u00b2\u00b3\u2070-\u209f]+", "", h)   # superscript footnote marks
    h = re.sub(r"^[\"“”'’\s,;:]+", "", h)                        # orphan opening quote
    h = re.sub(r"[\"“”'’\s,;:]+$", "", h)
    # an UNPAIRED quote mark left mid-heading by truncation ("Claim” against ...") —
    # strip it rather than open a quotation the brief never closes
    for q in ('"', "\u201c", "\u201d"):
        if h.count(q) % 2:
            h = h.replace(q, "")
    h = re.sub(r"\s+", " ", h).strip()
    return h


def _title_case(line: str) -> bool:
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z'\u2019-]*", line) if len(w) > 3]
    if not words:
        return False
    return sum(1 for w in words if w[0].isupper()) / len(words) >= 0.75


def _join_heading_lines(text: str) -> str:
    """OCR wraps headings at the page width. Rejoin each head with its continuation
    lines so the matrix carries the movant's whole proposition, never a stump.

    Title-Case head: continues while the following line is Title Case (body prose is
    sentence case, so it stops itself). Sentence-case numbered paragraph: continues to
    the first line ending in terminal punctuation. Hyphen-broken words rejoin.
    """
    lines = text.split("\n")
    out, i, n = [], 0, len(lines)
    while i < n:
        ln = lines[i]
        m = _HEADING.match(ln)
        # all-caps section labels (CONCLUSION, ARGUMENT) never wrap; only titled heads do
        if not m or m.group("h2") or not ln.strip() or re.search(r"[.;:?]\s*$", ln):
            out.append(ln)
            i += 1
            continue
        head, k, joined = ln.rstrip(), i + 1, 0
        tc = _title_case(m.group("h") or m.group("h2") or "")
        while k < n and joined < 3:
            nxt = lines[k].strip()
            if not nxt or nxt.startswith("---") or _HEADING.match(lines[k]):
                break
            if tc and not _title_case(nxt):
                break
            # a trailing hyphen in a heading is a compound ("Post-" / "Formation"), keep it
            head = head + nxt if head.endswith("-") else head + " " + nxt
            joined += 1
            k += 1
            if re.search(r"[.;:?]\s*$", nxt) or (tc and re.search(r"[.;:?]\s*$", nxt)):
                break
            if not tc and len(head) > 320:
                break
        out.append(head)
        out.extend([""] * joined)  # keep line count stable for any offset-based reader
        i = k
    return "\n".join(out)


def build_matrix(slug: str) -> dict:
    text = _join_heading_lines(_newest_adverse(slug))
    heads, seen = [], set()
    for m in _HEADING.finditer(text):
        h = _tidy_heading(" ".join((m.group("h") or m.group("h2") or "").split()))
        if len(h) < 10 or h.lower() in seen:
            continue
        if _NOT_ARGUMENT.search(h) or _BARE_LABEL.match(h) or _COUNSEL.search(h):
            continue
        # decretal lines from a [PROPOSED] ORDER are the movant's requested relief
        if re.search(r"\bis\s+DISMISSED\b|\bare\s+GRANTED\b|\bit\s+is\s+hereby\s+ORDERED\b"
                     r"|\bORDERED,?\s+ADJU[DG]ED\b", h, re.I):
            continue
        if re.match(r"^[A-Z][A-Z .,'&\-]+$", h) and not re.search(
                r"\b(ARGUMENT|COUNT|OBJECTION|DOCTRINE|CLAIM|RELIEF|LAW)\b", h, re.I):
            continue
        seen.add(h.lower())
        heads.append(h)
    return {"built_from": "adverse filing in case intake",
            "doctrine": "zero-waiver: every argument heading gets an answering section; "
                        "silence is never permitted",
            "briefs": {slug: {"headings_found": heads, "propositions_asserted": []}}}


def _score(h: str, f: str) -> int:
    ht = {t.lower() for t in _TOPIC.findall(h)}
    ft = {t.lower() for t in _TOPIC.findall(f)}
    score = len(ht & ft) * 10
    words = {w for w in re.findall(r"[a-z]{6,}", h.lower())}
    score += sum(2 for w in words if w in f.lower())
    return score


def _agree_verb(subject: str, phrase: str) -> str:
    """Make the inverted predicate agree with the subject the heading names.

    The rewrite table is written plural ("are legally sufficient"); a singular subject
    ("Count V of Plaintiff's Complaint", "Common law bad faith claim") then produced
    "Count V ... are legally sufficient" / "claim state legally sufficient claims".
    Decide number from the subject's head noun, not from the table.
    """
    subj = re.sub(r"\s*\([^)]*\)", "", subject or "").strip()
    # the head noun governs: it is the FIRST noun, before any prepositional tail
    head = re.split(r"\s+(?:of|in|under|for|to|against|as|concerning)\b", subj, 1)[0].strip()
    words = [w for w in re.split(r"[^\w\u2019']+", head) if w]
    plural = False
    if words:
        first = words[0]
        # explicit singular/plural nouns win over any suffix heuristic
        if re.match(r"^(?:Counts|Claims|Allegations|Objections|Defenses|They|These|Both)$",
                    first, re.I):
            plural = True
        elif re.match(r"^(?:Count|Claim|Allegation|Objection|Defense|Complaint|"
                      r"Relationship|Pleading|It|This|Each)$", first, re.I):
            plural = False
        else:
            plural = (first.lower().endswith("s") and not first.lower().endswith("ss"))
        # a coordinated head ("Counts II and VI", "Conversion and Unjust Enrichment")
        # is plural regardless of the nouns' own number
        if re.search(r"\b(?:and|&)\b", head, re.I) and not re.match(r"^(?:Count|Claim)\b",
                                                                    head, re.I):
            plural = True
        elif re.match(r"^Counts?\b", head, re.I) and re.search(r"\b(?:and|&)\b", head, re.I):
            plural = True
    fixes = (("are legally sufficient", "is legally sufficient"),
             ("state legally sufficient claims", "states a legally sufficient claim"),
             ("are not barred", "is not barred"),
             ("plead duties independent of the contract",
              "pleads duties independent of the contract"),
             ("rest on duties imposed by law", "rests on duties imposed by law"))
    if plural:
        for pl, sg in fixes:
            if phrase.startswith(sg):
                return pl + phrase[len(sg):]
        return phrase
    for pl, sg in fixes:
        if phrase.startswith(pl):
            return sg + phrase[len(pl):]
    return phrase


def _our_heading(their_heading: str) -> str:
    """Turn the movant's proposition into OUR answering proposition.

    A brief whose section titles are the opponent's headings reads as a list of their
    arguments with commentary — the court sees their framing in our voice. Rule 10:
    answer from our pleading, not theirs. Deterministic and agnostic: the subject is
    whatever the heading names; only the posture is inverted.
    """
    h = _tidy_heading(" ".join((their_heading or "").split()).rstrip("."))
    subj = re.sub(r"^(?:Plaintiff(?:['’]s)?|Defendant(?:['’]s)?|The)\s+", "", h, flags=re.I)
    # their assertion -> our denial of it
    pairs = [
        (r"\bshould be dismissed\b.*$", "state legally sufficient claims"),
        (r"\b(?:is|are) legally (?:deficient|insufficient)\b.*$", "are legally sufficient"),
        (r"\bfails? to state a claim\b.*$", "states a claim on the facts averred"),
        (r"\b(?:is|are) barred\b.*$", "are not barred"),
        (r"\bdoes not apply\b.*$", "applies by its enacted terms"),
        (r"\bhas rejected\b.*$", "does not foreclose the claim as pleaded"),
        (r"\b(?:is|are) wholly contractual\b.*$",
         "rests on duties imposed by law, not created by the contract"),
        (r"\bsound in contract\b.*$", "plead duties independent of the contract"),
        (r"\bmore akin to a first-party claim\b.*$",
         "is pleaded as a contractual covenant claim, not the tort attacked"),
        (r"\bimproperly alleges\b.*$", "pleads conduct the statute reaches"),
    ]
    for pat, repl in pairs:
        if re.search(pat, subj, re.I):
            base = re.sub(pat, "", subj, flags=re.I).strip(" ,;—-\"“”")
            # the replacement supplies its own verb — strip a dangling copula so we
            # never emit "X is is pleaded as..."
            base = re.sub(r"\s+(?:is|are|was|were)$", "", base, flags=re.I).strip()
            out = f"{base} {_agree_verb(base, repl)}".strip()
            # sentence case WITHOUT lowercasing proper nouns already in the heading
            return out[0].upper() + out[1:] if out else out
    return f"Response to the objection concerning {subj}".strip()


def _record_facts(slug: str) -> dict | None:
    """Build the RECORD-FACTS entry from the case's own evidence blocks.

    The bank was harvesting only m0's LEGAL findings, so the facts that defeat the
    movant's premise — the dated documents in the opponent's own words — never reached
    the brief. An opposition that argues duties-imposed-by-law in the abstract, without
    the record fact that makes the duty independent, is arguing half the case.

    Agnostic: reads whatever evidence blocks the case's stubs declare (E-tag, description,
    fact/quote), in whatever case. Contradictions between two of the movant's own
    statements are surfaced first — they are the strongest record material there is.
    """
    base = CASES / slug / "drafts"
    if not base.is_dir():
        return None
    blocks = []
    for p in sorted(base.rglob("*.md")):
        if "_DRAFT" in p.name or ".bak" in p.name:
            continue
        try:
            t = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        seg = re.search(r"^##\s*NEW EVIDENCE BLOCK(.*?)(?=^##\s|\Z)", t, re.S | re.M)
        if not seg:
            continue
        for m in re.finditer(
                r"^\s*[-*]\s*(E\d+)\s*[—\-–]\s*(.{4,90}?)\s*\|(.*?)(?=^\s*[-*]\s*E\d+|\Z)",
                seg.group(1), re.S | re.M):
            tag, desc, body = m.group(1), m.group(2).strip(), " ".join(m.group(3).split())
            fact = re.search(r"(?:fact|quote)\s*:\s*(.+?)(?:\||$)", body, re.I)
            if not fact:
                continue
            blocks.append((tag, desc, fact.group(1).strip()))
        if blocks:
            break
    if not blocks:
        return None
    lines = ["The objections are directed at the pleading in the abstract; the record "
             "answers them. The material facts are established by the movant's own "
             "documents:"]
    for tag, desc, fact in blocks[:8]:
        lines.append(f"{desc} — {fact} ({tag}).")
    return {"text": _clip_text(" ".join(lines), 1600), "cites": []}


_PREMISE_RE = re.compile(
    r"\*\*(Q\d+_[a-z_]+)(?:\s*[\u2014\u2013-]\s*([^*:\n]{3,80}))?:?\*\*"
    r"(.{40,2000}?)(?=\n\s*\*\*Q\d+_|\n#{1,6}\s|\Z)", re.S)
# grader stamps: m0 writes these as verdict tokens inside the finding; they are record
# STAMPS, never pleadable words. NOT-VERIFIED marks a finding that must not plead at all.
_STAMP_RE = re.compile(r"\b(?:HOLDS-AS-CITED|STRETCHED|CONTRARY|TARGETED|MOOT-NO-TARGET)\b\.?\s*")
_NOT_VERIFIED_RE = re.compile(r"NOT[- ]VERIFIED", re.I)


def _premise_findings(ana: str) -> list:
    """m0's premise-audit findings, keyed by their own identifier.

    Returns [(qid, label, body)] in record order. A finding stamped NOT-VERIFIED is
    dropped here (m0 has not read the opinion; nothing about it may plead). Stamps are
    scrubbed from the body; the finding's argument survives.
    """
    out = []
    for m in _PREMISE_RE.finditer(ana or ""):
        qid, label, body = m.group(1), (m.group(2) or "").strip(), m.group(3)
        if _NOT_VERIFIED_RE.search(body):
            continue
        body = _clean_prose(_STAMP_RE.sub("", body))
        if len(body) < 60:
            continue
        out.append((qid, label, body))
    return out


# A section title states OUR proposition. The premise audit's question ids are procedure
# names ("spine_authority", "pinpoint_granularity") -- read aloud in a brief they are work
# product. Each question resolves to the proposition its finding establishes; the finding's
# own label (the subject m0 named: "UTPCPL (Count V)") qualifies it.
_PREMISE_TITLES = {
    "whose_paper": "The filing is a party's request for relief, not an order of the Court",
    "our_pleading": "The objection does not answer the count as pleaded",
    "answer_from_our_pleading": "The objection does not answer the count as pleaded",
    "enacted_text": "The enacted text controls",
    "pinpoint_granularity": "The operative subsections of the enacted text",
    "binding_weight": "The controlling authority the movant omits",
    "spine_authority": "The controlling authority the movant omits",
    "holding_check": "The authority does not hold what the movant says it holds",
    "stretched_holding": "The authority does not hold what the movant says it holds",
    "doctrine_scope": "The doctrine is a general rule, not the movant's rule",
    "treatise": "The settled statement of the doctrine",
    "treatise_confrontation": "The settled statement of the doctrine",
    "remedy_targeting": "The objections target remedies, not defects",
    "remedy_stripping": "The objections target remedies, not defects",
    "concessions": "What the movant did not raise is waived",
    "waiver_scope": "The scope of the waiver under the rule",
    "answer_with_record": "The movant's own record answers the objection",
}


def _premise_heading(qid: str, label: str) -> str:
    key = qid.split("_", 1)[1] if "_" in qid else qid
    base = _PREMISE_TITLES.get(key)
    if base and label:
        return f"{base}: {label}"
    if base:
        return base
    return label or key.replace("_", " ").capitalize()


def _concessions(ana: str) -> dict | None:
    """Build the WAIVER / CONCESSIONS entry from m0's own waiver findings.

    What the movant did NOT raise is an argument in its own right, and it lives in the
    analysis as prose rather than as a per-heading answer — so it matched no heading and
    never reached a draft. Agnostic: lifts whatever waiver findings m0 produced, with the
    rule's own exceptions intact (overstating waiver is a defect the opponent flips).
    """
    # IDENTIFIER FIRST: m0's premise audit names its waiver findings (Q9_concessions,
    # Q13_waiver_scope). Take them whole -- the rule's exceptions are inside them. The
    # sentence regex below is the fallback for a record written without the audit.
    _q = [b for (q, _l, b) in _premise_findings(ana)
          if re.match(r"Q\d+_(?:concessions|waiver_scope)$", q)]
    if _q:
        text = "What the movant did not raise is itself a concession. " + " ".join(_q)
        return {"text": _clip_text(text, 1800), "cites": _cites_in(" ".join(_q))}
    hits = []
    for m in re.finditer(
            r"([^.\n]{0,200}\bwaiv\w+[^.\n]{0,400}\.)", ana, re.I):
        s = " ".join(m.group(1).split())
        if len(s) < 60:
            continue
        if not re.search(r"\b\d{3,4}\([a-z]\)|\bRule\s+\d|\bPa\.R\.C\.P", s, re.I):
            continue
        if s not in hits:
            hits.append(s)
    if not hits:
        return None
    text = ("What the movant did not raise is itself a concession. " + " ".join(hits[:3]))
    return {"text": _clip_text(text, 1400), "cites": _cites_in(" ".join(hits[:3]))}


_COURT_PAREN = re.compile(
    r"^(?:Pa\.|Pa\.\s?(?:Super|Cmwlth)\.|[1-9](?:st|nd|rd|th)\s+Cir\.|[EMWN]\.D\.\s?Pa\.|"
    r"U\.S\.|D\.\s?[A-Z][a-z]+\.?|S\.\s?Ct\.)(?:\s+\d{4})?$", re.I)


def _auth_str(name: str, cite: str, court: str) -> str:
    out = f"{name}, {cite}" if name else cite
    return f"{out} ({court})" if court else out


def _weight_ledger(ana: str, slug: str = "") -> dict | None:
    """Build the BINDING-WEIGHT LEDGER entry from m0's own classification rows.

    m0 writes the ledger as indented list rows ("- Case, cite — Court — PERSUASIVE ONLY"),
    which the bulleted-finding harvester skips, so the single strongest structural point —
    that a categorical rule of state law rests on decisions that do not bind this forum —
    never reached a draft. Agnostic: reads whatever rows m0 produced.
    """
    # Parse by CONTENT, not by a fixed em-dash arity: m0 states the weight inside a
    # prose clause ("— Federal district court; PERSUASIVE ONLY. Progressive cites it
    # for ..."), and a strict-shape regex silently dropped the entire ledger.
    try:
        _names_by_cite = _cite_name_map(slug) if slug else {}
    except Exception:
        _names_by_cite = {}
    _CITE = re.compile(r"\b\d{1,4}\s+(?:A\.\d[a-z]{0,2}|Pa\.(?:\s?Super\.)?|U\.S\.|"
                       r"S\.\s?Ct\.|F\.\s?Supp\.\s?\d?[a-z]{0,2}|F\.\d[a-z]{0,2})\s+\d{1,4}\b")
    persuasive, binding, seen = [], [], set()
    for raw in ana.splitlines():
        ln = raw.strip()
        if not ln.startswith(("-", "*")):
            continue
        low = ln.lower()
        is_p = ("persuasive only" in low or "not binding" in low
                or "does not bind" in low)
        is_b = (not is_p) and re.search(r"\bbind(?:s|ing)\b", low) is not None
        if not (is_p or is_b):
            continue
        mc = _CITE.search(ln)
        if not mc:
            continue
        cite = mc.group(0)
        if cite in seen:
            continue
        seen.add(cite)
        # name: text before the cite, stripped of markdown and leading bullet
        pre = ln[:mc.start()].strip(" -*_")
        pre = pre.replace("**", "").replace("__", "").strip().rstrip(",").strip()
        # NAME by identifier: the record names the case somewhere (its own pleaded
        # authorities, the ledger, the spine); a row with no name before the cite must
        # not repeat the cite as its name.
        pre = pre.strip("*_ ").rstrip(",").strip()
        name = pre if (pre and " v. " in pre) else (_names_by_cite.get(cite) or "")
        if not name:
            # the record names the case somewhere else ("*Stevenson v. Economy Bank of
            # Ambridge*, 197 A.2d 721"): resolve by the cite identifier across the record
            # the whole record, not the ADVERSE-FILING slice: the name may sit in
            # OPERATIVE FACTS or the damages ledger
            try:
                _rec = (CASES / slug / "CASE_FACTS.md").read_text(encoding="utf-8") if slug else ana
            except Exception:
                _rec = ana
            mm = re.search(r"\*([A-Z][^*\n]{3,90} v\. [^*\n]{2,90})\*,?\s*" + re.escape(cite), _rec)
            if mm:
                name = mm.group(1).strip()
        # COURT is a parenthetical that reads as a court; prose after a dash is not one
        court = ""
        mp = re.search(r"\(([^)]{2,40})\)", ln[mc.end(): mc.end() + 60])
        if mp and _COURT_PAREN.match(mp.group(1).strip()):
            court = mp.group(1).strip()
        row = (name[:90], cite, court[:40])
        (persuasive if is_p else binding).append(row)
    if not persuasive:
        return None
    # LEAD WITH THE ARITHMETIC. The closing count is the point of the ledger; placed last
    # it was cut by the length cap, leaving a list with no conclusion.
    # Agree the verb with the count: "1 of the authorities ... are decisions" is a
    # grammar error in the first sentence of the strongest structural point in the brief.
    _n = len(persuasive)
    if _n == 1:
        lines = ["One of the authorities the movant advances is a decision of a court "
                 "that does not bind this Court on a question of Pennsylvania law. "]
    else:
        _word = {2: "Two", 3: "Three", 4: "Four", 5: "Five", 6: "Six", 7: "Seven",
                 8: "Eight", 9: "Nine"}.get(_n, str(_n))
        lines = [f"{_word} of the authorities the movant advances are decisions of "
                 f"courts that do not bind this Court on a question of Pennsylvania law. "]
    # "Classified by issuing court:" is an internal label -- the per-authority sentences
    # below already state each court and its weight. A label read aloud in a brief is
    # work-product leakage, so introduce the list as argument instead.
    lines[0] += ("A categorical proposition of state law cannot rest on authority this "
                 "forum is free to disregard.")
    for n, c, ct in persuasive:
        lines.append(f"{_auth_str(n, c, ct)} — PERSUASIVE ONLY; a federal court sitting in "
                     f"diversity predicts state law, it does not make it.")
    # The binding side must ARGUE, not enumerate. A run of "Case, cite (Pa. 1957) —
    # binding." nine times over is classifier output wearing a brief's clothes: it tells
    # the court nothing it cannot read off the reporter, and a mid-list budget clip can
    # sever the final citation. State the asymmetry once, then cite in a normal string.
    if binding:
        names = [_auth_str(n, c, ct) for n, c, ct in binding]
        if len(names) == 1:
            lines.append(f"Against that, {names[0]} binds this Court directly.")
        else:
            head = "; ".join(names[:-1])
            lines.append(
                f"Against that stands authority this Court is not free to disregard: "
                f"{head}; and {names[-1]}. Each is a decision of a Pennsylvania court "
                f"of review, and on a question of Pennsylvania law the movant's federal "
                f"predictions yield to them.")
    return {"text": _clip_text(" ".join(lines), 1400),
            "cites": ([c for _, c, _ in persuasive][:6]
                      + [c for _, c, _ in binding][:6])}


# Deterministic fold sections m0 writes into the analysis. Each is a guarantee that
# something the model omitted gets said; none of them is keyed to a movant heading, so
# heading-matching must not decide whether they reach the brief.
# The fold-section contract is OWNED BY m0 (the producer). m1 only drafts, so it
# imports the list rather than keeping a second copy that can drift.
from pipeline.case_facts_refresh import FOLD_SECTIONS as _FOLD_SECTIONS


def _fold_texts(ana: str) -> list:
    """Every bullet that belongs to a deterministic fold section.

    These are arguments the folds own. If one also appears as some other entry's body,
    that entry is emitting a fold's argument under a heading the fold did not choose.
    """
    import re as _re
    out = []
    for _fs in _FOLD_SECTIONS:
        marker = _fs[0]
        m = _re.search(r"^#*\s*" + _re.escape(marker) + r"[^\n]*\n", ana, _re.M)
        if not m:
            continue
        _rest = ana[m.end():]
        _ends = [x.start() for x in (
            _re.search(r"(?m)^\s{0,3}#{1,6}\s", _rest),
            _re.search(r"(?m)^\*\*[^*\n]{4,80}?:?\*\*", _rest),
        ) if x]
        body = _rest[: min(_ends)] if _ends else _rest
        for x in _re.finditer(
                r"^\s*[-*]\s+(.{40,}?)(?=\n\s*[-*]\s|\n#{1,6}\s|\Z)", body, _re.S | _re.M):
            out.append(" ".join(_clean_prose(x.group(1)).split()))
    return [t for t in out if len(t) >= 60]


def _name_cites(cites: str, slug: str) -> str:
    """Prefix each bare reporter citation in a footnote with its case name.

    Footnote bodies were assembled from raw cite strings while the body text named its
    authorities properly, so a filed brief carried footnotes reading "723 A.2d 1053" with
    no indication of what case that is -- a citation a court cannot check at a glance.
    The name map already exists (_cite_name_map); this applies it at the footnote boundary.
    Statutes are left alone: "42 Pa.C.S. s 8371" already identifies itself.
    """
    if not cites or not slug:
        return cites
    try:
        names = _cite_name_map(slug) or {}
    except Exception:
        return cites
    if not names:
        return cites
    out = []
    for part in re.split(r";\s*", str(cites)):
        p = part.strip()
        if not p:
            continue
        nm = names.get(p)
        # only prefix when the name is not already present in this segment
        if nm and nm.split(" v.")[0].split()[0].lower() not in p.lower():
            out.append(f"{nm}, {p}")
        else:
            out.append(p)
    return "; ".join(out)


_SUP_MARK = re.compile("[\u2070\u00b9\u00b2\u00b3\u2074-\u2079]+(?:,[\u2070\u00b9\u00b2\u00b3\u2074-\u2079]+)*")


def _clean_case_name(name: str) -> str:
    """A case name carries no definition superscripts (they corrupt the citation)."""
    n = _SUP_MARK.sub("", str(name or ""))
    return " ".join(n.split()).strip(" ,;")


def _cite_name_map(slug: str) -> dict:
    """cite -> case name, from every source that knows one."""
    out = {}
    if not slug:
        return out
    try:
        for _c, _row in (_ledger_rows(slug) or {}).items():
            if _row and _row[0]:
                out.setdefault(_c, str(_row[0]).strip())
    except Exception:
        pass
    try:
        import pipeline.case_facts_refresh as _cfr_local
    except Exception:
        _cfr_local = None
    if _cfr_local is not None:
        for _fn in ("_our_pleaded_authorities", "_their_authorities"):
            try:
                for _c, _n in (getattr(_cfr_local, _fn)(slug) or {}).items():
                    if _n:
                        out.setdefault(_c, str(_n).strip())
            except Exception:
                continue
    out = {k: _clean_case_name(v) for k, v in out.items()}
    return {k: v for k, v in out.items() if v and " v. " in v}


def _point_body(p: str) -> str:
    """A point's body: heading and footnote definitions removed."""
    b = re.sub(r"^\*\*\d+\..*?\*\*", "", p or "", count=1, flags=re.S)
    b = re.sub(r"(?m)^\[\^\d+\]:.*$", "", b)   # footnote DEFINITIONS
    b = re.sub(r"\[\^\d+\]", "", b)             # inline footnote MARKERS
    return b


def _dedupe_split(text: str) -> list:
    """Sentences for DEDUPE KEYING only.

    The shared splitter is deliberately conservative: it refuses to break after "supra."
    or after a citation's closing "(Pa. 1981)." so citations survive intact. That is right
    for prose, but it fuses a restated black-letter rule to whatever citation preceded it,
    giving the same rule a different key in every point. For keying we split those two
    boundaries as well.
    """
    out = []
    for s0 in _split_sents(text or ""):
        parts = re.split(r"(?<=supra\.)\s+(?=[A-Z0-9])|(?<=\)\.)\s+(?=[A-Z])", s0)
        out.extend(p for p in (x.strip() for x in parts) if p)
    return out


def _collapse_immediate_repeats(text: str) -> str:
    """Collapse a chunk repeated immediately after itself, keeping one copy.

    m1 emits a short-cite or case name twice in a row when two sources contribute the same
    authority ("42 Pa.C.S. s 8371, supra. 42 Pa.C.S. s 8371, supra."). A regex with a
    backreference cannot do this reliably: re.sub CONSUMES a span even when the callback
    returns it unchanged, so an earlier non-matching pair swallows the real duplicate. So
    scan by index instead, longest repeat first, at word boundaries only.

    Comparison is length-preserving (curly->straight quotes, casefold, whitespace->space)
    so indices map 1:1 back onto the original text. Case-agnostic: keys on the repetition,
    never on a party or authority name.
    """
    if not text:
        return text
    norm = (text.replace("\u2019", "'").replace("\u2018", "'")
                .replace("\u201c", '"').replace("\u201d", '"').lower())
    norm = re.sub(r"\s", " ", norm) + " "      # pad: a repeat may end the text
    text_p = text + " "
    assert len(norm) == len(text_p)

    n = len(norm)
    cuts, i = [], 0
    while i < n:
        if i and norm[i - 1] not in " (\"'":
            i += 1
            continue
        hit = 0
        for L in range(90, 11, -1):
            if i + 2 * L > n:
                continue
            a = norm[i:i + L]
            if a != norm[i + L:i + 2 * L]:
                continue
            core = a.strip()
            # only citation-like or clause-like chunks, and never pure punctuation
            if len(core) < 12 or core[-1] not in ".," or not re.search(r"[a-z]", core):
                continue
            cuts.append((i + L, i + 2 * L))
            hit = L
            break
        i = i + hit if hit else i + 1

    if not cuts:
        return text
    out, prev = [], 0
    for a, b in cuts:
        out.append(text_p[prev:a])
        prev = b
    out.append(text_p[prev:])
    return "".join(out)[:-1] if out else text


def _rule_key(sent: str) -> str:
    """Identity of a black-letter rule statement, for duplicate detection.

    The same rule is restated with different supporting cites attached ("...inequitable.
    Mitchell v. Moore, 729 A.2d 1200 (Pa. Super. 1999)." in one point, bare in another).
    A rule's identity is its black-letter text, so cut the key at the first case name or
    reporter citation. Case-agnostic: matches citation SHAPE, never a named authority.
    """
    k = (sent or "").replace("\u2019", "'").replace("\u2018", "'")
    k = " ".join(k.split()).lower()
    cut = len(k)
    for pat in (r"\b[a-z][\w.'-]*\s+v\.\s",
                r"\b\d{1,4}\s+[a-z]{1,3}\.\s?\d?[a-z]?\.?\d*d?\s+\d{1,4}\b",
                r"\bsee\b|\baccord\b|\bsupra\b|\bid\."):
        m = re.search(pat, k)
        if m and m.start() < cut:
            cut = m.start()
    k = k[:cut].strip(" .,;:\u2014-")
    # drop any lead-in before the rule's own spine
    m2 = _re.search(
        r"\b[a-z][\w'-]*(?:\s+[a-z][\w'-]*){0,4}\s+"
        r"(?:requires|means|is established|is defined|consists of|exists where|"
        r"includes|applies where|obtains where)\b", k)
    if m2 and m2.start() > 0:
        k = k[m2.start():]
    return k if len(k) >= 60 else ""


_RECORD_SIGNAL = re.compile(r"\b\d{1,2}/\d{1,2}/\d{4}\b|\b(?:january|february|march|april|may|june|july|august|september|"
                            r"october|november|december) \d{1,2}, \d{4}\b|\$\d|\b\d{6,}\b|[\u201c\"][^\u201d\"]{12,}[\u201d\"]")


def _record_key(sent: str) -> str:
    """Identity of a RECORD sentence (a dated act, an amount, a document number, quoted words) for
    duplicate detection. m0's authority fold appends the same record-application paragraph to every
    opposing authority, so the sale date reached one brief 16 times and the policy number 11. A record
    fact is argued once -- where the rule is applied -- and referenced after. Case-agnostic: keys on
    the sentence's own text when it carries a record signal, never on a named party or doctrine."""
    k = " ".join((sent or "").replace("\u2019", "'").split()).lower()
    if len(k) < 60 or not _RECORD_SIGNAL.search(k):
        return ""
    return re.sub(r"[^a-z0-9/$ ]", "", k)


def _dedupe_rule_statements(text: str, keep: int = 1) -> str:
    """Collapse a black-letter rule restated verbatim across points.

    The same rule sentence ("Unjust enrichment requires (1)... (2)... (3)...") reached the
    brief four times because several folds each carry the authority that states it. A
    judge reading the same sentence four times skims; the argument is diluted and the
    filing reads as padded. Keep the FIRST statement of each rule -- where it is argued --
    and drop later verbatim repeats, but never touch a sentence that appears only once and
    never drop the only sentence in a point.

    Case-agnostic: keys on sentence identity, not on any doctrine or citation.
    """
    if not text:
        return text
    parts = re.split(r"(?m)^(?=\*\*\d+\. )", text)
    head, pts = parts[0], parts[1:]
    counts, order = {}, []
    for p in pts:
        for sent in _dedupe_split(_point_body(p)):
            k = _rule_key(sent) or _record_key(sent)
            if not k:
                continue
            if k not in counts:
                order.append(k)
            counts[k] = counts.get(k, 0) + 1
    repeated = {k for k in order if counts[k] > keep}
    if not repeated:
        return text
    seen, out, dropped = {}, [], 0
    # a black-letter rule may be restated with a different lead-in clause; key on the
    # rule's own words so "Bad faith includes..." is recognised wherever it reappears

    for p in pts:
        _hd = re.match(r"^\*\*\d+\..*?\*\*", p, re.S)
        _head_txt = _hd.group(0) if _hd else ""
        sents = _dedupe_split(_point_body(p))
        if len(sents) <= 1:
            # keep the point, but still COUNT its sentence: a one-sentence point that
            # states the rule is the rule's occurrence for every later point.
            for _s1 in sents:
                _k1 = _rule_key(_s1) or _record_key(_s1)
                if _k1 in repeated:
                    seen[_k1] = seen.get(_k1, 0) + 1
            out.append(p)
            continue
        kept_s, changed = [], False
        for sent in sents:
            k = _rule_key(sent) or _record_key(sent)
            if k in repeated:
                seen[k] = seen.get(k, 0) + 1
                if seen[k] > keep:
                    changed, dropped = True, dropped + 1
                    continue
            kept_s.append(sent)
        if not changed or not kept_s:
            out.append(p)
            continue
        # rebuild the point, preserving its footnote definition lines verbatim
        fns = re.findall(r"(?m)^\[\^\d+\]:.*$", p)
        body = " ".join(kept_s).strip()
        _mk = re.findall(r"\[\^\d+\](?!:)", p)
        if _mk and _mk[0] not in body:
            body = body.rstrip() + _mk[0]
        out.append(f"{_head_txt} {body}".strip()
                   + ("\n\n" + "\n".join(fns) if fns else "") + "\n\n")
    if dropped:
        print(f"  [m1] REPEATED RULE STATEMENTS COLLAPSED: {dropped}")
    return head + "".join(out)


def _drop_stub_points(text: str, min_body: int = 110) -> str:
    """Remove any numbered point whose body is too thin to argue, and renumber.

    A heading such as "The objection as to Count V is not well taken." with no body is
    worse than silence: the court reads an asserted conclusion the filer did not support
    and may treat the count as abandoned. Case-agnostic -- measures the body of whatever
    points exist, names no count and no authority.

    Body excludes the heading, the footnote marker, and the footnote definition line.
    """
    if not text:
        return text
    parts = re.split(r"(?m)^(?=\*\*\d+\. )", text)
    head, pts = parts[0], [p for p in parts[1:] if re.match(r"\*\*\d+\. ", p)]
    kept, dropped = [], []
    # Resolve orphan footnote DEFINITION LINES before any renumbering: once survivors are
    # renumbered into 1..N a dropped id may name a SURVIVOR (dropping id 2 of 1,2,3 makes the
    # old point 3 the new point 2), so id-based removal after renumbering deletes live text.
    dropped_ids = []
    for p in pts:
        body = re.sub(r"^\*\*\d+\..*?\*\*", "", p, count=1, flags=re.S)
        body = re.sub(r"(?m)^\[\^\d+\]:.*$", "", body)      # footnote definitions
        body = re.sub(r"\[\^\d+\]", "", body)                # footnote markers
        body = " ".join(body.split())
        if len(body) < min_body:
            dropped.append(re.sub(r"^\*\*\d+\.\s*", "", p.split("**")[1] or "")[:58])
            # A dropped point's footnote DEFINITION can live in a later block rather than
            # inside this segment, so dropping the point orphans it: the filing then shows
            # "[^19]: Hart v. Arnold, 884 A.2d 316" with no point 19 anywhere. Record the
            # id so the orphan definition goes out with the point that owned it.
            _m_id = re.match(r"\*\*(\d+)\. ", p)
            if _m_id:
                dropped_ids.append(_m_id.group(1))
            continue
        kept.append(p)
    if not dropped:
        return text
    # renumber survivors so numbering and footnote ids stay consistent
    out = []
    for i, p in enumerate(kept, 1):
        old = re.match(r"\*\*(\d+)\. ", p).group(1)
        p = re.sub(r"^\*\*\d+\. ", f"**{i}. ", p, count=1)
        p = p.replace(f"[^{old}]", f"[^{i}]")
        out.append(p)
    print(f"  [m1] STUB POINTS DROPPED: {len(dropped)} "
          f"({'; '.join(d.strip() for d in dropped[:3])}"
          f"{'...' if len(dropped) > 3 else ''})")
    # Capture the orphans' exact definition lines from the PRE-renumber text, keyed by the
    # ids that no surviving point owns. Matching on the literal line means the later removal
    # cannot collide with a renumbered survivor.
    _kept_ids = {re.match(r"\*\*(\d+)\. ", k).group(1) for k in kept if re.match(r"\*\*(\d+)\. ", k)}
    _orphan_lines = []
    for _old in dropped_ids:
        if _old in _kept_ids:
            continue                     # that id still belongs to a real point
        for _m in re.finditer(r"(?m)^\[\^" + re.escape(_old) + r"\]:.*$", text):
            _orphan_lines.append(_m.group(0))
    result = head + "".join(out)
    for _line in _orphan_lines:
        result = result.replace(_line + "\n", "").replace(_line, "")
    return result


def _name_bare_cites(text: str, names: dict) -> str:
    """Expand a bare leading reporter citation into "Name, cite"."""
    if not text or not names:
        return text
    import re as _re
    for _c, _n in names.items():
        _surname = _n.split(",")[0].split(" v. ")[0][:14]
        _pat = _re.compile(r"(^|\s|\*\*)" + _re.escape(_c) + r"(?=\s*[\u2014\-,.]|\s)")

        def _fix(m, _c=_c, _n=_n, _surname=_surname):
            # already named immediately before THIS occurrence? compare on a normalised
            # form: the stored name carries a straight apostrophe ("D'Ambrosio") while
            # the draft may carry a curly one, and a raw string compare then misses --
            # prepending a SECOND name to a case that was already named.
            def _norm(x):
                return (x.replace("\u2019", "'").replace("\u2018", "'")
                         .replace("\u201c", '"').replace("\u201d", '"').lower())
            _before = _norm(m.string[max(0, m.start() - 160): m.start()])
            if _surname and _norm(_surname) in _before:
                return m.group(0)
            return f"{m.group(1)}{_n}, {_c}"

        text = _pat.sub(_fix, text, count=1)
    return text


def _fold_entries(slug: str, ana: str, bank: list) -> list:
    """Carry each deterministic fold section into the bank as its own entry."""
    import re as _re
    blob = " ".join(str(e) for e in bank)
    out = []
    for _fs in _FOLD_SECTIONS:
        marker, title = _fs[0], _fs[1]
        _suffix = _fs[2] if len(_fs) > 2 else ""
        m = _re.search(r"^#*\s*" + _re.escape(marker) + r"[^\n]*\n", ana, _re.M)
        if not m:
            continue
        _rest = ana[m.end():]
        _ends = [x.start() for x in (
            _re.search(r"(?m)^\s{0,3}#{1,6}\s", _rest),
            # a bold label starting its own line: "**Document Classification (Q1):**"
            _re.search(r"(?m)^\*\*[^*\n]{4,80}?:?\*\*", _rest),
        ) if x]
        body = _rest[: min(_ends)] if _ends else _rest
        # each bullet in the fold is a discrete point
        pts = [_clean_prose(x.group(1)) for x in _re.finditer(
            r"^\s*[-*]\s+(.{40,}?)(?=\n\s*[-*]\s|\n#{1,6}\s|\Z)", body, _re.S | _re.M)]
        pts = [p for p in pts if len(p) >= 40]
        if not pts:
            continue
        # One bullet = one point when each bullet is its own authority. Short bullets that
        # share the generic fold title are sub-points of ONE argument -- merge them rather
        # than repeat a heading verbatim down the brief.
        _multi = len(pts) > 1
        _named = sum(1 for _p in pts if _re.match(
            r"\s*\*{0,2}[A-Z][A-Za-z.,'\u2019&\- ]{2,60}?\s+v\.\s", _p))
        if _multi:
            _is_named = lambda _p: bool(_re.match(
                r"\s*\*{0,2}[A-Z][A-Za-z.,'\u2019&\- ]{2,60}?\s+v\.\s", _p))
            _un = [_p for _p in pts if not _is_named(_p)]
            _nm = [_p for _p in pts if _is_named(_p)]
            if len(_un) > 1 and sum(len(_p) for _p in _un) <= 2600:
                _un = [" ".join(" ".join(_p.split()).rstrip(".") + "." for _p in _un)]
            pts = _nm + _un
            _multi = len(pts) > 1
        for _k, _p in enumerate(pts, 1):
            _t = _p.strip()
            probe = " ".join(_t.split())[:90]
            if probe and probe in " ".join(blob.split()):
                continue
            cites = _re.findall(
                r"\d{2,4}\s+(?:A\.\d[a-z]{0,2}|Pa\.|U\.S\.|F\.\s?Supp\.?\s?\d?[a-z]{0,2}|"
                r"F\.\d[a-z]{0,2})\s+\d+", _t)
            # a bullet that names one authority titles itself with that authority
            _RPTR = (r"(?:A\.\d[a-z]{0,2}|Pa\.(?:\s*(?:Super|Cmwlth)\.)?|U\.S\.|"
                     r"F\.\s?Supp\.?\s?\d?[a-z]{0,2}|F\.\d[a-z]{0,2}|S\.Ct\.)")
            _lead = _re.match(
                r"\s*\*{0,2}([A-Z][A-Za-z.,'\u2019&\- ]{2,60}?\s+v\.\s+"
                r"[A-Z][A-Za-z.,'\u2019&\- ]{2,70}?,\s*\d{2,4}\s+" + _RPTR +
                r"\s+\d+)[.,]?\*{0,2}\s*", _t)
            if _multi and _lead:
                _title = _lead.group(1).strip().rstrip(".,")
                # body repeats the name+cite it was titled with
                _rest_t = _t[_lead.end():].lstrip(". ").strip()
                for _rep in (_title, _title.split(",")[0]):
                    if _rep and _rest_t.startswith(_rep):
                        _cand = _rest_t[len(_rep):].lstrip(". ,").strip()
                        # never leave a dangling short-form ("Hart v. <sentence>")
                        if not _re.match(r"^[A-Z][A-Za-z.'\u2019\- ]{0,40}\s+v\.\s", _cand):
                            _rest_t = _cand
                        break
                if len(_rest_t) >= 40:
                    # Stripping "Name, cite," can leave the body opening mid-sentence
                    # ("addressed the interplay between..."). _sanitize repairs that when the
                    # text is consumed, but the BANK is a record other organs read, so it must
                    # not store a fragment either (skill rule 0a: the artifact is a second
                    # source of the same defect).
                    _t = _restore_sentence_open(_rest_t, _title)
                _title = _title + _suffix
            else:
                _title = title
            # the record can carry a severed sentence from a length-capped m0 answer
            # ("...rendering Wen"); the bank must not store it, and the whole row is
            # better than a row missing its last clause
            _t = _whole_sentences_only(_t)
            if len(_t) < 40:
                continue
            out.append({
                "heading": _title,
                "text": _t,
                "tier": "SETTLED",
                "pattern": "",
                "cites": sorted(set(cites)),
                "source": "fold_section",
            })
    return out


def _whole_sentences_only(text: str) -> str:
    """Drop a trailing incomplete sentence; a complete text comes back identical."""
    t = (text or "").rstrip()
    if not t or re.search(r"[.!?][\"\u201d')\]]*$", t):
        return t
    best = -1
    for m in re.finditer(r"[.!?][\"\u201d')\]]*(?=\s)", t):
        pre = t[max(0, m.start() - 6): m.start() + 1]
        if pre.endswith("...") or re.search(
                r"\b(?:Pa|Co|Inc|Corp|v|vs|Cir|Super|Cmwlth|No|Nos|Ltd|Mr|Mrs|Dr|St|Assn|Ins|Cas|Mut|Nat)\.$",
                pre):
            continue
        best = m.end()
    return t[:best].rstrip() if best > 0 else ""


def _ledger_rows(slug: str) -> dict:
    """Parse m0's binding-weight ledger: cite -> (name, court, binds, cited_for).

    m0 states, per authority, which court issued it, whether it binds this forum, and the
    proposition it is cited for. That is the analysis. m1 carries it.
    """
    import re as _re
    out = {}
    try:
        import pipeline.case_facts_refresh as _cfr
        from pathlib import Path as _P
        _d = (_P(str(_eroot() / "obsidian_sync/cases")) / slug / "CASE_FACTS.md")
        if not _d.exists():
            return out
        doc = _d.read_text(encoding="utf-8")
        blk = _cfr._current_block(doc, _cfr._SECTION_HEADERS["rebuttal"]) or doc
    except Exception:
        return out
    for m in _re.finditer(r"^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*$",
                          blk, _re.M):
        cell, court, binds, cited = (g.strip() for g in m.groups())
        if cell.lower().startswith("cite") or set(cell) <= set("-: "):
            continue
        cm = _re.search(r"(\d{2,4}\s+(?:A\.\d[a-z]{0,2}|Pa\.|U\.S\.|F\.Supp\.?\d?[a-z]{0,2}|F\.\d[a-z]{0,2})\s+\d+)",
                        cell)
        if not cm:
            continue
        cite = cm.group(1).strip()
        name = cell[:cm.start()].rstrip(", ").strip()
        out[cite] = (name, court, binds, cited)
    return out


def _authority_entries(slug: str, ana: str, bank: list) -> list:
    """Bank entries for required authorities no existing entry mentions.

    The opposition must ANSWER every authority: ours because dropping it abandons the
    count it supports, theirs because an authority left unanswered reads as conceded.
    """
    import re as _re
    blob = " ".join(str(e) for e in bank)
    _LEDGER = _ledger_rows(slug)
    # The authoritative required set, straight from the filings.
    required = {}
    try:
        import pipeline.case_facts_refresh as _cfr
        _theirs = _cfr._their_authorities(slug) or {}
        _ours = _cfr._our_pleaded_authorities(slug) or {}
        for _c, _n in _theirs.items():
            required[_c] = (_n or "", "MOVANT'S")
        for _c, _n in _ours.items():
            # ours wins the label: an authority we pleaded must never read as theirs
            required[_c] = (_n or required.get(_c, ("", ""))[0], "OURS")
    except Exception as _e:
        print(f"  [authority] required-set unavailable ({_e}); falling back to prose scan")

    # the authority fold m0 wrote, plus any binding-weight ledger line
    cites = []
    _PAT = (r"(?:\*\*|\*)([^*]{4,90}?)(?:\*\*|\*)?,\s*"
            r"(\d{2,4}\s+(?:A\.\d[a-z]{0,2}|Pa\.|U\.S\.)\s+\d+)"
            r"(?:\*\*)?(?:\s*\*\(([^)]*)\)\*)?([^\n]*)")
    for m in _re.finditer(_PAT, ana):
        name, cite, tag, tail = (m.group(1).strip(), m.group(2).strip(),
                                 (m.group(3) or "").strip(), (m.group(4) or "").strip())
        if cite in blob:
            continue
        cites.append((name, cite, tag, tail))
    # (filtered against fold-answered authorities below)
    # Anything required that neither the bank nor the prose scan produced. An authority
    # already ANSWERED by a deterministic fold (a distinction, a characterization check)
    # is covered -- a bare stub after it duplicates the citation and adds nothing.
    _answered = set()
    for _e in bank:
        if not isinstance(_e, dict) or _e.get("source") != "fold_section":
            continue
        for _c in (_e.get("cites") or []):
            _answered.add(_c)
    # an authority a fold already ARGUES needs no ledger stub restating its cite
    cites = [c for c in cites if c[1] not in _answered]
    _scanned = {c for _n, c, _t, _tl in cites} | _answered
    for _c, (_n, _tag) in required.items():
        if _c in blob or _c in _scanned:
            continue
        _rule = ""
        try:
            # m0's organ: the LLM SELECTS the holding from the authority's OWN opinion text and the
            # form gate validates it; the deterministic keyword picker is the fallback only (it
            # chose a citing case's procedural history as Terletsky's "holding").
            _rule = _cfr._holding_sentence_llm(_c, _n) or _cfr._corpus_rule_sentence(_c, _n) or ""
        except Exception:
            try:
                _rule = _cfr._corpus_rule_sentence(_c, _n) or ""
            except Exception:
                pass
        cites.append((_n, _c, _tag, _rule))

    out = []
    seen = set()
    # SPINE_PLEAD_V1 -- the pleadable sentence is authority, not decoration.
    # m0_authority_spine's table carries, per authority, a PLEAD sentence written to be
    # dropped into a brief verbatim (plus corpus locators proving it). build_feed() only
    # reaches it for authorities whose vocabulary overlaps one of the MOVANT'S headings;
    # with three headings in the matrix, Gregg (245 A.3d 637) and Schwartz (932 A.2d 885)
    # missed that overlap and fell through to this guarantee path, which knows only the
    # ledger row -- so both emitted "cites it without stating the proposition", read as an
    # m0 analysis gap, and the holding m0 had already verified never reached the filing.
    # Look the cite up in SPINE here too: same table, same verification, no new prose.
    _plead = {}
    try:
        from pipeline.m0_authority_spine import SPINE as _SPINE, _con as _spine_con, \
            _verbatim as _spine_verbatim
        _sc = _spine_con()
        for _entries in _SPINE.values():
            for _e in _entries:
                _c = str(_e.get("cite", "")).strip()
                _b = str(_e.get("plead") or "").strip()
                if not _c or not _b or _c in _plead:
                    continue
                # only corpus-verified authority reaches a filing
                if not _spine_verbatim(_sc, _e.get("source", ""), _e.get("locators", [])):
                    continue
                _plead[_c] = (_b, str(_e.get("name", "")).strip())
                # KEY FORMAT: SPINE writes "245 A.3d 637 (Pa. 2021)"; the ledger, the
                # bank and the movant's filing all write "245 A.3d 637". An exact-string
                # lookup therefore never hits and every PLEAD sentence stays unused --
                # the whole defect, a parenthetical. Key both forms.
                _bare = re.sub(r"\s*\([^)]*\)\s*$", "", _c).strip()
                if _bare and _bare not in _plead:
                    _plead[_bare] = (_b, str(_e.get("name", "")).strip())
        _sc.close()
    except Exception as _pe:
        print(f"  [authority] SPINE plead lookup unavailable ({_pe})")
    for name, cite, tag, tail in cites:
        if cite in seen:
            continue
        seen.add(cite)
        whose = "MOVANT'S" if "MOVANT" in tag.upper() else (
            "OURS" if "OURS" in tag.upper() else "")
        rule = tail.lstrip(" \u2014-").strip()
        # Carry m0's ledger determination for this authority.
        _row = _LEDGER.get(cite)
        _nm = name or (_row[0] if _row else "")
        _nm = re.sub(r"[\u2070-\u209f\u00b2\u00b3\u00b9,]+(?=\s|$)", "", _nm).strip()   # OCR footnote superscripts in a case name
        _court = (_row[1] if _row else "").strip()
        _binds = (_row[2] if _row else "").strip()
        _for = (_row[3] if _row else "").strip()

        parts = []
        if _court:
            _ct = _court.strip()
            if not _ct.lower().endswith("court"):
                _ct += " Court"
            parts.append(f"{_nm}, {cite}, is a decision of the {_ct}")
            if _binds:
                if _binds.upper().startswith("NO"):
                    parts[-1] += " and does not bind this Court; it is persuasive only"
                else:
                    parts[-1] += " and binds this Court"
            parts[-1] += "."
        elif _nm:
            parts.append(f"{_nm}, {cite}.")
        if _for:
            _first = _for.split()[0] if _for.split() else ""
            # "UTPCPL", "ECOA", "D'Ambrosio" keep their case; ordinary words lowercase
            _keep = (len(_first) > 1 and (_first.isupper() or _first[1:2].isupper()
                     or not _first.isalpha()))
            _lead = _for if _keep else (_for[0].lower() + _for[1:])
            if whose == "MOVANT'S":
                parts.append(f"The movant cites it for {_lead}.")
            else:
                parts.append(f"It stands for {_lead}.")
        if rule and len(rule) > 40:
            _r = re.sub(r"^[^:]{0,40}:\*\*\s*", "", rule.strip())
            _r = _r.replace("**", "").strip()
            parts.append(_r if _r.rstrip().endswith(".") else _r + ".")
        # SPINE_PLEAD_V1: state the holding m0 verified for this authority. This runs
        # BEFORE the empty/thin fallbacks below, so a cite with a pleadable sentence is
        # never described as "cited without stating the proposition" -- the proposition
        # exists, corpus-verified, and belongs in the brief.
        _pl = _plead.get(cite)
        def _near_dup(a, b):            # same proposition in other words: >= 70% shared content tokens
            ta = {w for w in re.findall(r"[a-z]{4,}", a.lower())}; tb = {w for w in re.findall(r"[a-z]{4,}", b.lower())}
            return bool(ta) and len(ta & tb) / len(ta) >= 0.7
        if _pl and _pl[0] not in " ".join(parts) and not _near_dup(_pl[0], " ".join(parts)):
            parts.append(_pl[0] if _pl[0].rstrip().endswith(".") else _pl[0] + ".")
        if not parts:
            # m0 said nothing about this authority: say so, do not invent a holding.
            parts.append(f"{_nm or cite}, {cite}, appears in the movant's filing and is "
                         f"not addressed by any proposition the movant states.")
        # A BARE CITE IS NOT A REBUTTAL POINT. When m0 supplied neither a proposition
        # (_for) nor a rule, parts holds only "Name, cite." -- a heading with a citation
        # and no argument. _drop_stub_points then deletes it downstream, so the authority
        # vanishes from the filing SILENTLY: observed on Gregg v. Ameriprise (245 A.3d
        # 637) and Schwartz v. Rockey (932 A.2d 885), both live UTPCPL authorities the
        # corpus holds in full (35 chunks / 50KB). A dropped point is worse than a
        # flagged one -- nothing downstream says the authority went missing. Emit the
        # same explicit non-treatment sentence the empty branch uses, so the point
        # survives the stub filter and states honestly what the record shows.
        elif len(" ".join(parts).split()) < 40 and not _for and not (rule and len(rule) > 40):
            parts.append(f"The movant cites {_nm or cite} without stating the proposition "
                         f"it is offered for, and no rule of law is drawn from it. An "
                         f"authority invoked without a stated proposition establishes "
                         f"nothing the movant may rely on, and the pleaded count stands "
                         f"unanswered by it.")
        duty = " ".join(parts)          # rule already appended once above (SPINE order); never twice
        out.append({
            "heading": f"Authority: {name}",
            # a section title states OUR proposition; the case name alone states theirs
            "answer_heading": _proposition_from_body(duty, _nm or name),
            "text": _restore_sentence_open(duty, _nm or name),
            "tier": "SETTLED",
            "pattern": "",          # not heading-matched; emitted in the authority sweep
            "cites": [cite],
            "source": "authority_fold",
        })
    return out


def build_bank(slug: str) -> list:
    """ONE entry per heading, pattern anchored to that heading's own distinctive words."""
    ana = _analysis(slug)
    heads = build_matrix(slug)["briefs"][slug]["headings_found"]

    findings = []
    # A bullet sitting under one of m0's LEVER headings ("## L1 - WENK ... defeats their own
    # use of it") is already graded pro-us by the analysis engine: the file is the graded
    # lever set, and anything m0 rejected lives in its separate kill list. Re-deciding such a
    # bullet with a prose regex threw away the two strongest attacks in the case, because both
    # must QUOTE the movant's limiting rule in order to destroy it:
    #   - Wenk's own "as alleged herein" qualifier (they quoted the sentence without it);
    #   - *210(4), which quotes "shall not be cited for general reference" to prove the breach.
    # _ACCEPTS_LIMITATION read the quoted rule as OUR concession. Decide from m0's label, not
    # from the prose (skill rule: identifier over regex).
    for m in re.finditer(
            r"^\s*(?:\d+\.|[-*])\s+(.{60,1200}?)(?=\n\s*(?:\d+\.|[-*])\s|\n#{1,6}\s|\Z)",
            ana, re.S | re.M):
        body = _clean_prose(m.group(1))
        if len(body) < 60:
            continue
        if _under_lever_heading(ana, m.start()) or _serves_our_side(body):
            findings.append(body)
    # PREMISE AUDIT by identifier (pipeline/premise_audit.py is the single definition).
    # Each finding is a graded answer to one question of the adverse filing; it competes
    # for a heading below, and whatever no heading claims is emitted on its own (the
    # sweep after the heading loop), so no finding m0 wrote is silently dropped.
    _premise_ix = {}
    for _qid, _lbl, body in _premise_findings(ana):
        if _serves_our_side(body):
            _premise_ix[len(findings)] = (_qid, _lbl)
            findings.append(body)
    # Not every lever is a bullet. m0 writes some as PARAGRAPH prose under the lever heading --
    # L8 (*210(4)) is a quoted rule followed by its application, with no list marker anywhere --
    # so a bullet-only scan is blind to the whole lever no matter how it is graded. Take the
    # paragraphs under each "## Ln" heading as findings too.
    for hm in re.finditer(r"(?m)^#{1,6}\s*L\d+\b[^\n]*$", ana):
        nxt = re.search(r"(?m)^#{1,6}\s", ana[hm.end():])
        block = ana[hm.end():hm.end() + (nxt.start() if nxt else len(ana))]
        for para in re.split(r"\n\s*\n", block):
            if re.match(r"^\s*(?:\d+\.|[-*])\s", para):
                continue          # bullets already harvested above
            body = _clean_prose(para)
            if len(body) >= 60 and body not in findings:
                findings.append(body)

    bank, used = [], set()
    # STRUCTURAL ENTRIES FIRST: the weight ledger answers whichever heading leans hardest
    # on non-binding authority, and it must not be crowded out by per-heading findings.
    _led = _weight_ledger(ana, slug)
    if _led and heads:
        _cites_l = " ".join(_led["cites"]).lower()
        _best = max(heads, key=lambda h: sum(
            1 for t in _TOPIC.findall(h) if t.lower() in _led["text"].lower()))
        _anchor = [w for w in re.findall(r"[a-z]{5,}", _best.lower())
                   if w not in ("plaintiff", "defendant", "complaint", "against",
                                "should", "claims", "claim", "assert", "asserts")][:4]
        if _anchor:
            bank.append({
                "pattern": "|".join(re.escape(w) for w in _anchor),
                "tier": "SETTLED", "heading": "Binding-weight ledger",
                "answer_heading": "The authorities advanced do not bind this Court",
                "text": _led["text"], "cites": _led["cites"]})

    # WAIVER / CONCESSIONS — same treatment: a structural argument that answers no single
    # heading but belongs in the brief once.
    _conc = _concessions(ana)
    if _conc and heads:
        # anchor on a DIFFERENT heading than the ledger — three structural entries sharing
        # one anchor meant the once-only rule dropped two of them from the draft entirely.
        _ch = heads[min(1, len(heads) - 1)]
        _canchor = [w for w in re.findall(r"[a-z]{5,}", _ch.lower())
                    if w not in ("plaintiff", "defendant", "complaint", "against",
                                 "should", "claims", "claim", "assert", "asserts")][:4]
        if _canchor:
            bank.append({
                "pattern": "|".join(re.escape(w) for w in _canchor),
                "tier": "SETTLED", "heading": "Waiver and concessions",
                "answer_heading": "What the movant did not raise is waived",
                "text": _conc["text"], "cites": _conc["cites"]})

    # RECORD FACTS — the documents that defeat the movant's premise. Structural, stated
    # once, and placed with the other structural entries so it is never crowded out.
    _rf = _record_facts(slug)
    if _rf and heads:
        _rh = heads[min(2, len(heads) - 1)]
        _rfanchor = [w for w in re.findall(r"[a-z]{5,}", _rh.lower())
                     if w not in ("plaintiff", "defendant", "complaint", "against",
                                  "should", "claims", "claim", "assert", "asserts")][:4]
        if _rfanchor:
            bank.append({
                "pattern": "|".join(re.escape(w) for w in _rfanchor),
                "tier": "SETTLED", "heading": "Record facts",
                "answer_heading": "The record answers the objections",
                "text": _rf["text"], "cites": _rf["cites"]})

    # LEVERS FIRST: m0's graded levers answer the movant's FILING, not one of its headings.
    for _i, _f in enumerate(findings):
        if _i in used or not _lever_finding(ana, _f):
            continue
        used.add(_i)
        bank.append({
            "pattern": "",                     # not heading-matched; a threshold ground
            "tier": "RECORD",
            "heading": _lever_label(ana, _f) or "The movant's filing and authorities",
            "answer_heading": _lever_label(ana, _f) or "The movant's own authority defeats the objection",
            "text": _clip_text(_f),
            "cites": _cites_in(_f),
            "source": "lever_fold",
        })

    for h in heads:
        ranked = sorted(((_score(h, f), i, f) for i, f in enumerate(findings)), reverse=True)
        pick = next((t for t in ranked if t[0] > 0 and t[1] not in used), None) \
            or next((t for t in ranked if t[0] > 0), None)
        if not pick:
            continue
        used.add(pick[1])
        text = pick[2]
        # ANCHOR THE PATTERN TO THIS HEADING ALONE. m1's classify() scans the bank in order
        # and returns the FIRST tier match, so a pattern that also matches a sibling heading
        # silently steals it — that is how 12 headings collapsed onto 3 answers. Build the
        # anchor from words unique to this heading among all headings.
        _hw = [w for w in re.findall(r"[a-z]{5,}", h.lower())
               if w not in ("plaintiff", "defendant", "complaint", "against", "should",
                            "claims", "claim", "assert", "asserts", "purports")]
        _others = " ".join(x.lower() for x in heads if x != h)
        _uniq = [w for w in _hw if _others.count(w) == 0]
        anchor = (_uniq or _hw)[:3]
        if _uniq:
            pat = "|".join(re.escape(w) for w in anchor)
        else:
            # no unique word — require the heading's own phrase so it cannot match a sibling
            pat = re.escape(" ".join(h.lower().split()[:8]))
        bank.append({"pattern": pat, "tier": "RECORD", "heading": h,
                     "answer_heading": _our_heading(h),
                     "text": _restore_sentence_open(_clip_text(text), h),
                     "cites": _cites_in(text)})

    # PREMISE SWEEP: findings no heading picked are arguments in their own right
    # (waiver scope, controlling authority omitted, remedy-stripping posture ...).
    for _i, (_qid, _lbl) in _premise_ix.items():
        if _i in used:
            continue
        used.add(_i)
        _ph = _premise_heading(_qid, _lbl)
        bank.append({
            "pattern": "",
            "tier": "SETTLED",
            "heading": _ph,
            "answer_heading": _ph,
            "text": _clip_text(findings[_i], 1400),
            "cites": _cites_in(findings[_i]),
            "source": "premise_fold",
            "qid": _qid,
        })

    try:
        from pipeline.m0_authority_spine import SPINE, _kind, _con, _verbatim
        adverse = _newest_adverse(slug)
        tax_terms = sorted({t.lower() for t in _TOPIC.findall(adverse)})
        # CARRY THE AUTHORITY INTO THE ARGUMENT, not just a footnote. build_section()'s
        # report rows carry only cite/status — the PLEADABLE sentence lives in the SPINE
        # table itself. Reading the report alone left 27 authorities (the statute's own
        # subsections, the controlling decisions) out of every draft while they sat
        # corpus-verbatim one call away.
        con = _con()
        seen_cites = set()
        for pat, entries in SPINE.items():
            if not any(re.search(pat, t, re.I) for t in tax_terms):
                continue
            for e in entries:
                cite = str(e.get("cite", "")).strip()
                if not cite or cite in seen_cites:
                    continue
                # only VERIFIED authority reaches a filing
                if not _verbatim(con, e.get("source", ""), e.get("locators", [])):
                    continue
                seen_cites.add(cite)
                body = str(e.get("plead") or e.get("point") or "").strip()
                if not body:
                    continue
                kind = _kind(e)
                lead = ("The enacted text governs; decisions construe it. "
                        if kind in ("statute", "constitution") else "")
                name = str(e.get("name", "")).strip()
                # PATTERN MUST MATCH THE OPPONENT'S HEADINGS. Anchoring on the case name
                # ("gregg|ameriprise") matched nothing — headings name the CLAIM, not the
                # authority. Anchoring on the SPINE key's raw tokens also failed, because a
                # brief writes "Unfair Trade Practices" where the key says "UTPCPL".
                # Derive the anchor from the ACTUAL HEADINGS this authority should serve:
                # any heading whose topic vocabulary overlaps this entry's subject.
                subj_text = f"{name} {pat} {body}"
                subj = {t.lower() for t in _TOPIC.findall(subj_text)}
                serves = [h for h in heads
                          if subj & {t.lower() for t in _TOPIC.findall(h)}]
                if not serves:
                    continue
                words = []
                for h in serves:
                    words += [w for w in re.findall(r"[a-z]{5,}", h.lower())
                              if w not in ("plaintiff", "defendant", "complaint", "against",
                                           "should", "claims", "claim", "assert", "asserts",
                                           "purports", "count")]
                anchor = list(dict.fromkeys(words))[:5]
                if not anchor:
                    continue
                bank.append({
                    "pattern": "|".join(re.escape(w) for w in anchor),
                    "tier": "SETTLED", "heading": name,
                    "answer_heading": name,
                    "text": _clip_text(lead + body),
                    "cites": [cite]})
        con.close()
    except Exception:
        pass
    # GUARANTEE: every required authority reaches the bank, or the draft cannot
    # cite what the analysis was made to carry.
    bank.extend(_authority_entries(slug, ana, bank))
    # Deterministic folds are guarantees, not heading-matched findings.
    bank.extend(_fold_entries(slug, ana, bank))

    return _refuse_stamped(bank)


# m0's grader/verification vocabulary. These are STAMPS on the record; a sentence that
# carries one is a note about the argument, not the argument, and must not plead.
_STAMP_RX = re.compile(
    r"\b(?:NOT-VERIFIED|NOT VERIFIED|Fidelity-graded|WRONG_POINT|WRONG_CITE|OVERSTATED|"
    r"HOLDS-AS-CITED|UNBACKED|SUPPORTED:|NOT-IN-CORPUS|NOT IN CORPUS)\b"
    r"|\(m0 fills|\(fragment\)|\[corpus\]|\[official\]")


def _refuse_stamped(bank: list) -> list:
    """Drop every row whose pleadable text carries a grader stamp, and say which."""
    kept = []
    for row in bank:
        t = f"{row.get('answer_heading', '')} {row.get('heading', '')} {row.get('text', '')}"
        m = _STAMP_RX.search(t)
        if m:
            print(f"  [bank] REFUSED stamped row {row.get('source', '')!s:>14} "
                  f"{row.get('heading', '')[:60]!r}: {m.group(0)}")
            continue
        kept.append(row)
    return kept


def build(slug: str) -> tuple[Path, Path]:
    OUT.mkdir(parents=True, exist_ok=True)
    mx, bk = build_matrix(slug), build_bank(slug)
    mp = OUT / f"{slug}_rebuttal_matrix.json"
    bp = OUT / f"{slug}_rebuttal_bank.json"
    mp.write_text(json.dumps(mx, indent=1), encoding="utf-8")
    bp.write_text(json.dumps(bk, indent=1), encoding="utf-8")
    return mp, bp


def build_feed(slug: str, matrix_path: str, bank_path: str) -> tuple:
    """Rebuild matrix + bank from the case's CURRENT analysis. Returns (n_headings, n_answers)."""
    mx = build_matrix(slug)
    bk = build_bank(slug)
    Path(matrix_path).parent.mkdir(parents=True, exist_ok=True)
    Path(matrix_path).write_text(json.dumps(mx, indent=1), encoding="utf-8")
    Path(bank_path).write_text(json.dumps(bk, indent=1), encoding="utf-8")
    # count the movant's headings, not the matrix's top-level keys
    _n = 0
    try:
        _n = sum(len(b.get("headings_found") or []) for b in (mx.get("briefs") or {}).values())
    except Exception:
        _n = len(mx)
    return (_n, len(bk))


def assemble(matrix_path, bank_path, out_path, ana_path=None):
    mx = json.loads(Path(matrix_path).read_text())
    bank = json.loads(Path(bank_path).read_text())
    # the case slug travels in the matrix; footnote citation naming needs it
    slug = str(mx.get("slug") or "")
    if not slug:
        _mname = Path(matrix_path).name
        slug = _mname.split("_rebuttal_matrix")[0] if "_rebuttal_matrix" in _mname else ""
    # A deterministic fold owns its argument. If that same prose leaked into another
    # entry's body, it gets emitted under a heading the fold never chose -- the Pittsburgh
    # distinction appeared under "duties independent of the contract", a proposition it
    # argues AGAINST. Remove the leak; the fold still emits the argument as its own point.
    try:
        _ana_p = ana_path or (CASES / mx.get("slug", "") / "CASE_FACTS.md")
        _ana = Path(_ana_p).read_text(encoding="utf-8") if Path(_ana_p).exists() else ""
    except Exception:
        _ana = ""
    if _ana:
        _owned = _fold_texts(_ana)
        for _e in bank:
            if not isinstance(_e, dict) or _e.get("source") == "fold_section":
                continue
            _t = " ".join(str(_e.get("text", "")).split())
            if not _t:
                continue
            for _o in _owned:
                if _o[:150] and _o[:150] in _t:
                    _e["text"] = " ".join(_t.replace(_o, " ").split())
                    break
    lines = []
    novel = []
    stats = {"MOOT": 0, "SETTLED": 0, "RECORD": 0, "NOVEL": 0}
    used_support = set()
    stated_full = set()   # AUTHORITY_ONCE_V1: full statement once per brief   # a structural authority is stated once, not per heading
    n = 0
    for brief, info in mx.get("briefs", {}).items():
        lines.append(f"\n<!-- brief: {brief} ({info.get('doc','')}) -->")
        heads = info.get("headings_found", []) + [k for k in info.get("known_args", [])]
        for h in heads:
            n += 1
            e = classify(h, bank)
            if e is None:
                stats["NOVEL"] += 1
                novel.append((n, brief, h))
                lines.append(f"\n**{n}. {h.strip()}.** [NOVEL — LLM PASS REQUIRED]")
                continue
            tier = e["tier"]; stats[tier] += 1
            cites = "; ".join(e.get("cites", []))
            # AUTHORITY MERGE: classify() returns ONE entry (first tier hit), so a heading
            # that matched a RECORD finding never saw the SETTLED authority rows — the
            # corpus-verified statute text and controlling decisions sat in the bank and
            # never reached a single draft. Append every SETTLED entry that also matches
            # this heading, so the argument carries its authority.
            support, support_cites = [], []
            # HOLDING_INHERIT_V1: a point whose OWN text is a ledger row ("Name, cite —
            # court — BINDS — cited for X") is a heading plus a citation, not an argument.
            # m0 wrote the holding: its authority fold for that same cite carries 100-200
            # words of analysis (Toy 196w, D'Ambrosio 165w, Kelly 193w). classify() returns
            # ONE entry, so when the movant's heading matched the thin RECORD/ledger row the
            # fold prose stayed in the bank and never reached the brief -- 11 of 31 points
            # rendered as a bare cite. Inherit the fold for the SAME authority (cite-anchored,
            # so no fold is spliced under a heading it does not answer), once per brief.
            _own_words = len(re.findall(r"[a-z]{3,}", str(e.get("text", ""))))
            _own_cites = {c for c in (e.get("cites") or []) if c}
            if _own_words < 40 and _own_cites:
                for _f in bank:
                    if _f is e or _f.get("source") != "fold_section":
                        continue
                    if not (_own_cites & {c for c in (_f.get("cites") or []) if c}):
                        continue
                    _fid = "fold:" + (_f.get("heading") or _f.get("text", "")[:60])
                    if _fid in used_support:
                        continue
                    _ft = _sanitize(str(_f.get("text", "")).strip())
                    if len(re.findall(r"[a-z]{3,}", _ft)) < 40:
                        continue
                    support.append(_ft)
                    support_cites.extend(_f.get("cites") or [])
                    used_support.add(_fid)
                    break
            for s in bank:
                if s is e or s.get("tier") != "SETTLED":
                    continue
                # folds are arguments in their own right; splicing one under another
                # heading can make it read as supporting the opposite proposition.
                # (The cite-anchored HOLDING_INHERIT_V1 pass above is the one exception:
                # same authority, thin host point, once per brief.)
                if s.get("source") == "fold_section":
                    continue
                # A structural authority (the weight ledger, a statute's operative text)
                # belongs in the brief ONCE. Repeating it under every heading it matches
                # padded a draft from 12K to 22K with six copies of the same paragraph.
                sid = s.get("heading") or s.get("text", "")[:60]
                if sid in used_support:
                    continue
                # a bare CITE entry (one authority) is not a structural block — several
                # may serve one heading. Only the big structural entries are once-only.
                _structural = sid in ("Binding-weight ledger", "Waiver and concessions",
                                      "Record facts")
                sp = s.get("pattern", "")
                try:
                    if sp and re.search(sp, h, re.I):
                        _txt = _sanitize(s["text"].strip())
                        # AUTHORITY_ONCE_V1: state the black-letter rule in full the
                        # first time it appears in the brief; afterwards cite it short
                        # form. Four verbatim restatements of Rancosky/Terletsky read
                        # as padding and bury the record answer.
                        if sid in stated_full:
                            _sf = _short_form(_txt)
                            if _sf:
                                _txt = _sf
                        else:
                            stated_full.add(sid)
                        support.append(_txt)
                        support_cites.extend(s.get("cites", []))
                        if _structural:
                            used_support.add(sid)
                except re.error:
                    continue
            if support:
                cites = "; ".join([c for c in ([cites] if cites else []) + support_cites if c])
            auth = (" " + " ".join(support[:4])) if support else ""
            # SECTION TITLE = OUR proposition, never the movant's heading. A brief whose
            # headings are the opponent's arguments reads as their case with commentary
            # ("answer from our pleading, not theirs"). The feed supplies answer_heading;
            # fall back to their heading only when none was derived.
            title = _normalize_title(e.get("answer_heading") or h, h,
                                     str(e.get("text", "")))
            if tier == "MOOT":
                lines.append(f"\n**{n}. {title}.** Moot. {_sanitize(e['text'])}{auth} "
                             f"*See* Part {e.get('threshold_ref','I')}, supra."
                             + (f" ({cites})" if cites else ""))
            elif tier == "RECORD":
                facts = " ".join(f"{_sanitize(f)}." for f in e.get("record_facts", []))
                lines.append(f"\n**{n}. {title}.** {_sanitize(e['text'])}{auth} {facts}"
                             + (f"[^{n}]\n\n[^{n}]: {_name_cites(cites, slug)}" if cites else ""))
            else:  # SETTLED
                lines.append(f"\n**{n}. {title}.** {_sanitize(e['text'])}{auth}"
                             + (f"[^{n}]\n\n[^{n}]: {_name_cites(cites, slug)}" if cites else ""))
    # AUTHORITY SWEEP: assemble() iterates the movant's headings, so a bank entry that is
    # not heading-matched never reaches the brief. The authority fold exists precisely to
    # carry authorities no heading claims -- emit any whose citation is still absent.
    # Merge the characterization check into the distinction for the same authority: one
    # authority gets one point, argued fully, not two adjacent points with the same name.
    _by_cite = {}
    for _e in bank:
        if not isinstance(_e, dict) or _e.get("source") != "fold_section":
            continue
        _cs = _e.get("cites") or []
        if len(_cs) != 1:
            continue
        _key = _cs[0]
        if "\u2014 the movant's description" in (_e.get("heading") or ""):
            _tgt = _by_cite.get(_key)
            if _tgt is not None:
                _add = " ".join(_e.get("text", "").split())
                if _add and _add not in _tgt.get("text", ""):
                    _tgt["text"] = _tgt.get("text", "").rstrip() + " " + _add
                _e["_merged"] = True
        else:
            _by_cite.setdefault(_key, _e)
    bank = [_e for _e in bank
            if not (isinstance(_e, dict) and _e.get("_merged"))]

    # expand bare reporter citations to "Name, cite" everywhere in the assembled body
    try:
        _slug = mx.get("slug") or ""
        if not _slug and ana_path:
            _slug = Path(ana_path).parent.name
        _names = _cite_name_map(_slug)
    except Exception:
        _names = {}
    if _names:
        lines = [_name_bare_cites(_l, _names) for _l in lines]

    _body = "\n".join(lines)
    # citations that a fold_section ARGUES (distinction / characterization check)
    _argued = set()
    for _e in bank:
        if isinstance(_e, dict) and _e.get("source") == "fold_section":
            for _c in (_e.get("cites") or []):
                _argued.add(_c)
    for _e in bank:
        if _e.get("source") not in ("authority_fold", "fold_section", "lever_fold",
                                    "premise_fold"):
            continue
        # a bare ledger stub for an authority already argued in full adds nothing
        if (_e.get("source") == "authority_fold"
                and (_e.get("cites") or [])
                and all(_c in _argued for _c in _e.get("cites"))):
            continue
        _cs = _e.get("cites") or []
        # Identity is the TEXT, not the citations. A fold that answers three authorities
        # the brief already cites is not a duplicate of them.
        _probe = " ".join(_sanitize(_e.get("text", "")).split())[:120]
        if _probe and _probe in " ".join(_body.split()):
            continue
        if not _probe and _cs and any(c in _body for c in _cs):
            continue
        n += 1
        stats["SETTLED"] = stats.get("SETTLED", 0) + 1
        _c = "; ".join(_cs)
        # a section title states OUR proposition; the bare case name states theirs
        _t = (_e.get("answer_heading")
              or _e.get("heading", "Authority").replace("Authority: ", "")).rstrip(". ")
        _t = _normalize_title(_t, "", str(_e.get("text", "")))
        _body_txt = _sanitize(_e["text"])
        # A STORED bank entry can be a BARE CITE. The bank is an artifact on disk from an
        # earlier run, so repairing the builder does not repair records already written:
        # entries for Gregg v. Ameriprise (245 A.3d 637) and Schwartz v. Rockey (932 A.2d
        # 885) carried "Name, cite." alone, and _drop_stub_points then deleted the point,
        # removing a live UTPCPL authority from the filing with nothing saying it went
        # missing. Repair at the CONSUMPTION point too, so a stale bank cannot silently
        # cost an authority. Asserts no holding -- states only what the record shows.
        # A BARE CITE is "Name, 123 Rep. 456." and nothing else. A short body that
        # states a proposition, a disposition quote, or a provenance clause is NOT a bare
        # cite; feeding it through this branch re-stitches the whole body as the "name"
        # ("The movant cites The movant advances this authority ... without stating").
        _bare_rx = re.compile(r"^\**(?:[A-Z][\w.'\u2019&,\- ]{2,140}?)\s+v\.\s+[\w.'\u2019&,\- ]{2,140}?,\s*\d{1,4}\s+[A-Z][A-Za-z.]{0,10}(?:\s?[23]d|\s?4th)?\s*\d{1,5}(?:\s*\([^)]{2,40}\))?\.?\**\s*$")
        if len(_body_txt.split()) < 40 and _bare_rx.match(_body_txt.strip()):
            _nm_bare = re.split(r",\s*\d", _body_txt.strip(), maxsplit=1)[0].strip(" .*")
            if _nm_bare:
                _body_txt = (
                    f"{_body_txt.rstrip('. ')}. The movant cites {_nm_bare} without "
                    f"stating the proposition it is offered for, and no rule of law is "
                    f"drawn from it. An authority invoked without a stated proposition "
                    f"establishes nothing the movant may rely on, and the pleaded count "
                    f"stands unanswered by it.")
        # the bullet often opens by naming the authority, which is already the title
        _body_txt = re.sub(r"^\*\*[^*]{0,90}?\*\*\s*", "", _body_txt)
        _body_txt = _body_txt.lstrip(". ").strip()
        _body_txt = _repair_cite_fragment(_body_txt)
        _body_txt = _body_txt.rstrip()
        # the bullet is a complete sentence; do not let its terminal period collide with
        # the emitted heading period of the NEXT fragment
        lines.append(f"\n**{n}. {_t.rstrip('. ')}.** {_body_txt}"
                     + (f"[^{n}]\n\n[^{n}]: {_name_cites(_c, slug)}" if _c else ""))
        _body += "\n" + lines[-1]

    hdr = (f"<!-- m1_rebuttal assembled {datetime.datetime.now().isoformat(timespec='seconds')} "
           f"points={n} dispatch={stats} llm_needed={len(novel)} -->\n"
           f"# REBUTTAL MATRIX (zero-waiver: every point answered)\n")
    _out_text = hdr + "\n".join(lines) + "\n"
    # FINAL PASS: a citation without a case name is not a citation. Run it here, after
    # every emission path has contributed, rather than mid-assembly where later appends
    # escape it.
    if _names:
        _out_text = "\n".join(_name_bare_cites(_l, _names) for _l in _out_text.split("\n"))
    # a heading with no argument under it reads as an abandoned position -- drop it here,
    # at the one place every emission path converges
    _out_text = _drop_stub_points(_out_text)
    # the same black-letter rule restated verbatim across points dilutes the argument
    _out_text = _collapse_immediate_repeats(_out_text)
    _out_text = _dedupe_rule_statements(_out_text)
    # PROSE HYGIENE LAST: the cite gate counts citations and cannot see internal tool
    # vocabulary, raw classifier metadata, or orphan superscripts. Those shipped in a
    # gate-clean draft, so the check belongs here, where every emission path converges.
    _out_text = _clean_filing_prose(_out_text)
    Path(out_path).write_text(_out_text, encoding="utf-8")
    return stats, novel, n

def _repair_heading_markers(text: str) -> str:
    """Re-close a bold heading whose closing ** was consumed by artifact stripping.

    _strip_tool_artifacts removes a whole sentence, and when that sentence sat between
    the heading text and its closing marker the heading welded onto the body:
        **10. ... does not foreclose the claim as pleaded.42 Pa.C.S. § 8371, supra.
    which renders as one run-on bold line with no visible heading. Close the marker at
    the first sentence end, and restore the space the removal ate.
    """
    out = []
    for ln in text.split("\n"):
        if ln.startswith("**") and ln.count("**") == 1:
            m = re.search(r"(?<=[a-z)\"\u201d])\.(?=\s|[A-Z0-9])", ln[2:])
            if m:
                cut = 2 + m.end()
                ln = ln[:cut] + "**" + ln[cut:]
        # "pleaded.42 Pa.C.S." -- a removal closed the gap between sentences
        ln = re.sub(r"(?<=[a-z])\.(?=[0-9]{1,3}\s+Pa\.)", ". ", ln)
        # Restore a space a removal ate AFTER the CLOSING marker only.
        # "**...pleaded.**42 Pa.C.S." -> "**...pleaded.** 42 Pa.C.S."
        # This must NOT touch the OPENING marker: "** 1. Text**" is not bold in markdown
        # (an opening ** followed by whitespace is not emphasis), so inserting a space
        # there silently unbolds every heading in the brief.
        ln = re.sub(r"(?<!^)\*\*(?=[A-Z0-9])", "** ", ln)
        ln = re.sub(r"^\*\*\s+(?=\d)", "**", ln)
        out.append(ln)
    return "\n".join(out)


def _dedupe_identical_headings(text: str) -> str:
    """Collapse points that carry the SAME heading text.

    Four consecutive points read "Every element of the count is met on the face of the
    pleading." A reader cannot tell which count each answers, and repetition reads as
    padding. Keep the first, and fold the others' bodies under it so no argument is lost.
    """
    lines = text.split("\n")
    seen, out = {}, []
    for ln in lines:
        m = re.match(r"\*\*(\d+)\.\s*([^*]+?)\*\*\s*(.*)$", ln)
        if not m:
            out.append(ln); continue
        key = re.sub(r"\W+", " ", m.group(2).strip().lower()).strip()
        body = m.group(3).strip()
        if key in seen and body:
            # append this body to the kept point instead of repeating the heading
            i = seen[key]
            if body not in out[i]:
                out[i] = out[i].rstrip() + " " + body
            continue
        seen[key] = len(out)
        out.append(ln)
    # Absorbing a point REMOVES its number from the sequence: the filing then reads
    # 18, 20, 21 (and 27, 31), so a court reading "20" cannot find 19 and the brief
    # looks like it lost a page. The dedupe is right to merge; it must also close the
    # numbering it opened. Renumber survivors 1..N and carry each point's footnote
    # marker+definition with it, since the ids are keyed to the point number.
    return _renumber_points("\n".join(out))


def _renumber_points(text: str) -> str:
    """Make the numbered points contiguous 1..N, footnote ids following their point.

    Any pass that drops or merges a point leaves a hole in the sequence. Numbering is
    the reader's index into the brief, so it is closed HERE, once, after the last pass
    that can remove a point -- not inside each remover.

    Old->new is applied as a single simultaneous mapping: rewriting ids one at a time
    lets a new number collide with an old one still present (dropping 2 of 1,2,3 makes
    old 3 the new 2), which silently merges two footnotes.
    """
    nums = [int(x) for x in re.findall(r"(?m)^\*\*(\d+)\. ", text)]
    if not nums or nums == list(range(1, len(nums) + 1)):
        return text
    remap = {old: new for new, old in enumerate(nums, 1)}

    def _pt(m):
        return f"**{remap.get(int(m.group(1)), m.group(1))}. "

    text = re.sub(r"(?m)^\*\*(\d+)\. ", _pt, text)

    def _fn(m):
        n = int(m.group(1))
        return f"[^{remap[n]}]" + (m.group(2) or "") if n in remap else m.group(0)

    return re.sub(r"\[\^(\d+)\](:)?", _fn, text)


def _fix_heading_agreement(text: str) -> str:
    """Agree the verb in a heading with a compound subject.

    "The objection as to Counts II and VI are not well taken" -- the subject is
    "objection" (singular). Filed as-is it is a grammar error in a heading.
    """
    return re.sub(r"(?i)\b(The objection(?:\s+as\s+to\s+[^.]*?)?)\bare\b",
                  r"\1is", text)


def _strip_tool_artifacts(text: str) -> str:
    """Remove INTERNAL tool vocabulary from filing prose.

    A gate that counts citations cannot see prose defects, so these shipped in a
    court-ready draft: "NOT-IN-CORPUS for insurance bad faith and UTPCPL. The settled
    statement ... would need to be ingested" (an instruction to ME, printed as argument),
    and raw classifier metadata used as a sentence -- "Toy v. Metro. Life Ins. Co., 928
    A.2d 186 (Pa. 2007) - PA Supreme Court - BINDS - cited for first-party bad faith".
    Opposing counsel reading either one learns the brief was machine-assembled and never
    re-read. Nothing here changes an argument: it deletes text that is not argument.
    """
    out = []
    for ln in text.split("\n"):
        # sentences whose whole content is a corpus-coverage confession
        ln = re.sub(r"(?i)\bNOT[- ]IN[- ]CORPUS\b[^.]*\.\s*", "", ln)
        ln = re.sub(r"(?i)[^.]*\bwould need to be ingested\b[^.]*\.\s*", "", ln)
        # "— PA Supreme Court — BINDS — cited for X" -> "(Pa. 2007)" already carries this.
        # `cited for[^.]*` must NOT cross a [^n] footnote marker or a newline: the metadata
        # tail often has no terminating period, and a greedy run swallowed both the marker
        # and the "[^6]: ..." definition line that followed it.
        _META = r"(?:[^.\[\n]|\.(?!\s|$))*"
        ln = re.sub(r"\s*[—-]\s*(?:PA|Pa\.?)\s+(?:Supreme Court|Superior Court)\s*"
                    r"[—-]\s*BINDS\s*[—-]\s*cited for" + _META + r"\.?", ".", ln)
        ln = re.sub(r"\s*[—-]\s*BINDS\s*[—-]\s*cited for" + _META + r"\.?", ".", ln)
        ln = re.sub(r"\s*[—-]\s*(?:PA|Pa\.?)\s+(?:Supreme|Superior)\s+Court\s*[—-]\s*BINDS\b\.?",
                    ".", ln)
        ln = re.sub(r"\s{2,}", " ", ln)
        ln = re.sub(r"\.\s*\.", ".", ln)
        out.append(ln.rstrip())
    return "\n".join(out)


def _strip_stray_superscripts(text: str) -> str:
    """DISABLED -- superscripts fused to words are m0's quantum-definition refs.

    Originally written to strip "damages¹" / "person²" as orphan footnote residue. That
    was WRONG: definitions_tagger.inject_quantum(..., sup="unicode") emits them on purpose
    and the DEFINITIONS & AUTHORITIES legend at the foot of the filing keys every one
    ("1. damages [Dict.] — A pecuniary compensation..."). Stripping them severs the
    superscripted term from its controlling-sense definition, which is the locked filing
    anatomy. Kept as a no-op so the reason survives with the code and nobody re-adds it.
    """
    return text


def _balance_quotes(text: str) -> str:
    """Close an unbalanced straight double quote inside a single point.

    An odd quote count leaves the rest of the brief looking quoted. Per-line, because
    each numbered point is one line in this format.
    """
    out = []
    for ln in text.split("\n"):
        if ln.startswith("**") and ln.count('"') % 2:
            m = re.search(r"\[\^\d+\]\s*$", ln)
            ln = (ln[:m.start()].rstrip() + '" ' + ln[m.start():]) if m else ln.rstrip() + '"'
        out.append(ln)
    return "\n".join(out)


def _drop_mid_brief_conclusion(text: str) -> str:
    """A point may not open its own 'Conclusion:' -- the brief has ONE conclusion.

    Point 8 carried "Conclusion: The movant's objections are defective on multiple
    grounds: ..." in the middle of the argument, ahead of 26 further points. The content
    is good; the label is wrong, because a reader hits 'Conclusion' and stops.
    """
    return re.sub(r"(?m)\bConclusion:\s*", "", text)


def _fix_citation_only_heading(text: str) -> str:
    """A heading must state a PROPOSITION, not just name a case.

    Point 25 read "**Wenk v. State Farm Fire & Cas. Co., 228 A.3d 540.**" -- that is a
    citation, and a court scanning headings learns nothing from it. The body already
    argues the case is distinguishable, so say so in the heading, matching the phrasing
    the other authority-answering points use.
    """
    def fix(m):
        num, head, rest = m.group(1), m.group(2).strip(), m.group(3)
        bare = re.sub(r"[.,\s]+$", "", head)
        # Heading is ONLY a case name + reporter cite, with no verb. The name itself
        # contains periods ("Cas. Co."), so the name part cannot be [^.]* -- match up to
        # the reporter cite and require a "v." plus a trailing volume/reporter/page.
        looks_like_cite = re.fullmatch(
            r"[A-Z][\w'’.,&\s-]*?\bv\.\s[\w'’.,&\s-]*?,\s*\d{1,4}\s+"
            r"[A-Z][\w.]*(?:\s*\d[a-z]{0,2})?\s+\d{1,4}", bare) is not None
        has_verb = re.search(r"\b(?:is|are|does|do|fails?|states?|rests?|cannot|"
                             r"must|reaches?|defeats?|forecloses?|bars?)\b", bare) is not None
        if looks_like_cite and not has_verb:
            name = bare.split(",")[0].strip()
            return f"**{num}. {name} does not defeat the claim as pleaded.** {rest}"
        return m.group(0)
    return re.sub(r"\*\*(\d+)\.\s*([^*]+?)\*\*\s*(.*)$", fix, text, flags=re.M)


def _clean_filing_prose(text: str) -> str:
    """One convergence point for prose hygiene, applied after every emission path.

    ORDER MATTERS: artifact stripping can consume a heading's closing ** or the space
    between two sentences, so marker repair must run AFTER it, not before.
    """
    text = _strip_tool_artifacts(text)
    text = _repair_heading_markers(text)
    text = _fix_citation_only_heading(text)
    text = _fix_heading_agreement(text)
    text = _dedupe_identical_headings(text)
    text = _strip_stray_superscripts(text)
    text = _drop_mid_brief_conclusion(text)
    text = _balance_quotes(text)
    return text


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--matrix", required=True)
    ap.add_argument("--bank", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--voice", action="store_true",
                    help="run the single LLM voice pass over the assembled text (prose only; invariants verified)")
    ap.add_argument("--llm-url", default="http://localhost:8000/v1")
    ap.add_argument("--court", default=None, help="court id (EDPA/3D_CIR/PA_CP_PHILA) — loads the measured style contract")
    ap.add_argument("--type", default=None, dest="ftype", help="filing type for the style profile")
    a = ap.parse_args()
    stats, novel, n = assemble(a.matrix, a.bank, a.out)
    print(f"assembled {n} rebuttals in tiers: {stats}")
    if a.voice:
        p = Path(a.out)
        text = p.read_text(encoding="utf-8")
        out, used = voice_pass(text, a.llm_url, court=a.court, filing_type=a.ftype)
        if used:
            p.with_suffix(".assembled.md").write_text(text, encoding="utf-8")
            p.write_text(out, encoding="utf-8")
            print("  [voice] prose polished (1 LLM call); invariants verified; assembled original kept alongside")
    if novel:
        print(f"LLM needed for {len(novel)} point(s) ONLY:")
        for i, brief, h in novel:
            print(f"  {i}. [{brief}] {h[:80]}")
    else:
        print("ZERO LLM calls needed - fully deterministic assembly")

if __name__ == "__main__":
    main()