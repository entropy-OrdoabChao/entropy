"""
pipeline/scorecard_temporal_appendix.py — Filing Schedule appendix writer.

Standalone module that writes a Filing Schedule markdown file alongside
the existing chain_outputs/ directory. The protected scorecard renderer
(generation/scorecard_renderer_v2.py) is NOT modified — this is an
unprotected sibling output that human reviewers and downstream tools can
include as an appendix.

Public API
──────────
write_appendix(case_id, user_role="plaintiff", today=None) -> dict
    Writes obsidian_sync/cases/{slug}/chain_outputs/temporal_schedule_{date}.md
    Returns {"path": str, "deadlines_count": int, "expired": int,
             "imminent": int, "upcoming": int, "open": int,
             "anchors": {...}}.

build_appendix_markdown(case_id, user_role, today) -> str
    Returns the appendix markdown string without writing to disk.

This module is read-only with respect to yaml/case_map. The only side
effect is writing the appendix .md file to chain_outputs/.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger("scorecard_temporal_appendix")

ROOT = Path(__file__).resolve().parent.parent
CASES_DIR = ROOT / "obsidian_sync" / "cases"


# ═════════════════════════════════════════════════════════════════════════
# CASE SLUG MAPPING (mirrors temporal_schedule._slug_for_case)
# ═════════════════════════════════════════════════════════════════════════

def _slug_for_case(case_id: str) -> Optional[str]:
    parts = case_id.split(":")
    if not parts:
        return None
    last = parts[-1]
    if case_id.startswith("fed:"):
        return f"fed_{last}"
    if case_id.startswith("phila_ccp:"):
        return f"phila_ccp_{last}"
    return last


# ═════════════════════════════════════════════════════════════════════════
# MARKDOWN BUILDER
# ═════════════════════════════════════════════════════════════════════════

def build_appendix_markdown(case_id: str,
                             user_role: str = "plaintiff",
                             today: Optional[date] = None) -> Dict[str, Any]:
    """Build the appendix markdown + summary stats.

    Returns dict with:
      markdown:  str, the appendix content
      anchors:   dict of trigger dates extracted from yaml
      deadlines: list of computed deadline records
      summary:   {expired, imminent, upcoming, open, total}
    """
    from pipeline.temporal_schedule import (
        compute_deadlines,
        get_case_anchors,
        format_deadlines_block,
    )

    today = today or date.today()
    anchors = get_case_anchors(case_id)
    deadlines = compute_deadlines(case_id, today=today, user_role=user_role)

    # Build summary stats
    summary = {"expired": 0, "imminent": 0, "upcoming": 0, "open": 0,
               "total": len(deadlines)}
    for d in deadlines:
        status = d.get("status", "OPEN").lower()
        if status in summary:
            summary[status] += 1

    # Header section — mirror chain_*.md style with explicit frontmatter
    head = []
    head.append("---")
    head.append(f"case_id: {case_id}")
    head.append(f"appendix_type: filing_schedule")
    head.append(f"generated_at: {datetime.now().isoformat(timespec='seconds')}")
    head.append(f"user_role: {user_role}")
    head.append(f"engine: pipeline/temporal_schedule.py v2")
    head.append(f"deadlines_total: {summary['total']}")
    head.append(f"deadlines_expired: {summary['expired']}")
    head.append(f"deadlines_imminent: {summary['imminent']}")
    head.append(f"deadlines_upcoming: {summary['upcoming']}")
    head.append(f"deadlines_open: {summary['open']}")
    head.append("---")
    head.append("")
    head.append("# Filing Schedule Appendix")
    head.append("")
    head.append(f"This appendix is computed from `pipeline/temporal_rules.yaml` "
                f"(53 rules across federal/PA/SCOTUS/Tax forums) and the case "
                f"yaml's anchor fields. Status is computed relative to "
                f"**{today.isoformat()}**.")
    head.append("")

    # Anchor sources table — transparency about what fed the computation
    head.append("## Trigger Anchors Extracted from Yaml")
    head.append("")
    head.append("| Field | Value | Source |")
    head.append("|-------|-------|--------|")
    src = anchors.get("anchor_sources", {}) or {}
    for field in ("filed", "latest_order", "entry_of_judgment",
                  "appeal_filed", "appeal_decided"):
        val = anchors.get(field)
        val_str = val.isoformat() if val else "—"
        srcv = src.get(field, "—")
        head.append(f"| {field} | {val_str} | {srcv} |")
    head.append("")
    head.append(f"Forum: `{anchors.get('forum', '?')}` | User role: `{user_role}`")
    head.append("")

    # Critical alerts section first — surface anything jurisdictional + imminent
    critical = [d for d in deadlines
                if d.get("status") == "IMMINENT" or
                (d.get("status") == "EXPIRED" and d.get("jurisdictional"))]
    if critical:
        head.append("## 🚨 Critical Items")
        head.append("")
        for d in critical:
            jur = " **[JURISDICTIONAL]**" if d.get("jurisdictional") else ""
            head.append(f"- **{d['name']}**{jur} — {d['citation']}")
            dr = d.get("days_remaining")
            if dr is None:
                head.append(f"  - Deadline: {d.get('deadline','?')} (no fixed time)")
            elif dr >= 0:
                head.append(f"  - Deadline: {d.get('deadline')} ({dr} days remaining)")
            else:
                head.append(f"  - Deadline: {d.get('deadline')} ({-dr} days OVERDUE)")
            head.append(f"  - Anchor: {d.get('anchor_date')} ({d.get('anchor_field')})")
            if d.get("notes"):
                head.append(f"  - Notes: {d['notes']}")
        head.append("")

    # Full filing-schedule block (delegate to format_deadlines_block)
    head.append("---")
    head.append("")
    block = format_deadlines_block(case_id, today=today,
                                    user_role=user_role,
                                    include_open=True)
    # Strip the H2 the formatter adds (we've already got our own H1)
    block_lines = block.split("\n")
    # Drop leading "## Filing Schedule — case_id" line if present
    if block_lines and block_lines[0].startswith("## Filing Schedule"):
        block_lines = block_lines[1:]
    head.append("\n".join(block_lines).lstrip("\n"))

    # Footer
    head.append("")
    head.append("---")
    head.append("")
    head.append("*Generated by `pipeline/scorecard_temporal_appendix.py`. "
                "Update CASE_GROUND_TRUTH.yaml to refine anchor extraction.*")

    return {
        "markdown": "\n".join(head),
        "anchors": anchors,
        "deadlines": deadlines,
        "summary": summary,
    }


# ═════════════════════════════════════════════════════════════════════════
# FILE WRITER
# ═════════════════════════════════════════════════════════════════════════

def write_appendix(case_id: str,
                    user_role: str = "plaintiff",
                    today: Optional[date] = None,
                    overwrite_latest: bool = True) -> Dict[str, Any]:
    """Write a Filing Schedule appendix to chain_outputs/.

    Files written:
      temporal_schedule_{YYYYMMDD}.md   (date-stamped, never overwritten)
      temporal_schedule_latest.md       (stable symlink-like file, overwrite=True)

    Returns dict with paths and summary stats.
    """
    today = today or date.today()
    slug = _slug_for_case(case_id)
    if not slug:
        return {"error": f"unknown case_id: {case_id}"}

    out_dir = CASES_DIR / slug / "chain_outputs"
    if not out_dir.exists():
        return {"error": f"chain_outputs dir not found for {slug}: {out_dir}"}

    built = build_appendix_markdown(case_id, user_role=user_role, today=today)
    if "markdown" not in built:
        return {"error": "failed to build markdown", "detail": built}

    md = built["markdown"]
    date_stamp = today.strftime("%Y%m%d")
    dated_path = out_dir / f"temporal_schedule_{date_stamp}.md"
    latest_path = out_dir / "temporal_schedule_latest.md"

    try:
        dated_path.write_text(md, encoding="utf-8")
        if overwrite_latest:
            latest_path.write_text(md, encoding="utf-8")
    except Exception as e:
        return {"error": f"failed to write file: {e}",
                "dated_path": str(dated_path),
                "latest_path": str(latest_path)}

    return {
        "dated_path": str(dated_path),
        "latest_path": str(latest_path) if overwrite_latest else None,
        "deadlines_count": built["summary"]["total"],
        "expired": built["summary"]["expired"],
        "imminent": built["summary"]["imminent"],
        "upcoming": built["summary"]["upcoming"],
        "open": built["summary"]["open"],
        "anchors_filed": str(built["anchors"].get("filed")) if built["anchors"].get("filed") else None,
        "anchors_latest_order": str(built["anchors"].get("latest_order")) if built["anchors"].get("latest_order") else None,
        "anchors_entry_of_judgment": str(built["anchors"].get("entry_of_judgment")) if built["anchors"].get("entry_of_judgment") else None,
        "anchors_appeal_filed": str(built["anchors"].get("appeal_filed")) if built["anchors"].get("appeal_filed") else None,
        "anchors_appeal_decided": str(built["anchors"].get("appeal_decided")) if built["anchors"].get("appeal_decided") else None,
        "bytes_written": len(md.encode("utf-8")),
    }


# ═════════════════════════════════════════════════════════════════════════
# CLI
# ═════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: python -m pipeline.scorecard_temporal_appendix <case_id> [user_role]")
        sys.exit(1)
    cid = sys.argv[1]
    role = sys.argv[2] if len(sys.argv) > 2 else "plaintiff"
    result = write_appendix(cid, user_role=role)
    print("Result:")
    for k, v in result.items():
        print(f"  {k}: {v}")
