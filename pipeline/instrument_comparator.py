#!/usr/bin/env python3
"""pipeline/instrument_comparator.py — cross-production instrument comparison.

THE GAP THIS CLOSES (2026-08-25, user-spotted): single-document analysis
(regex pins or the LLM reciprocity pass) reads one production at a time, so
it can NEVER see that two productions of the SAME instrument disagree —
e.g. AmeriSave's file copy of the note carries a blank-indorsed allonge
while Planet Home Lending's own "verification of the debt" packet contains
the same note with NO allonge at all. That conflict is dispositive-grade
(13 Pa.C.S. 3204(a)/3205(b); FRE 1003 genuine-question-of-authenticity)
and lives BETWEEN documents, not inside any one of them.

Deterministic by design — no LLM. Extraction is regex + proximity over the
already-OCR'd intake sidecars; comparison is set logic keyed by loan number.
The LLM layers stay upstream (reciprocity) — this is the guardrail organ.

Emitted category: INSTRUMENT_VERSION_CONFLICT (FATAL), ViolationPin-shaped,
merging into the scorecard exactly like block14 pins.
"""
from __future__ import annotations

import re
from pathlib import Path

# ── extraction patterns ────────────────────────────────────────────────
_LOAN_NO = re.compile(r"Loan\s*Number[:;]?\s*#?\s*(\d{6,12})", re.I)
_ALLONGE = re.compile(r"\bALLONGE\b", re.I)
_NOTE_HEAD = re.compile(
    r"(?:PROMISSORY\s+NOTE|FIXED\s+RATE\s+NOTE|BORROWER'?S\s+PROMISE\s+TO\s+PAY)", re.I)
_PAY_TO_ORDER = re.compile(r"PAY\s+TO\s+(?:'?THE\s+)?ORDER\s+OF\b[^\n]{0,120}", re.I)
_WITHOUT_RECOURSE = re.compile(r"Without\s+Recourse", re.I)
# names that count as a NAMED transferee on an allonge (extend as cases add parties)
_ENTITY = re.compile(
    r"(PLANET\s+HOME\s+LENDING|U\.?S\.?\s+BANK|GINNIE\s+MAE|BANK\s+OF|WELLS\s+FARGO|"
    r"DOVENMUEHLE|LAKEVIEW|FREEDOM\s+MORTGAGE|TRUST\s+20\d\d)", re.I)
_OFFSITE_ADMISSION = re.compile(
    r"original\s+(?:promissory\s+)?note[^.]{0,120}?"
    r"(?:housed|stored|held|maintained|located)[^.]{0,80}"
    r"(?:off[-\s]?site|secure|vault|custodian)(?:[ \t]*\w+){0,3}", re.I | re.S)
_VERIFICATION_PACKET = re.compile(
    r"(?:verification\s+of\s+the\s+debt|documents\s+as\s+verification)", re.I)


def extract_instrument_profile(text: str, source_doc: str) -> dict | None:
    """Extract one production's instrument facts from OCR text.

    Returns None when the document contains no note/allonge material at all.
    Profile: {source_doc, loan_numbers, has_note, has_allonge,
              allonge_transferee ('BLANK'|'<name>'|None), blank_indorsement,
              possession_admission (quote|''), is_verification_packet}
    """
    if not text:
        return None
    has_note = bool(_NOTE_HEAD.search(text))
    allonge_positions = [m.start() for m in _ALLONGE.finditer(text)]
    has_allonge = bool(allonge_positions)
    if not has_note and not has_allonge:
        return None

    loans = list(dict.fromkeys(_LOAN_NO.findall(text)))

    allonge_transferee = None
    blank_indorsement = False
    if has_allonge:
        # inspect the window after each ALLONGE heading for the indorsement line
        for pos in allonge_positions:
            window = text[pos:pos + 1200]
            m = _PAY_TO_ORDER.search(window)
            if not m:
                continue
            after = window[m.end():m.end() + 200]
            line_rest = m.group(0)[len("PAY TO THE ORDER OF"):].strip(" :;_\u2014-")
            named = _ENTITY.search(line_rest) or _ENTITY.search(after[:120])
            if named:
                allonge_transferee = named.group(1).upper().strip()
                break
            # no entity on/near the order-of line => blank indorsement
            if _WITHOUT_RECOURSE.search(window):
                allonge_transferee = "BLANK"
                blank_indorsement = True
                break
        if allonge_transferee is None:
            # ALLONGE heading present but no parseable order-of line: record as present/unparsed
            allonge_transferee = "UNPARSED"

    adm = _OFFSITE_ADMISSION.search(text)
    return {
        "source_doc": source_doc,
        "loan_numbers": loans,
        "has_note": has_note,
        "has_allonge": has_allonge,
        "allonge_transferee": allonge_transferee,
        "blank_indorsement": blank_indorsement,
        "possession_admission": " ".join(adm.group(0).split())[:300] if adm else "",
        "is_verification_packet": bool(_VERIFICATION_PACKET.search(text)),
    }


def compare_instrument_productions(profiles: list) -> list:
    """Compare productions of the same instrument (grouped by loan number).

    Emits ViolationPin-shaped dicts (category INSTRUMENT_VERSION_CONFLICT)
    when productions of one loan disagree on the instrument's physical state:
      C1: allonge present in one production, absent from another that
          purports to verify/enforce the same note (the PHL pattern);
      C2: allonge indorsed in blank (bearer paper) while an enforcing
          production shows admitted non-possession of the original;
      C3: allonge transferee named differently across productions.
    Even-handed records (all productions agree) emit nothing.
    """
    by_loan: dict[str, list] = {}
    for p in profiles or []:
        if not p:
            continue
        for ln in (p.get("loan_numbers") or ["_UNKEYED"]):
            by_loan.setdefault(ln, []).append(p)

    # DEDUP: a production listing multiple loan aliases for the same physical
    # note (origination + servicing numbers) would double-report. Keep the
    # loan group whose member set is not a strict duplicate of an
    # already-kept group.
    seen_sets = []
    for ln in sorted(by_loan, key=lambda k: -len(by_loan[k])):
        members = frozenset(id(p) for p in by_loan[ln])
        if any(members <= s for s in seen_sets):
            by_loan[ln] = []
            continue
        seen_sets.append(members)

    out = []
    for loan, prods in by_loan.items():
        if loan == "_UNKEYED" or len(prods) < 2:
            continue
        with_allonge = [p for p in prods if p["has_allonge"]]
        note_no_allonge = [p for p in prods if p["has_note"] and not p["has_allonge"]]

        # C1 — allonge present vs absent across productions of the same note
        if with_allonge and note_no_allonge:
            a = with_allonge[0]
            b = next((p for p in note_no_allonge if p["is_verification_packet"]),
                     note_no_allonge[0])
            transferee = a.get("allonge_transferee")
            t_txt = ("indorsed in BLANK (bearer paper, 13 Pa.C.S. § 3205(b))"
                     if transferee == "BLANK" else
                     f"indorsed to {transferee}" if transferee and transferee != "UNPARSED"
                     else "with an allonge whose indorsement line is present")
            out.append(_pin(
                loan=loan,
                quote=(f"{a['source_doc']}: ALLONGE {t_txt}; "
                       f"{b['source_doc']}: same note, NO allonge"
                       + (" (self-described 'verification of the debt' packet)"
                          if b["is_verification_packet"] else "")),
                explanation=(
                    f"Two productions of the note for loan {loan} are in materially "
                    f"different physical states: {a['source_doc']} carries an allonge "
                    f"{t_txt}, while {b['source_doc']} produces the identical note with "
                    "no allonge at all. An indorsement is effective only on the "
                    "instrument or a paper AFFIXED to it (13 Pa.C.S. § 3204(a); U.C.C. "
                    "§ 3-204(a)); an allonge that appears and disappears across "
                    "productions was either never affixed or was created separately — "
                    "either way it fails as an indorsement, and the conflict raises a "
                    "genuine question of authenticity requiring the ORIGINAL (FRE 1003)."),
                fix=("Compel production of the original wet-ink note for inspection with "
                     "whatever is or is not affixed; identify the document custodian and "
                     "the custody chain; strike enforcement reliance on the copy until "
                     "the original resolves the conflict."),
            ))

        # C2 — blank indorsement + admitted non-possession by an enforcing production
        blank = [p for p in prods if p.get("blank_indorsement")]
        admits = [p for p in prods if p.get("possession_admission")]
        if blank and admits:
            adm = admits[0]
            out.append(_pin(
                loan=loan,
                quote=adm["possession_admission"],
                explanation=(
                    f"The allonge for loan {loan} is indorsed in blank, making the note "
                    "payable to bearer — enforcement rides on POSSESSION of the original "
                    "(13 Pa.C.S. § 3205(b); U.C.C. §§ 3-205(b), 3-301). The enforcing "
                    f"production ({adm['source_doc']}) admits the original is held "
                    "elsewhere. A servicer that does not possess bearer paper is not the "
                    "holder and not a person entitled to enforce."),
                fix=("Require proof of possession of the original bearer instrument at "
                     "the commencement of enforcement, or dismissal for want of "
                     "real-party/PETE status (Fed. R. Civ. P. 17(a))."),
            ))

        # C3 — different named transferees across productions
        names = {p.get("allonge_transferee") for p in with_allonge
                 if p.get("allonge_transferee") not in (None, "UNPARSED")}
        if len(names) > 1:
            out.append(_pin(
                loan=loan,
                quote="; ".join(
                    f"{p['source_doc']}: order-of={p.get('allonge_transferee')}"
                    for p in with_allonge),
                explanation=(
                    f"Productions of the allonge for loan {loan} name DIFFERENT "
                    f"transferees ({', '.join(sorted(names))}) — the versions cannot "
                    "all be the instrument's true state."),
                fix="Compel the original; the inconsistent copies impeach each other.",
            ))
    return out


def _pin(loan: str, quote: str, explanation: str, fix: str) -> dict:
    return {
        "violation_type": "INSTRUMENT_VERSION_CONFLICT",
        "rule_citation": ("13 Pa.C.S. §§ 3204(a), 3205(b), 3301; U.C.C. §§ 3-204(a), "
                          "3-205(b), 3-301; Fed. R. Evid. 1003; Fed. R. Civ. P. 17(a)"),
        "rule_text": ("An indorsement must be written on the instrument or on a paper "
                      "affixed to it; a blank indorsement makes the instrument payable "
                      "to bearer and enforceable only by its possessor; a duplicate is "
                      "inadmissible where a genuine question is raised about the "
                      "original's authenticity."),
        "source_doc": f"cross-production comparison (loan {loan})",
        "quote": quote[:500],
        "explanation": explanation,
        "fix": fix,
        "severity": "FATAL",
    }


def scan_case_intake(intake_dir: str | Path, max_docs: int = 400) -> list:
    """Run the comparator over a case intake directory's OCR sidecars.

    Returns ViolationPin-shaped conflict dicts (possibly empty). Bounded and
    fail-safe: unreadable files are skipped, never raised.
    """
    idir = Path(intake_dir)
    if not idir.is_dir():
        return []
    profiles = []
    for fp in sorted(idir.glob("*.txt"))[:max_docs]:
        try:
            if fp.stat().st_size < 400:
                continue
            text = fp.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        prof = extract_instrument_profile(text, fp.name[:80])
        if prof:
            profiles.append(prof)
    return compare_instrument_productions(profiles)
