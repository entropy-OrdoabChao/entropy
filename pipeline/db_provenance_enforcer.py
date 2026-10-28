"""
db_provenance_enforcer.py — Block 16: DB-First Provenance Enforcement
======================================================================

PURPOSE:
  The entire point of building ChromaDB ingestion, statutory_construction,
  conformity_checker, and definition_engine was to ensure the system NEVER
  relies on LLM training data for legal definitions and statutory text.

  This module enforces that contract:
    1. ALL statutory definition queries go to DB FIRST
    2. Every response is tagged with provenance: "db" | "llm_fallback" | "mixed"
    3. If DB has no result, the system WARNS — it does NOT silently fall back to LLM
    4. Statutory construction canons (exclusio unius, expressio unius) are applied
       to the ACTUAL statutory text, not the regulatory gloss
    5. CFR vs USC divergence is flagged as a conformity violation

PROBLEM THIS SOLVES:
  Hermes was asked "what is the definition of employee under Title 26?"
  and returned a common-law control test (CFR/regulatory gloss) instead of
  the actual statutory definition at 26 U.S.C. § 3401(c), which is a
  RESTRICTIVE list: government officers, elected officials, corporate officers.

  Under exclusio unius, that list is definitional, not illustrative.
  The system should have:
    1. Pulled § 3401(c) from ChromaDB
    2. Applied statutory construction (exclusio unius on "includes [list]")
    3. Flagged the CFR control test as a regulatory EXPANSION of the statute
    4. Told the user the answer came from DB, not LLM training

Logs to: <home>\\logs\\engines\\db_provenance.log
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import json
import logging
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

# === BEEZLE_ROOT_SHIM (WSL-safe; auto-injected) ===
import os as _os_broot
from pathlib import Path as _Path_broot
def _resolve_beezle_root():
    _e = _os_broot.environ.get("BEEZLE_ROOT")
    if _e:
        return _Path_broot(_e)
    _d = _Path_broot(__file__).resolve().parent
    for _ in range(5):
        if (_d / "pipeline").exists() or (_d / "obsidian_sync").exists():
            return _d
        _d = _d.parent
    return _Path_broot(str(_eroot()))
_BEEZLE_ROOT = _resolve_beezle_root()
# === END BEEZLE_ROOT_SHIM ===

# ─── Paths ───
BASE_PATH = _BEEZLE_ROOT
PIPELINE_DIR = BASE_PATH / "pipeline"
LOGS_DIR = BASE_PATH / "logs" / "engines"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

log = logging.getLogger("db_provenance")
if not log.handlers:
    log.setLevel(logging.INFO)
    fh = logging.FileHandler(LOGS_DIR / "db_provenance.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"))
    log.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("[%(name)s] %(message)s"))
    log.addHandler(sh)


# ════════════════════════════════════════════════════════════════════
# PROVENANCE TAGS
# ════════════════════════════════════════════════════════════════════

class Provenance:
    DB = "db"                    # Answer came from ChromaDB ingested statute/regulation
    LLM_FALLBACK = "llm_fallback"  # DB had nothing; LLM filled in (MUST WARN USER)
    MIXED = "mixed"              # Some from DB, some from LLM
    DB_CFR = "db_cfr"            # Answer from DB but it's regulatory (CFR), not statutory (USC)
    DB_USC = "db_usc"            # Answer from DB and it's actual statutory text (USC)
    UNKNOWN = "unknown"          # Source not determined


@dataclass
class ProvenanceResult:
    """Wraps any engine output with provenance metadata."""
    query: str
    provenance: str              # Provenance tag
    source_collection: str       # ChromaDB collection name or "llm_training"
    source_citations: list[str] = field(default_factory=list)  # actual citations found
    db_chunks_found: int = 0
    db_text: str = ""            # actual text from DB
    llm_text: str = ""           # text from LLM (if fallback)
    warnings: list[str] = field(default_factory=list)
    conformity_flags: list[dict] = field(default_factory=list)  # CFR-vs-USC divergence
    construction_flags: list[dict] = field(default_factory=list)  # statutory construction issues
    answer: str = ""             # final answer to use

    def to_dict(self) -> dict:
        return asdict(self)


# ════════════════════════════════════════════════════════════════════
# DEFINITION QUERY PATTERNS — known statutory definition sections
# ════════════════════════════════════════════════════════════════════

# Maps common definition queries to their ACTUAL statutory location
# so we query DB with the right section, not a vague topic search
STATUTORY_DEFINITION_MAP = {
    # Title 26 — Internal Revenue Code
    "employee title 26": {
        "usc_section": "26 U.S.C. § 3401(c)",
        "db_queries": [
            "26 USC 3401 employee definition",
            "3401(c) employee includes officer",
            "employee definition Internal Revenue Code withholding",
        ],
        "cfr_counterpart": "26 CFR § 31.3401(c)-1",
        "construction_notes": "Uses 'includes' with a specific list — apply exclusio unius",
    },
    "wages title 26": {
        "usc_section": "26 U.S.C. § 3401(a)",
        "db_queries": [
            "26 USC 3401 wages definition remuneration",
            "3401(a) wages means remuneration",
        ],
        "cfr_counterpart": "26 CFR § 31.3401(a)-1",
        "construction_notes": "Definition of 'wages' — check exceptions list",
    },
    "employer title 26": {
        "usc_section": "26 U.S.C. § 3401(d)",
        "db_queries": [
            "26 USC 3401 employer definition",
            "3401(d) employer person control",
        ],
        "cfr_counterpart": "26 CFR § 31.3401(d)-1",
    },
    "person title 26": {
        "usc_section": "26 U.S.C. § 7701(a)(1)",
        "db_queries": [
            "26 USC 7701 person definition includes",
            "7701(a)(1) person individual trust estate",
        ],
        "construction_notes": "Broad definition — 'includes' here IS illustrative per legislative history",
    },
    "trade or business title 26": {
        "usc_section": "26 U.S.C. § 7701(a)(26)",
        "db_queries": [
            "26 USC 7701 trade or business definition",
            "7701(a)(26) trade business includes performance",
        ],
        "construction_notes": "'includes the performance of the functions of a public office' — very specific inclusion",
    },

    # Title 15 — FCRA/FDCPA
    "consumer title 15 fcra": {
        "usc_section": "15 U.S.C. § 1681a(c)",
        "db_queries": [
            "15 USC 1681a consumer definition individual",
        ],
    },
    "furnisher title 15 fcra": {
        "usc_section": "15 U.S.C. § 1681s-2",
        "db_queries": [
            "15 USC 1681s-2 furnisher duties accuracy",
        ],
    },
    "debt collector title 15 fdcpa": {
        "usc_section": "15 U.S.C. § 1692a(6)",
        "db_queries": [
            "15 USC 1692a debt collector definition",
            "1692a(6) debt collector principal purpose",
        ],
        "construction_notes": "Exclusions in (6)(A)-(F) — apply exclusio unius",
    },

    # Title 12 — RESPA
    "servicer title 12 respa": {
        "usc_section": "12 U.S.C. § 2605(i)(2)",
        "db_queries": [
            "12 USC 2605 servicer definition",
        ],
    },
    "qualified written request": {
        "usc_section": "12 U.S.C. § 2605(e)(1)(B)",
        "db_queries": [
            "12 USC 2605 qualified written request",
            "2605(e)(1)(B) qualified written request definition",
        ],
    },

    # Title 42 — Civil Rights
    "person section 1983": {
        "usc_section": "42 U.S.C. § 1983",
        "db_queries": [
            "42 USC 1983 person acting under color",
            "1983 every person subjects causes",
        ],
        "construction_notes": "'Every person' — check Monell for municipal liability limitation",
    },

    # Title 5 — Government Organization
    "employee title 5": {
        "usc_section": "5 U.S.C. § 2105",
        "db_queries": [
            "5 USC 2105 employee definition",
            "2105 employee civil service appointed",
        ],
    },
    "agency title 5": {
        "usc_section": "5 U.S.C. § 551(1)",
        "db_queries": [
            "5 USC 551 agency definition",
            "551(1) agency authority government",
        ],
    },
}


# ════════════════════════════════════════════════════════════════════
# STATUTORY CONSTRUCTION — "includes" analysis
# ════════════════════════════════════════════════════════════════════

def analyze_includes_clause(text: str, section: str) -> list[dict]:
    """Analyze whether 'includes' in a statutory definition is
    restrictive (exclusio unius) or illustrative.

    Under statutory construction:
      - "includes" followed by a SPECIFIC, CLOSED list = restrictive
        (exclusio unius est exclusio alterius)
      - "includes but is not limited to" = explicitly illustrative
      - "includes" with open-ended language = likely illustrative
      - "means" = always definitional/restrictive
    """
    flags = []
    text_lower = text.lower()

    # Check for "means" — always definitional
    means_match = re.search(r'(?:the\s+term\s+)?["\']?\w+["\']?\s+means\s+', text_lower)
    if means_match:
        flags.append({
            "canon": "definitional_means",
            "rule": "'Means' creates a closed, exhaustive definition",
            "authority": "Burgess v. United States, 553 U.S. 124, 130 (2008)",
            "finding": f"Section {section} uses 'means' — definition is EXHAUSTIVE, not illustrative",
            "implication": "Items not listed are EXCLUDED by operation of the definition",
        })

    # Check for "includes" patterns
    includes_match = re.search(
        r'(?:the\s+term\s+)?["\']?\w+["\']?\s+includes\s+(.{20,300})',
        text_lower
    )
    if includes_match:
        after_includes = includes_match.group(1)

        # "includes but is not limited to" = illustrative
        if "not limited to" in after_includes or "without limitation" in after_includes:
            flags.append({
                "canon": "illustrative_includes",
                "rule": "'Includes but is not limited to' is explicitly illustrative/non-exhaustive",
                "authority": "Federal Land Bank of St. Paul v. Bismarck Lumber Co., 314 U.S. 95 (1941)",
                "finding": f"Section {section} uses 'includes but is not limited to' — list is OPEN",
                "implication": "Other items beyond those listed may also qualify",
            })
        else:
            # "includes [specific list]" without "not limited to" — analyze further
            # Count enumerated items (look for commas, "and", "or")
            list_items = re.split(r',\s*(?:and|or)?\s*|(?:;\s*)', after_includes)
            list_items = [i.strip() for i in list_items if i.strip() and len(i.strip()) > 3]

            if len(list_items) >= 2:
                # Specific list found — strong case for exclusio unius
                flags.append({
                    "canon": "exclusio_unius",
                    "rule": "Expressio unius est exclusio alterius — expression of one is exclusion of others",
                    "authority": "Leatherman v. Tarrant County, 507 U.S. 163 (1993); Russello v. United States, 464 U.S. 16, 23 (1983)",
                    "finding": (f"Section {section} uses 'includes' followed by a specific enumerated list "
                                f"({len(list_items)} items) WITHOUT 'but is not limited to'. "
                                f"Under exclusio unius, this list is RESTRICTIVE."),
                    "implication": "Persons/things not in the list are EXCLUDED from the definition",
                    "items_listed": list_items[:10],
                })

            # Check if the list references specific government entities
            govt_terms = ["united states", "state", "political subdivision",
                          "district of columbia", "agency", "instrumentality",
                          "officer", "elected official"]
            govt_hits = [t for t in govt_terms if t in after_includes]
            if len(govt_hits) >= 2:
                flags.append({
                    "canon": "government_entity_restriction",
                    "rule": "Definition limited to government-related entities/persons",
                    "finding": (f"Section {section} defines term by reference to government entities: "
                                f"{', '.join(govt_hits)}. Private-sector workers not performing "
                                f"government functions may not fall within this definition."),
                    "implication": "Apply exclusio unius — private employees not listed are not 'employees' under this section",
                })

    return flags


# ════════════════════════════════════════════════════════════════════
# CFR vs USC DIVERGENCE CHECK
# ════════════════════════════════════════════════════════════════════

def check_cfr_usc_divergence(
    usc_text: str,
    cfr_text: str,
    usc_section: str,
    cfr_section: str,
) -> list[dict]:
    """Check if a CFR regulation EXPANDS or CONTRADICTS the USC statute.

    The hierarchy is: Constitution > Statute (USC) > Regulation (CFR)
    A regulation cannot expand a statutory definition beyond what
    Congress enacted. If it does, the regulation is ultra vires.
    """
    flags = []
    usc_lower = usc_text.lower()
    cfr_lower = cfr_text.lower()

    # Check if CFR adds categories not in USC
    # Look for common-law test language in CFR that isn't in USC
    expansion_markers = [
        (r"common[- ]law\s+(?:test|rule|standard|employee)", "common-law test"),
        (r"(?:behavioral|financial)\s+control", "control-based test"),
        (r"right\s+to\s+control\s+(?:how|when|where|what)", "right-to-control test"),
        (r"(?:full[- ]time|part[- ]time)\s+(?:worker|employee)", "full-time/part-time classification"),
        (r"(?:temporary|seasonal|casual)\s+(?:worker|employee)", "temporary worker expansion"),
        (r"(?:domestic\s+servant|household\s+employ)", "domestic worker expansion"),
    ]

    for pattern, label in expansion_markers:
        cfr_has = bool(re.search(pattern, cfr_lower))
        usc_has = bool(re.search(pattern, usc_lower))
        if cfr_has and not usc_has:
            flags.append({
                "type": "cfr_expansion",
                "severity": "HIGH",
                "label": label,
                "cfr_section": cfr_section,
                "usc_section": usc_section,
                "finding": (f"CFR {cfr_section} introduces '{label}' which does NOT appear "
                            f"in the statutory text of {usc_section}. This may be an ultra vires "
                            f"regulatory expansion of the statutory definition."),
                "authority": "Chevron U.S.A. v. NRDC, 467 U.S. 837 (1984); but see Loper Bright Enterprises v. Raimondo, 144 S. Ct. 2244 (2024) — Chevron overruled",
                "implication": (f"Post-Loper Bright, courts no longer defer to agency interpretation "
                                f"when the statute is clear. If {usc_section} has a clear, specific "
                                f"definition, the CFR cannot expand it."),
            })

    # Check if USC uses "means" (closed) but CFR uses "includes" (open)
    usc_uses_means = bool(re.search(r'\bmeans\b', usc_lower))
    cfr_uses_includes = bool(re.search(r'\bincludes\b', cfr_lower))
    if usc_uses_means and cfr_uses_includes:
        flags.append({
            "type": "definition_scope_conflict",
            "severity": "CRITICAL",
            "finding": (f"USC {usc_section} uses 'MEANS' (closed/exhaustive definition) but "
                        f"CFR {cfr_section} uses 'INCLUDES' (potentially open/illustrative). "
                        f"The regulation may be impermissibly broadening a closed statutory definition."),
            "authority": "Burgess v. United States, 553 U.S. 124 (2008) — 'means' is exhaustive",
        })

    return flags


# ════════════════════════════════════════════════════════════════════
# MAIN: DB-FIRST DEFINITION LOOKUP
# ════════════════════════════════════════════════════════════════════

def lookup_definition(
    term: str,
    title: str = "",
    context: str = "",
    force_db: bool = True,
) -> ProvenanceResult:
    """Look up a statutory definition with DB-first enforcement.

    Args:
        term: the term to define (e.g., "employee")
        title: title number or name (e.g., "26", "title 26", "IRC")
        context: additional context (e.g., "withholding", "FICA")
        force_db: if True (default), NEVER fall back to LLM silently

    Returns:
        ProvenanceResult with tagged provenance and any construction flags
    """
    t0 = time.perf_counter()

    # Normalize the lookup key
    lookup_key = f"{term.lower()} title {title}".strip() if title else term.lower()
    # Try exact match first, then fuzzy
    def_info = STATUTORY_DEFINITION_MAP.get(lookup_key)
    if not def_info:
        # Try partial match
        for key, info in STATUTORY_DEFINITION_MAP.items():
            if term.lower() in key and (not title or title.lower() in key):
                def_info = info
                break

    result = ProvenanceResult(
        query=f"Definition of '{term}'" + (f" under Title {title}" if title else ""),
        provenance=Provenance.UNKNOWN,
        source_collection="",
    )

    # ─── Step 1: Query ChromaDB ───
    db_text = ""
    db_hits = []
    try:
        from pipeline.db_connector import LegalDB
        db = LegalDB()

        queries = []
        if def_info:
            queries = def_info["db_queries"]
            result.source_citations.append(def_info["usc_section"])
        else:
            # Generic query construction
            queries = [
                f"{term} definition {title}",
                f"{term} means includes {title}",
            ]

        for q in queries[:4]:
            try:
                hits = db.query_text(q, n_results=5)
                for h in hits:
                    txt = h.get("text", h.get("document", ""))
                    src = h.get("metadata", {}).get("source", "")
                    collection = h.get("metadata", {}).get("collection", "")
                    if txt and len(txt) > 30:
                        db_hits.append({
                            "text": txt[:1000],
                            "source": src,
                            "collection": collection,
                        })
            except Exception as e:
                log.error(f"DB query failed for '{q}': {e}")

    except ImportError:
        result.warnings.append("ChromaDB (db_connector) not available — cannot enforce DB-first")
    except Exception as e:
        result.warnings.append(f"DB connection failed: {e}")

    # ─── Step 2: Evaluate DB results ───
    # SECTION-AWARE FILTER: When we know the section, require chunks to mention it.
    # Prevents §3401(c) 'employee' queries from matching §7521-§7523 noise.
    if db_hits and def_info and def_info.get('usc_section'):
        _sec_str = def_info['usc_section']
        # Extract section numbers (e.g. '26 U.S.C. § 3401(c)' → ['3401'])
        _section_nums = re.findall(r'§\s*(\d{2,5})', _sec_str)
        if not _section_nums:
            _section_nums = re.findall(r'\b(\d{3,5})(?=\(|\b)', _sec_str)
        if _section_nums:
            _filtered = []
            for _h in db_hits:
                _t = (_h.get('text') or '').lower()
                for _n in _section_nums:
                    if (f'§ {_n}' in _t or f'§{_n}' in _t or
                        f'section {_n}' in _t or f'sec. {_n}' in _t or
                        f'sec.{_n}' in _t or f'{_n}(' in _t):
                        _filtered.append(_h)
                        break
            if _filtered:
                log.info(f'  Section filter: kept {len(_filtered)}/{len(db_hits)} chunks mentioning §' + '/'.join(_section_nums))
                db_hits = _filtered
            else:
                log.warning(f'  Section filter: NO chunks mention §' + '/'.join(_section_nums) + f'; using all {len(db_hits)} hits')
                result.warnings.append(f'Section filter found no chunks for {_sec_str}. Results may be semantic-only.')

    if db_hits:
        result.db_chunks_found = len(db_hits)

        # Determine if we got USC (statute) or CFR (regulation)
        usc_chunks = []
        cfr_chunks = []
        other_chunks = []

        for hit in db_hits:
            src = (hit.get("source", "") + " " + hit.get("collection", "")).lower()
            text = hit["text"]
            if any(k in src for k in ["usc", "u.s.c", "statute", "public law", "title_"]):
                usc_chunks.append(text)
            elif any(k in src for k in ["cfr", "regulation", "reg_"]):
                cfr_chunks.append(text)
            else:
                other_chunks.append(text)

        if usc_chunks:
            result.provenance = Provenance.DB_USC
            result.source_collection = "usc_statutes"
            result.db_text = "\n\n".join(usc_chunks[:3])
            result.answer = result.db_text
            log.info(f"  DB-USC: found {len(usc_chunks)} statutory chunks for '{term}'")

            # Apply statutory construction to the ACTUAL text
            construction = analyze_includes_clause(result.db_text,
                                                     def_info["usc_section"] if def_info else f"Title {title}")
            result.construction_flags = construction

        elif cfr_chunks:
            result.provenance = Provenance.DB_CFR
            result.source_collection = "cfr_regulations"
            result.db_text = "\n\n".join(cfr_chunks[:3])
            result.answer = result.db_text
            result.warnings.append(
                f"⚠️ REGULATORY TEXT ONLY: DB returned CFR (regulation) text, NOT USC (statute). "
                f"The regulatory definition may EXPAND the statutory definition. "
                f"Check {def_info['usc_section'] if def_info else 'the actual statute'} for the authoritative definition."
            )
            if def_info and def_info.get("cfr_counterpart"):
                result.warnings.append(
                    f"CFR source appears to be {def_info['cfr_counterpart']}. "
                    f"The statutory authority is {def_info['usc_section']}. "
                    f"Under Loper Bright (2024), courts no longer defer to agency interpretation "
                    f"when the statute is clear."
                )
            log.info(f"  DB-CFR: found {len(cfr_chunks)} regulatory chunks for '{term}' — flagged as CFR-only")

        elif other_chunks:
            result.provenance = Provenance.DB
            result.source_collection = "other"
            result.db_text = "\n\n".join(other_chunks[:3])
            result.answer = result.db_text

        # CFR-vs-USC divergence check if we have both
        if usc_chunks and cfr_chunks:
            divergence = check_cfr_usc_divergence(
                usc_text="\n".join(usc_chunks),
                cfr_text="\n".join(cfr_chunks),
                usc_section=def_info["usc_section"] if def_info else f"Title {title}",
                cfr_section=def_info.get("cfr_counterpart", "CFR") if def_info else "CFR",
            )
            result.conformity_flags = divergence
            if divergence:
                result.provenance = Provenance.MIXED
                result.warnings.append(
                    f"⚠️ CFR-USC DIVERGENCE DETECTED: {len(divergence)} conflict(s) found. "
                    f"The regulation may expand the statutory definition beyond what Congress enacted."
                )

    else:
        # ─── No DB results ───
        if force_db:
            result.provenance = Provenance.LLM_FALLBACK
            result.warnings.append(
                f"🔴 DB MISS: ChromaDB has NO text for '{term}' definition "
                f"(Title {title}). The system is falling back to LLM training data. "
                f"THIS ANSWER IS NOT VERIFIED AGAINST ACTUAL STATUTORY TEXT. "
                f"Ingest the relevant statute section before relying on this definition."
            )
            if def_info:
                result.warnings.append(
                    f"The authoritative source should be {def_info['usc_section']}. "
                    f"Run: python bulk_ingest_to_chroma.py --section \"{def_info['usc_section']}\""
                )
            log.warning(f"  DB MISS for '{term}' Title {title} — LLM fallback triggered")
        else:
            result.provenance = Provenance.LLM_FALLBACK

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    log.info(f"  Definition lookup '{term}' Title {title}: provenance={result.provenance}, "
             f"{result.db_chunks_found} DB chunks, {len(result.warnings)} warnings, {elapsed_ms}ms")

    return result


# ════════════════════════════════════════════════════════════════════
# HERMES INTEGRATION — wrap any Hermes response with provenance check
# ════════════════════════════════════════════════════════════════════

# BEEZLE_FN_UNUSED 2026-06-13: enforce_provenance_on_response (no static refs in pipeline/)
def enforce_provenance_on_response(
    query: str,
    hermes_response: str,
    db_was_queried: bool = False,
    db_results: list = None,
) -> dict:
    """Post-process a Hermes response to check if it used DB or LLM.

    Call this AFTER Hermes generates a response to verify provenance.
    If the response contains legal definitions not backed by DB, flag it.

    Args:
        query: the user's original query
        hermes_response: what Hermes said
        db_was_queried: whether Hermes actually hit ChromaDB
        db_results: what ChromaDB returned (if anything)

    Returns:
        dict with provenance tags and warnings
    """
    result = {
        "query": query,
        "db_was_queried": db_was_queried,
        "db_results_count": len(db_results) if db_results else 0,
        "provenance": Provenance.UNKNOWN,
        "warnings": [],
        "needs_db_verification": False,
    }

    response_lower = hermes_response.lower()

    # Detect if the response contains definition-like content
    definition_patterns = [
        r"(?:the\s+term\s+['\"]?\w+['\"]?\s+(?:means|includes|is\s+defined\s+as))",
        r"(?:(?:an?\s+)?employee\s+is\s+(?:defined|considered|classified)\s+as)",
        r"(?:under\s+(?:title|section|§)\s+\d+.*(?:means|includes|defined))",
        r"(?:(?:the\s+)?definition\s+(?:of|under)\s+)",
        r"(?:common[- ]law\s+(?:test|standard|rule))",
    ]

    has_definition_content = any(
        re.search(p, response_lower) for p in definition_patterns
    )

    if has_definition_content:
        if not db_was_queried:
            result["provenance"] = Provenance.LLM_FALLBACK
            result["needs_db_verification"] = True
            result["warnings"].append(
                "🔴 PROVENANCE VIOLATION: Response contains statutory definitions "
                "but ChromaDB was NOT queried. This answer may be based on LLM "
                "training data, not actual statutory text. The definition may be "
                "wrong, outdated, or based on regulatory gloss instead of the statute."
            )
        elif db_results and len(db_results) > 0:
            result["provenance"] = Provenance.DB
        else:
            result["provenance"] = Provenance.LLM_FALLBACK
            result["needs_db_verification"] = True
            result["warnings"].append(
                "⚠️ DB was queried but returned no results. Response likely uses "
                "LLM training data. Ingest the relevant statutory section."
            )

        # Check for common-law test (regulatory gloss) vs statutory definition
        if re.search(r"common[- ]law\s+(?:test|rule|standard)", response_lower):
            result["warnings"].append(
                "⚠️ REGULATORY GLOSS DETECTED: Response references 'common-law test' "
                "which is a CFR/regulatory construction, not the statutory text. "
                "Check the actual USC section for the statutory definition — it may "
                "be more restrictive than the regulatory interpretation."
            )

        # Check for control-test language
        if re.search(r"(?:behavioral|financial)\s+control|right\s+to\s+control", response_lower):
            result["warnings"].append(
                "⚠️ CONTROL TEST LANGUAGE: Response uses IRS control-test factors "
                "(behavioral control, financial control) which come from IRS Revenue "
                "Ruling 87-41 and CFR regulations, NOT from the statutory definition. "
                "The statute at 26 U.S.C. § 3401(c) has a specific, restrictive definition."
            )

    return result


# ════════════════════════════════════════════════════════════════════
# HERMES BRIDGE HOOK — call this before returning any definition
# ════════════════════════════════════════════════════════════════════

# BEEZLE_FN_UNUSED 2026-06-13: get_definition_for_hermes (no static refs in pipeline/)
def get_definition_for_hermes(
    term: str,
    title: str = "",
    context: str = "",
) -> dict:
    """Entry point for hermes_beezle_bridge.py to get a DB-verified definition.

    Returns a dict that Hermes should use AS ITS ANSWER, not as supplemental info.
    If the DB has the definition, Hermes MUST use it.
    If the DB does NOT have it, Hermes MUST warn the user.

    Returns:
        {
            "definition": str,           # the actual statutory text
            "provenance": str,            # "db_usc" | "db_cfr" | "llm_fallback"
            "section": str,               # e.g. "26 U.S.C. § 3401(c)"
            "construction_flags": [...],  # exclusio unius etc.
            "conformity_flags": [...],    # CFR-vs-USC divergence
            "warnings": [...],            # user-facing warnings
            "instructions_for_hermes": str, # what Hermes should do
        }
    """
    result = lookup_definition(term, title, context)

    instructions = ""
    if result.provenance == Provenance.DB_USC:
        instructions = (
            f"USE THIS STATUTORY TEXT AS YOUR ANSWER. Do not supplement with "
            f"training data. The definition of '{term}' under {result.source_citations[0] if result.source_citations else f'Title {title}'} "
            f"is from the actual enacted statute in ChromaDB. "
            f"Apply any statutory construction flags noted below."
        )
    elif result.provenance == Provenance.DB_CFR:
        instructions = (
            f"WARN THE USER: This definition comes from CFR regulations, not the actual statute. "
            f"The regulatory definition may be broader than what Congress enacted. "
            f"Tell the user to check {result.source_citations[0] if result.source_citations else 'the USC section'} "
            f"for the statutory definition."
        )
    elif result.provenance == Provenance.LLM_FALLBACK:
        instructions = (
            f"CRITICAL: DB has no text for this definition. You MUST tell the user: "
            f"'This answer is based on my training data, not verified statutory text. "
            f"The authoritative source is {result.source_citations[0] if result.source_citations else 'the relevant USC section'}. "
            f"I recommend ingesting the statute into the database for a verified answer.' "
            f"Do NOT present training-data definitions as if they are from the statute."
        )

    return {
        "definition": result.answer,
        "provenance": result.provenance,
        "section": result.source_citations[0] if result.source_citations else "",
        "construction_flags": result.construction_flags,
        "conformity_flags": result.conformity_flags,
        "warnings": result.warnings,
        "instructions_for_hermes": instructions,
        "db_chunks_found": result.db_chunks_found,
    }


# ════════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Block 16 — DB Provenance Enforcer")
    parser.add_argument("term", nargs="?", default="employee",
                        help="Term to look up (e.g., 'employee')")
    parser.add_argument("--title", default="26",
                        help="USC title number (e.g., '26')")
    parser.add_argument("--context", default="",
                        help="Additional context")
    parser.add_argument("--test-construction", action="store_true",
                        help="Test statutory construction on sample text")
    args = parser.parse_args()

    if args.test_construction:
        # Test with the actual § 3401(c) text
        sample_3401c = (
            '(c) Employee. For purposes of this chapter, the term "employee" includes '
            'an officer, employee, or elected official of the United States, a State, '
            'or any political subdivision thereof, or the District of Columbia, or any '
            'agency or instrumentality of any one or more of the foregoing. The term '
            '"employee" also includes an officer of a corporation.'
        )
        print("=" * 60)
        print("STATUTORY CONSTRUCTION ANALYSIS: 26 U.S.C. § 3401(c)")
        print("=" * 60)
        print(f"\nText:\n  {sample_3401c}\n")

        flags = analyze_includes_clause(sample_3401c, "26 U.S.C. § 3401(c)")
        for f in flags:
            print(f"  [{f['canon'].upper()}]")
            print(f"    Rule: {f['rule']}")
            print(f"    Authority: {f.get('authority', 'N/A')}")
            print(f"    Finding: {f['finding']}")
            print(f"    Implication: {f['implication']}")
            if f.get("items_listed"):
                print(f"    Items listed: {f['items_listed']}")
            print()

        # Also test CFR divergence
        sample_cfr = (
            'Under the common-law test, a worker is an employee if the employer '
            'has the right to control how, when, and where the work is performed. '
            'Both full-time and part-time workers performing services for compensation '
            'under behavioral control and financial control of the employer are employees. '
            'Temporary and seasonal workers are also included.'
        )
        print("=" * 60)
        print("CFR-vs-USC DIVERGENCE CHECK")
        print("=" * 60)
        divergence = check_cfr_usc_divergence(
            sample_3401c, sample_cfr,
            "26 U.S.C. § 3401(c)", "26 CFR § 31.3401(c)-1"
        )
        for d in divergence:
            print(f"\n  [{d['type'].upper()}] Severity: {d['severity']}")
            print(f"    Finding: {d['finding']}")
            print(f"    Authority: {d.get('authority', 'N/A')}")
            if d.get("implication"):
                print(f"    Implication: {d['implication']}")
        return

    # Standard lookup
    result = lookup_definition(args.term, args.title, args.context)
    print(f"\n{'=' * 60}")
    print(f"DEFINITION LOOKUP: '{args.term}' (Title {args.title})")
    print(f"{'=' * 60}")
    print(f"Provenance: {result.provenance}")
    print(f"DB chunks: {result.db_chunks_found}")
    print(f"Source: {', '.join(result.source_citations) or 'N/A'}")

    if result.warnings:
        print(f"\nWARNINGS:")
        for w in result.warnings:
            print(f"  {w}")

    if result.construction_flags:
        print(f"\nSTATUTORY CONSTRUCTION:")
        for f in result.construction_flags:
            print(f"  [{f['canon'].upper()}] {f['finding']}")

    if result.conformity_flags:
        print(f"\nCFR-USC DIVERGENCE:")
        for f in result.conformity_flags:
            print(f"  [{f['type'].upper()}] {f['finding']}")

    if result.answer:
        print(f"\nDEFINITION TEXT:")
        print(f"  {result.answer[:500]}")


if __name__ == "__main__":
    main()
