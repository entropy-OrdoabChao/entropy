# -*- coding: utf-8 -*-
"""
originator_taxonomy.py
=======================
For each standalone violation type, defines:
  - Cause of action (statute)
  - Who to sue (defendant class)
  - Pre-suit demand required?
  - Agency complaint (enforcement referral)
  - Statute of limitations
  - Typical damages

Drives Track B (new-case origination).
"""

ORIGINATORS = {
    # ========== FAIR HOUSING / HUD ==========
    "FAIR_HOUSING_DISCRIMINATION": {
        "trigger_keywords": ["fair housing", "housing discrimination", "denied housing",
                             "rental denial", "mortgage denial", "redlining", "steering",
                             "reasonable accommodation denied", "hud complaint",
                             "disparate impact", "disparate treatment"],
        "statutes": ["42 USC 3604", "42 USC 3605", "42 USC 3617", "PA 43 PS 952"],
        "cause_of_action": "Fair Housing Act discrimination",
        "defendants": ["landlord", "lender", "real_estate_agent", "seller", "property_manager"],
        "agency_complaint": {
            "agency": "HUD",
            "form": "HUD-903",
            "url": "https://www.hud.gov/program_offices/fair_housing_equal_opp/online-complaint",
            "deadline_days": 365
        },
        "pre_suit_demand": True,
        "demand_days": 30,
        "sol_years": 2,
        "damages": ["actual", "punitive", "attorneys_fees", "injunction"],
        "court": "EDPA",
        "urgency": "HIGH"
    },

    # ========== CREDIT / FCRA ==========
    "FCRA_FAILURE_TO_INVESTIGATE": {
        "trigger_keywords": ["fcra", "credit report dispute", "failure to investigate",
                             "inaccurate credit", "refused to correct", "reinvestigation",
                             "credit bureau", "tradeline dispute"],
        "statutes": ["15 USC 1681i", "15 USC 1681n", "15 USC 1681o"],
        "cause_of_action": "FCRA failure to investigate",
        "defendants": ["equifax", "experian", "transunion", "furnisher"],
        "agency_complaint": {
            "agency": "CFPB",
            "form": "CFPB complaint portal",
            "url": "https://www.consumerfinance.gov/complaint/",
            "deadline_days": None
        },
        "pre_suit_demand": True,
        "demand_days": 30,
        "sol_years": 2,
        "damages": ["actual", "statutory_100_to_1000", "punitive_willful", "attorneys_fees"],
        "court": "EDPA",
        "urgency": "HIGH"
    },
    "ADVERSE_ACTION_NOTICE": {
        "trigger_keywords": ["adverse action", "credit denial", "application denied",
                             "denied credit", "denied your application",
                             "unable to approve", "your application has been declined"],
        "statutes": ["15 USC 1691", "15 USC 1691e", "15 USC 1681m"],
        "cause_of_action": "ECOA + FCRA adverse action",
        "defendants": ["creditor", "bank", "card_issuer", "lender"],
        "agency_complaint": {
            "agency": "CFPB",
            "form": "CFPB complaint portal",
            "url": "https://www.consumerfinance.gov/complaint/",
            "deadline_days": None
        },
        "pre_suit_demand": True,
        "demand_days": 30,
        "sol_years": 5,
        "damages": ["actual", "punitive_10000", "attorneys_fees"],
        "court": "EDPA",
        "urgency": "MEDIUM"
    },
    "ECOA_DISCRIMINATION": {
        "trigger_keywords": ["ecoa", "credit discrimination", "denied based on race",
                             "denied based on sex", "denied based on national origin",
                             "denied based on age", "public assistance"],
        "statutes": ["15 USC 1691", "15 USC 1691e", "12 CFR 1002"],
        "cause_of_action": "ECOA discrimination",
        "defendants": ["creditor", "bank", "lender"],
        "agency_complaint": {"agency": "CFPB", "url": "https://www.consumerfinance.gov/complaint/",
                             "deadline_days": None},
        "pre_suit_demand": True,
        "demand_days": 30,
        "sol_years": 5,
        "damages": ["actual", "punitive_10000", "attorneys_fees"],
        "court": "EDPA",
        "urgency": "HIGH"
    },

    # ========== DEBT COLLECTION ==========
    "FDCPA_VIOLATION": {
        "trigger_keywords": ["fdcpa", "debt collector", "collection agency",
                             "debt collection", "validation of debt", "cease and desist",
                             "collection call", "wage garnishment threat"],
        "statutes": ["15 USC 1692", "15 USC 1692e", "15 USC 1692k"],
        "cause_of_action": "FDCPA violations",
        "defendants": ["debt_collector", "collection_agency", "assignee"],
        "agency_complaint": {"agency": "CFPB", "url": "https://www.consumerfinance.gov/complaint/",
                             "deadline_days": None},
        "pre_suit_demand": False,  # strict liability; no demand needed
        "demand_days": 0,
        "sol_years": 1,
        "damages": ["actual", "statutory_1000", "attorneys_fees"],
        "court": "EDPA",
        "urgency": "MEDIUM"
    },

    # ========== MORTGAGE / TILA / RESPA ==========
    "TILA_VIOLATION": {
        "trigger_keywords": ["tila", "truth in lending", "rescission",
                             "disclosure violation", "annual percentage rate"],
        "statutes": ["15 USC 1601", "15 USC 1635", "15 USC 1640"],
        "cause_of_action": "TILA disclosure violation",
        "defendants": ["lender", "mortgage_servicer", "bank"],
        "agency_complaint": {"agency": "CFPB", "url": "https://www.consumerfinance.gov/complaint/",
                             "deadline_days": None},
        "pre_suit_demand": True,
        "demand_days": 30,
        "sol_years": 1,  # 3 for rescission
        "damages": ["actual", "statutory", "attorneys_fees", "rescission"],
        "court": "EDPA",
        "urgency": "MEDIUM"
    },
    "RESPA_VIOLATION": {
        "trigger_keywords": ["respa", "real estate settlement", "qualified written request",
                             "qwr", "escrow error", "servicing error", "loss mitigation",
                             "force placed insurance"],
        "statutes": ["12 USC 2605", "12 USC 2607", "12 CFR 1024"],
        "cause_of_action": "RESPA servicing violation",
        "defendants": ["mortgage_servicer", "lender"],
        "agency_complaint": {"agency": "CFPB", "url": "https://www.consumerfinance.gov/complaint/",
                             "deadline_days": None},
        "pre_suit_demand": True,
        "demand_days": 30,
        "sol_years": 3,
        "damages": ["actual", "statutory", "attorneys_fees"],
        "court": "EDPA",
        "urgency": "MEDIUM"
    },

    # ========== INSURANCE ==========
    "INSURANCE_BAD_FAITH": {
        "trigger_keywords": ["bad faith", "claim denied", "insurance denial",
                             "coverage denied", "wrongful denial", "unreasonable denial"],
        "statutes": ["42 Pa.C.S. 8371", "PA Unfair Insurance Practices Act"],
        "cause_of_action": "Insurance bad faith",
        "defendants": ["insurer", "adjuster"],
        "agency_complaint": {"agency": "PA_INSURANCE_DEPT",
                             "url": "https://www.insurance.pa.gov/Consumers/FileAComplaint",
                             "deadline_days": None},
        "pre_suit_demand": True,
        "demand_days": 30,
        "sol_years": 4,
        "damages": ["actual", "punitive", "interest_prime_plus_3", "attorneys_fees"],
        "court": "PA_CCP",
        "urgency": "MEDIUM"
    },

    # ========== PRIVACY / DATA ==========
    "GLBA_PRIVACY_VIOLATION": {
        "trigger_keywords": ["nonpublic personal information", "financial privacy",
                             "data breach", "unauthorized disclosure",
                             "privacy policy violation"],
        "statutes": ["15 USC 6801", "15 USC 6802", "16 CFR 313"],
        "cause_of_action": "GLBA privacy violation",
        "defendants": ["bank", "financial_institution"],
        "agency_complaint": {"agency": "FTC", "url": "https://reportfraud.ftc.gov/",
                             "deadline_days": None},
        "pre_suit_demand": True,
        "demand_days": 30,
        "sol_years": 2,
        "damages": ["actual", "injunction"],
        "court": "EDPA",
        "urgency": "LOW"
    },

    # ========== STATE CONSUMER PROTECTION ==========
    "PA_UTPCPL": {
        "trigger_keywords": ["unfair trade", "deceptive practice", "utpcpl",
                             "consumer protection", "fraud", "misrepresentation"],
        "statutes": ["73 P.S. 201-1", "73 P.S. 201-9.2"],
        "cause_of_action": "PA Unfair Trade Practices",
        "defendants": ["merchant", "seller", "service_provider"],
        "agency_complaint": {"agency": "PA_AG",
                             "url": "https://www.attorneygeneral.gov/submit-a-complaint/"},
        "pre_suit_demand": True,
        "demand_days": 30,
        "sol_years": 6,
        "damages": ["treble", "attorneys_fees"],
        "court": "PA_CCP",
        "urgency": "LOW"
    },

    # ========== REGULATORY FAILURE / DEPARTMENT TORT (escalation AFTER admin exhaustion) ==========
    "REGULATORY_FAILURE_DEPARTMENT_TORT": {
        "trigger_keywords": ["regulatory failure", "failure to enforce", "failure to regulate",
                             "abandonment of duty to regulate", "concurred with denial",
                             "department of insurance", "department of banking and insurance",
                             "dobi", "insurance commissioner", "regulator failed"],
        "statutes": ["NJAC 11:2-17", "N.J.S.A. 17:29B-4", "N.J.S.A. 17:28-2",
                     "42 U.S.C. 1983", "NJ Tort Claims Act (N.J.S.A. 59:1-1 et seq.)"],
        "cause_of_action": "Regulatory failure / department tort (regulator breach of duty to enforce)",
        "defendants": ["PA_INSURANCE_DEPT", "NJ_DOBI", "insurance_commissioner", "investigator"],
        "agency_complaint": {"agency": "STATE_AG_OR_FEDERAL_OIG",
                             "note": "escalation body only; the department tort IS the suit -- admin "
                                     "exhaustion (Notice of Claim / determination) is a prerequisite",
                             "deadline_days": None},
        "pre_suit_demand": True,
        "demand_days": 90,
        "sol_years": 2,
        "damages": ["actual", "injunction", "attorneys_fees"],
        "court": "forum depends on defendant -- see forum_by_defendant",
        "forum_by_defendant": {
            "PA_INSURANCE_DEPT": {"court": "PA Commonwealth Court", "level": "state appellate court, original jurisdiction",
                                  "basis": "42 Pa.C.S. 761 (original jurisdiction over Commonwealth parties); sovereign immunity 42 Pa.C.S. 8522 exceptions + 8528 damage caps"},
            "NJ_DOBI": {"court": "NJ Superior Court, Law Division", "level": "state trial court",
                        "basis": "NJ Tort Claims Act N.J.S.A. 59:1-1 et seq.; Notice of Claim + 6-month wait is a prerequisite"},
            "section_1983": {"court": "federal district (E.D. Pa. / D.N.J.) or state concurrent", "level": "federal trial court",
                             "basis": "42 U.S.C. 1983"}
        },
        "urgency": "HIGH",
        "requires_admin_exhaustion": True
    },
}


def match_originators(text: str, existing_violation_types=None) -> list:
    """
    Return list of originator keys whose trigger_keywords appear in text.
    """
    if not text:
        return []
    t = text.lower()
    hits = []
    for key, spec in ORIGINATORS.items():
        for kw in spec["trigger_keywords"]:
            if kw.lower() in t:
                hits.append(key)
                break
    # Also include direct matches by violation_type
    if existing_violation_types:
        for v in existing_violation_types:
            v_norm = str(v).upper().replace(" ", "_").replace("-", "_")
            if v_norm in ORIGINATORS and v_norm not in hits:
                hits.append(v_norm)
    return hits


def get_originator(key: str) -> dict | None:
    return ORIGINATORS.get(key)


# BEEZLE_FN_UNUSED 2026-06-13: all_originator_keys (no static refs in pipeline/)
def all_originator_keys() -> list:
    return list(ORIGINATORS.keys())
