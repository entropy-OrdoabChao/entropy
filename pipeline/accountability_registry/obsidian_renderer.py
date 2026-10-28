#!/usr/bin/env python3
"""
obsidian_renderer.py — regenerates obsidian_sync/02_accountability/ from registry.yaml.

Output layout:
  obsidian_sync/02_accountability/
    _index.md                      — top-level hub
    federal/
      _federal_index.md
      forums/
        <forum_id>.md              — one per federal forum
      bonds/
        <bond_id>.md
      writs/
        <writ_id>.md
    pa/
      _pa_index.md
      forums/...
      bonds/...
      writs/...
    by_category/
      _by_category_index.md
      <category>.md                — one hub per category (statutory, public_pressure, etc.)

All notes use wikilinks ([[note_id]]) for cross-references.
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

_THIS = Path(__file__).resolve()
if str(_THIS.parent) not in sys.path:
    sys.path.insert(0, str(_THIS.parent))

from schema import Registry, Forum, Bond, Writ, load_and_validate  # noqa: E402

DEFAULT_REGISTRY = (_eroot() / "pipeline/accountability_registry/registry.yaml")
DEFAULT_OUTPUT = (_eroot() / "obsidian_sync/02_accountability")


def _yaml_frontmatter(d: Dict[str, Any]) -> str:
    """Render a dict as YAML frontmatter (simple types only)."""
    lines = ["---"]
    for k, v in d.items():
        if isinstance(v, list):
            lines.append(f"{k}:")
            for item in v:
                lines.append(f"  - {item}")
        elif isinstance(v, dict):
            lines.append(f"{k}:")
            for kk, vv in v.items():
                lines.append(f"  {kk}: {vv}")
        elif v is None:
            lines.append(f"{k}: null")
        elif isinstance(v, bool):
            lines.append(f"{k}: {str(v).lower()}")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines)


# BEEZLE_FN_UNUSED 2026-06-13: _slugify (no static refs in pipeline/)
def _slugify(s: str) -> str:
    """Make a filesystem-safe slug."""
    return "".join(c if c.isalnum() or c in "_-" else "_" for c in s.lower())


def _jurisdiction_dir(jx: str) -> str:
    """Map jurisdiction to folder name."""
    if jx == "federal":
        return "federal"
    if jx.endswith("_state"):
        return jx[:-6]  # pa_state -> pa
    return jx


def render_forum(f: Forum, output_root: Path) -> Path:
    """Render one Forum to a .md file."""
    jdir = _jurisdiction_dir(f.jurisdiction)
    out_dir = output_root / jdir / "forums"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{f.id}.md"

    fm = {
        "type": "accountability_forum",
        "forum_id": f.id,
        "jurisdiction": f.jurisdiction,
        "category": f.category,
        "scope": f.scope,
        "geography": f.geography,
        "confidence": f.confidence,
        "last_verified": f.last_verified,
        "statutory_basis": f.statutory_basis,
    }

    body = [_yaml_frontmatter(fm), ""]
    body.append(f"# {f.id.replace('_', ' ').title()}")
    body.append("")

    if f.category == "limitation_note":
        body.append("> [!warning] NO FORUM EXISTS")
        body.append(">")
        for line in (f.limitation_note or "").strip().split("\n"):
            body.append(f"> {line}")
        body.append("")

    body.append("## Statutory Basis")
    body.append("")
    body.append(f.statutory_basis)
    body.append("")

    if f.governing_rules:
        body.append("## Governing Rules")
        body.append("")
        body.append(f.governing_rules)
        body.append("")

    if f.filing:
        body.append("## Filing")
        body.append("")
        for k, v in f.filing.items():
            if isinstance(v, list):
                body.append(f"- **{k}**:")
                for item in v:
                    body.append(f"    - {item}")
            elif isinstance(v, str) and "\n" in v:
                body.append(f"- **{k}**:")
                body.append("")
                body.append("```")
                body.append(v.strip())
                body.append("```")
            else:
                body.append(f"- **{k}**: {v}")
        body.append("")

    if f.timing:
        body.append("## Timing")
        body.append("")
        for k, v in f.timing.items():
            body.append(f"- **{k}**: {v}")
        body.append("")

    if f.remedies_available:
        body.append("## Remedies Available")
        body.append("")
        for r in f.remedies_available:
            body.append(f"- {r}")
        body.append("")

    if f.remedies_NOT_available:
        body.append("## Remedies NOT Available")
        body.append("")
        for r in f.remedies_NOT_available:
            body.append(f"- {r}")
        body.append("")

    if f.escalation:
        body.append("## Escalation")
        body.append("")
        for k, v in f.escalation.items():
            if k == "next_forum":
                # Wikilink to the next forum's note
                body.append(f"- **next forum**: [[{v}]]")
            else:
                body.append(f"- **{k}**: {v}")
        body.append("")

    if f.elements_to_plead:
        body.append("## Elements to Plead")
        body.append("")
        for e in f.elements_to_plead:
            body.append(f"- {e}")
        body.append("")

    if f.common_pitfalls:
        body.append("## Common Pitfalls")
        body.append("")
        for p in f.common_pitfalls:
            body.append(f"- {p}")
        body.append("")

    if f.notes:
        body.append("## Notes")
        body.append("")
        body.append(f.notes.strip())
        body.append("")

    if f.sources:
        body.append("## Sources")
        body.append("")
        for s in f.sources:
            body.append(f"- <{s}>")
        body.append("")

    out_path.write_text("\n".join(body), encoding="utf-8")
    return out_path


def render_bond(b: Bond, output_root: Path) -> Path:
    jdir = _jurisdiction_dir(b.jurisdiction)
    out_dir = output_root / jdir / "bonds"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{b.id}.md"

    fm = {
        "type": "accountability_bond",
        "bond_id": b.id,
        "jurisdiction": b.jurisdiction,
        "bonded_role": b.bonded_role,
        "bond_status": b.bond_status,
        "confidence": b.confidence,
        "last_verified": b.last_verified,
    }

    body = [_yaml_frontmatter(fm), ""]
    body.append(f"# {b.id.replace('_', ' ').title()}")
    body.append("")

    if b.bond_status == "repealed":
        body.append("> [!danger] BOND STATUS: REPEALED")
        body.append(f"> This bond was repealed. Repeal authority: {b.repealed_by}")
        body.append(">")
        body.append("> **Use alternative recovery paths listed below.**")
        body.append("")

    body.append("## Statutory Basis")
    body.append("")
    body.append(b.statutory_basis)
    body.append("")

    if b.bond_status == "active":
        if b.bond_amount_min:
            body.append(f"**Minimum bond amount**: {b.bond_amount_min}")
        if b.bond_amount_typical:
            body.append(f"**Typical amount**: {b.bond_amount_typical}")
        if b.surety_lookup:
            body.append(f"**Where to find surety**: {b.surety_lookup}")
        body.append("")

    if b.alternative_recovery:
        body.append("## Alternative Recovery Paths")
        body.append("")
        for a in b.alternative_recovery:
            body.append(f"- {a}")
        body.append("")

    if b.claim_procedure:
        body.append("## Claim Procedure")
        body.append("")
        for i, step in enumerate(b.claim_procedure, 1):
            body.append(f"{i}. {step}")
        body.append("")

    if b.elements_to_plead:
        body.append("## Elements to Plead")
        body.append("")
        for e in b.elements_to_plead:
            body.append(f"- {e}")
        body.append("")

    if b.statute_of_limitations:
        body.append(f"**Statute of limitations**: {b.statute_of_limitations}")
        body.append("")

    if b.forum_for_suit:
        body.append(f"**Forum for suit**: {b.forum_for_suit}")
        body.append("")

    if b.notes:
        body.append("## Notes")
        body.append("")
        body.append(b.notes.strip())
        body.append("")

    if b.sources:
        body.append("## Sources")
        body.append("")
        for s in b.sources:
            body.append(f"- <{s}>")
        body.append("")

    out_path.write_text("\n".join(body), encoding="utf-8")
    return out_path


def render_writ(w: Writ, output_root: Path) -> Path:
    # Writs are jurisdiction-flexible; use forum to pick dir
    if w.forum == "scotus" or w.forum.startswith("3d_") or w.forum.startswith("fed_") \
            or w.forum.startswith("edpa") or w.forum.startswith("mdpa") \
            or w.forum.startswith("wdpa") or w.forum == "dc_circuit":
        jdir = "federal"
    else:
        jdir = "pa"
    out_dir = output_root / jdir / "writs"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{w.id}.md"

    fm = {
        "type": "accountability_writ",
        "writ_id": w.id,
        "forum": w.forum,
        "name": w.name,
        "deadline_days": w.deadline_days,
        "deadline_jurisdictional": w.deadline_jurisdictional,
        "word_limit": w.word_limit,
        "confidence": w.confidence,
        "last_verified": w.last_verified,
    }

    body = [_yaml_frontmatter(fm), ""]
    body.append(f"# {w.name}")
    body.append("")
    body.append(f"**Forum**: {w.forum}  ")
    body.append(f"**Purpose**: {w.purpose}")
    body.append("")

    body.append("## Statutory Basis")
    body.append("")
    body.append(w.statutory_basis)
    body.append("")

    body.append("## Deadline")
    body.append("")
    if w.deadline_days:
        body.append(f"- **{w.deadline_days} days** from {w.deadline_trigger}")
    else:
        body.append(f"- No statutory deadline; {w.deadline_trigger or 'file promptly'}")
    if w.deadline_jurisdictional:
        body.append("- ⚠ JURISDICTIONAL — clerk refuses out-of-time filings; cannot be waived")
    body.append("")

    if w.fee:
        body.append(f"**Fee**: {w.fee}")
    if w.word_limit:
        body.append(f"**Word limit**: {w.word_limit:,}")
    if w.page_limit:
        body.append(f"**Page limit**: {w.page_limit}")
    if w.copies_required:
        body.append(f"**Copies**: {w.copies_required}")
    body.append("")

    if w.filing_address:
        body.append("## Filing Address")
        body.append("")
        body.append("```")
        body.append(w.filing_address.strip())
        body.append("```")
        body.append("")

    if w.ifp_path:
        body.append(f"**IFP path**: {w.ifp_path}")
        body.append("")

    if w.required_sections:
        body.append("## Required Sections")
        body.append("")
        for s in w.required_sections:
            body.append(f"- {s}")
        body.append("")

    if w.granting_factors:
        body.append("## Granting Factors")
        body.append("")
        for g in w.granting_factors:
            body.append(f"- {g}")
        body.append("")

    if w.elements_to_plead:
        body.append("## Elements to Plead")
        body.append("")
        for e in w.elements_to_plead:
            body.append(f"- {e}")
        body.append("")

    if w.service_requirements:
        body.append(f"**Service**: {w.service_requirements}")
        body.append("")

    if w.notes:
        body.append("## Notes")
        body.append("")
        body.append(w.notes.strip())
        body.append("")

    if w.sources:
        body.append("## Sources")
        body.append("")
        for s in w.sources:
            body.append(f"- <{s}>")
        body.append("")

    out_path.write_text("\n".join(body), encoding="utf-8")
    return out_path


def render_jurisdiction_index(reg: Registry, jurisdiction: str,
                              output_root: Path) -> Path:
    """One index per jurisdiction (federal/, pa/)."""
    jdir = _jurisdiction_dir(jurisdiction)
    out_path = output_root / jdir / f"_{jdir}_index.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    forums = [f for f in reg.forums if f.jurisdiction == jurisdiction]
    bonds = [b for b in reg.bonds if b.jurisdiction == jurisdiction]

    body = [_yaml_frontmatter({
        "type": "accountability_index",
        "jurisdiction": jurisdiction,
        "forum_count": len(forums),
        "bond_count": len(bonds),
    }), ""]
    body.append(f"# {jdir.upper()} — Accountability Index")
    body.append("")

    # Group forums by category
    by_cat: Dict[str, List[Forum]] = {}
    for f in forums:
        by_cat.setdefault(f.category, []).append(f)

    body.append("## Forums by Category")
    body.append("")
    for cat in sorted(by_cat.keys()):
        body.append(f"### {cat.replace('_', ' ').title()}")
        body.append("")
        for f in sorted(by_cat[cat], key=lambda x: x.id):
            body.append(f"- [[{f.id}]] — {f.statutory_basis[:80]}")
        body.append("")

    if bonds:
        body.append("## Bonds")
        body.append("")
        active = [b for b in bonds if b.bond_status == "active"]
        repealed = [b for b in bonds if b.bond_status == "repealed"]
        if active:
            body.append("### Active")
            body.append("")
            for b in sorted(active, key=lambda x: x.id):
                body.append(f"- [[{b.id}]] — {b.bonded_role}")
            body.append("")
        if repealed:
            body.append("### Repealed (alternative recovery only)")
            body.append("")
            for b in sorted(repealed, key=lambda x: x.id):
                body.append(f"- [[{b.id}]] — {b.bonded_role} (repealed)")
            body.append("")

    out_path.write_text("\n".join(body), encoding="utf-8")
    return out_path


def render_category_index(reg: Registry, output_root: Path) -> Path:
    """Cross-cutting index by category."""
    out_dir = output_root / "by_category"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "_by_category_index.md"

    by_cat: Dict[str, List[Forum]] = {}
    for f in reg.forums:
        by_cat.setdefault(f.category, []).append(f)

    body = [_yaml_frontmatter({
        "type": "accountability_category_index",
        "category_count": len(by_cat),
    }), ""]
    body.append("# Accountability Forums by Category")
    body.append("")
    for cat in sorted(by_cat.keys()):
        body.append(f"## {cat.replace('_', ' ').title()}")
        body.append("")
        for f in sorted(by_cat[cat], key=lambda x: x.id):
            body.append(f"- [[{f.id}]] ({f.jurisdiction})")
        body.append("")

    out_path.write_text("\n".join(body), encoding="utf-8")
    return out_path


def render_root_index(reg: Registry, output_root: Path) -> Path:
    """Top-level _index.md."""
    out_path = output_root / "_index.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    body = [_yaml_frontmatter({
        "type": "accountability_root_index",
        "version": reg.version,
        "last_updated": reg.last_updated,
        "forum_count": len(reg.forums),
        "bond_count": len(reg.bonds),
        "writ_count": len(reg.writs),
    }), ""]
    body.append("# Judicial Accountability Registry")
    body.append("")
    body.append(f"**Version**: {reg.version}  ")
    body.append(f"**Last updated**: {reg.last_updated}  ")
    body.append(f"**Forums**: {len(reg.forums)}  ")
    body.append(f"**Bonds**: {len(reg.bonds)} ({sum(1 for b in reg.bonds if b.bond_status == 'active')} active)  ")
    body.append(f"**Writs**: {len(reg.writs)}")
    body.append("")
    body.append("## Browse")
    body.append("")
    body.append("- [[_federal_index|Federal]]")
    body.append("- [[_pa_index|Pennsylvania]]")
    body.append("- [[_by_category_index|By Category]]")
    body.append("")
    body.append("## Quick Reference — Federal Judge Misconduct")
    body.append("")
    body.append("For misconduct by an EDPA/Article III/Magistrate/Bankruptcy judge in the 3d Cir:")
    body.append("")
    body.append("- Administrative: [[third_circuit_judicial_council]]")
    body.append("- Appellate review: [[judicial_conference_committee_on_judicial_conduct]]")
    body.append("- In-case recusal: [[federal_recusal_motion_in_case]]")
    body.append("- Escalation if recusal denied: [[writ_of_mandamus_3d_cir]]")
    body.append("- Criminal (under color of law): [[doj_civil_rights_division_criminal_section]]")
    body.append("- Criminal (bribery/corruption): [[doj_public_integrity_section]] or [[fbi_public_corruption_unit]]")
    body.append("- Impeachment: [[house_judiciary_committee_impeachment]]")
    body.append("- Public pressure: [[fix_the_court]]")
    body.append("- SCOTUS-specific limitation: [[scotus_no_forum_limitation]]")
    body.append("- Appellate cert: [[writ_of_certiorari_scotus]]")
    body.append("")

    out_path.write_text("\n".join(body), encoding="utf-8")
    return out_path


def render_all(registry_path: Path = DEFAULT_REGISTRY,
               output_root: Path = DEFAULT_OUTPUT) -> Dict[str, int]:
    """Render the entire vault. Returns counts."""
    reg = load_and_validate(registry_path)
    output_root.mkdir(parents=True, exist_ok=True)

    counts = {"forums": 0, "bonds": 0, "writs": 0, "indexes": 0}

    for f in reg.forums:
        render_forum(f, output_root)
        counts["forums"] += 1

    for b in reg.bonds:
        render_bond(b, output_root)
        counts["bonds"] += 1

    for w in reg.writs:
        render_writ(w, output_root)
        counts["writs"] += 1

    # Per-jurisdiction indexes
    for jx in sorted({f.jurisdiction for f in reg.forums}):
        render_jurisdiction_index(reg, jx, output_root)
        counts["indexes"] += 1

    # Cross-cutting indexes
    render_category_index(reg, output_root)
    counts["indexes"] += 1
    render_root_index(reg, output_root)
    counts["indexes"] += 1

    return counts


if __name__ == "__main__":
    registry = DEFAULT_REGISTRY
    output = DEFAULT_OUTPUT
    if len(sys.argv) > 1:
        registry = Path(sys.argv[1])
    if len(sys.argv) > 2:
        output = Path(sys.argv[2])
    counts = render_all(registry, output)
    print(f"Rendered:")
    for k, v in counts.items():
        print(f"  {k}: {v}")
    print(f"Output: {output}")
