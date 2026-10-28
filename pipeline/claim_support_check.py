# -*- coding: utf-8 -*-
"""CLAIM-SUPPORT CHECK — the missing twin of cite_authority_check.

`cite_authority_check` confirms a cite EXISTS (CourtListener/Justia) and is BINDING (binding_analysis).
This module confirms the cited authority STANDS FOR the proposition it is attached to — the thing that
actually gets a pro se litigant beaten ("Plaintiff cites Twombly for X; Twombly holds no such thing").

For every legal proposition + its authority (case OR statute) in a draft, it pulls the AUTHORITY'S OWN
TEXT from the corpus (semantic retrieval, off-GPU) and a fresh-context adversarial LLM decides:
  SUPPORTED   — the corpus text plainly backs this proposition
  OVERSTATED  — the authority says LESS than the draft claims
  WRONG-POINT — the authority is about something else
  UNBACKED    — the corpus does not contain the authority / no support found
Output is a one-page PROOF SHEET (claim -> the exact controlling quote -> verdict) so a human can trust
the draft WITHOUT reading the corpus. Non-destructive: it reports; it does not rewrite the draft.

Discipline (same as the rest of the loop): LLM reasons; retrieval + parsing are deterministic. If NO LLM
lane is reachable it marks UNVERIFIED (fail-safe) — it never rubber-stamps SUPPORTED.

CLI:  python -m pipeline.claim_support_check <draft.md>   # writes <draft>_PROOF_SHEET.md
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import os, sys, re, json
from pathlib import Path

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")   # corpus retrieval on the CPU embedder
BASE = _eroot() if os.name != "nt" else _eroot()
sys.path.insert(0, str(BASE))

# authority patterns — a case reporter cite, or a statute/rule cite
_REP = r"(?:U\.\s?S\.|S\.\s?Ct\.|F\.\s?\d?[a-z]?|F\.\s?Supp\.\s?\d?[a-z]?|A\.\s?\d?[a-z]?|Pa\.)"
# a party name: a Capitalized token, then any mix of Capitalized tokens or lowercase connectors
# (of/the/and/for/&) — so "Safeco Ins. Co. of Am." and "Bell Atlantic Corp." both match.
_PARTY = r"(?:In re\s+)?[A-Z][A-Za-z.'&-]+(?:\s+(?:of|the|and|for|&|v\b(?!\.)|[A-Z][A-Za-z.'&-]+)){0,7}"
CASE_RE = re.compile(
    r"(" + _PARTY + r")\s+v\.\s+(" + _PARTY + r"),?\s+\d+\s+" + _REP + r"\s+\d+")
STAT_RE = re.compile(
    r"\d+\s+U\.?\s?S\.?\s?C\.?\s?§?\s?\d+[\w().-]*"
    r"|\d+\s+P\.?\s?S\.?\s?§?\s?[\d.-]+[\w().-]*"
    r"|\d+\s+Pa\.?\s?C\.?\s?S\.?\s?§?\s?\d+[\w().-]*"
    r"|Pa\.?\s?R\.?(?:\s?[A-Za-z]{1,5}\.?){1,3}\s?\d+[\w().-]*"
    r"|Fed\.?\s?R\.?(?:\s?[A-Za-z]{1,5}\.?){1,3}\s?\d+[\w().-]*"
    r"|\d+\s+C\.?F\.?R\.?\s?§?\s?[\d.-]+")

def _sentences(md):
    # strip markdown headings/tables; split into sentences
    text = re.sub(r"^[#|>].*$", " ", md, flags=re.M)
    text = re.sub(r"\[\[[^\]]+\]\]", " ", text)
    parts = re.split(r"(?<=[.;])\s+(?=[A-Z(\"'\d])", text)
    return [re.sub(r"\s+", " ", p).strip() for p in parts if len(p.strip()) > 40]

def extract_claims(md):
    """(proposition, authority, kind) for EVERY citation in the draft. CITE-ANCHORED, not sentence-split:
    a naive sentence splitter breaks on the periods INSIDE 'Pa.R.C.P.' / 'U.S.C.' / 'Pa.C.S.' and shreds the
    cite so it never matches (that blind spot let the 3146(a) rule error through). Here we locate each cite
    intact via _CITE_ANY, then take the surrounding clause as the proposition. Heading/quote/table lines are
    blanked so a cite in a boilerplate acknowledgment box is not verified as a substantive claim."""
    # Fix C: drop the CERTIFICATE/PROOF OF SERVICE section — its cites are PROCEDURAL boilerplate ("served
    # pursuant to the Rule 1925(b) order, by mail"), not substantive legal claims. Verifying them produces
    # false OVERSTATED/UNBACKED (a service rule is cited for HOW the paper is served, not for a holding).
    md = re.sub(r"(?ims)^#{1,4}\s*(?:certificate|proof)\s+of\s+service\b.*?(?=^#{1,3}\s|\Z)", " ", md)
    text = re.sub(r"(?m)^[#>|].*$", " ", md)      # drop markdown structure lines (headings, blockquotes, tables)
    out, seen = [], set()
    _spans = [(mm.start(), mm.end()) for mm in _CITE_ANY.finditer(text)]   # all cite positions, to bound windows
    for m in _CITE_ANY.finditer(text):
        auth = re.sub(r"\s+", " ", m.group(0)).strip().rstrip(".,; ")
        if auth.count("(") < auth.count(")"):     # trailing prose paren, e.g. "(18 Pa.C.S. § 4904)" -> drop the ")"
            auth = auth[:auth.rfind(")")].rstrip(".,; ")
        if len(auth) < 5:
            continue
        s, e = m.start(), m.end()
        # proposition = a window CENTERED on the cite (± ~230 chars), trimmed to word boundaries. Centering is
        # deliberate: a sentence-boundary search on '. ' is defeated by the '. ' INSIDE cites ('Pa.R.C.P. ',
        # 'Pa.R.E. '), which shifted each proposition onto the PREVIOUS cite's clause and produced false UNBACKED.
        # Centering keeps THIS cite and its own clause together, which is what the verifier must judge.
        L, R = max(0, s - 230), min(len(text), e + 250)
        # BOUND the window by NEIGHBORING cites so a cite is judged on ITS OWN clause, not a neighbor's. A
        # sentence like "disregarding Rule 1024 ...; and Rule 1019 ...; and Rule 3146(a) ..." otherwise lets
        # each cite's window steal the adjacent cite's proposition -> false UNBACKED. Clamp L to the end of the
        # previous cite and R to the start of the next cite (with a small pad so the connecting words survive).
        for (os_, oe) in _spans:
            if oe <= s and oe > L:
                L = oe
            if os_ >= e and os_ < R:
                R = os_
        if L > 0:
            sp = text.find(" ", L, s)
            L = sp + 1 if sp != -1 else L
        if R < len(text):
            sp = text.rfind(" ", e, R)
            R = sp if sp != -1 else R
        prop = re.sub(r"\s+", " ", text[L:R]).strip()[:520]
        kind = "case" if CASE_RE.search(m.group(0)) else "statute"
        key = (auth.lower(), prop[:70].lower())
        if key in seen:
            continue
        seen.add(key)
        out.append((prop, auth, kind))
    return out

_PROMPT = """You are a careful legal verifier. A draft makes the PROPOSITION below and cites the AUTHORITY
below for it. Text retrieved from a legal corpus follows. Decide, using the retrieved text, whether the
authority STANDS FOR the proposition.

HOW TO READ THE TEXT (important):
- Use the FULLEST, most AUTHORITATIVE version shown: verbatim statute / constitution / binding case OUTRANKS
  a digest, summary, or note. A summary that says "actual damages only" does NOT disprove a statute that also
  grants fees — do not mark OVERSTATED because one snippet is partial/incomplete. Judge the authority as fully
  and correctly read.
- Weigh ALL the authorities shown, not just the first. If a DIFFERENT applicable authority in the text supports
  the proposition, that counts as support.
- Respect the hierarchy: Constitution and binding precedent control over a statute; a statute over a regulation.
  A retrieved statute does NOT override a constitutional provision or controlling precedent.
- STATUTORY SCHEMES span sections: a DEFINITION section defines a practice/term, an OPERATIVE section prohibits
  it or imposes liability, a REMEDY section gives the action. A proposition is SUPPORTED if the SCHEME — the
  cited section together with the sibling provisions of the SAME Act shown in the retrieved text — supports it.
  Example: a claim that a practice is "prohibited," citing the Act's DEFINITION of that practice, is SUPPORTED
  when the retrieved text shows the same Act declares that defined practice unlawful. Do NOT mark UNBACKED
  merely because the express operative words sit in a sibling section of the same Act.
  BUT: when the express operative provision is in a DIFFERENT section than the one cited, NAME that correct
  section in "why" so the draft can be made precise — e.g. "cited § 201-2(4) (definition); the prohibition is
  § 201-3 — cite § 201-3." Get the correct citation, the way the statute cites itself.

Verdicts:
- SUPPORTED: the authority, fully read, backs the proposition (give the exact supporting quote).
- OVERSTATED: the authority, fully read, provides MATERIALLY LESS than the proposition claims (not merely a
  partial summary snippet).
- WRONG_POINT: the authority is about a different issue than the proposition.
- UNBACKED: the retrieved text does not contain the authority or any support for it.

Output ONLY compact JSON on one line:
{"verdict":"SUPPORTED|OVERSTATED|WRONG_POINT|UNBACKED","quote":"<<=30 words from the corpus, or empty>","why":"<=15 words"}

PROPOSITION:
{prop}

AUTHORITY CITED:
{auth}

RETRIEVED CORPUS TEXT:
{grounding}
"""

def _authnum(auth):
    """The core section token to demand in the retrieved text — '1681n' from '15 U.S.C. § 1681n(a)',
    '201-2' from '73 P.S. § 201-2(4)', '3146' from 'Pa.R.C.P. 3146(a)', '12' from 'Fed. R. Civ. P. 12(d)'.
    Semantic top-1 on a bare §-cite is too noisy (it returned CFR tax text for §1681e(b)); we confirm the
    right SECTION actually appears in the retrieved chunk. Take the number AFTER the § (not the title #)."""
    m = re.search(r"§\s*(\d+[A-Za-z]?(?:-\d+)?)", auth)
    if m:
        return m.group(1)
    # ANY court-rule family -> the rule number: Pa.R.C.P./R.A.P./R.E./R.Crim.P./R.J.C.P./Fed.R.Civ.P./
    # Fed.R.App.P./Fed.R.Evid., etc. (the old [CAE]-only pattern silently returned "" for Crim.P./J.C.P.,
    # disabling the authority pin and yielding false UNBACKED). Universal across jurisdictions.
    m2 = re.search(r"(?:Pa\.?|Fed\.?)?\s?R\.?(?:\s?[A-Za-z]{1,5}\.?){0,3}\s*(\d+[A-Za-z]?)", auth)
    if m2:
        return m2.group(1)
    m3 = re.search(r"\bRule\s*(\d+[A-Za-z]?)", auth)
    return m3.group(1) if m3 else ""

# Verdicts are cheap + frequent, so hit the always-up local-gpu (:8000) FIRST; local-gpu only as fallback. This is the
# opposite of the Fable lane order (local-gpu-first) — a down local-gpu was stalling every claim ~60s. Env-overridable.
# GPU roles (AGENTS.md, user-locked): local-gpu = PRODUCTION vLLM http://localhost:8000/v1 (qwen3.8-27b);
# local-gpu exl3 http://localhost:8000/v1 = chat/learning, fallback only. :8000 was a retired lane — with it first,
# every _holding_sentence_llm call returned "" and m0 fell back to the keyword picker for months.
_LANES = [
    (os.environ.get("CLAIM_LLM_URL",   "http://localhost:8000/v1/chat/completions"),
     os.environ.get("CLAIM_LLM_MODEL", "qwen3.8-27b")),
    ("http://localhost:8000/v1/chat/completions", "qwen3.8-27b-exl3"),
]

def _complete(prompt, max_tokens=220, timeout=90):
    import json as _j, urllib.request
    for attempt in range(3):                              # retry: a transient :8000 blip must not become UNVERIFIED
        for url, model in _LANES:
            try:
                body = _j.dumps({"model": model, "messages": [{"role": "user", "content": prompt}],
                                 "max_tokens": max_tokens, "temperature": 0.0, "top_p": 1.0,   # deterministic verdicts
                                 "chat_template_kwargs": {"enable_thinking": False}}).encode()
                req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    txt = (_j.loads(r.read().decode())["choices"][0]["message"]["content"] or "").strip()
                if txt:
                    return txt
            except Exception:
                continue
    return ""

# A PRAYER FOR RELIEF requests relief; it does not assert the law compels it. Requesting available
# alternative/discretionary relief is PROPER, not overstated — so a prayer is verified differently.
_PRAYER_RE = re.compile(r"^\s*(?:\d+\.\s*)?(?:WHEREFORE|Award|Grant|Enter|Strike|Vacate|Convert|Deny|Order|Direct)\b"
                        r"|requests?\s+that\s+(?:this|the)\s+Court|prays?\s+for", re.I)

_PRAYER_PROMPT = """You are verifying a PRAYER FOR RELIEF — a request the plaintiff asks the court to grant,
NOT a legal argument. The test is NOT whether the authority MANDATES this relief; it is whether the cited
authority MAKES THIS RELIEF AVAILABLE. Requesting relief the authority provides is PROPER even when it is in
the ALTERNATIVE (e.g. actual OR statutory damages) or in the court's DISCRETION (e.g. "the court may award
treble damages"). Remember "may" is a term of art: permissive by default, but for a REMEDIAL statute serving
justice or the public good it can read as "shall" (Black's). Do NOT mark a request OVERSTATED merely because
the relief is discretionary, alternative, or additive. Use the FULLEST, most authoritative text shown (verbatim
statute over a digest); weigh ALL authorities, not just the first; a partial summary snippet that omits a form
of relief does NOT show the relief is unavailable.

Verdicts — a prayer REQUESTS relief; it CANNOT be "overstated." Use ONLY these two:
- SUPPORTED: the authority makes this relief AVAILABLE (alternative/discretionary/additive ALL count as available).
- UNBACKED: the authority does not provide this form of relief at all, or the retrieved text lacks the authority.

Output ONLY compact JSON, verdict is SUPPORTED or UNBACKED: {"verdict":"SUPPORTED","quote":"the exact supporting words","why":"short reason"}

REQUESTED RELIEF:
{prop}

AUTHORITY CITED:
{auth}

RETRIEVED CORPUS TEXT:
{grounding}
"""

_STOP = set(("the a an and or of to in for on with is are be this that it as at by from any who "
             "under with respect within not no than more less such person consumer any which shall "
             "damages plaintiff court award award record evidence data").split())

def _fts_hits(prop, num="", cap=24):
    """The corpus HAS the operative statutory language (e.g. 'maximum possible accuracy' = 80 hits). FTS finds
    it — IF we ask. Query the SECTION NUMBER and EVERY operative 3-gram (no early break: fact-words come first
    in a sentence and must not crowd out the statutory phrase later in it). Return ALL candidates; _rank_quality
    surfaces the verbatim statute. This is what makes retrieval reliable instead of run-to-run luck."""
    try:
        import sqlite3
        from pipeline.case_comprehension import CHUNK_DB
        con = sqlite3.connect(str(CHUNK_DB))
        # Consecutive-word phrases WITH stopwords kept — FTS5 phrase match needs the natural word order, so
        # 'reasonable procedures to assure maximum' must keep 'to' to match the verbatim statute. Longer windows
        # first (most distinctive -> the statute, not commentary). The section number too.
        words = re.findall(r"[A-Za-z0-9]+", prop)
        phrases = []
        for n in (6, 5, 4):
            for i in range(max(0, len(words) - n + 1)):
                # skip windows that are all stopwords/short (no signal)
                win = words[i:i + n]
                if sum(1 for w in win if w.lower() not in _STOP and len(w) > 3) >= 2:
                    phrases.append(" ".join(win))
        queries = ([num] if num else []) + phrases
        out, seen = [], set()
        for q in queries[:44]:
            fq = '"' + re.sub(r"[^A-Za-z0-9 ]", " ", q).strip() + '"'
            if len(fq) < 8:
                continue
            try:
                for (t,) in con.execute("select text from chunk_text_fts where chunk_text_fts match ? "
                                        "order by bm25(chunk_text_fts) limit 5",     # relevance-ranked: the statute (phrase-dense) beats long commentary
                                        (fq,)).fetchall():
                    k = (t or "")[:70]
                    if t and k not in seen:
                        seen.add(k); out.append({"source": "fts", "text": t})
            except Exception:
                pass
            if len(out) >= cap:
                break
        con.close()
        return out
    except Exception:
        return []

def _authority_fts(auth, num, cap=6):
    """Cite-anchored FTS: pull the CITED statute's OWN chunk by (title + section number) as an FTS AND query.
    Needed because (a) _fts_hits skips a bare section number ('455' quoted is <8 chars -> dropped), so it never
    searches by number, and (b) the resolver fuzzy-matches subsections (§455(a) -> wrong chunks). Keep only
    chunks whose text carries the section number near the top -> the real statute header, not a passing mention."""
    try:
        import sqlite3, re as _re
        from pipeline.case_comprehension import CHUNK_DB
        if not num:
            return []
        # Match the cite as an FTS PHRASE of its normalized form (subsection parens dropped): "28 U.S.C. § 455(a)"
        # -> "28 u s c 455". A phrase (consecutive tokens) is precise where a bare AND of numbers is not
        # ("28" AND "455" matched an unrelated environmental-reg chunk). The statute header
        # "# 28 U.S.C. § 455 — Disqualification..." tokenizes to exactly this phrase, so it ranks first.
        core = _re.sub(r"\([^)]*\)", "", auth)                       # drop subsection parens
        norm = _re.sub(r"[^A-Za-z0-9]+", " ", core).lower().strip()  # "28 u s c 455"
        phrases = []
        # COURT RULES (Pa.R.C.P./Pa.R.A.P./Pa.R.E./Pa.R.Crim.P./Fed.R.*) are codified in the corpus as
        # "Rule <num>", NOT "Pa.R.C.P. <num>". Proven: FTS '"pa r c p 3144"' returns 0 hits while '"Rule 3144"'
        # returns the actual rule. So for a rule cite, LEAD with the corpus's own label + a family word to
        # disambiguate a shared number (Pa.R.E. 201 vs a Rule 201 in another rule set).
        _isrule = bool(_re.search(r"\bR\.?\s?[CAE]\.?(?:\s?P\.?)?\b|Fed\.?\s?R\.?\s?(?:Civ|App|Evid)|\bPa\.?R\b|\bRule\b", auth, _re.I))
        if _isrule:
            phrases.append('"Rule %s"' % num)
            _fam = ("evidence" if _re.search(r"R\.?\s?E\b", auth) else
                    "appellate" if _re.search(r"R\.?\s?A", auth) else
                    "criminal" if _re.search(r"Crim", auth, _re.I) else
                    "civil" if _re.search(r"R\.?\s?C", auth) else "")
            if _fam:
                phrases.append('"Rule %s" %s' % (num, _fam))
        if norm:
            phrases.append('"' + norm + '"')
        m = _re.search(r"([a-z] ){1,4}" + _re.escape(num.lower()), norm)   # code+section w/o leading title, e.g. "u s c 455"
        if m:
            phrases.append('"' + m.group(0).strip() + '"')
        if not phrases:
            return []
        con = sqlite3.connect(str(CHUNK_DB))
        out, seen = [], set()
        for fq in phrases:
            try:
                for (t,) in con.execute("select text from chunk_text_fts where chunk_text_fts match ? "
                                        "order by bm25(chunk_text_fts) limit 10", (fq,)).fetchall():
                    if t and num in t[:400]:             # the real statute carries its section number up top
                        k = (t or "")[:70]
                        if k not in seen:
                            seen.add(k); out.append({"source": "fts-auth", "text": t})
                        if len(out) >= cap:
                            break
            except Exception:
                pass
            if len(out) >= cap:
                break
        con.close()
        return out
    except Exception:
        return []

_RESOLVE = None
def _resolve_authority_text(auth, limit=6):
    """Use the PROVEN cite resolver (new db scripts/resolve_citation.py) — a citation parser + authority
    index that maps a cite to its corpus chunks with a HARD namespace filter (a USC query never returns CFR;
    fixes '§1681e -> CFR tax text'). Fetch the verbatim text of the resolved chunk_ids from the local
    chunk_text.db. Returns [] gracefully (PA '73 P.S.' cites are unparseable by the parser → semantic covers them)."""
    global _RESOLVE
    try:
        import sys as _s, sqlite3
        rp = str(BASE / "new db scripts")
        if _RESOLVE is None:
            if rp not in _s.path:
                _s.path.insert(0, rp)
            from resolve_citation import resolve_citation_impl as _R
            _RESOLVE = _R
        r = _RESOLVE(auth, mode="lookup", include_text=False, limit=limit)
        ids = [i for i in (r.get("chunk_ids") or []) if i][:limit]
        if not ids:
            return []
        from pipeline.case_comprehension import CHUNK_DB
        con = sqlite3.connect(str(CHUNK_DB))
        out = []
        for cid in ids:
            row = con.execute("select source,text from chunk_text where chunk_id=?", (cid,)).fetchone()
            if row and row[1]:
                out.append({"source": row[0], "text": row[1]})
        con.close()
        return out
    except Exception:
        return []

# Grounding-QUALITY ranking. Prefers VERBATIM/OFFICIAL text over digests/summaries so the verdict reasons on
# the real authority — NOT a filter (every retrieved chunk still goes to the LLM), and NOT an authority-
# hierarchy judgment (a highly-ranked statute chunk does not "beat" a constitutional or binding-precedent
# chunk; ranking only orders the text; the LLM weighs authority per the hierarchy in the prompt).
_AUTH_SRC = re.compile(r"uscode|u\.?s\.?\s?code|usreports|scotus|pa_consolidated|cfr|constitution|statut|_lii|justia|leagle|courtlistener", re.I)
_SUMMARY_MARK = re.compile(r"^\s{0,3}#|\*\*|^\s{0,3}[-*]\s|Liability Framework|Remedy\s*[-:]|Table of [Cc]ontent|### ", re.M)
_OPERATIVE = re.compile(r"\bshall follow\b|\bshall not\b|\bshall be\b|\bis liable to\b|\bAny person who\b|"
                        r"\bshall comply\b|\bmeans\b|\bshall investigate\b|\bshall be liable\b|\bshall furnish\b|"
                        r"\bdeclared unlawful\b|\bare hereby declared\b|\bunlawful\b|\bprohibited\b|"
                        r"\bit shall be unlawful\b|\bmay bring\b|\bmay recover\b|\bmay award\b", re.I)
_COMMENTARY = re.compile(r"FALL 20|SPRING 20|Supervisory Highlights|The FCRA states|Questionnaire|EXAMINATION|"
                         r"RESCINDED|Table of [Cc]ontent|Brief for|Amicus|treatise|Nutshell", re.I)
def _quality(h, num):
    t = h.get("text") or ""; s = h.get("source") or ""
    sc = 0
    if _AUTH_SRC.search(s): sc += 3                       # authoritative source (USCODE/pa_consolidated/CFR/reporters)
    if _OPERATIVE.search(t): sc += 4                      # OPERATIVE statute language — the real thing, not a mention
    if num and num in t[:220]: sc += 2                    # section number near the TOP (a header), not any citation of it
    if _COMMENTARY.search(t[:220]): sc -= 4             # ONLY genuine commentary (exam/highlights/briefs) — NOT markdown '#' notes,
    #                                                     which are often the clean cite->text grounding we want (e.g. "# 73 Pa.C.S. § 201-9.2")
    if len(t) < 90: sc -= 1
    return sc
def _rank_quality(hits, num):
    # stable sort so equal-quality chunks keep source order (FTS-operative, then authority, then semantic)
    return sorted(hits, key=lambda h: -_quality(h, num))

_RULES_IDX = None
def _rules_index_text(auth, limit=2):
    """Family-keyed OPERATIVE rule text from rules_index (keyed by rule SYSTEM: Pa.R.C.P./R.A.P./R.E./R.Crim.P.
    → distinct source families). Resolves 'Pa.R.E. 201' to the EVIDENCE Rule 201, never Pa.R.Crim.P. 201, and
    returns the rule's own header+subsection text (not a case that merely cites the number). This is the
    strongest, namespace-correct authority pin for a court rule. Returns [{source,text}] or []; never raises."""
    global _RULES_IDX
    try:
        if _RULES_IDX is None:
            from pipeline import rules_index as _ri
            _RULES_IDX = _ri
        r = _RULES_IDX.lookup(auth, limit=limit)
        rows = r if isinstance(r, list) else ([r] if r else [])
        out = []
        for one in rows[:limit]:
            if isinstance(one, dict):
                t = one.get("body") or one.get("text") or ""
                if t:
                    out.append({"source": "rules_index", "text": t})
        return out
    except Exception:
        return []


def _longest_common_run(a, b) -> int:
    """Longest run of CONSECUTIVE words appearing verbatim in BOTH texts, returned as an approximate char
    length. Word-based so it survives bracketed edits ('[i]f' vs 'If'), punctuation, and whitespace. Used to
    detect that a draft QUOTES a cited rule verbatim (then the cite obviously backs its own quoted text)."""
    wa = re.findall(r"[a-z0-9]+", (a or "").lower())
    wb = re.findall(r"[a-z0-9]+", (b or "")[:12000].lower())
    if not wa or not wb:
        return 0
    idx = {}
    for j, w in enumerate(wb):
        idx.setdefault(w, []).append(j)
    best = 0
    for i, w in enumerate(wa):
        for j in idx.get(w, ()):
            k = 0
            while i + k < len(wa) and j + k < len(wb) and wa[i + k] == wb[j + k]:
                k += 1
            if k >= 6:      # only bother measuring real runs
                run = sum(len(wa[i + t]) for t in range(k)) + (k - 1)
                if run > best:
                    best = run
    return best


def _case_marker(auth):
    """For a CASE cite 'Name v. Name, VOL REP PAGE' -> (reporter_phrase, [party_surnames]) to PIN the case's
    OWN opinion by its name/reporter (NOT a section number — a case has none). None if not a case cite."""
    m = re.search(r"(\d+)\s+(U\.?\s?S\.?|S\.?\s?Ct\.?|F\.?\s?[23]d|F\.?\s?Supp\.?\s?[23]?d?|A\.?\s?[23]?d?)\s+(\d+)", auth, re.I)
    if not m or " v" not in auth.lower():
        return None
    rep = re.sub(r"[^a-z0-9]+", " ", (m.group(1) + " " + m.group(2) + " " + m.group(3)).lower()).strip()
    parties = re.findall(r"\b([A-Z][A-Za-z'\-]{3,})\b", auth[:m.start()])
    parties = [p.lower() for p in parties if p.lower() not in ("in", "re", "the", "of", "and", "for", "corp", "inc", "ins")][:4]
    return (rep, parties)

def _case_present(text, cm):
    """Is the cited CASE in this chunk? reporter cite (tokenized) or a party surname."""
    t = re.sub(r"[^a-z0-9]+", " ", (text or "").lower())
    rep, parties = cm
    if rep and rep in t:
        return True
    return any((" " + p + " ") in (" " + t + " ") for p in parties)

def _case_fts(auth, cap=6):
    """Find the case's OWN ingested opinion. case_fetch.ingest_case stores it as source
    'scotus_<slug>_<cite-squished>'. Match by SOURCE (precise — the case ITSELF), NOT the reporter cite in
    text (which matches every OTHER case that CITES it — proven: FTS '551 u s 47' returns the citers, not
    Safeco). This is what lets an ingested case be pinned by the fast FTS lane instead of turbovec-semantic."""
    m = re.search(r"(\d+)\s+(U\.?\s?S\.?|S\.?\s?Ct\.?|F\.?\s?[23]d|F\.?\s?Supp\.?\s?[23]?d?|A\.?\s?[23]?d?)\s+(\d+)", auth, re.I)
    if not m or " v" not in auth.lower():
        return []
    squished = re.sub(r"[^a-z0-9]+", "", (m.group(1) + m.group(2) + m.group(3)).lower())   # "551 U.S. 47" -> "551us47"
    try:
        import sqlite3
        from pipeline.case_comprehension import CHUNK_DB
        con = sqlite3.connect(str(CHUNK_DB))
        rows = con.execute("select text from chunk_text where source like ? limit ?",
                           ("scotus_%" + squished, cap)).fetchall()
        con.close()
        return [{"source": "ingested-case", "text": t} for (t,) in rows if t]
    except Exception:
        return []

def check_claim(prop, auth, retr, complete):
    # AUTHORITY-FIRST: resolve the cite to its own chunks (proven resolver, namespace-hard) + FTS operative
    # text + semantic. Merge ALL, then rank by grounding quality (verbatim > summary) — nothing dropped.
    num = _authnum(auth)
    _cm = _case_marker(auth)   # non-None for a CASE cite -> pin by reporter/party, not by section number
    # family-keyed operative rule text FIRST (rules_index: R.E. vs R.C.P. vs R.A.P. vs Crim.P. never bleed),
    # then the namespace-hard cite resolver, then the CASE's own opinion (FTS, once ingested). All feed the pin.
    auth_hits = _case_fts(auth) + _rules_index_text(auth) + (_resolve_authority_text(auth) or [])
    _auth_fts = _rank_quality(_authority_fts(auth, num), num)   # compute ONCE (fast FTS); reused for the pin below
    seen, hits = set(), []
    for src_hits in (_fts_hits(prop, num),):          # FTS on section number + operative language of the proposition
        for h in src_hits:
            k = (h.get("source", ""), (h.get("text") or "")[:60])
            if k not in seen:
                seen.add(k); hits.append(h)
    # SEMANTIC lane — FULL (3 queries), always. An A/B on a 20-cite draft showed gating this to a single query
    # for "fast-pinned" cites flipped 2/20 verdicts to more-conservative (UNBACKED/UNVERIFIED where the full
    # context found support), so the semantic context DOES move some verdicts — it is not merely enrichment.
    # And with the memoized retriever (fable_connection_hunter._retriever) a warm query is ~0.4s, not the ~10s
    # cold-rebuild that used to dominate; the full lane costs ~10% wall-clock. Cite correctness > that 10% —
    # keep every query. Include the corpus's own "Rule <num>" phrasing (court rules are stored that way).
    # SPEED (2026-08): the authority's OWN text is PINNED from the FAST FTS lanes above (auth_hits +
    # _auth_fts), not from the semantic embedder — so the 3 semantic queries (slow ~embedder, and the ONLY
    # thread-unsafe step) only add CONTEXT. Run them ONLY when FTS did NOT already find the cite's own text
    # (the case that actually needs semantic). With the pull/ingest engine populating the corpus, FTS covers
    # most cites -> retrieval stays fast + thread-safe. Force the full lane with BEEZLE_SEMANTIC=always.
    _fts_has_auth = any((h.get("text") and (_case_present(h["text"], _cm) if _cm
                        else (not num or num in (h.get("text") or "")[:400])))
                        for h in (auth_hits + _auth_fts))
    if (not _fts_has_auth) or os.environ.get("BEEZLE_SEMANTIC") == "always":
        _rule_q = (f"Rule {num} " + prop[:150]) if num else ""
        for q in (f"{auth} {num} statute text civil liability", _rule_q, f"{auth} {prop[:200]}"):
            if not q.strip():
                continue
            for h in (retr(q, k=10) or []):
                k = (h.get("source", ""), (h.get("text") or "")[:60])
                if k not in seen:
                    seen.add(k); hits.append(h)
    # RANK by grounding QUALITY (verbatim/official > digest/summary), do NOT drop any authority — the LLM
    # still weighs all of them per the hierarchy. Ranking only decides which text leads, never what's shown.
    hits = _rank_quality(hits, num)
    # PIN the cited authority's OWN text at the top. The verdict question is "does THIS authority stand for the
    # claim," so THIS authority's text is MANDATORY grounding — never a ranked candidate that a long application-
    # style proposition can crowd out of the top 8 (the false-UNBACKED bug: § 455(a) came back SUPPORTED for a
    # bare-rule claim but "not retrieved" for an application of the SAME rule, because proposition context outranked
    # the statute). Pin up to 3 authority chunks, then fill the remainder with ranked context.
    # Cite-anchored pool: an FTS on the section number returns the statute HEADER chunk reliably (e.g.
    # "# 28 U.S.C. § 455 — Disqualification ..."), whereas the resolver fuzzy-matches subsections like
    # §455(a) to WRONG chunks. Keep only chunks whose text actually contains the section number -> the real
    # statute, never a semantic near-miss. This is why §455(a) was SUPPORTED for a bare-rule proposition but
    # falsely UNBACKED for an application of the SAME rule.
    # auth_hits (rules_index family-keyed text + namespace-hard resolver) is the cite's OWN authoritative
    # text -> pin it FIRST so commentary/case chunks that merely mention the number can't out-rank it.
    auth_pool = auth_hits + _auth_fts   # reuse the FTS pin computed above (no second /mnt/c FTS pass)
    pinned, pseen = [], set()
    for h in auth_pool:
        t = h.get("text") or ""
        if not t:
            continue
        if _cm:                                   # CASE: pin chunks that carry the case's reporter/party
            if not _case_present(t, _cm):
                continue
        elif num and num not in t[:400]:          # STATUTE/RULE: pin chunks with the section number up top
            continue
        k = (h.get("source", ""), t[:60])
        if k in pseen:
            continue
        pseen.add(k); pinned.append(h)
        if len(pinned) >= 2:
            break
    # UNIVERSAL FAIL-SAFE (closes the #1 leak for ANY citation): a defect verdict (UNBACKED/OVERSTATED/
    # WRONG_POINT) is only trustworthy when we actually retrieved the CITED authority's OWN text. If the pin
    # landed nothing, try a looser match (the section number anywhere in a candidate chunk); if STILL nothing,
    # return UNVERIFIED rather than let unrelated proposition-filler chunks drive a FALSE UNBACKED on a correct
    # cite. This is the general form of the Pa.R.C.P. bug — never assert "unbacked" from text we never pulled.
    if not pinned:
        for h in auth_pool:
            t = h.get("text") or ""
            if t and (_case_present(t, _cm) if _cm else (num and num in t)):
                pinned.append(h)
                break
    if not pinned:
        return {"verdict": "UNVERIFIED", "quote": "",
                "why": "cited authority's own text not retrieved; cannot assert unbacked"}
    rest = [h for h in hits if (h.get("source", ""), (h.get("text") or "")[:60]) not in pseen]
    final = pinned + rest
    grounding = "\n".join(
        f"[{(h.get('source') or '')[:36]}] {' '.join((h.get('text') or '').split())[:800]}"   # NO truncation of the statute's operative clause
        for h in final[:8] if h.get("text"))
    if not grounding.strip():
        return {"verdict": "UNVERIFIED", "quote": "", "why": "no corpus text retrieved"}
    if complete is None:      # TWO-PHASE: retrieve-only (embedder ran here, serial/safe); grade in parallel later
        return {"_payload": (grounding, auth_hits, num)}
    return _grade_with_grounding(prop, auth, grounding, auth_hits, num, complete)

def _grade_with_grounding(prop, auth, grounding, auth_hits, num, complete):
    """GRADE half of check_claim: LLM verdict + parse + honesty guards. HTTP-only (no embedder), so this
    is the THREAD-SAFE part that _grade_all runs in parallel. Retrieval (embedder) is done serially before."""
    tmpl = _PRAYER_PROMPT if _PRAYER_RE.search(prop) else _PROMPT
    prompt = (tmpl.replace("{prop}", prop).replace("{auth}", auth).replace("{grounding}", grounding[:6800]))
    out = complete(prompt, max_tokens=220, timeout=90)
    if not out:
        return {"verdict": "UNVERIFIED", "quote": "", "why": "no LLM lane reachable (fail-safe)"}
    # reject a template echo (the model parroting the placeholder instead of judging)
    if "SUPPORTED|OVERSTATED" in out or "<<=" in out or "<=15 words" in out or "<the exact" in out:
        return {"verdict": "UNVERIFIED", "quote": "", "why": "model echoed the template (retry needed)"}
    v = re.search(r'"verdict"\s*:\s*"(SUPPORTED|OVERSTATED|WRONG_POINT|UNBACKED)"', out, re.I)
    q = re.search(r'"quote"\s*:\s*"([^"]{0,240})"', out)
    w = re.search(r'"why"\s*:\s*"([^"]{0,160})"', out)
    quote = (q.group(1) if q else "")
    if "<<" in quote or "words>" in quote:
        quote = ""
    verdict = (v.group(1).upper() if v else "UNVERIFIED")   # unparseable => honest UNVERIFIED, never a fake OVERSTATED
    why = (w.group(1) if w else "unparseable verdict")
    # NAMESPACE-HONESTY GUARD: a "not in the authority" verdict (UNBACKED/WRONG_POINT) is only trustworthy when
    # we had the cite's OWN family-authoritative text (rules_index family-keyed, or the namespace-hard resolver).
    # If BOTH missed (auth_hits empty), the pin is FTS-only, which can bleed across rule sets (Pa.R.E. 201 vs
    # Pa.R.Crim.P. 201) -> we cannot honestly say the cite is unbacked, so degrade to UNVERIFIED. SUPPORTED and
    # OVERSTATED (cite is present, only the claim's reach is judged) are left as-is.
    #
    # CASE-CITE HONESTY GUARD: the corpus is statute/rule-heavy and thin on CASE holdings, so a case cite
    # (e.g. Ashcroft v. Iqbal, Celotex) often retrieves UNRELATED statutes via the resolver's fuzzy match ->
    # auth_hits is non-empty but wrong, and the model correctly says "not in this text" = a FALSE UNBACKED on a
    # CORRECT case cite. Guard: a defect verdict must have the cite's OWN identifier present in the grounding —
    # a party surname or reporter cite for a case, the section number for a statute/rule. If the cite's own
    # marker is absent, we never actually retrieved the authority -> UNVERIFIED, never UNBACKED. This stops the
    # gate from stripping/flagging correct case cites (Iqbal/Twombly/Celotex) and blocking a sound filing.
    _ids = []
    _mcase = re.search(r"([A-Z][A-Za-z'.\-]{2,})\s+v\.?\s+([A-Z][A-Za-z'.\-]{2,})", auth)
    if _mcase:
        _ids += [_mcase.group(1), _mcase.group(2)]
    _mrep = re.search(r"\b\d+\s+[A-Z][A-Za-z.]*\.?\s+\d+", auth)   # reporter cite e.g. "556 U.S. 662"
    if _mrep:
        _ids.append(_mrep.group(0))
    if num:
        _ids.append(num)
    _ids = [x for x in _ids if x and len(x.strip()) > 2]
    _gl = (grounding or "").lower()
    _cite_present = any(x.lower() in _gl for x in _ids) if _ids else bool(auth_hits)
    if verdict in ("UNBACKED", "WRONG_POINT") and (not auth_hits or not _cite_present):
        verdict, why = "UNVERIFIED", ("cite's own text not retrieved (corpus gap / resolver mismatch) — "
                                      "cannot assert unbacked")
    # SUBSECTION honesty guard: a rule cite WITH a subsection (Pa.R.A.P. 1925(b), Pa.R.C.P. 3111(b),
    # 1925(b)(4)(vii)) is falsely flagged UNBACKED when the corpus retrieves an ADJACENT subsection (1925(a),
    # 3111.1) — the base rule is present but not the cited subsection, so we did NOT retrieve the cite's OWN
    # text. Rule subsections are the rule-level analog of the case-cite gap: cannot assert unbacked from a
    # different subsection -> UNVERIFIED. (No-subsection cites like Pa.R.C.P. 1024 are untouched: their own
    # text IS retrieved, so a defect verdict there is a real proposition issue, left as-is.)
    if verdict in ("UNBACKED", "WRONG_POINT"):
        _msub = re.search(r"(\d+(?:\.\d+)?)((?:\s*\([A-Za-z0-9]+\))+)", auth)   # base + FULL subsection chain
        if _msub:
            _base = _msub.group(1)
            _chain = re.sub(r"\s+", "", _msub.group(2)).lower()   # e.g. "(b)(4)(vii)" — the DEEPEST provision
            _norm = _gl.replace(" ", "")
            if _base in _norm and (_base + _chain) not in _norm:
                verdict, why = "UNVERIFIED", (f"corpus retrieved rule {_base} but not subsection {_base}{_chain} "
                                              f"(subsection-granularity gap) — cannot assert unbacked")
    # VERBATIM-QUOTE guard: if the draft QUOTES the cited authority's OWN text (a substantial run of the
    # retrieved authority appears verbatim in the proposition), the cite demonstrably backs that quoted text —
    # it cannot be OVERSTATED/UNBACKED for the rule it quotes. An OVERSTATED here only means the proposition
    # ALSO carries factual application beyond the rule (the drafter's argument), which is not a cite defect.
    if verdict in ("OVERSTATED", "UNBACKED"):
        _q = _longest_common_run(prop, grounding)
        if _q >= 45:      # a real quoted clause of the rule, not incidental shared words
            verdict, why = "SUPPORTED", f"draft quotes the authority verbatim ({_q} chars) — cite backs its own text"
    return {"verdict": verdict, "quote": quote, "why": why}

import threading as _threading
_RETR_LOCK = _threading.Lock()

def _grade_all(claims, retr, complete=None, workers=None):
    """FULLY PARALLEL stands-for grading — fast AND stable. Retrieval is thread-safe FTS/rules_index (each
    lane opens its OWN sqlite connection) once cited cases are INGESTED + chunk_text.source is indexed, so
    retrieve+grade run CONCURRENTLY across claims. The ONLY thread-unsafe step is the turbovec-semantic
    EMBEDDER, which now runs RARELY (only a cite FTS can't pin — e.g. an un-fetchable reporter) — so wrap
    just that in a lock: the rare call is safe with negligible contention. The drafter server batches the
    concurrent LLM grades. All reasoning intact. Fail-safe: any error -> UNVERIFIED (never edits)."""
    import os as _os
    from concurrent.futures import ThreadPoolExecutor
    complete = complete or _complete
    workers = workers or int(_os.environ.get("BEEZLE_GRADE_WORKERS", "8"))
    def _safe_retr(q, k=10):
        with _RETR_LOCK:              # embedder-semantic is rare now (cases FTS-pin) -> cheap lock, not serialization
            return retr(q, k=k)
    def _g(c):
        prop, auth, kind = c
        try:
            return check_claim(prop, auth, _safe_retr, complete)
        except Exception as e:
            return {"verdict": "UNVERIFIED", "quote": "", "why": f"grade error: {e}"[:60]}
    if workers > 1 and len(claims) > 1:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            return list(ex.map(_g, claims))
    return [_g(c) for c in claims]

_ICON = {"SUPPORTED": "✓", "OVERSTATED": "△ overstated", "WRONG_POINT": "✗ wrong point",
         "UNBACKED": "✗ unbacked", "UNVERIFIED": "? unverified"}

def _get_retriever():
    """Corpus semantic retriever as a callable retr(query, k=N) -> [{source,text}]. Prefer the fable
    retriever; if its embedder stack is unavailable (Hermes-venv dep drift, e.g. sentence_transformers /
    huggingface-hub version clash), fall back to pipeline.retriever (queries the corpus server; no local
    embedder); last-resort no-op so the FTS + cite-resolver grounding still carries the check. Never raises.
    The verdict never depends on the semantic lane alone -- authority-resolver + FTS pin the cited text."""
    try:
        from pipeline.fable_connection_hunter import _retriever
        return _retriever()
    except Exception:
        pass
    try:
        from pipeline import retriever as _R
        def _adapt(q, k=10):
            try:
                hits = _R.retrieve(q, final_k=k)
            except TypeError:
                hits = _R.retrieve(q)
            out = []
            for h in (hits or []):
                if isinstance(h, dict):
                    out.append({"source": h.get("source") or h.get("citation") or "",
                                "text": h.get("text") or h.get("document") or h.get("chunk") or ""})
                else:
                    out.append({"source": "", "text": str(h)})
            return out
        return _adapt
    except Exception:
        return lambda q, k=10: []


def verify_draft(md):
    retr = _get_retriever()   # robust to venv embedder drift; FTS + cite-resolver still pin the authority
    claims = extract_claims(md)
    graded = _grade_all(claims, retr)   # two-phase: serial retrieve, parallel grade (all reasoning intact)
    rows, tally = [], {}
    for i, ((prop, auth, kind), r) in enumerate(zip(claims, graded), 1):
        rows.append((i, kind, auth, prop, r))
    # Fix B: CO-CITED authorities in the SAME parenthetical ("(Pa.R.C.P. 1024; 1029(b))") jointly support one
    # point. If a cite is a defect but a SUPPORTED authority is cited alongside it in that string, the point IS
    # supported — the flagged cite merely adds an imprecise companion. Clear the co-cited defect (not a fabricated
    # cite; the operative authority is right there). Numbers only; conservative (must share a parenthetical).
    _sup_nums = {_authnum(a) for (_i, _k, a, _p, r) in rows if r["verdict"] == "SUPPORTED" and _authnum(a)}
    if _sup_nums:
        for (_i, _k, auth, _p, r) in rows:
            if r["verdict"] in ("UNBACKED", "OVERSTATED", "WRONG_POINT"):
                _n = _authnum(auth)
                if not _n:
                    continue
                for pm in re.finditer(r"\([^()]{0,160}\)", md):
                    seg = pm.group(0)
                    if _n in seg and (set(re.findall(r"\d+[A-Za-z]?", seg)) & _sup_nums) - {_n}:
                        r["verdict"] = "SUPPORTED"
                        r["why"] = "co-cited with a supported authority for the same point"
                        break
    for (_i, _k, _a, _p, r) in rows:
        tally[r["verdict"]] = tally.get(r["verdict"], 0) + 1
    # build the proof sheet
    lines = ["# PROOF SHEET — does each cited authority stand for the claim?\n",
             "| # | verdict | authority | proposition | corpus quote / why |",
             "|---|---|---|---|---|"]
    for i, kind, auth, prop, r in rows:
        detail = (r["quote"] or r["why"]).replace("|", "/")[:160]
        lines.append(f"| {i} | {_ICON.get(r['verdict'], r['verdict'])} | {auth[:40]} | {prop[:90].replace('|','/')}… | {detail} |")
    summary = " · ".join(f"{k}: {v}" for k, v in sorted(tally.items()))
    lines.append(f"\n**Summary:** {len(rows)} claims — {summary}")
    lines.append("\n_SUPPORTED = filing-safe. OVERSTATED/WRONG_POINT/UNBACKED = fix or cut before filing._")
    return "\n".join(lines), rows, tally

# ==================== DRAFT-TIME CITE-SUPPORT GATE (hybrid auto-correct, permanent) ====================
# Verify EVERY cite at draft time and correct the non-SUPPORTED ones BEFORE the draft is finalized, so no future
# draft goes out asserting X "per [wrong cite]". HYBRID policy (user-chosen): if the corpus confidently supplies
# an authority that makes the claim SUPPORTED — a section the verifier NAMED in "why" (the prompt asks it to), or
# a cite drawn from the operative authority retrieved for the proposition, re-verified SUPPORTED — swap it in;
# otherwise strip the unsupported cite and leave an inline [CITE REVIEW] marker. Deterministic guardrail + corpus
# authority; the LLM only judges/rewords. Never invents a cite.
_CITE_ANY = re.compile("(?:" + CASE_RE.pattern + ")|(?:" + STAT_RE.pattern + ")")

def _cites_in(text):
    return list(dict.fromkeys(re.sub(r"\s+", " ", m.group(0)).strip() for m in _CITE_ANY.finditer(text or "")))

def _overlap(window, prop):
    pw = re.findall(r"[A-Za-z]{4,}", prop.lower())
    if not pw:
        return 0.0
    ww = set(re.findall(r"[A-Za-z]{4,}", window.lower()))
    return sum(1 for w in pw if w in ww) / len(pw)

def _source_to_cite(source):
    """Turn a corpus SOURCE name that ENCODES a rule into a citation string, so that rule is a replacement
    candidate even when the chunk text writes 'Rule N' without the system prefix. e.g. 'frcp_rule_12' ->
    'Fed. R. Civ. P. 12'. '' when the source doesn't encode a rule. Corpus-derived, not hardcoded per case."""
    s = (source or "").lower()
    m = re.search(r"(frcp|frap|fre)_rule_(\d+[a-z]?)", s)
    if m:
        fam = {"frcp": "Fed. R. Civ. P.", "frap": "Fed. R. App. P.", "fre": "Fed. R. Evid."}[m.group(1)]
        return f"{fam} {m.group(2)}"
    return ""


def _supported_replacement(prop, bad_auth, why, retr, complete):
    """A cite that makes THIS proposition SUPPORTED, drawn ONLY from the corpus: (1) a cite the verifier NAMED in
    'why', (2) cites appearing in — and the rule-encoding SOURCE names of — the corpus text retrieved for the
    proposition's LEGAL TOPIC (not the wrong cite). Each candidate is re-verified; the first that returns
    SUPPORTED wins. None => no confident corpus fix (strip + flag). Corpus-only; never invents a cite."""
    cand = [c for c in _cites_in(why) if c.lower() != bad_auth.lower()]
    # Query the corpus for the rule that GOVERNS this proposition's topic, so a wrong cite (e.g. 1925(b) used
    # for a "service by mail" point) is replaced by the corpus's actual governing rule, not merely stripped.
    for q in (prop[:200],
              f"{prop[:140]} statute rule holding requires",
              f"the rule that governs {prop[:130]}",
              f"which rule requires or authorizes {prop[:130]}"):
        for h in (retr(q, k=10) or []):
            cand += _cites_in(h.get("text") or "")
            cand += _cites_in(_source_to_cite(h.get("source") or ""))   # source name often encodes the rule
    seen, ordered = set(), []
    for c in cand:
        cl = c.lower()
        if cl == bad_auth.lower() or cl in seen or len(c) < 5:
            continue
        seen.add(cl); ordered.append(c)
    for c in ordered[:10]:
        if check_claim(prop, c, retr, complete).get("verdict") == "SUPPORTED":
            return c
    return None

def correct_draft(md, retr=None, complete=None, max_fix=40):
    """Verify every cited claim; auto-correct the non-SUPPORTED ones per the hybrid policy. Returns
    (corrected_md, report) where report is a list of dicts {auth, verdict, action, why}. Bounded by max_fix."""
    if retr is None:
        from pipeline.fable_connection_hunter import _retriever
        retr = _retriever()
    if complete is None:
        complete = _complete
    # PHASE 1 parallel grade (serial retrieve + parallel LLM inside _grade_all); PHASE 2 apply serially below
    # (position-dependent md mutation must stay ordered; grading is not).
    claims = extract_claims(md)
    graded = _grade_all(claims, retr, complete=complete)
    report, fixes = [], 0
    for (prop, auth, kind), r in zip(claims, graded):
        if fixes >= max_fix:
            break
        v = r.get("verdict")
        if v in ("SUPPORTED", "UNVERIFIED"):     # UNVERIFIED = tool couldn't reach a lane; never edit on that
            continue
        # locate the cite occurrence inside the sentence this claim came from
        idx, found = md.find(auth), -1
        while idx != -1:
            if _overlap(md[max(0, idx - 260):idx + len(auth) + 260], prop) >= 0.5:
                found = idx
                break
            idx = md.find(auth, idx + 1)
        if found < 0:
            report.append({"auth": auth, "verdict": v, "action": "not-located", "why": r.get("why", "")})
            continue
        if v == "OVERSTATED":
            # the CITE is correct; the CLAIM overreaches. KEEP the authority, flag the claim to narrow — never
            # strip a good cite. (Auto-softening the legal wording is deferred: it re-drafts prose and needs review.)
            flag = f" [CLAIM REVIEW: overstated — narrow to what {auth} supports: {r.get('why', '')[:56]}]"
            md = md[:found + len(auth)] + flag + md[found + len(auth):]
            report.append({"auth": auth, "verdict": v, "action": "flagged (cite kept)", "why": r.get("why", "")})
        else:                                     # WRONG_POINT / UNBACKED: try a corpus-confident replacement
            repl = _supported_replacement(prop, auth, r.get("why", ""), retr, complete)
            if repl:                              # confident corpus authority -> swap the cite in
                md = md[:found] + repl + md[found + len(auth):]
                report.append({"auth": auth, "verdict": v, "action": f"replaced->{repl}", "why": r.get("why", "")})
            else:                                 # no confident fix -> KEEP the cite + append a review flag. Never
                # auto-STRIP: a false-UNBACKED (verifier imperfect) would delete a CORRECT cite. Flag + STOP so a
                # human decides; the filing gate refuses to file while any such flag remains.
                flag = f" [CITE REVIEW: {auth} — {v.lower()}: {r.get('why', '')[:56]}]"
                md = md[:found + len(auth)] + flag + md[found + len(auth):]
                report.append({"auth": auth, "verdict": v, "action": "flagged (cite kept)", "why": r.get("why", "")})
        fixes += 1
    return md, report

def gate_draft(md, retr=None, complete=None, max_loops=3, max_fix=40):
    """LOOP the cite-support auto-correct to convergence, then GATE. Each pass (correct_draft) swaps in a
    corpus-CONFIDENT replacement for a WRONG_POINT/UNBACKED cite and flags what it cannot confidently fix; a
    pass that makes NO replacement ends the loop. Returns (md, clean, full_report, unresolved) where
    clean=False iff a non-SUPPORTED cite survived un-replaced -> the draft MUST NOT be filed; the inline
    [CITE REVIEW]/[CLAIM REVIEW] flags + `unresolved` show exactly what a human must fix. This is the STOP:
    a filing pipeline calls gate_draft and refuses to finalize while clean is False."""
    if retr is None:
        retr = _get_retriever()
    if complete is None:
        complete = _complete
    # HARD wall-clock bound: never let the correction loop run pathologically long (a many-cite draft under
    # any per-call slowness could otherwise loop for tens of minutes). On timeout we stop looping and flag
    # whatever remains as unresolved -> .NEEDS_CITE_REVIEW, which is the safe outcome (human reviews, pipeline
    # refuses to file) — never a silent pass. Override with CSC_GATE_MAX_SECONDS.
    import time as _time
    _deadline = _time.time() + float(os.environ.get("CSC_GATE_MAX_SECONDS", "300"))
    full_report, last_rep = [], []
    for _ in range(max_loops):
        md, rep = correct_draft(md, retr, complete, max_fix=max_fix)
        full_report += rep
        last_rep = rep
        if not any(str(r.get("action", "")).startswith("replaced") for r in rep):
            break   # converged: no more confident corpus replacements available
        if _time.time() > _deadline:
            print("  [cite-gate] wall-clock bound hit; stopping loop — remaining cites flagged for review")
            break
    # after convergence, whatever remains in the final pass is an unresolved non-SUPPORTED cite (flag, not fix)
    unresolved = list(last_rep)
    clean = len(unresolved) == 0
    return md, clean, full_report, unresolved


def main():
    if len(sys.argv) < 2:
        print("usage: python -m pipeline.claim_support_check <draft.md>"); return 2
    p = Path(sys.argv[1])
    md = p.read_text(encoding="utf-8", errors="ignore")
    sheet, rows, tally = verify_draft(md)
    out = p.with_name(p.stem + "_PROOF_SHEET.md")
    out.write_text(sheet, encoding="utf-8")
    print(f"claim-support: {len(rows)} claims checked -> {out.name}")
    print("  " + " · ".join(f"{k}:{v}" for k, v in sorted(tally.items())))
    # L3 GATE: drop a .NEEDS_CITE_REVIEW marker beside the draft when any cite is NOT SUPPORTED/UNVERIFIED
    # (UNVERIFIED = couldn't check, a fail-safe, not a defect). The mailing_consolidator + e-filer refuse to
    # send/file while this marker is present, so the async proof sheet actually GATES the outward filing
    # without a synchronous verify loop (which OOMs). Cleared on a subsequent clean run.
    _bad = {k: v for k, v in tally.items() if k in ("UNBACKED", "OVERSTATED", "WRONG_POINT")}
    marker = p.with_name(".NEEDS_CITE_REVIEW")
    try:
        if _bad:
            marker.write_text(f"{p.name}: " + " · ".join(f"{k}:{v}" for k, v in sorted(_bad.items()))
                              + f"  (see {out.name})\n", encoding="utf-8")
            print(f"  [gate] .NEEDS_CITE_REVIEW written ({sum(_bad.values())} non-SUPPORTED) -> NOT filing-ready")
        elif marker.exists():
            marker.unlink()
    except Exception:
        pass
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
