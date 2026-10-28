#!/usr/bin/env python3
"""pipeline/trick_catcher.py — the unified trick-catcher engine.

One entry point that runs EVERY trick-detection organ over a case and returns
one consolidated, source-pinned report. The organs:

  1. INSTRUMENT (deterministic)  — pipeline.instrument_comparator: two
     productions of the same note in different physical states
     (INSTRUMENT_VERSION_CONFLICT).
  2. RECIPROCITY (LLM + guards)  — pipeline.reasoning_layer.reciprocity_audit
     with detect_document_author + direction gate: rules invoked one-way
     against the pro se party (REAL_PARTY_RECIPROCITY) vs leverage.
  3. ROLE REGISTER (deterministic, NEW) — cross-party assertion register:
     extracts every party's own statements of WHO IS WHAT (holder, owner,
     creditor, original creditor, servicer, trustee) for each loan/account,
     collapses loan aliases, and emits CROSS_PARTY_ROLE_CONFLICT when the
     defendants' own words are mutually exclusive — the contradictions a
     court cannot ignore because every side of the conflict is a quote from
     an opposing party's own document.

Design law (locked): the LLM finds meaning, the deterministic layer decides
and pins. Every emitted violation carries verbatim quotes + source docs.
Fail-safe: each organ is wrapped; one organ failing never kills the run.

CLI:  python3 -m pipeline.trick_catcher <case_slug> [--no-llm] [--json out]
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

BEEZLE_ROOT = _eroot()
CASES_DIR = BEEZLE_ROOT / "obsidian_sync/cases"

# ── loan alias groups: same physical instrument, multiple system numbers ──
# (extend per case; fed_26-cv-00456 mapped in _HUD_FOIA_DEPTH_V2.md § B.4)
LOAN_ALIASES = {
    "fed_26-cv-00456": [{"17862314", "5200144832", "5715200144832"}],
}

# ── role-assertion extraction ─────────────────────────────────────────────
_ROLES = ("holder", "owner", "creditor", "original creditor", "servicer",
          "trustee", "lender", "investor")
_ROLE_RX = re.compile(
    r"(?P<party>[A-Z][A-Za-z&\.\, ]{2,50}?(?:LLC|Inc\.?|Corp(?:oration)?|Bank|"
    r"N\.A\.|Company|Mae|Mac|Trust(?:ee)?\b)?)\s+"
    r"(?:is|was|remains|as)\s+(?:the\s+|a\s+|your\s+)?(?:current\s+|new\s+|prior\s+|previous\s+)?"
    r"(?P<role>original\s+creditor|holder|owner|creditor|servicer|trustee|lender|investor|"
    # V8 (gap_auditor VOCAB_GAP): insurance / credit-reporting / judgment domains
    r"insurer|insured|carrier|underwriter|policyholder|adjuster|"
    r"furnisher|consumer\s+reporting\s+agency|"
    r"judgment\s+creditor|judgment\s+debtor|garnishee)\b"
    r"(?:\s+of\s+(?:your|the|this)\s+(?:Note|note|Loan|loan|mortgage|debt|account|"
    r"[Pp]olicy|[Cc]laim|[Jj]udgment))?",
    re.X)
# V8: entity-identity assertions ("X incorrectly captioned as Y", a/k/a, f/k/a)
# — the ASI/Progressive shell-swap class. Each match asserts party≡alias.
_IDENTITY_RX = re.compile(
    r"(?P<party>[A-Z][A-Za-z&\., ]{2,60}?)\s*"
    r"(?:\((?:incorrectly|improperly)\s+(?:captioned|named|identified|sued)\s+as\s+"
    r"[\"\u201c']?(?P<alias>[A-Z][A-Za-z&\., ]{2,60})[\"\u201d']?\)"
    r"|(?:a/k/a|f/k/a|d/b/a|formerly\s+known\s+as|now\s+known\s+as)\s+"
    r"[\"\u201c']?(?P<alias2>[A-Z][A-Za-z&\., ]{2,60}))", re.X)
_FIRST_PERSON_RX = re.compile(
    r"(?:your\s+debt\s+is\s+owed\s+to|payments?\s+should\s+be\s+made\s+payable\s+to)\s+"
    r"[\"\u201c']?(?P<party>[A-Z][A-Za-z&\., ]{2,50})", re.I)
_LOAN_RX = re.compile(
    r"(?:loan|account|policy|claim|case)\s*(?:number|no\.?|#)?[:;\s]*"
    r"([A-Z0-9][A-Z0-9\-]{5,17}\d)", re.I)
_ROLE_NORMALIZE = {"original creditor": "original_creditor",
                   "consumer reporting agency": "cra",
                   "judgment creditor": "judgment_creditor",
                   "judgment debtor": "judgment_debtor"}
_PARTY_STOP = re.compile(
    r"^(?:the|this|that|your|our|a|an|it|there|which|who|debt|loan|note|account)\b", re.I)


def _norm_party(p: str) -> str:
    p = " ".join(p.split()).strip(" .,;:\"'\u201c\u201d")
    up = p.upper()
    # canonicalize the known cast (extend as cases add parties)
    for canon, keys in {
        "PLANET HOME LENDING": ("PLANET",),
        "AMERISAVE MORTGAGE CORPORATION": ("AMERISAVE",),
        "DOVENMUEHLE MORTGAGE": ("DOVENMUEHLE", "DMI"),
        "U.S. BANK": ("U.S. BANK", "US BANK"),
        "GINNIE MAE": ("GINNIE",),
        "TRANSUNION": ("TRANS UNION", "TRANSUNION"),
        "GREENSKY": ("GREENSKY",),
        "AVANT": ("AVANT",),
        "CAPITAL ONE": ("CAPITAL ONE",),
        "EQUIFAX": ("EQUIFAX",),
        "EXPERIAN": ("EXPERIAN",),
    }.items():
        if any(k in up for k in keys):
            return canon
    return up[:50]


def _alias_key(loan: str, case_slug: str) -> str:
    for group in LOAN_ALIASES.get(case_slug, []):
        if loan in group:
            return min(group)  # stable representative
    return loan


def extract_role_assertions(text: str, source_doc: str, case_slug: str = "") -> list:
    """Extract {party, role, loan, quote, source_doc} assertions from one doc."""
    if not text:
        return []
    out = []
    loans_in_doc = [_alias_key(l, case_slug) for l in _LOAN_RX.findall(text)]
    default_loan = loans_in_doc[0] if loans_in_doc else "_UNKEYED"

    for m in _ROLE_RX.finditer(text):
        party = _norm_party(m.group("party"))
        if not party or _PARTY_STOP.match(party):
            continue
        role = _ROLE_NORMALIZE.get(m.group("role").lower(), m.group("role").lower())
        s = max(0, m.start() - 80)
        quote = " ".join(text[s:m.end() + 80].split())[:260]
        # nearest loan number before the assertion, else doc default
        prior = [_alias_key(l, case_slug) for l in _LOAN_RX.findall(text[:m.start()])]
        loan = prior[-1] if prior else default_loan
        out.append({"party": party, "role": role, "loan": loan,
                    "quote": quote, "source_doc": source_doc})

    for m in _FIRST_PERSON_RX.finditer(text):
        party = _norm_party(m.group("party"))
        if not party:
            continue
        s = max(0, m.start() - 60)
        out.append({"party": party, "role": "creditor", "loan": default_loan,
                    "quote": " ".join(text[s:m.end() + 60].split())[:260],
                    "source_doc": source_doc})

    # V8: entity-identity assertions (shell-swap class: "incorrectly
    # captioned as", a/k/a, f/k/a, d/b/a). role='identity', alias in quote.
    for m in _IDENTITY_RX.finditer(text):
        party = _norm_party(m.group("party"))
        alias = _norm_party(m.group("alias") or m.group("alias2") or "")
        if not party or not alias or party.lower() == alias.lower():
            continue
        s = max(0, m.start() - 60)
        out.append({"party": party, "role": "identity", "loan": default_loan,
                    "alias": alias,
                    "quote": " ".join(text[s:m.end() + 60].split())[:260],
                    "source_doc": source_doc})
    return out


# roles that cannot be held by two different parties for the same instrument
_EXCLUSIVE_ROLES = {"holder", "owner", "creditor"}


def find_role_conflicts(assertions: list) -> list:
    """Cross-party contradiction detection over the assertion register.

    Emits ViolationPin-shaped dicts (CROSS_PARTY_ROLE_CONFLICT, FATAL) when:
      R1: two DIFFERENT parties are each asserted (in someone's own document)
          to hold an exclusive role (holder/owner/creditor) for the same loan;
      R2: the SAME party is asserted in different documents to hold
          incompatible roles for the same loan (e.g. mere servicer vs holder).
    Assertions about the same party+role are merged (all quotes kept).
    """
    reg = defaultdict(lambda: defaultdict(list))  # loan -> role -> [assertion]
    for a in assertions or []:
        reg[a["loan"]][a["role"]].append(a)

    out = []
    # V8 — R3: ENTITY IDENTITY CONFLICT (shell-swap). A party asserted an
    # identity mapping ("X incorrectly captioned as Y" / a/k/a) while the
    # record contains role assertions under BOTH names, or the mapping itself
    # shows the answering entity differs from the entity sued. Loan-agnostic.
    ident = [a for a in (assertions or []) if a.get("role") == "identity"]
    if ident:
        role_parties = {a["party"].lower() for a in (assertions or [])
                        if a.get("role") not in (None, "identity")}
        seen_pairs = set()
        for a in ident:
            pair = (a["party"].lower(), a["alias"].lower())
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            both_named = (a["party"].lower() in role_parties
                          and a["alias"].lower() in role_parties)
            out.append(_pin(
                loan=a.get("loan", "_UNKEYED"),
                quote=f"[{a['source_doc'][:45]}] \"{a['quote'][:200]}\"",
                explanation=(
                    f"ENTITY IDENTITY CONFLICT: {a['party']} appears in the record "
                    f"under a second identity — '{a['alias']}' — by the party's own "
                    "words. "
                    + ("Substantive role claims exist under BOTH names; " if both_named
                       else "")
                    + "A party answering or acting under a different identity than "
                    "the one sued/named must prove the identity chain (registration, "
                    "merger, succession); until proven, claims made under either name "
                    "are impeachable by the existence of the other."),
                fix=("Demand corporate-identity proof (Secretary of State registration, "
                     "merger/succession documents) and treat pleadings by the "
                     "unproven entity as subject to challenge for real-party status "
                     "(Rule 17(a)) and citizenship pleading (removal cases)."),
                category="ENTITY_IDENTITY_CONFLICT",
            ))
    for loan, roles in reg.items():
        if loan == "_UNKEYED":
            continue
        # R1 — exclusive role claimed by multiple parties
        for role in _EXCLUSIVE_ROLES:
            claims = roles.get(role, [])
            parties = {}
            for a in claims:
                parties.setdefault(a["party"], a)
            if len(parties) > 1:
                names = sorted(parties)
                out.append(_pin(
                    loan=loan,
                    quote=" || ".join(
                        f"[{parties[p]['source_doc'][:45]}] \"{parties[p]['quote'][:150]}\""
                        for p in names)[:600],
                    explanation=(
                        f"The record contains mutually exclusive claims to the role of "
                        f"{role.upper()} for loan {loan}: {', '.join(names)} — each in a "
                        "party's own document. These statements cannot all be true; the "
                        "conflict is between the opposing parties' own words, pinned to "
                        "their own filings/correspondence."),
                    fix=(f"Require each claimant to prove {role} status under the "
                         "controlling definition (13 Pa.C.S. § 3301 for enforcement; "
                         "possession for bearer paper) and treat the inconsistent "
                         "statements as admissions impeaching each claimant."),
                ))
        # R2 — same party, incompatible roles (servicer vs holder/owner/creditor)
        by_party = defaultdict(set)
        srcs = defaultdict(list)
        for role, claims in roles.items():
            for a in claims:
                by_party[a["party"]].add(role)
                srcs[(a["party"], role)].append(a)
        for party, prole in by_party.items():
            if "servicer" in prole and (prole & _EXCLUSIVE_ROLES):
                ex = sorted(prole & _EXCLUSIVE_ROLES)
                q1 = srcs[(party, "servicer")][0]
                q2 = srcs[(party, ex[0])][0]
                out.append(_pin(
                    loan=loan,
                    quote=(f"[{q1['source_doc'][:45]}] \"{q1['quote'][:150]}\" || "
                           f"[{q2['source_doc'][:45]}] \"{q2['quote'][:150]}\"")[:600],
                    explanation=(
                        f"{party} is described in the record BOTH as servicer AND as "
                        f"{'/'.join(ex)} for loan {loan}. A servicer administers a loan "
                        "for the party entitled to enforce; claiming both roles for the "
                        "same instrument is internally inconsistent absent proof of "
                        "acquisition — the party's own documents supply both halves of "
                        "the contradiction."),
                    fix=("Demand the acquisition proof (indorsement/allonge, possession, "
                         "purchase documentation) or treat the enforcement-role claim as "
                         "impeached by the party's own servicer designation."),
                ))
    return out


_DISMISS_HINT = re.compile(
    r"preliminary objection|motion to dismiss|demurrer|dismiss.{0,40}count", re.I)


def _pin(loan: str, quote: str, explanation: str, fix: str,
         category: str = "CROSS_PARTY_ROLE_CONFLICT") -> dict:
    return {
        "violation_type": category,
        "rule_citation": ("Fed. R. Civ. P. 17(a); 13 Pa.C.S. § 3301; U.C.C. § 3-301; "
                          "Fed. R. Evid. 801(d)(2) (party-opponent statements)"),
        "rule_text": ("An action must be prosecuted by the real party in interest; only "
                      "a person entitled to enforce may enforce an instrument; a party's "
                      "own statement offered against it is not hearsay."),
        "source_doc": f"cross-party register (loan {loan})",
        "quote": quote[:600],
        "explanation": explanation,
        "fix": fix,
        "severity": "FATAL",
    }


# ── the engine ────────────────────────────────────────────────────────────

def catch_tricks(case_slug: str, use_llm: bool = True,
                 pro_se_names: list | None = None, max_docs: int = 400) -> dict:
    """Run all organs over a case intake. Returns the consolidated report."""
    names = pro_se_names or ["filer", "filer"]
    idir = CASES_DIR / case_slug / "intake"
    report = {"case_slug": case_slug, "instrument_conflicts": [],
              "fact_conflicts": [],
              "reciprocity_tricks": [], "leverage": [],
              "role_conflicts": [], "remedy_stripping": [], "errors": [],
              "assertion_count": 0, "total_violations": 0}
    if not idir.is_dir():
        report["errors"].append(f"no intake dir: {idir}")
        return report

    # organ 1: instrument comparator
    try:
        from pipeline.instrument_comparator import scan_case_intake
        report["instrument_conflicts"] = scan_case_intake(idir, max_docs=max_docs)
    except Exception as e:
        report["errors"].append(f"instrument: {e}")

    # organ 1b: case-fact comparator (the genus: cross-filing FACT conflicts —
    # discharge-vs-demand, amount conflicts, insurance-vs-default, date
    # impossibilities; instrument states are the species handled above)
    try:
        from pipeline.case_fact_comparator import scan_case_facts
        _extra = [d for d in idir.glob("*/") if d.is_dir()]
        _ev = CASES_DIR / case_slug / "evidence"
        if _ev.is_dir():
            _extra.append(_ev)
        report["fact_conflicts"] = scan_case_facts(
            idir, case_slug, max_docs=max_docs, extra_dirs=_extra)
    except Exception as e:
        report["errors"].append(f"fact_comparator: {e}")

    # organ 3 first (deterministic; also builds the register the LLM pass can skip)
    assertions = []
    docs = []
    for fp in sorted(idir.glob("*.txt"))[:max_docs]:
        try:
            if fp.stat().st_size < 400:
                continue
            text = fp.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        docs.append((fp, text))
        try:
            assertions.extend(extract_role_assertions(text, fp.name[:80], case_slug))
        except Exception as e:
            report["errors"].append(f"register {fp.name[:40]}: {e}")
    try:
        report["role_conflicts"] = find_role_conflicts(assertions)
        report["assertion_count"] = len(assertions)
    except Exception as e:
        report["errors"].append(f"role_conflicts: {e}")

    # organ 2: LLM reciprocity (bounded, authorship-gated)
    if use_llm:
        try:
            from pipeline.reasoning_layer import (
                ReasoningEngine, reciprocity_asymmetries_to_violations,
                reciprocity_leverage, detect_document_author,
                bucket_asymmetries_by_author)
            eng = ReasoningEngine()
            for fp, text in docs:
                try:
                    tr = eng.run_audit("reciprocity_audit", fp.name, text,
                                       case_slug, pro_se_party=", ".join(names))
                    asyms = (tr.filtered_result or {}).get("asymmetries", [])
                    if not asyms:
                        continue
                    author = detect_document_author(text, names)
                    v_in, l_in = bucket_asymmetries_by_author(asyms, author)
                    report["reciprocity_tricks"].extend(
                        reciprocity_asymmetries_to_violations(v_in, fp.stem[:60]))
                    report["leverage"].extend(
                        reciprocity_leverage(l_in, fp.stem[:60]))
                except Exception as e:
                    report["errors"].append(f"reciprocity {fp.name[:40]}: {e}")
        except Exception as e:
            report["errors"].append(f"reciprocity import: {e}")

    # organ 5: remedy-stripping comparator (cross-references WHICH counts were
    # attacked against each count's REMEDY schedule — selective attack on the
    # multiplier counts is invisible to any single-document scan)
    try:
        from pipeline.remedy_strip_detector import detect as _rs_detect
        _pleading = ""
        for _pat in ("*COMPLAINT*.txt", "*complaint*.txt", "*COMPLAINT*.doctext.txt"):
            for _f in sorted(idir.glob(_pat)):
                try:
                    _pleading = _f.read_text(encoding="utf-8", errors="replace")
                    break
                except Exception:
                    continue
            if _pleading:
                break
        for fp, text in docs:
            if not _DISMISS_HINT.search(text[:6000]):
                continue
            try:
                report["remedy_stripping"].extend(
                    _rs_detect(text, _pleading or text, fp.name[:80]))
            except Exception as e:
                report["errors"].append(f"remedy_strip {fp.name[:40]}: {e}")
    except Exception as e:
        report["errors"].append(f"remedy_strip import: {e}")

    report["total_violations"] = (len(report["instrument_conflicts"])
                                  + len(report["fact_conflicts"])
                                  + len(report["reciprocity_tricks"])
                                  + len(report["role_conflicts"])
                                  + len(report["remedy_stripping"]))
    return report


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 1
    slug = args[0]
    use_llm = "--no-llm" not in args
    out_json = None
    if "--json" in args:
        out_json = args[args.index("--json") + 1]
    rep = catch_tricks(slug, use_llm=use_llm)
    print(f"[{slug}] instrument={len(rep['instrument_conflicts'])} "
          f"facts={len(rep['fact_conflicts'])} "
          f"reciprocity={len(rep['reciprocity_tricks'])} "
          f"role={len(rep['role_conflicts'])} leverage={len(rep['leverage'])} "
          f"assertions={rep.get('assertion_count', 0)} errors={len(rep['errors'])}")
    for v in rep["role_conflicts"]:
        print(f"\n  ROLE CONFLICT ({v['source_doc']}):")
        print(f"    {v['explanation'][:300]}")
    if out_json:
        Path(out_json).write_text(json.dumps(rep, indent=2, ensure_ascii=False),
                                  encoding="utf-8")
        print(f"\nwritten: {out_json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
