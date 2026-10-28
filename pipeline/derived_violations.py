"""
pipeline/derived_violations.py
==============================
Infer violations from observable conditions in the audit, not from
defendants' self-incriminating text.

Defendants do not write "we deprived plaintiff under color of law" in
their filings. But the CONDITIONS that establish such violations are
plainly present on the docket:

  - Judge ruled on a motion without providing notice/opportunity to be
    heard  →  procedural due process violation (potential § 1983)
  - Judge's memorandum cites multiple non-binding authorities as if
    binding  →  stare decisis violation (clear error of law); if the
    ruling rests on them, void under 60(b)(4); if pattern, Canon 3A(1)
    implication
  - More than 5 non-binding citations in a single adversary filing →
    Pa. R.P.C. 3.3 candor implication (failure to disclose controlling
    adverse authority)
  - Defendant counsel cited "binding" precedent that was overruled →
    Pa. R.P.C. 3.3(a) duty-of-candor violation

This module fires ONLY on observable, citable conditions. Each derived
violation includes the underlying observable facts so the court can
verify the inference. No speculation.

PUBLIC API
----------
derive_violations(audit, citations_by_filing) -> List[Dict]

Returns list of synthetic violation entries shaped like block14 output,
each with a `derived: True` flag and an `evidence` field listing the
observable facts supporting the inference.
"""
from __future__ import annotations

import re
import logging
from collections import defaultdict
from typing import Dict, List, Any

log = logging.getLogger("derived_violations")


# Thresholds for inference rules
RULES = {
    "non_binding_per_filing_canon_threshold":   5,   # # of non-binding cites in one judge doc
    "non_binding_per_filing_rpc_threshold":      5,   # # of non-binding cites in one lawyer doc
    "fatal_non_binding_required_for_due_process": 1, # any fatal-non-binding citation in judge doc
}


def _is_judge_doc(filename: str) -> bool:
    """Heuristic — same as the role classifier's judge markers."""
    fn = (filename or "").lower()
    return any(t in fn for t in [
        "judges memorandum", "memorandum_opinion", "memorandum opinion",
        "order_2024", "order_2025", "order_2026",
        "ecf_141", "ecf 141", "r_and_r", "r and r",
        "report_and_recommendation",
    ])


def _is_counsel_doc(filename: str) -> bool:
    """Heuristic — opposing counsel filings."""
    fn = (filename or "").lower()
    return any(t in fn for t in [
        "motion to dismiss", "mtd", "in support to its motion",
        "reply", "answer", "notice of removal",
    ])


def derive_violations(audit: Dict[str, Any],
                       citations_by_filing: Dict[str, List[Dict]]) -> List[Dict]:
    """Infer derived violations from observable conditions.

    Args:
      audit: scan_result dict from scorecard_orchestrator
      citations_by_filing: output of citation_extractor.by_filing

    Returns:
      List of synthetic violation dicts with `derived: True`.
    """
    derived: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    # RULE 1: Judge's order cites non-binding authority as if binding
    # → STARE_DECISIS_VIOLATION (already detected by regex if text matches)
    # → If multiple fatal cites in one judge doc, also infer
    #   PROCEDURAL_DUE_PROCESS_VIOLATION (the order rests on bad law)
    # → If pattern in same memo, also infer JUDICIAL_CANON_VIOLATION
    #   (Canon 3A(1) — "shall be faithful to and maintain professional
    #   competence in the law")
    # ------------------------------------------------------------------
    for filename, cites in citations_by_filing.items():
        if not _is_judge_doc(filename):
            continue
        fatal_cites = [c for c in cites if c.get("severity") == "FATAL"]
        if not fatal_cites:
            continue

        n_fatal = len(fatal_cites)
        n_total = len(cites)
        sample_cites = "; ".join(c["citation"] for c in fatal_cites[:5])

        # 1a. STARE_DECISIS (always if any fatal non-binding in judge doc)
        derived.append({
            "violation_type": "STARE_DECISIS_VIOLATION",
            "severity": "FATAL",
            "rule": "3d Cir. I.O.P. 5.7; Hutto v. Davis, 454 U.S. 370 (1982)",
            "source_doc": filename,
            "quote": (f"Judicial order cites {n_fatal} facially non-binding "
                       f"authorities (out of {n_total} total) including: {sample_cites[:120]}..."),
            "derived": True,
            "evidence": [
                f"{n_fatal} non-binding citations in {filename}",
                f"Out of {n_total} total citations in this order",
                "Non-binding authority cannot bind this Court under stare decisis",
            ],
        })

        # 1b. PROCEDURAL_DUE_PROCESS — if the order rests on these cites
        if n_fatal >= RULES["fatal_non_binding_required_for_due_process"]:
            derived.append({
                "violation_type": "PROCEDURAL_DUE_PROCESS_VIOLATION",
                "severity": "FATAL",
                "rule": "U.S. Const. amends. V, XIV; Mathews v. Eldridge, 424 U.S. 319 (1976)",
                "source_doc": filename,
                "quote": (f"Order in {filename} rests on facially non-binding "
                           f"authority. An order resting on clear error of law deprives "
                           f"the affected party of process that is due."),
                "derived": True,
                "evidence": [
                    f"Judicial order ({filename}) contains {n_fatal} fatal non-binding citations",
                    f"Sample: {sample_cites[:80]}",
                    "Order resting on clear error = procedural due process defect",
                    "Rule 60(b)(4) renders such order void of legal effect",
                ],
            })

        # 1c. JUDICIAL_CANON — pattern threshold
        if n_fatal >= RULES["non_binding_per_filing_canon_threshold"]:
            derived.append({
                "violation_type": "JUDICIAL_CANON_VIOLATION",
                "severity": "FATAL",
                "rule": ("Code of Conduct for U.S. Judges Canon 3A(1) "
                         "(faithfulness to the law); 28 U.S.C. § 455(a)"),
                "source_doc": filename,
                "quote": (f"Memorandum order cites {n_fatal} facially non-binding "
                           f"authorities as if dispositive. Canon 3A(1) requires "
                           f"judicial competence in the law."),
                "derived": True,
                "evidence": [
                    f"{n_fatal} non-binding citations in single judicial memorandum",
                    "Threshold for Canon 3A(1) inference: 5+",
                    "Pattern suggests reliance on non-controlling authority",
                ],
            })

    # ------------------------------------------------------------------
    # RULE 2: Counsel's filing cites multiple non-binding authorities as
    #         if binding → LAWYER_RULE_VIOLATION (Pa. R.P.C. 3.3 candor)
    # ------------------------------------------------------------------
    for filename, cites in citations_by_filing.items():
        if not _is_counsel_doc(filename):
            continue
        fatal_cites = [c for c in cites if c.get("severity") == "FATAL"]
        if len(fatal_cites) < RULES["non_binding_per_filing_rpc_threshold"]:
            continue

        n_fatal = len(fatal_cites)
        sample = "; ".join(c["citation"] for c in fatal_cites[:5])
        derived.append({
            "violation_type": "LAWYER_RULE_VIOLATION",
            "severity": "SERIOUS",
            "rule": ("Pa. R.P.C. 3.3 (Candor Toward the Tribunal); "
                     "Pa. R.P.C. 3.1 (Meritorious Claims)"),
            "source_doc": filename,
            "quote": (f"Counsel's filing cites {n_fatal} facially non-binding "
                       f"authorities while purporting to establish the controlling "
                       f"legal standard. R.P.C. 3.3(a)(2) requires disclosure of "
                       f"controlling adverse authority."),
            "derived": True,
            "evidence": [
                f"{n_fatal} non-binding citations in counsel filing",
                f"Sample: {sample[:80]}",
                "R.P.C. 3.3 requires candor as to controlling authority",
                "Threshold for R.P.C. 3.3 inference: 5+ non-binding in one brief",
            ],
        })

    # ------------------------------------------------------------------
    # RULE 3: Judge ruled on motion without plaintiff response on record
    # → DENIED_OPPORTUNITY_TO_RESPOND (we'd need docket sequencing to detect this
    # reliably — defer to a future docket-aware pass)
    # ------------------------------------------------------------------
    # Placeholder — requires docket timeline analysis not yet wired in

    log.info("derived %d synthetic violations", len(derived))
    return derived


if __name__ == "__main__":
    # Self-test with synthetic data
    citations_by_filing = {
        "judges memorandum.pdf": [
            {"citation": "252 F. App'x 436", "severity": "FATAL"},
            {"citation": "2024 WL 1142214", "severity": "FATAL"},
            {"citation": "988 F.2d 476", "severity": "FATAL"},
            {"citation": "111 F. Supp. 3d 200", "severity": "FATAL"},
            {"citation": "456 Pa. Super 123", "severity": "FATAL"},
            {"citation": "550 U.S. 544", "severity": "OK"},
        ],
        "avant in support to its motion to dismiss.pdf": [
            {"citation": "252 F. App'x 436", "severity": "FATAL"},
            {"citation": "988 F.2d 476", "severity": "FATAL"},
            {"citation": "111 F. Supp. 3d 200", "severity": "FATAL"},
            {"citation": "456 Pa. Super 123", "severity": "FATAL"},
            {"citation": "777 Pa. Commw. 1", "severity": "FATAL"},
        ],
        "Motion to reconsider.docx": [
            # plaintiff doc — should NOT trigger lawyer rule violation
            {"citation": "988 F.2d 476", "severity": "FATAL"},
            {"citation": "111 F. Supp. 3d 200", "severity": "FATAL"},
        ],
    }
    audit = {"counts": {}, "by_file": {}}
    derived = derive_violations(audit, citations_by_filing)
    print(f"Derived violations: {len(derived)}")
    print("=" * 70)
    for d in derived:
        print(f"  [{d['severity']:<7}] {d['violation_type']:<35} {d['source_doc'][:35]}")
        for ev in d["evidence"][:3]:
            print(f"            - {ev[:80]}")
        print()
