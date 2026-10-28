# -*- coding: utf-8 -*-
"""
email_action_generator.py (v3 — three-track)
============================================
TRACK A: Case-matched violations       → feed existing case
TRACK B: New-case originators          → demand + complaint + agency referral
TRACK C: Noise (already filtered by classifier)
"""

from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import argparse
import json
import logging
import re
import sys
from collections import defaultdict, Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = _eroot()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.chain_runner import ChainRunner
from pipeline.originator_taxonomy import match_originators, get_originator
from pipeline.new_case_originator import generate_originator_packet

log = logging.getLogger("email_action_generator")
if not log.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

HIGH_SEVERITY = {
    "PRIOR_RESTRAINT_FILING_BAN", "DO_NOT_FILE_LANGUAGE", "EX_PARTE_RULING",
    "DENIED_OPPORTUNITY_TO_RESPOND", "ACCESS_TO_COURTS_DENIAL",
    "VEXATIOUS_DESIGNATION_NO_PROCESS", "DEPRIVATION_RIGHTS_COLOR_OF_LAW",
    "FRAUD_ON_COURT_JUDICIAL", "RICO_PATTERN", "DISMISSED_WITH_PREJUDICE",
    "IMPROPER_CONVERSION_12_TO_56", "SANCTIONS_NO_SAFE_HARBOR",
    "REMAND_DENIED", "IFP_DENIAL_CONSTRUCTIVE",
}


def _load_registry(path: Path) -> Dict[str, Dict[str, Any]]:
    if not path.exists():
        log.warning("case_registry not found: %s", path)
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        if "cases" in data and isinstance(data["cases"], list):
            return {c.get("case_id") or c.get("id"): c for c in data["cases"]}
        return data
    if isinstance(data, list):
        return {c.get("case_id") or c.get("id"): c for c in data}
    return {}


def _build_case_matchers(registry):
    STOPWORDS = {"FILER", "ETAL", "AND", "THE", "VS", "LLC", "INC", "CORP",
                 "COMPANY", "CO", "LTD", "LP", "PLC", "INFORMATION", "SERVICES",
                 "MORTGAGE", "CORPORATION", "INSURANCE", "BANK", "NA", "USA"}
    matchers = []
    for cid, meta in registry.items():
        if not cid:
            continue
        patterns = set([cid])
        for m in re.finditer(r"(\d:\d{2}-cv-\d{4,5})", cid): patterns.add(m.group(1))
        for m in re.finditer(r"(\d{2}-cv-\d{4,5})", cid):     patterns.add(m.group(1))
        for m in re.finditer(r"(\d{6,10})", cid):             patterns.add(m.group(1))
        for part in cid.split(":"):
            if re.match(r"^\d", part or ""):
                patterns.add(part)
        title = (meta.get("title") or meta.get("name") or meta.get("caption") or "").upper()
        for tok in re.findall(r"[A-Z][A-Z0-9]{3,}", title):
            if tok not in STOPWORDS:
                patterns.add(tok)
        for field in ("defendants", "parties", "aliases", "tags"):
            v = meta.get(field)
            if isinstance(v, list):
                for item in v:
                    if isinstance(item, str):
                        for tok in re.findall(r"[A-Z][A-Z0-9]{3,}", item.upper()):
                            if tok not in STOPWORDS:
                                patterns.add(tok)
        patterns.discard("")
        regexes = [(p, re.compile(r"\b" + re.escape(p) + r"\b", re.IGNORECASE))
                   for p in patterns]
        matchers.append((cid, regexes, meta))
    return matchers


def _match_case(item: Dict[str, Any], matchers) -> Optional[str]:
    mc = item.get("matched_case")
    if mc:
        for cid, _, _ in matchers:
            if cid == mc or mc in cid or cid in mc:
                return cid
        return mc
    for k in ("case_id", "case_number", "docket"):
        v = item.get(k)
        if v:
            for cid, _, _ in matchers:
                if cid == v or str(v) in cid or cid in str(v):
                    return cid
    refs = item.get("case_references") or []
    if isinstance(refs, list):
        for ref in refs:
            ref_s = str(ref)
            for cid, regexes, _ in matchers:
                if cid == ref_s or ref_s in cid or cid in ref_s:
                    return cid
                for _name, rx in regexes:
                    if rx.search(ref_s):
                        return cid
    parts = []
    def _flatten(o):
        if isinstance(o, str): parts.append(o)
        elif isinstance(o, dict):
            for vv in o.values(): _flatten(vv)
        elif isinstance(o, list):
            for vv in o: _flatten(vv)
    _flatten(item)
    blob = " ".join(parts)
    for cid, regexes, _ in matchers:
        for _name, rx in regexes:
            if rx.search(blob):
                return cid
    return None


def _row_text(row: Dict[str, Any]) -> str:
    parts = [row.get("subject") or "", row.get("body_preview") or ""]
    v = row.get("violation") or {}
    for k in ("type", "text", "snippet", "description"):
        if isinstance(v.get(k), str):
            parts.append(v[k])
    return "\n".join(parts)


def run_pipeline(
    classified_path: Path,
    registry_path: Path,
    output_dir: Path,
    limit_per_case: int = 0,
    limit_new_cases: int = 0,
    only_cases: Optional[List[str]] = None,
    skip_new_cases: bool = False,
) -> Dict[str, Any]:
    data = json.loads(classified_path.read_text(encoding="utf-8"))
    rows = data["results"] if isinstance(data, dict) and "results" in data else data
    if not isinstance(rows, list):
        log.error("unexpected classified format")
        return {}

    registry = _load_registry(registry_path)
    matchers = _build_case_matchers(registry)
    log.info("registry: %d cases", len(registry))

    # =========================================================================
    # TRACK SPLIT
    # =========================================================================
    case_buckets:  Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    originator_rows: List[Dict[str, Any]] = []   # Track B candidates
    dropped = 0

    for row in rows:
        primary = (row.get("primary_category") or "").upper()

        # TRACK A: already classified + matches an existing case
        cid = _match_case(row, matchers)
        has_known_category = primary and primary != "UNKNOWN"

        if cid and has_known_category:
            row["_track"] = "A"
            case_buckets[cid].append(row)
            continue

        # TRACK B: check originator taxonomy (new case candidates)
        text = _row_text(row)
        v = row.get("violation") or {}
        vtypes = [v.get("type"), v.get("violation_type"), primary]
        originators = match_originators(text, existing_violation_types=vtypes)

        if originators:
            row["_track"] = "B"
            row["_originators"] = originators
            originator_rows.append(row)
            continue

        # Fallback: case-matched but no known category → Track A anyway (let chain figure it out)
        if cid:
            row["_track"] = "A"
            case_buckets[cid].append(row)
            continue

        dropped += 1

    log.info("TRACK A (existing cases):  %d rows across %d cases",
             sum(len(v) for v in case_buckets.values()), len(case_buckets))
    log.info("TRACK B (new originators): %d rows", len(originator_rows))
    log.info("TRACK C (dropped):         %d rows", dropped)

    for cid, v in case_buckets.items():
        log.info("  A: %s → %d", cid, len(v))

    orig_counter = Counter()
    for r in originator_rows:
        for o in r.get("_originators", []):
            orig_counter[o] += 1
    if orig_counter:
        log.info("  B originators:")
        for o, n in orig_counter.most_common():
            log.info("    %s → %d", o, n)

    runner = ChainRunner(vault=True, log_each=False)
    output_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "generated":             datetime.now().isoformat(),
        "input":                 str(classified_path),
        "track_a_cases":         {},
        "track_b_new_cases":     [],
        "track_c_dropped_count": dropped,
    }

    # =========================================================================
    # TRACK A — existing case feeds
    # =========================================================================
    for cid, violations in case_buckets.items():
        if only_cases and cid not in only_cases:
            continue

        case_meta  = registry.get(cid, {})
        case_title = case_meta.get("title") or case_meta.get("name") or cid
        safe_cid   = re.sub(r"[^A-Za-z0-9_-]", "_", cid)
        case_dir   = output_dir / "existing_cases" / safe_cid
        case_dir.mkdir(parents=True, exist_ok=True)

        log.info("=== TRACK A: %s (%d violations) ===", cid, len(violations))

        violations.sort(key=lambda v: (
            0 if (v.get("primary_category") or "").upper() in HIGH_SEVERITY else 1,
            -(len(v.get("classified_categories") or [])),
        ))
        if limit_per_case:
            violations = violations[:limit_per_case]

        per_violation_packets = []
        chain_summaries = []

        for i, v in enumerate(violations, 1):
            payload = {
                **v,
                "case_id":        cid,
                "case_title":     case_title,
                "violation_type": v.get("primary_category"),
                "category":       v.get("primary_category"),
            }
            try:
                state = runner.run(payload, case_id=cid)
                a8 = state.agent_json(8)
                chain_summaries.append({
                    "chain_id":  state.chain_id,
                    "ok":        state.ok,
                    "category":  v.get("primary_category"),
                    "packet_id": a8.get("packet_id"),
                    "folder":    a8.get("output_folder"),
                })
                if a8.get("packet_id"):
                    per_violation_packets.append(a8)
            except Exception as e:
                log.error("TRACK A chain failed %s #%d: %s", cid, i, e)
                chain_summaries.append({"ok": False, "error": str(e)})
            if i % 25 == 0:
                log.info("  %s: %d/%d chains done", cid, i, len(violations))

        case_packet_md = _build_case_packet_md(
            case_dir, cid, case_title, violations, per_violation_packets, chain_summaries
        )
        manifest["track_a_cases"][cid] = {
            "title":           case_title,
            "violations":      len(violations),
            "packets_written": len(per_violation_packets),
            "aggregate":       case_packet_md,
            "folder":          str(case_dir),
        }

    # =========================================================================
    # TRACK B — new case originators
    # =========================================================================
    if not skip_new_cases:
        log.info("=== TRACK B: generating new case packets ===")
        new_case_dir = output_dir / "new_cases"
        new_case_dir.mkdir(parents=True, exist_ok=True)

        if limit_new_cases:
            originator_rows = originator_rows[:limit_new_cases]

        for i, row in enumerate(originator_rows, 1):
            for orig_key in row.get("_originators", []):
                try:
                    pkt = generate_originator_packet(row, orig_key, runner=runner)
                    manifest["track_b_new_cases"].append({
                        "packet_id":      pkt.get("packet_id"),
                        "originator":     orig_key,
                        "defendant":      pkt.get("defendant", {}).get("name"),
                        "urgency":        pkt.get("urgency"),
                        "folder":         pkt.get("output_folder"),
                        "source_message": row.get("message_id"),
                    })
                except Exception as e:
                    log.error("TRACK B failed for %s: %s", orig_key, e)
            if i % 10 == 0:
                log.info("  TRACK B progress: %d/%d rows, %d packets generated",
                         i, len(originator_rows), len(manifest["track_b_new_cases"]))

        log.info("TRACK B complete: %d new case packets generated",
                 len(manifest["track_b_new_cases"]))

    manifest_path = output_dir / "MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    log.info("wrote manifest: %s", manifest_path)
    return manifest


def _build_case_packet_md(case_dir, cid, title, violations,
                          per_violation_packets, chain_summaries):
    cat_count = defaultdict(int)
    for v in violations:
        cat_count[v.get("primary_category", "unknown")] += 1

    lines = [
        f"# CASE ACTION PACKET — {title}",
        f"**Case ID:** {cid}",
        f"**Generated:** {datetime.now().isoformat()}",
        f"**Total violations:** {len(violations)}",
        f"**Chains run:** {len(chain_summaries)}  (ok: {sum(1 for c in chain_summaries if c.get('ok'))})",
        "",
        "## Violations by category",
    ]
    for cat, n in sorted(cat_count.items(), key=lambda x: -x[1]):
        hi = " 🔥" if cat in HIGH_SEVERITY else ""
        lines.append(f"- **{cat}**: {n}{hi}")

    lines += ["", "## Generated packets (in execution order)"]
    for i, pkt in enumerate(per_violation_packets, 1):
        lines.append(
            f"{i}. `{pkt.get('packet_id')}` — "
            f"{len(pkt.get('action_items') or [])} actions — "
            f"{pkt.get('output_folder')}"
        )
    out_md = case_dir / "00_CASE_PACKET.md"
    out_md.write_text("\n".join(lines), encoding="utf-8")
    return str(out_md)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--classified", required=True)
    p.add_argument("--registry",   default=str(ROOT / "cases_index" / "case_registry.json"))
    p.add_argument("--output",     default=str(ROOT / "output" / "action_packets"))
    p.add_argument("--limit-per-case", type=int, default=0)
    p.add_argument("--limit-new-cases", type=int, default=0)
    p.add_argument("--only-cases", type=str, default="")
    p.add_argument("--skip-new-cases", action="store_true",
                   help="skip Track B (new case originators)")
    args = p.parse_args()

    only = [c.strip() for c in args.only_cases.split(",") if c.strip()]
    run_pipeline(
        classified_path=Path(args.classified),
        registry_path=Path(args.registry),
        output_dir=Path(args.output),
        limit_per_case=args.limit_per_case,
        limit_new_cases=args.limit_new_cases,
        only_cases=only or None,
        skip_new_cases=args.skip_new_cases,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
