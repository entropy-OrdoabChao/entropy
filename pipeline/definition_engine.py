"""
pipeline/definition_engine.py — Definition Engine.

Locks the meaning of legal terms BEFORE the drafter touches them.

This is one of the two modules (along with if_then_engine) that converts
Beezle from "smart AI that improvises" into "deterministic reasoning engine
that the LLM only formats."

PRINCIPLE:
  Definitions come from authority, not from the LLM.
  Once locked, the opposing party either rebuts the definition or concedes it.
  If they don't rebut: their position becomes incoherent, exposing bias or
  incompetence.

NO LLM. Pure rule lookup + extraction.
"""

import re
from dataclasses import dataclass, field
from typing import Optional

from core.logger import get_logger

log = get_logger("DEFINITION")


# ══════════════════════════════════════════════════════════════════════
# CORE LEGAL DEFINITIONS (statute/rule-locked, never invented)
# ══════════════════════════════════════════════════════════════════════
#
# These are pinned definitions tied to specific authorities. The LLM
# never invents these — they are pulled from this table or from the DB.
#
# Each definition has:
#   term       - the word being defined
#   definition - the locked meaning
#   authority  - the rule, statute, or case the definition comes from
#   issue_tags - which conflict issues this applies to
# ══════════════════════════════════════════════════════════════════════

CORE_DEFINITIONS = {
    "verification": {
        "term": "verification",
        "definition": (
            "a signed statement attesting to the truth of the factual averments "
            "in a pleading, made under penalty of perjury or its equivalent"
        ),
        "authority": "Pa.R.C.P. 1024(a)",
        "issue_tags": ["verification"],
    },
    "verified": {
        "term": "verified",
        "definition": (
            "accompanied by a signed verification at the time of filing, "
            "where the verification attests to the truth of the factual averments"
        ),
        "authority": "Pa.R.C.P. 1024",
        "issue_tags": ["verification"],
    },
    "filed": {
        "term": "filed",
        "definition": (
            "submitted to and entered upon the record of the court at "
            "the time of submission, with all required components attached"
        ),
        "authority": "Pa.R.C.P. 205.1; Pa.R.C.P. 440",
        "issue_tags": ["verification", "service", "general"],
    },
    "shall": {
        "term": "shall",
        "definition": (
            "a mandatory directive that imposes a non-discretionary duty; "
            "courts uniformly hold that 'shall' is mandatory rather than permissive"
        ),
        "authority": "1 Pa.C.S. § 1903; Lozada v. Workers' Comp. Appeal Bd., 951 A.2d 1216 (Pa. Commw. 2008)",
        "issue_tags": ["verification", "service", "general", "garnishment", "default"],
    },
    "may": {
        "term": "may",
        "definition": (
            "a discretionary authorization, not a mandatory directive; "
            "discretion is exercised by the court, not by a party"
        ),
        "authority": "1 Pa.C.S. § 1903",
        "issue_tags": ["general"],
    },
    "garnishee": {
        "term": "garnishee",
        "definition": (
            "a third party in possession of property belonging to a judgment debtor "
            "against whom a writ of execution has been served"
        ),
        "authority": "Pa.R.C.P. 3101 et seq.",
        "issue_tags": ["garnishment"],
    },
    "default judgment": {
        "term": "default judgment",
        "definition": (
            "a judgment entered against a party who has failed to plead or "
            "otherwise defend within the time required by the rules"
        ),
        "authority": "Pa.R.C.P. 1037",
        "issue_tags": ["default", "garnishment"],
    },
    "objection": {
        "term": "objection",
        "definition": (
            "a written challenge to a pleading, request, or filing that must "
            "comply with the same procedural requirements as the underlying pleading"
        ),
        "authority": "Pa.R.C.P. 1028; Pa.R.C.P. 3145",
        "issue_tags": ["verification", "garnishment"],
    },
    "praecipe": {
        "term": "praecipe",
        "definition": (
            "a written request to the prothonotary to take a specific ministerial "
            "action, distinct from a pleading and not a substitute for compliance "
            "with the requirements applicable to pleadings"
        ),
        "authority": "Pa.R.C.P. 205.1",
        "issue_tags": ["verification"],
    },
    "service": {
        "term": "service",
        "definition": (
            "delivery of process or pleadings in the manner and within the time "
            "required by the applicable rule of civil procedure"
        ),
        "authority": "Pa.R.C.P. 400-410",
        "issue_tags": ["service"],
    },
    "due process": {
        "term": "due process",
        "definition": (
            "the constitutional guarantee of notice reasonably calculated to apprise "
            "interested parties of the pendency of an action and afford them an "
            "opportunity to present their objections"
        ),
        "authority": "U.S. Const. Amend. XIV; Mullane v. Central Hanover Bank, 339 U.S. 306 (1950)",
        "issue_tags": ["due_process", "service"],
    },
    "investigation": {
        "term": "investigation",
        "definition": (
            "a reasonable inquiry into the accuracy of disputed information, "
            "going beyond a mere review of the furnisher's own records"
        ),
        "authority": "15 U.S.C. § 1681i; Cushman v. Trans Union Corp., 115 F.3d 220 (3d Cir. 1997)",
        "issue_tags": ["fcra_furnisher", "failure_to_investigate"],
    },
    "willful": {
        "term": "willful",
        "definition": (
            "knowingly or in reckless disregard of one's obligations under the statute",
        ),
        "authority": "Safeco Ins. Co. v. Burr, 551 U.S. 47 (2007)",
        "issue_tags": ["fcra_furnisher"],
    },
}


# ══════════════════════════════════════════════════════════════════════
# DATACLASSES
# ══════════════════════════════════════════════════════════════════════

@dataclass
class Definition:
    term: str = ""
    definition: str = ""
    authority: str = ""
    source: str = "core"  # "core" | "db" | "extracted"

    def to_dict(self) -> dict:
        return {
            "term": self.term,
            "definition": self.definition,
            "authority": self.authority,
            "source": self.source,
        }

    def to_prose(self) -> str:
        """Format as a single sentence ready to drop into a brief."""
        return f'"{self.term.capitalize()}" means {self.definition}.'


# ══════════════════════════════════════════════════════════════════════
# DEFINITION LOOKUP
# ══════════════════════════════════════════════════════════════════════

def get_definitions_for_issue(issue: str) -> list[Definition]:
    """Return all core definitions tagged for a given conflict issue."""
    results = []
    for key, entry in CORE_DEFINITIONS.items():
        if issue in entry.get("issue_tags", []):
            results.append(Definition(
                term=entry["term"],
                definition=entry["definition"],
                authority=entry["authority"],
                source="core",
            ))
    return results


def extract_definitions_from_authority(authority_text: str) -> list[Definition]:
    """Extract definitions of the form 'X means Y' or 'X is Y' from DB authority text.

    Patterns matched:
      "X" means Y, ...
      X means Y, ...
      X is defined as Y
      X shall mean Y
    """
    if not authority_text:
        return []

    results = []
    # Pattern 1: "term" means definition
    for m in re.finditer(
        r'"([a-zA-Z][a-zA-Z\s\-]{2,40})"\s+(?:means|is\s+defined\s+as|shall\s+mean)\s+([^.;]+[.;])',
        authority_text,
        re.IGNORECASE,
    ):
        term = m.group(1).strip().lower()
        definition = m.group(2).strip().rstrip(".;")
        if 5 < len(definition) < 400:
            results.append(Definition(
                term=term,
                definition=definition,
                authority="(extracted from authority)",
                source="db",
            ))

    # Pattern 2: term shall mean definition (no quotes)
    for m in re.finditer(
        r'\bthe\s+term\s+([a-zA-Z][a-zA-Z\s\-]{2,40}?)\s+(?:means|shall\s+mean)\s+([^.;]+[.;])',
        authority_text,
        re.IGNORECASE,
    ):
        term = m.group(1).strip().lower()
        definition = m.group(2).strip().rstrip(".;")
        if 5 < len(definition) < 400:
            results.append(Definition(
                term=term,
                definition=definition,
                authority="(extracted from authority)",
                source="db",
            ))

    return results


# BEEZLE_FN_UNUSED 2026-06-13: build_definition_set (no static refs in pipeline/)
def build_definition_set(
    issue: str,
    db_authority: str = "",
    additional_terms: list = None,
) -> list[Definition]:
    """Build the complete definition set for an issue.

    Combines:
      1. Core definitions tagged for this issue
      2. Definitions extracted from DB authority text
      3. Any additional terms requested

    Deduplicated by term. Core definitions take precedence over extracted ones
    when there's a conflict.
    """
    definitions = {}

    # Core definitions first (highest priority)
    for d in get_definitions_for_issue(issue):
        definitions[d.term.lower()] = d

    # Extracted definitions fill gaps
    for d in extract_definitions_from_authority(db_authority):
        if d.term.lower() not in definitions:
            definitions[d.term.lower()] = d

    # Additional explicitly requested terms
    if additional_terms:
        for term in additional_terms:
            term_lower = term.lower()
            if term_lower not in definitions and term_lower in CORE_DEFINITIONS:
                entry = CORE_DEFINITIONS[term_lower]
                definitions[term_lower] = Definition(
                    term=entry["term"],
                    definition=entry["definition"],
                    authority=entry["authority"],
                    source="core",
                )

    result = list(definitions.values())

    # ── Statutory construction validation ──
    # Classify each definition's scope (means=closed, includes=expansive)
    # and check for cross-statute import violations
    try:
        from pipeline.statutory_construction import classify_definition_scope, validate_definition_usage
        for d in result:
            if d.definition:
                sc = classify_definition_scope(d.term, d.definition)
                d.scope = sc.scope  # "closed", "expansive", "cross_referenced", "undefined"
                if sc.rules_applied:
                    d.construction_rules = sc.rules_applied
    except Exception:
        pass  # construction module not available — degrade gracefully

    log.stage("build_definitions",
              f"issue={issue}, locked {len(result)} definitions")
    return result


# BEEZLE_FN_UNUSED 2026-06-13: format_definition_block (no static refs in pipeline/)
def format_definition_block(definitions: list) -> str:
    """Format a list of definitions as a prose block ready for drafting."""
    if not definitions:
        return ""
    lines = []
    for d in definitions:
        prose = d.to_prose() if hasattr(d, "to_prose") else (
            f'"{d.get("term", "").capitalize()}" means {d.get("definition", "")}.'
        )
        lines.append(prose)
    return " ".join(lines)



# ============================================================
# BLOCK_9B_DEFINITION_PRIORITY -- Hermes definition-lookup MERGE
# Append-only. Existing CORE_DEFINITIONS, get_definitions_for_issue,
# extract_definitions_from_authority, format_definition_block remain unchanged.
# ============================================================

DEFINITION_PRIORITY = [
    (1, "statutory",  "Controlling within statute scope"),
    (2, "regulatory", "CFR / state regulatory definitions"),
    (3, "case_law",   "Court interpretation in same jurisdiction"),
    (4, "blacks_law", "Blacks Law Dictionary gap-filler"),
    (5, "bouviers",   "Bouviers Law Dictionary"),
    (6, "amjur_cjs",  "Am. Jur. 2d / C.J.S. secondary treatise"),
]

BANNED_BEHAVIORS = (
    "translate user language into legal terms",
    "define common words as legal terms without statutory hook",
    "use refers-to or is-defined-as without a source citation",
    "invent a definition when no source is found",
    "paraphrase statutory text rather than quote it",
)


def validate_definition(d: dict) -> dict:
    """Validate a definition dict. Returns {valid, violations, required_fields_missing}."""
    missing = [f for f in ("term", "definition", "source", "scope") if not d.get(f)]
    violations = []

    defn = (d.get("definition") or "").lower()
    src = (d.get("source") or "").strip()
    term = (d.get("term") or "").strip()

    if not src or src.lower() in ("", "unknown", "n/a", "tbd"):
        violations.append("NO_SOURCE: definition has no citable source")
    if defn.startswith("refers to ") or "commonly means" in defn or "is defined as " in defn[:40]:
        if not src:
            violations.append("UNSOURCED_PHRASE: uses refers-to/is-defined-as without citation")
    if term and len(term) <= 4 and term.lower() in ("sold", "ghost", "money", "said", "any"):
        if "u.s.c" not in src.lower() and "pa." not in src.lower() and "cfr" not in src.lower():
            violations.append("COMMON_WORD_NO_STATUTE: everyday word without statutory tie")

    return {
        "valid": not (missing or violations),
        "required_fields_missing": missing,
        "violations": violations,
    }


# BEEZLE_FN_UNUSED 2026-06-13: lookup_with_priority (no static refs in pipeline/)
def lookup_with_priority(
    term: str,
    statute_scope: Optional[str] = None,
    candidates: Optional[list[dict]] = None,
) -> Optional[dict]:
    """Lookup term across sources, return highest-priority match."""
    if not candidates:
        core = CORE_DEFINITIONS.get(term.lower())
        if core:
            return {**core, "source_type": "statutory", "_priority_rank": 1}
        log.info(f"lookup_with_priority: no candidates for '{term}'")
        return None

    rank_map = {st: rank for (rank, st, _) in DEFINITION_PRIORITY}
    eligible = []
    for c in candidates:
        if statute_scope and statute_scope.lower() not in (c.get("scope") or "").lower():
            continue
        v = validate_definition(c)
        if not v["valid"]:
            continue
        rank = rank_map.get(c.get("source_type", "").lower(), 99)
        eligible.append((rank, c))

    if not eligible:
        return None
    eligible.sort(key=lambda x: x[0])
    chosen = {**eligible[0][1], "_priority_rank": eligible[0][0]}
    log.info(f"lookup_with_priority: '{term}' -> rank {chosen['_priority_rank']} ({chosen.get('source_type')})")
    return chosen


# BEEZLE_FN_UNUSED 2026-06-13: banned_behaviors_check (no static refs in pipeline/)
def banned_behaviors_check(text: str) -> list[str]:
    """Heuristic scan of generated text for banned definition behaviors."""
    flags = []
    tl = text.lower()
    if "refers to" in tl and "see " not in tl and "u.s.c" not in tl:
        flags.append("USES_REFERS_TO_WITHOUT_CITATION")
    if "commonly means" in tl:
        flags.append("COMMON_LANGUAGE_DEFINITION")
    if "is defined as" in tl and not any(s in tl for s in ("u.s.c", "cfr", "pa.r", "pa. r", "restat")):
        flags.append("IS_DEFINED_AS_NO_AUTHORITY")
    return flags
# ============================================================
# /BLOCK_9B_DEFINITION_PRIORITY
# ============================================================


# ══════════════════════════════════════════════════════════════════════
# CORE LEGAL MAXIMS (authority-locked, never invented)  [MAXIMS_EQUITY_DB_V1]
# ══════════════════════════════════════════════════════════════════════
# Maxims are interpretive canons / rules of decision. Each entry:
#   latin       - the maxim in Latin (canonical form)
#   translation - plain-English statement of the rule
#   authority   - the source that states it (treatise, dictionary, or case)
#   issue_tags  - which issue types invoke it
CORE_MAXIMS = {
    "presumption_of_regularity": {
        "latin": "omnia praesumuntur rite esse acta",
        "translation": (
            "all official acts are presumed to have been performed regularly and "
            "in due form; applied to the mails, a letter properly addressed, "
            "stamped, and posted is presumed to have reached its destination and "
            "been received"
        ),
        "authority": "Rosenthal v. Walker, 111 U.S. 185, 193 (1884); accord Rios v. Nicholson, 490 F.3d 928, 930-31 (Fed. Cir. 2007)",
        "issue_tags": ["service", "notice", "regularity"],
    },
    "equity_aids_the_vigilant": {
        "latin": "vigilantibus non dormientibus aequitas subvenit",
        "translation": (
            "equity aids the vigilant, not those who slumber on their rights; a "
            "party who fails to raise an objection timely while the cause proceeds "
            "may not later resurrect it to defeat the merits"
        ),
        "authority": "Pomeroy, A Treatise on Equity Jurisprudence Sec 418 (5th ed.)",
        "issue_tags": ["service", "laches", "waiver", "general"],
    },
    "void_is_a_nullity": {
        "latin": "quod ab initio non valet in tractu temporis non convalescit",
        "translation": (
            "that which is void from the beginning does not become valid by the "
            "passage of time; a void judgment is a nullity that may be challenged "
            "at any time and binds no one"
        ),
        "authority": "Pomeroy, A Treatise on Equity Jurisprudence (5th ed.); see Fed. R. Civ. P. 60(b)(4)",
        "issue_tags": ["void_judgment", "jurisdiction", "due_process"],
    },
}

# ══════════════════════════════════════════════════════════════════════
# EQUITY PRINCIPLES (authority-locked, never invented)  [MAXIMS_EQUITY_DB_V1]
# ══════════════════════════════════════════════════════════════════════
# Equity principles go to WHY a court of equity should grant or withhold
# relief in fairness (distinct from a maxim's interpretive function).
EQUITY_PRINCIPLES = {
    "no_profit_from_own_wrong": {
        "principle": (
            "no one may take advantage of, or profit from, a wrong or defect that "
            "the party itself created or invited"
        ),
        "authority": "Pomeroy, A Treatise on Equity Jurisprudence Sec 397-399 (5th ed.)",
        "issue_tags": ["service", "fraud", "general"],
    },
    "removal_service_burden_1448": {
        "principle": (
            "a party who invokes federal jurisdiction by removal bears the burden "
            "to complete any service left imperfect at removal, and its own (or its "
            "counsel's) failure to do so cannot be charged against the opposing party"
        ),
        "authority": "28 U.S.C. Sec 1448",
        "issue_tags": ["service", "removal"],
    },
    "clean_hands": {
        "principle": (
            "a party seeking equitable relief must come into court with clean hands; "
            "one who has engaged in inequitable conduct as to the matter in "
            "controversy will be denied relief"
        ),
        "authority": "Pomeroy, A Treatise on Equity Jurisprudence Sec 397 (5th ed.)",
        "issue_tags": ["fraud", "general"],
    },
    "no_wrong_without_remedy": {
        "principle": (
            "equity will not suffer a wrong to be without a remedy; where a legal "
            "right is established and the law affords no adequate remedy, equity "
            "will supply one"
        ),
        "authority": "Pomeroy, A Treatise on Equity Jurisprudence Sec 423 (5th ed.)",
        "issue_tags": ["due_process", "notice", "general"],
    },
}

# BEEZLE_FN_UNUSED 2026-06-13: get_maxim (no static refs in pipeline/)
def get_maxim(key: str) -> dict | None:
    """Return a CORE_MAXIMS entry by key, or None. [MAXIMS_EQUITY_DB_V1]"""
    return CORE_MAXIMS.get(key)

# BEEZLE_FN_UNUSED 2026-06-13: get_equity (no static refs in pipeline/)
def get_equity(key: str) -> dict | None:
    """Return an EQUITY_PRINCIPLES entry by key, or None. [MAXIMS_EQUITY_DB_V1]"""
    return EQUITY_PRINCIPLES.get(key)

def render_maxim(key: str) -> str:
    """Render a maxim as prose for a brief, or '' if unknown."""
    m = CORE_MAXIMS.get(key)
    if not m:
        return ""
    return f"{m['latin']} -- {m['translation']} ({m['authority']})"

def render_equity(key: str) -> str:
    """Render an equity principle as prose for a brief, or '' if unknown."""
    e = EQUITY_PRINCIPLES.get(key)
    if not e:
        return ""
    return f"{e['principle']} ({e['authority']})"

# BEEZLE_FN_UNUSED 2026-06-13: get_maxims_for_issue (no static refs in pipeline/)
def get_maxims_for_issue(issue: str) -> list[dict]:
    """All maxims + equity principles tagged for an issue. [MAXIMS_EQUITY_DB_V1]"""
    out = []
    for k, m in CORE_MAXIMS.items():
        if issue in m.get("issue_tags", []):
            out.append({"key": k, "kind": "maxim", **m})
    for k, e in EQUITY_PRINCIPLES.items():
        if issue in e.get("issue_tags", []):
            out.append({"key": k, "kind": "equity", **e})
    return out
# === END MAXIMS_EQUITY_DB_V1 ===
