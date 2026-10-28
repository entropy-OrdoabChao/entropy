#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""m0_authority_spine — ANALYSIS-side stare-decisis organ (m0).

USER DIRECTIVE (2026-08-31): "airtight means there's stare decis supreme court
cases and prima facie evidence that can't be argued against. standing, capacity,
jurisdiction. no room for argument. ... these should be pulled during the
analysis. yes weave, but this is a gap especially if m0 will not be doing the
drafts in the future."

WHAT IT DOES (case-agnostic, data-driven — R2 no hardcoded parties):
  For each claim in a stub's `## Claim taxonomy`, pull the CONTROLLING
  element-authority (highest court, verified), ensure it exists in the corpus
  (auto-ingest via pipeline.case_fetch when missing), and write a
  `## CONTROLLING AUTHORITY (m0-verified)` section back into the stub with
  per-claim verbatim holding quotes tagged [corpus: <source>]. Any drafter
  (m1 today, anything later) inherits a pre-verified stare-decisis spine —
  the spine travels with the ANALYSIS, not the drafter.

DISCIPLINE:
  - Corpus + written law decide. A claim with no verified controlling case is
    written as `NOT IN CORPUS — no verified controlling authority` (named gap,
    never a fabricated cite).
  - Deterministic lanes here are the GUARDRAIL (verbatim quote extraction,
    corpus presence checks). The semantic :8200 lane finds the holding text.
  - Never mutate the stub without a .bak.<TS>.

CLAIM->AUTHORITY MAP: seeded with PA consumer-insurance spine (verified
2026-08-31, all four ingested to corpus); extend by appending rows — or via
--discover, which semantically searches the corpus for candidate controlling
cases for unmapped claims and prints them for human confirmation (it does NOT
auto-cite unverified discoveries).

Usage:
  python3 -m pipeline.m0_authority_spine <stub.md> [--dry-run] [--discover]
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import argparse, datetime, json, re, sqlite3, sys
from pathlib import Path

CHUNK_DB = (_eroot() / "corpus/chunk_text.db")
# staged (not live) spine rows land here for human confirmation
ROOT_PROPOSALS = (_eroot() / "hermes_agent/proposals")

# ----------------------------------------------------------------------------
# The verified spine: claim-pattern -> controlling authorities.
# Each entry: (case name, cite, court rank, corpus source slug, holding-locator
# regexes used to pull the VERBATIM quote out of the corpus text).
# Only VERIFIED rows belong here (caselaw_verify or corpus-verbatim presence).
# ----------------------------------------------------------------------------
SPINE = {
    r"bad faith|8371": [
        dict(kind="statute",
             name="42 Pa.C.S. § 8371 (Actions on insurance policies)",
             cite="42 Pa.C.S. § 8371", court="PA General Assembly (enacted)",
             source="pa_cs_t42",
             locators=[r"8371\.\s*Actions on insurance policies.{0,600}"],
             point="the enacted remedy the claim arises under — interest at prime+3%, "
                   "punitive damages, court costs and attorney fees; the statute's own "
                   "words govern, decisions construe them",
             plead="42 Pa.C.S. § 8371 provides that in an action arising under an insurance policy, if the court finds the insurer has acted in bad faith toward the insured, the court may award interest on the amount of the claim, punitive damages, and court costs and attorney fees."),
        dict(name="Rancosky v. Washington National Ins. Co.",
             cite="170 A.3d 364 (Pa. 2017)", court="PA Supreme Court",
             source="scotus_rancosky_v_washington_national_ins_co_170a3d364",
             locators=[r"clear and convincing evidence \(1\).{0,400}",
                       r"motive of self-interest or ill-will.{0,200}"],
             point="two-prong § 8371 test; ill-will NOT a prerequisite",
             plead="To recover under 42 Pa.C.S. § 8371, a plaintiff must show by clear and convincing evidence (1) that the insurer did not have a reasonable basis for denying benefits under the policy and (2) that the insurer knew of or recklessly disregarded its lack of a reasonable basis. Rancosky v. Washington National Ins. Co., 170 A.3d 364 (Pa. 2017)."),
        dict(name="Terletsky v. Prudential Prop. & Cas. Ins. Co.",
             cite="649 A.2d 680 (Pa. Super. 1994)", court="PA Superior Court",
             source="scotus_terletsky_v_prudential_property_and_casualty_ins_649a2d680",
             locators=[r"frivolous or unfounded.{0,250}"],
             point="origin of the two-prong test",
             plead="Bad faith includes a frivolous or unfounded refusal to pay proceeds of a policy. Terletsky v. Prudential Property & Casualty Ins. Co., 649 A.2d 680 (Pa. Super. 1994)."),
    ],
    r"utpcpl|201-": [
        dict(kind="statute",
             name="UTPCPL, 73 P.S. § 201-1 et seq. (Act of Dec. 17, 1968, P.L.1224, No.387)",
             cite="73 P.S. §§ 201-2, 201-3, 201-9.2", court="PA General Assembly (enacted)",
             source="pa_73ps_utpcpl",
             locators=[r'"Trade" and "commerce" mean the advertising.{0,500}',
                       r"Section 3\. Unlawful Acts or Practices; Exclusions.{0,700}",
                       r"Section 9\.2\. Private Actions.{0,600}"],
             point="THE ENACTED ACT — cite by SUBSECTION, never as '§ 201-1 et seq.': "
                   "§ 201-2(3) defines trade and commerce to reach the sale or "
                   "distribution of any SERVICES and any property tangible or INTANGIBLE, and "
                   "any trade or commerce directly or indirectly affecting the people of this "
                   "Commonwealth. § 201-3 is captioned 'Unlawful Acts or Practices; EXCLUSIONS' "
                   "and the legislature enumerated its exclusions THERE (good-faith media/ISP "
                   "publishers; a pyramid carve-out). § 201-2(4)(xxi) is the catch-all. "
                   "§ 201-9.2 supplies the private action and up to treble damages. Any "
                   "asserted limitation absent from the enacted text must be shown IN the "
                   "text — expressio unius: where the legislature enumerated exceptions, a "
                   "court may not add one by construction",
             plead="The UTPCPL defines \"trade\" and \"commerce\" as the advertising, offering for sale, sale or distribution of any services and any property, tangible or intangible, real, personal or mixed, and includes any trade or commerce directly or indirectly affecting the people of this Commonwealth, 73 P.S. § 201-2(3); Section 3 of the Act enumerates the exclusions from its coverage; and Section 9.2 affords a private action for ascertainable loss with discretionary treble damages, 73 P.S. § 201-9.2."),
        dict(name="Gregg v. Ameriprise Financial, Inc.",
             cite="245 A.3d 637 (Pa. 2021)", court="PA Supreme Court",
             source="scotus_gregg_v_ameriprise_financial_245a3d637",
             locators=[r"not dependent in any respect upon proof of the actor.{0,300}",
                       r"tendency or capacity to deceive.{0,220}"],
             point="catchall deceptive conduct = strict liability, no state of mind",
             plead="Deceptive conduct under the UTPCPL catch-all, 73 P.S. § 201-2(4)(xxi), is measured by its tendency or capacity to deceive and does not depend on the actor's state of mind. Gregg v. Ameriprise Financial, Inc., 245 A.3d 637 (Pa. 2021)."),
        dict(name="Schwartz v. Rockey",
             cite="932 A.2d 885 (Pa. 2007)", court="PA Supreme Court",
             # NOTE (2026-08-31): source 'scotus_schwartz_v_rockey_898a2d590' is
             # POISONED — it actually contains PA DGS v. US Mineral Products
             # (898 A.2d 590). Schwartz's holdings verify inside the Gregg
             # source, which quotes them (justifiable reliance x3, treble x1).
             source="scotus_gregg_v_ameriprise_financial_245a3d637",
             locators=[r"justifiable reliance.{0,220}", r"treble.{0,220}"],
             point="§ 201-9.2 reliance element; treble-damages discretion",
             plead="A plaintiff under 73 P.S. § 201-9.2 who shows an ascertainable loss caused by the unlawful conduct may recover actual damages, and the court may award up to three times that amount. Schwartz v. Rockey, 932 A.2d 885 (Pa. 2007)."),
    ],
    r"personal jurisdiction|registration|foreign corp": [
        dict(name="Mallory v. Norfolk Southern Railway Co.",
             cite="600 U.S. 122 (2023)", court="U.S. Supreme Court",
             source="scotus_mallory_v_norfolk_southern_2023",
             locators=[r"consent.{0,60}(registration|jurisdiction).{0,220}"],
             point="consent-by-registration personal jurisdiction in PA",
             plead="A foreign corporation registered to do business in Pennsylvania has consented to the general personal jurisdiction of Pennsylvania courts. Mallory v. Norfolk Southern Railway Co., 600 U.S. 122 (2023); 42 Pa.C.S. § 5301(a)(2)(i)."),
    ],
    r"breach of contract": [
        dict(name="CoreStates Bank, N.A. v. Cutillo",
             cite="723 A.2d 1053 (Pa. Super. 1999)", court="PA Superior Court",
             source="scotus_corestates_bank_n_a_v_cutillo_723a2d1053",
             locators=[r"three elements.{0,300}", r"existence of a contract.{0,260}"],
             point="the three breach elements (contract, breach, damages)",
             plead="A breach-of-contract action requires (1) the existence of a contract, including its essential terms, (2) a breach of a duty imposed by the contract, and (3) resultant damages. CoreStates Bank, N.A. v. Cutillo, 723 A.2d 1053 (Pa. Super. 1999)."),
    ],
    r"unjust enrichment": [
        dict(name="Mitchell v. Moore",
             cite="729 A.2d 1200 (Pa. Super. 1999)", court="PA Superior Court",
             source="scotus_mitchell_v_moore_729a2d1200",
             locators=[r"benefits conferred.{0,280}",
                       r"unconscionable|inequitable.{0,240}"],
             point="UE elements: benefit conferred, appreciation, inequitable retention",
             plead="Unjust enrichment requires (1) a benefit conferred on the defendant by the plaintiff, (2) appreciation of the benefit by the defendant, and (3) retention of the benefit under circumstances making retention without payment inequitable. Mitchell v. Moore, 729 A.2d 1200 (Pa. Super. 1999)."),
        dict(name="Bruno v. Erie Insurance Co.",
             cite="106 A.3d 48 (Pa. 2014)", court="PA Supreme Court",
             source="scotus_bruno_v_erie_insurance_co_106a3d48",
             locators=[r"gist of the action.{0,280}",
                       r"mere existence of a contract.{0,280}",
                       r"social duty imposed by the law of torts.{0,240}"],
             point="gist-of-the-action DEFENSE KILLER: duty imposed by law "
                   "(not created by the contract) sounds in tort despite a "
                   "contract between the parties",
             plead="A claim sounds in tort, notwithstanding a contract between the parties, where the duty breached is imposed by law as a matter of social policy rather than created by the contract's terms. Bruno v. Erie Insurance Co., 106 A.3d 48 (Pa. 2014)."),
    ],
    r"conversion": [
        # ELEMENTS FIRST. This row previously held only Bruno (a gist-of-the-action
        # shield-breaker) and pointed at `pa_legal_defs` for elements -- which carried no
        # elements text. m0 would then plead the defense-defeater AS the cause of action,
        # the defect that draws a demurrer for failure to state a claim. Stevenson is the
        # PA Supreme Court's statement of the elements themselves.
        dict(name="Stevenson v. Economy Bank of Ambridge",
             cite="413 Pa. 442, 197 A.2d 721 (1964)", court="PA Supreme Court",
             source="scotus_stevenson_v_economy_bank_of_ambridge_197a2d721",
             locators=[r"deprivation of another.{0,300}",
                       r"without the owner.{0,240}lawful justification.{0,120}"],
             point="ELEMENTS of conversion (the cause of action itself, not a "
                   "defense-defeater); states the rule quoting Gottesfeld, 196 Pa. "
                   "Superior Ct. 109, 173 A.2d 763 (1961) -- cite Stevenson as the "
                   "Supreme Court's adoption",
             plead="A conversion is the deprivation of another's right of property in, or use or possession of, a chattel, or other interference therewith, without the owner's consent and without lawful justification. Stevenson v. Economy Bank of Ambridge, 413 Pa. 442, 197 A.2d 721 (1964)."),
        dict(name="Bruno v. Erie Insurance Co.",
             cite="106 A.3d 48 (Pa. 2014)", court="PA Supreme Court",
             source="scotus_bruno_v_erie_insurance_co_106a3d48",
             locators=[r"gist of the action.{0,280}",
                       r"social duty imposed by the law of torts.{0,240}"],
             point="defeats the gist-of-the-action / collateral-duty shield ONCE the "
                   "Stevenson elements are pleaded -- sequence matters: elements first, "
                   "then the shield-breaker",
             plead="A claim sounds in tort, notwithstanding a contract between the parties, where the duty breached is imposed by law as a matter of social policy rather than created by the contract's terms. Bruno v. Erie Insurance Co., 106 A.3d 48 (Pa. 2014)."),
    ],
    # extend by appending VERIFIED rows only; --discover surfaces candidates.
    # ===== PHASE D (2026-08-31): other cases' claim patterns, corpus-verified =====
    r"fcra|1681|credit report": [
        dict(name="Cushman v. Trans Union Corp.",
             cite="115 F.3d 220 (3d Cir. 1997)", court="Third Circuit",
             source="scotus_cushman_v_trans_union_corp_115f3d220",
             locators=[r"reasonable reinvestigation.{0,300}",
                       r"obligation, pursuant to section 611\(a\).{0,300}"],
             point="CRA must conduct a REASONABLE reinvestigation under § 1681i(a); "
                   "cost does not excuse; binding in EDPA",
             plead="A consumer reporting agency's obligation under 15 U.S.C. § 1681i(a) is to conduct a reasonable reinvestigation of disputed information, and it may not simply rely on the furnisher's say-so. Cushman v. Trans Union Corp., 115 F.3d 220 (3d Cir. 1997)."),
        dict(name="Safeco Ins. Co. of America v. Burr",
             cite="551 U.S. 47 (2007)", court="U.S. Supreme Court",
             source="551bv",
             locators=[r"willful.{0,260}", r"reckless disregard.{0,260}"],
             point="willful FCRA violation includes reckless disregard → § 1681n "
                   "statutory + punitive damages without proving actual harm",
             plead="A willful violation of the FCRA under 15 U.S.C. § 1681n includes conduct in reckless disregard of statutory duty, exposing the violator to statutory and punitive damages. Safeco Ins. Co. of America v. Burr, 551 U.S. 47 (2007)."),
    ],
    r"respa|2605|qualified written request|qwr|servicer": [
        dict(name="12 U.S.C. § 2605(e) / Regulation X",
             cite="12 U.S.C. § 2605(e); 12 C.F.R. § 1024.35", court="statute + CFPB rule",
             source="201301_cfpb_final-rule_servicing-respa",
             locators=[r"2605\(e\).{0,300}", r"1024\.35.{0,300}"],
             point="servicer SHALL acknowledge QWR within 5 days, resolve within 30; "
                   "error-resolution duties are mandatory, private right via § 2605(f)",
             plead="A servicer that fails to acknowledge a qualified written request within five days or to investigate and respond within thirty is liable for actual damages and, on a pattern or practice, statutory damages. 12 U.S.C. § 2605(e), (f); 12 C.F.R. § 1024.35."),
    ],
    r"quiet title|chain of title|real party": [
        dict(name="Pa.R.C.P. 1061 (quiet title) + 2002 (real party in interest)",
             cite="Pa.R.C.P. 1061; Pa.R.C.P. 2002", court="PA procedural rules",
             source="pa_code_t231_ch1000",
             locators=[r"1061.{0,300}"],
             point="quiet-title procedure + all actions SHALL be prosecuted by the "
                   "real party in interest — enforcement without the true holder's "
                   "interest fails at the threshold",
             plead="Every action shall be prosecuted by and in the name of the real party in interest, Pa.R.C.P. 2002, and an action to quiet title lies to determine any right, lien, title or interest in land, Pa.R.C.P. 1061; a party that cannot establish an unbroken chain of title to the instrument it seeks to enforce is not the real party in interest."),
    ],
    r"garnish|3145|judgment enforcement": [
        dict(name="Pa.R.C.P. 3145 (garnishee answer limits)",
             cite="Pa.R.C.P. 3145", court="PA procedural rules",
             source="29-18",
             locators=[r"3145.{0,300}", r"garnishee.{0,260}"],
             point="the garnishee's answer is CONFINED: immunity/exemption of property, "
                   "own defenses vs defendant — may NOT defend for the defendant or "
                   "attack the attachment's validity",
             plead="A garnishee may assert only the immunity or exemption of property and its own defenses or counterclaims against the defendant, and may not assert any defense on behalf of the defendant against the plaintiff or otherwise attack the validity of the attachment. Pa.R.C.P. 3145(b)."),
    ],
    r"public (office|officer|official)|breach of (public )?trust|government (duty|accountab)|public servant": [
        dict(kind="constitution",
             name="Pa. Const. art. I, § 2 (political powers; all power inherent in the people)",
             cite="Pa. Const. art. I, § 2", court="PA Constitution",
             source="the_constitution_of_pennsylvania",
             locators=[r"All power is inherent in the people.{0,600}"],
             point="the constitutional source of the public-trust relation: government is "
                   "founded on the people's authority and instituted for their benefit",
             plead="All power is inherent in the people, and all free governments are founded on their authority and instituted for their peace, safety and happiness. Pa. Const. art. I, § 2."),
        dict(kind="treatise",
             name="16 Am. Jur. 2d, Constitutional Law §§ 60-61 (supremacy; binding on all officers)",
             cite="16 Am. Jur. 2d Constitutional Law §§ 60-61",
             court="treatise (persuasive)",
             source="amjur2d_16_constitutions_operation_effect",
             locators=[r"enacted by the people themselves in their sovereign capacity.{0,900}"],
             point="the settled statement that answers decide-around-the-record conduct: a "
                   "constitution is enacted by the people in their sovereign capacity and NO ONE "
                   "has a right to ignore or disregard its mandates; it stands above "
                   "legislative and judge-made law, so executive officers AND THE JUDICIARY "
                   "cannot lawfully act beyond its limitations; it binds all departments, all "
                   "officers, all agencies — and NO FUNCTION OF GOVERNMENT can be discharged in "
                   "disregard of or in opposition to the fundamental law",
             plead="A constitution is enacted by the people themselves in their sovereign capacity, and no one has a right to ignore or disregard its mandates; a state constitution stands above legislative and judge-made law, and thus executive officers and the judiciary cannot lawfully act beyond its limitations. It is binding upon all the departments of government of the state, all its officers, and all its agencies, and no function of government can be discharged in disregard of or in opposition to the fundamental law. 16 Am. Jur. 2d, Constitutional Law §§ 60-61."),
        dict(kind="treatise",
             name="63C Am. Jur. 2d, Public Officers and Employees § 3 (nature as public trust)",
             cite="63C Am. Jur. 2d Public Officers and Employees § 3",
             court="treatise (persuasive)",
             source="amjur2d_63c_public_officers",
             locators=[r"powers delegated to a public officer are held in trust for the people.{0,900}"],
             point="the settled statement: delegated powers are HELD IN TRUST for the people; a "
                   "public officer occupies a FIDUCIARY relationship to the citizens served and, "
                   "dealing with public property, must act with utmost good faith, fidelity and "
                   "integrity. Scope boundary stated in the same section — some authority holds "
                   "there is no fiduciary duty to perform a specific act absent a statute "
                   "requiring it; plead the duty WITH its limit",
             plead="The powers delegated to a public officer are held in trust for the people and are to be exercised on behalf of all citizens who may need the intervention of the officer; a public officer occupies a fiduciary relationship to the political entity on whose behalf he or she serves and to the citizens he or she was elected to serve, and when dealing with public property must act with the utmost good faith, fidelity, and integrity. 63C Am. Jur. 2d, Public Officers and Employees § 3."),
    ],
    r"constructive trust|resulting trust|trust res|beneficial (title|interest)": [
        dict(kind="treatise",
             name="76 Am. Jur. 2d, Trusts (creation; constructive trust as a question of fact)",
             cite="76 Am. Jur. 2d Trusts", court="treatise (persuasive)",
             source="def_63551122_76_am_jur_2d_trusts1",
             locators=[r"intention to create a trust, through a definite disposition of property.{0,600}",
                       r"constructive trust exists is a question of fact.{0,600}"],
             point="an express trust requires the settlor's INTENTION plus a definite disposition "
                   "of property, proved by competent evidence and not by rules of law; a "
                   "CONSTRUCTIVE trust needs no such intention — its existence is a question of "
                   "fact turning on a confidential relationship and unjust retention, with no set "
                   "formula. Equity imposes it over property held that in good conscience belongs "
                   "to another — the remedy reaches where a contract does not",
             plead="Whether a trust has been created through a completed execution of the settlor's intention and a definite disposition of property is a question of fact to be determined by competent evidence and not by rules of law; and whether a constructive trust exists is likewise a question of fact, there being no set formula by which the existence of a confidential relationship may be determined. 76 Am. Jur. 2d, Trusts."),
        dict(kind="statute",
             name="20 Pa.C.S. ch. 77 (Pennsylvania Uniform Trust Act)",
             cite="20 Pa.C.S. §§ 7701 et seq.", court="PA General Assembly (enacted)",
             source="pa_20_ch77_trust_act",
             locators=[r"77\d\d\.\s+[A-Z].{0,400}"],
             point="the enacted PA trust law — the statutory layer that governs trust creation, "
                   "duties and remedies in this Commonwealth",
             plead="Pennsylvania's Uniform Trust Act, 20 Pa.C.S. §§ 7701 et seq., governs the creation, administration and enforcement of trusts in this Commonwealth."),
    ],
    r"recusal|disqualif|bias|impartial": [
        dict(name="Caperton v. A.T. Massey Coal Co.",
             cite="556 U.S. 868 (2009)", court="U.S. Supreme Court",
             source="scotus_caperton_v_a_t_massey_coal_co_inc_556us868",   # was 207_0033 (PA judicial-conduct text, misattributed) — fixed 2026-10-17
             locators=[r"probability of actual bias on the part of the judge or decisionmaker is too high to be constitutionally tolerable.{0,120}",
                       r"as an objective matter, require recusal.{0,200}",
                       r"We conclude that there is a serious risk of actual bias.{0,260}"],
             point="due process REQUIRES recusal when, judged objectively, the probability of actual bias is too high to be constitutionally tolerable",
             plead="Due process requires recusal when, as an objective matter, the probability of actual bias on the part of the judge is too high to be constitutionally tolerable. Caperton v. A.T. Massey Coal Co., 556 U.S. 868 (2009)."),
        dict(name="In re Murchison",
             cite="349 U.S. 133 (1955)", court="U.S. Supreme Court",
             source="scotus_in_re_murchison_349us133",
             locators=[r"no man can be a judge in his own case.{0,240}",
                       r"justice must satisfy the appearance of justice.{0,240}"],
             point="the foundational fairness rule; appearance of justice standard",
             plead="A fair trial in a fair tribunal is a basic requirement of due process; no man can be a judge in his own case, and justice must satisfy the appearance of justice. In re Murchison, 349 U.S. 133 (1955)."),
    ],
}


# ----------------------------------------------------------------------------
# THE AUTHORITY LADDER (UI contract: "the rule, the ladder it comes from —
# Constitution down to the maxims of equity").
#
# Every spine entry carries an optional `kind`. Entries without one are `case`,
# which is what every pre-2026-09-23 row is — so existing rows are unchanged.
# The ladder decides ORDER OF PRESENTATION, because enacted text governs and
# decisions construe it: a drafter that reads the cases first inherits the
# opponent's framing (the failure this fixes — a consumer statute's own
# exclusions clause was never surfaced to m0 because the spine carried only
# cases about the statute, never the statute).
#
# Each rung uses the SAME corpus source + locators + _verbatim() gate as cases,
# so nothing is asserted that is not literally in the corpus.
# ----------------------------------------------------------------------------
LADDER = [
    ("constitution", "CONSTITUTION"),
    ("statute", "ENACTED STATUTE (governs; decisions construe it)"),
    ("rule", "RULE OF COURT"),
    ("regulation", "REGULATION"),
    ("case", "BINDING DECISION"),
    ("treatise", "TREATISE (persuasive — the settled statement)"),
    ("maxim", "MAXIM / EQUITY"),
]
_LADDER_ORDER = {k: i for i, (k, _) in enumerate(LADDER)}
_LADDER_LABEL = dict(LADDER)


def _kind(entry: dict) -> str:
    """Explicit `kind` wins; otherwise infer from the cite so legacy rows that are
    really rules/statutes (e.g. a Pa.R.C.P. row shaped as a case) sort correctly."""
    k = (entry.get("kind") or "").strip().lower()
    if k in _LADDER_ORDER:
        return k
    cite = f"{entry.get('cite', '')} {entry.get('name', '')}"
    if re.search(r"\bConst\b|Constitution|amend\.", cite, re.I):
        return "constitution"
    if re.search(r"R\.C\.P|R\.A\.P|Fed\. ?R\.|Rule \d|R\.E\.", cite, re.I):
        return "rule"
    if re.search(r"C\.F\.R|Pa\. ?Code", cite, re.I):
        return "regulation"
    if re.search(r"P\.S\.|Pa\.C\.S|U\.S\.C|Stat\.|P\.L\.", cite, re.I):
        return "statute"
    if re.search(r"Am\.? ?Jur|C\.J\.S|Restatement|Proof of Facts", cite, re.I):
        return "treatise"
    return "case"


def _con(ro=True):
    uri = f"file:{CHUNK_DB}?mode=ro" if ro else str(CHUNK_DB)
    c = sqlite3.connect(uri, uri=ro, timeout=60)
    c.execute("PRAGMA busy_timeout=60000")
    return c


def _verbatim(con, source: str, locators) -> list[str]:
    """Deterministic guardrail: pull the holding VERBATIM from corpus text."""
    rows = con.execute(
        "SELECT text FROM chunk_text WHERE source=?", (source,)).fetchall()
    quotes = []
    blob = re.sub(r"\s+", " ", " ".join(r[0] for r in rows))
    for pat in locators:
        m = re.search(pat, blob, re.I)
        if m:
            q = m.group(0).strip()
            if len(q) > 40 and q not in quotes:
                quotes.append(q)
    return quotes


def build_section(taxonomy: str) -> tuple[str, list[dict]]:
    con = _con()
    lines = ["## CONTROLLING AUTHORITY (m0-verified — weave per count)", ""]
    report = []
    claims = [c.strip() for c in re.split(r"[,;]", taxonomy) if c.strip()]
    matched_pats = set()
    for claim in claims:
        hits = []
        for pat, entries in SPINE.items():
            if re.search(pat, claim, re.I):
                matched_pats.add(pat)
                # A claim can implicate SEVERAL patterns (a statutory claim also
                # carrying a procedural rule, say). Collect EVERY match instead of
                # breaking on the first — de-duped by (name, cite) below.
                for e in entries:
                    if not any(h.get("name") == e.get("name")
                               and h.get("cite") == e.get("cite") for h in hits):
                        hits.append(e)
        # ladder order: enacted text before the decisions construing it
        hits.sort(key=lambda e: _LADDER_ORDER.get(_kind(e), len(_LADDER_ORDER)))
        lines.append(f"### {claim}")
        if not hits:
            lines.append("- NOT IN CORPUS — no verified controlling authority "
                         "mapped for this claim; elements plead from "
                         "[corpus: pa_legal_defs]. (run --discover to surface "
                         "candidates for human confirmation)")
            report.append({"claim": claim, "status": "GAP"})
            lines.append("")
            continue
        _seen_rung = None
        for e in hits:
            rung = _kind(e)
            if rung != _seen_rung:
                lines.append(f"**{_LADDER_LABEL.get(rung, rung.upper())}**")
                _seen_rung = rung
            quotes = _verbatim(con, e["source"], e["locators"])
            status = "corpus-verbatim" if quotes else "corpus-present"
            present = con.execute(
                "SELECT 1 FROM chunk_text WHERE source=? LIMIT 1",
                (e["source"],)).fetchone()
            if not present:
                status = "MISSING FROM CORPUS — run pipeline.case_fetch"
            lines.append(f"- **{e['name']}, {e['cite']}** ({e['court']}) — "
                         f"{e['point']}. [{'corpus: ' + e['source'] if present else 'NOT IN CORPUS'}]")
            if e.get("plead"):
                lines.append(f"  - PLEAD: {e['plead']}")
            for q in quotes[:2]:
                lines.append(f"  > {q}")
            report.append({"claim": claim, "case": e["name"], "cite": e["cite"],
                           "kind": rung,
                           "status": status, "quotes": len(quotes)})
        # name the rungs this claim has NO authority for — a silent gap is the
        # failure mode that let an opponent's framing stand unchecked.
        _have = {_kind(e) for e in hits}
        _missing = [lbl for k, lbl in LADDER
                    if k not in _have and k in ("statute", "constitution", "treatise")]
        if _missing:
            lines.append(f"- LADDER GAP — no verified authority mapped at: "
                         f"{', '.join(_missing)}. Pull the enacted text / treatise "
                         f"statement for this claim before relying on the decisions alone.")
            report.append({"claim": claim, "status": "LADDER_GAP",
                           "missing_rungs": [k for k, _ in LADDER if k not in _have
                                             and k in ("statute", "constitution", "treatise")]})
        lines.append("")
    con.close()
    return "\n".join(lines).rstrip() + "\n", report


def inject(stub_path: Path, section: str, dry: bool) -> bool:
    text = stub_path.read_text(encoding="utf-8")
    # replace an existing section, else insert after Claim taxonomy block
    pat = r"## CONTROLLING AUTHORITY \(m0-verified[^\n]*\n(?:(?!^## ).*\n?)*"
    if re.search(pat, text, re.M):
        new = re.sub(pat, section + "\n", text, count=1, flags=re.M)
    else:
        m = re.search(r"^## Claim taxonomy\s*\n(?:(?!^## ).*\n?)*", text, re.M)
        if not m:
            print("[spine] no '## Claim taxonomy' section — refusing blind insert")
            return False
        new = text[:m.end()] + "\n" + section + "\n" + text[m.end():]
    if new == text:
        print("[spine] stub already current")
        return True
    if dry:
        print(section)
        return True
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = stub_path.with_suffix(stub_path.suffix + f".bak.{ts}")
    bak.write_text(text, encoding="utf-8")
    stub_path.write_text(new, encoding="utf-8")
    print(f"[spine] stub updated ({stub_path.name}); backup {bak.name}")
    return True


def _propose_entry(claim: str, name: str, cite: str, court: str,
                   source: str, quotes: list) -> dict:
    """Shape a STAGED spine row from a corpus-verified candidate.

    Staged, never live: the row is written to a proposals file for human confirmation.
    A candidate whose locators do not pull verbatim from corpus is never proposed —
    same `_verbatim` discipline the live table enforces.
    """
    locs = []
    for q in quotes[:2]:
        frag = " ".join(q.split())[:60]
        locs.append(re.escape(frag) + r".{0,400}")
    return {"claim": claim, "kind": _kind({"cite": cite, "name": name}),
            "name": name, "cite": cite, "court": court, "source": source,
            "locators": locs,
            "point": "PROPOSED — confirm the holding this stands for before use",
            "plead": "", "verified_quotes": len(quotes)}


def discover(taxonomy: str, stage: bool = False):
    """Find candidate authority for UNMAPPED claims, CORPUS-FIRST, and optionally stage
    verified candidates as proposed spine rows.

    Previously this hit an unauthenticated CourtListener endpoint (which now returns an
    empty body to non-browser clients) and printed bare case names — nothing a human could
    confirm and nothing verifiable. Corpus is the primary lane: a candidate only appears if
    its text is already on disk, so a proposal can be quote-verified the moment it is made.
    """
    claims = [c.strip() for c in re.split(r"[,;]", taxonomy) if c.strip()]
    unmapped = [c for c in claims
                if not any(re.search(p, c, re.I) for p in SPINE)]
    if not unmapped:
        print("[discover] all claims mapped")
        return []
    con = _con()
    staged = []
    for c in unmapped:
        toks = [w for w in re.findall(r"[a-zA-Z]{4,}", c)][:4]
        if not toks:
            continue
        try:                                                  # S-spine-fts-rowid: chunk_text is WITHOUT ROWID, the old
            from pipeline import retrieval_client as RC        # JOIN USING(rowid) always threw → daemon/sqlite FTS phrase instead
            rows = [(h.get("source"), h.get("text")) for h in RC.hits(RC.fts_phrase(" ".join(toks), limit=5)) if not h.get("error")]
        except Exception:
            rows = []
        if not rows:
            try:
                like = "%" + "%".join(toks[:2]) + "%"
                rows = con.execute(
                    "SELECT source, text FROM chunk_text WHERE text LIKE ? LIMIT 5",
                    (like,)).fetchall()
            except Exception:
                rows = []
        if not rows:
            print(f"[discover] {c}: NOT IN CORPUS — pull via pipeline.case_fetch, then re-run")
            continue
        for src, txt in rows[:3]:
            nm = re.search(r"([A-Z][A-Za-z.'&\- ]{3,40} v\.? [A-Z][A-Za-z.'&\- ]{3,40})", txt or "")
            ct = re.search(r"(\d{1,3}\s+[A-Z][A-Za-z.]*\.?\s?(?:2d|3d|4th)?\s+\d{1,4})", txt or "")
            print(f"[discover] {c}: [corpus: {src}] "
                  f"{nm.group(1) if nm else '(name not parsed)'} "
                  f"{ct.group(1) if ct else ''}")
            if stage and nm:
                sents = [s for s in re.split(r"(?<=\.)\s+", " ".join((txt or "").split()))
                         if 60 < len(s) < 400][:2]
                if sents:
                    staged.append(_propose_entry(
                        c, nm.group(1), ct.group(1) if ct else "", "VERIFY COURT",
                        src, sents))
    con.close()
    if stage and staged:
        out = ROOT_PROPOSALS / f"spine_proposals_{datetime.date.today():%Y%m%d}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        prev = json.loads(out.read_text()) if out.exists() else []
        out.write_text(json.dumps(prev + staged, indent=1), encoding="utf-8")
        print(f"[discover] STAGED {len(staged)} proposed row(s) -> {out}")
        print("[discover] review, set court/point/plead, then paste into SPINE. "
              "Nothing is live until you do.")
    return staged


# ----------------------------------------------------------------------------
# DEFECT LADDER (UI + drafters, 2026-10-17): for each defect CLASS m0 reports
# (scorecard / judicial_findings), "where the rule comes from" — Constitution
# down to the maxims of equity — per forum. Same _verbatim() gate as the spine:
# a rung whose locator misses is reported NOT IN CORPUS, never paraphrased.
# forum: "PA" (Pennsylvania courts) | "FED" | "*" (both).
# `spine`: also pull SPINE rows whose claim-pattern matches this word.
# ----------------------------------------------------------------------------
_PA_CONST = "The_Constitution_of_Pennsylvania"
_DUE_PROCESS_US = dict(kind="constitution", forum="*", name="U.S. Const. amend. XIV, § 1", cite="U.S. Const. amend. XIV",
    source="LAW_Constitution", locators=[r"of life, liberty, or property, without due process of law; nor deny to any person within"],
    point="no State, and so no state court, may take life, liberty or property without due process of law")
_REMEDY_PA = dict(kind="constitution", forum="PA", name="Pa. Const. art. I, § 11", cite="Pa. Const. art. I, § 11",
    source=_PA_CONST, locators=[r"shall have remedy by due course of law, and right and justice administered without sale, denial or delay"],
    point="every injury has a remedy by due course of law; justice may not be denied or delayed")
_UBI_JUS = dict(kind="maxim", forum="*", name="Ubi jus, ibi remedium", cite="Bouvier's Law Dictionary (1856)",
    source="bouvier_1856_law_dictionary", locators=[r"Ubi jus, ibi remedium\. Where there is a right, there is a remedy\."],
    point="a right with no way to enforce it is no right at all")
_CANDOR = dict(kind="canon", forum="*", name="Pa.R.P.C. 3.3(a)(1) (Candor Toward the Tribunal)", cite="204 Pa. Code Rule 3.3",
    source="pa_code_t204_ch81", locators=[r"Rule 3\.3\. Candor Toward the Tribunal\. \(a\) A lawyer shall not knowingly: \(1\) make a false statement of material fact or law to a tribunal.{0,120}"],
    point="a lawyer may not misstate the law to a court")
_NEMO_JUDEX = dict(kind="maxim", forum="*", name="Nemo debet esse judex in propria causa", cite="Bouvier's Law Dictionary (1856)",
    source="bouvier_1856_law_dictionary", locators=[r"Nemo debet esse judex in propria causa\. No one should be judge in his own cause\."],
    point="no one may judge a matter in which he has a stake")
DEFECT_SPINE = {
    r"NON_BINDING_AS_CONTROLLING|TRIAL_COURT_AS_APPELLATE_AUTHORITY|stare decisis|binding": [
        dict(kind="constitution", forum="PA", name="Pa. Const. art. V, §§ 1-2", cite="Pa. Const. art. V, §§ 1-2", source=_PA_CONST,
             locators=[r"The judicial power of the Commonwealth shall be vested in a unified judicial system.{0,200}",
                       r"The Supreme Court \(a\) shall be the highest court of the Commonwealth.{0,120}"],
             point="one unified ladder of courts with the Supreme Court on top; a court of common pleas sits on a lower rung"),
        dict(kind="constitution", forum="FED", name="U.S. Const. art. III, § 1", cite="U.S. Const. art. III, § 1", source="LAW_Constitution",
             locators=[r"The judicial Power of the United States, shall be vested"],
             point="one Supreme Court and the inferior courts Congress creates: the hierarchy is constitutional"),
        dict(kind="statute", forum="PA", name="42 Pa.C.S. § 742", cite="42 Pa.C.S. § 742", source="pa_cs_t42",
             locators=[r"742\. Appeals from courts of common pleas\. The Superior Court shall have exclusive appellate jurisdiction of all appeals from final orders of the courts of common pleas.{0,80}"],
             point="the Superior Court reviews the courts of common pleas; law flows down the ladder, not up"),
        dict(kind="statute", forum="FED", name="28 U.S.C. § 1291", cite="28 U.S.C. § 1291", source="usc_t28_s1291",
             locators=[r"have jurisdiction of appeals from all final decisions of the district courts of the United States"],
             point="the courts of appeals review the district courts"),
        dict(kind="rule", forum="PA", name="Pa.R.A.P. 126(b)", cite="210 Pa. Code Rule 126(b)", source="pa_code_t210_ch1",
             locators=[r"\(b\) Non-Precedential Decisions ?\. \(1\) As used in this rule, .{0,420}?persuasive value\."],
             point="the appellate rules themselves say which decisions bind and which are persuasive only"),
        dict(kind="rule", forum="PA", name="Pa.R.A.P. 3716 (Commonwealth Court)", cite="210 Pa. Code Rule 3716", source="pa_code_t210_ch37",
             locators=[r"Rule 3716\. Citing Judicial Opinions in Filings\. \(a\) A reported opinion of the Commonwealth Court en banc or three-Judge panel may be cited as binding precedent\. \(b\) An unreported panel decision of this Court issued after January 15, 2008, may be cited for its persuasive value, but not as binding precedent\."],
             point="only a reported appellate opinion is binding precedent; an unreported one is persuasive at most"),
        _CANDOR,
        dict(kind="maxim", forum="*", name="Stare decisis", cite="Bouvier's Law Dictionary (1856)", source="bouvier_1856_law_dictionary",
             locators=[r"STARE DECISIS\. To abide or adhere to decided cases\. 2\. It is a general maxim that when a point has been settled by decision, it forms a precedent which is not afterwards to be departed from\."],
             point="a point settled by a court with power to settle it binds the courts below it"),
    ],
    r"no_reasons_stated|no reasons|statement of reasons": [
        _DUE_PROCESS_US, _REMEDY_PA,
        dict(kind="rule", forum="PA", name="Pa.R.A.P. 1925(a)(1)", cite="210 Pa. Code Rule 1925(a)(1)", source="pa_code_t210_ch19",
             locators=[r"if the reasons for the order do not already appear of record, shall within.{0,40}file of record at least a brief opinion of the reasons for the order.{0,80}"],
             point="once an appeal is taken, the judge MUST put the reasons on the record"),
        dict(kind="rule", forum="FED", name="Fed. R. Civ. P. 52(a)(1)", cite="Fed. R. Civ. P. 52(a)(1)", source="courtlistener_frcp_rule_52",
             locators=[r"the court must find the facts specially and state its conclusions of law separately"],
             point="the court must find the facts and state its conclusions"),
        _UBI_JUS,
    ],
    r"financial_interest_not_disclosed|ruling_in_face_of_recusal_motion|recusal|disqualif": [
        _DUE_PROCESS_US,
        dict(kind="canon", forum="PA", name="Pa. Code of Judicial Conduct, Rule 2.11(A)", cite="207 Pa. Code Rule 2.11(A)", source="207_0033",
             locators=[r"Rule 2\.11\(A\) provides: A judge shall disqualify himself or herself in any proceeding in which the judge.s impartiality might reasonably be questioned"],
             point="a Pennsylvania judge MUST step aside when impartiality might reasonably be questioned"),
        dict(kind="statute", forum="FED", name="28 U.S.C. § 455(a)", cite="28 U.S.C. § 455(a)", source="usc_t28_s455",
             locators=[r"shall disqualify himself in any proceeding in which his impartiality might reasonably be questioned"],
             point="a federal judge MUST step aside when impartiality might reasonably be questioned (binds federal judges only)"),
        dict(kind="canon", forum="FED", name="Code of Conduct for U.S. Judges, Canon 3C(1)", cite="Canon 3C(1)",
             source="code_of_conduct_for_united_states_judges_effective_march_12_2019",
             locators=[r"must do so if the judge.s impartiality might reasonably be questioned under Canon 3C\(1\).{0,140}"],
             point="disqualification is required, not optional, when impartiality might reasonably be questioned"),
        _NEMO_JUDEX,
    ],
    r"non_discretionary_remedy_denied|non.discretionary|mandatory remedy": [
        _REMEDY_PA, _DUE_PROCESS_US,
        dict(kind="statute", forum="PA", name="42 Pa.C.S. § 8303", cite="42 Pa.C.S. § 8303", source="pa_consolidated_statutes",
             locators=[r"§ 8303\. Action for performance of a duty required by law\. A person who is adjudged in an action in the nature of mandamus.{0,200}?refusal\."],
             point="a person who refuses without lawful justification to do a duty the law requires is liable in damages"),
        dict(kind="statute", forum="FED", name="28 U.S.C. § 1651(a)", cite="28 U.S.C. § 1651(a)", source="usc_t28_s1651",
             locators=[r"may issue all writs necessary or appropriate in aid of their respective jurisdictions.{0,80}"],
             point="the federal courts' power to compel (mandamus) — federal courts only"),
        dict(kind="case", forum="*", name="Marbury v. Madison", cite="5 U.S. 137, 163 (1803)", source="scotus_marbury_v_madison_5us137",
             locators=[r"The very essence of civil liberty certainly consists in the right of every individual to claim the protection of the laws.{0,160}"],
             point="where a duty is fixed by law, the person injured by its refusal has a remedy"),
        _UBI_JUS,
    ],
}
_SPINE_ALIAS = {r"financial_interest_not_disclosed|ruling_in_face_of_recusal_motion": "recusal"}
LADDER_UI = [("constitution", "Constitution"), ("statute", "Public law · code"), ("regulation", "Regulation"),
             ("rule", "Court rule"), ("case", "Binding decision"), ("canon", "Canon · duty"),
             ("treatise", "Treatise"), ("maxim", "Maxim · equity")]
_LADDER_UI_ORDER = {k: i for i, (k, _) in enumerate(LADDER_UI)}


def _kind_ui(e: dict) -> str:
    return "canon" if (e.get("kind") or "").lower() == "canon" else _kind(e)


def defect_ladder(defect_class: str, forum: str = "PA", con=None) -> list[dict]:
    """Rungs for one defect class in one forum, ordered Constitution -> maxim. Each rung:
    {kind, label, name, cite, point, quote|None, source, status}. Rungs in (constitution, statute, rule,
    maxim) with nothing mapped come back as status 'NOT IN CORPUS' so the gap is shown, never filled."""
    own = con is None
    con = con or _con()
    hits = []
    for pat, entries in DEFECT_SPINE.items():
        if re.search(pat, defect_class or "", re.I):
            hits += [e for e in entries if e.get("forum", "*") in ("*", forum)]
    for pat, word in _SPINE_ALIAS.items():
        if re.search(pat, defect_class or "", re.I):
            for sp, entries in SPINE.items():
                if re.search(sp, word, re.I):
                    hits += entries
    hits += [e for e in _learned_rows() if e.get("defect_class") == defect_class and e.get("forum", "*") in ("*", forum)]
    out, seen = [], set()
    for e in hits:
        if (e["name"], e["cite"]) in seen:
            continue
        seen.add((e["name"], e["cite"]))
        q = _verbatim(con, e["source"], e["locators"])
        k = _kind_ui(e)
        out.append({"kind": k, "label": dict(LADDER_UI).get(k, k), "name": e["name"], "cite": e["cite"],
                    "point": e.get("point", ""), "quote": q[0] if q else None, "source": e["source"],
                    "status": "corpus-verbatim" if q else "NOT IN CORPUS (locator miss)"})
    have = {r["kind"] for r in out}
    for k in ("constitution", "statute", "rule", "maxim"):
        if k not in have:
            out.append({"kind": k, "label": dict(LADDER_UI)[k], "name": None, "cite": None, "point": "",
                        "quote": None, "source": None, "status": "NOT MAPPED", "searched": _last_search(defect_class, forum, k)})
    out.sort(key=lambda r: _LADDER_UI_ORDER.get(r["kind"], 99))
    if own:
        con.close()
    return out




# ----------------------------------------------------------------------------
# FORUM GUARD (rule, 2026-10-17): an authority written against a court must come from
# THAT court's sovereign. Federal procedural law (FRCP, 28 U.S.C. procedural sections,
# the U.S. Judges' Code, 3d Cir. precedent) does not bind a Pennsylvania court, and PA
# rules/statutes do not bind a federal court on procedure. Deterministic guardrail only:
# it flags clear sovereign mismatches; whether a SCOTUS case binds (constitutional vs
# statutory holding) is for m2 analysis/authority_binding.
# ----------------------------------------------------------------------------
FOREIGN_CITE = {
    "PA": [r"Fed\.\s?R\.\s?(?:Civ|App|Evid|Crim)\.\s?P\.\s?[\d.]+(?:\([a-z0-9]+\))*",
           r"28 U\.S\.C\.\s?§§?\s?(?:455|144|351|1291|1651|1746|2072)(?:\([a-z0-9]+\))*",
           r"Code of Conduct for U\.S\. Judges[^;]*", r"Canon \d\([A-Z]\)(?:\(\d\))?", r"CJRA[^;]*",
           r"[^;]*\(3d Cir\.[^;]*"],
    "FED": [r"Pa\.\s?R\.\s?(?:C|A|E|Crim)\.\s?P\.\s?[\d.]+(?:\([a-z0-9]+\))*", r"\d+ Pa\.\s?C\.\s?S\.[^;]*", r"\d+ Pa\. Code[^;]*"],
}


def forum_of(case_id: str = "", court_code: str = "") -> str:
    c = f"{case_id} {court_code}".lower()
    return "PA" if ("phila_ccp" in c or "pa_" in c or "_pa" in c and "edpa" not in c) else "FED"


def forum_authority(defect_class: str, forum: str, stored: str, con=None) -> dict:
    """Split a stored authority string into (kept, foreign) for this forum and add the forum-correct,
    corpus-verbatim rungs from defect_ladder. Returns {authority, foreign, added, changed}."""
    parts = [x.strip() for x in re.split(r";", stored or "") if x.strip()]
    foreign, kept = [], []
    for x in parts:
        (foreign if any(re.search(pat, x) for pat in FOREIGN_CITE.get(forum, [])) else kept).append(x)
    added = [r["name"] for r in defect_ladder(defect_class, forum, con)
             if r.get("quote") and r["kind"] in ("constitution", "statute", "rule", "canon")
             and r["name"] not in " ".join(kept)]
    auth = "; ".join(added + kept)
    if foreign:
        auth += f" [not binding in a {'Pennsylvania' if forum == 'PA' else 'federal'} court, kept for reference: {'; '.join(foreign)}]"
    return {"authority": auth, "foreign": foreign, "added": added, "changed": bool(foreign or added)}



# ----------------------------------------------------------------------------
# LADDER-GAP DISCOVERY (m0 half; 2026-10-17, user: "have it update the court rules and public law").
# Extends discover() — same ROOT_PROPOSALS staging + _verbatim gate — but per ladder LEVEL, SEMANTIC
# (m2's family-scoped turbovec retrieval, never FTS), and the LLM SELECTS (reasoning on) the one provision
# that imposes the duty, or none. m2 (pipeline.m2.ladder_confirm, authority_binding contract) confirms
# forum + on-point before a row goes live in LADDER_LEARNED, which defect_ladder() reads.
# ----------------------------------------------------------------------------
LADDER_LEARNED = (_eroot() / "output/audit/defect_spine_learned.json")
LADDER_PROPOSALS = ROOT_PROPOSALS / "ladder_proposals.json"
LEVEL_FAMILY = {   # prefixes checked against the live source census by family_census() before every run
    ("PA", "statute"): (["pa_cs_", "pa_consolidated_statutes", "pa_73ps_"], "Pennsylvania Consolidated Statutes (public law)"),
    ("PA", "rule"): (["pa_code_t201_", "pa_code_t210_", "pa_code_t231_", "pa_code_t234_", "pa_rcp_", "pa_rules-of-evidence", "PA_rules-of-evidence",
                      "207_", "pa_code_t207_", "pa_jcb_rules", "phila_civ_r_", "phila_cr_", "200130-supremeiop"],
                     "Pennsylvania Rules of Court (Pa.R.A.P., Pa.R.C.P., Code of Judicial Conduct 207 Pa. Code, Phila. local rules, Pa. Supreme Court IOPs)"),
    ("FED", "statute"): (["usc_t", "USCODE"], "United States Code (public law)"),
    ("FED", "rule"): (["courtlistener_frcp_rule_", "courtlistener_frap_rule_", "federal-rules-of-evidence", "ca3_local_rules", "ca3_local_appellate_rules",
                       "ca3_iop", "edpa_local_rules", "edpa_lr_", "judicial_conduct_and_disability_rules", "courtlistener_judicial_conduct_and_disability_rules"],
                      "Federal Rules of Civil / Appellate Procedure, Third Circuit L.A.R. and I.O.P.s, E.D. Pa. local rules, Judicial-Conduct Rules"),
}
FAMILY_SCAN_MAX = 2500     # families at or under this many chunks are embedded whole (exhaustive semantic recall); bigger ones use the index


def family_census(con=None) -> dict:
    """{prefix: chunk count} — a 0 means the prefix matches nothing (dead family entry => logged, never silent)."""
    con = con or _con()
    return {f: con.execute("SELECT count(*) FROM chunk_text WHERE source GLOB ?", (f + "*",)).fetchone()[0]
            for fams, _ in LEVEL_FAMILY.values() for f in fams}


def _family_scan(queries: list, fams: list, k: int, census: dict) -> list:
    """Exhaustive SEMANTIC recall inside the small families of a level (same bge model + prefix as m2 retrieval):
    the global index top-k drowns a 60-chunk rule set under 3M opinions, so small families are embedded whole.
    B-turbovec-daemon: vectors come from the resident daemon (pipeline.retrieval_client.embed, batches <= 64) — this
    process never imports torch. When the daemon cannot embed (down / degraded / FTS-only) the scan returns [] and
    says so once: an empty scan is 'not semantically verified', never a fabricated ranking."""
    from pipeline import retrieval_client as RC
    small = [f for f in fams if 0 < census.get(f, 0) <= FAMILY_SCAN_MAX]
    if not small:
        return []
    import numpy as np
    key = tuple(small)
    if key in _FAM_EMB:                                   # one embedding pass per family set per run
        rows, dv = _FAM_EMB[key]
    else:
        rows, dv = _embed_family(small, RC, _con())
        _FAM_EMB[key] = (rows, dv)
    if not rows or dv is None:
        return []
    qr = RC.embed([q[:400] for q in queries[:4]], is_query=True)
    qv = qr.get("vectors")
    if not qv:
        _scan_warn(f"query embed unavailable ({qr.get('error')})")
        return []
    sc = (np.asarray(dv, dtype="float32") @ np.asarray(qv, dtype="float32").T).max(axis=1)
    order = np.argsort(-sc)[:k]
    return [{"chunk_id": rows[i][0], "source": rows[i][2], "score": round(float(sc[i]), 3), "text": rows[i][1][:1200]} for i in order]


_FAM_EMB: dict = {}
_SCAN_WARNED: set = set()


def _scan_warn(msg: str):
    if msg not in _SCAN_WARNED:
        _SCAN_WARNED.add(msg)
        print(f"[family_scan] daemon embed unavailable — family scan skipped, NOT semantically verified: {msg}", file=sys.stderr, flush=True)


def _embed_family(small, RC, con):
    """rows + their bge vectors from the daemon in <= 64-text batches (FAMILY_SCAN_MAX bounds the total).
    Returns (rows, None) when any batch cannot be embedded (caller treats as 'no scan')."""
    rows = []
    for f in small:
        rows += con.execute("SELECT chunk_id, text, source FROM chunk_text WHERE source GLOB ? AND length(text) > 80",
                            (f + "*",)).fetchall()
    if not rows:
        return [], None
    vecs = []
    for i in range(0, len(rows), 64):
        r = RC.embed([(x[1] or "")[:1200] for x in rows[i:i + 64]])
        v = r.get("vectors")
        if not v:
            _scan_warn(f"family batch embed failed ({r.get('error')})")
            return rows, None
        vecs += v
    return rows, vecs


def _last_search(defect_class: str, forum: str, kind: str):
    """Latest discover_ladder_gaps / ladder_confirm outcome for this level, so a gap says what was searched."""
    try:
        rows = [x for x in json.loads(LADDER_PROPOSALS.read_text(encoding="utf-8"))
                if x.get("defect_class") == defect_class and x.get("forum") == forum and x.get("kind") == kind]
    except Exception:
        return None
    if not rows:
        return None
    x = rows[-1]
    return {"ts": x.get("m2_ts") or x.get("ts"), "status": x.get("status"), "why": x.get("m2_why") or x.get("why") or x.get("note", ""),
            "n": len(x.get("candidates") or []) or None}


def _learned_rows() -> list:
    try:
        return json.loads(LADDER_LEARNED.read_text(encoding="utf-8"))
    except Exception:
        return []


def _defect_meaning(defect_class: str) -> str:
    try:
        from pipeline.judicial_probe import ORDER_RED_FLAGS
        for flag, desc, _a, _s in ORDER_RED_FLAGS:
            if flag == defect_class:
                return desc
    except Exception:
        pass
    return defect_class.replace("_", " ").lower()


def live_defect_classes() -> list:
    """(defect_class, forum) pairs m0 actually reported: judicial_findings + opponent scorecards."""
    import sqlite3, glob
    out = set()
    try:
        c = sqlite3.connect("file:<home>/output/audit/emma_intel.db?mode=ro", uri=True)
        for cid, flag in c.execute("SELECT DISTINCT case_id, flag FROM judicial_findings"):
            out.add((flag, forum_of(cid or "")))
    except Exception:
        pass
    for f in glob.glob(str(_eroot() / "output/audit/scorecard_*.md")):
        key = Path(f).stem.replace("scorecard_", "")
        t = Path(f).read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"^### \[(\w+)\] (\S+)", t, re.M):
            out.add((m.group(2), forum_of(key)))
    return sorted(out)


_SELECT_SYS = """You are m0's authority selector for the Beezle defect ladder. Corpus + written law decide; never memory.
You get ONE defect (what a court or lawyer did wrong), the FORUM (court system) and the LEVEL (public law, or court rule),
and CANDIDATE excerpts from the corpus, each numbered. Pick the ONE candidate whose own words IMPOSE the duty or rule that
the defect breaks, in that forum, at that level. A provision that merely mentions the topic, or belongs to another court
system, or is a proposed amendment / commentary, is NOT a pick. If no candidate imposes the duty, pick null - that is a
valid, honest answer. Never pick a provision already listed under ALREADY-KNOWN AUTHORITY; a Comment or a
"should" is not a rule that imposes a duty - pick the operative rule text it explains.
Reply with ONE JSON object: {"pick": <candidate number or null>, "name": "<official cite, e.g. Pa.R.A.P. 1925(a)(1) or 42 Pa.C.S. § 8303>",
"quote": "<15-60 consecutive words copied EXACTLY from that candidate that state the duty>",
"point": "<one plain sentence, 11th-grade level: what it requires>", "why": "<one sentence>"}"""


def _llm_json(system: str, user: str, max_tokens: int = 4000) -> dict:
    """local-gpu production lane via m2's client; reasoning ON (selection = comprehension, per the quality doctrine)."""
    from pipeline.m2 import analysis_runner as A
    model = A.MODEL or A._get(f"{A.LANE}/models", 10)["data"][0]["id"]
    body = {"model": model, "temperature": 0, "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": True},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    raw = A._post(f"{A.LANE}/chat/completions", body, timeout=900)
    msg = raw["choices"][0].get("message", {})
    return A.parse_json(msg.get("content") or msg.get("reasoning_content") or "")


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", t or "").strip()


def _duty_queries(cls: str, meaning: str, forum: str, kind: str, famlabel: str, known: str) -> list:
    """Comprehension step before retrieval: the defect line ("non binding as controlling") is too thin to embed near the
    operative text, so the LLM writes how that body of law would itself word the duty; those sentences lead the
    queries (_family_scan uses the first 4). Deterministic queries still follow - the LLM widens recall, the gates
    downstream still decide. On any LLM error the deterministic queries run alone."""
    try:
        out = _llm_json(
            "You draft search queries for a legal corpus. Return JSON {\"queries\": [3 strings]}. Each string is one "
            "sentence written the way the named body of law itself would word the operative duty (shall/must language, "
            "its own terms of art), so it embeds near the real provision. Do not cite numbers you are unsure of.",
            f"Body of law: {famlabel}\nDefect class: {cls}\nDefect: {meaning}\nAlready-known authority: {known[:400]}",
            max_tokens=2500)
        return [q for q in (out.get("queries") or []) if isinstance(q, str) and len(q) > 20][:3]
    except Exception as e:
        print(f"[ladder] {cls} {forum} {kind}: duty-query LLM error {e} - deterministic queries only")
        return []


_MARK = r"(?:Rule|R\.|P\.|§+|Section|Sec\.|L\.A\.R\.|I\.O\.P\.|IOP|Canon|C\.S\.)"


def _provision_numbers(name: str) -> list:
    """The provision number(s) the pick names ("Rule 2.11(A)(3)" -> ["2.11"]; "Fed. R. App. P. 3" -> ["3"]).
    The old filter dropped single-digit rule numbers, so FRAP 3 / Canon 2 could never pass the gate."""
    got = [m.group(1) for m in re.finditer(_MARK + r"\s*(\d+(?:[.\-]\d+)*)", name)]
    return got[-1:] or [n for n in re.findall(r"\d+(?:\.\d+)*", name) if len(n) >= 3 or "." in n]


def _cite_number_ok(con, h: dict, q: str, n: str) -> bool:
    """The cited number must label the quoted text: in the candidate chunk, in the source key (rule_3), or as the
    LAST provision heading before the quote in the whole source (a rule split across chunks keeps its heading in an
    earlier chunk - 207_0033 Rule 2.11(A)(3) failed this way)."""
    num = rf"(?<![\d.]){re.escape(n)}(?![\d])"
    if re.search(num, _norm(h["text"])) and re.search(_MARK + r"\s*" + re.escape(n) + r"(?![\d])", _norm(h["text"])):
        return True
    if re.search(rf"(?:rule|section|iop|lar|_){re.escape(n)}$", (h.get("source") or "").lower()):
        return True
    # chunk_id is a content hash, so chunks of a source have NO order - "heading before the quote" cannot be read
    # across chunks. Instead: some chunk (same source or a sibling ingest of the same law) must carry BOTH the
    # provision heading and the start of the quote.
    head = _MARK + r"\s*" + re.escape(n) + r"\s*\.(?!\d)"
    probe = q[:60]
    for (t,) in con.execute("SELECT text FROM chunk_text WHERE text LIKE ? LIMIT 50", ("%" + probe.replace("%", "") + "%",)):
        if re.search(head, _norm(t)) and probe in _norm(t):
            return True
    return False


_NOTE = r"(?:Notes? of (?:the )?Advisory Committee|Committee Notes? on Rules|Committee Comments?|Comment:|Official Note|Explanatory Comment|Note to Rule)"


def _source_blob(con, source: str) -> str:
    return _norm(" ".join(t for (t,) in con.execute("SELECT text FROM chunk_text WHERE source=?", (source,))))


def _in_commentary(con, source: str, q: str) -> bool:
    """Guardrail: a quote from an Advisory Committee Note / Comment is not the rule (FRAP 3's 1998 Committee Note was
    GROUNDED by m2 on 2026-10-18). Chunks have no order, so decide inside the quote's own chunk: a note heading after
    the last provision heading -> commentary; a chunk with no provision heading whose rule text (the source's own
    rule heading) is absent and that carries note vocabulary -> commentary."""
    for (t,) in con.execute("SELECT text FROM chunk_text WHERE source=? AND text LIKE ?", (source, "%" + q[:60].replace("%", "") + "%")):
        t = _norm(t); i = t.find(q[:60])
        pre = t[:i]
        notes = [m.start() for m in re.finditer(_NOTE, pre, re.I)]
        heads = [m.start() for m in re.finditer(_MARK + r"\s*\d+(?:[.\-]\d+)*\s*\.\s+[A-Z]", pre)]
        if notes and (not heads or notes[-1] > heads[-1]):
            return True
    return False


def _chapeau(con, source: str, q: str, n: str = "") -> str:
    """A sub-paragraph quote ("(3) The judge knows ...") carries no duty alone; return the verbatim operative lead-in
    that follows the provision heading ("Rule 2.11. ... (A) A judge shall disqualify ... circumstances:") from
    whichever chunk holds that heading (same source or a sibling ingest of the same law). Order-free."""
    if not n or not re.match(r"^\(?[0-9a-zA-Z]{1,3}[.)]", q):
        return ""
    head = _MARK + r"\s*" + re.escape(n) + r"\s*\.(?!\d)"
    lead = r"\([A-Za-z0-9]{1,3}\)\s[^()]{0,400}?\b(?:shall|must|may not)\b[^:]{0,400}:"
    for (t,) in con.execute("SELECT text FROM chunk_text WHERE source=? AND text LIKE ?", (source, f"%{n}.%")):   # same law only (207 ch51 MDJ Rule 2.11 is a different code)
        t = _norm(t); m = re.search(head, t)
        if m:
            l = re.search(lead, t[m.end():m.end() + 1200])
            if l:
                return l.group(0).strip()
    return ""


def discover_ladder_gaps(pairs=None, stage: bool = True, k: int = 12, force: bool = False) -> list:
    """For every NOT MAPPED statute/rule level of every live defect class: semantic retrieval scoped to the
    level's corpus family -> LLM picks the provision (or none) -> deterministic gates (quote verbatim in
    the candidate + _verbatim on the whole source + the cite's section number present in the text) ->
    STAGED in LADDER_PROPOSALS for m2. Nothing goes live here."""
    from pipeline.m2 import analysis_runner as A
    con = _con()
    census = family_census(con)
    dead = [f for f, n in census.items() if not n]
    if dead:
        print(f"[ladder] family prefixes matching NO corpus source (fix LEVEL_FAMILY or ingest): {dead}")
    props = json.loads(LADDER_PROPOSALS.read_text()) if LADDER_PROPOSALS.exists() else []
    _cut = (datetime.datetime.now() - datetime.timedelta(days=7)).isoformat()
    fam_n = {key: sum(census.get(f, 0) for f in fams) for key, (fams, _l) in LEVEL_FAMILY.items()}
    # A negative (NO_PICK / NO_CANDIDATES / GAP) stands only while the level's corpus families are unchanged: any ingest
    # into them (fam_n differs, or a row recorded before fam_n existed) re-opens the level at once; otherwise weekly.
    done = {(x["defect_class"], x["forum"], x["kind"]) for x in props
            if not force and (x.get("status") in ("STAGED", "GROUNDED") or (
                x.get("status") in ("NO_PICK", "NO_CANDIDATES", "GAP") and x.get("ts", "") > _cut
                and x.get("fam_n") == fam_n.get((x["forum"], x["kind"]))))}
    staged = []
    for cls, forum in (pairs or live_defect_classes()):
        meaning = _defect_meaning(cls)
        ladder = defect_ladder(cls, forum, con)
        known = "; ".join(f'{r["name"]}: {r["point"]}' for r in ladder if r.get("quote"))[:600]
        for r in ladder:
            if r["status"] != "NOT MAPPED" or (forum, r["kind"]) not in LEVEL_FAMILY or (cls, forum, r["kind"]) in done:
                continue
            fams, famlabel = LEVEL_FAMILY[(forum, r["kind"])]
            qs = _duty_queries(cls, meaning, forum, r["kind"], famlabel, known) + [f"{famlabel}: {meaning}", f"{meaning} - the duty imposed by {famlabel}", f"{meaning}. {known[:300]}",
                  f"{'Pennsylvania' if forum == 'PA' else 'federal'} {'statute' if r['kind'] == 'statute' else 'rule of court'} requiring: {meaning}"]
            idx_hits = [h for h in A._retrieve(qs, fams, k * 12) if any((h.get("source") or "").startswith(f) for f in fams)]
            scan_hits = _family_scan(qs, fams, k, census)
            seen_c, cands = set(), []
            for h in sorted(idx_hits + scan_hits, key=lambda h: -h.get("score", 0)):   # merge both recall paths by similarity
                if h["text"][:80] not in seen_c:
                    seen_c.add(h["text"][:80]); cands.append(h)
            cands = cands[:k]
            row = {"defect_class": cls, "forum": forum, "kind": r["kind"], "meaning": meaning,
                   "ts": datetime.datetime.now().isoformat(timespec="seconds"), "fam_n": fam_n.get((forum, r["kind"]))}
            if not cands:
                row.update(status="NO_CANDIDATES", note=f"no {famlabel} text retrieved — pull per legal-corpus-ingest")
                props.append(row); print(f"[ladder] {cls} {forum} {r['kind']}: NO CANDIDATES in {fams}"); continue
            user = (f"DEFECT: {meaning}\nFORUM: {'Pennsylvania state court' if forum == 'PA' else 'federal court'}\n"
                    f"LEVEL: {famlabel}\nALREADY-KNOWN AUTHORITY (other levels): {known or 'none'}\n\nCANDIDATES:\n"
                    + "\n\n".join(f"[{i}] source={h['source']}\n{h['text'][:1100]}" for i, h in enumerate(cands)))
            try:
                pick = _llm_json(_SELECT_SYS, user)
            except Exception as e:
                print(f"[ladder] {cls} {forum} {r['kind']}: LLM error {e}"); continue
            i = pick.get("pick")
            if isinstance(i, str) and i.strip().strip("[]").isdigit():
                i = int(i.strip().strip("[]"))
            if not isinstance(i, int) or not (0 <= i < len(cands)):
                row.update(status="NO_PICK", why=pick.get("why", ""), candidates=[h["source"] for h in cands])
                props.append(row); print(f"[ladder] {cls} {forum} {r['kind']}: no candidate imposes the duty ({pick.get('why','')[:120]})"); continue
            h, q = cands[i], _norm(pick.get("quote", ""))
            nums = _provision_numbers(pick.get("name", ""))
            gates = {"quote_in_candidate": len(q) >= 60 and q in _norm(h["text"]),
                     "verbatim_in_source": bool(_verbatim(con, h["source"], [re.escape(q)])) if q else False,
                     "cite_number_in_text": bool(nums) and all(_cite_number_ok(con, h, q, n) for n in nums),
                     "rule_text_not_commentary": not _in_commentary(con, h["source"], q)}
            row.update(name=pick.get("name", ""), cite=pick.get("name", ""), source=h["source"], chunk_id=h.get("chunk_id"),
                       locators=[re.escape(q)], quote=q, chapeau=_chapeau(con, h["source"], q, (nums or [""])[0]), point=pick.get("point", ""), why=pick.get("why", ""), gates=gates,
                       status="STAGED" if all(gates.values()) else "GATE_FAIL")
            props.append(row); staged.append(row)
            print(f"[ladder] {cls} {forum} {r['kind']}: {row['status']} {row['name']} [{h['source']}] {gates}")
    con.close()
    if stage:
        LADDER_PROPOSALS.parent.mkdir(parents=True, exist_ok=True)
        LADDER_PROPOSALS.write_text(json.dumps(props, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"[ladder] proposals -> {LADDER_PROPOSALS} (m2: python3 -m pipeline.m2.ladder_confirm)")
    return staged


def main():
    if "--ladder-gaps" in sys.argv:            # nightly: m0 stages ladder-level proposals; m2 ladder_confirm promotes
        discover_ladder_gaps(force="--force" in sys.argv); sys.exit(0)
    ap = argparse.ArgumentParser()
    ap.add_argument("stub")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--discover", action="store_true")
    a = ap.parse_args()
    p = Path(a.stub)
    text = p.read_text(encoding="utf-8")
    m = re.search(r"^## Claim taxonomy\s*\n+([^\n#][^\n]*)", text, re.M)
    if not m:
        sys.exit("[spine] stub has no '## Claim taxonomy'")
    taxonomy = m.group(1).strip()
    section, report = build_section(taxonomy)
    for r in report:
        print("[spine]", json.dumps(r, ensure_ascii=False))
    if a.discover:
        discover(taxonomy, stage="--stage" in sys.argv)
    ok = inject(p, section, a.dry_run)
    sys.exit(0 if ok else 2)


if __name__ == "__main__":
    main()
