"""
source_display.py - Single source of truth for rendering a docket
source file as it should appear in court-facing documents.

If a filename clearly identifies a docket entry (leading number, or
ECF/Doc/Dkt/DE marker), render as "Dkt. N" or "Dkt. N-S".
Otherwise return the original basename. Conservative by design:
better to show the filename than guess a docket number wrong.
"""
from __future__ import annotations
import re
from pathlib import Path

_DKT_PATTERNS = [
    # Leading bare number: "68-1.pdf", "121.pdf", "122-2.pdf"
    re.compile(r"^(\d{1,4})(?:-(\d{1,3}))?\.(?:pdf|docx)$", re.IGNORECASE),
    # Leading ECF/Doc/Dkt/DE marker: "ecf 130.pdf", "Doc 88.pdf", "DE-52.pdf"
    re.compile(r"^(?:ecf|doc|dkt|de)[\s_-]+(\d{1,4})(?:-(\d{1,3}))?", re.IGNORECASE),
]


def filename_to_dkt(fname):
    """Return 'Dkt. N' or 'Dkt. N-S' if filename clearly identifies a
    docket entry. Returns None otherwise (caller keeps original filename)."""
    if not fname:
        return None
    for pat in _DKT_PATTERNS:
        m = pat.search(fname)
        if m:
            main = m.group(1)
            sub = m.group(2) if m.lastindex and m.lastindex >= 2 else None
            return f"Dkt. {main}-{sub}" if sub else f"Dkt. {main}"
    return None


def display_source(fpath):
    """Render a source-file path for a court-facing scorecard.
    Returns 'Dkt. N' when extractable, basename otherwise."""
    if not fpath:
        return ""
    fname = Path(fpath).name
    return filename_to_dkt(fname) or fname