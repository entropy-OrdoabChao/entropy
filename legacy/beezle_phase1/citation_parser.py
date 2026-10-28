#!/usr/bin/env python3
"""
citation_parser.py — Phase 1

Parses legal citations into structured fields. Pure module — no I/O, no state.
Imported by backfill_namespace.py and phase1_verify.py.

Supported forms:
  Statutes:   26 USC § 6330(b)(2)
              26 U.S.C. § 6330
              26 U.S.C.A. § 6330(a)
              42 USC §§ 1981-1988          (range, takes first)
              15 U.S.C. § 1681i

  Regulations: 26 CFR § 301.6330-1
               12 CFR 226.5
               31 CFR 285.5(d)(8)

  Federal cases:  410 U.S. 113
                  403 U.S. 388 (1971)
                  734 F.2d 132
                  941 F.3d 224 (3d Cir. 1991)
                  229 F. Supp. 647
                  229 F.Supp.2d 647

  State cases:    50 A.2d 100  (Atlantic Reporter for PA)
                  789 N.E.2d 1
                  500 P.3d 200

  PA statutes:    25 Pa.Stat.Ann. § 2776
                  Pa.R.C.P. 1028(a)(2)
                  Pa.R.C.P. No. 244

Output:
  Citation dataclass with .namespace, .canonical, .tier, .branch, .path_parts.
  Returns None on unparseable input.

Acceptance test (run python3 citation_parser.py --selftest):
  Title 26 USC vs Title 26 CFR produce DIFFERENT namespaces.
"""
import re
import sys
import argparse
from dataclasses import dataclass, field, asdict
from typing import Optional


# ============================================================
# REPORTER CATALOG
# ============================================================
# Each entry: (regex_for_reporter, normalized_code, namespace_prefix, tier, branch, court_hint)
# Order matters: more specific patterns first.
REPORTER_PATTERNS = [
    # ---- Federal codes ----
    {
        "id": "usc",
        "regex": r"U\.?\s*S\.?\s*C\.?\s*A?\.?",  # USC, U.S.C., U.S.C.A.
        "kind": "statute",
        "namespace_template": "usc_t{title}",
        "tier": 1,
        "branch": "federal",
        "track": "na",
    },
    {
        "id": "cfr",
        "regex": r"C\.?\s*F\.?\s*R\.?",  # CFR, C.F.R.
        "kind": "regulation",
        "namespace_template": "cfr_t{title}",
        "tier": 2,
        "branch": "federal",
        "track": "na",
    },
    # ---- Federal case reporters ----
    {
        "id": "us",
        "regex": r"U\.?\s*S\.?",  # U.S., US  (BARE — only after volume+page check)
        "kind": "case",
        "namespace_template": "case",
        "tier": 3,
        "branch": "federal",
        "court_level": "scotus",
        "track": "art_iii",
    },
    {
        "id": "sct",
        "regex": r"S\.?\s*Ct\.?",
        "kind": "case",
        "namespace_template": "case",
        "tier": 3,
        "branch": "federal",
        "court_level": "scotus",
        "track": "art_iii",
    },
    {
        "id": "led",
        "regex": r"L\.?\s*Ed\.?\s*2?d?",
        "kind": "case",
        "namespace_template": "case",
        "tier": 3,
        "branch": "federal",
        "court_level": "scotus",
        "track": "art_iii",
    },
    {
        "id": "fsupp",
        "regex": r"F\.?\s*Supp\.?\s*(?:2d|3d)?",  # F.Supp., F.Supp.2d, F. Supp. 3d
        "kind": "case",
        "namespace_template": "case",
        "tier": 3,
        "branch": "federal",
        "court_level": "district",
        "track": "art_iii",
    },
    {
        "id": "fed_reporter",
        "regex": r"F\.?\s*(?:2d|3d|4th)?",   # F., F.2d, F.3d, F.4th
        "kind": "case",
        "namespace_template": "case",
        "tier": 3,
        "branch": "federal",
        "court_level": "circuit",
        "track": "art_iii",
    },
    # ---- State case reporters (most common; expand as needed) ----
    {
        "id": "atlantic",
        "regex": r"A\.?\s*(?:2d|3d)?",  # A., A.2d, A.3d  (PA, NJ, etc.)
        "kind": "case",
        "namespace_template": "case",
        "tier": 7,
        "branch": "state",
        "court_level": "state_appellate",
        "track": "art_iii",
    },
    {
        "id": "northeastern",
        "regex": r"N\.?\s*E\.?\s*(?:2d|3d)?",
        "kind": "case",
        "namespace_template": "case",
        "tier": 7,
        "branch": "state",
        "court_level": "state_appellate",
        "track": "art_iii",
    },
    {
        "id": "pacific",
        "regex": r"P\.?\s*(?:2d|3d)?",
        "kind": "case",
        "namespace_template": "case",
        "tier": 7,
        "branch": "state",
        "court_level": "state_appellate",
        "track": "art_iii",
    },
    {
        "id": "southeastern",
        "regex": r"S\.?\s*E\.?\s*(?:2d|3d)?",
        "kind": "case",
        "namespace_template": "case",
        "tier": 7,
        "branch": "state",
        "court_level": "state_appellate",
        "track": "art_iii",
    },
    {
        "id": "southern",
        "regex": r"So\.?\s*(?:2d|3d)?",
        "kind": "case",
        "namespace_template": "case",
        "tier": 7,
        "branch": "state",
        "court_level": "state_appellate",
        "track": "art_iii",
    },
    {
        "id": "southwestern",
        "regex": r"S\.?\s*W\.?\s*(?:2d|3d)?",
        "kind": "case",
        "namespace_template": "case",
        "tier": 7,
        "branch": "state",
        "court_level": "state_appellate",
        "track": "art_iii",
    },
    {
        "id": "northwestern",
        "regex": r"N\.?\s*W\.?\s*(?:2d|3d)?",
        "kind": "case",
        "namespace_template": "case",
        "tier": 7,
        "branch": "state",
        "court_level": "state_appellate",
        "track": "art_iii",
    },
]

# Compile a master regex that captures ANY of the above
# Pattern: <volume:\d+>  <reporter>  <page:\d+>
_REPORTER_RE = re.compile(
    r"\b(?P<volume>\d{1,4})\s+"
    r"(?P<reporter>"
    + r"|".join(p["regex"] for p in REPORTER_PATTERNS) +
    r")\s*"
    r"(?P<page>\d{1,5})\b",
    re.IGNORECASE,
)

# Statute citation pattern (separate from case-reporter pattern):
#   <title>  <code>  §? <section>  (<subsec>)? (<para>)? (<subpara>)? (<clause>)?
# Match USC/CFR/USCA explicitly. Section can be alphanumeric (e.g. "301.6330-1" for CFR).
_STATUTE_RE = re.compile(
    r"""
    (?P<title>\d{1,2})\s+
    (?P<code>U\.?\s*S\.?\s*C\.?\s*A?\.?|C\.?\s*F\.?\s*R\.?)\s*
    (?:§{1,2}|sec(?:tion|s)?\.?)?\s*
    (?P<section>[\w]+(?:[.\-][\w]+)*)
    (?:\s*\(\s*(?P<subsec>[a-zA-Z])\s*\))?
    (?:\s*\(\s*(?P<para>\d+)\s*\))?
    (?:\s*\(\s*(?P<subpara>[A-Z])\s*\))?
    (?:\s*\(\s*(?P<clause>[ivxIVX]+)\s*\))?
    """,
    re.IGNORECASE | re.VERBOSE,
)

# PA-specific patterns
_PA_STATUTE_RE = re.compile(
    r"""
    (?P<title>\d{1,3})\s+
    (?P<code>Pa\.?\s*Stat\.?\s*Ann?\.?|Pa\.?\s*Cons\.?\s*Stat\.?\s*Ann?\.?|Pa\.?\s*C\.?\s*S\.?\s*A?\.?)\s*
    (?:§{1,2}|sec(?:tion|s)?\.?)?\s*
    (?P<section>[\w.\-]+)
    """,
    re.IGNORECASE | re.VERBOSE,
)

_PA_RULE_RE = re.compile(
    r"""
    Pa\.?\s*R\.?\s*(?P<rule_type>C\.?P\.?|Crim\.?P\.?|App\.?P\.?|E(?:vid)?\.?)\s*
    (?:No\.?\s*)?
    (?P<rule_num>[\d.]+)
    (?:\s*\(\s*(?P<subsec>[a-zA-Z])\s*\))?
    (?:\s*\(\s*(?P<para>\d+)\s*\))?
    """,
    re.IGNORECASE | re.VERBOSE,
)


# ============================================================
# DATACLASS
# ============================================================
@dataclass
class Citation:
    raw: str
    kind: str               # statute | regulation | case | pa_statute | pa_rule
    title: Optional[str] = None       # e.g. "26"
    code: Optional[str] = None        # e.g. "usc" | "cfr"
    section: Optional[str] = None     # e.g. "6330" or "301.6330-1"
    subsec: str = ""
    para: str = ""
    subpara: str = ""
    clause: str = ""
    volume: Optional[str] = None      # for case cites
    reporter: Optional[str] = None    # normalized: "us"|"sct"|"fed_reporter"|...
    page: Optional[str] = None        # for case cites
    namespace: str = ""               # usc_t26 | cfr_t26 | case | pa_stat_t25 | pa_rcp ...
    canonical: str = ""               # human-readable normalized form
    tier: int = 0
    branch: str = "unknown"           # federal | state
    court_level: str = ""             # scotus | circuit | district | state_*
    track: str = "na"                 # art_i | art_iii | na
    confidence: float = 0.0           # 0-1
    parser_method: str = ""           # statute_re | pa_statute_re | pa_rule_re | reporter_re

    def to_dict(self) -> dict:
        return asdict(self)


# ============================================================
# NORMALIZERS
# ============================================================
def _normalize_code(raw_code: str) -> str:
    """Normalize 'U. S. C. A.' → 'usc', 'C.F.R.' → 'cfr'."""
    s = re.sub(r"[\s.]", "", raw_code).upper()
    if s in ("USC", "USCA"):
        return "usc"
    if s == "CFR":
        return "cfr"
    return s.lower()


def _normalize_reporter(raw_reporter: str) -> Optional[dict]:
    """Match raw reporter text against REPORTER_PATTERNS, return the matched entry."""
    norm = re.sub(r"\s+", "", raw_reporter).upper()
    for p in REPORTER_PATTERNS:
        # Build a strict no-whitespace version of the pattern for matching
        strict_pat = re.sub(r"\\s\*", "", p["regex"])
        if re.fullmatch(strict_pat, norm, re.IGNORECASE):
            return p
    return None


def _section_parts(section: str) -> list[tuple[int, str, str]]:
    """Split '301.6330-1' into [(2, '§ 301', '301'), (3, '.6330', '6330'), (4, '-1', '1')] etc."""
    parts = []
    # Split on dots and dashes, keep separators in the label
    chunks = re.split(r"([.\-])", section)
    depth = 2
    acc_label = "§ "
    for c in chunks:
        if c in ".-":
            acc_label = c
            continue
        if c:
            parts.append((depth, f"{acc_label}{c}", c))
            depth += 1
            acc_label = ""
    return parts


# ============================================================
# PARSERS
# ============================================================
def _parse_statute(text: str) -> Optional[Citation]:
    m = _STATUTE_RE.search(text)
    if not m:
        return None
    code_norm = _normalize_code(m["code"])
    if code_norm not in ("usc", "cfr"):
        return None

    kind = "statute" if code_norm == "usc" else "regulation"
    namespace = f"{code_norm}_t{m['title']}"
    tier = 1 if code_norm == "usc" else 2

    # Canonical: "26 USC § 6330(b)(2)"
    canonical_parts = [m["title"], "USC" if code_norm == "usc" else "CFR", "§", m["section"]]
    for x in (m["subsec"], m["para"], m["subpara"], m["clause"]):
        if x: canonical_parts.append(f"({x})")
    # Tighter join: ['26','USC','§','6330','(b)','(2)'] → '26 USC § 6330(b)(2)'
    canonical = f"{m['title']} {canonical_parts[1]} § {m['section']}"
    for x in (m["subsec"], m["para"], m["subpara"], m["clause"]):
        if x: canonical += f"({x})"

    # Confidence: higher when more subsection depth captured
    captured = sum(1 for v in (m["subsec"], m["para"], m["subpara"], m["clause"]) if v)
    confidence = 0.7 + 0.05 * captured

    return Citation(
        raw=text.strip(),
        kind=kind,
        title=m["title"],
        code=code_norm,
        section=m["section"],
        subsec=m["subsec"] or "",
        para=m["para"] or "",
        subpara=m["subpara"] or "",
        clause=m["clause"] or "",
        namespace=namespace,
        canonical=canonical,
        tier=tier,
        branch="federal",
        track="na",
        confidence=confidence,
        parser_method="statute_re",
    )


def _parse_pa_statute(text: str) -> Optional[Citation]:
    m = _PA_STATUTE_RE.search(text)
    if not m:
        return None
    code_compact = re.sub(r"[\s.]", "", m["code"]).upper()
    canonical = f"{m['title']} Pa.C.S. § {m['section']}"
    namespace = f"pa_stat_t{m['title']}"
    return Citation(
        raw=text.strip(),
        kind="pa_statute",
        title=m["title"],
        code="pa_stat",
        section=m["section"],
        namespace=namespace,
        canonical=canonical,
        tier=6,
        branch="state",
        track="na",
        confidence=0.75,
        parser_method="pa_statute_re",
    )


def _parse_pa_rule(text: str) -> Optional[Citation]:
    m = _PA_RULE_RE.search(text)
    if not m:
        return None
    rt = re.sub(r"[\s.]", "", m["rule_type"]).upper()  # CP, CRIMP, APPP, E, EVID
    if rt == "CP":
        ns = "pa_rcp"
        canonical_code = "Pa.R.C.P."
    elif rt == "CRIMP":
        ns = "pa_rcrimp"
        canonical_code = "Pa.R.Crim.P."
    elif rt == "APPP":
        ns = "pa_rapp"
        canonical_code = "Pa.R.App.P."
    elif rt in ("E", "EVID"):
        ns = "pa_re"
        canonical_code = "Pa.R.E."
    else:
        ns = "pa_rule"
        canonical_code = f"Pa.R.{rt}"

    canonical = f"{canonical_code} {m['rule_num']}"
    if m["subsec"]: canonical += f"({m['subsec']})"
    if m["para"]:   canonical += f"({m['para']})"
    return Citation(
        raw=text.strip(),
        kind="pa_rule",
        section=m["rule_num"],
        subsec=m["subsec"] or "",
        para=m["para"] or "",
        namespace=ns,
        canonical=canonical,
        tier=6,
        branch="state",
        track="art_iii",
        confidence=0.78,
        parser_method="pa_rule_re",
    )


def _parse_reporter(text: str) -> Optional[Citation]:
    m = _REPORTER_RE.search(text)
    if not m:
        return None
    reporter_entry = _normalize_reporter(m["reporter"])
    if not reporter_entry:
        return None

    volume, page = m["volume"], m["page"]
    raw_reporter = m["reporter"].strip()

    # Build canonical: "5 U.S. 137", "941 F.3d 224", "229 F. Supp. 647"
    canonical = f"{volume} {raw_reporter} {page}"

    return Citation(
        raw=text.strip(),
        kind="case",
        volume=volume,
        reporter=reporter_entry["id"],
        page=page,
        namespace=reporter_entry["namespace_template"],  # always "case"
        canonical=canonical,
        tier=reporter_entry["tier"],
        branch=reporter_entry["branch"],
        court_level=reporter_entry.get("court_level", ""),
        track=reporter_entry["track"],
        confidence=0.72,
        parser_method="reporter_re",
    )


# ============================================================
# PUBLIC API
# ============================================================
def parse(text: str) -> Optional[Citation]:
    """
    Try each parser in priority order. First successful parse wins.
    Returns None if nothing matches.
    """
    if not text:
        return None
    text = text.strip()
    # Statute is most specific (requires § or "section" or numbered subsections),
    # try it before case reporter which is greedy on "U.S."
    for fn in (_parse_statute, _parse_pa_statute, _parse_pa_rule, _parse_reporter):
        c = fn(text)
        if c:
            return c
    return None


def parse_all(text: str, max_cites: int = 20) -> list[Citation]:
    """
    Find all citations in a longer block of text. Useful for case opinions
    citing many authorities. Returns deduplicated list by canonical form.
    """
    found = []
    seen = set()
    for fn in (_parse_statute, _parse_pa_statute, _parse_pa_rule, _parse_reporter):
        pattern_map = {
            _parse_statute: _STATUTE_RE,
            _parse_pa_statute: _PA_STATUTE_RE,
            _parse_pa_rule: _PA_RULE_RE,
            _parse_reporter: _REPORTER_RE,
        }
        regex = pattern_map[fn]
        for m in regex.finditer(text):
            sub = text[max(0, m.start()-10):m.end()+10]
            c = fn(sub)
            if c and c.canonical not in seen:
                seen.add(c.canonical)
                found.append(c)
                if len(found) >= max_cites:
                    return found
    return found


def path_parts(c: Citation) -> list[tuple[int, str, str]]:
    """
    Hierarchical path for authority_path table.
    Returns [(depth, label, code), ...]
    """
    if c.kind in ("statute", "regulation"):
        out = [
            (0, f"Title {c.title}", c.title or ""),
            (1, "USC" if c.code == "usc" else "CFR", c.code or ""),
        ]
        if c.section:
            out.extend(_section_parts(c.section))
        depth = max(p[0] for p in out) + 1
        for v in (c.subsec, c.para, c.subpara, c.clause):
            if v:
                out.append((depth, f"({v})", v))
                depth += 1
        return out
    if c.kind == "case":
        return [
            (0, c.branch, c.branch),
            (1, c.court_level or "case", c.court_level or "case"),
            (2, f"{c.volume} {c.reporter}", c.reporter or ""),
            (3, c.page or "", c.page or ""),
        ]
    if c.kind == "pa_statute":
        return [
            (0, "Pennsylvania", "pa"),
            (1, f"Title {c.title}", c.title or ""),
            (2, f"§ {c.section}", c.section or ""),
        ]
    if c.kind == "pa_rule":
        return [
            (0, "Pennsylvania", "pa"),
            (1, c.namespace, c.namespace),
            (2, c.section or "", c.section or ""),
        ]
    return []


# ============================================================
# SELF-TEST — run with: python3 citation_parser.py --selftest
# ============================================================
SELFTEST_CASES = [
    # (input, expected_namespace, expected_kind, must_contain_in_canonical)
    ("26 USC § 6330(b)(2)",            "usc_t26", "statute",    "26 USC § 6330(b)(2)"),
    ("26 U.S.C. § 6330",               "usc_t26", "statute",    "26 USC § 6330"),
    ("26 U.S.C.A. § 6330(a)",          "usc_t26", "statute",    "26 USC § 6330(a)"),
    ("26 CFR § 301.6330-1",            "cfr_t26", "regulation", "26 CFR § 301.6330-1"),
    ("26 C.F.R. § 301.6330-1",         "cfr_t26", "regulation", "26 CFR § 301.6330-1"),
    ("12 CFR 226.5",                   "cfr_t12", "regulation", "12 CFR § 226.5"),
    ("15 U.S.C. § 1681i",              "usc_t15", "statute",    "15 USC § 1681i"),
    ("42 U.S.C. § 1983",               "usc_t42", "statute",    "42 USC § 1983"),
    ("31 CFR 285.5(d)(8)",             "cfr_t31", "regulation", "31 CFR § 285.5"),
    # Cases
    ("Marbury v. Madison, 5 U.S. 137 (1803)",        "case", "case", "5 U.S. 137"),
    ("Stump v. Sparkman, 435 U.S. 349 (1978)",       "case", "case", "435 U.S. 349"),
    ("Forrester v. White, 484 U.S. 219 (1988)",      "case", "case", "484 U.S. 219"),
    ("Mireles v. Waco, 502 U.S. 9 (1991)",           "case", "case", "502 U.S. 9"),
    ("Trinsey v. Pagliaro, 229 F. Supp. 647",        "case", "case", "229 F. Supp. 647"),
    ("Trinsey v. Pennsylvania, 941 F.2d 224 (3d Cir. 1991)", "case", "case", "941 F.2d 224"),
    ("Clearfield Trust Co. v. United States, 318 U.S. 363 (1943)", "case", "case", "318 U.S. 363"),
    # PA
    ("Pa.R.C.P. 1028(a)(2)",           "pa_rcp", "pa_rule",  "Pa.R.C.P. 1028(a)(2)"),
    ("25 Pa.Stat.Ann. § 2776",         "pa_stat_t25", "pa_statute", "25 Pa.C.S. § 2776"),
    # The critical disambiguation test
    # ↓↓ These two MUST produce different namespaces ↓↓
]


def run_selftest() -> int:
    """Returns nonzero exit if any test fails. Prints details."""
    n_pass, n_fail = 0, 0
    fails = []
    print(f"Running {len(SELFTEST_CASES)} parser self-tests ...\n")
    for i, (input_text, exp_ns, exp_kind, must_contain) in enumerate(SELFTEST_CASES, 1):
        c = parse(input_text)
        if c is None:
            n_fail += 1
            fails.append(f"  [{i:2}] FAIL (no parse): {input_text!r}")
            continue
        problems = []
        if c.namespace != exp_ns:
            problems.append(f"namespace={c.namespace!r} expected={exp_ns!r}")
        if c.kind != exp_kind:
            problems.append(f"kind={c.kind!r} expected={exp_kind!r}")
        if must_contain not in c.canonical:
            problems.append(f"canonical={c.canonical!r} missing {must_contain!r}")
        if problems:
            n_fail += 1
            fails.append(f"  [{i:2}] FAIL: {input_text!r}\n        " + "\n        ".join(problems))
        else:
            n_pass += 1
            print(f"  [{i:2}] OK   {input_text!r:60} → {c.namespace:14} {c.canonical}")

    # CRITICAL DISAMBIGUATION TEST
    print("\nCritical Title 26 USC vs CFR disambiguation test:")
    c1 = parse("26 USC § 6330(b)(2)")
    c2 = parse("26 CFR § 301.6330-1")
    if c1 and c2 and c1.namespace != c2.namespace:
        print(f"  PASS: usc namespace={c1.namespace!r}, cfr namespace={c2.namespace!r}")
        n_pass += 1
    else:
        n_fail += 1
        msg = f"  FAIL: usc namespace={c1.namespace if c1 else None!r}, cfr namespace={c2.namespace if c2 else None!r}"
        print(msg)
        fails.append(msg)

    print(f"\n{'='*60}")
    print(f"RESULT: {n_pass} pass, {n_fail} fail ({n_pass*100/(n_pass+n_fail):.1f}%)")
    if fails:
        print("\nFAILURES:")
        for f in fails:
            print(f)
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true", help="Run parser self-tests")
    ap.add_argument("--parse", help="Parse a single citation and print JSON")
    args = ap.parse_args()
    if args.selftest:
        sys.exit(run_selftest())
    if args.parse:
        c = parse(args.parse)
        if c:
            import json
            print(json.dumps(c.to_dict(), indent=2))
            print("\nPath parts:")
            for d, label, code in path_parts(c):
                print(f"  depth={d}  label={label!r}  code={code!r}")
        else:
            print(f"UNPARSEABLE: {args.parse!r}")
            sys.exit(2)
        sys.exit(0)
    ap.print_help()
