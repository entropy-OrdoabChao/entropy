#!/usr/bin/env python3
"""m0_answer_or_admit — the ANSWER-OR-ADMIT layer (agnostic, one flow).

USER DOCTRINE (2026-09-02): "are we rebutting every point are we adding an
affidavit... how are we going to address when they purposely dodge the point
to take away our victory?" — the dodge must be MECHANICAL, not narrative.

THREE FUNCTIONS, all deterministic (no LLM — the facts come from the case's
own exhibit register + INITIAL CLAIM; the LLM already did comprehension
upstream in the m0 chain):

  affidavit(slug)    -> numbered § 1746 AFFIDAVIT OF FACTS assembled from the
                        case's exhibit register + MATTER_BRIEF identity. Every
                        averment numbered, every averment tied to its exhibit.
                        Unrebutted affidavit = the record's strongest object.

  demand_table(slug) -> numbered DEMAND TABLE: each averment requires
                        admission, specific denial with documentary support,
                        or production — with the instrument's own stated term
                        that non-response/evasion to a numbered item is an
                        admission of that item (1029(b) logic, exported).

  reply_diff(slug, reply_text)
                     -> scores their reply against the numbered record:
                        ANSWERED / DODGED (responds but not to the point —
                        no admit/deny/produce on the item) / SILENT.
                        The dodge map IS the next instrument's lead exhibit.

Wired into m1 as assembly sections (complaint + replies use the SAME organ)
and into the mail-watch triage (reply arrives -> diff runs -> scored ledger).
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = _eroot()
VAULT = ROOT / "obsidian_sync"

_SIG_TAIL = ("Executed on ____________.\n\n"
             "_________________________________\n"
             "Filer Babah Filer, sui juris\n"
             "1 Pa.C.S. § 1308; 28 U.S.C. § 1654")
_OATH_FED = ("I declare under penalty of perjury under the laws of the United States of "
             "America that the foregoing is true and correct. 28 U.S.C. § 1746.")
_OATH_PA = ("I verify that the statements made in the foregoing affidavit are true and "
            "correct upon my personal knowledge. I understand that false statements herein "
            "are made subject to the penalties of 18 Pa.C.S. § 4904 relating to unsworn "
            "falsification to authorities. Pa.R.C.P. 1024.")


def oath_for(court: str | None) -> str:
    """The oath must match the forum: PA state courts verify under Pa.R.C.P. 1024 / 18 Pa.C.S.
    § 4904; the federal declaration statute is 28 U.S.C. § 1746 and its wording differs. Same
    rule the complaint drafter and companion forms already apply -- one source, not three."""
    c = (court or "").upper()
    is_pa_state = c.startswith("PA_") and "EDPA" not in c and "MDPA" not in c and "WDPA" not in c
    return (_OATH_PA if is_pa_state else _OATH_FED) + "\n\n" + _SIG_TAIL


SIG = oath_for(None)   # legacy default (federal) for callers that pass no court


def _register(slug: str) -> list[dict]:
    """The case's own exhibit register (auto-maintained by m1) — the fact source."""
    cf = (VAULT / "cases" / slug / "CASE_FACTS.md").read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"## EXHIBIT REGISTER.*?(?=^## |\Z)", cf, re.M | re.S)
    rows = []
    if m:
        for ln in m.group(0).splitlines():
            cells = [c.strip() for c in ln.split("|")]
            if len(cells) >= 5 and cells[1] not in ("Exhibit", "---", ""):
                rows.append({"tag": cells[1], "description": cells[2], "source": cells[3]})
    return rows


def _identity(slug: str) -> str:
    p = VAULT / "cases" / slug / "MATTER_BRIEF.md"
    if not p.exists():
        return ""
    t = p.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"## 1\. IDENTITY.*?(?=^## \d)", t, re.M | re.S)
    return m.group(0) if m else ""


def _stub_averments(slug: str, stub_path: str | None = None) -> list[tuple[str, str]]:
    """(averment, exhibit_tag) pairs from the newest stub's evidence blocks —
    the SAME deterministic source m1 weaves from (one flow, no re-derivation)."""
    base = VAULT / "cases" / slug
    stub = Path(stub_path) if stub_path else None
    if stub is None:
        cands = sorted(base.glob("drafts/**/10_complaint.md")) + \
                sorted(base.glob("drafts/**/*stub*.md"))
        stub = cands[-1] if cands else None
    if not stub or not stub.exists():
        return []
    t = stub.read_text(encoding="utf-8", errors="ignore")
    out = []
    # real stub shape: '### E1 — <title> _(weight)_' then '- **Element:**' /
    # '- **Source:**' / '- **Quote:**' with '> ' blockquote lines
    for m in re.finditer(
            r"^### (?P<tag>E\d+) — (?P<title>[^\n_]+).*?\n"
            r"(?P<body>.*?)(?=^### |\Z)", t, re.M | re.S):
        body = m.group("body")
        qm = re.search(r"\*\*Quote:\*\*\s*\n(?P<q>(?:\s*>.*\n?)+)", body)
        el = re.search(r"\*\*Element:\*\*\s*(?P<e>[^\n]+)", body)
        src = re.search(r"\*\*Source:\*\*\s*`?(?P<s>[^`\n]+)", body)
        if not qm:
            continue
        quote = re.sub(r"\s*\n\s*>?\s*", " ", qm.group("q")).strip().strip(">").strip()
        aver = (f"{m.group('title').strip()}. The document states: {quote[:340]}"
                + (f" (source: {src.group('s').strip()})" if src else ""))
        out.append((aver, m.group("tag"), (el.group("e").strip() if el else "")))
    return out




# ---------------------------------------------------------------- predicate + conflict
# Two averments an evidence-block walk can never produce on its own, and both are
# structural — they apply to any case, learned from the case's own record.

_PREDICATE_PATTERNS = (
    # (what the fact IS, regex over the record, how to say it)
    ("sale/disposition of the insured or disputed property",
     r"(?:sold|sale of|disposed of|transferred)\s+(?:the|his|her|my|plaintiff['’]?s?)?\s*"
     r"(?:insured\s+)?(?:vehicle|property|item|goods)?[^.\n]{0,80}?"
     r"\b(?P<date>(?:January|February|March|April|May|June|July|August|September|October|"
     r"November|December)\s+\d{1,2},\s+\d{4}|\d{2}/\d{2}/\d{4})"),
)


def _predicate_averments(slug: str) -> list[str]:
    """The FOUNDATIONAL fact the whole claim rests on, stated on its own.

    The evidence-block walk yields only what a DOCUMENT says. The predicate event —
    the sale, the payoff, the termination — is the affiant's own act and often appears
    in the record only inside another document's narrative. Unstated, every later
    averment hangs on an event never sworn to.

    The DATE is resolved through the same role-learning organ m0's date-role guard uses
    (pipeline.case_facts_refresh), so the predicate cannot pick up a neighbouring date
    that belongs to a different event — the proof-delivery date instead of the sale.
    """
    cf = VAULT / "cases" / slug / "CASE_FACTS.md"
    if not cf.exists():
        return []
    t = cf.read_text(encoding="utf-8", errors="ignore")
    try:
        from pipeline.case_facts_refresh import _date_roles_from_record, _dominant_roles
    except Exception:
        return []
    roles = _date_roles_from_record(t)
    # the date the record itself assigns the disposition role
    dispo = [d for d, c in roles.items() if "dispose" in _dominant_roles(c)]
    if not dispo:
        return []
    d_iso = sorted(dispo)[0]
    y, mo, dy = d_iso.split("-")
    us = f"{mo}/{dy}/{y}"
    months = ("January February March April May June July August September October "
              "November December").split()
    long_form = f"{months[int(mo) - 1]} {int(dy)}, {y}"
    # quote the record's own sentence for that date
    ctxs = [" ".join(ln.split()) for ln in t.splitlines()
            if (us in ln or long_form in ln) and re.search(r"sold|sale", ln, re.I)]
    ctx = ctxs[0][:260] if ctxs else ""
    return [f"I sold the insured vehicle on {long_form}. This is my own act, of which I "
            f"have direct personal knowledge. From that date forward I had no insurable "
            f"interest in the vehicle."
            + (f" The record reflects the sale as follows: {ctx}" if ctx else "")]


def _conflict_averments(slug: str, avers: list) -> list[str]:
    """Bind two record statements that cannot both be true.

    A contradiction sitting in two separate averments is a contradiction the reader
    must assemble. Stated as its own averment it becomes a single proposition the
    opponent must deny specifically under Pa.R.C.P. 1029(b) — and denying it means
    denying one of their own documents.
    """
    import itertools
    # learn each averment's date + whether it ASSERTS receipt or DENIES it
    _D = re.compile(r"\b(?:\d{2}/\d{2}/\d{4}|(?:January|February|March|April|May|June|July|"
                    r"August|September|October|November|December)\s+\d{1,2},\s+\d{4})\b")
    AFFIRM = re.compile(r"\b(?:we(?:'ve| have)? received|received your|thanks for sending|"
                        r"you're all set|confirm\w*|acknowledg\w+)\b", re.I)
    DENY = re.compile(r"\b(?:neither|does not provide|did not provide|no (?:date|proof|"
                      r"documentation|record)|never received|not received|was not a .{0,20}error|"
                      r"fail\w+ to provide)\b", re.I)
    tagged = []
    for a, tag, _el in avers:
        # the date that belongs to the ASSERTION, not the first date in the averment:
        # search from the receipt/denial phrase forward, then fall back to the first.
        mk = AFFIRM.search(a) or DENY.search(a)
        if not mk:
            continue
        kind = "affirm" if AFFIRM.search(a) else "deny"
        # the date that belongs to the ASSERTION. Many evidence blocks carry the date
        # ONLY in the source filename (2026-02-16_gmail_...), so fall back to it — an
        # averment is not disqualified just because its quote omits the date.
        d = _D.search(a, mk.end()) or _D.search(a)
        dstr = d.group(0) if d and abs(d.start() - mk.end()) <= 220 else ""
        if not dstr:
            # NOTE: no trailing \b — source filenames run 2026-02-16_gmail_... and "_"
            # is a word character, so \b never matches there.
            fn = re.search(r"\b(20\d{2})[-_](\d{2})[-_](\d{2})(?!\d)", a)
            if fn:
                dstr = f"{fn.group(2)}/{fn.group(3)}/{fn.group(1)}"
        if not dstr:
            continue
        tagged.append((kind, dstr, tag, a))
    # STRONGEST PAIR, not the first pair: an averment that merely lists confirmations
    # also matches "confirm", so rank by how squarely the text asserts/denies RECEIPT.
    def _score(kind, txt):
        t = txt.lower()
        sc = 0
        if kind == "affirm":
            for ph, w in (("you're all set", 6), ("we've received your", 5),
                          ("received your document", 5), ("thanks for sending", 4),
                          ("if we don't reach out", 4)):
                if ph in t:
                    sc += w
        else:
            for ph, w in (("neither email provides", 6), ("does not provide", 4),
                          ("no date", 4), ("not a progressive error", 3),
                          ("never received", 5)):
                if ph in t:
                    sc += w
        return sc

    affirms = sorted([x for x in tagged if x[0] == "affirm"],
                     key=lambda x: -_score("affirm", x[3]))
    denies = sorted([x for x in tagged if x[0] == "deny"],
                    key=lambda x: -_score("deny", x[3]))
    if not affirms or not denies:
        return []
    _, d_aff, t_aff, a_aff = affirms[0]
    _, d_den, t_den, a_den = denies[0]
    if _score("affirm", a_aff) == 0 or _score("deny", a_den) == 0 or t_aff == t_den:
        return []
    return [
        f"Defendant's own documents state two things that cannot both be true. On "
        f"{d_aff} Defendant confirmed in writing that it had received my documentation "
        f"and that no further action was required of me (Exhibit {t_aff}). On {d_den} "
        f"Defendant told the Pennsylvania Insurance Department the opposite (Exhibit "
        f"{t_den}). Exhibit {t_aff} and Exhibit {t_den} cannot both be accurate. I have "
        f"personal knowledge that I received the communication described in Exhibit "
        f"{t_aff} and that I received no request for further information after it."]


def _status_averments() -> list[str]:
    """Declared-status averments for the affidavit, built FROM the authority spine.

    These go in the AFFIDAVIT, not a separate notice: an affidavit puts the opponent
    under Pa.R.C.P. 1029(b) — deny specifically or it stands admitted. A notice carries
    no such consequence. Each averment states what the authority says and cites it, so
    the affiant swears to the DECLARATION of status, not to a legal conclusion about
    what the authority means.

    Text is pulled from pipeline.m0_authority_spine (corpus-verbatim, already gated),
    never composed here — one organ, one source of authority.
    """
    try:
        from pipeline.m0_authority_spine import SPINE
    except Exception:
        return []
    want = ("the_constitution_of_pennsylvania",
            "amjur2d_63c_public_officers",
            "amjur2d_16_constitutions_operation_effect")
    picked, seen = [], set()
    for pat, entries in SPINE.items():
        low = pat.lower()
        if "public" not in low or "office" not in low:
            continue
        for e in entries:
            src = e.get("source", "")
            if src in want and src not in seen and e.get("plead"):
                seen.add(src)
                picked.append((want.index(src), e))
    if not picked:
        return []
    picked.sort(key=lambda x: x[0])
    avers = []
    for _, e in picked:
        avers.append(
            f"I am informed and verily believe, and upon that basis declare, that the "
            f"following is the law of this Commonwealth: {e['plead'].strip()}")
    # the declaration itself — a statement of the affiant's own status, which is what
    # an affiant CAN swear to, placed after the authorities that establish it.
    avers.append(
        "I am one of the people of this Commonwealth for whose peace, safety and "
        "happiness government is instituted, and I hereby declare my status as a "
        "beneficiary of the public trust described in the foregoing authorities. I "
        "claim that status expressly and do not waive it.")
    avers.append(
        "I do not assert that any officer owes a duty untethered to law. I assert that "
        "the duties already imposed by enacted law in this matter are held in trust and "
        "must be performed: the duty of this Court under Pa.R.C.P. 1028 in disposing of "
        "preliminary objections, and the duty of Defendant under 42 Pa.C.S. § 8371 and "
        "the certificate of authority under which it transacts insurance in this "
        "Commonwealth.")
    return avers


def affidavit(slug: str, stub_path: str | None = None, court: str | None = None) -> str:
    reg = {r["tag"]: r for r in _register(slug)}
    avers = _stub_averments(slug, stub_path)
    ts = datetime.now().strftime("%Y-%m-%d")
    lines = ["## AFFIDAVIT OF FACTS", "",
             "I, Filer Babah Filer, being competent to testify and having "
             "personal knowledge of the facts stated, declare:", ""]
    n = 0
    # PREDICATE — the affiant's own act that the entire claim rests on, sworn first.
    for pa in _predicate_averments(slug):
        n += 1
        lines.append(f"{n}. {pa}")
        lines.append("")
    for aver, tag, el in avers:
        n += 1
        desc = reg.get(tag, {}).get("description", "")
        lines.append(f"{n}. {aver} (Exhibit {tag}{': ' + desc if desc else ''}.)")
        lines.append("")
    # CONFLICT — bind the two statements that cannot both be true into ONE averment.
    for ca in _conflict_averments(slug, avers):
        n += 1
        lines.append(f"{n}. {ca}")
        lines.append("")
    # DECLARED STATUS — sworn in the affidavit so Pa.R.C.P. 1029(b) applies to it.
    for sa in _status_averments():
        n += 1
        lines.append(f"{n}. {sa}")
        lines.append("")
    if n == 0:
        lines.append("(no evidence blocks found — affidavit not assembled; FIX THE INPUT)")
    lines += ["", oath_for(court)]
    return "\n".join(lines)


def demand_table(slug: str, stub_path: str | None = None, respond_days: int = 30) -> str:
    avers = _stub_averments(slug, stub_path)
    lines = ["## DEMAND FOR SPECIFIC RESPONSE", "",
             f"Each numbered item below requires, within {respond_days} days, ONE of: "
             "(a) an ADMISSION; (b) a SPECIFIC DENIAL supported by SUBSTANTIAL "
             "EVIDENCE — such relevant evidence as a reasonable mind might accept "
             "as adequate to support the denial — identifying the documents relied "
             "on; or (c) PRODUCTION of the records that answer it. "
             "A general denial, a denial unsupported by evidence, a response not "
             "directed to the numbered item, or silence as to any item will be "
             "treated on the record as an admission of that item. "
             "(Cf. Pa.R.C.P. 1029(b).)", "",
             "Any ORDER, DETERMINATION, or COMMENT relied upon in response must "
             "likewise identify the substantial evidence supporting it; an order "
             "or determination unsupported by substantial evidence on the record "
             "is subject to being set aside (cf. 5 U.S.C. § 706(2)(E)).", "",
             "Each response must address the SUBSTANTIVE FACTS presented — the "
             "specific documents, dates, amounts, and instruments of record. A "
             "response addressing characterizations while leaving the underlying "
             "facts unengaged is a non-response; each fact not specifically and "
             "substantively denied with supporting evidence stands ADMITTED "
             "(cf. Fed. R. Civ. P. 8(b)(6); Pa.R.C.P. 1029(b)).", "",
             "Each response, and any order or determination relied upon, must "
             "IDENTIFY THE ENACTED AUTHORITY under which it acts — the statute "
             "(with its Public Law origin where codification is disputed), rule, "
             "or canon authorizing the specific act taken. Public officers "
             "exercise only delegated authority; an act whose authorizing "
             "enactment cannot be named carries no presumption of validity "
             "(cf. 5 U.S.C. § 706(2)(C); Kelly v. R.R. Ret. Bd., 625 F.2d 486 "
             "(3d Cir. 1980)).", "",
             "WAIVER SYMMETRY: waiver is an intentional relinquishment or "
             "abandonment of a known right or privilege. Johnson v. Zerbst, 304 "
             "U.S. 458, 464 (1938). Each defense, objection, and denial not "
             "raised in response to this instrument within the response period "
             "is WAIVED under the same standard; no waiver by the undersigned "
             "may be presumed from silence, ambiguity, or participation under "
             "protest — waiver must be proven as an intentional relinquishment "
             "of a known right by the party asserting it. All rights reserved "
             "(1 Pa.C.S. § 1308).", ""]
    for i, (aver, tag, el) in enumerate(avers, 1):
        lines.append(f"{i}. Admit, deny with specificity, or produce the records "
                     f"concerning: {aver} (Exhibit {tag}).")
        lines.append("")
    return "\n".join(lines)


_DODGE_TOKENS = re.compile(r"(?i)\b(admit|admits|admitted|deny|denies|denied|produce|enclosed|attached)\b")


def reply_diff(slug: str, reply_text: str, stub_path: str | None = None) -> dict:
    """Score their reply against the numbered record. Deterministic:
    ANSWERED = the item's number (or its exhibit tag / a distinctive 5+char
    token from the averment) appears near an admit/deny/produce verb.
    DODGED  = item referenced but no admit/deny/produce verb near it.
    SILENT  = item never referenced."""
    avers = _stub_averments(slug, stub_path)
    low = reply_text.lower()
    result = {"answered": [], "dodged": [], "silent": []}
    for i, (aver, tag, el) in enumerate(avers, 1):
        keys = [f"{i}.", f"item {i}", f"paragraph {i}", tag.lower()]
        toks = [w for w in re.findall(r"[a-z]{6,}", aver.lower())
                if w not in ("defendant", "plaintiff", "records", "account")][:4]
        keys += toks
        pos = [low.find(k) for k in keys if low.find(k) >= 0]
        if not pos:
            result["silent"].append(i)
            continue
        window = low[max(0, min(pos) - 200): min(pos) + 400]
        (result["answered"] if _DODGE_TOKENS.search(window) else result["dodged"]).append(i)
    result["verdict"] = (
        f"{len(result['answered'])} answered / {len(result['dodged'])} DODGED / "
        f"{len(result['silent'])} SILENT of {len(avers)} numbered items — dodged and "
        "silent items stand admitted per the instrument's stated terms.")
    return result


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    cmd, slug = sys.argv[1], sys.argv[2]
    stub = sys.argv[3] if len(sys.argv) > 3 and not sys.argv[3].startswith("-") else None
    if cmd == "affidavit":
        print(affidavit(slug, stub))
    elif cmd == "demand":
        print(demand_table(slug, stub))
    elif cmd == "diff":
        reply = Path(sys.argv[3]).read_text(errors="ignore")
        r = reply_diff(slug, reply, sys.argv[4] if len(sys.argv) > 4 else None)
        print(json.dumps(r, indent=1))
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
