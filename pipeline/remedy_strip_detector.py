"""remedy_strip_detector — the GENUS organ for selective-attack objections.

THE TRICK (caught live 2026-09-23, the insurer PO wave; no prior detector saw it):
an opposing brief does NOT attack the legally weakest counts — it attacks the counts
carrying the MONEY MULTIPLIERS (treble damages, fee-shifting, punitive, restitution/
disgorgement of the whole course of dealing), while leaving untouched the counts whose
recovery is narrowest. Liability's shape is conceded; the expensive remedies are deleted.
Then the case settles cheap on what survives.

Why no existing organ catches it: every current detector reads ONE document for legal
defects inside it. This defect is only visible by CROSS-REFERENCING which counts were
attacked against each count's REMEDY SCHEDULE. Same architectural shape as
case_fact_comparator (cross-filing facts) — a comparator over a ledger, not a scan.

AGNOSTIC: remedy weights come from the statute cited by the count (or its common-law
remedy class), never from a hardcoded case. Any jurisdiction, any claim taxonomy.
"""
from __future__ import annotations
import re

# ── remedy schedule: what each claim class can actually PAY ────────────────
# weight = recovery multiplier potential, not merit. Sources are the statutes' own
# remedy sections; common-law classes carry their restitutionary/punitive scope.
REMEDY_CLASSES = {
    "treble_and_fees": {
        "weight": 5,
        "signals": [r"201-9\.2", r"UTPCPL", r"unfair trade practices",
                    r"consumer protection law", r"\btreble\b", r"1692k", r"1681n"],
        "remedy": "treble damages + attorney fees (statutory multiplier)",
    },
    "punitive_interest_fees": {
        "weight": 4,
        "signals": [r"8371", r"bad faith", r"punitive damages"],
        "remedy": "interest, punitive damages, court costs, attorney fees",
    },
    "restitution_disgorgement": {
        "weight": 4,
        "signals": [r"unjust enrichment", r"money had and received", r"restitution",
                    r"disgorge", r"constructive trust", r"quasi.?contract"],
        "remedy": "restitution/disgorgement of the ENTIRE benefit retained "
                  "(not capped by contract expectancy)",
    },
    "conversion_full_value": {
        "weight": 3,
        "signals": [r"\bconversion\b", r"trover", r"wrongful dominion"],
        "remedy": "full value of the property converted + possible punitive exposure",
    },
    "contract_expectancy": {
        "weight": 1,
        "signals": [r"breach of contract", r"implied covenant",
                    r"good faith and fair dealing"],
        "remedy": "expectancy damages only — the narrowest recovery",
    },
}

_COUNT_RE = re.compile(
    r"Count\s+([IVXLC]+|\d+)\s*[\(\-—:]?\s*([A-Za-z §\.\d'’\-]{0,60})", re.I)
_DISMISS_RE = re.compile(
    r"dismiss(?:al|ed|es)?|strike|strik|demurrer|preliminary objection", re.I)


def _roman(s: str) -> int:
    s = s.strip().upper()
    if s.isdigit():
        return int(s)
    vals = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}
    tot = prev = 0
    for ch in reversed(s):
        v = vals.get(ch, 0)
        tot = tot - v if v < prev else tot + v
        prev = max(prev, v)
    return tot


def classify_count(label: str) -> tuple[str, int, str]:
    """Map a count's text to its remedy class. Returns (class, weight, remedy)."""
    low = (label or "").lower()
    best = ("unclassified", 0, "unknown remedy scope")
    for cls, spec in REMEDY_CLASSES.items():
        for sig in spec["signals"]:
            if re.search(sig, low, re.I):
                if spec["weight"] > best[1]:
                    best = (cls, spec["weight"], spec["remedy"])
    return best


def parse_attacked_counts(motion_text: str) -> set[int]:
    """Counts the movant asks the court to dismiss/strike."""
    attacked: set[int] = set()
    for m in re.finditer(
            r"(?:dismiss|strike|stricken)[^.;]{0,200}?Counts?\s+"
            r"((?:[IVXLC]+|\d+)(?:\s*,\s*(?:and\s+)?(?:[IVXLC]+|\d+))*"
            r"(?:\s*,?\s*and\s+(?:[IVXLC]+|\d+))?)", motion_text, re.I):
        for tok in re.findall(r"[IVXLC]+|\d+", m.group(1)):
            n = _roman(tok)
            if 0 < n < 40:
                attacked.add(n)
    for m in re.finditer(
            r"Count\s+([IVXLC]+|\d+)[^.]{0,120}?(?:should be|must be|is)\s+dismissed",
            motion_text, re.I):
        n = _roman(m.group(1))
        if 0 < n < 40:
            attacked.add(n)
    return attacked


# A "label" that is really order/prayer boilerplate ("... is DISMISSED with prejudice")
# carries no claim identity — reject it or every count classifies as unclassified.
_NOISE_LABEL = re.compile(
    r"dismiss|with prejudice|of plaintiff|complaint is|should be|stricken|granted", re.I)


def parse_complaint_counts(complaint_text: str) -> dict[int, str]:
    """Map count number -> its CLAIM label, from the pleading (or a brief's recital of it).

    Inline recitals ("unjust enrichment (Count II)") are the MOST reliable source and are
    read FIRST: a brief's proposed order repeats "Count II ... is DISMISSED with prejudice",
    which matches the heading shape but names no claim.
    """
    counts: dict[int, str] = {}
    # primary: "statutory bad faith (Count I), unjust enrichment (Count II)" recitals
    for m in re.finditer(r"([A-Za-z][A-Za-z §\.\d'’\- ]{3,50}?)\s*\(Count\s+([IVXLC]+|\d+)\)",
                         complaint_text, re.I):
        n = _roman(m.group(2))
        label = m.group(1).strip(" ,;.")
        if 0 < n < 40 and label and not _NOISE_LABEL.search(label):
            counts.setdefault(n, label)
    # secondary: real pleading headings "COUNT II — UNJUST ENRICHMENT"
    for m in re.finditer(
            r"COUNT\s+([IVXLC]+|\d+)\s*[\-—:–]?\s*\n?\s*([^\n]{0,90})",
            complaint_text, re.I):
        n = _roman(m.group(1))
        label = m.group(2).strip(" ,;.")
        if 0 < n < 40 and label and not _NOISE_LABEL.search(label):
            counts.setdefault(n, label)
    return counts


def detect(motion_text: str, complaint_text: str, source_doc: str = "") -> list[dict]:
    """Flag REMEDY_STRIPPING_OBJECTION when attacked counts carry materially higher
    remedy weight than the counts left untouched."""
    counts = parse_complaint_counts(complaint_text) or parse_complaint_counts(motion_text)
    attacked = parse_attacked_counts(motion_text)
    if not counts or not attacked:
        return []
    spared = set(counts) - attacked
    if not spared or not (attacked & set(counts)):
        return []

    att = {n: classify_count(counts[n]) for n in attacked if n in counts}
    spa = {n: classify_count(counts[n]) for n in spared}
    if not att or not spa:
        return []

    aw = sum(c[1] for c in att.values()) / len(att)
    sw = sum(c[1] for c in spa.values()) / len(spa)
    # materiality gate: the attacked set must be meaningfully richer in remedy,
    # and at least one high-multiplier (>=3) count must be targeted.
    if aw - sw < 1.0 or max((c[1] for c in att.values()), default=0) < 3:
        return []

    hit = "; ".join(f"Count {n} ({counts[n].strip()[:40]}) = {att[n][2]}"
                    for n in sorted(att) if att[n][1] >= 3)
    left = "; ".join(f"Count {n} ({counts[n].strip()[:40]}) = {spa[n][2]}"
                     for n in sorted(spa))
    return [{
        "violation_type": "REMEDY_STRIPPING_OBJECTION",
        "rule_citation": ("Pa.R.C.P. 1028(a)(4) / Fed. R. Civ. P. 12(b)(6) "
                          "(demurrer tests legal sufficiency, not exposure); "
                          "Pa.R.C.P. 1020(c) (pleading in the alternative permitted)"),
        "rule_text": ("A demurrer admits all well-pleaded facts and tests whether recovery "
                      "is impossible as a matter of law; it is not a vehicle for selecting "
                      "which remedies a defendant prefers to face. A plaintiff may plead "
                      "alternative and inconsistent theories."),
        "source_doc": source_doc or "opposing motion",
        "quote": f"Counts attacked: {sorted(attacked)}; counts left standing: {sorted(spared)}",
        "explanation": (
            f"The objections cluster on the HIGH-RECOVERY counts while leaving the "
            f"narrowest-recovery counts untouched (mean remedy weight attacked={aw:.1f} vs "
            f"spared={sw:.1f}). Targeted: {hit}. Left standing: {left}. "
            "Selective attack on the multiplier counts — while conceding the counts whose "
            "recovery is capped at contract expectancy — is a remedy-stripping posture, not "
            "a legal-sufficiency argument. It is also an implicit concession that the spared "
            "counts state claims."),
        "fix": ("Name the pattern in the response: the movant did not object to the counts "
                "it considers weakest — it objected to the counts that multiply recovery, "
                "and conceded legal sufficiency of the rest. Plead the high-remedy counts in "
                "the ALTERNATIVE (Pa.R.C.P. 1020(c)) so the alternative-pleading rule, not "
                "the movant's preference, controls which theories survive."),
        "severity": "SERIOUS",
    }]
