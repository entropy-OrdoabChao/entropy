"""
Beezle Scenario Engine v0
=========================
Deterministic fact-pattern decomposition into structured legal theories.

ABSOLUTE RULES:
  - NO LLM CALLS. Pure Python, pure data.
  - NO GUESSING FACTS. Works with what's in the case facts YAML.
  - NO UNSUPPORTED THEORIES. If core elements aren't matched, theory is skipped.
  - DETERMINISTIC. Same input -> same output. Always.

INPUT:
  - fact_pattern: list of facts (strings) OR single string
  - context: {role, jurisdiction, entity_type, ...}

OUTPUT:
  Structured JSON matching locked schema (see _empty_result()).

ARCHITECTURE:
  facts
    -> trigger matching (finds candidate theories)
    -> element matching (confirms support)
    -> graph enrichment (pulls authority via obsidian_query)
    -> duty map (if entity_type is licensed)
    -> cross-theory synthesis
    -> structured output

INTEGRATION:
  - Registered as MCP tool `scenario_decompose` via mcp_tools_scenario.py
  - Called by Hermes like any other tool
  - Returns JSON, LLM formats into prose in Hermes UI
"""
# GENERIC_V03_APPLIED  # carrier-neutral element signatures

from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
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

import json
import logging
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_PATH = _BEEZLE_ROOT
PIPELINE_DIR = BASE_PATH / "pipeline"
THEORY_MAP_PATH = PIPELINE_DIR / "theory_map.yaml"
DUTY_MAPS_DIR = PIPELINE_DIR / "duty_maps"
LOGS_DIR = BASE_PATH / "logs" / "engines"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# Obsidian graph access — reuse existing singleton
import sys
OBSIDIAN_SYNC_DIR = BASE_PATH / "obsidian_sync"
if str(OBSIDIAN_SYNC_DIR) not in sys.path:
    sys.path.insert(0, str(OBSIDIAN_SYNC_DIR))

try:
    from obsidian_query import get_index
    OBSIDIAN_AVAILABLE = True
except Exception as e:
    OBSIDIAN_AVAILABLE = False
    _IMPORT_ERROR = str(e)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("scenario_engine")
if not logger.handlers:
    handler = logging.FileHandler(LOGS_DIR / "scenario_engine.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(message)s"
    ))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


# ===========================================================================
# Data structures
# ===========================================================================

@dataclass
class TheoryMatch:
    name: str
    display_name: str
    triggers_hit: list[str] = field(default_factory=list)
    core_elements: list[str] = field(default_factory=list)
    matched_elements: list[str] = field(default_factory=list)
    missing_elements: list[str] = field(default_factory=list)
    supporting_elements_matched: list[str] = field(default_factory=list)
    confidence: str = "low"  # high / medium / low
    domains: list[str] = field(default_factory=list)
    jurisdictions: list[str] = field(default_factory=list)
    regulators: list[str] = field(default_factory=list)
    seed_nodes: list[str] = field(default_factory=list)
    authority: dict[str, list] = field(default_factory=dict)
    doctrines: list[str] = field(default_factory=list)
    remedies: dict[str, Any] = field(default_factory=dict)
    venue: dict[str, Any] = field(default_factory=dict)
    procedural_vehicle: str = ""
    graph_coverage: str = "unknown"  # full / partial / gap / unknown


# ===========================================================================
# Theory map loading
# ===========================================================================

_THEORY_MAP_CACHE: dict | None = None



# === ENGINE_BLACKLIST_V1 ===
# Per case_type source-prefix blacklist. Mirrors Block 6.9.3 matrix logic
# but enforced at duty ATTACHMENT time so claim.attached_duties never
# contains forbidden sources. Applied via _filter_disallowed_for_case().

ENGINE_DISALLOWED_SOURCES_BY_CASE_TYPE = {
    "contracts_other": [
        "15-USC-1681",   # FCRA
        "15-USC-1692",   # FDCPA
        "12-USC-2605",   # RESPA QWR
        "15-USC-1601",   # TILA
    ],
    "fraud_fcra": [
        "12-USC-2605",
        "12-CFR-1024",
    ],
    "quiet_title": [
        "15-USC-1681",
        "42-Pa-CS-8371",
    ],
    "bad_faith": [
        "15-USC-1681",
        "12-USC-2605",
    ],
}


# BEEZLE_FN_UNUSED 2026-06-13: _resolve_engine_disallowed (no static refs in pipeline/)
def _resolve_engine_disallowed(canonical_id: str, case_type: str) -> list:
    """Resolve disallowed source prefixes for a case. Registry override
    via `disallowed_sources:` field wins over case_type default."""
    try:
        import json as _json
        from pathlib import Path as _P
        reg_path = _P(str((_BEEZLE_ROOT / "cases_index" / "case_registry.json")))
        if reg_path.exists():
            reg = _json.loads(reg_path.read_text(encoding="utf-8"))
            entry = reg.get(canonical_id, {})
            override = entry.get("disallowed_sources")
            if isinstance(override, list):
                return override
    except Exception:
        pass
    return ENGINE_DISALLOWED_SOURCES_BY_CASE_TYPE.get(case_type, [])


# BEEZLE_FN_UNUSED 2026-06-13: _filter_disallowed_for_case (no static refs in pipeline/)
def _filter_disallowed_for_case(duties: list, disallowed: list) -> list:
    """Drop duties whose source starts with any disallowed prefix."""
    if not disallowed:
        return duties
    out = []
    for d in duties:
        src_ = (d.get("source") or "").strip()
        if any(src_.startswith(prefix) for prefix in disallowed):
            continue
        out.append(d)
    return out
# === END ENGINE_BLACKLIST_V1 ===


# === CORPUS_CLEANUP_V1 ===
# Vague filler entries to strip from claim.attached_duties at engine time
# (mirrors the matrix-time strip from Block 6.9.2 _VAGUE_DROPLIST).
ENGINE_VAGUE_DROPLIST = {
    "Regulatory-Compliance-Cross-Cutting",
    # add more identifiers here as patterns surface them
}


# BEEZLE_FN_UNUSED 2026-06-13: _filter_vague_duties (no static refs in pipeline/)
def _filter_vague_duties(duties: list) -> list:
    """Drop duties whose source matches the vague droplist."""
    if not duties:
        return duties
    return [d for d in duties
            if (d.get("source") or "").strip() not in ENGINE_VAGUE_DROPLIST]


# UTPCPL fact triggers. The pa_utpcpl_violation theory has
# consumer_protection in its domains, which is too broad. UTPCPL only
# fires when fact text actually contains deceptive/unfair conduct
# language. Otherwise it's a false positive across consumer cases.
UTPCPL_FACT_TRIGGERS = (
    "deceptive",
    "unfair trade",
    "consumer harm",
    "consumer fraud",
    "false representation",
    "misleading statement",
    "unfair practice",
    "utpcpl",
    "trade practice",
)


def _utpcpl_should_fire(facts_text: str) -> bool:
    """True iff facts_text contains an UTPCPL trigger phrase."""
    if not facts_text:
        return False
    fl = facts_text.lower()
    return any(t in fl for t in UTPCPL_FACT_TRIGGERS)


# BEEZLE_FN_UNUSED 2026-06-13: _filter_utpcpl_if_no_triggers (no static refs in pipeline/)
def _filter_utpcpl_if_no_triggers(theories: list, facts_text: str) -> list:
    """Strip pa_utpcpl_violation if no UTPCPL fact triggers present."""
    if _utpcpl_should_fire(facts_text):
        return theories
    return [t for t in theories
            if (t.get("id") or t.get("theory_id")) != "pa_utpcpl_violation"]
# === END CORPUS_CLEANUP_V1 ===


def load_theory_map(force: bool = False) -> dict:
    """Load theory_map.yaml with caching. Call force=True to reload."""
    global _THEORY_MAP_CACHE
    if _THEORY_MAP_CACHE is not None and not force:
        return _THEORY_MAP_CACHE

    if not THEORY_MAP_PATH.exists():
        raise FileNotFoundError(
            f"theory_map.yaml not found at {THEORY_MAP_PATH}. "
            "Copy the file from the Beezle distribution."
        )

    with THEORY_MAP_PATH.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    _THEORY_MAP_CACHE = data
    logger.info(f"Loaded theory map v{data.get('version')} "
                f"({data.get('total_theories')} theories)")
    return data


# ===========================================================================
# Fact normalization
# ===========================================================================

def normalize_facts(fact_pattern: Any) -> list[str]:
    """Convert input facts to a normalized list of lowercase strings."""
    if fact_pattern is None:
        return []

    if isinstance(fact_pattern, str):
        # Split multi-line strings into sentences
        lines = re.split(r"[\n\.\!\?]+", fact_pattern)
        facts = [ln.strip() for ln in lines if ln.strip()]
    elif isinstance(fact_pattern, list):
        facts = [str(f).strip() for f in fact_pattern if f]
    else:
        facts = [str(fact_pattern)]

    return [f.lower() for f in facts if f]


def facts_blob(facts: list[str]) -> str:
    """Join facts into a single searchable blob."""
    return " ; ".join(facts)


# ===========================================================================
# Trigger matching (Phase 1 of decomposition)
# ===========================================================================

def match_triggers(facts: list[str], theory_def: dict) -> list[str]:
    """Return list of triggers hit by the fact pattern."""
    blob = facts_blob(facts)
    hits = []
    for trigger in theory_def.get("triggers", []):
        if trigger.lower() in blob:
            hits.append(trigger)
    return hits


# ===========================================================================
# Element matching (Phase 2 — strict)
# ===========================================================================

# Element -> list of fact patterns that indicate the element is met.
# This is the "element dictionary" the system uses to confirm whether
# a theory's core elements are actually supported by facts.
# Extensible: add entries as the baseline loop discovers gaps.
ELEMENT_SIGNATURES: dict[str, list[str]] = {
    # Credit reporting elements
    "consumer_reporting_agency_or_furnisher": [
        "credit bureau", "equifax", "experian", "transunion",
        "furnisher", "data furnisher", "credit reporting agency",
    ],
    "inaccuracy_in_report": [
        "inaccurate", "incorrect", "wrong", "error in report",
        "false information", "reported incorrectly",
    ],
    "reckless_disregard_of_duty": [
        "ignored dispute", "refused to investigate", "no investigation",
        "reckless", "knowing", "willful", "deliberate",
    ],
    "dispute_submitted": [
        "submitted dispute", "disputed", "sent dispute",
        "filed dispute", "notified", "notice of dispute",
    ],
    "investigation_inadequate": [
        "no investigation", "inadequate investigation", "cursory",
        "perfunctory", "did not investigate",
    ],
    "negligent_failure_to_comply": [
        "failed to comply", "failed to follow", "negligent",
        "did not follow procedure",
    ],
    "actual_damages": [
        "damaged", "harmed", "loss", "credit denied", "denied credit",
        "financial harm", "lost", "cost me",
    ],

    # Debt collection
    "debt_collector": [
        "debt collector", "collection agency", "collections",
        "sent to collection", "collection letter",
        "debt collection agency",
        "collector claimed",
        "sent the disputed amount",
    
        "collected",
        "collecting",
        "collection of",
        "seeks collection",
        "collection stalled",
        "garnishment",
    ],
    "consumer_debt": [
        "personal debt", "consumer debt", "personal transaction",
        "household", "personal use", "premium", "insurance bill",
        "amount",
    ],
    "false_or_misleading_representation": [
        "falsely represented", "false statement", "misleading",
        "said i owed", "claimed i owed", "not owed",
        "sent to collection",
        "sent the disputed amount to a debt collection",
        "debt collector claimed",
        "collector claimed i owed",
        "claimed the amount",
    ],
    "amount_not_authorized_by_agreement_or_law": [
        "amount not owed", "not authorized", "no agreement",
        "beyond contract", "retained after cancellation",
        "no insurable interest",
        "absence of insurable interest",
        "after risk ceased",
        "after cancellation",
        "despite the absence",
        "not authorized by any agreement",
        "beyond the contract",
        "after the risk ceased",
        "not authorized by any agreement or by law",
    ],
    "debt_disputed": [
        "i disputed", "told them wrong", "contested", "challenged",
        "disputed the debt",
        "refused the collection",
    ],

    # Insurance — unearned premium
    "insurance_contract_existed": [
        "insurance policy", "policy with", "insured by",
        "auto policy", "home policy", "liability policy",
        "maintained an insurance policy", "had a policy",
        "insurance policy with",
        "had a policy with",
        "policy with insurer",
    ],
    "insurable_risk_ceased": [
        "sold the vehicle", "sold the car", "no longer own",
        "vehicle transferred", "no insurable interest",
        "risk ceased", "title transferred",
        "sold the insured vehicle",
        "sold my vehicle",
        "no insurable risk",
        "insurable risk ceased",
        "absence of insurable interest",
        "no longer had any insurable interest",
    ],
    "premium_retained_after_risk_ceased": [
        "retained premium", "kept premium", "continued to charge",
        "continued billing", "auto withdrawal",
        "refused refund", "no refund",
        "retained all collected premiums",
        "continued to charge premiums",
        "automatic withdrawal",
        "kept the premium",
        "kept collecting",
    ],
    "notice_to_insurer": [
        "notified the insurer", "told the insurer", "informed",
        "sent notice", "requested cancellation",
    ],
    "refund_demanded": [
        "demanded refund", "requested refund", "asked for money back",
        "asked for refund",
    ],
    "refund_refused": [
        "refused refund", "denied refund", "refused to refund",
        "kept the money", "would not return",
    ],

    # Bad faith insurance
    "insurer_denied_or_delayed_claim": [
        "denied claim", "delayed claim", "refused to pay",
        "lowball", "underpaid claim",
    ],
    "no_reasonable_basis_for_denial": [
        "no basis", "unreasonable denial", "without reason",
        "no investigation", "arbitrary denial",
    ],
    "insurer_knew_no_reasonable_basis": [
        "knew", "knowingly", "aware", "ignored evidence",
    ],
    "valid_insurance_contract": [
        "valid policy", "in force", "active policy",
        "insurance contract", "paid premiums",
        "maintained an insurance policy",
        "insurance policy with",
        "had coverage",
        "auto policy",
        "policy with insurer",
    ],

    # Contract
    "valid_contract": [
        "agreement", "contract", "agreed to", "signed",
        "terms", "offer and acceptance",
        "maintained an insurance policy",
        "insurance policy with",
        "had a policy",
        "policy with insurer",
        "auto policy",
        "insurance contract",
    ],
    "plaintiff_performance_or_excuse": [
        "paid", "performed", "fulfilled", "complied",
        "did my part",
        "maintained",
        "policyholder",
        "paid premiums",
        "continued to pay",
        "insured",
    ],
    "defendant_breach": [
        "breached", "violated", "failed to", "refused to",
        "did not honor",
        "refused refund",
        "refused to return",
        "continued to charge",
        "retained premium",
        "retained all collected",
        "kept collecting",
        "continued billing",
    ],
    "damages": [
        "damaged", "loss", "harmed", "cost me", "out of pocket",
    
        "ascertainable loss",
        "financial harm",
        "harm to plaintiff",
        "bad faith damages",
        "actual damages",
        "punitive",
        "credit denied",
        "monetary loss",
        "economic harm",
        "failed to pay",
        "withheld",
        "refused to pay",
        "causing ascertainable",
        "consumer harm",
    ],
    "performance_by_insured_or_excused": [
        "paid premiums", "premiums current", "not in default",
    
        "retained premium",
        "premium was retained",
        "premium paid",
        "policy in force",
        "valid claim",
        "submitted claim",
        "maintained policy",
        "coverage in effect",
        "received valid claims",
        "operative policies",
    ],
    "breach_by_insurer": [
        "insurer breached", "insurer failed", "insurer refused",
        "continued to charge",
        "retained premium",
        "retained all collected",
        "refused refund",
        "collected without",
        "continued billing",
    
        "failed to perform",
        "failed to pay",
        "refused to honor",
        "denied claim",
        "ignored claim",
        "bad faith",
        "failed to perform their contractual",
        "failed to perform obligations",
    ],

    # Unjust enrichment / conversion
    "benefit_conferred_on_defendant": [
        "paid", "money to", "gave", "transferred",
        "premium", "payment",
        "collected premiums",
        "continued to charge",
        "automatic withdrawal",
        "automatic withdrawals",
        "charged premiums",
        "billed",
    ],
    "appreciation_of_benefit": [
        "received", "accepted", "kept", "retained",
        "retained all collected",
        "kept collected",
        "collected",
        "withheld",
    ],
    "retention_unjust_under_circumstances": [
        "unjust", "no service", "no consideration",
        "no insurable interest", "no benefit to me",
        "risk ceased",
        "absence of insurable interest",
        "despite the absence",
        "no insurable risk",
        "after risk ceased",
        "after the sale",
        "after cancellation",
        "continued to charge",
    ],
    "no_adequate_legal_remedy": [
        "no remedy", "no other recourse",
    ],
    "plaintiff_ownership_or_right_to_possession": [
        "my money", "my property", "belonged to me",
        "rightfully mine",
        "premiums paid",
        "my premiums",
        "collected premiums",
        "my vehicle",
        "insured by",
    ],
    "defendant_exercise_of_dominion": [
        "kept", "took", "retained", "withheld",
        "controlled",
        "retained all collected",
        "kept all",
        "withdrew",
        "continued charging",
        "automatic withdrawal",
        "automatic withdrawals",
        "billed",
        "collected premiums",
    ],
    "inconsistency_with_plaintiff_rights": [
        "without permission", "wrongfully", "against my wishes",
        "refused to return",
        "refused refund",
        "denied refund",
        "ignored demand",
        "refused to refund",
    ],
    "demand_for_return": [
        "demanded return", "asked for it back", "requested return",
        "demanded refund",
        "requested refund",
        "requested a refund",
        "asked for money back",
    ],

    # PA UTPCPL
    "consumer_transaction": [
        "consumer", "personal", "household", "purchased",
        "bought", "policy", "service",
        "insurance policy",
        "premium",
        "auto policy",
        "my policy",
    ],
    "unfair_or_deceptive_act": [
        "deceptive", "unfair", "misleading", "fraudulent",
        "false advertising",
        "refused refund",
        "continued to charge",
        "retained despite",
        "sent to collection",
        "no insurable interest",
        "absence of insurable interest",
        "claimed i failed to notify",
    ],
    "reliance": [
        "relied on", "believed", "trusted",
    
        "consumer harm",
        "relied upon",
        "in reliance",
        "plaintiff relied",
        "acted in reliance",
        "based on",
        "causing ascertainable",
        "as a consumer",
    ],
    "ascertainable_loss": [
        "lost money", "damages", "out of pocket", "cost me",
    
        "ascertainable loss",
        "consumer harm",
        "financial harm",
        "harm to plaintiff",
        "credit denied",
        "denied credit",
        "monetary loss",
        "economic harm",
        "injury",
        "causing ascertainable loss",
    ],

    # RESPA
    "mortgage_loan": [
        "mortgage", "home loan", "loan", "deed of trust",
    ],
    "qwr_sent_to_servicer": [
        "qwr", "qualified written request", "written request",
        "servicer request", "sent to servicer",
    ],
    "servicer_failed_to_respond_within_timeframe": [
        "no response", "failed to respond", "ignored request",
        "past deadline", "untimely",
    ],

    # Quiet title
    "plaintiff_claim_of_ownership": [
        "owner", "title holder", "deed holder",
    ],
    "defendant_adverse_claim": [
        "claimed title", "asserted lien", "foreclosure",
        "recorded against",
    ],
    "defect_in_defendant_chain": [
        "broken chain", "no assignment", "MERS",
        "missing assignment", "robosigned",
    ],
    "ucc_article_3_issue": [
        "note", "holder", "holder in due course",
        "endorsement", "negotiation",
    ],

    # Fraudulent concealment
    "defendant_concealed_cause_of_action": [
        "hid", "concealed", "fraudulent concealment",
        "covered up", "misrepresented",
    ],
    "plaintiff_exercised_reasonable_diligence": [
        "could not have known", "no way to know",
        "investigated", "diligent",
    ],
    "discovery_of_cause_date": [
        "discovered", "found out", "learned",
        "became aware",
    ],
    "affirmative_acts_of_concealment": [
        "affirmative act", "lied", "falsely stated",
        "actively hid",
    ],

    # Regulatory
    "defendant_is_regulated_entity": [
        "bank", "insurance", "broker", "dealer",
        "servicer", "credit union", "licensed",
    ],
    "regulation_imposes_duty": [
        "required to", "must comply", "duty to",
        "regulation requires",
    ],
    "facts_suggest_nonperformance": [
        "failed to", "did not", "never",
        "no record of",
    ],
}


def match_element(element_key: str, facts: list[str]) -> bool:
    """Return True if any signature for the element appears in facts."""
    signatures = ELEMENT_SIGNATURES.get(element_key, [])
    if not signatures:
        # Unknown element — conservatively return False (no speculation)
        return False
    blob = facts_blob(facts)
    return any(sig.lower() in blob for sig in signatures)


def compute_confidence(
    triggers_hit: int,
    core_total: int,
    core_matched: int,
    supporting_matched: int,
) -> str:
    """Compute theory confidence based on match strength."""
    if core_total == 0:
        return "low"
    core_ratio = core_matched / core_total

    if core_ratio >= 1.0 and triggers_hit >= 2:
        return "high"
    if core_ratio >= 0.67:
        return "medium"
    if core_ratio >= 0.5:
        return "low"
    return "low"


# ===========================================================================
# Graph enrichment (Phase 3 — pull authority from Obsidian)
# ===========================================================================

def enrich_with_graph(theory: TheoryMatch, target_court: str, max_depth: int = 2) -> None:
    """Walk the Obsidian graph from theory.seed_nodes to populate authority.

    Calls ObsidianIndex.trace_authority(concept_or_id, target_court, max_depth).
    """
    if not OBSIDIAN_AVAILABLE:
        theory.authority = {"error": f"Obsidian not available: {_IMPORT_ERROR}"}
        theory.graph_coverage = "unknown"
        return

    if not theory.seed_nodes:
        theory.authority = {}
        theory.graph_coverage = "gap"
        return

    try:
        idx = get_index()
    except Exception as e:
        logger.warning(f"Could not load Obsidian index: {e}")
        theory.authority = {"error": str(e)}
        theory.graph_coverage = "unknown"
        return

    all_statutes = []
    all_regulations = []
    all_cases = []
    all_rules = []
    all_constitutional = []
    total_visited = 0

    for seed in theory.seed_nodes:
        try:
            # Real ObsidianIndex API: trace_authority(concept_or_id, target_court, max_depth)
            result = idx.trace_authority(
                concept_or_id=seed,
                target_court=target_court,
                max_depth=max_depth,
            )
            all_statutes.extend(result.get("binding_statutes", []))
            all_regulations.extend(result.get("binding_regulations", []))
            all_cases.extend(result.get("binding_cases", []))
            all_rules.extend(result.get("binding_rules", []))
            all_constitutional.extend(result.get("binding_constitutional", []))
            total_visited += int(result.get("total_nodes_visited", 0))
        except Exception as e:
            logger.warning(f"trace_authority failed for {seed}: {e}")

    # Deduplicate by canonical_id
    def dedupe(items):
        seen = set()
        out = []
        for item in items:
            cid = item.get("canonical_id") if isinstance(item, dict) else str(item)
            if cid not in seen:
                seen.add(cid)
                out.append(item)
        return out

    theory.authority = {
        "statutes": dedupe(all_statutes)[:10],
        "regulations": dedupe(all_regulations)[:10],
        "cases": dedupe(all_cases)[:10],
        "rules": dedupe(all_rules)[:10],
        "constitutional": dedupe(all_constitutional)[:5],
    }

    total_authority = sum(len(v) for v in theory.authority.values() if isinstance(v, list))
    if total_authority >= 10:
        theory.graph_coverage = "full"
    elif total_authority >= 3:
        theory.graph_coverage = "partial"
    else:
        theory.graph_coverage = "gap"


# ===========================================================================
# Output structure (LOCKED)
# ===========================================================================

def _empty_result() -> dict:
    """The locked output schema. All scenario_decompose calls return this shape."""
    return {
        "facts": [],
        "theories": [],
        "domains": [],
        "jurisdictions": [],
        "regulators": [],
        "actions": {
            "civil": [],
            "regulatory": [],
            "enforcement": [],
        },
        "strategy": [],
        "gaps": [],
        "telemetry": {
            "theories_evaluated": 0,
            "theories_returned": 0,
            "nodes_visited": 0,
            "runtime_ms": 0,
        },
    }


def _venue_to_action(
    theory: TheoryMatch,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Split a theory's venue into civil/regulatory/enforcement actions."""
    civil = []
    regulatory = []
    enforcement = []

    venue = theory.venue or {}

    if "federal" in venue:
        civil.append({
            "theory": theory.name,
            "court": venue["federal"].get("court"),
            "basis": venue["federal"].get("basis"),
            "vehicle": theory.procedural_vehicle,
            "forum": "federal",
        })
    if "state" in venue:
        civil.append({
            "theory": theory.name,
            "court": venue["state"].get("court"),
            "basis": venue["state"].get("basis"),
            "vehicle": theory.procedural_vehicle,
            "forum": "state",
        })
    if "agency" in venue:
        regulatory.append({
            "theory": theory.name,
            "body": venue["agency"].get("body"),
            "basis": venue["agency"].get("basis"),
            "vehicle": "regulatory complaint",
        })
    if "any" in venue:
        civil.append({
            "theory": theory.name,
            "note": venue["any"].get("note"),
            "vehicle": theory.procedural_vehicle,
            "forum": "either",
        })

    # Regulators beyond agency venue become enforcement tips
    for reg in theory.regulators:
        if not any(reg in a.get("body", "") for a in regulatory):
            enforcement.append({
                "theory": theory.name,
                "regulator": reg,
                "action_type": "tip_or_complaint",
            })

    return civil, regulatory, enforcement


# ===========================================================================
# Main entry point
# ===========================================================================

def scenario_decompose(
    fact_pattern: Any,
    context: dict | None = None,
    max_depth: int = 2,
    min_confidence: str = "low",
) -> dict:
    """
    Decompose a fact pattern into structured legal theories.

    Args:
        fact_pattern: Either a list of fact strings or a single string to split.
        context: Dict with keys: role, jurisdiction, entity_type, target_court.
        max_depth: Graph walk depth for authority enrichment.
        min_confidence: 'low' | 'medium' | 'high'. Theories below this are excluded.

    Returns:
        Dict matching the locked schema from _empty_result().
    """
    import time
    t0 = time.time()

    result = _empty_result()
    ctx = context or {}

    # Normalize facts
    facts = normalize_facts(fact_pattern)
    result["facts"] = facts

    if not facts:
        result["strategy"].append("No facts provided; cannot decompose.")
        return result

    # Load theory map
    try:
        theory_map = load_theory_map()
    except Exception as e:
        logger.error(f"Failed to load theory map: {e}")
        result["strategy"].append(f"Theory map load error: {e}")
        return result

    target_court = ctx.get("target_court") or ctx.get("federal_district") or "EDPA"

    # Phase 1 — Trigger matching
    candidate_theories = []
    for theory_key, theory_def in theory_map.get("theories", {}).items():
        if theory_def.get("status") == "deprecated":
            continue
        triggers_hit = match_triggers(facts, theory_def)
        if triggers_hit:
            candidate_theories.append((theory_key, theory_def, triggers_hit))

    result["telemetry"]["theories_evaluated"] = len(candidate_theories)
    logger.info(f"Phase 1: {len(candidate_theories)} candidate theories after trigger matching")

    # Phase 2 — Element matching (strict)
    confidence_rank = {"low": 1, "medium": 2, "high": 3}
    min_rank = confidence_rank.get(min_confidence, 1)

    matched_theories: list[TheoryMatch] = []
    for theory_key, theory_def, triggers_hit in candidate_theories:
        core_elements = theory_def.get("core_elements", [])
        supporting_elements = theory_def.get("supporting_elements", [])

        matched_core = [e for e in core_elements if match_element(e, facts)]
        missing_core = [e for e in core_elements if e not in matched_core]
        matched_supporting = [e for e in supporting_elements if match_element(e, facts)]

        # STRICT: Must have at least one matched core element
        if not matched_core:
            logger.info(f"  SKIP {theory_key}: no core elements matched")
            continue

        confidence = compute_confidence(
            triggers_hit=len(triggers_hit),
            core_total=len(core_elements),
            core_matched=len(matched_core),
            supporting_matched=len(matched_supporting),
        )

        if confidence_rank[confidence] < min_rank:
            logger.info(f"  SKIP {theory_key}: confidence {confidence} below threshold")
            continue

        theory = TheoryMatch(
            name=theory_key,
            display_name=theory_def.get("display_name", theory_key),
            triggers_hit=triggers_hit,
            core_elements=core_elements,
            matched_elements=matched_core,
            missing_elements=missing_core,
            supporting_elements_matched=matched_supporting,
            confidence=confidence,
            domains=theory_def.get("domains", []),
            jurisdictions=theory_def.get("jurisdictions", []),
            regulators=theory_def.get("regulators", []),
            seed_nodes=theory_def.get("seed_nodes", []),
            doctrines=theory_def.get("doctrines", []),
            remedies=theory_def.get("remedies", {}),
            venue=theory_def.get("venue", {}),
            procedural_vehicle=theory_def.get("procedural_vehicle", ""),
        )
        matched_theories.append(theory)

    logger.info(f"Phase 2: {len(matched_theories)} theories passed element matching")

    # Phase 3 — Graph enrichment
    for theory in matched_theories:
        enrich_with_graph(theory, target_court=target_court, max_depth=max_depth)

    # Phase 4 — Compile output
    for theory in matched_theories:
        result["theories"].append({
            "name": theory.name,
            "display_name": theory.display_name,
            "confidence": theory.confidence,
            "triggers_hit": theory.triggers_hit,
            "core_elements": theory.core_elements,
            "matched_elements": theory.matched_elements,
            "missing_elements": theory.missing_elements,
            "supporting_elements_matched": theory.supporting_elements_matched,
            "domains": theory.domains,
            "jurisdictions": theory.jurisdictions,
            "regulators": theory.regulators,
            "doctrines": theory.doctrines,
            "authority": theory.authority,
            "graph_coverage": theory.graph_coverage,
            "remedies": theory.remedies,
            "venue": theory.venue,
            "procedural_vehicle": theory.procedural_vehicle,
        })

        # Aggregate
        for d in theory.domains:
            if d not in result["domains"]:
                result["domains"].append(d)
        for j in theory.jurisdictions:
            if j not in result["jurisdictions"]:
                result["jurisdictions"].append(j)
        for r in theory.regulators:
            if r not in result["regulators"]:
                result["regulators"].append(r)

        civil, regulatory, enforcement = _venue_to_action(theory)
        result["actions"]["civil"].extend(civil)
        result["actions"]["regulatory"].extend(regulatory)
        result["actions"]["enforcement"].extend(enforcement)

    # Phase 5 — Strategy observations
    if len(matched_theories) >= 3:
        result["strategy"].append(
            f"{len(matched_theories)} theories support the fact pattern. "
            "Consider parallel filings across venues."
        )
    high_conf = [t for t in matched_theories if t.confidence == "high"]
    if high_conf:
        result["strategy"].append(
            f"{len(high_conf)} high-confidence theories with all core elements matched: "
            + ", ".join(t.display_name for t in high_conf)
        )
    if any("state" in t.jurisdictions for t in matched_theories) and \
       any("federal" in t.jurisdictions for t in matched_theories):
        result["strategy"].append(
            "Fact pattern supports both state AND federal claims — consider parallel "
            "filings or supplemental jurisdiction."
        )
    if result["actions"]["regulatory"]:
        result["strategy"].append(
            f"Regulatory complaint lanes available: "
            + ", ".join(sorted(set(a["body"] for a in result["actions"]["regulatory"] if a.get("body"))))
        )

    # Phase 6 — Gap analysis
    for theory in matched_theories:
        if theory.graph_coverage == "gap":
            result["gaps"].append({
                "theory": theory.name,
                "issue": "No authority found in graph for seed nodes",
                "seeds": theory.seed_nodes,
                "recommendation": "Run scrapers for missing primary sources",
            })
        if theory.missing_elements:
            result["gaps"].append({
                "theory": theory.name,
                "issue": "Core elements not supported by current facts",
                "missing": theory.missing_elements,
                "recommendation": "Add facts supporting these elements if they exist",
            })

    # Telemetry
    result["telemetry"]["theories_returned"] = len(matched_theories)
    result["telemetry"]["runtime_ms"] = int((time.time() - t0) * 1000)

    logger.info(
        f"scenario_decompose complete: {len(matched_theories)} theories returned "
        f"in {result['telemetry']['runtime_ms']}ms"
    )

    # ---- PHASE 3.5 (v0.4 fix7): Auto-attach duties ----  # V04_FIX7_APPLIED
    # Enrich result['theories'] with entity_type/seed_nodes/id from theory_map
    # before passing to auto_attach_duties. scenario_decompose strips these fields
    # when building display output, but auto_attach_duties needs them.
    theory_duty_map = []
    try:
        # Build a name-to-source lookup from theory_map
        _src_by_name = {}
        for _tid, _tdef in (theory_map.get("theories", {}) or {}).items():
            _src_by_name[_tdef.get("display_name") or _tid] = (_tid, _tdef)

        # Enrich each output theory
        for _t in result.get('theories', []):
            _dn = _t.get('display_name') or _t.get('name')
            if _dn and _dn in _src_by_name:
                _tid, _tdef = _src_by_name[_dn]
                if not _t.get('id'):
                    _t['id'] = _tid
                if not _t.get('theory_id'):
                    _t['theory_id'] = _tid
                if not _t.get('entity_type'):
                    _t['entity_type'] = _tdef.get('entity_type')
                if not _t.get('seed_nodes'):
                    _t['seed_nodes'] = _tdef.get('seed_nodes')

        _target_court = target_court if 'target_court' in dir() else 'EDPA'
        _updated, theory_duty_map = auto_attach_duties(result['theories'], theory_map, _target_court)
        result['theories'] = _updated
        result['theory_duty_map'] = theory_duty_map
    except Exception as _autocall_err:
        import logging as _lg
        _lg.getLogger(__name__).warning(f"auto_attach_duties failed: {_autocall_err}")
        import traceback as _tb
        _lg.getLogger(__name__).warning(_tb.format_exc())
        result['theory_duty_map'] = []
    return result


# ===========================================================================
# CLI entry for standalone testing
# ===========================================================================

def main():
    """CLI for testing: python scenario_engine.py <case_facts.yaml>"""
    import argparse
    parser = argparse.ArgumentParser(description="Beezle Scenario Engine v0")
    parser.add_argument("facts_file", help="Path to YAML facts file")
    parser.add_argument("--depth", type=int, default=2, help="Graph walk depth")
    parser.add_argument("--min-confidence", default="low", choices=["low", "medium", "high"])
    parser.add_argument("--output", help="Write JSON output to file")
    args = parser.parse_args()

    facts_path = Path(args.facts_file)
    if not facts_path.exists():
        print(f"ERROR: facts file not found: {facts_path}")
        return 1

    with facts_path.open("r", encoding="utf-8") as f:
        case_data = yaml.safe_load(f)

    facts = case_data.get("facts", [])
    context = case_data.get("context", {})

    print(f"Running scenario_decompose on {case_data.get('case_name', 'unknown case')}")
    print(f"  {len(facts)} facts, depth={args.depth}, min_confidence={args.min_confidence}")

    result = scenario_decompose(
        fact_pattern=facts,
        context=context,
        max_depth=args.depth,
        min_confidence=args.min_confidence,
    )

    print(f"\n=== RESULT ===")
    print(f"Theories returned: {result['telemetry']['theories_returned']}")
    print(f"Runtime: {result['telemetry']['runtime_ms']}ms")
    print(f"\nDomains: {', '.join(result['domains'])}")
    print(f"Jurisdictions: {', '.join(result['jurisdictions'])}")
    print(f"Regulators: {', '.join(result['regulators'])}")
    print(f"\nTheories:")
    for t in result["theories"]:
        print(f"  [{t['confidence'].upper()}] {t['display_name']}")
        print(f"    Matched: {', '.join(t['matched_elements'])}")
        if t['missing_elements']:
            print(f"    Missing: {', '.join(t['missing_elements'])}")
        print(f"    Graph coverage: {t['graph_coverage']}")
    print(f"\nCivil actions: {len(result['actions']['civil'])}")
    print(f"Regulatory actions: {len(result['actions']['regulatory'])}")
    print(f"Enforcement actions: {len(result['actions']['enforcement'])}")
    print(f"\nStrategy notes:")
    for s in result["strategy"]:
        print(f"  - {s}")
    print(f"\nGaps: {len(result['gaps'])}")

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, default=str)
        print(f"\nFull JSON written to {args.output}")

    return 0




# ============================================================================
# V04_AUTOCALL_APPLIED — Duty Engine auto-call integration
# ============================================================================

# BEEZLE_FN_UNUSED 2026-06-13: _infer_entity_from_theory (no static refs in pipeline/)
def _infer_entity_from_theory(theory_id: str, theory_map: dict) -> str | None:
    """Look up entity_type declared in theory_map.yaml for this theory."""
    theory_def = theory_map.get("theories", {}).get(theory_id, {})
    return theory_def.get("entity_type")



# ---- V04_FIX4_APPLIED ----
def _normalize_cite(raw: str) -> str:
    """Normalize any citation form to a compact lowercase key.

    Examples:
      "15 U.S.C. § 1692e"   -> "15usc1692e"
      "15-USC-1692e"        -> "15usc1692e"
      "15 USC 1692(e)"      -> "15usc1692e"
      "42-Pa-CS-8371"       -> "42pacs8371"
      "75 Pa.C.S. § 1799.3" -> "75pacs17993"
      "12-CFR-1024.41"      -> "12cfr102441"
    """
    if not raw:
        return ""
    s = str(raw).lower()
    # Strip punctuation / separators
    s = re.sub(r"[\s\-_\.\u00a7\u00b6§¶()\[\]{}/\\,;:]+", "", s)
    # Compact common abbreviations
    s = s.replace("u.s.c", "usc").replace("u.s.code", "usc")
    s = s.replace("p.s.", "ps").replace("pa.c.s", "pacs").replace("pacs.", "pacs")
    s = s.replace("c.f.r", "cfr")
    return s


def _extract_theory_cites(theory: dict) -> set:
    """Pull normalized citation tokens from a theory dict.

    Looks in (in order):
      - authorities (if user added it)
      - seed_nodes (the canonical source — list of Obsidian node IDs)
      - citation / primary_citation / primary_cite
    """
    cites = set()
    for key in ("authorities", "seed_nodes"):
        val = theory.get(key)
        if isinstance(val, list):
            for item in val:
                n = _normalize_cite(item)
                if n:
                    cites.add(n)
        elif isinstance(val, str):
            n = _normalize_cite(val)
            if n:
                cites.add(n)
    for key in ("citation", "primary_citation", "primary_cite"):
        v = theory.get(key)
        if v:
            n = _normalize_cite(v)
            if n:
                cites.add(n)
    return cites


def _extract_duty_cites(duty: dict) -> set:
    """Pull normalized citation tokens from a duty dict.

    Duty YAMLs store the citation in one of:
      - source            (primary in insurance_company.yaml etc.)
      - source_title
      - citation
      - id
    """
    cites = set()
    for key in ("source", "citation", "id"):
        v = duty.get(key)
        if v:
            n = _normalize_cite(v)
            if n:
                cites.add(n)
    # Also check source_title for inline cite forms
    st = duty.get("source_title") or ""
    # Extract things that look like legal cites from the title
    for m in re.finditer(r"\b\d{1,3}[\s\-\.]?(usc|pacs|cfr|ps|pa\.c\.s|p\.s)[\s\-\.§]*[\d\.\-]+", st.lower()):
        n = _normalize_cite(m.group(0))
        if n:
            cites.add(n)
    return cites


def _duty_matches_theory(duty: dict, theory: dict) -> bool:
    """A duty matches a theory if ANY of its normalized citations
    overlaps with ANY of the theory's normalized citations, OR if
    one is a prefix of the other (so "15usc1692" matches "15usc1692e")."""
    t_cites = _extract_theory_cites(theory)
    d_cites = _extract_duty_cites(duty)
    if not t_cites or not d_cites:
        return False
    # Exact overlap
    if t_cites & d_cites:
        return True
    # Prefix match (either direction) for section vs subsection
    for tc in t_cites:
        for dc in d_cites:
            if len(tc) >= 6 and len(dc) >= 6:
                if tc.startswith(dc) or dc.startswith(tc):
                    return True
    return False


# Back-compat alias (old name used elsewhere in v04)
# BEEZLE_FN_UNUSED 2026-06-13: _citation_keywords (no static refs in pipeline/)
def _citation_keywords(obj: dict) -> set:
    """Compatibility shim — route to the new extractors."""
    if not isinstance(obj, dict):
        return set()
    # Heuristic: duties have a "class" field, theories don't
    if "class" in obj or "source" in obj or "evidence_demanded" in obj:
        return _extract_duty_cites(obj)
    return _extract_theory_cites(obj)
# ---- /V04_FIX4_APPLIED ----



# ---- V04_FIX5_APPLIED ----
def auto_attach_duties(theories, theory_map, target_court="EDPA"):
    """Phase 3.5: bind duties to each theory by entity_type.

    For each theory with an entity_type:
      1. Load the duty map for that entity (yaml file in pipeline/duty_maps/)
      2. Find duties whose citations overlap with theory seed_nodes
         (via _duty_matches_theory)
      3. If no citation overlap, fall back to ALL duties in that entity map
         (baseline duties that apply to every claim against this entity type)
      4. Attach to theory['attached_duties'] and build theory_duty_map entry
    """
    import logging
    import yaml
    from pathlib import Path as _Path

    _log = logging.getLogger(__name__)
    theory_duty_map = []

    DUTY_MAPS_DIR = _Path(__file__).parent / "duty_maps"

    # Cache duty maps so we don't re-read the same file for multiple theories
    _duty_cache = {}
    def _load_duty_map(entity):
        if entity in _duty_cache:
            return _duty_cache[entity]
        f = DUTY_MAPS_DIR / f"{entity}.yaml"
        if not f.exists():
            _duty_cache[entity] = None
            return None
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            _duty_cache[entity] = data
            return data
        except Exception as e:
            _log.warning(f"Failed to load duty map {f}: {e}")
            _duty_cache[entity] = None
            return None

    for theory in theories:
        entity_type = theory.get("entity_type")

        # === COMMON_DUTIES_V1 ===
        # === MULTI_DUTY_LOAD_V1 ===
        # If entity_type is null, fall back to MULTIPLE common duty_maps
        # (common_defendant + procedural_rules + constitutional) and merge
        # all duties whose `applies_to_domains` overlaps the theory's domains.
        if not entity_type:
            theory_domains = set((theory.get("domains") or []))
            theory_domains_lc = {d.lower() for d in theory_domains}

            # Always pull from these three maps when entity_type is null
            FALLBACK_MAPS = ["common_defendant", "procedural_rules", "constitutional"]
            domain_matched = []
            seen_ids = set()
            sources_loaded = []

            for fname in FALLBACK_MAPS:
                fb_map = _load_duty_map(fname)
                if not fb_map:
                    continue
                fb_duties = fb_map.get("duties", []) or []
                pre = len(domain_matched)
                for d in fb_duties:
                    d_id = d.get("id")
                    if d_id and d_id in seen_ids:
                        continue
                    d_domains = set((d.get("applies_to_domains") or []))
                    d_domains_lc = {x.lower() for x in d_domains}
                    if theory_domains_lc & d_domains_lc:
                        domain_matched.append(d)
                        if d_id:
                            seen_ids.add(d_id)
                if len(domain_matched) > pre:
                    sources_loaded.append(fname)

            theory["attached_duties"] = domain_matched
            theory_duty_map.append({
                "theory_id": theory.get("id") or theory.get("theory_id"),
                "theory_name": theory.get("display_name") or theory.get("name"),
                "entity_type": "+".join(sources_loaded) or "common_defendant",
                "duty_count": len(domain_matched),
                "duty_citations": [
                    (d.get("source") or d.get("citation") or d.get("id"))
                    for d in domain_matched
                ],
                "match_mode": "multi_common_domain_match",
            })
            continue
        # === END MULTI_DUTY_LOAD_V1 ===
        # === END COMMON_DUTIES_V1 ===

        duty_map = _load_duty_map(entity_type)
        if not duty_map:
            continue

        all_duties = duty_map.get("duties", []) or []

        # Step 1: citation-based matching
        matched = []
        for d in all_duties:
            try:
                if _duty_matches_theory(d, theory):
                    matched.append(d)
            except Exception as e:
                _log.debug(f"matcher error on duty {d.get('id')}: {e}")

        # Step 2: fallback — if NO citation match, attach all baseline duties
        # (statutory + regulatory + fiduciary) for that entity
        fallback_used = False
        if not matched:
            fallback_used = True
            for d in all_duties:
                cls = (d.get("class") or "").lower()
                if cls in ("statutory", "regulatory", "fiduciary"):
                    matched.append(d)

        # Step 3: attach to theory + build map entry
        # === MULTI_DUTY_LOAD_V1 ===
        # ALSO pull procedural + constitutional duties for entity-typed theories
        # when the theory's domains include procedural or constitutional.
        _t_domains = {d.lower() for d in (theory.get("domains") or [])}
        for _xtra in ("procedural_rules", "constitutional"):
            _xmap = _load_duty_map(_xtra)
            if not _xmap:
                continue
            for _d in (_xmap.get("duties") or []):
                _dd = {x.lower() for x in (_d.get("applies_to_domains") or [])}
                _did = _d.get("id")
                if _t_domains & _dd and _did and _did not in {x.get("id") for x in matched}:
                    matched.append(_d)
        # === END MULTI_DUTY_LOAD_V1 ===
        theory["attached_duties"] = matched

        cites = []
        for d in matched:
            src = d.get("source") or d.get("citation") or d.get("id")
            if src and src not in cites:
                cites.append(src)

        theory_duty_map.append({
            "theory_id": theory.get("id") or theory.get("theory_id"),
            "theory_name": theory.get("display_name") or theory.get("name"),
            "entity_type": entity_type,
            "duty_count": len(matched),
            "duty_citations": cites,
            "match_mode": "fallback_baseline" if fallback_used else "citation_match",
        })

    return theories, theory_duty_map
# ---- /V04_FIX5_APPLIED ----

# ============================================================================
# /V04_AUTOCALL_APPLIED
# ============================================================================


if __name__ == "__main__":
    raise SystemExit(main())


# Unified alias
run_scenario = scenario_decompose
