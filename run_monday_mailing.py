# -*- coding: utf-8 -*-
"""
run_monday_mailing.py
=====================
One-shot driver for Monday's mailing. Runs the full sequence:

  1. violation_classifier → classify 1,870 unknowns
  2. case_audit_engine    → audit all 4 cases (judicial_order_scanner + Agent 10)
  3. email_action_generator → full 10-agent chain → action packets
  4. Prints summary + file locations

Place in: <home>\\run_monday_mailing.py   (repo root, NOT inside pipeline)

Usage:
    python run_monday_mailing.py
    python run_monday_mailing.py --no-audit             # skip case audit
    python run_monday_mailing.py --limit-per-case 20    # cap violations per case
    python run_monday_mailing.py --regex-only           # classifier: regex, no LLM fallback
"""

from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

ROOT = _eroot()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("monday_mailing")


def _find_latest_email_scan() -> Path:
    scan_dir = ROOT / "output" / "email_scans"
    candidates = sorted(scan_dir.glob("email_scan_*.json"))
    if not candidates:
        raise SystemExit(f"no email_scan_*.json files found in {scan_dir}")
    return candidates[-1]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--input", type=str, default=None, help="email_scan JSON (defaults to latest)")
    p.add_argument("--regex-only", action="store_true", help="classifier skips Agent 1 LLM fallback")
    p.add_argument("--no-audit", action="store_true")
    p.add_argument("--limit-per-case", type=int, default=0)
    p.add_argument("--limit-new-cases", type=int, default=0,
                   help="cap TRACK B originator rows processed")
    p.add_argument("--only-cases", type=str, default="")
    p.add_argument("--skip-new-cases", action="store_true",
                   help="skip TRACK B (new case originators)")
    args = p.parse_args()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out   = ROOT / "output"
    out.mkdir(parents=True, exist_ok=True)

    input_path      = Path(args.input) if args.input else _find_latest_email_scan()
    classified_path = out / f"classified_violations_{stamp}.json"
    packets_dir     = out / "action_packets"

    log.info("=" * 70)
    log.info("BEEZLE MONDAY MAILING — %s", stamp)
    log.info("  email scan:   %s", input_path)
    log.info("  classified:   %s", classified_path)
    log.info("  packets dir:  %s", packets_dir)
    log.info("=" * 70)

    # 1) Classify
    from pipeline.violation_classifier import classify_all
    log.info("[1/3] classifying violations...")
    cls_summary = classify_all(
        input_path=input_path,
        output_path=classified_path,
        use_llm_fallback=not args.regex_only,
    )
    log.info("  → %d classified / %d unknown",
             cls_summary["classified"], cls_summary["unknown_remaining"])

    # 2) Audit (optional)
    if not args.no_audit:
        from pipeline.case_audit_engine import audit_all
        log.info("[2/3] auditing all cases...")
        audit_all()
    else:
        log.info("[2/3] skipping case audit")

    # 3) Generate packets
    from pipeline.email_action_generator import run_pipeline
    log.info("[3/3] running full 10-agent chain → packets...")
    only = [c.strip() for c in args.only_cases.split(",") if c.strip()]
    manifest = run_pipeline(
        classified_path=classified_path,
        registry_path=ROOT / "cases_index" / "case_registry.json",
        output_dir=packets_dir,
        limit_per_case=args.limit_per_case,
        limit_new_cases=args.limit_new_cases,
        only_cases=only or None,
        skip_new_cases=args.skip_new_cases,
    )

    # Final summary
    log.info("=" * 70)
    log.info("MONDAY MAILING READY")
    log.info("=" * 70)
    log.info("  Manifest:           %s", packets_dir / "MANIFEST.json")
    log.info("  TRACK A — Existing case packets:")
    for cid, info in (manifest.get("track_a_cases") or {}).items():
        log.info("    %-30s  %3d violations  →  %s",
                 cid, info["violations"], info["folder"])
    new_cases = manifest.get("track_b_new_cases") or []
    log.info("  TRACK B — New case originators: %d packets", len(new_cases))
    from collections import Counter
    by_type = Counter(p.get("originator") for p in new_cases)
    for otype, n in by_type.most_common():
        log.info("    %-35s  %d packets", otype, n)
    log.info("  TRACK C — Dropped: %d rows", manifest.get("track_c_dropped_count", 0))
    log.info("")
    log.info("  Chain logs:         %s", ROOT / "logs" / "chains")
    log.info("  Obsidian chains:    %s", ROOT / "obsidian_sync" / "chains")
    log.info("  Case audits:        %s", ROOT / "output" / "case_audits")
    log.info("=" * 70)
    log.info("Next steps:")
    log.info("  1. Open each <case>/00_CASE_PACKET.md to review")
    log.info("  2. Each violation packet has 00_PREAMBLE.md + draft text")
    log.info("  3. Print, sign, certified-mail")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
