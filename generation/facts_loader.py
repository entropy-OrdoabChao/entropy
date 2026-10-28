"""generation/facts_loader.py — Phase 3F Step 6d.

The missing data feed between Layer 2 (audit/extraction) and Layer 3 (drafter).

Reads:
  - output/audit/data_<case>.json           (audit + scorecard + violations)
  - output/audit/order_indexes/orders_<case>.json  (Phase 3F extracted orders)

Produces structured strings suitable for direct injection into LLM section
prompts. Each function returns plain text — the LLM translates to prose.

The LLM never invents facts. Facts come from here only.

Public API:
    load_drafting_facts(case_id, audit_data) -> dict
        Master function. Returns dict with keys:
          - dispositive_orders     : list of dispositive order dicts
          - dispositive_text       : human-readable dispositive narrative
          - record_facts_text      : numbered list of plaintiff record facts
          - violation_facts_text   : violations grouped by category for arguments
          - citations_text         : numbered citation list from scorecard
          - record_evidence_gaps   : list of evidence not addressed in orders

Adapted from C:\AI_AGENT\beezle_src\beezle\drafting\facts_loader.py.
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
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

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

# Match anything that's a real dispositive ruling (not a procedural grant)
_DISPOSITIVE_SUBJECT_RE = re.compile(
    r"\b("
    r"motion(?:s)?\s+to\s+dismiss"
    r"|MTD"
    r"|motion(?:s)?\s+to\s+strike"
    r"|motion(?:s)?\s+for\s+summary\s+judgment"
    r"|MSJ"
    r"|dismiss(?:ed|al)?\s+(?:claim|with\s+prejudice|count)"
    r"|judgment\s+(?:on|for)\s+the\s+pleadings"
    r"|JOP"
    r")\b",
    re.IGNORECASE,
)

# Subjects that are routine procedural events, NOT violations
_NON_VIOLATION_SUBJECT_RE = re.compile(
    r"\b("
    r"pro\s+hac\s+vice"
    r"|admission\s+(?:of\s+counsel)?"
    r"|sur(?:-)?reply"
    r"|extension\s+of\s+time"
    r"|leave\s+to\s+(?:file|amend|reply)"
    r"|continuance"
    r"|reschedul"
    r"|substitution\s+of\s+counsel"
    r"|withdrawal\s+of\s+counsel"
    r"|local\s+rule\s+\d"
    r")\b",
    re.IGNORECASE,
)



# ===PHASE_3F_STEP6F_REGRESSION_FIX_V1=== restore helpers consumed by 6e
def _format_date(iso_date: str) -> str:
    """Convert YYYY-MM-DD to 'September 19, 2025'."""
    if not iso_date:
        return ""
    try:
        from datetime import datetime as _dt
        dt = _dt.strptime(iso_date, "%Y-%m-%d")
        return f"{dt.strftime('%B')} {dt.day}, {dt.year}"
    except Exception:
        return iso_date


def _format_defendants(defs):
    """Format ['capital_one', 'avant'] as 'Example Card Issuer and Avant'."""
    if not defs:
        return ""
    pretty = []
    for d in defs:
        pretty.append(d.replace("_", " ").title())
    if len(pretty) == 1:
        return pretty[0]
    if len(pretty) == 2:
        return f"{pretty[0]} and {pretty[1]}"
    return ", ".join(pretty[:-1]) + f", and {pretty[-1]}"
# ===END PHASE_3F_STEP6F_REGRESSION_FIX_V1===

def _is_dispositive_subject(subject: str) -> bool:
    """True if a granted ruling subject indicates dispositive action."""
    if not subject:
        return False
    if _NON_VIOLATION_SUBJECT_RE.search(subject):
        return False
    if _DISPOSITIVE_SUBJECT_RE.search(subject):
        return True
    # ===PHASE_3F_STEP6E_FIXES_V1=== Apr 24 CA3 dismissal subject = "Appeal"
    # Appeal dismissals from a higher court are dispositive.
    if re.search(r"\bappeal\b", subject, re.IGNORECASE):
        return True
    # ===END PHASE_3F_STEP6E_FIXES_V1===
    return False


def extract_dispositive_orders(case_id: str, base_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Pull dispositive orders from order_indexes/orders_<case>.json.

    Step 6e: Pulls from outcomes_by_type.granted AND outcomes_by_type.dismissed
    (CA3 appeal dismissals are dispositive but live under \'dismissed\').
    Also dedupes by (filing_date, ecf, subject) tuple.
    """
    base = base_path or _BEEZLE_ROOT
    safe_id = case_id.replace(":", "_").replace("/", "_")
    p = base / "output" / "audit" / "order_indexes" / f"orders_{safe_id}.json"
    if not p.exists():
        return []

    try:
        # ===PHASE_3F_STEP6E_FIXES_V1=== utf-8 explicit
        data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return []

    orders = data.get("orders", []) or []
    dispositive = []
    seen = set()

    for o in orders:
        outcomes = o.get("outcomes_by_type", {}) or {}

        # ===PHASE_3F_STEP6E_FIXES_V1=== check both granted AND dismissed
        for outcome_type in ("granted", "dismissed"):
            entries = outcomes.get(outcome_type, []) or []
            for g in entries:
                subject = g.get("subject", "") or ""
                if not _is_dispositive_subject(subject):
                    continue

                filing_date = o.get("filing_date") or ""
                ecf = o.get("ecf_number") or ""
                # Dedupe key
                dkey = (filing_date, ecf, subject[:80])
                if dkey in seen:
                    continue
                seen.add(dkey)

                dispositive.append({
                    "doc_id":         o.get("doc_id", "") or "",
                    "filing_date":    filing_date,
                    "ecf":            ecf,
                    "subject":        subject,
                    "outcome":        outcome_type,
                    "defendants":     g.get("subject_defendants", []) or [],
                    "deadlines":      o.get("deadlines_set", []) or [],
                    "authorities":    o.get("authorities_relied_on", []) or [],
                })
        # ===END PHASE_3F_STEP6E_FIXES_V1===

    # Sort newest first
    dispositive.sort(key=lambda x: x.get("filing_date", "") or "0000", reverse=True)
    return dispositive


def build_dispositive_text(dispositive_orders: List[Dict[str, Any]]) -> str:
    """Build a human-readable dispositive narrative.

    Pure string assembly — no LLM. The LLM later renders this to legal prose.
    """
    if not dispositive_orders:
        return "(no dispositive orders identified in record)"

    lines = []
    for d in dispositive_orders:
        date_pretty = _format_date(d.get("filing_date", ""))
        defs_pretty = _format_defendants(d.get("defendants", []))
        ecf = d.get("ecf", "")
        subject = d.get("subject", "")[:200]

        if date_pretty and defs_pretty:
            line = f"On {date_pretty}, the Court (ECF {ecf}) granted: {subject}. "
            line += f"Subject defendants: {defs_pretty}."
        elif date_pretty:
            line = f"On {date_pretty}, the Court (ECF {ecf}) granted: {subject}."
        else:
            line = f"The Court (ECF {ecf}) granted: {subject}."

        deadlines = d.get("deadlines", [])
        if deadlines:
            d0 = deadlines[0]
            if isinstance(d0, dict):
                ddate = _format_date(d0.get("date", ""))
                if ddate:
                    line += f" Amend deadline: {ddate}."

        lines.append(line)

    return "\n\n".join(lines)


def build_violation_facts(audit_data: Dict[str, Any], cap: int = 30) -> Dict[str, List[str]]:
    """Group violations by category. Each category gets formatted fact strings.

    Returns dict: {category: [fact_str, ...]}.
    Used by argument sections to ground each issue in real record cites.
    """
    by_cat = {}
    pv = audit_data.get("procedural_violations", []) or []

    for v in pv[:cap * 5]:  # process more, cap output per category
        cat = v.get("violation_type", "GENERAL")
        if not cat:
            continue
        rule = v.get("rule_citation", "")
        rule_text = v.get("rule_text", "")
        quote = v.get("quote", "")[:200]
        explanation = v.get("explanation", "")
        source = v.get("file", "")
        page = v.get("page", "")
        sev = v.get("severity", "")

        fact = f"[{sev}] {explanation}"
        if quote:
            fact += f" Record quote: \"{quote}\""
        if source:
            fact += f" (Source: {source}"
            if page:
                fact += f" p.{page}"
            fact += ")"

        by_cat.setdefault(cat, []).append(fact)

    # Cap each category at `cap` items
    for cat in by_cat:
        by_cat[cat] = by_cat[cat][:cap]

    return by_cat


def build_citations_text(audit_data: Dict[str, Any], cap: int = 50) -> str:
    """Build numbered citations list from scorecard.violations.

    Format matches AI_AGENT/builder.py:
      - [1] source (jurisdiction)
      - [2] source (jurisdiction)
    """
    sc = audit_data.get("scorecard", {})
    violations = sc.get("violations", []) or []

    if not violations:
        return "(no citations flagged in scorecard)"

    lines = []
    seen = set()
    for i, v in enumerate(violations[:cap * 2], start=1):
        evidence = v.get("evidence", "")[:80]
        cat = v.get("category", "")
        sev = v.get("severity", "")
        key = (evidence, cat)
        if key in seen:
            continue
        seen.add(key)
        if not evidence:
            continue
        lines.append(f"  - [{len(lines)+1}] {evidence}  ({cat} / {sev})")
        if len(lines) >= cap:
            break

    return "\n".join(lines) if lines else "(no specific citations extracted)"


def build_record_evidence_gaps(audit_data: Dict[str, Any],
                                 dispositive_orders: List[Dict[str, Any]]) -> List[str]:
    """Identify record evidence that dispositive orders failed to address.

    For each dispositive order, list the violations / facts that the order
    does NOT address. This is the "Failure to Address Record Evidence"
    section's data source.

    Currently returns a list of explanation strings flagged FATAL or SERIOUS
    that don't appear in the dispositive order's authorities_relied_on.
    """
    gaps = []
    if not dispositive_orders:
        return gaps

    pv = audit_data.get("procedural_violations", []) or []

    # Authorities the dispositive order DID rely on (string match for now)
    relied_on = set()
    for d in dispositive_orders:
        for a in d.get("authorities", []) or []:
            if isinstance(a, str):
                relied_on.add(a.lower())
            elif isinstance(a, dict):
                relied_on.add(str(a.get("citation", "")).lower())

    for v in pv:
        sev = (v.get("severity") or "").upper()
        if sev not in ("FATAL", "SERIOUS"):
            continue
        explanation = v.get("explanation", "")
        rule = v.get("rule_citation", "").lower()
        if rule and rule in relied_on:
            continue  # the order DID consider this
        if explanation:
            gaps.append(f"[{sev}] {v.get('violation_type','')}: {explanation}")

    # Cap at 10 strongest gaps
    return gaps[:10]


def load_drafting_facts(case_id: str, audit_data: Dict[str, Any],
                         base_path: Optional[Path] = None) -> Dict[str, Any]:
    """Master facts loader. Pulls everything the drafter needs.

    Returns dict with all structured inputs the LLM section builder needs.
    Empty values are still returned (not None) so downstream code can
    rely on key presence.
    """
    dispositive_orders = extract_dispositive_orders(case_id, base_path=base_path)
    dispositive_text = build_dispositive_text(dispositive_orders)
    violation_facts = build_violation_facts(audit_data, cap=20)
    citations_text = build_citations_text(audit_data, cap=30)
    record_gaps = build_record_evidence_gaps(audit_data, dispositive_orders)

    record_facts_text = ""
    if violation_facts:
        out_lines = []
        for cat, facts in list(violation_facts.items())[:8]:  # top 8 categories
            out_lines.append(f"\n## {cat}")
            for i, f in enumerate(facts[:5], start=1):
                out_lines.append(f"  {i}. {f}")
        record_facts_text = "\n".join(out_lines)

    return {
        "case_id":              case_id,
        "dispositive_orders":   dispositive_orders,
        "dispositive_text":     dispositive_text,
        "record_facts_text":    record_facts_text,
        "violation_facts":      violation_facts,
        "citations_text":       citations_text,
        "record_evidence_gaps": record_gaps,
        "has_dispositive":      bool(dispositive_orders),
    }
