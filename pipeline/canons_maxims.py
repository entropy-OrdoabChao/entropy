#!/usr/bin/env python3
"""pipeline/canons_maxims.py — construction canons + equity maxims + mandatory
language: the COMPREHENSION organ (SWEEP 4, 2026-08-26).

THE PRINCIPLE (user-locked): law has RULES and PROCEDURES; the written text
controls. "Interpretation" cannot displace text — statutory command words
mean what they say (shall = mandatory), canons discipline construction when
text needs it, maxims are operative law in equity proceedings, and judgments
already final and unoverturned are FINAL (both directions).

Grounding: definitions/1854_broom (Broom's Legal Maxims), the two equity
treatises, Wright & Miller, the LOCKED includes-rule (obsidian AGENTS.md),
1 Pa.C.S. 1901-1991 (PA Statutory Construction Act) + 1 U.S.C. + Gould.

Three new categories (55-57):
  MANDATORY_LANGUAGE_DISREGARDED  'shall/must' treated as optional
  CONSTRUCTION_CANON_VIOLATED     text displaced by 'interpretation'
  EQUITY_MAXIM_APPLICABLE         maxim controls an equitable proceeding
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import re
from pathlib import Path

CASES_DIR = (_eroot() / "obsidian_sync/cases")

# ═══════════════════════════════════════════════════════════════════════════
# CANONS OF CONSTRUCTION — each with its counter-canon (they hunt in pairs)
# ═══════════════════════════════════════════════════════════════════════════
CANONS = {
    "plain_meaning": {
        "rule": ("Unambiguous text is applied as written — no construction "
                 "permitted. 1 Pa.C.S. 1921(b): when words are clear and free "
                 "from ambiguity, the letter is not to be disregarded under "
                 "the pretext of pursuing its spirit. [corpus: pa_consolidated_statutes]"),
        "abuse": "a court or party 'interprets' away clear text (the pretext clause is the trap 1921(b) names)",
    },
    "means_vs_includes": {
        "rule": ("'Means' = closed class. 'Includes' expands ONLY with an "
                 "expansion clause (26 U.S.C. 7701(c)); otherwise enumerated "
                 "items define the class. [LOCKED 2026-07-05]"),
        "abuse": "reading 'includes' as 'for example, anything similar'",
    },
    "ejusdem_generis": {
        "rule": ("General words following an enumeration take the class of the "
                 "enumerated items. 1 Pa.C.S. 1903(b). [corpus]"),
        "abuse": "stretching a general residual phrase beyond the class",
    },
    "expressio_unius": {
        "rule": ("Expression of one thing excludes others — an enumerated list "
                 "with no residual is exhaustive. [Broom's Maxims; corpus]"),
        "abuse": "adding unenumerated grounds/parties/powers to a closed list",
    },
    "noscitur_a_sociis": {
        "rule": "A word is known by its companions — meaning drawn from surrounding words. [corpus]",
        "abuse": "isolating a word from context to inflate its scope",
    },
    "in_pari_materia": {
        "rule": ("Statutes on the same subject are construed together. "
                 "1 Pa.C.S. 1932. [corpus]"),
        "abuse": "reading one section to nullify its neighbor (e.g. UCC 3-301 read without 3-203/3-205)",
    },
    "specific_over_general": {
        "rule": ("Specific provisions control over general ones. 1 Pa.C.S. "
                 "1933. [corpus]"),
        "abuse": "invoking a general power to escape a specific limitation",
    },
    "strict_construction_penal_tax": {
        "rule": ("Penal and taxing/duty statutes are construed strictly "
                 "against the government, doubt for the citizen. Gould v. "
                 "Gould, 245 U.S. 151. [corpus]"),
        "abuse": "resolving statutory doubt FOR the enforcing authority",
    },
    "remedial_liberal": {
        "rule": ("Remedial statutes (FDCPA, FCRA, TILA, UTPCPL) are construed "
                 "liberally to effect their protective purpose. [corpus: "
                 "consumer-protection provisions]"),
        "abuse": "construing a consumer-protection statute narrowly against the consumer",
    },
    "surplusage": {
        "rule": ("Every word is given effect; no clause is rendered "
                 "surplusage. 1 Pa.C.S. 1921(a) ('every statute shall be "
                 "construed, if possible, to give effect to all its "
                 "provisions'). [corpus]"),
        "abuse": "a reading that makes a clause (e.g. '[Sign Original Only]', 'shall') do nothing",
    },
}

# ═══════════════════════════════════════════════════════════════════════════
# EQUITY MAXIMS — operative law in equitable proceedings (foreclosure, quiet
# title, injunctions). Grounded: Broom's (1854) + the equity treatises.
# ═══════════════════════════════════════════════════════════════════════════
MAXIMS = {
    "clean_hands": {
        "maxim": "He who comes into equity must come with clean hands",
        "operative": ("A claimant with its own inequity in the same "
                      "transaction (inconsistent 1099-C positions, conflicting "
                      "productions, concealed securitization) is barred from "
                      "the equitable remedy outright."),
        "trigger": r"(?:foreclos|quiet\s+title|injunct|equitable\s+relief)",
    },
    "equity_aids_vigilant": {
        "maxim": "Equity aids the vigilant, not those who slumber (laches)",
        "operative": ("Delay + prejudice bars equitable claims — runs against "
                      "enforcers who sat on claimed rights (unrecorded "
                      "assignments surfacing years later) as much as anyone."),
        "trigger": r"(?:laches|delay|years\s+(?:later|after)|untimely)",
    },
    "equity_regards_done": {
        "maxim": "Equity regards as done that which ought to have been done",
        "operative": ("Duties that SHOULD have been performed (termination of "
                      "insurance on cancellation, recording, notice) are "
                      "treated as performed for the wrongdoer's accounting — "
                      "they cannot profit from their own omission."),
        "trigger": r"(?:should\s+have|ought\s+to\s+have|fail(?:ed|ure)\s+to\s+(?:record|terminate|notify))",
    },
    "equity_follows_law": {
        "maxim": "Equity follows the law",
        "operative": ("Equity cannot override statute or written rule — an "
                      "equitable workaround of 13 Pa.C.S. 3301/3205 possession "
                      "requirements is unavailable; the statute controls."),
        "trigger": r"(?:equit(?:y|able)\s+(?:exception|workaround|notwithstanding)|substantial\s+compliance)",
    },
    "no_profit_own_wrong": {
        "maxim": "No one may profit from his own wrong (nullus commodum capere potest de injuria sua propria — Broom's)",
        "operative": ("A party whose own breach/fraud created the situation "
                      "cannot found a claim on it (foreclosing on a default "
                      "the servicer's own misapplication created; claiming "
                      "under an instrument it altered)."),
        "trigger": r"(?:their\s+own\s+(?:breach|error|misapplication)|self.created|manufactured\s+default)",
    },
    "equality_is_equity": {
        "maxim": "Equality is equity; equity delights in equality",
        "operative": ("Asymmetric administration (strict rules for the pro se, "
                      "indulgence for counsel) violates the foundational maxim "
                      "of the forum claiming equitable jurisdiction."),
        "trigger": r"(?:pro\s+se.{0,80}(?:held|strict)|double\s+standard|asymmetr)",
    },
    "substance_over_form": {
        "maxim": "Equity looks to the substance rather than the form",
        "operative": ("Labels do not control: a 'servicing transfer' that is a "
                      "debt sale, a 'verification' that is a photocopy, a "
                      "'holder' that is a database entry — substance decides."),
        "trigger": r"(?:in\s+substance|mere\s+form|label|styled\s+as|denominated)",
    },
}

# ═══════════════════════════════════════════════════════════════════════════
# MANDATORY LANGUAGE — shall means shall
# ═══════════════════════════════════════════════════════════════════════════
# order/opinion text where a mandatory duty is acknowledged then not performed
_MANDATE_DISREGARD_RX = re.compile(
    r"(?:(?:although|while|notwithstanding)[^.\n]{0,80}\bshall\b[^.\n]{0,120}|"
    r"\bshall\b[^.\n]{0,60}(?:is\s+directory|not\s+mandatory|substantial\s+compliance|"
    r"court\s+(?:declines|need\s+not))[^.\n]{0,80})", re.I)
_SHALL_DUTY_RX = re.compile(
    r"(?:court\s+shall|shall\s+(?:disqualify|recuse|take\s+(?:judicial\s+)?notice|"
    r"be\s+construed|give\s+effect|state\s+(?:its|the)\s+reasons|dismiss|remand))", re.I)


def _read(fp):
    try:
        return fp.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _sidecars(case_slug, max_docs=400):
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
            "rule_text": rule_text[:400], "source_doc": source[:90],
            "file": source[:90], "quote": " ".join(quote.split())[:300],
            "explanation": explanation, "fix": fix, "severity": severity,
            "doc_category": "canons_maxims"}


def scan_mandatory_language(case_slug: str) -> list:
    """Orders/filings acknowledging a 'shall' duty then treating it as
    optional — the text-displacement trick in its purest form."""
    out = []
    for fp in _sidecars(case_slug):
        n = fp.name.lower()
        if not any(k in n for k in ("order", "opinion", "memorandum", "ecf",
                                    "reply", "opposition", "brief")):
            continue
        t = _read(fp)
        m = _MANDATE_DISREGARD_RX.search(t)
        if m:
            s = max(0, m.start() - 100)
            out.append(_pin(
                "MANDATORY_LANGUAGE_DISREGARDED",
                "1 Pa.C.S. 1921(b); the statute's own command word",
                "'Shall' imposes a mandatory duty; clear text may not be disregarded under the pretext of pursuing its spirit.",
                fp.name, t[s:m.end() + 120],
                "The document acknowledges a mandatory 'shall' duty and treats it as optional/directory — displacing command text with interpretation.",
                "Pin the command word; demand performance or reversal — a mandatory duty is not a factor to weigh.",
                severity="FATAL"))
            if len(out) >= 4:
                break
    return out


def scan_equity_maxims(case_slug: str) -> list:
    """Equitable proceedings where a maxim is triggered by the record."""
    out, seen = [], set()
    # only in equity-subject cases — case registry decides (same materiality
    # gate as conditions-precedent; doc-grep fails on shared exhibits)
    is_equity = False
    try:
        import json as _json
        _cm = _json.load(open(CASES_DIR.parent.parent / "cases_index" /
                              "case_map.json", encoding="utf-8"))
        for _k, _v in (_cm.items() if isinstance(_cm, dict) else []):
            if not isinstance(_v, dict):
                continue
            if _k.replace(":", "_").replace("fed_2_", "fed_").endswith(
                    case_slug.replace("fed_", "")) or case_slug.endswith(
                    _k.split(":")[-1].replace(":", "_")):
                _blob = (str(_v.get("title", "")) + " " +
                         str(_v.get("type", ""))).lower()
                is_equity = any(w in _blob for w in
                                ("mortgage", "foreclos", "quiet title"))
                break
    except Exception:
        pass
    if not is_equity:
        _cf = CASES_DIR / case_slug / "CASE_FACTS.md"
        if _cf.exists():
            _t = _read(_cf).lower()
            is_equity = any(w in _t[:3000] for w in
                            ("foreclos", "quiet title", "injunct"))
    if not is_equity:
        return out
    for fp in _sidecars(case_slug):
        t = _read(fp)
        for key, m in MAXIMS.items():
            if key in seen:
                continue
            mt = re.search(m["trigger"], t, re.I)
            if mt:
                seen.add(key)
                s = max(0, mt.start() - 80)
                out.append(_pin(
                    "EQUITY_MAXIM_APPLICABLE",
                    "equity jurisprudence (Broom's Maxims 1854; equity treatises) [doc: definitions/]",
                    f"{m['maxim']} — maxims are operative law in equitable proceedings, not rhetoric.",
                    fp.name, t[s:mt.end() + 120],
                    f"MAXIM: {m['maxim']}. OPERATION HERE: {m['operative']}",
                    "Plead the maxim as controlling forum law; foreclosure/quiet title ARE equity — the maxim decides, not colors.",
                    severity="SERIOUS"))
        if len(seen) == len(MAXIMS):
            break
    return out


def canon_reference() -> str:
    lines = ["# Canons of Construction + Equity Maxims — m0 Reference",
             "*The written text controls. Interpretation cannot displace rules "
             "and procedure. Generated from pipeline/canons_maxims.py.*", "",
             "## Canons (each names its abuse)", ""]
    for k, c in CANONS.items():
        lines += [f"### {k}", f"**Rule:** {c['rule']}",
                  f"**The abuse to catch:** {c['abuse']}", ""]
    lines += ["## Equity maxims (operative in foreclosure/quiet title)", ""]
    for k, m in MAXIMS.items():
        lines += [f"### {m['maxim']}", f"{m['operative']}", ""]
    lines += ["[[DEFINITION_LEXICON]] [[_LEGAL_PRINCIPLES_COVERAGE]] [[_MOC]]"]
    return "\n".join(lines)


def run_all(case_slug: str, existing=None) -> list:
    pins = []
    for fn in (scan_mandatory_language, scan_equity_maxims):
        try:
            pins.extend(fn(case_slug))
        except Exception:
            pass
    return pins


if __name__ == "__main__":
    import sys as _s
    if _s.argv[1:2] == ["--reference"]:
        out = (_eroot() / "obsidian_sync/analysis/CANONS_MAXIMS_REFERENCE.md")
        out.write_text(canon_reference(), encoding="utf-8")
        print(f"-> {out}")
        raise SystemExit(0)
    from collections import Counter
    for slug in (_s.argv[1:] or ["fed_26-cv-00456", "fed_26-cv-00123",
                                 "fed_26-cv-00789", "phila_ccp_260100000"]):
        r = run_all(slug)
        print(f"[{slug}] pins={len(r)} "
              f"{dict(Counter(p['violation_type'] for p in r))}")
