#!/usr/bin/env python3
"""forum_rules_merge — feed a corpus SWEEP (m2/*_SWEEP_*.md, REASONS_DUTY_SWEEP, etc.) into scripts/rooms/forum_rules.json.

A sweep that stays in a markdown file is a hand value one step removed: the room grounds on forum_rules.json, so every
verified source must land there. Rows: {"cite", "why", "source"} under FORUM[court][type]; de-duped by cite across the
court (ground_pack --type _all unions all types). Never deletes; writes .bak.<TS> first.

usage: python3 -m pipeline.forum_rules_merge --court PA_CP_PHILA --type posture_order --sweep <file.md> [--sweep ...] [--dry]
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FR = ROOT / "scripts" / "rooms" / "forum_rules.json"

_CITE = re.compile(
    r"(Pa\.R\.A\.P\. \d+(?:\([a-z0-9]+\))*(?: Official Note)?"
    r"|Pa\.R\.C\.P\. (?:No\. )?\d+(?:\.\d+)?(?:\([a-z0-9]+\))*"
    r"|Phila\. Civ\. R\. \*?\d+(?:\.\d+)?(?:\([a-z0-9]+\))*"
    r"|42 Pa\.C\.S\.(?:A\.)? § ?\d+(?:\([a-z0-9]+\))*"
    r"|1 Pa\.C\.S\.(?:A\.)? § ?\d+"
    r"|Pa\. Const\. art\. [IVX]+,? § ?\d+"
    r"|\d{3} U\.S\. \d+"
    r"|Fed\. R\. Civ\. P\. \d+(?:\([a-z0-9]+\))*|Fed\. R\. App\. P\. \d+(?:\([a-z0-9]+\))*|28 U\.S\.C\. § ?\d+(?:\([a-z0-9]+\))*)"
)


def rows_from_sweep(path: Path) -> list[dict]:
    """One row per distinct cite; `why` = the sweep line that carries it (first occurrence, trimmed)."""
    out, seen = [], set()
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        for m in _CITE.finditer(line):
            c = re.sub(r"\s+", " ", m.group(1)).replace("Pa.C.S.A.", "Pa.C.S.").replace("§", "§ ").replace("§  ", "§ ").strip()
            if c in seen:
                continue
            seen.add(c)
            why = re.sub(r"\s+", " ", line.strip().lstrip("|-*# ")).strip()
            out.append({"cite": c, "why": why[:220], "source": f"{path.name}"})
    return out


def merge(court: str, ftype: str, sweeps: list[Path], dry: bool = False) -> dict:
    data = json.loads(FR.read_text(encoding="utf-8"))
    cell = data.setdefault(court, {})
    have = {r["cite"] for rs in cell.values() for r in (rs or []) if isinstance(r, dict)}
    bucket = cell.setdefault(ftype, [])
    added = []
    for sw in sweeps:
        for r in rows_from_sweep(sw):
            if r["cite"] not in have:
                bucket.append(r); have.add(r["cite"]); added.append(r["cite"])
    if added and not dry:
        shutil.copy(FR, str(FR) + ".bak." + time.strftime("%Y%m%d_%H%M%S"))
        FR.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    return {"court": court, "type": ftype, "added": added, "cell_total": len(have), "written": bool(added and not dry)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--court", required=True); ap.add_argument("--type", required=True)
    ap.add_argument("--sweep", action="append", required=True); ap.add_argument("--dry", action="store_true")
    a = ap.parse_args(argv)
    r = merge(a.court, a.type, [Path(s) for s in a.sweep], a.dry)
    print(json.dumps(r, indent=1, ensure_ascii=False)); return 0


if __name__ == "__main__":
    raise SystemExit(main())
