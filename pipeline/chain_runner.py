# -*- coding: utf-8 -*-
"""
chain_runner.py
===============
10-agent sequential chain runner for Beezle.

Runs Agent 1 â†’ 10 in order. Passes JSON between agents (no LLM context sharing).
Each agent is a lightweight wrapper around existing pipeline modules.

Place in: <home>\\pipeline\\chain_runner.py

Usage (CLI):
    python -m pipeline.chain_runner --input email_scan_20260424_121104.json
    python -m pipeline.chain_runner --case fed:2:26-cv-00123 --violation <violation_json>

Usage (import):
    from pipeline.chain_runner import ChainRunner
    cr = ChainRunner()
    result = cr.run(input_payload, case_id="fed:2:26-cv-00123")
"""

from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import argparse
import json
import re
import logging
import os
import sys
import time
import traceback
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# === BEEZLE_ROOT_SHIM (WSL-safe; auto-injected) ===
import os as _os_broot
from pathlib import Path as _Path_broot
def _resolve_beezle_root():
    _e = _os_broot.environ.get("BEEZLE_ROOT")
    if _e:
        return _Path_broot(_e)
    _d = _Path_broot(__file__).resolve().parent
    for _ in range(5):
        if (_d / "pipeline").exists() or (_d / "obsidian_sync").exists():
            return _d
        _d = _d.parent
    return _Path_broot(str(_eroot()))
_BEEZLE_ROOT = _resolve_beezle_root()
# === END BEEZLE_ROOT_SHIM ===

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT         = _BEEZLE_ROOT
PIPELINE_DIR = ROOT / "pipeline"
OUTPUT_DIR   = ROOT / "output"
LOG_DIR      = ROOT / "logs" / "chains"
VAULT_DIR    = ROOT / "obsidian_sync" / "chains"
PACKET_DIR   = OUTPUT_DIR / "action_packets"
REGISTRY     = ROOT / "cases_index" / "case_registry.json"

for d in (LOG_DIR, VAULT_DIR, PACKET_DIR):
    d.mkdir(parents=True, exist_ok=True)

# Make pipeline importable when running as a script
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
_log_file = LOG_DIR / f"chain_runner_{datetime.now().strftime('%Y%m%d')}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(_log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("chain_runner")


# ---------------------------------------------------------------------------
# Safe-import helper â€” any missing module becomes a degraded agent instead
# of killing the whole run. Prints one warning per missing module.
# ---------------------------------------------------------------------------
_IMPORT_WARNED: set = set()

def _safe_import(module: str, attrs: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    Try to import `module`. Return dict of requested attrs (or all public names
    if attrs is None). Missing module / missing attr => None entries.
    """
    out: Dict[str, Any] = {}
    try:
        mod = __import__(module, fromlist=attrs or ["*"])
    except Exception as e:
        if module not in _IMPORT_WARNED:
            log.warning("import failed: %s (%s)", module, e)
            _IMPORT_WARNED.add(module)
        for a in attrs or []:
            out[a] = None
        out["__module__"] = None
        return out

    out["__module__"] = mod
    for a in attrs or [n for n in dir(mod) if not n.startswith("_")]:
        out[a] = getattr(mod, a, None)
    return out




# ============================================================================
# Citation expansion map â€” maps issue/violation labels to specific citations
# Used by Agent 2 and Agent 9 to query DB with the actual statute numbers
# instead of generic violation labels (which get 0 hits).
# ============================================================================
CITATION_EXPANSION = {
    # ========================================================================
    # FCRA (Equifax case â€” fed:2:26-cv-00123)
    # ========================================================================
    "FCRA":                            ["15 USC 1681i", "15 USC 1681n", "15 USC 1681o", "15 USC 1681s-2", "12 CFR 1022"],
    "FCRA_FAILURE_TO_INVESTIGATE":     ["15 USC 1681i", "15 USC 1681s-2(b)", "12 CFR 1022.43"],
    "FCRA_WILLFUL_NONCOMPLIANCE":      ["15 USC 1681n"],
    "FCRA_NEGLIGENT_NONCOMPLIANCE":    ["15 USC 1681o"],
    "FCRA_INACCURATE_REPORTING":       ["15 USC 1681e(b)", "15 USC 1681i"],
    "FCRA_FURNISHER_DUTIES":           ["15 USC 1681s-2(a)", "15 USC 1681s-2(b)", "12 CFR 1022.42"],
    "FCRA_CRA_DUTIES":                 ["15 USC 1681e", "15 USC 1681i", "12 CFR 1022.43"],
    "FCRA_PERMISSIBLE_PURPOSE":        ["15 USC 1681b", "15 USC 1681n"],
    "FCRA_IDENTITY_THEFT":             ["15 USC 1681c-2", "15 USC 1681c-1", "16 CFR 603"],
    "FCRA_FILE_DISCLOSURE":            ["15 USC 1681g", "15 USC 1681h"],
    "FCRA_CONSUMER":                   ["15 USC 1681a(c)"],
    "FCRA_CONSUMER_REPORT":            ["15 USC 1681a(d)"],
    "FCRA_CONSUMER_REPORTING_AGENCY":  ["15 USC 1681a(f)"],
    "ADVERSE_ACTION_NOTICE":           ["15 USC 1681m", "15 USC 1691(d)", "12 CFR 1002.9", "12 CFR 1022.74"],
    "ADVERSE_ACTION":                  ["15 USC 1681m", "15 USC 1691(d)"],

    # ========================================================================
    # ECOA â€” discrimination
    # ========================================================================
    "ECOA":                            ["15 USC 1691", "15 USC 1691e", "12 CFR 1002"],
    "ECOA_DISCRIMINATION":             ["15 USC 1691(a)", "12 CFR 1002.4"],
    "ECOA_NOTICE":                     ["15 USC 1691(d)", "12 CFR 1002.9"],

    # ========================================================================
    # FDCPA â€” debt collection
    # ========================================================================
    "FDCPA":                           ["15 USC 1692", "15 USC 1692e", "15 USC 1692g", "12 CFR 1006"],
    "FDCPA_VALIDATION":                ["15 USC 1692g"],
    "FDCPA_HARASSMENT":                ["15 USC 1692d"],
    "FDCPA_FALSE_REPRESENTATION":      ["15 USC 1692e"],
    "FDCPA_UNFAIR_PRACTICES":          ["15 USC 1692f"],
    "FDCPA_COMMUNICATION":             ["15 USC 1692c", "12 CFR 1006.6"],

    # ========================================================================
    # TILA â€” Truth in Lending (Amerisave/Equifax cases)
    # ========================================================================
    "TILA":                            ["15 USC 1601", "15 USC 1640", "12 CFR 1026"],
    "TILA_DISCLOSURE":                 ["15 USC 1638", "12 CFR 1026.18"],
    "TILA_RESCISSION":                 ["15 USC 1635", "12 CFR 1026.23"],
    "TILA_HOEPA":                      ["15 USC 1639", "12 CFR 1026.32"],

    # ========================================================================
    # RESPA â€” mortgage servicing (Amerisave case â€” fed:2:26-cv-00456)
    # ========================================================================
    "RESPA":                           ["12 USC 2601", "12 USC 2605", "12 USC 2607", "12 CFR 1024"],
    "RESPA_QWR":                       ["12 USC 2605(e)", "12 CFR 1024.36"],
    "RESPA_NOI":                       ["12 USC 2605(e)", "12 CFR 1024.36(c)"],
    "RESPA_KICKBACK":                  ["12 USC 2607", "12 CFR 1024.14"],
    "RESPA_LOSS_MITIGATION":           ["12 CFR 1024.41"],
    "RESPA_FORCE_PLACED_INSURANCE":    ["12 USC 2605(l)", "12 CFR 1024.37"],
    "RESPA_ESCROW":                    ["12 USC 2609", "12 CFR 1024.17"],

    # ========================================================================
    # Fair Housing Act
    # ========================================================================
    "FAIR_HOUSING_DISCRIMINATION":     ["42 USC 3604", "42 USC 3605", "24 CFR 100"],
    "FHA":                             ["42 USC 3601", "42 USC 3604", "42 USC 3605", "42 USC 3613", "24 CFR 100"],

    # ========================================================================
    # GLBA â€” privacy
    # ========================================================================
    "GLBA":                            ["15 USC 6801", "15 USC 6802", "16 CFR 313"],
    "GLBA_PRIVACY":                    ["15 USC 6801", "16 CFR 313"],
    "GLBA_SAFEGUARDS":                 ["15 USC 6801", "16 CFR 314"],

    # ========================================================================
    # PA UTPCPL + state consumer protection (Lippi case â€” phila_ccp:260100000)
    # ========================================================================
    "PA_UTPCPL":                       ["73 PS 201-2", "73 PS 201-3", "73 PS 201-9.2"],
    "UTPCPL":                          ["73 PS 201-2", "73 PS 201-3", "73 PS 201-9.2"],
    "PA_FCEUA":                        ["73 PS 2270", "73 PS 2270.4"],

    # ========================================================================
    # IRS / 1099 / Cancellation of Debt
    # ========================================================================
    "1099_C_CANCELLATION":             ["26 USC 108", "26 USC 6050P"],
    "1099_A_ACQUISITION":              ["26 USC 6050J"],
    "INSOLVENCY_EXCLUSION":            ["26 USC 108(a)(1)(B)", "26 USC 108(d)"],
    "REMIC":                           ["26 USC 860D", "26 USC 860F", "26 USC 860G"],

    # ========================================================================
    # UCC â€” adopted as PA Title 13
    # ========================================================================
    "NEGOTIABLE_INSTRUMENT":           ["UCC 3-104", "UCC 3-501", "UCC 3-502", "UCC 4-104", "13 PA CS 3104", "12 CFR 229.2"],
    "UCC_TENDER":                      ["UCC 3-603", "UCC 3-501", "13 PA CS 3603"],
    "UCC_DISHONOR":                    ["UCC 3-502", "UCC 3-505", "13 PA CS 3502"],
    "UCC_HOLDER_IN_DUE_COURSE":        ["UCC 3-302", "13 PA CS 3302"],
    "UCC_PRESENTMENT":                 ["UCC 3-501", "UCC 3-502", "13 PA CS 3501"],
    "UCC_ACCEPTANCE":                  ["UCC 3-409", "UCC 3-410", "13 PA CS 3409"],
    "UCC_ACCORD_AND_SATISFACTION":     ["UCC 3-311", "13 PA CS 3311"],
    "UCC_BANK_DEPOSIT":                ["UCC 4-102", "UCC 4-104", "13 PA CS 4102"],
    "UCC_FUNDS_TRANSFER":              ["UCC 4A-104", "UCC 4A-208", "13 PA CS 4A104"],
    "UCC_LETTER_OF_CREDIT":            ["UCC 5-102", "UCC 5-108", "13 PA CS 5102"],
    "UCC_SECURED_TRANSACTIONS":        ["UCC 9-102", "UCC 9-203", "UCC 9-308", "13 PA CS 9102"],
    "UCC_PERFECTION":                  ["UCC 9-308", "UCC 9-310", "13 PA CS 9308"],
    "UCC_PRIORITY":                    ["UCC 9-322", "UCC 9-324", "13 PA CS 9322"],

    # ========================================================================
    # Banking / Federal Reserve
    # ========================================================================
    "BANK_HOLDING_COMPANY":            ["12 USC 1841", "12 USC 1842"],
    "GLASS_STEAGALL":                  ["12 USC 24", "12 USC 378"],
    "NATIONAL_BANK":                   ["12 USC 21", "12 USC 24", "12 USC 81"],
    "FEDERAL_RESERVE":                 ["12 USC 221", "12 USC 248"],
    "DEPOSITORY_INSTITUTIONS":         ["12 USC 1813", "12 USC 1818"],

    # ========================================================================
    # Insurance â€” bad faith (State Farm case â€” fed:2:26-cv-00789)
    # ========================================================================
    "INSURANCE_BAD_FAITH":             ["42 PS 8371"],
    "PA_INSURANCE_BAD_FAITH":          ["42 PS 8371"],
    "INSURANCE_DUTY_TO_DEFEND":        ["42 PS 8371"],
    "INSURANCE_UCSPA":                 ["31 Pa Code 146", "40 PS 1171.5"],
    "UNFAIR_INSURANCE_PRACTICES":      ["40 PS 1171.5", "31 Pa Code 146"],

    # ========================================================================
    # Title 18 criminal â€” judicial / opposing party
    # ========================================================================
    "DEPRIVATION_RIGHTS_COLOR_OF_LAW": ["18 USC 242", "18 USC 241", "42 USC 1983"],
    "MAIL_FRAUD":                      ["18 USC 1341"],
    "WIRE_FRAUD":                      ["18 USC 1343"],
    "RICO":                            ["18 USC 1961", "18 USC 1962", "18 USC 1964"],
    "RICO_PATTERN":                    ["18 USC 1961(5)", "18 USC 1962", "18 USC 1964"],
    "HONEST_SERVICES_FRAUD":           ["18 USC 1346"],
    "HOBBS_ACT":                       ["18 USC 1951"],
    "OBSTRUCTION_OF_JUSTICE":          ["18 USC 1503", "18 USC 1505", "18 USC 1512"],
    "FALSE_STATEMENTS":                ["18 USC 1001"],
    "CONSPIRACY_AGAINST_RIGHTS":       ["18 USC 241"],
    "FRAUD_ON_COURT_JUDICIAL":         ["18 USC 1001", "18 USC 1503", "Hazel-Atlas"],
    "BANK_FRAUD":                      ["18 USC 1344"],

    # ========================================================================
    # Civil rights
    # ========================================================================
    "SECTION_1983":                    ["42 USC 1983"],
    "SECTION_1985":                    ["42 USC 1985"],
    "SECTION_1986":                    ["42 USC 1986"],
    "BIVENS":                          ["Bivens v Six Unknown Agents", "42 USC 1983"],

    # ========================================================================
    # Judicial / procedural â€” from scanner_categories
    # ========================================================================
    "EX_PARTE_RULING":                 ["28 USC 455", "Canon 3", "Caperton v Massey"],
    "PRIOR_RESTRAINT_FILING_BAN":      ["First Amendment", "28 USC 1651", "Procup v Strickland"],
    "DO_NOT_FILE_LANGUAGE":            ["First Amendment", "28 USC 1651", "Christopher v Harbury"],
    "DENIED_OPPORTUNITY_TO_RESPOND":   ["Fifth Amendment", "Fourteenth Amendment", "Mathews v Eldridge"],
    "ACCESS_TO_COURTS_DENIAL":         ["First Amendment", "Christopher v Harbury", "Bounds v Smith"],
    "JUDICIAL_BIAS_LANGUAGE":          ["28 USC 455", "28 USC 144", "Liteky v US"],
    "SANCTIONS_NO_SAFE_HARBOR":        ["FRCP 11(c)(2)", "Star Mark v Bounty Jewelry"],
    "SUA_SPONTE_NO_LEAVE":             ["Pierce v Underwood", "Hill v United States"],
    "VEXATIOUS_DESIGNATION_NO_PROCESS":["Procup v Strickland", "28 USC 1651"],
    "NO_REASONS_STATED":               ["FRCP 52", "United States v Forness"],
    "PRO_SE_HEIGHTENED_STANDARD":      ["Haines v Kerner", "Erickson v Pardus", "Hughes v Rowe"],
    "IMPROPER_CONVERSION_12_TO_56":    ["FRCP 12(d)", "Burns v US"],
    "DISMISSED_WITH_PREJUDICE":        ["FRCP 41(b)", "Poulis v State Farm"],
    "REMAND_DENIED":                   ["28 USC 1447", "28 USC 1441", "28 USC 1446"],
    "TWOMBLY_IQBAL_MISAPPLIED":        ["Bell Atlantic v Twombly", "Ashcroft v Iqbal", "Erickson v Pardus"],
    "DEFAULT_MOTION_DENIED":           ["FRCP 55", "FRCP 60(b)"],
    "DENIED_WITHOUT_REASONS":          ["FRCP 52", "United States v Forness"],
    "CANON_VIOLATION":                 ["28 USC 455", "Canon 1", "Canon 2", "Canon 3"],
    "STARE_DECISIS_VIOLATION":         ["Planned Parenthood v Casey", "Payne v Tennessee"],
    "IMPROPER_REMOVAL_FACT":           ["28 USC 1441", "28 USC 1446", "28 USC 1447"],
    "AGENCY_FAILURE_FACT":             ["5 USC 706", "APA"],
    "IFP_DENIAL_CONSTRUCTIVE":         ["28 USC 1915", "Adkins v DuPont"],
    "WILLFUL_NONCOMPLIANCE_FACT":      ["15 USC 1681n", "15 USC 1640(a)"],

    # ========================================================================
    # Procedural attack vectors â€” Rule 12, Rule 56, Rule 60
    # ========================================================================
    "MOTION_TO_DISMISS":               ["FRCP 12(b)(6)", "Bell Atlantic v Twombly", "Ashcroft v Iqbal"],
    "MOTION_FOR_SUMMARY_JUDGMENT":     ["FRCP 56", "Celotex v Catrett", "Anderson v Liberty Lobby"],
    "MOTION_TO_VACATE":                ["FRCP 60(b)", "FRCP 60(b)(4)", "FRCP 60(b)(6)"],
    "VOID_JUDGMENT":                   ["FRCP 60(b)(4)", "United Student Aid v Espinosa"],
    "DUE_PROCESS_VIOLATION":           ["Fifth Amendment", "Fourteenth Amendment", "Mathews v Eldridge", "Goldberg v Kelly"],
    "EQUAL_PROTECTION":                ["Fourteenth Amendment", "42 USC 1983", "City of Cleburne v Cleburne Living Center"],

    # ========================================================================
    # APA â€” agency review
    # ========================================================================
    "APA":                             ["5 USC 706", "5 USC 553", "5 USC 555"],
    "AGENCY_OVERREACH":                ["5 USC 706", "Loper Bright v Raimondo"],
    "CHEVRON_DEFERENCE":               ["Loper Bright v Raimondo", "Chevron v NRDC"],
    "MAJOR_QUESTIONS":                 ["West Virginia v EPA", "Biden v Nebraska"],

    # ========================================================================
    # Foreclosure / mortgage (Amerisave overlap)
    # ========================================================================
    "FORECLOSURE_STANDING":            ["UCC 3-301", "UCC 3-309", "Carpenter v Longan"],
    "WET_INK_SIGNATURE":               ["UCC 3-104", "UCC 3-301"],
    "MERS_STANDING":                   ["UCC 3-301", "Bain v Metropolitan Mortgage"],
    "SECURITIZATION_FAILURE":          ["26 USC 860D", "PSA"],
    "QUIET_TITLE":                     ["28 USC 2409a"],

    # ========================================================================
    # Immunity defenses (when opposing claims qualified/absolute immunity)
    # ========================================================================
    "QUALIFIED_IMMUNITY":              ["Harlow v Fitzgerald", "Pearson v Callahan", "Pierson v Ray"],
    "JUDICIAL_IMMUNITY":               ["Stump v Sparkman", "Mireles v Waco"],
    "ABSOLUTE_IMMUNITY":               ["Imbler v Pachtman", "Stump v Sparkman"],
    "ELEVENTH_AMENDMENT":              ["Eleventh Amendment", "Hans v Louisiana", "Ex parte Young"],

    # ========================================================================
    # Burden / standard of review
    # ========================================================================
    "BURDEN_OF_PROOF":                 ["Schaffer v Weast", "Director v Greenwich Collieries"],
    "STANDING":                        ["Lujan v Defenders of Wildlife", "Spokeo v Robins", "TransUnion v Ramirez"],

    # ========================================================================
    # State-specific PA
    # ========================================================================
    "PA_RULES_CIVIL_PROCEDURE":        ["Pa RCP 1028", "Pa RCP 1035", "Pa RCP 237"],
    "PA_QUIET_TITLE":                  ["Pa RCP 1061"],
    "PA_DECLARATORY_JUDGMENT":         ["42 PS 7531", "42 PS 7532"],

    # ========================================================================
    # Constitutional
    # ========================================================================
    "FIRST_AMENDMENT":                 ["First Amendment", "NAACP v Button", "California Motor Transport"],
    "FOURTH_AMENDMENT":                ["Fourth Amendment", "Katz v US"],
    "FIFTH_AMENDMENT_DUE_PROCESS":     ["Fifth Amendment", "Mathews v Eldridge"],
    "FOURTEENTH_AMENDMENT_DUE_PROCESS":["Fourteenth Amendment", "Mathews v Eldridge"],
    "TAKINGS":                         ["Fifth Amendment", "Penn Central v New York"],

    # ========================================================================
    # Debt enforcement / real party in interest (v16)
    # ========================================================================
    "DEBT_ENFORCEMENT":               ["UCC 3-301", "UCC 3-302", "UCC 3-203", "UCC 3-309", "FRCP 17(a)"],
    "STANDING_TO_SUE_ON_DEBT":        ["UCC 3-301", "FRCP 17(a)", "Lujan v Defenders of Wildlife", "Spokeo v Robins"],
    "REAL_PARTY_IN_INTEREST":         ["FRCP 17(a)", "Pa RCP 2002", "UCC 3-301"],
    "PERSON_ENTITLED_TO_ENFORCE":     ["UCC 3-301", "UCC 3-309"],
    "HOLDER_IN_DUE_COURSE_DOCTRINE":  ["UCC 3-302", "UCC 3-305"],
    "CHAIN_OF_TITLE_DEBT":            ["UCC 3-203", "UCC 3-204", "UCC 3-301", "Carpenter v Longan"],
    "LOST_INSTRUMENT_ENFORCEMENT":    ["UCC 3-309", "UCC 3-301"],
    "MERS_FORECLOSURE_STANDING":      ["UCC 3-301", "Bain v Metropolitan Mortgage", "Carpenter v Longan"],
    "ASSIGNMENT_OF_DEBT":             ["UCC 3-203", "UCC 9-406"],
    "TRANSUNION_CONCRETE_INJURY":     ["TransUnion v Ramirez", "Spokeo v Robins"],
}




def _classify_by_citation(citation: str) -> str:
    """
    Classify a citation string into authority tier using REGEX patterns.
    Works for ANY title/jurisdiction, not just hardcoded examples.

    Hierarchy: CONSTITUTION > USC > BLACKS > BOUVIER > RULE > CFR > CASE_LAW > AGENCY_GUIDANCE > TREATY > OTHER

    Examples handled by regex (not hardcoded):
        "15 USC 1681i"               -> USC      (any \d+ USC \d+)
        "8 USC 1182(a)(9)(B)"        -> USC      (immigration)
        "26 USC 3401(c)"             -> USC      (tax)
        "29 USC 1132"                -> USC      (ERISA)
        "12 CFR 1022.43"             -> CFR
        "29 CFR 1630"                -> CFR      (ADA regs)
        "UCC 3-104"                  -> USC
        "13 PA CS 3104"              -> USC      (PA codes)
        "73 PS 201-2"                -> USC      (PA Purdon's Statutes)
        "FRCP 12(b)(6)"              -> RULE
        "Pa RCP 1028"                -> RULE
        "Bell Atlantic v Twombly"    -> CASE_LAW
        "Loper Bright v Raimondo"    -> CASE_LAW
        "First Amendment"            -> CONSTITUTION
        "Black's Law"                -> BLACKS
    """
    if not citation:
        return "OTHER"
    c_upper = citation.upper().replace(".", "")

    # Dictionaries (highest specificity first)
    if re.search(r"\bBLACK[\'\u2019]?S\b|\bLAW DICTIONARY\b", c_upper):
        return "BLACKS"
    if "BOUVIER" in c_upper:
        return "BOUVIER"

    # Constitutional
    if re.search(r"\bAMENDMENT\b|\bCONSTITUTION\b|\bU\s*S\s*CONST\b|\bART(ICLE)?\s+(I{1,3}|IV|V|VI|VII)\b", c_upper):
        return "CONSTITUTION"

    # Federal court rules
    if re.search(r"\bF\s*R\s*CP\b|\bF\s*R\s*AP\b|\bF\s*R\s*E\b|\bF\s*R\s*CRP\b|\bF\s*R\s*BP\b", c_upper):
        return "RULE"
    if re.search(r"\bPA\s+R\s*C\s*P\b|\bPA\s+RAP\b", c_upper):
        return "RULE"
    if re.search(r"\b[A-Z]{2,3}\s+RULE\s+\d+\b", c_upper):
        return "RULE"

    # Federal statutes
    if re.search(r"\b\d+\s*U\s*S\s*C\b", c_upper):
        return "USC"

    # UCC
    if re.search(r"\bUCC\s+\d", c_upper):
        return "USC"

    # PA statutes (codified)
    if re.search(r"\b\d+\s+PA\s+C\s*S\b|\b\d+\s+P\s*S\b", c_upper):
        return "USC"

    # Other state statutes
    if re.search(r"\b\d+\s+(NY|CAL|TEX|FLA|OH|MA|IL|VA|MD|NJ|MI|WA|GA|NC|AZ|OR|CO|MN|WI|MO)\b", c_upper):
        return "USC"
    if re.search(r"\b(REV(ISED)?\s+CODE|GEN(ERAL)?\s+LAWS?|STAT(UTES?)?|CIVIL\s+CODE|PENAL\s+CODE)\b", c_upper):
        return "USC"

    # Federal regulations
    if re.search(r"\b\d+\s+C\s*F\s*R\b", c_upper):
        return "CFR"

    # State regulations
    if re.search(r"\bPA\s+CODE\b|\bPA\s+ADMIN\b|\b\d+\s+PA\s+CODE\b", c_upper):
        return "CFR"

    # Treaties
    if re.search(r"\bTREATY\b|\bTIAS\b|\bUST\b|\bICCPR\b|\bUDHR\b|\bGENEVA\b|\bVIENNA\s+CONV\b", c_upper):
        return "TREATY"

    # Case law
    if re.search(r"\b(V\.?|VS\.?|VERSUS|EX\s+PARTE|EX\s+REL)\b", c_upper):
        return "CASE_LAW"
    if re.search(r"\b\d+\s+(U\s*S|S\s*CT|F\s*\d|F\s*SUPP|A\s*\d|P\s*\d|N\s*E\s*\d|N\s*W\s*\d|S\s*E\s*\d|S\s*W\s*\d)\s+\d+", c_upper):
        return "CASE_LAW"

    # Canons / agency guidance
    if re.search(r"\bCANON\b|\bABA\s+MODEL\b|\bRESTATEMENT\b", c_upper):
        return "AGENCY_GUIDANCE"
    if re.search(r"\bIRS\s+REV(ENUE)?\s+RUL(ING)?\b|\bIRS\s+NOTICE\b|\bSEC\s+RELEASE\b|\bCFPB\s+BULLETIN\b|\bAGENCY\s+GUIDANCE\b", c_upper):
        return "AGENCY_GUIDANCE"

    return "OTHER"


def _extract_citations_from_text(text: str) -> list:
    """
    Pull every legal citation out of free-form text using regex (raw strings).
    No hardcoded violation list â€” works for any domain.
    """
    if not text:
        return []
    found = []
    seen = set()

    def _add(s):
        s = s.strip()
        if s and s not in seen:
            seen.add(s)
            found.append(s)

    # Federal statutes â€” "15 USC 1681i", "15 U.S.C. Â§ 1681(a)(1)"
    for m in re.finditer(
        r"\b(\d+)\s+U\.?\s*S\.?\s*C\.?(?:\s*[\u00a7])?\s*(\d+[a-z\-]*(?:\([^)]+\))*)",
        text, re.IGNORECASE):
        _add(f"{m.group(1)} USC {m.group(2)}")

    # Federal regulations â€” "12 CFR 1022.43"
    for m in re.finditer(
        r"\b(\d+)\s+C\.?\s*F\.?\s*R\.?(?:\s*[\u00a7])?\s*(\d+(?:\.\d+)*(?:\([^)]+\))*)",
        text, re.IGNORECASE):
        _add(f"{m.group(1)} CFR {m.group(2)}")

    # PA statutes
    for m in re.finditer(
        r"\b(\d+)\s+P\.?\s*(?:S\.?|a\.?\s*C\.?\s*S\.?)(?:\s*[\u00a7])?\s*(\d+[\d\-\.]*)",
        text, re.IGNORECASE):
        _add(f"{m.group(1)} PS {m.group(2)}")

    # UCC
    for m in re.finditer(
        r"\bU\.?\s*C\.?\s*C\.?(?:\s*[\u00a7])?\s*(\d+[\-\.\d]*)",
        text, re.IGNORECASE):
        _add(f"UCC {m.group(1)}")

    # Federal rules
    for m in re.finditer(
        r"\b(?:Fed\.?\s*R\.?\s*(?:Civ|App|Evid|Bankr|Crim)\.?\s*P\.?|F\s*R\s*(?:CP|AP|E|BP|CRP))\s+(\d+(?:\([a-z]\d?\))*)",
        text, re.IGNORECASE):
        _add(m.group(0).strip())

    # State rules
    for m in re.finditer(
        r"\bPa\.?\s*R\.?\s*(?:C\.?\s*P|A\.?\s*P|E)\.?\s+(\d+(?:\([a-z]\))?)",
        text, re.IGNORECASE):
        _add(m.group(0).strip())

    # Constitution
    for m in re.finditer(
        r"\b(First|Second|Third|Fourth|Fifth|Sixth|Seventh|Eighth|Ninth|Tenth|Eleventh|Twelfth|Thirteenth|Fourteenth|Fifteenth)\s+Amendment\b",
        text, re.IGNORECASE):
        _add(f"{m.group(1)} Amendment")

    # Case names â€” "Bell Atlantic v. Twombly"
    for m in re.finditer(
        r"\b([A-Z][A-Za-z\.\&\-]+(?:\s+[A-Z][A-Za-z\.\&\-]+)*)\s+v\.?\s+([A-Z][A-Za-z\.\&\-]+(?:\s+[A-Z][A-Za-z\.\&\-]+)*)",
        text):
        _add(f"{m.group(1)} v {m.group(2)}")

    # Reporter citations
    for m in re.finditer(
        r"\b(\d+)\s+(U\.S\.|S\.\s*Ct\.|F\.\s*\d?d?|F\.\s*Supp\.?\s*\d?d?|A\.\s*\d?d?|P\.\s*\d?d?)\s+(\d+)",
        text):
        _add(f"{m.group(1)} {m.group(2).strip()} {m.group(3)}")

    return found


def _harvest_citations_from_db(db, query_text: str, n_results: int = 5) -> list:
    """
    Layer 3: Ask the DB itself what citations are relevant to a query.
    Harvests the `citations` metadata field from each returned chunk,
    plus extracts citations from the chunk text body as backup.
    """
    if not query_text or db is None:
        return []
    out = []
    seen = set()
    try:
        results = db.query_text(query_text, n_results=n_results) or []
        if not isinstance(results, list):
            return []
        for r in results:
            if not isinstance(r, dict):
                continue
            cites = r.get("citations") or []
            if isinstance(cites, str):
                try:
                    cites = json.loads(cites)
                except Exception:
                    cites = [cites]
            if not isinstance(cites, list):
                continue
            for c in cites:
                if not c:
                    continue
                c = str(c).strip()
                if c and c not in seen:
                    seen.add(c)
                    out.append(c)
            text_field = r.get("text") or ""
            for c in _extract_citations_from_text(text_field[:2000]):
                if c not in seen:
                    seen.add(c)
                    out.append(c)
    except Exception:
        pass
    return out



def _harvest_citations_from_db(db, query_text: str, n_results: int = 5) -> list:
    """
    Layer 3: Ask the DB itself what citations are relevant to a query.
    Queries DB with raw issue text, harvests the `citations` metadata field
    from each returned chunk. This is what makes novel domains work.

    Returns deduped list of citations the DB associates with the query.
    """
    if not query_text or db is None:
        return []
    out = []
    seen = set()
    try:
        results = db.query_text(query_text, n_results=n_results) or []
        if not isinstance(results, list):
            return []
        for r in results:
            if not isinstance(r, dict):
                continue
            # query_text returns a `citations` field per the diag output
            cites = r.get("citations") or []
            if isinstance(cites, str):
                # Sometimes it's a JSON-encoded list as a string
                try:
                    cites = json.loads(cites)
                except Exception:
                    cites = [cites]
            if not isinstance(cites, list):
                continue
            for c in cites:
                if not c:
                    continue
                c = str(c).strip()
                if c and c not in seen:
                    seen.add(c)
                    out.append(c)
            # Also extract from text body as a backup (catches inline citations
            # the metadata field missed)
            text_field = r.get("text") or ""
            for c in _extract_citations_from_text(text_field[:2000]):
                if c not in seen:
                    seen.add(c)
                    out.append(c)
    except Exception:
        pass
    return out

def _expand_citations(issue: str, statute: str = "", fact_text: str = "", db=None) -> list:
    """
    4-layer cascade: hardcoded -> regex from facts -> DB discovery -> statute pass.

    Args:
        issue:      violation type / category (e.g., "FCRA_FAILURE_TO_INVESTIGATE")
        statute:    explicit statute_cited from Agent 1 (e.g., "15 USC 1681i")
        fact_text:  raw text to scan for citation patterns (email body, etc.)
        db:         LegalDB instance for Layer 3 discovery (optional)
    """
    out = []
    seen = set()

    def _add(c):
        if c and c not in seen:
            seen.add(c)
            out.append(c)

    # Layer 4 (always first): explicit statute from Agent 1
    if statute:
        _add(statute)

    # Layer 1: hardcoded map (fast path for common violations)
    if issue and "CITATION_EXPANSION" in globals():
        if issue in CITATION_EXPANSION:
            for c in CITATION_EXPANSION[issue]:
                _add(c)
        else:
            issue_upper = issue.upper()
            for key, cites in CITATION_EXPANSION.items():
                if key in issue_upper or issue_upper in key:
                    for c in cites:
                        _add(c)
                    break

    # Layer 2: regex extraction from fact text + statute field
    combined_text = f"{statute} {fact_text}".strip()
    if combined_text:
        for c in _extract_citations_from_text(combined_text):
            _add(c)

    # Layer 3: DB-driven discovery (only when Layers 1+2 produce sparse results)
    if db is not None and len(out) < 3:
        query = " ".join(str(p) for p in (issue, statute, fact_text[:200] if fact_text else "") if p).strip()
        if query:
            for c in _harvest_citations_from_db(db, query, n_results=5):
                _add(c)

    return out


# ---------------------------------------------------------------------------
# Chain state
# ---------------------------------------------------------------------------
@dataclass
class AgentResult:
    agent: int
    name: str
    ok: bool
    elapsed_ms: int
    output: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


@dataclass
class ChainState:
    chain_id: str
    case_id: Optional[str]
    started: str
    input_payload: Dict[str, Any]
    results: List[AgentResult] = field(default_factory=list)
    finished: Optional[str] = None
    ok: bool = False

    def agent_json(self, n: int) -> Dict[str, Any]:
        for r in self.results:
            if r.agent == n:
                return r.output or {}
        return {}

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # dataclass asdict already handles nested
        return d


# ---------------------------------------------------------------------------
# Chain runner
# ---------------------------------------------------------------------------


# ============================================================================
# AGENT 9 SUPPORT: context detection, learned-definitions cache, writeback
# ============================================================================

LEARNED_DEFS_DIR = ROOT / "obsidian_sync" / "definitions_learned"
LEARNED_DEFS_DIR.mkdir(parents=True, exist_ok=True)


def _detect_context(facts: dict, statute: str = "") -> dict:
    """
    Strict context detection. NEVER guesses. Returns:
      {context, title, chapter, source, confidence}

    Priority order:
      1. Explicit Title in facts text ("Title 26", "Title 15")
      2. Title derived from cited statute ("15 USC 1681i" -> Title 15)
      3. case_id mapped to known case domain
      4. UNKNOWN (returns context="UNKNOWN", confidence="LOW")
    """
    fact_blob = " ".join(str(v) for v in (facts or {}).values()
                         if v and not isinstance(v, (dict, list)))[:3000]

    # Priority 1: explicit Title mention
    m = re.search(r"\bTitle\s+(\d+)\b", fact_blob, re.IGNORECASE)
    if m:
        title_num = m.group(1)
        return {
            "context": f"title_{title_num}",
            "title": title_num,
            "source": "explicit_title_mention",
            "confidence": "HIGH",
        }

    # Priority 2: derive from cited statute
    if statute:
        m = re.search(r"\b(\d+)\s*U\.?\s*S\.?\s*C\.?", statute, re.IGNORECASE)
        if m:
            title_num = m.group(1)
            return {
                "context": f"title_{title_num}",
                "title": title_num,
                "source": "from_statute_cited",
                "confidence": "HIGH",
            }
        m = re.search(r"\b(\d+)\s+C\.?\s*F\.?\s*R\.?", statute, re.IGNORECASE)
        if m:
            title_num = m.group(1)
            return {
                "context": f"cfr_title_{title_num}",
                "title": title_num,
                "source": "from_cfr_cited",
                "confidence": "MEDIUM",
            }

    # Priority 3: case_id domain mapping
    case_id = facts.get("case_number") or facts.get("case_id") or ""
    case_domains = {
        "fed:2:26-cv-00123":  ("title_15", "FCRA Equifax case"),
        "fed:2:26-cv-00456":  ("title_12", "RESPA Amerisave case"),
        "phila_ccp:260100000":("title_42_pa_state", "PA state Lippi case"),
        "fed:2:26-cv-00789":  ("title_42_pa_state", "PA insurance State Farm"),
    }
    if case_id in case_domains:
        ctx, desc = case_domains[case_id]
        return {
            "context": ctx,
            "title": ctx.split("_")[1] if "_" in ctx else "",
            "source": f"case_id:{desc}",
            "confidence": "MEDIUM",
        }

    return {
        "context": "UNKNOWN",
        "title": "",
        "source": "no_signal",
        "confidence": "LOW",
    }


def _learned_def_path(term: str, context: str) -> Path:
    """Sanitize term+context for filesystem-safe filename."""
    safe_term = re.sub(r"[^a-zA-Z0-9_-]", "_", (term or "unknown"))[:80]
    safe_ctx  = re.sub(r"[^a-zA-Z0-9_-]", "_", (context or "unknown"))[:40]
    return LEARNED_DEFS_DIR / f"{safe_term}__{safe_ctx}.md"


def _read_learned_def(term: str, context: str, max_age_days: int = 7,
                       fallback_terms: list = None) -> dict:
    """
    Read a learned definition. v16: tries multiple cache keys before giving up.

    Order:
      1. (term, context) â€” primary key
      2. each fallback_term, context  â€” used when retry broadened the query
      3. (term_uppercased, context) â€” case variations
    Returns dict (with _path / _age_days / _key_used) or None if all miss.
    """
    candidates = []
    if term:
        candidates.append(term)
    if fallback_terms:
        for t in fallback_terms:
            if t and t not in candidates:
                candidates.append(t)
    if term:
        upper = term.upper()
        if upper not in candidates:
            candidates.append(upper)

    from datetime import datetime, timedelta

    for candidate in candidates:
        p = _learned_def_path(candidate, context)
        if not p.exists():
            continue
        try:
            mtime = datetime.fromtimestamp(p.stat().st_mtime)
            if datetime.now() - mtime > timedelta(days=max_age_days):
                continue
            text = p.read_text(encoding="utf-8")
            if text.startswith("---"):
                end = text.find("---", 3)
                if end > 0:
                    yaml_block = text[3:end]
                    body = text[end+3:].strip()
                    meta = {}
                    # v18d3: parse YAML scalars AND list-type fields
                    current_list_key = None
                    current_list = []
                    for line in yaml_block.splitlines():
                        # List item: "  - value" (continuation of previous key)
                        m_list = re.match(r"^(\s+)-\s+(.+)$", line)
                        if m_list and current_list_key:
                            current_list.append(m_list.group(2).strip())
                            continue
                        # New top-level key (no leading whitespace before name)
                        if ":" in line and not line.startswith("  "):
                            # Close out any previous list
                            if current_list_key:
                                meta[current_list_key] = current_list
                                current_list_key = None
                                current_list = []
                            k, _, v = line.partition(":")
                            k = k.strip()
                            v = v.strip()
                            if v == "":
                                # This key is followed by a list â€” start collecting
                                current_list_key = k
                                current_list = []
                            else:
                                meta[k] = v
                    # Final flush if file ends with a list
                    if current_list_key:
                        meta[current_list_key] = current_list
                    meta["_body"] = body
                    meta["_path"] = str(p)
                    meta["_age_days"] = (datetime.now() - mtime).days
                    meta["_key_used"] = candidate
                    return meta
        except Exception:
            continue
    return None

    try:
        from datetime import datetime, timedelta
        mtime = datetime.fromtimestamp(p.stat().st_mtime)
        if datetime.now() - mtime > timedelta(days=max_age_days):
            return None  # stale, force re-query
        text = p.read_text(encoding="utf-8")
        # Parse frontmatter
        if text.startswith("---"):
            end = text.find("---", 3)
            if end > 0:
                yaml_block = text[3:end]
                body = text[end+3:].strip()
                meta = {}
                for line in yaml_block.splitlines():
                    if ":" in line:
                        k, _, v = line.partition(":")
                        meta[k.strip()] = v.strip()
                meta["_body"] = body
                meta["_path"] = str(p)
                meta["_age_days"] = (datetime.now() - mtime).days
                return meta
    except Exception:
        pass
    return None


def _write_learned_def(
    term: str,
    context: str,
    primary: dict,
    supporting: list,
    conflicts: list,
    attack_vectors: list,
    chain_id: str,
) -> str:
    """
    Write a learned-definition node to obsidian_sync/definitions_learned/.
    If file already exists, increments times_referenced and merges new findings.
    Returns path written to.
    """
    from datetime import datetime
    p = _learned_def_path(term, context)
    times_ref = 1
    if p.exists():
        try:
            existing = p.read_text(encoding="utf-8")
            m = re.search(r"^times_referenced:\s*(\d+)", existing, re.MULTILINE)
            if m:
                times_ref = int(m.group(1)) + 1
        except Exception:
            pass

    primary_cite = (primary or {}).get("citation", "")
    primary_tier = (primary or {}).get("tier", "")
    primary_text = (primary or {}).get("text", "")[:500]

    # YAML frontmatter
    lines = [
        "---",
        f"term: {term}",
        f"context: {context}",
        f"primary_citation: {primary_cite}",
        f"primary_tier: {primary_tier}",
        f"primary_distance: {(primary or {}).get('distance', 'null')}",
        f"chain_origin: {chain_id}",
        f"last_updated: {datetime.now().isoformat()}",
        f"times_referenced: {times_ref}",
        f"confidence: {(primary or {}).get('confidence', 'MEDIUM')}",
        f"conflict_count: {len(conflicts)}",
        f"attack_vector_count: {len(attack_vectors)}",
        "supporting_citations:",
    ]
    for s in supporting[:10]:
        lines.append(f"  - {s.get('citation', '')} [{s.get('tier', '')}]")
    lines.append("---")
    lines.append("")
    lines.append(f"# {term} (context: {context})")
    lines.append("")

    if primary:
        lines.append(f"## Primary Definition â€” {primary_cite} ({primary_tier})")
        lines.append("")
        lines.append("```")
        lines.append(primary_text)
        lines.append("```")
        lines.append("")

    if supporting:
        lines.append("## Supporting Definitions")
        lines.append("")
        for s in supporting[:10]:
            lines.append(f"### {s.get('citation', '')} ({s.get('tier', '')})")
            lines.append("")
            lines.append("```")
            lines.append((s.get("text") or "")[:500])
            lines.append("```")
            lines.append("")

    if conflicts:
        lines.append("## Conflicts Detected")
        lines.append("")
        for c in conflicts:
            lines.append(f"- **{c.get('type', 'conflict')}**: {c.get('summary', '')}")
            if c.get("usc_text") and c.get("cfr_text"):
                lines.append(f"  - USC: {c['usc_text'][:200]}")
                lines.append(f"  - CFR: {c['cfr_text'][:200]}")
        lines.append("")

    if attack_vectors:
        lines.append("## Attack Vectors")
        lines.append("")
        for av in attack_vectors:
            lines.append(f"- {av}")
        lines.append("")

    lines.append("## Provenance")
    lines.append("")
    lines.append(f"- Chain: `{chain_id}`")
    lines.append(f"- Times referenced: {times_ref}")

    p.write_text("\n".join(lines), encoding="utf-8")

    # Update _index.md
    _update_learned_index(term, context, primary_cite, p)

    return str(p)


def _update_learned_index(term: str, context: str, primary_cite: str, file_path: Path):
    """Maintain a flat index for Hermes to browse all learned definitions."""
    idx = LEARNED_DEFS_DIR / "_index.md"
    line = f"- [[{file_path.stem}]] â€” {term} ({context}) -> {primary_cite}"
    if idx.exists():
        try:
            existing = idx.read_text(encoding="utf-8")
            # Don't double-add
            if file_path.stem not in existing:
                existing = existing + "\n" + line
                idx.write_text(existing, encoding="utf-8")
            else:
                # Update timestamp
                from datetime import datetime
                stamp = f"\n<!-- updated: {datetime.now().isoformat()} -->\n"
                idx.write_text(existing + stamp, encoding="utf-8")
        except Exception:
            pass
    else:
        from datetime import datetime
        idx.write_text(
            f"# Learned Definitions Index\n\nGenerated by Beezle chain_runner.\n\n"
            f"Last updated: {datetime.now().isoformat()}\n\n"
            f"{line}\n",
            encoding="utf-8",
        )


class ChainRunner:
    """Runs the 10-agent chain sequentially, one agent per step."""

    def __init__(self, vault: bool = True, log_each: bool = True):
        self.vault = vault
        self.log_each = log_each

        # Infra (optional â€” chain degrades if either is down)
        self.db  = self._load_db()
        self.llm = self._load_llm()

        # Pre-bind agent handlers
        self._handlers = [
            (1,  "extractor",          self._agent_1_extractor),
            (2,  "definition_resolver",self._agent_2_definition),
            (3,  "logic_chain",        self._agent_3_logic),
            (4,  "remedy_finder",      self._agent_4_remedy),
            (5,  "contradiction",      self._agent_5_contradiction),
            (6,  "document_generator", self._agent_6_document),
            (7,  "learning_writer",    self._agent_7_learning),
            (8,  "coalescence",        self._agent_8_coalescence),
            (9,  "master_definition",  self._agent_9_master_def),
            (10, "title_18_criminal",  self._agent_10_title18),
        ]

    # ---- infra ----------------------------------------------------------
    def _load_db(self):
        m = _safe_import("pipeline.db_connector", ["LegalDB"])
        cls = m.get("LegalDB")
        if cls is None:
            return None
        try:
            return cls()
        except Exception as e:
            log.warning("LegalDB init failed: %s", e)
            return None

    def _load_llm(self):
        m = _safe_import("pipeline.llm_layer", ["LlamaLLM"])
        cls = m.get("LlamaLLM")
        if cls is None:
            return None
        try:
            return cls()
        except Exception as e:
            log.warning("LlamaLLM init failed: %s", e)
            return None

    # ---- public ---------------------------------------------------------
    def run(
        self,
        input_payload: Dict[str, Any],
        case_id: Optional[str] = None,
        skip: Optional[List[int]] = None,
        only: Optional[List[int]] = None,
    ) -> ChainState:
        """Run the full chain. `skip` / `only` filter which agents execute."""
        skip = set(skip or [])
        only = set(only or [])

        state = ChainState(
            chain_id  = f"chain_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}",
            case_id   = case_id,
            started   = datetime.now().isoformat(),
            input_payload=input_payload,
        )
        log.info("=== chain %s start (case=%s) ===", state.chain_id, case_id)

        for num, name, handler in self._handlers:
            if only and num not in only:
                continue
            if num in skip:
                continue

            t0 = time.time()
            try:
                out = handler(state) or {}
                res = AgentResult(
                    agent=num, name=name, ok=True,
                    elapsed_ms=int((time.time() - t0) * 1000),
                    output=out,
                )
            except Exception as e:
                res = AgentResult(
                    agent=num, name=name, ok=False,
                    elapsed_ms=int((time.time() - t0) * 1000),
                    error=f"{type(e).__name__}: {e}",
                    output={"traceback": traceback.format_exc(limit=3)},
                )
                log.error("agent %d (%s) failed: %s", num, name, e)

            state.results.append(res)
            if self.log_each:
                log.info("  A%d %-22s %s (%dms)",
                         num, name, "OK" if res.ok else "ERR", res.elapsed_ms)

        state.finished = datetime.now().isoformat()
        state.ok = all(r.ok for r in state.results if r.agent in {1, 2, 3, 4, 8})

        self._persist(state)
        log.info("=== chain %s done (ok=%s) ===", state.chain_id, state.ok)
        return state

    # ---- persistence ----------------------------------------------------
    def _persist(self, state: ChainState) -> None:
        # 1) JSON log
        p = LOG_DIR / f"{state.chain_id}.json"
        p.write_text(json.dumps(state.to_dict(), indent=2, default=str), encoding="utf-8")

        # 2) Obsidian vault node (markdown)
        if self.vault:
            md = self._render_vault_md(state)
            (VAULT_DIR / f"{state.chain_id}.md").write_text(md, encoding="utf-8")

    def _render_vault_md(self, s: ChainState) -> str:
        lines = [
            f"# Chain {s.chain_id}",
            f"- case: {s.case_id}",
            f"- started: {s.started}",
            f"- finished: {s.finished}",
            f"- ok: {s.ok}",
            "",
            "## Agent Results",
        ]
        for r in s.results:
            lines.append(f"### A{r.agent}. {r.name} â€” {'OK' if r.ok else 'ERR'} ({r.elapsed_ms}ms)")
            if r.error:
                lines.append(f"> error: `{r.error}`")
            lines.append("```json")
            lines.append(json.dumps(r.output, indent=2, default=str)[:4000])
            lines.append("```")
        return "\n".join(lines)

    # =====================================================================
    # AGENT HANDLERS
    # =====================================================================

    # ---- Agent 1: Extractor -------------------------------------------------
    def _agent_1_extractor(self, state: ChainState) -> Dict[str, Any]:
        payload = state.input_payload

        # v17: Map email_scan field names to chain-expected schema.
        # Email scan items use: from, subject, body_preview, violations[],
        # agency, message_id, case_references[], date, deadlines[]
        # Chain expects: sender, subject, text, violation_type, statute_cited,
        # entity, agency, case_number, category
        if isinstance(payload, dict):
            # Sender from "from" field
            if not payload.get("sender") and payload.get("from"):
                payload["sender"] = payload["from"]
            # Body text from "body_preview" or "body"
            if not payload.get("text"):
                if payload.get("body_preview"):
                    payload["text"] = payload["body_preview"]
                elif payload.get("body"):
                    payload["text"] = payload["body"]
                elif payload.get("snippet"):
                    payload["text"] = payload["snippet"]
            # First violation as violation_type + category
            v_list = payload.get("violations") or []
            if v_list and not payload.get("violation_type"):
                first_v = v_list[0]
                if isinstance(first_v, dict):
                    payload["violation_type"] = (first_v.get("type") or
                                                  first_v.get("name") or
                                                  first_v.get("violation") or "")
                    if not payload.get("category"):
                        payload["category"] = first_v.get("category", "")
                    if not payload.get("statute_cited"):
                        payload["statute_cited"] = (first_v.get("statute") or
                                                     first_v.get("citation") or "")
                elif isinstance(first_v, str):
                    payload["violation_type"] = first_v
            # Case number from matched_case or case_references
            if not payload.get("case_number"):
                if payload.get("matched_case"):
                    mc = payload["matched_case"]
                    if isinstance(mc, dict):
                        payload["case_number"] = mc.get("case_id") or mc.get("number") or ""
                    elif isinstance(mc, str):
                        payload["case_number"] = mc
                elif payload.get("case_references"):
                    refs = payload["case_references"]
                    if refs and isinstance(refs, list):
                        first_ref = refs[0]
                        if isinstance(first_ref, dict):
                            payload["case_number"] = first_ref.get("case_id", "") or first_ref.get("number", "")
                        elif isinstance(first_ref, str):
                            payload["case_number"] = first_ref
            # Entity from agency or sender domain
            if not payload.get("entity"):
                if payload.get("agency"):
                    payload["entity"] = payload["agency"]
                elif payload.get("from"):
                    # extract domain from email
                    m = re.search(r"@([^\s>]+)", payload["from"])
                    if m:
                        payload["entity"] = m.group(1)

        text = payload.get("text") or payload.get("body") or payload.get("subject") or ""

        facts: Dict[str, Any] = {}

        # Pre-populate from inbound structured fields
        for k in ("sender", "subject", "date", "case_number", "statute_cited",
                  "deadline", "amount", "agency", "violation_type",
                  "entity", "category"):
            if k in payload:
                facts[k] = payload[k]

        # Try intake_engine / intake_pipeline / trigger_engine
        ie = _safe_import("pipeline.intake_engine",
                          ["extract_facts", "identify_gaps", "build_new_case"])
        if ie.get("extract_facts") and text:
            try:
                extra = ie["extract_facts"](text) or {}
                if isinstance(extra, dict):
                    facts.update({k: v for k, v in extra.items() if v})
            except Exception as e:
                log.debug("intake_engine.extract_facts: %s", e)

        ip = _safe_import("pipeline.intake_pipeline", ["extract_facts_from_document"])
        if ip.get("extract_facts_from_document") and text and not facts.get("violation_type"):
            try:
                extra = ip["extract_facts_from_document"](text) or {}
                if isinstance(extra, dict):
                    for k, v in extra.items():
                        facts.setdefault(k, v)
            except Exception as e:
                log.debug("intake_pipeline: %s", e)

        # Triggers
        te = _safe_import("pipeline.trigger_engine", ["extract_triggers", "check_violation"])
        triggers: List[Any] = []
        if te.get("extract_triggers") and text:
            try:
                triggers = te["extract_triggers"](text) or []
            except Exception as e:
                log.debug("trigger_engine: %s", e)

        gaps: List[str] = []
        if ie.get("identify_gaps"):
            try:
                gaps = ie["identify_gaps"](facts) or []
            except Exception:
                pass

        return {
            "facts": facts,
            "triggers": triggers,
            "gaps": gaps,
            "has_violation": bool(facts.get("violation_type") and facts["violation_type"] != "unknown"),
        }

    # ---- Agent 2: Definition Resolver ----------------------------------
    def _agent_2_definition(self, state: ChainState) -> Dict[str, Any]:
        # _RETRY_LOOP_v13c â€” broadens search up to 3 iterations
        attempt = 0
        last_result = None
        max_attempts = 3
        original_facts = (state.agent_json(1) or {}).get("facts", {}) or {}

        while attempt < max_attempts:
            attempt += 1
            try:
                result = self._agent_2_definition_inner(state)
            except Exception as e:
                import traceback
                log.warning("Agent 2 attempt %d failed: %s", attempt, e)
                result = {
                    "definitions": [], "scope": {}, "statute": "", "issue": "",
                    "registry_hits": 0, "discovery_hits": 0, "db_hits": 0,
                    "expanded_citations": [],
                    "_error": f"{type(e).__name__}: {e}",
                    "_traceback": traceback.format_exc(limit=5),
                    "_attempt": attempt,
                }

            last_result = result
            db_hits = result.get("db_hits", 0) or 0
            disc_hits = result.get("discovery_hits", 0) or 0
            if db_hits + disc_hits > 0:
                result["_attempts_used"] = attempt
                return result

            # Broaden for next attempt: strip qualifiers, fall back to entity name,
            # or use raw subject text
            if attempt < max_attempts:
                a1 = state.agent_json(1) or {}
                facts = a1.get("facts", {}) or {}
                if attempt == 1:
                    # Try without violation_type qualifier â€” use just the statute
                    facts["violation_type"] = ""
                elif attempt == 2:
                    # Last resort: feed in the email subject as the issue
                    payload = state.input_payload or {}
                    facts["violation_type"] = (payload.get("subject") or
                                                payload.get("category") or
                                                "general legal question")[:120]

        if last_result is not None:
            last_result["_attempts_used"] = attempt
            return last_result
        return {"definitions": [], "_attempts_used": attempt, "_all_failed": True}

    def _agent_2_definition_inner(self, state: ChainState) -> Dict[str, Any]:
        """
        Three-tier strategy:
          1. definition_engine static registry (fast, deterministic, often empty)
          2. authority_discovery.discover_authority (DB + CourtListener)
          3. Direct db.query_text on each expanded citation
        """
        a1 = state.agent_json(1)
        facts = a1.get("facts", {})
        statute = facts.get("statute_cited") or facts.get("statute") or ""
        issue   = facts.get("violation_type") or facts.get("category") or ""

        # Tier 1: static registry
        de = _safe_import("pipeline.definition_engine",
                          ["get_definitions_for_issue", "build_definition_set"])
        sc = _safe_import("pipeline.statutory_construction",
                          ["classify_definition_scope", "check_means_vs_includes"])

        defs: List[Any] = []
        if de.get("get_definitions_for_issue") and issue:
            try:
                defs = de["get_definitions_for_issue"](issue) or []
            except Exception as e:
                log.debug("definition_engine.get_definitions_for_issue: %s", e)

        if not defs and de.get("build_definition_set") and statute:
            try:
                defs = de["build_definition_set"](statute) or []
            except Exception as e:
                log.debug("definition_engine.build_definition_set: %s", e)

        # Tier 2: authority_discovery (DB + CourtListener) â€” when registry empty
        ad_result = None
        ad_hits: List[Dict[str, Any]] = []
        if not defs:
            ad = _safe_import("pipeline.authority_discovery",
                              ["discover_authority"])
            if ad.get("discover_authority"):
                # Build a richer query from issue + statute + entity
                query_parts = [issue or "", statute or ""]
                entity = facts.get("entity") or facts.get("agency") or ""
                if entity:
                    query_parts.append(entity)
                query = " ".join(p for p in query_parts if p).strip() or "violation"
                subject_areas = self._infer_subject_areas(issue, statute)
                try:
                    ad_result = ad["discover_authority"](
                        query=query,
                        subject_areas=subject_areas,
                        jurisdiction="PA",
                        include_case_law=False,  # case law slow + needs API key
                        max_db_results=8,
                    ) or {}
                    # discover_authority returns {db_results, case_law, citations, ...}
                    if isinstance(ad_result, dict):
                        ad_hits = (ad_result.get("db_results") or
                                   ad_result.get("primary_sources") or [])
                except Exception as e:
                    log.debug("authority_discovery.discover_authority: %s", e)

        # Tier 3: direct DB queries on expanded citations â€” when discover_authority empty/thin
        # v12: limit to 5 citations + 2 results each to control latency (was 8x2 = 16+ queries)
        # v12: relaxed distance threshold from 0.6 to 0.7 (FCRA matches ~0.43-0.58)
        db_hits: List[Dict[str, Any]] = []
        if not defs and not ad_hits:
            # v13: pass fact text + db for dynamic citation discovery
            fact_text_blob = " ".join(str(v) for v in (facts or {}).values() if v and not isinstance(v, (dict, list)))[:2000]
            citations_to_query = _expand_citations(issue, statute, fact_text=fact_text_blob, db=self.db)
            if citations_to_query and self.db is not None:
                for citation in citations_to_query[:5]:
                    try:
                        results = self.db.query_text(citation, n_results=2) or []
                        if isinstance(results, list):
                            for r in results:
                                if isinstance(r, dict) and r.get("distance", 1.0) < 0.7:
                                    db_hits.append({
                                        "citation": citation,
                                        "text": (r.get("text") or "")[:1500],
                                        "source": r.get("source", ""),
                                        "section": r.get("section", ""),
                                        "distance": r.get("distance"),
                                        "doc_id": r.get("doc_id", ""),
                                    })
                    except Exception as e:
                        log.debug("db.query_text(%s): %s", citation, e)

        # Build unified definitions list
        unified_defs: List[Dict[str, Any]] = []
        for d in defs or []:
            if hasattr(d, "to_dict"):
                unified_defs.append({**d.to_dict(), "tier": "registry"})
            elif isinstance(d, dict):
                unified_defs.append({**d, "tier": "registry"})
            else:
                unified_defs.append({"definition": str(d), "tier": "registry"})

        for h in ad_hits[:6]:
            if isinstance(h, dict):
                unified_defs.append({
                    "term": issue or statute or "authority",
                    "definition": (h.get("text") or h.get("snippet") or "")[:1500],
                    "authority": h.get("source") or h.get("citation") or "",
                    "source": h.get("source", ""),
                    "tier": "authority_discovery",
                })

        for h in db_hits[:8]:
            unified_defs.append({
                "term": h["citation"],
                "definition": h["text"],
                "authority": h["citation"],
                "source": h["source"],
                "distance": h["distance"],
                "tier": "db_direct",
            })

        scope_info = {}
        if sc.get("classify_definition_scope") and unified_defs:
            try:
                scope_info = sc["classify_definition_scope"](unified_defs[0]) or {}
            except Exception:
                pass

        return {
            "definitions": unified_defs,
            "scope": scope_info,
            "statute": statute,
            "issue": issue,
            "registry_hits":  len([d for d in unified_defs if d.get("tier") == "registry"]),
            "discovery_hits": len([d for d in unified_defs if d.get("tier") == "authority_discovery"]),
            "db_hits":        len([d for d in unified_defs if d.get("tier") == "db_direct"]),
            "expanded_citations": _expand_citations(issue, statute),
        }

    @staticmethod
    def _infer_subject_areas(issue: str, statute: str) -> List[str]:
        """Map issue/statute to authority_discovery subject areas."""
        text = (issue + " " + statute).upper()
        areas = []
        if any(k in text for k in ("FCRA", "1681", "CREDIT", "TRADELINE")):
            areas.append("fcra")
        if any(k in text for k in ("FDCPA", "1692", "DEBT")):
            areas.append("fdcpa")
        if any(k in text for k in ("ECOA", "1691", "DISCRIMINATION")):
            areas.append("ecoa")
        if any(k in text for k in ("RESPA", "QWR", "MORTGAGE", "2605")):
            areas.append("respa")
        if any(k in text for k in ("TILA", "1601", "TRUTH IN LENDING")):
            areas.append("tila")
        if any(k in text for k in ("FAIR HOUSING", "3604", "HUD")):
            areas.append("fha")
        if any(k in text for k in ("UCC", "NEGOTIABLE", "TENDER", "INSTRUMENT")):
            areas.append("ucc")
        if any(k in text for k in ("1099", "IRS", "108", "TAX")):
            areas.append("irs")
        if any(k in text for k in ("RICO", "1961", "FRAUD")):
            areas.append("rico")
        return areas or ["general"]

    # ---- Agent 3: Logic Chain ------------------------------------------
    def _agent_3_logic(self, state: ChainState) -> Dict[str, Any]:
        a1 = state.agent_json(1)
        a2 = state.agent_json(2)
        facts = a1.get("facts", {})

        le = _safe_import("pipeline.logic_engine", ["match_rules", "identify_regulators"])
        rl = _safe_import("pipeline.reasoning_layer",
                          ["hierarchy_audit", "contradiction_audit", "element_coverage_audit"])
        ro = _safe_import("pipeline.reasoning_orchestrator",
                          ["decide", "detect_posture", "build_strategy"])

        matched_rules: List[Any] = []
        if le.get("match_rules"):
            try:
                matched_rules = le["match_rules"](facts) or []
            except Exception as e:
                log.debug("logic_engine.match_rules: %s", e)

        posture = None
        if ro.get("detect_posture"):
            try:
                posture = ro["detect_posture"](facts)
            except Exception:
                pass

        hierarchy_issues = []
        if rl.get("hierarchy_audit"):
            try:
                hierarchy_issues = rl["hierarchy_audit"](a2.get("definitions", [])) or []
            except Exception:
                pass

        actions: List[Any] = []
        if ro.get("decide"):
            try:
                actions = ro["decide"](facts) or []
            except Exception as e:
                log.debug("reasoning_orchestrator.decide: %s", e)

        return {
            "matched_rules": matched_rules,
            "posture": posture,
            "hierarchy_issues": hierarchy_issues,
            "actions": actions,
            "conclusion": self._derive_conclusion(facts, matched_rules),
            "confidence": "HIGH" if matched_rules else "LOW",
        }

    @staticmethod
    def _derive_conclusion(facts: Dict[str, Any], rules: List[Any]) -> str:
        v = facts.get("violation_type") or facts.get("category") or "violation"
        e = facts.get("entity") or facts.get("agency") or "respondent"
        if rules:
            return f"{e} committed {v} â€” {len(rules)} rule(s) matched"
        return f"{e} implicated in {v} â€” no matching rule in DB"

    # ---- Agent 4: Remedy Finder ----------------------------------------
    def _agent_4_remedy(self, state: ChainState) -> Dict[str, Any]:
        a1 = state.agent_json(1)
        a3 = state.agent_json(3)
        facts = a1.get("facts", {})
        violation = facts.get("violation_type") or facts.get("category") or ""

        re_ = _safe_import("pipeline.remedy_engine",
                           ["extract_remedies", "format_remedy_block"])
        du  = _safe_import("pipeline.duty_engine",
                           ["load_duty_map", "list_available_entity_types"])

        remedies: List[Any] = []
        if re_.get("extract_remedies"):
            try:
                raw = re_["extract_remedies"](violation)
                # Handle RemedyAnalysis object, dict, or list
                if raw is None:
                    remedies = []
                elif isinstance(raw, list):
                    remedies = raw
                elif isinstance(raw, dict):
                    remedies = raw.get("remedies") or raw.get("steps") or [raw]
                elif hasattr(raw, "remedies"):
                    remedies = getattr(raw, "remedies", []) or []
                elif hasattr(raw, "steps"):
                    remedies = getattr(raw, "steps", []) or []
                elif hasattr(raw, "__dict__"):
                    d = {k: v for k, v in raw.__dict__.items() if not k.startswith("_")}
                    remedies = d.get("remedies") or d.get("steps") or [d]
                else:
                    remedies = [raw]
            except Exception as e:
                log.debug("remedy_engine.extract_remedies: %s", e)

        duties: List[Any] = []
        if du.get("load_duty_map"):
            try:
                dmap = du["load_duty_map"]() or {}
                ent = facts.get("entity") or facts.get("agency") or ""
                duties = dmap.get(ent, []) if isinstance(dmap, dict) else []
            except Exception:
                pass

        steps = self._remedies_to_steps(remedies, facts)
        return {
            "remedies": remedies,
            "duties": duties,
            "steps": steps,
            "rule_actions": a3.get("actions", []),
        }

    @staticmethod
    def _remedies_to_steps(remedies: List[Any], facts: Dict[str, Any]) -> List[Dict[str, Any]]:
        out = []
        for i, r in enumerate(remedies or [], start=1):
            if isinstance(r, dict):
                out.append({
                    "priority": i,
                    "step":     r.get("step")    or r.get("action")    or f"Remedy {i}",
                    "form":     r.get("form")    or r.get("document"),
                    "deadline": r.get("deadline")or r.get("timeframe"),
                    "authority":r.get("authority") or r.get("statute") or facts.get("statute_cited"),
                })
            else:
                out.append({"priority": i, "step": str(r)})
        return out

    # ---- Agent 5: Contradiction ----------------------------------------
    def _agent_5_contradiction(self, state: ChainState) -> Dict[str, Any]:
        a3 = state.agent_json(3)

        ce = _safe_import("pipeline.conflict_engine",
                          ["extract_opposing_arguments", "build_conflict_map"])
        cs = _safe_import("pipeline.consistency_scorer",
                          ["score_consistency", "score_batch"])
        av = _safe_import("pipeline.authority_validity",
                          ["classify_authority", "check_preclusion"])

        opposing: List[Any] = []
        conc = a3.get("conclusion", "")
        if ce.get("extract_opposing_arguments") and conc:
            try:
                opposing = ce["extract_opposing_arguments"](conc) or []
            except Exception as e:
                log.debug("conflict_engine: %s", e)

        score = None
        if cs.get("score_consistency"):
            try:
                score = cs["score_consistency"](conc)
            except Exception:
                pass

        return {
            "opposing_arguments": opposing,
            "consistency_score": score,
            "support": [],
            "confidence": "HIGH" if not opposing else "MEDIUM",
        }

    # ---- Agent 6: Document Generator -----------------------------------
    def _agent_6_document(self, state: ChainState) -> Dict[str, Any]:
        a1, a3, a4, a5 = (state.agent_json(n) for n in (1, 3, 4, 5))
        facts = a1.get("facts", {})

        dp = _safe_import("pipeline.draft_pipeline", ["pipeline_draft", "pipeline_draft_v3"])
        ca = _safe_import("pipeline.case_assembler", ["CaseAssembler"])
        di = _safe_import("pipeline.document_intelligence", ["classify_document"])

        draft_text = None
        draft_fn = dp.get("pipeline_draft_v3") or dp.get("pipeline_draft")
        if draft_fn:
            try:
                draft_text = draft_fn({
                    "facts": facts,
                    "conclusion": a3.get("conclusion"),
                    "steps": a4.get("steps", []),
                    "confidence": a5.get("confidence"),
                })
            except Exception as e:
                log.debug("draft_pipeline: %s", e)

        doc_type = None
        if di.get("classify_document") and draft_text:
            try:
                doc_type = di["classify_document"](draft_text)
            except Exception:
                pass

        return {
            "draft_text": draft_text or self._fallback_draft(facts, a3, a4),
            "document_type": doc_type or self._infer_doc_type(facts),
            "citations": [s.get("authority") for s in a4.get("steps", []) if s.get("authority")],
        }

    @staticmethod
    def _fallback_draft(facts: Dict[str, Any], a3: Dict[str, Any], a4: Dict[str, Any]) -> str:
        lines = [
            "DRAFT â€” auto-generated by Beezle chain_runner",
            "",
            f"RE: {facts.get('violation_type', 'Violation')}",
            f"CASE: {facts.get('case_number', '(none)')}",
            f"ENTITY: {facts.get('entity') or facts.get('agency') or '(unknown)'}",
            "",
            "FACTS:",
            json.dumps(facts, indent=2, default=str)[:2000],
            "",
            f"CONCLUSION: {a3.get('conclusion','(pending)')}",
            "",
            "ACTION STEPS:",
        ]
        for s in a4.get("steps", []):
            lines.append(f"  {s.get('priority')}. {s.get('step')}"
                         + (f" [deadline: {s.get('deadline')}]" if s.get('deadline') else ""))
        return "\n".join(lines)

    @staticmethod
    def _infer_doc_type(facts: Dict[str, Any]) -> str:
        v = (facts.get("violation_type") or facts.get("category") or "").upper()
        if "REMAND" in v or "REMOVAL" in v:               return "motion_to_remand"
        if "DISMISS" in v:                                 return "motion_opposing_dismissal"
        if "EX_PARTE" in v or "PRIOR_RESTRAINT" in v:      return "motion_to_vacate"
        if "SANCTION" in v:                                return "motion_opposing_sanctions"
        if "JUDICIAL" in v or "CANON" in v:                return "notice_of_deficiency"
        if "IFP" in v:                                     return "ifp_appeal"
        if v.startswith("RICO"):                           return "rico_notice"
        return "demand_letter"

    # ---- Agent 7: Learning Writer --------------------------------------
    def _agent_7_learning(self, state: ChainState) -> Dict[str, Any]:
        sc = _safe_import("pipeline.scorecard", ["score_case"])

        score = None
        if sc.get("score_case") and state.case_id:
            try:
                score = sc["score_case"](state.case_id)
            except Exception as e:
                log.debug("scorecard.score_case: %s", e)

        return {
            "vault_node": f"obsidian_sync/chains/{state.chain_id}.md",
            "chain_id":   state.chain_id,
            "case_id":    state.case_id,
            "score":      score,
            "indexed":    True,
        }

    # ---- Agent 8: Coalescence ------------------------------------------
    def _agent_8_coalescence(self, state: ChainState) -> Dict[str, Any]:
        a1, a3, a4, a5, a6 = (state.agent_json(n) for n in (1, 3, 4, 5, 6))
        facts = a1.get("facts", {})

        # Defensive: coerce any non-string fact values that downstream code
        # may try to use in re.search / f-string / json string concat.
        if isinstance(facts, dict):
            for k, v in list(facts.items()):
                if isinstance(v, dict):
                    # Compact dict to string representation
                    try:
                        import json as _j
                        facts[k] = _j.dumps(v, default=str)[:500]
                    except Exception:
                        facts[k] = str(v)[:500]
                elif isinstance(v, list):
                    facts[k] = [str(x) if not isinstance(x, (str, int, float, bool, type(None))) else x for x in v]

        # Try scenario_engine for theory matching
        se = _safe_import("pipeline.scenario_engine",
                          ["load_theory_map", "match_triggers", "match_element"])
        theories: List[Any] = []
        if se.get("load_theory_map") and se.get("match_triggers"):
            try:
                tm = se["load_theory_map"]()
                raw = se["match_triggers"](facts, tm) or []
                # Normalize: every entry must be str or simple dict
                for t in raw:
                    if isinstance(t, (str, int, float)):
                        theories.append(str(t))
                    elif isinstance(t, dict):
                        theories.append(t)
                    elif hasattr(t, "__dict__"):
                        theories.append({k: v for k, v in t.__dict__.items()
                                         if not k.startswith("_") and
                                         isinstance(v, (str, int, float, bool, list, dict, type(None)))})
                    else:
                        theories.append(str(t))
            except Exception as e:
                log.debug("scenario_engine: %s", e)

        # _v17_a8_wrap â€” packet-building errors no longer kill Agent 8
        try:
            # Build packet
            packet_id = f"PKT-{datetime.now().strftime('%Y%m%d_%H%M%S')}-{state.chain_id[-6:]}"
            packet_folder = PACKET_DIR / packet_id
            packet_folder.mkdir(parents=True, exist_ok=True)

            action_items = []
            for s in a4.get("steps", []):
                action_items.append({
                    "priority": s.get("priority"),
                    "action":   s.get("step"),
                    "deadline": s.get("deadline"),
                    "authority":s.get("authority"),
                    "document": s.get("form"),
                })

            # Write preamble + draft
            preamble = self._build_preamble(facts, a3, a4, a5, theories)
            (packet_folder / "00_PREAMBLE.md").write_text(preamble, encoding="utf-8")

            if a6.get("draft_text"):
                _v18g_dt = a6.get("draft_text", "")
            if isinstance(_v18g_dt, dict):
                _v18g_text = json.dumps(_v18g_dt, indent=2, default=str)
            elif isinstance(_v18g_dt, (list, tuple)):
                _v18g_text = json.dumps(_v18g_dt, default=str)
            elif _v18g_dt is None:
                _v18g_text = ""
            else:
                _v18g_text = str(_v18g_dt)
            (packet_folder / f"01_{a6.get('document_type','draft')}.txt").write_text(
                _v18g_text, encoding="utf-8"
            )

            summary = {
                "packet_id":    packet_id,
                "case_id":      state.case_id,
                "summary":      preamble.split("\n\n", 1)[0][:400],
                "conclusion":   a3.get("conclusion"),
                "confidence":   a5.get("confidence"),
                "action_items": action_items,
                "theories":     theories,
                "documents_generated": [
                    {"type": "preamble", "file": str(packet_folder / "00_PREAMBLE.md")},
                    {"type": a6.get("document_type", "draft"),
                     "file": str(packet_folder / f"01_{a6.get('document_type','draft')}.txt")},
                ],
                "output_folder": str(packet_folder),
                "print_ready":   True,
                "next_query":    "Print packet and execute steps in order.",
            }

            def _safe(o):
                if isinstance(o, (str, int, float, bool, type(None))): return o
                if isinstance(o, dict): return {k: _safe(v) for k, v in o.items()}
                if isinstance(o, (list, tuple)): return [_safe(x) for x in o]
                if hasattr(o, "__dict__"):
                    return {k: _safe(v) for k, v in o.__dict__.items() if not k.startswith("_")}
                return str(o)
            safe_summary = _safe(summary)
            (packet_folder / "packet.json").write_text(
                json.dumps(safe_summary, indent=2, default=str), encoding="utf-8"
            )
            return safe_summary
        except Exception as _v17_e:
            import traceback as _v17_tb
            log.warning('Agent 8 packet building failed: %s', _v17_e)
            return {
                'packet_id': None,
                'packet_folder': None,
                'theories': theories if 'theories' in dir() else [],
                'action_items': action_items if 'action_items' in dir() else [],
                '_error': f'{type(_v17_e).__name__}: {_v17_e}',
                '_traceback': _v17_tb.format_exc(limit=5),
                '_partial': True,
            }


    @staticmethod
    def _build_preamble(facts, a3, a4, a5, theories) -> str:
        lines = [
            f"# ACTION PACKET",
            "",
            f"**Violation:** {facts.get('violation_type', facts.get('category', 'unknown'))}",
            f"**Entity:** {facts.get('entity') or facts.get('agency', '(unknown)')}",
            f"**Case:** {facts.get('case_number', '(none)')}",
            f"**Statute:** {facts.get('statute_cited', '(tbd)')}",
            "",
            f"**Conclusion:** {a3.get('conclusion','(pending)')}",
            f"**Confidence:** {a5.get('confidence','(pending)')}",
            "",
            "## Theories",
        ]
        for t in (theories or [])[:10]:
            if isinstance(t, dict):
                label = t.get("name") or t.get("theory") or t.get("id") or str(t)[:100]
                lines.append(f"- {label}")
            else:
                lines.append(f"- {t}")
        lines += ["", "## Actions (in order)"]
        for s in a4.get("steps", []):
            lines.append(f"{s.get('priority')}. **{s.get('step')}**"
                         + (f" â€” deadline: {s.get('deadline')}" if s.get('deadline') else "")
                         + (f" â€” authority: {s.get('authority')}" if s.get('authority') else ""))
        return "\n".join(lines)

    # ---- Agent 9: Master Definition ------------------------------------
    def _agent_9_master_def(self, state: ChainState) -> Dict[str, Any]:
        # v18e2 Speedup: fast-fail when no legal signal to investigate
        _v18e2_a1 = state.agent_json(1) if hasattr(state, "agent_json") else None
        _v18e2_facts = (_v18e2_a1 or {}).get("facts", {}) if isinstance(_v18e2_a1, dict) else {}
        _v18e2_vt = (_v18e2_facts.get("violation_type") or "").strip()
        _v18e2_st = (_v18e2_facts.get("statute_cited") or _v18e2_facts.get("statute") or "").strip()
        _v18e2_ct = (_v18e2_facts.get("category") or "").strip()
        if not _v18e2_vt and not _v18e2_st and not _v18e2_ct:
            return {
                "term": "",
                "context": "unknown",
                "from_cache": False,
                "primary": None,
                "primary_citation": None,
                "expanded_citations": [],
                "tier_counts": {},
                "supporting": [],
                "supporting_count": 0,
                "attack_vectors": [],
                "elapsed_note": "v18e2_fast_fail_no_signals",
                "_v18e2_fast_fail": True,
                "_attempts_used": 0,
            }

        # _RETRY_LOOP_v13c â€” broadens search up to 3 iterations
        attempt = 0
        last_result = None
        max_attempts = 3

        while attempt < max_attempts:
            attempt += 1
            try:
                result = self._agent_9_master_def_inner(state)
            except Exception as e:
                import traceback
                log.warning("Agent 9 attempt %d failed: %s", attempt, e)
                result = {
                    "term": "", "context": "ERROR", "expanded_citations": [],
                    "tier_counts": {}, "all_definitions": [],
                    "primary": None, "supporting": [],
                    "conflicts": [], "attack_vectors": [],
                    "_error": f"{type(e).__name__}: {e}",
                    "_traceback": traceback.format_exc(limit=5),
                    "_attempt": attempt,
                }

            last_result = result
            tier_counts = result.get("tier_counts") or {}
            total_hits = sum(tier_counts.values())
            if total_hits > 0:
                result["_attempts_used"] = attempt
                return result

            # Broaden for retry: replace context-specific terms with broader ones
            if attempt < max_attempts:
                a1 = state.agent_json(1) or {}
                facts = a1.get("facts", {}) or {}
                if attempt == 1:
                    # Try with just the statute, no violation type
                    facts["violation_type"] = ""
                elif attempt == 2:
                    # Use raw email subject as the term
                    payload = state.input_payload or {}
                    facts["term"] = (payload.get("subject") or
                                      payload.get("category") or "")[:120]

        if last_result is not None:
            last_result["_attempts_used"] = attempt
            return last_result
        return {"primary": None, "_attempts_used": attempt, "_all_failed": True}

    def _agent_9_master_def_inner(self, state: ChainState) -> Dict[str, Any]:
        """
        Master Definition Agent â€” FULL ATTACK MAP.

        Per architecture spec: every legal argument starts with a definition.
        If you control the definition, you control the outcome.

        This agent:
          1. Detects context strictly (Title 26 vs Title 15 vs UNKNOWN)
          2. Checks Obsidian definitions_learned cache first (speed win)
          3. Queries DB across ALL tiers (USC, Black\'s, Bouvier, CFR, case law)
          4. Picks PRIMARY based on context match
          5. Keeps SUPPORTING definitions (Black\'s reinforcing USC)
          6. Detects CONFLICTS (USC vs CFR scope divergence -> Loper Bright)
          7. Generates ATTACK VECTORS for Agent 6 to weaponize
          8. Writes findings back to obsidian_sync/definitions_learned/
        """
        a1 = state.agent_json(1)
        facts = a1.get("facts", {})

        # v14: cache key prefers violation_type > category > term > subject
        # so cache groups per-violation rather than per-email-subject
        term = (facts.get("violation_type") or facts.get("category")
                or facts.get("term") or facts.get("subject") or "")
        # Keep the original "human term" separately for the writeback frontmatter
        display_term = (facts.get("term") or facts.get("subject")
                         or facts.get("violation_type") or facts.get("category") or "")
        statute = facts.get("statute_cited") or ""

        # 1. DETECT CONTEXT (strict â€” never guess)
        context_info = _detect_context(facts, statute)
        context = context_info["context"]

        # 2. CHECK CACHE â€” if learned definition exists and fresh, return it
        # v16: also check under category, statute, raw violation_type so cache
        # hits even when retry loop broadened the query
        fallback_terms = []
        for v in (facts.get("violation_type"), facts.get("category"),
                   facts.get("term"), facts.get("subject"), statute):
            if v and v != term:
                fallback_terms.append(v)
        cached = _read_learned_def(term, context, max_age_days=7,
                                    fallback_terms=fallback_terms)
        if cached:
            # v18d2: reconstruct rich fields from cached supporting_citations
            sc_raw = cached.get("supporting_citations") or []
            if isinstance(sc_raw, str):
                sc_raw = [s.strip() for s in sc_raw.split("\n") if s.strip()]

            _expanded = []
            _seen = set()
            _tier_counts = {}
            _supporting = []
            for entry in sc_raw:
                # Format from cache: "15 USC 1681i [USC]" or "12 CFR 1022 [CFR]"
                _mm = re.match(r"^(.*?)\s*\[([^\]]+)\]\s*$", str(entry))
                if _mm:
                    _cite = _mm.group(1).strip()
                    _tier = _mm.group(2).strip()
                else:
                    _cite = str(entry).strip()
                    _tier = ""
                if _cite and _cite not in _seen:
                    _seen.add(_cite)
                    _expanded.append(_cite)
                if _tier:
                    _tier_counts[_tier] = _tier_counts.get(_tier, 0) + 1
                _supporting.append({"citation": _cite, "tier": _tier})

            # Build primary as dict (downstream consumers expect dict, not just string)
            _primary_cite = cached.get("primary_citation") or ""
            _primary_tier = cached.get("primary_tier") or ""
            _primary_dist = cached.get("primary_distance") or ""
            try:
                _primary_dist_f = float(_primary_dist) if _primary_dist else 0.0
            except Exception:
                _primary_dist_f = 0.0
            _primary_dict = None
            if _primary_cite:
                _primary_dict = {
                    "citation": _primary_cite,
                    "tier": _primary_tier,
                    "distance": _primary_dist_f,
                }

            # Attack vectors: cache stores count, synthesize a minimal vector
            _avc_raw = cached.get("attack_vector_count") or 0
            try:
                _avc = int(_avc_raw)
            except Exception:
                _avc = 0
            _attack_vectors = []
            if _avc > 0 and _primary_dict:
                _attack_vectors.append({
                    "primary_citation": _primary_cite,
                    "primary_tier": _primary_tier,
                    "supporting_count": len(_supporting),
                    "tier_counts": _tier_counts,
                    "_synthesized_from_cache": True,
                })

            return {
                "term": term,
                "context": context,
                "context_info": context_info,
                "from_cache": True,
                "cache_path": cached.get("_path"),
                "cache_age_days": cached.get("_age_days"),
                "primary": _primary_dict,
                "primary_citation": _primary_cite,
                "primary_tier": _primary_tier,
                "times_referenced": cached.get("times_referenced"),
                "expanded_citations": _expanded,
                "tier_counts": _tier_counts,
                "winner_tier": _primary_tier,
                "tiered_definitions": {},
                "all_definitions": [],
                "supporting": _supporting,
                "supporting_count": len(_supporting),
                "conflicts": [],
                "attack_vectors": _attack_vectors,
                "confidence": cached.get("confidence", "MEDIUM"),
                "elapsed_note": "served_from_cache_v18d2_enriched",
                "_v18d2_enriched": True,
            }

        # 3. EXPAND CITATIONS using full 4-layer cascade
        fact_text_blob = " ".join(str(v) for v in (facts or {}).values()
                                   if v and not isinstance(v, (dict, list)))[:2000]
        issue_for_expansion = (facts.get("violation_type") or facts.get("category")
                                or term or "")
        expanded = _expand_citations(issue_for_expansion, statute,
                                      fact_text=fact_text_blob, db=self.db)

        # 4. QUERY DB FOR EACH CITATION â€” keep ALL definitions, all tiers
        tiered_defs = {
            "USC": [], "BLACKS": [], "BOUVIER": [], "CFR": [],
            "RULE": [], "CASE_LAW": [], "CONSTITUTION": [], "AGENCY_GUIDANCE": [],
            "TREATY": [], "OTHER": [],
        }
        all_definitions = []  # flat list for Agent 3/6 consumption

        if expanded and self.db is not None:
            for citation in expanded[:2]:  # v18e2: cap from 8 -> 2 for speedup
                try:
                    results = self.db.query_text(citation, n_results=3) or []
                    if not isinstance(results, list):
                        continue
                    for r in results:
                        if not isinstance(r, dict):
                            continue
                        if r.get("distance", 1.0) >= 0.7:
                            continue
                        text = (r.get("text") or "")[:1500]
                        entry = {
                            "citation": citation,
                            "source":   r.get("source"),
                            "text":     text,
                            "distance": r.get("distance"),
                            "section":  r.get("section"),
                        }
                        # Classify by citation (clean)
                        tier = _classify_by_citation(citation)
                        # Override for dictionary sources
                        source_lower = (r.get("source") or "").lower()
                        if "blacks" in source_lower or "law_dictionary" in source_lower:
                            tier = "BLACKS"
                        elif "bouvier" in source_lower:
                            tier = "BOUVIER"
                        entry["tier"] = tier
                        tiered_defs[tier].append(entry)
                        all_definitions.append(entry)
                except Exception as e:
                    log.debug("db.query_text(%s): %s", citation, e)

        # 5. PICK PRIMARY based on context match
        # Strict rule: if context UNKNOWN, no primary. Return all with equal weight.
        primary = None
        primary_reason = ""
        if context_info["confidence"] == "LOW":
            primary_reason = "context_unknown_no_primary"
        else:
            # Look for definition matching the context Title
            target_title = context_info.get("title", "")
            if target_title:
                # Prefer USC entries matching the cited Title
                for entry in tiered_defs["USC"]:
                    cite = (entry.get("citation") or "").upper()
                    if f"{target_title} USC" in cite.replace(".", "").replace(" ", " "):
                        primary = entry
                        primary_reason = f"matches_title_{target_title}"
                        break
                # Then USC matching cited statute
                if not primary and statute:
                    for entry in tiered_defs["USC"]:
                        if statute.upper() in (entry.get("citation") or "").upper():
                            primary = entry
                            primary_reason = "matches_cited_statute"
                            break
            # Fallback: best USC by distance
            if not primary and tiered_defs["USC"]:
                primary = min(tiered_defs["USC"],
                              key=lambda e: e.get("distance", 1.0))
                primary_reason = "best_usc_by_distance"
            # Last resort: any tier with content
            if not primary:
                for tier in ("CONSTITUTION", "BLACKS", "BOUVIER", "RULE",
                             "CFR", "CASE_LAW"):
                    if tiered_defs.get(tier):
                        primary = tiered_defs[tier][0]
                        primary_reason = f"fallback_to_{tier.lower()}"
                        break

        # 6. SUPPORTING â€” non-primary entries grouped by tier
        supporting = []
        for tier in ("USC", "BLACKS", "BOUVIER", "CFR", "CASE_LAW",
                     "CONSTITUTION", "RULE", "AGENCY_GUIDANCE"):
            for entry in tiered_defs.get(tier, []):
                if entry is primary:
                    continue
                supporting.append({**entry, "tier": tier})

        # 7. CONFLICTS â€” USC vs CFR scope divergence (Loper Bright)
        conflicts = []
        attack_vectors = []
        if tiered_defs["USC"] and tiered_defs["CFR"]:
            usc_text = tiered_defs["USC"][0].get("text", "")
            cfr_text = tiered_defs["CFR"][0].get("text", "")
            # Naive heuristic: if CFR text >> USC text, regulatory expansion
            if cfr_text and len(cfr_text) > len(usc_text) * 1.5:
                conflicts.append({
                    "type": "regulatory_expansion",
                    "summary": "CFR definition substantially broader than USC",
                    "usc_citation": tiered_defs["USC"][0].get("citation"),
                    "cfr_citation": tiered_defs["CFR"][0].get("citation"),
                    "usc_text": usc_text[:500],
                    "cfr_text": cfr_text[:500],
                })
                attack_vectors.append(
                    f"CFR overreach: {tiered_defs['CFR'][0].get('citation')} "
                    f"expands beyond {tiered_defs['USC'][0].get('citation')} â€” "
                    f"Loper Bright v. Raimondo (2024) bars Chevron deference"
                )

        # Always add expressio unius if we have a USC definition
        if primary and primary.get("tier") == "USC":
            attack_vectors.append(
                f"Expressio unius est exclusio alterius: {primary.get('citation')} "
                f"enumerates scope; anything not enumerated is excluded"
            )

        # CFR-only definition with no USC support is suspect
        if not tiered_defs["USC"] and tiered_defs["CFR"]:
            attack_vectors.append(
                "Regulatory definition lacking statutory anchor â€” challenge under "
                "5 USC 706 (APA) and Loper Bright (no Chevron deference)"
            )

        # 8. WRITE-BACK to definitions_learned/
        # v16: skip writeback if a recent cache file under any candidate key
        # already has the same primary citation â€” prevents duplicate cache files
        cache_path = None
        skip_write = False
        if primary:
            existing = _read_learned_def(term, context, max_age_days=7,
                                          fallback_terms=fallback_terms)
            if existing and existing.get("primary_citation") == primary.get("citation"):
                cache_path = existing.get("_path")
                skip_write = True
                log.debug("Agent 9: skipping writeback, existing cache %s has same primary",
                          cache_path)

        if primary and term and not skip_write:
            try:
                cache_path = _write_learned_def(
                    term=term,
                    context=context,
                    primary=primary,
                    supporting=supporting,
                    conflicts=conflicts,
                    attack_vectors=attack_vectors,
                    chain_id=state.chain_id,
                )
            except Exception as e:
                log.debug("write_learned_def failed: %s", e)

        return {
            "term":              term,
            "context":           context,
            "context_info":      context_info,
            "statute":           statute,
            "from_cache":        False,
            "expanded_citations": expanded,
            "tier_counts":       {k: len(v) for k, v in tiered_defs.items() if v},
            "tiered_definitions": tiered_defs,
            "all_definitions":   all_definitions,
            "primary":           primary,
            "primary_reason":    primary_reason,
            "supporting":        supporting,
            "supporting_count":  len(supporting),
            "conflicts":         conflicts,
            "attack_vectors":    attack_vectors,
            "winner_tier":       (primary or {}).get("tier"),
            "winner":            primary,
            "cfr_overreach_flag":any(c.get("type") == "regulatory_expansion" for c in conflicts),
            "loper_bright_attack_vector": any("Loper Bright" in av for av in attack_vectors),
            "cache_path":        cache_path,
        }


    def _agent_10_title18(self, state: ChainState) -> Dict[str, Any]:
        a1 = state.agent_json(1)
        facts = a1.get("facts", {})
        text = state.input_payload.get("text") or state.input_payload.get("body") or ""

        ie = _safe_import("pipeline.immunity_engine",
                          ["analyze_immunity", "check_waiver"])
        jos = _safe_import("pipeline.judicial_order_scanner",
                           ["scan_order", "JudicialViolation"])

        immunity = None
        if ie.get("analyze_immunity"):
            try:
                immunity = ie["analyze_immunity"](facts)
            except Exception as e:
                log.debug("immunity_engine: %s", e)

        order_violations: List[Any] = []
        if jos.get("scan_order") and text:
            try:
                raw = jos["scan_order"](text)
                if raw is None:
                    order_violations = []
                elif isinstance(raw, list):
                    order_violations = raw
                elif hasattr(raw, "violations"):
                    # Some scanners return a result object with .violations
                    order_violations = list(getattr(raw, "violations", []) or [])
                elif hasattr(raw, "__iter__"):
                    # Generator / iterator â€” materialize to list
                    order_violations = list(raw)
                else:
                    order_violations = [raw]
            except Exception as e:
                log.debug("judicial_order_scanner: %s", e)

        # Dumb heuristic for Title 18 attach-points
        t18_hits = []
        v = (facts.get("violation_type") or facts.get("category") or "").upper()
        if any(k in v for k in ("EX_PARTE", "PRIOR_RESTRAINT", "DENIED_OPPORTUNITY",
                                 "DEPRIVATION_RIGHTS_COLOR_OF_LAW", "ACCESS_TO_COURTS")):
            t18_hits.append("18 USC 242 â€” deprivation under color of law")
        if "FRAUD" in v or "RICO" in v:
            t18_hits.append("18 USC 1341/1343 â€” mail/wire fraud")
        if "CONSPIRACY" in v or "RICO" in v:
            t18_hits.append("18 USC 241 â€” conspiracy against rights")

        return {
            "immunity": immunity,
            "order_violations": [
                (vo.__dict__ if hasattr(vo, "__dict__") else str(vo))
                for vo in (list(order_violations) if order_violations else [])[:50]
            ],
            "title_18_candidates": t18_hits,
            "criminal_referral_recommended": bool(t18_hits),
        }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


# ============================================================================
# v18b: Vault input/output helpers
# ============================================================================

VAULT_ROOT = ROOT / "obsidian_sync"
VAULT_CASES = VAULT_ROOT / "cases"

CASE_SLUG_MAP = {
    "fed:2:26-cv-00123":   "fed_26-cv-00123",
    "fed:2:26-cv-00456":   "fed_2-26-cv-00456_filer_v_amerisave",
    "phila_ccp:260100000": "phila_ccp_260100000_filer_v_lippi",
    "fed:2:26-cv-00789":   "fed_2-26-cv-00789_filer_v_state_farm",
}


def _parse_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    """Parse YAML-ish frontmatter from a markdown file. Returns (meta, body).

    We avoid pyyaml dependency â€” use a simple parser that handles:
      - scalar key: value
      - lists with "  - item" or "  - {json}"
      - nested dicts (one level deep) with "  key: value"
      - quoted strings
      - null / true / false / numeric literals
    """
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        end = text.find("---", 3)
    if end == -1:
        return {}, text

    block = text[3:end].strip()
    body = text[end + 3:].lstrip("\n").lstrip("-").lstrip("\n")
    meta: Dict[str, Any] = {}

    current_key = None
    current_kind = None  # "list" or "dict"
    current_collection: Any = None

    for raw_line in block.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        if line.startswith("  - ") or line.startswith("  -\t"):
            # list item
            item_str = line[4:].strip()
            value = _parse_yaml_scalar(item_str)
            if current_kind == "list" and current_collection is not None:
                current_collection.append(value)
            continue
        if line.startswith("  ") and ":" in line and current_kind == "dict":
            sub_k, _, sub_v = line[2:].partition(":")
            current_collection[sub_k.strip()] = _parse_yaml_scalar(sub_v.strip())
            continue
        if ":" in line and not line.startswith(" "):
            # New key
            k, _, v = line.partition(":")
            k = k.strip()
            v = v.strip()
            if v == "" or v == "null":
                # Could be a list/dict header â€” peek next line to decide
                meta[k] = None
                current_key = k
                current_kind = None
                current_collection = None
            elif v == "[]":
                meta[k] = []
                current_key = None
                current_kind = None
            elif v == "{}":
                meta[k] = {}
                current_key = None
                current_kind = None
            elif v.startswith("[") or v.startswith("{"):
                # JSON literal
                try:
                    meta[k] = json.loads(v)
                except Exception:
                    meta[k] = v
                current_key = None
                current_kind = None
            else:
                meta[k] = _parse_yaml_scalar(v)
                current_key = None
                current_kind = None
            continue
        # Continuation lines: detect list vs dict by indentation+marker
        if current_key is not None and (line.startswith("  - ") or line.startswith("\t-")):
            if current_kind is None:
                current_collection = []
                meta[current_key] = current_collection
                current_kind = "list"
            current_collection.append(_parse_yaml_scalar(line[4:].strip()))
        elif current_key is not None and line.startswith("  "):
            if current_kind is None:
                current_collection = {}
                meta[current_key] = current_collection
                current_kind = "dict"
            sub_k, _, sub_v = line[2:].partition(":")
            current_collection[sub_k.strip()] = _parse_yaml_scalar(sub_v.strip())

    return meta, body


def _parse_yaml_scalar(s: str) -> Any:
    """Parse a single YAML scalar into Python value."""
    s = s.strip()
    if not s:
        return ""
    if s == "null" or s == "~":
        return None
    if s == "true":
        return True
    if s == "false":
        return False
    # JSON literal
    if s.startswith("{") or s.startswith("["):
        try:
            return json.loads(s)
        except Exception:
            return s
    # Quoted string
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1].replace('\\"', '"')
    # Numeric
    try:
        if "." in s:
            return float(s)
        return int(s)
    except ValueError:
        pass
    return s


def _vault_email_to_chain_input(md_path: Path) -> Dict[str, Any]:
    """
    Read a vault email .md file and return a dict that looks like the
    original email_scan items (so Agent 1 mapping in v17 still works).
    """
    text = md_path.read_text(encoding="utf-8")
    meta, body = _parse_frontmatter(text)

    # Build the chain-shaped dict
    item = {
        "_vault_path": str(md_path),
        "_vault_meta": meta,
        "message_id": meta.get("message_id", ""),
        "date": meta.get("date", ""),
        "from": meta.get("from", "") or meta.get("sender", ""),
        "sender": meta.get("from", "") or meta.get("sender", ""),
        "subject": meta.get("subject", ""),
        "body_preview": body.split("## Body", 1)[1].split("## ", 1)[0].strip()
                          if "## Body" in body else body,
        "body_length": meta.get("body_length", len(body)),
        # Already-extracted fields take priority over raw
        "violation_type": meta.get("violation_type", ""),
        "statute_cited": meta.get("statute_cited", ""),
        "category": meta.get("category", ""),
        "entity": meta.get("entity", ""),
        "agency": {
            "name": meta.get("agency_name", ""),
            "type": meta.get("agency_type", ""),
            "domain": meta.get("agency_domain", ""),
            "statutes": meta.get("agency_statutes", []) or [],
            "match_reason": meta.get("agency_match_reason", ""),
        },
        "case_number": meta.get("case_number", ""),
        "case_references": meta.get("case_references", []) or [],
        "deadlines": meta.get("deadlines", []) or [],
        "attachments": meta.get("attachments", []) or [],
        "matched_case": meta.get("case_number", ""),
        # Chain processing state
        "_chain_processed": meta.get("chain_processed", False),
        "_existing_chain_id": meta.get("chain_id", ""),
    }
    return item


def _load_vault_emails(case_id: str, force: bool = False, limit: int = 0) -> List[Dict[str, Any]]:
    """Glob cases/{slug}/emails/*.md and yield shaped dicts."""
    if case_id == "all":
        all_items = []
        for cid in CASE_SLUG_MAP:
            all_items.extend(_load_vault_emails(cid, force=force, limit=0))
        if limit:
            return all_items[:limit]
        return all_items

    slug = CASE_SLUG_MAP.get(case_id)
    if not slug:
        log.warning("case_id %s not in CASE_SLUG_MAP", case_id)
        return []

    emails_dir = VAULT_CASES / slug / "emails"
    if not emails_dir.exists():
        log.warning("vault path missing: %s", emails_dir)
        return []

    md_files = sorted(emails_dir.glob("*.md"))
    log.info("vault: found %d .md files in %s", len(md_files), slug)

    items = []
    skipped = 0
    for p in md_files:
        try:
            item = _vault_email_to_chain_input(p)
            if item.get("_chain_processed") and not force:
                skipped += 1
                continue
            items.append(item)
            if limit and len(items) >= limit:
                break
        except Exception as e:
            log.warning("failed to load %s: %s", p.name, e)

    if skipped:
        log.info("vault: skipped %d already-processed (use --force to override)", skipped)

    return items


def _writeback_chain_output(state, source_md_path: str, case_id: str):
    """
    Write chain output to vault and update source email frontmatter.
    v18b2: handles AgentResult OBJECTS via getattr() instead of dict.get().
    """
    try:
        slug = CASE_SLUG_MAP.get(case_id)
        if not slug:
            return

        chain_outputs_dir = VAULT_CASES / slug / "chain_outputs"
        chain_outputs_dir.mkdir(parents=True, exist_ok=True)

        chain_id = getattr(state, "chain_id", "unknown_chain")
        ok = getattr(state, "ok", False)
        results = getattr(state, "results", []) or []

        def _r(obj, key, default=None):
            """Get attr OR dict key â€” works for AgentResult or plain dict."""
            if isinstance(obj, dict):
                return obj.get(key, default)
            return getattr(obj, key, default)

        chain_md = chain_outputs_dir / f"{chain_id}.md"

        lines = ["---"]
        lines.append("type: chain_output")
        lines.append(f"chain_id: {chain_id}")
        lines.append(f"case_id: {case_id}")
        lines.append(f"case_slug: {slug}")
        lines.append(f"source_email: {Path(source_md_path).name if source_md_path else ''}")
        lines.append(f"chain_ok: {str(ok).lower()}")
        lines.append(f"agent_count: {len(results)}")
        ts = chain_id.split("_", 1)[1] if "_" in chain_id else ""
        lines.append(f"timestamp: {ts}")
        lines.append("agents:")
        for r in results:
            lines.append(f"  - agent: {_r(r, 'agent', '?')}")
            lines.append(f"    name: {_r(r, 'name', '')}")
            lines.append(f"    ok: {_r(r, 'ok', False)}")
            lines.append(f"    elapsed_ms: {_r(r, 'elapsed_ms', 0)}")
        lines.append("---")
        lines.append("")
        lines.append(f"# Chain {chain_id}")
        lines.append("")
        if source_md_path:
            stem = Path(source_md_path).stem
            slug_for_link = slug
            lines.append(f"**Source email:** [[{slug_for_link}/emails/{stem}|{stem}]]")
        lines.append(f"**Case:** [[{slug}/_case|{case_id}]]")
        lines.append(f"**Status:** {'OK' if ok else 'FAILED'}")
        lines.append("")
        lines.append("## Agent Results")
        lines.append("")
        for r in results:
            agent_num = _r(r, "agent", "?")
            agent_name = _r(r, "name", "")
            agent_ok = _r(r, "ok", False)
            elapsed = _r(r, "elapsed_ms", 0)
            err = _r(r, "error", None)
            output = _r(r, "output", {})

            lines.append(f"### Agent {agent_num}: {agent_name}")
            lines.append(f"- ok: {agent_ok}")
            lines.append(f"- elapsed: {elapsed}ms")
            if err:
                lines.append(f"- error: `{err}`")
            lines.append("")
            lines.append("```json")
            try:
                lines.append(json.dumps(output, indent=2, default=str)[:3000])
            except Exception:
                lines.append(str(output)[:3000])
            lines.append("```")
            lines.append("")

        chain_md.write_text("\n".join(lines), encoding="utf-8")

        if source_md_path:
            _update_source_email_frontmatter(Path(source_md_path), chain_id, ok)

    except Exception as e:
        import traceback as _tb
        log.warning("vault writeback failed: %s", e)
        log.debug("traceback:\n%s", _tb.format_exc(limit=5))

def _update_source_email_frontmatter(md_path: Path, chain_id: str, ok: bool):
    """Update an email .md file's frontmatter to mark chain_processed=true."""
    try:
        from datetime import datetime as _dt
        text = md_path.read_text(encoding="utf-8")
        if not text.startswith("---"):
            return
        end = text.find("\n---", 3)
        if end == -1:
            return

        block = text[3:end]
        body = text[end + 3:].lstrip("\n").lstrip("-").lstrip("\n")

        # Replace specific lines if present, else append
        new_lines = []
        seen_processed = False
        seen_chain_id = False
        seen_last = False
        seen_status = False
        for line in block.splitlines():
            if line.startswith("chain_processed:"):
                new_lines.append(f"chain_processed: {str(ok).lower()}")
                seen_processed = True
            elif line.startswith("chain_id:"):
                new_lines.append(f'chain_id: "{chain_id}"')
                seen_chain_id = True
            elif line.startswith("last_processed:"):
                new_lines.append(f'last_processed: "{_dt.now().isoformat()}"')
                seen_last = True
            elif line.startswith("chain_status:"):
                new_lines.append(f'chain_status: {"ok" if ok else "failed"}')
                seen_status = True
            else:
                new_lines.append(line)

        if not seen_processed:
            new_lines.append(f"chain_processed: {str(ok).lower()}")
        if not seen_chain_id:
            new_lines.append(f'chain_id: "{chain_id}"')
        if not seen_last:
            new_lines.append(f'last_processed: "{_dt.now().isoformat()}"')
        if not seen_status:
            new_lines.append(f'chain_status: {"ok" if ok else "failed"}')

        new_block = "\n".join(new_lines)
        new_text = "---\n" + new_block + "\n---\n" + body
        md_path.write_text(new_text, encoding="utf-8")

    except Exception as e:
        log.warning("frontmatter update failed for %s: %s", md_path.name, e)


def _load_input(path: Path) -> List[Dict[str, Any]]:
    """
    Load chain input. Handles three formats:
      1. {"results": [...]}     - email_scan output (most common)
      2. {"emails": [...]}      - alternate scan output
      3. [{...}, {...}]         - bare list of items
      4. {single_item}          - single payload
    Returns a flat list of dicts.
    """
    if not path.exists():
        raise FileNotFoundError(f"Input not found: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))

    # Unwrap email_scan / alternate scan formats
    if isinstance(raw, dict):
        for key in ("results", "emails", "items", "payload", "data"):
            if key in raw and isinstance(raw[key], list):
                log.info("input: unwrapped %d items from key '%s'", len(raw[key]), key)
                return raw[key]
        # No recognized array key â€” treat the whole dict as a single item
        return [raw]

    if isinstance(raw, list):
        return raw

    # Anything else â€” wrap defensively
    return [{"text": str(raw)}]

def main() -> int:
    p = argparse.ArgumentParser(description="Beezle 10-agent chain runner")
    p.add_argument("--input", type=str, help="JSON file (single payload, list, or email_scan)")
    p.add_argument("--case",  type=str, default=None, help="case_id")
    p.add_argument("--limit", type=int, default=0, help="max items to process (0 = all)")
    p.add_argument("--skip",  type=str, default="", help="comma-separated agent numbers to skip")
    p.add_argument("--only",  type=str, default="", help="comma-separated agent numbers to run")
    p.add_argument("--no-vault", action="store_true", help="do not write vault markdown")
    p.add_argument("--force", action="store_true",
                    help="v18b: reprocess even if chain_processed=true in vault")
    p.add_argument("--vault-folder", type=str, default=None,
                    help="v18b: custom path to a folder of .md emails")
    args = p.parse_args()

    skip = [int(x) for x in args.skip.split(",") if x.strip()]
    only = [int(x) for x in args.only.split(",") if x.strip()]

    cr = ChainRunner(vault=not args.no_vault)

    # v18b_vault_mode: prefer vault when --case given without --input
    items = []
    if args.input:
        items = _load_input(Path(args.input))
        log.info("loaded %d items from %s", len(items), args.input)
    elif args.vault_folder:
        # Custom vault folder of .md files
        folder = Path(args.vault_folder)
        if not folder.exists():
            log.error("vault folder not found: %s", folder)
            return 1
        md_files = sorted(folder.glob("*.md"))
        for p in md_files:
            try:
                items.append(_vault_email_to_chain_input(p))
            except Exception as e:
                log.warning("skip %s: %s", p.name, e)
        if not args.force:
            items = [x for x in items if not x.get("_chain_processed")]
        log.info("loaded %d items from vault folder %s", len(items), folder)
    elif args.case:
        # Vault mode: read from cases/{slug}/emails/
        items = _load_vault_emails(args.case, force=args.force, limit=args.limit or 0)
        log.info("loaded %d items from vault for case=%s", len(items), args.case)
    else:
        # No input source â€” demo mode
        payload = {"text": "Demo chain with no input", "violation_type": "unknown"}
        cr.run(payload, case_id=args.case, skip=skip, only=only)
        return 0
    if args.limit:
        items = items[: args.limit]

    log.info("processing %d items", len(items))
    n_ok = 0
    for i, item in enumerate(items, 1):
        case_id = args.case if args.case and args.case != "all" else (
            item.get("case_id") or item.get("case_number") or item.get("matched_case")
        )
        state = cr.run(item, case_id=case_id, skip=skip, only=only)
        if state.ok:
            n_ok += 1
        # v18b: writeback to vault if source was a vault email
        if item.get("_vault_path") and case_id and case_id in CASE_SLUG_MAP:
            _writeback_chain_output(state, item.get("_vault_path"), case_id)
        if i % 50 == 0:
            log.info("progress: %d/%d (%d ok)", i, len(items), n_ok)
    log.info("done: %d/%d ok", n_ok, len(items))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
