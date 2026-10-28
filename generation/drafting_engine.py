"""
generation/drafting_engine.py — Template-driven structured drafter.

NO deviation from templates. Output is a list of structured sections
that the document_generator turns into a .docx.

Section schema:
  { "heading": str, "body": str, "authority": [str], "elements": [...] }
"""
from __future__ import annotations
from typing import Any, Dict, List

from core.logger import get_logger

log = get_logger("drafting_engine")


# ─── Templates (verbatim, no deviation) ──────────────────────────────────
TEMPLATES: Dict[str, List[str]] = {
    "reply_brief": [
        "INTRODUCTION",
        "PROCEDURAL HISTORY",
        "STATEMENT OF FACTS",
        "ARGUMENT",
        "VIOLATION SCORECARD",
        "REQUESTED RELIEF",
        "CONCLUSION",
    ],
    "complaint": [
        "PARTIES",
        "JURISDICTION AND VENUE",
        "FACTUAL ALLEGATIONS",
        "CAUSES OF ACTION",
        "PRAYER FOR RELIEF",
    ],
    "regulatory_complaint": [
        "COMPLAINANT",
        "RESPONDENT",
        "STATEMENT OF FACTS",
        "MANDATORY DUTY VIOLATED",
        "AUTHORITY",
        "REQUESTED ACTION",
    ],
}


def draft_document(
    facts: List[str],
    violations: List[Dict[str, Any]],
    template: str = "reply_brief",
    case_caption: str = "",
) -> List[Dict[str, Any]]:
    if template not in TEMPLATES:
        log.fail("draft", f"unknown template: {template}")
        raise ValueError(f"unknown template: {template}")

    sections: List[Dict[str, Any]] = []
    for heading in TEMPLATES[template]:
        body, authority = _build_section(heading, facts, violations, case_caption)
        sections.append({
            "heading": heading,
            "body": body,
            "authority": authority,
        })
    log.info("draft", f"{len(sections)} sections built ({template})")
    return sections


def _build_section(
    heading: str,
    facts: List[str],
    violations: List[Dict[str, Any]],
    case_caption: str,
) -> tuple:
    """Deterministic section builder. No LLM."""
    if heading == "INTRODUCTION":
        return (
            f"This brief is filed in {case_caption or '[CAPTION]'}. "
            f"It addresses {len(violations)} identified violation(s).",
            [],
        )
    if heading == "STATEMENT OF FACTS":
        return ("\n".join(f"{i+1}. {f}" for i, f in enumerate(facts)), [])
    if heading == "ARGUMENT":
        lines = []
        auth: List[str] = []
        for i, v in enumerate(violations, 1):
            lines.append(f"{i}. {v.get('violation') or v.get('rule_id')}")
            if v.get("rule_id"):
                auth.append(str(v["rule_id"]))
            lines.append(f"   IF {v.get('rule_id')} THEN {v.get('remedy')}")
        return ("\n".join(lines), auth)
    if heading == "VIOLATION SCORECARD":
        rows = ["| # | Rule | Violation | Remedy | Mandatory |",
                "|---|------|-----------|--------|-----------|"]
        for i, v in enumerate(violations, 1):
            rows.append(
                f"| {i} | {v.get('rule_id')} | {v.get('violation')} | "
                f"{v.get('remedy')} | {'Y' if v.get('mandatory') else 'N'} |"
            )
        return ("\n".join(rows), [])
    if heading == "REQUESTED RELIEF":
        remedies = sorted({v.get("remedy") for v in violations if v.get("remedy")})
        return ("\n".join(f"- {r}" for r in remedies), [])
    if heading == "CONCLUSION":
        return ("For the foregoing reasons, the requested relief should be granted.", [])
    if heading == "MANDATORY DUTY VIOLATED":
        m = [v for v in violations if v.get("mandatory")]
        return (
            "\n".join(f"- {v.get('rule_id')}: {v.get('violation')}" for v in m) or "(none)",
            [str(v.get("rule_id")) for v in m if v.get("rule_id")],
        )
    return ("[section placeholder]", [])
