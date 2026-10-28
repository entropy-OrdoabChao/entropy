"""
pipeline/_definition_anchors.py  v2

v2 changes:
  - format_anchor_block() scales to anchor density. Thin clusters (<3 total)
    emit a skeletal block (cites + holdings only, ~500 B). Rich clusters
    (>=3 total) emit the full mandate banner block (~9 KB).
  - anchors_for() pulls statute captions from stub text via
    pipeline.statutes_registry.lookup() — autonomous.
"""
from __future__ import annotations
import re
import yaml
from pathlib import Path
from typing import Optional, List, Dict, Any

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_YAML_PATH = _PROJECT_ROOT / "obsidian_sync" / "definitions" / "definition_anchors.yaml"

_cache: Optional[Dict[str, Any]] = None

THIN_THRESHOLD = 3


def load_anchors() -> Dict[str, Any]:
    global _cache
    if _cache is not None:
        return _cache
    if not _YAML_PATH.exists():
        _cache = {
            "by_filing_type": {}, "clusters": {}, "by_keyword": {},
            "anti_conclusory_rules": [], "pin_cite_mandate": "",
        }
        return _cache
    try:
        _cache = yaml.safe_load(_YAML_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        _cache = {
            "by_filing_type": {}, "clusters": {}, "by_keyword": {},
            "anti_conclusory_rules": [], "pin_cite_mandate": "",
        }
    return _cache


def _filing_type_from_filename(motion_filename: str) -> str:
    if not motion_filename:
        return ""
    name = str(motion_filename)
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    if "\\" in name:
        name = name.rsplit("\\", 1)[-1]
    name = name.replace("_DRAFT.md", "").replace("_DRAFT", "").replace(".md", "")
    name = re.sub(r"^\d+_", "", name)
    return name


def clusters_for(filing_type: str, stub_text: Optional[str] = None) -> List[str]:
    data = load_anchors()
    by_ft = data.get("by_filing_type", {}) or {}
    by_kw = data.get("by_keyword", {}) or {}

    result: List[str] = []
    seen = set()

    for entry in (by_ft.get(filing_type, []) or []):
        c = entry.get("cluster") if isinstance(entry, dict) else entry
        if c and c not in seen:
            result.append(c)
            seen.add(c)

    if stub_text:
        s = stub_text.lower()
        for kw, names in (by_kw or {}).items():
            if kw.lower() in s:
                for c in (names or []):
                    if c and c not in seen:
                        result.append(c)
                        seen.add(c)

    return result


_STATUTE_RX = re.compile(
    r"""\b(?:
        \d+\s+U\.?\s*S\.?\s*C\.?\s*§?\s*\d+[\w()\-\.]*
      | Fed\.\s*R\.\s*(?:Civ|App|Crim|Evid|Bankr|Cv)\.\s*P\.\s*\d+[\w()\-\.]*
      | Pa\.\s*R\.\s*(?:C|A|J|E|Cv|App|Crim|Evid)\.\s*P\.\s*\d+[\w()\-\.]*
      | FRAP\s*\d+[\w()\-\.]*
      | FRCP\s*\d+[\w()\-\.]*
      | Sup\.\s*Ct\.\s*R\.\s*\d+[\w()\-\.]*
      | \d+\s+Pa\.?\s*C\.?\s*S\.?\s*§?\s*\d+[\w()\-\.]*
      | 73\s*P\.?\s*S\.?\s*§?\s*[\d\-\.]+
    )\b""",
    re.VERBOSE,
)


def _statutes_in_stub(stub_text: str, cap: int = 8) -> List[Dict[str, str]]:
    if not stub_text:
        return []
    try:
        from pipeline.statutes_registry import lookup as _stat_lookup
    except Exception:
        return []
    seen = set()
    found = []
    for m in _STATUTE_RX.finditer(stub_text):
        c = re.sub(r"\s+", " ", m.group(0).strip())
        key = c.lower().replace(" ", "")
        if key in seen:
            continue
        seen.add(key)
        try:
            summary = _stat_lookup(c)
        except Exception:
            summary = None
        if summary and summary.strip() and summary.strip().lower() != c.lower():
            found.append({"cite": c, "holding": summary.strip()})
            if len(found) >= cap:
                break
    return found


def anchors_for(motion_filename: str, stub_text: Optional[str] = None,
                section: Optional[str] = None) -> List[Dict[str, Any]]:
    ft = _filing_type_from_filename(motion_filename)
    cluster_names = clusters_for(ft, stub_text)
    data = load_anchors()
    clusters = data.get("clusters", {}) or {}
    out: List[Dict[str, Any]] = []
    for cn in cluster_names:
        items = clusters.get(cn, []) or []
        if items:
            out.append({"cluster": cn, "anchors": items})

    if stub_text:
        stat_anchors = _statutes_in_stub(stub_text)
        if stat_anchors:
            out.append({"cluster": "statutes_cited_in_stub", "anchors": stat_anchors})

    return out


def _format_cite(a: Dict[str, Any]) -> str:
    cite = a.get("cite", "")
    year = a.get("year", "")
    court = a.get("court", "")
    if year and "(" not in cite:
        paren = f"({court} {year})" if court else f"({year})"
        cite = f"{cite} {paren}"
    pin = a.get("pin", "")
    if pin:
        cite = f"{cite} at {pin}"
    return cite


def _format_skeletal(anchor_groups) -> str:
    if not anchor_groups:
        return ""
    lines = ["CONTROLLING AUTHORITY (apply with pin cites; do not paraphrase holdings):"]
    for g in anchor_groups:
        for a in g["anchors"]:
            lines.append(f"  - {_format_cite(a)}")
            holding = a.get("holding", "")
            if holding:
                lines.append(f"      Holds: {holding[:200]}")
            apply_as = a.get("apply_as", "")
            if apply_as:
                lines.append(f"      Apply as: {apply_as[:120]}")
    lines.append("")
    return "\n".join(lines)


def _format_full(anchor_groups, include_pin_mandate: bool, include_anti_conclusory: bool,
                 include_element_walkthrough: bool) -> str:
    data = load_anchors()
    bar = "=" * 76
    lines = []
    lines.append(bar)
    lines.append("CONTROLLING DEFINITIONS — APPLY VERBATIM; DO NOT PARAPHRASE THE HOLDINGS")
    lines.append(bar)
    lines.append("")
    lines.append(
        "Before drafting, anchor every legal term used in your argument to the "
        "controlling authorities below. Quote the operative definition with a pin "
        "cite. Do NOT use legal terms (\"willful\", \"reasonable\", \"void\", "
        "\"fatal\", \"misapplied\") without anchoring them to one of these "
        "definitions first. This converts the filing from advocacy into rule "
        "application — the single best defense against a frivolous/vexatious "
        "characterization."
    )
    lines.append("")

    for group in anchor_groups:
        label = group["cluster"].upper().replace("_", " ")
        lines.append(f"--- {label} ---")
        for a in group["anchors"]:
            lines.append(f"  - {_format_cite(a)}")
            holding = a.get("holding", "")
            if holding:
                lines.append(f"      Holds: {holding}")
            apply_as = a.get("apply_as", "")
            if apply_as:
                lines.append(f"      Apply as: {apply_as}")
        lines.append("")

    if include_pin_mandate:
        mandate = (data.get("pin_cite_mandate") or "").rstrip()
        if mandate:
            lines.append(bar)
            lines.append("PIN-CITATION MANDATE")
            lines.append(bar)
            lines.append(mandate)
            lines.append("")

    if include_anti_conclusory:
        rules = data.get("anti_conclusory_rules") or []
        if rules:
            lines.append(bar)
            lines.append("ANTI-CONCLUSORY PHRASING — RULE APPLICATION, NOT OPINION")
            lines.append(bar)
            for r in rules:
                bad = r.get("bad", "")
                good = r.get("good", "")
                if bad and good:
                    lines.append(f"  - replace '{bad}' with '{good}'")
            lines.append("")

    if include_element_walkthrough:
        lines.append(bar)
        lines.append("ELEMENT WALKTHROUGH MANDATE")
        lines.append(bar)
        lines.append(
            "For each claim asserted, enumerate the prima facie elements in "
            "order. For each element, identify the specific fact in the record "
            "that satisfies it (with ECF cite). Do not assert a claim without "
            "this element-by-element grounding."
        )
        lines.append("")

    lines.append(bar)
    lines.append("END CONTROLLING DEFINITIONS — proceed to the section/user prompt below")
    lines.append(bar)
    return "\n".join(lines)


def format_anchor_block(anchor_groups, include_pin_mandate: bool = True,
                        include_anti_conclusory: bool = True,
                        include_element_walkthrough: bool = True) -> str:
    if not anchor_groups:
        return ""

    total_anchors = sum(len(g.get("anchors", []) or []) for g in anchor_groups)

    if total_anchors < THIN_THRESHOLD:
        return _format_skeletal(anchor_groups)

    return _format_full(anchor_groups, include_pin_mandate, include_anti_conclusory,
                        include_element_walkthrough)
