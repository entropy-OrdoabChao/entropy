"""
pipeline/authority_attack_agent.py — Authority Attack Agent.

DETERMINISTIC. NO LLM. NO INTERPRETATION.

Converts scorecard data + conflict map into a ready-to-inject draft section
that attacks opposing party's non-binding citations and emits the legal
consequence (argument fails as a matter of law).

This is the "weapon firing" layer that connects:
    Scorecard finding (X% non-binding)
        ↓
    Legal effect (no controlling authority)
        ↓
    Draft section (auto-injected into brief)
        ↓
    Conclusion trigger (court must rule for plaintiff)

INPUTS:
    audit_data: per-case audit JSON (or in-memory equivalent)
    conflict_map: from conflict_engine
    case_court: target court that determines binding hierarchy
    binding_hierarchy: dict of {target_court: {binding: [...], persuasive: [...]}}

OUTPUTS:
    {
        "binding_failures": [...],
        "missing_controlling_authority": True/False,
        "non_binding_count": int,
        "non_binding_percentage": int,
        "fatal_count": int,
        "total_count": int,
        "legal_effect": str,
        "drafted_section": str,    # full Section III text ready to inject
        "drafted_conclusion_addendum": str,  # closing trigger
    }

NO LLM. Pure string templates parameterized by scorecard numbers.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Optional

from core.logger import get_logger

log = get_logger("AUTH_ATTACK")


# ══════════════════════════════════════════════════════════════════════
# COURT BINDING METADATA
# ══════════════════════════════════════════════════════════════════════

COURT_LABELS = {
    "SCOTUS": "United States Supreme Court",
    "CA3": "United States Court of Appeals for the Third Circuit",
    "CA_OTHER": "Other federal Courts of Appeals",
    "EDPA": "United States District Court for the Eastern District of Pennsylvania",
    "DPA_OTHER": "Other federal district courts in Pennsylvania",
    "DIST_OTHER": "Federal district courts outside this district",
    "PA_SUP": "Supreme Court of Pennsylvania",
    "PA_SUPER": "Superior Court of Pennsylvania",
    "PA_CMWLTH": "Commonwealth Court of Pennsylvania",
    "PA_CP_PHILA": "Court of Common Pleas of Philadelphia County",
    "PA_CP_OTHER": "Other Pennsylvania Courts of Common Pleas",
    "STATE_OTHER": "Courts of other states",
    "UNKNOWN": "Court of unknown jurisdiction",
}

# What is binding on each target court
BINDING_HIERARCHY = {
    "EDPA": ["SCOTUS", "CA3"],
    "DPA_OTHER": ["SCOTUS"],
    "DIST_OTHER": ["SCOTUS"],
    "PA_CP_PHILA": ["PA_SUP", "PA_SUPER", "PA_CMWLTH"],
    "PA_CP_OTHER": ["PA_SUP", "PA_SUPER", "PA_CMWLTH"],
}

# Why a citation is NOT binding on the target court — used for rebuttal text
NON_BINDING_REASONS = {
    ("EDPA", "DPA_OTHER"): "is a sister federal district court and does not bind this Court under stare decisis",
    ("EDPA", "DIST_OTHER"): "is a federal district court outside the Third Circuit and does not bind this Court",
    ("EDPA", "PA_SUP"): "interprets state law; while persuasive on Pennsylvania substantive issues, it is not controlling on federal procedural questions before this Court",
    ("EDPA", "PA_SUPER"): "is an intermediate state appellate court and does not bind this federal Court",
    ("EDPA", "PA_CMWLTH"): "is a state intermediate appellate court of limited jurisdiction and does not bind this federal Court",
    ("EDPA", "PA_CP_PHILA"): "is a state trial court and does not bind this federal Court",
    ("EDPA", "PA_CP_OTHER"): "is a state trial court of limited jurisdiction and does not bind this federal Court",
    ("EDPA", "STATE_OTHER"): "is a court of another state and has no binding effect on this federal Court",
    ("PA_CP_PHILA", "PA_CP_OTHER"): "is a sister Court of Common Pleas and does not bind this Court under Pennsylvania stare decisis",
    ("PA_CP_PHILA", "PA_CP_PHILA"): "is a coordinate Court of Common Pleas decision and does not constitute binding precedent on this Court",
    ("PA_CP_PHILA", "DPA_OTHER"): "is a federal district court and does not bind this state Court on questions of state law",
    ("PA_CP_PHILA", "DIST_OTHER"): "is a federal district court outside Pennsylvania and does not bind this state Court",
    ("PA_CP_PHILA", "STATE_OTHER"): "is a court of another state and has no binding effect on this Pennsylvania Court",
}


def _get_non_binding_reason(target_court: str, cited_court: str) -> str:
    """Return a one-sentence rebuttal explaining why the cited court does
    not bind the target court."""
    key = (target_court, cited_court)
    if key in NON_BINDING_REASONS:
        return NON_BINDING_REASONS[key]
    cited_label = COURT_LABELS.get(cited_court, cited_court)
    return f"({cited_label}) is not within the binding hierarchy of this Court"


def _classify_binding(target_court: str, cited_court: str) -> str:
    """Return 'binding', 'non-binding', or 'unknown'."""
    binding_courts = BINDING_HIERARCHY.get(target_court, [])
    if cited_court in binding_courts:
        return "binding"
    if cited_court == target_court:
        # Same-court decisions are binding only if from a higher panel — treat as persuasive for safety
        return "persuasive"
    return "non-binding"


# ══════════════════════════════════════════════════════════════════════
# MAIN ENGINE
# ══════════════════════════════════════════════════════════════════════

def analyze_authority_failures(
    target_court: str,
    violations: list,
) -> dict:
    """Analyze a list of violations and classify each as binding/non-binding.

    Args:
        target_court: court hearing the case (e.g., 'EDPA', 'PA_CP_PHILA')
        violations: list of dicts with at minimum:
            - citation (str)
            - cited_court (str — court code)
            - severity (str — 'fatal', 'serious', etc.)

    Returns:
        Analysis dict with binding_failures, counts, percentages, legal_effect.
    """
    if not violations:
        return {
            "binding_failures": [],
            "missing_controlling_authority": False,
            "non_binding_count": 0,
            "non_binding_percentage": 0,
            "fatal_count": 0,
            "total_count": 0,
            "legal_effect": "no opposing authority cited",
            "court_breakdown": {},
        }

    binding_failures = []
    non_binding_count = 0
    fatal_count = 0
    court_breakdown: Counter = Counter()

    for v in violations:
        if not isinstance(v, dict):
            continue
        citation = str(v.get("citation", "")).strip()
        cited_court = str(v.get("cited_court", "UNKNOWN")).strip()
        severity = str(v.get("severity", "")).strip().lower()
        if not citation:
            continue

        binding_status = _classify_binding(target_court, cited_court)
        court_breakdown[cited_court] += 1

        if severity == "fatal":
            fatal_count += 1

        if binding_status == "non-binding":
            non_binding_count += 1
            binding_failures.append({
                "citation": citation,
                "cited_court": cited_court,
                "cited_court_label": COURT_LABELS.get(cited_court, cited_court),
                "severity": severity,
                "binding_status": "non-binding",
                "reason": _get_non_binding_reason(target_court, cited_court),
            })

    total = len([v for v in violations if isinstance(v, dict) and v.get("citation")])
    pct = int(round(100.0 * non_binding_count / total)) if total > 0 else 0

    missing_controlling = (non_binding_count >= max(2, total // 2))

    if missing_controlling:
        legal_effect = (
            "Defendant has failed to present controlling authority in support of "
            "its position. Under stare decisis, the argument fails as a matter of "
            "law and the Motion must be denied."
        )
    elif non_binding_count > 0:
        legal_effect = (
            "Defendant relies in part on non-binding authority. To the extent "
            "Defendant's argument depends on those decisions, it cannot compel "
            "any legal conclusion in this matter."
        )
    else:
        legal_effect = (
            "Defendant cites authority of varying weight; no facially non-binding "
            "citations were identified by the structural audit."
        )

    return {
        "binding_failures": binding_failures,
        "missing_controlling_authority": missing_controlling,
        "non_binding_count": non_binding_count,
        "non_binding_percentage": pct,
        "fatal_count": fatal_count,
        "total_count": total,
        "legal_effect": legal_effect,
        "court_breakdown": dict(court_breakdown),
        "target_court": target_court,
        "target_court_label": COURT_LABELS.get(target_court, target_court),
    }


# ══════════════════════════════════════════════════════════════════════
# DRAFT SECTION BUILDER
# ══════════════════════════════════════════════════════════════════════

def build_authority_attack_section(
    analysis: dict,
    section_number: str = "III",
) -> str:
    """Build the full draft section text from an authority analysis.

    Returns plain text with Roman-numeral header. No markdown.
    No LLM. Pure template fill.
    """
    if not analysis or analysis.get("total_count", 0) == 0:
        return ""

    if analysis.get("non_binding_count", 0) == 0:
        return ""

    target_label = analysis.get("target_court_label", "this Court")
    pct = analysis.get("non_binding_percentage", 0)
    n_non_binding = analysis.get("non_binding_count", 0)
    n_total = analysis.get("total_count", 0)
    legal_effect = analysis.get("legal_effect", "")
    failures = analysis.get("binding_failures", [])

    lines = []
    lines.append(f"{section_number}. DEFENDANT RELIES ON NON-BINDING AUTHORITY")
    lines.append("")
    lines.append(
        f"Defendant relies on {n_non_binding} of {n_total} cited decisions "
        f"({pct}%) from courts that do not bind {target_label} under the "
        f"doctrine of stare decisis. As demonstrated in Plaintiff's Stare "
        f"Decisis Hierarchy Scorecard, attached hereto as Exhibit A, these "
        f"citations are facially non-binding and cannot compel any legal "
        f"conclusion in this matter."
    )
    lines.append("")

    # Group by issuing court for the per-court rebuttal
    by_court: dict = {}
    for f in failures:
        cc = f["cited_court"]
        by_court.setdefault(cc, []).append(f)

    for cited_court, cites_in_court in by_court.items():
        court_label = COURT_LABELS.get(cited_court, cited_court)
        reason = cites_in_court[0]["reason"]
        n = len(cites_in_court)
        plural = "s" if n != 1 else ""
        lines.append(
            f"Defendant cites {n} decision{plural} from the {court_label}, which {reason}. "
        )
        # List the actual citations under each court
        for c in cites_in_court[:5]:  # cap at 5 per court to avoid wall-of-text
            cite = c["citation"]
            if len(cite) > 200:
                cite = cite[:200] + "..."
            lines.append(f"    - {cite}")
        if len(cites_in_court) > 5:
            lines.append(f"    - ... and {len(cites_in_court) - 5} additional citation{'s' if len(cites_in_court) - 5 != 1 else ''} from this court.")
        lines.append("")

    # Repetition / non-authority pattern callout (your friend's fix #3)
    lines.append(
        "Defendant's repeated assertion that litigants \"commonly\" engage in "
        "the disputed conduct is not legal authority. Common practice does not "
        "satisfy the burden to cite controlling law. The standard is not what "
        "is common; it is what the controlling rule requires."
    )
    lines.append("")

    # Legal effect — the closer
    lines.append(legal_effect)
    lines.append("")

    return "\n".join(lines)


def build_conclusion_addendum(analysis: dict) -> str:
    """Build the closing-trigger paragraph for the brief's conclusion.

    Returns a single paragraph that ties scorecard → legal consequence →
    requested ruling.
    """
    if not analysis or not analysis.get("missing_controlling_authority"):
        return ""

    pct = analysis.get("non_binding_percentage", 0)
    n_non_binding = analysis.get("non_binding_count", 0)
    n_total = analysis.get("total_count", 0)
    target_label = analysis.get("target_court_label", "this Court")

    return (
        f"Because Defendant relies on non-binding authority — {pct}% of its "
        f"cited decisions ({n_non_binding} of {n_total}) are facially non-binding "
        f"on {target_label} — and has failed to present controlling precedent "
        f"in support of its position, Defendant has not met its burden under "
        f"governing law. Accordingly, the Motion must be denied and judgment "
        f"entered in favor of Plaintiff."
    )


# ══════════════════════════════════════════════════════════════════════
# FULL ANALYSIS + DRAFT IN ONE CALL
# ══════════════════════════════════════════════════════════════════════

# BEEZLE_FN_UNUSED 2026-06-13: run_authority_attack (no static refs in pipeline/)
def run_authority_attack(
    target_court: str,
    violations: list,
    section_number: str = "III",
) -> dict:
    """Run the full authority attack pipeline.

    Returns a dict containing both the analysis AND the drafted section/closer.
    Suitable for direct injection into chunked_drafter.
    """
    analysis = analyze_authority_failures(target_court, violations)
    drafted_section = build_authority_attack_section(analysis, section_number)
    drafted_closer = build_conclusion_addendum(analysis)

    log.stage(
        "run_attack",
        f"target={target_court}, total={analysis['total_count']}, "
        f"non-binding={analysis['non_binding_count']} ({analysis['non_binding_percentage']}%), "
        f"missing_controlling={analysis['missing_controlling_authority']}"
    )

    return {
        **analysis,
        "drafted_section": drafted_section,
        "drafted_conclusion_addendum": drafted_closer,
        "section_number": section_number,
    }


# ══════════════════════════════════════════════════════════════════════
# AUDIT DATA LOADER (convenience for chunked_drafter)
# ══════════════════════════════════════════════════════════════════════

# BEEZLE_FN_UNUSED 2026-06-13: load_violations_from_audit (no static refs in pipeline/)
def load_violations_from_audit(audit_path: Path) -> tuple:
    """Load violations + target court from a Beezle audit JSON.

    Returns (violations_list, target_court_code)
    """
    import json
    if not audit_path.exists():
        return [], "UNKNOWN"
    try:
        data = json.loads(audit_path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("load_audit", f"Could not parse {audit_path}: {e}")
        return [], "UNKNOWN"

    target_court = data.get("case_court", "UNKNOWN")
    hier = data.get("hierarchy", {}) or {}
    violations = []

    # Walk by_file structure
    for fpath, viols in (hier.get("by_file", {}) or {}).items():
        for v in viols:
            if v.get("severity") in ("fatal", "serious"):
                violations.append({
                    "citation": v.get("citation", ""),
                    "cited_court": v.get("cited_court", "UNKNOWN"),
                    "severity": v.get("severity", ""),
                    "rule_violated": v.get("rule_violated", ""),
                    "source_file": fpath,
                })

    return violations, target_court
