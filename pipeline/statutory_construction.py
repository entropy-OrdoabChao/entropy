"""
pipeline/statutory_construction.py — Rules of Statutory Construction

DETERMINISTIC. NO LLM. NO INTERPRETATION.

Encodes the formal rules courts use to read statutes. These are not opinions —
they are binding canons of construction that control how every word, list,
and definition in a statute must be interpreted.

Rules encoded:
  1. Expressio unius est exclusio alterius — listing specific items excludes unlisted
  2. Ejusdem generis — general term after specific list = same category only
  3. Rule of exclusion — defined as X excludes from being Y in same statute
  4. Noscitur a sociis — word is known by the company it keeps
  5. Means vs includes — "means" = closed, "includes" = open/expansive
  6. Whole act rule — every word has meaning, no surplus
  7. Last antecedent rule — qualifier modifies only the last preceding term
  8. Title headings do not control — 1 U.S.C. § 1 note
  9. Shall vs may — "shall" = mandatory, "may" = discretionary
  10. In pari materia — same-subject statutes read together (NOT definition import)
  11. Remedial statutes construed liberally
  12. Penal statutes construed strictly (rule of lenity)

Usage:
    from pipeline.statutory_construction import (
        check_exclusion_rule,
        check_means_vs_includes,
        check_ejusdem_generis,
        apply_construction_rules,
        classify_definition_scope,
    )

    # Check if two definitions in the same statute are mutually exclusive
    result = check_exclusion_rule("security", "note", statute_text)

    # Full construction analysis on a provision
    analysis = apply_construction_rules(provision_text, citation="15 U.S.C. § 1681a")
"""

import re
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Literal, Tuple
from core.logger import get_logger

log = get_logger("statutory_construction")

# ─── Data Structures ────────────────────────────────────────────────

DefinitionScope = Literal["closed", "expansive", "expansive_enumerated", "undefined", "cross_referenced"]

ConstructionRule = Literal[
    "expressio_unius", "ejusdem_generis", "exclusion_rule",
    "noscitur_a_sociis", "means_closed", "includes_expansive",
    "whole_act", "last_antecedent", "title_no_control",
    "shall_mandatory", "may_discretionary", "in_pari_materia",
    "remedial_liberal", "penal_strict", "surplusage_avoided",
]


@dataclass
class DefinitionClassification:
    """How a statutory definition should be read."""
    term: str
    scope: DefinitionScope
    definition_text: str
    excludes: List[str] = field(default_factory=list)
    limited_to: List[str] = field(default_factory=list)
    cross_ref_to: str = ""
    source_text: str = ""
    rules_applied: List[str] = field(default_factory=list)


@dataclass
class ListAnalysis:
    """Analysis of a statutory list (enumeration)."""
    items: List[str]
    general_term: str = ""
    is_exhaustive: bool = False
    category: str = ""
    rules_applied: List[str] = field(default_factory=list)
    source_text: str = ""


@dataclass
class ExclusionResult:
    """Result of checking mutual exclusion between two terms."""
    term_a: str
    term_b: str
    mutually_exclusive: bool
    explanation: str
    source_text: str = ""


@dataclass
class ConstructionAnalysis:
    """Complete statutory construction analysis for a provision."""
    citation: str
    definitions: List[DefinitionClassification] = field(default_factory=list)
    lists: List[ListAnalysis] = field(default_factory=list)
    exclusions: List[ExclusionResult] = field(default_factory=list)
    rules_triggered: List[str] = field(default_factory=list)
    mandatory_duties: List[str] = field(default_factory=list)
    discretionary_powers: List[str] = field(default_factory=list)
    construction_notes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_frontmatter(self) -> dict:
        d = {
            "construction_rules": self.rules_triggered,
        }
        if self.definitions:
            d["definition_scopes"] = [
                {
                    "term": dc.term,
                    "scope": dc.scope,
                    "excludes": dc.excludes if dc.excludes else None,
                }
                for dc in self.definitions
            ]
        if self.exclusions:
            d["mutual_exclusions"] = [
                {"a": e.term_a, "b": e.term_b, "exclusive": e.mutually_exclusive}
                for e in self.exclusions if e.mutually_exclusive
            ]
        if self.mandatory_duties:
            d["mandatory_count"] = len(self.mandatory_duties)
        if self.discretionary_powers:
            d["discretionary_count"] = len(self.discretionary_powers)
        return d


# ─── Rule 1: Means vs Includes ──────────────────────────────────────

# "means" = closed definition (only what is listed)
# "includes" = expansive (at minimum what is listed, possibly more)
# "includes only" or "means and includes only" = closed despite "includes"

MEANS_PATTERN = re.compile(
    r'["\u201c]([^"\u201d]+)["\u201d]\s+means\s+(.+?)(?:\.|;|$)',
    re.IGNORECASE | re.DOTALL
)

INCLUDES_PATTERN = re.compile(
    r'["\u201c]([^"\u201d]+)["\u201d]\s+includes?\s+(.+?)(?:\.|;|$)',
    re.IGNORECASE | re.DOTALL
)

INCLUDES_ONLY_PATTERN = re.compile(
    r'["\u201c]([^"\u201d]+)["\u201d]\s+(?:means\s+and\s+)?includes?\s+only\s+',
    re.IGNORECASE
)

DEFINED_AS_PATTERN = re.compile(
    r'["\u201c]([^"\u201d]+)["\u201d]\s+(?:is\s+defined\s+as|shall\s+mean|has\s+the\s+meaning)\s+(.+?)(?:\.|;|$)',
    re.IGNORECASE | re.DOTALL
)


def _has_expansion_clause(text_lower: str) -> bool:
    """True only when the definition text itself supplies an expansion clause
    (7701(c)-style). Absent this, "includes" is an enumerated floor, not open-ended."""
    return bool(re.search(
        r'shall\s+not\s+be\s+deemed\s+to\s+exclude'
        r'|not\s+limited\s+to'
        r'|without\s+limitation'
        r'|among\s+other\s+things'
        r'|but\s+is\s+not\s+limited', text_lower))


def classify_definition_scope(term: str, definition_text: str) -> DefinitionClassification:
    """
    Classify whether a definition is closed or expansive.

    Rules:
      - "X means Y" → closed (only Y)
      - "X includes Y" (no expansion clause) → expansive_enumerated (floor = Y; canons govern beyond)
      - "X includes Y ... [7701(c)-style clause]" → expansive
      - "X includes only Y" → closed (despite "includes")
      - "X means and includes" → context-dependent, default expansive
    """
    text_lower = definition_text.lower()
    result = DefinitionClassification(
        term=term,
        scope="undefined",
        definition_text=definition_text,
        source_text=definition_text[:200],
    )

    # Check "includes only" first (narrowest)
    if re.search(r'\bincludes?\s+only\b', text_lower):
        result.scope = "closed"
        result.rules_applied.append("includes_only_is_closed")
        result.limited_to = _extract_list_items(definition_text)
        return result

    # Check "means" (closed)
    if re.search(r'\bmeans\b', text_lower) and not re.search(r'\bincludes?\b', text_lower):
        result.scope = "closed"
        result.rules_applied.append("means_closed")
        result.limited_to = _extract_list_items(definition_text)
        return result

    # Check "includes" without "means".
    # CORRECTED (user 2026-07-05): bare "includes" is NOT automatically expansive.
    # It is expansive only where the statute ITSELF supplies an expansion clause
    # (e.g. 26 U.S.C. 7701(c)-style "shall not be deemed to exclude..." / "including but not
    # limited to"). Absent such a clause the enumeration defines the class and the default
    # canons (ejusdem generis / expressio unius) govern -> classify as "expansive_enumerated"
    # (floor = the listed items; reaching beyond must be argued, never assumed).
    if re.search(r'\bincludes?\b', text_lower) and not re.search(r'\bmeans\b', text_lower):
        if _has_expansion_clause(text_lower):
            result.scope = "expansive"
            result.rules_applied.append("includes_expansive_by_clause")
        else:
            result.scope = "expansive_enumerated"
            result.rules_applied.append("includes_enumerated_floor_canons_govern")
            result.limited_to = _extract_list_items(definition_text)
        return result

    # "means and includes": closed frame per "means", enumeration listed via "includes".
    if re.search(r'\bmeans\b', text_lower) and re.search(r'\bincludes?\b', text_lower):
        if _has_expansion_clause(text_lower):
            result.scope = "expansive"
            result.rules_applied.append("means_and_includes_expansive_by_clause")
        else:
            result.scope = "expansive_enumerated"
            result.rules_applied.append("means_and_includes_enumerated_canons_govern")
            result.limited_to = _extract_list_items(definition_text)
        return result

    # Cross-reference check
    if re.search(r'(?:as\s+defined\s+in|has\s+the\s+(?:same\s+)?meaning\s+(?:given|set\s+forth)\s+in)\s+', text_lower):
        result.scope = "cross_referenced"
        result.rules_applied.append("cross_referenced_definition")
        # Try to extract the reference
        ref_match = re.search(
            r'(?:as\s+defined\s+in|meaning\s+(?:given|set\s+forth)\s+in)\s+(?:section\s+)?(.+?)(?:\.|;|,|$)',
            text_lower
        )
        if ref_match:
            result.cross_ref_to = ref_match.group(1).strip()
        return result

    # "is defined as" / "shall mean" (closed by default)
    if re.search(r'\b(?:is\s+defined\s+as|shall\s+mean)\b', text_lower):
        result.scope = "closed"
        result.rules_applied.append("defined_as_closed")
        result.limited_to = _extract_list_items(definition_text)
        return result

    return result


def check_means_vs_includes(text: str) -> List[DefinitionClassification]:
    """
    Extract all definitions from text and classify each as closed/expansive.

    Returns list of DefinitionClassification for every "X means Y" or
    "X includes Y" pattern found.
    """
    results = []
    seen_terms = set()

    for pattern in [MEANS_PATTERN, INCLUDES_PATTERN, DEFINED_AS_PATTERN]:
        for match in pattern.finditer(text):
            term = match.group(1).strip()
            defn_text = match.group(2).strip()
            if term.lower() in seen_terms:
                continue
            seen_terms.add(term.lower())
            result = classify_definition_scope(term, defn_text)
            result.source_text = match.group(0)[:200]
            results.append(result)

    return results


# ─── Rule 2: Exclusion Rule ─────────────────────────────────────────

def check_exclusion_rule(term_a: str, term_b: str, statute_text: str) -> ExclusionResult:
    """
    Check if two terms in the same statute are mutually exclusive.

    Rule: If a statute separately defines X and Y, and the definitions
    do not overlap, then something that is X cannot be Y and vice versa.

    Also checks for explicit exclusion language:
      - "does not include"
      - "excludes"
      - "other than"
      - "is not a"
    """
    text_lower = statute_text.lower()
    a_lower = term_a.lower()
    b_lower = term_b.lower()

    # Check explicit exclusion
    explicit_patterns = [
        rf'["\u201c]{re.escape(a_lower)}["\u201d]\s+(?:does\s+not\s+include|excludes?)\s+.*?\b{re.escape(b_lower)}\b',
        rf'["\u201c]{re.escape(b_lower)}["\u201d]\s+(?:does\s+not\s+include|excludes?)\s+.*?\b{re.escape(a_lower)}\b',
        rf'\b{re.escape(a_lower)}\b\s+(?:other\s+than|except)\s+.*?\b{re.escape(b_lower)}\b',
        rf'\b{re.escape(b_lower)}\b\s+(?:other\s+than|except)\s+.*?\b{re.escape(a_lower)}\b',
        rf'\b{re.escape(a_lower)}\b\s+(?:is\s+not|shall\s+not\s+be)\s+(?:a\s+|an\s+)?{re.escape(b_lower)}\b',
        rf'\b{re.escape(b_lower)}\b\s+(?:is\s+not|shall\s+not\s+be)\s+(?:a\s+|an\s+)?{re.escape(a_lower)}\b',
    ]

    for pattern in explicit_patterns:
        match = re.search(pattern, text_lower)
        if match:
            return ExclusionResult(
                term_a=term_a,
                term_b=term_b,
                mutually_exclusive=True,
                explanation=(
                    f"Explicit exclusion: statute text excludes '{term_a}' from '{term_b}' "
                    f"or vice versa. If something is defined as a {term_a}, it is excluded "
                    f"from the definition of {term_b}."
                ),
                source_text=match.group(0)[:150],
            )

    # Check if both terms are separately defined with non-overlapping definitions
    defs = check_means_vs_includes(statute_text)
    def_a = next((d for d in defs if d.term.lower() == a_lower), None)
    def_b = next((d for d in defs if d.term.lower() == b_lower), None)

    if def_a and def_b:
        # Both defined — check if they're closed and non-overlapping
        if def_a.scope == "closed" and def_b.scope == "closed":
            items_a = set(w.lower() for w in def_a.limited_to)
            items_b = set(w.lower() for w in def_b.limited_to)
            overlap = items_a & items_b
            if not overlap and items_a and items_b:
                return ExclusionResult(
                    term_a=term_a,
                    term_b=term_b,
                    mutually_exclusive=True,
                    explanation=(
                        f"Both '{term_a}' and '{term_b}' are defined with 'means' (closed scope) "
                        f"in the same statute with non-overlapping items. Under the rule of exclusion, "
                        f"something that falls within the definition of '{term_a}' is excluded from "
                        f"the definition of '{term_b}' and conversely."
                    ),
                )

    return ExclusionResult(
        term_a=term_a,
        term_b=term_b,
        mutually_exclusive=False,
        explanation=f"No mutual exclusion detected between '{term_a}' and '{term_b}' in this text.",
    )


# ─── Rule 3: Expressio Unius ────────────────────────────────────────

# BEEZLE_FN_UNUSED 2026-06-13: check_expressio_unius (no static refs in pipeline/)
def check_expressio_unius(listed_items: List[str], candidate: str,
                           statute_text: str = "") -> dict:
    """
    Expressio unius est exclusio alterius — the expression of one thing
    is the exclusion of another.

    If a statute lists specific items, anything NOT on the list is excluded.

    Args:
        listed_items: items explicitly listed in the statute
        candidate: item to test for inclusion
        statute_text: optional full text for context

    Returns:
        dict with included (bool), explanation, and rule
    """
    listed_lower = [item.lower().strip() for item in listed_items]
    candidate_lower = candidate.lower().strip()

    if candidate_lower in listed_lower:
        return {
            "included": True,
            "candidate": candidate,
            "explanation": f"'{candidate}' is expressly listed in the statute.",
            "rule": "expressio_unius",
        }

    # Check for partial matches (e.g., "federal savings bank" matches "bank")
    for item in listed_lower:
        if candidate_lower in item or item in candidate_lower:
            return {
                "included": True,
                "candidate": candidate,
                "explanation": f"'{candidate}' is encompassed by listed item '{item}'.",
                "rule": "expressio_unius_partial",
            }

    return {
        "included": False,
        "candidate": candidate,
        "explanation": (
            f"'{candidate}' is NOT listed. Under expressio unius est exclusio alterius, "
            f"the express mention of [{', '.join(listed_items[:5])}] excludes unlisted items. "
            f"Congress knew how to include '{candidate}' and chose not to."
        ),
        "rule": "expressio_unius",
    }


# ─── Rule 4: Ejusdem Generis ────────────────────────────────────────

GENERAL_TERMS = {
    "other", "others", "similar", "such", "like", "related",
    "any other", "all other", "and other", "or other",
    "other similar", "other such", "and the like",
}


# BEEZLE_FN_UNUSED 2026-06-13: check_ejusdem_generis (no static refs in pipeline/)
def check_ejusdem_generis(specific_items: List[str], general_term: str,
                           candidate: str) -> dict:
    """
    Ejusdem generis — when a general term follows specific terms,
    the general term is limited to the same category as the specific terms.

    Args:
        specific_items: the specific terms preceding the general term
        general_term: the general catch-all term (e.g., "other vehicles")
        candidate: item to test against the inferred category

    Returns:
        dict with included (bool), inferred_category, explanation
    """
    if not specific_items:
        return {
            "included": True,
            "candidate": candidate,
            "explanation": "No specific items to constrain the general term.",
            "rule": "ejusdem_generis",
        }

    # Infer category from specific items
    category = _infer_category(specific_items)

    return {
        "included": None,  # requires human/LLM judgment on category membership
        "candidate": candidate,
        "inferred_category": category,
        "specific_items": specific_items,
        "general_term": general_term,
        "explanation": (
            f"Under ejusdem generis, '{general_term}' following "
            f"[{', '.join(specific_items[:5])}] is limited to the same category: "
            f"'{category}'. '{candidate}' must be of the same kind to be covered."
        ),
        "rule": "ejusdem_generis",
    }


# ─── Rule 5: Last Antecedent ────────────────────────────────────────

# BEEZLE_FN_UNUSED 2026-06-13: check_last_antecedent (no static refs in pipeline/)
def check_last_antecedent(clause: str) -> dict:
    """
    Last antecedent rule — a qualifying phrase modifies only the
    immediately preceding term, not the entire list.

    "A, B, and C who are D" → only C must be D (not A or B).

    Detects common patterns and identifies what the qualifier modifies.
    """
    # Pattern: "X, Y, and Z [qualifier]"
    match = re.search(
        r'(.+?),\s+(?:and|or)\s+(\w+(?:\s+\w+)?)\s+(who\s+|that\s+|which\s+|where\s+|when\s+|if\s+)(.+?)(?:\.|;|$)',
        clause, re.IGNORECASE
    )
    if match:
        preceding_items = match.group(1)
        last_item = match.group(2)
        qualifier_type = match.group(3).strip()
        qualifier = match.group(4).strip()

        return {
            "preceding_items": preceding_items,
            "last_item": last_item,
            "qualifier": f"{qualifier_type}{qualifier}",
            "modifies": last_item,
            "explanation": (
                f"Under the last antecedent rule, the qualifier '{qualifier_type}{qualifier}' "
                f"modifies only '{last_item}', not the preceding items [{preceding_items}]. "
                f"The qualifying phrase attaches to the immediately preceding term."
            ),
            "rule": "last_antecedent",
        }

    return {
        "preceding_items": "",
        "last_item": "",
        "qualifier": "",
        "modifies": "",
        "explanation": "No last-antecedent pattern detected.",
        "rule": "last_antecedent",
    }


# ─── Rule 6: Shall vs May ───────────────────────────────────────────

def classify_shall_may(text: str) -> dict:
    """
    Classify provisions as mandatory (shall) or discretionary (may).

    Returns counts and extracted provisions for each category.
    """
    shall_provisions = []
    may_provisions = []

    for match in re.finditer(r'([^.;]*\bshall\b(?:\s+not)?\s+\w+[^.;]*)[.;]', text, re.IGNORECASE):
        provision = match.group(1).strip()
        if len(provision) > 20:
            shall_provisions.append(provision[:150])

    for match in re.finditer(r'([^.;]*\bmay\b(?:\s+not)?\s+\w+[^.;]*)[.;]', text, re.IGNORECASE):
        provision = match.group(1).strip()
        if len(provision) > 20:
            # Exclude "may not" which is prohibitory (= shall not)
            if re.search(r'\bmay\s+not\b', provision, re.IGNORECASE):
                shall_provisions.append(provision[:150])
            else:
                may_provisions.append(provision[:150])

    return {
        "mandatory_count": len(shall_provisions),
        "discretionary_count": len(may_provisions),
        "mandatory": shall_provisions[:10],
        "discretionary": may_provisions[:10],
        "rules_applied": ["shall_mandatory", "may_discretionary"],
    }


# ─── Rule 7: Whole Act / No Surplusage ──────────────────────────────

# BEEZLE_FN_UNUSED 2026-06-13: check_surplusage (no static refs in pipeline/)
def check_surplusage(term_a: str, term_b: str, statute_text: str) -> dict:
    """
    Whole act rule / surplusage avoidance — if a statute uses two
    different words, they must mean different things.

    If "employee" and "officer" both appear, they are NOT synonyms.
    Each must have independent meaning.
    """
    text_lower = statute_text.lower()
    a_lower = term_a.lower()
    b_lower = term_b.lower()

    a_count = len(re.findall(r'\b' + re.escape(a_lower) + r'\b', text_lower))
    b_count = len(re.findall(r'\b' + re.escape(b_lower) + r'\b', text_lower))

    if a_count > 0 and b_count > 0:
        return {
            "term_a": term_a,
            "term_b": term_b,
            "both_present": True,
            "a_occurrences": a_count,
            "b_occurrences": b_count,
            "are_synonyms": False,
            "explanation": (
                f"The statute uses both '{term_a}' ({a_count}x) and '{term_b}' ({b_count}x). "
                f"Under the whole act rule, Congress does not use surplus language. "
                f"These terms must have different meanings — they are NOT synonymous. "
                f"Treating '{term_a}' as interchangeable with '{term_b}' violates the canon "
                f"against surplusage."
            ),
            "rule": "surplusage_avoided",
        }

    return {
        "term_a": term_a,
        "term_b": term_b,
        "both_present": False,
        "explanation": f"Only one of '{term_a}'/'{term_b}' appears in this text.",
        "rule": "surplusage_avoided",
    }


# ─── Master Analysis Function ───────────────────────────────────────

def apply_construction_rules(text: str, citation: str = "") -> ConstructionAnalysis:
    """
    Apply all statutory construction rules to a provision.

    This is the main entry point. Runs every rule and returns a
    comprehensive analysis with all findings.
    """
    result = ConstructionAnalysis(citation=citation)

    # 1. Classify all definitions (means vs includes)
    definitions = check_means_vs_includes(text)
    result.definitions = definitions
    if definitions:
        for d in definitions:
            result.rules_triggered.extend(d.rules_applied)
            if d.scope == "closed":
                result.construction_notes.append(
                    f"'{d.term}' is a CLOSED definition ('means'). Only what is listed qualifies."
                )
            elif d.scope == "expansive":
                result.construction_notes.append(
                    f"'{d.term}' is an EXPANSIVE definition ('includes'). Listed items are the minimum, not the maximum."
                )

    # 2. Check mutual exclusions between all pairs of defined terms
    if len(definitions) >= 2:
        for i in range(len(definitions)):
            for j in range(i + 1, len(definitions)):
                exclusion = check_exclusion_rule(
                    definitions[i].term,
                    definitions[j].term,
                    text
                )
                if exclusion.mutually_exclusive:
                    result.exclusions.append(exclusion)
                    result.rules_triggered.append("exclusion_rule")

    # 3. Detect enumerations and apply expressio unius / ejusdem generis
    lists = _extract_enumerations(text)
    result.lists = lists
    for lst in lists:
        if lst.general_term:
            result.rules_triggered.append("ejusdem_generis")
        if lst.is_exhaustive:
            result.rules_triggered.append("expressio_unius")

    # 4. Shall vs may classification
    shall_may = classify_shall_may(text)
    result.mandatory_duties = shall_may["mandatory"]
    result.discretionary_powers = shall_may["discretionary"]
    if shall_may["mandatory_count"] > 0:
        result.rules_triggered.append("shall_mandatory")
    if shall_may["discretionary_count"] > 0:
        result.rules_triggered.append("may_discretionary")

    # 5. Title headings note
    if re.search(r'^(?:TITLE|CHAPTER|SUBCHAPTER|PART|SUBTITLE)\s', text, re.MULTILINE):
        result.rules_triggered.append("title_no_control")
        result.construction_notes.append(
            "Title/chapter headings present. Per 1 U.S.C. § 1 note, "
            "headings do NOT control the scope or meaning of the provision."
        )

    # Deduplicate rules
    result.rules_triggered = list(dict.fromkeys(result.rules_triggered))

    return result


# ─── Validation Functions (for other engines to call) ────────────────

def validate_definition_usage(term: str, used_as: str, statute_text: str) -> dict:
    """
    Validate that a term is being used within its statutory definition scope.

    Called by definition_engine to verify terms aren't being applied
    beyond their defined meaning.

    Args:
        term: the defined term
        used_as: how it's being applied (e.g., "all employees" when statute says "officer")
        statute_text: full text of the defining statute

    Returns:
        dict with valid (bool), violation (str), rule (str)
    """
    defs = check_means_vs_includes(statute_text)
    defn = next((d for d in defs if d.term.lower() == term.lower()), None)

    if not defn:
        return {
            "valid": True,
            "term": term,
            "note": "Term not found in statute definitions — may be using common meaning.",
        }

    if defn.scope == "closed" and defn.limited_to:
        used_lower = used_as.lower()
        items_lower = [item.lower() for item in defn.limited_to]
        if not any(item in used_lower for item in items_lower):
            return {
                "valid": False,
                "term": term,
                "violation": (
                    f"'{term}' is defined with 'means' (closed scope) as: "
                    f"{', '.join(defn.limited_to[:5])}. "
                    f"Usage '{used_as}' may exceed this closed definition."
                ),
                "rule": "means_closed",
            }

    if defn.scope == "cross_referenced":
        return {
            "valid": True,
            "term": term,
            "note": f"Definition is cross-referenced to {defn.cross_ref_to}. Check source definition.",
            "rule": "cross_referenced",
        }

    return {"valid": True, "term": term, "scope": defn.scope}


# BEEZLE_FN_UNUSED 2026-06-13: validate_no_cross_statute_import (no static refs in pipeline/)
def validate_no_cross_statute_import(term: str, source_statute: str,
                                      target_statute: str,
                                      target_text: str) -> dict:
    """
    Verify that a definition is NOT being imported from one statute to another
    unless the target statute explicitly cross-references it.

    This is the CRITICAL rule your system enforces:
    Same word ≠ same meaning across statutes.

    Returns:
        dict with valid (bool), violation (str) if invalid
    """
    if source_statute == target_statute:
        return {"valid": True, "term": term, "note": "Same statute — definition applies."}

    # Check if target statute explicitly references the source
    text_lower = target_text.lower()
    source_lower = source_statute.lower()

    cross_ref = bool(re.search(
        rf'(?:as\s+defined\s+in|meaning\s+(?:given|set\s+forth)\s+in|under)\s+.*?'
        + re.escape(source_lower),
        text_lower
    ))

    if cross_ref:
        return {
            "valid": True,
            "term": term,
            "note": f"Target statute cross-references {source_statute}. Import is authorized.",
            "rule": "cross_reference_authorized",
        }

    return {
        "valid": False,
        "term": term,
        "violation": (
            f"'{term}' is defined in {source_statute} but being applied in {target_statute}. "
            f"The target statute does NOT cross-reference {source_statute}. "
            f"Under statutory construction rules, the same word may have different meanings "
            f"in different statutes. This definition import is NOT authorized."
        ),
        "rule": "no_cross_statute_import",
    }


# ─── Helpers ─────────────────────────────────────────────────────────

def _extract_list_items(text: str) -> List[str]:
    """Extract items from a comma-separated list in statutory text."""
    # Remove the definition intro ("X means...")
    cleaned = re.sub(r'^.*?\b(?:means|includes?|defined\s+as)\b\s*', '', text, flags=re.IGNORECASE)
    # Split on commas and "and"/"or"
    parts = re.split(r',\s*(?:and|or)\s+|,\s+|\s+and\s+|\s+or\s+', cleaned)
    items = [p.strip().strip('""\u201c\u201d').strip() for p in parts if p.strip()]
    # Filter out noise
    items = [i for i in items if len(i) > 1 and len(i) < 100]
    return items


def _extract_enumerations(text: str) -> List[ListAnalysis]:
    """Extract enumerated lists from statutory text."""
    results = []

    # Pattern: "(A) item; (B) item; (C) item"
    enum_pattern = re.compile(
        r'\(([A-Z]|[ivx]+|\d+)\)\s+(.+?)(?=\([A-Z]|[ivx]+|\d+\)|$)',
        re.DOTALL
    )
    matches = list(enum_pattern.finditer(text))
    if len(matches) >= 2:
        items = [m.group(2).strip()[:100] for m in matches]
        # Check for general term at the end
        general = ""
        is_exhaustive = True
        last_item = items[-1].lower() if items else ""
        for g in GENERAL_TERMS:
            if g in last_item:
                general = items[-1]
                is_exhaustive = False
                break

        results.append(ListAnalysis(
            items=items,
            general_term=general,
            is_exhaustive=is_exhaustive,
            source_text=text[matches[0].start():matches[-1].end()][:200],
            rules_applied=["ejusdem_generis"] if general else ["expressio_unius"],
        ))

    return results


def _infer_category(items: List[str]) -> str:
    """Infer the shared category of a list of items."""
    if not items:
        return "unknown"

    # Simple keyword-based category inference
    categories = {
        "financial_institution": ["bank", "credit union", "savings", "thrift", "institution"],
        "vehicle": ["car", "truck", "motorcycle", "automobile", "bus"],
        "person": ["individual", "citizen", "resident", "alien", "national"],
        "government_entity": ["agency", "department", "bureau", "commission", "board"],
        "document": ["record", "report", "filing", "form", "statement"],
        "offense": ["felony", "misdemeanor", "crime", "violation", "infraction"],
    }

    items_text = " ".join(items).lower()
    for category, keywords in categories.items():
        if any(kw in items_text for kw in keywords):
            return category

    return "same general kind as: " + ", ".join(items[:3])


# BEEZLE_FN_UNUSED 2026-06-13: format_construction_analysis (no static refs in pipeline/)
def format_construction_analysis(analysis: ConstructionAnalysis) -> str:
    """Format a construction analysis for human reading or filing inclusion."""
    lines = [f"STATUTORY CONSTRUCTION ANALYSIS — {analysis.citation}"]
    lines.append(f"  Rules triggered: {', '.join(analysis.rules_triggered) or 'none'}")

    if analysis.definitions:
        lines.append(f"\n  DEFINITIONS ({len(analysis.definitions)}):")
        for d in analysis.definitions:
            scope_label = {
                "closed": "CLOSED ('means')",
                "expansive": "EXPANSIVE ('includes')",
                "cross_referenced": f"CROSS-REF → {d.cross_ref_to}",
                "undefined": "UNDEFINED",
            }.get(d.scope, d.scope)
            lines.append(f"    '{d.term}' → {scope_label}")
            if d.excludes:
                lines.append(f"      Excludes: {', '.join(d.excludes)}")
            if d.limited_to:
                lines.append(f"      Limited to: {', '.join(d.limited_to[:5])}")

    if analysis.exclusions:
        lines.append(f"\n  MUTUAL EXCLUSIONS ({len(analysis.exclusions)}):")
        for e in analysis.exclusions:
            lines.append(f"    '{e.term_a}' ↔ '{e.term_b}': {e.explanation[:100]}")

    if analysis.mandatory_duties:
        lines.append(f"\n  MANDATORY DUTIES ({len(analysis.mandatory_duties)}):")
        for d in analysis.mandatory_duties[:5]:
            lines.append(f"    SHALL: {d[:100]}")

    if analysis.discretionary_powers:
        lines.append(f"\n  DISCRETIONARY ({len(analysis.discretionary_powers)}):")
        for d in analysis.discretionary_powers[:5]:
            lines.append(f"    MAY: {d[:100]}")

    if analysis.construction_notes:
        lines.append(f"\n  NOTES:")
        for n in analysis.construction_notes:
            lines.append(f"    • {n}")

    return "\n".join(lines)
