"""
pipeline/intake_engine.py — Block 11: Structured Case Intake.

BLOCK_11_INTAKE_ENGINE

Converts free-text user descriptions into structured case data.
Generates intake questions when facts are incomplete.
Accepts user-specified damages without modification.
Builds damages justification layers (compensatory, statutory, punitive, deterrence).

NO LLM for legal classification. LLM only for fact extraction from free text.
Legal classification uses scenario_engine deterministically.

Flow:
  free_text → extract_facts() → identify_gaps() → ask_questions()
  answers + docs → build_case() → scenario_decompose() → run_case()
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import re
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field, asdict

from core.logger import get_logger

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

log = get_logger("intake")

CASES_INDEX = (_BEEZLE_ROOT / "cases_index")
CASES_INDEX.mkdir(parents=True, exist_ok=True)


# ======================================================================
# FACT EXTRACTION (deterministic keyword matching)
# ======================================================================

FACT_PATTERNS = {
    "insurance": [
        (r"(?:auto|car|vehicle|home|renters?|life|health)\s*(?:insurance|policy)", "insurance_type"),
        (r"\binsurance\b", "insurance_type"),
        (r"\binsurer\b", "insurance_type"),
        (r"\bpolicy\s*holder\b", "insurance_type"),
        (r"premium|monthly\s*payment|billing|charged|deducted|withdrew", "premium_issue"),
        (r"cancel|cancelled|cancellation|terminated", "cancellation"),
        (r"refund|reimburs|money\s*back", "refund"),
        (r"collection|sent\s*to\s*collect|debt\s*collect", "collections"),
        (r"sold\s*(?:my|the)\s*(?:car|vehicle|house|home|property)", "asset_disposed"),
        (r"no\s*(?:insurable\s*)?interest", "no_insurable_interest"),
        (r"bad\s*faith|refused\s*to\s*pay|denied\s*claim|ignored", "bad_faith"),
        (r"underwriter|adjuster|claims?\s*department", "insurer_contact"),
        (r"department\s*of\s*insurance|DOI|insurance\s*commission|regulator", "regulatory_complaint"),
    ],
    "credit": [
        (r"credit\s*report|credit\s*bureau|equifax|experian|transunion", "credit_reporting"),
        (r"inaccurate|incorrect|wrong\s*information|error", "inaccuracy"),
        (r"dispute|disputed|challenged", "dispute_filed"),
        (r"furnisher|reporting\s*entity", "furnisher"),
    ],
    "debt_collection": [
        ("collection_agency", "Was a collection agency involved? Which one?"),
        ("debt_collector_entity", "Who is the debt collector?"),
        ("validation_notice", "Did you receive a validation notice within 5 days?"),
        ("damages_amount", "What amount are you seeking in damages?"),
    ],
    "contract": [
        (r"contract|agreement|signed|terms", "contract_exists"),
        (r"breach|violated|failed\s*to\s*perform|didn.t\s*do", "breach"),
        (r"paid|payment|consideration", "consideration"),
    ],
    "debt_collection": [
        (r"\bcollection\s*agency\b", "collection_agency"),
        (r"\bdebt\s*collector\b", "debt_collector_entity"),
        (r"\bsent\s*to\s*collection", "sent_to_collections"),
        (r"\bcollection\s*letter\b", "collection_letter"),
        (r"\bcollection\s*call", "collection_call"),
        (r"\bvalidation\s*notice", "validation_notice"),
        (r"\b30\s*day", "thirty_day_period"),
        (r"\bceased\s*collection", "ceased_collection"),
        (r"\bthird\s*party\s*collect", "third_party_collector"),
    ],
    "general": [
        (r"\$[\d,]+(?:\.\d{2})?|\d+\s*(?:thousand|million|billion)", "damages_amount"),
        (r"(\d+)\s*(?:year|month|week|day)s?\s*(?:ago)?", "time_period"),
        (r"sue|lawsuit|complaint|file|court", "litigation_intent"),
        (r"unjust\s*enrichment", "unjust_enrichment"),
        (r"conversion", "conversion"),
        (r"fraud|deceptive|misleading|unfair", "fraud_indicators"),
        (r"prima\s*facie", "prima_facie"),
        (r"\badmit|\badmission|\backnowledge|confirmed|conceded", "admission"),
        (r"\bundisputed|\buncontested|\bunrebutted", "unrebutted_evidence"),
        (r"\bproof|\bevidence\s*(?:shows?|demonstrates?|establishes?)", "evidence_established"),
    ],
}


# ─── Phase E: LLM + theory_map matching (Closed-Loop Phase 8) ────────────────
# Soft-fail design: each helper returns empty result on any error.
# extract_facts() still works with regex-only output if llama-server is down.

_PHASE_E_LLAMA_URL = "http://localhost:8000/v1/chat/completions"
_PHASE_E_LLAMA_TIMEOUT = 30
_PHASE_E_THEORY_MAP_PATH = (_eroot() / "pipeline/theory_map.yaml")

_PHASE_E_EXTRACTION_SYSTEM = (
    "You are a legal fact extractor. Extract ONLY verifiable facts from the text. "
    "Return STRICT JSON with these keys (use empty list/string if unknown):\n"
    "  - plaintiffs: list of strings\n"
    "  - defendants: list of strings\n"
    "  - court: string (e.g. 'EDPA', 'PA_CP_PHILA', '3D_CIR')\n"
    "  - jurisdiction: string ('federal' | 'state_pa' | 'state_nj' | 'unknown')\n"
    "  - statutes: list of strings (cite verbatim, e.g. '42 Pa.C.S. § 8371')\n"
    "  - dates: list of {date: string, event: string} objects\n"
    "  - amounts: list of {amount: string, description: string} objects\n"
    "  - claim_numbers: list of strings\n"
    "  - case_numbers: list of strings\n"
    "  - evidence_refs: list of strings (docket numbers, exhibit letters, document names)\n"
    "  - violation_keywords: list of strings (e.g. 'bad faith', 'breach of contract', "
    "'FCRA willful', 'FDCPA 1692e', 'RESPA QWR')\n"
    "  - document_type: string (one word: complaint, motion, order, letter, etc.)\n"
    "DO NOT interpret, conclude, or add legal analysis. Facts only. "
    "Output JSON ONLY, no preamble, no markdown fences."
)


def _phase_e_llm_extract(text: str) -> dict:
    """Call llama-server for structured fact extraction. Returns dict or {} on failure."""
    if not text or len(text.strip()) < 10:
        return {}
    try:
        import json as _json
        import urllib.request as _req
        # Truncate to 12k chars to stay well under 131k context
        snippet = text[:12000]
        body = _json.dumps({
            "model": "Qwen3.6-35B-A3B-MTP-TQ3_4S.gguf",
            "messages": [
                {"role": "system", "content": _PHASE_E_EXTRACTION_SYSTEM},
                {"role": "user", "content": f"Extract facts from this legal text:\n\n{snippet}"},
            ],
            "max_tokens": 1200,
            "temperature": 0.05,
        }).encode("utf-8")
        req = _req.Request(
            _PHASE_E_LLAMA_URL,
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with _req.urlopen(req, timeout=_PHASE_E_LLAMA_TIMEOUT) as resp:
            payload = _json.loads(resp.read().decode("utf-8"))
        content = (payload.get("choices") or [{}])[0].get("message", {}).get("content", "")
        if not content:
            return {}
        # Strip markdown fences if present
        c = content.strip()
        if c.startswith("```"):
            c = c.split("\n", 1)[1] if "\n" in c else c[3:]
            if c.endswith("```"):
                c = c[:-3]
            if c.startswith("json\n"):
                c = c[5:]
        c = c.strip()
        # Find first '{' and last '}' to bracket valid JSON
        i, j = c.find("{"), c.rfind("}")
        if i < 0 or j < i:
            return {}
        parsed = _json.loads(c[i:j+1])
        return parsed if isinstance(parsed, dict) else {}
    except Exception as e:
        log.debug(f"phase_e llm_extract failed: {e}")
        return {}


def _phase_e_load_theory_triggers() -> dict:
    """Load theory_map.yaml triggers. Returns {theory_id: {display_name, triggers: [lowercased]}}."""
    try:
        import yaml as _yaml
        if not _PHASE_E_THEORY_MAP_PATH.exists():
            return {}
        tm = _yaml.safe_load(_PHASE_E_THEORY_MAP_PATH.read_text(encoding="utf-8")) or {}
        theories = tm.get("theories", {}) or {}
        out = {}
        for tid, tdef in theories.items():
            if not isinstance(tdef, dict):
                continue
            trigs = tdef.get("triggers") or []
            if not isinstance(trigs, list):
                continue
            # lowercase, dedupe, keep only string triggers
            trig_norm = sorted({str(t).lower().strip() for t in trigs if isinstance(t, str) and t.strip()})
            if trig_norm:
                out[tid] = {
                    "display_name": tdef.get("display_name", tid),
                    "triggers": trig_norm,
                }
        return out
    except Exception as e:
        log.debug(f"phase_e theory_map load failed: {e}")
        return {}


def _phase_e_theory_match(combined_text: str, llm_facts: dict) -> list:
    """Match combined_text + LLM violation_keywords against theory_map triggers.
    Returns list of {theory_id, display_name, score, matched_triggers}.
    """
    theories = _phase_e_load_theory_triggers()
    if not theories:
        return []
    haystack = (combined_text or "").lower()
    # Add LLM violation_keywords + statutes to haystack for trigger matching
    if isinstance(llm_facts, dict):
        for k in ("violation_keywords", "statutes"):
            v = llm_facts.get(k)
            if isinstance(v, list):
                haystack += " " + " ".join(str(x).lower() for x in v if x)
    results = []
    for tid, tdef in theories.items():
        matched = [trig for trig in tdef["triggers"] if trig in haystack]
        if matched:
            results.append({
                "theory_id": tid,
                "display_name": tdef["display_name"],
                "score": len(matched),
                "matched_triggers": matched[:10],  # cap for readability
            })
    results.sort(key=lambda r: r["score"], reverse=True)
    return results


def _phase_e_suggest_case_type(theory_matches: list) -> tuple:
    """Map top theory matches → suggested case_type via CASE_TYPE_CLAIMS inverse.
    Returns (case_type_str, top_claim_ids_list).
    """
    if not theory_matches:
        return ("", [])
    try:
        from pipeline.case_loader import CASE_TYPE_CLAIMS
    except Exception:
        return ("", [])
    # Score each case_type by how many of its claims appear in top theory matches
    top_theory_ids = {m["theory_id"] for m in theory_matches[:8]}
    ct_scores = {}
    for ct, claims in CASE_TYPE_CLAIMS.items():
        overlap = [c for c in claims if c in top_theory_ids]
        if overlap:
            # Weighted by theory rank
            score = sum(
                (theory_matches[i]["score"] if i < len(theory_matches) else 0)
                for i, m in enumerate(theory_matches[:8])
                if m["theory_id"] in claims
            )
            ct_scores[ct] = (score, overlap)
    if not ct_scores:
        return ("", [m["theory_id"] for m in theory_matches[:3]])
    best_ct = max(ct_scores.items(), key=lambda kv: kv[1][0])
    return (best_ct[0], best_ct[1][1])


# ─── end Phase E helpers ─────────────────────────────────────────────────────



def extract_facts(text: str, use_llm: bool = True) -> dict:
    """Extract structured facts from free-text description.

    Phase E upgrade: now runs regex + (optional) LLM + theory_map matching.
    Backward-compatible: still returns {detected_facts, domains, raw_text, fact_count}.
    NEW keys: {llm_facts, theory_matches, suggested_case_type, suggested_claim_ids, extraction_mode}.

    Args:
        text: free-text legal description
        use_llm: if True, also calls llama-server :8000 for structured extraction.
                 Soft-fails to regex-only on any error.

    Returns dict with all keys above. Caller can ignore new keys safely.
    """
    text_lower = text.lower()
    detected = []
    domains = set()

    # ───── E1: existing regex pass (unchanged behavior) ─────
    for category, patterns in FACT_PATTERNS.items():
        for pattern, fact_type in patterns:
            for m in re.finditer(pattern, text_lower):
                detected.append({
                    "category": category,
                    "fact_type": fact_type,
                    "matched_text": m.group(0),
                    "span": [m.start(), m.end()],
                })
                domains.add(category)

    # Extract dollar amounts
    amounts = re.findall(r"\$?([\d,]+(?:\.\d{2})?)\s*(?:million|M)", text, re.I)
    if amounts:
        for a in amounts:
            val = float(a.replace(",", "")) * 1_000_000
            detected.append({"category": "damages", "fact_type": "damages_requested", "matched_text": f"${val:,.0f}", "span": []})

    amounts_raw = re.findall(r"\$([\d,]+(?:\.\d{2})?)", text)
    if amounts_raw and not amounts:
        for a in amounts_raw:
            detected.append({"category": "damages", "fact_type": "damages_requested", "matched_text": f"${a}", "span": []})

    # ───── E2: LLM structured extraction (soft-fail) ─────
    llm_facts = {}
    extraction_mode = "regex_only"
    if use_llm:
        llm_facts = _phase_e_llm_extract(text) or {}
        if llm_facts:
            extraction_mode = "regex+llm"

    # ───── E3: theory_map trigger matching ─────
    theory_matches = _phase_e_theory_match(text, llm_facts)
    if theory_matches:
        extraction_mode += "+theory"

    # ───── E4: case_type suggestion ─────
    suggested_case_type, suggested_claim_ids = _phase_e_suggest_case_type(theory_matches)

    return {
        # backward-compat keys (chain_runner.py:1235 reads these)
        "detected_facts": detected,
        "domains": list(domains),
        "raw_text": text,
        "fact_count": len(detected),
        # Phase E new keys
        "llm_facts": llm_facts,
        "theory_matches": theory_matches,
        "suggested_case_type": suggested_case_type,
        "suggested_claim_ids": suggested_claim_ids,
        "extraction_mode": extraction_mode,
    }


# ======================================================================
# GAP ANALYSIS + QUESTION GENERATION
# ======================================================================

REQUIRED_FACTS = {
    "insurance": [
        ("insurance_type", "What type of insurance policy is involved? (auto, home, health, etc.)"),
        ("premium_issue", "Were premiums charged or deducted? For how long?"),
        ("asset_disposed", "When did you sell/dispose of the insured asset?"),
        ("cancellation", "Did you or the insurer cancel the policy? When?"),
        ("damages_amount", "What amount are you seeking in damages?"),
    ],
    "credit": [
        ("inaccuracy", "What information is inaccurate on your credit report?"),
        ("dispute_filed", "Did you file a formal dispute? When and how?"),
        ("damages_amount", "What amount are you seeking in damages?"),
    ],
    "contract": [
        ("contract_exists", "Is there a written contract or agreement?"),
        ("breach", "What did the other party fail to do?"),
        ("damages_amount", "What amount are you seeking in damages?"),
    ],
}

UNIVERSAL_QUESTIONS = [
    ("defendants", "Who are you suing? (company names, individuals)"),
    ("jurisdiction", "What state did this occur in?"),
    ("timeline", "When did this start and when did you discover the problem?"),
    ("evidence", "What evidence do you have? (documents, emails, bank statements, letters)"),
    ("regulatory", "Have you filed any complaints with regulators? (DOI, CFPB, FTC, etc.)"),
]


def identify_gaps(extracted: dict) -> list[dict]:
    """Identify missing facts and generate follow-up questions."""
    detected_types = {f["fact_type"] for f in extracted["detected_facts"]}
    domains = extracted["domains"]
    gaps = []

    # Domain-specific gaps
    for domain in domains:
        required = REQUIRED_FACTS.get(domain, [])
        for fact_type, question in required:
            if fact_type not in detected_types:
                gaps.append({"fact_type": fact_type, "question": question, "domain": domain})

    # Universal gaps
    for fact_type, question in UNIVERSAL_QUESTIONS:
        if fact_type not in detected_types:
            gaps.append({"fact_type": fact_type, "question": question, "domain": "general"})

    return gaps


# ======================================================================
# DAMAGES JUSTIFICATION (never limits — only justifies)
# ======================================================================

def build_damages_justification(facts: dict, requested_amount: float = 0) -> dict:
    """Build multi-layer damages justification.

    RULE: System NEVER reduces user's requested amount.
    System ALWAYS builds justification layers to SUPPORT the demand.
    """
    detected_types = {f["fact_type"] for f in facts.get("detected_facts", [])}

    layers = {
        "requested": requested_amount,
        "compensatory": {"basis": [], "description": ""},
        "statutory": {"basis": [], "description": ""},
        "punitive": {"basis": [], "possible": False, "description": ""},
        "deterrence": {"basis": [], "description": ""},
    }

    # Compensatory
    if "premium_issue" in detected_types:
        layers["compensatory"]["basis"].append("All funds wrongfully collected after insurable interest ceased")
    if "collections" in detected_types:
        layers["compensatory"]["basis"].append("Credit damage from improper collection activity")
    if "inaccuracy" in detected_types:
        layers["compensatory"]["basis"].append("Harm from inaccurate credit reporting")
    layers["compensatory"]["description"] = "Direct monetary harm + consequential damages"

    # Statutory
    if "insurance" in facts.get("domains", []):
        layers["statutory"]["basis"].append("42 Pa.C.S. 8371 — bad faith insurance (interest + punitive + fees)")
        layers["statutory"]["basis"].append("73 P.S. 201-9.2 — UTPCPL (treble damages possible)")
    if "credit" in facts.get("domains", []):
        layers["statutory"]["basis"].append("15 U.S.C. 1681n — FCRA willful ($100-$1000 per violation + punitive)")
        layers["statutory"]["basis"].append("15 U.S.C. 1692k — FDCPA ($1000 + actual damages)")

    # Punitive
    punitive_triggers = {"bad_faith", "fraud_indicators", "collections", "no_insurable_interest"}
    if punitive_triggers & detected_types:
        layers["punitive"]["possible"] = True
        layers["punitive"]["basis"].append("Pattern of willful, reckless conduct over extended period")
        layers["punitive"]["basis"].append("Continued wrongful conduct even after notice/complaint")
        layers["punitive"]["description"] = (
            "Defendant's conduct was willful, reckless, and part of a pattern "
            "requiring punitive damages sufficient to deter future misconduct"
        )

    # Debt collection statutory
    if "debt_collection" in facts.get("domains", []):
        layers["statutory"]["basis"].append("15 U.S.C. 1692k -- FDCPA ($1000 statutory + actual damages)")
        layers["statutory"]["basis"].append("15 U.S.C. 1692e -- false/misleading representations")
        layers["statutory"]["basis"].append("15 U.S.C. 1692f -- unfair practices")

    # Unjust enrichment (common law -- always available)
    if any(f.get("fact_type") == "unjust_enrichment" for f in facts.get("detected_facts", [])):
        layers["compensatory"]["basis"].append("Unjust enrichment -- defendant retained benefit at plaintiff expense")

    # Deterrence
    layers["deterrence"]["basis"].append("Corporate defendant's conduct affects many consumers")
    layers["deterrence"]["basis"].append("Fine amounts must be proportional to corporate revenue to deter")
    layers["deterrence"]["description"] = "Damages must be sufficient to change corporate behavior"

    return layers


# ======================================================================
# CASE BUILDER (creates registry entry from intake)
# ======================================================================

def build_new_case(
    intake_facts: dict,
    defendants: list[str],
    jurisdiction: str = "PA",
    court: str = "PA_CP_PHILA",
    damages_requested: float = 0,
    plaintiff: str = "FILER, FILER-FARDY BABAH",
) -> dict:
    """Create a new case entry from intake data.

    Returns case dict ready for case_registry.json insertion.
    """
    # Generate case ID
    ts = datetime.utcnow().strftime("%Y%m%d%H%M")
    case_num = f"NEW-{ts}"
    canonical_id = f"pending:{case_num}"

    # Determine case type from domains (priority order)
    domains = intake_facts.get("domains", [])
    if "insurance" in domains:
        case_type = "bad_faith"
    elif "debt_collection" in domains:
        case_type = "fdcpa"
    elif "credit" in domains:
        case_type = "fraud_fcra"
    elif "contract" in domains:
        case_type = "contracts_other"
    else:
        case_type = "general"

    # Build engine_facts from detected facts
    engine_facts = []
    for f in intake_facts.get("detected_facts", []):
        engine_facts.append(f["matched_text"])

    # Build factual application from raw text
    factual_app = {
        "_default": intake_facts.get("raw_text", ""),
    }

    # Damages justification
    damages = build_damages_justification(intake_facts, damages_requested)

    entry = {
        "canonical_id": canonical_id,
        "case_court": court,
        "caption": f"FILER VS {' '.join(d.upper() for d in defendants[:3])} ETAL" if len(defendants) > 1 else f"FILER VS {defendants[0].upper()}",
        "filed": datetime.utcnow().strftime("%Y-%m-%d"),
        "status": "pre_filing",
        "case_type": case_type,
        "parties": ["_filer_"] + [d.lower().replace(" ", "_") for d in defendants],
        "engine_facts": engine_facts,
        "factual_application": factual_app,
        "damages_requested": damages_requested,
        "damages_justification": damages,
        "notes": [
            f"Created via intake engine {datetime.utcnow().isoformat()}Z",
            f"Domains: {', '.join(domains)}",
            f"Facts detected: {intake_facts.get('fact_count', 0)}",
        ],
        "related_cases": [],
    }

    return entry


# BEEZLE_FN_UNUSED 2026-06-13: register_new_case (no static refs in pipeline/)
def register_new_case(entry: dict) -> str:
    """Add a new case to case_registry.json. Returns canonical_id."""
    reg_path = CASES_INDEX / "case_registry.json"
    reg = json.loads(reg_path.read_text(encoding="utf-8"))
    cid = entry["canonical_id"]
    reg[cid] = entry
    reg_path.write_text(json.dumps(reg, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info(f"registered new case: {cid}")
    return cid


# ======================================================================
# FULL INTAKE FLOW
# ======================================================================

# BEEZLE_FN_UNUSED 2026-06-13: run_intake (no static refs in pipeline/)
def run_intake(
    description: str,
    defendants: list[str] = None,
    damages_requested: float = 0,
    jurisdiction: str = "PA",
    court: str = "PA_CP_PHILA",
) -> dict:
    """Full intake pipeline.

    Returns {facts, gaps, damages_justification, case_entry, needs_answers}
    """
    # Extract facts
    facts = extract_facts(description)
    log.info(f"intake: extracted {facts['fact_count']} facts from {len(description)} chars")

    # Identify gaps
    gaps = identify_gaps(facts)

    # Build damages justification (never limits)
    damages = build_damages_justification(facts, damages_requested)

    result = {
        "facts": facts,
        "gaps": gaps,
        "needs_answers": len(gaps) > 0,
        "damages_justification": damages,
    }

    # If we have enough to build a case
    if defendants:
        entry = build_new_case(facts, defendants, jurisdiction, court, damages_requested)
        result["case_entry"] = entry
        result["ready_to_register"] = len(gaps) < 3  # allow filing with minor gaps

    return result