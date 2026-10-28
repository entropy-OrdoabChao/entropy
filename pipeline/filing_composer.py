# -*- coding: utf-8 -*-
"""
pipeline/filing_composer.py
============================

Deterministic composable filing system. v2 (post-install hotfixes).

CHANGES from v1:
  - PyYAML preferred always; fallback parser only when truly absent
  - _resolve_path supports list indexing (registry.docket_aliases.0)
  - build_context derives cross_case_signal from registry notes/status as
    fallback when scanner returns 0 but registry indicates active motions

INPUT:
  - case_id (canonical)
  - filing_type (matches templates/filing_types/<id>.yaml)

PIPELINE:
  1. Load case diagnosis (defects + cross_case + strategy + scorecard)
  2. Load filing_type YAML config
  3. For each block in YAML structure:
       - Evaluate `when` clause against diagnosis context (deterministic, no LLM)
       - If True, load the block's markdown + frontmatter
       - Extract required_facts, hydrate placeholders from case data
  4. Return structured composition dict — every block has hydrated text
  5. The drafter renders the composition into .docx (separate step)

NO LLM IN THIS LAYER.
"""

from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import json
import logging
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

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

_ROOT_WSL = _eroot()
_ROOT_WIN = _BEEZLE_ROOT
ROOT = _ROOT_WSL if _ROOT_WSL.exists() else _ROOT_WIN
TEMPLATES_DIR = ROOT / "obsidian_sync" / "templates"
BLOCKS_DIR = TEMPLATES_DIR / "blocks"
FILING_TYPES_DIR = TEMPLATES_DIR / "filing_types"
LEARNING_DIR = ROOT / "obsidian_sync" / "learning"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

log = logging.getLogger("filing_composer")
if not log.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] [composer] %(message)s"))
    log.addHandler(h)
log.setLevel(logging.INFO)


# ───────────────────────────────────────────────────────────────────────────
# Data classes
# ───────────────────────────────────────────────────────────────────────────

@dataclass
class Block:
    id: str
    relative_path: str
    title: str = ""
    body_template: str = ""
    applies_to_defects: list = field(default_factory=list)
    required_authorities: list = field(default_factory=list)
    required_facts: list = field(default_factory=list)
    output_format: str = "section"
    raw_frontmatter: dict = field(default_factory=dict)


@dataclass
class FilingType:
    id: str
    court: str = ""
    title: str = ""
    description: str = ""
    required_authorities: list = field(default_factory=list)
    trigger_defects: list = field(default_factory=list)
    structure: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)


@dataclass
class ComposedBlock:
    block_id: str
    relative_path: str
    title: str
    rendered_text: str
    placeholders_filled: dict = field(default_factory=dict)
    placeholders_missing: list = field(default_factory=list)
    output_format: str = "section"


@dataclass
class Composition:
    case_id: str
    filing_type_id: str
    court: str = ""
    title: str = ""
    blocks: list = field(default_factory=list)
    skipped_blocks: list = field(default_factory=list)
    diagnosis_snapshot: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    errors: list = field(default_factory=list)


# ───────────────────────────────────────────────────────────────────────────
# YAML parsing — PyYAML preferred, fallback parser only if missing
# ───────────────────────────────────────────────────────────────────────────

_PYYAML_OK = False
try:
    import yaml as _yaml  # type: ignore
    _PYYAML_OK = True
except ImportError:
    _yaml = None



# ──────────────────────────────────────────────────────────────────────
# Caption / court string helpers
# Templates expect ctx["caption"] and ctx["court"] to be STRINGS.
# CASE_GROUND_TRUTH.yaml stores them as DICTS (plaintiff/defendants).
# These helpers flatten dict → display string. The original dict is preserved
# at ctx["caption_data"] and ctx["court_data"] for blocks that need it.
# ──────────────────────────────────────────────────────────────────────

def _flatten_caption(value) -> str:
    """Convert caption dict (or str) into 'Filer v. Defendant et al.' string."""
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return str(value) if value else "[Caption Pending]"
    plaintiff = value.get("plaintiff") or "Plaintiff"
    # last-name short form for caption: "FILER NAME" → "Filer"
    short = plaintiff.split()[-1] if " " in plaintiff else plaintiff
    defendants = value.get("defendants") or []
    # filter out placeholders like "Other parties of record" if real names exist
    named = [d for d in defendants if isinstance(d, dict)
             and d.get("name") and not d["name"].lower().startswith("other parties")]
    if not named and defendants:
        named = [d for d in defendants if isinstance(d, dict) and d.get("name")]
    if not named:
        return f"{short} v. [Defendant]"
    primary = named[0].get("name", "Defendant")
    suffix = " et al." if len(named) > 1 else ""
    return f"{short} v. {primary}{suffix}"


def _flatten_court(value) -> str:
    """Convert court dict (or str) into a display string like 'E.D. Pa.' / full name."""
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return str(value) if value else "the Court"
    # Prefer full_name; fall back to id; never return the bare dict
    return value.get("full_name") or value.get("id") or "the Court"


def _parse_simple_yaml(text: str) -> dict:
    """Parse YAML. PyYAML if available, fallback parser otherwise.

    The fallback handles only flat key:value + simple lists. It does NOT
    handle block scalars (`|`, `>`), nested mappings, etc.
    """
    if _PYYAML_OK:
        try:
            result = _yaml.safe_load(text) or {}
            return result if isinstance(result, dict) else {}
        except Exception as e:
            log.warning("PyYAML parse failed, falling back: %s", e)

    return _fallback_yaml_parse(text)


def _fallback_yaml_parse(text: str) -> dict:
    """Minimal fallback parser. Only used if PyYAML import fails entirely."""
    result: dict = {}
    raw_lines = text.split("\n")

    # Skip block scalars and comments
    lines = []
    skip_block_scalar = False
    block_indent = -1
    for line in raw_lines:
        if not line.strip():
            if skip_block_scalar and block_indent >= 0:
                pass  # blank lines inside block scalars don't terminate them
            continue
        if line.lstrip().startswith("#"):
            continue
        ind = len(line) - len(line.lstrip(" "))
        if skip_block_scalar:
            if ind > block_indent:
                continue
            skip_block_scalar = False
            block_indent = -1
        # Detect block-scalar marker (description: |)
        if line.rstrip().endswith("|") or line.rstrip().endswith(">"):
            skip_block_scalar = True
            block_indent = ind
            continue
        lines.append(line.rstrip())

    def indent_of(line: str) -> int:
        return len(line) - len(line.lstrip(" "))

    i = 0
    stack = [(-1, result)]

    while i < len(lines):
        line = lines[i]
        ind = indent_of(line)
        stripped = line.strip()

        while len(stack) > 1 and stack[-1][0] >= ind:
            stack.pop()
        container = stack[-1][1]

        if stripped.startswith("- "):
            item_text = stripped[2:].strip()
            if isinstance(container, list):
                if ":" in item_text and not item_text.endswith(":"):
                    k, _, v = item_text.partition(":")
                    obj = {k.strip(): _coerce_yaml_value(v.strip())}
                    while i + 1 < len(lines) and indent_of(lines[i + 1]) > ind:
                        nx = lines[i + 1].strip()
                        if ":" in nx and not nx.startswith("- "):
                            kk, _, vv = nx.partition(":")
                            obj[kk.strip()] = _coerce_yaml_value(vv.strip())
                        i += 1
                    container.append(obj)
                else:
                    container.append(_coerce_yaml_value(item_text))
        elif ":" in stripped:
            k, _, v = stripped.partition(":")
            k = k.strip()
            v = v.strip()
            if v == "":
                if i + 1 < len(lines) and indent_of(lines[i + 1]) > ind:
                    nxt = lines[i + 1].strip()
                    new_container: Any = [] if nxt.startswith("- ") else {}
                    if isinstance(container, dict):
                        container[k] = new_container
                    elif isinstance(container, list):
                        container.append({k: new_container})
                    stack.append((ind, new_container))
                else:
                    if isinstance(container, dict):
                        container[k] = ""
            else:
                pv = _coerce_yaml_value(v)
                if isinstance(container, dict):
                    container[k] = pv
        i += 1

    return result


def _coerce_yaml_value(v: str) -> Any:
    if not isinstance(v, str):
        return v
    v = v.strip()
    if v.lower() == "true":
        return True
    if v.lower() == "false":
        return False
    if v.lower() in ("null", "~", ""):
        return None if v.lower() != "" else ""
    try:
        if "." in v:
            return float(v)
        return int(v)
    except (ValueError, TypeError):
        pass
    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
        return v[1:-1]
    return v


# ───────────────────────────────────────────────────────────────────────────
# Frontmatter parsing
# ───────────────────────────────────────────────────────────────────────────

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def parse_frontmatter(text: str) -> tuple[dict, str]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    fm_raw = m.group(1)
    body = m.group(2)
    try:
        fm = _parse_simple_yaml(fm_raw)
    except Exception as e:
        log.warning("frontmatter parse failed: %s", e)
        fm = {}
    return fm, body


# ───────────────────────────────────────────────────────────────────────────
# Block + filing_type loaders
# ───────────────────────────────────────────────────────────────────────────

def load_block(relative_path: str) -> Optional[Block]:
    rel = relative_path.strip().lstrip("/")
    if rel.endswith(".md"):
        rel = rel[:-3]
    candidates = [BLOCKS_DIR / (rel + ".md"), BLOCKS_DIR / rel]
    fp = next((c for c in candidates if c.exists()), None)
    if not fp:
        log.warning("block not found: %s", rel)
        return None

    text = fp.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(text)

    return Block(
        id=fm.get("id", rel.split("/")[-1]),
        relative_path=rel,
        title=fm.get("title", ""),
        body_template=body.strip(),
        applies_to_defects=fm.get("applies_to_defects", []) or [],
        required_authorities=fm.get("required_authorities", []) or [],
        required_facts=fm.get("required_facts", []) or [],
        output_format=fm.get("output_format", "section"),
        raw_frontmatter=fm,
    )


def list_all_blocks(category: str = "") -> list[dict]:
    if not BLOCKS_DIR.exists():
        return []
    out = []
    search_dir = BLOCKS_DIR / category if category else BLOCKS_DIR
    if not search_dir.exists():
        return []
    for fp in sorted(search_dir.rglob("*.md")):
        rel_str = fp.relative_to(BLOCKS_DIR).as_posix()
        if rel_str.endswith(".md"):
            rel_str = rel_str[:-3]
        b = load_block(rel_str)
        if b:
            out.append({
                "id": b.id,
                "relative_path": b.relative_path,
                "title": b.title,
                "category": rel_str.split("/")[0] if "/" in rel_str else "",
                "applies_to_defects": b.applies_to_defects,
                "required_facts": b.required_facts,
                "required_authorities": b.required_authorities,
            })
    return out


def load_filing_type(filing_type_id: str) -> Optional[FilingType]:
    fid = filing_type_id.strip()
    for ext in (".yaml", ".yml"):
        if fid.endswith(ext):
            fid = fid[:-len(ext)]
    candidates = [
        FILING_TYPES_DIR / (fid + ".yaml"),
        FILING_TYPES_DIR / (fid + ".yml"),
    ]
    fp = next((c for c in candidates if c.exists()), None)
    if not fp:
        log.warning("filing_type not found: %s", fid)
        return None

    raw = _parse_simple_yaml(fp.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        log.warning("filing_type %s did not parse to dict", fid)
        raw = {}
    return FilingType(
        id=raw.get("id", fid),
        court=raw.get("court", ""),
        title=raw.get("title", ""),
        description=raw.get("description", ""),
        required_authorities=raw.get("required_authorities", []) or [],
        trigger_defects=raw.get("trigger_defects", []) or [],
        structure=raw.get("structure", []) or [],
        raw=raw,
    )


def list_all_filing_types() -> list[dict]:
    if not FILING_TYPES_DIR.exists():
        return []
    out = []
    for fp in sorted(FILING_TYPES_DIR.glob("*.y*ml")):
        ft = load_filing_type(fp.stem)
        if ft:
            out.append({
                "id": ft.id,
                "court": ft.court,
                "title": ft.title,
                "description": ft.description,
                "trigger_defects": ft.trigger_defects,
                "block_count": len(ft.structure),
            })
    return out


# ───────────────────────────────────────────────────────────────────────────
# Diagnosis context
# ───────────────────────────────────────────────────────────────────────────

# Registry-derived signals that suggest cross-case authority misuse even
# when the scanner returns 0. These are deterministic patterns.
_CROSS_CASE_REGISTRY_PATTERNS = [
    "motions_to_strike",
    "active_motions",
    "void_order",
    "garnishee in default",
    "unverified interrogatory",
    "default judgment without proper service",
]


def _derive_cross_case_signal_from_registry(reg_entry: dict) -> int:
    """Returns >0 if registry indicates ongoing opposing-counsel issues
    even if the cross_case scanner found nothing in vault.

    Counts hits across status, notes, engine_facts. Each hit = 1.
    """
    if not reg_entry:
        return 0
    blob = " ".join([
        str(reg_entry.get("status", "")),
        " ".join(reg_entry.get("notes", []) or []),
        " ".join(reg_entry.get("engine_facts", []) or []),
    ]).lower()
    hits = sum(1 for pat in _CROSS_CASE_REGISTRY_PATTERNS if pat in blob)
    return hits


def build_context(case_id: str) -> dict:
    """Build the diagnosis context dict used by `when` clauses + placeholders."""
    ctx = {"case_id": case_id, "is_federal": case_id.startswith("fed:")}

    # Defects
    try:
        from pipeline.procedural_attack_builder import load_and_scan_audit
        court_type = "federal" if ctx["is_federal"] else "state"
        d = load_and_scan_audit(case_id, court_type=court_type)
        ctx["defect_total"] = d.get("total", 0)
        ctx["fatal_count"] = d.get("fatal_count", 0)
        ctx["defects"] = d.get("defects", [])
        cats = {}
        for x in ctx["defects"]:
            if isinstance(x, dict):
                c = x.get("category", "OTHER")
                cats[c] = cats.get(c, 0) + 1
        ctx["defect_categories"] = cats
    except Exception as e:
        log.warning("defect load failed: %s", e)
        ctx["defect_total"] = 0
        ctx["fatal_count"] = 0
        ctx["defects"] = []
        ctx["defect_categories"] = {}

    # Cross-case scanner
    cross_case_scanner = 0
    try:
        try:
            from pipeline.cross_case_authority_check import scan as cc_scan
        except ImportError:
            from cross_case_authority_check import scan as cc_scan
        cc = cc_scan(case_id)
        cross_case_scanner = cc.get("total", 0)
        ctx["cross_case_defects"] = cc.get("defects", [])
    except Exception as e:
        log.warning("cross-case scan failed: %s", e)
        ctx["cross_case_defects"] = []

    # Strategy
    try:
        from pipeline.reasoning_orchestrator import build_strategy
        s = build_strategy(case_id, include_case_law=False)
        ctx["caption"] = s.get("caption", case_id)
        ctx["court"] = s.get("court", "")
        ctx["case_type"] = s.get("case_type", "")
        ctx["posture"] = s.get("posture", "")
        ctx["strategy"] = s
    except Exception as e:
        log.warning("strategy load failed: %s", e)
        ctx["caption"] = case_id
        ctx["strategy"] = {}

    # Registry — load the entry directly
    try:
        reg_path = ROOT / "cases_index" / "case_registry.json"
        if reg_path.exists():
            registry = json.loads(reg_path.read_text(encoding="utf-8"))
            ctx["registry"] = registry.get(case_id, {})
        else:
            ctx["registry"] = {}
    except Exception:
        ctx["registry"] = {}

    # Cross-case signal: scanner result + registry-derived fallback
    cross_case_registry = _derive_cross_case_signal_from_registry(ctx["registry"])
    ctx["cross_case_count"] = max(cross_case_scanner, cross_case_registry)
    ctx["cross_case_scanner"] = cross_case_scanner
    ctx["cross_case_registry"] = cross_case_registry

    # Convenience: fill caption from registry if strategy didn't provide it
    if not ctx.get("caption") or ctx["caption"] == case_id:
        ctx["caption"] = ctx["registry"].get("caption", case_id)
    if not ctx.get("court"):
        ctx["court"] = ctx["registry"].get("case_court", "")

    # ─── PRIORITY-1: CASE_GROUND_TRUTH.yaml overrides registry ─────────────
    # The auto-generated registry corrupts captions ("FILER VS AMERISAVE ETAL").
    # CASE_GROUND_TRUTH.yaml is human-curated and correct. Use it whenever present.
    try:
        import yaml
        # Derive slug: fed:2:26-cv-00456 → fed_26-cv-00456 (strip district number)
        slug = case_id
        if slug.startswith("fed:") and slug.count(":") == 2:
            # "fed:2:26-cv-00456" → "fed_26-cv-00456"
            parts = slug.split(":")
            slug = f"{parts[0]}_{parts[2]}"
        elif ":" in slug:
            slug = slug.replace(":", "_")
        gt_path = ROOT / "obsidian_sync" / "cases" / slug / "meta" / "CASE_GROUND_TRUTH.yaml"
        if gt_path.exists():
            gt = yaml.safe_load(gt_path.read_text(encoding="utf-8")) or {}
            if gt.get("caption"):
                ctx["caption_data"] = gt["caption"]              # keep raw dict for blocks that need it
                ctx["caption"] = _flatten_caption(gt["caption"]) # template-safe string
            if gt.get("court"):
                ctx["court_data"] = gt["court"]                  # keep raw dict
                ctx["court"] = _flatten_court(gt["court"])       # template-safe string
            if gt.get("judge"):
                ctx["judge"] = gt["judge"]
            if gt.get("case_type") and not ctx.get("case_type"):
                ctx["case_type"] = gt["case_type"]
            # Infer posture from status if strategy gave a weak/wrong one
            status = (gt.get("status") or "").lower()
            current_posture = (ctx.get("posture") or "").lower()
            # "merits" is wrong if status says appeal dismissed/pending - override
            if "appeal" in status:
                ctx["posture"] = "appellate posture (appeal filed/pending)"
            elif "dismiss" in status:
                ctx["posture"] = "post-dismissal"
            elif "judgment" in status:
                ctx["posture"] = "post-judgment"
            elif not current_posture and status:
                ctx["posture"] = status.replace("_", " ")
            # Expose factual_application dict at top level for placeholder fallback
            fa = gt.get("factual_application") or {}
            if fa:
                ctx["factual_application"] = fa
            # Expose enforcement/post-judgment fields for block placeholders
            if gt.get("garnishees"):
                ctx["garnishees"] = gt["garnishees"]
            if gt.get("latest_order"):
                ctx["latest_order"] = gt["latest_order"]
            if gt.get("pending_motions"):
                ctx["pending_motions"] = gt["pending_motions"]
            # Defendants live under caption.defendants in the YAML
            _cap = gt.get("caption") or {}
            if _cap.get("defendants"):
                ctx["defendants"] = _cap["defendants"]
            if gt.get("notes"):
                ctx["notes"] = gt["notes"]
            # Expose appellate docket from gt.appeal_number for appellate templates
            _appeal = gt.get("appeal_number")
            if _appeal:
                ctx["appellate_docket"] = _appeal
                # Also inject into registry dict so {{registry.appellate_docket}} works
                if isinstance(ctx.get("registry"), dict):
                    ctx["registry"]["appellate_docket"] = _appeal
            # Pre-rendered procedural summary for federal factual block
            _status = (gt.get("status") or "").replace("_", " ")
            _lo = gt.get("latest_order") or {}
            _lo_date = _lo.get("date", "")
            _lo_summary = _lo.get("summary", "")
            _proc_parts = []
            if _status:
                _proc_parts.append(f"The current posture is *{_status}*.")
            if _lo_date and _lo_summary:
                _proc_parts.append(f"On {_lo_date}, the Court entered the following: {_lo_summary}")
            if _appeal:
                _proc_parts.append(f"An appeal is pending before the Third Circuit (No. {_appeal}).")
            if _proc_parts:
                ctx["procedural_summary"] = " ".join(_proc_parts)
            else:
                ctx["procedural_summary"] = "Plaintiff incorporates the docket by reference."
            # === INTAKE_FACTS_LOAD_V1 ===
            # Load THIS case's distilled record so drafting sees facts, not a
            # template. slug is in scope above. Caps protect the context window.
            try:
                _cdir = ROOT / "obsidian_sync" / "cases" / slug
                _cf = _cdir / "CASE_FACTS.md"
                if _cf.exists():
                    ctx["case_facts"] = _cf.read_text(encoding="utf-8", errors="ignore")
                _tl = _cdir / "CASE_TIMELINE.json"
                if _tl.exists():
                    ctx["timeline"] = _tl.read_text(encoding="utf-8", errors="ignore")
                _PER, _TOT, _dig, _used = 1500, 24000, [], 0
                _idir = _cdir / "intake"
                if _idir.exists():
                    for _df in sorted(_idir.rglob("*.ocr.txt")):
                        if _used >= _TOT:
                            break
                        _t = _df.read_text(encoding="utf-8", errors="ignore")[:_PER]
                        _dig.append(f"### {_df.name}\n{_t}")
                        _used += len(_t)
                    if _dig:
                        ctx["intake_docs"] = "\n\n".join(_dig)
            except Exception as _e:
                log.warning("intake facts load failed: %s", _e)
            # === END INTAKE_FACTS_LOAD_V1 ===
            # Pre-rendered flat strings for block placeholders (composer has no loops)
            _defs = (gt.get("caption") or {}).get("defendants") or []
            if _defs:
                _names = [d.get("name", "") for d in _defs if d.get("name")]
                if len(_names) > 1:
                    ctx["defendants_rendered"] = ", ".join(_names[:-1]) + ", and " + _names[-1]
                elif _names:
                    ctx["defendants_rendered"] = _names[0]
            _gars = gt.get("garnishees") or []
            if _gars:
                _glines = []
                for i, g in enumerate(_gars, 1):
                    _role = (g.get("role") or "").replace("_", " ")
                    _status = (g.get("status") or "").replace("_", " ")
                    _glines.append(
                        f"{i}. **{g.get('name','')}** \u2014 {_role}. "
                        f"{g.get('basis','')} Status: *{_status}*."
                    )
                ctx["garnishees_rendered"] = "\n\n".join(_glines)
            _pm = gt.get("pending_motions") or []
            if _pm:
                _plines = []
                for m in _pm:
                    _mt = (m.get("motion_type") or "").replace("_", " ").title()
                    _dkt = f" (control no. {m['target_dkt']})" if m.get("target_dkt") else ""
                    _auth = m.get("primary_authority", "")
                    _notes = f" {m['notes']}" if m.get("notes") else ""
                    _plines.append(
                        f"{m.get('priority','')}. **{_mt}**{_dkt} \u2014 Authority: {_auth}.{_notes}"
                    )
                ctx["pending_motions_rendered"] = "\n\n".join(_plines)
            _lo = gt.get("latest_order") or {}
            if _lo:
                ctx["latest_order_date"] = _lo.get("date", "")
                ctx["latest_order_summary"] = _lo.get("summary", "")
            ctx["ground_truth"] = gt
    except Exception as e:
        log.warning("CASE_GROUND_TRUTH.yaml load failed: %s", e)
    # ─── END PRIORITY-1 ────────────────────────────────────────────────────


    # ─── PRIORITY-2 (Phase A): causes_of_action from CASE_TYPE_CLAIMS + theory_map ─
    # Per ARCHITECTURE.md core invariant: "No fact in the record → no paragraph."
    # Renders claim list from case_type, not from hardcoded template assumptions.
    try:
        import yaml as _yaml_p2
        try:
            from pipeline.case_loader import CASE_TYPE_CLAIMS as _CTC
        except Exception:
            _CTC = {}
        _case_type = ctx.get("case_type", "")
        _claim_ids = _CTC.get(_case_type, [])
        _causes = []
        if _claim_ids:
            _tm_path = ROOT / "pipeline" / "theory_map.yaml"
            _theories = {}
            if _tm_path.exists():
                try:
                    _tm = _yaml_p2.safe_load(_tm_path.read_text(encoding="utf-8")) or {}
                    _theories = _tm.get("theories", {}) or {}
                except Exception:
                    _theories = {}
            _fa_overrides = ctx.get("factual_application") or {}
            for _cid in _claim_ids:
                _theory = _theories.get(_cid, {}) or {}
                _display = _theory.get("display_name", _cid.replace("_", " ").title())
                _seeds = _theory.get("seed_nodes", []) or []
                _statutes = []
                for _s in _seeds:
                    if not isinstance(_s, str):
                        continue
                    if "-USC-" in _s:
                        _p = _s.split("-USC-")
                        _statutes.append(f"{_p[0]} U.S.C. § {_p[1]}")
                    elif "-CFR-" in _s:
                        _p = _s.split("-CFR-")
                        _statutes.append(f"{_p[0]} C.F.R. § {_p[1].replace('-', '.')}")
                    elif _s.startswith("ucc-"):
                        _statutes.append(f"U.C.C. § {_s[4:]}")
                    elif _s.startswith("pa-"):
                        _statutes.append(_s.replace("pa-", "Pa. ").replace("-", " ").title())
                    else:
                        _statutes.append(_s.replace("-", " "))
                _citation_str = "; ".join(_statutes) if _statutes else ""
                _fa_text = _fa_overrides.get(_cid) or "[Claim-specific factual application to be supplied from case record.]"
                _causes.append({
                    "id": _cid,
                    "display_name": _display,
                    "citation": _citation_str,
                    "factual_app": _fa_text,
                })
        ctx["causes_of_action"] = _causes
        if _causes:
            _lines = []
            for _i, _c in enumerate(_causes, 1):
                _heading = _c["display_name"]
                if _c["citation"]:
                    _heading = f"{_heading} ({_c['citation']})"
                _lines.append(f"{_i}.   {_heading}. {_c['factual_app']}")
            ctx["claim_list_rendered"] = "\n\n".join(_lines)
            _summary_parts = [f"{_c['display_name']}, {_c['citation']}" if _c['citation'] else _c['display_name'] for _c in _causes]
            ctx["statute_summary"] = "This action arises under " + "; ".join(_summary_parts) + "."
        else:
            ctx["claim_list_rendered"] = "[Claims to be specified.]"
            _ct_human = _case_type.replace("_", " ") if _case_type else "the matters set forth herein"
            ctx["statute_summary"] = f"This action sounds in {_ct_human}."
    except Exception as _e:
        import logging as _lp2
        _lp2.getLogger("composer").warning("causes_of_action population failed: %s", _e)
        ctx.setdefault("causes_of_action", [])
        ctx.setdefault("claim_list_rendered", "[Claims to be specified.]")
        ctx.setdefault("statute_summary", "")

    # Pa.R.A.P. 1925(b) Concise Statement: hydrate the numbered errors DATA-DRIVEN from the case's own
    # ADVERSE-ORDER ERRORS block (produced by case_facts_refresh's court-error pass, corpus-verified).
    # No hand-authoring; all-cases. Harmless for other filing types (they don't use the placeholder).
    try:
        ctx["statement_of_errors_1925b"] = _statement_of_errors_from_case_facts(slug)
    except Exception as _se:
        import logging as _lse
        _lse.getLogger("composer").warning("statement_of_errors hydration failed: %s", _se)
        ctx["statement_of_errors_1925b"] = "[Statement of errors pending case-facts analysis.]"

    # Appearance-of-partiality: ONLY drafted if the record shows a pattern. If the judge followed the
    # rules and equity, _partiality_pattern_from_case_facts returns '' -> partiality_detected is False ->
    # the block's `when: partiality_detected == true` skips it entirely. Never manufacture bias.
    try:
        _pp = _partiality_pattern_from_case_facts(slug)
    except Exception:
        _pp = ""
    ctx["partiality_pattern"] = _pp
    ctx["partiality_detected"] = bool(_pp)

    # Jurisdiction-aware citations: hydrate ctx["j_<concept>"] with the PA-state or federal cite for this
    # case's court, so blocks that reference {{j_<concept>}} render the correct authority (all cases).
    try:
        from pipeline.jurisdiction_cites import cites_for
        for _jk, _jv in cites_for(bool(ctx.get("is_federal"))).items():
            ctx["j_" + _jk] = _jv
    except Exception as _je:
        import logging as _lje
        _lje.getLogger("composer").warning("jurisdiction cites hydration failed: %s", _je)

    return ctx


def _partiality_pattern_from_case_facts(slug: str) -> str:
    """Return the objective appearance-of-partiality pattern ONLY if the record evidences it (sua sponte
    relief against us, a record-contradicted finding, an unexplained order, a judicial financial/fiduciary
    interest, or logged bias/accountability findings). If the court followed the rules and equity, return
    '' so the section is NOT drafted. Grounded, data-driven, all cases; never manufactures bias."""
    cf = ROOT / "obsidian_sync" / "cases" / slug / "CASE_FACTS.md"
    if not cf.exists():
        return ""
    text = cf.read_text(encoding="utf-8", errors="ignore")
    sig = re.compile(r"sua sponte|record[- ]?contradict|contradicted by|oath breach|C\.J\.C|Caperton|"
                     r"\brecus|financial interest|conflict of interest|bias indicator|unexplained order|"
                     r"DENIED IN ITS ENTIRETY|pecuniary|impartialit", re.I)
    if not sig.search(text):
        return ""   # judge followed the rules/equity -> no partiality section
    _appealed = _appealed_control_numbers(slug)
    parts = []
    for hdr in ("Contradictions", "Accountability routing", "ADVERSE-ORDER ERRORS"):
        mm = re.search(r"##\s*(?:\d+\)\s*)?" + re.escape(hdr) + r".*?(?=\n##\s|\Z)", text, re.S)
        if mm:
            seg = mm.group(0).strip()
            if hdr == "ADVERSE-ORDER ERRORS":
                # Scope the court-order facts to the orders ON APPEAL so a non-appealed / OCR- or sync-suspect
                # entry (e.g. a Control No. that is actually the party's OWN filing, mis-recorded as an order)
                # cannot seed a bias bullet. Same scope as the numbered errors -- one appeal, one order set.
                seg = _scope_errors_block_to_appealed(seg, _appealed)
            parts.append(seg)
    ground = "\n\n".join(parts)[:6000]
    if not ground:
        return ""
    prompt = ("From the case record below, list ONLY the objective, record-based facts that show an "
              "APPEARANCE OF PARTIALITY -- a bulleted markdown list, each bullet ONE fact drawn from the "
              "court's own orders or the record: e.g. sua sponte relief granted for a represented party "
              "against the pro-se plaintiff without a motion; a mandatory rule disregarded in the "
              "opponents' favor; a finding contradicted by the record; an unexplained one-line order "
              "denying the pro-se party; a financial or fiduciary interest of the judge in a party or the "
              "subject matter. Do NOT impugn motives, do NOT argue, do NOT invent -- only record facts, "
              "each tied to its specific order/document. If the record does not support such a pattern, "
              "output the single word NONE and nothing else.\n\n" + ground)
    try:
        from pipeline.case_facts_refresh import _call_direct
        out = re.sub(r"<think>.*?</think>", "", _call_direct(prompt, max_tokens=1400), flags=re.S | re.I).strip()
        if out.upper().startswith("NONE") or len(out) < 40:
            return ""
        mb = re.search(r"(?m)^\s*[-*]\s", out)
        return out[mb.start():].strip() if mb else out
    except Exception:
        # drafter down: omit rather than over-assert bias
        return ""


def _appealed_control_numbers(slug: str) -> set:
    """Control numbers actually ON APPEAL for this case, read from its Notice(s) of Appeal in drafts/:
    every Control No. the NoA names, MINUS any it explicitly says not to appeal / excludes. A Pa.R.A.P.
    1925(b) Concise Statement may raise errors only in the orders on appeal, so this scopes the statement
    to the appeal document (data-driven, all-cases). Returns an EMPTY set when no NoA is found or none names
    a control number -- the caller then keeps every order (correct for non-appeal filings). Never raises."""
    dd = ROOT / "obsidian_sync" / "cases" / slug / "drafts"
    if not dd.is_dir():
        return set()
    ctrl = re.compile(r"Control\s*(?:No\.?|Number)?\s*(\d{6,})", re.I)
    neg = re.compile(r"not\s+appeal|do\s+not\s+appeal|shall\s+not\s+appeal|never\s+appeal|exclud\w*", re.I)
    named, excluded = set(), set()
    for f in sorted(dd.rglob("*notice_of_appeal*")):
        if f.suffix.lower() not in (".md", ".txt"):
            continue
        try:
            t = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        named.update(ctrl.findall(t))
        for seg in re.split(r"[;.\n]", t):          # a control number in a "do NOT appeal / exclude" clause is out
            if neg.search(seg):
                excluded.update(ctrl.findall(seg))
    return named - excluded


def _scope_errors_block_to_appealed(block: str, appealed: set) -> str:
    """Keep only the ADVERSE-ORDER ERRORS entries whose Control No. is on appeal, in chronological order,
    renumbered sequentially. Empty `appealed` (no NoA) or no match => return the block unchanged (never
    empties the statement). This is the ingest-side scope that stops an un-appealed order (e.g. one the NoA
    excluded) from becoming a numbered error."""
    if not appealed:
        return block
    parts = re.split(r"(?m)^(?=###\s*\d+\.\s*Control)", block)
    head, entries = parts[0], []
    for p in parts[1:]:
        mnum = re.search(r"Control\s*(?:No\.?)?\s*(\d{6,})", p)
        if not (mnum and mnum.group(1) in appealed):
            continue
        md = re.search(r"\((\d{4}-\d{2}-\d{2})\)", p)
        entries.append((md.group(1) if md else "9999-99-99", p))
    if not entries:
        return block
    entries.sort(key=lambda e: e[0])               # chronological, matching the Notice of Appeal's ordering
    kept = [re.sub(r"^###\s*\d+\.", f"### {i}.", p, count=1) for i, (_, p) in enumerate(entries, 1)]
    return head + "".join(kept)


def _statement_of_errors_from_case_facts(slug: str) -> str:
    """Read the case's ADVERSE-ORDER ERRORS block (court-error analysis, corpus-verified) and reformat it
    into a Pa.R.A.P. 1925(b) CONCISE numbered list -- one paragraph per error, order + ignored rule +
    no-finding, no argument. LLM = formatting only; the logic/grounding is already in CASE_FACTS. Falls
    back to a deterministic reshape of the block if the drafter is unavailable, so it never blocks."""
    cf = ROOT / "obsidian_sync" / "cases" / slug / "CASE_FACTS.md"
    if not cf.exists():
        return "[Statement of errors pending case-facts analysis.]"
    text = cf.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"## ADVERSE-ORDER ERRORS.*?(?=\n## [^A]|\Z)", text, re.S)
    if not m:
        return "[Statement of errors pending ADVERSE-ORDER ERRORS analysis in CASE_FACTS.]"
    block = m.group(0)
    # SCOPE TO THE APPEAL: a Pa.R.A.P. 1925(b) statement may raise errors ONLY in the orders on appeal.
    # The Notice of Appeal defines that scope; an error in an un-appealed order is out of bounds (and its
    # inclusion can taint the statement). Filter the ADVERSE-ORDER ERRORS to the appealed control numbers,
    # data-driven from this case's NoA. Empty set (no NoA / non-appeal filing) => keep every order.
    _appealed = _appealed_control_numbers(slug)
    block = _scope_errors_block_to_appealed(block, _appealed)
    # CLOSE THE VERBATIM LEAK: route this composer path through m0's SAME mandatory-rule-text mechanism
    # (drafter_grounding.get_rule_grounding -> family-keyed rules_index), so the LLM quotes the rule's own
    # verbatim language (e.g. Pa.R.C.P. 3144(b) "twenty (20) days") and cannot confabulate a figure like
    # "30 days". Without this the composer reformatted from a weaker grounding and drifted every regen.
    _mandatory_rule_text = ""
    try:
        from pipeline import drafter_grounding as _dg
        _mandatory_rule_text = _dg.get_rule_grounding(block) or ""
    except Exception:
        _mandatory_rule_text = ""
    # GROUNDED LLM reformat (NOT deterministic parsing -- that loses context). Corpus + Obsidian + the
    # cite-keyed rules_index are what keep the LLM from muddling a rule: pull the VERBATIM rule text
    # (rules_index 'body', built from corpus) for every rule the analysis cites, and let the LLM render the
    # numbered errors grounded in that text + this case's own CASE_FACTS block. Agnostic; no hardcoding.
    grounding = _rule_grounding_for(block, text)
    # LEAD with m0's verbatim mandatory-rule-text (family-keyed) so the operative figures/language are the
    # FIRST thing the LLM sees and it quotes them, not general-knowledge substitutes.
    if _mandatory_rule_text:
        grounding = _mandatory_rule_text + "\n\n" + (grounding or "")
    # Also ground on the party's OWN prior filings for the SUBSTANTIVE obligation an order ignored (e.g. an
    # insurer's Coverage E liability making a garnishee a proper garnishee), so the error names it -- not
    # just the rule. Data-driven from the case's drafts; agnostic.
    _subst = _prior_filing_substance(slug)
    if _subst:
        grounding = (grounding or "") + ("\nSUBSTANTIVE GROUNDS from the party's own prior filings:\n"
                                         + _subst + "\n")
    # GENERAL: pull the GOVERNING LAW from the corpus for THIS case's named entities/obligations -- whatever
    # title applies (insurance/Title 40, tax/Title 26, civil rights/Title 42, UCC, ...). Query is built from
    # the case's OWN terms; the corpus decides which title. Reuses the existing retrieval; nothing hardcoded.
    _law = _corpus_law_grounding((_subst or block)[:600])
    if _law:
        grounding = (grounding or "") + "\n" + _law
    prompt = ("Reformat the court-error analysis below into a Pennsylvania Rule of Appellate Procedure "
              "1925(b) CONCISE STATEMENT OF ERRORS. Output ONLY a NUMBERED list (1., 2., 3., ...), one "
              "concise numbered paragraph per DISTINCT error. Each paragraph: name the order (date + "
              "Control No.), state the error concisely with the operative record FACT and the SPECIFIC "
              "Pennsylvania rule the court disregarded, and note the order stated no findings of fact or "
              "conclusions of law. Explicitly cite, BY NUMBER, every Pennsylvania rule the source analysis "
              "lists for that order (e.g. Pa.R.C.P. 1033 for an amendable misnomer) -- do not soften a rule "
              "into a generic phrase. Use the VERBATIM RULE TEXT below to keep each rule with its correct "
              "error and to describe what the rule requires -- never reassign a rule to a different order. "
              "Write party names and motions in full (e.g. 'the Motion to Compel and for sanctions', "
              "'Example Trust Co', 'State Farm'); no internal abbreviations. Where the GROUNDING below names a "
              "substantive obligation, its governing statutory framework, or a learned connection an order "
              "ignored (and where an order enjoined or restrained collection), fold that into the relevant "
              "error -- the grounding, not this instruction, supplies the specifics. NO argument, NO block "
              "quotes, NO "
              "prayer, NO headings, NO federal rules (this is PA state). Comprehensive (every error) yet "
              "concise (Pa.R.A.P. 1925(b)(4)(iv)). SCOPE: every order in the SOURCE ANALYSIS below is on "
              "appeal and each must appear exactly once as a numbered error; introduce NO order that is not "
              "in the SOURCE ANALYSIS (do not add any other Control No.). "
              "VERBATIM FIGURES: copy every DEADLINE, DAY-COUNT, dollar amount, date, and RULE NUMBER EXACTLY "
              "as the SOURCE ANALYSIS states it — never substitute a figure from general knowledge (if the "
              "source says a 20-day deadline under Pa.R.C.P. 3144(b), write '20 days', never '30 days'). "
              "Begin at '1.' and output only the list.\n\n"
              + grounding + "\n=== SOURCE ANALYSIS (from this case's CASE_FACTS, corpus-verified) ===\n"
              + block)
    try:
        from pipeline.case_facts_refresh import _call_direct
        out = _call_direct(prompt, max_tokens=2400)
        out = re.sub(r"<think>.*?</think>", "", out, flags=re.S | re.I).strip()
        mm = re.search(r"(?m)^\s*1[.)]\s", out)
        if mm and len(out) - mm.start() > 200:
            return out[mm.start():].strip()
    except Exception as _e:
        import logging as _lg
        _lg.getLogger("composer").warning("statement_of_errors reformat LLM unavailable: %s", _e)
    # LLM momentarily unavailable: return the grounded analysis body itself (context preserved) rather than
    # a mechanical reshape -- the caller re-renders once the drafter is back.
    return block.split("\n", 1)[1].strip() if "\n" in block else block


_RETRIEVAL = {}


def _corpus_law_grounding(query: str, k: int = 4, limit: int = 1200) -> str:
    """Pull GOVERNING SUBSTANTIVE LAW from the corpus for the case's entities/obligations, using the SAME
    semantic retrieval Hermes/Fable use (case_comprehension._retrieve). This is how the drafter connects a
    named term to its governing statute (e.g. Coverage E -> Title 40 insurance law) without hardcoding.
    Cached; best-effort (empty on any failure)."""
    if not (query or "").strip():
        return ""
    try:
        import os as _os
        _os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
        if not _RETRIEVAL:
            import json as _j, sqlite3 as _sq, turbovec
            from pipeline.case_comprehension import NEW_INDEX, NEW_IDS, CHUNK_DB, _retrieve
            from pipeline.llm_layer import get_embedder
            _RETRIEVAL["idx"] = turbovec.TurboQuantIndex.load(str(NEW_INDEX))
            _RETRIEVAL["ids"] = _j.loads(open(str(NEW_IDS), encoding="utf-8").read())
            _RETRIEVAL["con"] = _sq.connect(str(CHUNK_DB))
            _RETRIEVAL["model"] = get_embedder()
            _RETRIEVAL["fn"] = _retrieve
        hits = _RETRIEVAL["fn"](_RETRIEVAL["idx"], _RETRIEVAL["ids"], _RETRIEVAL["con"],
                                _RETRIEVAL["model"], query, k=k, prefer_authority=True) or []
        rows = ["- [%s] %s" % ((h.get("source") or "")[:24], re.sub(r"\s+", " ", h.get("text") or "")[:220])
                for h in hits[:k]]
        return ("GOVERNING LAW from the corpus (connect each named obligation/party to its governing statute "
                "and the court's duty to apply it):\n" + "\n".join(rows) + "\n") if rows else ""
    except Exception:
        return ""


def _prior_filing_substance(slug: str, limit: int = 1400) -> str:
    """Grep the party's own prior draft filings for the SUBSTANTIVE obligation behind a garnishee/party
    (e.g. an insurer's Coverage E liability) so the error can name it. Bounded; grounded in the case's
    own record; agnostic. Never raises."""
    import glob
    base = ROOT / "obsidian_sync" / "cases" / slug / "drafts"
    if not base.is_dir():
        return ""
    keys = re.compile(r"Coverage\s+E\b|garnishable|garnishee[^.\n]{0,60}obligation|credit/obligation|"
                      r"obligation[^.\n]{0,40}subject to garnishment|substantive[, ]+evidentiary dispute", re.I)
    hits, seen = [], set()
    for p in sorted(glob.glob(str(base / "*.md"))):
        try:
            t = open(p, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        for m in keys.finditer(t):
            frag = re.sub(r"\s+", " ", t[max(0, m.start() - 40):m.start() + 240]).strip()
            k = frag[:60].lower()
            if k not in seen:
                seen.add(k); hits.append("- " + frag)
            if len(hits) >= 4:
                break
        if len(hits) >= 4:
            break
    return "\n".join(hits)[:limit]


def _rule_grounding_for(block: str, case_facts_text: str = "") -> str:
    """Corpus grounding that keeps the LLM from muddling a rule. PRIMARY source = THIS case's own
    corpus-verified rule text in CASE_FACTS (its 'Controlling rules … verified against corpus' + 'Deadlines
    … verified against corpus' sections) -- that is the corpus+Obsidian truth curated for this case, and it
    is correct where a generic cite-index is not. Falls back to the cite-keyed rules_index only for a rule
    CASE_FACTS does not describe."""
    parts = []
    if case_facts_text:
        for hdr in ("Controlling rules", "Deadlines", "Money layer", "Accountability routing", "Contradictions"):
            mm = re.search(r"##\s*\d+\)\s*" + re.escape(hdr) + r".*?(?=\n##\s|\Z)", case_facts_text, re.S)
            if mm:
                parts.append(re.sub(r"\n{3,}", "\n\n", mm.group(0)).strip())
    grounding = ""
    if parts:
        grounding += ("CORPUS-VERIFIED RULE TEXT (from this case's CASE_FACTS -- authoritative; use this "
                      "wording for each rule):\n" + "\n\n".join(parts) + "\n\n")
    # Secondary: CORPUS access for any cited rule NOT already described in CASE_FACTS above -- pull the
    # rule's verbatim text straight from the corpus (chunk_text.db, the written law), which is more reliable
    # than a generic cite-index body. This is the "corpus access" that keeps the LLM grounded for any case.
    cites = re.findall(r"\bPa\.R\.(?:C\.P|E|A\.P)\.\s*\d+(?:\.\d+)?(?:\([a-z0-9]+\))*", block)
    described = grounding
    extra, seen = [], set()
    for c in cites:
        num = re.search(r"\d+(?:\.\d+)?", c)
        key = num.group(0) if num else c
        if key in seen or (num and num.group(0) in described):
            continue
        seen.add(key)
        txt = _corpus_rule_text(num.group(0) if num else c)
        if txt:
            extra.append(f"- {c}: {txt[:260]}")
    if extra:
        grounding += ("ADDITIONAL rule text from the CORPUS (written law; use verbatim for these rules):\n"
                      + "\n".join(extra) + "\n")
    return grounding


def _corpus_rule_text(num: str) -> str:
    """Pull a rule's verbatim text from the corpus (chunk_text.db) by its number -- the written law itself,
    grounded. Best-effort; returns '' if the corpus is unreachable."""
    import sqlite3
    for db in (str(_eroot() / "corpus/chunk_text.db"), str(ROOT / "chunk_text.db")):
        try:
            con = sqlite3.connect(db)
            try:
                for pat in (f"Rule {num}. ", f"Rule {num}."):
                    row = con.execute("SELECT text FROM chunk_text WHERE text LIKE ? LIMIT 1",
                                      ("%" + pat + "%",)).fetchone()
                    if row and row[0]:
                        s = re.sub(r"\s+", " ", row[0])
                        i = s.find(f"Rule {num}.")
                        return s[i:i + 300].strip() if i >= 0 else ""
            finally:
                con.close()
        except Exception:
            continue
    return ""


# ───────────────────────────────────────────────────────────────────────────
# `when` clause evaluator (DETERMINISTIC, NO eval/exec)
# ───────────────────────────────────────────────────────────────────────────

_WHEN_RE = re.compile(
    r"^\s*([a-zA-Z_][a-zA-Z0-9_.]*(?:\([^)]*\))?)\s*(==|!=|>=|<=|>|<)\s*(.+?)\s*$"
)

# `contains` operator: `lhs contains "needle"` (quoted needle) or
# `lhs contains needle` (bare needle). Placed BEFORE the comparison-operator
# regex so it takes precedence; the comparison regex would not match a bare
# `contains(...)` anyway, but keeping it first makes intent explicit.
_CONTAINS_RE = re.compile(
    r'^\s*([a-zA-Z_][a-zA-Z0-9_.]*)\s+contains\s+(?:"([^"]*)"|\'([^\']*)\'|(.+?))\s*$'
)


def evaluate_when(expr: str, ctx: dict) -> bool:
    if not expr or not expr.strip():
        return True
    expr = expr.strip()

    if expr.startswith("has_defect_category(") and expr.endswith(")"):
        cat = expr[len("has_defect_category("):-1].strip().strip('"').strip("'")
        return ctx.get("defect_categories", {}).get(cat, 0) > 0
    if expr.startswith("has_cross_case()"):
        return ctx.get("cross_case_count", 0) > 0

    cm = _CONTAINS_RE.match(expr)
    if cm:
        lhs_path = cm.group(1)
        # Quoted group (double) takes precedence, then single, then bare.
        if cm.group(2) is not None:
            needle = cm.group(2)
        elif cm.group(3) is not None:
            needle = cm.group(3)
        else:
            needle = cm.group(4)
        lhs = _resolve_path(lhs_path, ctx)
        if lhs is None:
            return False
        return str(needle).lower() in str(lhs).lower()

    m = _WHEN_RE.match(expr)
    if not m:
        log.warning("when clause unparseable: %s", expr)
        return False

    lhs_path, op, rhs_raw = m.group(1), m.group(2), m.group(3)
    lhs = _resolve_path(lhs_path, ctx)
    rhs = _coerce_yaml_value(rhs_raw)

    try:
        if op == "==":
            return lhs == rhs
        if op == "!=":
            return lhs != rhs
        if op == ">":
            return float(lhs or 0) > float(rhs or 0)
        if op == "<":
            return float(lhs or 0) < float(rhs or 0)
        if op == ">=":
            return float(lhs or 0) >= float(rhs or 0)
        if op == "<=":
            return float(lhs or 0) <= float(rhs or 0)
    except (TypeError, ValueError):
        return False
    return False


def _resolve_path(path: str, ctx: dict) -> Any:
    """Resolve dot path. NEW: handles list indexing for numeric segments.

    Examples:
      defect_categories.AUTHORITY  →  ctx['defect_categories']['AUTHORITY']
      registry.docket_aliases.0    →  ctx['registry']['docket_aliases'][0]
    """
    parts = path.split(".")
    cur: Any = ctx
    for p in parts:
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(p)
        elif isinstance(cur, list):
            # Numeric segment → list index
            try:
                idx = int(p)
                if -len(cur) <= idx < len(cur):
                    cur = cur[idx]
                else:
                    return None
            except (ValueError, TypeError):
                return None
        else:
            return None
    return cur


# ───────────────────────────────────────────────────────────────────────────
# Placeholder hydration
# ───────────────────────────────────────────────────────────────────────────

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_.]*)\s*\}\}")


def hydrate_block(block: Block, ctx: dict) -> ComposedBlock:
    filled = {}
    missing = []

    def _sub(m: re.Match) -> str:
        key = m.group(1)
        val = _resolve_path(key, ctx)
        if val is None or val == "":
            val = _resolve_path(f"registry.{key}", ctx)
        # NEW: factual_app_X → factual_application.X (from CASE_GROUND_TRUTH.yaml)
        if (val is None or val == "") and key.startswith("factual_app_"):
            sub = key[len("factual_app_"):]
            if sub == "default":
                sub = "_default"
            val = _resolve_path(f"factual_application.{sub}", ctx)
            if val is None or val == "":
                val = _resolve_path(f"strategy.factual_application.{sub}", ctx)
        if val is None or val == "":
            missing.append(key)
            return f"[MISSING: {key}]"
        filled[key] = val
        return str(val)

    rendered = _PLACEHOLDER_RE.sub(_sub, block.body_template)
    return ComposedBlock(
        block_id=block.id,
        relative_path=block.relative_path,
        title=block.title,
        rendered_text=rendered,
        placeholders_filled=filled,
        placeholders_missing=missing,
        output_format=block.output_format,
    )


# ───────────────────────────────────────────────────────────────────────────
# MAIN: compose
# ───────────────────────────────────────────────────────────────────────────

def compose_filing(case_id: str, filing_type_id: str) -> Composition:
    log.info("compose_filing: case=%s filing_type=%s", case_id, filing_type_id)

    ctx = build_context(case_id)
    ft = load_filing_type(filing_type_id)

    if not ft:
        return Composition(
            case_id=case_id,
            filing_type_id=filing_type_id,
            errors=[f"filing_type not found: {filing_type_id}"],
        )

    composition = Composition(
        case_id=case_id,
        filing_type_id=filing_type_id,
        court=ft.court,
        title=ft.title or filing_type_id.replace("_", " ").title(),
        diagnosis_snapshot={
            "defect_total": ctx.get("defect_total", 0),
            "fatal_count": ctx.get("fatal_count", 0),
            "defect_categories": ctx.get("defect_categories", {}),
            "cross_case_count": ctx.get("cross_case_count", 0),
            "cross_case_scanner": ctx.get("cross_case_scanner", 0),
            "cross_case_registry": ctx.get("cross_case_registry", 0),
            "posture": ctx.get("posture", ""),
            "case_type": ctx.get("case_type", ""),
        },
    )

    for entry in ft.structure:
        if isinstance(entry, str):
            block_path = entry
            when = ""
        elif isinstance(entry, dict):
            block_path = entry.get("block", "")
            when = entry.get("when", "")
        else:
            continue

        if not block_path:
            continue

        if when and not evaluate_when(when, ctx):
            composition.skipped_blocks.append({"block": block_path, "reason": f"when: {when}"})
            continue

        block = load_block(block_path)
        if not block:
            composition.warnings.append(f"block not loaded: {block_path}")
            continue

        composed = hydrate_block(block, ctx)
        composition.blocks.append(composed)

    log.info("compose_filing done: %d blocks composed, %d skipped",
             len(composition.blocks), len(composition.skipped_blocks))
    return composition


def render_composition_to_markdown(composition: Composition) -> str:
    lines = []
    for cb in composition.blocks:
        lines.append(cb.rendered_text)
        lines.append("")
    return "\n".join(lines)


# ───────────────────────────────────────────────────────────────────────────
# Recommend filing types
# ───────────────────────────────────────────────────────────────────────────

def recommend_filing_types(case_id: str, top_n: int = 3) -> list[dict]:
    ctx = build_context(case_id)
    all_fts = []
    for fid in (ft["id"] for ft in list_all_filing_types()):
        ft = load_filing_type(fid)
        if not ft:
            continue
        score = 0
        reasons = []
        for trigger in ft.trigger_defects:
            if trigger == "cross_case" and ctx.get("cross_case_count", 0) > 0:
                score += 5
                reasons.append(f"cross_case_signal={ctx['cross_case_count']}")
            elif "/" in trigger:
                sev, cat = trigger.split("/", 1)
                hits = sum(1 for d in ctx.get("defects", [])
                           if isinstance(d, dict)
                           and d.get("severity") == sev
                           and d.get("category") == cat)
                if hits > 0:
                    score += hits
                    reasons.append(f"{trigger}={hits}")
            else:
                hits = ctx.get("defect_categories", {}).get(trigger, 0)
                if hits > 0:
                    score += hits
                    reasons.append(f"{trigger}={hits}")
        # ─── Phase B5: posture + case_type bonuses ─────────────────
        try:
            _b5_posture = (ctx.get("posture") or "").lower()
            _b5_case_type = (ctx.get("case_type") or "").lower()
            _ft_court = (ft.court or "").lower()
            _ft_id = (ft.id if hasattr(ft, "id") and ft.id else fid) or fid
            _ft_id_l = str(_ft_id).lower()
            # Appeal-gate: skip appellate filings when no appeal pending
            _b5_gt = ctx.get("ground_truth") or {}
            _b5_has_appeal = bool(_b5_gt.get("appeal_number"))
            # === PENDING_MOTIONS_PRIORITY_V1 ===
            # The case diagnosis already ranks the correct filings by priority in
            # ground_truth.pending_motions. Honor the priority-1 recommendation so
            # m0 picks the case's own #1 filing instead of tie-breaking by name.
            try:
                _pms = _b5_gt.get("pending_motions") or []
                if _pms:
                    _ranked = sorted(
                        [m for m in _pms if isinstance(m, dict)],
                        key=lambda m: m.get("priority", 999),
                    )
                    for _rank, _pm in enumerate(_ranked):
                        _pm_type = str(_pm.get("motion_type") or "").lower()
                        if _pm_type and _pm_type == _ft_id_l:
                            # priority-1 gets the biggest boost; lower priorities less
                            _boost = max(15 - (_rank * 4), 4)
                            score += _boost
                            reasons.append(
                                f"pending_motion_priority={_pm.get('priority', _rank+1)}(+{_boost})"
                            )
                            break
            except Exception as _pm_e:
                log.warning(f"pending_motions boost failed: {_pm_e}")
            # === END PENDING_MOTIONS_PRIORITY_V1 ===
            _b5_appellate_kws = ("appellate_reply_brief", "appellate_brief_3rd_cir",
                                 "appellate_brief_pa_super", "petition_for_writ_of_mandamus")
            if (_ft_id_l in _b5_appellate_kws) and not _b5_has_appeal:
                continue
            # === APPEAL_PREDICATE_GATE_V1 ===
            # A corrective push files every motion whose posture predicate is met.
            # Appellate-class motions require an appeal to exist; on a no-appeal
            # case they are frivolous, not corrective. Cut them when appeal_number
            # is falsy. Cases WITH an appeal keep the full appellate slate.
            _is_appellate_ft = (
                "appellate" in _ft_court
                or "3d_cir" in _ft_court
                or _ft_id_l.startswith("frap_")
                or any(k in _ft_id_l for k in (
                    "appeal", "mandamus", "rehearing", "recall_mandate",
                    "reinstate", "en_banc", "cert_petition", "certiorari",
                ))
            )
            if _is_appellate_ft and not _b5_has_appeal:
                continue
            # === END APPEAL_PREDICATE_GATE_V1 ===

            if _b5_posture == "appellate":
                if ("appellate" in _ft_court or "3d_cir" in _ft_court or "pa_super" in _ft_court
                        or "reply_brief" in _ft_id_l or "mandamus" in _ft_id_l
                        or "reinstate" in _ft_id_l or "scotus" in _ft_court
                        or "appellate_brief" in _ft_id_l):
                    score += 10
                    reasons.append("posture=appellate(+10)")
            elif _b5_posture == "mandamus":
                if "mandamus" in _ft_id_l or "appellate" in _ft_court:
                    score += 10
                    reasons.append("posture=mandamus(+10)")
            elif _b5_posture == "enforcement":
                if ("garnishee" in _ft_id_l or "default" in _ft_id_l
                        or "strike" in _ft_id_l or "praecipe" in _ft_id_l):
                    score += 8
                    reasons.append("posture=enforcement(+8)")
            elif _b5_posture == "merits":
                if ("reconsideration" in _ft_id_l or "strike" in _ft_id_l
                        or "opposition" in _ft_id_l or "summary_judgment" in _ft_id_l
                        or "compel" in _ft_id_l):
                    score += 5
                    reasons.append("posture=merits(+5)")

            _b5_case_type_filings = {
                "fraud_fcra": ["motion_to_strike_federal", "motion_to_strike_state",
                               "motion_for_reconsideration", "motion_to_certify_54b",
                               "appellate_brief_3rd_cir", "appellate_reply_brief"],
                "quiet_title": ["motion_to_strike_federal", "motion_to_vacate",
                                "motion_to_remand", "appellate_brief_3rd_cir",
                                "appellate_reply_brief"],
                "bad_faith": ["petition_for_writ_of_mandamus",
                              "motion_for_reconsideration", "motion_to_vacate"],
                "contracts_other": ["motion_for_garnishee_default",
                                    "praecipe_to_enter_default",
                                    "motion_to_strike_state"],
            }
            _matching = _b5_case_type_filings.get(_b5_case_type, [])
            if _ft_id_l in [m.lower() for m in _matching]:
                score += 5
                reasons.append(f"case_type=match({_b5_case_type})")
        except Exception as _b5_e:
            log.warning(f"recommender bonus failed: {_b5_e}")
        # ─── end Phase B5 ──────────────────────────────────────────
        is_federal = ctx.get("is_federal", False)
        court_lower = ft.court.lower()
        court_compat = (
            court_lower in ("any", "")
            or (is_federal and ("federal" in court_lower))
            or (not is_federal and ("state" in court_lower or "pa_ccp" in court_lower))
        )
        if not court_compat:
            score -= 100
        all_fts.append({
            "id": ft.id,
            "title": ft.title,
            "court": ft.court,
            "score": score,
            "reasons": reasons,
        })


    # === POSTURE_TIEBREAKER_V1 ===
    # When two filings score equally close, posture decides:
    #   posture=enforcement       → boost strike, suppress reconsideration
    #   posture=attacking_judgment → boost vacate, suppress reconsideration
    #   posture=merits            → no change (reconsideration may be valid)
    try:
        posture = (ctx.get("strategy") or {}).get("posture", "")
    except Exception:
        posture = ""
    if posture in ("enforcement", "enforcing_judgment"):
        for f in all_fts:
            fid = f.get("id", "")
            if "strike" in fid or "garnishee" in fid:
                f["score"] += 10
                f.setdefault("reasons", []).append("posture=enforcement(+10)")
            elif "reconsideration" in fid:
                f["score"] -= 10
                f.setdefault("reasons", []).append("posture=enforcement(-10)")
    elif posture in ("attacking_judgment", "void"):
        for f in all_fts:
            fid = f.get("id", "")
            if "vacate" in fid or "mandamus" in fid:
                f["score"] += 10
                f.setdefault("reasons", []).append("posture=attacking_judgment(+10)")
            elif "reconsideration" in fid:
                f["score"] -= 10
                f.setdefault("reasons", []).append("posture=attacking_judgment(-10)")
    # === END POSTURE_TIEBREAKER_V1 ===

    # === PHASE2_PRIORITY_TIEBREAKER_V1 ===
    # Replace alphabetical tie-break with deterministic priority order.
    # Recusal requires an explicit bias_indicator — pure jurisdictional
    # defects do NOT count as bias.
    PRIORITY_ORDER = [
        "motion_to_strike_state",
        "motion_to_strike_federal",
        "motion_to_compel",
        "motion_for_sanctions",
        "motion_for_garnishee_default",
        "motion_to_vacate",
        "motion_to_remand",
        "petition_for_writ_of_mandamus",
        "notice_of_supplemental_authority",
        "motion_for_reconsideration",
        "motion_for_recusal",
    ]
    def _priority_rank(fid: str) -> int:
        try:
            return PRIORITY_ORDER.index(fid)
        except ValueError:
            return len(PRIORITY_ORDER) + 1

    # Recusal guard: drop motion_for_recusal unless explicit bias indicator
    # appears in the case context defects.
    has_bias = False
    try:
        for d in (ctx.get("defects") or []):
            if not isinstance(d, dict):
                continue
            blob = (str(d.get("category", "")) + " "
                    + str(d.get("severity", "")) + " "
                    + str(d.get("defect", ""))).lower()
            if any(kw in blob for kw in
                   ("bias", "prejudice", "ex parte", "appearance of impropriety",
                    "extrajudicial", "predetermined", "personal interest",
                    "financial interest")):
                has_bias = True
                break
    except Exception:
        has_bias = False

    if not has_bias:
        all_fts = [f for f in all_fts if f.get("id") != "motion_for_recusal"]

    # Sort: score DESC, then priority ASC
    all_fts.sort(key=lambda x: (-x["score"], _priority_rank(x.get("id", ""))))
    # === END PHASE2_PRIORITY_TIEBREAKER_V1 ===

    return [f for f in all_fts if f["score"] > 0][:top_n]


# ───────────────────────────────────────────────────────────────────────────
# CLI
# ───────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd")

    p_blocks = sub.add_parser("list-blocks")
    p_blocks.add_argument("--category", default="")

    p_types = sub.add_parser("list-types")

    p_recommend = sub.add_parser("recommend")
    p_recommend.add_argument("case_id")

    p_compose = sub.add_parser("compose")
    p_compose.add_argument("case_id")
    p_compose.add_argument("filing_type_id")

    p_diagnose = sub.add_parser("diagnose")
    p_diagnose.add_argument("case_id")

    p_self_test = sub.add_parser("self-test")

    args = p.parse_args()

    if args.cmd == "list-blocks":
        print(json.dumps(list_all_blocks(args.category), indent=2))
    elif args.cmd == "list-types":
        print(json.dumps(list_all_filing_types(), indent=2))
    elif args.cmd == "recommend":
        print(json.dumps(recommend_filing_types(args.case_id), indent=2))
    elif args.cmd == "compose":
        c = compose_filing(args.case_id, args.filing_type_id)
        print(json.dumps(asdict(c), indent=2, default=str))
    elif args.cmd == "diagnose":
        print(json.dumps(build_context(args.case_id), indent=2, default=str))
    elif args.cmd == "self-test":
        print(f"PyYAML available: {_PYYAML_OK}")
        print(f"BLOCKS_DIR exists: {BLOCKS_DIR.exists()}")
        print(f"FILING_TYPES_DIR exists: {FILING_TYPES_DIR.exists()}")
        all_fts = list_all_filing_types()
        print(f"Filing types loaded: {len(all_fts)}")
        for f in all_fts:
            print(f"  - {f['id']} ({f['block_count']} blocks)")
        all_blocks = list_all_blocks()
        print(f"Blocks loaded: {len(all_blocks)}")
        # Test list resolution
        test_ctx = {"registry": {"docket_aliases": ["260100000", "alias2", "alias3"]}}
        v = _resolve_path("registry.docket_aliases.0", test_ctx)
        print(f"List index test: registry.docket_aliases.0 -> {v} (expected '260100000')")
        assert v == "260100000", "list-index resolver broken"
        print("OK")
    else:
        p.print_help()
