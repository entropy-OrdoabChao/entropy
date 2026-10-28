"""
BLOCK_9A_HERMES_SKILLS_PORT_1_OF_2
Four Gates analysis. Ported from hermes four-gates-analysis skill.

Sequential gates — first FAIL stops the pipeline:
  1. JURISDICTION   (delegates to jurisdiction_check.run)
  2. STANDING       (injury / causation / redressability)
  3. CAUSE OF ACTION (statute or common-law claim with elements)
  4. PLAUSIBILITY   (Twombly/Iqbal — facts make claim plausible)

Pure logic. Returns structured per-gate verdict + overall.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Optional, Literal

from core.logger import get_logger
from pipeline.jurisdiction_check import run as run_jurisdiction

log = get_logger("four_gates")

Verdict = Literal["PASS", "FAIL", "UNCLEAR"]


@dataclass
class GateResult:
    name: str
    verdict: Verdict = "UNCLEAR"
    reason: str = ""
    authority: list[str] = field(default_factory=list)
    sub_checks: dict = field(default_factory=dict)


@dataclass
class FourGatesResult:
    gates: list[GateResult] = field(default_factory=list)
    overall: Verdict = "UNCLEAR"
    failed_at: Optional[str] = None
    summary: str = ""


def gate_jurisdiction(jurisdiction_inputs: dict) -> GateResult:
    j = run_jurisdiction(**jurisdiction_inputs)
    return GateResult(
        name="JURISDICTION",
        verdict=j["overall"],
        reason="; ".join(j["blocking_issues"]) if j["blocking_issues"] else "All jurisdiction checks passed",
        authority=j["subject_matter_authority"] + j["personal_authority"] + j["removal_authority"],
        sub_checks=j,
    )


def gate_standing(
    injury_in_fact: bool,
    injury_concrete_particularized: bool,
    causation_traceable: bool,
    redressable_by_court: bool,
    injury_description: str = "",
) -> GateResult:
    if not injury_in_fact:
        return GateResult("STANDING", "FAIL", "No injury in fact alleged",
                          ["Lujan v. Defenders of Wildlife, 504 U.S. 555 (1992)"])
    if not injury_concrete_particularized:
        return GateResult("STANDING", "FAIL",
                          "Injury not concrete and particularized",
                          ["Spokeo v. Robins, 578 U.S. 330 (2016)",
                           "TransUnion v. Ramirez, 594 U.S. ___ (2021)"])
    if not causation_traceable:
        return GateResult("STANDING", "FAIL",
                          "Injury not fairly traceable to defendant's conduct",
                          ["Lujan, 504 U.S. 555 (1992)"])
    if not redressable_by_court:
        return GateResult("STANDING", "FAIL",
                          "Injury not redressable by favorable decision",
                          ["Lujan, 504 U.S. 555 (1992)"])
    return GateResult("STANDING", "PASS",
                      f"Injury, causation, redressability satisfied: {injury_description}".strip(": "),
                      ["Lujan v. Defenders of Wildlife, 504 U.S. 555 (1992)",
                       "Spokeo v. Robins, 578 U.S. 330 (2016)"])


def gate_cause_of_action(
    statute_or_common_law: str = "",
    elements_identified: bool = False,
    elements_count: int = 0,
    private_right_of_action: bool = True,
) -> GateResult:
    if not statute_or_common_law.strip():
        return GateResult("CAUSE_OF_ACTION", "FAIL",
                          "No statute or common-law theory identified",
                          [])
    if not elements_identified or elements_count == 0:
        return GateResult("CAUSE_OF_ACTION", "FAIL",
                          f"Elements of {statute_or_common_law} not identified",
                          [statute_or_common_law])
    if not private_right_of_action:
        return GateResult("CAUSE_OF_ACTION", "FAIL",
                          f"{statute_or_common_law} provides no private right of action",
                          ["Alexander v. Sandoval, 532 U.S. 275 (2001)",
                           "Cort v. Ash, 422 U.S. 66 (1975)"])
    return GateResult("CAUSE_OF_ACTION", "PASS",
                      f"{statute_or_common_law}: {elements_count} elements identified, private right of action confirmed",
                      [statute_or_common_law])


def gate_plausibility(
    elements_satisfied: int,
    elements_total: int,
    facts_specific: bool = True,
    threadbare_recitals_only: bool = False,
) -> GateResult:
    if elements_total == 0:
        return GateResult("PLAUSIBILITY", "FAIL",
                          "No elements to evaluate",
                          ["Bell Atlantic v. Twombly, 550 U.S. 544 (2007)"])
    if threadbare_recitals_only or not facts_specific:
        return GateResult("PLAUSIBILITY", "FAIL",
                          "Threadbare recitals; specific facts required for plausibility",
                          ["Bell Atlantic v. Twombly, 550 U.S. 544 (2007)",
                           "Ashcroft v. Iqbal, 556 U.S. 662 (2009)"])
    if elements_satisfied < elements_total:
        return GateResult("PLAUSIBILITY", "FAIL",
                          f"Only {elements_satisfied}/{elements_total} elements supported by facts",
                          ["Bell Atlantic v. Twombly, 550 U.S. 544 (2007)",
                           "Ashcroft v. Iqbal, 556 U.S. 662 (2009)"])
    return GateResult("PLAUSIBILITY", "PASS",
                      f"All {elements_total} elements supported by specific factual allegations",
                      ["Bell Atlantic v. Twombly, 550 U.S. 544 (2007)",
                       "Ashcroft v. Iqbal, 556 U.S. 662 (2009)"])


def run(
    jurisdiction_inputs: dict,
    standing_inputs: dict,
    cause_inputs: dict,
    plausibility_inputs: dict,
    short_circuit: bool = True,
) -> dict:
    """Run all four gates. If short_circuit=True, stops at first FAIL."""
    result = FourGatesResult()
    gates_fns = [
        ("JURISDICTION", gate_jurisdiction, jurisdiction_inputs),
        ("STANDING", gate_standing, standing_inputs),
        ("CAUSE_OF_ACTION", gate_cause_of_action, cause_inputs),
        ("PLAUSIBILITY", gate_plausibility, plausibility_inputs),
    ]
    for name, fn, inputs in gates_fns:
        g = fn(**inputs) if name != "JURISDICTION" else fn(inputs)
        result.gates.append(g)
        if g.verdict == "FAIL":
            result.failed_at = name
            result.overall = "FAIL"
            result.summary = f"Failed at gate {name}: {g.reason}"
            log.info(f"four_gates FAIL @ {name}: {g.reason[:80]}")
            if short_circuit:
                return _to_dict(result)
    if result.overall != "FAIL":
        if any(g.verdict == "UNCLEAR" for g in result.gates):
            result.overall = "UNCLEAR"
            result.summary = "One or more gates UNCLEAR; resolve before filing"
        else:
            result.overall = "PASS"
            result.summary = "All four gates passed; claim viable for filing"
    log.info(f"four_gates overall={result.overall}")
    return _to_dict(result)


def _to_dict(r: FourGatesResult) -> dict:
    return {
        "overall": r.overall,
        "failed_at": r.failed_at,
        "summary": r.summary,
        "gates": [asdict(g) for g in r.gates],
    }