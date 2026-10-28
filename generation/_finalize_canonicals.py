"""
generation/_finalize_canonicals.py — Block 8 supporting data

Maps constitutional duty IDs to canonical case citation parentheticals
that the finalize_brief script auto-inserts after each constitutional
duty mention.

These are the canonical Mathews/Mullane/International Shoe authorities
that brief drafts MUST cite when asserting due process violations but
that the engine doesn't auto-insert.

Pure data module. No LLM. No interpretation.
"""
# === FINALIZE_REGEX_FIX_V1 ===
from __future__ import annotations

# ─────────────────────────────────────────────────────────────────────────
# Constitutional duty ID → list of canonical case parentheticals
# Match key is duty ID from constitutional.yaml
# ─────────────────────────────────────────────────────────────────────────
CONSTITUTIONAL_CANONICALS = {
    "const_001": [
        "Mullane v. Central Hanover Bank & Trust Co., 339 U.S. 306 (1950)",
        "Mathews v. Eldridge, 424 U.S. 319 (1976)",
    ],
    "const_002": [
        "Fuentes v. Shevin, 407 U.S. 67 (1972)",
        "Mathews v. Eldridge, 424 U.S. 319 (1976)",
    ],
    "const_003": [
        "International Shoe Co. v. Washington, 326 U.S. 310 (1945)",
        "Burger King Corp. v. Rudzewicz, 471 U.S. 462 (1985)",
    ],
}

# By source identifier (fallback if duty ID not present)
CONSTITUTIONAL_BY_SOURCE = {
    "U.S. Const. amend. XIV § 1": [
        "Mullane v. Central Hanover Bank & Trust Co., 339 U.S. 306 (1950)",
        "Mathews v. Eldridge, 424 U.S. 319 (1976)",
        "International Shoe Co. v. Washington, 326 U.S. 310 (1945)",
    ],
}


# ─────────────────────────────────────────────────────────────────────────
# Citation format conversion table
# Maps internal-ID format (engine output) → court-grade citation format
# ─────────────────────────────────────────────────────────────────────────

# Specific known mappings (highest priority — exact match)
EXACT_CITATION_REWRITES = {
    "73-PS-201-2":          "73 P.S. § 201-2",
    "40-P-S-1171.1":        "40 P.S. § 1171.1",
    "42-Pa-CS-8371":        "42 Pa.C.S. § 8371",
    "75-Pa-CS-1799.3":      "75 Pa.C.S. § 1799.3",
    "31-Pa-Code-146":       "31 Pa. Code § 146",
    "31-Pa-Code-146.3":     "31 Pa. Code § 146.3",
    "15-USC-1681i":         "15 U.S.C. § 1681i",
    "15-USC-1681e":         "15 U.S.C. § 1681e",
    "15-USC-1681n":         "15 U.S.C. § 1681n",
    "15-USC-1681o":         "15 U.S.C. § 1681o",
    "15-USC-1692e":         "15 U.S.C. § 1692e",
    "15-USC-1692f":         "15 U.S.C. § 1692f",
    "12-USC-2605":          "12 U.S.C. § 2605",
    "12-CFR-1024":          "12 C.F.R. Part 1024",
    "28-USC-1446":          "28 U.S.C. § 1446",
    "Restatement-2d-Contracts-205":
        "Restatement (Second) of Contracts § 205 (1981)",
    "Restatement-2d-Contracts-235":
        "Restatement (Second) of Contracts § 235 (1981)",
    "Restatement-2d-Torts-222A":
        "Restatement (Second) of Torts § 222A (1965)",
    "Restatement-3d-Restitution-1":
        "Restatement (Third) of Restitution and Unjust Enrichment § 1 (2011)",
    "NAIC-Model-Act-900":
        "NAIC Unfair Claims Settlement Practices Model Act #900",
    "Fiduciary-Insurance-Doctrine":
        "Fiduciary Insurance Doctrine (PA common law)",
    "Uberrimae-Fidei-Doctrine":
        "Uberrimae Fidei Doctrine (PA common law of insurance)",
    "PA-Quiet-Title-Doctrine":
        "Pennsylvania Quiet Title Doctrine (PA common law)",
    "PA-Fraudulent-Concealment-Doctrine":
        "Pennsylvania Fraudulent Concealment Doctrine (PA common law)",
    "rule_60b4_void_judgment":
        "Fed. R. Civ. P. 60(b)(4)",
    "due_process_voidness":
        "Due Process Voidness Doctrine (U.S. Const. amend. XIV § 1)",
    "coram_nobis_default_vacatur":
        "Coram Nobis (PA common law) / Default Judgment Vacatur",
}

# Pattern-based mappings (regex fallback for ones not in exact list)
import re as _re

CITATION_REGEX_REWRITES = [
    # Pa.R.C.P. — already correct format from procedural_rules.yaml
    # 75-Pa-CS-XXXX → 75 Pa.C.S. § XXXX
    (_re.compile(r"\b(\d+)-Pa-CS-([\d\.]+)"),
     r"\1 Pa.C.S. § \2"),
    # 42-Pa-CS-XXXX → 42 Pa.C.S. § XXXX
    # 73-PS-XXX-X → 73 P.S. § XXX-X
    (_re.compile(r"\b(\d+)-PS-([\d\-]+)"),
     r"\1 P.S. § \2"),
    # 40-P-S-XXXX → 40 P.S. § XXXX (handle dashed P-S form)
    (_re.compile(r"\b(\d+)-P-S-([\d\.]+)"),
     r"\1 P.S. § \2"),
    # 15-USC-XXXX → 15 U.S.C. § XXXX
    (_re.compile(r"\b(\d+)-USC-([\d]+[a-z]*)\b"),
     r"\1 U.S.C. § \2"),
    # 12-CFR-XXXX → 12 C.F.R. Part XXXX
    (_re.compile(r"\b(\d+)-CFR-([\d\.]+)"),
     r"\1 C.F.R. Part \2"),
    # 28-USC-XXXX → 28 U.S.C. § XXXX
    (_re.compile(r"\b(\d+)-Pa-Code-([\d\.]+)"),
     r"\1 Pa. Code § \2"),
]


def reformat_citation(text: str) -> str:
    """Apply both exact and regex citation rewrites to a string."""
    if not text:
        return text
    # Pass 1: exact rewrites (more specific first, sorted by length desc)
    for old in sorted(EXACT_CITATION_REWRITES.keys(),
                      key=lambda x: -len(x)):
        if old in text:
            text = text.replace(old, EXACT_CITATION_REWRITES[old])
    # Pass 2: regex rewrites for remaining patterns
    for pattern, repl in CITATION_REGEX_REWRITES:
        text = pattern.sub(repl, text)
    return text
