#!/usr/bin/env python3
"""organ_parity.py — shadow-parity test: organ SEAT vs the m0 organ on the SAME inputs.

Mirrors the 2026-09-04 router harness (3-vote majority, substance-over-label scoring, deterministic scorer).
The seat is never consumed by the pipeline until it is equal-or-better on 3 consecutive real matters.

Usage:
  python3 -m harness.organ_parity quality_review <draft1.md> [<draft2.md> ...] [--votes 3] [--type complaint] [--court X]
          [--m0-lane URL --m0-model ID]  [--seat-lane URL --seat-model ID]

For each draft:  m0 verdict (the pipeline's own module, on its configured lane)  vs  seat verdict (organ_seat on its lane),
each run `--votes` times; majority verdict + median axis scores compared. Ledger appended to
$ENTROPY_HOME/output/audit/organ_parity.jsonl; summary printed.

Scoring (deterministic):
  verdict_match  : majority PASS/DEFECTS agree
  axis_delta     : mean |seat_score - m0_score| over the 9 axes (<= 0.75 counts as parity)
  defect_overlap : Jaccard of (axis, where[:40]) tuples — do they flag the same places?
"""
from __future__ import annotations
import argparse, json, os, statistics, subprocess, sys, time
from collections import Counter
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG))
from core.entropy_paths import entropy_root  # noqa: E402

LEDGER = entropy_root() / "output" / "audit" / "organ_parity.jsonl"


def run_m0(organ: str, draft: str, a) -> dict:
    env = dict(os.environ)
    if a.m0_lane:
        env["BEEZLE_LLAMA_URL"] = a.m0_lane.rstrip("/") + ("" if a.m0_lane.endswith("/chat/completions") else "/v1/chat/completions")
    if a.m0_model:
        env["BEEZLE_CF_MODEL"] = a.m0_model
    mod = {"quality_review": "pipeline.m0_quality_review"}[organ]
    cmd = [sys.executable, "-m", mod, draft, "--type", a.type] + (["--court", a.court] if a.court else [])
    t0 = time.time()
    p = subprocess.run(cmd, cwd=str(PKG), env=env, capture_output=True, text=True, timeout=1800)
    side = Path(draft).with_suffix(".quality.json")
    data = json.loads(side.read_text()) if side.exists() else {"verdict": "NO_OUTPUT", "stderr": p.stderr[-500:]}
    data["_seconds"] = round(time.time() - t0, 1)
    return data


def run_seat(organ: str, draft: str, a) -> dict:
    out = Path(draft).with_suffix(f".{organ}.seat.json")
    cmd = [sys.executable, "-m", "harness.organ_seat", "run", organ, draft, "--type", a.type, "--out", str(out)]
    if a.court: cmd += ["--court", a.court]
    if a.seat_lane: cmd += ["--lane", a.seat_lane]
    if a.seat_model: cmd += ["--model", a.seat_model]
    p = subprocess.run(cmd, cwd=str(PKG), capture_output=True, text=True, timeout=1800)
    if not out.exists():
        return {"verdict": "NO_OUTPUT", "stderr": p.stderr[-500:]}
    side = json.loads(out.read_text())
    d = side.get("result", {}); d["_seconds"] = side.get("seconds")
    return d


def majority(runs: list[dict], key="verdict") -> str:
    c = Counter(str(r.get(key)) for r in runs)
    return c.most_common(1)[0][0] if c else "NONE"


def median_scores(runs: list[dict]) -> dict:
    axes = set()
    for r in runs: axes |= set((r.get("scores") or {}).keys())
    out = {}
    for ax in axes:
        vals = [r["scores"][ax] for r in runs if isinstance((r.get("scores") or {}).get(ax), (int, float))]
        if vals: out[ax] = statistics.median(vals)
    return out


def defect_keys(runs: list[dict]) -> set:
    """(axis, count/paragraph number) — the LOCATION a reviewer points at, normalised so 'Count I, Paragraph 32' and
    'Para. 32 (Count I)' and '¶32' compare equal. Reviewers legitimately phrase 'where' differently."""
    import re as _re
    keys = set()
    for r in runs:
        for d in r.get("defects") or []:
            if isinstance(d, dict):
                w = str(d.get("where", "")).lower()
                nums = _re.findall(r"(?:paragraph|para\.?|¶)\s*(\d+)", w) or _re.findall(r"\b(\d{1,3})\b", w)
                loc = "p" + nums[0] if nums else _re.sub(r"[^a-z]+", "", w)[:24]
                keys.add((str(d.get("axis", "")).lower(), loc))
    return keys


def score(m0: list[dict], seat: list[dict]) -> dict:
    vm, vs = majority(m0), majority(seat)
    sm, ss = median_scores(m0), median_scores(seat)
    common = [ax for ax in sm if ax in ss]
    delta = statistics.mean(abs(ss[ax] - sm[ax]) for ax in common) if common else None
    km, ks = defect_keys(m0), defect_keys(seat)
    jac = (len(km & ks) / len(km | ks)) if (km | ks) else 1.0
    parity = (vm == vs) and (delta is None or delta <= 1.0)
    return {"m0_verdict": vm, "seat_verdict": vs, "verdict_match": vm == vs, "axis_delta": delta, "defect_overlap": round(jac, 2),
            "m0_scores": sm, "seat_scores": ss, "parity": parity,
            "m0_seconds": [r.get("_seconds") for r in m0], "seat_seconds": [r.get("_seconds") for r in seat]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("organ"); ap.add_argument("drafts", nargs="+")
    ap.add_argument("--votes", type=int, default=3); ap.add_argument("--type", default="complaint"); ap.add_argument("--court", default="")
    ap.add_argument("--m0-lane", default=""); ap.add_argument("--m0-model", default="")
    ap.add_argument("--seat-lane", default=""); ap.add_argument("--seat-model", default="")
    ap.add_argument("--skip-m0", action="store_true", help="reuse an existing <stem>.quality.json as the m0 side")
    a = ap.parse_args()
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    results = []
    for draft in a.drafts:
        m0_runs, seat_runs = [], []
        for v in range(a.votes):
            if a.skip_m0:
                side = Path(draft).with_suffix(".quality.json")
                m0_runs.append(json.loads(side.read_text()) if side.exists() else {"verdict": "NO_OUTPUT"})
            else:
                m0_runs.append(run_m0(a.organ, draft, a))
            seat_runs.append(run_seat(a.organ, draft, a))
            print(f"  vote {v+1}/{a.votes}: m0={m0_runs[-1].get('verdict')} seat={seat_runs[-1].get('verdict')}", file=sys.stderr)
        sc = score(m0_runs, seat_runs)
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "organ": a.organ, "draft": draft, "votes": a.votes,
               "seat_lane": a.seat_lane or os.environ.get("ENTROPY_LANE_URL", ""), "seat_model": a.seat_model or os.environ.get("ENTROPY_MODEL", ""), **sc}
        with LEDGER.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        results.append(row)
        print(f"{Path(draft).name}: m0={sc['m0_verdict']} seat={sc['seat_verdict']} match={sc['verdict_match']} "
              f"axis_delta={sc['axis_delta']} defect_overlap={sc['defect_overlap']} -> {'PARITY' if sc['parity'] else 'MISS'}")
    n = sum(1 for r in results if r["parity"])
    print(f"\n{n}/{len(results)} at parity · ledger {LEDGER}")
    return 0 if n == len(results) else 2


if __name__ == "__main__":
    sys.exit(main())
