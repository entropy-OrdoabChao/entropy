# -*- coding: utf-8 -*-
"""
violation_classifier.py (v2 — matches real email_scan schema)
=============================================================
Schema it expects (from output/email_scans/email_scan_*.json):
{
  "results": [
    {
      "message_id": "...",
      "date": "...",
      "from": "...",
      "subject": "...",
      "agency": {"name": "...", "type": "detected_legal|official",
                 "domain": "...", "statutes": [...], "match_reason": "..."},
      "violations": [ {"type": "...", "text": "...", ...}, ... ],
      "case_references": ["docket", ...],
      "deadlines": [...],
      "matched_case": null | "fed:2:25-cv-XXXX",
      "body_preview": "..."
    }, ...
  ]
}

Output: one row per (email, violation) tuple with a classification.
"""

from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import argparse
import json
import logging
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# === BEEZLE_ROOT_SHIM (WSL-safe) — MUST run before ROOT use + chain_runner import ===
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
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.chain_runner import ChainRunner  # noqa: E402

log = logging.getLogger("violation_classifier")
if not log.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

# ---------------------------------------------------------------------------
# Category regex map (unchanged from v1)
# ---------------------------------------------------------------------------
CATEGORY_PATTERNS: List[tuple] = [
    ("PRIOR_RESTRAINT_FILING_BAN", [
        r"\bprior restraint\b", r"\bfiling ban\b", r"\bpre-?filing (?:review|approval)\b",
        r"\benjoined? from filing\b", r"\brestrict(?:ed|ing)?\b.{0,30}\bfil(?:e|ing)\b",
    ]),
    ("DO_NOT_FILE_LANGUAGE", [
        r"\bdo not file\b", r"\bshall not file\b", r"\bwill not accept\b.{0,30}filing",
        r"\bcourt will not accept\b", r"\bno further filings\b",
    ]),
    ("EX_PARTE_RULING", [
        r"\bex[\s-]?parte\b", r"\bwithout (?:notice|hearing)\b",
        r"\bchambers (?:order|ruling)\b", r"\bunopposed\b.{0,30}\bgranted\b",
    ]),
    ("DENIED_OPPORTUNITY_TO_RESPOND", [
        r"\bno (?:opportunity|chance) to respond\b", r"\bdenied.{0,20}response\b",
        r"\bwithout allowing\b.{0,30}respon", r"\bbefore (?:response|opposition)\b",
    ]),
    ("ACCESS_TO_COURTS_DENIAL", [
        r"\baccess to (?:the )?courts?\b", r"\bdenied (?:access|court)\b",
        r"\bblocked.{0,20}filing\b",
    ]),
    ("JUDICIAL_BIAS_LANGUAGE", [
        r"\bjudicial bias\b", r"\b(?:openly|apparent) (?:bias|partial)\b",
        r"\bprejudg(?:ed|ment)\b", r"\bappearance of (?:impropriety|partiality)\b",
    ]),
    ("SANCTIONS_NO_SAFE_HARBOR", [
        r"\bsanctions?\b.{0,40}\bwithout (?:safe harbor|notice|21[- ]day)\b",
        r"\brule 11\b.{0,40}\b(?:without|no) safe harbor\b",
        r"\bmonetary sanctions? imposed\b",
    ]),
    ("SUA_SPONTE_NO_LEAVE", [
        r"\bsua sponte\b.{0,40}\b(?:dismiss|denied|strike)\b",
        r"\bcourt's own motion\b", r"\bown initiative\b",
    ]),
    ("VEXATIOUS_DESIGNATION_NO_PROCESS", [
        r"\bvexatious (?:litigant|litigation)\b", r"\bfrivolous litigant\b",
        r"\bpre-?filing (?:injunction|order)\b",
    ]),
    ("NO_REASONS_STATED", [
        r"\bdenied without (?:reasons?|explanation|opinion)\b",
        r"\bno (?:reasons|findings)\b", r"\bwithout memorandum\b",
    ]),
    ("PRO_SE_HEIGHTENED_STANDARD", [
        r"\bpro se\b.{0,60}(?:higher|heightened|stricter) standard\b",
        r"\bliberal(?:ly)? constru\w+\b.{0,30}\bdenied\b",
        r"\bhaines v\.? kerner\b",
    ]),
    ("IMPROPER_CONVERSION_12_TO_56", [
        r"\b12\(b\)\(6\)\b.{0,60}\b(?:convert|summary judgment|56)\b",
        r"\brule 12\b.{0,40}\brule 56\b",
        r"\bmatters outside (?:the )?pleadings?\b",
    ]),
    ("DISMISSED_WITH_PREJUDICE", [
        r"\bdismiss(?:ed|al) with prejudice\b",
        r"\bwith prejudice\b.{0,30}(?:pro se|first amended|leave to amend denied)",
    ]),
    ("REMAND_DENIED", [
        r"\bremand (?:denied|refused)\b",
        r"\bmotion to remand\b.{0,20}\bdenied\b",
        r"\b28 u\.?s\.?c\.? 1447\b",
    ]),
    ("TWOMBLY_IQBAL_MISAPPLIED", [
        r"\btwombly\b", r"\biqbal\b", r"\bplausibility standard\b",
        r"\bconclusory allegations?\b",
    ]),
    ("DEFAULT_MOTION_DENIED", [
        r"\bmotion for default\b.{0,20}\bdenied\b",
        r"\bdefault judgment denied\b",
    ]),
    ("DENIED_WITHOUT_REASONS", [
        r"\bdenied\b\.?\s*$", r"\bmotion denied\b(?!\s+(?:because|for|as))",
    ]),
    ("DEPRIVATION_RIGHTS_COLOR_OF_LAW", [
        r"\bcolor of law\b", r"\b18 u\.?s\.?c\.? 242\b",
        r"\bdeprivation of (?:rights|constitutional)\b",
    ]),
    ("RICO_PATTERN", [
        r"\brico\b", r"\b18 u\.?s\.?c\.? 1962\b",
        r"\bracketeer(?:ing)?\b", r"\bpattern of racketeering\b",
    ]),
    ("CANON_VIOLATION", [
        r"\bcanon \d\b", r"\bcode of judicial conduct\b",
        r"\b28 u\.?s\.?c\.? 455\b", r"\brecus(?:al|e)\b",
    ]),
    ("FRAUD_ON_COURT_JUDICIAL", [
        r"\bfraud on the court\b", r"\bintrinsic fraud\b", r"\bextrinsic fraud\b",
        r"\bfraudulent (?:order|ruling|filing)\b",
    ]),
    ("STARE_DECISIS_VIOLATION", [
        r"\bstare decisis\b", r"\bignored (?:binding|controlling) precedent\b",
        r"\bcontrary to precedent\b",
    ]),
    ("IMPROPER_REMOVAL_FACT", [
        r"\bimproper removal\b", r"\bremov(?:al|ed) without (?:basis|grounds)\b",
        r"\b28 u\.?s\.?c\.? 1441\b",
    ]),
    ("AGENCY_FAILURE_FACT", [
        r"\bagency failed\b", r"\bfailure to (?:investigate|respond|act)\b",
        r"\b(?:cfpb|ftc|hud|occ|sec) (?:complaint|investigation)\b",
    ]),
    ("IFP_DENIAL_CONSTRUCTIVE", [
        r"\bifp\b.{0,20}\bdenied\b", r"\bin forma pauperis\b.{0,30}\bdenied\b",
        r"\bfee waiver denied\b",
    ]),
    ("WILLFUL_NONCOMPLIANCE_FACT", [
        r"\bwillful(?:ly)? (?:non-?compliance|violat)\b",
        r"\bknow(?:ingly|n) (?:violat|non-?comply)\b",
        r"\b15 u\.?s\.?c\.? 1681n\b",
    ]),
]
COMPILED_PATTERNS = [
    (cat, [re.compile(p, re.IGNORECASE) for p in pats])
    for cat, pats in CATEGORY_PATTERNS
]
ALLOWED_CATEGORIES = {c for c, _ in CATEGORY_PATTERNS}

# ---------------------------------------------------------------------------
# Marketing / spam domains to drop (avoid wasting cycles on LendingTree etc.)
# ---------------------------------------------------------------------------
NOISE_DOMAINS = {
    "savings.lendingtree.com", "lendingtree.com", "creditkarma.com",
    "experian.com", "mint.intuit.com", "nerdwallet.com",
    "bankrate.com", "rocketmortgage.com",
}
NOISE_FROM_PATTERNS = [
    re.compile(r"no-?reply", re.I),
    re.compile(r"notification", re.I),
    re.compile(r"marketing", re.I),
    re.compile(r"newsletter", re.I),
    re.compile(r"promo", re.I),
]

def _is_noise(email_item: Dict[str, Any]) -> bool:
    """
    Drop ONLY if all of these are true:
      1. Sender domain is a known marketing domain
      2. No violations extracted upstream
      3. No case references
      4. Subject/body contains no actionable keywords
    This keeps adverse-action notices, denial letters, and HUD/agency
    correspondence even when sender looks like marketing.
    """
    # Hard keep: upstream extracted violations or matched a case
    if email_item.get("violations") or email_item.get("matched_case"):
        return False
    if email_item.get("case_references"):
        return False

    # Check for actionable keywords anywhere in subject + body
    subject = (email_item.get("subject") or "").lower()
    body    = (email_item.get("body_preview") or "").lower()
    blob    = subject + " " + body
    ACTIONABLE = [
        "denied", "denial", "adverse action", "unable to approve",
        "application declined", "discrimin", "fair housing", "hud",
        "cfpb", "fcra", "fdcpa", "ecoa", "tila", "respa",
        "debt collector", "collection", "validation of debt",
        "credit report", "inaccurate", "dispute",
        "bad faith", "claim denied",
        "violation", "violated", "unlawful",
        "complaint", "investigation",
        "rescission", "tort", "damages",
    ]
    for kw in ACTIONABLE:
        if kw in blob:
            return False  # contains actionable language — keep

    # Only NOW check marketing domain
    agency = email_item.get("agency") or {}
    domain = (agency.get("domain") or "").lower()
    if domain in NOISE_DOMAINS:
        return True

    sender = (email_item.get("from") or agency.get("sender") or "").lower()
    for rx in NOISE_FROM_PATTERNS:
        if rx.search(sender):
            return True
    return False


# ---------------------------------------------------------------------------
# Row extraction
# ---------------------------------------------------------------------------
def _text_for_item(item: Dict[str, Any], violation: Optional[Dict[str, Any]] = None) -> str:
    parts: List[str] = []
    for k in ("subject", "body_preview", "body", "text", "excerpt", "snippet", "content"):
        v = item.get(k)
        if isinstance(v, str):
            parts.append(v)
    agency = item.get("agency") or {}
    if isinstance(agency, dict):
        for k in ("match_reason", "name", "full"):
            v = agency.get(k)
            if isinstance(v, str):
                parts.append(v)
        stats = agency.get("statutes")
        if isinstance(stats, list):
            parts.extend(str(s) for s in stats)
    if violation:
        for k in ("type", "violation_type", "category", "text", "snippet",
                  "description", "statute", "citation", "match_reason"):
            v = violation.get(k)
            if isinstance(v, str):
                parts.append(v)
            elif isinstance(v, list):
                parts.extend(str(x) for x in v if x)
    return "\n".join(parts)


def _regex_classify(text: str) -> List[str]:
    if not text:
        return []
    hits = []
    for cat, pats in COMPILED_PATTERNS:
        for p in pats:
            if p.search(text):
                hits.append(cat)
                break
    return hits


def _normalize_existing_label(label: str) -> Optional[str]:
    if not label:
        return None
    lbl = label.upper().replace(" ", "_").replace("-", "_")
    if lbl in ALLOWED_CATEGORIES:
        return lbl
    # Partial match
    for cat in ALLOWED_CATEGORIES:
        if cat in lbl or lbl in cat:
            return cat
    return None


def _flatten_to_rows(email_item: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    One row per violation; if email has no violations but has a matched_case
    or case_references + real agency, emit one skeleton row for classification.
    """
    rows: List[Dict[str, Any]] = []
    base = {
        "message_id":      email_item.get("message_id"),
        "date":            email_item.get("date"),
        "from":            email_item.get("from"),
        "subject":         email_item.get("subject"),
        "agency_name":     (email_item.get("agency") or {}).get("name"),
        "agency_domain":   (email_item.get("agency") or {}).get("domain"),
        "agency_type":     (email_item.get("agency") or {}).get("type"),
        "agency_statutes": (email_item.get("agency") or {}).get("statutes") or [],
        "matched_case":    email_item.get("matched_case"),
        "case_references": email_item.get("case_references") or [],
        "deadlines":       email_item.get("deadlines") or [],
        "body_preview":    email_item.get("body_preview", "")[:2000],
    }
    vios = email_item.get("violations") or []
    if isinstance(vios, list) and vios:
        for v in vios:
            if not isinstance(v, dict):
                v = {"value": str(v)}
            rows.append({**base, "violation": v})
        return rows

    # No extracted violations but still potentially actionable
    if email_item.get("matched_case") or email_item.get("case_references"):
        rows.append({**base, "violation": {}})
    return rows


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
def classify_row(
    row: Dict[str, Any],
    runner: Optional[ChainRunner] = None,
    use_llm_fallback: bool = True,
) -> Dict[str, Any]:
    violation = row.get("violation") or {}
    text = _text_for_item(row, violation)

    # Check for a pre-labeled category on the violation
    for k in ("category", "violation_type", "type", "label"):
        lbl = _normalize_existing_label(violation.get(k) or "")
        if lbl:
            return {**row, "classified_categories": [lbl],
                    "primary_category": lbl, "classifier": "existing"}

    # Regex pass
    cats = _regex_classify(text)
    if cats:
        return {**row, "classified_categories": cats,
                "primary_category": cats[0], "classifier": "regex"}

    # LLM fallback (expensive — only if enabled)
    if use_llm_fallback and runner is not None and text:
        try:
            payload = {"text": text[:5000], **{k: v for k, v in row.items()
                                                if k not in ("violation",)}}
            state = runner.run(payload, case_id=row.get("matched_case"), only=[1])
            a1 = state.agent_json(1)
            facts = a1.get("facts", {}) or {}
            lbl = _normalize_existing_label(
                facts.get("violation_type") or facts.get("category") or "")
            if lbl:
                return {**row, "classified_categories": [lbl],
                        "primary_category": lbl, "classifier": "agent_1",
                        "agent_1_facts": facts}
        except Exception as e:
            log.debug("agent_1 failed: %s", e)

    return {**row, "classified_categories": [],
            "primary_category": "unknown", "classifier": "none"}


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
def _extract_emails(path: Path) -> List[Dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("results"), list):
        return data["results"]
    if isinstance(data, list):
        return data
    return []


def classify_all(
    input_path: Path,
    output_path: Path,
    use_llm_fallback: bool = True,
    limit: int = 0,
    drop_noise: bool = True,
) -> Dict[str, Any]:
    emails = _extract_emails(input_path)
    if limit:
        emails = emails[:limit]
    log.info("loaded %d emails from %s", len(emails), input_path.name)

    # Filter noise
    if drop_noise:
        before = len(emails)
        emails = [e for e in emails if not _is_noise(e)]
        log.info("dropped %d noise emails (marketing/newsletter)", before - len(emails))

    # Flatten to rows
    rows: List[Dict[str, Any]] = []
    for e in emails:
        rows.extend(_flatten_to_rows(e))
    log.info("flattened to %d (email, violation) rows", len(rows))

    runner = ChainRunner(vault=False, log_each=False) if use_llm_fallback else None

    classified: List[Dict[str, Any]] = []
    counter: Counter = Counter()
    classifier_stats: Counter = Counter()

    for i, row in enumerate(rows, 1):
        result = classify_row(row, runner=runner, use_llm_fallback=use_llm_fallback)
        classified.append(result)
        counter[result.get("primary_category", "unknown")] += 1
        classifier_stats[result.get("classifier", "none")] += 1
        if i % 200 == 0:
            log.info("  %d/%d classified (known=%d)",
                     i, len(rows), sum(v for k, v in counter.items() if k != "unknown"))

    total = len(classified)
    unknown = counter.get("unknown", 0)

    summary = {
        "generated":         datetime.now().isoformat(),
        "input":             str(input_path),
        "emails_processed":  len(emails),
        "rows_total":        total,
        "unknown_remaining": unknown,
        "classified":        total - unknown,
        "by_category":       dict(counter),
        "by_classifier":     dict(classifier_stats),
        "results":           classified,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    log.info("wrote %s — %d classified / %d unknown", output_path, total - unknown, unknown)

    # Category histogram
    log.info("category breakdown:")
    for cat, n in counter.most_common(10):
        log.info("  %-40s %d", cat, n)

    return summary


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--input",  required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--limit",  type=int, default=0,
                   help="max emails to process (not rows)")
    p.add_argument("--no-llm", action="store_true",
                   help="regex only — skip Agent 1 LLM fallback")
    p.add_argument("--keep-noise", action="store_true",
                   help="do not filter marketing/newsletter emails")
    args = p.parse_args()

    classify_all(
        input_path=Path(args.input),
        output_path=Path(args.output),
        use_llm_fallback=not args.no_llm,
        limit=args.limit,
        drop_noise=not args.keep_noise,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
