#!/usr/bin/env python3
"""
schema.py v0.2 — registry.yaml schema validator for Judicial Accountability Registry.

Bundle 2 changes:
  - Split 'federal_judge' into precise categories (federal_district_judge,
    federal_circuit_judge, federal_magistrate_judge, federal_bankruptcy_judge,
    federal_senior_judge, scotus_justice, court_of_federal_claims_judge,
    tax_court_judge). 'federal_judge' RETAINED as backward-compat union.
  - Added 'category' field to Forum (statutory | public_pressure | criminal_referral
    | recusal_in_case | impeachment | financial_disclosure).
  - Added 'limitation_note' to Forum for documenting "no forum exists" entries.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml not installed", file=sys.stderr)
    sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# Allowed-value vocabularies (extend, don't replace)
# ─────────────────────────────────────────────────────────────────────────────

JURISDICTIONS = {
    "federal",
    "pa_state", "nj_state", "de_state", "ny_state", "ca_state",
    "tribal", "us_territory",
}

SCOPE_TARGETS = {
    # === Federal Article III (life-tenured) ===
    "federal_district_judge",        # e.g. EDPA's the-judge
    "federal_circuit_judge",         # e.g. 3d Cir panel members
    "federal_senior_judge",          # 28 U.S.C. § 371
    "scotus_justice",                # NOTE: subject to impeachment only
    # === Federal Article I (statutory) ===
    "federal_magistrate_judge",      # 28 U.S.C. § 631
    "federal_bankruptcy_judge",      # 28 U.S.C. § 152
    "court_of_federal_claims_judge", # 28 U.S.C. § 171
    "tax_court_judge",               # 26 U.S.C. § 7443
    # === Backward-compat union (matches any Article III/I federal judge) ===
    "federal_judge",
    # === Federal executive/investigative ===
    "federal_prosecutor", "ausa", "doj_attorney", "federal_law_clerk",
    "us_marshal", "us_trustee", "irs_employee", "tax_court_personnel",
    "federal_court_clerk", "bankruptcy_trustee", "court_appointed_receiver",
    "fbi_agent", "dea_agent",
    # === State judicial ===
    "state_judge", "pa_state_judge", "magisterial_district_judge",
    "state_appellate_judge", "state_supreme_justice",
    # === State executive/quasi ===
    "state_prosecutor", "state_da", "state_ag_attorney",
    "state_court_clerk", "sheriff", "deputy_sheriff", "constable",
    "notary_public",
    # === Tax / information return filers ===
    "third_party_information_return_filer",  # for § 7434 / 1099-C abuse cases
    "accountant_cpa",
    "audit_firm_cpa",
    "credit_reporting_agency",
    "debt_collector",
    "title_agent",
    "title_underwriter",
    "force_placed_insurer",
    "insurance_producer",
    "insurance_company",
    "municipal_bond_issuer",
    "securitizer",
    "mortgage_servicer",
    "remic_trustee",
    # === Attorneys (bar) ===
    "attorney_federal_bar", "attorney_state_bar",
    # === Universal catchall ===
    "color_of_law_actor",
}

FILING_METHODS = {
    "written_complaint", "online_form", "email", "in_person",
    "certified_mail", "efile_pacer", "efile_state",
    "house_judiciary_intake",  # impeachment
}

REMEDY_TYPES = {
    "private_admonishment", "public_reprimand", "censure",
    "suspension", "removal_from_office",
    "certification_for_impeachment_referral",
    "referral_to_attorney_disciplinary_board",
    "request_voluntary_retirement",
    "disbarment", "license_suspension", "license_revocation",
    "fines", "restitution",
    "monetary_damages",
    "injunctive_relief", "mandamus_relief", "prohibition_relief",
    "vacatur_of_order", "case_reopening",
    "criminal_referral", "civil_referral",
    "compensatory_damages", "punitive_damages", "attorney_fees",
    "public_report", "media_exposure",  # for public_pressure category
    "impeachment_resolution",
}

WRIT_FORUMS = {
    "scotus", "3d_circuit", "fed_circuit", "dc_circuit",
    "pa_supreme", "pa_superior", "pa_commonwealth",
    "edpa", "mdpa", "wdpa",
    "phila_ccp",
}

CONFIDENCE_LEVELS = {"high", "med", "low", "unverified"}

# NEW in v0.2: forum categories
FORUM_CATEGORIES = {
    "statutory",              # AO-mandated, judicial council, bar, etc.
    "criminal_referral",      # 18 USC §§ 241/242 → DOJ
    "recusal_in_case",        # §§ 144/455 motions
    "impeachment",            # House Judiciary
    "financial_disclosure",   # Ethics in Government Act
    "public_pressure",        # NGOs like Fix the Court
    "limitation_note",
    "transparency",        # "no forum exists" documentation entry
}


# ─────────────────────────────────────────────────────────────────────────────
# Dataclasses
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Forum:
    id: str
    jurisdiction: str
    scope: List[str]
    statutory_basis: str
    category: str = "statutory"   # NEW in v0.2
    geography: List[str] = field(default_factory=list)
    governing_rules: Optional[str] = None
    filing: Dict[str, Any] = field(default_factory=dict)
    timing: Dict[str, Any] = field(default_factory=dict)
    remedies_available: List[str] = field(default_factory=list)
    remedies_NOT_available: List[str] = field(default_factory=list)
    escalation: Dict[str, Any] = field(default_factory=dict)
    elements_to_plead: List[str] = field(default_factory=list)
    common_pitfalls: List[str] = field(default_factory=list)
    limitation_note: Optional[str] = None   # NEW: for "no forum exists" entries
    notes: Optional[str] = None
    sources: List[str] = field(default_factory=list)
    confidence: str = "unverified"
    last_verified: Optional[str] = None


@dataclass
class Bond:
    id: str
    jurisdiction: str
    bonded_role: str
    statutory_basis: str
    bond_status: str = "active"   # NEW: active | repealed | obsolete
    repealed_by: Optional[str] = None   # NEW: e.g. "Pub. L. 92-310 (1972)"
    alternative_recovery: List[str] = field(default_factory=list)  # NEW
    bond_amount_min: Optional[str] = None
    bond_amount_typical: Optional[str] = None
    surety_lookup: Optional[str] = None
    claim_procedure: List[str] = field(default_factory=list)
    elements_to_plead: List[str] = field(default_factory=list)
    statute_of_limitations: Optional[str] = None
    forum_for_suit: Optional[str] = None
    notes: Optional[str] = None
    sources: List[str] = field(default_factory=list)
    confidence: str = "unverified"
    last_verified: Optional[str] = None


@dataclass
class Writ:
    id: str
    forum: str
    name: str
    statutory_basis: str
    purpose: str
    deadline_days: Optional[int] = None
    deadline_trigger: Optional[str] = None
    deadline_jurisdictional: bool = False   # NEW: true if statutory/mandatory
    page_limit: Optional[str] = None
    word_limit: Optional[int] = None
    fee: Optional[str] = None
    required_sections: List[str] = field(default_factory=list)
    elements_to_plead: List[str] = field(default_factory=list)
    granting_factors: List[str] = field(default_factory=list)
    ifp_path: Optional[str] = None
    service_requirements: Optional[str] = None
    filing_address: Optional[str] = None   # NEW
    copies_required: Optional[str] = None  # NEW: "40 copies" or "original + 10"
    common_pitfalls: List[str] = field(default_factory=list)
    service_steps: List[str] = field(default_factory=list)
    notes: Optional[str] = None
    sources: List[str] = field(default_factory=list)
    confidence: str = "unverified"
    last_verified: Optional[str] = None


@dataclass
class Registry:
    version: str
    last_updated: str
    forums: List[Forum]
    bonds: List[Bond]
    writs: List[Writ]


# ─────────────────────────────────────────────────────────────────────────────
# Validators
# ─────────────────────────────────────────────────────────────────────────────

class ValidationError(Exception):
    pass


def _check_id_unique(items: List[Any], kind: str) -> None:
    seen = {}
    for item in items:
        if item.id in seen:
            raise ValidationError(
                f"duplicate {kind} id '{item.id}'")
        seen[item.id] = item


def _check_enum(value: str, allowed: set, field_name: str, entry_id: str) -> None:
    if value not in allowed:
        raise ValidationError(
            f"{entry_id}: invalid {field_name}='{value}'. "
            f"Allowed (sample): {sorted(allowed)[:5]}")


def _check_list_enum(values: List[str], allowed: set, field_name: str,
                     entry_id: str) -> None:
    for v in values:
        _check_enum(v, allowed, field_name, entry_id)


def validate_forum(f: Forum) -> List[str]:
    warnings = []
    _check_enum(f.jurisdiction, JURISDICTIONS, "jurisdiction", f.id)
    _check_enum(f.category, FORUM_CATEGORIES, "category", f.id)
    _check_list_enum(f.scope, SCOPE_TARGETS, "scope", f.id)
    _check_list_enum(f.remedies_available, REMEDY_TYPES,
                     "remedies_available", f.id)
    _check_list_enum(f.remedies_NOT_available, REMEDY_TYPES,
                     "remedies_NOT_available", f.id)
    _check_enum(f.confidence, CONFIDENCE_LEVELS, "confidence", f.id)
    if not f.statutory_basis and f.category != "public_pressure":
        warnings.append(f"{f.id}: empty statutory_basis (non-pressure forum)")
    if not f.sources and f.confidence in {"high", "med"}:
        warnings.append(f"{f.id}: confidence='{f.confidence}' but no sources cited")
    if f.category == "limitation_note" and not f.limitation_note:
        warnings.append(f"{f.id}: category='limitation_note' but limitation_note empty")
    return warnings


def validate_bond(b: Bond) -> List[str]:
    warnings = []
    _check_enum(b.jurisdiction, JURISDICTIONS, "jurisdiction", b.id)
    _check_enum(b.confidence, CONFIDENCE_LEVELS, "confidence", b.id)
    if b.bond_status not in {"active", "repealed", "obsolete"}:
        raise ValidationError(f"{b.id}: invalid bond_status='{b.bond_status}'")
    if b.bond_status == "repealed" and not b.repealed_by:
        warnings.append(f"{b.id}: bond_status='repealed' but no repealed_by citation")
    if b.bond_status == "active" and not b.claim_procedure:
        warnings.append(f"{b.id}: active bond but no claim_procedure")
    return warnings


def validate_writ(w: Writ) -> List[str]:
    warnings = []
    _check_enum(w.forum, WRIT_FORUMS, "forum", w.id)
    _check_enum(w.confidence, CONFIDENCE_LEVELS, "confidence", w.id)
    if not w.required_sections:
        warnings.append(f"{w.id}: no required_sections listed")
    if w.deadline_days is None and "no_deadline" not in (w.notes or "").lower():
        warnings.append(f"{w.id}: no deadline_days and notes don't note 'no_deadline'")
    return warnings


def load_and_validate(yaml_path: Path, strict: bool = False) -> Registry:
    raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValidationError(f"{yaml_path}: top-level must be a dict")

    forums = [Forum(**f) for f in (raw.get("forums") or [])]
    bonds = [Bond(**b) for b in (raw.get("bonds") or [])]
    writs = [Writ(**w) for w in (raw.get("writs") or [])]

    _check_id_unique(forums, "forum")
    _check_id_unique(bonds, "bond")
    _check_id_unique(writs, "writ")

    all_warnings = []
    for f in forums:
        all_warnings.extend(validate_forum(f))
    for b in bonds:
        all_warnings.extend(validate_bond(b))
    for w in writs:
        all_warnings.extend(validate_writ(w))

    if all_warnings:
        print(f"⚠ {len(all_warnings)} validation warning(s):", file=sys.stderr)
        for w in all_warnings:
            print(f"  · {w}", file=sys.stderr)
        if strict:
            raise ValidationError(
                f"{len(all_warnings)} warnings (strict mode raises)")

    return Registry(
        version=raw.get("version", "0.0"),
        last_updated=raw.get("last_updated", "unknown"),
        forums=forums, bonds=bonds, writs=writs,
    )


def stats(reg: Registry) -> Dict[str, Any]:
    return {
        "forums_total": len(reg.forums),
        "forums_by_jurisdiction": {
            j: sum(1 for f in reg.forums if f.jurisdiction == j)
            for j in sorted({f.jurisdiction for f in reg.forums})
        },
        "forums_by_category": {
            c: sum(1 for f in reg.forums if f.category == c)
            for c in sorted({f.category for f in reg.forums})
        },
        "bonds_total": len(reg.bonds),
        "bonds_by_status": {
            s: sum(1 for b in reg.bonds if b.bond_status == s)
            for s in sorted({b.bond_status for b in reg.bonds})
        },
        "writs_total": len(reg.writs),
        "confidence_breakdown": {
            level: sum(1 for x in (reg.forums + reg.bonds + reg.writs)
                       if x.confidence == level)
            for level in CONFIDENCE_LEVELS
        },
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: schema.py <registry.yaml> [--strict]")
        sys.exit(1)
    p = Path(sys.argv[1])
    strict = "--strict" in sys.argv
    reg = load_and_validate(p, strict=strict)
    import json
    print(json.dumps(stats(reg), indent=2))
