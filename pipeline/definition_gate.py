#!/usr/bin/env python3
"""pipeline/definition_gate.py — term-of-art vs colloquial-sense guardrail.

WHY (2026-08-25, user-directed): people — including courts and opposing
counsel — use legal terms in their NORMAL sense when the statutory/regulatory
definition says something different. The trick lives in that gap: 'discharge'
argued as 'debt gone' (or denied as if it meant that), 'holder' as 'has the
loan', 'includes' as 'for example', 'person' as 'human'. m0 must (1) know the
controlling definition, (2) detect when a document trades on the colloquial
sense, and (3) emit a pinned DEFINITION_DIVERGENCE demand: define the term on
the record, under the controlling text, before the argument proceeds.

DISCIPLINE: definitions below are corpus-anchored (source class cited per
entry; verbatim-confirmed classes marked [corpus]). Statutory-construction
rules follow the LOCKED includes-rule (obsidian_sync/AGENTS.md 2026-07-05):
'means' = closed; 'includes' expansive ONLY with an expansion clause; doubt
in taxing/duty statutes construed against the government (Gould v. Gould).

Detection is deterministic (term + claim-context regex). The organ NEVER
decides who is right — it pins the definition and demands the record use it.
CLI: python3 -m pipeline.definition_gate <case_slug|file> ...
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = _eroot()
CASES_DIR = ROOT / "obsidian_sync" / "cases"

# ═══════════════════════════════════════════════════════════════════════════
# THE LEXICON — term: {legal, cite, colloquial_trap, trigger}
#   legal   = the controlling definition (compressed, source class named)
#   trap    = the colloquial sense documents trade on
#   trigger = regex over usage that suggests the colloquial sense is in play
# ═══════════════════════════════════════════════════════════════════════════
LEXICON = {
    "discharge": {
        "legal": ("Multiple DISTINCT legal meanings that must never be blended: "
                  "(1) discharge of an INSTRUMENT/party — UCC/13 Pa.C.S. § 3-601/3-604 "
                  "(act of the person entitled to enforce: cancellation, renunciation, "
                  "payment per § 3-602) [corpus]; (2) bankruptcy discharge — 11 U.S.C. "
                  "§ 727 (court order) [corpus]; (3) 1099-C 'discharge' — 26 U.S.C. "
                  "§ 6050P + 26 CFR 1.6050P-1: a REPORTING duty on an 'identifiable "
                  "event' [corpus]. The creditor's own filing of a 1099-C is its "
                  "admission of an identifiable event; whether the debt is legally "
                  "extinguished follows from (1)/(2) or the inconsistent-positions "
                  "doctrine — NOT from the form alone."),
        "trap": ("'discharged = debt is gone' (debtor side) OR 'the 1099-C means "
                 "nothing' (creditor side). BOTH are colloquial. The form is a "
                 "party-opponent admission (FRE 801(d)(2)) of the identifiable event "
                 "it reports, with § 7434 damages if willfully false [corpus]."),
        "trigger": r"(?:1099-?C|debt\s+(?:was|is|been)\s+discharged|discharge[dsn]?\s+(?:of|the)\s+(?:debt|loan|note))",
    },
    "holder": {
        "legal": ("UCC § 1-201(b)(21) / 13 Pa.C.S.: the person in POSSESSION of a "
                  "negotiable instrument payable to bearer or to that person "
                  "[corpus]. Enforcement: § 3-301 (holder, nonholder with holder "
                  "rights, or § 3-309 lost-instrument claimant). Bearer paper "
                  "(blank indorsement, § 3-205(b)): possession IS the test. "
                  "DISTINCT from HUD's 'Holder Name' (mortgagee-of-record for "
                  "insurance administration) and from 'servicer' (12 U.S.C. "
                  "§ 2605(i) — administers, does not own) [corpus]."),
        "trap": ("'holder = the company handling the loan' or 'named in the "
                 "system'. Possession of the original is the statutory test for "
                 "bearer paper; a servicing role or database entry proves nothing "
                 "under § 3-301."),
        "trigger": r"(?:current\s+holder|holder\s+of\s+(?:the|your)\s+(?:note|loan)|we\s+are\s+the\s+holder)",
    },
    "person": {
        "legal": ("1 U.S.C. § 1: 'person' includes corporations, companies, "
                  "associations, firms, partnerships, societies, joint stock "
                  "companies, as well as individuals [corpus]. Statute-specific "
                  "definitions control over the Dictionary Act when present "
                  "(e.g. FDCPA § 1692a, FCRA § 1681a definitions)."),
        "trap": "'person = human being'. In statutes it almost never means only that.",
        "trigger": r"(?:no\s+person|any\s+person|a\s+person\s+who)",
    },
    "includes": {
        "legal": ("LOCKED RULE: 'means' = closed class. 'Includes' is expansive "
                  "ONLY where the statute supplies an expansion clause (26 U.S.C. "
                  "§ 7701(c) 'shall not be deemed to exclude'; 'including but not "
                  "limited to') [corpus]. Absent that, the enumerated items define "
                  "the class; reaching beyond requires the canons (ejusdem "
                  "generis / expressio unius), never assumption. Doubt in "
                  "taxing/duty statutes: against the government (Gould v. Gould, "
                  "245 U.S. 151) [corpus]."),
        "trap": "'includes = for example, so anything similar counts'.",
        "trigger": r"(?:includ(?:es|ing)\s+(?!but\s+not\s+limited))",
    },
    "debt": {
        "legal": ("FDCPA 15 U.S.C. § 1692a(5): obligation of a CONSUMER to pay "
                  "money arising out of a transaction primarily for personal, "
                  "family, or household purposes [corpus]. FCRA and tax code "
                  "carry their own definitions. An 'account' or 'tradeline' is "
                  "not automatically a § 1692a(5) debt."),
        "trap": "'debt = any amount someone says you owe'.",
        "trigger": r"(?:validate\s+the\s+debt|debt\s+collector|collection\s+of\s+(?:a|the|any)\s+debt)",
    },
    "creditor": {
        "legal": ("FDCPA § 1692a(4): one who offers or extends credit creating a "
                  "debt or to whom a debt is owed — EXCLUDING one who receives an "
                  "assignment or transfer of a debt in default solely to "
                  "facilitate collection for another [corpus]. TILA § 1602(g) is "
                  "narrower still (extends credit + payable on its face). The "
                  "same entity can be a 'debt collector' as to defaulted paper."),
        "trap": "'creditor = whoever is asking for money now'.",
        "trigger": r"(?:original\s+creditor|current\s+creditor|we\s+are\s+(?:the|your)\s+creditor)",
    },
    "security": {
        "legal": ("Context-split: 15 U.S.C. § 77b(a)(1) (Securities Act: notes, "
                  "bonds, investment contracts — a NOTE is presumptively a "
                  "security subject to family-resemblance analysis) [corpus]; "
                  "UCC Art. 8/9 'security'/'security interest' (collateral) "
                  "[corpus]; 'security instrument' (mortgage/deed of trust). "
                  "A securitized note lives in BOTH worlds: the REMIC certificate "
                  "is the § 77b security; the note is the receivable."),
        "trap": "'security = the mortgage paper' only — hides the securitization layer.",
        "trigger": r"(?:securit(?:y|ized|ization)|REMIC|pooling\s+and\s+servicing)",
    },
    "bond": {
        "legal": ("COURT bonds, each with its own trigger and level: supersedeas/"
                  "appeal bond — Fed. R. Civ. P. 62(b): stay of execution on "
                  "appeal requires bond or other security [corpus]; injunction "
                  "bond — Rule 65(c): NO preliminary injunction/TRO without "
                  "security for wrongfully-enjoined costs (mandatory text; amount "
                  "discretionary) [corpus]; cost bonds (28 U.S.C. § 1917, local "
                  "rules); removal bond (former 28 U.S.C. § 1446(d) practice, now "
                  "abolished — demanding one today is anachronism); official/"
                  "judicial bonds — public-official surety statutes are "
                  "STATE-specific: a CANDIDATE THEORY requiring pinpoint cite per "
                  "the locked 2026-06-12 rule, never asserted blanket."),
        "trap": ("'the judge/court must have a bond I can claim against' asserted "
                 "blanket (candidate theory — needs the specific statute) OR the "
                 "inverse trick: opponent obtains injunctive relief WITHOUT the "
                 "Rule 65(c) security the rule's text requires — that omission is "
                 "attackable and routinely overlooked."),
        "trigger": r"(?:supersedeas|injunction\s+bond|appeal\s+bond|surety|Rule\s+65\(c\)|without\s+bond)",
    },
    "tender": {
        "legal": ("UCC § 3-603 [corpus]: tender of payment of an obligation to "
                  "pay an instrument — if REFUSED, discharge to the extent of the "
                  "tender of the obligation of an indorser/accommodation party, "
                  "and interest stops on the amount tendered. Common law: an "
                  "unconditional offer of the full amount due, in lawful medium, "
                  "kept good."),
        "trap": ("'I sent them something, that's tender' (instruments that are "
                 "not lawful medium do not tender) AND the mirror trick: a "
                 "REFUSED valid tender has real § 3-603 consequences the refusing "
                 "party will never volunteer."),
        "trigger": r"(?:tender(?:ed)?\s+(?:of\s+)?payment|refus(?:ed|al)\s+.{0,30}tender)",
    },
    "payment": {
        "legal": ("UCC § 3-602 [corpus]: an instrument is paid to the extent "
                  "payment is made by or on behalf of a party obliged to pay, TO "
                  "a person entitled to enforce. Payment to a non-PETE does not "
                  "discharge — which is exactly why holder status matters: years "
                  "of payments to a servicer for an entity that cannot prove "
                  "entitlement raise § 3-602 questions the payer can force."),
        "trap": "'we paid the company that sent statements, so the note is being paid'.",
        "trigger": r"(?:payments?\s+(?:were\s+)?made\s+to|payable\s+to|person\s+entitled\s+to\s+enforce)",
    },
    "assignment": {
        "legal": ("Transfer of MORTGAGE (recording statutes) vs NEGOTIATION of "
                  "the NOTE (UCC § 3-201/3-203/3-204) are distinct events with "
                  "distinct rules [corpus]. § 3-203: transfer without indorsement "
                  "vests only the transferor's rights; the mortgage follows the "
                  "note, not the reverse. A recorded assignment of mortgage "
                  "proves NOTHING about who may enforce the note."),
        "trap": "'we recorded an assignment, so we own the loan'.",
        "trigger": r"(?:assignment\s+of\s+mortgage|assigned\s+(?:the\s+)?(?:mortgage|loan)|MERS)",
    },
    "verification": {
        "legal": ("Pa.R.C.P. 1024 [corpus]: verification = sworn statement by a "
                  "PARTY with knowledge (or explanation why not); FDCPA § 1692g "
                  "validation is a distinct scheme; and 'verification' of a debt "
                  "or instrument in litigation means evidence admissible under "
                  "FRE 803(6)/901/1002 — not a letter enclosing photocopies. "
                  "FRE 1002 (best evidence): proving content of a writing "
                  "requires the original; FRE 1003: duplicates suffice UNLESS a "
                  "genuine question about the original's authenticity exists "
                  "[corpus] — conflicting productions raise exactly that."),
        "trap": "'we mailed you copies = verified'.",
        "trigger": r"(?:verif(?:y|ied|ication)\s+(?:of\s+)?(?:the\s+)?(?:debt|note|loan|account))",
    },
    "equity": {
        "legal": ("A jurisdiction and body of MAXIMS with operative legal force "
                  "[corpus: Am Jur Equity]: he who comes into equity must come "
                  "with clean hands (unclean hands bars the claimant's OWN "
                  "inequity in the same transaction — inconsistent 1099-C "
                  "positions, concealed securitization, altered productions); "
                  "equity regards as done that which ought to be done; equity "
                  "abhors a forfeiture; one who seeks equity must do equity; "
                  "equity aids the vigilant (laches); equity acts in personam. "
                  "Foreclosure and quiet title are EQUITABLE proceedings — the "
                  "maxims are not rhetoric there, they are the operative law of "
                  "the forum."),
        "trap": ("'equity = fairness talk'. In an equitable proceeding the maxims "
                 "are dispositive rules: a foreclosing claimant with unclean "
                 "hands in the same transaction can be denied the remedy "
                 "outright."),
        "trigger": r"(?:clean\s+hands|equit(?:y|able)\s+(?:relief|remedy|proceeding|jurisdiction)|foreclos)",
    },
    "consideration": {
        "legal": ("Bargained-for exchange of legal value [corpus: Am Jur "
                  "Contracts]. UCC § 3-303: an instrument is issued for value "
                  "when issued for a promise performed, security interest, "
                  "antecedent claim, negotiable instrument, or irrevocable "
                  "obligation. Recitals of consideration are rebuttable; absence "
                  "of consideration is a defense against non-holders in due "
                  "course (§ 3-305(a)(2))."),
        "trap": "'the paper says consideration was given, so it was'.",
        "trigger": r"(?:for\s+value\s+received|good\s+and\s+valuable\s+consideration|holder\s+in\s+due\s+course)",
    },
    "credit": {
        "legal": ("Context-split with legal consequences: TILA § 1602(f) — right "
                  "to defer payment of debt or incur debt and defer payment "
                  "[corpus]; bank 'extends credit' by crediting a deposit "
                  "account (the ledger operation) — 12 U.S.C. banking definitions "
                  "of deposit/loan [corpus]; consumer-report 'credit' (FCRA). "
                  "The locked 2026-06-12 principle: Federal Reserve credit is "
                  "not the bank's asset lent — the funding mechanics are "
                  "ledger operations, and mischaracterizing them in litigation "
                  "is a definitional misrepresentation to expose with the "
                  "banking definitions, not rhetoric."),
        "trap": "'the bank lent its own money' asserted without the ledger reality; or dismissing the question as frivolous instead of answering the definition.",
        "trigger": r"(?:extend(?:ed|s)?\s+credit|loan(?:ed)?\s+(?:you\s+)?(?:money|funds)|deposit\s+account)",
    },
    "default": {
        "legal": ("Failure to perform an obligation WHEN THE OBLIGATION AND "
                  "OBLIGEE ARE ESTABLISHED. Note default is defined by the note's "
                  "own terms; procedural default (Rule 55) is failure to plead. "
                  "A borrower cannot be in § 3-602 default to a party never shown "
                  "entitled to enforce; an insurer-paid or discharged obligation "
                  "changes the default analysis (double-recovery bar) [corpus]."),
        "trap": "'missed payments to the servicer = default, end of analysis'.",
        "trigger": r"(?:in\s+default|event\s+of\s+default|declare[ds]?\s+.{0,20}default)",
    },
    "dishonor": {
        "legal": ("UCC § 3-502 [corpus]: dishonor occurs on defined presentment/"
                  "refusal events. § 3-505: evidence of dishonor. Distinct from "
                  "'dispute'. Notice of dishonor duties (§ 3-503) have real "
                  "consequences for indorser liability."),
        "trap": "'they ignored my letter = dishonor' (colloquial) — and its mirror: real presentment-and-refusal sequences dismissed as meaningless paper.",
        "trigger": r"(?:dishonor|presentment|notice\s+of\s+dishonor)",
    },
    "mutual company": {
        "legal": ("Insurance: a company OWNED BY ITS POLICYHOLDERS — the "
                  "policyholders are both insurers and insureds, premiums into a "
                  "common fund [corpus: insurance-law definitions]. Consequence "
                  "class: membership interests travel with policies (State Farm "
                  "Mutual: PA policyholders are members), bearing on citizenship "
                  "analysis, derivative duties, and surplus distribution."),
        "trap": "'State Farm = ordinary corporation with shareholders'.",
        "trigger": r"(?:mutual\s+(?:automobile\s+)?insurance\s+compan|policyholder)",
    },
    "willful": {
        "legal": ("FCRA willfulness (15 U.S.C. 1681n) INCLUDES reckless disregard "
                  "of statutory duty - Safeco Ins. v. Burr, 551 U.S. 47: conduct "
                  "entailing an unjustifiably high risk of violation. Not limited "
                  "to knowing violations [corpus: safeco/fcra sources]."),
        "trap": ("'willful = they knew for certain' - defendants argue actual "
                 "knowledge required; Safeco says recklessness suffices, which "
                 "unlocks statutory + punitive damages without proving intent."),
        "trigger": r"(?:willful(?:ly|ness)?|reckless\s+disregard|knowing\s+violation)",
    },
    "arbitrary and capricious": {
        "legal": ("APA 5 U.S.C. 706(2)(A): agency action set aside if arbitrary, "
                  "capricious, an abuse of discretion, or otherwise not in "
                  "accordance with law - requires reasoned decisionmaking, "
                  "consideration of the relevant factors, no clear error of "
                  "judgment [corpus: usc_t5]."),
        "trap": ("'the agency has discretion' - discretion is not immunity from "
                 "the reasoned-decision requirement; unexplained departures from "
                 "the agency's own precedent/rules are the classic 706(2)(A) kill."),
        "trigger": r"(?:arbitrary\s+and\s+capricious|abuse\s+of\s+discretion|reasoned\s+decision)",
    },
    "good faith": {
        "legal": ("UCC 1-201(b)(20): honesty in fact AND the observance of "
                  "reasonable commercial standards of fair dealing [corpus]. "
                  "Implied covenant: neither party may destroy the other's right "
                  "to receive the contract's fruits. Insurance: 42 Pa.C.S. 8371 "
                  "bad faith = frivolous/unfounded claim handling."),
        "trap": ("'we believed we were right = good faith' - the objective half "
                 "(reasonable commercial standards) cannot be satisfied by "
                 "subjective belief; conduct is measured against the standard."),
        "trigger": r"(?:good\s+faith|bad\s+faith|fair\s+dealing|honesty\s+in\s+fact)",
    },
    "charge-off": {
        "legal": ("An ACCOUNTING/regulatory classification (bank calls the asset "
                  "uncollectible per regulator timing rules) — NOT forgiveness, "
                  "NOT discharge, and NOT extinguishment; but ALSO an event with "
                  "reporting consequences (FCRA accuracy; 1099-C identifiable-"
                  "event overlap) [corpus]. Continuing to report a charged-off, "
                  "1099-C'd account as an active delinquent balance is the "
                  "inconsistency, in either direction."),
        "trap": "'charged off = they wrote it off, it's gone' AND 'charge-off changes nothing' — both halves wrong; the inconsistency between the entity's own treatments is the finding.",
        "trigger": r"(?:charge[ds]?\s*-?\s*off|charged\s+off)",
    },
    "coupon": {
        "legal": ("TWO DISTINCT instruments [corpus]. (1) Bond/interest coupon "
                  "(Black's; banking dictionary): 'a warrant for the payment of "
                  "interest'... 'cut off and delivered to the payer' — negotiable; "
                  "'suit may be maintained on them without the necessity of "
                  "producing the bonds.' (2) Billing payment coupon/stub: a "
                  "'payment medium supplied by the servicer' (12 U.S.C. "
                  "2605(e)(1)(B) verbatim); Reg Z 1026.41 'periodic statement or "
                  "coupon book' (statement-substitute); Treasury TFM/IRM: machine-"
                  "scannable 'return document' routing remittances into a lockbox "
                  "deposit chain. The billing stub is EVIDENCE of the biller's "
                  "receivable system and collection routing — never tender."),
        "trap": ("LABEL-PLEADING in either direction. ELEMENT TEST decides, not a rule "
                 "(COUPON_ELEMENT_TEST_20261005.md): 13 Pa.C.S. 3104(a)/3103/3109 on the "
                 "face of the writing. Obligor-signed promise/order + fixed sum + definite "
                 "time + bearer/order words = negotiable instrument (Black's 4th; Clark v. "
                 "Iowa City, 20 Wall. 583; Koshkonong v. Burton, 104 U.S. 668) — plead it. "
                 "Obligee-issued stub 'payable to <biller>' with no order words fails 3109 — "
                 "it is an account rendered + TFM return document: plead as admission of "
                 "sum/claimant, 3311(c)(1) designated place, Art. 4 item routing (4302 "
                 "clock). The adversary's mirror trap: dismissing coupon/lockbox/remittance "
                 "records as meaningless — those records are their own books showing who "
                 "actually collects (PETE evidence, REMIC routing, application of payments)."),
        "trigger": r"(?:payment\s+coupon|remittance\s+coupon|coupon\s+book|detachable\s+(?:stub|portion|coupon)|payment\s+stub)",
    },
    "statement": {
        "legal": ("A billing statement is the creditor's RENDERED ACCOUNT — its "
                  "own ledger assertion of the receivable (Black's: account "
                  "payable = 'balance owed to a creditor'; invoice = account "
                  "'requiring the person to whom it is addressed to pay') "
                  "[corpus]. FRE 801(d)(2) admission of amount/claimant/basis. "
                  "ACCOUNT STATED (CACI 373 elements [corpus]): prior "
                  "transactions + AGREEMENT (words or conduct) on a particular "
                  "sum + promise to pay. Reg Z 1026.41 makes periodic statements "
                  "a mandatory disclosure; 2605(e) makes any non-coupon written "
                  "dispute a QWR with mandatory servicer duties."),
        "trap": ("'balance due per our statements' as if statements self-prove "
                 "the debt — account stated FAILS where the debtor objected "
                 "(no agreement) or where the claimant's own amounts are "
                 "inconsistent across entities/filings (no 'particular amount'); "
                 "statements rendered AFTER a 1099-C discharge report are a "
                 "books-inconsistency admission, not proof of debt."),
        "trigger": r"(?:account\s+stated|billing\s+statement|periodic\s+statement|statement\s+of\s+account|balance\s+due\s+per)",
    },
}

_CATEGORY = "DEFINITION_DIVERGENCE"


def scan_text(text: str, source_doc: str) -> list:
    """Detect term-of-art usages that may trade on the colloquial sense.
    Emits pins DEMANDING definition-first analysis — never deciding merits."""
    out = []
    if not text or len(text) < 40:
        return out
    for term, spec in LEXICON.items():
        for m in re.finditer(spec["trigger"], text, re.I):
            s = max(0, m.start() - 100)
            quote = " ".join(text[s:m.end() + 140].split())[:300]
            out.append({
                "violation_type": _CATEGORY,
                "term": term,
                "rule_citation": spec["legal"].split("[corpus]")[0][:180],
                "rule_text": spec["legal"][:600],
                "colloquial_trap": spec["trap"][:400],
                "source_doc": source_doc[:90],
                "quote": quote,
                "explanation": (
                    f"'{term}' is used where its controlling legal definition and "
                    f"its colloquial sense diverge. The record must proceed on the "
                    f"DEFINED meaning; any argument trading on the ordinary sense "
                    f"is a definitional misrepresentation to expose."),
                "fix": (f"Define '{term}' on the record from the controlling text "
                        "before the argument proceeds; test every use in this "
                        "document against that definition; treat colloquial-sense "
                        "reliance as misrepresentation (candor duty; FRE 801(d)(2) "
                        "for the opponent's own definitional admissions)."),
                "severity": "SERIOUS",
            })
            break  # one pin per term per document — the demand, not spam
    return out


def scan_case(case_slug: str, max_docs: int = 400) -> dict:
    idir = CASES_DIR / case_slug / "intake"
    ev = CASES_DIR / case_slug / "evidence"
    report = {"case": case_slug, "pins": [], "terms_seen": {},
              "generated": datetime.now().isoformat(timespec="seconds")}
    dirs = [d for d in (idir, ev) if d.is_dir()]
    n = 0
    for d in dirs:
        for fp in sorted(d.rglob("*")):
            if not fp.is_file() or not fp.name.lower().endswith(
                    (".doctext.txt", ".ocr.txt")):
                continue
            if n >= max_docs:
                break
            n += 1
            try:
                text = fp.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            pins = scan_text(text, fp.name)
            report["pins"].extend(pins)
            for p in pins:
                report["terms_seen"][p["term"]] = \
                    report["terms_seen"].get(p["term"], 0) + 1
    return report


def lexicon_reference() -> str:
    """Markdown reference of the full lexicon (for the vault + m0 grounding)."""
    lines = ["# Definition Gate — Term-of-Art Lexicon",
             f"*{len(LEXICON)} terms; corpus-anchored; the colloquial trap "
             "named per term. Generated from pipeline/definition_gate.py — "
             "edit THERE, not here.*", ""]
    for term, spec in sorted(LEXICON.items()):
        lines += [f"## {term}", "", f"**Legal:** {spec['legal']}", "",
                  f"**The trap:** {spec['trap']}", ""]
    return "\n".join(lines)


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] == "--reference":
        out = ROOT / "obsidian_sync" / "analysis" / "DEFINITION_LEXICON.md"
        out.write_text(lexicon_reference(), encoding="utf-8")
        print(f"reference -> {out}")
        return 0
    cases = args or ["fed_26-cv-00123", "fed_26-cv-00456", "fed_26-cv-00789",
                     "phila_ccp_260100000"]
    for slug in cases:
        rep = scan_case(slug)
        print(f"[{slug}] definitional pins={len(rep['pins'])} "
              f"terms={json.dumps(rep['terms_seen'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
