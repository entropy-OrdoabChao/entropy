"""
pipeline/actor_scope_engine.py — Actor-Scope Engine

DETERMINISTIC. NO LLM. NO INTERPRETATION.

The critical engine: for every provision, determines:
  - WHO has the duty (actor classification)
  - WHO can be penalized (target classification)
  - WHETHER there is a MISMATCH (duty on government, penalty on citizen)
  - WHAT the statutory text actually says about scope

This is what catches government overreach: when an agency applies
a penalty provision meant for one class to a different class.

Usage:
    from pipeline.actor_scope_engine import analyze_actor_scope, detect_mismatch

    result = analyze_actor_scope(statute_text, citation="5 U.S.C. § 552a")
"""

import re
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Literal
from core.logger import get_logger

log = get_logger("actor_scope_engine")

# ─── Data Structures ────────────────────────────────────────────────

ActorClass = Literal["any_person", "individual", "citizen",
                      "agency", "officer", "employee",
                      "entity", "corporation", "bank",
                      "contractor", "grantee",
                      "secretary", "commissioner", "director",
                      "court", "judge",
                      "broad", "restricted", "unknown"]

ScopeType = Literal["universal", "government_only", "private_only",
                     "entity_only", "mixed", "unknown"]


@dataclass
class ActorBinding:
    """A single actor-action binding from statutory text."""
    actor_text: str          # verbatim actor phrase from statute
    actor_class: ActorClass
    action: str              # what they must/may do
    mandatory: bool          # "shall" vs "may"
    is_duty: bool            # True = imposes obligation
    is_penalty: bool         # True = imposes liability/penalty
    is_right: bool           # True = grants right/entitlement
    source_text: str = ""
    line_offset: int = 0


@dataclass
class ScopeMismatch:
    """A detected mismatch between duty holder and penalty target."""
    duty_actor: str
    duty_class: ActorClass
    penalty_actor: str
    penalty_class: ActorClass
    duty_text: str
    penalty_text: str
    explanation: str


@dataclass
class ActorScopeAnalysis:
    """Complete actor-scope analysis for a provision."""
    citation: str
    scope_type: ScopeType = "unknown"
    actors: List[ActorBinding] = field(default_factory=list)
    duty_holders: List[str] = field(default_factory=list)
    penalty_targets: List[str] = field(default_factory=list)
    right_holders: List[str] = field(default_factory=list)
    mismatches: List[ScopeMismatch] = field(default_factory=list)
    has_mismatch: bool = False
    source_excerpts: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_frontmatter(self) -> dict:
        return {
            "scope_type": str(self.scope_type),
            "duty_holders": [str(d) for d in self.duty_holders],
            "penalty_targets": [str(p) for p in self.penalty_targets],
            "right_holders": [str(r) for r in self.right_holders],
            "has_scope_mismatch": self.has_mismatch,
            "actors": [
                {"text": str(a.actor_text), "class": str(a.actor_class),
                 "duty": a.is_duty, "penalty": a.is_penalty}
                for a in self.actors
            ],
        }


# ─── Actor Classification ───────────────────────────────────────────

# Broad scope indicators (applies to everyone)
BROAD_ACTORS = [
    (r"\bany\s+person\b", "any_person"),
    (r"\bwhoever\b", "any_person"),
    (r"\bno\s+person\b", "any_person"),
    (r"\bevery\s+person\b", "any_person"),
    (r"\ba\s+person\b", "any_person"),
    (r"\bany\s+individual\b", "individual"),
    (r"\bany\s+entity\b", "entity"),
]

# Government-restricted actors
GOVERNMENT_ACTORS = [
    (r"\b(?:the\s+)?(?:head\s+of\s+(?:an?\s+)?)?agency\b", "agency"),
    (r"\b(?:the\s+)?secretary\b", "secretary"),
    (r"\b(?:the\s+)?(?:comptroller|commissioner|administrator|director)\b", "director"),
    (r"\b(?:an?\s+)?(?:officer|official)\s+(?:of\s+(?:the\s+)?(?:united\s+states|government))", "officer"),
    (r"\b(?:an?\s+)?(?:employee|officer)\s+(?:of\s+(?:an?\s+)?agency)", "employee"),
    (r"\b(?:the\s+)?(?:department|bureau|commission|board|office|administration)\b", "agency"),
    (r"\b(?:federal|government)\s+(?:employee|officer|official|agent)\b", "officer"),
]

# Private/entity actors
PRIVATE_ACTORS = [
    (r"\b(?:the\s+)?(?:individual|citizen|resident|applicant|requester)\b", "individual"),
    (r"\b(?:the\s+)?(?:bank|financial\s+institution|insured\s+(?:institution|depository))\b", "bank"),
    (r"\b(?:the\s+)?(?:corporation|company|firm|business|enterprise)\b", "corporation"),
    (r"\b(?:the\s+)?(?:employer|contractor|grantee|licensee)\b", "contractor"),
]

# Duty language
DUTY_PATTERNS = [
    (r"shall\s+(?!not\b)(\w+(?:\s+\w+){0,6})", True),   # "shall [verb]" = mandatory duty
    (r"is\s+required\s+to\s+(\w+(?:\s+\w+){0,6})", True),
    (r"must\s+(\w+(?:\s+\w+){0,6})", True),
    (r"has\s+(?:a\s+|the\s+)?duty\s+to\s+(\w+(?:\s+\w+){0,6})", True),
    (r"shall\s+not\s+(\w+(?:\s+\w+){0,6})", True),       # prohibition = duty not to
    (r"may\s+not\s+(\w+(?:\s+\w+){0,6})", True),          # prohibition
]

# Penalty/liability language
PENALTY_PATTERNS = [
    r"shall\s+be\s+(?:liable|subject\s+to|guilty|fined|imprisoned|punished)",
    r"(?:liable|subject)\s+(?:to|for)\s+(?:a\s+)?(?:fine|penalty|forfeiture|damages)",
    r"(?:fine|penalty|forfeiture)\s+(?:of\s+)?(?:not\s+(?:more|less)\s+than\s+)?\$",
    r"imprisoned\s+(?:for\s+)?(?:not\s+(?:more|less)\s+than)",
    r"(?:criminal|civil)\s+(?:penalty|liability|fine)",
    r"shall\s+(?:pay|forfeit|be\s+assessed)",
]

# Right/entitlement language
RIGHT_PATTERNS = [
    r"(?:shall\s+)?(?:have\s+the\s+right|be\s+entitled)\s+to",
    r"(?:shall|may)\s+(?:bring|commence|maintain|file)\s+(?:a\s+)?(?:civil\s+)?(?:action|suit|proceeding)",
    r"(?:is|shall\s+be)\s+entitled\s+to\s+(?:recover|receive|obtain)",
    r"(?:right|entitlement)\s+(?:to|of)\s+(?:access|review|hearing|notice|appeal)",
]


# ─── Core Analysis ───────────────────────────────────────────────────

def analyze_actor_scope(text: str, citation: str = "") -> ActorScopeAnalysis:
    """
    Analyze actor-scope relationships in statutory/regulatory text.

    Extracts who has duties, who faces penalties, who has rights,
    and detects mismatches where penalty targets differ from duty holders.
    """
    text_lower = text.lower()
    result = ActorScopeAnalysis(citation=citation)

    # Split into sentences for granular analysis
    sentences = re.split(r'(?<=[.;])\s+', text)

    for sent_idx, sentence in enumerate(sentences):
        sent_lower = sentence.lower()

        # Detect actors in this sentence
        actors_found = []
        for patterns, category in [
            (BROAD_ACTORS, "broad"),
            (GOVERNMENT_ACTORS, "government"),
            (PRIVATE_ACTORS, "private"),
        ]:
            for pattern, actor_class in patterns:
                match = re.search(pattern, sent_lower)
                if match:
                    actors_found.append((match.group(), actor_class, category))

        if not actors_found:
            continue

        # Check what this sentence does: duty, penalty, or right
        is_duty = any(re.search(p, sent_lower) for p, _ in DUTY_PATTERNS)
        is_penalty = any(re.search(p, sent_lower) for p in PENALTY_PATTERNS)
        is_right = any(re.search(p, sent_lower) for p in RIGHT_PATTERNS)

        for actor_text, actor_class, category in actors_found:
            binding = ActorBinding(
                actor_text=actor_text,
                actor_class=actor_class,
                action=sentence[:100].strip(),
                mandatory="shall" in sent_lower or "must" in sent_lower,
                is_duty=is_duty,
                is_penalty=is_penalty,
                is_right=is_right,
                source_text=sentence[:120].strip(),
                line_offset=sent_idx,
            )
            result.actors.append(binding)

            if is_duty:
                if actor_text not in result.duty_holders:
                    result.duty_holders.append(actor_text)
            if is_penalty:
                if actor_text not in result.penalty_targets:
                    result.penalty_targets.append(actor_text)
            if is_right:
                if actor_text not in result.right_holders:
                    result.right_holders.append(actor_text)

            result.source_excerpts.append(sentence[:120].strip())

    # Determine overall scope
    has_gov = any(a.actor_class in ("agency", "officer", "employee", "secretary",
                                     "director", "commissioner") for a in result.actors)
    has_private = any(a.actor_class in ("any_person", "individual", "citizen",
                                         "bank", "corporation", "contractor") for a in result.actors)
    has_broad = any(a.actor_class == "any_person" for a in result.actors)

    if has_broad:
        result.scope_type = "universal"
    elif has_gov and has_private:
        result.scope_type = "mixed"
    elif has_gov:
        result.scope_type = "government_only"
    elif has_private:
        result.scope_type = "private_only"

    # Detect mismatches
    result.mismatches = detect_mismatch(result)
    result.has_mismatch = len(result.mismatches) > 0

    # Deduplicate excerpts
    seen = set()
    result.source_excerpts = [e for e in result.source_excerpts if e not in seen and not seen.add(e)]

    return result


def detect_mismatch(analysis: ActorScopeAnalysis) -> List[ScopeMismatch]:
    """
    Detect mismatches where duty holders differ from penalty targets.

    The critical check: if the statute says "the agency shall..."
    but the penalty falls on "any person" or vice versa.
    """
    mismatches = []

    duty_actors = [a for a in analysis.actors if a.is_duty]
    penalty_actors = [a for a in analysis.actors if a.is_penalty]

    gov_classes = {"agency", "officer", "employee", "secretary", "director", "commissioner"}
    private_classes = {"any_person", "individual", "citizen", "bank", "corporation", "contractor"}

    for duty in duty_actors:
        for penalty in penalty_actors:
            duty_is_gov = duty.actor_class in gov_classes
            penalty_is_gov = penalty.actor_class in gov_classes
            duty_is_private = duty.actor_class in private_classes
            penalty_is_private = penalty.actor_class in private_classes

            # Mismatch: duty on government, penalty on private
            if duty_is_gov and penalty_is_private:
                mismatches.append(ScopeMismatch(
                    duty_actor=duty.actor_text,
                    duty_class=duty.actor_class,
                    penalty_actor=penalty.actor_text,
                    penalty_class=penalty.actor_class,
                    duty_text=duty.source_text,
                    penalty_text=penalty.source_text,
                    explanation=(
                        f"MISMATCH: Duty is on '{duty.actor_text}' ({duty.actor_class}) "
                        f"but penalty targets '{penalty.actor_text}' ({penalty.actor_class}). "
                        f"The statute imposes the obligation on the government actor "
                        f"but the penalty provision targets a different class."
                    ),
                ))

            # Mismatch: duty on private, penalty on broader class
            elif duty_is_private and not penalty_is_private and penalty.actor_class == "any_person":
                # This is less concerning but still worth flagging
                pass

    return mismatches


# BEEZLE_FN_UNUSED 2026-06-13: format_actor_scope_block (no static refs in pipeline/)
def format_actor_scope_block(analysis: ActorScopeAnalysis) -> str:
    """Format actor-scope analysis for filings."""
    lines = [f"ACTOR-SCOPE ANALYSIS — {analysis.citation}"]
    lines.append(f"  Scope: {analysis.scope_type}")

    if analysis.duty_holders:
        lines.append(f"  Duty holders: {', '.join(analysis.duty_holders)}")
    if analysis.penalty_targets:
        lines.append(f"  Penalty targets: {', '.join(analysis.penalty_targets)}")
    if analysis.right_holders:
        lines.append(f"  Right holders: {', '.join(analysis.right_holders)}")

    if analysis.has_mismatch:
        lines.append(f"\n  *** SCOPE MISMATCH DETECTED ***")
        for m in analysis.mismatches:
            lines.append(f"  {m.explanation}")
            lines.append(f"    Duty text: {m.duty_text[:80]}")
            lines.append(f"    Penalty text: {m.penalty_text[:80]}")

    return "\n".join(lines)
