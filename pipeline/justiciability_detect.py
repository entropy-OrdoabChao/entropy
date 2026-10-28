# -*- coding: utf-8 -*-
"""
justiciability_detect — grounded RIPENESS + MOOTNESS detector (the two thinnest legs of the
authority triad). Scans case/opposing text for the defect signals defined by Black's and emits
procedural_violation dicts that authority_triad's ripeness/mootness legs consume.

Black's: ripeness — a claim must be 'fit for judicial decision'; an unripe/premature claim is
non-justiciable. Mootness — a court 'will not decide moot cases,' i.e. cases in which there is no
longer a live controversy (the issue has become academic or dead).
"""
import re

_RIPENESS = re.compile(
    r"\bnot ripe\b|\bunripe\b|\bpremature(ly)?\b|not (yet )?accrued|not (yet )?fit for judicial|"
    r"no (actual|concrete) controvers(y|ies) yet|contingent (up)?on (a )?future event|"
    r"hypothetical (injury|harm|dispute)", re.I)
_MOOTNESS = re.compile(
    r"\bmoot\b|\bmootness\b|no longer.{0,20}(live|actual).{0,12}controvers|"
    r"controvers(y|ies).{0,20}(resolved|ended|dead|academic|expired)|"
    r"already (paid|satisfied|dismissed|cured|resolved)|relief.{0,20}no longer (available|needed)", re.I)


def detect(text: str) -> list:
    """Return procedural_violation dicts for any ripeness/mootness defect present in `text`."""
    out = []
    t = text or ""
    if _RIPENESS.search(t):
        out.append({"violation_type": "RIPENESS_DEFECT",
                    "description": "Claim appears NOT RIPE / premature — an unripe claim is non-justiciable; "
                                   "Black's: a claim must be 'fit for judicial decision.'",
                    "rule": "Ripeness doctrine (justiciability; case-or-controversy)"})
    if _MOOTNESS.search(t):
        out.append({"violation_type": "MOOTNESS_DEFECT",
                    "description": "Controversy appears MOOT — a court will not decide cases with no live "
                                   "controversy; Black's: the issue has become academic or dead.",
                    "rule": "Mootness doctrine (justiciability; case-or-controversy)"})
    return out
