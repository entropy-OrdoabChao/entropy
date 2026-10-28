"""
generation/scorecard_exhibit.py — hotfix7

Standalone scorecard exhibit generator. Produces a separate .docx
that attaches to every reply brief as Exhibit A.

The exhibit is the "bias receipts" document. It tabulates:
  - Total flagged citations across all defendant filings
  - Fatal vs serious breakdown
  - Per-source-file table (which brief, how many cites, court mix)
  - Per-issuing-court table (D.N.J. vs M.D. Pa. vs W.D. Pa. vs CA3 etc.)
  - Per-issue cluster (verification, garnishment, FCRA, bad faith, etc.)
  - The judge-trap framing language

Used both:
  (a) attached to filings as judicial-notice exhibit
  (b) tracked over time to expose pattern of bias in rulings that ignore
      structural stare-decisis defects on the record

NO LLM CALLS. Pure structural assembly from audit data.
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
from collections import defaultdict, OrderedDict
from datetime import datetime
from pathlib import Path
from generation.source_display import display_source
from typing import TYPE_CHECKING, Dict, List

if TYPE_CHECKING:
    from generation.reply_brief_drafter import ReplyBriefData

# Pull court labels from the drafter for consistency
from generation.reply_brief_drafter import (
    COURT_LABELS,
    BINDING_HIERARCHY,
    NON_BINDING_RATIONALE,
    _clean_citation_text,
)


# ─────────────────────────────────────────────────────────────────────────────
# COLOR / SEVERITY MAPPING
# ─────────────────────────────────────────────────────────────────────────────

SEVERITY_LABEL = {
    "fatal": "FATAL — Facially Non-Binding",
    "serious": "SERIOUS — Verify Binding Effect",
    "minor": "MINOR — Note for Record",
}


# ─────────────────────────────────────────────────────────────────────────────
# AGGREGATIONS
# ─────────────────────────────────────────────────────────────────────────────

def _by_court(data: "ReplyBriefData") -> List[tuple]:
    """Aggregate violations by issuing court."""
    counts = defaultdict(lambda: {"fatal": 0, "serious": 0, "total": 0})
    for viols in data.by_source_file.values():
        for v in viols:
            counts[v.cited_court][v.severity] = counts[v.cited_court].get(
                v.severity, 0
            ) + 1
            counts[v.cited_court]["total"] += 1
    rows = []
    target = data.case_court
    binding = BINDING_HIERARCHY.get(target, {}).get("binding", [])
    for court_code, c in sorted(counts.items(), key=lambda x: -x[1]["total"]):
        is_binding = court_code in binding
        rows.append({
            "court_code": court_code,
            "court_label": COURT_LABELS.get(court_code, court_code),
            "fatal": c.get("fatal", 0),
            "serious": c.get("serious", 0),
            "total": c["total"],
            "is_binding": is_binding,
        })
    return rows


def _by_file(data: "ReplyBriefData") -> List[Dict]:
    rows = []
    for fpath, viols in data.by_source_file.items():
        fatal = sum(1 for v in viols if v.severity == "fatal")
        serious = sum(1 for v in viols if v.severity == "serious")
        court_mix = defaultdict(int)
        for v in viols:
            court_mix[v.cited_court] += 1
        top_courts = ", ".join(
            f"{COURT_LABELS.get(c, c).split(' (')[0]} ({n})"
            for c, n in sorted(court_mix.items(), key=lambda x: -x[1])[:3]
        )
        rows.append({
            "filename": display_source(fpath),
            "total": len(viols),
            "fatal": fatal,
            "serious": serious,
            "top_courts": top_courts,
        })
    rows.sort(key=lambda r: -r["total"])
    return rows


def _by_issue(data: "ReplyBriefData") -> List[Dict]:
    rows = []
    for issue_key, group in data.by_issue.items():
        rows.append({
            "issue": issue_key.replace("_", " ").title(),
            "fatal": group.fatal_count,
            "serious": group.serious_count,
            "total": len(group.violations),
            "files": len(group.source_files),
        })
    rows.sort(key=lambda r: -r["total"])
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# DOCX BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def build_scorecard_exhibit(data: "ReplyBriefData", output_path: Path) -> Path:
    from docx import Document as DocxDocument
    from docx.shared import Pt, Inches, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING

    doc = DocxDocument()

    for section in doc.sections:
        section.top_margin = Inches(1)
        section.bottom_margin = Inches(1)
        section.left_margin = Inches(1.0)
        section.right_margin = Inches(1.0)

    style = doc.styles["Normal"]
    style.font.name = "Times New Roman"
    style.font.size = Pt(11)
    pf = style.paragraph_format
    pf.line_spacing_rule = WD_LINE_SPACING.SINGLE
    pf.space_after = Pt(4)

    # ─── Title ──────────────────────────────────────────────────────────────
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run("EXHIBIT A")
    run.bold = True
    run.font.size = Pt(14)

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run("STARE DECISIS HIERARCHY SCORECARD")
    run.bold = True
    run.font.size = Pt(13)

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(
        "Structural Audit of Defendant's Authority Citations Under "
        "Binding-Precedent Hierarchy"
    )
    run.italic = True
    run.font.size = Pt(11)
    doc.add_paragraph()

    # ─── Case identification ────────────────────────────────────────────────
    p = doc.add_paragraph()
    run = p.add_run("Case: ")
    run.bold = True
    p.add_run(data.case_caption)

    p = doc.add_paragraph()
    run = p.add_run("Docket No.: ")
    run.bold = True
    p.add_run(data.docket_id)

    p = doc.add_paragraph()
    run = p.add_run("Court: ")
    run.bold = True
    p.add_run(COURT_LABELS.get(data.case_court, data.case_court))

    p = doc.add_paragraph()
    run = p.add_run("Audit Generated: ")
    run.bold = True
    p.add_run(datetime.now().strftime("%B %d, %Y at %I:%M %p"))

    if data.judge:
        p = doc.add_paragraph()
        run = p.add_run("Judge: ")
        run.bold = True
        p.add_run(f"The Honorable {data.judge}")
    doc.add_paragraph()

    # ─── Methodology ────────────────────────────────────────────────────────
    p = doc.add_paragraph()
    run = p.add_run("METHODOLOGY")
    run.bold = True
    run.font.size = Pt(12)

    target_label = COURT_LABELS.get(data.case_court, data.case_court)
    binding = BINDING_HIERARCHY.get(data.case_court, {}).get("binding", [])
    binding_labels = [COURT_LABELS.get(c, c) for c in binding]
    binding_str = (
        ", ".join(binding_labels[:-1]) + " and " + binding_labels[-1]
        if len(binding_labels) > 1
        else (binding_labels[0] if binding_labels else "courts of binding jurisdiction")
    )

    doc.add_paragraph(
        f"This Scorecard tabulates each citation in defendant's filings, "
        f"identifies the issuing court, and classifies the citation under "
        f"the binding-precedent hierarchy that governs the {target_label}. "
        f"Under stare decisis, the court is bound only by decisions from "
        f"{binding_str}. Citations from any other court are persuasive "
        f"authority only and cannot be presented as controlling precedent."
    )
    doc.add_paragraph(
        "FATAL flags identify citations that are facially non-binding on "
        "their face under standard hierarchy rules. SERIOUS flags identify "
        "citations whose binding status requires verification before they "
        "can be treated as controlling. The Scorecard is structural only; "
        "it does not address whether each cited decision remains good law, "
        "has been overruled, or has subsequent appellate treatment."
    )
    doc.add_paragraph()

    # ─── Section 1: Overall summary ─────────────────────────────────────────
    p = doc.add_paragraph()
    run = p.add_run("1. OVERALL SUMMARY")
    run.bold = True
    run.font.size = Pt(12)

    n_files = len(data.by_source_file)
    n_issues = len(data.by_issue)
    p = doc.add_paragraph(
        f"Adversary filings audited: {n_files}    "
        f"Discrete legal issues: {n_issues}    "
        f"Total citations flagged: {data.total_fatal + data.total_serious}    "
        f"FATAL: {data.total_fatal}    "
        f"SERIOUS: {data.total_serious}    "
        f"Reversibility score: {data.score}/100 "
    )
    doc.add_paragraph()

    # ─── Section 2: By issuing court ────────────────────────────────────────
    p = doc.add_paragraph()
    run = p.add_run("2. BREAKDOWN BY ISSUING COURT")
    run.bold = True
    run.font.size = Pt(12)

    doc.add_paragraph(
        "The following table reports the distribution of citations across "
        "the courts that issued them. The 'Binding?' column indicates "
        f"whether decisions of that court bind the {target_label}."
    )

    by_court_rows = _by_court(data)
    if by_court_rows:
        t = doc.add_table(rows=1 + len(by_court_rows), cols=5)
        try:
            t.style = "Light Grid Accent 1"
        except KeyError:
            pass
        headers = ["Issuing Court", "Total", "FATAL", "SERIOUS", "Binding?"]
        for ci, h in enumerate(headers):
            cell = t.rows[0].cells[ci]
            cell.text = ""
            run = cell.paragraphs[0].add_run(h)
            run.bold = True
            run.font.size = Pt(10)
        for ri, row in enumerate(by_court_rows, 1):
            cells = t.rows[ri].cells
            cells[0].text = row["court_label"]
            cells[1].text = str(row["total"])
            cells[2].text = str(row["fatal"])
            cells[3].text = str(row["serious"])
            cells[4].text = "YES — binding" if row["is_binding"] else "NO — non-binding"
            for c in cells:
                for para in c.paragraphs:
                    for r in para.runs:
                        r.font.size = Pt(10)
    doc.add_paragraph()

    # ─── Section 3: By source file ──────────────────────────────────────────
    p = doc.add_paragraph()
    run = p.add_run("3. BREAKDOWN BY DEFENDANT FILING")
    run.bold = True
    run.font.size = Pt(12)

    doc.add_paragraph(
        "The following table reports the citation profile of each defendant "
        "filing audited. Each row identifies the filing, the total flagged "
        "citations contained in that filing, the FATAL/SERIOUS breakdown, "
        "and the top three issuing courts cited."
    )

    by_file_rows = _by_file(data)
    if by_file_rows:
        t = doc.add_table(rows=1 + len(by_file_rows), cols=5)
        try:
            t.style = "Light Grid Accent 1"
        except KeyError:
            pass
        headers = ["Filing", "Total", "FATAL", "SERIOUS", "Top Issuing Courts"]
        for ci, h in enumerate(headers):
            cell = t.rows[0].cells[ci]
            cell.text = ""
            run = cell.paragraphs[0].add_run(h)
            run.bold = True
            run.font.size = Pt(10)
        for ri, row in enumerate(by_file_rows, 1):
            cells = t.rows[ri].cells
            cells[0].text = row["filename"]
            cells[1].text = str(row["total"])
            cells[2].text = str(row["fatal"])
            cells[3].text = str(row["serious"])
            cells[4].text = row["top_courts"]
            for c in cells:
                for para in c.paragraphs:
                    for r in para.runs:
                        r.font.size = Pt(9)
    doc.add_paragraph()

    # ─── Section 4: By issue cluster ────────────────────────────────────────
    by_issue_rows = _by_issue(data)
    if by_issue_rows:
        p = doc.add_paragraph()
        run = p.add_run("4. BREAKDOWN BY LEGAL ISSUE")
        run.bold = True
        run.font.size = Pt(12)
        doc.add_paragraph(
            "The flagged citations cluster across the following discrete "
            "legal issues. The reply brief addresses each cluster as a "
            "separate argument."
        )
        t = doc.add_table(rows=1 + len(by_issue_rows), cols=5)
        try:
            t.style = "Light Grid Accent 1"
        except KeyError:
            pass
        headers = ["Issue", "Total Cites", "FATAL", "SERIOUS", "Filings"]
        for ci, h in enumerate(headers):
            cell = t.rows[0].cells[ci]
            cell.text = ""
            run = cell.paragraphs[0].add_run(h)
            run.bold = True
            run.font.size = Pt(10)
        for ri, row in enumerate(by_issue_rows, 1):
            cells = t.rows[ri].cells
            cells[0].text = row["issue"]
            cells[1].text = str(row["total"])
            cells[2].text = str(row["fatal"])
            cells[3].text = str(row["serious"])
            cells[4].text = str(row["files"])
            for c in cells:
                for para in c.paragraphs:
                    for r in para.runs:
                        r.font.size = Pt(10)
        doc.add_paragraph()

    # ─── Section 5: Full citation list ──────────────────────────────────────
    p = doc.add_paragraph()
    run = p.add_run("5. FULL CITATION INVENTORY")
    run.bold = True
    run.font.size = Pt(12)

    doc.add_paragraph(
        "The following table lists every flagged citation in defendant's "
        "filings, in source-file order. Each row identifies the citation "
        "as it appears in the filing, the issuing court, and the severity "
        "of the stare-decisis defect."
    )

    rows_data = []
    for fpath, viols in data.by_source_file.items():
        fname = display_source(fpath)
        for v in viols:
            rows_data.append({
                "file": fname,
                "citation": _clean_citation_text(v.citation),
                "court": COURT_LABELS.get(v.cited_court, v.cited_court),
                "severity": v.severity.upper(),
            })

    if rows_data:
        # Cap at 250 for readability; if more, note overflow
        cap = 250
        display_rows = rows_data[:cap]
        t = doc.add_table(rows=1 + len(display_rows), cols=4)
        try:
            t.style = "Light Grid Accent 1"
        except KeyError:
            pass
        headers = ["Source Filing", "Citation", "Issuing Court", "Severity"]
        for ci, h in enumerate(headers):
            cell = t.rows[0].cells[ci]
            cell.text = ""
            run = cell.paragraphs[0].add_run(h)
            run.bold = True
            run.font.size = Pt(9)
        for ri, row in enumerate(display_rows, 1):
            cells = t.rows[ri].cells
            cells[0].text = row["file"][:30]
            cells[1].text = row["citation"][:120]
            cells[2].text = row["court"]
            cells[3].text = row["severity"]
            for c in cells:
                for para in c.paragraphs:
                    for r in para.runs:
                        r.font.size = Pt(8)
        if len(rows_data) > cap:
            doc.add_paragraph(
                f"({len(rows_data) - cap} additional citation(s) omitted from "
                f"this table for readability; the full list is available in "
                f"the underlying audit data.)"
            )

    doc.add_paragraph()

    # ─── Section 6: Judge-trap framing ──────────────────────────────────────
    p = doc.add_paragraph()
    run = p.add_run("6. STARE DECISIS RISK ASSESSMENT")
    run.bold = True
    run.font.size = Pt(12)

    fatal_pct = 0
    if data.total_fatal + data.total_serious > 0:
        fatal_pct = round(
            100 * data.total_fatal / (data.total_fatal + data.total_serious)
        )

    risk_para = (
        f"Of the {data.total_fatal + data.total_serious} citation(s) audited "
        f"in defendant's filings, {data.total_fatal} ({fatal_pct}%) are "
        f"facially non-binding on the {target_label} under standard hierarchy "
        f"rules. The structural reversibility score for this case is "
        f"{data.score} out of 100 , reflecting the volume "
        f"and severity of stare-decisis defects in defendant's reliance on "
        f"non-binding authority."
    )
    doc.add_paragraph(risk_para)
    doc.add_paragraph(
        "If this court were to accept defendant's reliance on the flagged "
        "citations as a basis for ruling against plaintiff, the resulting "
        "order would rest on authority that does not bind this court. Such "
        "an order would be subject to reversal on appeal for clear error in "
        "the application of stare decisis principles."
    )
    doc.add_paragraph(
        "Plaintiff respectfully requests that the court take judicial notice "
        "of this Scorecard as part of the record on the underlying motion."
    )
    doc.add_paragraph()

    # ─── Footer ─────────────────────────────────────────────────────────────
    p = doc.add_paragraph("_" * 70)
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(
        "This Scorecard was generated by Beezle, a deterministic "
        "stare-decisis audit tool. The audit is structural only and does "
        "not constitute legal advice."
    )
    run.italic = True
    run.font.size = Pt(9)

    # ─── Section 7: Statutes & Rules Implicated (PATCH AUTHORITY_ATTACK) ──
    try:
        from pipeline.statutes_registry import collect_statutes_from_violations

        # Collect from all violations across by_source_file and by_issue
        all_violations = []
        for src_violations in (data.by_source_file or {}).values():
            for v in src_violations:
                if hasattr(v, "rule_violated"):
                    all_violations.append({"rule_violated": v.rule_violated})
                elif isinstance(v, dict):
                    all_violations.append(v)
        for ig in (data.by_issue or {}).values():
            for v in (ig.violations if hasattr(ig, "violations") else []):
                if hasattr(v, "rule_violated"):
                    all_violations.append({"rule_violated": v.rule_violated})
                elif isinstance(v, dict):
                    all_violations.append(v)

        statutes = collect_statutes_from_violations(all_violations)
        if statutes:
            doc.add_paragraph()
            p = doc.add_paragraph()
            run = p.add_run("7. STATUTES & RULES IMPLICATED")
            run.bold = True
            run.font.size = Pt(12)

            doc.add_paragraph(
                "The following statutes and procedural rules govern the issues "
                "raised in defendant's filings. Citations are presented for "
                "reference; full text and analysis appear in the briefing."
            )

            for s in statutes:
                p = doc.add_paragraph()
                p.paragraph_format.left_indent = Inches(0.25)
                run = p.add_run(s["citation"])
                run.bold = True
                p.add_run(f" — {s['summary']}")
    except Exception as _e:
        # Section 7 is non-essential; don't break the scorecard if it fails
        pass


    # ════════════════════════════════════════════════════════════════


    # ════════════════════════════════════════════════════════════════
    # EXHIBIT B: PROCEDURAL & CIVIL RULE VIOLATIONS
    # Appended as separate exhibit in same document
    # ════════════════════════════════════════════════════════════════
    try:
        from pipeline.block14_pinpointer import scan_document_for_violations
        from pipeline.doc_reader import extract_text
        import json as _json

        # === PATH_B_EXHIBIT_READS_PERSISTED_V1 ===
        # Try persisted procedural_violations from the audit JSON first.
        # This is the single source of truth shared with the motion drafter.
        # Fall back to inline scan only if audit JSON is missing or empty.
        case_violations = []
        try:
            _canonical = (
                getattr(data, "canonical_id", "")
                or getattr(data, "case_id", "")
                or ""
            )
            # WSL-safe audit dir (mirror the motion-drafter resolution).
            def _audit_dir_local():
                here = Path(__file__).resolve().parent          # .../generation
                repo = here.parent                               # .../AI_Beezle
                for cand in (repo / "output" / "audit",
                             (_eroot() / "output/audit"),
                             Path.cwd() / "output" / "audit"):
                    if cand.exists():
                        return cand
                return repo / "output" / "audit"
            _adir = _audit_dir_local()
            _audit_p = None
            # 1) direct slug from canonical_id (fed:2:26-cv-00123 -> fed_2_26-cv-00123)
            if _canonical:
                _cand = _adir / f"data_{_canonical.replace(':', '_')}.json"
                if _cand.exists():
                    _audit_p = _cand
            # 2) reconstruct from case_court + docket when canonical missing
            if _audit_p is None:
                _docket = str(getattr(data, "docket_id", "") or "")
                if _docket:
                    _cc = str(getattr(data, "case_court", "") or "").upper()
                    _prefix = "fed" if _cc in ("EDPA", "MDPA", "WDPA", "CA3") else "fed"
                    _cand = _adir / f"data_{_prefix}_{_docket.replace(':', '_')}.json"
                    if _cand.exists():
                        _audit_p = _cand
            # 3) last resort: glob the audit dir for the docket digits
            if _audit_p is None:
                _docket = str(getattr(data, "docket_id", "") or "")
                _key = _docket.split(":")[-1] if _docket else ""
                if _key:
                    for _f in _adir.glob("data_*.json"):
                        if _key in _f.name:
                            _audit_p = _f
                            break
            if _audit_p is not None and _audit_p.exists():
                _audit = _json.loads(_audit_p.read_text(encoding="utf-8"))
                _persisted = _audit.get("procedural_violations") or []
                if _persisted:
                    case_violations = list(_persisted)
        except Exception:
            # Persisted read is best-effort; fall through to inline scan.
            case_violations = []
        # === END PATH_B_EXHIBIT_READS_PERSISTED_V1 ===

        cm_path = (_eroot() / "cases/_consolidated/case_map.json")
        # If persisted source already populated case_violations, skip inline scan.
        if case_violations:
            cm_path = None  # signal to skip the inline scan branch below

        if cm_path is not None and cm_path.exists():
            cm = _json.loads(cm_path.read_text(encoding="utf-8"))
            docket = str(getattr(data, "docket_id", "") or "")
            caption = str(getattr(data, "caption", "") or "")
            matched = None

            for ckey, cinfo in cm.items():
                cm_id = str(cinfo.get("case_id", ""))
                cm_title = str(cinfo.get("title", "")).upper()
                if (docket and docket in cm_id) or (docket and docket in ckey):
                    matched = ckey
                    break
                if caption and len(caption) > 3 and caption.upper() in cm_title:
                    matched = ckey
                    break

            if matched:
                cinfo = cm[matched]
                for doc_key in ["orders", "motions_to_dismiss", "oppositions", "court_notices"]:
                    for fpath_str in cinfo.get(doc_key, []):
                        fp = Path(fpath_str)
                        if not fp.exists() or fp.stat().st_size < 100:
                            continue
                        try:
                            text = extract_text(fp)
                            if text and len(text) > 200:
                                vios = scan_document_for_violations(text, fp.stem[:60])
                                for v in vios:
                                    d = v.to_dict()
                                    d["file"] = display_source(str(fp))
                                    d["doc_category"] = doc_key
                                    case_violations.append(d)
                        except Exception:
                            pass

        if case_violations:
            doc.add_page_break()

            eh = doc.add_paragraph()
            eh.alignment = WD_ALIGN_PARAGRAPH.CENTER
            eh.add_run("EXHIBIT B").bold = True
            eh.runs[0].font.size = Pt(14)

            eh2 = doc.add_paragraph()
            eh2.alignment = WD_ALIGN_PARAGRAPH.CENTER
            eh2.add_run("PROCEDURAL & CIVIL RULE VIOLATION SCORECARD")
            eh2.runs[0].bold = True
            eh2.runs[0].font.size = Pt(13)

            eh3 = doc.add_paragraph()
            eh3.alignment = WD_ALIGN_PARAGRAPH.CENTER
            eh3.add_run("Systematic Audit of Opposing Filings for Rule Compliance")
            eh3.runs[0].font.size = Pt(11)
            doc.add_paragraph("")

            fatal_v = len([v for v in case_violations if v.get("severity") == "FATAL"])
            serious_v = len([v for v in case_violations if v.get("severity") == "SERIOUS"])

            # Dedup
            seen = {}
            deduped = []
            for v in case_violations:
                key = (v.get("violation_type", ""), v.get("rule_citation", ""), v.get("file", ""))
                if key in seen:
                    seen[key]["count"] += 1
                else:
                    entry = dict(v)
                    entry["count"] = 1
                    seen[key] = entry
                    deduped.append(entry)

            sp = doc.add_paragraph()
            sp.add_run("Violation Summary").bold = True
            doc.add_paragraph(
                f"Total: {len(case_violations)} ({len(deduped)} unique). "
                f"FATAL: {fatal_v}. SERIOUS: {serious_v}."
            )
            doc.add_paragraph("")

            DOMAINS = {
                "A. Constitutional Violations": ["NOTICE_INADEQUATE", "DUE_PROCESS"],
                "B. Pleading Standard Violations": ["TWOMBLY_IQBAL_MISAPPLICATION", "PRO_SE_LIBERAL_CONSTRUCTION", "IMPROPER_STANDARD_OF_REVIEW"],
                "C. Procedural Rule Violations": ["VERIFICATION_MISSING", "SERVICE_DEFECTIVE", "FILING_DEADLINE_VIOLATION", "PAGE_LIMIT_EXCEEDED"],
                "D. Judicial Conduct Violations": ["BIAS_JUDICIAL"],
                "E. Adverse Judicial Rulings & Dismissals": ["COURT_RULING_GRANTED", "CLAIMS_DISMISSED", "MTD_GRANTED_AGAINST_PLAINTIFF", "FILING_STRICKEN"],
            }
            # Catch-all: any detected violation_type not routed above still
            # gets a section, so nothing identified is ever silently dropped.
            _routed = set()
            for _types in DOMAINS.values():
                _routed.update(_types)
            _leftover = sorted({
                v.get("violation_type", "") for v in deduped
                if v.get("violation_type") and v.get("violation_type") not in _routed
            })
            if _leftover:
                DOMAINS["F. Other Identified Violations"] = _leftover

            for domain_name, domain_types in DOMAINS.items():
                dvios = [v for v in deduped if v.get("violation_type") in domain_types]
                if not dvios:
                    continue
                dp = doc.add_paragraph()
                dp.add_run(domain_name).bold = True

                t = doc.add_table(rows=1 + len(dvios), cols=5)
                t.style = "Table Grid"
                for j, h in enumerate(["Violation", "Sev.", "Rule", "Source", "Quote"]):
                    t.rows[0].cells[j].text = h
                    for par in t.rows[0].cells[j].paragraphs:
                        for run in par.runs:
                            run.bold = True
                        par.style.font.size = Pt(9)
                for i, v in enumerate(dvios):
                    row = t.rows[i + 1]
                    vt = v.get("violation_type", "").replace("_", " ")
                    if v.get("count", 1) > 1:
                        vt += f" (x{v['count']})"
                    row.cells[0].text = vt[:30]
                    row.cells[1].text = v.get("severity", "")[:3]
                    row.cells[2].text = v.get("rule_citation", "")[:45]
                    row.cells[3].text = v.get("file", "")[:30]
                    q = v.get("quote", "")[:60]
                    row.cells[4].text = f"\u201c{q}\u201d" if q else ""
                    for cell in row.cells:
                        for par in cell.paragraphs:
                            par.style.font.size = Pt(8)
                doc.add_paragraph("")

            # IF/THEN triggers
            tp = doc.add_paragraph()
            tp.add_run("Decision Triggers").bold = True
            by_type = set(v.get("violation_type") for v in case_violations)
            triggers = []
            if "TWOMBLY_IQBAL_MISAPPLICATION" in by_type:
                triggers.append(("Twombly/Iqbal misapplied", "Accept all well-pleaded facts", "Strike/vacate"))
            if "NOTICE_INADEQUATE" in by_type:
                triggers.append(("Due process denied", "Order VOID (Rule 60(b)(4))", "Vacate (no time limit)"))
            if "BIAS_JUDICIAL" in by_type:
                triggers.append(("Judicial bias", "28 U.S.C. \u00a7 455", "Recusal"))
            if "VERIFICATION_MISSING" in by_type:
                triggers.append(("Verification missing", "Pa.R.C.P. 1024 admissions", "Strike"))
            if "SERVICE_DEFECTIVE" in by_type:
                triggers.append(("Service defective", "No legal effect", "Strike"))
            if triggers:
                tt = doc.add_table(rows=1 + len(triggers), cols=3)
                tt.style = "Table Grid"
                for j, h in enumerate(["IF Violation", "THEN Rule Requires", "Action"]):
                    tt.rows[0].cells[j].text = h
                    for par in tt.rows[0].cells[j].paragraphs:
                        for run in par.runs:
                            run.bold = True
                for i, (a, b, c) in enumerate(triggers):
                    tt.rows[i+1].cells[0].text = a
                    tt.rows[i+1].cells[1].text = b
                    tt.rows[i+1].cells[2].text = c

            doc.add_paragraph("")
            doc.add_paragraph(
                "Deterministic Analysis \u2014 No Generative Reasoning Applied. "
                "All conclusions derived from rule-based cross-reference validation."
            )

    except Exception as _e:
        pass

        output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))
    return output_path
