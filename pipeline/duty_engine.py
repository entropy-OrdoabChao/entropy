"""
Duty-of-Business Engine (Block 4 v0)
=====================================

Deterministic engine that maps entity_type + action to mandatory duties
across 5 classes:
  - statutory          (federal/state statutes: 15 USC, 12 USC, state codes)
  - regulatory         (CFR, state insurance codes, NAIC model acts)
  - industry_standard  (FNMA, ISO forms, NCOIL, industry best practices)
  - fiduciary          (fiduciary duties arising from relationship)
  - common_law         (reasonable care, good faith, fair dealing)

Purpose: burden-flip. When defendant says "no cause of action," this engine
produces the list of duties they owed, Rule 34 RFPs to force production of
compliance evidence, and regulator complaint templates.

Usage:
    from duty_engine import run_duty_analysis

    result = run_duty_analysis(
        entity_type="insurance_company",
        actions=["holding_premium", "denying_refund"],
        target_court="EDPA",
        fact_pattern="sold vehicle, insurer retained premium..."
    )

CLI:
    python duty_engine.py --entity insurance_company \\
        --actions holding_premium,denying_refund \\
        --court EDPA

Hermes orchestration:
    scenario_engine.py will auto-call run_duty_analysis() when entity_type
    is provided, and embed a compact duties_summary in its output.

Rules:
    - DO NOT interpret law
    - DO NOT hallucinate duty sources
    - ONLY return duties present in YAML duty_maps
    - Log every call to <home>\\logs\\engines\\duty_engine.log
"""

from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import argparse
import json
import logging
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import yaml

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

# ───────────────────────────── Paths & logging ─────────────────────────────

BASE_PATH = _BEEZLE_ROOT
DUTY_MAPS_DIR = BASE_PATH / "pipeline" / "duty_maps"
LOG_DIR = BASE_PATH / "logs" / "engines"
LOG_DIR.mkdir(parents=True, exist_ok=True)

LOG_PATH = LOG_DIR / "duty_engine.log"

logger = logging.getLogger("duty_engine")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(
        logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s")
    )
    logger.addHandler(fh)

DUTY_CLASSES = [
    "statutory",
    "regulatory",
    "industry_standard",
    "fiduciary",
    "common_law",
]

# ───────────────────────────── Data classes ─────────────────────────────


@dataclass
class Duty:
    """A single duty owed by an entity."""

    duty_id: str
    name: str
    duty_class: str
    source: str  # e.g. "15-USC-1692g" or "NAIC Model Act 900 §4"
    source_title: str
    jurisdiction: str  # federal, state, pa, etc.
    description: str
    triggering_actions: list[str] = field(default_factory=list)
    burden_flip_questions: list[str] = field(default_factory=list)
    evidence_demanded: list[str] = field(default_factory=list)
    violation_remedy: list[str] = field(default_factory=list)
    regulator: str | None = None
    matched_actions: list[str] = field(default_factory=list)

    def to_compact(self) -> dict[str, Any]:
        return {
            "id": self.duty_id,
            "name": self.name,
            "class": self.duty_class,
            "source": self.source,
            "matched": self.matched_actions,
            "regulator": self.regulator,
        }


@dataclass
class DutyAnalysisResult:
    """Full result from duty engine."""

    entity_type: str
    target_court: str
    actions_provided: list[str]
    total_duties_in_map: int
    matched_duties: list[Duty] = field(default_factory=list)
    by_class: dict[str, list[Duty]] = field(default_factory=dict)
    regulators_implicated: list[str] = field(default_factory=list)
    rule_34_rfps: list[str] = field(default_factory=list)
    regulator_complaint: dict[str, Any] | None = None
    strategy_notes: list[str] = field(default_factory=list)
    runtime_ms: int = 0

    def to_full_dict(self) -> dict[str, Any]:
        out = {
            "entity_type": self.entity_type,
            "target_court": self.target_court,
            "actions_provided": self.actions_provided,
            "total_duties_in_map": self.total_duties_in_map,
            "matched_duties": [asdict(d) for d in self.matched_duties],
            "by_class": {
                k: [asdict(d) for d in v] for k, v in self.by_class.items()
            },
            "regulators_implicated": self.regulators_implicated,
            "rule_34_rfps": self.rule_34_rfps,
            "regulator_complaint": self.regulator_complaint,
            "strategy_notes": self.strategy_notes,
            "runtime_ms": self.runtime_ms,
        }
        return out

    def to_compact_dict(self) -> dict[str, Any]:
        """Compact format for Hermes (< 2000 tokens)."""
        return {
            "entity_type": self.entity_type,
            "total_duties_matched": len(self.matched_duties),
            "by_class_counts": {
                k: len(v) for k, v in self.by_class.items() if v
            },
            "top_duties": [
                d.to_compact() for d in self.matched_duties[:10]
            ],
            "regulators": self.regulators_implicated,
            "rfp_count": len(self.rule_34_rfps),
            "rfp_preview": self.rule_34_rfps[:3],
            "complaint_lane": (
                self.regulator_complaint.get("regulator")
                if self.regulator_complaint
                else None
            ),
            "strategy_notes": self.strategy_notes,
            "runtime_ms": self.runtime_ms,
        }


# ───────────────────────────── Loaders ─────────────────────────────


def list_available_entity_types() -> list[str]:
    """Return all entity types with a duty map YAML."""
    if not DUTY_MAPS_DIR.exists():
        return []
    return sorted(p.stem for p in DUTY_MAPS_DIR.glob("*.yaml"))


def load_duty_map(entity_type: str) -> dict[str, Any]:
    """Load a single entity's duty map."""
    path = DUTY_MAPS_DIR / f"{entity_type}.yaml"
    if not path.exists():
        raise FileNotFoundError(
            f"No duty map for entity_type={entity_type!r}. "
            f"Available: {list_available_entity_types()}"
        )
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict) or "duties" not in data:
        raise ValueError(
            f"Invalid duty map at {path}: must be dict with 'duties' key"
        )
    logger.info(
        f"Loaded duty map {entity_type} v{data.get('version', '?')} "
        f"({len(data['duties'])} duties)"
    )
    return data


def _duty_from_yaml(raw: dict[str, Any]) -> Duty:
    return Duty(
        duty_id=raw.get("id", "unknown"),
        name=raw.get("name", "Unnamed duty"),
        duty_class=raw.get("class", "statutory"),
        source=raw.get("source", ""),
        source_title=raw.get("source_title", ""),
        jurisdiction=raw.get("jurisdiction", "federal"),
        description=raw.get("description", ""),
        triggering_actions=list(raw.get("triggering_actions", [])),
        burden_flip_questions=list(raw.get("burden_flip_questions", [])),
        evidence_demanded=list(raw.get("evidence_demanded", [])),
        violation_remedy=list(raw.get("violation_remedy", [])),
        regulator=raw.get("regulator"),
    )


# ───────────────────────────── Matcher ─────────────────────────────


def _normalize_action(s: str) -> str:
    return s.lower().strip().replace("-", "_").replace(" ", "_")


def match_duties(
    duty_map: dict[str, Any], actions: list[str]
) -> list[Duty]:
    """Return duties whose triggering_actions intersect with `actions`."""
    norm_actions = {_normalize_action(a) for a in actions if a}
    matched: list[Duty] = []

    for raw in duty_map.get("duties", []):
        duty = _duty_from_yaml(raw)
        triggers = {_normalize_action(t) for t in duty.triggering_actions}
        # Empty triggers = universal duty (always applies to this entity)
        if not triggers:
            duty.matched_actions = ["universal"]
            matched.append(duty)
            continue
        hits = triggers & norm_actions
        if hits:
            duty.matched_actions = sorted(hits)
            matched.append(duty)

    # Sort: statutory > regulatory > industry_standard > fiduciary > common_law
    class_order = {c: i for i, c in enumerate(DUTY_CLASSES)}
    matched.sort(key=lambda d: (class_order.get(d.duty_class, 99), d.duty_id))

    logger.info(
        f"Matched {len(matched)} duties from {len(duty_map.get('duties', []))} "
        f"total (actions={sorted(norm_actions)})"
    )
    return matched


# ───────────────────────────── RFP drafting ─────────────────────────────


def draft_rule_34_rfps(duties: list[Duty], max_rfps: int = 25) -> list[str]:
    """Generate Rule 34 Requests for Production from matched duties' evidence_demanded."""
    rfps: list[str] = []
    seen: set[str] = set()
    rfp_num = 1

    for duty in duties:
        for item in duty.evidence_demanded:
            key = item.lower().strip()
            if key in seen:
                continue
            seen.add(key)
            rfp = (
                f"REQUEST NO. {rfp_num}: "
                f"Produce all documents, records, policies, procedures, "
                f"communications, and ESI concerning {item} as required under "
                f"{duty.source} ({duty.source_title or duty.name}). "
                f"This request includes but is not limited to the time period "
                f"from three years prior to the events alleged in the "
                f"Complaint through the present."
            )
            rfps.append(rfp)
            rfp_num += 1
            if len(rfps) >= max_rfps:
                return rfps
    return rfps


def draft_regulator_complaint(
    duties: list[Duty], entity_type: str, fact_pattern: str = ""
) -> dict[str, Any] | None:
    """Build a regulator complaint template from the highest-priority duty violations."""
    if not duties:
        return None

    # Pick the regulator with the most implicated duties
    regulator_counts: dict[str, int] = {}
    for d in duties:
        if d.regulator:
            regulator_counts[d.regulator] = regulator_counts.get(d.regulator, 0) + 1

    if not regulator_counts:
        return None

    primary_regulator = max(regulator_counts, key=regulator_counts.get)
    relevant_duties = [d for d in duties if d.regulator == primary_regulator]

    violations_text = []
    for d in relevant_duties[:10]:
        violations_text.append(
            f"- {d.name} ({d.source}): {d.description[:200]}"
        )

    complaint = {
        "regulator": primary_regulator,
        "subject": f"Complaint against {entity_type} — multiple duty violations",
        "entity_type": entity_type,
        "violations_count": len(relevant_duties),
        "duty_sources_cited": [d.source for d in relevant_duties],
        "violations_summary": "\n".join(violations_text),
        "requested_action": (
            "Commence investigation, preserve all records, issue findings "
            "of noncompliance, impose civil penalties where warranted, and "
            "provide complainant with copy of investigative file."
        ),
        "fact_pattern_excerpt": (fact_pattern[:500] + "...") if len(fact_pattern) > 500 else fact_pattern,
    }
    return complaint


# ───────────────────────────── Strategy notes ─────────────────────────────


def generate_strategy_notes(
    result: DutyAnalysisResult, duty_map: dict[str, Any]
) -> list[str]:
    notes: list[str] = []

    if not result.matched_duties:
        notes.append(
            "No duties matched the provided actions. Verify entity_type and "
            "action terminology against duty map triggers."
        )
        return notes

    by_class_counts = {k: len(v) for k, v in result.by_class.items() if v}
    notes.append(
        f"{len(result.matched_duties)} duties matched across "
        f"{len(by_class_counts)} classes: "
        f"{', '.join(f'{k}={v}' for k, v in by_class_counts.items())}"
    )

    if by_class_counts.get("statutory", 0) > 0:
        notes.append(
            f"STATUTORY duties triggered ({by_class_counts['statutory']}) — "
            "cite verbatim in Complaint under 'Duty' element of each claim. "
            "Defendant cannot contest existence of statutory duty; only "
            "factual compliance is in dispute."
        )

    if by_class_counts.get("regulatory", 0) > 0:
        notes.append(
            f"REGULATORY duties triggered ({by_class_counts['regulatory']}) — "
            "parallel complaint lane available via regulator. Regulator "
            "findings are admissible as evidence of breach."
        )

    if by_class_counts.get("fiduciary", 0) > 0:
        notes.append(
            "FIDUCIARY duties found — pleading must allege the "
            "fiduciary relationship (special trust, reliance, superior "
            "knowledge). Higher standard of care applies."
        )

    if by_class_counts.get("common_law", 0) > 0:
        notes.append(
            "COMMON LAW duties (reasonable care, good faith) — serve as "
            "backstop when statutory/regulatory duties are contested on "
            "technical grounds."
        )

    if result.regulators_implicated:
        notes.append(
            f"File parallel regulatory complaints with: "
            f"{', '.join(result.regulators_implicated)}. "
            "Regulator investigation creates discovery leverage and record "
            "for summary judgment."
        )

    if result.rule_34_rfps:
        notes.append(
            f"{len(result.rule_34_rfps)} Rule 34 RFPs drafted — serve "
            "immediately after answer/pre-answer motion. These shift burden "
            "by forcing defendant to produce compliance evidence or admit "
            "nonproduction."
        )

    return notes


# ───────────────────────────── Main entry ─────────────────────────────


def run_duty_analysis(
    entity_type: str,
    actions: list[str],
    target_court: str = "EDPA",
    fact_pattern: str = "",
    include_rfps: bool = True,
    include_regulator_complaint: bool = True,
) -> DutyAnalysisResult:
    """Run full duty analysis for an entity/action combination."""
    t0 = time.time()
    logger.info(
        f"run_duty_analysis: entity={entity_type} actions={actions} "
        f"court={target_court}"
    )

    try:
        duty_map = load_duty_map(entity_type)
    except FileNotFoundError as e:
        logger.error(str(e))
        return DutyAnalysisResult(
            entity_type=entity_type,
            target_court=target_court,
            actions_provided=list(actions),
            total_duties_in_map=0,
            strategy_notes=[f"ERROR: {e}"],
            runtime_ms=int((time.time() - t0) * 1000),
        )

    matched = match_duties(duty_map, actions)

    # Group by class
    by_class: dict[str, list[Duty]] = {c: [] for c in DUTY_CLASSES}
    for d in matched:
        by_class.setdefault(d.duty_class, []).append(d)

    # Regulators
    regulators = sorted({d.regulator for d in matched if d.regulator})

    # RFPs
    rfps = draft_rule_34_rfps(matched) if include_rfps else []

    # Regulator complaint
    complaint = (
        draft_regulator_complaint(matched, entity_type, fact_pattern)
        if include_regulator_complaint
        else None
    )

    result = DutyAnalysisResult(
        entity_type=entity_type,
        target_court=target_court,
        actions_provided=list(actions),
        total_duties_in_map=len(duty_map.get("duties", [])),
        matched_duties=matched,
        by_class=by_class,
        regulators_implicated=regulators,
        rule_34_rfps=rfps,
        regulator_complaint=complaint,
        runtime_ms=int((time.time() - t0) * 1000),
    )
    result.strategy_notes = generate_strategy_notes(result, duty_map)

    logger.info(
        f"run_duty_analysis complete: {len(matched)} duties, "
        f"{len(rfps)} RFPs, {len(regulators)} regulators in {result.runtime_ms}ms"
    )
    return result


# ───────────────────────────── CLI ─────────────────────────────


def _main_cli() -> None:
    ap = argparse.ArgumentParser(description="Beezle Duty Engine (Block 4)")
    ap.add_argument("--entity", required=True, help="entity_type (e.g. insurance_company)")
    ap.add_argument(
        "--actions",
        required=True,
        help="comma-separated actions (e.g. holding_premium,denying_refund)",
    )
    ap.add_argument("--court", default="EDPA")
    ap.add_argument("--facts", default="")
    ap.add_argument("--output", help="write full JSON to this path")
    ap.add_argument("--compact", action="store_true", help="print compact summary")
    args = ap.parse_args()

    actions = [a.strip() for a in args.actions.split(",") if a.strip()]
    print(f"Running duty_analysis on entity={args.entity}")
    print(f"  actions: {actions}")
    print(f"  target_court: {args.court}")
    print()

    result = run_duty_analysis(
        entity_type=args.entity,
        actions=actions,
        target_court=args.court,
        fact_pattern=args.facts,
    )

    print("=== RESULT ===")
    print(f"Total duties in map: {result.total_duties_in_map}")
    print(f"Matched duties: {len(result.matched_duties)}")
    print(f"Runtime: {result.runtime_ms}ms")
    print()

    if result.by_class:
        print("By class:")
        for cls in DUTY_CLASSES:
            duties = result.by_class.get(cls, [])
            if duties:
                print(f"  [{cls.upper()}] {len(duties)}")
                for d in duties[:5]:
                    print(f"    - {d.name} ({d.source})")
                if len(duties) > 5:
                    print(f"    ... and {len(duties) - 5} more")
        print()

    if result.regulators_implicated:
        print(f"Regulators: {', '.join(result.regulators_implicated)}")
        print()

    print(f"Rule 34 RFPs drafted: {len(result.rule_34_rfps)}")
    for rfp in result.rule_34_rfps[:3]:
        print(f"  {rfp[:200]}{'...' if len(rfp) > 200 else ''}")
    if len(result.rule_34_rfps) > 3:
        print(f"  ... and {len(result.rule_34_rfps) - 3} more")
    print()

    if result.regulator_complaint:
        rc = result.regulator_complaint
        print(f"Regulator complaint ready for: {rc['regulator']}")
        print(f"  Violations cited: {rc['violations_count']}")
        print()

    print("Strategy notes:")
    for note in result.strategy_notes:
        print(f"  - {note}")

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(result.to_full_dict(), f, indent=2)
        print(f"\nFull JSON written to {out_path}")


if __name__ == "__main__":
    _main_cli()
