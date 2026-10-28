#!/usr/bin/env python3
"""
case_reanalysis.py — Hermes selection layer (Phase 3 Layer C).

Three functions, each invokable from CLI or importable for MCP wiring:

  case_reanalyze(case_id, filing_type_filter=None) -> dict
      Walk all candidate drafts for a case, score each, return leaderboard.

  select_filing_from_candidates(case_id, filing_type) -> dict
      Find all candidates for a (case, filing_type) slot, score, return winner
      and ranked list.

  approve_case_ground_truth(case_id, draft_paths) -> dict
      Copy approved drafts to _approved/<slug>/ for the filing layer to use
      as canonical ground-truth.

CLI:
    python3 -m pipeline.case_reanalysis reanalyze --case-id fed:2:26-cv-00789
    python3 -m pipeline.case_reanalysis select    --case-id fed:2:26-cv-00789 --filing-type opposition_to_motion_to_dismiss
    python3 -m pipeline.case_reanalysis approve   --case-id fed:2:26-cv-00789 --paths /path1 /path2
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import argparse, json, re, shutil, sys
from datetime import datetime
from pathlib import Path
from typing import Optional

_ROOT_WSL = _eroot()
_ROOT_WIN = _eroot()
ROOT = _ROOT_WSL if _ROOT_WSL.exists() else _ROOT_WIN

CASE_MAP = {
    "fed:2:26-cv-00123":   "fed_26-cv-00123",
    "fed:2:26-cv-00456":   "fed_26-cv-00456",
    "fed:2:26-cv-00789":   "fed_26-cv-00789",
    "phila_ccp:260100000": "phila_ccp_260100000",
}

APPROVED_DIR = ROOT / "_approved"
REANALYSIS_LOG = ROOT / "_review" / "case_reanalysis.jsonl"


def _slug(case_id: str) -> str:
    return CASE_MAP.get(case_id, case_id.replace(":", "_").replace("2_", ""))


def _baseline(case_id: str) -> Optional[Path]:
    p = ROOT / "obsidian_sync" / "baseline_memos" / f"{_slug(case_id)}.md"
    return p if p.exists() else None


def _walk_candidates(case_id: str, filing_type_filter: Optional[str] = None):
    """Yield all candidate draft paths for a case: canonical _DRAFT.md +
    multi-sample candidates under _candidates/."""
    case_dir = ROOT / "obsidian_sync" / "cases" / _slug(case_id) / "drafts"
    if not case_dir.exists():
        return
    for draft in case_dir.rglob("*_DRAFT.md"):
        if filing_type_filter and filing_type_filter not in draft.name:
            continue
        yield draft


def _infer_filing_type(draft: Path) -> str:
    name = draft.stem.replace("_DRAFT", "")
    name = re.sub(r"^\d+_", "", name)
    name = re.sub(r"^candidate_\d+_(?:\d+_)?", "", name)
    return name


def _score(draft: Path, case_id: str, filing_type: str) -> dict:
    sys.path.insert(0, str(ROOT))
    from pipeline.tools.quality_score import score_draft  # type: ignore
    return score_draft(draft, case_id, filing_type, baseline_memo=_baseline(case_id))


# ============================================================================
# Public API (importable for MCP wiring)
# ============================================================================

def case_reanalyze(case_id: str, filing_type_filter: Optional[str] = None,
                   log: bool = True) -> dict:
    """Score every candidate draft for a case. Return ranked leaderboard
    grouped by filing slot."""
    rows = []
    for draft in _walk_candidates(case_id, filing_type_filter):
        ft = _infer_filing_type(draft)
        try:
            r = _score(draft, case_id, ft)
            rows.append({
                "draft":       str(draft),
                "filing_type": ft,
                "overall":     r["overall"],
                "axes":        r["axes"],
                "word_count":  r["word_count"],
                "schema":      r["diagnostics"]["schema"],
            })
        except Exception as e:
            rows.append({"draft": str(draft), "filing_type": "?", "error": str(e)})

    # Group by filing slot (parent_dir + filing_type), rank within each
    slots: dict[str, list[dict]] = {}
    for r in rows:
        slot_key = f"{Path(r['draft']).parent.name}::{r.get('filing_type', '?')}"
        slots.setdefault(slot_key, []).append(r)
    for k in slots:
        slots[k].sort(key=lambda x: x.get("overall", 0), reverse=True)

    flat = sorted(rows, key=lambda x: x.get("overall", 0), reverse=True)
    result = {
        "case_id":      case_id,
        "ts":           datetime.now().isoformat(),
        "n_drafts":     len(rows),
        "n_candidates": len(rows),       # alias for full_reanalysis orchestrator
        "n_slots":      len(slots),
        "slots":        slots,
        "flat_ranked":  flat,
        "rows":         flat,            # alias for full_reanalysis orchestrator
    }

    if log:
        REANALYSIS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(REANALYSIS_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"event": "reanalyze", "case_id": case_id,
                                "ts": result["ts"], "n_drafts": len(rows)}) + "\n")

    return result


def select_filing_from_candidates(case_id: str, filing_type: str,
                                  set_canonical: bool = False) -> dict:
    """Find candidates for a filing slot, score, return winner.

    If set_canonical=True, copy the winner over the canonical
    <slot>/<NN>_<filing_type>_DRAFT.md (the file the docx renderer reads)."""
    candidates = list(_walk_candidates(case_id, filing_type))
    if not candidates:
        return {"error": f"no candidates for {case_id} / {filing_type}"}

    scored = []
    for c in candidates:
        try:
            r = _score(c, case_id, filing_type)
            scored.append({
                "path":      str(c),
                "overall":   r["overall"],
                "axes":      r["axes"],
                "is_canonical": "_candidates" not in c.parts,
                "schema":    r["diagnostics"]["schema"],
            })
        except Exception as e:
            scored.append({"path": str(c), "error": str(e)})

    scored.sort(key=lambda r: r.get("overall", 0), reverse=True)
    winner = scored[0]

    promoted_to = None
    if set_canonical and "overall" in winner:
        # Find the canonical (non-_candidates) target in the same slot dir
        winner_path = Path(winner["path"])
        if "_candidates" in winner_path.parts:
            canonical_dir = winner_path.parent.parent
            canonical_name = re.sub(r"^candidate_\d+_", "", winner_path.name)
            canonical = canonical_dir / canonical_name
            if canonical.exists():
                shutil.copy2(winner_path, canonical)
                promoted_to = str(canonical)

    return {
        "case_id":      case_id,
        "filing_type":  filing_type,
        "ts":           datetime.now().isoformat(),
        "n_candidates": len(scored),
        "winner":       winner,
        "leaderboard":  scored,
        "promoted_to":  promoted_to,
    }


def approve_case_ground_truth(case_id: str, draft_paths: list) -> dict:
    """Copy specified drafts to _approved/<slug>/<filename> as ground truth.
    Writes a manifest with timestamp and source provenance."""
    slug = _slug(case_id)
    dest_dir = APPROVED_DIR / slug
    dest_dir.mkdir(parents=True, exist_ok=True)
    records = []
    for path in draft_paths:
        p = Path(path)
        if not p.exists():
            records.append({"src": str(p), "approved": False, "error": "missing"})
            continue
        dest = dest_dir / p.name
        shutil.copy2(p, dest)
        records.append({
            "src":      str(p),
            "dest":     str(dest),
            "approved": True,
            "size":     p.stat().st_size,
        })
    manifest = {
        "case_id":  case_id,
        "ts":       datetime.now().isoformat(),
        "approved": records,
    }
    mf_path = dest_dir / f"manifest_{datetime.now().strftime('%Y%m%dT%H%M%S')}.json"
    mf_path.write_text(json.dumps(manifest, indent=2))
    manifest["manifest_path"] = str(mf_path)
    return manifest


# ============================================================================
# CLI
# ============================================================================

def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("reanalyze", help="Score all candidates for a case")
    p1.add_argument("--case-id", required=True)
    p1.add_argument("--filing-type-filter", default=None)
    p1.add_argument("--top", type=int, default=20)

    p2 = sub.add_parser("select", help="Pick highest-scoring candidate per slot")
    p2.add_argument("--case-id", required=True)
    p2.add_argument("--filing-type", required=True)
    p2.add_argument("--set-canonical", action="store_true",
                    help="Promote winner to canonical _DRAFT.md path")

    p3 = sub.add_parser("approve", help="Mark drafts as approved ground truth")
    p3.add_argument("--case-id", required=True)
    p3.add_argument("--paths", nargs="+", required=True)

    args = ap.parse_args()

    if args.cmd == "reanalyze":
        r = case_reanalyze(args.case_id, args.filing_type_filter)
        print(f"\n==== reanalyze: {args.case_id} ====")
        print(f"  drafts scored: {r['n_drafts']}")
        print(f"  filing slots:  {r['n_slots']}")
        print(f"\n  TOP {args.top} (flat ranking):")
        for i, row in enumerate(r["flat_ranked"][:args.top], 1):
            if "error" in row:
                print(f"   {i:3d}. ERROR {row['draft']}: {row['error']}")
                continue
            short = "/".join(Path(row["draft"]).parts[-3:])
            print(f"   {i:3d}. {row['overall']:.4f}  {short}  ({row['word_count']:,} words)")
        print(f"\n  full json: --json (or read {REANALYSIS_LOG})")

    elif args.cmd == "select":
        r = select_filing_from_candidates(args.case_id, args.filing_type,
                                          set_canonical=args.set_canonical)
        if "error" in r:
            print(f"ERROR: {r['error']}")
            return 2
        print(f"\n==== select: {args.case_id} / {args.filing_type} ====")
        print(f"  candidates: {r['n_candidates']}")
        print(f"  WINNER:     {r['winner']['overall']:.4f}  {r['winner']['path']}")
        if r["promoted_to"]:
            print(f"  promoted:   {r['promoted_to']}")
        print(f"\n  leaderboard:")
        for i, row in enumerate(r["leaderboard"], 1):
            if "error" in row:
                print(f"   {i:3d}. ERROR {row['path']}: {row['error']}")
                continue
            short = "/".join(Path(row["path"]).parts[-3:])
            canon = " [canonical]" if row.get("is_canonical") else ""
            print(f"   {i:3d}. {row['overall']:.4f}  {short}{canon}")

    elif args.cmd == "approve":
        r = approve_case_ground_truth(args.case_id, args.paths)
        n_ok = sum(1 for x in r["approved"] if x["approved"])
        print(f"\n==== approve: {args.case_id} ====")
        print(f"  approved:    {n_ok}/{len(r['approved'])}")
        print(f"  manifest:    {r['manifest_path']}")
        for rec in r["approved"]:
            mark = "OK " if rec["approved"] else "FAIL"
            print(f"   {mark} {rec.get('src')}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
