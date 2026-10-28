# -*- coding: utf-8 -*-
"""
run_universal_intake.py
=======================
Top-level driver: universal intake → classify → three-track action generation.

Pipeline:
  1. universal_intake.gather()    — email + PC docs + case filings (with OCR)
  2. violation_classifier.classify_all()  — regex + agent 1 fallback
  3. email_action_generator.run_pipeline() — Track A/B/C

Usage:
    python run_universal_intake.py                              # full pipeline, all sources
    python run_universal_intake.py --sources email docs         # email + docs only
    python run_universal_intake.py --regex-only                 # no LLM fallback
    python run_universal_intake.py --no-ocr                     # skip OCR
    python run_universal_intake.py --limit-per-case 5 --limit-new-cases 10
"""

from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import argparse
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
log = logging.getLogger("universal_driver")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--sources", nargs="+",
                   default=["email", "docs", "cases"],
                   choices=["email", "docs", "cases"])
    p.add_argument("--no-ocr",    action="store_true")
    p.add_argument("--no-dedup",  action="store_true")
    p.add_argument("--no-checkpoint", action="store_true",
                   help="Don't resume from checkpoint")
    p.add_argument("--pdf-timeout", type=int, default=120,
                   help="Per-PDF timeout in seconds (default 120)")
    p.add_argument("--max-files", type=int, default=0,
                   help="Cap doc files processed (0=unlimited)")
    p.add_argument("--use-scanner", action="store_true",
                   help="Use DocumentScanner (slow up-front extraction; preserves dockets/parties)")
    p.add_argument("--max-pdf-mb", type=float, default=20.0,
                   help="Skip PDFs larger than this (MB) (default 20)")
    p.add_argument("--all-files", action="store_true",
                   help="Process ALL files (turn off evidence-only filter)")
    p.add_argument("--strict-evidence", action="store_true",
                   help="Only process files with docket/defendant/litigation keyword (no fallback)")
    p.add_argument("--mtime-months", type=int, default=18,
                   help="Skip files older than N months (0=no age filter) (default 18)")
    p.add_argument("--no-sort", action="store_true",
                   help="Don't sort files newest-first")
    p.add_argument("--incremental", action="store_true",
                   help="Email source: process only emails not seen on a prior run (checkpoint dedup)")
    p.add_argument("--regex-only", action="store_true",
                   help="Skip LLM fallback in classifier")
    p.add_argument("--no-audit",   action="store_true")
    p.add_argument("--limit-per-case",  type=int, default=0)
    p.add_argument("--limit-new-cases", type=int, default=0)
    p.add_argument("--only-cases", type=str, default="")
    p.add_argument("--skip-new-cases", action="store_true")
    args = p.parse_args()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = ROOT / "output"
    intake_path     = out / f"universal_intake_{stamp}.json"
    classified_path = out / f"classified_universal_{stamp}.json"
    packets_dir     = out / "action_packets"

    log.info("=" * 70)
    log.info("BEEZLE UNIVERSAL INTAKE — %s", stamp)
    log.info("  sources:      %s", args.sources)
    log.info("  ocr:          %s", not args.no_ocr)
    log.info("  dedup:        %s", not args.no_dedup)
    log.info("  intake out:   %s", intake_path)
    log.info("  classified:   %s", classified_path)
    log.info("  packets dir:  %s", packets_dir)
    log.info("=" * 70)

    # ------ Phase 1: Universal intake ------
    log.info("[1/4] gathering from all sources...")
    from pipeline.universal_intake import gather
    # Inject extras for gather_pc_documents via module-level config
    import pipeline.universal_intake as _ui
    _ui._GATHER_PC_KWARGS = {
        "pdf_timeout_sec":   args.pdf_timeout,
        "checkpoint":        not args.no_checkpoint,
        "max_files":         args.max_files,
        "use_scanner_cache": args.use_scanner,
        "max_pdf_mb":        args.max_pdf_mb,
        "evidence_only":     not args.all_files,
        "evidence_strict":   args.strict_evidence,
        "mtime_months":      args.mtime_months,
        "newest_first":      not args.no_sort,
    }
    intake = gather(
        sources=tuple(args.sources),
        ocr=not args.no_ocr,
        dedup=not args.no_dedup,
        output_path=intake_path,
        incremental=args.incremental,
    )
    log.info("  intake totals: %s", intake["source_counts"])

    # ------ Phase 2: Convert intake rows → email-scan shape for classifier ------
    # The classifier already handles the email_scan shape; our rows match it.
    # Wrap in the expected top-level dict for classify_all to read.
    import json as _json
    wrapped = {
        "scan_date": stamp,
        "results":   intake["rows"],
    }
    wrapped_path = out / f"intake_wrapped_{stamp}.json"
    wrapped_path.write_text(_json.dumps(wrapped, indent=2, default=str),
                            encoding="utf-8")
    log.info("  wrapped rows for classifier: %s", wrapped_path)

    # ------ Phase 3: Classify ------
    log.info("[2/4] classifying %d rows...", intake["total_rows"])
    from pipeline.violation_classifier import classify_all
    cls_summary = classify_all(
        input_path=wrapped_path,
        output_path=classified_path,
        use_llm_fallback=not args.regex_only,
    )
    log.info("  classified: %d / %d rows (unknown: %d)",
             cls_summary["classified"],
             cls_summary.get("rows_total") or cls_summary.get("total"),
             cls_summary["unknown_remaining"])

    # ------ Phase 4: Track A / B / C ------
    log.info("[3/4] running 3-track action generator...")
    from pipeline.email_action_generator import run_pipeline
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

    # ------ Summary ------
    log.info("[4/4] done.")
    log.info("=" * 70)
    log.info("UNIVERSAL INTAKE COMPLETE")
    log.info("=" * 70)
    log.info("Intake by source:")
    for src, n in (intake["source_counts"] or {}).items():
        log.info("  %-20s  %d rows", src, n)
    log.info("Classification:")
    for cat, n in sorted((cls_summary.get("by_category") or {}).items(),
                         key=lambda x: -x[1])[:15]:
        log.info("  %-40s  %d", cat, n)
    log.info("TRACK A — Existing case packets:")
    for cid, info in (manifest.get("track_a_cases") or {}).items():
        log.info("  %-30s  %3d violations  →  %s",
                 cid, info["violations"], info["folder"])
    new_cases = manifest.get("track_b_new_cases") or []
    log.info("TRACK B — New case originators: %d packets", len(new_cases))
    from collections import Counter
    by_type = Counter(p.get("originator") for p in new_cases)
    for otype, n in by_type.most_common():
        log.info("  %-40s  %d packets", otype, n)
    log.info("TRACK C — Dropped: %d rows", manifest.get("track_c_dropped_count", 0))
    log.info("")
    log.info("Outputs:")
    log.info("  intake:       %s", intake_path)
    log.info("  classified:   %s", classified_path)
    log.info("  manifest:     %s/MANIFEST.json", packets_dir)
    log.info("  case packets: %s/existing_cases/", packets_dir)
    log.info("  new cases:    %s/new_cases/", packets_dir)
    log.info("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
