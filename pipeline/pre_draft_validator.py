"""
pipeline/pre_draft_validator.py — Pre-Draft Validator.

The last line of defense before content reaches the drafter.
Catches:
  - Double negatives ("has not did not")
  - Contradictions ("verification not required" + "verification required")
  - Defense framing leaks
  - Broken grammar from polarity rewrites
  - Empty / junk facts
  - Plaintiff-against-self statements

NO LLM. Pure validation + sanitization.
Returns cleaned content + a list of issues found.
"""

import re
from dataclasses import dataclass, field
from typing import Optional

from core.logger import get_logger
try:
    from pipeline.legal_validity_check import LegalValidityChecker, Claim as M0Claim
    _M0_AVAILABLE = True
except Exception:
    _M0_AVAILABLE = False


log = get_logger("PRE_DRAFT")


# ══════════════════════════════════════════════════════════════════════
# DETECTION PATTERNS
# ══════════════════════════════════════════════════════════════════════

# Double negation patterns (regex)
DOUBLE_NEGATION_PATTERNS = [
    re.compile(r"\b(has\s+not|have\s+not|had\s+not)\s+did\s+not\b", re.IGNORECASE),
    re.compile(r"\b(has\s+not|have\s+not|had\s+not)\s+does\s+not\b", re.IGNORECASE),
    re.compile(r"\b(did\s+not|does\s+not|do\s+not)\s+not\b", re.IGNORECASE),
    re.compile(r"\bnot\s+not\b", re.IGNORECASE),
    re.compile(r"\bnever\s+(?:did|do|does)\s+not\b", re.IGNORECASE),
    re.compile(r"\bfailed\s+to\s+(?:not|fail)\b", re.IGNORECASE),
]

# Defense framing patterns that should never appear in our output
DEFENSE_LEAK_PATTERNS = [
    (re.compile(r"\bjudgment\s+(?:is|was)\s+not\s+warranted\b", re.IGNORECASE),
     "judgment is warranted"),
    (re.compile(r"\bplaintiff\s+(?:has\s+)?failed\s+to\s+comply\b", re.IGNORECASE),
     "the opposing party asserts that plaintiff failed to comply, which is contradicted by the record"),
    (re.compile(r"\bplaintiff'?s?\s+motion\s+(?:should\s+be|is)\s+denied\b", re.IGNORECASE),
     "plaintiff's motion should be granted"),
    (re.compile(r"\bdoes\s+not\s+(?:have|hold)\s+(?:a\s+)?valid\s+claim\b", re.IGNORECASE),
     "has a valid claim"),
    (re.compile(r"\bis\s+entitled\s+to\s+the\s+relief\s+sought\s+by\s+(?:garnishee|defendant)\b", re.IGNORECASE),
     "is not entitled to the relief sought by the opposing party"),
]

# Contradictory pairs (if both phrases appear, flag a contradiction)
CONTRADICTION_PAIRS = [
    (r"\bverification\s+not\s+required\b", r"\bverification\s+required\b"),
    (r"\bservice\s+not\s+required\b", r"\bservice\s+(?:is\s+)?required\b"),
    (r"\bjurisdiction\s+(?:exists|proper)\b", r"\bno\s+jurisdiction\b"),
    (r"\brule\s+does\s+not\s+apply\b", r"\brule\s+applies\b"),
]

# Patterns that indicate plaintiff is being attacked (subject-flip needed)
PLAINTIFF_ATTACK_PATTERNS = [
    re.compile(r"\bplaintiff\s+(?:has\s+)?failed\s+to\b", re.IGNORECASE),
    re.compile(r"\bplaintiff'?s?\s+(?:position|argument|claim)\s+fails\b", re.IGNORECASE),
    re.compile(r"\bplaintiff\s+did\s+not\s+(?:provide|comply|file|serve)\b", re.IGNORECASE),
    re.compile(r"\bplaintiff\s+is\s+(?:incorrect|wrong|mistaken)\b", re.IGNORECASE),
    re.compile(r"\bplaintiff\s+has\s+no\s+(?:valid|legal)\b", re.IGNORECASE),
]

# Empty / junk fact patterns
JUNK_FACT_PATTERNS = [
    re.compile(r"^\s*$"),
    re.compile(r"^\d+\.\s*$"),
    re.compile(r"^[a-z_]+:\s*$"),  # bare issue label
    re.compile(r"^the\s+document\s+is\s+dated", re.IGNORECASE),  # PDF metadata
    re.compile(r"^the\s+order\s+was\s+(?:issued|signed)", re.IGNORECASE),  # not a fact for our brief
]


# ══════════════════════════════════════════════════════════════════════
# DATACLASSES
# ══════════════════════════════════════════════════════════════════════

@dataclass
class ValidationResult:
    cleaned_text: str = ""
    issues_found: list = field(default_factory=list)
    fixes_applied: list = field(default_factory=list)
    is_safe: bool = True

    def to_dict(self) -> dict:
        return {
            "cleaned_text": self.cleaned_text,
            "issues_found": self.issues_found,
            "fixes_applied": self.fixes_applied,
            "is_safe": self.is_safe,
        }


# ══════════════════════════════════════════════════════════════════════
# VALIDATION FUNCTIONS
# ══════════════════════════════════════════════════════════════════════

def fix_double_negatives(text: str) -> tuple[str, list]:
    """Find and fix double negatives.

    "Plaintiff has not did not comply" → "Plaintiff did not comply"
    """
    fixes = []
    cleaned = text

    # Specific fixes for known patterns
    repairs = [
        (r"\b(has\s+not|have\s+not|had\s+not)\s+did\s+not\b", "did not"),
        (r"\b(has\s+not|have\s+not|had\s+not)\s+does\s+not\b", "does not"),
        (r"\bdid\s+not\s+not\b", "did not"),
        (r"\bdoes\s+not\s+not\b", "does not"),
        (r"\bnot\s+not\b", "not"),
        (r"\bfailed\s+to\s+not\b", "failed to"),
        (r"\bfailed\s+to\s+fail\b", "failed"),
    ]

    for pattern, replacement in repairs:
        new_text = re.sub(pattern, replacement, cleaned, flags=re.IGNORECASE)
        if new_text != cleaned:
            fixes.append(f"double_negative: '{pattern}' -> '{replacement}'")
            cleaned = new_text

    return cleaned, fixes


def fix_defense_leaks(text: str) -> tuple[str, list]:
    """Find and rewrite defense framing leaks."""
    fixes = []
    cleaned = text

    for pattern, replacement in DEFENSE_LEAK_PATTERNS:
        if pattern.search(cleaned):
            cleaned = pattern.sub(replacement, cleaned)
            fixes.append(f"defense_leak: replaced with '{replacement}'")

    return cleaned, fixes


def fix_plaintiff_attacks(text: str) -> tuple[str, list]:
    """If text attacks the plaintiff, reframe as opposing party assertion."""
    fixes = []
    cleaned = text

    for pattern in PLAINTIFF_ATTACK_PATTERNS:
        match = pattern.search(cleaned)
        if match:
            attack_text = match.group(0)
            # Wrap the attack in "the opposing party asserts that" framing
            replacement = f"the opposing party asserts that {attack_text.lower()}, which is contradicted by the record"
            cleaned = cleaned.replace(attack_text, replacement, 1)
            fixes.append(f"plaintiff_attack: wrapped '{attack_text[:50]}'")

    return cleaned, fixes


def detect_contradictions(text: str) -> list:
    """Detect contradictory statements in the text."""
    issues = []
    text_lower = text.lower()

    for neg_pat, pos_pat in CONTRADICTION_PAIRS:
        has_neg = bool(re.search(neg_pat, text_lower))
        has_pos = bool(re.search(pos_pat, text_lower))
        if has_neg and has_pos:
            issues.append(f"contradiction: '{neg_pat}' AND '{pos_pat}'")

    return issues


def is_junk_fact(fact: str) -> bool:
    """Return True if a fact is junk (PDF metadata, blank, label-only)."""
    if not fact or not isinstance(fact, str):
        return True
    return any(p.match(fact) for p in JUNK_FACT_PATTERNS)


# BEEZLE_FN_UNUSED 2026-06-13: sanitize_facts (no static refs in pipeline/)
def sanitize_facts(facts: list) -> list:
    """Filter junk and clean each fact in a list."""
    cleaned = []
    for f in facts:
        if not f or not isinstance(f, str):
            continue
        if is_junk_fact(f):
            continue
        # Strip issue label prefix
        f = re.sub(r"^[a-z_]+:\s*", "", f.strip())
        # Strip leading numbering
        f = re.sub(r"^\d+\.\s*", "", f)
        if f and len(f) > 5:
            cleaned.append(f)
    return cleaned


# ══════════════════════════════════════════════════════════════════════
# MAIN VALIDATOR
# ══════════════════════════════════════════════════════════════════════

def validate_text(text: str) -> ValidationResult:
    """Run full validation + sanitization on a text block.

    Returns the cleaned text, list of issues found, and list of fixes applied.
    """
    if not text:
        return ValidationResult(cleaned_text="", is_safe=True)

    result = ValidationResult(cleaned_text=text)
    cleaned = text

    # Pass 1: fix double negatives
    cleaned, fixes = fix_double_negatives(cleaned)
    result.fixes_applied.extend(fixes)

    # Pass 2: fix defense framing leaks
    cleaned, fixes = fix_defense_leaks(cleaned)
    result.fixes_applied.extend(fixes)

    # Pass 3: fix plaintiff attacks
    cleaned, fixes = fix_plaintiff_attacks(cleaned)
    result.fixes_applied.extend(fixes)

    # Pass 4: detect contradictions (don't auto-fix, just flag)
    contradictions = detect_contradictions(cleaned)
    result.issues_found.extend(contradictions)

    # Pass 5: detect remaining double negatives (sanity check)
    for pat in DOUBLE_NEGATION_PATTERNS:
        if pat.search(cleaned):
            result.issues_found.append(f"residual_double_negative: {pat.pattern}")

    # Pass 6: detect remaining plaintiff attacks (sanity check)
    for pat in PLAINTIFF_ATTACK_PATTERNS:
        if pat.search(cleaned):
            result.issues_found.append(f"residual_plaintiff_attack: {pat.pattern}")

    # Pass 7: M0 rule-as-written routing check
    if _M0_AVAILABLE:
        try:
            m0_issues = _m0_check_rules(cleaned)
            for issue in m0_issues:
                result.issues_found.append(issue)
        except Exception as e:
            log.warning("validate", f"M0 check skipped: {e}")

    result.cleaned_text = cleaned
    result.is_safe = len(result.issues_found) == 0

    if result.fixes_applied:
        log.stage("validate", f"Applied {len(result.fixes_applied)} fixes")
    if result.issues_found:
        log.warning("validate", f"Found {len(result.issues_found)} unresolved issues")

    return result


# ??????????????????????????????????????????????????????????????????????
# M0 ROUTING HELPER ? call LegalValidityChecker on detected rule cites
# ??????????????????????????????????????????????????????????????????????

_RULE_CITE_PATTERN = re.compile(
    r"\b("
    r"FRCP\s+\d+(?:\([a-z]\)(?:\(\d+\))?)?|"
    r"Fed\.?\s*R\.?\s*Civ\.?\s*P\.?\s*\d+|"
    r"FRCrP\s+\d+|FRE\s+\d+|"
    r"\d+\s*U\.?S\.?C\.?\s*§?\s*\d+(?:\([a-z]\))?|"
    r"L\.?R\.?\s*\d+\.\d+\([a-z]\)|"
    r"Rule\s+\d+(?:\([a-z]\)(?:\(\d+\))?)?"
    r")",
    re.IGNORECASE,
)

_CONTEXT_WINDOW = 200  # chars around each cite to capture intended use

def _m0_check_rules(text: str) -> list:
    """Scan text for rule citations; run each through M0 router.
    Returns list of issue strings for any rule misuse detected."""
    if not _M0_AVAILABLE or not text:
        return []
    issues = []
    checker = LegalValidityChecker()
    seen_rules = set()
    for m in _RULE_CITE_PATTERN.finditer(text):
        rule_id = m.group(1).strip()
        if rule_id.lower() in seen_rules:
            continue
        seen_rules.add(rule_id.lower())
        # Grab context window around the cite to infer intended_use
        s = max(0, m.start() - _CONTEXT_WINDOW)
        e = min(len(text), m.end() + _CONTEXT_WINDOW)
        context = text[s:e]
        claim = M0Claim(
            claim_id=f"text_cite_{m.start()}",
            grievance="",
            asserted_rule=rule_id,
            intended_use=context,
            requested_relief="",
            court="EDPA",  # default; caller can override later
        )
        try:
            res = checker.validate_claim(claim)
            if res.validity == "FAIL":
                msg = "rule_misuse[" + str(rule_id) + "]: " + (res.error or "misuse detected")
                if res.reroute_to_rule:
                    msg += " - reroute to " + str(res.reroute_to_rule)
                issues.append(msg)
        except Exception:
            continue
    return issues


# BEEZLE_FN_UNUSED 2026-06-13: validate_section (no static refs in pipeline/)
def validate_section(section_text: str) -> str:
    """Convenience: validate and return cleaned text only."""
    return validate_text(section_text).cleaned_text
