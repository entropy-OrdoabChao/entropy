#!/usr/bin/env python3
"""pipeline/case_fact_comparator.py — cross-filing FACT comparison (the general organ).

GENERALIZATION (2026-08-25, user-directed): the instrument comparator catches one
species (two productions of the same note in different physical states). The GENUS
is any FACT asserted in one filing/production that conflicts with the same fact in
another — amounts, balances, discharge events, dates, role claims. Conflicts built
from the parties' own documents are party-opponent admissions (FRE 801(d)(2)) a
court cannot wave off.

Deterministic organ. Extracts a FACT LEDGER per document:
    (fact_type, loan_key, value, quote, source_doc, doc_date?)
then runs conflict rules over the ledger:

  F1  DISCHARGE_VS_DEMAND    — a party's 1099-C / discharge statement for a loan
                               vs the same party (or any enforcer) billing or
                               demanding that debt (26 U.S.C. § 6050P reporting +
                               inconsistent-positions; 26 U.S.C. § 7434 exposure).
  F2  AMOUNT_CONFLICT        — materially different principal/payoff/discharge
                               amounts for the same loan across filings
                               (> $1 tolerance, same fact_type).
  F3  INSURANCE_VS_DEFAULT   — active FHA insurance + premiums being paid on the
                               loan while the same record forecloses it as
                               defaulted (24 CFR Part 203 preconditions; double-
                               recovery architecture: cancel → insure → foreclose
                               with claim still available).
  F4  DATE_IMPOSSIBILITY     — discharge dated BEFORE endorsement/sale of the
                               same obligation (cancel-then-sell timeline).

Emits ViolationPin-shaped dicts (category CASE_FACT_CONFLICT, FATAL) that merge
into the scorecard like every other organ. instrument_comparator remains the
instrument-state specialist; trick_catcher runs both.
"""
from __future__ import annotations

import re
from pathlib import Path

# reuse the alias map so all organs agree on loan identity
try:
    from pipeline.trick_catcher import LOAN_ALIASES
except Exception:                                     # standalone use
    LOAN_ALIASES = {}

_LOAN_RX = re.compile(r"(?:loan|account)\s*(?:number|no\.?|#)?[:;\s]*(\d{6,14})", re.I)
_MONEY = r"\$?\s?([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{2})?)"
_DATE_RX = re.compile(r"\b(\d{1,2}[/-]\d{1,2}[/-](?:20)?\d{2})\b")

_FACT_PATTERNS = {
    # fact_type: (regex with one money/date group, context window chars)
    "discharge_amount": re.compile(
        r"(?:amount\s+of\s+debt\s+discharged|debt\s+discharged)[^\d$]{0,60}" + _MONEY, re.I),
    "principal_balance": re.compile(
        r"(?:unpaid\s+)?principal\s+balance[^\d$]{0,40}(?:is[:\s]*)?" + _MONEY, re.I),
    "payoff_demand": re.compile(
        r"(?:due\s+and\s+owing[^\d$]{0,40}|payoff\s+(?:amount|statement)[^\d$]{0,40}|"
        r"total\s+(?:sum|amount)\s+due[^\d$]{0,40})" + _MONEY, re.I),
    "payment_due": re.compile(
        r"loan\s+is\s+due\s+for\s+the\s+([A-Z][a-z]+\s+\d{1,2},?\s+\d{4})\s+payment", re.I),
    "insurance_active": re.compile(
        r"(?:case\s+status[:\s]*active|insurance\s+(?:in\s+force|active)|"
        r"endorsement\s+date[:\s]*\d)", re.I),
    "insurance_terminated": re.compile(
        r"termination\s+(?:date|type)[:\s]*(?!not\s+applicable)[A-Z0-9]", re.I),
    "premium_paid_by": re.compile(
        r"PYMT[_\s]*\d{2}-\d{2}-\d{4}\s+([A-Z][A-Za-z\. ]{3,30})"),
    "endorsement_date": re.compile(
        r"endorsement\s+date[:\s]*(\d{1,2}/\d{1,2}/\d{4})", re.I),
    "discharge_date": re.compile(
        r"(?:date\s+of\s+(?:identifiable\s+event|cancell?ation|discharge))[^\d]{0,30}"
        r"(\d{1,2}[/-]\d{1,2}[/-](?:20)?\d{2})", re.I),
    "foreclosure_started": re.compile(
        r"(?:foreclosure\s+(?:action|proceeding|has\s+been|was\s+)(?:commenced|initiated|started|filed)|"
        r"notice\s+of\s+intention\s+to\s+foreclose|intent\s+to\s+foreclose)", re.I),
}


def _to_float(s: str) -> float | None:
    try:
        return float(s.replace(",", ""))
    except Exception:
        return None


def _alias_key(loan: str, case_slug: str) -> str:
    for group in LOAN_ALIASES.get(case_slug, []):
        if loan in group:
            return min(group)
    return loan


def extract_fact_ledger(text: str, source_doc: str, case_slug: str = "") -> list:
    """One document -> fact records {fact_type, loan, value, quote, source_doc}."""
    if not text:
        return []
    loans = [_alias_key(l, case_slug) for l in _LOAN_RX.findall(text)]
    default_loan = loans[0] if loans else "_UNKEYED"
    out = []
    for ftype, rx in _FACT_PATTERNS.items():
        for m in rx.finditer(text):
            grp = m.groups()[0] if m.groups() else ""
            s = max(0, m.start() - 80)
            quote = " ".join(text[s:m.end() + 80].split())[:240]
            prior = [_alias_key(l, case_slug) for l in _LOAN_RX.findall(text[:m.start()])]
            out.append({
                "fact_type": ftype,
                "loan": prior[-1] if prior else default_loan,
                "value": grp.strip() if isinstance(grp, str) else "",
                "quote": quote,
                "source_doc": source_doc,
            })
    return out


def find_fact_conflicts(ledger: list) -> list:
    """Run F1-F4 over the consolidated ledger. Returns ViolationPin dicts."""
    from collections import defaultdict
    by_loan = defaultdict(lambda: defaultdict(list))
    for f in ledger or []:
        by_loan[f["loan"]][f["fact_type"]].append(f)

    out = []
    for loan, facts in by_loan.items():
        if loan == "_UNKEYED":
            continue
        discharges = facts.get("discharge_amount", [])
        balances = facts.get("principal_balance", [])
        demands = facts.get("payoff_demand", []) + facts.get("payment_due", [])

        # F1 — discharge vs live demand for the same loan
        if discharges and (balances or demands):
            d = discharges[0]
            live = (balances + demands)[0]
            out.append(_pin(
                "F1_DISCHARGE_VS_DEMAND", loan,
                quote=f"[{d['source_doc'][:45]}] \"{d['quote'][:150]}\" || "
                      f"[{live['source_doc'][:45]}] \"{live['quote'][:150]}\"",
                explanation=(
                    f"The record for loan {loan} contains BOTH a discharge/cancellation "
                    f"of the debt (reported per 26 U.S.C. § 6050P) AND a live balance or "
                    "payoff demand on the same obligation. The positions are mutually "
                    "exclusive: a cancelled debt cannot simultaneously be billed. Either "
                    "the discharge report is false (26 U.S.C. § 7434 civil damages for "
                    "willfully fraudulent information returns) or the demand is on a "
                    "debt the claimant itself reported cancelled."),
                fix=("Require the claimant to elect and prove ONE position; the "
                     "abandoned position is a party-opponent admission impeaching the "
                     "enforcement claim."),
            ))

        # F2 — materially different amounts, same fact type, same loan.
        # MATERIALITY GATE: OCR noise produces truncated numbers ($3.00, $5.00),
        # and balances amortize legitimately over time — so require both values
        # >= $500, different source docs, variance > $50, and frame the pin as
        # an ACCOUNTING DEMAND (SERIOUS), not a standalone fatal conflict.
        for ftype in ("principal_balance", "payoff_demand", "discharge_amount"):
            vals = {}
            for f in facts.get(ftype, []):
                v = _to_float(f["value"])
                if v is not None and v >= 500.0:
                    vals.setdefault(round(v, 2), f)
            if len(vals) > 1 and (max(vals) - min(vals)) > 50.0:
                lo, hi = vals[min(vals)], vals[max(vals)]
                if lo["source_doc"] == hi["source_doc"]:
                    continue
                pin = _pin(
                    "F2_AMOUNT_CONFLICT", loan,
                    quote=f"[{lo['source_doc'][:45]}] \"{lo['quote'][:130]}\" || "
                          f"[{hi['source_doc'][:45]}] \"{hi['quote'][:130]}\"",
                    explanation=(
                        f"The record states materially different values for "
                        f"{ftype.replace('_', ' ')} on loan {loan}: "
                        f"${min(vals):,.2f} vs ${max(vals):,.2f} in different "
                        "documents. Amortization can explain drift over time, but the "
                        "enforcement demand rests on an accounting the claimant has "
                        "never reconciled on this record."),
                    fix=("Demand a full accounting reconciling the figures "
                         "(U.C.C. § 9-210; RESPA § 2605(e) QWR duty); until reconciled "
                         "the demanded amount is unproven."),
                )
                pin["severity"] = "SERIOUS"
                out.append(pin)

        # F3 — active insurance + premiums flowing while foreclosure proceeds
        if facts.get("insurance_active") and facts.get("foreclosure_started") \
                and not facts.get("insurance_terminated"):
            ins = facts["insurance_active"][0]
            fc = facts["foreclosure_started"][0]
            payers = {p["value"].strip()[:25] for p in facts.get("premium_paid_by", [])}
            out.append(_pin(
                "F3_INSURANCE_VS_DEFAULT", loan,
                quote=f"[{ins['source_doc'][:45]}] \"{ins['quote'][:130]}\" || "
                      f"[{fc['source_doc'][:45]}] \"{fc['quote'][:130]}\"",
                explanation=(
                    f"FHA insurance on loan {loan} is ACTIVE with no termination and no "
                    f"claim (premiums paid by {', '.join(sorted(payers)) or 'the servicer'}) "
                    "while the same record shows foreclosure being pursued on the loan as "
                    "defaulted. The insured mortgagee keeps the FHA claim available while "
                    "also pursuing the property — the double-recovery architecture. 24 CFR "
                    "Part 203 servicing preconditions apply to the insured loan, and "
                    "insurance-claim election bears on what the foreclosure can recover."),
                fix=("Demand disclosure of insurance status, claim intentions, and all "
                     "premium accounting; assert the Part 203 preconditions and the "
                     "election-of-remedies bar against double recovery."),
            ))

        # F4 — discharge date before endorsement date (cancel-then-insure/sell)
        dd = facts.get("discharge_date", [])
        ed = facts.get("endorsement_date", [])
        if dd and ed:
            def _parse(x):
                for fmt in ("%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y", "%m-%d-%y"):
                    try:
                        from datetime import datetime
                        return datetime.strptime(x, fmt)
                    except Exception:
                        continue
                return None
            d0 = _parse(dd[0]["value"])
            e0 = _parse(ed[0]["value"])
            if d0 and e0 and d0 < e0:
                out.append(_pin(
                    "F4_DATE_IMPOSSIBILITY", loan,
                    quote=f"[{dd[0]['source_doc'][:45]}] \"{dd[0]['quote'][:130]}\" || "
                          f"[{ed[0]['source_doc'][:45]}] \"{ed[0]['quote'][:130]}\"",
                    explanation=(
                        f"The debt on loan {loan} was reported discharged "
                        f"({dd[0]['value']}) BEFORE the obligation was endorsed for FHA "
                        f"insurance ({ed[0]['value']}). Insuring (and later selling) an "
                        "obligation already reported cancelled is a timeline the "
                        "claimants' own documents cannot reconcile."),
                    fix=("Pin the timeline from the parties' own filings and require an "
                         "explanation of what obligation was endorsed and sold after its "
                         "reported cancellation."),
                ))
    return out


def _pin(rule_id: str, loan: str, quote: str, explanation: str, fix: str) -> dict:
    return {
        "violation_type": "CASE_FACT_CONFLICT",
        "rule_citation": ("Fed. R. Evid. 801(d)(2); 26 U.S.C. §§ 6050P, 7434; "
                          "U.C.C. § 9-210; 12 U.S.C. § 2605(e); 24 CFR Part 203"),
        "rule_text": ("A party's own statements are admissible against it; mutually "
                      "exclusive facts asserted across a party's filings mean at least "
                      "one assertion is false, and the enforcement demand built on them "
                      "fails until reconciled."),
        "source_doc": f"cross-filing fact comparison [{rule_id}] (loan {loan})",
        "quote": quote[:600],
        "explanation": explanation,
        "fix": fix,
        "severity": "FATAL",
    }


def scan_case_facts(intake_dir: str | Path, case_slug: str = "",
                    max_docs: int = 400, extra_dirs: list | None = None) -> list:
    """Build the ledger over the case intake (+ optional extra dirs, e.g. the
    HUD FOIA subdirectory) and return conflicts. Bounded, fail-safe."""
    dirs = [Path(intake_dir)] + [Path(d) for d in (extra_dirs or [])]
    ledger = []
    for d in dirs:
        if not d.is_dir():
            continue
        for fp in sorted(d.glob("*.txt"))[:max_docs]:
            try:
                if fp.stat().st_size < 400:
                    continue
                text = fp.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            try:
                ledger.extend(extract_fact_ledger(text, fp.name[:80], case_slug))
            except Exception:
                continue
    return find_fact_conflicts(ledger)
