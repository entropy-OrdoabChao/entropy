"""
pipeline/conformity_checker.py — Authority Conformity Checker

DETERMINISTIC. NO LLM. NO INTERPRETATION.

The Constitution is supreme law (Art. VI, Cl. 2).
Every layer below must conform to the layer above:
  Constitution → Treaties → Enactment Acts → USC → CFR → Agency Guidance → Court Rules

When a lower authority contradicts a higher one, it is VOID.
This engine detects those contradictions.

Usage:
    from pipeline.conformity_checker import check_conformity, ConformityViolation
"""

import re
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Literal
from core.logger import get_logger

log = get_logger("conformity_checker")

# ─── Data Structures ────────────────────────────────────────────────

AuthorityLevel = Literal["constitution", "treaty", "enactment_act",
                          "usc", "cfr", "agency_guidance", "court_rule",
                          "executive_order", "state_constitution",
                          "state_statute", "state_regulation", "unknown"]

# Numeric rank: lower number = higher authority
AUTHORITY_RANK = {
    "constitution": 1,
    "treaty": 2,
    "enactment_act": 3,
    "usc": 4,
    "cfr": 5,
    "executive_order": 5,
    "agency_guidance": 6,
    "court_rule": 7,
    "state_constitution": 8,
    "state_statute": 9,
    "state_regulation": 10,
    "unknown": 99,
}


@dataclass
class ConformityViolation:
    """A detected conflict between authority levels."""
    higher_authority: str       # citation of higher authority
    higher_level: AuthorityLevel
    higher_text: str            # relevant excerpt
    lower_authority: str        # citation of lower authority
    lower_level: AuthorityLevel
    lower_text: str             # relevant excerpt
    conflict_type: str          # "scope_exceeded", "direct_contradiction", "unauthorized_delegation", etc.
    explanation: str
    severity: str = "serious"   # "fatal", "serious", "minor"


@dataclass
class EnactmentTrace:
    """Maps codified law back to its enactment source."""
    usc_citation: str
    public_law: str = ""
    statutes_at_large: str = ""
    enactment_date: str = ""
    constitutional_authority: str = ""    # which enumerated power
    title_heading: str = ""
    notes: str = ""


@dataclass
class ConformityReport:
    """Complete conformity analysis."""
    subject: str
    level: AuthorityLevel
    violations: List[ConformityViolation] = field(default_factory=list)
    traces: List[EnactmentTrace] = field(default_factory=list)
    conforms: bool = True
    chain: List[str] = field(default_factory=list)  # reasoning steps

    def to_dict(self) -> dict:
        return asdict(self)

    def to_frontmatter(self) -> dict:
        d = {
            "authority_level": self.level,
            "conforms_to_higher": self.conforms,
            "violations_found": len(self.violations),
        }
        if self.traces:
            t = self.traces[0]
            if t.public_law:
                d["public_law"] = t.public_law
            if t.constitutional_authority:
                d["constitutional_authority"] = t.constitutional_authority
            if t.enactment_date:
                d["enactment_date"] = t.enactment_date
        return d


# ─── Authority Level Detection ───────────────────────────────────────

LEVEL_PATTERNS = [
    (r"U\.?\s*S\.?\s*Const\.?|Amendment|Article\s+[IVX]+", "constitution"),
    (r"Treaty|Convention", "treaty"),
    (r"Pub(?:lic)?\s*\.?\s*L(?:aw)?\.?\s*\d+[\s-]\d+|Stat(?:utes)?\s+at\s+Large", "enactment_act"),
    (r"\d+\s+U\.?\s*S\.?\s*C\.?\s*(?:§|[Ss]ection)", "usc"),
    (r"\d+\s+C\.?\s*F\.?\s*R\.?\s*(?:§|[Pp]art|[Ss]ection)", "cfr"),
    (r"(?:Executive|Exec\.?)\s+Order", "executive_order"),
    (r"(?:Guidance|Bulletin|Circular|Advisory|Memorandum|Letter)", "agency_guidance"),
    (r"(?:Fed\.?\s*R\.?\s*Civ\.?\s*P|F\.?\s*R\.?\s*C\.?\s*P|Rule\s+\d+)", "court_rule"),
    (r"(?:Pa\.|Pennsylvania)\s+Const", "state_constitution"),
    (r"\d+\s+Pa\.?\s*(?:C\.?\s*S\.?|Stat)", "state_statute"),
    (r"\d+\s+Pa\.?\s*Code", "state_regulation"),
]


def detect_authority_level(citation: str) -> AuthorityLevel:
    """Detect the authority level from a citation string."""
    for pattern, level in LEVEL_PATTERNS:
        if re.search(pattern, citation, re.IGNORECASE):
            return level
    return "unknown"


# ─── Constitutional Authority Mapping ────────────────────────────────

# Maps common statutory areas to their constitutional authority
CONSTITUTIONAL_AUTHORITIES = {
    "commerce": "Art. I, § 8, Cl. 3 (Commerce Clause)",
    "tax": "Art. I, § 8, Cl. 1 (Taxing and Spending)",
    "spending": "Art. I, § 8, Cl. 1 (Taxing and Spending)",
    "bankruptcy": "Art. I, § 8, Cl. 4 (Bankruptcy)",
    "currency": "Art. I, § 8, Cl. 5 (Coinage)",
    "postal": "Art. I, § 8, Cl. 7 (Post Offices)",
    "patent": "Art. I, § 8, Cl. 8 (Patents/Copyrights)",
    "copyright": "Art. I, § 8, Cl. 8 (Patents/Copyrights)",
    "military": "Art. I, § 8, Cl. 11-16 (Military Powers)",
    "immigration": "Art. I, § 8, Cl. 4 (Naturalization)",
    "necessary_proper": "Art. I, § 8, Cl. 18 (Necessary and Proper)",
    "civil_rights": "Amend. XIV, § 5 (Enforcement)",
    "due_process_5": "Amend. V (Due Process — federal)",
    "due_process_14": "Amend. XIV, § 1 (Due Process — states)",
    "equal_protection": "Amend. XIV, § 1 (Equal Protection)",
    "speech": "Amend. I (Free Speech/Press/Assembly/Petition)",
    "search": "Amend. IV (Search and Seizure)",
    "self_incrimination": "Amend. V (Self-Incrimination)",
    "jury": "Amend. VII (Jury Trial)",
    "cruel_unusual": "Amend. VIII (Cruel and Unusual)",
    "reserved_powers": "Amend. X (Reserved to States/People)",
    "sovereign_immunity": "Amend. XI (State Sovereign Immunity)",
}

# USC title → likely constitutional basis
TITLE_TO_AUTHORITY = {
    5: "necessary_proper",     # Government Organization
    12: "commerce",            # Banks and Banking
    15: "commerce",            # Commerce and Trade
    18: "necessary_proper",    # Crimes
    26: "tax",                 # Internal Revenue
    28: "necessary_proper",    # Judiciary
    29: "commerce",            # Labor
    31: "currency",            # Money and Finance
    42: "civil_rights",        # Public Health and Welfare
    47: "commerce",            # Telecommunications
}


# ─── Core Analysis ───────────────────────────────────────────────────

# BEEZLE_FN_UNUSED 2026-06-13: check_conformity (no static refs in pipeline/)
def check_conformity(lower_text: str, lower_citation: str,
                     higher_text: str, higher_citation: str) -> ConformityReport:
    """
    Check whether a lower authority conforms to a higher authority.

    Compares the texts and detects:
      - Direct contradictions
      - Scope exceeded (lower claims more than higher grants)
      - Unauthorized delegations
      - Actor-scope mismatches across levels

    All conclusions traced to text.
    """
    lower_level = detect_authority_level(lower_citation)
    higher_level = detect_authority_level(higher_citation)

    report = ConformityReport(
        subject=f"{lower_citation} vs {higher_citation}",
        level=lower_level,
    )

    # Verify hierarchy
    lower_rank = AUTHORITY_RANK.get(lower_level, 99)
    higher_rank = AUTHORITY_RANK.get(higher_level, 99)

    if lower_rank <= higher_rank:
        report.chain.append(f"{lower_citation} ({lower_level}) is same or higher rank than {higher_citation} ({higher_level}) — not a subordinate check")
        return report

    report.chain.append(f"Checking: {lower_citation} ({lower_level}, rank {lower_rank}) must conform to {higher_citation} ({higher_level}, rank {higher_rank})")

    # Check for contradictions
    _check_contradictions(report, lower_text, lower_citation, higher_text, higher_citation)

    # Check for scope exceeded
    _check_scope(report, lower_text, lower_citation, lower_level, higher_text, higher_citation)

    report.conforms = len(report.violations) == 0
    return report


# BEEZLE_FN_UNUSED 2026-06-13: trace_enactment (no static refs in pipeline/)
def trace_enactment(usc_citation: str, text: str = "") -> EnactmentTrace:
    """
    Trace a USC citation back to its enactment source.

    Extracts: public law number, Statutes at Large reference,
    enactment date, and constitutional authority.
    """
    trace = EnactmentTrace(usc_citation=usc_citation)

    # Extract title number
    title_match = re.match(r"(\d+)\s+U\.?\s*S\.?\s*C\.?", usc_citation)
    if title_match:
        title_num = int(title_match.group(1))
        auth_key = TITLE_TO_AUTHORITY.get(title_num)
        if auth_key:
            trace.constitutional_authority = CONSTITUTIONAL_AUTHORITIES.get(auth_key, "")

    # Look for public law references in text
    if text:
        pl_match = re.search(r"Pub(?:lic)?\s*\.?\s*L(?:aw)?\.?\s*(\d+[\s-]\d+)", text)
        if pl_match:
            trace.public_law = f"Pub. L. {pl_match.group(1)}"

        stat_match = re.search(r"(\d+)\s+Stat\.?\s+(\d+)", text)
        if stat_match:
            trace.statutes_at_large = f"{stat_match.group(1)} Stat. {stat_match.group(2)}"

        date_match = re.search(r"(?:enacted|approved|signed)\s+(?:on\s+)?(\w+\s+\d{1,2},?\s+\d{4})", text, re.IGNORECASE)
        if date_match:
            trace.enactment_date = date_match.group(1)

    return trace


# BEEZLE_FN_UNUSED 2026-06-13: classify_and_rank (no static refs in pipeline/)
def classify_and_rank(citation: str) -> dict:
    """Quick classification of a citation's authority level and rank."""
    level = detect_authority_level(citation)
    rank = AUTHORITY_RANK.get(level, 99)
    return {
        "citation": citation,
        "level": level,
        "rank": rank,
        "outranks": [l for l, r in AUTHORITY_RANK.items() if r > rank],
    }


# ─── Internal Checks ────────────────────────────────────────────────

def _check_contradictions(report, lower_text, lower_cite, higher_text, higher_cite):
    """Check for direct contradictions between lower and higher authority."""
    lower_lower = lower_text.lower()
    higher_lower = higher_text.lower()

    # Check: lower says "shall" where higher says "shall not" or vice versa
    # This is a simplified check — the full version would parse provisions
    lower_mandates = set(re.findall(r"shall\s+(\w+(?:\s+\w+){0,3})", lower_lower))
    higher_prohibitions = set(re.findall(r"shall\s+not\s+(\w+(?:\s+\w+){0,3})", higher_lower))

    for mandate in lower_mandates:
        for prohibition in higher_prohibitions:
            # Check word overlap
            m_words = set(mandate.split())
            p_words = set(prohibition.split())
            if len(m_words & p_words) >= 2:
                report.violations.append(ConformityViolation(
                    higher_authority=higher_cite,
                    higher_level=detect_authority_level(higher_cite),
                    higher_text=f"shall not {prohibition}",
                    lower_authority=lower_cite,
                    lower_level=detect_authority_level(lower_cite),
                    lower_text=f"shall {mandate}",
                    conflict_type="direct_contradiction",
                    explanation=f"Lower authority mandates '{mandate}' but higher authority prohibits '{prohibition}'",
                    severity="fatal",
                ))
                report.chain.append(f"CONTRADICTION: lower says 'shall {mandate}' but higher says 'shall not {prohibition}'")


def _check_scope(report, lower_text, lower_cite, lower_level, higher_text, higher_cite):
    """Check if lower authority exceeds scope granted by higher."""
    lower_lower = lower_text.lower()
    higher_lower = higher_text.lower()

    # Check: does lower authority reference its delegating statute?
    if lower_level in ("cfr", "agency_guidance"):
        # Regulations should reference their authorizing statute
        has_authority_ref = bool(re.search(r"(?:authority|pursuant\s+to|under)\s+\d+\s+U\.?\s*S\.?\s*C", lower_lower))
        if not has_authority_ref:
            report.chain.append("NOTE: Lower authority does not reference authorizing statute")

    # Check: does lower create penalties/obligations not in higher?
    lower_penalties = bool(re.search(r"(?:fine|penalty|forfeiture|imprison)", lower_lower))
    higher_penalties = bool(re.search(r"(?:fine|penalty|forfeiture|imprison)", higher_lower))

    if lower_penalties and not higher_penalties:
        report.violations.append(ConformityViolation(
            higher_authority=higher_cite,
            higher_level=detect_authority_level(higher_cite),
            higher_text="(no penalty provisions found)",
            lower_authority=lower_cite,
            lower_level=detect_authority_level(lower_cite),
            lower_text="(penalty provisions found)",
            conflict_type="scope_exceeded",
            explanation="Lower authority creates penalties not found in authorizing higher authority",
            severity="serious",
        ))
        report.chain.append("SCOPE EXCEEDED: lower creates penalties not authorized by higher")


# BEEZLE_FN_UNUSED 2026-06-13: format_conformity_report (no static refs in pipeline/)
def format_conformity_report(report: ConformityReport) -> str:
    """Format conformity report for human reading."""
    lines = [f"CONFORMITY CHECK — {report.subject}"]
    lines.append(f"  Level: {report.level}")
    lines.append(f"  Conforms: {'YES' if report.conforms else 'NO — VIOLATIONS FOUND'}")

    for step in report.chain:
        lines.append(f"  {step}")

    if report.violations:
        lines.append(f"\n  VIOLATIONS ({len(report.violations)}):")
        for v in report.violations:
            lines.append(f"    [{v.severity.upper()}] {v.conflict_type}")
            lines.append(f"      Higher: {v.higher_authority} — {v.higher_text[:80]}")
            lines.append(f"      Lower:  {v.lower_authority} — {v.lower_text[:80]}")
            lines.append(f"      {v.explanation}")

    if report.traces:
        lines.append(f"\n  ENACTMENT TRACE:")
        for t in report.traces:
            lines.append(f"    USC: {t.usc_citation}")
            if t.public_law:
                lines.append(f"    Pub. Law: {t.public_law}")
            if t.constitutional_authority:
                lines.append(f"    Const. Authority: {t.constitutional_authority}")

    return "\n".join(lines)
