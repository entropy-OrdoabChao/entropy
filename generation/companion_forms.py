# -*- coding: utf-8 -*-
"""
Companion-form drafter for a NEW complaint filing package.

Fills the gap M0 left open: `m0_new_claim_engine.required_forms()` only NAMES the
court-required companion forms; nothing drafted them. This module drafts them:

  * build_ifp_petition()      -> Petition to Proceed In Forma Pauperis + Affidavit
                                 + proposed Order. PA = Pa.R.C.P. 240; federal =
                                 Application to Proceed Without Prepaying Fees
                                 (AO 240 / 28 U.S.C. 1915).
  * build_civil_cover_sheet() -> Philadelphia Civil Cover Sheet (commencement) for
                                 PA_CP_PHILA; JS-44 fields for federal.

Grounding: Pa.R.C.P. 240 text (official PA Code, 231 Pa. Code r. 240) — subdivision
(c) petition+affidavit, (h) prescribed affidavit form + financial fields, (j)
frivolous-dismissal, (c)(3) 20-day order. Affidavit oath uses 18 Pa.C.S. § 4904
(Pa.R.C.P. 1023.1/1024 verification), NOT the federal 28 U.S.C. § 1746.

Financial figures are NEVER fabricated: the affidavit renders LABELLED BLANK
fields for the litigant to complete, unless a `financials` dict is supplied.
"""
from pathlib import Path
from typing import List, Dict, Optional

_PA = "PA_CP_PHILA"


def _court_lines(case_court: str) -> List[str]:
    if case_court == _PA:
        return ["IN THE COURT OF COMMON PLEAS",
                "OF PHILADELPHIA COUNTY, PENNSYLVANIA",
                "CIVIL TRIAL DIVISION"]
    if case_court == "EDPA":
        return ["IN THE UNITED STATES DISTRICT COURT",
                "FOR THE EASTERN DISTRICT OF PENNSYLVANIA"]
    if case_court == "CA3":
        return ["IN THE UNITED STATES COURT OF APPEALS", "FOR THE THIRD CIRCUIT"]
    return ["IN THE COURT"]


def _new_doc():
    from docx import Document
    from docx.shared import Pt, Inches
    doc = Document()
    st = doc.styles["Normal"]
    st.font.name = "Times New Roman"; st.font.size = Pt(12)
    st.paragraph_format.line_spacing = 1.5; st.paragraph_format.space_after = Pt(2)
    for s in doc.sections:
        s.top_margin = s.bottom_margin = s.left_margin = s.right_margin = Inches(1)
    return doc


def _caption(doc, case_court, plaintiff_name, defendants, case_number):
    from docx.shared import Pt, Inches
    from docx.enum.text import WD_LINE_SPACING
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    left = [plaintiff_name.upper() + ",", "", "    Plaintiff,", "", "v.", ""]
    for i, d in enumerate(defendants):
        left.append(d.upper() + ("," if i == len(defendants) - 1 else ";"))
    left += ["", "    Defendants."]
    right = list(_court_lines(case_court)) + ["", case_number]
    n = max(len(left), len(right)); left += [""] * (n - len(left)); right += [""] * (n - len(right))
    tbl = doc.add_table(rows=n, cols=2); tbl.autofit = False; tbl.allow_autofit = False
    for r, (lt, rt) in enumerate(zip(left, right)):
        cells = tbl.rows[r].cells; cells[0].width = Inches(3.5); cells[1].width = Inches(2.5)
        for cell, txt in ((cells[0], lt), (cells[1], rt)):
            cell.text = ""; cp = cell.paragraphs[0]
            cp.paragraph_format.line_spacing_rule = WD_LINE_SPACING.SINGLE
            cp.paragraph_format.space_after = Pt(0)
            run = cp.add_run(txt); run.font.size = Pt(11); run.font.name = "Times New Roman"
    for row in tbl.rows:
        tcPr = row.cells[0]._tc.get_or_add_tcPr(); b = OxmlElement("w:tcBorders")
        rb = OxmlElement("w:right"); rb.set(qn("w:val"), "single"); rb.set(qn("w:sz"), "8"); rb.set(qn("w:color"), "000000")
        b.append(rb); tcPr.append(b)
    doc.add_paragraph("")


def _center(doc, text, bold=True, size=None):
    from docx.shared import Pt
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run(text); r.bold = bold
    if size: r.font.size = Pt(size)
    return p


def _p(doc, text="", bold=False, indent=0.0):
    from docx.shared import Inches
    p = doc.add_paragraph()
    if indent: p.paragraph_format.left_indent = Inches(indent)
    r = p.add_run(text); r.bold = bold
    return p


def _num(doc, n, text):
    from docx.shared import Inches
    p = doc.add_paragraph(); p.paragraph_format.left_indent = Inches(0.5)
    p.paragraph_format.first_line_indent = Inches(-0.5); p.add_run(f"{n}.   {text}")
    return p


def _field(doc, label, value=""):
    _p(doc, f"{label}: {value if value else '____________________________'}", indent=0.5)


def _zero_for(label: str) -> str:
    """Truthful nil value per affidavit line when the affiant has no income/assets."""
    l = label.lower()
    if "date of last employment" in l:
        return "N/A"
    if "names and ages" in l:
        return "None"
    if any(k in l for k in ("employer", "type of work", "real estate", "motor vehicle",
                            "stocks", "other property", "spouse")):
        return "None"
    return "$0.00"


# ── Rule 240(h) financial-disclosure structure (official PA Code) ──
_AFFIDAVIT_SECTIONS = [
    ("A. EMPLOYMENT", [
        "Employer (name and address)", "Type of work", "Salary or wages per month",
        "If unemployed, date of last employment"]),
    ("B. OTHER INCOME RECEIVED IN THE PAST TWELVE (12) MONTHS (state monthly amount)", [
        "Business or profession", "Self-employment", "Interest", "Dividends",
        "Pension and annuities", "Social Security benefits", "Support payments",
        "Disability payments", "Unemployment compensation", "Workers' compensation",
        "Public assistance", "Other income"]),
    ("C. PROPERTY OWNED", [
        "Cash", "Checking account", "Savings account", "Certificates of deposit",
        "Real estate (including home) — value and mortgage owed", "Motor vehicle — make, year, value, amount owed",
        "Stocks and bonds", "Other property"]),
    ("D. HOUSEHOLD SUPPORT AND DEPENDENTS", [
        "Spouse's employment and monthly income", "Contributions from children",
        "Contributions from parents", "Other contributions", "Names and ages of persons dependent upon me"]),
    ("E. DEBTS AND MONTHLY OBLIGATIONS", [
        "Mortgage", "Rent", "Loans", "Other debts and obligations"]),
]


def build_ifp_petition(plaintiff_name: str, plaintiff_address: str, defendants: List[str],
                       case_court: str, case_number: str, output_path: Path,
                       financials: Optional[Dict[str, str]] = None,
                       zero_financials: bool = False) -> Path:
    """Petition to Proceed IFP + Affidavit (Rule 240(h) fields) + proposed Order.
    financials: optional {label: value}; any label not supplied renders a blank line.
    zero_financials=True: fill every unspecified line with its truthful nil ($0.00 / None /
    N/A) for an affiant with no income or assets."""
    is_pa = case_court == _PA
    financials = financials or {}
    doc = _new_doc()
    _caption(doc, case_court, plaintiff_name, defendants, case_number)

    if is_pa:
        _center(doc, "PETITION FOR LEAVE TO PROCEED IN FORMA PAUPERIS", size=12)
    else:
        _center(doc, "APPLICATION TO PROCEED IN DISTRICT COURT WITHOUT PREPAYING FEES OR COSTS", size=12)
    doc.add_paragraph("")

    rule = "Pa.R.C.P. 240" if is_pa else "28 U.S.C. § 1915"
    _num(doc, 1, f"Petitioner, {plaintiff_name}, is the Plaintiff in the above-captioned action.")
    _num(doc, 2, f"Because of Petitioner's financial condition, Petitioner is without financial resources to "
                 f"pay the fees and costs of prosecuting this action, and is entitled to proceed in forma "
                 f"pauperis pursuant to {rule}.")
    _num(doc, 3, "Petitioner is unable to obtain funds from anyone, including family and associates, to pay "
                 "the costs of this litigation.")
    _num(doc, 4, "The facts concerning Petitioner's financial condition are set forth in the accompanying "
                 "Affidavit, which is incorporated herein by reference.")
    doc.add_paragraph("")
    _p(doc, f"WHEREFORE, Petitioner respectfully requests that this Court grant leave to proceed in forma "
            f"pauperis and waive the payment of all filing fees and costs.", bold=False)
    doc.add_paragraph("")
    _p(doc, "Respectfully submitted,")
    doc.add_paragraph(""); _p(doc, "_________________________________")
    _p(doc, plaintiff_name); _p(doc, "Plaintiff, sui juris (1 Pa.C.S. § 1308; 28 U.S.C. § 1654)"); _p(doc, plaintiff_address)
    _p(doc, "Dated: _________________________")

    # ── AFFIDAVIT (Rule 240(h)) ──
    doc.add_page_break()
    _center(doc, "AFFIDAVIT IN SUPPORT OF PETITION TO PROCEED IN FORMA PAUPERIS", size=12)
    doc.add_paragraph("")
    oath_stmt = ("18 Pa.C.S. § 4904, relating to unsworn falsification to authorities" if is_pa
                 else "28 U.S.C. § 1746, under penalty of perjury")
    _p(doc, f"I, {plaintiff_name}, am the Plaintiff in the above matter, and because of my financial condition "
            f"I am unable to pay the fees and costs of prosecuting this action. I am unable to obtain funds from "
            f"anyone, including my family and associates, to pay the costs of this litigation. I represent that "
            f"the information below relating to my ability to pay the fees and costs is true and correct:")
    doc.add_paragraph("")
    for header, items in _AFFIDAVIT_SECTIONS:
        _p(doc, header, bold=True)
        for it in items:
            val = financials.get(it, "")
            if not val and zero_financials:
                val = _zero_for(it)
            _field(doc, it, val)
        doc.add_paragraph("")
    if zero_financials:
        _p(doc, "Affiant has no employment, no income from any source, and no assets. The foregoing "
                "reflects that condition in full.", bold=False)
    _p(doc, f"I verify that the statements made in this Affidavit are true and correct. I understand that false "
            f"statements herein are made subject to the penalties of {oath_stmt}.")
    doc.add_paragraph(""); _p(doc, "_________________________________")
    _p(doc, plaintiff_name); _p(doc, "Affiant / Petitioner, sui juris (1 Pa.C.S. § 1308; 28 U.S.C. § 1654)")
    _p(doc, "Date: _________________________")

    # ── proposed ORDER (Rule 240(c)(3)) ──
    doc.add_page_break()
    _center(doc, "ORDER", size=12)
    doc.add_paragraph("")
    _p(doc, "AND NOW, this ______ day of __________________, 20____, upon consideration of the foregoing "
            "Petition for Leave to Proceed In Forma Pauperis and the Affidavit in support thereof, it is hereby "
            "ORDERED and DECREED that the Petition is GRANTED. Petitioner is granted leave to proceed in forma "
            "pauperis, and the prepayment of filing fees and costs is waived.")
    doc.add_paragraph(""); doc.add_paragraph("")
    _p(doc, "BY THE COURT:"); doc.add_paragraph("")
    _p(doc, "_________________________________  J.")

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))
    return output_path


def draft_companion_forms_for_route(required_forms: List[str], plaintiff_name: str,
                                    plaintiff_address: str, defendants, case_court: str,
                                    case_number: str, output_dir, amount_cap: int = 0,
                                    case_type: str = "", jury_demand: bool = True) -> Dict[str, Path]:
    """Dispatch: read the route's required-form NAMES and draft whichever documents they call for.
    This is the piece M0 was missing — required_forms() named the forms; this renders them.
    Returns {form_key: path}. `defendants` may be a list of names or of {name,address} dicts."""
    out: Dict[str, Path] = {}
    joined = " ".join(required_forms or []).lower()
    out_dir = Path(output_dir)
    def_dicts = [(d if isinstance(d, dict) else {"name": d}) for d in defendants]
    def_names = [d.get("name", "") for d in def_dicts]
    if "cover sheet" in joined or "cover_sheet" in joined:
        out["cover_sheet"] = build_civil_cover_sheet(
            plaintiff_name, plaintiff_address, def_dicts, case_court, case_number,
            out_dir / "civil_cover_sheet.docx", case_type=case_type, amount_cap=amount_cap,
            jury_demand=jury_demand)
    if "forma pauperis" in joined or "without prepaying" in joined or " ifp" in (" " + joined):
        out["ifp"] = build_ifp_petition(
            plaintiff_name, plaintiff_address, def_names, case_court, case_number,
            out_dir / "ifp_petition.docx")
    return out


# Philadelphia CCP case-type/track determination
_PHILA_ARBITRATION_LIMIT = 50000  # ≤ this => compulsory arbitration; above => Major Jury/Non-Jury


def build_civil_cover_sheet(plaintiff_name: str, plaintiff_address: str,
                            defendants: List[Dict], case_court: str, case_number: str,
                            output_path: Path, case_type: str = "", amount_cap: int = 0,
                            jury_demand: bool = True, related_cases: str = "None") -> Path:
    """Philadelphia Civil Cover Sheet (commencement) for PA_CP_PHILA; JS-44 fields for federal.
    case_type: descriptive (e.g. 'Contract — Other' / 'Tort — Insurance Bad Faith'). Confirm the
    exact Phila code at filing. amount_cap drives arbitration vs. Major Jury track."""
    is_pa = case_court == _PA
    doc = _new_doc()
    if is_pa:
        _center(doc, "PHILADELPHIA COURT OF COMMON PLEAS — CIVIL COVER SHEET", size=12)
        _center(doc, "(Commencement of Civil Action)", bold=False, size=11)
    else:
        _center(doc, "CIVIL COVER SHEET (JS-44)", size=12)
    doc.add_paragraph("")
    _field(doc, "Plaintiff", plaintiff_name)
    _field(doc, "Plaintiff address", plaintiff_address)
    for i, d in enumerate(defendants, 1):
        _field(doc, f"Defendant {i}", d.get("name", ""))
        if d.get("address"):
            _field(doc, f"Defendant {i} address", d["address"])
    _field(doc, "Docket / Case No.", case_number)
    _field(doc, "Commencement of action", "Complaint")
    _field(doc, "Case type", case_type or "____________________")
    over = amount_cap and amount_cap > _PHILA_ARBITRATION_LIMIT
    if is_pa:
        _field(doc, "Amount in controversy",
               f"Not to exceed ${amount_cap:,}" if amount_cap else "____________")
        _field(doc, "Arbitration / Major case",
               ("MAJOR JURY / NON-JURY (amount exceeds $50,000 compulsory-arbitration limit)"
                if over else "COMPULSORY ARBITRATION (amount $50,000 or less)"))
    _field(doc, "Jury demand", "YES" if jury_demand else "NO")
    _field(doc, "Related pending cases", related_cases)
    doc.add_paragraph("")
    _p(doc, "Filer certifies the above is correct to the best of the filer's knowledge.", bold=False)
    doc.add_paragraph(""); _p(doc, "_________________________________")
    _p(doc, plaintiff_name); _p(doc, "Plaintiff, sui juris (1 Pa.C.S. § 1308; 28 U.S.C. § 1654)"); _p(doc, "Date: _________________________")

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))
    return output_path
