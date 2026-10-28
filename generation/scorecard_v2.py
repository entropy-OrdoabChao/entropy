"""
generation/scorecard_v2.py — Enhanced Scorecard Generator.

Reads audit data from output/audit/data_*.json and generates
court-ready + layman-readable scorecards.

Improvements over v1:
  1. "How to Read" section at top
  2. Severity key with plain-English impact
  3. Statute/rule references on every violation
  4. Plain-English summary
  5. Court impact box
  6. Consolidated violations (no duplicates)
  7. Statutes & Rules Implicated section
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import json
from pathlib import Path
from generation.source_display import display_source
from datetime import datetime

from core.logger import get_logger
from pipeline.statutes_registry import lookup as statute_lookup
from pipeline.court_hierarchy import COURT_RANK

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

log = get_logger("scorecard_v2")

AUDIT_DIR = (_BEEZLE_ROOT / "output" / "audit")
OUTPUT_DIR = (_BEEZLE_ROOT / "output" / "audit")

# Severity explanations (plain English)
SEVERITY_EXPLAIN = {
    "FATAL": {
        "label": "FATAL",
        "meaning": "Cannot legally support a ruling",
        "impact": "If the court relies on this, the decision is legally defective and subject to reversal on appeal",
        "icon": "X",
    },
    "serious": {
        "label": "SERIOUS",
        "meaning": "Must be independently verified before use",
        "impact": "If treated as controlling without verification, it creates reversible legal error",
        "icon": "!",
    },
    "minor": {
        "label": "MINOR",
        "meaning": "Persuasive only — cannot control the outcome",
        "impact": "May inform but does not bind the court; reliance without acknowledgment is misleading",
        "icon": "-",
    },
    "STANDARD": {
        "label": "STANDARD",
        "meaning": "Procedural or conduct issue detected",
        "impact": "May indicate bias, improper procedure, or prejudicial language in the record",
        "icon": "~",
    },
    "REVERSIBLE": {
        "label": "REVERSIBLE",
        "meaning": "Strong signal of reversible error",
        "impact": "The record contains indicators that an appellate court would likely find error",
        "icon": "!",
    },
}

# Violation type → governing rule/statute
VIOLATION_RULES = {
    "NON_BINDING_AS_CONTROLLING": {
        "rule": "Stare decisis doctrine",
        "statute": "Courts must follow binding precedent from superior courts within the same jurisdiction",
        "fed_authority": "Erie Railroad Co. v. Tompkins, 304 U.S. 64 (1938)",
        "pa_authority": "Commonwealth v. Tilghman, 673 A.2d 898 (Pa. 1996)",
    },
    "TRIAL_COURT_AS_APPELLATE_AUTHORITY": {
        "rule": "Judicial hierarchy / stare decisis",
        "statute": "A trial court may not treat decisions from co-equal or inferior courts as binding authority",
        "fed_authority": "Camreta v. Greene, 563 U.S. 692 (2011)",
        "pa_authority": "Commonwealth v. Tilghman, 673 A.2d 898 (Pa. 1996)",
    },
    "BIAS_EMOTIONAL_LANGUAGE": {
        "rule": "Judicial impartiality",
        "statute": "Judicial conduct must avoid the appearance of bias or prejudice",
        "fed_authority": "28 U.S.C. 455(a) — Disqualification of justice, judge, or magistrate",
        "pa_authority": "Pa. Code of Judicial Conduct Canon 2.3",
    },
    "BIAS_PUNITIVE_APPEAL": {
        "rule": "Judicial impartiality",
        "statute": "Punitive or deterrent language directed at a party may indicate bias",
        "fed_authority": "28 U.S.C. 455(a)",
        "pa_authority": "Pa. Code of Judicial Conduct Canon 2.3",
    },
    "PRIMA_FACIE_IGNORED": {
        "rule": "Prima facie evidence standard",
        "statute": "When prima facie evidence establishes an element, the burden shifts to the opposing party to rebut",
        "fed_authority": "Texas Dep't of Community Affairs v. Burdine, 450 U.S. 248 (1981)",
        "pa_authority": "Corestates Bank, N.A. v. Cutillo, 723 A.2d 1053 (Pa. Super. 1999)",
    },
    "ADMISSION_CONTRADICTED": {
        "rule": "Judicial admissions / party admissions",
        "statute": "A party's own admission is binding and cannot be contradicted by that party without explanation",
        "fed_authority": "Fed. R. Evid. 801(d)(2) — Opposing Party Statements",
        "pa_authority": "Pa.R.E. 803(25) — Party Admissions",
    },
    "BIAS_PRO_SE_GATEKEEPING": {
        "rule": "Access to courts / pro se protections",
        "statute": "Pro se litigants are entitled to liberal construction of filings",
        "fed_authority": "Haines v. Kerner, 404 U.S. 519 (1972)",
        "pa_authority": "Pa. Const. Art. I Sec. 11 (Open Courts)",
    },
}


def load_audit(case_id: str) -> dict:
    """Load audit data for a case."""
    safe = case_id.replace(":", "_").replace("/", "_")
    path = AUDIT_DIR / f"data_{safe}.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def generate_scorecard_v2(case_id: str) -> str:
    """Generate enhanced scorecard markdown."""
    data = load_audit(case_id)
    if not data:
        return f"# No audit data found for {case_id}"

    case_info = data.get("case", {})
    hierarchy = data.get("hierarchy", {})
    scorecard = data.get("scorecard", {})
    dismissal = data.get("dismissal", [])

    caption = case_info.get("title", scorecard.get("title", case_id))
    court = case_info.get("case_court", scorecard.get("case_court", "UNKNOWN"))
    is_federal = court in ("EDPA", "MDPA", "WDPA", "CA3")

    total_violations = hierarchy.get("total_violations", scorecard.get("fatal_count", 0))
    fatal_count = hierarchy.get("fatal_count", 0)
    serious_count = hierarchy.get("serious_count", 0)
    score = scorecard.get("reversibility_score", 0)

    all_violations = hierarchy.get("all", [])
    sc_violations = scorecard.get("violations", [])

    lines = []

    # ── Title ──
    lines.append(f"# VIOLATION SCORECARD")
    lines.append(f"## {caption}")
    lines.append("")
    lines.append(f"**Case ID:** {case_id}")
    lines.append(f"**Court:** {court}")
    lines.append(f"**Date Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"**Reversibility Score:** {score}/100")
    lines.append("")

    # ── Section 0: How to Read ──
    lines.append("---")
    lines.append("")
    lines.append("## HOW TO READ THIS SCORECARD")
    lines.append("")
    lines.append("This Scorecard identifies structural defects in how the opposing party used legal authority.")
    lines.append("")
    lines.append("Each citation is classified by how it affects the court's ability to rely on it:")
    lines.append("")
    lines.append("| Severity | Meaning | Court Impact |")
    lines.append("|----------|---------|--------------|")
    for sev_key in ["FATAL", "serious", "STANDARD"]:
        s = SEVERITY_EXPLAIN.get(sev_key, {})
        lines.append(f"| **{s.get('label', sev_key)}** | {s.get('meaning', '')} | {s.get('impact', '')} |")
    lines.append("")
    lines.append("> **KEY POINT:** Courts must follow binding precedent. When a party relies on non-binding")
    lines.append("> authority as if it were controlling, it risks producing an invalid ruling that will not")
    lines.append("> withstand appellate review.")
    lines.append("")

    # ── Section 1: Plain-English Summary ──
    lines.append("---")
    lines.append("")
    lines.append("## PLAIN LANGUAGE SUMMARY")
    lines.append("")
    pct_fatal = (fatal_count / max(total_violations, 1)) * 100
    lines.append(f"**{fatal_count}** of **{total_violations}** citations used by the opposing party")
    lines.append(f"cannot legally control this court ({pct_fatal:.0f}% of all cited authority).")
    lines.append("")
    lines.append("This means the opposing party's argument relies heavily on authority that this")
    lines.append("court is **not required to follow**.")
    lines.append("")
    if fatal_count > 5:
        lines.append("If a decision were based on these citations, it would be **vulnerable to being")
        lines.append("overturned on appeal**.")
    lines.append("")

    # ── Section 2: Court Impact Box ──
    lines.append("---")
    lines.append("")
    lines.append("## COURT IMPACT")
    lines.append("")
    lines.append("If the court relies on the flagged citations:")
    lines.append("")
    lines.append("- The ruling may rest on **non-binding authority**")
    lines.append("- This creates **reversible error**")
    lines.append("- The decision may **not withstand appellate review**")
    lines.append("")
    lines.append("> The court must rely only on binding authority when deciding the case.")
    lines.append("> Use of non-binding authority as controlling is legal error.")
    lines.append("")

    # ── Section 2B: Decision Trigger ──
    lines.append("---")
    lines.append("")
    lines.append("## DECISION TRIGGER")
    lines.append("")
    lines.append("If the Court relies on the flagged citations as controlling authority,")
    lines.append("it risks issuing a ruling based on non-binding law, constituting **reversible error**.")
    lines.append("")

    # ── Section 2C: Burden Shift ──
    lines.append("## BURDEN SHIFT")
    lines.append("")
    lines.append("Once non-binding authority is identified, the burden shifts to the opposing party")
    lines.append("to demonstrate that the cited authority is controlling or otherwise applicable")
    lines.append("to the issues before this Court.")
    lines.append("")

    # ── Section 2D: Authority Reliability Score ──
    binding_count = total_violations - fatal_count - serious_count
    binding_pct = (binding_count / max(total_violations, 1)) * 100
    nonbinding_pct = ((fatal_count + serious_count) / max(total_violations, 1)) * 100
    lines.append("## AUTHORITY RELIABILITY")
    lines.append("")
    lines.append(f"| Metric | Count | Percentage |")
    lines.append(f"|--------|-------|------------|")
    lines.append(f"| Binding citations | {binding_count} | {binding_pct:.0f}% |")
    lines.append(f"| Non-binding citations | {fatal_count + serious_count} | {nonbinding_pct:.0f}% |")
    lines.append(f"| Total analyzed | {total_violations} | 100% |")
    lines.append("")
    if nonbinding_pct > 40:
        lines.append("**Conclusion:** The opposing party's argument relies **primarily on non-binding authority**.")
    elif nonbinding_pct > 20:
        lines.append("**Conclusion:** A significant portion of the opposing party's citations are non-binding.")
    else:
        lines.append("**Conclusion:** Most cited authority is binding, but flagged citations remain problematic.")
    lines.append("")

    # ── Section 3: Violation Summary Table ──
    lines.append("---")
    lines.append("")
    lines.append("## VIOLATIONS BY TYPE")
    lines.append("")

    # Consolidate violations by type
    by_type = {}
    for v in all_violations:
        vtype = v.get("violation_type", "UNKNOWN")
        if vtype not in by_type:
            by_type[vtype] = {"count": 0, "severity": v.get("severity", "?"), "citations": [], "sources": set()}
        by_type[vtype]["count"] += 1
        cite = v.get("citation", "")
        if cite and cite not in by_type[vtype]["citations"]:
            by_type[vtype]["citations"].append(cite)
        src = v.get("source_file", "")
        if src:
            by_type[vtype]["sources"].add(display_source(src))

    lines.append("| Type | Count | Severity | Impact | Governing Rule |")
    lines.append("|------|-------|----------|--------|----------------|")
    for vtype, info in sorted(by_type.items(), key=lambda x: -x[1]["count"]):
        sev = info["severity"].upper()
        sev_info = SEVERITY_EXPLAIN.get(sev, SEVERITY_EXPLAIN.get(info["severity"], {}))
        impact = sev_info.get("meaning", "See above")
        rule_info = VIOLATION_RULES.get(vtype, {})
        rule = rule_info.get("rule", "Court rules")
        lines.append(f"| {vtype} | {info['count']} | **{sev}** | {impact} | {rule} |")
    lines.append("")

    # ── Section 4: Detailed Violations (consolidated) ──
    lines.append("---")
    lines.append("")
    lines.append("## DETAILED VIOLATIONS")
    lines.append("")

    for vtype, info in sorted(by_type.items(), key=lambda x: -x[1]["count"]):
        sev = info["severity"].upper()
        sev_info = SEVERITY_EXPLAIN.get(sev, SEVERITY_EXPLAIN.get(info["severity"], {}))
        rule_info = VIOLATION_RULES.get(vtype, {})

        lines.append(f"### [{sev}] {vtype} ({info['count']} instances)")
        lines.append("")
        lines.append(f"**What this means:** {sev_info.get('impact', 'See severity key above')}")
        lines.append("")
        lines.append(f"**Governing Rule:** {rule_info.get('statute', 'Stare decisis doctrine')}")
        lines.append("")

        auth_key = "fed_authority" if is_federal else "pa_authority"
        if rule_info.get(auth_key):
            lines.append(f"**Controlling Authority:** {rule_info[auth_key]}")
            lines.append("")

        # List unique citations
        if info["citations"]:
            lines.append("**Citations flagged:**")
            seen = set()
            for cite in info["citations"][:10]:
                if cite not in seen:
                    seen.add(cite)
                    lines.append(f"- {cite}")
            if len(info["citations"]) > 10:
                lines.append(f"- *(and {len(info['citations']) - 10} more)*")
            lines.append("")

        # Source files
        if info["sources"]:
            lines.append(f"**Found in:** {', '.join(sorted(info['sources'])[:5])}")
            lines.append("")

    # ── Section 5: Statutes & Rules Implicated ──
    lines.append("---")
    lines.append("")
    lines.append("## STATUTES & RULES IMPLICATED")
    lines.append("")

    statutes_seen = set()
    for vtype in by_type:
        rule_info = VIOLATION_RULES.get(vtype, {})
        auth_key = "fed_authority" if is_federal else "pa_authority"
        auth = rule_info.get(auth_key, "")
        if auth and auth not in statutes_seen:
            statutes_seen.add(auth)
            lines.append(f"- **{auth}** — {rule_info.get('rule', '')}")

    # Also pull from statutes_registry
    for v in all_violations[:20]:
        cite = v.get("citation", "")
        if cite:
            summary = statute_lookup(cite)
            if summary and cite not in statutes_seen:
                statutes_seen.add(cite)
                lines.append(f"- **{cite}** — {summary}")
    lines.append("")

    # ── Section 6: Recommended Actions ──
    lines.append("---")
    lines.append("")
    lines.append("## RECOMMENDED ACTIONS")
    lines.append("")

    rec = scorecard.get("recommended_filing", "")
    if rec:
        lines.append(f"{rec}")
        lines.append("")

    top_args = scorecard.get("top_arguments", [])
    if top_args:
        lines.append("**Strongest arguments:**")
        seen_args = set()
        for arg in top_args[:5]:
            if arg not in seen_args:
                seen_args.add(arg)
                lines.append(f"- {arg[:150]}")
        lines.append("")

    # ── Section 7: Conclusion (Kill Shot) ──
    lines.append("---")
    lines.append("")
    lines.append("## CONCLUSION")
    lines.append("")
    lines.append("The opposing party's argument is structurally defective because it relies on authority")
    lines.append("that this Court is not required to follow. Any ruling based on the flagged citations")
    lines.append("would rest on a legally insufficient foundation.")
    lines.append("")
    lines.append("> This analysis is based on established precedent hierarchy principles and does not")
    lines.append("> require interpretation of case-specific facts.")
    lines.append("")

    # ── Section 8: Required Response ──
    if fatal_count > 0 or serious_count > 0:
        lines.append("## REQUIRED RESPONSE")
        lines.append("")
        lines.append("Because the opposing party relies on non-binding authority:")
        lines.append("")
        if fatal_count > 0:
            lines.append("- A **Motion to Strike** is appropriate to remove improper citations")
        if serious_count > 0:
            lines.append("- A **Motion for Reconsideration** is appropriate if the Court relied on them")
        lines.append("")
        lines.append("These motions follow directly from the defects identified above.")
        lines.append("")

    # ── Footer ──
    lines.append("---")
    lines.append("")
    lines.append(f"*Generated by Beezle AI Legal Decision Engine — {datetime.now().strftime('%Y-%m-%d %H:%M')}*")
    lines.append(f"*This scorecard is a factual analysis of cited authorities, not legal advice.*")

    return "\n".join(lines)


def regenerate_all_scorecards():
    """Regenerate scorecards for all cases with audit data."""
    results = []
    for data_file in sorted(AUDIT_DIR.glob("data_*.json")):
        # Extract case_id from filename
        stem = data_file.stem.replace("data_", "")
        # Convert back: fed_2_26-cv-00123 -> fed:2:26-cv-00123
        parts = stem.split("_", 2)
        if len(parts) >= 2:
            case_id = f"{parts[0]}:{parts[1]}"
            if len(parts) > 2:
                case_id += f":{parts[2]}"
        else:
            case_id = stem

        md = generate_scorecard_v2(case_id)
        out_path = OUTPUT_DIR / f"scorecard_{stem}.md"
        out_path.write_text(md, encoding="utf-8")
        results.append({"case_id": case_id, "path": str(out_path), "lines": len(md.splitlines())})
        log.info(f"regenerated scorecard: {case_id} ({len(md.splitlines())} lines)")

    return results


if __name__ == "__main__":
    results = regenerate_all_scorecards()
    for r in results:
        print(f"  {r['case_id']}: {r['lines']} lines -> {r['path']}")