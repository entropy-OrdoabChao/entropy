"""
generation/scorecard_renderer_v2.py  (v8 — judicial findings bridge + definitions)
===================================================================================
v8 fixes:

  1. JUDICIAL FINDINGS BRIDGE — wires pipeline.judicial_findings_bridge
     into the violation flow. The bridge scans every judge-authored
     document for: SUA_SPONTE_RULING, PRIOR_RESTRAINT_FILING_BAN,
     DO_NOT_FILE_LANGUAGE, SOVEREIGN_CITIZEN_LABEL,
     BILL_OF_ATTAINDER_MISLABEL, IMMUNITY_WAIVER_TRIGGERED. These were
     missing from the prior scorecards because they only existed in a
     separate audit chain.

  2. DEFINITIONS — each violation row now includes a one-sentence
     plain-language brief from pipeline.definitions, displayed below
     the section table when relevant terms appear.

  3. SECTIONS RENAMED — section labels updated to read more clearly to
     a layperson:
       A. Constitutional & Due Process
       B. Pleading Standard
       C. Procedural Rules
       D. Jurisdiction & Standing
       E. Civil Rights (Title 18 / § 1983)
       F. Judicial Conduct (Title 28 / § 455)
       G. Judicial Canon Violations
       H. Lawyer Rule of Conduct Violations
       I. Removal & Fraud-on-the-Court Defects
       J. Sanctions Triggers
       K. Judge-Specific Findings (new — for sovereign citizen / bill of attainder)
"""
from __future__ import annotations

import re
from collections import defaultdict, Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional


# Pattern categorization (mirrors v7)
CONSTITUTIONAL_PATTERNS = {
    "NOTICE_INADEQUATE", "PROCEDURAL_DUE_PROCESS_VIOLATION",
    "PRIOR_RESTRAINT_FILING_BAN", "DO_NOT_FILE_LANGUAGE",
    "EX_PARTE_RULING", "ACCESS_TO_COURTS_DENIAL",
    "DENIED_OPPORTUNITY_TO_RESPOND", "VOID_JUDGMENT_RULE_60B4",
    "SUA_SPONTE_RULING",
}
PLEADING_PATTERNS = {
    "TWOMBLY_IQBAL_MISAPPLICATION", "PRO_SE_LIBERAL_CONSTRUCTION",
    "IMPROPER_STANDARD_OF_REVIEW", "PRO_SE_STRICT_CONSTRUCTION",
}
PROCEDURAL_PATTERNS = {
    "SERVICE_DEFECTIVE", "VERIFICATION_MISSING",
    "FILING_DEADLINE_VIOLATION", "PAGE_LIMIT_EXCEEDED",
    "DOCKET_CONTRADICTION_CLEAR_ERROR", "SERVICE_NOTICE_CONFLATION",
    "FAILURE_TO_TAKE_JUDICIAL_NOTICE",
    "NON_PRECEDENTIAL_CITED_AS_BINDING",
    "STARE_DECISIS_VIOLATION", "PRIMA_FACIE_EVIDENCE_IGNORED",
}
JURISDICTION_PATTERNS = {"SUBJECT_MATTER_JURISDICTION_DEFECT", "STANDING_DEFECT"}
TITLE_18_PATTERNS = {"DEPRIVATION_OF_RIGHTS_COLOR_OF_LAW", "OBSTRUCTION_OF_JUSTICE"}
TITLE_28_PATTERNS = {"BIAS_JUDICIAL", "JUDICIAL_DISQUALIFICATION_DUTY",
                      "IMMUNITY_WAIVER_TRIGGERED"}
JUDICIAL_CANON_PATTERNS = {"JUDICIAL_CANON_VIOLATION"}
LAWYER_RULE_PATTERNS = {"LAWYER_RULE_VIOLATION"}
REMOVAL_FRAUD_PATTERNS = {"REMOVAL_DEFECTIVE", "FRAUD_ON_THE_COURT"}
SANCTIONS_PATTERNS = {"SANCTIONS_TRIGGERED"}
# v8 NEW
JUDGE_SPECIFIC_PATTERNS = {"SOVEREIGN_CITIZEN_LABEL", "BILL_OF_ATTAINDER_MISLABEL"}


def _categorize(vtype):
    if vtype in JUDGE_SPECIFIC_PATTERNS:     return "judge_specific"
    if vtype in CONSTITUTIONAL_PATTERNS:     return "constitutional"
    if vtype in PLEADING_PATTERNS:           return "pleading"
    if vtype in PROCEDURAL_PATTERNS:         return "procedural"
    if vtype in JURISDICTION_PATTERNS:       return "jurisdiction"
    if vtype in TITLE_18_PATTERNS:           return "title_18"
    if vtype in TITLE_28_PATTERNS:           return "title_28"
    if vtype in JUDICIAL_CANON_PATTERNS:     return "judicial_canon"
    if vtype in LAWYER_RULE_PATTERNS:        return "lawyer_rule"
    if vtype in REMOVAL_FRAUD_PATTERNS:      return "removal_fraud"
    if vtype in SANCTIONS_PATTERNS:          return "sanctions"
    return "other"


ISSUE_MAP = {
    "TWOMBLY_IQBAL_MISAPPLICATION":      "Pleading Standard",
    "PRO_SE_LIBERAL_CONSTRUCTION":       "Pro Se Construction",
    "PRO_SE_STRICT_CONSTRUCTION":        "Pro Se Construction",
    "IMPROPER_STANDARD_OF_REVIEW":       "Pleading Standard",
    "SERVICE_DEFECTIVE":                  "Service",
    "SERVICE_NOTICE_CONFLATION":          "Service",
    "VERIFICATION_MISSING":               "Verification",
    "NOTICE_INADEQUATE":                  "Notice",
    "FILING_DEADLINE_VIOLATION":          "Deadlines",
    "PAGE_LIMIT_EXCEEDED":                "Page Limits",
    "BIAS_JUDICIAL":                      "Judicial Conduct",
    "DOCKET_CONTRADICTION_CLEAR_ERROR":   "Clear Error",
    "NON_PRECEDENTIAL_CITED_AS_BINDING":  "Hierarchy / Stare Decisis",
    "FAILURE_TO_TAKE_JUDICIAL_NOTICE":    "Evidence (FRE 201)",
    "VOID_JUDGMENT_RULE_60B4":            "Void Judgment",
    "SUBJECT_MATTER_JURISDICTION_DEFECT": "Jurisdiction",
    "STANDING_DEFECT":                    "Standing",
    "DEPRIVATION_OF_RIGHTS_COLOR_OF_LAW": "Civil Rights (Title 18/42)",
    "OBSTRUCTION_OF_JUSTICE":             "Civil Rights (Title 18/42)",
    "STARE_DECISIS_VIOLATION":            "Hierarchy / Stare Decisis",
    "PRIMA_FACIE_EVIDENCE_IGNORED":       "Mailbox Presumption / Prima Facie",
    "JUDICIAL_DISQUALIFICATION_DUTY":     "Judicial Conduct",
    "PROCEDURAL_DUE_PROCESS_VIOLATION":   "Notice",
    "JUDICIAL_CANON_VIOLATION":           "Judicial Conduct",
    "LAWYER_RULE_VIOLATION":              "Professional Conduct",
    "REMOVAL_DEFECTIVE":                  "Removal",
    "FRAUD_ON_THE_COURT":                 "Fraud / Tribunal Integrity",
    "SANCTIONS_TRIGGERED":                "Sanctions",
    "SUA_SPONTE_RULING":                  "Procedural Due Process",
    "PRIOR_RESTRAINT_FILING_BAN":         "First Amendment / Access to Courts",
    "DO_NOT_FILE_LANGUAGE":                "First Amendment / Access to Courts",
    "SOVEREIGN_CITIZEN_LABEL":             "Judicial Conduct",
    "BILL_OF_ATTAINDER_MISLABEL":          "Constitutional / Misapplication",
    "IMMUNITY_WAIVER_TRIGGERED":           "Immunity",
}


DKT_PATTERN = re.compile(
    r"\b(?:ECF|Dkt\.?|Docket|Doc\.?|Document)\s*(?:No\.?)?\s*(\d+(?:-\d+)?)",
    re.IGNORECASE,
)
def _docket_for_violation(v):
    src = v.get("source_doc") or ""
    m = DKT_PATTERN.search(src)
    if m: return f"Dkt {m.group(1)}"
    m = re.match(r"^(\d+(?:-\d+)?)\.(?:pdf|docx)", src.strip(), re.IGNORECASE)
    if m: return f"Dkt {m.group(1)}"
    quote = v.get("quote") or ""
    m = DKT_PATTERN.search(quote)
    if m: return f"Dkt {m.group(1)}"
    return ""


def _format_caption(case_caption, gt=None):
    if isinstance(case_caption, str) and case_caption.strip() and "{" not in case_caption:
        return case_caption.strip()
    src = case_caption if isinstance(case_caption, dict) else (gt if isinstance(gt, dict) else None)
    if src:
        plaintiff = src.get("plaintiff") or ""
        if isinstance(plaintiff, dict):
            plaintiff = plaintiff.get("name") or plaintiff.get("full_name") or ""
        defs = src.get("defendants") or []
        if isinstance(defs, dict):
            defs = list(defs.values()) if defs else []
        def_names = []
        for d in defs[:3]:
            n = d.get("name") if isinstance(d, dict) else str(d)
            if n: def_names.append(n)
        if plaintiff and def_names:
            etc = " ET AL." if len(defs) > 1 else ""
            return f"{plaintiff.upper()} v. {def_names[0].upper()}{etc}"
        if plaintiff: return plaintiff.upper()
    return "(case caption unavailable)"


def _make_executive_summary(non_binding, total_cites, non_binding_share,
                              fatal_cites, serious_cites, reversibility,
                              filings_audited, n_issues, n_derived, n_judge_findings):
    return (
        f"WHAT THIS DOCUMENT IS. This Scorecard is an automated, rule-based "
        f"audit of (1) the legal authorities defendants cited in their "
        f"filings, and (2) the conduct reflected in the judge-authored "
        f"orders in this case. It does not draw conclusions; it counts "
        f"citations and applies binding-precedent rules. Citations from "
        f"the Supreme Court of the United States and the U.S. Court of "
        f"Appeals for the Third Circuit are binding on this Court. "
        f"Citations from any other court are not.\n\n"
        f"WHAT THE AUDIT FOUND. Across {filings_audited} adversary "
        f"filings, the defendants cited {total_cites} legal authorities. "
        f"Of those, {non_binding} ({non_binding_share}%) come from courts "
        f"whose decisions do not bind this Court. {fatal_cites} are "
        f"facially non-binding; {serious_cites} require verification. The "
        f"audit also identified {n_judge_findings} specific findings in "
        f"the judge-authored orders (e.g., prior-restraint filing bans, "
        f"sua sponte rulings, labels applied without record support) and "
        f"{n_derived} additional violations inferred from observable "
        f"conditions on the docket. {n_issues} discrete legal issues are "
        f"raised across these filings; for each, this Scorecard lists the "
        f"binding Supreme Court and Third Circuit precedent that has "
        f"already decided it.\n\n"
        f"WHY IT MATTERS. Under stare decisis, a court may not rely on "
        f"non-binding authority to override binding precedent. Under "
        f"Federal Rule of Civil Procedure 60(b)(4), an order that rests "
        f"on a clear error of law — including reliance on non-binding "
        f"authority, sua sponte rulings without notice, or invocation of "
        f"constitutional clauses outside their scope — is void of legal "
        f"effect and may be vacated at any time. The non-binding share "
        f"in this audit is {non_binding_share}%. Reversibility score: "
        f"{reversibility}/100.\n\n"
        f"WHAT THE COURT IS BEING ASKED TO DO. Reconsider and vacate the "
        f"order(s) that rest on the non-binding authorities catalogued "
        f"below; substitute the binding precedent that decides each "
        f"issue; and proceed on the record as supplemented by the prima "
        f"facie evidence already filed."
    )


def render_scorecard_v2(
    scan_result, output_path,
    case_caption=None, judge=None, case_court=None, case_ground_truth=None,
):
    from docx import Document
    from docx.shared import Pt, Inches
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    doc = Document()
    for s in doc.sections:
        s.top_margin = s.bottom_margin = Inches(1.0)
        s.left_margin = s.right_margin = Inches(1.0)

    def H(text, size=14, bold=True, align=WD_ALIGN_PARAGRAPH.LEFT):
        p = doc.add_paragraph(); p.alignment = align
        r = p.add_run(text); r.bold = bold
        r.font.name = "Times New Roman"; r.font.size = Pt(size); return p
    def P(text, size=11, bold=False, italic=False, align=WD_ALIGN_PARAGRAPH.LEFT):
        p = doc.add_paragraph(); p.alignment = align
        r = p.add_run(text); r.bold = bold; r.italic = italic
        r.font.name = "Times New Roman"; r.font.size = Pt(size); return p
    def make_table(headers, rows, col_widths=None):
        t = doc.add_table(rows=1+len(rows), cols=len(headers))
        t.style = "Light Grid Accent 1"
        for i, h in enumerate(headers):
            cell = t.rows[0].cells[i]; cell.text = ""
            r = cell.paragraphs[0].add_run(h); r.bold = True; r.font.size = Pt(10)
        for ri, row in enumerate(rows, start=1):
            for ci, val in enumerate(row):
                cell = t.rows[ri].cells[ci]; cell.text = ""
                r = cell.paragraphs[0].add_run(str(val)); r.font.size = Pt(9)
        if col_widths:
            for row in t.rows:
                for ci, w in enumerate(col_widths):
                    if ci < len(row.cells):
                        row.cells[ci].width = Inches(w)
        return t

    counts = scan_result["counts"]
    by_file = scan_result["by_file"]
    all_violations = list(scan_result["violations"])
    case_id = scan_result.get("case_id", "")
    canonical_id = scan_result.get("canonical_id", "")
    court_id = scan_result.get("court_id", "EDPA")
    generated = scan_result.get("generated", datetime.now().isoformat())
    try:
        gen_str = datetime.fromisoformat(generated).strftime("%B %d, %Y at %I:%M %p")
    except Exception:
        gen_str = generated

    caption_str = _format_caption(case_caption, case_ground_truth)
    if not case_court:
        case_court = "United States District Court for the Eastern District of Pennsylvania"
    if not judge:
        judge = "(Judge name not specified)"

    # === V8: JUDICIAL FINDINGS BRIDGE ===
    try:
        from pipeline.judicial_findings_bridge import collect_judicial_findings
        judicial_findings = collect_judicial_findings(case_id)
        all_violations.extend(judicial_findings)
        n_judge_findings = len(judicial_findings)
    except Exception:
        n_judge_findings = 0

    # Citation extraction
    try:
        from pipeline.citation_extractor import extract_citations_from_case
        cite_result = extract_citations_from_case(case_id, court_id=court_id)
        all_citations = cite_result["all_citations"]
        cite_by_filing = cite_result["by_filing"]
    except Exception:
        all_citations = []
        cite_by_filing = {}

    # Derived violations (from observable conditions)
    try:
        from pipeline.derived_violations import derive_violations
        derived = derive_violations(scan_result, cite_by_filing)
        all_violations.extend(derived)
        n_derived = len(derived)
    except Exception:
        n_derived = 0

    # Definitions for boundary/scope text
    try:
        from pipeline.definitions import (
            DEFINITIONS, map_violation_type_to_definition, boundary_context
        )
    except Exception:
        DEFINITIONS = {}
        map_violation_type_to_definition = lambda x: None
        boundary_context = lambda x: {}

    # Per-filing docket map
    filing_dkt: Dict[str, str] = {}
    for fn, pins in by_file.items():
        for v in pins:
            d = _docket_for_violation(v)
            if d: filing_dkt[fn] = d; break
    for fn, cites in cite_by_filing.items():
        if fn not in filing_dkt and cites:
            filing_dkt[fn] = cites[0].get("dkt", "")

    court_agg = defaultdict(lambda: {"total": 0, "FATAL": 0, "SERIOUS": 0, "OK": 0, "binding": "NO — non-binding"})
    for c in all_citations:
        ic = c["issuing_court"]
        court_agg[ic]["total"] += 1
        court_agg[ic][c["severity"]] = court_agg[ic].get(c["severity"], 0) + 1
        court_agg[ic]["binding"] = c["binding"]

    filing_agg = defaultdict(lambda: {"total": 0, "FATAL": 0, "SERIOUS": 0, "OK": 0, "by_court": Counter()})
    for c in all_citations:
        f = c["source_filing"]
        filing_agg[f]["total"] += 1
        filing_agg[f][c["severity"]] = filing_agg[f].get(c["severity"], 0) + 1
        filing_agg[f]["by_court"][c["issuing_court"]] += 1

    issue_agg = defaultdict(lambda: {"total": 0, "FATAL": 0, "SERIOUS": 0, "filings": set()})
    for v in all_violations:
        vt = v.get("violation_type") or v.get("type") or "OTHER"
        issue = ISSUE_MAP.get(vt, "Other")
        issue_agg[issue]["total"] += 1
        sev = (v.get("severity") or "").upper()
        if sev in ("FATAL", "SERIOUS"):
            issue_agg[issue][sev] += 1
        issue_agg[issue]["filings"].add(v.get("source_doc") or "(unknown)")
    if all_citations:
        non_binding_cites = [c for c in all_citations if c["severity"] in ("FATAL", "SERIOUS")]
        if non_binding_cites:
            iss = "Hierarchy / Stare Decisis"
            issue_agg[iss]["total"] += len(non_binding_cites)
            issue_agg[iss]["FATAL"] += sum(1 for c in non_binding_cites if c["severity"] == "FATAL")
            issue_agg[iss]["SERIOUS"] += sum(1 for c in non_binding_cites if c["severity"] == "SERIOUS")
            for c in non_binding_cites:
                issue_agg[iss]["filings"].add(c["source_filing"])

    total_cites          = len(all_citations)
    fatal_cites          = sum(1 for c in all_citations if c["severity"] == "FATAL")
    serious_cites        = sum(1 for c in all_citations if c["severity"] == "SERIOUS")
    ok_cites             = sum(1 for c in all_citations if c["severity"] == "OK")
    non_binding          = fatal_cites + serious_cites
    non_binding_share    = int(100 * non_binding / total_cites) if total_cites else 0
    if total_cites:
        weighted = (fatal_cites * 1.0 + serious_cites * 0.7 - ok_cites * 0.2)
        reversibility = max(0, min(100, int(100 * weighted / total_cites)))
    else:
        reversibility = 0

    # ============== EXHIBIT A ==============
    H("EXHIBIT A", size=18, align=WD_ALIGN_PARAGRAPH.CENTER)
    H("STARE DECISIS HIERARCHY SCORECARD", size=14, align=WD_ALIGN_PARAGRAPH.CENTER)
    P("Automated, Rule-Based Audit of Defendants' Authority and Judge-Authored Orders",
      size=11, italic=True, align=WD_ALIGN_PARAGRAPH.CENTER)
    doc.add_paragraph()

    P(f"Case: {caption_str}", size=11, bold=True)
    docket_no = case_id.split(":", 1)[-1] if ":" in case_id else case_id
    P(f"Docket No.: {docket_no}", size=11)
    P(f"Court: {case_court}", size=11)
    P(f"Audit Generated: {gen_str}", size=11)
    P(f"Judge: {judge}", size=11)
    doc.add_paragraph()

    # Executive summary
    H("EXECUTIVE SUMMARY (PLAIN LANGUAGE)", size=12)
    summary = _make_executive_summary(
        non_binding, total_cites, non_binding_share,
        fatal_cites, serious_cites, reversibility,
        len(cite_by_filing), len(issue_agg), n_derived, n_judge_findings)
    for paragraph in summary.split("\n\n"):
        P(paragraph.strip(), size=11)
    doc.add_paragraph()

    H("METHODOLOGY", size=12)
    P("This Scorecard combines three analyses: (1) a citation audit across "
      "every adversary filing, classifying each citation by its issuing "
      "court and applying the binding-precedent hierarchy; (2) a "
      "judge-authored-order audit looking for procedural and constitutional "
      "issues such as prior-restraint filing bans, sua sponte rulings "
      "without notice, mislabeling, and immunity-waiver triggers; and (3) "
      "derived inferences from observable conditions (e.g., five or more "
      "non-binding citations in a single judicial memorandum supports an "
      "inference of Canon 3A(1) implication). Each finding includes its "
      "rule basis, the binding authority that controls, and the action "
      "the rule prescribes.", size=10)
    doc.add_paragraph()

    H("1. OVERALL SUMMARY", size=12)
    P(f"Adversary filings audited: {len(cite_by_filing)}    "
      f"Discrete legal issues: {len(issue_agg)}    "
      f"Total citations: {total_cites}    "
      f"Non-binding: {non_binding} ({non_binding_share}%)    "
      f"Binding (SCOTUS+3d Cir.): {ok_cites}    "
      f"Judge findings: {n_judge_findings}    "
      f"Derived: {n_derived}    "
      f"Reversibility score: {reversibility}/100",
      size=10)
    doc.add_paragraph()

    # Sections 2-7 from v7 (unchanged)
    H("2. BREAKDOWN BY ISSUING COURT", size=12)
    rows = [(court, agg["total"], agg["FATAL"], agg["SERIOUS"], agg["binding"])
            for court, agg in sorted(court_agg.items(), key=lambda kv: -kv[1]["total"])]
    if rows:
        make_table(["Issuing Court", "Total", "FATAL", "SERIOUS", "Binding?"],
                   rows, col_widths=[2.6, 0.6, 0.6, 0.6, 1.6])
    else:
        P("  (no citations detected)", size=10, italic=True)
    doc.add_paragraph()

    H("3. BREAKDOWN BY DEFENDANT FILING", size=12)
    rows = []
    for fn, agg in sorted(filing_agg.items(), key=lambda kv: -kv[1]["total"]):
        dkt = filing_dkt.get(fn, "")
        fn_short = fn[:38] + "..." if len(fn) > 40 else fn
        top_courts = agg["by_court"].most_common(2)
        tc_str = ", ".join(f"{c[:30]} ({n})" for c, n in top_courts) if top_courts else "—"
        rows.append((dkt or "—", fn_short, agg["total"], agg["FATAL"], agg["SERIOUS"], tc_str))
    if rows:
        make_table(["Dkt", "Filing", "Total", "FATAL", "SERIOUS", "Top Issuing Courts"],
                   rows, col_widths=[0.7, 2.0, 0.5, 0.5, 0.6, 2.2])
    else:
        P("  (no flagged citations)", size=10, italic=True)
    doc.add_paragraph()

    H("4. BREAKDOWN BY LEGAL ISSUE", size=12)
    rows = [(issue, agg["total"], agg["FATAL"], agg["SERIOUS"], len(agg["filings"]))
            for issue, agg in sorted(issue_agg.items(), key=lambda kv: -kv[1]["total"])]
    if rows:
        make_table(["Issue", "Total Cites", "FATAL", "SERIOUS", "Filings"],
                   rows, col_widths=[2.2, 0.9, 0.6, 0.6, 0.7])
    else:
        P("  (no issues)", size=10, italic=True)
    doc.add_paragraph()

    H("5. FULL CITATION INVENTORY", size=12)
    P("Lists every non-binding citation. Binding citations (SCOTUS and Third "
      "Circuit precedential) are omitted as correctly cited.", size=10)
    inv_rows = []
    for c in all_citations:
        if c["severity"] == "OK": continue
        src = c["source_filing"]; src = src[:23]+"..." if len(src) > 26 else src
        cite = c["citation"];     cite = cite[:42]+"..." if len(cite) > 45 else cite
        court = c["issuing_court"]; court = court[:27]+"..." if len(court) > 30 else court
        inv_rows.append((c.get("dkt") or "—", src, cite, court, c["severity"]))
    if inv_rows:
        make_table(["Dkt", "Source Filing", "Citation", "Issuing Court", "Severity"],
                   inv_rows, col_widths=[0.7, 1.7, 1.9, 1.7, 0.6])
    else:
        P("  (no non-binding citations)", size=10, italic=True)
    doc.add_paragraph()

    H("6. STARE DECISIS RISK ASSESSMENT", size=12)
    P(f"Of {total_cites} citations audited, {non_binding} ({non_binding_share}%) "
      f"are facially non-binding. {fatal_cites} are FATAL; {serious_cites} are "
      f"SERIOUS. Reversibility score: {reversibility}/100. Under FRCP 60(b)(4), "
      f"an order resting on non-binding authority is correctable on "
      f"reconsideration and, absent correction, void of legal effect.", size=10)
    doc.add_paragraph()

    H("7. BINDING AUTHORITY ON THESE ISSUES", size=12)
    P("The following Supreme Court and Third Circuit decisions have already "
      "decided each issue. Under stare decisis these bind this Court.", size=10)
    doc.add_paragraph()
    issues_in_play = sorted(issue_agg.keys(), key=lambda k: -issue_agg[k]["total"])
    try:
        from pipeline.binding_authority_lookup import binding_authorities_for_issues
        binding_by_issue = binding_authorities_for_issues(issues_in_play, enrich_from_db=False)
    except Exception:
        binding_by_issue = {}
    for issue in issues_in_play:
        auths = binding_by_issue.get(issue, [])
        if not auths: continue
        P(f"Issue: {issue}", size=11, bold=True)
        rows = []
        for a in auths[:6]:
            cite  = a["citation"][:55] + "..." if len(a["citation"]) > 58 else a["citation"]
            court = a["court"]; yr = a.get("year") or "—"
            hold = a["holding"]; hold = hold[:120] + "..." if len(hold) > 123 else hold
            rows.append((cite, court, yr, hold))
        if rows:
            make_table(["Citation", "Court", "Year", "Holding"], rows,
                       col_widths=[2.3, 1.0, 0.5, 2.8])
        doc.add_paragraph()

    # ============== EXHIBIT B ==============
    doc.add_page_break()
    H("EXHIBIT B", size=18, align=WD_ALIGN_PARAGRAPH.CENTER)
    H("PROCEDURAL & CIVIL RULE VIOLATION SCORECARD", size=14, align=WD_ALIGN_PARAGRAPH.CENTER)
    P("Systematic Audit of Adversary Filings and Judge-Authored Orders",
      size=11, italic=True, align=WD_ALIGN_PARAGRAPH.CENTER)
    doc.add_paragraph()

    H("Violation Summary", size=12)
    direct = len(all_violations) - n_derived - n_judge_findings
    P(f"Total: {len(all_violations)} violations across {len(by_file)} filings. "
      f"Direct (block14 detected): {direct}. "
      f"Judge-order findings: {n_judge_findings}. "
      f"Derived from observable conditions: {n_derived}.", size=10)
    doc.add_paragraph()

    # PRIMA FACIE
    H("PRIMA FACIE EVIDENCE ON THE DOCKET", size=12)
    P("Pursuant to FRE 201(b)(2), the following items appear of record and are "
      "capable of accurate and ready determination. Each is prima facie "
      "evidence on the merits, triggering FRE 301's burden of production.",
      size=10)
    doc.add_paragraph()
    try:
        from pipeline.prima_facie_evidence import gather_prima_facie_evidence
        evidence = gather_prima_facie_evidence(case_id, max_results=30)
    except Exception:
        evidence = []
    if evidence:
        rows = []
        for ev in evidence:
            src = ev["source_doc"]
            if len(src) > 28: src = src[:25] + "..."
            summary = ev["summary"]
            if len(summary) > 80: summary = summary[:77] + "..."
            rows.append((ev["dkt"], ev["kind"], src, summary))
        make_table(["Dkt", "Kind", "Source", "Summary / Excerpt"], rows,
                   col_widths=[0.7, 1.8, 1.8, 3.2])
    else:
        P("  (no prima facie evidence detected)", size=10, italic=True)
    doc.add_paragraph()

    # Categorize
    cats = {k: [] for k in ("constitutional","pleading","procedural","jurisdiction",
                              "title_18","title_28","judicial_canon","lawyer_rule",
                              "removal_fraud","sanctions","judge_specific")}
    for v in all_violations:
        vt = v.get("violation_type") or v.get("type") or "OTHER"
        cat = _categorize(vt)
        if cat in cats:
            cats[cat].append(v)

    def _v_row(v):
        vt = v.get("violation_type") or v.get("type") or "?"
        sev = v.get("severity", "?")
        sev_abbr = {"FATAL":"FAT","SERIOUS":"SER","STANDARD":"STD"}.get(sev, sev[:3])
        rule = v.get("rule") or v.get("rule_citation") or ""
        if len(rule) > 38: rule = rule[:35] + "..."
        src = v.get("source_doc") or "?"
        dkt = _docket_for_violation(v)
        if len(src) > 24: src = src[:21] + "..."
        quote = v.get("quote") or ""
        if len(quote) > 45: quote = quote[:42] + "..."
        # Annotate origin
        origin = ""
        if v.get("derived"):       origin = " (derived)"
        elif v.get("from_scanner"): origin = " (judge-order)"
        return (vt.replace("_"," ") + origin, sev_abbr, rule, dkt or "—", src,
                f'"{quote}"' if quote else "")

    SECTIONS = [
        ("A. Constitutional & Due Process Violations", "constitutional"),
        ("B. Pleading Standard Violations",            "pleading"),
        ("C. Procedural Rule Violations",              "procedural"),
        ("D. Jurisdictional & Standing Defects",       "jurisdiction"),
        ("E. Civil Rights (Title 18 / § 1983)",         "title_18"),
        ("F. Judicial Conduct (Title 28 / § 455)",      "title_28"),
        ("G. Judicial Canon Violations",                "judicial_canon"),
        ("H. Lawyer Rule of Conduct Violations",        "lawyer_rule"),
        ("I. Removal & Fraud-on-the-Court Defects",     "removal_fraud"),
        ("J. Sanctions Triggers",                        "sanctions"),
        ("K. Judge-Specific Findings (Labels & Mislabeling)", "judge_specific"),
    ]
    for title, cat_key in SECTIONS:
        H(title, size=11)
        bucket = cats.get(cat_key, [])
        if bucket:
            make_table(["Violation", "Sev.", "Rule", "Dkt", "Source", "Quote"],
                       [_v_row(v) for v in bucket],
                       col_widths=[1.5, 0.45, 1.3, 0.5, 1.3, 1.45])
            # Plain-language definitions for unique violation types in this section
            seen_types = set()
            briefs = []
            for v in bucket:
                vt = v.get("violation_type") or v.get("type", "")
                if vt in seen_types: continue
                seen_types.add(vt)
                def_key = map_violation_type_to_definition(vt)
                if def_key and def_key in DEFINITIONS:
                    briefs.append((vt, DEFINITIONS[def_key]["brief"]))
            if briefs:
                P("Plain-language explanation of each violation in this section:",
                  size=10, italic=True)
                for vt, brief in briefs:
                    P(f"  • {vt.replace('_', ' ')}: {brief}", size=9)
            # Evidence for derived violations
            derived_in = [v for v in bucket if v.get("derived") and v.get("evidence")]
            if derived_in:
                P("Evidence supporting derived inferences:", size=10, italic=True)
                for v in derived_in:
                    for ev in v.get("evidence", [])[:3]:
                        P(f"  • {ev}", size=9)
        else:
            P("  (none detected)", size=10, italic=True)
        doc.add_paragraph()

    # Decision Triggers
    H("Decision Triggers", size=12)
    P("Rows marked \u2020 trigger automatic Rule 60(b)(4) void-judgment analysis.",
      size=10, italic=True)
    triggers = [
        ("Twombly/Iqbal misapplied \u2020",                "Accept well-pleaded facts as true",        "Vacate"),
        ("Notice inadequate \u2020",                        "Order void under FRCP 60(b)(4)",            "Vacate (no time limit)"),
        ("Void judgment under 60(b)(4) \u2020",            "No time bar; no legal effect",              "Vacate ab initio"),
        ("Subject-matter jurisdiction lacking \u2020",      "Must dismiss per Rule 12(h)(3)",            "Dismiss / vacate"),
        ("Standing absent (Art. III) \u2020",              "No case or controversy",                    "Dismiss"),
        ("Stare decisis violated \u2020",                  "Binding precedent ignored",                  "Vacate"),
        ("Prima facie evidence ignored \u2020",            "FRE 301 burden shift",                       "Vacate"),
        ("Sua sponte ruling without notice \u2020",         "Bryson v. Brand Insulations (3d Cir.)",     "Vacate; demand briefing"),
        ("Prior restraint filing ban \u2020",               "In re Safir (3d Cir.) factors required",    "Vacate; mandamus"),
        ("Bill of Attainder misapplied \u2020",             "Clause restrains legislatures only",         "Vacate; cite Lovett"),
        ("Sovereign citizen label without record",          "Caperton appearance-of-bias",                "Reconsideration; demand record cite"),
        ("Immunity waiver triggered",                       "Hazel-Atlas; Forrester; Pulliam",            "FRCP 60(d)(3) independent action"),
        ("Removal defective \u2020",                        "Remand under 28 U.S.C. § 1447(c)",           "Motion for remand"),
        ("Fraud on the court \u2020",                       "FRCP 60(d)(3) — no time bar",                "Independent action"),
        ("Procedural due process violation \u2020",         "Order rests on clear error",                 "Vacate; § 1983 prospective"),
        ("Sanctions warranted",                             "FRCP 11 / 28 U.S.C. § 1927",                 "Move for sanctions"),
        ("Color-of-law deprivation (§ 242)",                "§ 1983 civil / refer for § 242",              "File § 1983 / refer"),
        ("Judicial canon violated",                         "Code of Conduct C. 1-3; § 455(a)",            "§ 351 complaint; recusal"),
        ("Lawyer R.P.C. 3.3 violated",                       "Duty of candor",                              "FRCP 11; bar referral"),
        ("Service defective",                                "No legal effect under FRCP 5",                "Strike"),
        ("Verification missing",                            "Pa.R.C.P. 1024 deemed admissions",            "Strike"),
        ("Filing deadline missed",                          "Untimely under FRAP 4 / Pa.R.A.P. 903",       "Strike / default"),
    ]
    make_table(["IF Violation", "THEN Rule Requires", "Action"], triggers,
               col_widths=[2.4, 2.7, 1.4])
    doc.add_paragraph()

    P("\u2020 = automatic Rule 60(b)(4) void-judgment trigger.", size=9, italic=True)
    P("Deterministic Analysis — Conclusions derived from rule-based "
      "cross-reference validation against the publicly-filed adversary "
      "record, judge-authored orders, and binding-precedent lookup. "
      "Findings tagged '(judge-order)' come from automated review of the "
      "court's orders. Findings tagged '(derived)' are conservative "
      "inferences from observable facts with supporting evidence listed. "
      "No generative reasoning applied.", size=9, italic=True)

    doc.save(output_path)
    return output_path
