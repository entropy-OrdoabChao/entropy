"""
hierarchy_enforcer.py — Scan a case's filings for binding-authority violations.

This is the killer module. For each filing (especially opposing motions and
court orders), it:

  1. Extracts every case citation.
  2. Classifies each citation's issuing court (via court_hierarchy).
  3. Checks whether that court binds the case's actual court.
  4. Flags three categories of violation:
       - NON_BINDING_AS_CONTROLLING: non-binding authority presented as if controlling
       - BINDING_IGNORED:            (requires DB cross-reference — stub for now)
       - RULE_CONFLICT:              (requires rules module — stub for now)

NO LLM. Pure regex + deterministic lookups. The LLM layer will cross-check
our findings later, but the initial detection is code, not inference.
"""

from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import re
import logging
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Iterable

from .court_hierarchy import audit_citation, classify_court  # CitationAudit removed in court_hierarchy refactor

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

log = logging.getLogger("beezle.hierarchy_enforcer")


# =============================================================================
# CITATION EXTRACTION
# =============================================================================
# Strategy: find "X v. Y" anchors then capture everything up to the first
# closing paren that contains a year. This handles both "X v. Y, 123 Rep 456
# (Court 2020)" and "X v. Y, No. 1234-cv-56, 2021 WL 789 (E.D. Pa. 2021)".

# "Party v. Party" anchor
PARTY_ANCHOR = re.compile(
    r"""
    \b
    ([A-Z][A-Za-z'.\-]+                     # First word of party name
     (?:\s+(?:[A-Za-z'.&\-]+|of))*           # Additional words
    )
    \s+v\.?\s+                                # "v." separator
    ([A-Z][A-Za-z'.\-]+                       # First word of second party
     (?:\s+(?:[A-Za-z'.&\-]+|of))*
    )
    """,
    re.VERBOSE,
)

# Reporter-style cite for standalone detection
REPORTER_PATTERN = re.compile(
    r"""
    (\b\d+\s+                                # Volume number
     (?:U\.S\.|S\.\s*Ct\.|F\.(?:2d|3d|4th)?|F\.\s*Supp\.(?:\s*2d|\s*3d)?
       |Pa\.(?:\s*Super\.?|\s*Commw\.?)?|A\.(?:2d|3d)?
       |N\.J\.(?:\s*Super\.?)?|Pa\.\s*D\.\s*&\s*C\.(?:\s*\d+[a-z]{0,2})?
     )\s+
     \d+                                     # First page
     (?:\s*,\s*[\*\d]+)?                     # Optional pincite (may have asterisk)
     (?:\s*\([^)]*\d{4}\))?                  # Court + year
    )
    """,
    re.VERBOSE,
)

# Year-in-parens marker — we look for this to close out a citation
YEAR_PAREN = re.compile(r"\(([^()]*?\d{4}[^()]*?)\)")


# V-separator positions — anchors at every "X v. Y" we can find
V_SEPARATOR = re.compile(r"\s+v\.?\s+", re.I)

# Capitalized word that could be part of a party name
PARTY_WORD = re.compile(r"[A-Z][A-Za-z'.\-&]*\.?")

# Year-in-parens marker — we look for this to close out a citation
YEAR_PAREN = re.compile(r"\(([^()]*?\d{4}[^()]*?)\)")


def _find_party_anchors(text: str):
    """
    Yield (start, end) positions of every Party-v-Party anchor in text.
    Walks backward from each "v." to find the preceding party name (up to
    5 words, all capitalized), then forward to find the trailing party name.
    Avoids the greedy-regex problem where a single anchor can swallow
    a whole sentence.
    """
    for vm in V_SEPARATOR.finditer(text):
        v_start = vm.start()
        v_end = vm.end()

        # Walk backward up to 60 chars and split into tokens
        back_window = text[max(0, v_start - 80):v_start]
        # Find the rightmost run of capitalized tokens
        tokens = re.findall(r"\S+", back_window)
        # Take up to 5 trailing tokens that look like party words
        party_left_tokens = []
        for tok in reversed(tokens):
            # Strip trailing comma/period for the test
            clean = tok.rstrip(",.;:")
            if clean and clean[0].isupper() and len(clean) <= 30:
                party_left_tokens.insert(0, tok)
                if len(party_left_tokens) >= 5:
                    break
            elif clean.lower() in ("of", "the", "and", "&"):
                # connector word — keep going
                if party_left_tokens:
                    party_left_tokens.insert(0, tok)
                else:
                    continue
            else:
                break

        if not party_left_tokens:
            continue

        # Compute the actual start position
        joined_left = " ".join(party_left_tokens)
        left_start = back_window.rfind(party_left_tokens[0])
        if left_start == -1:
            continue
        start = max(0, v_start - 80) + left_start

        # Walk forward up to 60 chars for the right-side party name
        forward_window = text[v_end:v_end + 80]
        forward_tokens = re.findall(r"\S+", forward_window)
        party_right_tokens = []
        for tok in forward_tokens:
            clean = tok.rstrip(",.;:")
            if clean and clean[0].isupper() and len(clean) <= 30:
                party_right_tokens.append(tok)
                if len(party_right_tokens) >= 5:
                    break
            elif clean.lower() in ("of", "the", "and", "&"):
                if party_right_tokens:
                    party_right_tokens.append(tok)
                else:
                    break
            else:
                break

        if not party_right_tokens:
            continue

        # Anchor identified — return the v_start as the anchor center
        yield start


def extract_citations(text: str) -> List[str]:
    """
    Pull all case citations out of a chunk of text.
    For each "Party v. Party" anchor, extend forward until the first
    closing paren containing a year, and treat that span as the full citation.

    A valid citation must contain one of: a proper reporter abbreviation
    (F.3d, F. Supp., Pa., U.S., S. Ct., A.2d, etc.), OR a Westlaw number
    (YYYY WL NNNNN), OR a slip-opinion marker (U.S. ___).
    """
    seen = set()
    results = []

    if not text:
        return results

    # A "real reporter" pattern — any of these must appear inside the span
    # for it to be treated as a citation rather than a party caption
    REAL_REPORTER = re.compile(
        r"""
        (?:
            \b\d+\s+F\.(?:2d|3d|4th)\b                   # F.2d / F.3d / F.4th
          | \b\d+\s+F\.\s*Supp\.(?:\s*2d|\s*3d)?\b       # F. Supp.
          | \b\d+\s+U\.?S\.?\s*(?:\d+|_+)                # U.S. (including slip)
          | \b\d+\s+S\.\s*Ct\.\s*\d+                     # S. Ct.
          | \b\d+\s+L\.\s*Ed\.(?:\s*2d)?\s*\d+           # L. Ed.
          | \b\d+\s+Pa\.(?:\s+Super\.?|\s+Commw\.?|\s+C\.|\s+D\.)?   # Pa reporters
          | \b\d+\s+A\.(?:2d|3d)\b                       # A.2d / A.3d
          | \b\d+\s+N\.J\.(?:\s+Super\.?)?               # N.J. reporters
          | \b\d+\s+N\.?E\.?(?:\s*2d|\s*3d)?\b          # N.E. reporters
          | \b\d+\s+N\.?W\.?(?:\s*2d|\s*3d)?\b          # N.W. reporters
          | \b\d{4}\s+WL\s+\d+                           # Westlaw
          | \b\d{4}\s+U\.?S\.?\s*Dist\.?\s*LEXIS         # LEXIS
          | \b\d{4}\s+Pa\.\s*Dist\.\s*(?:&|and)\s*Cnty\.\s*Dec\.\s*LEXIS
        )
        """,
        re.VERBOSE,
    )

    # Strategy 1: Party v. Party anchors, extended to year-in-paren
    for start in _find_party_anchors(text):
        # Look ahead up to 600 chars for a closing year-paren
        lookahead = text[start:start + 600]
        year_match = YEAR_PAREN.search(lookahead)
        if not year_match:
            continue
        end = start + year_match.end()
        cite = text[start:end]
        cite = re.sub(r"\s+", " ", cite).strip().rstrip(",. ")

        # Length filter
        if len(cite) < 20 or len(cite) > 400:
            continue

        # HARD REQUIREMENT: must contain a real reporter abbreviation
        if not REAL_REPORTER.search(cite):
            continue

        # Reject if the "citation" starts with a pronoun/verb/preamble indicating
        # it's actually the case caption bleeding through
        if re.match(r"\s*(Plaintiff|Defendant|FILER|FILER|COMES\s+NOW)\b", cite, re.I):
            continue
        if re.match(r"\s*IN\s+THE\s+(COURT|UNITED\s+STATES|SUPERIOR|SUPREME|COMMONWEALTH)", cite, re.I):
            continue
        if re.match(r"\s*(EASTERN|WESTERN|MIDDLE|SOUTHERN|NORTHERN)\s+DISTRICT", cite, re.I):
            continue
        # Reject if it contains case caption signals anywhere (not just at start)
        if re.search(r"\b(?:Case\s+No\.|Civil\s+Action\s+No\.|DEFENDANT\s+\w+\s+(?:OPPOSITION|MOTION|ANSWER))", cite, re.I):
            continue
        if re.search(r"\b(?:PLEAS|COURT)\s+OF\s+(?:PHILADELPHIA|ALLEGHENY|MONTGOMERY|BUCKS|CHESTER|DELAWARE)\b", cite, re.I):
            continue

        if cite.lower() not in seen:
            seen.add(cite.lower())
            results.append(cite)

    # Strategy 2: standalone reporter cites (no v.)
    for match in REPORTER_PATTERN.finditer(text):
        cite = match.group(1).strip()
        cite = re.sub(r"\s+", " ", cite).rstrip(",. ")
        if len(cite) < 10 or len(cite) > 300:
            continue
        # Skip if already captured as part of a party-anchor cite
        if any(cite.lower() in existing.lower() for existing in results):
            continue
        if cite.lower() not in seen:
            seen.add(cite.lower())
            results.append(cite)

    return results


# =============================================================================
# VIOLATION TYPES
# =============================================================================

VIOLATION_NON_BINDING = "NON_BINDING_AS_CONTROLLING"
VIOLATION_BINDING_IGNORED = "BINDING_IGNORED"
VIOLATION_RULE_CONFLICT = "RULE_CONFLICT"
VIOLATION_TRIAL_COURT_AS_APPELLATE = "TRIAL_COURT_AS_APPELLATE_AUTHORITY"


@dataclass
class HierarchyViolation:
    """One flagged hierarchy/stare-decisis violation."""
    violation_type: str
    severity: str                    # "fatal" | "serious" | "minor"
    citation: str
    cited_court: str
    case_court: str
    source_file: str
    explanation: str
    fix: str = ""                    # Recommended countermove

    def to_dict(self) -> dict:
        return asdict(self)


# =============================================================================
# FILE READER
# =============================================================================

def read_text_from_file(path) -> str:
    """
    Read text from a filing. Tries in order:
      1. If .txt, read directly
      2. Sibling .txt in same dir (legacy pipeline output)
      3. Look up in <home>\\text_cache via _manifest.json (Path 2)
    Returns empty string if no text found.
    """
    from pathlib import Path
    path = Path(path)

    # Direct .txt
    if path.suffix.lower() == ".txt" and path.exists():
        for encoding in ("utf-8", "cp1252", "latin-1"):
            try:
                return path.read_text(encoding=encoding, errors="ignore")
            except Exception:
                continue
        return ""

    # Sibling .txt
    sibling_txt = path.with_suffix(".txt")
    if sibling_txt.exists():
        for encoding in ("utf-8", "cp1252", "latin-1"):
            try:
                return sibling_txt.read_text(encoding=encoding, errors="ignore")
            except Exception:
                continue

    # Path 2 text cache lookup via manifest
    text = _read_from_text_cache(str(path))
    if text:
        return text

    return ""


# Cache the manifest in memory so we don't reload it for every file
_MANIFEST_CACHE: Optional[dict] = None
_MANIFEST_PATHS = [
    (_BEEZLE_ROOT / "text_cache" / "_manifest.json"),
]


def _load_manifest() -> dict:
    global _MANIFEST_CACHE
    if _MANIFEST_CACHE is not None:
        return _MANIFEST_CACHE
    import json
    for mp in _MANIFEST_PATHS:
        if mp.exists():
            try:
                _MANIFEST_CACHE = json.loads(mp.read_text(encoding="utf-8"))
                log.info(f"Loaded text cache manifest with {len(_MANIFEST_CACHE)} entries")
                return _MANIFEST_CACHE
            except Exception as e:
                log.warning(f"Failed to load manifest at {mp}: {e}")
    _MANIFEST_CACHE = {}
    return _MANIFEST_CACHE


def _read_from_text_cache(original_path: str) -> str:
    """Look up cached extracted text for an original PDF/DOCX path."""
    manifest = _load_manifest()
    if not manifest:
        return ""

    # Manifest key is the absolute original path. Try exact, then case-insensitive.
    entry = manifest.get(original_path)
    if not entry:
        # Case-insensitive fallback for Windows path casing differences
        op_lower = original_path.lower()
        for k, v in manifest.items():
            if k.lower() == op_lower:
                entry = v
                break
    if not entry:
        return ""

    sha1 = entry.get("sha1", "")
    if not sha1:
        return ""

    cache_root = _MANIFEST_PATHS[0].parent
    cache_file = cache_root / sha1[:2] / f"{sha1}.txt"
    if not cache_file.exists():
        return ""

    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            return cache_file.read_text(encoding=encoding, errors="ignore")
        except Exception:
            continue
    return ""


# =============================================================================
# ENFORCER
# =============================================================================

class HierarchyEnforcer:
    """
    For a given case (from case_consolidator), walk every filing and return
    a list of hierarchy violations.
    """

    def __init__(self, case_court: str):
        self.case_court = case_court

    def audit_file(self, file_path: str) -> List[HierarchyViolation]:
        """Audit a single filing for hierarchy violations."""
        text = read_text_from_file(Path(file_path))
        if not text:
            return []

        citations = extract_citations(text)
        violations = []

        for cite in citations:
            # audit_citation now returns a DICT {citation, level, binding, weight, jurisdiction, notes}
            # (court_hierarchy refactor); severity/cited_court are derived from 'level'.
            audit = audit_citation(cite, self.case_court)

            if audit.get("binding"):
                continue  # Binding authority, no violation

            level = audit.get("level", "unknown")
            cited_court = level.replace("_", " ")
            is_trial = level in ("district_court", "state_trial")
            severity = "fatal" if is_trial else ("serious" if level != "unknown" else "note")

            if is_trial:
                vtype = VIOLATION_TRIAL_COURT_AS_APPELLATE
                explanation = (
                    f"Citation '{cite[:80]}...' is from a {cited_court}, "
                    f"which is a trial/inferior court with no binding authority over "
                    f"{self.case_court}. If the filing presents this as controlling "
                    f"authority, it violates stare decisis."
                )
                fix = (
                    f"Cite binding appellate authority on the same issue. Trial/district "
                    f"court decisions are persuasive only; distinguish the cited case as non-binding."
                )
            elif level != "unknown":
                vtype = VIOLATION_NON_BINDING
                explanation = (
                    f"Citation from a {cited_court} does not bind {self.case_court}. "
                    f"It may be persuasive but cannot be presented as controlling."
                )
                fix = (
                    f"Identify the actual binding authority on this issue (Supreme Court or the "
                    f"controlling circuit), not persuasive authority from a sister court."
                )
            else:
                vtype = VIOLATION_NON_BINDING
                explanation = f"Non-binding or unresolved authority ({cited_court})."
                fix = "Note persuasive status; rely on true binding authority."

            violations.append(HierarchyViolation(
                violation_type=vtype,
                severity=severity,
                citation=cite,
                cited_court=cited_court,
                case_court=self.case_court,
                source_file=str(file_path),
                explanation=explanation,
                fix=fix,
            ))

        return violations

    def audit_case(self, filings: Iterable[str]) -> List[HierarchyViolation]:
        """Audit every filing in a case."""
        all_violations = []
        for f in filings:
            all_violations.extend(self.audit_file(f))
        return all_violations

    def audit_case_grouped(self, case_dict: dict) -> dict:
        """
        Audit a ConsolidatedCase. Dedupes violations by (canonical_file, citation)
        so the same citation found via both sibling .txt and .pdf doesn't
        double-count.
        """
        result = {
            "case_id": case_dict.get("case_id"),
            "case_court": case_dict.get("case_court"),
            "total_violations": 0,
            "fatal_count": 0,
            "serious_count": 0,
            "minor_count": 0,
            "by_file": {},
            "all": [],
        }

        priority_files = (
            case_dict.get("orders", [])
            + case_dict.get("oppositions", [])
            + case_dict.get("motions_to_dismiss", [])
        )

        seen: set[tuple[str, str]] = set()

        for f in priority_files:
            # Canonical key: strip extension so foo.pdf and foo.txt dedup
            canonical = str(Path(f).with_suffix(""))
            vs = self.audit_file(f)
            if not vs:
                continue

            unique_here = []
            for v in vs:
                cite_key = re.sub(r"\s+", " ", v.citation.lower()).strip()[:120]
                key = (canonical, cite_key)
                if key in seen:
                    continue
                seen.add(key)
                unique_here.append(v)

            if unique_here:
                result["by_file"][f] = [v.to_dict() for v in unique_here]
                for v in unique_here:
                    result["all"].append(v.to_dict())
                    result["total_violations"] += 1
                    if v.severity == "fatal":
                        result["fatal_count"] += 1
                    elif v.severity == "serious":
                        result["serious_count"] += 1
                    else:
                        result["minor_count"] += 1

        return result
