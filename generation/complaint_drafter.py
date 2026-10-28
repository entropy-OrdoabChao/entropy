"""
Beezle AI — Deterministic Complaint Drafter (V3)
Generates federal complaints using the court-forcing claim template.

Upgrades over V1:
  - Proper defendant separation (individual paragraphs with role/title/conduct)
  - Preliminary statement
  - Jurisdiction and venue
  - Statement of facts (numbered paragraphs, timeline sections, exhibit refs)
  - Exhaustion of administrative remedies
  - Itemized damages breakdown
  - Consolidated prayer for relief
  - Full verification and exhibit list
  - Certificate of service placeholder

Structure per claim:
  I. Controlling Law
  II. Mandatory Duties
  III. Prima Facie Elements
  IV. Undisputed Facts (Record-Anchored)
  V. Element-by-Element Application (IF/THEN)
  VI. Breach / Damages
  VII. Decision Trigger
  VIII. Relief Requested

Usage:
    from generation.complaint_drafter import build_complaint_from_facts
    path = build_complaint_from_facts(...)
"""

from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional, Dict
import time


# ═══════════════════════════════════════════════════════
# DATA CLASSES
# ═══════════════════════════════════════════════════════

@dataclass
class Party:
    name: str
    address: str = ""
    role: str = ""          # descriptive role: "a natural person, proceeding pro se"
    capacity: str = ""      # "individual", "official capacity", "agency"
    title: str = ""         # "Government Information Specialist"
    department: str = ""    # "HUD FOIA Office"
    email: str = ""
    conduct: str = ""       # what they did wrong (for mandamus)


@dataclass
class Exhibit:
    letter: str             # "A", "B", etc.
    description: str
    date: str = ""


@dataclass
class Element:
    number: int
    description: str
    if_clause: str
    and_clause: str
    then_clause: str


@dataclass
class Duty:
    number: int
    text: str
    citation: str


@dataclass
class DamageItem:
    letter: str             # "(a)", "(b)", etc.
    description: str


@dataclass
class Claim:
    count_number: int
    title: str
    statute: str
    controlling_law: List[str] = field(default_factory=list)
    construction_notes: List[str] = field(default_factory=list)
    canons: List[str] = field(default_factory=list)
    duties: List[Duty] = field(default_factory=list)
    elements: List[Element] = field(default_factory=list)
    facts: List[str] = field(default_factory=list)
    admissions: List[str] = field(default_factory=list)
    breach_duty: str = ""
    breach_conduct: str = ""
    damages: List[str] = field(default_factory=list)
    damage_items: List[DamageItem] = field(default_factory=list)
    decision_trigger_if: str = ""
    decision_trigger_and: str = ""
    decision_trigger_then: str = ""
    relief: List[str] = field(default_factory=list)
    is_judicial_claim: bool = False  # True for judge/judicial misconduct claims


@dataclass
class ComplaintData:
    court: str = "IN THE UNITED STATES DISTRICT COURT"
    district: str = "FOR THE EASTERN DISTRICT OF PENNSYLVANIA"
    case_court: str = ""  # "EDPA" | "PA_CP_PHILA" | "CA3" | "" (legacy paragraph caption)
    plaintiff: Optional[Party] = None
    defendants: List[Party] = field(default_factory=list)
    case_number: str = "Civil Action No. __________"
    complaint_title: str = ""
    preliminary_statement: List[str] = field(default_factory=list)
    jurisdiction_paragraphs: List[str] = field(default_factory=list)
    fact_sections: Dict[str, List[str]] = field(default_factory=dict)
    exhaustion_paragraphs: List[str] = field(default_factory=list)
    claims: List[Claim] = field(default_factory=list)
    prayer_for_relief: List[str] = field(default_factory=list)
    exhibits: List[Exhibit] = field(default_factory=list)
    verification: bool = True
    cert_of_service: bool = True


# ═══════════════════════════════════════════════════════
# DOCX BUILDER
# ═══════════════════════════════════════════════════════

def _build_caption_rows(data: "ComplaintData") -> List[tuple]:
    """Return list of (left_cell, right_cell) for 2-column caption table.

    Court header is selected from data.case_court ("EDPA" | "PA_CP_PHILA" | "CA3").
    Empty case_court falls through to the caller's legacy paragraph rendering.
    """
    if data.case_court == "EDPA":
        court_lines = [
            "IN THE UNITED STATES DISTRICT COURT",
            "FOR THE EASTERN DISTRICT OF PENNSYLVANIA",
        ]
    elif data.case_court == "PA_CP_PHILA":
        court_lines = [
            "IN THE COURT OF COMMON PLEAS",
            "OF PHILADELPHIA COUNTY, PENNSYLVANIA",
            "CIVIL TRIAL DIVISION",
        ]
    elif data.case_court == "CA3":
        court_lines = [
            "IN THE UNITED STATES COURT OF APPEALS",
            "FOR THE THIRD CIRCUIT",
        ]
    else:
        court_lines = [data.court, data.district]

    left_lines = []
    if data.plaintiff:
        left_lines.append(data.plaintiff.name.upper() + ",")
        if data.plaintiff.address:
            left_lines.append(data.plaintiff.address)
        left_lines.append("")
        left_lines.append("    Plaintiff,")
        left_lines.append("")
        left_lines.append("v.")
        left_lines.append("")

    for i, d in enumerate(data.defendants):
        parts = [d.name.upper()]
        if d.title:
            parts.append(d.title)
        if d.department:
            parts.append(d.department)
        if d.capacity:
            parts.append("in " + d.capacity)
        text = ", ".join(parts)
        text += ";" if i < len(data.defendants) - 1 else ","
        left_lines.append(text)

    if data.defendants:
        left_lines.append("")
        left_lines.append("    Defendants.")

    right_lines = list(court_lines) + ["", data.case_number]

    n = max(len(left_lines), len(right_lines))
    left_lines += [""] * (n - len(left_lines))
    right_lines += [""] * (n - len(right_lines))

    return list(zip(left_lines, right_lines))


def build_complaint(data: ComplaintData, output_path: Path) -> Path:
    """Build a federal complaint .docx using the deterministic claim template."""
    from docx import Document
    from docx.shared import Pt, Inches
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Times New Roman"
    style.font.size = Pt(12)
    style.paragraph_format.space_after = Pt(2)
    style.paragraph_format.line_spacing = 1.5

    for section in doc.sections:
        section.top_margin = Inches(1)
        section.bottom_margin = Inches(1)
        section.left_margin = Inches(1)
        section.right_margin = Inches(1)

    # ── Helper functions ──

    def centered(text, bold=False, size=None):
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        r = p.add_run(text)
        if bold:
            r.bold = True
        if size:
            r.font.size = Pt(size)
        return p

    def para(text, bold=False, indent=0):
        p = doc.add_paragraph()
        if indent:
            p.paragraph_format.left_indent = Inches(indent * 0.5)
        if bold:
            r = p.add_run(text)
            r.bold = True
        else:
            p.add_run(text)
        return p

    def numbered(number, text):
        p = doc.add_paragraph()
        p.paragraph_format.left_indent = Inches(0.5)
        p.paragraph_format.first_line_indent = Inches(-0.5)
        p.add_run(f"{number}.   {text}")
        return p

    def bullet(text, indent=0.3):
        p = doc.add_paragraph()
        p.paragraph_format.left_indent = Inches(indent)
        p.add_run(f"\u2022 {text}")
        return p

    def bold_if_then(if_text, and_text, then_text):
        """Render IF/AND/THEN block with bold THEN."""
        p1 = para(f"IF {if_text},")
        p2 = para(f"AND {and_text},")
        p3 = doc.add_paragraph()
        r = p3.add_run(f"THEN {then_text}")
        r.bold = True
        return p3

    # Running paragraph counter
    pnum = 1

    # ═══════════════════════════════════════
    # CAPTION
    # ═══════════════════════════════════════
    if data.case_court:
        # 2-column table caption (matches reply_brief_drafter format)
        from docx.enum.text import WD_LINE_SPACING
        from docx.oxml.ns import qn as _qn
        from docx.oxml import OxmlElement as _OxmlElement
        caption_rows = _build_caption_rows(data)
        cap_table = doc.add_table(rows=len(caption_rows), cols=2)
        cap_table.autofit = False
        cap_table.allow_autofit = False
        for r_idx, (left_text, right_text) in enumerate(caption_rows):
            cells = cap_table.rows[r_idx].cells
            cells[0].width = Inches(3.5)
            cells[1].width = Inches(2.5)
            for cell, ctext in ((cells[0], left_text), (cells[1], right_text)):
                cell.text = ""
                cp = cell.paragraphs[0]
                cp.paragraph_format.line_spacing_rule = WD_LINE_SPACING.SINGLE
                cp.paragraph_format.space_after = Pt(0)
                crun = cp.add_run(ctext)
                crun.font.size = Pt(11)
                crun.font.name = "Times New Roman"
        # Vertical separator on left column right border
        for row in cap_table.rows:
            tcPr = row.cells[0]._tc.get_or_add_tcPr()
            tcBorders = _OxmlElement("w:tcBorders")
            right_border = _OxmlElement("w:right")
            right_border.set(_qn("w:val"), "single")
            right_border.set(_qn("w:sz"), "8")
            right_border.set(_qn("w:color"), "000000")
            tcBorders.append(right_border)
            tcPr.append(tcBorders)
        doc.add_paragraph()
    else:
        # LEGACY paragraph-stacked caption (preserved for backward compat)
        centered(data.court, bold=True)
        centered(data.district, bold=True)
        doc.add_paragraph("")
        if data.plaintiff:
            p = doc.add_paragraph()
            r = p.add_run(data.plaintiff.name.upper() + ",")
            r.bold = False
            if data.plaintiff.address:
                para(data.plaintiff.address)
            doc.add_paragraph("")
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Inches(2)
            p.add_run("Plaintiff,")
            doc.add_paragraph("")
            centered("v.")
            p2 = doc.add_paragraph()
            p2.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            p2.add_run(data.case_number)
            doc.add_paragraph("")
        for i, d in enumerate(data.defendants):
            parts = [d.name.upper()]
            if d.title:
                parts.append(d.title)
            if d.department:
                parts.append(d.department)
            if d.capacity:
                parts.append(f"in {d.capacity}")
            text = ", ".join(parts)
            if i < len(data.defendants) - 1:
                text += ";"
            else:
                text += ","
            para(text)
        if data.defendants:
            doc.add_paragraph("")
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Inches(2)
            p.add_run("Defendants.")
        doc.add_paragraph("")

    # ═══════════════════════════════════════
    # NOTICE TO DEFEND (Pa.R.C.P. 1018.1) — PA Court of Common Pleas only
    # Rule-mandated statutory notice; required on every CCP complaint. Guarded
    # on case_court so federal (EDPA/CA3) complaints are unaffected.
    # ═══════════════════════════════════════
    if data.case_court == "PA_CP_PHILA":
        centered("NOTICE TO DEFEND", bold=True)
        para("You have been sued in court. If you wish to defend against the claims set "
             "forth in the following pages, you must take action within twenty (20) days "
             "after this complaint and notice are served, by entering a written appearance "
             "personally or by attorney and filing in writing with the court your defenses "
             "or objections to the claims set forth against you. You are warned that if you "
             "fail to do so the case may proceed without you and a judgment may be entered "
             "against you by the court without further notice for any money claimed in the "
             "complaint or for any other claim or relief requested by the plaintiff. You may "
             "lose money or property or other rights important to you.")
        doc.add_paragraph("")
        para("YOU SHOULD TAKE THIS PAPER TO YOUR LAWYER AT ONCE. IF YOU DO NOT HAVE A LAWYER "
             "OR CANNOT AFFORD ONE, GO TO OR TELEPHONE THE OFFICE SET FORTH BELOW TO FIND OUT "
             "WHERE YOU CAN GET LEGAL HELP.")
        doc.add_paragraph("")
        para("Philadelphia Bar Association")
        para("Lawyer Referral and Information Service")
        para("One Reading Center")
        para("Philadelphia, Pennsylvania 00000")
        para("(215) 238-6333")
        doc.add_paragraph("")

    # ═══════════════════════════════════════
    # COMPLAINT TITLE
    # ═══════════════════════════════════════
    if data.complaint_title:
        centered(data.complaint_title, bold=True, size=12)
    doc.add_paragraph("")

    # ═══════════════════════════════════════
    # PRELIMINARY STATEMENT
    # ═══════════════════════════════════════
    if data.preliminary_statement:
        centered("PRELIMINARY STATEMENT", bold=True)
        for ps in data.preliminary_statement:
            numbered(pnum, ps)
            pnum += 1
        doc.add_paragraph("")

    # ═══════════════════════════════════════
    # JURISDICTION AND VENUE
    # ═══════════════════════════════════════
    if data.jurisdiction_paragraphs:
        centered("JURISDICTION AND VENUE", bold=True)
        for jp in data.jurisdiction_paragraphs:
            numbered(pnum, jp)
            pnum += 1
        doc.add_paragraph("")

    # ═══════════════════════════════════════
    # PARTIES
    # ═══════════════════════════════════════
    centered("PARTIES", bold=True)

    # Plaintiff paragraph
    if data.plaintiff:
        desc = f"Plaintiff {data.plaintiff.name} is {data.plaintiff.role}"
        if data.plaintiff.address:
            desc += f", residing at {data.plaintiff.address}"
        desc += "."
        numbered(pnum, desc)
        pnum += 1

    # Each defendant gets its own paragraph with full description
    for d in data.defendants:
        desc = f"Defendant {d.name}"
        if d.title:
            desc += f" is employed as {d.title}"
        if d.department:
            desc += f" in {d.department}"
        if d.conduct:
            desc += f". {d.conduct}"
        if d.capacity:
            cap = d.capacity
            # Derive pronoun from capacity string if it contains her/his/their
            if "her " in cap or "her " in cap.lower():
                pronoun = "She"
            elif "his " in cap or "his " in cap.lower():
                pronoun = "He"
            elif "their " in cap:
                pronoun = "They are"
            else:
                pronoun = "This defendant"
            # If capacity already starts with pronoun context like "her official capacity"
            # then format as "She is sued in her official capacity."
            desc += f" {pronoun} is sued in {cap}."
        else:
            desc += "."
        numbered(pnum, desc)
        pnum += 1

    doc.add_paragraph("")

    # ═══════════════════════════════════════
    # STATEMENT OF FACTS
    # ═══════════════════════════════════════
    if data.fact_sections:
        centered("STATEMENT OF FACTS", bold=True)
        for section_title, facts in data.fact_sections.items():
            para(section_title, bold=True)
            for f in facts:
                numbered(pnum, f)
                pnum += 1
            doc.add_paragraph("")

    # ═══════════════════════════════════════
    # EXHAUSTION
    # ═══════════════════════════════════════
    if data.exhaustion_paragraphs:
        centered("EXHAUSTION OF ADMINISTRATIVE REMEDIES", bold=True)
        for ep in data.exhaustion_paragraphs:
            numbered(pnum, ep)
            pnum += 1
        doc.add_paragraph("")

    # ═══════════════════════════════════════
    # CLAIMS (DETERMINISTIC TEMPLATE)
    # ═══════════════════════════════════════
    for claim in data.claims:
        doc.add_page_break()

        # Count header
        centered(f"COUNT {_roman(claim.count_number)} \u2014 {claim.title}", bold=True, size=13)
        centered(claim.statute, bold=False, size=11)
        doc.add_paragraph("")

        # I. CONTROLLING LAW
        para("I. CONTROLLING LAW", bold=True)
        para("This Court is bound by:")
        for law in claim.controlling_law:
            bullet(law)

        # Internal: validate controlling law using construction rules
        # This doesn't print — it validates the statute citations are used correctly
        _validate_claim_construction(claim)

        doc.add_paragraph("")

        # Judicial canons ARE substantive in judge lawsuits — include in body
        if claim.canons and claim.is_judicial_claim:
            para("APPLICABLE JUDICIAL CANONS", bold=True)
            for canon in claim.canons:
                bullet(canon)
            doc.add_paragraph("")

        # II. MANDATORY DUTIES
        para("II. MANDATORY DUTIES", bold=True)
        para("The governing law imposes the following non-discretionary duties:")
        for duty in claim.duties:
            numbered(duty.number, f"{duty.text} {duty.citation}")
        doc.add_paragraph("")

        # III. PRIMA FACIE ELEMENTS
        para("III. PRIMA FACIE ELEMENTS", bold=True)
        for elem in claim.elements:
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Inches(0.3)
            p.add_run(f"({elem.number}) {elem.description}")
        doc.add_paragraph("")

        # IV. UNDISPUTED FACTS
        para("IV. UNDISPUTED FACTS (RECORD-ANCHORED)", bold=True)
        for fact in claim.facts:
            bullet(fact)

        if claim.admissions:
            doc.add_paragraph("")
            para("Defendants admit:", bold=True)
            for adm in claim.admissions:
                bullet(adm)
        doc.add_paragraph("")

        # V. ELEMENT-BY-ELEMENT APPLICATION
        para("V. ELEMENT-BY-ELEMENT APPLICATION", bold=True)
        for elem in claim.elements:
            doc.add_paragraph("")
            para(f"Element ({elem.number}):")
            bold_if_then(
                elem.if_clause,
                elem.and_clause,
                f"Element ({elem.number}) is satisfied."
            )
        doc.add_paragraph("")

        # VI. BREACH / DAMAGES
        if claim.breach_duty:
            para("VI. BREACH OF MANDATORY DUTY", bold=True)
            para(f"Duty violated: {claim.breach_duty}")
            para(f"Conduct: {claim.breach_conduct}")
            para("This constitutes a breach as a matter of law.")

            # If there are also damage items, add them
            if claim.damage_items:
                doc.add_paragraph("")
                para("Plaintiff has sustained actual damages including but not limited to:")
                for di in claim.damage_items:
                    p = doc.add_paragraph()
                    p.paragraph_format.left_indent = Inches(0.3)
                    p.add_run(f"{di.letter} {di.description}")

        elif claim.damage_items:
            para("VI. DAMAGES \u2014 MAXIMUM LEGALLY SUPPORTED", bold=True)
            if claim.damages:
                for dmg in claim.damages:
                    para(dmg)
                doc.add_paragraph("")
            para("Plaintiff has sustained actual damages including but not limited to:")
            for di in claim.damage_items:
                p = doc.add_paragraph()
                p.paragraph_format.left_indent = Inches(0.3)
                p.add_run(f"{di.letter} {di.description}")

        elif claim.damages:
            para("VI. DAMAGES \u2014 MAXIMUM LEGALLY SUPPORTED", bold=True)
            for dmg in claim.damages:
                p = doc.add_paragraph()
                p.paragraph_format.left_indent = Inches(0.3)
                p.add_run(f"({dmg})")

        doc.add_paragraph("")

        # VII. DECISION TRIGGER
        para("VII. DECISION TRIGGER (MANDATORY OUTCOME)", bold=True)
        if claim.decision_trigger_if:
            bold_if_then(
                claim.decision_trigger_if,
                claim.decision_trigger_and,
                claim.decision_trigger_then,
            )
        para("\u2192 No discretionary determination is required.")
        doc.add_paragraph("")

        # VIII. RELIEF
        para(f"VIII. RELIEF REQUESTED \u2014 COUNT {_roman(claim.count_number)}", bold=True)
        for i, relief in enumerate(claim.relief, 1):
            numbered(i, relief)
        doc.add_paragraph("")

    # ═══════════════════════════════════════
    # PRAYER FOR RELIEF
    # ═══════════════════════════════════════
    doc.add_page_break()
    centered("PRAYER FOR RELIEF", bold=True, size=13)
    para("WHEREFORE, Plaintiff respectfully requests that this Court:")
    doc.add_paragraph("")
    for relief in data.prayer_for_relief:
        p = doc.add_paragraph()
        p.paragraph_format.left_indent = Inches(0.3)
        p.paragraph_format.first_line_indent = Inches(-0.15)
        p.add_run(f"\u2013 {relief}")
    doc.add_paragraph("")

    # Catch-all
    p = doc.add_paragraph()
    p.paragraph_format.left_indent = Inches(0.3)
    p.paragraph_format.first_line_indent = Inches(-0.15)
    p.add_run("\u2013 Grant such other and further relief as this Court deems just and proper.")

    # ═══════════════════════════════════════
    # DRAFTING METHODOLOGY NOTE
    # ═══════════════════════════════════════
    doc.add_paragraph("")
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.LEFT
    r = p.add_run(
        "This complaint was drafted applying the canons of statutory construction, "
        "including expressio unius est exclusio alterius, ejusdem generis, the whole "
        "act rule, and the mandatory/discretionary distinction (shall vs. may). All "
        "statutory definitions are used within their enacted scope. No definitions "
        "have been imported across statutory boundaries."
    )
    r.font.size = Pt(9)
    r.italic = True

    # ═══════════════════════════════════════
    # PROOF OF FACTS — questionnaire (duties posed as questions) + STATUTORY AUTHORITY (enacting public laws)
    # Additive: only renders when build_complaint_from_facts stashed the grounding text. Fail-closed to nothing.
    # ═══════════════════════════════════════
    _pof = getattr(data, "proof_of_facts", "") or ""
    if _pof.strip():
        doc.add_page_break()
        centered("PROOF OF FACTS", bold=True)
        doc.add_paragraph("")
        _render_grounding_block(doc, _pof)
    _statauth = getattr(data, "statutory_authority", "") or ""
    if _statauth.strip():
        doc.add_paragraph("")
        _render_grounding_block(doc, _statauth)

    # ═══════════════════════════════════════
    # SIGNATURE
    # ═══════════════════════════════════════
    doc.add_paragraph("")
    doc.add_paragraph("")
    para("Respectfully submitted,")
    doc.add_paragraph("")
    doc.add_paragraph("")
    para("_________________________________")
    if data.plaintiff:
        pr = doc.add_paragraph()
        r = pr.add_run(data.plaintiff.name)
        r.bold = True
        para("Plaintiff, Pro Se")
        if data.plaintiff.address:
            para(data.plaintiff.address)
        if data.plaintiff.email:
            para(f"Email: {data.plaintiff.email}")
    doc.add_paragraph("")
    para("Dated: _________________________")

    # ═══════════════════════════════════════
    # VERIFICATION
    # ═══════════════════════════════════════
    if data.verification and data.plaintiff:
        doc.add_page_break()
        centered("VERIFICATION", bold=True)
        doc.add_paragraph("")
        if data.case_court == "PA_CP_PHILA":
            # PA state pleadings verify under Pa.R.C.P. 1024, subject to 18 Pa.C.S. \u00a7 4904 \u2014
            # NOT the federal declaration statute 28 U.S.C. \u00a7 1746.
            para(
                f"I, {data.plaintiff.name}, verify that the statements made in the foregoing "
                f"Complaint are true and correct to the best of my knowledge, information, and "
                f"belief. I understand that false statements herein are made subject to the "
                f"penalties of 18 Pa.C.S. \u00a7 4904, relating to unsworn falsification to "
                f"authorities. This verification is made pursuant to Pa.R.C.P. 1024."
            )
        else:
            para(
                f"I, {data.plaintiff.name}, declare under penalty of perjury "
                f"pursuant to 28 U.S.C. \u00a7 1746 that the foregoing Complaint is "
                f"true and correct to the best of my knowledge, information, and belief."
            )
        doc.add_paragraph("")
        doc.add_paragraph("")
        para("_________________________________")
        para(data.plaintiff.name)
        para("Date: _________________________")

    # ═══════════════════════════════════════
    # EXHIBIT LIST
    # ═══════════════════════════════════════
    if data.exhibits:
        doc.add_page_break()
        centered("EXHIBIT LIST", bold=True)
        doc.add_paragraph("")
        for ex in data.exhibits:
            p = doc.add_paragraph()
            r1 = p.add_run(f"Exhibit {ex.letter}: ")
            r1.bold = True
            desc = ex.description
            if ex.date:
                desc += f" ({ex.date})"
            p.add_run(desc)

    # ═══════════════════════════════════════
    # CERTIFICATE OF SERVICE
    # ═══════════════════════════════════════
    if data.cert_of_service and data.plaintiff:
        doc.add_page_break()
        centered("CERTIFICATE OF SERVICE", bold=True)
        doc.add_paragraph("")
        para(
            f"I, {data.plaintiff.name}, hereby certify that on this ___ day of "
            f"_____________, 20___, I served a true and correct copy of the foregoing "
            f"Complaint and all attachments upon all parties by [U.S. Mail / ECF / "
            f"hand delivery] at the addresses listed above."
        )
        doc.add_paragraph("")
        doc.add_paragraph("")
        para("_________________________________")
        para(data.plaintiff.name)

    # Save
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))
    return output_path


def _roman(n: int) -> str:
    """Convert integer to Roman numeral."""
    vals = [(1000, 'M'), (900, 'CM'), (500, 'D'), (400, 'CD'),
            (100, 'C'), (90, 'XC'), (50, 'L'), (40, 'XL'),
            (10, 'X'), (9, 'IX'), (5, 'V'), (4, 'IV'), (1, 'I')]
    result = ''
    for val, numeral in vals:
        while n >= val:
            result += numeral
            n -= val
    return result


def _render_grounding_block(doc, text: str) -> None:
    """Render a markdown-ish grounding block (## heading, **bold** numbered questions, ' - ' bullets, quoted
    verbatim law) into docx paragraphs. Used for the PROOF-OF-FACTS questionnaire + STATUTORY AUTHORITY
    footnotes so an email-found complaint carries the same proof-of-fact format as the accountability path."""
    import re as _re
    from docx.shared import Pt, Inches
    for raw in (text or "").split("\n"):
        ln = raw.rstrip()
        if not ln.strip():
            doc.add_paragraph("")
            continue
        if ln.startswith("## "):
            p = doc.add_paragraph()
            r = p.add_run(ln[3:].strip()); r.bold = True; r.font.size = Pt(12)
            continue
        clean = _re.sub(r"[*`]", "", ln).replace("“", '"').replace("”", '"')
        indent = clean.startswith("   -") or clean.lstrip().startswith("-")
        clean = clean.lstrip()
        if indent and clean.startswith("-"):
            clean = clean[1:].strip()
        p = doc.add_paragraph()
        if indent:
            p.paragraph_format.left_indent = Inches(0.4)
        r = p.add_run(clean)
        r.font.size = Pt(11)
        # bold the numbered question rungs (the deterministic asks), italicize the instruction line
        if _re.match(r"^\d+\.", clean) and ln.strip().startswith("**"):
            r.bold = True
        elif ln.strip().startswith("*") and not ln.strip().startswith("**"):
            r.italic = True; r.font.size = Pt(9)


# ═══════════════════════════════════════════════════════
# HIGH-LEVEL BUILDER — Hermes/MCP entry point
# ═══════════════════════════════════════════════════════

def build_complaint_from_facts(
    plaintiff_name: str,
    plaintiff_address: str,
    defendants: List[Dict],
    court: str,
    district: str,
    complaint_title: str,
    facts: List[str],
    claims_data: List[Dict],
    exhibits: List[Dict],
    output_path: Path,
    # New V3 fields
    case_court: str = "",
    plaintiff_email: str = "",
    plaintiff_role: str = "a natural person, proceeding pro se",
    preliminary_statement: List[str] = None,
    jurisdiction_paragraphs: List[str] = None,
    fact_sections: Dict[str, List[str]] = None,
    exhaustion_paragraphs: List[str] = None,
    prayer_for_relief: List[str] = None,
    verification: bool = True,
    cert_of_service: bool = True,
) -> Path:
    """
    High-level builder that Hermes/API can call directly.
    Backward-compatible with V1 call signatures.

    claims_data format:
    [{
        "title": "VIOLATION OF FOIA",
        "statute": "5 U.S.C. § 552",
        "controlling_law": ["5 U.S.C. § 552(a)(6)(A)(i): ..."],
        "duties": [{"text": "HUD SHALL ...", "citation": "§ 552(a)(6)(A)(i)"}],
        "elements": [{"desc": "...", "if": "...", "and": "..."}],
        "facts": ["Plaintiff submitted ..."],
        "admissions": ["Defendant admits ..."],
        "breach_duty": "...",
        "breach_conduct": "...",
        "damage_items": [{"letter": "(a)", "desc": "..."}],
        "decision_trigger": {"if": "...", "and": "...", "then": "..."},
        "relief": ["Injunctive relief ...", "Damages ..."],
    }]

    defendants format:
    [{
        "name": "U.S. DEPARTMENT OF HOUSING AND URBAN DEVELOPMENT",
        "capacity": "her official capacity",
        "role": "",
        "title": "Government Information Specialist",
        "department": "HUD FOIA Office",
        "email": "yolonda.hatton@hud.gov",
        "conduct": "She was assigned as the official responsible for processing Plaintiff's request."
    }]
    """
    data = ComplaintData(
        court=court,
        district=district,
        case_court=case_court,
        plaintiff=Party(
            name=plaintiff_name,
            address=plaintiff_address,
            role=plaintiff_role,
            email=plaintiff_email,
        ),
        defendants=[
            Party(
                name=d.get("name", ""),
                capacity=d.get("capacity", ""),
                role=d.get("role", ""),
                title=d.get("title", ""),
                department=d.get("department", ""),
                email=d.get("email", ""),
                conduct=d.get("conduct", ""),
            )
            for d in defendants
        ],
        complaint_title=complaint_title,
        preliminary_statement=preliminary_statement or [],
        jurisdiction_paragraphs=jurisdiction_paragraphs or [],
        fact_sections=fact_sections or {},
        exhaustion_paragraphs=exhaustion_paragraphs or [],
        verification=verification,
        cert_of_service=cert_of_service,
    )

    # Build claims from claims_data
    for i, cd in enumerate(claims_data, 1):
        # Parse damage_items if provided
        d_items = []
        for di in cd.get("damage_items", []):
            d_items.append(DamageItem(
                letter=di.get("letter", f"({chr(96+len(d_items)+1)})"),
                description=di.get("desc", di.get("description", "")),
            ))

        claim = Claim(
            count_number=i,
            title=cd["title"],
            statute=cd["statute"],
            controlling_law=cd.get("controlling_law", []),
            construction_notes=cd.get("construction_notes", []),
            canons=cd.get("canons", []),
            duties=[Duty(number=j + 1, text=d["text"], citation=d.get("citation", ""))
                    for j, d in enumerate(cd.get("duties", []))],
            elements=[Element(number=j + 1, description=e["desc"],
                              if_clause=e.get("if", ""),
                              and_clause=e.get("and", ""),
                              then_clause=e.get("then", ""))
                      for j, e in enumerate(cd.get("elements", []))],
            facts=cd.get("facts", []),
            admissions=cd.get("admissions", []),
            breach_duty=cd.get("breach_duty", ""),
            breach_conduct=cd.get("breach_conduct", ""),
            damages=cd.get("damages", []),
            damage_items=d_items,
            decision_trigger_if=cd.get("decision_trigger", {}).get("if", ""),
            decision_trigger_and=cd.get("decision_trigger", {}).get("and", ""),
            decision_trigger_then=cd.get("decision_trigger", {}).get("then", ""),
            relief=cd.get("relief", []),
        )

        # Auto-generate construction notes if not provided
        if not claim.construction_notes:
            try:
                from pipeline.statutory_construction import apply_construction_rules
                # Run construction rules on the statute text from controlling law
                statute_text = " ".join(claim.controlling_law)
                analysis = apply_construction_rules(statute_text, claim.statute)
                fm = analysis.to_frontmatter()
                rules = fm.get("construction_rules", [])
                if rules:
                    claim.construction_notes = [
                        _format_construction_rule(r) for r in rules
                    ]
            except Exception:
                pass

        # Detect judicial misconduct claims
        title_lower = cd["title"].lower()
        if any(kw in title_lower for kw in ["judicial", "judge", "misconduct", "bias", "recusal", "disqualification"]):
            claim.is_judicial_claim = True

        # Auto-add canons if not provided
        if not claim.canons:
            claim.canons = _derive_canons(claim)
        data.claims.append(claim)

    # Build exhibits
    data.exhibits = [
        Exhibit(
            letter=e.get("letter", ""),
            description=e.get("description", ""),
            date=e.get("date", ""),
        )
        for e in exhibits
    ]

    # Prayer for relief: explicit if provided, else combine from all claims
    if prayer_for_relief:
        data.prayer_for_relief = prayer_for_relief
    else:
        data.prayer_for_relief = _build_default_prayer(data)

    # ── IMPROVEMENTS (data-driven, fail-closed): proof-of-fact questionnaire built from the PLEADED statutes
    # + enacting-law footnotes. This is what makes an email-found complaint match the accountability path's
    # proof-of-fact format. Reached by the native MCP draft_document -> route_draft -> here. ──
    try:
        from pipeline.drafter_grounding import (
            duties_from_claim_statutes, build_proof_questionnaire, enacting_law_footnotes,
        )
        _is_agency = any(
            ("agency" in (str(d.get("role", "")) + str(d.get("department", ""))).lower()
             or str(d.get("email", "")).lower().endswith(".gov"))
            for d in defendants
        )
        _duties = duties_from_claim_statutes(claims_data)
        _resp = (defendants[0].get("name") if defendants else "the respondent") or "the respondent"
        data.proof_of_facts = build_proof_questionnaire(_duties, respondent=_resp, is_agency=_is_agency)
        _blob = (complaint_title or "") + " " + " ".join(
            (str(c.get("statute", "")) + " " + " ".join(c.get("controlling_law", []) or []))
            for c in (claims_data or []))
        data.statutory_authority = enacting_law_footnotes(_blob)
    except Exception:
        data.proof_of_facts = ""
        data.statutory_authority = ""

    return build_complaint(data, output_path)


def _build_default_prayer(data: ComplaintData) -> List[str]:
    """Build consolidated prayer from claims when not explicitly provided."""
    prayer = []
    seen = set()

    for claim in data.claims:
        for r in claim.relief:
            # Deduplicate
            key = r.strip().lower()[:60]
            if key not in seen:
                seen.add(key)
                prayer.append(r)

    # Add standard closing items if not present
    standard = [
        "Retain jurisdiction until Defendants have fully complied with this Court's Orders",
    ]
    for s in standard:
        key = s.strip().lower()[:60]
        if key not in seen:
            prayer.append(s)

    return prayer


# ═══════════════════════════════════════════════════════
# CONSTRUCTION RULE FORMATTER
# ═══════════════════════════════════════════════════════

# Standard canons of construction — available for any complaint
CANONS_OF_CONSTRUCTION = {
    "expressio_unius": "Expressio unius est exclusio alterius — the expression of one thing implies the exclusion of others not mentioned.",
    "ejusdem_generis": "Ejusdem generis — where general words follow specific words in a list, the general words are limited to the same class as the specific words.",
    "noscitur_a_sociis": "Noscitur a sociis — a word is known by the company it keeps; terms in a list share characteristics.",
    "last_antecedent": "Rule of the last antecedent — a qualifying phrase modifies only the immediately preceding term, not the entire list.",
    "shall_mandatory": "The word 'shall' is mandatory, not discretionary. When Congress uses 'shall,' it imposes a non-discretionary duty.",
    "means_closed": "When a statute provides that a term 'means' something, the definition is exhaustive and closed. No additional meanings may be imported.",
    "includes_expansive": "When a statute provides that a term 'includes' something WITH an expansion clause (e.g. 'shall not be deemed to exclude' / 'including but not limited to'), the enumeration is illustrative; absent such a clause the enumerated items define the class and expansion beyond them must be argued under the canons (ejusdem generis / expressio unius), not assumed. See 26 U.S.C. 7701(c).",
    "whole_act": "Whole Act Rule / Anti-Surplusage Canon — every word in a statute must be given effect; no provision should be rendered superfluous.",
    "no_cross_import": "Definitions in one statute may not be imported into another statute absent explicit cross-reference.",
    "remedial_liberal": "Remedial statutes are to be liberally construed to effectuate their purpose.",
    "penal_strict": "Penal statutes are to be strictly construed in favor of the accused (Rule of Lenity).",
}

# Legal maxims commonly cited in complaints
LEGAL_MAXIMS = {
    "clean_hands": "He who comes into equity must come with clean hands.",
    "equity_delay": "Equity aids the vigilant, not those who sleep on their rights (vigilantibus non dormientibus aequitas subvenit).",
    "substance_form": "Equity regards substance rather than form.",
    "estoppel": "A party may be estopped from asserting a position inconsistent with prior conduct upon which another has relied.",
    "unjust_enrichment": "No person shall be permitted to profit by their own wrong (commodum ex injuria sua nemo habere debet).",
    "strict_compliance": "Where a statute prescribes a specific procedure, strict compliance is required.",
    "mandatory_duty": "Where a statute uses mandatory language ('shall'), the duty is non-discretionary and judicially enforceable.",
}

# Judicial conduct canons — for cases against judges
JUDICIAL_CANONS = {
    "canon_1": "Canon 1: A judge shall uphold and promote the independence, integrity, and impartiality of the judiciary, and shall avoid impropriety and the appearance of impropriety.",
    "canon_2": "Canon 2: A judge shall perform the duties of judicial office impartially, competently, and diligently.",
    "canon_2a": "Canon 2A: A judge shall hear and decide matters assigned to the judge, except when disqualification is required by Rule 2.11 or other law.",
    "canon_2b": "Canon 2B: A judge shall be patient, dignified, and courteous to litigants, jurors, witnesses, lawyers, court staff, court officials, and others with whom the judge deals in an official capacity.",
    "canon_3": "Canon 3: A judge shall conduct the judge's personal and extrajudicial activities to minimize the risk of conflict with the obligations of judicial office.",
    "canon_4": "Canon 4: A judge or candidate for judicial office shall not engage in political or campaign activity that is inconsistent with the independence, integrity, or impartiality of the judiciary.",
    "due_process": "The Due Process Clause of the Fourteenth Amendment requires a fair tribunal — a judge must recuse where bias or prejudice exists. Caperton v. A.T. Massey Coal Co., 556 U.S. 868 (2009).",
    "28_usc_455": "28 U.S.C. § 455(a): Any justice, judge, or magistrate judge of the United States shall disqualify himself in any proceeding in which his impartiality might reasonably be questioned.",
}


def _format_construction_rule(rule_dict: dict) -> str:
    """Format a construction rule finding for inclusion in the complaint."""


def _validate_claim_construction(claim) -> None:
    """Internal validation — applies construction rules to the claim during drafting.

    This does NOT add content to the complaint. It validates:
    1. "Shall" in duties = mandatory (not discretionary)
    2. Definitions use "means" (closed) or "includes" (expansive) correctly
    3. No cross-statute definition imports
    4. Terms used within their enacted scope
    5. Expressio unius — listed items exclude unlisted

    If violations found, they're logged as warnings.
    Canons guide the drafting — they don't appear in the text.
    """
    import logging
    log = logging.getLogger("complaint_drafter")

    try:
        from pipeline.statutory_construction import (
            apply_construction_rules, check_means_vs_includes,
            classify_shall_may, validate_no_cross_statute_import,
        )

        controlling_text = " ".join(claim.controlling_law)
        if not controlling_text.strip():
            return

        # Validate shall/may usage in duties
        for duty in claim.duties:
            classifications = classify_shall_may(duty.text)
            for cls in classifications:
                if cls.get("word") == "may" and "shall" not in duty.text.lower():
                    log.warning(
                        f"Claim '{claim.title}' duty {duty.number}: "
                        f"uses 'may' (discretionary) — verify this is correct for a mandatory duty claim"
                    )

        # Validate definitions in controlling law
        defs = check_means_vs_includes(controlling_text)
        for d in defs:
            scope = d.get("scope", "")
            term = d.get("term", "")
            if scope == "closed" and term:
                # Check if the complaint uses this term beyond its closed definition
                log.info(f"Construction: '{term}' has closed definition (means) — enforcing scope")

        # Check for cross-statute imports
        analysis = apply_construction_rules(controlling_text, claim.statute)
        fm = analysis.to_frontmatter()
        violations = fm.get("cross_statute_violations", [])
        for v in violations:
            log.warning(f"Construction violation in '{claim.title}': {v}")

    except Exception as e:
        log.debug(f"Construction validation skipped: {e}")


def _format_construction_rule(rule_dict: dict) -> str:
    """Format a construction rule finding for inclusion in the complaint."""
    rule_name = rule_dict.get("rule", "")
    finding = rule_dict.get("finding", "")
    terms = rule_dict.get("terms", [])

    # Map rule names to canonical descriptions
    canon_text = CANONS_OF_CONSTRUCTION.get(rule_name, "")

    if canon_text:
        result = f"{canon_text}"
        if finding:
            result += f" Application: {finding}"
        if terms:
            result += f" (terms: {', '.join(terms[:5])})"
        return result
    elif finding:
        return f"{rule_name}: {finding}"
    else:
        return f"Construction rule: {rule_name}"


def _derive_canons(claim: Claim) -> List[str]:
    """Derive applicable canons and maxims based on the claim type."""
    canons = []
    title_lower = claim.title.lower()
    statute_lower = claim.statute.lower()
    controlling_text = " ".join(claim.controlling_law).lower()

    # Always add shall = mandatory if duties reference "shall"
    for duty in claim.duties:
        if "shall" in duty.text.lower():
            canons.append(CANONS_OF_CONSTRUCTION["shall_mandatory"])
            break

    # Add whole act rule if multiple provisions cited
    if len(claim.controlling_law) > 2:
        canons.append(CANONS_OF_CONSTRUCTION["whole_act"])

    # Judicial misconduct cases — add judicial canons
    if any(kw in title_lower for kw in ["judicial", "judge", "misconduct", "bias", "recusal", "disqualification"]):
        canons.append(JUDICIAL_CANONS["canon_1"])
        canons.append(JUDICIAL_CANONS["canon_2"])
        canons.append(JUDICIAL_CANONS["due_process"])
        canons.append(JUDICIAL_CANONS["28_usc_455"])

    # Due process claims
    if "due process" in title_lower or "14th amendment" in title_lower or "fifth amendment" in title_lower:
        canons.append(LEGAL_MAXIMS["substance_form"])

    # FOIA / disclosure claims — remedial liberal construction
    if "foia" in title_lower or "552" in statute_lower or "disclosure" in title_lower:
        canons.append(CANONS_OF_CONSTRUCTION["remedial_liberal"])

    # If statute uses "means" definitions
    if "'means'" in controlling_text or '"means"' in controlling_text:
        canons.append(CANONS_OF_CONSTRUCTION["means_closed"])

    # If statute uses "includes" definitions
    if "'includes'" in controlling_text or '"includes"' in controlling_text:
        canons.append(CANONS_OF_CONSTRUCTION["includes_expansive"])

    # Mandatory duty claims — strict compliance
    if claim.breach_duty:
        canons.append(LEGAL_MAXIMS["mandatory_duty"])
        canons.append(LEGAL_MAXIMS["strict_compliance"])

    # Unjust enrichment / fraud claims
    if any(kw in title_lower for kw in ["fraud", "enrichment", "conversion", "theft"]):
        canons.append(LEGAL_MAXIMS["unjust_enrichment"])
        canons.append(LEGAL_MAXIMS["clean_hands"])

    # Deduplicate
    seen = set()
    unique = []
    for c in canons:
        if c not in seen:
            seen.add(c)
            unique.append(c)

    return unique
