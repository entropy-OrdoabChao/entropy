"""
order_engine.py
================
Block 5 — Void/Voidable Order Engine v0

Analyzes a fact pattern describing an order/judgment and returns:
  - detected_role: plaintiff / defendant (or 'unknown' with prompt)
  - applicable_doctrines: each with elements matched/missing
  - attack_vectors: role-aware procedural moves
  - sources: rules, cases, statutes
  - timing: deadlines per doctrine
  - risk_summary: per-doctrine risk to invoker

Mirrors scenario_engine architecture:
  - Reads order_doctrines.yaml (deterministic)
  - No LLM, no synthesis
  - Returns structured JSON

Logs to: <home>\\logs\\engines\\order_engine.log
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any

import yaml

# ─── Paths ───
BASE_PATH = Path(__file__).resolve().parent.parent  # OS-agnostic repo root
PIPELINE_DIR = BASE_PATH / "pipeline"
DOCTRINES_FILE = PIPELINE_DIR / "order_doctrines.yaml"
LOGS_DIR = BASE_PATH / "logs" / "engines"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# ─── Logging ───
log = logging.getLogger("order_engine")
if not log.handlers:
    log.setLevel(logging.INFO)
    fh = logging.FileHandler(LOGS_DIR / "order_engine.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"))
    log.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("[%(name)s] %(message)s"))
    log.addHandler(sh)


# ════════════════════════════════════════════════════════════════════
# DOCTRINE LOADER (cached)
# ════════════════════════════════════════════════════════════════════
_DOCTRINES_CACHE = None

def load_doctrines() -> dict:
    global _DOCTRINES_CACHE
    if _DOCTRINES_CACHE is None:
        if not DOCTRINES_FILE.exists():
            raise FileNotFoundError(f"Doctrine library not found: {DOCTRINES_FILE}")
        _DOCTRINES_CACHE = yaml.safe_load(DOCTRINES_FILE.read_text(encoding="utf-8"))
        log.info(f"Loaded order_doctrines v{_DOCTRINES_CACHE.get('version', '?')} "
                 f"({len(_DOCTRINES_CACHE.get('doctrines', {}))} doctrines)")
    return _DOCTRINES_CACHE


# ════════════════════════════════════════════════════════════════════
# FACT NORMALIZATION
# ════════════════════════════════════════════════════════════════════
def normalize_facts(fact_pattern: Any) -> tuple[list[str], str]:
    """Returns (fact_list, full_lowercase_text)."""
    if isinstance(fact_pattern, list):
        facts = [str(f).strip() for f in fact_pattern if str(f).strip()]
        full_text = " ".join(facts).lower()
    else:
        text = str(fact_pattern).strip()
        # Split on sentence boundaries
        facts = [s.strip().lower() for s in re.split(r"[.!?\n]+", text) if s.strip()]
        full_text = text.lower()
    return facts, full_text


# ════════════════════════════════════════════════════════════════════
# ROLE DETECTION
# ════════════════════════════════════════════════════════════════════
def detect_role(full_text: str, doctrines_data: dict, hint: str | None = None) -> dict:
    """Detect whether user is plaintiff or defendant based on fact pattern.

    Returns:
        {
          "role": "plaintiff" | "defendant" | "unknown",
          "confidence": "high" | "medium" | "low",
          "signals": [...keywords matched...],
          "needs_clarification": bool,
        }
    """
    if hint and hint.lower() in ("plaintiff", "defendant"):
        return {
            "role": hint.lower(),
            "confidence": "high",
            "signals": ["explicit_user_hint"],
            "needs_clarification": False,
        }

    role_kw = (doctrines_data.get("role_keywords", {}) or {})
    plaintiff_kws = role_kw.get("plaintiff", []) or []
    defendant_kws = role_kw.get("defendant", []) or []

    p_hits = [kw for kw in plaintiff_kws if kw in full_text]
    d_hits = [kw for kw in defendant_kws if kw in full_text]

    if not p_hits and not d_hits:
        return {
            "role": "unknown",
            "confidence": "low",
            "signals": [],
            "needs_clarification": True,
        }

    if len(p_hits) > len(d_hits):
        conf = "high" if len(p_hits) >= 2 else "medium"
        return {"role": "plaintiff", "confidence": conf, "signals": p_hits, "needs_clarification": False}
    elif len(d_hits) > len(p_hits):
        conf = "high" if len(d_hits) >= 2 else "medium"
        return {"role": "defendant", "confidence": conf, "signals": d_hits, "needs_clarification": False}
    else:
        # Tie — ambiguous
        return {
            "role": "unknown",
            "confidence": "low",
            "signals": p_hits + d_hits,
            "needs_clarification": True,
        }


# ════════════════════════════════════════════════════════════════════
# ORDER PRESENCE CHECK
# ════════════════════════════════════════════════════════════════════
def has_order_indicators(full_text: str, doctrines_data: dict) -> tuple[bool, list[str]]:
    """Return (has_order, matched_indicators)."""
    indicators = doctrines_data.get("order_indicators", []) or []
    hits = [i for i in indicators if i in full_text]
    return (len(hits) > 0, hits)


# ════════════════════════════════════════════════════════════════════
# DOCTRINE MATCHING
# ════════════════════════════════════════════════════════════════════
def match_doctrine(doctrine_id: str, doctrine: dict, full_text: str, role: str) -> dict | None:
    """Test a single doctrine against the fact pattern.

    Returns matched doctrine result or None if no triggers hit.
    """
    # Role applicability filter
    role_applicable = doctrine.get("role_applicable", []) or []
    role_ok = (
        role == "unknown"
        or "either" in role_applicable
        or role in role_applicable
    )

    # Trigger matching
    triggers = doctrine.get("triggers", []) or []
    triggers_hit = [t for t in triggers if t in full_text]

    if not triggers_hit:
        return None

    # Element heuristic match — count how many element keywords appear
    elements = doctrine.get("elements", []) or []
    matched_elements = []
    missing_elements = []
    for elem in elements:
        # Extract content words from element description for fuzzy match
        elem_lower = elem.lower()
        # Take 2-3 distinctive words and check if they appear in fact text
        words = [w for w in re.findall(r"[a-z_]{4,}", elem_lower)
                 if w not in {"that", "with", "from", "this", "have", "been",
                              "court", "party", "judgment", "order"}]
        # Element matches if any 2 distinctive words appear
        word_hits = sum(1 for w in words[:5] if w in full_text)
        if word_hits >= 2:
            matched_elements.append(elem)
        else:
            missing_elements.append(elem)

    # Confidence = function of trigger hits + element coverage
    trigger_score = min(len(triggers_hit), 3) / 3.0
    element_score = len(matched_elements) / max(len(elements), 1)
    overall = (trigger_score + element_score) / 2

    if overall >= 0.65:
        confidence = "high"
    elif overall >= 0.35:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "id": doctrine_id,
        "display_name": doctrine.get("display_name", doctrine_id),
        "classification": doctrine.get("classification", "voidable"),
        "role_applicable": role_applicable,
        "role_ok": role_ok,
        "confidence": confidence,
        "triggers_hit": triggers_hit,
        "matched_elements": matched_elements,
        "missing_elements": missing_elements,
        "attack_vector": doctrine.get("attack_vector", ""),
        "procedural_vehicle": doctrine.get("procedural_vehicle", ""),
        "timing": doctrine.get("timing", ""),
        "remedy": doctrine.get("remedy", ""),
        "risk_to_invoker": doctrine.get("risk_to_invoker", ""),
        "sources": doctrine.get("sources", []),
        "forum": doctrine.get("forum", "either"),
    }


# ════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ════════════════════════════════════════════════════════════════════
def analyze_order(
    fact_pattern: Any,
    role_hint: str | None = None,
    min_confidence: str = "low",
) -> dict:
    """Main analysis function.

    Args:
        fact_pattern: str or list of strings describing the order/judgment scenario
        role_hint: Optional 'plaintiff' or 'defendant' to skip auto-detection
        min_confidence: 'low' | 'medium' | 'high' — filter doctrines by confidence

    Returns:
        Structured analysis dict
    """
    t0 = time.perf_counter()
    doctrines_data = load_doctrines()

    facts, full_text = normalize_facts(fact_pattern)
    log.info(f"Analyzing {len(facts)} facts")

    # Sanity check: are we even looking at an order/judgment scenario?
    has_order, order_signals = has_order_indicators(full_text, doctrines_data)

    # Role detection
    role_info = detect_role(full_text, doctrines_data, hint=role_hint)
    log.info(f"Detected role: {role_info['role']} (confidence={role_info['confidence']})")

    # Match all doctrines
    all_doctrines = doctrines_data.get("doctrines", {}) or {}
    matched = []
    confidence_order = {"low": 0, "medium": 1, "high": 2}
    min_threshold = confidence_order.get(min_confidence.lower(), 0)

    for did, ddef in all_doctrines.items():
        result = match_doctrine(did, ddef, full_text, role_info["role"])
        if result is None:
            continue
        if confidence_order[result["confidence"]] < min_threshold:
            continue
        matched.append(result)

    # Sort by confidence descending, then by element coverage
    matched.sort(
        key=lambda d: (
            confidence_order[d["confidence"]],
            len(d["matched_elements"]),
        ),
        reverse=True,
    )

    # Build attack vector summary by classification
    void_doctrines = [d for d in matched if d["classification"] == "void"]
    voidable_doctrines = [d for d in matched if d["classification"] == "voidable"]
    sanction_doctrines = [d for d in matched if d["classification"] == "sanctionable"]

    # Strategy notes
    strategy = []
    if not has_order:
        strategy.append("No clear order/judgment indicators in fact pattern. Verify there is an actual order to attack.")
    if role_info["needs_clarification"]:
        strategy.append("Role unclear — output assumes 'either applicable'. Provide role hint for sharper analysis.")
    if void_doctrines:
        strategy.append(f"{len(void_doctrines)} VOID doctrine(s) apply — relief mandatory if proven, no time bar. PRIORITIZE THESE.")
    if voidable_doctrines:
        strategy.append(f"{len(voidable_doctrines)} VOIDABLE doctrine(s) apply — discretionary relief, watch deadlines.")
    if sanction_doctrines:
        strategy.append(f"{len(sanction_doctrines)} sanction-bearing doctrine(s) apply — leverage even if ultimate relief denied.")
    if role_info["role"] == "plaintiff" and any("forum" in d["id"] for d in matched):
        strategy.append("As plaintiff: forum-shopping doctrine cuts BOTH ways — opponent may use against your filing choices.")
    if role_info["role"] == "defendant" and "coram_nobis_default_vacatur" in [d["id"] for d in matched]:
        strategy.append("As defendant facing default: act FAST — most jurisdictions impose 30-day prompt-action requirements.")

    # Highest-priority recommended motion
    recommended_motion = None
    if matched:
        top = matched[0]
        recommended_motion = {
            "doctrine": top["display_name"],
            "vehicle": top["procedural_vehicle"],
            "classification": top["classification"],
            "timing": top["timing"],
            "confidence": top["confidence"],
        }

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    log.info(f"Analysis complete: {len(matched)} doctrines matched in {elapsed_ms}ms")

    return {
        "facts": facts,
        "fact_count": len(facts),
        "order_indicators_found": order_signals,
        "has_order_in_facts": has_order,
        "detected_role": role_info,
        "doctrines_evaluated": len(all_doctrines),
        "doctrines_matched": len(matched),
        "doctrines": matched,
        "classification_summary": {
            "void_count": len(void_doctrines),
            "voidable_count": len(voidable_doctrines),
            "sanctionable_count": len(sanction_doctrines),
        },
        "recommended_motion": recommended_motion,
        "strategy": strategy,
        "telemetry": {
            "runtime_ms": elapsed_ms,
            "min_confidence_filter": min_confidence,
        },
    }


# ════════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description="Order Engine v0 — void/voidable order analyzer")
    parser.add_argument("input", nargs="?", help="Path to YAML/JSON facts file OR raw fact text")
    parser.add_argument("--role", choices=["plaintiff", "defendant"], help="Role hint")
    parser.add_argument("--min-confidence", default="low", help="low | medium | high")
    parser.add_argument("--output", help="Path to write JSON result")
    args = parser.parse_args()

    if not args.input:
        # Demo mode
        demo_facts = (
            "I was sued in EDPA. The court entered default judgment against me without "
            "proper service. I never received the complaint. The judgment was entered "
            "ex parte and I had no notice of the hearing."
        )
        print(f"Demo mode. Using fact pattern:\n  {demo_facts}\n")
        result = analyze_order(demo_facts, role_hint=args.role, min_confidence=args.min_confidence)
    else:
        input_path = Path(args.input)
        if input_path.exists():
            text = input_path.read_text(encoding="utf-8")
            if input_path.suffix in (".yaml", ".yml"):
                data = yaml.safe_load(text)
                facts = data.get("facts", text) if isinstance(data, dict) else text
            elif input_path.suffix == ".json":
                data = json.loads(text)
                facts = data.get("facts", text) if isinstance(data, dict) else text
            else:
                facts = text
        else:
            facts = args.input  # treat as raw text
        result = analyze_order(facts, role_hint=args.role, min_confidence=args.min_confidence)

    print()
    print("=" * 60)
    print(" ORDER ENGINE RESULT")
    print("=" * 60)
    print(f"Facts: {result['fact_count']}")
    print(f"Has order indicators: {result['has_order_in_facts']}  ({', '.join(result['order_indicators_found'][:5])})")
    print(f"Detected role: {result['detected_role']['role']} ({result['detected_role']['confidence']})")
    print(f"Doctrines matched: {result['doctrines_matched']} / {result['doctrines_evaluated']}")
    print(f"  void: {result['classification_summary']['void_count']}")
    print(f"  voidable: {result['classification_summary']['voidable_count']}")
    print(f"  sanctionable: {result['classification_summary']['sanctionable_count']}")
    print(f"Runtime: {result['telemetry']['runtime_ms']}ms")
    print()

    if result["recommended_motion"]:
        rm = result["recommended_motion"]
        print(f"RECOMMENDED MOTION:")
        print(f"  Doctrine: {rm['doctrine']}")
        print(f"  Vehicle:  {rm['vehicle']}")
        print(f"  Class:    {rm['classification']}")
        print(f"  Timing:   {rm['timing']}")
        print(f"  Confidence: {rm['confidence']}")
        print()

    print("DOCTRINES MATCHED:")
    for d in result["doctrines"]:
        marker = "✓" if d["role_ok"] else "?"
        print(f"  [{d['confidence'].upper()}] {marker} {d['display_name']} ({d['classification']})")
        print(f"      Triggers: {', '.join(d['triggers_hit'][:3])}")
        if d["matched_elements"]:
            print(f"      Elements matched: {len(d['matched_elements'])}/{len(d['matched_elements']) + len(d['missing_elements'])}")
        print(f"      Vehicle: {d['procedural_vehicle']}")
    print()

    if result["strategy"]:
        print("STRATEGY:")
        for s in result["strategy"]:
            print(f"  - {s}")

    if args.output:
        Path(args.output).write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"\nFull JSON written to {args.output}")


if __name__ == "__main__":
    main()


# Unified alias
run_order_analysis = analyze_order
