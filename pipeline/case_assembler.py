"""
case_assembler.py — Group Document records into consolidated cases.

Takes a list of Documents from document_scanner and builds a case_map
compatible with the rest of the Beezle pipeline (hierarchy_enforcer,
dismissal_analyzer, scorecard_patch).

Pipeline:
  1. Group documents by their primary detected docket number
  2. For docket-less documents with a Filer mention, use defendant tag
     to smart-merge into an existing case (option 3 from earlier discussion):
       - If exactly one case has a matching defendant in all_defendants, merge
       - If zero or multiple candidates, park in unknown_docket
  3. Emit ConsolidatedCase records identical in shape to the old output
     so hierarchy_enforcer etc. work unchanged

Output structure matches the old case_consolidator:
    {
      "fed:2:26-cv-00123": {
          "case_id": "2:26-cv-00123",
          "jurisdiction": "fed",
          "case_court": "EDPA",
          "party_tag": "transunion",
          "title": "Filer v. Transunion (2:26-cv-00123)",
          "all_defendants": ["transunion", "equifax", ...],
          "source_folders": ["source paths..."],
          "orders": ["file paths..."],
          "oppositions": [...],
          "motions_to_dismiss": [...],
          "filings": [...],
          "my_drafts": [...],      # NEW: documents authored by Filer
          "exhibits": [...],
          "notes": [...]
      },
      ...
    }
"""

from __future__ import annotations
import json
import logging
import re
from dataclasses import dataclass, field, asdict
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .document_scanner import Document

log = logging.getLogger("beezle.assembler")


JURIS_TO_COURT = {
    "fed":       "EDPA",
    "phila_ccp": "PA_CP_PHILA",
    "ca3":       "CA3",
    "state_cv":  "PA_CP_PHILA",
    "unknown":   "UNKNOWN",
}

# Priority for picking a primary docket when a document has multiple
# (e.g., a filing that cites OTHER case numbers in its body)
JURIS_PRIORITY = {"fed": 0, "ca3": 1, "state_cv": 2, "phila_ccp": 3}


# S-no-hardcode (2026-11-14): "filer" was spelled into the author test, two party filters and
# the title builder, so for any other filer the author routing and the defendant list were wrong.
# The filer's tags/name come from cases_index/filer_identity.json via pipeline.case_profile.
def _filer_tags() -> set:
    try:
        from pipeline.case_profile import filer_party_tags, filer_tokens
        return filer_party_tags() | filer_tokens()
    except Exception:
        return set()


def _filer_surname() -> str:
    try:
        from pipeline.case_profile import filer_surname
        return filer_surname()
    except Exception:
        return ""


@dataclass
class ConsolidatedCase:
    case_id: str
    jurisdiction: str
    case_court: str
    party_tag: str = ""
    title: str = ""
    all_defendants: List[str] = field(default_factory=list)
    source_folders: List[str] = field(default_factory=list)
    filings: List[str] = field(default_factory=list)
    my_drafts: List[str] = field(default_factory=list)
    exhibits: List[str] = field(default_factory=list)
    orders: List[str] = field(default_factory=list)
    motions_to_dismiss: List[str] = field(default_factory=list)
    oppositions: List[str] = field(default_factory=list)
    court_notices: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    document_count: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def doctype_bucket(doc: Document) -> str:
    """Map doc_type to which list on the ConsolidatedCase this doc belongs in.

    DOCTYPE-FIRST for unambiguous adversary types: motion_to_dismiss and
    motion_to_strike ALWAYS route to auditable buckets regardless of what
    the author detector says, because Filer doesn't file MTDs against
    himself and doesn't file motions to strike his own filings.

    AUTHOR-FIRST for ambiguous types (opposition, reply, rebuttal, memorandum):
    These can legitimately be authored by either side, so we trust the
    author detector. Filer-authored files go to my_drafts.
    """
    # ----- Court / administrative -----
    if doc.doc_type == "order":
        return "orders"
    if doc.doc_type == "exhibit":
        return "exhibits"
    if doc.doc_type == "court_notice":
        return "court_notices"
    if doc.doc_type == "efile_rejection":
        return "court_notices"
    if doc.doc_type == "service":
        return "filings"  # Certificates of service are not auditable
    if doc.doc_type == "motion_cover_sheet":
        return "filings"  # FJD cover sheets are non-substantive

    # ----- DOCTYPE-FIRST adversary routing -----
    # These doctypes are unambiguously adversary regardless of author detector
    if doc.doc_type == "motion_to_dismiss":
        return "motions_to_dismiss"
    if doc.doc_type == "motion_to_strike":
        return "oppositions"

    # ----- AUTHOR-FIRST routing for ambiguous types -----
    # filer-authored anything else goes to my_drafts
    if doc.author in _filer_tags():
        return "my_drafts"

    # ----- Adversary-authored ambiguous types -----
    if doc.doc_type == "opposition":
        return "oppositions"
    if doc.doc_type in ("memorandum", "motion") and doc.author == "adversary":
        return "oppositions"

    # ----- Court-authored -----
    if doc.author == "court":
        return "court_notices"

    # ----- Incoming mail -----
    if doc.author == "incoming_mail":
        return "filings"

    return "filings"


class CaseAssembler:
    """Groups Documents into ConsolidatedCase records."""

    def __init__(self, use_registry: bool = True):
        self.cases: Dict[str, ConsolidatedCase] = {}
        self.unassigned: List[Document] = []
        self.unassigned_unregistered: List[Document] = []  # Has docket but not in registry
        self.reference_docs: List[Document] = []
        self.use_registry = use_registry
        self.registry = None
        if use_registry:
            try:
                from .case_registry import get_registry
                self.registry = get_registry()
                if len(self.registry) > 0:
                    log.info(f"Using case registry with {len(self.registry)} canonical cases")
                else:
                    log.warning("Case registry is empty - falling back to docket-only assembly")
                    self.registry = None
            except Exception as e:
                log.warning(f"Could not load case registry: {e}")
                self.registry = None

    def assemble(self, documents: List[Document]) -> Dict[str, ConsolidatedCase]:
        """Main entry point."""
        # Phase 0: partition
        docketed: List[Document] = []
        no_docket: List[Document] = []

        for doc in documents:
            if doc.is_reference:
                self.reference_docs.append(doc)
                continue
            if doc.dockets:
                docketed.append(doc)
            else:
                no_docket.append(doc)

        # Phase 1: attach docketed documents to cases
        for doc in docketed:
            self._attach_docketed(doc)

        # Phase 2: build defendant index for smart-merge lookup
        defendant_index: Dict[str, List[str]] = defaultdict(list)
        for key, case in self.cases.items():
            seen = set()
            for d in case.all_defendants:
                if d not in seen:
                    defendant_index[d].append(key)
                    seen.add(d)
            if case.party_tag and case.party_tag not in seen:
                defendant_index[case.party_tag].append(key)

        # Phase 3: smart-merge docket-less documents
        for doc in no_docket:
            if not doc.is_filer_case:
                # Docket-less AND no Filer mention — drop, not a case doc
                continue

            doc_primary = doc.parties[0] if doc.parties else ""

            candidates: Set[str] = set()

            # Tier 1 (strictest): doc's primary party matches case's primary tag
            if doc_primary:
                for key, case in self.cases.items():
                    if case.party_tag == doc_primary:
                        candidates.add(key)

            # Tier 2: doc's primary in case's defendants OR vice versa
            if not candidates and doc.parties:
                for key, case in self.cases.items():
                    if case.party_tag in doc.parties[:2]:
                        candidates.add(key)

            # Tier 3 (NEW): single-party doc, attach via all_defendants if
            # exactly one case has that party. This catches things like
            # "1099 C amers.pdf" → 1 party (example_lender) → exactly 1 example_lender
            # case → auto-merge.
            if not candidates and len(doc.parties) == 1:
                only_party = doc.parties[0]
                cases_with_party = [
                    key for key, case in self.cases.items()
                    if only_party in case.all_defendants
                ]
                if len(cases_with_party) == 1:
                    candidates.add(cases_with_party[0])

            # Tier 4 (NEW): for "binding precedent" / "authority pack" research
            # documents, don't require docket attachment — these are general
            # research and should be available to all cases via the authority
            # miner. Mark them with a special pseudo-case key.
            doc_filename_lower = Path(doc.path).name.lower()
            is_research_pack = any(marker in doc_filename_lower for marker in [
                "binding precedent", "authority pack", "binding authority",
                "precedent affidavit",
            ])
            if not candidates and is_research_pack:
                # Skip — let the authority_miner pick it up directly via
                # documents list. It'll get mined regardless of case attachment.
                continue

            if len(candidates) == 1:
                target_key = next(iter(candidates))
                target = self.cases[target_key]
                self._attach_doc(target, doc, is_smart_merged=True)
                target.notes.append(
                    f"Smart-merged docket-less: {Path(doc.path).name}"
                )
            else:
                self.unassigned.append(doc)

        # Update document counts
        for case in self.cases.values():
            case.document_count = (
                len(case.orders) + len(case.motions_to_dismiss)
                + len(case.oppositions) + len(case.exhibits)
                + len(case.court_notices) + len(case.filings)
                + len(case.my_drafts)
            )

        return self.cases

    def _pick_best_docket(self, dockets: List[str]) -> Tuple[str, str]:
        """Pick the highest-priority docket from a list."""
        if not dockets:
            return "", ""
        parsed = []
        for d in dockets:
            if ":" not in d:
                continue
            juris, num = d.split(":", 1)
            parsed.append((juris, num, JURIS_PRIORITY.get(juris, 99)))
        parsed.sort(key=lambda t: t[2])
        if not parsed:
            return "", ""
        return parsed[0][1], parsed[0][0]

    def _attach_docketed(self, doc: Document) -> None:
        """
        Attach a document with detected docket numbers to the right case.

        With registry: every detected docket is looked up in the registry.
        First match wins. If no detected docket matches the registry, the
        doc goes to unassigned_unregistered.

        Without registry (legacy): falls back to picking the highest-priority
        docket and creating a case key from it.
        """
        if self.registry is not None:
            # Registry-first identity: walk every detected docket and look it up.
            # First registry hit wins.
            canonical_id = None
            matched_docket = None
            for d in doc.dockets:
                # The detected dockets in document_scanner are formatted as
                # "juris:docket" (e.g., "fed:2:26-cv-00123", "phila_ccp:260100000").
                # The registry can match either the docket portion alone or the
                # full juris:docket string, since aliases include both forms.
                if ":" in d:
                    _, docket_part = d.split(":", 1)
                else:
                    docket_part = d
                hit = self.registry.lookup_canonical(docket_part)
                if not hit:
                    # Try the full juris:docket form as a fallback
                    hit = self.registry.lookup_canonical(d)
                if hit:
                    canonical_id = hit
                    matched_docket = d
                    break

            if canonical_id is None:
                # Document has dockets but none of them match the registry.
                # This is either a false-positive citation extraction OR a
                # case that needs to be added to the registry.
                self.unassigned_unregistered.append(doc)
                return

            # Get or create the case from the registry entry
            entry = self.registry.get_entry(canonical_id)
            if canonical_id not in self.cases:
                # Parse the canonical_id back into juris and case_id
                if ":" in canonical_id:
                    juris, case_id = canonical_id.split(":", 1)
                else:
                    juris, case_id = "fed", canonical_id

                # Title comes from registry caption
                title = entry.caption if entry else self._make_title(case_id, juris, "")

                # Pick primary party tag from registry parties (skip the filer's own tag)
                _mine = _filer_tags()
                primary_party = ""
                for p in entry.parties:
                    if p not in _mine:
                        primary_party = p
                        break

                self.cases[canonical_id] = ConsolidatedCase(
                    case_id=case_id,
                    jurisdiction=juris,
                    case_court=entry.case_court if entry else JURIS_TO_COURT.get(juris, "UNKNOWN"),
                    party_tag=primary_party,
                    title=title,
                )
                # Seed defendants from the registry, NOT from document body parsing
                self.cases[canonical_id].all_defendants = [
                    p for p in entry.parties if p not in _mine
                ]

            case = self.cases[canonical_id]

            # Track the source folder
            folder = str(Path(doc.path).parent)
            if folder not in case.source_folders:
                case.source_folders.append(folder)

            self._attach_doc(case, doc)
            return

        # Legacy path (no registry): pick best docket and build a case key from it
        case_id, juris = self._pick_best_docket(doc.dockets)
        if not case_id:
            self.unassigned.append(doc)
            return

        key = f"{juris}:{case_id}"
        if key not in self.cases:
            primary_party = doc.parties[0] if doc.parties else ""
            self.cases[key] = ConsolidatedCase(
                case_id=case_id,
                jurisdiction=juris,
                case_court=JURIS_TO_COURT.get(juris, "UNKNOWN"),
                party_tag=primary_party,
                title=self._make_title(case_id, juris, primary_party),
            )

        case = self.cases[key]

        # Merge defendants
        for d in doc.parties:
            if d not in case.all_defendants:
                case.all_defendants.append(d)

        # Upgrade party_tag if we didn't have one
        if not case.party_tag and doc.parties:
            case.party_tag = doc.parties[0]
            case.title = self._make_title(case_id, juris, case.party_tag)

        # Track the source folder
        folder = str(Path(doc.path).parent)
        if folder not in case.source_folders:
            case.source_folders.append(folder)

        self._attach_doc(case, doc)

    def _attach_doc(self, case: ConsolidatedCase, doc: Document,
                    is_smart_merged: bool = False) -> None:
        """Append a document to the correct bucket on the case."""
        bucket = doctype_bucket(doc)
        getattr(case, bucket).append(doc.path)

        if is_smart_merged:
            folder = str(Path(doc.path).parent)
            if folder not in case.source_folders:
                case.source_folders.append(folder)
            # Only merge defendants when NOT using registry — registry parties
            # are authoritative and should not be polluted by body-inferred ones
            if self.registry is None:
                for d in doc.parties:
                    if d not in case.all_defendants:
                        case.all_defendants.append(d)

    def _make_title(self, case_id: str, jurisdiction: str, party_tag: str) -> str:
        defendant = party_tag.replace("_", " ").title() if party_tag else "Unknown Defendant"
        plaintiff = _filer_surname() or "Plaintiff"
        return f"{plaintiff} v. {defendant} ({case_id})"


def assemble(
    documents: List[Document],
    output_dir: Optional[Path] = None,
) -> Tuple[Dict[str, ConsolidatedCase], List[Document], List[Document]]:
    """
    Assemble documents into cases and optionally write case_map.json.
    Returns (cases, unassigned, reference_docs).
    """
    assembler = CaseAssembler()
    cases = assembler.assemble(documents)

    if output_dir:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        case_map = {key: case.to_dict() for key, case in cases.items()}
        (output_dir / "case_map.json").write_text(
            json.dumps(case_map, indent=2), encoding="utf-8"
        )

        unassigned_data = [
            {
                "filename": Path(d.path).name,
                "path": d.path,
                "parties": d.parties,
                "doc_type": d.doc_type,
                "author": d.author,
                "is_filer": d.is_filer_case,
            }
            for d in assembler.unassigned
        ]
        (output_dir / "unassigned_documents.json").write_text(
            json.dumps(unassigned_data, indent=2), encoding="utf-8"
        )

        # NEW: write unassigned_unregistered (docs with dockets that aren't in registry)
        unregistered_data = [
            {
                "filename": Path(d.path).name,
                "path": d.path,
                "dockets": d.dockets,
                "parties": d.parties,
                "doc_type": d.doc_type,
                "author": d.author,
                "is_filer": d.is_filer_case,
            }
            for d in assembler.unassigned_unregistered
        ]
        (output_dir / "unassigned_unregistered.json").write_text(
            json.dumps(unregistered_data, indent=2), encoding="utf-8"
        )

        ref_data = [
            {
                "filename": Path(d.path).name,
                "path": d.path,
                "text_chars": d.text_chars,
            }
            for d in assembler.reference_docs
        ]
        (output_dir / "reference_documents.json").write_text(
            json.dumps(ref_data, indent=2), encoding="utf-8"
        )

        log.info(
            f"Assembled {len(cases)} cases, "
            f"{len(assembler.unassigned)} unassigned, "
            f"{len(assembler.reference_docs)} reference docs"
        )

    return cases, assembler.unassigned, assembler.reference_docs
