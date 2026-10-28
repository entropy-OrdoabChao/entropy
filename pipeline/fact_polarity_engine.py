"""
pipeline/fact_polarity_engine.py — Fact Polarity Engine.

Locks fact framing from the plaintiff's perspective BEFORE any drafter
touches them. This is the final piece that prevents the system from
"flipping sides" by parroting the opposing party's framing.

PRINCIPLE:
  Same fact, two framings:
    - "verification was provided" (defense framing — adopts their narrative)
    - "verification was missing at filing and only later attempted" (plaintiff framing)

  We pass ONLY the plaintiff framing forward.

NO LLM. Pure rule-based transformation.

The transformations are issue-aware: a verification fact gets framed
differently than a service fact or a default fact.
"""

import re
from dataclasses import dataclass, field
from typing import Optional

from core.logger import get_logger

log = get_logger("FACT_POLARITY")


# ══════════════════════════════════════════════════════════════════════
# FRAMING RULES
# ══════════════════════════════════════════════════════════════════════
#
# Each rule has:
#   - detection: pattern that identifies a fact relevant to an issue
#   - defense_phrasing: phrases that signal the fact is in their voice
#   - plaintiff_framing: how the same fact reads in plaintiff's voice
#
# When a fact matches a rule, we either:
#   1. Replace defense phrases with plaintiff equivalents, OR
#   2. Substitute the entire fact with a plaintiff-framed version
# ══════════════════════════════════════════════════════════════════════


# Phrase substitutions: defense → plaintiff
PHRASE_FLIPS = [
    # Verification framings
    (r"\bprovided\s+(?:a\s+)?verification\b", "did not provide verification at the time of filing and only attempted to attach one later"),
    (r"\bproperly\s+verified\b", "filed without the required verification"),
    (r"\banswers\s+are\s+complete\b", "answers were filed without proper verification"),
    (r"\bcomplete\s+and\s+accurate\s+responses\b", "responses that were filed without the required verification"),
    (r"\bproper\s+verification\b", "untimely verification"),

    # Service framings
    (r"\bproperly\s+served\b", "did not properly serve"),
    (r"\bservice\s+was\s+effected\b", "service was defective"),
    (r"\bduly\s+served\b", "improperly served"),

    # Default / response framings
    (r"\btimely\s+(?:filed|served|responded)\b", "filed in a manner that does not satisfy the rule"),
    (r"\bcomplied\s+with\b", "did not comply with"),
    (r"\bsatisfied\s+the\s+requirement\b", "failed to satisfy the requirement"),

    # Garnishment framings
    (r"\bno\s+(?:State\s+Farm\s+)?(?:property|assets)\s+in\s+(?:its\s+)?possession\b",
     "made an unverified assertion of having no responsive property"),
    (r"\bhas\s+no\s+property\b", "made an unverified assertion of having no property"),

    # Generic compliance flips
    (r"\bdemonstrating\s+that\s+the\s+answers\s+are\s+complete\b",
     "with answers that remain procedurally defective"),
    (r"\bcorrectly\s+(?:filed|served|responded)\b", "filed in a manner that fails the rule"),
    (r"\bappropriate\s+procedure\b", "improper procedure"),
    (r"\blegally\s+sufficient\b", "legally insufficient"),
]


# Issue-specific full rewrites (when a fact's framing is so neutral it needs replacement)
ISSUE_REWRITES = {
    "verification": {
        "praecipe": "The Garnishee filed a praecipe twenty-five days after the original filing in an attempt to retroactively cure the absence of verification, which does not satisfy the rule's contemporaneous-filing requirement.",
        "no verification": "The Garnishee filed objections without the required verification, in violation of the mandatory rule.",
        "verified": "The Garnishee did not provide a contemporaneous verification at the time of the original filing.",
    },
    "service": {
        "served": "The opposing party did not effect service in compliance with the applicable rule.",
    },
    "garnishment": {
        "no assets": "The Garnishee asserted, without proper verification, that it holds no responsive property.",
        "no property": "The Garnishee asserted, without proper verification, that it holds no responsive property.",
    },
    "default": {
        "responded": "The opposing party did not respond in a manner that satisfies the rule's requirements.",
    },
}


# Patterns that indicate a fact is from defense framing and should be flipped
DEFENSE_INDICATORS = [
    r"\bprovided\s+(?:a\s+)?verification\b",
    r"\bproperly\s+verified\b",
    r"\banswers?\s+(?:is|are)\s+complete\b",
    r"\bcomplied\s+with\b",
    r"\bproperly\s+(?:filed|served)\b",
    r"\bappropriate\s+procedure\b",
    r"\bcomplete\s+and\s+accurate\b",
]


# Patterns that indicate a fact is already plaintiff-framed (skip rewriting)
PLAINTIFF_INDICATORS = [
    r"\bfailed\s+to\b",
    r"\bdid\s+not\b",
    r"\bwithout\s+verification\b",
    r"\buntimely\b",
    r"\bdefective\b",
    r"\bnon-compliant\b",
    r"\bin\s+violation\s+of\b",
]


# ══════════════════════════════════════════════════════════════════════
# DATACLASSES
# ══════════════════════════════════════════════════════════════════════

@dataclass
class FramedFact:
    raw: str = ""
    plaintiff_framing: str = ""
    defense_framing: str = ""
    polarity: str = "plaintiff"
    transformation: str = "none"  # none | phrase_flip | issue_rewrite

    def to_dict(self) -> dict:
        return {
            "raw": self.raw,
            "plaintiff_framing": self.plaintiff_framing,
            "defense_framing": self.defense_framing,
            "polarity": self.polarity,
            "transformation": self.transformation,
        }


# ══════════════════════════════════════════════════════════════════════
# CORE TRANSFORMATION
# ══════════════════════════════════════════════════════════════════════

def reframe_fact(raw_fact: str, issue: str = "general") -> FramedFact:
    """Reframe a single fact from plaintiff's perspective.

    Strategy:
      1. If fact is already plaintiff-framed, keep it
      2. If fact contains defense indicators, apply phrase flips
      3. If issue has a specific rewrite that matches keywords, use it
      4. Otherwise return raw fact unchanged (presumed neutral)

    PHASE 3.5j: now negation-aware. If a fact already contains negation
    (not, no, never, failed to, did not), we DO NOT apply phrase flips
    that would create double negatives.
    """
    if not raw_fact or not isinstance(raw_fact, str):
        return FramedFact(raw="", plaintiff_framing="", polarity="plaintiff")

    fact = raw_fact.strip()
    fact_lower = fact.lower()
    issue_lower = issue.lower() if issue else "general"

    framed = FramedFact(raw=fact, defense_framing=fact)

    # Step 1: Check if already plaintiff-framed
    is_plaintiff_already = any(re.search(pat, fact_lower) for pat in PLAINTIFF_INDICATORS)
    if is_plaintiff_already:
        framed.plaintiff_framing = fact
        framed.transformation = "already_plaintiff"
        return framed

    # Step 1b: PHASE 3.5j — check for existing negation that would cause double-negatives
    has_existing_negation = bool(re.search(
        r"\b(not|no|never|none|cannot|wasn'?t|isn'?t|aren'?t|didn'?t|hasn'?t|haven'?t)\b",
        fact_lower
    ))
    if has_existing_negation:
        # Fact already contains negation - if it's about plaintiff, flip subject;
        # if it's about opposing, leave it alone
        if "plaintiff" in fact_lower or "creditor" in fact_lower:
            # Defense framing of plaintiff: "Plaintiff has not complied"
            # Reframe as opposing party position
            framed.plaintiff_framing = (
                f"The opposing party asserts that {fact[0].lower() + fact[1:] if fact else fact}, "
                f"which is contradicted by the record."
            )
            framed.transformation = "subject_flip"
            return framed
        # Otherwise it's already arguing against opposing party — keep as-is
        framed.plaintiff_framing = fact
        framed.transformation = "kept_negation"
        return framed

    # Step 2: Check for issue-specific full rewrites
    if issue_lower in ISSUE_REWRITES:
        for keyword, rewrite in ISSUE_REWRITES[issue_lower].items():
            if keyword in fact_lower:
                framed.plaintiff_framing = rewrite
                framed.transformation = "issue_rewrite"
                return framed

    # Step 3: Check for defense indicators and apply phrase flips
    has_defense_phrasing = any(re.search(pat, fact_lower) for pat in DEFENSE_INDICATORS)
    if has_defense_phrasing:
        rewritten = fact
        for pattern, replacement in PHRASE_FLIPS:
            rewritten = re.sub(pattern, replacement, rewritten, flags=re.IGNORECASE)
        framed.plaintiff_framing = rewritten
        framed.transformation = "phrase_flip"
        return framed

    # Step 4: No transformation needed — fact is presumed neutral / dates / amounts
    framed.plaintiff_framing = fact
    framed.transformation = "none"
    return framed


def reframe_facts(facts: list, issue: str = "general") -> list:
    """Reframe a list of facts. Returns list of FramedFact objects."""
    if not facts:
        return []
    return [reframe_fact(f, issue) for f in facts if f]


# BEEZLE_FN_UNUSED 2026-06-13: get_plaintiff_facts (no static refs in pipeline/)
def get_plaintiff_facts(facts: list, issue: str = "general") -> list:
    """Convenience: return just the plaintiff-framed strings."""
    framed = reframe_facts(facts, issue)
    return [f.plaintiff_framing for f in framed if f.plaintiff_framing]


# ══════════════════════════════════════════════════════════════════════
# RULE TEXT SANITIZATION
# ══════════════════════════════════════════════════════════════════════
#
# Strips template garbage and unrelated case fragments from rule text
# before it gets passed to the IF/THEN engine.
# ══════════════════════════════════════════════════════════════════════

# Patterns that indicate template / unrelated content
JUNK_PATTERNS = [
    r"BY\s+THE\s+COURT[:\s].*",
    r"ARBITRATION\s*SCHEDULING.*",
    r"AMOUNT\s*IN\s*CONTROVERSY.*",
    r"BLAIR\s+COUNTY.*",
    r"PRE-?TRIAL\s+CONFERENCE.*",
    r"\bAttachment\s*[A-Z]\b.*",
    r"\bCertificate\s+of\s+Service\b.*",
    r"\bRespectfully\s+submitted\b.*",
    r"Order\.\s*BY\s+THE\s+COURT.*",
    r"Order\s+of\s+Court\s+was\s+signed.*",
    r"on\s+further\s+directives.*",
    r"\bPLAINTIFF\s*:\s*[A-Z]+\s*:.*",
    r"\bDEFENDANT\s*:\s*[A-Z]+\s*:.*",
    r"\$\d+\.\d{2}\s*OR\s*Less.*",
]


# BEEZLE_FN_UNUSED 2026-06-13: sanitize_rule_text (no static refs in pipeline/)
def sanitize_rule_text(rule_text: str) -> str:
    """Strip template garbage from rule text before IF/THEN extraction.

    Returns the cleaned rule text. If everything is junk, returns empty string.
    """
    if not rule_text:
        return ""

    cleaned = rule_text

    # Strip junk patterns
    for pattern in JUNK_PATTERNS:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE | re.DOTALL)

    # Strip non-printable garbage
    cleaned = re.sub(r"[^\x20-\x7E\n]", " ", cleaned)

    # Collapse whitespace
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    # If the cleaned result is too short or contains no rule-like language, reject
    if len(cleaned) < 15:
        return ""

    has_rule_language = bool(re.search(
        r"\b(shall|must|may|required|provided|filed|verified|served|prohibited)\b",
        cleaned,
        re.IGNORECASE
    ))
    if not has_rule_language:
        return ""

    return cleaned[:500]


# ══════════════════════════════════════════════════════════════════════
# PROCEDURAL RULING SAFETY FILTER (added by patch_state_fix)
# ══════════════════════════════════════════════════════════════════════
#
# Q2 fix: prevent the LLM from extracting opposing counsel's
# characterizations of court rulings as if they were record facts.
#
# Real case from v9 run:
#   Example Trust Co's brief said "the Court denied plaintiff's motion in
#   its entirety" and the LLM extracted that as fact #7. But the state
#   court did NOT deny in entirety. The federal court rejected with
#   leave to amend, and the user filed a writ of mandamus instead.
#
# Rule: a fact containing a procedural-ruling word (denied, granted,
# dismissed, sustained, overruled, stricken, vacated, remanded, etc.)
# MUST also contain (a) a date AND (b) a court reference. Otherwise it's
# counsel's characterization and should be marked as untrusted.
# ══════════════════════════════════════════════════════════════════════

import re as _re_filter

_PROCEDURAL_RULING_WORDS = _re_filter.compile(
    r"\b(?:denied|granted|dismissed|sustained|overruled|stricken|"
    r"vacated|remanded|reversed|affirmed|quashed|set\s+aside)\b",
    _re_filter.IGNORECASE,
)

# Date patterns: numeric (3/30/2026, 2026-03-30), or month-name (March 30, 2026)
_DATE_PATTERN = _re_filter.compile(
    r"\b\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4}\b"
    r"|\b\d{4}-\d{2}-\d{2}\b"
    r"|\b(?:January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+\d{1,2}\b",
    _re_filter.IGNORECASE,
)

# Court reference: "the Court", "Judge X", "J. X", "Hon.", "Order", "Opinion"
_COURT_REFERENCE = _re_filter.compile(
    r"\b(?:the\s+court|judge\s+\w+|j\.\s+\w+|hon\.|order|opinion|"
    r"docket|memorandum)\b",
    _re_filter.IGNORECASE,
)


def is_procedural_ruling_safe(fact: str) -> bool:
    """Return True if a fact is safe to treat as a record fact.

    Rules:
      1. If the fact does NOT contain a procedural-ruling word → safe.
      2. If it DOES contain a ruling word, it must ALSO have:
         - a date pattern, AND
         - a court reference
      3. Otherwise it's almost certainly counsel's characterization
         from a brief, not an actual court order.

    Examples:
      "Example Trust Co filed objections on Feb 10, 2026"
        → no ruling word → SAFE
      "The Court denied plaintiff's motion in its entirety"
        → ruling word + court ref but NO date → UNSAFE
      "The Court denied plaintiff's motion on March 30, 2026"
        → ruling word + date + court ref → SAFE
      "Defendant argues the motion should be denied"
        → ruling word but no date → UNSAFE
      "The motion was denied"
        → ruling word, no date, no court ref → UNSAFE
    """
    if not fact or not isinstance(fact, str):
        return False
    if not _PROCEDURAL_RULING_WORDS.search(fact):
        return True  # not a ruling claim, no special check needed
    if not _DATE_PATTERN.search(fact):
        return False  # ruling word without a date — characterization
    if not _COURT_REFERENCE.search(fact):
        return False  # ruling word + date but no court — also characterization
    return True


# BEEZLE_FN_UNUSED 2026-06-13: filter_unsafe_procedural_facts (no static refs in pipeline/)
def filter_unsafe_procedural_facts(facts: list) -> tuple:
    """Split a fact list into (safe, unsafe) facts.

    Returns:
        (safe_facts, unsafe_facts) — both lists.
        Unsafe facts can be logged or shown separately as
        "characterizations" but should NOT be treated as record facts.
    """
    safe = []
    unsafe = []
    for f in facts or []:
        if is_procedural_ruling_safe(f):
            safe.append(f)
        else:
            unsafe.append(f)
    return safe, unsafe
