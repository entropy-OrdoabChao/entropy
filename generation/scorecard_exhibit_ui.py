"""
generation/scorecard_exhibit_ui.py — UI pipeline bridge for scorecard exhibit.

The existing generation/scorecard_exhibit.py expects a ReplyBriefData object
built from audit JSON files (CLI flow). This module bridges the UI pipeline
(core/beezle.py) — which has in-memory conflict_engine output and case_info —
to the same scorecard renderer.

STRATEGY (per Q1 = A with fallback to B):
  1. Try to load output/audit/data_<canonical_id>.json (the CLI-produced
     audit data). If present, use it — richer scorecard.
  2. Otherwise, build a minimal ReplyBriefData from the in-memory conflict
     map + case info from the current run. Lighter scorecard but still
     useful as Exhibit A.

The output is identical in both cases: a separate .docx attached to the
filing. The UI's Recent Documents panel picks it up automatically.

NO LLM CALLS. Pure data shape adaptation + delegation to the existing builder.
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from core.logger import get_logger

log = get_logger("SCORECARD_UI")


def _safe_canonical_id(case_info: dict) -> Optional[str]:
    """Try to derive a canonical_id from case_info for audit lookup."""
    if not case_info:
        return None
    # Direct hit
    cid = case_info.get("canonical_id") or case_info.get("registry_id")
    if cid:
        return cid
    # Build from court + case_no
    court = (case_info.get("case_court") or case_info.get("court") or "").lower()
    case_no = case_info.get("case_no") or case_info.get("docket_id") or ""
    if not case_no:
        return None
    if "common pleas" in court or "phila" in court or "ccp" in court:
        return f"phila_ccp:{case_no}"
    if "district" in court or "edpa" in court or "fed" in court or "u.s." in court:
        # Federal — strip leading "2:" if present, then re-add
        clean = case_no.replace(" ", "")
        if not clean.startswith("2:") and "cv" in clean:
            clean = f"2:{clean}"
        return f"fed:{clean}"
    return None


def _try_audit_data_path(canonical_id: str, base_path: Path) -> Optional[Path]:
    """Find the audit JSON for a canonical_id, if it exists."""
    if not canonical_id:
        return None
    safe = canonical_id.replace(":", "_").replace("/", "_")
    candidates = [
        base_path / "output" / "audit" / f"data_{safe}.json",
        base_path / "output" / "audit" / f"data_{canonical_id.replace(':', '_')}.json",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def _build_data_from_audit(canonical_id: str, base_path: Path):
    """Path A: Load full ReplyBriefData from audit JSON. Returns None on failure."""
    try:
        from generation.reply_brief_drafter import load_brief_data
        return load_brief_data(canonical_id, base_path)
    except Exception as e:
        log.warning("audit_load", f"Could not load audit data for {canonical_id}: {e}")
        return None


def _build_data_from_pipeline(
    case_info: dict,
    conflict_map: dict,
    logic_result: dict,
    canonical_id: str = "",
):
    """Path B fallback: Build a minimal ReplyBriefData from in-memory pipeline data.

    The conflict_engine produces conflicts with attack_vectors but doesn't
    classify by issuing court (that's the audit pipeline's job). This builds
    a lighter scorecard that still works as Exhibit A.
    """
    from generation.reply_brief_drafter import (
        ReplyBriefData,
        FlaggedViolation,
        IssueGroup,
        classify_issue,
    )

    case_info = case_info or {}
    conflict_map = conflict_map or {}
    logic_result = logic_result or {}

    conflicts = conflict_map.get("conflicts", []) or []

    # Convert each conflict into a FlaggedViolation. The conflict has:
    #   - issue (verification, garnishment, etc.)
    #   - their_position (the opposing argument)
    #   - rule_text (if retrieved)
    #   - rule_authority (if retrieved)
    #   - status (violation | challenge)
    #   - attack_vector (timing | scope | authority | application | procedure | burden)
    by_source: dict = defaultdict(list)
    by_issue: dict = {}

    for c in conflicts:
        if not isinstance(c, dict):
            continue
        issue = c.get("issue", "hierarchy")
        their_position = c.get("their_position", "") or c.get("position", "")
        rule_authority = c.get("rule_authority", "") or "(in-memory pipeline — not from audit)"
        status = c.get("status", "violation")

        v = FlaggedViolation(
            citation=their_position[:200] if their_position else f"({issue} conflict)",
            cited_court="UNKNOWN",  # conflict_engine doesn't classify
            severity="fatal" if status == "violation" else "serious",
            source_file="(uploaded document)",
            source_filename="(uploaded document)",
            rule_violated=rule_authority,
            issue=issue,
        )
        by_source["(uploaded document)"].append(v)
        if issue not in by_issue:
            by_issue[issue] = IssueGroup(issue=issue)
        by_issue[issue].violations.append(v)

    # Also fold any logic_result violations into the count
    extra_violations = logic_result.get("violations_found", []) or []
    fatal_extra = sum(1 for v in extra_violations
                      if isinstance(v, dict) and v.get("severity") == "fatal")
    serious_extra = len(extra_violations) - fatal_extra

    total_fatal = sum(g.fatal_count for g in by_issue.values()) + fatal_extra
    total_serious = sum(g.serious_count for g in by_issue.values()) + serious_extra

    scorecard = logic_result.get("scorecard", {}) or {}
    score = scorecard.get("total_score", 0) or scorecard.get("score", 0) or 0

    # Caption
    plaintiff = case_info.get("plaintiff", "FILER-FARDY BABAH FILER")
    defendant = case_info.get("defendant", "[DEFENDANT]")
    caption = f"{plaintiff} v. {defendant}"

    # Court detection
    court_str = (case_info.get("court", "") or "").lower()
    if "common pleas" in court_str or "phila" in court_str:
        case_court = "PA_CP_PHILA"
    elif "district" in court_str or "edpa" in court_str:
        case_court = "EDPA"
    else:
        case_court = case_info.get("case_court", "UNKNOWN")

    return ReplyBriefData(
        canonical_id=canonical_id or "(in-memory)",
        case_caption=caption,
        case_court=case_court,
        docket_id=case_info.get("case_no", "(unknown)"),
        judge=case_info.get("judge", ""),
        case_type=case_info.get("case_type", ""),
        parties=[plaintiff.lower().replace(" ", "_"), defendant.lower().replace(" ", "_")],
        by_source_file=dict(by_source),
        by_issue=by_issue,
        total_fatal=total_fatal,
        total_serious=total_serious,
        score=score if isinstance(score, int) else 0,
        rank=scorecard.get("rank", "?"),
        mined_authorities=[],
    )


def generate_scorecard_for_run(
    case_info: dict,
    conflict_map: dict,
    logic_result: dict,
    base_path: Path,
    output_path: Path,
) -> Optional[Path]:
    """Generate a scorecard exhibit for a UI pipeline run.

    Args:
        case_info: from case_context extraction (plaintiff, defendant, court, case_no)
        conflict_map: from conflict_engine
        logic_result: from logic_engine (includes scorecard, violations_found)
        base_path: <home> (for audit data lookup)
        output_path: where to write the .docx

    Returns:
        Path to the written .docx, or None on failure.

    Strategy:
        1. Try to find canonical_id and load audit data (Path A — richer)
        2. Fall back to in-memory pipeline data (Path B — lighter)
        3. Call existing build_scorecard_exhibit() with whichever data we got
    """
    try:
        from generation.scorecard_exhibit import build_scorecard_exhibit
    except ImportError as e:
        log.warning("import", f"scorecard_exhibit not available: {e}")
        return None

    # Path A: try audit data
    canonical_id = _safe_canonical_id(case_info)
    data = None
    source = "none"

    if canonical_id:
        audit_path = _try_audit_data_path(canonical_id, base_path)
        if audit_path:
            data = _build_data_from_audit(canonical_id, base_path)
            if data:
                source = f"audit:{canonical_id}"
                log.stage("source", f"Using audit data: {audit_path.name}")

    # Path B fallback
    if data is None:
        data = _build_data_from_pipeline(
            case_info=case_info,
            conflict_map=conflict_map,
            logic_result=logic_result,
            canonical_id=canonical_id or "",
        )
        source = "in-memory conflicts"
        log.stage("source", "Using in-memory conflict map (no audit data)")

    # Render
    try:
        result_path = build_scorecard_exhibit(data, output_path)
        log.stage("generate",
                  f"Scorecard exhibit written: {result_path.name} (source: {source})")
        return result_path
    except Exception as e:
        log.warning("generate", f"Scorecard generation failed: {e}")
        return None
