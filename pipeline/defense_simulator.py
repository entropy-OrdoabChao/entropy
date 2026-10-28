"""
BLOCK_9B_HERMES_SKILLS_PORT_2_OF_2
Defense Simulator — adversarial element-by-element attack. Ported from hermes defense-simulator skill.

Simulates strongest possible defense response. Tests:
  1. ELEMENT attacks   — fact disputed / alternative interpretation
  2. AUTHORITY attacks — overruled / distinguished / wrong jurisdiction
  3. FACT attacks      — counter-showing available
  4. LOGIC attacks     — chain breaks / unstated premise

Each attack rated WEAK / MODERATE / STRONG.
If any STRONG attack survives → argument NOT ready for filing.

NO LLM. Rule-based pattern matching. Existing pipeline/authority_attack_agent.py
remains untouched (it does conclusion addendums; this does adversarial scenarios).
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Optional, Literal

from core.logger import get_logger

log = get_logger("defense_simulator")

Strength = Literal["WEAK", "MODERATE", "STRONG"]
AttackCategory = Literal["ELEMENT", "AUTHORITY", "FACT", "LOGIC"]


@dataclass
class DefenseAttack:
    category: AttackCategory
    target: str               # what's being attacked (element name / citation / fact / chain link)
    attack: str               # the defense's argument
    strength: Strength
    your_rebuttal_required: bool   # True if attack must be rebutted before filing
    rebuttal_authority_needed: list[str] = field(default_factory=list)
    notes: str = ""


@dataclass
class SimulationResult:
    attacks: list[DefenseAttack] = field(default_factory=list)
    total: int = 0
    by_strength: dict = field(default_factory=dict)
    blocking_count: int = 0
    ready_for_filing: bool = False
    summary: str = ""


# ──────────── pattern catalogs (rule-based, no LLM) ────────────

# Common element-attack patterns: keywords that suggest the element is disputable
DISPUTED_KEYWORDS = (
    "alleged", "claims", "contends", "asserts", "purports", "supposedly",
    "approximately", "around", "about", "believed to", "to my knowledge",
)

# Authorities that have been narrowed/overruled/limited (stub list — extend from DB)
KNOWN_LIMITED_AUTHORITIES = {
    "Mathews v. Eldridge": "Limited in due process context to administrative proceedings",
    "Lujan v. Defenders of Wildlife": "Distinguished where injury is concrete and imminent (Spokeo, TransUnion)",
    "Twombly": "Applied broadly but Iqbal extended to all civil cases",
}


def attack_elements(elements: list[dict]) -> list[DefenseAttack]:
    """For each SATISFIED element, generate plausible defense attacks.

    Each element dict: {name, satisfied:bool, fact_basis:str, evidence:list[str]}
    """
    attacks: list[DefenseAttack] = []
    for el in elements:
        name = el.get("name", "unnamed_element")
        if not el.get("satisfied"):
            continue  # only satisfied elements get attacked here

        fact = (el.get("fact_basis") or "").lower()
        evidence = el.get("evidence", [])

        # Check for hedging language → moderate dispute risk
        hedges = [k for k in DISPUTED_KEYWORDS if k in fact]
        if hedges:
            attacks.append(DefenseAttack(
                category="ELEMENT",
                target=name,
                attack=f"Defense will argue element '{name}' is disputed: fact uses hedging ({', '.join(hedges[:3])})",
                strength="MODERATE",
                your_rebuttal_required=True,
                rebuttal_authority_needed=["specific factual proof — affidavit, document, or admission"],
                notes="Replace hedged language with declarative, evidenced facts.",
            ))

        # No evidence cited → strong attack
        if not evidence:
            attacks.append(DefenseAttack(
                category="ELEMENT",
                target=name,
                attack=f"Defense will argue element '{name}' is unsupported: no evidence cited",
                strength="STRONG",
                your_rebuttal_required=True,
                rebuttal_authority_needed=["primary evidence (exhibit, affidavit, admission, judicial notice)"],
                notes="Element fails Twombly/Iqbal plausibility without evidence.",
            ))
        elif len(evidence) == 1:
            attacks.append(DefenseAttack(
                category="ELEMENT",
                target=name,
                attack=f"Defense will argue element '{name}' rests on a single uncorroborated source",
                strength="WEAK",
                your_rebuttal_required=False,
                notes="Single source acceptable if from authoritative origin (court order, statute, admission).",
            ))

    return attacks


def attack_authorities(authorities: list[str], target_jurisdiction: str = "") -> list[DefenseAttack]:
    """For each cited authority, check known limitations + jurisdiction match."""
    attacks: list[DefenseAttack] = []
    for cite in authorities:
        cite_norm = cite.strip()

        # Check the limited-authorities catalog
        for key, limitation in KNOWN_LIMITED_AUTHORITIES.items():
            if key.lower() in cite_norm.lower():
                attacks.append(DefenseAttack(
                    category="AUTHORITY",
                    target=cite_norm,
                    attack=f"Defense will argue {key} has been limited: {limitation}",
                    strength="MODERATE",
                    your_rebuttal_required=True,
                    rebuttal_authority_needed=[f"post-limiting case applying {key} to facts like ours"],
                    notes="Cite the most recent SCOTUS/circuit case applying this rule.",
                ))

        # Cross-jurisdiction citation flag
        if target_jurisdiction:
            tj = target_jurisdiction.upper()
            if tj == "EDPA" and not any(s in cite_norm for s in ("3d Cir", "E.D. Pa", "U.S.", "F.R.C.P", "FRCP")):
                if not any(s in cite_norm for s in ("Pa.R.", "Pennsylvania")):
                    attacks.append(DefenseAttack(
                        category="AUTHORITY",
                        target=cite_norm,
                        attack=f"Defense will argue {cite_norm} is not binding in EDPA (out-of-circuit / out-of-jurisdiction)",
                        strength="MODERATE",
                        your_rebuttal_required=True,
                        rebuttal_authority_needed=["Third Circuit or EDPA case on point", "or persuasive-authority justification"],
                    ))

    return attacks


def attack_facts(facts: list[str]) -> list[DefenseAttack]:
    """Scan facts for vagueness / unsourced assertions."""
    attacks: list[DefenseAttack] = []
    for i, f in enumerate(facts):
        fl = f.lower()
        hedges = [k for k in DISPUTED_KEYWORDS if k in fl]
        if hedges:
            attacks.append(DefenseAttack(
                category="FACT",
                target=f"fact_{i+1}",
                attack=f"Defense will dispute fact #{i+1}: hedging language ({', '.join(hedges[:3])})",
                strength="MODERATE",
                your_rebuttal_required=True,
                notes=f"Original fact: '{f[:120]}'",
            ))
    return attacks


def attack_logic_chain(logic_chain: list[dict]) -> list[DefenseAttack]:
    """For each link in the chain, check that premises are supported.

    Each link dict: {if_clause, then_clause, authority}
    """
    attacks: list[DefenseAttack] = []
    for i, link in enumerate(logic_chain):
        if_c = link.get("if_clause", "")
        then_c = link.get("then_clause", "")
        auth = link.get("authority", "")

        if not auth:
            attacks.append(DefenseAttack(
                category="LOGIC",
                target=f"chain_link_{i+1}",
                attack=f"Defense will argue chain link #{i+1} ('{if_c[:40]}...→{then_c[:40]}...') has no cited authority",
                strength="STRONG",
                your_rebuttal_required=True,
                rebuttal_authority_needed=["statute, case, or rule supporting this inference"],
            ))
        # Detect unstated premises
        if " must " in if_c.lower() or " shall " in if_c.lower():
            if "must" not in auth.lower() and "shall" not in auth.lower():
                attacks.append(DefenseAttack(
                    category="LOGIC",
                    target=f"chain_link_{i+1}",
                    attack=f"Defense will argue 'must/shall' is not in cited authority — premise overstates",
                    strength="MODERATE",
                    your_rebuttal_required=True,
                    notes=f"If-clause: {if_c[:120]}",
                ))
    return attacks


def simulate(
    elements: Optional[list[dict]] = None,
    authorities: Optional[list[str]] = None,
    facts: Optional[list[str]] = None,
    logic_chain: Optional[list[dict]] = None,
    target_jurisdiction: str = "",
) -> dict:
    """Run all attack categories. Returns SimulationResult as dict."""
    result = SimulationResult()
    result.attacks.extend(attack_elements(elements or []))
    result.attacks.extend(attack_authorities(authorities or [], target_jurisdiction))
    result.attacks.extend(attack_facts(facts or []))
    result.attacks.extend(attack_logic_chain(logic_chain or []))

    result.total = len(result.attacks)
    by_str: dict[str, int] = {}
    blocking = 0
    for a in result.attacks:
        by_str[a.strength] = by_str.get(a.strength, 0) + 1
        if a.your_rebuttal_required:
            blocking += 1
    result.by_strength = by_str
    result.blocking_count = blocking
    result.ready_for_filing = blocking == 0
    if result.ready_for_filing:
        result.summary = f"No blocking attacks ({result.total} total). Argument is filing-ready."
    else:
        result.summary = (
            f"{blocking} blocking attacks must be rebutted before filing "
            f"(STRONG: {by_str.get('STRONG',0)}, MODERATE: {by_str.get('MODERATE',0)}, WEAK: {by_str.get('WEAK',0)})"
        )

    log.info(f"defense_simulate: {result.total} attacks, {blocking} blocking, ready={result.ready_for_filing}")
    return {
        "ready_for_filing": result.ready_for_filing,
        "total": result.total,
        "by_strength": result.by_strength,
        "blocking_count": result.blocking_count,
        "summary": result.summary,
        "attacks": [asdict(a) for a in result.attacks],
    }