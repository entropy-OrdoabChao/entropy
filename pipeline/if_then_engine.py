"""
pipeline/if_then_engine.py — IF/THEN Logic Chain Builder
No LLM. Builds deterministic logic chains from DB rules.
Reused from legacy pipeline/if_then_engine.py.
"""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class LogicLink:
    rule_text: str
    authority: str
    condition: str    # IF clause
    conclusion: str   # THEN clause
    facts_matched: list[str] = field(default_factory=list)
    satisfied: bool = False


# BEEZLE_FN_UNUSED 2026-06-13: build_logic_chain (no static refs in pipeline/)
def build_logic_chain(rule_text: str, rule_authority: str,
                      facts: list[str], issue: str = "") -> list[LogicLink]:
    """Build an IF/THEN chain from a rule and facts."""
    links = []
    requirements = _extract_requirements(rule_text)

    for req in requirements:
        condition = f"IF {req}"
        conclusion = _build_then_clause(req, issue)
        matched = [f for f in facts if _fact_matches_requirement(f, req)]

        links.append(LogicLink(
            rule_text=rule_text[:200],
            authority=rule_authority,
            condition=condition,
            conclusion=conclusion,
            facts_matched=matched,
            satisfied=len(matched) > 0,
        ))

    return links


# BEEZLE_FN_UNUSED 2026-06-13: format_chain_block (no static refs in pipeline/)
def format_chain_block(chain: list[LogicLink]) -> str:
    """Format a logic chain for inclusion in a legal document."""
    if not chain:
        return ""
    lines = []
    for i, link in enumerate(chain, 1):
        status = "✓" if link.satisfied else "✗"
        lines.append(f"{i}. [{status}] {link.condition}")
        lines.append(f"   THEN: {link.conclusion}")
        lines.append(f"   Authority: {link.authority}")
        if link.facts_matched:
            for f in link.facts_matched[:3]:
                lines.append(f"   Fact: {f[:100]}")
        lines.append("")
    return "\n".join(lines)


def _extract_requirements(rule_text: str) -> list[str]:
    """Extract requirement clauses from a rule."""
    import re
    reqs = []
    # "shall" requirements
    for m in re.finditer(r'shall\s+(.+?)(?:\.|;|$)', rule_text, re.IGNORECASE):
        reqs.append(m.group(1).strip())
    # "must" requirements
    for m in re.finditer(r'must\s+(.+?)(?:\.|;|$)', rule_text, re.IGNORECASE):
        reqs.append(m.group(1).strip())
    # "required to" requirements
    for m in re.finditer(r'required\s+to\s+(.+?)(?:\.|;|$)', rule_text, re.IGNORECASE):
        reqs.append(m.group(1).strip())

    if not reqs:
        # Fallback: treat entire rule as one requirement
        reqs = [rule_text[:200]]

    return reqs[:10]


def _build_then_clause(requirement: str, issue: str) -> str:
    """Build a plain-language THEN consequence from a requirement.

    Every requirement reaching here was extracted from mandatory ("shall"/
    "must"/"required to") rule language, so it IS a binding duty. The THEN states
    the consequence of breach in plain terms a non-lawyer can follow — and frames
    it as non-discretionary (the court must address it), not a generic echo.
    """
    req = (requirement or "").strip().rstrip(".")
    if not req:
        return "the governing duty was not met, which is a violation the court must address."
    # lower-case the lead-in unless it starts with a proper noun-ish capital run
    lead = req[0].lower() + req[1:] if req[:2].lower() == req[:2] or not req[:1].isupper() else req
    return (f"the obligated party was required to {lead}; "
            f"failure to do so is a violation that the court must address as a "
            f"matter of law, not discretion.")


def _fact_matches_requirement(fact: str, requirement: str) -> bool:
    """Check if a fact matches a requirement. Keyword overlap."""
    fact_words = set(fact.lower().split())
    req_words = set(requirement.lower().split())
    stops = {"a", "an", "the", "of", "or", "and", "is", "was", "to", "in", "for", "shall", "must"}
    req_words -= stops
    if not req_words:
        return False
    overlap = fact_words & req_words
    return len(overlap) >= 2
