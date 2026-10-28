"""
pipeline/remedy_engine.py — Remedy Engine

DETERMINISTIC. NO LLM. NO INTERPRETATION.

For every provision, extracts:
  - WHAT relief is available (damages, injunction, mandamus, fees, penalties)
  - MANDATORY vs DISCRETIONARY ("shall award" vs "may award")
  - FEE SHIFTING provisions (leverage multiplier)
  - STATUTORY DAMAGES (fixed amounts, multipliers)
  - ENFORCEMENT PATHS (private right of action, agency complaint, both)

Usage:
    from pipeline.remedy_engine import extract_remedies

    result = extract_remedies(statute_text, citation="15 U.S.C. § 1681n")
"""

import re
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Literal
from core.logger import get_logger

log = get_logger("remedy_engine")

# ─── Data Structures ────────────────────────────────────────────────

RemedyType = Literal["actual_damages", "statutory_damages", "punitive_damages",
                      "compensatory_damages", "liquidated_damages", "treble_damages",
                      "injunction", "mandamus", "declaratory_relief",
                      "attorney_fees", "costs", "penalties", "forfeiture",
                      "equitable_relief", "specific_performance", "rescission",
                      "restitution", "disgorgement", "unknown"]

EnforcementPath = Literal["private_right", "agency_action", "both",
                           "criminal", "qui_tam", "unknown"]


@dataclass
class Remedy:
    """A single remedy available under a provision."""
    remedy_type: RemedyType
    mandatory: bool              # True = "shall", False = "may"
    amount_text: str = ""        # "$1,000" or "actual damages" or "treble"
    multiplier: Optional[float] = None  # 2.0 for double, 3.0 for treble
    minimum: Optional[float] = None     # statutory minimum
    maximum: Optional[float] = None     # statutory maximum
    per_violation: bool = False
    source_text: str = ""


@dataclass
class RemedyAnalysis:
    """Complete remedy analysis for a provision."""
    citation: str
    remedies: List[Remedy] = field(default_factory=list)
    enforcement_path: EnforcementPath = "unknown"
    has_private_right: bool = False
    has_fee_shifting: bool = False
    has_mandatory_remedy: bool = False
    has_statutory_damages: bool = False
    source_excerpts: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_frontmatter(self) -> dict:
        d = {
            "enforcement_path": self.enforcement_path,
            "has_private_right": self.has_private_right,
            "has_fee_shifting": self.has_fee_shifting,
            "has_mandatory_remedy": self.has_mandatory_remedy,
            "has_statutory_damages": self.has_statutory_damages,
        }
        if self.remedies:
            d["remedies"] = [
                {
                    "type": r.remedy_type,
                    "mandatory": r.mandatory,
                    "amount": r.amount_text or None,
                }
                for r in self.remedies
            ]
        return d


# ─── Detection Patterns ─────────────────────────────────────────────

# Mandatory vs discretionary
MANDATORY_AWARD = re.compile(
    r"(?:court|agency|tribunal)\s+shall\s+(?:award|grant|order|enter|impose)|"
    r"shall\s+(?:be\s+)?(?:entitled\s+to|awarded|granted)\s+(?:recover|receive|obtain)?|"
    r"shall\s+recover|"
    r"is\s+entitled\s+to\s+(?:recover|receive)",
    re.IGNORECASE
)

DISCRETIONARY_AWARD = re.compile(
    r"(?:court|agency|tribunal)\s+may\s+(?:award|grant|order|enter|impose)|"
    r"may\s+(?:be\s+)?(?:entitled\s+to|awarded|granted)|"
    r"in\s+(?:its|the\s+court'?s?)\s+discretion",
    re.IGNORECASE
)

# Damage types
DAMAGE_PATTERNS = [
    (r"actual\s+damages?", "actual_damages"),
    (r"compensatory\s+damages?", "compensatory_damages"),
    (r"statutory\s+damages?", "statutory_damages"),
    (r"punitive\s+damages?", "punitive_damages"),
    (r"liquidated\s+damages?", "liquidated_damages"),
    (r"treble\s+damages?", "treble_damages"),
    (r"(?:double|twice|two\s+times)\s+(?:the\s+)?(?:amount|damages)", "treble_damages"),  # double
    (r"three\s+times?\s+(?:the\s+)?(?:amount|damages)", "treble_damages"),
]

# Specific dollar amounts
AMOUNT_PATTERNS = [
    (r"\$\s?([\d,]+(?:\.\d{2})?)\s+(?:per|for\s+each)\s+(?:violation|occurrence|day)", True),
    (r"\$\s?([\d,]+(?:\.\d{2})?)\s+(?:minimum|not\s+less\s+than)", False),
    (r"not\s+(?:less|more)\s+than\s+\$\s?([\d,]+(?:\.\d{2})?)", False),
    (r"\$\s?([\d,]+(?:\.\d{2})?)", False),
]

# Fee shifting
FEE_PATTERNS = [
    r"(?:reasonable\s+)?(?:attorney|counsel|lawyer)(?:'?s?)?\s+fees?",
    r"costs?\s+of\s+(?:the\s+)?(?:action|litigation|suit|proceeding)",
    r"litigation\s+costs?",
    r"expert\s+(?:witness\s+)?fees?",
]

# Equitable remedies
EQUITABLE_PATTERNS = [
    (r"injunctive\s+relief|(?:temporary|preliminary|permanent)\s+injunction", "injunction"),
    (r"mandamus", "mandamus"),
    (r"declaratory\s+(?:judgment|relief)", "declaratory_relief"),
    (r"equitable\s+relief", "equitable_relief"),
    (r"specific\s+performance", "specific_performance"),
    (r"rescission", "rescission"),
    (r"restitution", "restitution"),
    (r"disgorgement", "disgorgement"),
]

# Enforcement path
PRIVATE_RIGHT_PATTERNS = [
    r"(?:any|a)\s+person\s+(?:may|shall\s+have\s+the\s+right\s+to)\s+(?:bring|commence|maintain|file)\s+(?:a\s+)?(?:civil\s+)?action",
    r"private\s+(?:right|cause)\s+of\s+action",
    r"(?:civil|private)\s+action\s+(?:may|shall)\s+be\s+(?:brought|commenced|maintained)",
    r"aggrieved\s+(?:person|party|individual)\s+may\s+(?:bring|commence|file)",
    r"may\s+(?:sue|bring\s+suit)\s+in\s+(?:any|a)\s+(?:court|district)",
]

QUI_TAM_PATTERNS = [
    r"qui\s+tam",
    r"(?:private|citizen)\s+(?:attorney|enforcement)\s+(?:general|action)",
    r"relator",
]


# ─── Core Analysis ───────────────────────────────────────────────────

def extract_remedies(text: str, citation: str = "") -> RemedyAnalysis:
    """
    Extract all available remedies from statutory/regulatory text.
    All conclusions traced to source text excerpts.
    """
    text_lower = text.lower()
    result = RemedyAnalysis(citation=citation)

    # Determine mandatory vs discretionary context
    has_shall = bool(MANDATORY_AWARD.search(text))
    has_may = bool(DISCRETIONARY_AWARD.search(text))

    # 1. Damage types
    for pattern, dtype in DAMAGE_PATTERNS:
        match = re.search(pattern, text_lower)
        if match:
            mandatory = has_shall and not has_may
            remedy = Remedy(
                remedy_type=dtype,
                mandatory=mandatory,
                source_text=_extract_excerpt(text, match.start(), match.end()),
            )

            # Check for multiplier
            if dtype == "treble_damages":
                if re.search(r"three\s+times|treble", text_lower):
                    remedy.multiplier = 3.0
                elif re.search(r"double|twice|two\s+times", text_lower):
                    remedy.multiplier = 2.0
                    remedy.remedy_type = "liquidated_damages"

            # Check for statutory amounts
            if dtype == "statutory_damages":
                result.has_statutory_damages = True
                for amt_pattern, per_viol in AMOUNT_PATTERNS:
                    amt_match = re.search(amt_pattern, text)
                    if amt_match:
                        amount_str = amt_match.group(1).replace(",", "")
                        try:
                            amount = float(amount_str)
                            remedy.amount_text = f"${amount:,.2f}"
                            remedy.per_violation = per_viol
                        except ValueError:
                            remedy.amount_text = amt_match.group(0)
                        break

            result.remedies.append(remedy)
            result.source_excerpts.append(remedy.source_text)

    # 2. Fee shifting
    for pattern in FEE_PATTERNS:
        match = re.search(pattern, text_lower)
        if match:
            result.has_fee_shifting = True
            excerpt = _extract_excerpt(text, match.start(), match.end())
            # Determine if mandatory fee shifting
            mandatory = bool(re.search(
                r"shall\s+(?:award|allow|include).*(?:attorney|counsel|fees)",
                text_lower
            ))
            remedy = Remedy(
                remedy_type="attorney_fees",
                mandatory=mandatory,
                source_text=excerpt,
            )
            result.remedies.append(remedy)
            result.source_excerpts.append(excerpt)
            break  # only count once

    # 3. Equitable remedies
    for pattern, etype in EQUITABLE_PATTERNS:
        match = re.search(pattern, text_lower)
        if match:
            excerpt = _extract_excerpt(text, match.start(), match.end())
            remedy = Remedy(
                remedy_type=etype,
                mandatory=has_shall and not has_may,
                source_text=excerpt,
            )
            result.remedies.append(remedy)
            result.source_excerpts.append(excerpt)

    # 4. Enforcement path
    for pattern in PRIVATE_RIGHT_PATTERNS:
        if re.search(pattern, text_lower):
            result.has_private_right = True
            result.enforcement_path = "private_right"
            break

    for pattern in QUI_TAM_PATTERNS:
        if re.search(pattern, text_lower):
            result.enforcement_path = "qui_tam"
            break

    # Check for agency enforcement too
    if re.search(r"(?:agency|commission|secretary|board)\s+(?:may|shall)\s+(?:bring|commence|institute|initiate)", text_lower):
        if result.has_private_right:
            result.enforcement_path = "both"
        else:
            result.enforcement_path = "agency_action"

    # Set flags
    result.has_mandatory_remedy = any(r.mandatory for r in result.remedies)

    # Deduplicate excerpts
    seen = set()
    result.source_excerpts = [e for e in result.source_excerpts if e not in seen and not seen.add(e)]

    return result


# BEEZLE_FN_UNUSED 2026-06-13: format_remedy_block (no static refs in pipeline/)
def format_remedy_block(analysis: RemedyAnalysis) -> str:
    """Format remedy analysis as readable text block."""
    lines = [f"AVAILABLE REMEDIES — {analysis.citation}"]

    if analysis.has_mandatory_remedy:
        lines.append("  *** MANDATORY REMEDY ('shall award' language) ***")
    if analysis.has_fee_shifting:
        lines.append("  *** FEE SHIFTING AVAILABLE ***")
    if analysis.has_private_right:
        lines.append(f"  Enforcement: {analysis.enforcement_path}")

    for i, r in enumerate(analysis.remedies, 1):
        mand = "MANDATORY" if r.mandatory else "discretionary"
        lines.append(f"  [{i}] {r.remedy_type} ({mand})")
        if r.amount_text:
            per = " per violation" if r.per_violation else ""
            lines.append(f"      Amount: {r.amount_text}{per}")
        if r.multiplier:
            lines.append(f"      Multiplier: {r.multiplier}x")
        lines.append(f"      Source: {r.source_text[:100]}")

    return "\n".join(lines)


def _extract_excerpt(text: str, start: int, end: int, context: int = 40) -> str:
    excerpt_start = max(0, start - context)
    excerpt_end = min(len(text), end + context)
    excerpt = text[excerpt_start:excerpt_end].strip()
    return excerpt[:120]
