"""
pipeline/logic_engine.py — Deterministic rule matching.

NO LLM. Pure IF/THEN against rules already retrieved from the DB.

Functions:
  match_rules(facts, rules)        → List[violation_dict]
  identify_regulators(facts)       → List[str]
"""
from __future__ import annotations
from typing import Any, Dict, List

from core.logger import get_logger

log = get_logger("logic_engine")

# ─── Regulator identification by trigger keywords ────────────────────────
_REGULATOR_TRIGGERS = {
    "CFPB":       ["fcra", "fdcpa", "ecoa", "tila", "respa", "credit reporting"],
    "FTC":        ["unfair", "deceptive", "uadap"],
    "Pa AG":      ["upcl", "pennsylvania consumer", "uipa"],
    "PA DOI":     ["bad faith", "insurance claim", "uipa"],
    "HUD":        ["fair housing", "disparate impact"],
}


# BEEZLE_FN_UNUSED 2026-06-13: match_rules (no static refs in pipeline/)
def match_rules(facts: List[str], rules: List[Any]) -> List[Dict[str, Any]]:
    """
    Apply each rule to the facts. A rule is expected to be a dict with:
      { id, condition (callable | list[str]), violation, remedy, mandatory }

    For unknown shapes, the rule is skipped (logged as warn).
    """
    violations: List[Dict[str, Any]] = []
    fact_blob = " ".join(facts).lower()

    for r in rules or []:
        if not isinstance(r, dict):
            log.warning("match", f"non-dict rule skipped: {type(r).__name__}")
            continue
        cond = r.get("condition")
        triggered = False
        if callable(cond):
            try:
                triggered = bool(cond(facts))
            except Exception as e:
                log.fail("match", f"rule {r.get('id')}: {e}")
                continue
        elif isinstance(cond, list):
            triggered = all(kw.lower() in fact_blob for kw in cond)
        if triggered:
            violations.append({
                "rule_id":   r.get("id"),
                "violation": r.get("violation"),
                "remedy":    r.get("remedy"),
                "mandatory": bool(r.get("mandatory")),
            })

    log.info("match", f"{len(violations)} violations from {len(rules or [])} rules")
    return violations


# BEEZLE_FN_UNUSED 2026-06-13: identify_regulators (no static refs in pipeline/)
def identify_regulators(facts: List[str]) -> List[str]:
    blob = " ".join(facts).lower()
    hits = [reg for reg, kws in _REGULATOR_TRIGGERS.items()
            if any(kw in blob for kw in kws)]
    log.info("regulators", f"{len(hits)} matched: {hits}")
    return hits
