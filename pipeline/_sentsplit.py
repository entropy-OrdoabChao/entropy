"""Abbreviation- and quote-aware sentence splitting for filing text.

A naive `re.split(r"(?<=[.!?])\s+", t)` breaks after every period, so it splits inside
"Mr. Filer", "Pa. Super. 1999", "42 Pa.C.S. § 8371", "E.D. Pa. 2016", and inside any
quotation containing an abbreviation. Downstream code that keeps only the first fragment
then emits a one-word quote -- e.g. a record admission rendered as: states: "Mr.

This module is case-agnostic: it knows legal abbreviations, not any particular case.
"""
import re

# Abbreviations after which a period does NOT end a sentence.
_ABBR = (
    # honorifics / names
    "mr", "mrs", "ms", "dr", "hon", "jr", "sr", "st", "atty", "esq", "prof",
    # courts / reporters / jurisdictions
    "pa", "u.s", "us", "f", "f.2d", "f.3d", "a.2d", "a.3d", "s.ct", "l.ed",
    "supp", "ct", "cir", "dist", "div", "super", "cmwlth", "app", "ed",
    "e.d", "w.d", "m.d", "n.d", "s.d", "c.d",
    # citation furniture
    "no", "nos", "art", "sec", "secs", "ch", "chap", "para", "pp", "p", "id",
    "cf", "eg", "ie", "etc", "seq", "al", "v", "vs", "ibid", "supra", "infra",
    "stat", "reg", "regs", "rev", "rul", "op", "cit", "n",
    # entities
    "co", "corp", "inc", "llc", "llp", "ltd", "assn", "bros", "dept", "univ",
    "ins", "mut", "cas", "natl", "nat", "am", "jur", "c.j.s",
    # dates / misc
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sept", "sep", "oct",
    "nov", "dec", "approx", "est", "min", "max", "vol", "ex",
)
_ABBR_SET = set(_ABBR)


def split_sentences(text: str) -> list:
    """Split into sentences without breaking abbreviations or quoted material."""
    t = " ".join((text or "").split())
    if not t:
        return []
    out, buf, depth_q = [], [], False
    # walk candidate boundaries: a terminal mark followed by whitespace
    i, n, start = 0, len(t), 0
    while i < n:
        c = t[i]
        if c == '"':
            depth_q = not depth_q
        elif c == "\u201c":
            depth_q = True
        elif c == "\u201d":
            depth_q = False
        if c in ".!?":
            # look back at the token immediately before the mark
            j = i - 1
            while j >= 0 and (t[j].isalnum() or t[j] in ".&"):
                j -= 1
            tok = t[j + 1:i].lower().strip(".")
            nxt = t[i + 1:i + 3]
            # a closing quote or bracket may sit between the mark and the space
            k = i + 1
            while k < n and t[k] in '"\u201d\u2019\')]':
                k += 1
            ends = (k >= n) or (t[k] == " ")
            # the mark sits inside a quotation that closes right here -> the sentence
            # ends with the quotation ("... all set." Then ...)
            _closes = k > i + 1
            if ends and (not depth_q or _closes):
                after = t[k + 1:k + 2]
                # not a boundary when: known abbreviation, single initial (J. Smith),
                # or a numbered citation fragment continuing in lower case
                if tok in _ABBR_SET or (len(tok) == 1 and tok.isalpha()):
                    pass
                elif after and (after.islower() or after in "\u00a7&%/"):
                    pass
                elif after and after.isdigit() and tok.replace(".", "").isalpha():
                    pass          # "Pa. 1957" / "Supp. 2d 520" / "Pa.R.P.C. 3.3" (dotted chains too)
                else:
                    out.append(t[start:k].strip())
                    start = k + 1
        i += 1
    if start < n:
        tail = t[start:].strip()
        if tail:
            out.append(tail)
    return [s for s in out if s]


def first_sentence(text: str) -> str:
    s = split_sentences(text)
    return s[0] if s else ""
