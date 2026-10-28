"""definitions_tagger — the Definitions-Schedule + defined-term tagging system (master-plan Stage 1
applied to drafting). Closes the structural "failure to state a claim" / ambiguity attack the way
English law actually solves it: define every weight-bearing term once, tag it in the body with a
numbered marker, link it to a Definitions Schedule (Schedule A / appendix), and use the defined term
MECHANICALLY (no elegant variation — the AI-prose failure mode).

This is NOT QLPSG. It is conventional pleading discipline (Garner / Twombly-Iqbal) applied rigorously.

For a draft it produces:
  1. SCHEDULE A — Definitions: numbered entries (term | definition | source authority cite | first-use para).
     Statutory terms anchored to the controlling section (optionally grounded verbatim from the corpus).
  2. A TAGGED body — first use becomes  Term ("Term")[N]  with a footnote marker [N] -> Schedule A entry N.
  3. ELEGANT-VARIATION flags — every synonym/pronoun used where a defined term should appear (fix before filing).
  4. Obsidian definition nodes — one per term, so terminology stays consistent across the whole case file.

Non-destructive: reads draft text; writes output/definitions/ + an Obsidian note. Deterministic; the
statutory definitions come from a stable catalog, and `--ground` pulls the verbatim corpus text.

CLI:
  python -m pipeline.definitions_tagger --file draft.md --slug fed_26-cv-00456
  python -m pipeline.definitions_tagger --file draft.md --slug fed_26-cv-00456 --ground
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import sys, re, json
from pathlib import Path

ROOT = next((p for p in (_eroot(), _eroot()) if (p / "cases_index").exists()),
            _eroot())
OUT = ROOT / "output" / "definitions"
NOTES = ROOT / "obsidian_sync" / "studies" / "definitions"

# Stable statutory term-of-art catalog: term -> (definition, controlling cite, grounding seed).
# These are the weight-bearing pleading terms; the cite anchors each, --ground pulls verbatim text.
_STATUTORY = {
    "Debt Collector": ("a person who regularly collects or attempts to collect debts owed or due another",
                       "15 U.S.C. 1692a(6)", "debt collector 1692a(6) regularly collects debts owed another"),
    "Consumer Report": ("a communication by a consumer reporting agency bearing on creditworthiness used for a permissible purpose",
                        "15 U.S.C. 1681a(d)", "consumer report 1681a definition consumer reporting agency"),
    "Furnisher": ("an entity that furnishes information to a consumer reporting agency, with a reinvestigation duty",
                  "15 U.S.C. 1681s-2(b)", "furnisher reinvestigation duty 1681s-2 accuracy"),
    "Qualified Written Request": ("a borrower's written correspondence identifying the account and the error/info sought",
                                 "12 U.S.C. 2605(e)", "qualified written request 2605 servicer response duty"),
    "Person Entitled to Enforce": ("the holder, a nonholder with the rights of a holder, or a person under 3-309/3-418(d)",
                                  "UCC 3-301", "person entitled to enforce instrument holder 3-301 foreclose"),
    "Holder in Due Course": ("a holder who took the instrument for value, in good faith, without notice of defenses",
                            "UCC 3-302", "holder in due course value good faith without notice 3-302"),
    "Creditor": ("a person to whom an obligation is owed", "UCC 1-201(b)(13)", "creditor person to whom obligation owed 1-201"),
    "Obligor": ("a person obligated to pay the instrument or perform the obligation", "UCC 9-102 / 3-103",
                "obligor person obligated to pay instrument 9-102 3-103"),
    "Standing": ("Article III injury-in-fact, traceable causation, and redressability", "U.S. Const. art. III; Lujan 504 U.S. 555",
                "Article III standing injury in fact causation redressability"),
}

# Stopwords that are NOT definable proper nouns
_STOP = {"The", "This", "That", "Plaintiff", "Defendant", "Court", "Order", "Motion", "Exhibit",
         "Notice", "Complaint", "United", "States", "Honorable", "Federal", "Rule", "Section"}
# Synonym families the AI tends to drift into for a named party/instrument (elegant variation = ambiguity)
_VARIATION = ["the bank", "the lender", "the servicer", "the company", "the institution", "the carrier",
              "the insurer", "the creditor", "the firm", "the entity", "said defendant", "the loan",
              "the mortgage", "the note", "the policy", "the account"]


def _ground(seed: str):
    try:
        from pipeline.case_comprehension import _retrieve
        from beezle_mcp_server import _retr_ctx
        idx, ids, con, model = _retr_ctx()
        hits = _retrieve(idx, ids, con, model, seed, k=2)
        return (hits[0].get("text") or "")[:300] if hits else ""
    except Exception:
        return ""


def extract_terms(text: str):
    """Return (statutory_terms_present, party_instrument_candidates)."""
    statutory = [t for t in _STATUTORY
                 if re.search(r"\b" + re.escape(t) + r"\b", text, re.I)
                 and (t not in _TERM_CONTEXT or re.search(_TERM_CONTEXT[t], text, re.I))
                 and not (_forum_is_state(text) and t in _FEDERAL_ONLY_TERMS)]
    # named parties: 'Defendant <Proper Noun(s)>' / 'Plaintiff <Name>' / capitalized multiword orgs
    parties = set()
    for m in re.finditer(r"\b(?:Defendant|Plaintiff)\s+([A-Z][A-Za-z&.,'-]+(?:\s+[A-Z][A-Za-z&.,'-]+){0,4})", text):
        name = m.group(1).strip(" ,.")
        if name.split()[0] not in _STOP:
            parties.add(name)
    # instruments: 'the Note', 'the Mortgage', 'the Policy', 'the Loan', 'the Agreement'
    instruments = set(re.findall(r"\bthe\s+(Note|Mortgage|Policy|Loan|Agreement|QWR|Default Notice|Assignment)\b", text))
    return statutory, sorted(parties), sorted(instruments)


# The hardcoded term table is a credit/FCRA/mortgage vocabulary. A filing in any other
# subject matter (insurance, landlord-tenant, employment) therefore gets a Schedule A of
# one irrelevant entry -- worse than none, because it tells the court we define terms we
# never use and leave the operative ones undefined. The doctrine corpus already holds 573
# defined terms with their controlling authority; draw the schedule from what the FILING
# actually uses, and fall back to the static table when the corpus is unavailable.
def _doctrine_db() -> str:
    """Corpus path, resolved from the repo root rather than this file's location
    (a sandboxed copy of this module must still read the real corpus)."""
    import os
    env = os.environ.get("BEEZLE_DOCTRINE_DB")
    if env and Path(env).exists():
        return env
    here = Path(__file__).resolve()
    for base in list(here.parents) + [_eroot()]:
        cand = base / "output" / "audit" / "doctrine_corpus.db"
        if cand.exists():
            return str(cand)
    return str((_eroot() / "output/audit/doctrine_corpus.db"))


def _corpus_terms(text: str, limit: int = 12) -> list:
    """(term, definition, authority) for corpus-defined terms this filing actually uses."""
    import sqlite3
    out = []
    try:
        _db = _doctrine_db()
        if not Path(_db).exists():
            return out
        con = sqlite3.connect(f"file:{_db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT term, canonical, definition, authorities FROM beezle_doctrine_chunks "
            "WHERE category IN ('definition','doctrine','equity','pleading_standard')").fetchall()
        con.close()
    except Exception:
        return out
    low = (text or "").lower()
    seen = set()
    _state = _forum_is_state(text)
    for r in rows:
        canon = (r["canonical"] or r["term"] or "").strip()
        if not canon:
            continue
        # the human-readable phrase: "conversion_pa_law" -> "conversion"
        phrase = re.sub(r"_(pa_law|pennsylvania|federal|law)$", "", str(r["term"] or ""))
        phrase = phrase.replace("_", " ").strip()
        if len(phrase) < 4 or phrase in seen:
            continue
        # count only mechanical use: the term must appear as a whole phrase, 2+ times
        n = len(re.findall(r"\b" + re.escape(phrase) + r"\b", low))
        if n < 2:
            continue
        body = str(r["definition"] or "")
        m = re.search(r"##\s*Definition\s*\n+(.+?)(?=\n##|\Z)", body, re.S)
        defn = " ".join((m.group(1) if m else body).split())
        # strip any leftover markdown scaffolding: a schedule entry is prose
        defn = re.sub(r"^#+\s*", "", defn)
        defn = re.sub(r"\*\*[^*]+?:\*\*", "", defn)
        defn = re.sub(r"#+\s*\w[\w \-]*", " ", defn)
        defn = " ".join(defn.split()).strip(" -:")
        if not defn or len(defn) < 25:
            continue
        defn = _tidy_definition(defn)
        auth = ""
        try:
            import json as _j
            al = _j.loads(r["authorities"] or "[]")
            auth = "; ".join(a for a in al if a) if isinstance(al, list) else ""
        except Exception:
            auth = ""
        if not auth:
            am = re.search(r"\*\*Citation:\*\*\s*([^\n]+)", body)
            src = re.search(r"##\s*Source\s*\n+([^\n]+)", body)
            auth = (am.group(1).strip() if am else (src.group(1).strip() if src else ""))
        # forum discipline: a state filing does not define its terms from federal
        # procedure, and federal-only doctrine has no place in a CCP schedule
        _fed_auth = re.search(r"Fed\.\s*R\.|U\.S\.C|federal", auth or "", re.I)
        if _state and (_fed_auth or phrase.title() in _FEDERAL_ONLY_TERMS):
            continue
        seen.add(phrase)
        out.append((phrase.title(), defn, auth or f"{_FORUM_SOV} common law", n))
    # most-used terms first: the schedule should define what the brief leans on
    out.sort(key=lambda x: -x[3])
    return [(t, d, a) for t, d, a, _n in out[:limit]]


def build(text: str, slug: str = "", ground: bool = False) -> dict:
    statutory, parties, instruments = extract_terms(text)
    entries, n = [], 0
    _have = set()
    for t in statutory:
        n += 1
        defn, cite, seed = _STATUTORY[t]
        verbatim = _ground(seed) if ground else ""
        entries.append({"n": n, "term": t, "definition": (verbatim or defn), "source": cite, "kind": "statutory"})
        _have.add(t.lower())
    # corpus-defined terms the filing actually uses (subject-matter agnostic)
    for _t, _d, _a in _corpus_terms(text):
        if _t.lower() in _have:
            continue
        n += 1
        _have.add(_t.lower())
        entries.append({"n": n, "term": _t, "definition": _d, "source": _a, "kind": "doctrine"})
    # merge party variants: drop a name whose words are a subset of a longer party name (keep full legal name)
    pw = {p: set(p.lower().split()) for p in parties}
    canon = [p for p in parties if not any(p != q and pw[p] < pw[q] for q in parties)]
    # party-name Schedule A entries removed: caption + PARTIES section already
    # define the parties; a name defining itself earned three reviewer flags.
    for ins in instruments:
        n += 1
        entries.append({"n": n, "term": ins, "definition": f"the {ins} referenced in this filing (identify by date + number)",
                        "source": "record", "kind": "instrument"})
    # elegant-variation flags: synonyms present that should be a defined term
    variation = []
    low = text.lower()
    for v in _VARIATION:
        for m in re.finditer(r"\b" + re.escape(v) + r"\b", low):
            s = max(0, m.start() - 50); e = min(len(text), m.end() + 50)
            variation.append({"phrase": v, "context": text[s:e].replace("\n", " ").strip()})
    # dedupe variation by (phrase, ctx)
    seen, vu = set(), []
    for f in variation:
        k = (f["phrase"], f["context"][:40])
        if k not in seen:
            seen.add(k); vu.append(f)
    return {"slug": slug, "entries": entries, "variation_flags": vu}


def _tidy_definition(defn: str, limit: int = 300) -> str:
    """A schedule entry must read as one clean statement.

    Two faults come out of the corpus bodies: the same sentence restated verbatim
    (a source that repeats itself), and a hard character cut that ends mid-word
    ("An implied covenant to c..."). Dedupe sentences, then end on a sentence
    boundary -- never inside a word.
    """
    defn = " ".join(defn.split()).strip(" -:;")
    # dedupe repeated sentences/clauses, case- and punctuation-insensitive
    parts = re.split(r"(?<=[.;])\s+", defn)
    def _overlap(a: str, b: str) -> float:
        wa, wb = set(a.split()), set(b.split())
        return len(wa & wb) / max(1, min(len(wa), len(wb)))

    seen, keep = set(), []
    for p in parts:
        k = re.sub(r"[^a-z0-9 ]", "", p.lower()).strip()
        if not k or len(k) < 3:
            continue
        # a restatement is not a second definition: drop exact repeats, containment,
        # and clauses that are mostly the same words as one already kept
        if (k in seen
                or any(k in s2 or s2 in k for s2 in seen)
                or any(_overlap(k, s2) >= 0.8 for s2 in seen if len(k.split()) > 4)):
            continue
        seen.add(k); keep.append(p.strip())
    defn = " ".join(keep).strip()
    if len(defn) <= limit:
        return defn.rstrip(" ;,")
    # end on the last sentence boundary inside the budget; else last whole word
    window = defn[:limit]
    cut = max(window.rfind(". "), window.rfind("; "))
    if cut > 80:
        return defn[:cut + 1].rstrip(" ;,")
    cut = window.rfind(" ")
    return (defn[:cut] if cut > 80 else window).rstrip(" ;,.") + "..."


def render_schedule(d: dict) -> str:
    md = ["## SCHEDULE A — DEFINITIONS",
          "Capitalized terms have the meanings below; each is used mechanically throughout. "
          "Statutory terms are anchored to the controlling authority.", ""]
    for e in d["entries"]:
        _src_tag = "" if e.get("kind") in ("party", "instrument") else f"  [{e['source']}]"
        md.append(f"{e['n']}. **{e['term']}** — {e['definition']}{_src_tag}")
    if not d["entries"]:
        md.append("_(no weight-bearing terms detected)_")
    return "\n".join(md)


def tag_body(text: str, d: dict) -> str:
    """Leave the prose alone. Schedule A is the reading key.

    This used to write  Term ("Term")[N]  at first use. Two defects shipped from it:
      1. the parenthetical is circular -- the match pattern is the term itself, so it
         always rendered as  Conversion ("Conversion")[8],  a term defined as itself;
      2. it wrote inside quotations, altering quoted text. A filing that quotes its own
         Complaint as  Breach ("Breach")[3] of the Implied Covenant ("Implied Covenant")[5]
         has misquoted the pleading.
    Nothing downstream parses the [N] brackets, and the cross-reference a reader needs is
    already carried by Schedule A plus the superscript layer (tag_body_quantum), which
    respects _protected_spans. So the correct output is the prose unchanged.
    """
    return text


def render_short(d: dict) -> str:
    """A short top-of-document definitions block: parties + instruments only (the most-used terms).
    Used for LONG filings so the front stays focused on argument; the full schedule goes to an appendix."""
    md = ["## DEFINITIONS (key terms; full Schedule A as appendix)"]
    for e in d["entries"]:
        if e["kind"] in ("party", "instrument"):
            md.append(f"- **{e['term']}** — {e['definition']}")
    md.append("Statutory terms of art are defined in **Schedule A** (appendix), each anchored to its controlling section.")
    return "\n".join(md)


def inject_into_draft(md_text: str, slug: str = "", ground: bool = False) -> str:
    """Return the draft markdown made SELF-DEFINING, placed the way courts actually read filings:
      - SHORT filing (< ~15 pages): full numbered SCHEDULE A embedded at the top (a reading key).
      - LONG filing (>= ~15 pages): a short key-terms block at the top + the FULL Schedule A as an
        APPENDIX at the end (front stays focused on argument; a footnote points to the appendix).
    First use of each term is tagged  Term ("Term")[N] -> Schedule A entry N. Conservative: inserts the
    schedule + tags; does NOT blind-replace synonyms (party assignment is the drafter's job, rule 8).
    Initial-cap convention only (never ALL-CAPS). Safe to wrap in try/except; returns input on no terms."""
    d = build(md_text, slug, ground)
    if not d.get("entries"):
        return md_text
    tagged = tag_body(md_text, d)               # tag first, on original positions
    lines = tagged.splitlines(keepends=True)
    ins = next((i + 1 for i, l in enumerate(lines) if l.lstrip().startswith("#")), 0)  # after title/caption
    PAGE = 3000                                  # ~chars per page
    is_motion = bool(re.search(r"\bMOTION\b", md_text[:300], re.I)) and not re.search(r"\bCOMPLAINT\b", md_text[:300], re.I)
    incorp = ("\nCapitalized terms used herein and not otherwise defined have the meanings given to them "
              "in the Complaint.\n") if is_motion else ""
    if len(tagged) < 15 * PAGE:
        block = "\n" + render_schedule(d) + incorp + \
                "\n\nAll capitalized terms above are used as defined and mechanically throughout.\n\n---\n\n"
        return "".join(lines[:ins]) + block + "".join(lines[ins:])
    # long: short top block + full appendix
    top = "\n" + render_short(d) + incorp + "\n\n---\n\n"
    appendix = ("\n\n---\n\n# SCHEDULE A — DEFINITIONS (Appendix)\n\n" + render_schedule(d) +
                "\n\nAll capitalized terms are used as defined and mechanically throughout this filing.\n")
    return "".join(lines[:ins]) + top + "".join(lines[ins:]) + appendix


def review(text: str, slug: str = "", ground: bool = False, draft_path: str | None = None) -> dict:
    d = build(text, slug, ground)
    OUT.mkdir(parents=True, exist_ok=True); NOTES.mkdir(parents=True, exist_ok=True)
    sched = render_schedule(d)
    report = [f"# Definitions review — {slug or '(draft)'}",
              f"{len(d['entries'])} defined term(s); {len(d['variation_flags'])} elegant-variation flag(s) to fix.",
              "", sched, "", "## ELEGANT-VARIATION FLAGS (replace each with the defined term)"]
    if not d["variation_flags"]:
        report.append("- none")
    for f in d["variation_flags"][:40]:
        report.append(f"- \"{f['phrase']}\" → use the defined term  (…{f['context'][:80]}…)")
    out = "\n".join(report)
    (OUT / f"definitions_{slug or 'draft'}.md").write_text(out, encoding="utf-8")
    (OUT / f"SCHEDULE_A_{slug or 'draft'}.md").write_text(sched, encoding="utf-8")
    if draft_path:
        try:
            Path(draft_path).with_name(Path(draft_path).stem.replace("_DRAFT", "") + "_SCHEDULE_A.md").write_text(sched, encoding="utf-8")
        except Exception:
            pass
    # Obsidian definition nodes (one per term) for cross-filing consistency
    for e in d["entries"]:
        slugterm = re.sub(r"[^a-z0-9]+", "-", e["term"].lower()).strip("-")
        node = (f"---\nname: def-{slugterm}\ntype: definition\nsource: {e['source']}\n---\n\n"
                f"# {e['term']}\n\n{e['definition']}\n\n**Source:** {e['source']}. Use this exact term mechanically; "
                f"no synonyms/pronouns. Links: [[{slug}]].\n")
        (NOTES / f"def-{slugterm}.md").write_text(node, encoding="utf-8")
    return {"slug": slug, "defined_terms": len(d["entries"]), "variation_flags": len(d["variation_flags"]),
            "schedule_path": str(OUT / f"SCHEDULE_A_{slug or 'draft'}.md")}


# ─────────────────────────────────────────────────────────────────────────────
# QUANTUM LEGALESE layer — every operative word carries its LEGAL meaning inline, so the
# reader cannot slide to the common meaning (the Hall failure mode). A term renders as
# possession^{1} ; when TWO+ authorities corroborate the same meaning it stacks: possession^{1,2}
# (belt-and-suspenders). Each marker -> a footnote (rendered small / as endnotes / appendix),
# ordered by the AUTHORITY HIERARCHY: constitution > statute > rule > case > dictionary/maxim.
# Definitions are corpus-grounded; --ground pulls the verbatim text.
_TIER = {"constitution": 0, "statute": 1, "rule": 2, "case": 3, "dictionary": 4, "maxim": 5}

# term (lowercase) -> list of corroborating definitions: (definition, cite, tier, grounding_seed)
_TERMS_OF_ART = {
    "possession": [
        ("control or dominion over a thing; possession is ACTUAL or CONSTRUCTIVE — a custodian in control possesses",
         "Black's Law Dictionary 8th ed. (constructive possession, p.3692)", "dictionary",
         "constructive possession control or dominion over property Black's"),
        ("service of the writ shall attach all property of the defendant in the possession of the garnishee, by operation of rule",
         "Pa.R.C.P. 3111(b)", "rule", "Pa.R.C.P. 3111 writ attaches property in possession of garnishee"),
    ],
    "property": [
        ("personal property is either IN POSSESSION or IN ACTION (a chose in action) — both are property",
         "Black's Law Dictionary 8th ed.", "dictionary", "personal property in possession or in action chose"),
        ("a chose in action is an intangible personal right not reduced to possession, recoverable by suit — includes debts, stock, funds",
         "Black's Law Dictionary 8th ed. (chose in action); accord IRS IRM 5.17.3.10.5", "dictionary", "chose in action intangible right recoverable by suit debts stock"),   # lead authority Black's; IRM kept as accord (agency manual, not statute)
    ],
    "allege": [
        ("a party's formal statement of a factual matter as being true in a pleading — a pleading formality, not a jurisdictional fact",
         "Black's Law Dictionary 8th ed.", "dictionary", "allegation formal statement of fact in a pleading Black's"),
        ("no separate allegation of possession is a precondition; the served writ effects the attachment by operation of rule",
         "Pa.R.C.P. 3111(b)", "rule", "writ service effects attachment operation of rule 3111"),
    ],
    "misnomer": [
        ("a mistake in a party's name; correctable by amendment, which relates back to the date of the original — never a merits bar",
         "Black's Law Dictionary 8th ed.", "dictionary", "misnomer of a party corrected by amendment relates back"),
        ("the court may permit amendment of pleadings (including to correct a name) at any stage",
         "Pa.R.C.P. 1033", "rule", "Pa.R.C.P. 1033 amendment of pleadings correct name"),
    ],
    "claim": [
        ("an assertion of an existing right; any right to payment or to an equitable remedy, even if contingent — a cause of action is a state of facts entitling a party to relief",
         "Black's Law Dictionary 8th ed.", "dictionary", "claim assertion of existing right payment equitable remedy"),
    ],
    "custodian": [
        ("one who HOLDS bare legal title/possession for another; distinguished from the beneficial (equitable) owner — to hold as custodian is to possess",
         "Black's Law Dictionary 8th ed. (beneficial owner)", "dictionary", "custodian holds legal title beneficial owner distinguished"),
    ],
    "demurrer": [
        ("a pleading that admits, for testing the sufficiency of the complaint, ALL properly pleaded facts as true",
         "Black's Law Dictionary 8th ed.", "dictionary", "demurrer admits all well pleaded facts sufficiency"),
    ],
    # VERIFIED-CORRECT corpus additions (2026-07-06): each pulled from the corpus and CONTEXT-checked by hand
    # (auto-extraction was rejected where it returned a wrong-context sense, e.g. bank vs equitable subrogation).
    "reinvestigation": [
        ("a REASONABLE reinvestigation to determine whether the disputed information is inaccurate, and to "
         "record the current status of the disputed information or delete it — not a cursory or perfunctory review",
         "15 U.S.C. § 1681i(a)(1)(A)", "statute", "FCRA reasonable reinvestigation disputed information 1681i"),
    ],
    "garnishee": [
        ("one who is indebted to, or holds property or a chose in action of, the defendant, and whose "
         "obligation is attached and restrained by service of the writ — the garnishee's holding IS possession",
         "Am. Jur. / Black's Law Dictionary; Pa.R.C.P. 3111(b)", "dictionary", "garnishee holds property of defendant attached by writ"),
    ],
    "includes": [
        ("'includes' is NOT automatically a term of enlargement: it is expansive ONLY where the statute supplies "
         "its own expansion clause (e.g. 26 U.S.C. § 7701(c): 'includes' and 'including' 'shall not be deemed to "
         "exclude other things otherwise within the meaning of the term defined' — a clause that is TITLE-SCOPED, "
         "not a general rule of the U.S. Code); absent such a clause the enumerated items define the class and any "
         "reach beyond them must be argued under the canons (ejusdem generis / expressio unius), never assumed",
         "26 U.S.C. § 7701(c) (Title-26 expansion clause); canons of construction", "statute",   # L5-PIN/C5: cite the REAL expansion clause; not blanket
         "includes including expansion clause 7701 c shall not be deemed to exclude ejusdem generis expressio unius"),
    ],
    # PINNED corpus definitions (2026-07-06) for operative terms the definitions index resolves WRONG or MISSES
    # (verified against the corpus candidates by hand: index grabbed a neighbor entry or the wrong sense — e.g.
    # writ->a plea, service->employment, execution->OCR garble, person->pro-se). Text is the correct corpus/
    # black-letter definition with its controlling cite. These OVERRIDE the index (curated wins on name clash).
    "writ": [
        ("a precept in writing, issued in the name of the sovereign or court, commanding the person to whom it is "
         "addressed to do or refrain from doing a specified act; in execution, the writ ATTACHES the property by "
         "operation of rule upon service — no separate allegation of possession is required",
         "Black's Law Dictionary; Pa.R.C.P. 3111(b)", "dictionary", "writ precept in writing commanding act attaches property"),
    ],
    "execution": [
        ("the carrying of the judgment of a court into effect; the process (writ of execution) by which a judgment "
         "for the recovery of money or property is enforced against the judgment debtor",
         "Black's Law Dictionary; Pa.R.C.P. 3101 et seq.", "dictionary", "execution carrying judgment into effect writ enforce"),
    ],
    "attachment": [
        ("the process by which the property of a defendant is seized or restrained in the hands of a garnishee to "
         "answer a judgment; service of the writ attaches the property by operation of rule",
         "Black's Law Dictionary; Pa.R.C.P. 3111(b)", "dictionary", "attachment process property seized garnishee answer judgment"),
    ],
    "praecipe": [
        ("a formal written order or request filed with the prothonotary/clerk directing a ministerial act — the "
         "issuance of a writ or the entry of judgment; it invokes the clerk's ministerial, not the court's judicial, function",
         "Black's Law Dictionary; Pa.R.C.P. 1351", "dictionary", "praecipe written request prothonotary issue writ ministerial"),
    ],
    "satisfaction": [
        ("the discharge of an obligation by paying or performing what is due; entry of satisfaction of a judgment "
         "extinguishes the judgment and the lien it creates",
         "Black's Law Dictionary", "dictionary", "satisfaction discharge obligation paying performing extinguishes judgment"),
    ],
    "levy": [
        ("the seizure of property by an officer under a writ of execution in order to satisfy a judgment",
         "Black's Law Dictionary", "dictionary", "levy seizure of property under writ of execution satisfy judgment"),
    ],
    "lien": [
        ("a charge, security interest, or encumbrance upon property as security for the payment of a debt or "
         "performance of an obligation; a right to retain the property until the debt is satisfied",
         "Black's Law Dictionary", "dictionary", "lien charge encumbrance upon property security for debt retain"),
    ],
    "service": [
        ("the formal delivery of a writ, summons, complaint, or other legal process to a party, which gives the "
         "court jurisdiction over the person and affords the party legal notice; defective service is a jurisdictional defect",
         "Black's Law Dictionary; Pa.R.C.P. 400 et seq.", "dictionary", "service formal delivery of process jurisdiction notice"),
    ],
    "person": [
        ("a term of art that INCLUDES individuals, partnerships, associations, corporations, and other legal "
         "entities — it is not limited to a natural human being; the legal meaning is fixed by statute, not by common usage",
         "1 U.S.C. § 1; Black's Law Dictionary", "statute", "person includes individuals partnerships corporations entities"),
    ],
    "debtor": [
        ("one who owes a debt; he who may be compelled to pay a claim or demand",
         "Black's Law Dictionary", "dictionary", "debtor one who owes a debt compelled to pay claim"),
    ],
    "creditor": [
        ("a person to whom a debt is owing by another person, called the debtor",
         "Black's Law Dictionary", "dictionary", "creditor person to whom a debt is owing by the debtor"),
    ],
    "furnisher": [
        ("a person who furnishes information relating to consumers to one or more consumer reporting agencies for "
         "inclusion in a consumer report — bound by the accuracy and reinvestigation duties of the FCRA",
         "15 U.S.C. § 1681s-2", "statute", "furnisher person who furnishes information to consumer reporting agencies duties"),
    ],
    "consumer report": [
        ("any communication by a consumer reporting agency bearing on a consumer's credit worthiness, standing, "
         "capacity, character, or reputation, used or expected to be used for a permissible purpose",
         "15 U.S.C. § 1681a(d)", "statute", "consumer report communication bearing on credit worthiness permissible purpose"),
    ],
    "willful": [
        ("15 U.S.C. § 1681n imposes liability for one who 'willfully fails to comply'; the Supreme Court in "
         "Safeco construed FCRA 'willful' to reach RECKLESS DISREGARD of the Act's requirements, not only a "
         "knowing violation (the reckless-disregard reading is the holding, not the statutory text)",
         "15 U.S.C. § 1681n; Safeco Ins. Co. of America v. Burr, 551 U.S. 47", "case",   # L5-PIN: reckless-disregard gloss is Safeco (case), not statutory text
         "willful knowing reckless disregard FCRA 1681n Safeco Burr"),
    ],
    "judgment": [
        ("the final determination by a court of the rights and obligations of the parties in an action",
         "Black's Law Dictionary", "dictionary", "judgment final determination rights obligations parties action"),
    ],
}


# Match every INFLECTION of an operative term (possesses/possessed, properties, allegation, custodial)
# so the verb/plural forms are tagged too — the Hall order used "possesses", not "possession".
_TERM_PATTERNS = {
    "possession": r"\bpossess(?:es|ed|ing|ion|ions)?\b",
    "property":   r"\bpropert(?:y|ies)\b",
    "allege":     r"\balleg(?:e|es|ed|ing|ation|ations)\b",
    "misnomer":   r"\bmisnomers?\b",
    "claim":      r"\bclaim(?:s|ed|ing)?\b",
    "custodian":  r"\bcustod(?:ian|ians|y|ial)\b",
    "demurrer":   r"\bdemurr(?:er|ers|ed)\b",
    "reinvestigation": r"\breinvestigat(?:e|es|ed|ing|ion|ions)\b",
    "garnishee":  r"\bgarnish(?:ee|ees|ment|ments)\b",
    "includes":   r"\binclud(?:e|es|ed|ing)\b",
    "writ":       r"\bwrits?\b",
    "execution":  r"\bexecut(?:e|es|ed|ing|ion|ions)\b",
    "attachment": r"\battach(?:ment|ments|ed|es|ing)?\b",
    "praecipe":   r"\bpraecipes?\b",
    "satisfaction": r"\bsatisf(?:action|actions|y|ied|ies)\b",
    "levy":       r"\blev(?:y|ied|ies|ying)\b",
    "lien":       r"\bliens?\b",
    "service":    r"\bserv(?:ice|ices|ed|ing)\b",
    "person":     r"\bpersons?\b",
    "debtor":     r"\bdebtors?\b",
    "creditor":   r"\bcreditors?\b",
    # context-gated: only the FCRA noun ('furnisher'), never the ordinary verb
    # ('furnishing of a service' in UTPCPL) — cross-statute pollution guard
    "furnisher":  r"\bfurnisher(?:s)?\b",
    "consumer report": r"\bconsumer reports?\b",
    "willful":    r"\bwil?lful(?:ly|ness)?\b",
    "judgment":   r"\bjudgments?\b",
}
# CONTEXT GATES: these terms of art belong to a specific statutory scheme; they tag
# only when the document actually invokes that scheme (cite present). Prevents an
# insurance complaint from carrying FCRA/Title-26 apparatus it never uses.
_TERM_CONTEXT = {
    "furnisher": r"1681s-2|FCRA|consumer reporting|credit report",
    "includes": r"7701|Title 26|Internal Revenue|26 U\.S\.C",
    "chose in action": r"chose in action",   # only when the term itself is used
    # Article III / Lujan standing is FEDERAL doctrine; a PA state complaint
    # never carries it (state standing is prudential, not constitutional)
    # NOTE: deliberately NOT a bare "28 U.S.C." test -- 28 U.S.C. 1654 appears in the
    # signature block of every filing this user signs, in any forum. Federal standing
    # requires a FEDERAL FORUM, proven by the caption or the rules invoked.
    "Standing": (r"UNITED STATES DISTRICT COURT|U\.S\. District Court|"
                 r"Fed\. ?R\. ?Civ\. ?P|F\.R\.C\.P|"
                 r"\bUnited States Court of Appeals\b"),
}


_STATE_FORUM = re.compile(
    r"COURT OF COMMON PLEAS|Pa\.\s?R\.\s?C\.\s?P|Pa\.R\.C\.P|"
    r"COMMONWEALTH COURT|SUPERIOR COURT OF PENNSYLVANIA|MAGISTERIAL DISTRICT",
    re.I)


# Forum declared by the caller (a COURT_RULES key such as PA_CP_PHILA / EDPA / 3D_CIR).
# Set via set_forum(); consulted before any text scan. None = undeclared -> scan the text.
_FORUM_HINT = None
_STATE_COURT_ID = re.compile(r"^(?:PA_|PHILA|CCP|COMMON_PLEAS|PA\b|NJ_)", re.I)
_FORUM_SOV = "Pennsylvania"   # sovereign named in an untagged Schedule A row (set_forum)
_FED_COURT_ID = re.compile(r"^(?:EDPA|MDPA|WDPA|[0-9]D_CIR|3D|CA3|SCOTUS|USDC|FED|US_)", re.I)


def set_forum(court_id) -> None:
    """Declare the forum for every subsequent Schedule A / quantum build in this process."""
    global _FORUM_HINT, _FORUM_SOV
    c = str(court_id or "").strip()
    from pipeline import forum_profile as _fp
    _sov = _fp.sovereign_of(c)
    _FORUM_SOV = _fp.sovereign_name(_sov, "Pennsylvania")
    if not c:
        _FORUM_HINT = None
    elif _STATE_COURT_ID.match(c) or (_sov and not _FED_COURT_ID.match(c)):
        _FORUM_HINT = "state"
    elif _FED_COURT_ID.match(c):
        _FORUM_HINT = "federal"
    else:
        _FORUM_HINT = None


def _forum_is_state(text: str) -> bool:
    """True when the document is filed in a state forum.

    A state filing must not carry federal-only apparatus (Article III standing,
    Rule 12(b)(6), Twombly/Iqbal plausibility). The caller's declared forum (set_forum)
    decides; the caption / rules the document invokes are the fallback for a body that
    reaches this organ before its caption is attached.
    """
    if _FORUM_HINT == "state":
        return True
    if _FORUM_HINT == "federal":
        return False
    t = text or ""
    if re.search(r"UNITED STATES DISTRICT COURT|U\.S\. District Court", t, re.I):
        return False
    return bool(_STATE_FORUM.search(t))


# terms that are FEDERAL-FORUM ONLY -- never defined in a state-court filing
_FEDERAL_ONLY_TERMS = ("Standing",)


# Reporter/statute/rule tokens that identify a definition's authority. A dictionary or maxim
# ("Black's Law Dictionary", "maxim") carries NO matter-specific authority and is always in
# scope -- it supplies the ordinary legal sense, not another matter's governing law.
_AUTH_TOKEN = re.compile(
    r"\d+\s+U\.S\.C\.?\s*§*\s*[\w().-]+"          # 15 U.S.C. 1681n
    r"|\d+\s+C\.F\.R\.\s*§*\s*[\w().-]+"
    r"|\d+\s+Pa\.C\.S\.\s*§*\s*[\w().-]+"
    r"|\d+\s+P\.S\.\s*§*\s*[\w().-]+"
    r"|Pa\.\s*R\.\s*C\.\s*P\.\s*(?:No\.\s*)?[\d.]+(?:\([a-z0-9]+\))?"   # Pa.R.C.P. 3111(b)
    r"|Fed\.\s*R\.\s*(?:Civ|App|Evid)\.\s*P\.\s*[\w().]+"
    r"|\d+\s+A\.\d?d\s+\d+|\d+\s+F\.\s*(?:Supp\.?\s*\d?d|\d?d)\s+\d+|\d+\s+U\.S\.\s+\d+",
    re.I)

# Running-head / page-number debris from OCR'd dictionary scans: "to appear in 948: PROCESS - court"
_OCR_DEBRIS = re.compile(r"\b\d{2,4}:\s*[A-Z]{3,}\b")


def _ocr_debris(defn: str) -> bool:
    return bool(_OCR_DEBRIS.search(str(defn or "")))


def _norm_auth(s: str) -> str:
    """Compare authorities on digits+letters only, so 'Pa.R.C.P. 3111(b)' == 'Pa. R.C.P. 3111(b)'."""
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def _matter_authorities(text: str) -> set:
    """Every authority THIS matter invokes: the draft body plus the case's authority bank.

    The bank is the m0-verified authority set for the case, so a definition may legitimately
    cite an authority argued in the bank even when the body's short-cite form differs.
    """
    pool = str(text or "")
    try:
        import os, glob
        root = os.environ.get("BEEZLE_HOME") or str(_eroot())
        for f in glob.glob(os.path.join(root, "output/audit/*_rebuttal_bank.json")):
            try:
                with open(f, encoding="utf-8") as fh:
                    pool += fh.read()
            except OSError:
                continue
    except Exception:
        pass
    return {_norm_auth(m.group(0)) for m in _AUTH_TOKEN.finditer(pool)}


def _authority_in_matter(cite: str, text: str) -> bool:
    """True when the definition's authority is one this matter actually invokes.

    A cite with no statute/rule/reporter token (dictionary, maxim, 'record') is always in
    scope. A cite that names one must have it present in the body or the authority bank.
    """
    toks = [m.group(0) for m in _AUTH_TOKEN.finditer(str(cite or ""))]
    if not toks:
        return True
    pool = _matter_authorities(text)
    return any(_norm_auth(t) in pool for t in toks)


def _term_re(t: str):
    return re.compile(_TERM_PATTERNS.get(t, r"\b" + re.escape(t) + r"\b"), re.I)


# Operative litigation terms of art the DEFINITIONS INDEX resolves cleanly (verified 2026-07-06 by dumping the
# index over the full vocab). Their definitions are pulled DYNAMICALLY from the corpus index at build time, so
# the catalog is not a hand-typed list — it is the corpus speaking. Terms the index gets wrong/misses are pinned
# above (curated wins). A term here is tagged ONLY if the index returns a gate-passed definition; otherwise skipped.
_INDEX_OPERATIVE = {
    "chose in action", "allegation", "verification", "amendment", "assignment", "assignee", "subrogation",
    "novation", "accord and satisfaction", "tender", "consideration", "indebtedness", "discharge", "waiver",
    "laches", "void", "voidable", "damages", "remedy", "stare decisis", "supersedeas", "stay", "garnishment",
    "holder", "sanction", "process", "consideration",
}
_INDEX_CACHE: dict = {}

def _index_def(term: str):
    """Resolve one operative term to a corpus definition via the definitions index (gate-passed).
    Returns a catalog entry [(defn, cite, tier, seed)] or None. Cached; never raises."""
    if term in _INDEX_CACHE:
        return _INDEX_CACHE[term]
    entry = None
    try:
        from pipeline import definitions_index
        hits = definitions_index.lookup(term, limit=1)
        if hits:
            e = hits[0]
            tier = "statute" if e["kind"] in ("statute", "structured", "rule") else "dictionary"
            # repair OCR line-break hyphenation ('per- sonal' -> 'personal'); leave real compounds ('co-partner')
            defn = re.sub(r"([a-z])-\s+([a-z])", r"\1\2", e["definition"]).strip()
            entry = [(defn.rstrip(" .,;-") + ".", e["cite"], tier, term)]
    except Exception:
        entry = None
    _INDEX_CACHE[term] = entry
    return entry


# DYNAMIC term source: the corpus-grounded definitions m0 has LEARNED (obsidian_sync/definitions_learned/,
# growing via the nightly definitions cron). This makes the quantum catalog NOT a fixed 6-word list — it
# expands as m0 learns — while keeping every definition GROUNDED (each carries a Citation), so nothing
# unverified is injected into a filing. Only clean 1-3-word core terms are tagged (avoids mis-tagging).
_LEARNED_CACHE = None
_COMMON_SKIP = {"request", "information", "error", "loan", "account", "notice", "party", "court", "action",
                "statute", "rule", "law", "right", "claim", "person", "borrower", "servicer", "response"}

def _load_learned_terms():
    global _LEARNED_CACHE
    if _LEARNED_CACHE is not None:
        return _LEARNED_CACHE
    out = {}
    try:
        from pathlib import Path
        d = Path(__file__).resolve().parents[1] / "obsidian_sync" / "definitions_learned"
        for f in d.glob("*.md"):
            try:
                txt = f.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            mt = re.search(r"(?ms)^##\s*Term\s*\n(.+?)(?=\n##|\Z)", txt)
            md = re.search(r"(?ms)^##\s*Definition\s*\n(.+?)(?=\n##|\Z)", txt)
            mc = re.search(r"\*\*Citation:\*\*\s*(.+)", txt)
            if not (mt and md):
                continue
            # core term = the primary noun phrase before ' and '/'—'/'(' ; keep 1-3 words
            core = re.split(r"\s+and\s+|\s+—\s+|\s*\(", mt.group(1).strip(), 1)[0].strip().lower()
            core = re.sub(r"[^a-z0-9 §.-]", "", core).strip()
            if not core or not (1 <= len(core.split()) <= 3) or core in _COMMON_SKIP:
                continue
            defn = " ".join(md.group(1).split())
            defn = re.split(r"(?<=[.;])\s", defn, 1)[0][:240]
            # reject a garbage learned def that is just a citation / too short to be a definition
            if len(defn) < 30 or re.match(r"^(Pa\.|U\.?S\.?C?\.?|Fed\.|§|\d+\s|see\b)", defn, re.I):
                continue
            cite = (mc.group(1).strip()[:80] if mc else f.stem)
            tier = "statute" if "statute" in txt.lower()[:200] else "dictionary"
            out.setdefault(core, [])
            if not any(cite == c for _, c, _, _ in out[core]):
                out[core].append((defn, cite, tier, ""))
    except Exception:
        pass
    _LEARNED_CACHE = out
    return out


def build_quantum(text: str, ground: bool = False) -> dict:
    """Find operative terms of art present in the text and assign footnote numbers.
    Returns {markers: {term: [n,...]}, notes: [{n, term, definition, cite, tier}], } — notes ordered
    by authority hierarchy then term. Multiple corroborating cites for one term => stacked markers."""
    # terms in READING ORDER (footnote numbers track the page); each term's defs kept CONSECUTIVE
    # so the stacked superscript reads possession^{3,4}; within a term, strongest authority first.
    # DYNAMIC catalog: curated terms of art (legally-reviewed) + the terms m0 has LEARNED (grounded,
    # growing). Curated definitions win on a name clash. Not a fixed 6-word list.
    catalog = dict(_TERMS_OF_ART)
    for term, defs in _load_learned_terms().items():
        if term not in catalog:
            catalog[term] = defs
    def _first(t):
        m = _term_re(t).search(text); return m.start() if m else 10 ** 9
    # candidate terms = curated + learned + the index-operative vocabulary; resolve the index-only ones (that
    # actually appear) to a corpus definition. No clean corpus def -> the term is NOT tagged (airtight: never
    # tag a term we can't define from the corpus). Curated pins already loaded above win on any name clash.
    candidates = set(catalog) | _INDEX_OPERATIVE
    present = sorted([t for t in candidates if _term_re(t).search(text)], key=_first)
    # CONTEXT GATES: statute-scheme terms only tag when the document invokes that scheme
    present = [t for t in present
               if (t not in _TERM_CONTEXT or re.search(_TERM_CONTEXT[t], text, re.I))
               and not (_forum_is_state(text) and t in _FEDERAL_ONLY_TERMS)]
    for t in list(present):
        if t not in catalog:
            d = _index_def(t)
            if d:
                catalog[t] = d
            else:
                present.remove(t)
    notes, markers, n = [], {}, 0
    for t in present:
        for (defn, cite, tier, seed) in sorted(catalog[t], key=lambda d: _TIER.get(d[2], 9)):
            # SCOPE GATE (class-level): a definition is substantive -- it tells the court which
            # legal sense the filing uses. A definition whose AUTHORITY this matter never invokes
            # imports another matter's law. Measured: an insurance brief defined "willful" via
            # FCRA 15 U.S.C. 1681n/Safeco and "possession"/"allege" via garnishment Pa.R.C.P.
            # 3111(b) -- none pleaded here. Per-term context gates cannot scale to 573 corpus
            # terms; test the AUTHORITY against the matter instead.
            if not _authority_in_matter(cite, text):
                continue
            if _ocr_debris(defn):      # page-number/running-head debris, e.g. "948: PROCESS"
                continue
            n += 1
            verbatim = _ground(seed) if ground else ""
            notes.append({"n": n, "term": t, "definition": (verbatim or defn), "cite": cite, "tier": tier})
            markers.setdefault(t, []).append(n)
    return {"markers": markers, "notes": notes}


def tag_body_quantum(text: str, q: dict, sup: str = "unicode") -> str:
    """Tag operative terms with STACKED superscript markers (possession^{1,2}) — the FIRST use in EACH
    SECTION, so every argument section that uses a loaded term carries its definition marker and the
    reader cannot presume the common meaning anywhere in the brief (not just once at the top). A term
    already carrying a marker is not re-tagged (idempotent). sup='unicode'->¹² | 'md'->^1,2^ | 'html'-><sup>."""
    _U = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")
    def mark(nums):
        s = ",".join(str(n) for n in nums)
        if sup == "unicode":
            return s.translate(_U)
        if sup == "html":
            return f"<sup>{s}</sup>"
        return f"^{s}^"
    # split into sections on markdown headers (## / ###) so "first use" resets per section
    segs = re.split(r"(?m)(^#{1,6}\s.*$)", text)
    _sup_chars = "⁰¹²³⁴⁵⁶⁷⁸⁹"
    for i, seg in enumerate(segs):
        if re.match(r"^#{1,6}\s", seg or ""):
            continue  # leave headers untouched
        s = seg
        for term, nums in q["markers"].items():
            _spans = _protected_spans(s)
            for m in _term_re(term).finditer(s):
                # skip if this occurrence already carries a superscript marker right after it
                tail = s[m.end():m.end() + 2]
                if tail and tail[0] in _sup_chars:
                    break
                # never mark inside a citation, quoted statute, footnote line or heading
                if _in_spans(m.start(), _spans) or _in_spans(m.end() - 1, _spans):
                    continue
                # the marker goes after a WHOLE word, never inside one
                if m.end() < len(s) and (s[m.end()].isalnum() or s[m.end()] in "'\u2019-"):
                    continue
                s = s[:m.end()] + mark(nums) + s[m.end():]
                break  # first use in THIS section only
        segs[i] = s
    return "".join(segs)


def _protected_spans(text: str) -> list:
    """Character ranges a definition marker must never enter.

    A superscript spliced into a citation ("Terletsky v. Prudential Property(4,5) &
    Casualty Ins. Co.") corrupts the one thing a court checks first. Protect:
      - footnote DEFINITION lines ("[^3]: ...")
      - any case citation: "X v. Y, 123 A.2d 456 (Pa. 2014)"
      - reporter/statute cites standing alone
      - quoted statutory text (the enacted words must be reproduced exactly)
      - markdown headings and exhibit tags
    """
    spans = []
    for pat in (
        r"(?m)^\[\^\d+\]:.*$",                                  # footnote definitions
        r"[A-Z][\w.'\u2019-]*(?:\s+[A-Z][\w.'\u2019-]*)*\s+v\.\s+[^,;\n]{2,70}"
        r"(?:,\s*\d+\s+[A-Z][\w.]*\s?\d*d?\s+\d+)?(?:\s*\([^)]{2,30}\))?",  # case cites
        r"\b\d{1,4}\s+[A-Z][\w.]*\.?\s?\d?[a-z]?\.?\d*d?\s+\d{1,4}\b",      # reporters
        r"\b\d+\s+(?:Pa\.C\.S|P\.S|U\.S\.C|Pa\.R\.C\.P)[^\s]*\s*(?:\u00a7+\s*[\d.\-()]+)?",
        r"\u201c[^\u201d]{15,}\u201d",                            # quoted enacted text
        r'"[^"]{15,}"',
        r"(?m)^#{1,6}\s.*$",                                    # markdown headings
        r"(?m)^\*\*\d+\..*?\*\*",                                # point headings
        r"(?m)^\d+\.\s+\*\*[^*]+\*\*",                           # schedule A entries
        r"\(E\d+\)|\(Exhibit [A-Z0-9]+\)",                      # exhibit tags
    ):
        for m in re.finditer(pat, text or ""):
            spans.append((m.start(), m.end()))
    spans.sort()
    merged = []
    for a, b in spans:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def _in_spans(pos: int, spans: list) -> bool:
    for a, b in spans:
        if a <= pos < b:
            return True
        if a > pos:
            break
    return False


def render_footnotes(q: dict) -> str:
    """Small-font footnote block (rendered as endnotes / appendix; a docx pass can lower the font).
    Ordered by the authority hierarchy so the strongest source reads first."""
    if not q["notes"]:
        return ""
    tier_label = {"constitution": "Const.", "statute": "Statute", "rule": "Rule", "case": "Case",
                  "dictionary": "Dict.", "maxim": "Maxim"}
    # The heading "DEFINITIONS & AUTHORITIES" is the sentinel the docx formatter renders in small font
    # (real footnote-size text). No HTML — that renders literally in .docx.
    md = ["", "DEFINITIONS & AUTHORITIES — the superscripted terms above are used in their controlling "
          "legal sense (authority hierarchy: Constitution > statute > rule > case > dictionary/maxim):", ""]
    for e in q["notes"]:
        md.append(f"{e['n']}. {e['term']} [{tier_label.get(e['tier'], e['tier'])}] — {e['definition']} — {e['cite'].rstrip('.')}.")
    return "\n".join(md)


def inject_quantum(md_text: str, slug: str = "", ground: bool = False, sup: str = "unicode") -> str:
    """Make a draft self-defining the QUANTUM way: tag every operative term of art inline with stacked
    superscripts and append the hierarchy-ordered footnote block. Safe: returns input if no terms.
    Composes with inject_into_draft (Schedule A) — run Schedule A first, then this for the term-of-art layer."""
    q = build_quantum(md_text, ground)
    if not q["notes"]:
        return md_text
    tagged = tag_body_quantum(md_text, q, sup=sup)
    return tagged + "\n\n---\n" + render_footnotes(q)


if __name__ == "__main__":
    if "--quantum" in sys.argv:
        fp = sys.argv[sys.argv.index("--file") + 1] if "--file" in sys.argv else None
        slug = sys.argv[sys.argv.index("--slug") + 1] if "--slug" in sys.argv else ""
        ground = "--ground" in sys.argv
        text = Path(fp).read_text(encoding="utf-8", errors="ignore") if fp else (
            "Plaintiff does not allege that Example Trust Co possesses any property of the defendants; "
            "the court found a misnomer and that the complaint fails to state a claim.")
        print(inject_quantum(text, slug=slug, ground=ground, sup="unicode"))
        sys.exit(0)
    ground = "--ground" in sys.argv
    fp = sys.argv[sys.argv.index("--file") + 1] if "--file" in sys.argv else None
    slug = sys.argv[sys.argv.index("--slug") + 1] if "--slug" in sys.argv else ""
    text = Path(fp).read_text(encoding="utf-8", errors="ignore") if fp else (
        'Defendant Wells Fargo Bank failed to respond to the Qualified Written Request. The bank is a '
        'Debt Collector. Plaintiff sent the QWR about the Loan; the servicer ignored it. The lender is also a Furnisher.')
    r = review(text, slug=slug, ground=ground, draft_path=fp)
    print(f"definitions: {r['defined_terms']} defined term(s); {r['variation_flags']} variation flag(s)")
    print(f"  -> {r['schedule_path']}")
