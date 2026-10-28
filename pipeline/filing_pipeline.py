# -*- coding: utf-8 -*-
"""
pipeline/filing_pipeline.py
=============================

Deterministic filing pipeline orchestrator.

PRIMARY drafter: generation.reply_brief_drafter.draft_for_case
  - mode="pipeline" — uses _build_data_from_pipeline (no pre-existing audit JSON needed)
  - mode="audit"    — uses output/audit/data_<safe_key>.json
  - mode="hybrid"   — pipeline + audit data merged

This drafter produces:
  - reply_brief_<case>_<ts>.md
  - reply_brief_<case>_<ts>.docx
  - scorecard_exhibit_<case>_<ts>.docx
  - violations_matrix exhibit (when in pipeline/hybrid mode)

Fallback: composer (compose_filing → render) used only if real drafter fails.

Post-processing:
  - basic_output_clean — replaces internal IDs/jargon with court-friendly text
  - enforce_court_profile — strips wrong-jurisdiction citations
"""

from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import json
import logging
import re
import shutil
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional

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

ROOT = _BEEZLE_ROOT
OUTPUT_FILINGS = ROOT / "output" / "filings"

log = logging.getLogger("filing_pipeline")


# ---------------------------------------------------------------------------
# CASE_ID resolution
# ---------------------------------------------------------------------------

_CASE_ALIASES = {
    "lippi":              "phila_ccp:260100000",
    "lippi case":         "phila_ccp:260100000",
    "lippi etal":         "phila_ccp:260100000",
    "contractor":         "phila_ccp:260100000",
    "garnishment":        "phila_ccp:260100000",
    "ccp":                "phila_ccp:260100000",
    "philadelphia ccp":   "phila_ccp:260100000",
    "260100000":          "phila_ccp:260100000",

    "equifax":            "fed:2:26-cv-00123",
    "fcra":               "fed:2:26-cv-00123",
    "credit report":      "fed:2:26-cv-00123",
    "credit bureau":      "fed:2:26-cv-00123",
    "2:26-cv-00123":      "fed:2:26-cv-00123",
    "26-cv-00123":        "fed:2:26-cv-00123",

    "amerisave":          "fed:2:26-cv-00456",
    "respa":              "fed:2:26-cv-00456",
    "tila":               "fed:2:26-cv-00456",
    "quiet title":        "fed:2:26-cv-00456",
    "mortgage":           "fed:2:26-cv-00456",
    "2:26-cv-00456":      "fed:2:26-cv-00456",
    "26-cv-00456":        "fed:2:26-cv-00456",

    "state farm":         "fed:2:26-cv-00789",
    "statefarm":          "fed:2:26-cv-00789",
    "bad faith":          "fed:2:26-cv-00789",
    "insurance":          "fed:2:26-cv-00789",
    "8371":               "fed:2:26-cv-00789",
    "2:26-cv-00789":      "fed:2:26-cv-00789",
    "26-cv-00789":        "fed:2:26-cv-00789",
}

_CASE_ID_DISPLAY = {
    "phila_ccp:260100000": "No. 260100000",
    "fed:2:26-cv-00123":   "No. 2:26-cv-00123",
    "fed:2:26-cv-00456":   "No. 2:26-cv-00456",
    "fed:2:26-cv-00789":   "No. 2:26-cv-00789",
}

# ---------------------------------------------------------------------------
# Filing intent detection
# ---------------------------------------------------------------------------

_FILING_VERBS = re.compile(
    r"\b(generate|draft|build|compose|prepare|create|file|write)\b",
    re.IGNORECASE,
)
_FILING_NOUNS = re.compile(
    r"\b(filing|motion|brief|petition|notice|opposition|reply|motions?\s+to\s+strike|"
    r"motion\s+to\s+vacate|motion\s+to\s+dismiss|motion\s+to\s+remand|"
    r"motion\s+for\s+(?:reconsideration|recusal|sanctions|garnishee\s+default)|"
    r"supplemental\s+authority|writ\s+of\s+mandamus|complaint|amended\s+complaint)\b",
    re.IGNORECASE,
)

_FILING_TYPE_MAP = {
    "motion to strike state":           "motion_to_strike_state",
    "motion to strike federal":         "motion_to_strike_federal",
    "motion to strike":                 "motion_to_strike_state",
    "motion to vacate":                 "motion_to_vacate",
    "motion for reconsideration":       "motion_for_reconsideration",
    "motion for recusal":               "motion_for_recusal",
    "motion to remand":                 "motion_to_remand",
    "motion for garnishee default":     "motion_for_garnishee_default",
    "garnishee default":                "motion_for_garnishee_default",
    "supplemental authority":           "notice_of_supplemental_authority",
    "notice of supplemental authority": "notice_of_supplemental_authority",
    "mandamus":                         "petition_for_writ_of_mandamus",
    "writ of mandamus":                 "petition_for_writ_of_mandamus",
}


# BEEZLE_FN_UNUSED 2026-06-13: detect_filing_intent (no static refs in pipeline/)
def detect_filing_intent(message: str) -> Optional[dict]:
    if not message:
        return None
    msg = message.lower().strip()

    has_verb = bool(_FILING_VERBS.search(msg))
    has_noun = bool(_FILING_NOUNS.search(msg))
    if not (has_verb or has_noun):
        return None

    case_id = None
    matched_alias = None
    for alias in sorted(_CASE_ALIASES.keys(), key=len, reverse=True):
        if alias in msg:
            case_id = _CASE_ALIASES[alias]
            matched_alias = alias
            break

    if not case_id:
        return None

    filing_type = None
    for phrase, slug in _FILING_TYPE_MAP.items():
        if phrase in msg:
            filing_type = slug
            break

    return {
        "action":        "generate_filing",
        "case_id":       case_id,
        "filing_type":   filing_type,
        "matched_alias": matched_alias,
    }


# ---------------------------------------------------------------------------
# OUTPUT FILTER  — replace, not delete
# ---------------------------------------------------------------------------

# Replace internal references with court-friendly substitutes.
# Order matters — longer phrases first.
_REPLACEMENTS = [
    # System references → court-friendly equivalents
    ("Scorecard Exhibit attached hereto",     "Exhibit A"),
    ("Scorecard Exhibit",                      "Exhibit A"),
    ("scorecard exhibit",                      "Exhibit A"),
    ("audit data on file",                     "the record"),
    ("audit data",                             "the record"),
    ("identified in the ;",                    "identified in the record."),
    ("identified in the .",                    "identified in the record."),
    ("identified in the  ",                    "identified in the record "),
    ("cross_case_count",                       ""),
    ("cross_case attack",                      "cross-case attack"),
    ("cross_case",                             "cross-case"),
    (" registry ",                             " "),
    ("(registry)",                             ""),
    # Internal status tokens
    ("status: removed_from_state_to_edpa",     ""),
    ("status: registered",                     ""),
    ("status: merits_mandamus_pending",        ""),
    ("posture: enforcing_judgment",            ""),
    ("posture: attacking_judgment",            ""),
    ("posture: merits",                        ""),
    # Engine / module names (should never appear in output)
    ("scenario_engine",      ""),
    ("chain_runner",         ""),
    ("filing_composer",      ""),
    ("reasoning_orchestrator", ""),
    ("reply_brief_drafter",  ""),
    ("violations_matrix",    "violations matrix"),
]


def _scrub_case_ids(text: str) -> str:
    for canonical, display in _CASE_ID_DISPLAY.items():
        text = text.replace(f"`{canonical}`", display)
        text = text.replace(canonical, display)
    text = re.sub(r"phila_ccp:(\d+)",            r"No. \1", text)
    text = re.sub(r"PHILA_CCP:(\d+)",            r"No. \1", text)
    text = re.sub(r"fed:(\d:\d{2}-cv-\d{5})",    r"No. \1", text)
    text = re.sub(r"FED:(\d:\d{2}-cv-\d{5})",    r"No. \1", text)
    return text


def basic_output_clean(text: str) -> str:
    """Deterministic output filter. Strips internal IDs and system jargon
    from any text destined for a court audience or chat user.
    """
    if not text:
        return text
    out = text
    out = _scrub_case_ids(out)
    for needle, replace in _REPLACEMENTS:
        out = out.replace(needle, replace)
    out = re.sub(r"\n{3,}", "\n\n", out)
    out = re.sub(r"  +",     " ", out)
    return out


# ---------------------------------------------------------------------------
# COURT PROFILE ENFORCEMENT — strip wrong-jurisdiction citations
# ---------------------------------------------------------------------------

# State-court (PA CCP) filings should NOT cite federal procedural rules
_STATE_COURT_BAD_CITES = [
    re.compile(r"\bFed\.?\s*R\.?\s*Civ\.?\s*P\.?\s*\d+(?:\([a-z0-9]+\))?\b", re.IGNORECASE),
    re.compile(r"\bFRCP\s+\d+(?:\([a-z0-9]+\))?\b",                          re.IGNORECASE),
    re.compile(r"\b28\s+U\.?\s*S\.?\s*C\.?\s*§?\s*\d+\b",                    re.IGNORECASE),
    re.compile(r"\bFederal\s+Rules?\s+of\s+(Civil|Criminal|Appellate)\s+Procedure\b", re.IGNORECASE),
]

# Federal filings should NOT cite Pennsylvania state procedural rules
_FED_COURT_BAD_CITES = [
    re.compile(r"\bPa\.?\s*R\.?\s*C\.?\s*P\.?\s*\d+(?:\([a-z0-9]+\))?\b",   re.IGNORECASE),
    re.compile(r"\b42\s+Pa\.?\s*C\.?\s*S\.?\s*§?\s*\d+\b",                  re.IGNORECASE),
    re.compile(r"\bPennsylvania\s+Rules?\s+of\s+Civil\s+Procedure\b",       re.IGNORECASE),
]


def enforce_court_profile(text: str, court: str) -> tuple[str, list[str]]:
    """Strip out citations that don't belong in a filing for this court.

    Args:
      text:  document text
      court: court key (e.g. 'PA_CP_PHILA', 'PA_CCP', 'EDPA', '3CIR')

    Returns:
      (cleaned_text, list_of_removed_citations)
    """
    if not text or not court:
        return text, []

    is_state = bool(re.search(r"PA_CCP|PA_CP|PA_STATE|COMMON_PLEAS",
                              court, re.IGNORECASE))
    is_federal = bool(re.search(r"EDPA|MDPA|WDPA|3CIR|FED|US_DIST|D\.|CIRCUIT",
                                court, re.IGNORECASE))

    if not (is_state or is_federal):
        return text, []

    patterns = _STATE_COURT_BAD_CITES if is_state else _FED_COURT_BAD_CITES
    removed = []
    out = text
    for p in patterns:
        for m in p.finditer(out):
            removed.append(m.group(0))
        out = p.sub("", out)

    # Clean up the spacing left by removals: double spaces, dangling punctuation
    out = re.sub(r"\s+([,.;])",        r"\1",  out)        # " ," → ","
    out = re.sub(r"\(\s*\)",           "",     out)        # "()" → ""
    out = re.sub(r"\(\s*,",            "(",    out)        # "(,"  → "("
    out = re.sub(r",\s*\)",            ")",    out)        # ", )" → ")"
    out = re.sub(r"  +",                " ",   out)
    out = re.sub(r"\n{3,}",            "\n\n", out)

    return out, list(set(removed))


def _scrub_text_file(path: Path, court: str = "") -> dict:
    """Apply both basic_output_clean AND enforce_court_profile to a UTF-8
    text file in place. Returns dict with 'changed' bool and 'removed' list."""
    try:
        before = path.read_text(encoding="utf-8", errors="replace")
        cleaned = basic_output_clean(before)
        cleaned, removed = enforce_court_profile(cleaned, court) if court else (cleaned, [])
        if cleaned != before:
            path.write_text(cleaned, encoding="utf-8")
            return {"changed": True, "removed": removed}
        return {"changed": False, "removed": removed}
    except Exception as e:
        log.warning(f"_scrub_text_file({path}) failed: {e}")
        return {"changed": False, "removed": []}


def _scrub_docx_file(path: Path, court: str = "") -> dict:
    """Apply filters to every paragraph + table cell in a .docx."""
    try:
        from docx import Document
    except ImportError:
        return {"changed": False, "removed": []}

    try:
        doc = Document(str(path))
    except Exception as e:
        log.warning(f"could not open docx {path}: {e}")
        return {"changed": False, "removed": []}

    changed = False
    all_removed = []

    def _scrub_runs(paragraph):
        nonlocal changed
        for run in paragraph.runs:
            new = basic_output_clean(run.text)
            if court:
                new, rem = enforce_court_profile(new, court)
                all_removed.extend(rem)
            if new != run.text:
                run.text = new
                changed = True

    for p in doc.paragraphs:
        _scrub_runs(p)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    _scrub_runs(p)

    if changed:
        try:
            doc.save(str(path))
        except Exception as e:
            log.warning(f"could not save scrubbed docx {path}: {e}")
            return {"changed": False, "removed": []}
    return {"changed": changed, "removed": list(set(all_removed))}


# ---------------------------------------------------------------------------
# Pipeline core — uses reply_brief_drafter as PRIMARY
# ---------------------------------------------------------------------------

def generate_filing(case_id: str,
                    filing_type: Optional[str] = None,
                    mode: str = "pipeline") -> dict:
    """Run the full filing pipeline.

    Tries reply_brief_drafter.draft_for_case first (real drafter that pulls
    in violations_matrix, scorecard, posture_templates). Falls back to
    composer (compose_filing) if the real drafter errors.

    Args:
      case_id:     canonical id ('phila_ccp:260100000' or alias)
      filing_type: optional hint for fallback path. Real drafter chooses internally.
      mode:        'pipeline' (default) | 'audit' | 'hybrid'
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    case_slug = re.sub(r"[^a-zA-Z0-9_]+", "_", case_id).strip("_").lower()
    out_dir = OUTPUT_FILINGS / case_slug
    out_dir.mkdir(parents=True, exist_ok=True)

    result = {
        "ok":           False,
        "case_id":      case_id,
        "filing_type":  filing_type,
        "timestamp":    timestamp,
        "mode":         mode,
        "steps":        [],
        "errors":       [],
    }

    def _step(name: str, status: str, detail: str = ""):
        entry = {"step": name, "status": status, "detail": detail}
        result["steps"].append(entry)
        if status == "ERROR":
            result["errors"].append(f"{name}: {detail}")
        log.info(f"[{name}] {status}: {detail[:200]}")
        return entry

    # ---- 1. Try the REAL drafter (reply_brief_drafter.draft_for_case) ----
    primary_used = False
    md_path = None
    docx_path = None
    scorecard_path = None
    court = ""

    try:
        from generation.reply_brief_drafter import draft_for_case

        # Lookup court from registry for court-profile enforcement later
        try:
            registry = json.loads(
                (ROOT / "cases_index" / "case_registry.json").read_text(encoding="utf-8")
            )
            entry = registry.get(case_id, {})
            court = entry.get("case_court", "")
            result["caption"] = entry.get("caption", case_id)
            result["court"]   = court
            result["case_type"] = entry.get("case_type", "")
        except Exception as e:
            _step("registry_read", "WARN", f"{type(e).__name__}: {e}")

        # === FILING_TYPE_WIRING_V1 ===
        # Pick filing_type via recommender BEFORE drafting so the drafter
        # can title and frame the document correctly.
        # BUG-5 FIX: an explicit --filing-type is BINDING — the recommender's top pick must NOT override a
        # filing the operator or the appeal gate deliberately requested (e.g. rule_54b_certification,
        # motion_for_relief_from_judgment_60b4_void). Auto-recommend ONLY when none was passed.
        _explicit_ft = bool(filing_type)
        try:
            from pipeline.filing_composer import recommend_filing_types
            # === PATH_B_AUDIT_REFRESH_V1 ===
            # Refresh procedural violations from opposing filings before recommender runs.
            try:
                from pipeline.audit_writer import build_audit_data
                _court = court or "EDPA"
                _caption = result.get("caption", "") or ""
                build_audit_data(case_id, court=_court, caption=_caption, persist=True, skip_if_fresh=True)
            except Exception as _e:
                import logging as _logging
                _logging.getLogger("filing_pipeline").warning(
                    f"audit refresh failed (non-fatal): {_e}"
                )
            # === END PATH_B_AUDIT_REFRESH_V1 ===
            recs = recommend_filing_types(case_id, top_n=5) or []
            if _explicit_ft:
                bundled_with = []
                _step("recommend", "OK",
                      f"explicit filing_type={filing_type} (BINDING; recommender bypassed, "
                      f"would-be top={ (recs[0].get('id') if recs else None) })")
            elif recs:
                top = recs[0]
                filing_type = top.get("id") or top.get("filing_type")
                # === PHASE2_BUNDLING_V1 ===
                # If the next-ranked motion is within 5 points AND compatible,
                # bundle it (e.g., motion_to_strike + motion_for_sanctions).
                COMPATIBLE_BUNDLES = {
                    ("motion_to_strike_state",   "motion_for_sanctions"),
                    ("motion_to_strike_federal", "motion_for_sanctions"),
                    ("motion_to_strike_state",   "motion_to_compel"),
                    ("motion_to_strike_federal", "motion_to_compel"),
                    ("motion_for_garnishee_default", "motion_for_sanctions"),
                }
                # Add reverse pairs for symmetry
                COMPATIBLE_BUNDLES = COMPATIBLE_BUNDLES | {(b, a) for (a, b) in COMPATIBLE_BUNDLES}
                bundled_with = []
                if len(recs) > 1:
                    second = recs[1]
                    second_id = second.get("id") or second.get("filing_type")
                    score_gap = abs(top.get("score", 0) - second.get("score", 0))
                    if score_gap <= 5 and (filing_type, second_id) in COMPATIBLE_BUNDLES:
                        bundled_with.append(second_id)
                # === END PHASE2_BUNDLING_V1 ===
                _step("recommend", "OK",
                      f"top={filing_type} score={top.get('score')} "
                      f"bundled_with={bundled_with} "
                      f"runners_up={[r.get('id') for r in recs[1:3]]}")
            else:
                filing_type = None
                bundled_with = []
                _step("recommend", "WARN", "no recommendations returned")
        except Exception as _e:
            filing_type = None
            _step("recommend", "WARN", f"{type(_e).__name__}: {_e}")
        # === END FILING_TYPE_WIRING_V1 ===

        md, docx, scor = draft_for_case(
            canonical_id=case_id,
            base_path=ROOT,
            output_dir=out_dir,
            with_scorecard=True,
            mode=mode,
            filing_type=filing_type,
        )
        md_path        = Path(md) if md else None
        docx_path      = Path(docx) if docx else None
        scorecard_path = Path(scor) if scor else None
        primary_used = True
        result["md_path"]        = str(md_path) if md_path else None
        result["docx_path"]      = str(docx_path) if docx_path else None
        result["scorecard_path"] = str(scorecard_path) if scorecard_path else None
        _step("primary_drafter", "OK",
              f"reply_brief_drafter (mode={mode}) "
              f"md={md_path.name if md_path else '?'} "
              f"docx={docx_path.name if docx_path else '?'} "
              f"scorecard={scorecard_path.name if scorecard_path else 'none'}")
        result["filing_type"] = "reply_brief_pipeline"
    except Exception as e:
        _step("primary_drafter", "WARN",
              f"falling back to composer: {type(e).__name__}: {e}")
        log.warning(f"reply_brief_drafter failed: {traceback.format_exc()}")

    # ---- 2. Fallback to composer if primary failed ----
    if not primary_used:
        try:
            from pipeline.filing_composer import (
                build_context,
                recommend_filing_types,
                compose_filing as _compose,
                render_composition_to_markdown,
            )

            ctx = build_context(case_id)
            if ctx.get("error"):
                _step("build_context", "ERROR", str(ctx["error"]))
                return result
            result["caption"] = ctx.get("caption") or case_id
            result["court"] = ctx.get("court", "")
            court = court or result["court"]

            if not filing_type:
                recs = recommend_filing_types(case_id, top_n=3) or []
                if recs:
                    filing_type = recs[0].get("filing_type") or recs[0].get("id")
                    result["filing_type"] = filing_type
                    _step("recommend", "OK",
                          f"top={filing_type} (score={recs[0].get('score')})")
                else:
                    _step("recommend", "ERROR", "no recommendations")
                    return result

            composition = _compose(case_id, filing_type)
            blocks      = getattr(composition, "blocks", []) or (composition.get("blocks", []) if isinstance(composition, dict) else [])
            skipped     = getattr(composition, "skipped_blocks", []) or (composition.get("skipped_blocks", []) if isinstance(composition, dict) else [])
            result["composition_summary"] = {"blocks": len(blocks), "skipped": len(skipped)}
            _step("compose", "OK", f"blocks={len(blocks)} skipped={len(skipped)}")

            md_text = render_composition_to_markdown(composition)
            md_path = out_dir / f"{filing_type}_{timestamp}.md"
            md_path.write_text(md_text, encoding="utf-8")
            result["md_path"] = str(md_path)
            _step("render_md", "OK", str(md_path))

            docx_path = out_dir / f"{filing_type}_{timestamp}.docx"
            try:
                from pipeline.filing_composer import render_composition_to_docx as renderer
                renderer(composition, str(docx_path))
            except ImportError:
                _simple_md_to_docx(md_text, docx_path)
            result["docx_path"] = str(docx_path)
            _step("render_docx", "OK", str(docx_path))
        except Exception as e:
            _step("composer_fallback", "ERROR", f"{type(e).__name__}: {e}")
            result["traceback"] = traceback.format_exc()
            return result

    # ---- 3. POST-PROCESS — output filter + court profile enforcement ----
    scrub_summary = {"files_changed": 0, "removed_cites": []}
    for fpath in [md_path, docx_path, scorecard_path]:
        if not fpath or not fpath.exists():
            continue
        if fpath.suffix == ".docx":
            r = _scrub_docx_file(fpath, court=court)
        else:
            r = _scrub_text_file(fpath, court=court)
        if r["changed"]:
            scrub_summary["files_changed"] += 1
        scrub_summary["removed_cites"].extend(r["removed"])
    scrub_summary["removed_cites"] = sorted(set(scrub_summary["removed_cites"]))
    result["scrub_summary"] = scrub_summary
    _step("scrub_output", "OK",
          f"files_changed={scrub_summary['files_changed']} "
          f"removed_cites={scrub_summary['removed_cites'][:6]}")

    # ---- 4. Find any extra files the real drafter wrote (violations_matrix etc) ----
    extra_files = []
    try:
        recent = sorted(out_dir.glob(f"*{timestamp[:8]}*"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
        seen = {p.resolve() for p in [md_path, docx_path, scorecard_path] if p}
        for p in recent:
            if p.resolve() in seen:
                continue
            if (datetime.now().timestamp() - p.stat().st_mtime) < 120:
                extra_files.append(p)
                # Apply scrub here too
                if p.suffix == ".docx":
                    _scrub_docx_file(p, court=court)
                elif p.suffix in (".md", ".txt"):
                    _scrub_text_file(p, court=court)
    except Exception as e:
        log.warning(f"extra-files sweep failed: {e}")

    # ---- 5. Assemble packet ----
    try:
        packet_dir = out_dir / f"packet_{timestamp}"
        packet_dir.mkdir(parents=True, exist_ok=True)
        copied = []
        for src in [docx_path, scorecard_path, md_path, *extra_files]:
            if src and Path(src).exists():
                dest = packet_dir / Path(src).name
                shutil.copy2(src, dest)
                copied.append(dest.name)

        checklist_path = packet_dir / "checklist.md"
        checklist_path.write_text(
            _build_checklist(
                case_id=case_id,
                filing_type=result.get("filing_type", "reply_brief_pipeline"),
                caption=result.get("caption", ""),
                court=result.get("court", ""),
                files=copied,
                scrub_summary=scrub_summary,
            ),
            encoding="utf-8",
        )

        result["packet_dir"]     = str(packet_dir)
        result["packet_files"]   = copied
        result["checklist_path"] = str(checklist_path)
        _step("packet", "OK", f"{packet_dir} ({len(copied)} files)")
    except Exception as e:
        _step("packet", "ERROR", f"{type(e).__name__}: {e}")

    result["ok"] = (
        result.get("docx_path") is not None
        and result.get("packet_dir") is not None
    )
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _simple_md_to_docx(md_text: str, dest: Path) -> None:
    try:
        from docx import Document
        from docx.shared import Pt
    except ImportError:
        dest.with_suffix(".txt").write_text(md_text, encoding="utf-8")
        return
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Times New Roman"
    style.font.size = Pt(12)
    for line in md_text.splitlines():
        if line.startswith("# "):
            doc.add_heading(line[2:], level=1)
        elif line.startswith("## "):
            doc.add_heading(line[3:], level=2)
        elif line.startswith("### "):
            doc.add_heading(line[4:], level=3)
        else:
            doc.add_paragraph(line)
    doc.save(str(dest))


def _build_checklist(case_id: str, filing_type: str, caption: str,
                     court: str, files: list, scrub_summary: dict) -> str:
    removed = scrub_summary.get("removed_cites", []) or []
    files_changed = scrub_summary.get("files_changed", 0)

    lines = [
        f"# Filing Packet Checklist",
        "",
        f"**Case**: {caption}  ",
        f"**case_id**: `{case_id}`  ",
        f"**Court**: {court}  ",
        f"**Filing type**: `{filing_type}`  ",
        f"**Generated**: {datetime.now().isoformat(timespec='seconds')}",
        "",
        "## Output filter",
        f"- Files modified by scrubber: {files_changed}",
    ]
    if removed:
        lines.append(f"- Wrong-jurisdiction citations removed ({len(removed)}):")
        for r in removed[:20]:
            lines.append(f"  - `{r}`")
    else:
        lines.append("- No wrong-jurisdiction citations detected")

    lines += ["", "## Files in packet"]
    if not files:
        lines.append("- (none)")
    for f in files:
        lines.append(f"- `{f}`")

    lines += [
        "",
        "## Pre-filing review",
        "- [ ] Caption is correct (party names, docket number)",
        "- [ ] All citations validated against rules database",
        "- [ ] Cross-case attack blocks (if present) reference correct related case",
        "- [ ] Certificate of service lists every party + counsel",
        "- [ ] Exhibits referenced in body are attached",
        "- [ ] Page limits per local rule",
        "",
        "## Filing log",
        "- Filed at: ",
        "- Confirmation #: ",
        "- Court receipt path: ",
        "- Outcome (won / denied / pending): ",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Run the filing pipeline end-to-end.")
    p.add_argument("case_id", help="Canonical case_id or party alias (e.g. 'lippi')")
    p.add_argument("--filing-type", default=None,
                   help="Optional. Only used in composer fallback path.")
    p.add_argument("--mode", default="pipeline",
                   choices=["pipeline", "audit", "hybrid"],
                   help="Drafter data source mode (default: pipeline)")
    p.add_argument("--json", action="store_true", help="Print full JSON result.")
    args = p.parse_args()

    case_arg = args.case_id.strip()
    if case_arg in _CASE_ALIASES:
        case_arg = _CASE_ALIASES[case_arg]
    elif case_arg.lower() in _CASE_ALIASES:
        case_arg = _CASE_ALIASES[case_arg.lower()]

    result = generate_filing(case_id=case_arg,
                             filing_type=args.filing_type,
                             mode=args.mode)

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(f"OK:          {result.get('ok')}")
        print(f"Case:        {result.get('caption')}  ({case_arg})")
        print(f"Court:       {result.get('court')}")
        print(f"Filing type: {result.get('filing_type')}")
        print(f"Mode:        {result.get('mode')}")
        print(f"DOCX:        {result.get('docx_path')}")
        print(f"Scorecard:   {result.get('scorecard_path')}")
        print(f"Packet:      {result.get('packet_dir')}")
        print()
        print("Steps:")
        for s in result.get("steps", []):
            print(f"  [{s['status']:5s}] {s['step']:20s} {s['detail'][:140]}")
        scrub = result.get("scrub_summary", {})
        if scrub.get("removed_cites"):
            print()
            print(f"Court-profile filter removed {len(scrub['removed_cites'])} bad citations:")
            for r in scrub["removed_cites"][:10]:
                print(f"  - {r}")
        if result.get("errors"):
            print()
            print("Errors:")
            for e in result["errors"]:
                print(f"  - {e}")
