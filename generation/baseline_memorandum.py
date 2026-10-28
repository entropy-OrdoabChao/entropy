"""
BLOCK_9B_HERMES_SKILLS_PORT_2_OF_2
Baseline Memorandum — Standing Legal Notice generator. Ported from hermes baseline-memorandum skill.

Generates persistent procedural enforcement document that travels with every case.
NOT argument — NOTICE. Establishes:
  1. What law controls
  2. What procedures are mandatory
  3. What definitions apply
  4. What happens when rules are violated

Output:
  - Markdown for Obsidian (obsidian_sync/baseline_memos/<case_id>.md)
  - .docx for filing (via generation.document_generator.build_docx)

Pre-labels every rule with violation_id for scorecard / appellate-violation-tracker linkage.
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
from dataclasses import dataclass, field, asdict
from pathlib import Path
from datetime import datetime
from typing import Optional

from core.logger import get_logger

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

log = get_logger("baseline_memorandum")

OBSIDIAN_MEMO_DIR = (_BEEZLE_ROOT / "obsidian_sync" / "baseline_memos")


@dataclass
class ControllingAuthority:
    name: str          # e.g. "U.S. Constitution, Art. III, § 2"
    citation: str      # e.g. "U.S. Const. art. III, § 2"
    proposition: str   # what it controls
    binding_on: str = "all parties and this Court"


@dataclass
class MandatoryDuty:
    duty: str             # e.g. "All pleadings must be verified"
    authority: str        # e.g. "Pa.R.C.P. 1024"
    consequence: str      # e.g. "Unverified pleading is a nullity"
    violation_id: str = ""  # auto-assigned: V001, V002, ...


@dataclass
class DefinitionLock:
    term: str
    definition: str       # exact text
    source: str           # statute/case citation
    scope: str = "this proceeding"


@dataclass
class CourtRule:
    rule: str             # e.g. "FRCP 7(b)(1) — motion must state relief sought"
    citation: str
    application: str      # how it applies in this case


@dataclass
class BaselineMemo:
    case_id: str
    caption: str = ""
    filed_date: str = ""
    controlling_authorities: list[ControllingAuthority] = field(default_factory=list)
    mandatory_duties: list[MandatoryDuty] = field(default_factory=list)
    definitions: list[DefinitionLock] = field(default_factory=list)
    court_rules: list[CourtRule] = field(default_factory=list)


def _assign_violation_ids(duties: list[MandatoryDuty]) -> None:
    """Assigns sequential violation IDs (V001, V002, ...) to every duty."""
    for i, d in enumerate(duties, start=1):
        if not d.violation_id:
            d.violation_id = f"V{i:03d}"


def build_memo_markdown(memo: BaselineMemo) -> str:
    """Render the BaselineMemo as filing-ready markdown."""
    _assign_violation_ids(memo.mandatory_duties)
    if not memo.filed_date:
        memo.filed_date = datetime.utcnow().strftime("%B %d, %Y")

    lines: list[str] = []
    lines.append(f"# MEMORANDUM OF LAW — STANDING LEGAL NOTICE")
    lines.append("")
    lines.append(f"**Case:** {memo.caption or memo.case_id}")
    lines.append(f"**Filed:** {memo.filed_date}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # ── Section 1 ──
    lines.append("## SECTION 1 — Statement of Standing & Notice")
    lines.append("")
    lines.append("This Memorandum of Law is filed to establish controlling authority,")
    lines.append("procedural requirements, and binding obligations applicable to all")
    lines.append("parties and this Court.")
    lines.append("")
    lines.append("**All parties are hereby on notice that:**")
    lines.append("")
    lines.append("1. The controlling authority identified herein governs this proceeding.")
    lines.append("2. Mandatory procedural requirements must be satisfied.")
    lines.append("3. Failure to comply will be documented and preserved for appellate review.")
    lines.append("")

    # ── Section 2: Controlling Authority ──
    lines.append("## SECTION 2 — Controlling Authority")
    lines.append("")
    if memo.controlling_authorities:
        for a in memo.controlling_authorities:
            lines.append(f"### {a.name}")
            lines.append(f"- **Citation:** {a.citation}")
            lines.append(f"- **Proposition:** {a.proposition}")
            lines.append(f"- **Binding on:** {a.binding_on}")
            lines.append("")
    else:
        lines.append("*(No controlling authorities declared.)*")
        lines.append("")

    # ── Section 3: Mandatory Duties ──
    lines.append("## SECTION 3 — Mandatory Procedural Duties")
    lines.append("")
    lines.append("Each duty below is binding. Failure to comply constitutes a recorded")
    lines.append("violation, pre-labeled for scorecard scoring and appellate tracking.")
    lines.append("")
    if memo.mandatory_duties:
        lines.append("| Violation ID | Mandatory Duty | Authority | Consequence on Failure |")
        lines.append("|--------------|----------------|-----------|------------------------|")
        for d in memo.mandatory_duties:
            duty = d.duty.replace("|", "/")
            cons = d.consequence.replace("|", "/")
            lines.append(f"| {d.violation_id} | {duty} | {d.authority} | {cons} |")
        lines.append("")
    else:
        lines.append("*(No mandatory duties declared.)*")
        lines.append("")

    # ── Section 4: Definition Locks ──
    lines.append("## SECTION 4 — Definition Control")
    lines.append("")
    lines.append("The following terms have locked definitions for this proceeding.")
    lines.append("Any party seeking to use a different meaning must rebut with controlling")
    lines.append("authority before the term may be construed otherwise.")
    lines.append("")
    if memo.definitions:
        for d in memo.definitions:
            lines.append(f"### {d.term}")
            lines.append(f"- **Definition:** {d.definition}")
            lines.append(f"- **Source:** {d.source}")
            lines.append(f"- **Scope:** {d.scope}")
            lines.append("")
    else:
        lines.append("*(No definitions locked.)*")
        lines.append("")

    # ── Section 5: Court Rules ──
    lines.append("## SECTION 5 — Applicable Court Rules")
    lines.append("")
    if memo.court_rules:
        for r in memo.court_rules:
            lines.append(f"### {r.rule}")
            lines.append(f"- **Citation:** {r.citation}")
            lines.append(f"- **Application:** {r.application}")
            lines.append("")
    else:
        lines.append("*(No court rules declared.)*")
        lines.append("")

    # ── Section 6: Enforcement ──
    lines.append("## SECTION 6 — Enforcement & Preservation")
    lines.append("")
    lines.append("This Memorandum is a record of NOTICE. Each violation listed below will be:")
    lines.append("")
    lines.append("- **Logged** to the persistent violation tracker (per appellate-violation-tracker procedure)")
    lines.append("- **Scored** in the legal scorecard (per legal-scorecard procedure)")
    lines.append("- **Preserved** for appellate review and, where applicable, regulatory complaint")
    lines.append("")
    lines.append("Total mandatory duties on notice: " + str(len(memo.mandatory_duties)))
    lines.append("Violation ID range: " + (
        f"{memo.mandatory_duties[0].violation_id}–{memo.mandatory_duties[-1].violation_id}"
        if memo.mandatory_duties else "(none)"
    ))
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(f"*Generated: {datetime.utcnow().isoformat()}Z*")
    lines.append("")
    return "\n".join(lines)


def write_memo_markdown(memo: BaselineMemo) -> Path:
    """Write the markdown memo to obsidian_sync/baseline_memos/<safe_id>.md."""
    OBSIDIAN_MEMO_DIR.mkdir(parents=True, exist_ok=True)
    safe = memo.case_id.replace(":", "_").replace("/", "_")
    target = OBSIDIAN_MEMO_DIR / f"{safe}.md"
    target.write_text(build_memo_markdown(memo), encoding="utf-8")
    log.info(f"wrote markdown memo: {target}")
    return target


def write_memo_docx(memo: BaselineMemo) -> Path:
    """Build the memo as .docx via Beezle's document_generator (versioned)."""
    from generation.document_generator import build_docx

    _assign_violation_ids(memo.mandatory_duties)
    if not memo.filed_date:
        memo.filed_date = datetime.utcnow().strftime("%B %d, %Y")

    sections = [
        {"heading": "SECTION 1 — Statement of Standing & Notice",
         "body": ("This Memorandum of Law establishes controlling authority, procedural "
                  "requirements, and binding obligations applicable to all parties and "
                  "this Court.\n\n"
                  "All parties are on notice that:\n"
                  "1. The controlling authority herein governs this proceeding.\n"
                  "2. Mandatory procedural requirements must be satisfied.\n"
                  "3. Failure to comply will be documented and preserved for appeal."),
         "authority": []},

        {"heading": "SECTION 2 — Controlling Authority",
         "body": "\n\n".join(
             f"{a.name}\n  Citation: {a.citation}\n  Proposition: {a.proposition}\n  Binding on: {a.binding_on}"
             for a in memo.controlling_authorities
         ) or "(None declared.)",
         "authority": [a.citation for a in memo.controlling_authorities]},

        {"heading": "SECTION 3 — Mandatory Procedural Duties",
         "body": "\n".join(
             f"[{d.violation_id}] {d.duty} — Authority: {d.authority} — Consequence: {d.consequence}"
             for d in memo.mandatory_duties
         ) or "(None declared.)",
         "authority": list({d.authority for d in memo.mandatory_duties})},

        {"heading": "SECTION 4 — Definition Control",
         "body": "\n\n".join(
             f"{d.term}\n  Definition: {d.definition}\n  Source: {d.source}\n  Scope: {d.scope}"
             for d in memo.definitions
         ) or "(No definitions locked.)",
         "authority": [d.source for d in memo.definitions]},

        {"heading": "SECTION 5 — Applicable Court Rules",
         "body": "\n\n".join(
             f"{r.rule}\n  Citation: {r.citation}\n  Application: {r.application}"
             for r in memo.court_rules
         ) or "(None declared.)",
         "authority": [r.citation for r in memo.court_rules]},

        {"heading": "SECTION 6 — Enforcement & Preservation",
         "body": (f"Total mandatory duties on notice: {len(memo.mandatory_duties)}\n"
                  f"Violation ID range: " + (
                      f"{memo.mandatory_duties[0].violation_id}-{memo.mandatory_duties[-1].violation_id}"
                      if memo.mandatory_duties else "(none)") + "\n\n"
                  "Each listed violation will be logged to the persistent tracker, "
                  "scored, and preserved for appellate review."),
         "authority": []},
    ]

    safe = memo.case_id.replace(":", "_").replace("/", "_")
    name = f"baseline_memo_{safe}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
    title = f"Memorandum of Law — Standing Legal Notice — {memo.caption or memo.case_id}"
    out = build_docx(sections, name=name, title=title)
    log.info(f"wrote docx memo: {out}")
    return out


def build_memo(
    case_id: str,
    caption: str = "",
    controlling_authorities: Optional[list[dict]] = None,
    mandatory_duties: Optional[list[dict]] = None,
    definitions: Optional[list[dict]] = None,
    court_rules: Optional[list[dict]] = None,
    write_markdown: bool = True,
    write_docx: bool = True,
) -> dict:
    """One-call builder. Accepts plain dicts (MCP-friendly), writes both formats."""
    memo = BaselineMemo(
        case_id=case_id,
        caption=caption,
        controlling_authorities=[ControllingAuthority(**a) for a in (controlling_authorities or [])],
        mandatory_duties=[MandatoryDuty(**d) for d in (mandatory_duties or [])],
        definitions=[DefinitionLock(**d) for d in (definitions or [])],
        court_rules=[CourtRule(**r) for r in (court_rules or [])],
    )
    out = {"case_id": case_id, "duties_count": len(memo.mandatory_duties)}
    if write_markdown:
        out["markdown_path"] = str(write_memo_markdown(memo))
    if write_docx:
        out["docx_path"] = str(write_memo_docx(memo))
    out["violation_ids"] = [d.violation_id for d in memo.mandatory_duties]
    return out