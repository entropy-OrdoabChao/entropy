"""
pipeline/burden_engine.py — Burden of Proof Engine

DETERMINISTIC. NO LLM. NO INTERPRETATION.

For every provision, extracts:
  - WHO holds the burden (plaintiff, defendant, government)
  - WHAT type (prima facie, rebuttal, strict liability, preponderance, clear and convincing)
  - WHEN burden shifts (trigger conditions in statutory text)
  - WHERE the shift is codified (exact citation)

Usage:
    from pipeline.burden_engine import analyze_burden, extract_burden_from_text

    result = analyze_burden(statute_text, citation="5 U.S.C. § 552a(g)(4)")
    # result.holder = "defendant"
    # result.burden_type = "rebuttal"
    # result.shift_trigger = "once plaintiff establishes prima facie violation"
"""

import re
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Literal
from core.logger import get_logger

log = get_logger("burden_engine")

# ─── Data Structures ────────────────────────────────────────────────

BurdenHolder = Literal["plaintiff", "defendant", "government", "petitioner",
                        "respondent", "moving_party", "agency", "unknown"]

BurdenType = Literal["prima_facie", "rebuttal", "strict_liability",
                      "preponderance", "clear_and_convincing",
                      "beyond_reasonable_doubt", "presumption", "unknown"]


@dataclass
class BurdenShift:
    """A single burden-shift event within a provision."""
    trigger: str             # the textual condition
    from_holder: BurdenHolder
    to_holder: BurdenHolder
    burden_type: BurdenType
    source_text: str         # verbatim excerpt (≤120 chars)
    line_offset: int = 0     # approximate position in source


@dataclass
class BurdenAnalysis:
    """Complete burden analysis for a provision."""
    citation: str
    initial_holder: BurdenHolder
    initial_type: BurdenType
    shifts: List[BurdenShift] = field(default_factory=list)
    strict_liability: bool = False
    mandatory_remedy: bool = False
    source_excerpts: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_frontmatter(self) -> dict:
        """YAML-safe dict for Obsidian frontmatter."""
        d = {
            "burden_holder": self.initial_holder,
            "burden_type": self.initial_type,
            "strict_liability": self.strict_liability,
            "mandatory_remedy": self.mandatory_remedy,
        }
        if self.shifts:
            d["burden_shifts"] = [
                {
                    "trigger": s.trigger,
                    "from": s.from_holder,
                    "to": s.to_holder,
                    "type": s.burden_type,
                }
                for s in self.shifts
            ]
        return d


# ─── Detection Patterns ─────────────────────────────────────────────

# Strict liability indicators
STRICT_LIABILITY_PATTERNS = [
    r"strict(?:ly)?\s+liab(?:le|ility)",
    r"liable\s+without\s+(?:regard\s+to|proof\s+of)\s+(?:fault|negligence|intent)",
    r"irrespective\s+of\s+(?:fault|intent|negligence)",
    r"absolute\s+liability",
    r"no\s+(?:proof|showing)\s+of\s+(?:fault|intent|negligence)\s+(?:is\s+)?required",
]

# Presumption patterns (burden on other party to rebut)
PRESUMPTION_PATTERNS = [
    (r"shall\s+be\s+presumed", "presumption"),
    (r"presumption\s+(?:that|of)", "presumption"),
    (r"prima\s+facie\s+(?:evidence|proof|showing|case)", "prima_facie"),
    (r"creates?\s+a\s+(?:rebuttable\s+)?presumption", "presumption"),
    (r"establishes?\s+a\s+presumption", "presumption"),
]

# Burden shift triggers
SHIFT_PATTERNS = [
    (r"once\s+(?:the\s+)?(?:plaintiff|complainant|petitioner)\s+(?:has\s+)?(?:establish|show|demonstrat|prov)", "plaintiff", "defendant"),
    (r"(?:burden|onus)\s+(?:then\s+)?shifts?\s+to\s+(?:the\s+)?(?:defendant|respondent|agency|government)", "plaintiff", "defendant"),
    (r"(?:defendant|respondent|agency)\s+(?:shall|must|has\s+the\s+burden\s+to)\s+(?:prove|show|demonstrat|establish|rebut)", "defendant", "defendant"),
    (r"(?:plaintiff|complainant|petitioner)\s+(?:bears?|has)\s+the\s+(?:initial\s+)?burden", "plaintiff", "plaintiff"),
    (r"the\s+(?:burden|onus)\s+(?:of\s+(?:proof|persuasion|production))?\s*(?:is|shall\s+be|rests?)\s+(?:on|upon|with)\s+(?:the\s+)?(?:defendant|respondent|agency|government)", "defendant", "defendant"),
    (r"the\s+(?:burden|onus)\s+(?:of\s+(?:proof|persuasion|production))?\s*(?:is|shall\s+be|rests?)\s+(?:on|upon|with)\s+(?:the\s+)?(?:plaintiff|complainant|petitioner|claimant)", "plaintiff", "plaintiff"),
]

# Mandatory remedy indicators ("shall award" vs "may award")
MANDATORY_REMEDY_PATTERNS = [
    r"(?:court|agency)\s+shall\s+award",
    r"shall\s+(?:be\s+entitled\s+to|recover|receive)\s+(?:damages|costs|fees|relief)",
    r"entitled\s+to\s+(?:recover|receive)\s+(?:reasonable\s+)?(?:attorney|counsel)\s*(?:'s|s')?\s+fees",
    r"shall\s+(?:pay|compensate|reimburse)",
]

# Proof standard indicators
PROOF_STANDARD_PATTERNS = [
    (r"clear\s+and\s+convincing", "clear_and_convincing"),
    (r"beyond\s+(?:a\s+)?reasonable\s+doubt", "beyond_reasonable_doubt"),
    (r"preponderance\s+of\s+(?:the\s+)?evidence", "preponderance"),
    (r"substantial\s+evidence", "preponderance"),
]


# ─── Core Analysis ───────────────────────────────────────────────────


# ─── BURDEN SHIFT PATTERNS (added v2) ───
# These catch "shall demonstrate", "must prove", "burden on insurer" etc.
BURDEN_SHIFT_DUTY_PATTERNS = [
    # "shall demonstrate/prove/establish/show" = burden on the subject
    (r"((?:insurer|defendant|respondent|agency|bank|servicer|furnisher|collector)\s+(?:shall|must|has the burden to)\s+(?:demonstrate|prove|establish|show))", "defendant"),
    (r"((?:plaintiff|claimant|petitioner|applicant)\s+(?:shall|must|has the burden to)\s+(?:demonstrate|prove|establish|show))", "plaintiff"),
    # "burden is on" / "burden falls on" / "burden shifts to"
    (r"(burden\s+(?:is on|falls on|rests with|shifts to|placed on)\s+(?:the\s+)?(?:insurer|defendant|respondent))", "defendant"),
    (r"(burden\s+(?:is on|falls on|rests with|shifts to|placed on)\s+(?:the\s+)?(?:plaintiff|claimant|petitioner))", "plaintiff"),
    # "insurer bears the burden"
    (r"((?:insurer|defendant|respondent|bank|servicer)\s+bears\s+(?:the\s+)?burden)", "defendant"),
    (r"((?:plaintiff|claimant)\s+bears\s+(?:the\s+)?burden)", "plaintiff"),
    # Bad faith specific: "insurer must show reasonable basis"
    (r"((?:insurer|insurance company)\s+(?:must|shall)\s+(?:show|demonstrate|prove)\s+(?:a\s+)?reasonable\s+basis)", "defendant"),
    # FCRA: "furnisher shall conduct reasonable investigation"
    (r"(furnisher\s+(?:shall|must)\s+(?:conduct|perform|complete)\s+(?:a\s+)?reasonable\s+investigation)", "defendant"),
    # General "shall" duty patterns
    (r"((?:the\s+)?(?:agency|secretary|director|commissioner|board)\s+shall\s+(?:investigate|respond|act|determine|provide|furnish|produce|disclose))", "defendant"),
]

# Patterns for when burden NEVER shifts (absolute duties)
ABSOLUTE_DUTY_PATTERNS = [
    (r"(shall\s+not\s+(?:be\s+)?(?:liable|responsible|required)\s+(?:if|when|where))", "safe_harbor"),
    (r"(no\s+(?:civil\s+)?liability\s+(?:shall\s+)?(?:attach|arise|exist))", "immunity"),
]


def analyze_burden(text: str, citation: str = "") -> BurdenAnalysis:
    """
    Analyze burden of proof from statutory/regulatory text.

    Returns a BurdenAnalysis with initial holder, type, shifts,
    and whether strict liability or mandatory remedy applies.
    All conclusions traced to text excerpts.
    """
    text_lower = text.lower()
    result = BurdenAnalysis(
        citation=citation,
        initial_holder="plaintiff",  # default: plaintiff bears initial burden
        initial_type="preponderance",  # default civil standard
    )

    # 1. Check strict liability
    for pattern in STRICT_LIABILITY_PATTERNS:
        match = re.search(pattern, text_lower)
        if match:
            result.strict_liability = True
            result.initial_type = "strict_liability"
            excerpt = _extract_excerpt(text, match.start(), match.end())
            result.source_excerpts.append(excerpt)
            log.info(f"Strict liability detected in {citation}: {excerpt[:80]}")
            break

    # 2. Check proof standard
    for pattern, standard in PROOF_STANDARD_PATTERNS:
        match = re.search(pattern, text_lower)
        if match:
            if not result.strict_liability:
                result.initial_type = standard
            excerpt = _extract_excerpt(text, match.start(), match.end())
            result.source_excerpts.append(excerpt)
            break

    # 3. Check presumptions
    for pattern, p_type in PRESUMPTION_PATTERNS:
        match = re.search(pattern, text_lower)
        if match:
            excerpt = _extract_excerpt(text, match.start(), match.end())
            result.source_excerpts.append(excerpt)
            # Presumption = burden on other party to rebut
            shift = BurdenShift(
                trigger=f"Statutory presumption established",
                from_holder="plaintiff",
                to_holder="defendant",
                burden_type="rebuttal",
                source_text=excerpt,
                line_offset=text[:match.start()].count('\n'),
            )
            result.shifts.append(shift)

    
    # 3.5 Check duty-based burden shifts ("shall demonstrate", "must prove", etc.)
    for pattern, holder in BURDEN_SHIFT_DUTY_PATTERNS:
        match = re.search(pattern, text_lower)
        if match:
            excerpt = _extract_excerpt(text, match.start(), match.end())
            shift = BurdenShift(
                trigger=f"Duty-based: {holder} must prove/demonstrate",
                from_holder="plaintiff" if holder == "defendant" else "defendant",
                to_holder=holder,
                burden_type="production_and_persuasion",
                source_text=excerpt,
            )
            result.shifts.append(shift)
            result.source_excerpts.append(excerpt)
            log.info(f"Burden shift (duty): {holder} in {citation}: {excerpt[:60]}")

# 4. Check explicit burden assignments and shifts
    for pattern, from_h, to_h in SHIFT_PATTERNS:
        for match in re.finditer(pattern, text_lower):
            excerpt = _extract_excerpt(text, match.start(), match.end())
            # Determine if this sets initial holder or is a shift
            if "shift" in excerpt.lower() or "then" in excerpt.lower() or "once" in excerpt.lower():
                shift = BurdenShift(
                    trigger=excerpt,
                    from_holder=from_h,
                    to_holder=to_h,
                    burden_type="rebuttal",
                    source_text=excerpt,
                    line_offset=text[:match.start()].count('\n'),
                )
                result.shifts.append(shift)
            else:
                # Initial assignment
                if to_h in ("defendant", "agency", "government"):
                    result.initial_holder = "defendant"
                elif from_h == "plaintiff":
                    result.initial_holder = "plaintiff"
            result.source_excerpts.append(excerpt)

    # 5. Check mandatory remedy
    for pattern in MANDATORY_REMEDY_PATTERNS:
        match = re.search(pattern, text_lower)
        if match:
            result.mandatory_remedy = True
            excerpt = _extract_excerpt(text, match.start(), match.end())
            result.source_excerpts.append(excerpt)
            break

    # Deduplicate excerpts
    seen = set()
    unique = []
    for e in result.source_excerpts:
        if e not in seen:
            seen.add(e)
            unique.append(e)
    result.source_excerpts = unique

    return result


# BEEZLE_FN_UNUSED 2026-06-13: extract_burden_from_text (no static refs in pipeline/)
def extract_burden_from_text(text: str) -> dict:
    """
    Quick extraction of burden indicators from any legal text.
    Returns a summary dict suitable for vault node frontmatter.
    """
    analysis = analyze_burden(text)
    return analysis.to_frontmatter()


# BEEZLE_FN_UNUSED 2026-06-13: analyze_provision_burdens (no static refs in pipeline/)
def analyze_provision_burdens(provisions: list) -> List[BurdenAnalysis]:
    """
    Batch analyze multiple provisions.

    Args:
        provisions: list of dicts with keys 'text' and 'citation'

    Returns:
        List of BurdenAnalysis results
    """
    results = []
    for p in provisions:
        text = p.get("text", "")
        citation = p.get("citation", "")
        if text.strip():
            result = analyze_burden(text, citation)
            results.append(result)
    return results


# BEEZLE_FN_UNUSED 2026-06-13: format_burden_block (no static refs in pipeline/)
def format_burden_block(analysis: BurdenAnalysis) -> str:
    """Format burden analysis as readable text block for filings."""
    lines = []
    lines.append(f"BURDEN OF PROOF — {analysis.citation}")
    lines.append(f"  Initial burden: {analysis.initial_holder} ({analysis.initial_type})")

    if analysis.strict_liability:
        lines.append(f"  STRICT LIABILITY: No proof of fault/intent required")

    for i, shift in enumerate(analysis.shifts, 1):
        lines.append(f"  Shift {i}: {shift.from_holder} → {shift.to_holder}")
        lines.append(f"    Trigger: {shift.trigger}")
        lines.append(f"    Type: {shift.burden_type}")
        lines.append(f"    Source: {shift.source_text[:100]}")

    if analysis.mandatory_remedy:
        lines.append(f"  MANDATORY REMEDY: 'shall award' language detected")

    return "\n".join(lines)


# ─── Helpers ─────────────────────────────────────────────────────────

def _extract_excerpt(text: str, start: int, end: int, context: int = 40) -> str:
    """Extract text around a match with context, preserving original case."""
    excerpt_start = max(0, start - context)
    excerpt_end = min(len(text), end + context)
    excerpt = text[excerpt_start:excerpt_end].strip()
    # Clean up to sentence boundaries if possible
    if excerpt_start > 0:
        first_space = excerpt.find(' ')
        if first_space > 0 and first_space < 20:
            excerpt = excerpt[first_space + 1:]
    if excerpt_end < len(text):
        last_period = excerpt.rfind('.')
        if last_period > len(excerpt) - 30 and last_period > 0:
            excerpt = excerpt[:last_period + 1]
    return excerpt[:120]
