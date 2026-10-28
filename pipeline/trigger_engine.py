"""
pipeline/trigger_engine.py — Trigger Condition Engine

DETERMINISTIC. NO LLM. NO INTERPRETATION.

For every provision, extracts:
  - WHEN the statute activates (trigger conditions)
  - DEADLINES (time limits with day counts)
  - MANDATORY DUTIES (shall + action = non-discretionary)
  - VIOLATION COMPLETION (trigger met + deadline passed = violation)

Usage:
    from pipeline.trigger_engine import extract_triggers, check_violation

    triggers = extract_triggers(statute_text, citation="5 U.S.C. § 552(a)(6)(A)")
    violation = check_violation(triggers, facts={"request_date": "2025-01-15", "response_date": None})
"""

import re
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Literal
from datetime import datetime, timedelta
from core.logger import get_logger

log = get_logger("trigger_engine")

# ─── Data Structures ────────────────────────────────────────────────

TriggerType = Literal["temporal", "conditional", "mandatory_duty",
                       "upon_request", "failure_to_act", "notice", "filing"]


@dataclass
class TriggerCondition:
    """A single activation condition in a provision."""
    trigger_type: TriggerType
    condition_text: str          # the parsed condition
    deadline_days: Optional[int] = None   # None if no time limit
    deadline_unit: str = "days"  # days, business_days, calendar_days, months, years
    deadline_text: str = ""      # original text of deadline
    mandatory: bool = False      # True if "shall" language
    actor: str = ""              # who must act
    action: str = ""             # what they must do
    source_text: str = ""        # verbatim excerpt
    line_offset: int = 0


@dataclass
class ViolationCheck:
    """Result of checking triggers against facts."""
    triggered: bool = False
    violation_complete: bool = False
    deadline_passed: bool = False
    days_overdue: int = 0
    trigger_met: str = ""
    deadline_text: str = ""
    citation: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TriggerAnalysis:
    """Complete trigger analysis for a provision."""
    citation: str
    triggers: List[TriggerCondition] = field(default_factory=list)
    has_mandatory_duty: bool = False
    has_deadline: bool = False
    shortest_deadline_days: Optional[int] = None
    source_excerpts: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_frontmatter(self) -> dict:
        """YAML-safe dict for Obsidian frontmatter."""
        d = {
            "has_mandatory_duty": self.has_mandatory_duty,
            "has_deadline": self.has_deadline,
        }
        if self.shortest_deadline_days is not None:
            d["shortest_deadline_days"] = self.shortest_deadline_days
        if self.triggers:
            d["trigger_conditions"] = [
                {
                    "type": t.trigger_type,
                    "condition": t.condition_text[:100],
                    "deadline_days": t.deadline_days,
                    "mandatory": t.mandatory,
                    "actor": t.actor,
                }
                for t in self.triggers
            ]
        return d


# ─── Detection Patterns ─────────────────────────────────────────────

# Temporal deadlines
DEADLINE_PATTERNS = [
    # "within 20 days", "not later than 30 days", "no more than 10 business days"
    (r"within\s+(\d+)\s+(business\s+)?day", "days"),
    (r"not\s+later\s+than\s+(\d+)\s+(business\s+)?day", "days"),
    (r"no\s+(?:more|later)\s+than\s+(\d+)\s+(business\s+)?day", "days"),
    (r"(\d+)\s+(business\s+)?day(?:s)?\s+(?:after|from|of|following)", "days"),
    (r"within\s+(\d+)\s+(calendar\s+)?day", "days"),
    (r"within\s+(\d+)\s+month", "months"),
    (r"within\s+(\d+)\s+year", "years"),
    (r"(\d+)[\s-]+day\s+(?:period|deadline|time\s+limit|window)", "days"),
]

# Mandatory duty: "shall" + verb
MANDATORY_PATTERNS = [
    r"(?:the\s+)?(?:agency|secretary|officer|department|bureau|commission|board|administrator|director)\s+shall\s+(\w+(?:\s+\w+){0,5})",
    r"shall\s+(?:be\s+required\s+to|promptly|immediately|forthwith)\s+(\w+(?:\s+\w+){0,5})",
    r"shall\s+(make\s+available|provide|furnish|produce|respond|notify|issue|publish|disclose|release|grant|deny|determine)",
    r"(?:it|there)\s+shall\s+be\s+(?:the\s+)?duty\s+of",
    r"is\s+required\s+to\s+(\w+(?:\s+\w+){0,5})",
]

# Conditional triggers
CONDITIONAL_PATTERNS = [
    (r"upon\s+(?:receipt\s+of\s+(?:a\s+)?)?request", "upon_request"),
    (r"upon\s+(?:written\s+)?(?:notice|demand|application|petition|complaint)", "notice"),
    (r"upon\s+(?:a\s+)?filing", "filing"),
    (r"if\s+(?:the\s+)?(?:agency|secretary|department)\s+fails?\s+to", "failure_to_act"),
    (r"in\s+the\s+event\s+(?:that|of)\s+(?:a\s+)?(?:failure|refusal|denial)", "failure_to_act"),
    (r"after\s+(?:receipt\s+of\s+)?(?:notice|request|demand|complaint|petition)", "notice"),
    (r"when\s+(?:a\s+)?(?:request|demand|application|complaint)\s+(?:is|has\s+been)\s+(?:made|filed|submitted|received)", "upon_request"),
    (r"(?:following|after)\s+exhaustion\s+of\s+(?:administrative\s+)?remedies", "conditional"),
]

# Actor extraction
ACTOR_PATTERNS = [
    (r"(?:the\s+)?(agency|secretary|officer|department|bureau|commission|board|administrator|director|comptroller|treasurer|inspector\s+general)", "government"),
    (r"(?:the\s+)?(person|individual|citizen|applicant|requester|complainant|petitioner|claimant)", "private"),
    (r"(?:the\s+)?(bank|institution|entity|corporation|company|employer|contractor)", "entity"),
]


# ─── Core Analysis ───────────────────────────────────────────────────

def extract_triggers(text: str, citation: str = "") -> TriggerAnalysis:
    """
    Extract all trigger conditions from statutory/regulatory text.

    Returns a TriggerAnalysis with all conditions, deadlines,
    and mandatory duties found. All traced to source text.
    """
    text_lower = text.lower()
    result = TriggerAnalysis(citation=citation)

    # 1. Extract deadlines
    for pattern, unit in DEADLINE_PATTERNS:
        for match in re.finditer(pattern, text_lower):
            days = int(match.group(1))
            is_business = match.group(2) is not None if match.lastindex >= 2 else False
            actual_unit = f"business_{unit}" if is_business else unit

            # Convert to calendar days for comparison
            calendar_days = days
            if is_business:
                calendar_days = int(days * 7 / 5) + 2  # rough conversion
            if unit == "months":
                calendar_days = days * 30
            elif unit == "years":
                calendar_days = days * 365

            excerpt = _extract_excerpt(text, match.start(), match.end())

            trigger = TriggerCondition(
                trigger_type="temporal",
                condition_text=f"Within {days} {actual_unit}",
                deadline_days=calendar_days,
                deadline_unit=actual_unit,
                deadline_text=excerpt,
                source_text=excerpt,
                line_offset=text[:match.start()].count('\n'),
            )
            result.triggers.append(trigger)
            result.has_deadline = True
            result.source_excerpts.append(excerpt)

            if result.shortest_deadline_days is None or calendar_days < result.shortest_deadline_days:
                result.shortest_deadline_days = calendar_days

    # 2. Extract mandatory duties
    for pattern in MANDATORY_PATTERNS:
        for match in re.finditer(pattern, text_lower):
            excerpt = _extract_excerpt(text, match.start(), match.end())
            action = match.group(1) if match.lastindex and match.group(1) else ""
            actor = _detect_actor(excerpt)

            trigger = TriggerCondition(
                trigger_type="mandatory_duty",
                condition_text=f"{actor} shall {action}".strip(),
                mandatory=True,
                actor=actor,
                action=action.strip(),
                source_text=excerpt,
                line_offset=text[:match.start()].count('\n'),
            )
            result.triggers.append(trigger)
            result.has_mandatory_duty = True
            result.source_excerpts.append(excerpt)

    # 3. Extract conditional triggers
    for pattern, t_type in CONDITIONAL_PATTERNS:
        for match in re.finditer(pattern, text_lower):
            excerpt = _extract_excerpt(text, match.start(), match.end())

            trigger = TriggerCondition(
                trigger_type=t_type,
                condition_text=excerpt,
                source_text=excerpt,
                line_offset=text[:match.start()].count('\n'),
            )
            result.triggers.append(trigger)
            result.source_excerpts.append(excerpt)

    # Deduplicate
    seen = set()
    unique = []
    for e in result.source_excerpts:
        if e not in seen:
            seen.add(e)
            unique.append(e)
    result.source_excerpts = unique

    return result


def check_violation(triggers: TriggerAnalysis, facts: dict) -> ViolationCheck:
    """
    Check if trigger conditions have been met based on case facts.

    Args:
        triggers: TriggerAnalysis from extract_triggers()
        facts: dict with keys like:
            - request_date: ISO date string
            - response_date: ISO date string or None
            - notice_date: ISO date string
            - filing_date: ISO date string
            - current_date: ISO date string (defaults to today)

    Returns:
        ViolationCheck indicating whether violation is complete.
    """
    result = ViolationCheck(citation=triggers.citation)
    current = facts.get("current_date", datetime.now().strftime("%Y-%m-%d"))

    try:
        current_dt = datetime.strptime(current, "%Y-%m-%d")
    except (ValueError, TypeError):
        current_dt = datetime.now()

    for trigger in triggers.triggers:
        # Check temporal triggers with deadlines
        if trigger.trigger_type == "temporal" and trigger.deadline_days:
            # Find the start date from facts
            start_date_str = (
                facts.get("request_date") or
                facts.get("notice_date") or
                facts.get("filing_date")
            )
            if not start_date_str:
                continue

            try:
                start_dt = datetime.strptime(start_date_str, "%Y-%m-%d")
            except (ValueError, TypeError):
                continue

            deadline_dt = start_dt + timedelta(days=trigger.deadline_days)
            response_str = facts.get("response_date")

            if response_str is None:
                # No response at all
                if current_dt > deadline_dt:
                    result.triggered = True
                    result.violation_complete = True
                    result.deadline_passed = True
                    result.days_overdue = (current_dt - deadline_dt).days
                    result.trigger_met = trigger.condition_text
                    result.deadline_text = trigger.deadline_text
                    return result
            else:
                try:
                    response_dt = datetime.strptime(response_str, "%Y-%m-%d")
                    if response_dt > deadline_dt:
                        result.triggered = True
                        result.violation_complete = True
                        result.deadline_passed = True
                        result.days_overdue = (response_dt - deadline_dt).days
                        result.trigger_met = trigger.condition_text
                        result.deadline_text = trigger.deadline_text
                        return result
                except (ValueError, TypeError):
                    pass

        # Check failure-to-act triggers
        elif trigger.trigger_type == "failure_to_act":
            if facts.get("response_date") is None and facts.get("request_date"):
                result.triggered = True
                result.trigger_met = trigger.condition_text

    return result


# BEEZLE_FN_UNUSED 2026-06-13: format_trigger_block (no static refs in pipeline/)
def format_trigger_block(analysis: TriggerAnalysis) -> str:
    """Format trigger analysis as readable text block for filings."""
    lines = []
    lines.append(f"TRIGGER CONDITIONS — {analysis.citation}")

    if analysis.has_mandatory_duty:
        lines.append(f"  MANDATORY DUTY DETECTED")
    if analysis.has_deadline:
        lines.append(f"  SHORTEST DEADLINE: {analysis.shortest_deadline_days} days")

    for i, t in enumerate(analysis.triggers, 1):
        lines.append(f"  [{i}] Type: {t.trigger_type}")
        lines.append(f"      Condition: {t.condition_text}")
        if t.deadline_days:
            lines.append(f"      Deadline: {t.deadline_days} {t.deadline_unit}")
        if t.mandatory:
            lines.append(f"      MANDATORY (shall language)")
        if t.actor:
            lines.append(f"      Actor: {t.actor}")
        lines.append(f"      Source: {t.source_text[:100]}")

    return "\n".join(lines)


# ─── Helpers ─────────────────────────────────────────────────────────

def _detect_actor(text: str) -> str:
    """Detect the actor type from text."""
    text_lower = text.lower()
    for pattern, actor_type in ACTOR_PATTERNS:
        match = re.search(pattern, text_lower)
        if match:
            return match.group(1)
    return "unspecified"


def _extract_excerpt(text: str, start: int, end: int, context: int = 40) -> str:
    """Extract text around a match with context."""
    excerpt_start = max(0, start - context)
    excerpt_end = min(len(text), end + context)
    excerpt = text[excerpt_start:excerpt_end].strip()
    if excerpt_start > 0:
        first_space = excerpt.find(' ')
        if 0 < first_space < 20:
            excerpt = excerpt[first_space + 1:]
    if excerpt_end < len(text):
        last_period = excerpt.rfind('.')
        if last_period > len(excerpt) - 30 and last_period > 0:
            excerpt = excerpt[:last_period + 1]
    return excerpt[:120]
