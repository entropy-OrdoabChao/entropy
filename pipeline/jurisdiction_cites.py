# -*- coding: utf-8 -*-
"""Single source of truth for jurisdiction-appropriate citations. The composer/comprehension know a case's
court (is_federal); this maps each legal CONCEPT to its PA-state cite and its federal cite so the SAME block
prose renders the correct authority. Federal EDPA adopts the PA Rules of Professional Conduct (E.D. Pa. L.R.
83.6 IV.B), so candor stays R.P.C. 3.3 in both; where a concept is truly one-jurisdiction-only (e.g. the
Pa.R.A.P. 1925 concise-statement regime has no federal analog) that is marked.

Usage:  from pipeline.jurisdiction_cites import cites_for ;  J = cites_for(is_federal)  ->  {concept: cite}
        blocks reference {{j_<concept>}} which the composer sets from J.
"""

JURISDICTION_CITES = {
    # concept                : (PA / state cite,                                   federal cite)
    "judicial_oath":          ("Pa. Const. art. VI, § 3",
                               "28 U.S.C. § 453"),
    "judicial_oath_text":     ("I do solemnly swear (or affirm) that I will support, obey and defend the "
                               "Constitution of the United States and the Constitution of this Commonwealth, "
                               "and that I will discharge the duties of my office with fidelity",
                               "I will administer justice without respect to persons, and do equal right to the "
                               "poor and to the rich, and … faithfully and impartially discharge and perform all "
                               "the duties incumbent upon me … under the Constitution and laws of the United States"),
    "canons":                 ("Code of Judicial Conduct, Rules 2.2, 2.6, 2.9, 2.11",
                               "Code of Conduct for United States Judges, Canons 2 and 3, and 28 U.S.C. § 455"),
    "disqualification":       ("Code of Judicial Conduct, Rule 2.11",
                               "28 U.S.C. § 455(a)-(b)"),
    "candor":                 ("Pa.R.P.C. 3.3",
                               "Pa.R.P.C. 3.3 (adopted by E.D. Pa. L.R. 83.6); Fed. R. Civ. P. 11(b)"),
    "deemed_admission":       ("Pa.R.C.P. 1029(b)",
                               "Fed. R. Civ. P. 8(b)(6) (an allegation is admitted if a responsive pleading is "
                               "required and it is not denied)"),
    "personal_knowledge":     ("Pa.R.E. 602; Salvitti v. Throppe, 343 Pa. 642, 23 A.2d 445 (1942)",
                               "Fed. R. Evid. 602"),
    "verification":           ("Pa.R.C.P. 1024",
                               "28 U.S.C. § 1746 (unsworn declaration under penalty of perjury); "
                               "Fed. R. Civ. P. 11(b)"),
    "unsworn_falsification":  ("18 Pa.C.S. § 4904",
                               "28 U.S.C. § 1746; 18 U.S.C. §§ 1621, 1001"),
    "reasons_for_ruling":     ("Pa.R.A.P. 1925(a) (a written opinion stating the reasons for the order)",
                               "Fed. R. Civ. P. 52(a) findings and the duty to state the basis for the ruling"),
    "waiver_rule":            ("Pa.R.A.P. 1925(b)(4)(vii)",
                               "[no federal concise-statement-of-errors analog; see Fed. R. App. P. 3-4 and the "
                               "issue-preservation doctrine]"),
    "due_process_property":   ("U.S. Const. amend. V and Pa. Const. art. I, §§ 1, 9, 11",
                               "U.S. Const. amend. V (due process and the taking of property without just "
                               "compensation)"),
    "garnishee_default":      ("Pa.R.C.P. 3146(a)",
                               "Fed. R. Civ. P. 69(a) (execution proceeds under the law of the state) and the "
                               "applicable state garnishment rule"),
    "post_appeal_authority":  ("Pa.R.A.P. 1701(b)",
                               "the divestiture rule: a notice of appeal transfers jurisdiction to the court of "
                               "appeals (Griggs v. Provident Consumer Discount Co., 459 U.S. 56 (1982))"),
    # These are federal-constitutional / common-law and identical in both fora (no split):
    "immunity_limits":        ("Stump v. Sparkman, 435 U.S. 349 (1978); Bradley v. Fisher, 80 U.S. 335 (1871); "
                               "Pulliam v. Allen, 466 U.S. 522 (1984)",
                               "Stump v. Sparkman, 435 U.S. 349 (1978); Bradley v. Fisher, 80 U.S. 335 (1871); "
                               "Pulliam v. Allen, 466 U.S. 522 (1984)"),
    "fair_tribunal":          ("In re Murchison, 349 U.S. 133 (1955); Caperton v. A.T. Massey Coal Co., 556 U.S. "
                               "868 (2009)",
                               "In re Murchison, 349 U.S. 133 (1955); Caperton v. A.T. Massey Coal Co., 556 U.S. "
                               "868 (2009)"),
}


def cites_for(is_federal: bool) -> dict:
    """Return {concept: cite} for the jurisdiction. Blocks use {{j_<concept>}}."""
    idx = 1 if is_federal else 0
    return {k: v[idx] for k, v in JURISDICTION_CITES.items()}
