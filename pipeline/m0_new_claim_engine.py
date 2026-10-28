"""m0_new_claim_engine — m0 decides the FORUM for a NEW claim and whether a Phila-CCP filing
RESISTS removal to federal. Data-driven, no hardcoded party (R2). All citations are black-letter.

THREE SEPARATE QUESTIONS (kept distinct on purpose — this is the part to be crystal-clear about):

1) WHERE CAN I SUE?  (personal jurisdiction — about the defendant's contacts, NOT diversity)
   PA has personal jurisdiction over a defendant that is:
     - a PA citizen (at home), OR
     - registered / doing business in PA -> CONSENT to general jurisdiction
       (Mallory v. Norfolk Southern, 600 U.S. 122 (2023); 42 Pa.C.S. 5301(a)(2)(i)), OR
     - sued on a claim arising from its PA contacts (long-arm, 42 Pa.C.S. 5322).
   => Default forum = Court of Common Pleas of Philadelphia County.

2) IS FEDERAL AVAILABLE?
   - Diversity (28 U.S.C. 1332): COMPLETE diversity (no defendant shares plaintiff's citizenship)
     AND amount in controversy > $75,000. Corporate citizenship = state of incorporation AND
     principal place of business (28 U.S.C. 1332(c)(1)) -- NOT merely "doing business."
   - Federal question (28 U.S.C. 1331): a federal statute (FCRA/FDCPA/TILA/ECOA).

3) CAN A PHILA-CCP FILING BE REMOVED TO FEDERAL?  (removal resistance -- the litigant's lever)
   - Forum-defendant rule (28 U.S.C. 1441(b)(2)): a DIVERSITY case may NOT be removed if any
     properly joined & served defendant is a CITIZEN of the forum state (PA). So a PA-citizen
     co-defendant defeats diversity removal.
   - No complete diversity (any PA-citizen defendant) -> no 1332 removal at all.
   - CAVEAT (honest): a FEDERAL-QUESTION claim (e.g. pure FCRA) is removable under 1331 even with
     a forum defendant. To stay in Phila CCP, anchor with a PA-citizen defendant and/or plead
     STATE-law claims (PA UTPCPL, 73 P.S. 201) so the well-pleaded complaint isn't purely federal.
   - "Doing business in PA" alone does NOT defeat removal -- it is personal jurisdiction, not
     citizenship. Removal is defeated by a defendant that is actually a PA CITIZEN.
"""
from __future__ import annotations

PLAINTIFF_STATE = "PA"
DIVERSITY_AMOUNT = 75000
PHILA_CCP = "phila_ccp:Court of Common Pleas of Philadelphia County"
EDPA = "fed:U.S. District Court, Eastern District of Pennsylvania"

CLAIM_STATUTES = {
    "credit_reporting": ["15 U.S.C. 1681 et seq. (FCRA)"],
    "fcra_furnisher":   ["15 U.S.C. 1681s-2 (FCRA furnisher)"],
    "debt_collection":  ["15 U.S.C. 1692 et seq. (FDCPA)"],
    "credit_card":      ["15 U.S.C. 1601 et seq. (TILA)", "15 U.S.C. 1666 (FCBA)"],
    "ecoa":             ["15 U.S.C. 1691 et seq. (ECOA)"],
    "medical_billing":  ["15 U.S.C. 1692 (FDCPA - medical debt)", "73 P.S. 201-1 et seq. (PA UTPCPL)"],
    "medical_privacy":  ["73 P.S. 201 (PA UTPCPL)", "HIPAA (no private right of action)"],
    "insurance_bad_faith": ["42 Pa.C.S. 8371"],
    "consumer_protection": ["73 P.S. 201-1 et seq. (PA UTPCPL)"],
    "nj_tort_claims_act": ["N.J.S.A. 59:1-1 et seq. (New Jersey Tort Claims Act; suit after notice, 59:8-8)"],
}
FEDERAL_QUESTION_TYPES = {"credit_reporting", "fcra_furnisher", "debt_collection", "credit_card", "ecoa"}

# Court-required companion forms that MUST be drafted + attached to a NEW complaint.
REQUIRED_FORMS = {
    "phila_ccp": [
        "Notice to Defend (Pa.R.C.P. 1018.1) - REQUIRED on the face of the complaint",
        "Philadelphia Civil Cover Sheet (commencement of action)",
        "Verification (Pa.R.C.P. 1024)",
        "Certificate of Service",
    ],
    "fed": [
        "Civil Cover Sheet (JS-44)",
        "Summons (AO 440) - ONE per defendant",
        "FRCP 7.1 Disclosure Statement (corporate/financial disclosure)",
        "EDPA Case Management Track Designation Form (E.D. Pa. Local Rule)",
        "Certificate of Service",
    ],
    "nj_super": [
        "Case Information Statement (R. 4:5-1(b)(1)) - annexed to the first pleading",
        "Certification of no other actions / parties (R. 4:5-1(b)(2))",
        "Certification of redacted confidential personal identifiers (R. 1:38-7(b))",
        "Summons (R. 4:4-2) - ONE per defendant; the State is served on the Attorney General (R. 4:4-4(a))",
        "Designation of trial counsel / jury demand (R. 4:25-4; R. 1:8-2(b), 4:35-1) - as elected",
        "Certification / proof of service",
    ],
}
# Claim-type predicate documents to DRAFT and attach as exhibits to the complaint.
CLAIM_ATTACHMENTS = {
    "credit_reporting": ["FCRA 1681i dispute letters to the CRAs + their responses", "credit-report excerpts showing the inaccuracy"],
    "fcra_furnisher":   ["dispute sent to the furnisher + ACDV/response", "report excerpts"],
    "debt_collection":  ["FDCPA 1692g validation/dispute correspondence", "Notice of Default (if the claim is default-based)"],
    "credit_card":      ["FCBA 1666 billing-error notice + creditor response", "statements showing the disputed charge"],
    "ecoa":             ["the adverse-action notice (or its absence)", "the credit application"],
    "medical_billing":  ["itemized bill + written dispute", "Notice of Default (if in collection)"],
    "insurance_bad_faith": ["claim submission + denial correspondence", "proof of loss"],
    "consumer_protection": ["the deceptive/unfair communication", "Notice of Default / demand letter (if applicable)"],
    "nj_tort_claims_act": ["the Notice of Claim (N.J.S.A. 59:8-4 contents)", "proof of delivery/receipt of the notice (starts the 6-month wait, 59:8-8)",
                           "the agency's written disposition (if any)"],
}

# SOVEREIGN DEFENDANTS (2026-10-20, S-amjur-gaps). A State or its agency is NOT a diversity citizen and is immune in
# federal court on state-law claims; the state's own waiver statute names where it may be sued. Data, not code: a new
# state = one row. Every proposition is pinned to a corpus chunk the router quotes back. 'gap' = NOT IN CORPUS.
SOVEREIGN_KINDS = {"state", "state_agency", "state_official_official_capacity"}
SOVEREIGN_LAW = [
    ("A State is not a diversity citizen", "There is no question that a State is not a \"citizen\" for purposes of the diversity jurisdiction",
     "Moor v. County of Alameda, 411 U.S. 693", "212681c2bd3e0c87051f12befd37e142"),
    ("No federal jurisdiction over state-law claims against the State",
     "pendent jurisdiction nor any other basis of jurisdiction may override the Eleventh Amendment",
     "Pennhurst State School & Hospital v. Halderman, 465 U.S. 89, 121 (as quoted, 470 U.S.)", "USREPORTS-470_PDFA_chunk_581"),
    ("Immunity covers WHERE the State may be sued",
     "encompasses not merely whether it may be sued, but where it may be sued",
     "Pennhurst, 465 U.S. 89", "USREPORTS-465_PDFA_chunk_15"),
    ("State / official-capacity defendants are not 1983 persons",
     "a State nor its officials acting in their official capacities are",
     "Will v. Michigan Dept. of State Police, 491 U.S. 58, 71", "dce4161713b976f0"),
    ("Individual-capacity officials ARE 1983 persons",
     "state officials sued in their individual capacities are \u201cpersons\u201d for purposes of § 1983",
     "Hafer v. Melo, 502 U.S. 21", "1bb0f96b5f5f750b"),
    ("Officer acting outside the law is not the sovereign",
     "an action against a state officer seeking redress for conduct not permitted by state law is a suit against the officer, not the sovereign",
     "Pennhurst, 465 U.S. 89 (describing Ex parte Young)", "USREPORTS-465_PDFA_chunk_1470"),
]
SOVEREIGN_GAPS = ["Franchise Tax Bd. of Cal. v. Hyatt, 587 U.S. 230 (2019) - whether a State may be sued in ANOTHER State's courts "
                  "(Nevada v. Hall, held in corpus, was overruled by it) - NOT IN CORPUS: pull before choosing a PA state forum"]
# the State's own courts, per its waiver statute
SOVEREIGN_HOME_COURT = {
    "NJ": ("nj_super", "nj_super:Superior Court of New Jersey, Law Division",
           "N.J.S.A. 59:8-8: after six months from receipt of the notice of claim 'the claimant may file suit in an appropriate court of law'",
           "nj_stat_t59_s8-8", "R. 4:3-2 (venue)"),
}


def required_forms(court_key, claim):
    forms = list(REQUIRED_FORMS.get(court_key, []))
    if claim.get("ifp"):
        forms.append("Petition to Proceed In Forma Pauperis (Pa.R.C.P. 240)" if court_key == "phila_ccp"
                     else "Application to proceed as an indigent / waiver of fees (R. 1:13-2)" if court_key == "nj_super"
                     else "Application to Proceed Without Prepaying Fees (AO 240; 28 U.S.C. 1915)")
    return forms


def _is_pa_citizen(d, plaintiff_state=PLAINTIFF_STATE):
    """Corporate citizenship (1332(c)(1)) = incorporation OR principal place of business.
    Accepts d['state'] (citizenship) and optional d['ppb']. 'does_business' does NOT count."""
    st = (d.get("state") or "").upper()
    ppb = (d.get("ppb") or "").upper()
    return st == plaintiff_state.upper() or ppb == plaintiff_state.upper()


def _can_sue_in_pa(d):
    """Personal jurisdiction in PA: PA citizen, OR registered/doing business (consent, Mallory /
    5301), OR claim arises from PA contacts (long-arm 5322)."""
    if _is_pa_citizen(d):
        return "PA citizen (at home)"
    if d.get("does_business_in_pa") or d.get("registered_in_pa"):
        return "consent by doing business/registration (Mallory; 42 Pa.C.S. 5301)"
    if d.get("pa_contacts"):
        return "long-arm over PA-related claim (42 Pa.C.S. 5322)"
    return None


def _is_sovereign(d) -> bool:
    return (d.get("kind") or "").lower() in SOVEREIGN_KINDS


def route_sovereign(claim: dict, sov: list) -> dict:
    """A claim naming a State / state agency. Diversity is unavailable against it and the Eleventh Amendment keeps its
    state-law claims out of federal court, so the state-law claim goes to the State's own court under its waiver
    statute; the FEDERAL option is a 1983 claim against officials in their INDIVIDUAL capacity (or prospective relief)."""
    ctype = (claim.get("claim_type") or "").lower()
    states = sorted({(d.get("state") or "").upper() for d in sov})
    home = SOVEREIGN_HOME_COURT.get(states[0]) if len(states) == 1 else None
    individuals = [d.get("name") for d in (claim.get("defendants") or [])
                   if (d.get("kind") or "").lower() == "state_official_individual_capacity"]
    out = {
        "sovereign_defendants": [d.get("name") for d in sov],
        "law": [{"rule": r, "quote": q, "cite": c, "chunk_id": k} for r, q, c, k in SOVEREIGN_LAW],
        "not_in_corpus": list(SOVEREIGN_GAPS),
        "federal_option": ("42 U.S.C. 1983 against the officials in their INDIVIDUAL capacity (Hafer) / prospective relief "
                           "against the officer (Ex parte Young line) - NOT against the State or the agency; personal "
                           "jurisdiction + venue (28 U.S.C. 1391(b)) over out-of-state officials must be shown separately")
                          + (f"; individual-capacity defendants named: {individuals}" if individuals else
                             "; no individual-capacity defendant named yet"),
        "statutes": CLAIM_STATUTES.get(ctype, []),
        "claim_attachments_to_draft": CLAIM_ATTACHMENTS.get(ctype, []),
        "complete_diversity": False,
        "removal_resistance": "n/a - a State defendant cannot be the basis of diversity removal (not a citizen)",
    }
    if home:
        key, court, basis, chunk, venue = home
        out.update(court=court, court_key=key, basis=f"{basis} [corpus:{chunk}]", venue=venue,
                   required_forms=required_forms(key, claim))
    else:
        out.update(court=None, court_key=None, required_forms=[], venue=None,
                   basis=f"NO HOME-COURT ROW for sovereign state(s) {states} - add one to SOVEREIGN_HOME_COURT (its waiver statute) before routing")
    return out


def route_claim(claim: dict) -> dict:
    pstate = (claim.get("plaintiff_state") or PLAINTIFF_STATE)
    defs = claim.get("defendants") or []
    sov = [d for d in defs if _is_sovereign(d)]
    if sov:
        return route_sovereign(claim, sov)
    amount = claim.get("amount") or 0
    ctype = (claim.get("claim_type") or "").lower()
    statutes = CLAIM_STATUTES.get(ctype, [])
    fed_q = ctype in FEDERAL_QUESTION_TYPES

    pa_citizen_defs = [d for d in defs if _is_pa_citizen(d, pstate)]
    complete_div = bool(defs) and not pa_citizen_defs
    diversity_ok = complete_div and amount > DIVERSITY_AMOUNT

    pj = {(d.get("name") or "?"): _can_sue_in_pa(d) for d in defs}
    no_pj = [n for n, b in pj.items() if not b]

    # removal resistance of a Phila-CCP filing
    if pa_citizen_defs:
        if fed_q:
            removal = ("PARTIAL: forum-defendant rule (28 U.S.C. 1441(b)(2)) + no complete diversity "
                       "block DIVERSITY removal, but the federal-statute claim is removable under 1331 - "
                       "anchor with the PA defendant and plead PA UTPCPL/state claims to resist")
        else:
            removal = ("STRONG: a PA-citizen defendant defeats complete diversity AND triggers the "
                       "forum-defendant rule (28 U.S.C. 1441(b)(2)) - not removable on diversity; no "
                       "federal question -> stays in Phila CCP")
    else:
        if fed_q:
            removal = "LOW: no PA-citizen defendant; federal-question claim is freely removable under 1331"
        elif diversity_ok:
            removal = "LOW: complete diversity + >$75k, no forum defendant -> removable / federal-appropriate"
        else:
            removal = "MODERATE: no PA anchor, but diversity not met (state-law claim) -> removal would be defective"

    # forum decision: Phila CCP default; federal only on clean diversity with no forum-defendant bar
    if diversity_ok and not pa_citizen_defs:
        court = EDPA
        basis = (f"diversity (28 U.S.C. 1332): complete diversity, amount ${amount:,} > $75k, "
                 f"no forum-defendant bar")
        venue = "28 U.S.C. 1391"
    else:
        court = PHILA_CCP
        basis = "plaintiff's home forum; PA Common Pleas has concurrent jurisdiction over the consumer statutes"
        venue = "Pa.R.C.P. 1006 / 2179 (Philadelphia County)"

    court_key = "fed" if court == EDPA else "phila_ccp"
    return {
        "court": court,
        "court_key": court_key,
        "basis": basis,
        "venue": venue,
        "statutes": statutes,
        "required_forms": required_forms(court_key, claim),
        "claim_attachments_to_draft": CLAIM_ATTACHMENTS.get(ctype, []),
        "federal_question_available": fed_q,
        "complete_diversity": complete_div,
        "pa_citizen_defendants": [d.get("name") for d in pa_citizen_defs],
        "amount": amount,
        "personal_jurisdiction": pj,
        "no_personal_jurisdiction_over": no_pj,
        "removal_resistance": removal,
    }


if __name__ == "__main__":
    samples = [
        {"label": "credit, all out-of-state, $120k",
         "claim_type": "credit_reporting", "amount": 120000,
         "defendants": [{"name": "Experian", "state": "TX", "does_business_in_pa": True},
                        {"name": "Equifax", "state": "GA", "does_business_in_pa": True}]},
        {"label": "credit + a PA-citizen co-defendant (removal-resistant anchor)",
         "claim_type": "credit_reporting", "amount": 120000,
         "defendants": [{"name": "Experian", "state": "TX"},
                        {"name": "Local PA furnisher", "state": "PA"}]},
        {"label": "medical billing, PA hospital, $4k",
         "claim_type": "medical_billing", "amount": 4000,
         "defendants": [{"name": "Phila Hospital", "state": "PA"}]},
        {"label": "credit card, VA bank, $30k (doing business in PA)",
         "claim_type": "credit_card", "amount": 30000,
         "defendants": [{"name": "Example Card Issuer", "state": "VA", "does_business_in_pa": True}]},
    ]
    samples.append({"label": "NJ DOBI (state agency) + its supervisor individually, NJ Tort Claims Act",
                    "claim_type": "nj_tort_claims_act", "amount": 200000,
                    "defendants": [{"name": "NJ Dept. of Banking and Insurance", "state": "NJ", "kind": "state_agency"},
                                   {"name": "L. Giallella", "state": "NJ", "kind": "state_official_individual_capacity"}]})
    for c in samples:
        r = route_claim(c)
        print(f"\n[{c['label']}]\n  -> {r['court']}\n     {r['basis']}")
        if r.get("sovereign_defendants"):
            print(f"     federal option: {r['federal_option']}"); print(f"     forms: {r['required_forms']}"); continue
        print(f"     PJ: {r['personal_jurisdiction']}")
        print(f"     complete_diversity={r['complete_diversity']} PA-citizen-defs={r['pa_citizen_defendants']}")
        print(f"     removal: {r['removal_resistance']}")
