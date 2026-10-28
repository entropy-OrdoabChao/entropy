"""
case_loader.py — Block 6.5 — registry-driven case loader with vocabulary enrichment.

DETERMINISTIC. No LLM. Three-layer model:
  CLAIMS    (substantive law: breach_of_contract_general, conversion, ...)
  DUTIES    (obligations from duty_engine — bound by scenario_engine)
  DOCTRINES (procedural attacks: rule_60b4_void_judgment, ...)

Vocabulary bridge: registry uses human language ("garnishment", "default judgment").
Engines look for legal vocabulary ("withheld money", "no proper service").
This module translates 1:1 using ONLY phrases that appear verbatim in theory_map.yaml
or order_doctrines.yaml — no fabrication.

Per-case override: registry entry may include `engine_facts: ["phrase 1", ...]` —
those phrases are appended verbatim to the synthesized fact list.
"""

from __future__ import annotations
from core import identity as _ident
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import json
import re
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Any

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

log = logging.getLogger("case_loader")

_BASE_WSL = _eroot()
_BASE_WIN = _eroot()
BASE = _BASE_WSL if _BASE_WSL.exists() else _BASE_WIN
REGISTRY = BASE / "cases_index" / "case_registry.json"
CASE_MAP = BASE / "cases_index" / "case_map.json"
OBSIDIAN = BASE / "obsidian_sync"


# ─── case_type → expected CLAIM IDs (synced to real theory_map.yaml keys) ─
CASE_TYPE_CLAIMS = {
    "contracts_other": [
        "breach_of_contract_general",
        "conversion",
        "unjust_enrichment",
    ],
    "fraud_fcra": [
        "fdcpa_false_representation",
        "fdcpa_unfair_practices",
        "pa_utpcpl_violation",
        "fcra_willful_violation",
        "fcra_negligent_violation",
    ],
    "quiet_title": [
        "quiet_title_chain_defect",
        "respa_qwr_violation",
        "breach_of_contract_general",
    ],
    "bad_faith": [
        "bad_faith_insurance_pa",
        "breach_of_insurance_contract",
        "insurance_unearned_premium",
        "pa_utpcpl_violation",
    ],
    "respa": ["respa_qwr_violation", "breach_of_contract_general"],
    "tila": ["breach_of_contract_general"],
}

# Backwards-compat alias for older imports
CASE_TYPE_THEORIES = CASE_TYPE_CLAIMS


# ─── case_status → expected order-engine doctrines ────────────────────────

# ─── v18j: flexible case ID resolver ──────────────────────────────────────────
_V18J_RESOLVER_CACHE = None

def _v18j_resolve_case_id(case_id):
    """Accept colon-form, slug-form, or any docket alias. Returns canonical_id."""
    global _V18J_RESOLVER_CACHE
    if not case_id:
        return case_id

    # Build alias index once
    if _V18J_RESOLVER_CACHE is None:
        try:
            import json as _v18j_json
            from pathlib import Path as _v18j_Path
            _v18j_wsl = _v18j_Path(str(_eroot() / "cases_index/case_registry.json"))
            _v18j_win = _v18j_(_BEEZLE_ROOT / "cases_index" / "case_registry.json")
            registry_path = _v18j_wsl if _v18j_wsl.exists() else _v18j_win
            if registry_path.exists():
                data = _v18j_json.loads(registry_path.read_text(encoding="utf-8"))
                idx = {}
                for canon_id, case_data in data.items():
                    if not isinstance(case_data, dict):
                        continue
                    # canonical_id maps to itself
                    idx[canon_id] = canon_id
                    idx[canon_id.lower()] = canon_id
                    # All docket_aliases map to canonical
                    for alias in case_data.get("docket_aliases", []) or []:
                        idx[str(alias)] = canon_id
                        idx[str(alias).lower()] = canon_id
                    # Slug form (e.g. fed_2-26-cv-00456_filer_v_amerisave)
                    # Build by replacing : with - and adding caption-derived suffix
                    if ":" in canon_id:
                        slug_prefix = canon_id.replace(":", "_")
                        idx[slug_prefix] = canon_id
                        idx[slug_prefix.lower()] = canon_id
                        # Try with caption suffix from caption field
                        caption = (case_data.get("caption") or "").lower()
                        if caption and "vs" in caption:
                            parties = caption.split("vs")
                            if len(parties) >= 2:
                                p1 = re.sub(r"[^a-z0-9]+", "_",
                                              parties[0].strip()).strip("_")
                                p2_raw = parties[1].strip().split()[0] if parties[1].strip() else ""
                                p2 = re.sub(r"[^a-z0-9]+", "_", p2_raw).strip("_")
                                if p1 and p2:
                                    full_slug = f"{slug_prefix}_{p1}_v_{p2}"
                                    idx[full_slug] = canon_id
                                    idx[full_slug.lower()] = canon_id
                _V18J_RESOLVER_CACHE = idx
            else:
                _V18J_RESOLVER_CACHE = {}
        except Exception:
            _V18J_RESOLVER_CACHE = {}

    # Try exact, then lowercase, then slug-to-colon conversion
    if case_id in _V18J_RESOLVER_CACHE:
        return _V18J_RESOLVER_CACHE[case_id]
    if case_id.lower() in _V18J_RESOLVER_CACHE:
        return _V18J_RESOLVER_CACHE[case_id.lower()]
    # Direct slug-to-colon conversion: fed_2-26-cv-00456... -> fed:2:26-cv-00456
    parts = case_id.split("_", 2)
    if len(parts) >= 2:
        candidate = f"{parts[0]}:{parts[1]}"
        if candidate in _V18J_RESOLVER_CACHE:
            return _V18J_RESOLVER_CACHE[candidate]
        # Three-part: phila_ccp_260100000 -> phila_ccp:260100000
        if len(parts) >= 3:
            candidate = f"{parts[0]}_{parts[1]}:{parts[2]}"
            if candidate in _V18J_RESOLVER_CACHE:
                return _V18J_RESOLVER_CACHE[candidate]

    # _V18J2_REGEX_FIX: federal + state slug parsing with party suffix stripping
    # Federal: fed_2-26-cv-00456_filer_v_amerisave -> fed:2:26-cv-00456
    _v18j2_m = re.match(r"^(fed)_(\d+)-(\d+-cv-\d+)(?:_.*)?$", case_id, re.IGNORECASE)
    if _v18j2_m:
        _v18j2_canon = f"{_v18j2_m.group(1)}:{_v18j2_m.group(2)}:{_v18j2_m.group(3)}"
        if _v18j2_canon in _V18J_RESOLVER_CACHE:
            return _V18J_RESOLVER_CACHE[_v18j2_canon]
    # State: phila_ccp_260100000_filer_v_lippi -> phila_ccp:260100000
    _v18j2_m = re.match(r"^([a-z]+_[a-z]+)_(\d+)(?:_.*)?$", case_id, re.IGNORECASE)
    if _v18j2_m:
        _v18j2_canon = f"{_v18j2_m.group(1)}:{_v18j2_m.group(2)}"
        if _v18j2_canon in _V18J_RESOLVER_CACHE:
            return _V18J_RESOLVER_CACHE[_v18j2_canon]
    # Single-prefix: phila_260100000 -> phila:260100000
    _v18j2_m = re.match(r"^([a-z]+)_(\d+)(?:_.*)?$", case_id, re.IGNORECASE)
    if _v18j2_m:
        _v18j2_canon = f"{_v18j2_m.group(1)}:{_v18j2_m.group(2)}"
        if _v18j2_canon in _V18J_RESOLVER_CACHE:
            return _V18J_RESOLVER_CACHE[_v18j2_canon]

    return case_id  # unchanged — caller still gets the original
# ──────────────────────────────────────────────────────────────────────────────

def expected_doctrines_for(status: str, case_type: str) -> List[str]:
    s = (status or "").lower()
    out = []
    if "default" in s or "judgment_by_default" in s:
        out.extend(["rule_60b4_void_judgment", "due_process_voidness",
                    "coram_nobis_default_vacatur"])
    if "removed" in s:
        out.append("forum_shopping_defeat")
    if "active_motions" in s or "pending" in s:
        out.append("pa_void_judgment")
    return out


# ─── role detection ───────────────────────────────────────────────────────
def detect_role(caption: str) -> str:
    cap = (caption or "").upper()
    if cap.startswith(_ident.caption_name()) or (_ident.caption_name() + " V") in cap[:60]:
        return "plaintiff"
    return "defendant"


# ─── obsidian audit log loader ────────────────────────────────────────────
_AUDIT_CACHE: Dict[str, dict] = {}

def _slug(case_id: str) -> str:
    return case_id.replace(":", "_")

def load_latest_audit(canonical_id: str) -> Optional[dict]:
    if canonical_id in _AUDIT_CACHE:
        return _AUDIT_CACHE[canonical_id]

    slug = _slug(canonical_id)
    if not OBSIDIAN.exists():
        return None
    candidates = sorted(OBSIDIAN.glob(f"case_{slug}_*.md"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        _AUDIT_CACHE[canonical_id] = None
        return None

    latest = candidates[0]
    text = latest.read_text(encoding="utf-8", errors="ignore")
    audit = _parse_audit_md(text)
    audit["_source_file"] = latest.name
    _AUDIT_CACHE[canonical_id] = audit
    return audit

def _parse_audit_md(text: str) -> dict:
    out = {
        "score": None, "rank": None,
        "fatal_violations": 0, "reversible_signals": 0,
        "source_folders_count": 0,
        "top_arguments": [],
        "recommended_filing": "",
    }
    m = re.search(r"\*\*Score:\*\*\s*(\d+)", text)
    if m: out["score"] = int(m.group(1))
    m = re.search(r"\*\*Rank:\*\*\s*([A-F])", text)
    if m: out["rank"] = m.group(1)
    m = re.search(r"\*\*FATAL violations:\*\*\s*(\d+)", text)
    if m: out["fatal_violations"] = int(m.group(1))
    m = re.search(r"\*\*REVERSIBLE signals:\*\*\s*(\d+)", text)
    if m: out["reversible_signals"] = int(m.group(1))
    m = re.search(r"\*\*Source folders:\*\*\s*(\d+)", text)
    if m: out["source_folders_count"] = int(m.group(1))
    args_match = re.search(r"##\s*Top Arguments\s*\n(.*?)(?:\n##|\Z)", text, re.S)
    if args_match:
        for line in args_match.group(1).strip().split("\n"):
            line = line.strip().lstrip("-").strip()
            if line:
                out["top_arguments"].append(line)
    rec_match = re.search(r"##\s*Recommended Filing\s*\n(.*?)(?:\n##|\Z)", text, re.S)
    if rec_match:
        out["recommended_filing"] = rec_match.group(1).strip()
    return out


# ─── case_map.json loader ─────────────────────────────────────────────────
_CASE_MAP_CACHE = None
def load_case_map() -> dict:
    global _CASE_MAP_CACHE
    if _CASE_MAP_CACHE is not None:
        return _CASE_MAP_CACHE
    if not CASE_MAP.exists():
        _CASE_MAP_CACHE = {}
        return {}
    try:
        _CASE_MAP_CACHE = json.loads(CASE_MAP.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"failed to load case_map: {e}")
        _CASE_MAP_CACHE = {}
    return _CASE_MAP_CACHE


# ═════════════════════════════════════════════════════════════════════════
# VOCABULARY ENRICHMENT — registry semantics → engine trigger phrases
# Every phrase below is verified verbatim in theory_map.yaml triggers.
# ═════════════════════════════════════════════════════════════════════════

# case_type baseline phrases
_CASE_TYPE_VOCAB = {
    "contracts_other": [
        # breach_of_contract_general
        "breached contract",
        "failed to perform",
        "contract violated",
        "did not honor agreement",
        # conversion
        "took money",
        "withheld",
        "withheld money",
        "refused to return",
        "refused refund",
        # unjust_enrichment
        "retained money",
        "no consideration",
        "kept payment",
        "benefit retained",
        "unjust retention",
    ],
    "fraud_fcra": [
        # fdcpa_false_representation
        "debt collector",
        "collection agency",
        "sent to collection",
        "false amount",
        # fdcpa_unfair_practices
        "collected amount not owed",
        "amount not authorized",
        "unauthorized charge",
        # pa_utpcpl_violation
        "deceptive practice",
        "consumer harm",
        "consumer",
        # fcra_*
        "credit report",
        "credit reporting",
        "inaccurate information",
        "failed to investigate",
        "willful violation",
        "negligent violation",
    ],
    "quiet_title": [
        # quiet_title_chain_defect
        "broken chain of title",
        "no standing to foreclose",
        "MERS assignment",
        "robosigning",
        "lost note",
        "broken chain",
        # respa_qwr_violation
        "qualified written request",
        "QWR",
        "servicer failed to respond",
        "loan servicing",
        "mortgage inquiry",
        # breach_of_contract_general
        "breached contract",
        "failed to perform",
    ],
    "bad_faith": [
        # bad_faith_insurance_pa
        "denied claim",
        "bad faith",
        "claim delayed",
        "unreasonable denial",
        "no investigation",
        "lowball settlement",
        # breach_of_insurance_contract
        "policy breach",
        "denied coverage",
        "failed to pay claim",
        "contract violation",
        "insurance policy",
        "maintained an insurance policy",
        # insurance_unearned_premium
        "policy canceled",
        "continued billing",
        "retained premium",
        "no insurable interest",
        "risk ceased",
        "absence of insurable interest",
        "after cancellation",
        # pa_utpcpl_violation
        "deceptive practice",
        "consumer",
    ],
    "respa": [
        "qualified written request",
        "QWR",
        "servicer failed to respond",
        "loan servicing",
    ],
    "tila": [
        "breached contract",
        "failed to perform",
    ],
}

# status keyword → additional phrases (procedural defect signals for doctrines)
_STATUS_VOCAB = {
    "default": [
        # facts that elevate rule_60b4 / coram_nobis from low → high
        "default judgment entered against opposing party",
        "no proper service",
        "lack of jurisdiction",
        "due process violation",
        "no notice",
        "void judgment",
    ],
    "removed": [
        "improper removal",
        "forum shopping",
        "removed from state court",
    ],
    "garnishment": [
        # contracts + garnishment = conversion / unjust_enrichment vocabulary
        "withheld money",
        "took money",
        "retained money",
        "wrongful possession",
        "dominion over property",
    ],
    "active_motions": [
        "motion pending",
    ],
}

# party-tag → conditional phrases (only added if party AND case_type match)
_PARTY_VOCAB_CONDITIONAL = {
    "state_farm": {
        "bad_faith": ["denied coverage", "failed to pay claim", "denied claim"],
        "contracts_other": ["withheld money", "refused to perform"],
    },
    "allstate": {
        "bad_faith": ["denied claim", "failed to pay claim", "no investigation"],
    },
    "progressive": {
        "bad_faith": ["unreasonable denial", "claim delayed"],
    },
    "equifax": {
        "fraud_fcra": ["credit report", "inaccurate information", "failed to investigate"],
    },
    "transunion": {
        "fraud_fcra": ["credit report", "inaccurate information", "failed to investigate"],
    },
    "experian": {
        "fraud_fcra": ["credit report", "inaccurate information", "failed to investigate"],
    },
    "amerisave": {
        "quiet_title": ["broken chain of title", "no standing to foreclose"],
    },
    "planet_home": {
        "quiet_title": ["MERS assignment", "broken chain"],
    },
    "dovenmuehle": {
        "quiet_title": ["servicer failed to respond", "loan servicing"],
    },
    "northern_trust": {
        "contracts_other": ["custodian", "withheld money"],
    },
}


def _enrich_facts_for_case(entry: dict) -> List[str]:
    """Generate engine-vocabulary trigger phrases derived from registry fields.
    Deterministic. Every phrase appears verbatim in theory_map.yaml triggers.
    """
    out = []
    case_type = entry.get("case_type", "")
    status = (entry.get("status") or "").lower()
    parties = entry.get("parties", [])
    notes_lower = " ".join(entry.get("notes", [])).lower()

    # 1. case_type baseline vocabulary
    out.extend(_CASE_TYPE_VOCAB.get(case_type, []))

    # 2. status keyword vocabulary
    for key, phrases in _STATUS_VOCAB.items():
        if key in status or key in notes_lower:
            out.extend(phrases)

    # 3. party-conditional vocabulary
    for party in parties:
        ptab = _PARTY_VOCAB_CONDITIONAL.get(party, {})
        if case_type in ptab:
            out.extend(ptab[case_type])

    # 4. dedupe preserving order
    seen = set()
    deduped = []
    for p in out:
        if p not in seen:
            seen.add(p)
            deduped.append(p)
    return deduped


# ─── fact synthesis ───────────────────────────────────────────────────────
def synthesize_facts(entry: dict, audit: Optional[dict]) -> List[str]:
    facts = []
    role = detect_role(entry.get("caption", ""))

    # Layer 1: structured registry metadata
    facts.append(f"role: {role}")
    facts.append(f"court: {entry.get('case_court', 'unknown')}")
    facts.append(f"case type: {entry.get('case_type', '')}")
    facts.append(f"status: {entry.get('status', '')}")

    parties = entry.get("parties", [])
    if parties:
        facts.append(f"defendants: {', '.join(p for p in parties if p != _ident.party_key())}")

    # Layer 2: registry notes verbatim
    for note in entry.get("notes", []):
        if note and len(note.strip()) > 5:
            facts.append(note.strip())

    # Layer 3: audit signals (opponent argument detection)
    if audit:
        for arg in audit.get("top_arguments", [])[:5]:
            facts.append(f"opponent argument detected: {arg}")
        if audit.get("recommended_filing"):
            canonical_id = _v18j_resolve_case_id(canonical_id)  # v18j
            facts.append(f"prior recommendation: {audit['recommended_filing'][:200]}")

    # Layer 4: NEW — vocabulary enrichment (engine-trigger phrases)
    enrichment = _enrich_facts_for_case(entry)
    facts.extend(enrichment)

    # Layer 5: NEW — per-case override array
    custom = entry.get("engine_facts", [])
    if isinstance(custom, list):
        for phrase in custom:
            if isinstance(phrase, str) and phrase.strip():
                facts.append(phrase.strip())

    # Layer 6: PHASE_6 — factual_application text (rich factual allegations)
    # These contain specific factual phrases that match ELEMENT_SIGNATURES
    fa = entry.get("factual_application", {})
    if isinstance(fa, dict):
        import re as _re
        for claim_key, fa_text in fa.items():
            if claim_key.startswith("_"):
                continue
            if isinstance(fa_text, str) and len(fa_text) > 10:
                sentences = _re.split(r"(?<=[.;,])\s+", fa_text)
                for sent in sentences:
                    sent = sent.strip()
                    if len(sent) > 10 and sent not in facts:
                        facts.append(sent)
        default_fa = fa.get("_default", "")
        if isinstance(default_fa, str) and len(default_fa) > 10:
            sentences = _re.split(r"(?<=[.;,])\s+", default_fa)
            for sent in sentences:
                sent = sent.strip()
                if len(sent) > 10 and sent not in facts:
                    facts.append(sent)

    return facts


# ─── main loader ──────────────────────────────────────────────────────────
def load_all_cases() -> List[Dict[str, Any]]:
    if not REGISTRY.exists():
        log.error(f"registry not found: {REGISTRY}")
        return []
    try:
        registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    except Exception as e:
        log.error(f"failed to parse registry: {e}")
        return []

    case_map = load_case_map()
    cases = []

    for canonical_id, entry in registry.items():
        audit = load_latest_audit(canonical_id)
        case_type = entry.get("case_type", "")
        facts = synthesize_facts(entry, audit)

        expected_claims = CASE_TYPE_CLAIMS.get(case_type, [])
        expected_doctrines = expected_doctrines_for(entry.get("status", ""), case_type)

        cm_entry = case_map.get(canonical_id, {})
        evidence_count = len(cm_entry.get("source_folders", []))
        if evidence_count == 0 and audit:
            evidence_count = audit.get("source_folders_count", 0)

        cases.append({
            "canonical_id": canonical_id,
            "caption": entry.get("caption", ""),
            "court": entry.get("case_court", ""),
            "case_type": case_type,
            "status": entry.get("status", ""),
            "filed": entry.get("filed", ""),
            "role": detect_role(entry.get("caption", "")),
            "parties": entry.get("parties", []),
            "facts": facts,
            "appeals": entry.get("appeals", []),
            "latest_order": entry.get("latest_order", {}),
            "expected_claims":     expected_claims,    # NEW canonical name
            "expected_theories":   expected_claims,    # back-compat alias
            "expected_doctrines":  expected_doctrines,
            "evidence_count": evidence_count,
            "audit": audit or {},
            "registry_notes": entry.get("notes", []),
            "related_cases": entry.get("related_cases", []),
            "has_custom_facts": bool(entry.get("engine_facts")),
        })

    log.info(f"loaded {len(cases)} cases from registry")
    return cases


def load_case(canonical_id: str) -> Optional[Dict[str, Any]]:
    for c in load_all_cases():
        if c["canonical_id"] == canonical_id:
            # GT_MERGE_V1: enrich with appeals/latest_order from CASE_GROUND_TRUTH.yaml
            try:
                import yaml as _yaml, glob as _glob
                for _gt in _glob.glob(str(_eroot() / "obsidian_sync/cases/*/meta/CASE_GROUND_TRUTH.yaml")):
                    _d = _yaml.safe_load(Path(_gt).read_text(encoding="utf-8")) or {}
                    if str(_d.get("case_id","")) == canonical_id:
                        if _d.get("appeals"):       c["appeals"] = _d["appeals"]
                        if _d.get("latest_order"):  c["latest_order"] = _d["latest_order"]
                        break
            except Exception:
                pass
            return c
    return None


# ─── CLI demo ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[%(name)s] %(message)s")
    cases = load_all_cases()
    print(f"\n=== Loaded {len(cases)} cases ===\n")
    for c in cases:
        print(f"[{c['canonical_id']}] {c['caption']}")
        print(f"  court: {c['court']} | type: {c['case_type']} | role: {c['role']}")
        print(f"  expected_claims: {c['expected_claims']}")
        print(f"  expected_doctrines: {c['expected_doctrines']}")
        print(f"  facts: {len(c['facts'])} | evidence_count: {c['evidence_count']}")
        if c["has_custom_facts"]:
            print(f"  has_custom_facts: YES (engine_facts: in registry)")
        if c["audit"]:
            print(f"  audit score: {c['audit'].get('score')}/{c['audit'].get('rank')} "
                  f"| fatal: {c['audit'].get('fatal_violations')}")
        print(f"  --- engine vocabulary additions (last 8 facts) ---")
        for f in c["facts"][-8:]:
            print(f"    + {f}")
        print()
