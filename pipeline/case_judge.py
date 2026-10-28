#!/usr/bin/env python3
"""case_judge — resolve the presiding judge for a case FROM THE CASE FILE, never from a literal.

Source order (first hit wins; every hit carries its provenance):
  1. obsidian_sync/cases/<slug>/CASE_FACTS.md        "- **Judge**: Name" / "**Judge:** Name"
  2. obsidian_sync/cases/<slug>/intake/DOCKET_REPORT_*.txt   "Judge: Name" / "Assigned Judge ..." / "Team Leader"
  3. obsidian_sync/cases/<slug>/record/orders/*.txt (+ intake order .txt)  "/s/ Name, J." / "BY THE COURT: /s/ Name"
  4. cases_index/case_registry.json[...].judge  ONLY when judge_source names a record file (a mirror, not a hand value)
Returns {"judge": str|None, "source": str}. A None judge is a data gap: run efiling/docket_pull.py --case <slug>
--write-timeline (FJD) or the PACER docket pull; do NOT type a name anywhere.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

_FACTS = (r"^\s*-?\s*\*\*Judge\*\*\s*:\s*(.+?)\s*$", r"^\s*\*\*Judge:\*\*\s*(.+?)\s*$", r"^\s*Judge:\s*([A-Z][^\n|]{2,60})$")
_DOCKET = (r"(?:Assigned\s+)?Judge\s*[:\-]\s*([A-Z][A-Za-z.,'\- ]{2,60}?)(?:\s{2,}|\n|$)",
           r"Team\s+Leader\s*[:\-]\s*([A-Z][A-Za-z.,'\- ]{2,60}?)(?:\s{2,}|\n|$)",
           r"(?:before|Hon\.|Honorable)\s+([A-Z][A-Za-z.'\- ]{2,40})(?:,\s*J\.|\s*$)")
_ORDER = (r"/s/\s*([A-Z][A-Za-z.'\- ]{2,40}?)\s*,\s*J\.", r"BY\s+THE\s+COURT:\s*\n?\s*/s/\s*([A-Z][A-Za-z.'\- ]{2,40})",
          r"BY\s+THE\s+COURT:\s*\n?\s*(?!_)([A-Z][a-z]+(?:\s[A-Z]\.)?\s[A-Z][a-z]+)\s*,?\s*J\.")


def _clean(n: str) -> str:
    n = re.sub(r"\s*\((?:[A-Z]{2,4})\)\s*$", "", n.strip())      # "(GJP)" initials tag
    n = re.sub(r"^(?:the\s+)?(?:Hon\.|Honorable|Judge)\s+", "", n, flags=re.I)
    return n.strip(" .,")


def slug_for(root: Path, case_id: str) -> str | None:
    """canonical_id / registry key / bare docket -> vault slug, via case_map.json (the configured map) then registry."""
    root = Path(root)
    try:
        cm = json.loads((root / "cases_index" / "case_map.json").read_text(encoding="utf-8"))
    except Exception:
        cm = {}
    bare = case_id.split(":", 1)[-1]
    for k, v in cm.items():
        if isinstance(v, dict) and (v.get("canonical_id") == case_id or str(v.get("case_id")) == bare or str(v.get("docket")) == bare):
            if (root / "obsidian_sync" / "cases" / k).is_dir():
                return k
    try:
        reg = json.loads((root / "cases_index" / "case_registry.json").read_text(encoding="utf-8"))
    except Exception:
        reg = {}
    for k, v in reg.items():
        if isinstance(v, dict) and (k == case_id or v.get("canonical_id") == case_id):
            for cand in (v.get("slug"), (v.get("folder_path") or "").rstrip("/").split("/")[-1], k):
                if cand and (root / "obsidian_sync" / "cases" / cand).is_dir():
                    return cand
    return case_id if (root / "obsidian_sync" / "cases" / case_id).is_dir() else None


def resolve(root: Path, slug: str) -> dict:
    root = Path(root)
    if not slug or not (root / "obsidian_sync" / "cases" / slug).is_dir():
        s2 = slug_for(root, slug) if slug else None
        if not s2:
            return {"judge": None, "source": f"no case folder for {slug!r} — fix cases_index/case_map.json"}
        slug = s2
    cdir = root / "obsidian_sync" / "cases" / slug
    facts = cdir / "CASE_FACTS.md"
    if facts.exists():
        for line in facts.read_text(encoding="utf-8", errors="ignore").splitlines():
            for rx in _FACTS:
                m = re.match(rx, line)
                if m and not re.search(r"unknown|n/?a|TBD|none", m.group(1), re.I):
                    return {"judge": _clean(m.group(1)), "source": f"{facts.relative_to(root)} ('{line.strip()[:60]}')"}
    for rep in sorted((cdir / "intake").glob("DOCKET_REPORT_*.txt"), reverse=True):
        t = rep.read_text(encoding="utf-8", errors="ignore")
        for rx in _DOCKET:
            m = re.search(rx, t)
            if m:
                return {"judge": _clean(m.group(1)), "source": f"{rep.relative_to(root)}"}
    orders = sorted((cdir / "record" / "orders").glob("*.txt"), reverse=True) + sorted((cdir / "intake").glob("*order*.txt"), reverse=True)
    for o in orders:
        t = o.read_text(encoding="utf-8", errors="ignore")
        for rx in _ORDER:
            m = re.search(rx, t)
            if m:
                return {"judge": _clean(m.group(1)), "source": f"{o.relative_to(root)} (signature line)"}
    regp = root / "cases_index" / "case_registry.json"
    if regp.exists():
        for v in json.loads(regp.read_text(encoding="utf-8")).values():
            if isinstance(v, dict) and slug in (v.get("slug"), (v.get("folder_path") or "").rstrip("/").split("/")[-1]) and v.get("judge"):
                src = str(v.get("judge_source") or "")
                if re.search(r"\.(pdf|txt|md|json)\b|DOCKET|docket|ECF|Dkt", src):
                    return {"judge": _clean(v["judge"]), "source": f"case_registry mirror <- {src[:80]}"}
                return {"judge": None, "source": f"case_registry has judge={v['judge']!r} but no record source ({src[:60] or 'none'}) — IGNORED"}
    return {"judge": None, "source": "NOT IN CASE FILE — run the docket pull (efiling/docket_pull.py --case <slug> --write-timeline)"}


if __name__ == "__main__":
    import sys
    print(json.dumps(resolve(Path(sys.argv[1]) if len(sys.argv) > 2 else Path("."), sys.argv[-1]), indent=1))
