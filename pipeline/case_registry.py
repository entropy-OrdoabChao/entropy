"""
case_registry.py â€” Authoritative case identity registry.

This is the source of truth for "what is a case in Beezle." Replaces
party-tag heuristics and folder-based guessing with hand-curated docket
identity loaded from real Philadelphia FJD docket sheets and federal
PACER records.

The registry is loaded from <home>\\cases_index\\case_registry.json,
which is built and maintained by beezle_register_dockets.py.

Key concept: ONE canonical case can have many docket aliases. State case
250500817 and federal case 2:26-cv-00123 are the SAME case at different
procedural stages â€” both should resolve to canonical_id "fed:2:26-cv-00123"
(the federal docket, since that's where the case currently lives).

When a state case gets removed to federal:
  - state_origin_docket: 250500817
  - canonical_id: fed:2:26-cv-00123
  - All aliases (state, federal, cross-references, typos) point to the
    canonical_id

When a case mentions an unrelated docket in citation context:
  - The lookup returns None
  - The document's other dockets are checked
  - If no match, the document goes to unassigned_unregistered

This eliminates:
  1. State/federal split where the same case showed as 2 separate cases
  2. False-positive cases built from citation-body docket numbers
  3. Typo'd dockets (2:25-cv-0767) being treated as different cases
  4. Cross-contamination from party-tag overlap (Lippi vs bad faith)
"""

from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import json
import logging
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Set

log = logging.getLogger("beezle.case_registry")


# =============================================================================
# DATACLASSES
# =============================================================================

@dataclass
class CaseRegistryEntry:
    canonical_id: str                       # e.g. "fed:2:26-cv-00123"
    case_court: str                         # EDPA, PA_CP_PHILA, CA3
    caption: str                            # e.g. "FILER VS LIPPI ETAL"
    filed: str = ""                         # ISO date
    status: str = ""                        # active / removed_to_fed / dismissed / etc.
    case_type: str = ""                     # contracts_other / fraud / quiet_title / bad_faith
    judge: str = ""
    docket_aliases: List[str] = field(default_factory=list)  # All ways the docket may appear
    federal_cross_ref: Optional[str] = None
    state_origin_docket: Optional[str] = None
    parties: List[str] = field(default_factory=list)         # Authoritative party list
    related_cases: List[str] = field(default_factory=list)   # canonical_ids of related cases
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict, key_hint: str = "") -> "CaseRegistryEntry":
        """Resilient loader. Accepts legacy/stub shapes:
        - "case_id" as an alias for "canonical_id"
        - "court" as an alias for "case_court"
        - missing fields filled with safe defaults
        key_hint is the registry dict key, used as last-resort canonical_id.
        """
        canonical = (
            d.get("canonical_id")
            or d.get("case_id")
            or key_hint
            or ""
        )
        court = d.get("case_court") or d.get("court") or ""
        return cls(
            canonical_id=canonical,
            case_court=court,
            caption=d.get("caption", ""),
            filed=d.get("filed", ""),
            status=d.get("status", ""),
            case_type=d.get("case_type", ""),
            judge=d.get("judge", ""),
            docket_aliases=d.get("docket_aliases", []),
            federal_cross_ref=d.get("federal_cross_ref"),
            state_origin_docket=d.get("state_origin_docket"),
            parties=d.get("parties", []),
            related_cases=d.get("related_cases", []),
            notes=d.get("notes", []),
        )


# =============================================================================
# DOCKET NORMALIZATION
# =============================================================================

# All these patterns refer to the SAME canonical docket identifier.
# Normalization makes lookups robust to formatting variation.

def normalize_docket(raw: str) -> str:
    """
    Normalize a docket string to a canonical lookup form.

    Strips:
      - Whitespace
      - Case ('No.', 'No', 'no.')
      - Court labels ('Civil Action No.', 'Case No.', 'Docket No.')
      - Judge initials suffix ('-XYZ', '-MAK')
      - 'DC ' prefix on cross-references
      - Hyphens (so 2:26-cv-00123 == 2:25cv02822 == 22502822)

    Examples:
      "No. 260100000"           -> "260100000"
      "2:26-cv-00123-XYZ"       -> "225cv02822"
      "2:26-cv-00123"           -> "225cv02822"
      "DC 2025CV02822"          -> "2025cv02822"
      "2025CV02822"             -> "2025cv02822"
      "October Term 2024 No. 002969" -> "2024oct002969"
    """
    if not raw:
        return ""
    s = raw.strip()
    # Strip court labels
    s = re.sub(r"^(?:civil\s+action\s+no\.?|case\s+no\.?|docket\s+no\.?|no\.?)\s*", "",
               s, flags=re.IGNORECASE)
    # Strip "DC " prefix
    s = re.sub(r"^dc\s+", "", s, flags=re.IGNORECASE)
    # Strip judge initials suffix on federal dockets
    s = re.sub(r"-[A-Z]{2,4}$", "", s)
    # October Term form
    m = re.match(r"october\s+term\s+(\d{4})\s+no\.?\s*(\d+)", s, re.IGNORECASE)
    if m:
        year, num = m.groups()
        return f"{year}oct{num.zfill(6)}"
    # Strip remaining whitespace and lowercase
    s = re.sub(r"\s+", "", s).lower()
    # Strip hyphens and colons
    s = s.replace("-", "").replace(":", "")
    return s


# Federal docket short-form regex: <year>[<court_code>]<sequence>[<judge>]
# Anchored at end of string so it matches the FULL identifier, not a fragment.
_SHORT_SIG_FEDERAL_RE = re.compile(
    r"""
    (?:^|[\s:-])           # start or boundary
    (\d{2,4})              # year (2 or 4 digit)
    [\s:-]*
    (?:cv|cr|mc|mj|bk|md)? # optional court code
    [\s:-]*
    (\d{1,8})              # sequence
    (?:[\s-][A-Z]{2,4})?   # optional judge initials
    $                      # anchored
    """,
    re.IGNORECASE | re.VERBOSE,
)


def short_sig(raw: str):
    """
    Extract an unambiguous (year_2digit, sequence_int) signature from a
    federal-style docket alias. Returns None if the alias doesn't fit
    the federal pattern (state cases stay in the full-form index).

    Examples:
      "25-2822"          -> ("25", 2822)
      "2:26-cv-00123"    -> ("25", 2822)
      "25cv02822"        -> ("25", 2822)
      "2025-CV-02822"    -> ("25", 2822)
      "241099828"        -> None  (state-style; ambiguous)
    """
    if not raw:
        return None
    s = raw.strip()
    s = re.sub(r"^(?:civil\s+action\s+no\.?|case\s+no\.?|docket\s+no\.?|no\.?)\s*", "",
               s, flags=re.IGNORECASE)
    s = re.sub(r"^dc\s+", "", s, flags=re.IGNORECASE)
    s = s.replace(":", " ").replace("-", " ").strip()

    m = _SHORT_SIG_FEDERAL_RE.search(s)
    if not m:
        return None
    yr, seq = m.group(1), m.group(2)
    if len(yr) == 4:
        yr = yr[-2:]
    elif len(yr) != 2:
        return None
    try:
        seq_int = int(seq)
    except ValueError:
        return None
    if seq_int == 0:
        return None
    return (yr, seq_int)


# =============================================================================
# REGISTRY
# =============================================================================

class CaseRegistry:
    """The authoritative case registry. Loaded once, queried many times."""

    def __init__(self, registry_path: Optional[Path] = None):
        self.path = Path(registry_path) if registry_path else (_eroot() / "cases_index/case_registry.json")
        self.entries: Dict[str, CaseRegistryEntry] = {}
        # Index: normalized_docket -> canonical_id
        self.docket_index: Dict[str, str] = {}
        # Secondary index: (yy, seq_int) -> canonical_id (federal short form)
        self.short_sig_index: Dict[tuple, str] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            log.warning(f"Case registry not found at {self.path}")
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as e:
            log.error(f"Failed to load registry: {e}")
            return

        for canonical_id, entry_dict in data.items():
            try:
                entry = CaseRegistryEntry.from_dict(entry_dict, key_hint=canonical_id)
            except Exception as e:
                log.error(f"Skipping malformed registry entry {canonical_id!r}: {e}")
                continue
            self.entries[canonical_id] = entry
            for alias in entry.docket_aliases:
                normalized = normalize_docket(alias)
                if normalized:
                    if normalized in self.docket_index:
                        existing = self.docket_index[normalized]
                        if existing != canonical_id:
                            log.warning(
                                f"Docket {alias} (normalized: {normalized}) "
                                f"maps to both {existing} and {canonical_id}"
                            )
                    self.docket_index[normalized] = canonical_id
                # Also index by federal short signature (yy, seq)
                sig = short_sig(alias)
                if sig:
                    if sig in self.short_sig_index:
                        existing_sig = self.short_sig_index[sig]
                        if existing_sig != canonical_id:
                            log.warning(
                                f"Short sig {sig} from alias {alias!r} "
                                f"maps to both {existing_sig} and {canonical_id}; "
                                f"keeping first"
                            )
                    else:
                        self.short_sig_index[sig] = canonical_id

        log.info(
            f"Loaded case registry: {len(self.entries)} cases, "
            f"{len(self.docket_index)} docket aliases"
        )

    def lookup_canonical(self, docket: str) -> Optional[str]:
        """
        Given a raw docket string from a document, return the canonical
        case ID. Returns None if no registry match found.

        Resolution order:
          1. Exact match against full normalized form (existing behavior)
          2. Federal short-form signature fallback (yy, seq_int) — only
             matches when unambiguous across all registered cases.
        """
        if not docket:
            return None
        normalized = normalize_docket(docket)
        hit = self.docket_index.get(normalized)
        if hit:
            return hit
        # Fallback: federal short-form
        sig = short_sig(docket)
        if sig:
            return self.short_sig_index.get(sig)
        return None

    def get_entry(self, canonical_id: str) -> Optional[CaseRegistryEntry]:
        return self.entries.get(canonical_id)

    def parties_for(self, canonical_id: str) -> List[str]:
        entry = self.entries.get(canonical_id)
        return list(entry.parties) if entry else []

    def all_canonical_ids(self) -> List[str]:
        return sorted(self.entries.keys())

    def __len__(self) -> int:
        return len(self.entries)

    def __contains__(self, canonical_id: str) -> bool:
        return canonical_id in self.entries


# Singleton instance for use throughout pipeline
_REGISTRY: Optional[CaseRegistry] = None


def get_registry() -> CaseRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = CaseRegistry()
    return _REGISTRY


# BEEZLE_FN_UNUSED 2026-06-13: reload_registry (no static refs in pipeline/)
def reload_registry() -> CaseRegistry:
    global _REGISTRY
    _REGISTRY = CaseRegistry()
    return _REGISTRY
