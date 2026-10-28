#!/usr/bin/env python3
"""APPLICATION lane — affirmative instruments (applications / motions for relief, any forum).

WHY THIS LANE EXISTS
m1 had three lanes: affidavit, complaint, rebuttal. An affirmative application is none
of them, so `filing_type_from_stub` fell through to the COMPLAINT lane and emitted a
NOTICE TO DEFEND, a SCHEDULE A, and a twenty-day answer warning on a paper that asks an
appellate prothonotary to perform a ministerial duty. The form gate passed it, because a
complaint IS a well-formed complaint. That is the silent-misroute class: wrong instrument,
no error anywhere.

An application states GROUNDS and asks for RELIEF. It takes no notice to defend, no
schedule of definitions, no standard of review, no prayer for damages. m1_finisher
already classifies 'application_for_relief' in ADMIN_TYPES for exactly this reason.

DETERMINISTIC: every sentence below is transcribed from the stub m0 wrote. No LLM call.
The cite gate runs on the assembled text in the shared tail, as with every other lane.

S-m1-application-crosscase (2026-10-24, SEVERITY HIGH, fabrication class). CLASS RULE: a
filing carries ONLY what its own stub supplies — never another matter's prayer, never a
default forum's rule. Defect of record: the CA3 26-0003 FRAP 26(b) motion (fed_26-cv-00123,
--court 3D_CIR) passed the gate rc=0 while its RELIEF SOUGHT was the phila_ccp_260100000
PA Superior Court record-transmission prayer (Pa.R.A.P. 1935/1931/2185/1701, "brief received
by the Prothonotary on August 28, 2026") hard-coded in this file, its preamble invoked
Pa.R.A.P. 123 by default, its verification recited 18 Pa.C.S. § 4904, and the stub's three
`PLEAD, verbatim:` FRAP quotes were dropped (m1.parse_stub keeps only `- PLEAD:` lines).
Fixes here: (a) relief items come ONLY from the stub's `## RELIEF SHAPE`; zero parsed items
is a hard exit naming the stub, never a template fill; (b) `PLEAD, verbatim:` lines are read
from the stub text and each must appear verbatim (whitespace-normalized) in the draft or the
lane exits non-zero listing the missing lines; (c) rule-of-authority and verification form
are keyed by court family (FORUM_FORMS data), never a hard-coded PA default.
"""
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------- forum data (2026-10-24)
# Court family -> the forum's own verification form. DATA keyed by family; a new forum is a
# row here, not a code path. A court that resolves to no family gets an UNVERIFIED marker the
# gate FAILs (NO FORUM VERIFICATION FORM) rather than another forum's statute.
FORUM_FAMILY = {
    "FED": ("3D_CIR", "CA3", "EDPA", "SCOTUS", "MDPA", "WDPA", "DNJ"),
    "PA": ("PA_SUP", "PA_SUPER", "PA_SUPREME", "PA_CMWLTH", "PA_CP_PHILA", "PA_CP"),
}
FORUM_FORMS = {
    "FED": {
        "verification_heading": "DECLARATION",
        "verification": (
            "I, FILER NAME, declare under penalty of perjury under the laws of the "
            "United States of America that the foregoing is true and correct. 28 U.S.C. § 1746."
        ),
        "default_rule": "",   # a federal motion needs no default rule phrase; the title carries it
    },
    "PA": {
        "verification_heading": "VERIFIED STATEMENT",
        "verification": (
            "I, FILER NAME, verify that I have knowledge of the facts set forth in "
            "this application and that they are true and correct to the best of my knowledge, "
            "information and belief. I understand that false statements herein are made subject "
            "to the penalties of 18 Pa.C.S. § 4904, relating to unsworn falsification to "
            "authorities."
        ),
        "default_rule": "Pa.R.A.P. 123",
    },
}


def court_family(court: str) -> str:
    k = (court or "").strip().upper().replace("-", "_").replace(" ", "_")
    for fam, ids in FORUM_FAMILY.items():
        if k in ids:
            return fam
    if k.startswith("PA_") or "PHILA" in k:
        return "PA"
    if k.startswith(("CA", "ED", "MD", "WD", "ND", "SD", "D_")) or "CIR" in k or "SCOTUS" in k:
        return "FED"
    return ""


def _is_supplement(stub) -> bool:
    t = (stub.get("title") or "") + " " + (stub.get("filing_type") or "")
    return "supplement" in t.lower()


def _rule_from_title(title: str, court: str = "") -> str:
    """The authorizing rule, taken from the title m0 already composed. 2026-10-24: no foreign
    default — a title that names no rule yields the forum family's own default (PA: Pa.R.A.P.
    123) or nothing at all (FED). The old `else "Pa.R.A.P. 123"` put a PA rule in a CA3 motion."""
    t = title or ""
    m = re.search(r"Pa\.\s?R\.\s?A\.\s?P\.\s*(\d+(?:\.\d+)?)", t)
    if m:
        return f"Pa.R.A.P. {m.group(1)}"
    m = re.search(r"(?:Fed\.\s?R\.\s?App\.\s?P\.|FRAP)\s*(\d+(?:\([a-z0-9]+\))*)", t, re.I)
    if m:
        return f"Fed. R. App. P. {m.group(1)}"
    m = re.search(r"Fed\.\s?R\.\s?Civ\.\s?P\.\s*(\d+(?:\([a-z0-9]+\))*)", t, re.I)
    if m:
        return f"Fed. R. Civ. P. {m.group(1)}"
    return (FORUM_FORMS.get(court_family(court)) or {}).get("default_rule", "")


_TAG_RX = re.compile(r"\s*\[\s*(?:corpus|doc|official|record|source|chunk|chunk_id)\s*(?::[^\]\n]*)?\]", re.I)


def _strip_tags(s: str) -> str:
    return _TAG_RX.sub("", s).strip()


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _stub_text(stub: dict) -> str:
    p = stub.get("_path") or stub.get("path")
    if p and Path(p).exists():
        return Path(p).read_text(encoding="utf-8", errors="replace")
    return stub.get("_text") or ""


def _section(text: str, heading: str) -> str:
    """Body of '## <heading>...' up to the next '## ' (same tolerant match as m1._section)."""
    m = re.search(rf"^##\s+{re.escape(heading)}.*?$", text, re.M | re.I)
    if not m:
        return ""
    start = m.end()
    nxt = re.search(r"^##\s+", text[start:], re.M)
    return text[start:start + nxt.start()] if nxt else text[start:]


def relief_from_stub(stub: dict, stub_text: str = None) -> list:
    """RELIEF items from the stub's own `## RELIEF SHAPE` block ONLY (numbered or bulleted
    lines). Returns [] when the block is absent or empty — the caller hard-exits; this function
    never substitutes a template."""
    t = stub_text if stub_text is not None else _stub_text(stub)
    body = _section(t, "RELIEF SHAPE")
    items = []
    for ln in body.splitlines():
        m = re.match(r"^\s*(?:\d+[.)]|[-*•])\s+(.+?)\s*$", ln)
        if m:
            it = _strip_tags(m.group(1))
            if it:
                items.append(it)
    return items


def verbatim_pleads(stub_text: str) -> list:
    """Every `- PLEAD, verbatim: <text>` line in the stub's CONTROLLING AUTHORITY block, with
    provenance tags removed (the finisher strips them from the draft anyway)."""
    out = []
    for ln in _section(stub_text, "CONTROLLING AUTHORITY").splitlines():
        m = re.match(r"^\s*-\s*PLEAD,\s*verbatim:\s*(.+?)\s*$", ln, re.I)
        if m:
            out.append(_strip_tags(m.group(1)))
    return out


def spine_from_stub_text(stub_text: str) -> dict:
    """{claim_heading: [plead, ...]} preserving stub order and BOTH forms (`PLEAD:` and
    `PLEAD, verbatim:`). m1.parse_stub only keeps `PLEAD:`; the lane reads the stub itself so
    the rules being construed are not dropped from under the argument."""
    spine, claim = {}, None
    for ln in _section(stub_text, "CONTROLLING AUTHORITY").splitlines():
        h = re.match(r"^###\s+(.+)", ln)
        if h:
            claim = h.group(1).strip()
            spine.setdefault(claim, [])
            continue
        p = re.match(r"^\s*-\s*PLEAD(?:,\s*verbatim)?:\s*(.+?)\s*$", ln, re.I)
        if p and claim:
            spine[claim].append(_strip_tags(p.group(1)))
    return spine


def missing_verbatim(draft_text: str, pleads: list) -> list:
    """The verbatim lines NOT present (whitespace-normalized) in the draft."""
    d = _norm_ws(draft_text)
    return [p for p in pleads if _norm_ws(p) not in d]


def _filer_role(stub_text: str) -> str:
    cap = _section(stub_text, "CAPTION") or stub_text[:1500]
    if re.search(r"\bPetitioner\b", cap):
        return "Petitioner"
    if re.search(r"\bAppellant\b", cap):
        return "Appellant"
    if re.search(r"\bMovant\b", cap):
        return "Movant"
    if re.search(r"\bPlaintiff\b", cap):
        return "Plaintiff"
    return "Appellant"


def finisher_supplies(court: str, filing_type: str = None, stub_name: str = "") -> dict:
    """S-finisher-duplicate-organs (2026-10-25): which of this lane's organs the finisher will
    append for (court, filing_type) -- the lane then does NOT emit them (fix at the source; the
    finisher is authoritative for the forum's mandatory RELIEF / VERIFICATION / CERTIFICATE OF
    SERVICE). `filing_type` is the kind m1 finishes + gates under; when the caller passes none,
    it is inferred the same way m1.run() does (post_order_vehicle_of on the stub stem, else the
    admin 'application_for_relief'). Any failure -> {} and the lane emits everything (safe: the
    finisher's strip is the safety net)."""
    kind = (filing_type or "").strip()
    if not kind:
        try:
            from pipeline.m1 import post_order_vehicle_of
            kind = post_order_vehicle_of(Path(stub_name).stem if stub_name else "") or ""
        except Exception:
            kind = ""
        kind = kind or "application_for_relief"
    try:
        from pipeline.m1_finisher import lane_organs_supplied
        return dict(lane_organs_supplied(court, kind), _kind=kind)
    except Exception:
        return {}


GENERIC_SECTIONS = {"STATEMENT OF GROUNDS", "PROCEDURAL HISTORY", "RELIEF SOUGHT", "RELIEF", "PRAYER FOR RELIEF",
                    "EXHIBITS", "VERIFICATION", "CERTIFICATE OF SERVICE", "FACTS", "ARGUMENT", "QUESTIONS PRESENTED"}


def _named_sections(court: str, filing_type: str, spine: dict) -> dict:
    """{claim -> mandated heading} for every NON-generic mandatory section of (court, filing_type):
    the ground whose claim text shares the most key terms with the section's heading+rule+requires
    (cite tokens like '341(c)' weigh most). Empty when the profile mandates nothing special or no
    ground matches (the gate then reports the gap — never a fabricated section)."""
    try:
        from pipeline import m1_filing_gate as _g
        secs = _g._mandatory_sections(court, filing_type or "") or []
    except Exception:
        return {}
    out, used = {}, set()
    for sec in secs:
        head = str(sec.get("heading") or "").strip()
        if not head or head.upper() in GENERIC_SECTIONS:
            continue
        probe = " ".join(str(sec.get(k) or "") for k in ("heading", "rule", "requires"))
        cites = set(re.findall(r"\d+(?:\([a-z0-9]+\))+|\d{3,4}", probe))
        words = {w for w in re.findall(r"[a-z]{5,}", probe.lower())} - {"shall", "would", "which", "court"}
        best, score = None, 0
        for claim in spine:
            if claim in used:
                continue
            t = claim.lower()
            sc = 3 * sum(1 for c in cites if c in t) + sum(1 for w in words if w in t)
            if sc > score:
                best, score = claim, sc
        if best and score >= 3:
            out[best] = head; used.add(best)
    return out


def assemble(stub: dict, case_id: str, court: str, out_path: str, filing_type: str = None) -> dict:
    """Write the application body. Returns counts for the caller to print.
    Hard exits (SystemExit, non-zero): zero RELIEF SHAPE items; any `PLEAD, verbatim:` line
    missing from the assembled draft.
    `filing_type` (optional): the kind the finisher/gate will use; see finisher_supplies()."""
    title = (stub.get("title") or "").strip()
    stub_text = _stub_text(stub)
    stub_name = stub.get("_path") or "<stub>"
    fam = court_family(court)
    forms = FORUM_FORMS.get(fam) or {}
    rule = _rule_from_title(title, court)
    supp = _is_supplement(stub)
    role = _filer_role(stub_text)
    supplied = finisher_supplies(court, filing_type, stub.get("_path") or "")
    _omit = [h for h in ("RELIEF SOUGHT", "VERIFIED STATEMENT", "PROOF OF SERVICE") if supplied.get(h)]
    if _omit:
        print(f"  [organs] lane omits {', '.join(_omit)} -- finisher supplies the forum organ "
              f"({court}/{supplied.get('_kind')})")

    # (a) RELIEF: stub's own block or stop. Never a template fill (S-m1-application-crosscase).
    relief = relief_from_stub(stub, stub_text)
    if not relief:
        sys.exit(f"application lane: ZERO relief items parsed from '## RELIEF SHAPE' in stub "
                 f"{stub_name} — refusing to draft; a relief prayer is never filled from a template "
                 f"(S-m1-application-crosscase 2026-10-24)")
    # (b) spine from the stub text (both PLEAD forms, stub order); fall back to the parsed dict.
    spine = spine_from_stub_text(stub_text) if stub_text else {}
    if not spine:
        spine = stub.get("authority_spine") or {}
    pleads_v = verbatim_pleads(stub_text) if stub_text else []

    lines = []
    n = 0

    def para(text: str):
        nonlocal n
        n += 1
        lines.append(f"{n}. {text.strip()}")
        lines.append("")

    # ---- WRITTEN APPLICATION. The heading is the rule's language (Pa.R.A.P. 123(a)) and the
    # gate's exact-heading matcher keys on it; the rule phrase itself comes from the title.
    lines.append("## WRITTEN APPLICATION")
    lines.append("")
    verb = ("submits this supplement to his pending application for relief" if supp
            else "applies to this Court for relief")
    opener = (
        f"{role} FILER NAME, sui juris, "
        + (f"pursuant to {rule}, " if rule else "")
        + f"{verb} and states the following grounds with particularity, together with proof of "
        f"service on all other parties."
    )
    lines.append(opener)
    lines.append("")

    if supp:
        # 2026-10-24: the old text named Pa.R.A.P. 1935 and the prothonotary — one matter's facts
        # in every supplement. A supplement says only what every supplement is.
        lines.append(
            "This supplement does not repeat the relief already requested. It supplies grounds "
            "that the pending application did not reach, as stated below."
        )
        lines.append("")

    # ---- STATEMENT OF GROUNDS. Every ground m0 derived goes here; omitting one waives it.
    # A forum profile may mandate a NAMED section for one showing (PA_CP_PHILA/petition_341c:
    # "DETERMINATION OF FINALITY (Pa.R.A.P. 341(c))"); the ground that makes that showing is
    # promoted out of the grounds list under the mandated heading. DATA (m1_style_profiles.json
    # mandatory_sections[].heading/.requires), not a filing-type branch here.
    named = _named_sections(court, filing_type, spine)
    lines.append("## STATEMENT OF GROUNDS")
    lines.append("")
    for claim, pleads in spine.items():
        if claim in named:
            continue
        lines.append(f"### {claim.strip()}")
        lines.append("")
        for p in pleads:
            para(p)

    # ---- Procedural history, as numbered averments (dates come from the docket).
    hist = stub.get("admin_history") or []
    if hist:
        lines.append("## PROCEDURAL HISTORY")
        lines.append("")
        for h in hist:
            para(re.sub(r"^\s*[-*]\s*", "", h))

    # ---- Forum-named showings (profile mandatory_sections beyond the generic organs) sit between the
    # history and the exhibits/relief — the profile's own order: GROUNDS, HISTORY, <named>, RELIEF.
    for claim, heading in named.items():
        lines.append(f"## {heading}")
        lines.append("")
        lines.append(f"### {claim.strip()}")
        lines.append("")
        for p in spine[claim]:
            para(p)

    # ---- Exhibits: the stub's evidence blocks, each with its quoted source.
    ev = stub.get("evidence") or []
    if ev:
        lines.append("## EXHIBITS")
        lines.append("")
        for i, e in enumerate(ev, 1):
            tag = e.get("tag") or f"E{i}"
            src = (e.get("source") or "").strip()
            quote = (e.get("quote") or "").strip()
            para(f"**Exhibit {tag}** — {(e.get('title') or '').strip()} Source: {src}")
            if quote:
                for q in quote.splitlines():
                    q = q.strip().lstrip(">").strip()
                    if q:
                        lines.append(f"    > {q}")
                lines.append("")

    # ---- RELIEF SOUGHT — the stub's RELIEF SHAPE items, in the stub's order, nothing else.
    # Omitted when the finisher supplies the forum's prayer (the items reach it via finish(relief=...),
    # m1.py passes relief_from_stub(); heading per m1_finisher.prayer_heading).
    if not supplied.get("RELIEF SOUGHT"):
        lines.append("## RELIEF SOUGHT")
        lines.append("")
        lines.append(f"WHEREFORE, {role} respectfully requests that this Court:")
        lines.append("")
        for i, r in enumerate(relief, 1):
            lines.append(f"{chr(96 + i)}. {r}")
            lines.append("")

    # ---- VERIFICATION in the forum's own form (FORUM_FORMS). Pa.R.A.P. 123(c) wants a verified
    # statement in PA; a federal paper declares under 28 U.S.C. § 1746. Unknown family -> marker.
    # Omitted when the finisher appends the forum's Pa.R.C.P. 1024 VERIFICATION.
    if not supplied.get("VERIFIED STATEMENT"):
        lines.append(f"## {forms.get('verification_heading', 'VERIFICATION')}")
        lines.append("")
        lines.append(forms.get("verification") or f"[NO FORUM VERIFICATION FORM for court {court!r}]")
        lines.append("")
        lines.append("Date: ______________________")
        lines.append("")
        lines.append("/s/ FILER NAME")
        lines.append("FILER NAME, sui juris")
        lines.append("")

    # ---- PROOF OF SERVICE, filed WITH the application. Omitted when the finisher appends the
    # CERTIFICATE OF SERVICE (it does for every filing type).
    if not supplied.get("PROOF OF SERVICE"):
        lines.append("## PROOF OF SERVICE")
        lines.append("")
        lines.append(
            "I certify that on the date below I served a true and correct copy of this "
            "application upon all other parties of record by first class United States Mail, "
            "postage prepaid, at the addresses of record."
        )
        lines.append("")
        lines.append("Date: ______________________")
        lines.append("")
        lines.append("/s/ FILER NAME")
        lines.append("FILER NAME, sui juris")
        lines.append("")

    body = f"# {title}\n\n" + "\n".join(lines).rstrip() + "\n"

    # (b) every PLEAD, verbatim line must be on the page or the lane stops.
    miss = missing_verbatim(body, pleads_v)
    if miss:
        sys.exit("application lane: %d `PLEAD, verbatim:` line(s) from %s missing from the draft "
                 "(S-m1-application-crosscase 2026-10-24):\n  - %s"
                 % (len(miss), stub_name, "\n  - ".join(m[:160] for m in miss)))

    Path(out_path).write_text(body, encoding="utf-8")
    return {
        "paragraphs": n,
        "claims": len(spine),
        "exhibits": len(ev),
        "relief_items": len(relief),
        "verbatim_pleads": len(pleads_v),
        "words": len(body.split()),
        "supplement": supp,
        "family": fam,
    }
