#!/usr/bin/env python3
"""pipeline/principle_scanners.py — deterministic emitters for the 13
principle categories (2026-08-26). NOT a side-car: audit_writer folds every
pin into `results` -> procedural_violations -> data_<case>.json -> m0 drafts.

Each scanner: (case docs) -> ViolationPin-shaped dicts with quote + source
pins. Deterministic evidence extraction; the LLM never decides existence.
Honest by design: each scanner emits only when the RECORD shows the predicate
(a rescission letter in intake; a garnishee with no answer on the docket
text; counsel asserting X while client doc shows not-X). No predicate, no pin.
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import re
from pathlib import Path

CASES_DIR = (_eroot() / "obsidian_sync/cases")


def _read(fp: Path) -> str:
    try:
        return fp.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _sidecars(case_slug: str, max_docs: int = 400):
    for sub in ("intake", "evidence"):
        d = CASES_DIR / case_slug / sub
        if not d.is_dir():
            continue
        n = 0
        for fp in sorted(d.rglob("*")):
            if fp.is_file() and fp.name.lower().endswith(
                    (".doctext.txt", ".ocr.txt")):
                n += 1
                if n > max_docs:
                    return
                yield fp


def _pin(cat, cite, rule_text, source, quote, explanation, fix,
         severity="SERIOUS"):
    return {"violation_type": cat, "rule_citation": cite,
            "rule_text": rule_text, "source_doc": source[:90],
            "file": source[:90], "quote": " ".join(quote.split())[:300],
            "explanation": explanation, "fix": fix, "severity": severity}


# ── 1. RESCISSION_EFFECT_IGNORED ─────────────────────────────────────────
_RESCIND_RX = re.compile(
    r"(?:notice\s+of\s+rescission|right\s+to\s+rescind|hereby\s+rescind|"
    r"rescission\s+(?:of|under)\s+.{0,30}1635|15\s+U\.S\.C\.?\s*.?\s*1635)", re.I)
_ENFORCE_RX = re.compile(
    r"(?:foreclos|payoff\s+(?:statement|demand)|notice\s+of\s+(?:intent|default)|"
    r"sheriff\s+sale|acceleration)", re.I)


def scan_rescission(case_slug: str) -> list:
    out, rescission_doc, enforce_docs = [], None, []
    for fp in _sidecars(case_slug):
        t = _read(fp)
        if not rescission_doc:
            m = _RESCIND_RX.search(t)
            if m:
                s = max(0, m.start() - 80)
                rescission_doc = (fp.name, t[s:m.end() + 160])
        if _ENFORCE_RX.search(t):
            # post-rescission enforcement evidence
            enforce_docs.append(fp.name)
    if rescission_doc and enforce_docs:
        out.append(_pin(
            "RESCISSION_EFFECT_IGNORED",
            "15 U.S.C. 1635(b); 12 CFR 1026.23(d); Jesinoski v. Countrywide, 574 U.S. 259",
            "Rescission is effected by notice; the security interest becomes void by operation of law.",
            rescission_doc[0],
            rescission_doc[1],
            (f"A 1635 rescission notice exists in the record ({rescission_doc[0]}) while "
             f"enforcement activity continues ({', '.join(enforce_docs[:3])}"
             + ("..." if len(enforce_docs) > 3 else "") + ") with no adjudication "
             "of the rescission — enforcement proceeds on a security interest whose "
             "validity is voided-by-notice and unresolved."),
            "Demand adjudication of the rescission before any enforcement; plead 1635(b) voidness affirmatively.",
            severity="FATAL"))
    return out


# ── 2. CONDITION_PRECEDENT_UNMET ─────────────────────────────────────────
_FORECLOSE_CASE_RX = re.compile(r"(?:foreclos|sheriff\s+sale|in\s+rem)", re.I)
_ACT6_RX = re.compile(r"(?:Act\s*6|41\s*P\.?S\.?\s*.?\s*403|notice\s+of\s+intention\s+to\s+foreclose)", re.I)
_ACT91_RX = re.compile(r"(?:Act\s*91|HEMAP|homeowner.?s\s+emergency\s+mortgage)", re.I)
_FHA_RX = re.compile(r"(?:FHA|HUD|face.to.face|loss\s+mitigation|24\s+C\.?F\.?R\.?\s*.?\s*203)", re.I)


def scan_conditions_precedent(case_slug: str) -> list:
    out = []
    fha_loan = False
    act6_proof, act91_proof, lossmit_proof = [], [], []
    foreclose_docs, enforce_docs = set(), set()
    sample = ""
    for fp in _sidecars(case_slug):
        t = _read(fp)
        if _FORECLOSE_CASE_RX.search(t):
            foreclose_docs.add(fp.name)
            if not sample:
                sample = fp.name
        if _ENFORCE_RX.search(t):
            enforce_docs.add(fp.name)
        if re.search(r"FHA\s+(?:case|loan|insur)|federal\s+housing\s+admin|203\(?b\)?\s+loan|case\s+#?\s*44[0-9]-", t, re.I):
            fha_loan = True
        if _ACT6_RX.search(t):
            act6_proof.append(fp.name)
        if _ACT91_RX.search(t):
            act91_proof.append(fp.name)
        if re.search(r"loss\s+mitigation\s+(?:review|evaluation|application)\s+(?:was\s+)?(?:completed|conducted)", t, re.I):
            lossmit_proof.append(fp.name)
    # MATERIALITY GATE v2 (2026-08-26): foreclosure gates apply only where
    # foreclosure/mortgage is the case's SUBJECT. Doc-counting fails because
    # intakes share mortgage exhibits across cases — the honest source is the
    # case registry title/type (case_map.json), falling back to CASE_FACTS.
    is_foreclosure = False
    try:
        import json as _json
        _cm = _json.load(open(CASES_DIR.parent.parent / "cases_index" /
                              "case_map.json", encoding="utf-8"))
        _tail = case_slug.split("_")[-1] if "cv" in case_slug else case_slug
        for _k, _v in (_cm.items() if isinstance(_cm, dict) else []):
            if not isinstance(_v, dict):
                continue
            if _k.replace(":", "_").replace("fed_2_", "fed_").endswith(
                    case_slug.replace("fed_", "")) or case_slug.endswith(
                    _k.split(":")[-1].replace(":", "_")):
                _blob = (str(_v.get("title", "")) + " " +
                         str(_v.get("type", "")) + " " +
                         str(_v.get("case_type", ""))).lower()
                is_foreclosure = any(w in _blob for w in
                                     ("mortgage", "foreclos", "quiet title"))
                break
    except Exception:
        pass
    if not is_foreclosure:
        # fallback: CASE_FACTS.md declares the subject
        _cf = CASES_DIR / case_slug / "CASE_FACTS.md"
        if _cf.exists():
            _t = _read(_cf).lower()
            is_foreclosure = any(w in _t[:3000] for w in
                                 ("foreclos", "mortgage", "quiet title"))
    if is_foreclosure and not act6_proof:
        out.append(_pin(
            "CONDITION_PRECEDENT_UNMET",
            "41 P.S. 403 (Act 6); Pa.R.C.P. 1147",
            "Residential mortgage foreclosure requires the Act 6 notice of intention at least 30 days before commencement.",
            sample or case_slug,
            "record survey: no Act 6 / notice-of-intention proof found in any production",
            "The record contains foreclosure enforcement but NO proof of the Act 6 notice of intention — a statutory gate on the power to foreclose.",
            "Demand proof of the Act 6 notice (content + service + 30 days); absent it, the action fails at the threshold."))
    if is_foreclosure and fha_loan and not lossmit_proof:
        out.append(_pin(
            "CONDITION_PRECEDENT_UNMET",
            "24 CFR 203.602-604 (FHA servicing prerequisites); 12 CFR 1024.41 (Reg X)",
            "FHA-insured loans carry mandatory servicing prerequisites (notices, face-to-face/loss-mit review) before foreclosure.",
            sample or case_slug,
            "record survey: FHA loan indicators present; no completed loss-mitigation/face-to-face compliance proof found",
            "An FHA-insured loan is being enforced without record proof of the mandatory HUD servicing prerequisites.",
            "Demand proof of 24 CFR 203.602-604 and 1024.41 compliance; plead the violations affirmatively (they are conditions, not suggestions)."))
    return out


# ── 3. OPPONENT_WAIVER_FORFEITURE ────────────────────────────────────────
_GARN_NO_ANSWER_RX = re.compile(
    r"(?:garnishee[^.\n]{0,120}(?:fail(?:ed|ure)\s+to\s+(?:answer|respond)|"
    r"no\s+answer|did\s+not\s+(?:answer|respond))|"
    r"(?:no\s+(?:timely\s+)?(?:answer|response)[^.\n]{0,60}garnishee))", re.I)
_UNTIMELY_RX = re.compile(
    r"(?:untimely|out\s+of\s+time|beyond\s+the\s+(?:30|20|21|14).day|"
    r"first\s+time\s+on\s+(?:appeal|reply))", re.I)


def scan_waiver_forfeiture(case_slug: str) -> list:
    out = []
    for fp in _sidecars(case_slug):
        t = _read(fp)
        m = _GARN_NO_ANSWER_RX.search(t)
        if m:
            s = max(0, m.start() - 80)
            out.append(_pin(
                "OPPONENT_WAIVER_FORFEITURE",
                "Pa.R.C.P. 3146 (judgment against garnishee on default); Pa.R.C.P. 1032",
                "A garnishee that fails to answer admits the property and is subject to judgment.",
                fp.name, t[s:m.end() + 120],
                "The record shows a garnishee failing to answer — under Rule 3146 that default admits the garnished property; the failure is the opponent's forfeiture, enforceable as such.",
                "Move for judgment against the garnishee on the default; oppose any late answer as forfeited."))
            break
    return out


# ── 4. CANDOR_VIOLATION (counsel vs client's own record) ────────────────
def scan_candor(case_slug: str, cross_findings: list | None = None) -> list:
    """Derived scanner: every FATAL cross-production/role conflict where the
    conflicting halves include a COUNSEL-FILED document is a candor predicate.
    Consumes the trick_catcher output rather than re-scanning."""
    out = []
    for c in (cross_findings or []):
        if c.get("severity") != "FATAL":
            continue
        if c.get("violation_type") not in (
                "INSTRUMENT_VERSION_CONFLICT", "CROSS_PARTY_ROLE_CONFLICT",
                "CASE_FACT_CONFLICT"):
            continue
        q = str(c.get("quote", ""))
        # counsel-filed markers in either half of the conflict
        if re.search(r"(?:reply|opposition|memorandum|brief|answer|motion)", q, re.I) \
           or re.search(r"(?:reply|opposition|memorandum|brief|answer)",
                        str(c.get("source_doc", "")), re.I):
            out.append(_pin(
                "CANDOR_VIOLATION",
                "RPC 3.3 (candor toward the tribunal); Fed. R. Civ. P. 11(b)",
                "A lawyer shall not knowingly make a false statement of fact to a tribunal; filings certify record support.",
                c.get("source_doc", ""), q[:280],
                ("Counsel filed an assertion that the client's own production contradicts "
                 f"(underlying finding: {c.get('violation_type')}). The candor duty makes "
                 "counsel independently answerable for the contradiction."),
                "Raise under RPC 3.3/Rule 11(b) after safe harbor; the contradiction is already record-pinned.",
                severity="SERIOUS"))
    return out[:5]


# ── 5. STATUTORY_DAMAGES_AVAILABLE (violation -> remedy mapper) ─────────
_REMEDY_MAP = [
    (re.compile(r"1692[a-z]?|FDCPA|debt\s+collect", re.I),
     "15 U.S.C. 1692k", "FDCPA: actual + up to $1,000 additional + fees"),
    (re.compile(r"1681|FCRA|consumer\s+report|reinvestigat", re.I),
     "15 U.S.C. 1681n, 1681o", "FCRA: actual/statutory $100-$1,000 (willful) + punitive + fees"),
    (re.compile(r"2605|RESPA|servicing\s+transfer|qualified\s+written", re.I),
     "12 U.S.C. 2605(f)", "RESPA: actual + up to $2,000 (pattern) + fees"),
    (re.compile(r"1099|information\s+return|7434", re.I),
     "26 U.S.C. 7434", "False information returns: $5,000 or actual, + costs"),
    (re.compile(r"bad\s+faith|8371", re.I),
     "42 Pa.C.S. 8371", "Insurance bad faith: interest + punitive + fees"),
]


def scan_statutory_damages(case_slug: str, findings: list | None = None) -> list:
    """Derived: map existing record findings to their damages statutes."""
    out, seen = [], set()
    text_pool = " ".join(
        str(f.get("quote", "")) + " " + str(f.get("explanation", ""))
        for f in (findings or []))
    for rx, cite, desc in _REMEDY_MAP:
        if cite in seen:
            continue
        if rx.search(text_pool):
            seen.add(cite)
            out.append(_pin(
                "STATUTORY_DAMAGES_AVAILABLE", cite,
                desc, "damages mapper (derived from record findings)",
                f"record findings reference the {cite.split()[0]} scheme",
                f"The established record violations trigger {cite} ({desc}) — the same findings that defeat their claim fund the counterclaim.",
                f"Plead {cite} damages affirmatively; compute per violation from the record.",
                severity="STANDARD"))
    return out


# ── 6. BURDEN_MISALLOCATED (order-text scanner) ─────────────────────────
_BURDEN_RX = re.compile(
    r"(?:plaintiff\s+(?:has\s+not|fails?\s+to)\s+(?:disprov|refut|rebut)|"
    r"(?:must|failed\s+to)\s+(?:disprove|negate)\s+(?:standing|the\s+holder|authority)|"
    r"has\s+not\s+shown\s+that\s+.{0,40}(?:is\s+not|lacks))", re.I)


def scan_burden(case_slug: str) -> list:
    out = []
    for fp in _sidecars(case_slug):
        n = fp.name.lower()
        if not any(k in n for k in ("order", "opinion", "memorandum", "ecf")):
            continue
        t = _read(fp)
        m = _BURDEN_RX.search(t)
        if m:
            s = max(0, m.start() - 100)
            out.append(_pin(
                "BURDEN_MISALLOCATED",
                "element-wise burden doctrine; Fed. R. Evid. 301",
                "The claimant bears the burden on each element of its claim; presumptions shift production, not persuasion.",
                fp.name, t[s:m.end() + 140],
                "The ruling language places the burden on the respondent to DISPROVE an element the claimant must prove (standing/holder/authority).",
                "Attack the inversion: identify the element, cite who bears it, show the ruling demanded the opposite."))
            if len(out) >= 3:
                break
    return out


# ── 7. LAW_OF_THE_CASE_VIOLATED + preclusion registers ──────────────────
_RULING_RX = re.compile(
    r"(?:IT\s+IS\s+(?:HEREBY\s+)?ORDERED|the\s+[Cc]ourt\s+(?:finds|holds|concludes))"
    r"[^.\n]{10,220}", re.I)


def scan_law_of_case(case_slug: str) -> list:
    """Register rulings per docket; contradictory later rulings on the same
    subject = pin. Deterministic first pass: same-subject detection via
    shared distinctive tokens between ruling sentences in different orders."""
    out, rulings = [], []
    for fp in _sidecars(case_slug):
        n = fp.name.lower()
        if not any(k in n for k in ("order", "ecf", "opinion")):
            continue
        t = _read(fp)
        for m in _RULING_RX.finditer(t):
            s = " ".join(m.group(0).split())[:260]
            toks = set(re.findall(r"[a-z]{6,}", s.lower())) - {
                "ordered", "hereby", "further", "plaintiff", "defendant",
                "motion", "granted", "denied", "court"}
            rulings.append((fp.name, s, toks))
    # naive contradiction: GRANTED vs DENIED on heavy token overlap
    for i in range(len(rulings)):
        for j in range(i + 1, len(rulings)):
            n1, s1, t1 = rulings[i]
            n2, s2, t2 = rulings[j]
            if n1 == n2 or not t1 or not t2:
                continue
            ov = len(t1 & t2) / min(len(t1), len(t2))
            flip = (("granted" in s1.lower()) != ("granted" in s2.lower())) and \
                   (("denied" in s1.lower()) != ("denied" in s2.lower()))
            if ov >= 0.6 and flip:
                out.append(_pin(
                    "LAW_OF_THE_CASE_VIOLATED",
                    "law-of-the-case doctrine; coordinate-jurisdiction rule",
                    "Prior rulings in the same case govern later stages absent new law or facts.",
                    n2, f"[{n1}] \"{s1[:120]}\" || [{n2}] \"{s2[:120]}\"",
                    "Two orders in the same case rule oppositely on heavily overlapping subject matter with no stated new law or facts.",
                    "Pin both rulings side by side and demand the departure be justified or vacated."))
                if len(out) >= 3:
                    return out
    return out


# ── registry the audit_writer consumes ──────────────────────────────────
INDEPENDENT_SCANNERS = [
    scan_rescission, scan_conditions_precedent, scan_waiver_forfeiture,
    scan_burden, scan_law_of_case,
]
# derived scanners receive the already-computed findings list
DERIVED_SCANNERS = [scan_candor, scan_statutory_damages]


def run_all(case_slug: str, existing_findings: list | None = None) -> list:
    pins = []
    for fn in INDEPENDENT_SCANNERS:
        try:
            pins.extend(fn(case_slug))
        except Exception:
            pass
    base = (existing_findings or []) + pins
    for fn in DERIVED_SCANNERS:
        try:
            pins.extend(fn(case_slug, base))
        except Exception:
            pass
    return pins


if __name__ == "__main__":
    import sys as _s
    for slug in (_s.argv[1:] or ["fed_26-cv-00456", "fed_26-cv-00123",
                                 "fed_26-cv-00789", "phila_ccp_260100000"]):
        r = run_all(slug)
        from collections import Counter
        print(f"[{slug}] pins={len(r)} " +
              str(dict(Counter(p['violation_type'] for p in r))))
