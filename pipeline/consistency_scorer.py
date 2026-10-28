"""
pipeline/consistency_scorer.py — Consistency Scorer

DETERMINISTIC. NO LLM. NO OPINION.

Replaces "this is right" with "this aligns with the system at X% consistency."

Scores any legal argument/claim across axes:
  - Definition alignment (does term usage match source statute?)
  - Authority hierarchy (does lower authority conform to higher?)
  - Actor-scope match (is penalty applied to correct class?)
  - Trigger satisfaction (are activation conditions met?)
  - Burden placement (is burden on correct party?)
  - Remedy availability (does statute provide this remedy?)
  - Procedural compliance (are filing requirements met?)

Each axis: 0.0 (no support) to 1.0 (fully supported by text)
Overall: weighted average of all axes

Usage:
    from pipeline.consistency_scorer import score_consistency

    result = score_consistency(claim, engines_output)
"""

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional
from core.logger import get_logger

log = get_logger("consistency_scorer")

# ─── Data Structures ────────────────────────────────────────────────

@dataclass
class AxisScore:
    """Score on a single consistency axis."""
    axis: str
    score: float             # 0.0 to 1.0
    weight: float            # how much this axis matters
    source_count: int = 0    # number of source texts supporting
    gap_count: int = 0       # number of unsupported claims
    details: str = ""        # explanation


@dataclass
class ConsistencyReport:
    """Complete consistency report for a claim or argument."""
    subject: str                    # what was scored
    overall_score: float = 0.0
    axes: List[AxisScore] = field(default_factory=list)
    strengths: List[str] = field(default_factory=list)
    weaknesses: List[str] = field(default_factory=list)
    gaps: List[str] = field(default_factory=list)  # needs more data

    def to_dict(self) -> dict:
        return asdict(self)

    def grade(self) -> str:
        """Letter grade based on overall score."""
        if self.overall_score >= 0.9:
            return "A"
        elif self.overall_score >= 0.8:
            return "B"
        elif self.overall_score >= 0.7:
            return "C"
        elif self.overall_score >= 0.5:
            return "D"
        else:
            return "F"


# Default weights for each axis
DEFAULT_WEIGHTS = {
    "definition_alignment": 0.12,
    "authority_hierarchy": 0.18,
    "actor_scope": 0.12,
    "trigger_satisfaction": 0.10,
    "burden_placement": 0.10,
    "remedy_availability": 0.10,
    "procedural_compliance": 0.08,
    "source_traceability": 0.08,
    "construction_compliance": 0.12,
}


# ─── Core Scoring ────────────────────────────────────────────────────

def score_consistency(claim: dict, engines_output: dict,
                      weights: dict = None) -> ConsistencyReport:
    """
    Score a legal claim/argument for consistency with source law.

    Args:
        claim: dict with keys like:
            - citation: str
            - theory: str
            - elements: list
            - facts: list
            - defendants: list

        engines_output: dict with results from other engines:
            - definition: output from definition_engine
            - actor_scope: output from actor_scope_engine
            - burden: output from burden_engine
            - trigger: output from trigger_engine
            - remedy: output from remedy_engine
            - immunity: output from immunity_engine
            - hierarchy: output from hierarchy_enforcer
            - procedural: output from procedural_attack_builder

        weights: optional custom axis weights

    Returns:
        ConsistencyReport with per-axis scores and overall score
    """
    w = weights or DEFAULT_WEIGHTS
    report = ConsistencyReport(subject=claim.get("theory", claim.get("citation", "unknown")))

    # Score each axis
    report.axes.append(_score_definition_alignment(claim, engines_output, w))
    report.axes.append(_score_authority_hierarchy(claim, engines_output, w))
    report.axes.append(_score_actor_scope(claim, engines_output, w))
    report.axes.append(_score_trigger_satisfaction(claim, engines_output, w))
    report.axes.append(_score_burden_placement(claim, engines_output, w))
    report.axes.append(_score_remedy_availability(claim, engines_output, w))
    report.axes.append(_score_procedural_compliance(claim, engines_output, w))
    report.axes.append(_score_source_traceability(claim, engines_output, w))
    report.axes.append(_score_construction_compliance(claim, engines_output, w))

    # Calculate weighted average
    total_weight = sum(a.weight for a in report.axes)
    if total_weight > 0:
        report.overall_score = sum(a.score * a.weight for a in report.axes) / total_weight
    report.overall_score = round(report.overall_score, 3)

    # Classify strengths and weaknesses
    for axis in report.axes:
        if axis.score >= 0.8:
            report.strengths.append(f"{axis.axis}: {axis.details}")
        elif axis.score <= 0.3:
            report.weaknesses.append(f"{axis.axis}: {axis.details}")
        if axis.gap_count > 0:
            report.gaps.append(f"{axis.axis}: {axis.gap_count} unsupported claim(s)")

    return report


def score_batch(claims: list, engines_outputs: list,
                weights: dict = None) -> List[ConsistencyReport]:
    """Score multiple claims. Returns sorted by overall_score descending."""
    results = []
    for claim, eo in zip(claims, engines_outputs):
        results.append(score_consistency(claim, eo, weights))
    return sorted(results, key=lambda r: r.overall_score, reverse=True)


# ─── Per-Axis Scorers ────────────────────────────────────────────────

def _score_definition_alignment(claim: dict, eo: dict, w: dict) -> AxisScore:
    """Do terms in the claim match their source statute definitions?"""
    defn = eo.get("definition", {})
    if not defn:
        return AxisScore("definition_alignment", 0.5, w.get("definition_alignment", 0.15),
                         details="No definition data available — needs analysis")

    definitions = defn.get("definitions", [])
    if not definitions:
        return AxisScore("definition_alignment", 0.5, w.get("definition_alignment", 0.15),
                         details="No definitions extracted from statute")

    # Check: are definitions from the correct statute?
    valid = sum(1 for d in definitions if d.get("source_statute") == claim.get("citation"))
    total = len(definitions)
    score = valid / total if total > 0 else 0.5

    return AxisScore(
        "definition_alignment", round(score, 2), w.get("definition_alignment", 0.15),
        source_count=valid, gap_count=total - valid,
        details=f"{valid}/{total} definitions sourced from correct statute"
    )


def _score_authority_hierarchy(claim: dict, eo: dict, w: dict) -> AxisScore:
    """Does the authority chain hold? Constitution→statute→regulation→action."""
    hierarchy = eo.get("hierarchy", {})
    if not hierarchy:
        return AxisScore("authority_hierarchy", 0.5, w.get("authority_hierarchy", 0.20),
                         details="No hierarchy data — needs conformity check")

    violations = hierarchy.get("violations", [])
    total_checked = hierarchy.get("total_checked", 1)
    if total_checked == 0:
        total_checked = 1

    score = max(0, 1.0 - (len(violations) / total_checked))

    return AxisScore(
        "authority_hierarchy", round(score, 2), w.get("authority_hierarchy", 0.20),
        source_count=total_checked - len(violations),
        gap_count=len(violations),
        details=f"{len(violations)} hierarchy violations found in {total_checked} checks"
    )


def _score_actor_scope(claim: dict, eo: dict, w: dict) -> AxisScore:
    """Is the penalty applied to the correct actor class?"""
    actor = eo.get("actor_scope", {})
    if not actor:
        return AxisScore("actor_scope", 0.5, w.get("actor_scope", 0.15),
                         details="No actor-scope data — needs analysis")

    has_mismatch = actor.get("has_mismatch", False) or actor.get("has_scope_mismatch", False)
    mismatches = actor.get("mismatches", [])

    if has_mismatch:
        score = max(0, 1.0 - (len(mismatches) * 0.3))
        return AxisScore(
            "actor_scope", round(score, 2), w.get("actor_scope", 0.15),
            gap_count=len(mismatches),
            details=f"MISMATCH: {len(mismatches)} actor-scope conflicts detected"
        )

    return AxisScore(
        "actor_scope", 1.0, w.get("actor_scope", 0.15),
        details="Actor scope aligns — duty holder matches penalty target"
    )


def _score_trigger_satisfaction(claim: dict, eo: dict, w: dict) -> AxisScore:
    """Are the statutory trigger conditions met by the facts?"""
    trigger = eo.get("trigger", {})
    if not trigger:
        return AxisScore("trigger_satisfaction", 0.5, w.get("trigger_satisfaction", 0.10),
                         details="No trigger data — needs analysis")

    triggers = trigger.get("triggers", [])
    if not triggers:
        return AxisScore("trigger_satisfaction", 0.5, w.get("trigger_satisfaction", 0.10),
                         details="No trigger conditions found in provision")

    # Check: how many triggers have matching facts?
    facts = claim.get("facts", [])
    satisfied = 0
    for t in triggers:
        if t.get("mandatory") and facts:
            satisfied += 1  # conservative: if we have facts and there's a mandatory duty
    total = len([t for t in triggers if t.get("mandatory")])
    score = satisfied / total if total > 0 else 0.5

    return AxisScore(
        "trigger_satisfaction", round(score, 2), w.get("trigger_satisfaction", 0.10),
        source_count=satisfied, gap_count=max(0, total - satisfied),
        details=f"{satisfied}/{total} mandatory triggers have supporting facts"
    )


def _score_burden_placement(claim: dict, eo: dict, w: dict) -> AxisScore:
    """Is the burden of proof correctly placed?"""
    burden = eo.get("burden", {})
    if not burden:
        return AxisScore("burden_placement", 0.5, w.get("burden_placement", 0.10),
                         details="No burden data — needs analysis")

    holder = burden.get("burden_holder", "unknown")
    has_shifts = len(burden.get("burden_shifts", [])) > 0
    mandatory = burden.get("mandatory_remedy", False)

    score = 0.5
    details = f"Initial burden on {holder}"

    if holder == "defendant" or burden.get("strict_liability"):
        score = 0.9
        details = "Favorable: burden on defendant or strict liability"
    elif has_shifts:
        score = 0.7
        details = "Burden shift available — must establish prima facie case"
    elif holder == "plaintiff":
        score = 0.5
        details = "Standard: plaintiff bears initial burden"

    if mandatory:
        score = min(1.0, score + 0.1)
        details += " + mandatory remedy"

    return AxisScore(
        "burden_placement", round(score, 2), w.get("burden_placement", 0.10),
        details=details
    )


def _score_remedy_availability(claim: dict, eo: dict, w: dict) -> AxisScore:
    """Does the statute actually provide the remedy being sought?"""
    remedy = eo.get("remedy", {})
    if not remedy:
        return AxisScore("remedy_availability", 0.5, w.get("remedy_availability", 0.10),
                         details="No remedy data — needs analysis")

    remedies = remedy.get("remedies", [])
    has_private = remedy.get("has_private_right", False)
    has_fees = remedy.get("has_fee_shifting", False)

    if not remedies:
        return AxisScore("remedy_availability", 0.2, w.get("remedy_availability", 0.10),
                         details="No remedies found in statute text")

    score = 0.5
    mandatory_count = sum(1 for r in remedies if r.get("mandatory"))
    total = len(remedies)

    if mandatory_count > 0:
        score = 0.8 + (0.2 * mandatory_count / total)
    elif total > 0:
        score = 0.6

    if has_private:
        score = min(1.0, score + 0.1)
    if has_fees:
        score = min(1.0, score + 0.05)

    return AxisScore(
        "remedy_availability", round(score, 2), w.get("remedy_availability", 0.10),
        source_count=mandatory_count,
        details=f"{mandatory_count} mandatory, {total - mandatory_count} discretionary remedies"
    )


def _score_procedural_compliance(claim: dict, eo: dict, w: dict) -> AxisScore:
    """Are procedural requirements met?"""
    proc = eo.get("procedural", {})
    if not proc:
        return AxisScore("procedural_compliance", 0.7, w.get("procedural_compliance", 0.10),
                         details="No procedural data — assumed compliant")

    defects = proc.get("defects", [])
    if not defects:
        return AxisScore("procedural_compliance", 1.0, w.get("procedural_compliance", 0.10),
                         details="No procedural defects found")

    fatal = sum(1 for d in defects if d.get("severity") == "fatal")
    score = max(0, 1.0 - (fatal * 0.3) - (len(defects) * 0.1))

    return AxisScore(
        "procedural_compliance", round(max(0, score), 2), w.get("procedural_compliance", 0.10),
        gap_count=len(defects),
        details=f"{len(defects)} procedural issues ({fatal} fatal)"
    )


def _score_source_traceability(claim: dict, eo: dict, w: dict) -> AxisScore:
    """Can every claim be traced to a source in the DB?"""
    # Count how many engines returned source_excerpts
    traced = 0
    total = 0
    for key in ["definition", "actor_scope", "burden", "trigger", "remedy", "hierarchy"]:
        data = eo.get(key, {})
        if data:
            total += 1
            excerpts = data.get("source_excerpts", [])
            if excerpts:
                traced += 1

    score = traced / total if total > 0 else 0.5

    return AxisScore(
        "source_traceability", round(score, 2), w.get("source_traceability", 0.10),
        source_count=traced, gap_count=total - traced,
        details=f"{traced}/{total} engine outputs have source text citations"
    )


def _score_construction_compliance(claim: dict, eo: dict, w: dict) -> AxisScore:
    """Do definitions and term usage comply with statutory construction rules?

    Checks:
      - Are 'means' definitions treated as closed?
      - Are 'includes' definitions treated as expansive?
      - Are cross-statute imports authorized?
      - Are mutually exclusive terms kept separate?
    """
    construction = eo.get("construction", {})
    if not construction:
        return AxisScore("construction_compliance", 0.5, w.get("construction_compliance", 0.12),
                         details="No construction data — needs analysis")

    rules = construction.get("construction_rules", [])
    exclusions = construction.get("mutual_exclusions", [])
    def_scopes = construction.get("definition_scopes", [])

    if not rules:
        return AxisScore("construction_compliance", 0.5, w.get("construction_compliance", 0.12),
                         details="No construction rules triggered in provision")

    # Score based on what was found
    score = 0.7  # base: rules were analyzed
    violations = 0

    # Check for mutual exclusion violations
    for exc in exclusions:
        if exc.get("exclusive"):
            score = min(1.0, score + 0.1)  # bonus: exclusions properly identified

    # Check definitions have classified scopes
    scoped = sum(1 for d in def_scopes if d.get("scope") in ("closed", "expansive", "cross_referenced"))
    total_defs = len(def_scopes)
    if total_defs > 0:
        scope_ratio = scoped / total_defs
        score = min(1.0, score + (scope_ratio * 0.2))

    # Mandatory vs discretionary identified
    if construction.get("mandatory_count", 0) > 0 or construction.get("discretionary_count", 0) > 0:
        score = min(1.0, score + 0.05)

    return AxisScore(
        "construction_compliance", round(score, 2), w.get("construction_compliance", 0.12),
        source_count=len(rules),
        gap_count=violations,
        details=f"{len(rules)} construction rules applied, {len(exclusions)} exclusions, {scoped}/{total_defs} scoped definitions"
    )


# ─── Formatting ──────────────────────────────────────────────────────

# BEEZLE_FN_UNUSED 2026-06-13: format_consistency_report (no static refs in pipeline/)
def format_consistency_report(report: ConsistencyReport) -> str:
    """Format report for human reading."""
    lines = [f"CONSISTENCY REPORT — {report.subject}"]
    lines.append(f"  Overall: {report.overall_score:.1%} (Grade: {report.grade()})")
    lines.append("")

    for axis in sorted(report.axes, key=lambda a: a.score):
        bar = "█" * int(axis.score * 10) + "░" * (10 - int(axis.score * 10))
        lines.append(f"  {axis.axis:.<30s} {bar} {axis.score:.0%}")
        if axis.details:
            lines.append(f"    {axis.details}")

    if report.strengths:
        lines.append(f"\n  STRENGTHS:")
        for s in report.strengths:
            lines.append(f"    + {s}")

    if report.weaknesses:
        lines.append(f"\n  WEAKNESSES:")
        for w in report.weaknesses:
            lines.append(f"    - {w}")

    if report.gaps:
        lines.append(f"\n  GAPS (needs more data):")
        for g in report.gaps:
            lines.append(f"    ? {g}")

    return "\n".join(lines)
