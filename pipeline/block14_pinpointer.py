"""
BLOCK 14: Scorecard Violation Pinpointer + Deadline Calculator
1. Pinpoints exact violations with ECF/page/line citations
2. Cross-references court rules DB for rule citations
3. Quotes offending language from filings
4. Calculates filing deadlines for appeals, cert, motions
"""
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
import sys, re, json, time
sys.path.insert(0, str(_BEEZLE_ROOT))
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional
from core.logger import get_logger


log = get_logger("block14")

# ═══════════════════════════════════════════════════════════
# PART 1: VIOLATION PINPOINTER
# ═══════════════════════════════════════════════════════════

class ViolationPin:
    """A single pinpointed violation with exact citation."""
    def __init__(
        self,
        violation_type: str,
        rule_citation: str,
        rule_text: str,
        source_doc: str,          # e.g., "ECF 50" or "Order dated 2024-03-15"
        page: Optional[int] = None,
        line_start: Optional[int] = None,
        line_end: Optional[int] = None,
        header: str = "",
        quote: str = "",          # exact offending text
        explanation: str = "",    # why this violates the rule
        fix: str = "",            # what should have happened
        severity: str = "STANDARD",  # FATAL / SERIOUS / STANDARD / MINOR
    ):
        self.violation_type = violation_type
        self.rule_citation = rule_citation
        self.rule_text = rule_text
        self.source_doc = source_doc
        self.page = page
        self.line_start = line_start
        self.line_end = line_end
        self.header = header
        self.quote = quote
        self.explanation = explanation
        self.fix = fix
        self.severity = severity

    def to_dict(self) -> dict:
        d = {
            "violation_type": self.violation_type,
            "rule_citation": self.rule_citation,
            "rule_text": self.rule_text,
            "source_doc": self.source_doc,
            "quote": self.quote,
            "explanation": self.explanation,
            "fix": self.fix,
            "severity": self.severity,
        }
        if self.page:
            d["page"] = self.page
        if self.line_start:
            d["lines"] = f"{self.line_start}-{self.line_end or self.line_start}"
        if self.header:
            d["header"] = self.header
        return d

    def format_citation(self) -> str:
        """Format as legal-style citation: ECF 50, p. 3, ll. 4-7."""
        parts = [self.source_doc]
        if self.page:
            parts.append(f"p. {self.page}")
        if self.line_start:
            if self.line_end and self.line_end != self.line_start:
                parts.append(f"ll. {self.line_start}-{self.line_end}")
            else:
                parts.append(f"l. {self.line_start}")
        return ", ".join(parts)

    def format_full(self) -> str:
        """Full formatted violation for scorecard/brief."""
        lines = []
        lines.append(f"**VIOLATION: {self.violation_type}** [{self.severity}]")
        lines.append(f"  Rule: {self.rule_citation}")
        lines.append(f"  Source: {self.format_citation()}")
        if self.quote:
            lines.append(f'  Quote: "{self.quote}"')
        lines.append(f"  Violation: {self.explanation}")
        if self.fix:
            lines.append(f"  Required: {self.fix}")
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
# RULE VIOLATION PATTERNS
# ═══════════════════════════════════════════════════════════

RULE_VIOLATION_PATTERNS = {
    "TWOMBLY_IQBAL_MISAPPLICATION": {
        "rule": "FRCP Rule 12(b)(6); Bell Atlantic Corp. v. Twombly, 550 U.S. 544 (2007); Ashcroft v. Iqbal, 556 U.S. 662 (2009)",
        "rule_text": "A complaint must contain sufficient factual matter, accepted as true, to state a claim to relief that is plausible on its face.",
        "triggers": [
            r"conclusory\s+(?:allegations?|statements?|assertions?)",
            r"fail(?:s|ed)?\s+to\s+(?:state|plead|allege)\s+(?:a\s+)?(?:plausible|sufficient|viable)",
            r"(?:bare|naked|threadbare)\s+(?:recitals?|assertions?)",
            r"mere(?:ly)?\s+(?:labels?|conclusions?|formulaic)",
        ],
        "explanation": "Court applied heightened pleading standard beyond Twombly/Iqbal requirements or failed to accept well-pleaded facts as true",
        "fix": "All well-pleaded factual allegations must be accepted as true and all reasonable inferences drawn in plaintiff's favor at the 12(b)(6) stage",
        "severity": "FATAL",
    },
    "PRO_SE_LIBERAL_CONSTRUCTION": {
        "rule": "Haines v. Kerner, 404 U.S. 519 (1972); Erickson v. Pardus, 551 U.S. 89 (2007)",
        "rule_text": "Pro se complaints are to be held to less stringent standards than formal pleadings drafted by lawyers and must be liberally construed.",
        "triggers": [
            r"pro\s+se\s+(?:litigant|plaintiff|party).*(?:held|subject)\s+to\s+(?:same|equal|identical)\s+standard",
            r"(?:despite|notwithstanding|regardless).*pro\s+se\s+status",
            r"pro\s+se.*(?:not\s+(?:an?\s+)?excuse|no\s+exception|must\s+(?:still\s+)?comply)",
        ],
        "explanation": "Court failed to liberally construe pro se pleadings as required by binding SCOTUS precedent",
        "fix": "Pro se pleadings must be liberally construed, held to less stringent standards, and the court should identify the strongest arguments",
        "severity": "FATAL",
    },
    "VERIFICATION_MISSING": {
        "rule": "Pa.R.C.P. 1024; 42 Pa.C.S. § 7542(a)",
        "rule_state": "Pa.R.C.P. 1024",   # L7: forum-correct split (consumer picks by is_state)
        "rule_federal": "Fed. R. Civ. P. 11(a); 28 U.S.C. § 1746 (an unsworn factual averment is competent only if declared under penalty of perjury)",
        "rule_text": "Every pleading containing an averment of fact not appearing of record shall be verified by the party filing it.",
        "triggers": [
            r"(?:unverified|not\s+verified|lacks?\s+verification|without\s+verification)",
            r"(?:failed|failure)\s+to\s+verify",
            r"no\s+(?:affidavit|verification|oath)",
        ],
        "explanation": "Pleading contains factual averments without required verification under Pa.R.C.P. 1024",
        "fix": "Opposing party must file verified pleading or the unverified averments should be treated as admissions",
        "severity": "SERIOUS",
    },
    "SERVICE_DEFECTIVE": {
        "rule": "FRCP Rule 5(b); Pa.R.C.P. 440",
        "rule_state": "Pa.R.C.P. 440",   # L7: forum-correct split (consumer picks by is_state)
        "rule_federal": "Fed. R. Civ. P. 5(b)",
        "rule_text": "Service must be made on every party and proof of service filed with the court.",
        "triggers": [
            r"(?:not\s+(?:properly\s+)?served|improper\s+service|defective\s+service)",
            r"(?:no\s+(?:proof|certificate)\s+of\s+service)",
            r"(?:failed|failure)\s+to\s+serve",
        ],
        "explanation": "Filing was not properly served on all parties as required",
        "fix": "All filings must be served on every party with proof of service filed",
        "severity": "SERIOUS",
    },
    "NOTICE_INADEQUATE": {
        "rule": "U.S. Const. amend. XIV; Mullane v. Central Hanover Bank, 339 U.S. 306 (1950)",
        "rule_text": "Due process requires notice reasonably calculated to apprise interested parties of the pendency of the action and afford them an opportunity to present their objections.",
        "triggers": [
            r"(?:no|without|lack\s+of)\s+(?:adequate\s+)?notice",
            r"(?:denied|deprived).*(?:opportunity|chance)\s+to\s+(?:be\s+heard|respond|object)",
            r"(?:ex\s+parte|without\s+(?:a\s+)?hearing)",
        ],
        "explanation": "Party was denied constitutionally required notice and opportunity to be heard",
        "fix": "Due process requires notice and meaningful opportunity to be heard before deprivation of a protected interest",
        "severity": "FATAL",
    },
    "IMPROPER_STANDARD_OF_REVIEW": {
        "rule": "FRCP Rule 12(b)(6); FRCP Rule 56",
        "rule_text": "Motion to dismiss standard requires accepting all well-pleaded facts as true. Summary judgment requires showing no genuine dispute of material fact.",
        "triggers": [
            r"(?:weigh(?:ed|ing)?\s+(?:the\s+)?evidence|credibility\s+determination)\s+(?:at|on|in).*(?:12\(b\)|motion\s+to\s+dismiss)",
            r"(?:resolv(?:ed|ing)\s+(?:factual\s+)?disputes?|fact[-\s]?finding)\s+(?:at|on|in).*(?:12\(b\)|motion\s+to\s+dismiss)",
        ],
        "explanation": "Court applied wrong standard of review — weighed evidence or resolved factual disputes at improper stage",
        "fix": "At the 12(b)(6) stage, the court must accept all well-pleaded facts as true without weighing evidence",
        "severity": "FATAL",
    },
    "FILING_DEADLINE_VIOLATION": {
        "rule": "FRAP Rule 4(a)(1)(A); Pa.R.A.P. 903(a)",
        "rule_text": "Notice of appeal must be filed within 30 days after entry of the judgment or order appealed from.",
        "triggers": [
            r"(?:untimely|late|tardy)\s+(?:filed|filing|submission)",
            r"(?:after|past|beyond)\s+(?:the\s+)?deadline",
            r"(?:out\s+of\s+time|time[-\s]?barred)",
        ],
        "explanation": "Filing was submitted after the applicable deadline",
        "fix": "Filings must be submitted within the time prescribed by applicable rules",
        "severity": "SERIOUS",
    },
    "PAGE_LIMIT_EXCEEDED": {
        "rule": "FRAP Rule 32(a)(7); Pa.R.A.P. 2135",
        "rule_text": "Briefs must comply with applicable page and word count limits.",
        "triggers": [
            r"(?:exceeds?|over|beyond)\s+(?:the\s+)?(?:page|word)\s+(?:limit|count|maximum)",
        ],
        "explanation": "Filing exceeds applicable page or word count limits",
        "fix": "Reduce filing to comply with court-specific limits",
        "severity": "STANDARD",
    },
    "BIAS_JUDICIAL": {
        "rule": "28 U.S.C. § 455; Pa. Code of Judicial Conduct Canon 2.2",
        "rule_text": "A judge shall perform the duties of judicial office impartially, competently, and diligently without bias or prejudice.",
        "triggers": [
            r"(?:you\s+(?:people|pro\s+se)|(?:these|those)\s+(?:types?\s+of\s+)?(?:cases?|litigants?|plaintiffs?))",
            r"(?:waste|wasting)\s+(?:the\s+)?court(?:'s)?\s+time",
            r"(?:frivolous|vexatious|harassing).*(?:without|no)\s+(?:analysis|explanation|basis)",
        ],
        "explanation": "Court's language or conduct demonstrates bias or prejudice against a party",
        "fix": "Judicial conduct must comply with canons of judicial ethics; biased language is grounds for recusal motion",
        "severity": "FATAL",
    },

    # === BEEZLE V2 EXTENDED VIOLATIONS PATCH ===
    # The 9 patterns below are case-agnostic and statute-driven. They
    # detect: void judgments, jurisdictional defects, standing problems,
    # 18 USC color-of-law deprivations, obstruction, stare-decisis
    # violations, ignored prima facie evidence, disqualification duties,
    # and procedural due process violations.

    "VOID_JUDGMENT_RULE_60B4": {
        "severity": "FATAL",
        "category": "Void Judgment",
        "rule": "Fed. R. Civ. P. 60(b)(4); U.S. Const. amends. V, XIV",
        "description": (
            "Order is void on its face because the court that entered it "
            "lacked jurisdiction or acted in a manner inconsistent with due "
            "process of law. Void orders carry no time bar and may be "
            "challenged at any time."
        ),
        "authority": (
            "United Student Aid Funds, Inc. v. Espinosa, 559 U.S. 260, 270 "
            "(2010); Marshall v. Bd. of Educ., 575 F.2d 417, 422 (3d Cir. "
            "1978); In re Texlon Corp., 596 F.2d 1092, 1099 (2d Cir. 1979)"
        ),
        "triggers": [
            r"\bvoid\s+(?:ab\s+initio|on\s+its\s+face|order|judgment)\b",
            r"\b(?:lack(?:s|ed|ing)?)\s+(?:of\s+)?(?:subject[- ]?matter\s+)?jurisdiction\b",
            r"\bwithout\s+jurisdiction\b",
            r"\bRule\s+60\s*\(b\)\s*\(4\)",
            r"\bfundamental\s+(?:procedural\s+)?defect\b",
            r"\binconsistent\s+with\s+due\s+process\b",
        ],
        "remedy": (
            "Vacate the order under Fed. R. Civ. P. 60(b)(4). No time bar "
            "applies to void orders."
        ),
    },

    "SUBJECT_MATTER_JURISDICTION_DEFECT": {
        "severity": "FATAL",
        "category": "Jurisdiction",
        "rule": (
            "Fed. R. Civ. P. 12(h)(3); 28 U.S.C. §§ 1331, 1332; "
            "U.S. Const. art. III, § 2"
        ),
        "description": (
            "Court purported to exercise subject-matter jurisdiction over a "
            "matter where no federal-question, diversity, or supplemental "
            "basis was demonstrated on the record. Rule 12(h)(3) requires "
            "the court to dismiss whenever it determines such jurisdiction "
            "is lacking — even sua sponte and at any stage."
        ),
        "authority": (
            "Steel Co. v. Citizens for a Better Environment, 523 U.S. 83, "
            "94 (1998); Arbaugh v. Y&H Corp., 546 U.S. 500, 514 (2006); "
            "Henderson v. Shinseki, 562 U.S. 428, 434 (2011)"
        ),
        "triggers": [
            r"\bsubject[- ]?matter\s+jurisdiction\b",
            r"\bdiversity\s+jurisdiction\b",
            r"\bfederal[- ]?question\s+jurisdiction\b",
            r"\b28\s+U\.\s*S\.\s*C\.\s*§+\s*1331\b",
            r"\b28\s+U\.\s*S\.\s*C\.\s*§+\s*1332\b",
            r"\bRule\s+12\s*\(h\)\s*\(3\)",
            r"\b(?:lack(?:s|ed|ing)?|no)\s+(?:of\s+)?(?:federal\s+)?jurisdiction\b",
        ],
        "remedy": (
            "Move to dismiss under Fed. R. Civ. P. 12(h)(3) or move to vacate "
            "under Rule 60(b)(4) for lack of subject-matter jurisdiction."
        ),
    },

    "STANDING_DEFECT": {
        "severity": "FATAL",
        "category": "Standing",
        "rule": "U.S. Const. art. III, § 2; Fed. R. Civ. P. 12(b)(1)",
        "description": (
            "Party purports to assert a claim or defense without satisfying "
            "the three constitutional standing requirements: (1) injury in "
            "fact, (2) causation, and (3) redressability. Standing must be "
            "established at every stage of the litigation."
        ),
        "authority": (
            "Lujan v. Defenders of Wildlife, 504 U.S. 555, 560–61 (1992); "
            "Spokeo, Inc. v. Robins, 578 U.S. 330, 338 (2016); "
            "TransUnion LLC v. Ramirez, 141 S. Ct. 2190, 2203 (2021)"
        ),
        "triggers": [
            r"\bstanding\s+(?:to\s+)?(?:sue|assert|raise)\b",
            r"\barticle\s+III\s+standing\b",
            r"\binjury[- ]in[- ]fact\b",
            r"\bredressabil(?:e|ity)\b",
            r"\bconcrete\s+(?:and\s+particularized\s+)?(?:harm|injury)\b",
            r"\b(?:lack(?:s|ed)?|no|without)\s+(?:Article\s+III\s+)?standing\b",
        ],
        "remedy": (
            "Move to dismiss under Fed. R. Civ. P. 12(b)(1) for lack of "
            "standing; standing is a jurisdictional prerequisite."
        ),
    },

    "DEPRIVATION_OF_RIGHTS_COLOR_OF_LAW": {
        "severity": "FATAL",
        "category": "Civil Rights",
        "rule": "18 U.S.C. § 242; 42 U.S.C. § 1983; U.S. Const. amends. V, XIV",
        "description": (
            "Conduct under color of law that willfully deprives a person of "
            "rights, privileges, or immunities secured by the Constitution "
            "or laws of the United States. Section 242 criminalizes the "
            "conduct; § 1983 provides the civil remedy."
        ),
        "authority": (
            "Screws v. United States, 325 U.S. 91, 103 (1945); "
            "Monroe v. Pape, 365 U.S. 167, 184 (1961); "
            "West v. Atkins, 487 U.S. 42, 49 (1988)"
        ),
        "triggers": [
            r"\bcolor\s+of\s+(?:state\s+)?law\b",
            r"\bunder\s+color\s+of\b",
            r"\bdeprivation\s+of\s+(?:rights|civil\s+rights|due\s+process)\b",
            r"\b18\s+U\.\s*S\.\s*C\.\s*§+\s*242\b",
            r"\b42\s+U\.\s*S\.\s*C\.\s*§+\s*1983\b",
            r"\bwillful(?:ly)?\s+depriv(?:e|ed|ing|ation)\b",
        ],
        "remedy": (
            "Seek civil remedy under 42 U.S.C. § 1983; refer the predicate "
            "conduct for criminal review under 18 U.S.C. § 242."
        ),
    },

    "OBSTRUCTION_OF_JUSTICE": {
        "severity": "FATAL",
        "category": "Title 18",
        "rule": "18 U.S.C. § 1503; 18 U.S.C. § 1505; 18 U.S.C. § 1512",
        "description": (
            "Conduct that corruptly influences, obstructs, or impedes the "
            "due administration of justice, including by intimidation, "
            "concealment, or interference with the right to petition the "
            "courts."
        ),
        "authority": (
            "United States v. Aguilar, 515 U.S. 593, 599 (1995); "
            "Arthur Andersen LLP v. United States, 544 U.S. 696, 707 (2005)"
        ),
        "triggers": [
            r"\bobstruction\s+of\s+justice\b",
            r"\b18\s+U\.\s*S\.\s*C\.\s*§+\s*150[35]\b",
            r"\b18\s+U\.\s*S\.\s*C\.\s*§+\s*1512\b",
            r"\bcorruptly\s+(?:influenc|obstruct|imped)",
            r"\binterference\s+with\s+(?:the\s+)?(?:administration\s+of\s+)?justice\b",
        ],
        "remedy": (
            "Refer for criminal review under 18 U.S.C. §§ 1503, 1505; seek "
            "civil sanctions for litigation misconduct."
        ),
    },

    "STARE_DECISIS_VIOLATION": {
        "severity": "FATAL",
        "category": "Binding Precedent",
        "rule": "Hierarchy of binding precedent; 3d Cir. I.O.P. 5.7",
        "description": (
            "Court or party relies on non-binding authority (non-precedential "
            "disposition, sister-circuit opinion, out-of-circuit district "
            "court) as if it were controlling. Under stare decisis, only "
            "decisions of the Supreme Court of the United States and the "
            "governing Court of Appeals bind a district court."
        ),
        "authority": (
            "Hutto v. Davis, 454 U.S. 370, 375 (1982) (per curiam); "
            "Rodriguez de Quijas v. Shearson/Am. Express, 490 U.S. 477, 484 "
            "(1989); 3d Cir. I.O.P. 5.7"
        ),
        "triggers": [
            r"\bstare\s+decisis\b",
            r"\bbinding\s+(?:precedent|authority)\b",
            r"\bnon[-\s]?precedential\b",
            r"\bnot\s+precedential\b",
            r"\bunpublished\s+(?:opinion|disposition)\b",
            r"\bF\.\s*App'?x\b",
            r"\bI\.O\.P\.\s*5\.7\b",
        ],
        "remedy": (
            "Strike or disregard non-binding citations; demand that the "
            "court rely only on authority that binds it."
        ),
    },

    "PRIMA_FACIE_EVIDENCE_IGNORED": {
        "severity": "SERIOUS",
        "category": "Evidence",
        "rule": "Fed. R. Evid. 301; Fed. R. Evid. 201(b)(2)",
        "description": (
            "Court or opposing party disregards evidence that establishes a "
            "prima facie case or invokes a binding presumption. Under FRE "
            "301, a presumption shifts the burden of production; under FRE "
            "201(b)(2), a fact 'capable of accurate and ready determination' "
            "must be judicially noticed on request."
        ),
        "authority": (
            "Rosenthal v. Walker, 111 U.S. 185, 193 (1884) (mailbox "
            "presumption); Hagner v. United States, 285 U.S. 427, 430 "
            "(1932); Texas Dep't of Cmty. Affairs v. Burdine, 450 U.S. 248, "
            "254 (1981)"
        ),
        "triggers": [
            r"\bprima\s+facie\b",
            r"\bmailbox\s+(?:presumption|rule)\b",
            r"\b(?:rebuttable\s+)?presumption\b",
            r"\bFed\.\s*R\.\s*Evid\.\s*301\b",
            r"\bF\.?R\.?E\.?\s*301\b",
            r"\b(?:burden\s+of\s+production|burden[- ]shift)\b",
        ],
        "remedy": (
            "Move for judicial notice under FRE 201(b)(2); invoke the "
            "presumption under FRE 301; if presumption is rebuttable, "
            "demand the contrary evidence."
        ),
    },

    "JUDICIAL_DISQUALIFICATION_DUTY": {
        "severity": "FATAL",
        "category": "Judicial Conduct",
        "rule": "28 U.S.C. § 455(a), (b); Code of Conduct for U.S. Judges Canon 3C",
        "description": (
            "Judge has a non-discretionary duty to disqualify in any "
            "proceeding in which the judge's impartiality might reasonably "
            "be questioned. The standard is objective. The duty is "
            "self-enforcing and does not require a motion."
        ),
        "authority": (
            "Liljeberg v. Health Servs. Acquisition Corp., 486 U.S. 847, "
            "858 (1988); Liteky v. United States, 510 U.S. 540, 548 (1994); "
            "Caperton v. A.T. Massey Coal Co., 556 U.S. 868, 884 (2009)"
        ),
        "triggers": [
            r"\bdisqualif(?:y|ication|ied)\b",
            r"\brecus(?:e|al|ation)\b",
            r"\bimpartial(?:ity)?\b",
            r"\b28\s+U\.\s*S\.\s*C\.\s*§+\s*455\b",
            r"\bappearance\s+of\s+(?:bias|partiality|impropriety)\b",
            r"\bjudicial\s+bias\b",
        ],
        "remedy": (
            "Move for recusal under 28 U.S.C. § 455(a); preserve the issue "
            "for appellate review under abuse-of-discretion standard."
        ),
    },

    "PROCEDURAL_DUE_PROCESS_VIOLATION": {
        "severity": "FATAL",
        "category": "Due Process",
        "rule": "U.S. Const. amends. V, XIV; Fed. R. Civ. P. 60(b)(4)",
        "description": (
            "Government action that deprives a person of life, liberty, or "
            "property without providing the procedural safeguards required "
            "by the Constitution: notice reasonably calculated to inform, "
            "an opportunity to be heard, and a neutral decision-maker."
        ),
        "authority": (
            "Mullane v. Central Hanover Bank, 339 U.S. 306, 314 (1950); "
            "Mathews v. Eldridge, 424 U.S. 319, 333 (1976); "
            "Goldberg v. Kelly, 397 U.S. 254, 267 (1970)"
        ),
        "triggers": [
            r"\bdue\s+process\b",
            r"\bMullane\b",
            r"\bMathews\s+v\.?\s+Eldridge\b",
            r"\bopportunity\s+to\s+be\s+heard\b",
            r"\bnotice\s+(?:and|or)\s+(?:an?\s+)?opportunity\b",
            r"\bprocedural\s+due\s+process\b",
        ],
        "remedy": (
            "Move to vacate the offending order as void under Rule 60(b)(4); "
            "seek injunctive relief under 42 U.S.C. § 1983 if state action."
        ),
    },


    # === BEEZLE CANONS PATCH ===
    # Judicial Code of Conduct + Rules of Professional Conduct applied to
    # both judge and lawyer authored documents.

    "JUDICIAL_CANON_VIOLATION": {
        "severity": "FATAL",
        "category": "Judicial Conduct",
        "rule": (
            "Code of Conduct for U.S. Judges Canons 1, 2, 3; "
            "Pa. Code of Judicial Conduct Canons 1, 2, 3; "
            "28 U.S.C. § 455"
        ),
        "description": (
            "Conduct that fails to uphold the integrity and independence of "
            "the judiciary (Canon 1), creates an appearance of impropriety "
            "(Canon 2), or breaches the duty to perform judicial duties "
            "impartially and diligently (Canon 3). Includes prejudgment of "
            "facts, ex parte communications, and failure to disqualify when "
            "required."
        ),
        "authority": (
            "Code of Conduct for U.S. Judges Canons 1-3; "
            "Caperton v. A.T. Massey Coal Co., 556 U.S. 868, 884 (2009); "
            "Liljeberg v. Health Servs. Acquisition Corp., 486 U.S. 847, 858 (1988); "
            "Liteky v. United States, 510 U.S. 540, 548 (1994)"
        ),
        "triggers": [
            r"\bappearance\s+of\s+impropriety\b",
            r"\bappearance\s+of\s+bias\b",
            r"\bappearance\s+of\s+partiality\b",
            r"\bex\s+parte\s+(?:communication|contact|hearing|order)\b",
            r"\bprejudg(?:e|ed|ment)\b",
            r"\bjudicial\s+(?:integrity|independence|impartiality)\b",
            r"\bCanon\s+[123]\b",
            r"\bCode\s+of\s+(?:Judicial\s+)?Conduct\b",
        ],
        "remedy": (
            "File judicial conduct complaint under 28 U.S.C. § 351; move "
            "for recusal under § 455(a); preserve issue for appellate "
            "review."
        ),
    },

    "LAWYER_RULE_VIOLATION": {
        "severity": "SERIOUS",
        "category": "Professional Conduct",
        "rule": (
            "ABA Model Rules of Professional Conduct 1.1, 3.1, 3.3, 3.4; "
            "Pa. R.P.C. 1.1, 3.1, 3.3, 3.4"
        ),
        "description": (
            "Conduct by counsel that violates the duty of competence "
            "(R.P.C. 1.1), the prohibition on frivolous arguments (R.P.C. "
            "3.1), the duty of candor to the tribunal (R.P.C. 3.3) — "
            "including knowingly making a false statement of law or "
            "failing to disclose controlling adverse authority — or the "
            "duty of fairness to the opposing party (R.P.C. 3.4)."
        ),
        "authority": (
            "Pa. R.P.C. 3.3 (Candor Toward the Tribunal); "
            "Pa. R.P.C. 3.4 (Fairness to Opposing Party); "
            "Pa. R.P.C. 3.1 (Meritorious Claims); "
            "Nix v. Whiteside, 475 U.S. 157, 174 (1986)"
        ),
        "triggers": [
            r"\bduty\s+of\s+candor\b",
            r"\bcandor\s+to\s+(?:the\s+)?tribunal\b",
            r"\bRule\s+(?:of\s+Professional\s+Conduct\s+)?3\.[1-4]\b",
            r"\bR\.?P\.?C\.?\s*3\.[1-4]\b",
            r"\bMod(?:el)?\.?\s*R\.?P\.?C\.?\s*3\.[1-4]\b",
            r"\bfrivolous\s+(?:argument|claim|motion|filing)\b",
            r"\bcontrolling\s+adverse\s+authority\b",
            r"\bfailure\s+to\s+disclose\b",
            r"\bmisrepresent(?:ed|ation|ing)?\s+(?:the\s+)?(?:law|record)\b",
        ],
        "remedy": (
            "Move for sanctions under FRCP 11 or 28 U.S.C. § 1927; refer "
            "to state bar for disciplinary action; demand correction on "
            "the record under R.P.C. 3.3(a)."
        ),
    },


    # === BEEZLE REMOVAL+SANCTIONS PATCH ===

    "REMOVAL_DEFECTIVE": {
        "severity": "FATAL",
        "category": "Removal",
        "rule": "28 U.S.C. §§ 1441, 1446, 1447; Fed. R. Civ. P. 81",
        "description": (
            "Notice of removal that fails the procedural requirements of "
            "28 U.S.C. § 1446: filing within 30 days of service of the "
            "initial pleading, unanimous consent of all properly served "
            "defendants, and attachment of all process, pleadings, and "
            "orders served upon the defendants. Defective removal is "
            "non-curable beyond 30 days and requires remand under § 1447(c)."
        ),
        "authority": (
            "Murphy Bros., Inc. v. Michetti Pipe Stringing, Inc., 526 U.S. "
            "344, 354 (1999); Lewis v. Rego Co., 757 F.2d 66, 68 (3d Cir. "
            "1985); Balazik v. County of Dauphin, 44 F.3d 209, 213 (3d "
            "Cir. 1995) (unanimous-consent rule)"
        ),
        "triggers": [
            r"\bnotice\s+of\s+removal\b",
            r"\b28\s+U\.\s*S\.\s*C\.\s*§+\s*1446\b",
            r"\b28\s+U\.\s*S\.\s*C\.\s*§+\s*1441\b",
            r"\b28\s+U\.\s*S\.\s*C\.\s*§+\s*1447\b",
            r"\bremoval\s+(?:was\s+)?(?:improper|defective|untimely|premature)\b",
            r"\b(?:30|thirty)[- ]day\s+(?:removal\s+)?deadline\b",
            r"\bunanimous\s+consent\b.*\bremov",
            r"\bremand(?:ed)?\s+to\s+state\s+court\b",
            r"\bremoval\s+(?:is\s+|was\s+)?procedurally\s+(?:defective|improper)\b",
        ],
        "remedy": (
            "Move for remand under 28 U.S.C. § 1447(c). Motion must be "
            "filed within 30 days of removal except for subject-matter "
            "jurisdiction defects (which can be raised at any time)."
        ),
    },

    "FRAUD_ON_THE_COURT": {
        "severity": "FATAL",
        "category": "Fraud / Tribunal Integrity",
        "rule": "Fed. R. Civ. P. 60(d)(3); inherent power of the court",
        "description": (
            "Conduct that defiles the court itself or improperly influences "
            "the judicial machinery — including knowingly false statements "
            "of material fact, fabricated evidence, bribery, or improper "
            "lawyer-client conduct that prevents the adverse party from "
            "fully and fairly presenting its case. Rule 60(d)(3) recognizes "
            "an independent action for fraud on the court without time bar."
        ),
        "authority": (
            "Hazel-Atlas Glass Co. v. Hartford-Empire Co., 322 U.S. 238 "
            "(1944); Universal Oil Prods. Co. v. Root Refining Co., 328 "
            "U.S. 575 (1946); Herring v. United States, 424 F.3d 384, 386 "
            "(3d Cir. 2005)"
        ),
        "triggers": [
            r"\bfraud\s+(?:upon|on)\s+the\s+court\b",
            r"\bRule\s+60\s*\(d\)\s*\(3\)",
            r"\bHazel[- ]Atlas\b",
            r"\bfabricat(?:ed|ion)\s+(?:evidence|document)",
            r"\bknowingly\s+false\s+statement",
            r"\bperjur(?:y|ed|ious)",
            r"\bsuborn(?:ed|ation)\b",
        ],
        "remedy": (
            "File an independent action under FRCP 60(d)(3); no time bar. "
            "Seek inherent-power sanctions; refer to disciplinary "
            "authority and U.S. Attorney for criminal referral."
        ),
    },

    "SANCTIONS_TRIGGERED": {
        "severity": "SERIOUS",
        "category": "Sanctions",
        "rule": (
            "Fed. R. Civ. P. 11; 28 U.S.C. § 1927; inherent power "
            "(Chambers v. NASCO)"
        ),
        "description": (
            "Conduct that warrants sanctions under FRCP 11 (frivolous "
            "filings, factual contentions without evidentiary support, "
            "legal contentions not warranted by existing law), or § 1927 "
            "(unreasonably and vexatiously multiplied proceedings), or the "
            "court's inherent power (bad faith conduct)."
        ),
        "authority": (
            "Chambers v. NASCO, Inc., 501 U.S. 32 (1991); "
            "Cooter & Gell v. Hartmarx Corp., 496 U.S. 384 (1990); "
            "Roadway Express, Inc. v. Piper, 447 U.S. 752 (1980); "
            "In re Prudential Ins. Co., 278 F.3d 175 (3d Cir. 2002)"
        ),
        "triggers": [
            r"\bRule\s+11\b.*\bsanction",
            r"\bFRCP\s+11\b",
            r"\b28\s+U\.\s*S\.\s*C\.\s*§+\s*1927\b",
            r"\bvexatious(?:ly)?\b",
            r"\bunreasonabl(?:y|e)\s+multipl(?:y|ied)\b",
            r"\bbad[- ]faith\s+(?:conduct|litigation|filing)\b",
            r"\bfrivolous\s+(?:filing|claim|motion|appeal)\b",
            r"\bsafe[- ]harbor\b.*\b21[- ]day\b",
        ],
        "remedy": (
            "Move for Rule 11 sanctions (with 21-day safe harbor) or "
            "§ 1927 sanctions; inherent-power sanctions in egregious cases."
        ),
    },


    # === BEEZLE DKT141 SPECIFICS PATCH ===

    "SUA_SPONTE_RULING": {
        "severity": "FATAL",
        "category": "Procedural Due Process",
        "rule": (
            "U.S. Const. amends. V, XIV; "
            "Fed. R. Civ. P. 12(h)(3) (only SMJ); "
            "Bryson v. Brand Insulations, 621 F.2d 556 (3d Cir. 1980)"
        ),
        "description": (
            "Court issued a substantive ruling on its own motion without "
            "briefing or notice to the affected party. Sua sponte rulings "
            "on the merits generally violate procedural due process; the "
            "Third Circuit requires notice and opportunity to respond "
            "before sua sponte dismissal or exclusion."
        ),
        "authority": (
            "Mathews v. Eldridge, 424 U.S. 319 (1976); "
            "Bryson v. Brand Insulations, Inc., 621 F.2d 556 (3d Cir. 1980); "
            "Oatess v. Sobolevitch, 914 F.2d 428 (3d Cir. 1990); "
            "Goldberg v. Kelly, 397 U.S. 254 (1970)"
        ),
        "triggers": [
            r"\bsua\s+sponte\b",
            r"\bon\s+(?:the\s+)?Court'?s?\s+own\s+motion\b",
            r"\bof\s+its\s+own\s+motion\b",
            r"\b(?:the\s+)?Court\s+(?:hereby\s+)?(?:dismisses|excludes|strikes|enjoin)",
        ],
        "remedy": (
            "Move for reconsideration / vacate under FRCP 60(b)(4); cite "
            "Bryson for the binding 3d Cir. notice requirement."
        ),
    },

    "SOVEREIGN_CITIZEN_LABEL": {
        "severity": "SERIOUS",
        "category": "Judicial Conduct",
        "rule": (
            "U.S. Const. amend. V; "
            "Caperton v. A.T. Massey Coal Co., 556 U.S. 868 (2009); "
            "Liteky v. United States, 510 U.S. 540 (1994)"
        ),
        "description": (
            "Court characterizes a pro se litigant as a 'sovereign citizen' "
            "or invokes related labels (redemption movement, Strawman "
            "doctrine, parse-syntax grammar) without record evidence that "
            "the litigant's filings invoke those movements. The label "
            "functions as ad hominem dismissal and supports an appearance-"
            "of-bias inference under Caperton."
        ),
        "authority": (
            "Caperton v. A.T. Massey Coal Co., 556 U.S. 868 (2009); "
            "Liteky v. United States, 510 U.S. 540, 555 (1994) "
            "(rulings must rest on the record); "
            "Liljeberg v. Health Servs., 486 U.S. 847 (1988)"
        ),
        "triggers": [
            r"\bsovereign\s+citizen\b",
            r"\bsovereign\s+citizen[- ]like\b",
            r"\bsov\s*cit\b",
            r"\bredemption\s+movement\b",
            r"\bstrawman\s+(?:theory|doctrine|argument)\b",
            r"\bpseudolaw\b",
        ],
        "remedy": (
            "Move for reconsideration; demand record citations supporting "
            "the characterization; preserve under Caperton for appellate "
            "review."
        ),
    },

    "BILL_OF_ATTAINDER_MISLABEL": {
        "severity": "FATAL",
        "category": "Constitutional / Misapplication",
        "rule": (
            "U.S. Const. art. I, §§ 9, 10; "
            "United States v. Lovett, 328 U.S. 303 (1946)"
        ),
        "description": (
            "Article I, §§ 9 and 10 forbid the legislature (Congress and "
            "the states) from passing bills of attainder. The Bill of "
            "Attainder Clause does not apply to courts or to civil "
            "pleadings. Invocation of the clause against a pro se "
            "plaintiff's filings, or treatment of a complaint as "
            "'punishment without trial,' reflects legal misapplication."
        ),
        "authority": (
            "U.S. Const. art. I, §§ 9, 10; "
            "United States v. Lovett, 328 U.S. 303 (1946) "
            "(legislative punishment requirement); "
            "Nixon v. Adm'r of Gen. Servs., 433 U.S. 425 (1977); "
            "Selective Serv. Sys. v. Minnesota PIRG, 468 U.S. 841 (1984)"
        ),
        "triggers": [
            r"\bbill\s+of\s+attainder\b",
            r"\bart(?:icle|\.|s)?\s*I\b.*\battainder\b",
            r"\bbills?\s+of\s+attainder\s+(?:clause|provision)\b",
        ],
        "remedy": (
            "Move for reconsideration on the ground that the Bill of "
            "Attainder Clause does not apply to civil pleadings or to "
            "judicial orders; cite Lovett."
        ),
    },

    "IMMUNITY_WAIVER_TRIGGERED": {
        "severity": "SERIOUS",
        "category": "Immunity",
        "rule": (
            "Hazel-Atlas Glass Co. v. Hartford-Empire Co., 322 U.S. 238 (1944); "
            "Forrester v. White, 484 U.S. 219 (1988); "
            "Pulliam v. Allen, 466 U.S. 522 (1984)"
        ),
        "description": (
            "Conditions on the record trigger one of the recognized "
            "exceptions to judicial immunity: (a) clear absence of all "
            "jurisdiction (Bradley v. Fisher); (b) non-judicial acts — "
            "administrative or ministerial (Forrester; Mireles); "
            "(c) declaratory or prospective injunctive relief (Pulliam; "
            "Ex parte Young); or (d) fraud upon the court (Hazel-Atlas)."
        ),
        "authority": (
            "Hazel-Atlas Glass Co. v. Hartford-Empire Co., 322 U.S. 238 (1944); "
            "Forrester v. White, 484 U.S. 219 (1988); "
            "Mireles v. Waco, 502 U.S. 9 (1991); "
            "Bradley v. Fisher, 80 U.S. 335 (1871); "
            "Pulliam v. Allen, 466 U.S. 522 (1984); "
            "Ex parte Young, 209 U.S. 123 (1908); "
            "Stump v. Sparkman, 435 U.S. 349 (1978)"
        ),
        "triggers": [
            r"\b(?:judicial\s+)?immunity\s+waiver\b",
            r"\bHazel[- ]Atlas\b",
            r"\bForrester\s+v\.?\s+White\b",
            r"\bMireles\s+v\.?\s+Waco\b",
            r"\bPulliam\s+v\.?\s+Allen\b",
            r"\bclear\s+absence\s+of\s+(?:all\s+)?jurisdiction\b",
            r"\bministerial\s+(?:act|function|duty)\b",
            r"\bprospective\s+(?:injunctive\s+)?relief\s+against\s+(?:a\s+)?judge\b",
        ],
        "remedy": (
            "Independent action under FRCP 60(d)(3) for fraud on the "
            "court (no time bar); § 1983 for prospective relief; refer "
            "to Judicial Council under 28 U.S.C. § 351."
        ),
    },

    # === REAL_PARTY_RECIPROCITY_V2 (flip-it-back, 2026-08-25) ===
    # Fires when an ENFORCING FILER or the court wields a standing /
    # real-party / holder argument against the pro se party. The flip:
    # the identical requirement binds the party who FILED to enforce.
    # Name WHO filed; test THEIR holder status (allonge / indorsement /
    # possession) under Rule 17(a) + U.C.C. 3-203/3-205/3-301.
    "REAL_PARTY_RECIPROCITY": {
        "severity": "FATAL",
        "category": "Standing",
        "rule": (
            "Fed. R. Civ. P. 17(a); U.C.C. §§ 3-203, 3-204, 3-205, 3-301; "
            "13 Pa.C.S. §§ 3203, 3204, 3205, 3301, 3309; Pa.R.C.P. 2002"
        ),
        "rule_state": "Pa.R.C.P. 2002; 13 Pa.C.S. §§ 3203, 3205, 3301",
        "rule_federal": "Fed. R. Civ. P. 17(a); U.C.C. §§ 3-203, 3-205, 3-301",
        "rule_text": (
            "An action must be prosecuted in the name of the real party in "
            "interest; only a holder or a person entitled to enforce under "
            "U.C.C. 3-301 may enforce a negotiable instrument."
        ),
        "description": (
            "A standing / real-party / holder requirement is being applied "
            "against the pro se party while the party who FILED to enforce "
            "is excused from the identical showing. The requirement is "
            "reciprocal: the enforcing filer must itself prove holder or "
            "person-entitled-to-enforce status (indorsement, allonge, or "
            "possession chain) under the same rules."
        ),
        "authority": (
            "Fed. R. Civ. P. 17(a); U.C.C. §§ 3-203, 3-204, 3-205, 3-301; "
            "13 Pa.C.S. §§ 3203, 3204, 3205, 3301, 3309; "
            "Lujan v. Defenders of Wildlife, 504 U.S. 555, 561 (1992) "
            "(the party invoking the court's power bears the burden of "
            "establishing standing)"
        ),
        "triggers": [
            r"\b(?:borrower|plaintiff|debtor|mortgagor|appellant)s?\s+(?:lacks?|has\s+no|have\s+no|without)\s+standing\b",
            r"\blacks?\s+standing\s+to\s+(?:challenge|contest|question|attack)\s+(?:the\s+)?(?:assignment|securitization|transfer|chain|indorsement|endorsement)\b",
            r"\bnot\s+a\s+(?:party|third[- ]party\s+beneficiary)\s+(?:to|of)\s+the\s+(?:assignment|pooling|PSA|pooling\s+and\s+servicing|securitization|trust)\b",
            r"\bstanding\s+to\s+(?:enforce|foreclose)\b",
            r"\breal\s+party\s+in\s+interest\b",
            r"\b(?:is|as)\s+the\s+(?:holder|owner)\s+of\s+the\s+(?:note|mortgage|loan)\b",
            r"\bperson\s+entitled\s+to\s+enforce\b",
            r"\bentitled\s+to\s+enforce\s+the\s+(?:note|instrument|mortgage)\b",
        ],
        "explanation": (
            "The enforcing filer invoked the standing / real-party / holder "
            "rule against the pro se party; the identical rule disqualifies "
            "the filer absent proof of its own holder or "
            "person-entitled-to-enforce status (Rule 17(a); U.C.C. 3-301)."
        ),
        "fix": (
            "Name the party who filed the enforcement and require its "
            "compliance with the same rule it invoked: production of the "
            "original note with unbroken indorsements or allonge "
            "(U.C.C. 3-203/3-204/3-205), or dismissal / joinder of the real "
            "party in interest under Rule 17(a)."
        ),
        "remedy": (
            "Move to dismiss the enforcement (or the enforcing claim) for "
            "want of real-party / holder status, or require joinder of the "
            "real party in interest under Fed. R. Civ. P. 17(a); the filer "
            "must satisfy the identical standard it invoked."
        ),
    },

}


def scan_document_for_violations(
    text: str,
    source_doc: str = "Unknown Document",
    court: str = "EDPA",
) -> list[ViolationPin]:
    """Scan a document for rule violations with exact location pinpointing.

    # === BEEZLE_BLOCK14_KEYERROR_FIX_V1 ===
    Defensive: pattern_data may be missing any of the optional keys
    ("rule", "rule_text", "explanation", "fix", "severity").  We use
    .get() with sensible defaults and isolate each pattern in a
    try/except so one malformed entry cannot abort the whole scan
    (which previously caused REMOVAL_DEFECTIVE / FRAUD_ON_THE_COURT /
    SANCTIONS_TRIGGERED hits to silently disappear).
    """
    violations = []
    lines = text.split("\n")
    _warned_missing = set()
    # L7: forum flag so a jurisdiction-specific pattern (VERIFICATION_MISSING/SERVICE_DEFECTIVE) renders the
    # rule for THIS court — not a PA rule in a federal filing. is_state = PA Common Pleas courts.
    _cu = (court or "").upper()
    _is_state = _cu.startswith("PA") or "CCP" in _cu or "COMMON PLEAS" in _cu or "PHILA" in _cu
    _required_keys = ("rule", "rule_text", "explanation", "fix", "severity")

    for vtype, pattern_data in RULE_VIOLATION_PATTERNS.items():
        try:
            triggers = pattern_data.get("triggers", []) if isinstance(pattern_data, dict) else []
            if not triggers:
                continue
            # one-time warning about missing keys
            if isinstance(pattern_data, dict):
                missing = [k for k in _required_keys if k not in pattern_data]
                if missing and vtype not in _warned_missing:
                    log.warning(
                        "block14 pattern %s missing keys %s (filling with defaults)",
                        vtype, missing,
                    )
                    _warned_missing.add(vtype)
            for trigger in triggers:
                try:
                    iterator = re.finditer(trigger, text, re.IGNORECASE)
                except re.error as _e:
                    log.warning("block14 pattern %s trigger %r is not valid regex: %s",
                                vtype, trigger, _e)
                    continue
                for match in iterator:
                    char_pos = match.start()
                    line_num = text[:char_pos].count("\n") + 1
                    page_num = (line_num // 50) + 1

                    header = ""
                    for i in range(max(0, line_num - 10), line_num):
                        if i < len(lines):
                            l = lines[i].strip()
                            if l and (l.isupper() or l.startswith("##") or l.startswith("**")):
                                header = l[:80]

                    ctx_start = max(0, match.start() - 40)
                    ctx_end = min(len(text), match.end() + 60)
                    quote = text[ctx_start:ctx_end].replace("\n", " ").strip()
                    if ctx_start > 0:
                        quote = "..." + quote
                    if ctx_end < len(text):
                        quote = quote + "..."

                    line_end = min(line_num + 3, len(lines))

                    pin = ViolationPin(
                        violation_type=vtype,
                        rule_citation=((pattern_data.get("rule_state") if _is_state
                                        else pattern_data.get("rule_federal"))
                                       or pattern_data.get("rule", "")),   # L7: forum-correct rule
                        rule_text=pattern_data.get("rule_text", ""),
                        source_doc=source_doc,
                        page=page_num,
                        line_start=line_num,
                        line_end=line_end,
                        header=header,
                        quote=quote,
                        explanation=pattern_data.get("explanation", ""),
                        fix=pattern_data.get("fix", ""),
                        severity=pattern_data.get("severity", "STANDARD"),
                    )
                    violations.append(pin)
                    break  # one match per pattern per document section
        except Exception as _e:
            log.warning(
                "block14 pattern %s raised %s during scan of %s: %s -- skipping this pattern only",
                vtype, type(_e).__name__, source_doc, _e,
            )
            continue

    # Deduplicate by type+page
    seen = set()
    unique = []
    for v in violations:
        key = f"{v.violation_type}|{v.page}"
        if key not in seen:
            seen.add(key)
            unique.append(v)

    log.info(f"Scanned {source_doc}: {len(unique)} violations found")
    return unique


# BEEZLE_FN_UNUSED 2026-06-13: scan_case_filings (no static refs in pipeline/)
def scan_case_filings(case_dir: Path) -> list[ViolationPin]:
    """Scan all filings in a case directory for violations."""
    from pipeline.doc_reader import extract_text

    all_violations = []
    if not case_dir.exists():
        return all_violations

    for f in sorted(case_dir.iterdir()):
        if f.suffix.lower() in {".pdf", ".docx", ".txt", ".md"}:
            try:
                text = extract_text(f)
                if text and len(text) > 100:
                    # Detect ECF number from filename
                    ecf_match = re.search(r"(?:ecf|doc)[-_]?(\d+)", f.stem, re.IGNORECASE)
                    source = f"ECF {ecf_match.group(1)}" if ecf_match else f.stem
                    violations = scan_document_for_violations(text, source)
                    all_violations.extend(violations)
            except Exception as e:
                log.info(f"  Scan failed for {f.name}: {e}")

    return all_violations


# ═══════════════════════════════════════════════════════════
# PART 2: DEADLINE CALCULATOR
# ═══════════════════════════════════════════════════════════

FILING_DEADLINES = {
    # Federal
    "notice_of_appeal_federal": {
        "days": 30,
        "from": "entry of judgment or order",
        "rule": "FRAP Rule 4(a)(1)(A)",
        "extensions": "30-day extension if motion filed within 30 days (FRAP 4(a)(5))",
    },
    "cert_petition_scotus": {
        "days": 90,
        "from": "entry of judgment by court of appeals",
        "rule": "SCOTUS Rule 13.1",
        "extensions": "Extension up to 60 days for good cause (Rule 13.5)",
    },
    "motion_reconsideration_federal": {
        "days": 28,
        "from": "entry of judgment",
        "rule": "FRCP Rule 59(e)",
        "extensions": "None — jurisdictional deadline",
        "note": "Tolls appeal deadline if timely filed",
    },
    "rule_60b_motion": {
        "days": 365,
        "from": "entry of judgment",
        "rule": "FRCP Rule 60(b)(1)-(3)",
        "note": "Rule 60(b)(4)-(6) have no time limit but must be 'reasonable'",
    },
    "answer_to_complaint": {
        "days": 21,
        "from": "service of summons and complaint",
        "rule": "FRCP Rule 12(a)(1)(A)(i)",
        "extensions": "60 days if waiver of service (Rule 12(a)(1)(A)(ii))",
    },
    "response_to_motion": {
        "days": 14,
        "from": "service of motion",
        "rule": "FRCP Rule 6(c)(1); E.D.Pa. Local Rule 7.1(c)",
    },
    # PA State
    "notice_of_appeal_pa": {
        "days": 30,
        "from": "entry of order",
        "rule": "Pa.R.A.P. 903(a)",
        "extensions": "30 days nunc pro tunc for breakdown in court operations",
    },
    "rule_1925_statement": {
        "days": 21,
        "from": "entry of order directing statement",
        "rule": "Pa.R.A.P. 1925(b)",
        "note": "Failure to file = waiver of all issues on appeal",
    },
    "petition_for_allowance_pa_sup": {
        "days": 30,
        "from": "entry of Superior Court order",
        "rule": "Pa.R.A.P. 1113(a)",
    },
    "reconsideration_pa": {
        "days": 30,
        "from": "entry of order",
        "rule": "Pa.R.A.P. 1701(b)(3); 42 Pa.C.S. § 5505",
        "note": "Tolls appeal deadline",
    },
    "preliminary_objections_pa": {
        "days": 20,
        "from": "service of pleading",
        "rule": "Pa.R.C.P. 1028(c)(1)",
    },
}



# Deadline exceptions and overrides — NO time limit
DEADLINE_EXCEPTIONS = {
    "fraud_on_court": {
        "rule": "FRCP Rule 60(d)(3)",
        "time_limit": None,
        "description": "Independent action for fraud on the court. No time limit. Court has inherent power to vacate judgment obtained by fraud.",
        "requirements": [
            "Intentional fraud, misrepresentation, or misconduct by officer of the court",
            "Fraud was directed at the court itself, not just the opposing party",
            "The fraud prevented a fair adjudication",
        ],
        "key_cases": [
            "Hazel-Atlas Glass Co. v. Hartford-Empire Co., 322 U.S. 238 (1944)",
            "Chambers v. NASCO, Inc., 501 U.S. 32 (1991)",
        ],
    },
    "void_judgment": {
        "rule": "FRCP Rule 60(b)(4)",
        "time_limit": None,
        "description": "A void judgment is a legal nullity and can be attacked at any time, in any court, directly or collaterally.",
        "requirements": [
            "Court lacked subject matter jurisdiction",
            "Court lacked personal jurisdiction",
            "Order violated due process (no notice or opportunity to be heard)",
            "Court acted in a manner inconsistent with due process of law",
        ],
        "key_cases": [
            "Pennoyer v. Neff, 95 U.S. 714 (1878)",
            "Mullane v. Central Hanover Bank, 339 U.S. 306 (1950)",
            "United Student Aid Funds v. Espinosa, 559 U.S. 260 (2010)",
        ],
    },
    "due_process_violation": {
        "rule": "U.S. Const. amend. V, XIV; FRCP Rule 60(b)(4)",
        "time_limit": None,
        "description": "When a party was denied constitutionally required notice and opportunity to be heard, the resulting order is void.",
        "requirements": [
            "Party was not given adequate notice of proceedings",
            "Party was denied opportunity to be heard",
            "Party was denied right to present evidence or arguments",
        ],
    },
    "equitable_tolling": {
        "rule": "Holland v. Florida, 560 U.S. 631 (2010)",
        "time_limit": "tolled during extraordinary circumstances",
        "description": "Deadlines are tolled when party was prevented from filing by extraordinary circumstances beyond their control.",
        "requirements": [
            "Party pursued rights diligently",
            "Extraordinary circumstances prevented timely filing",
            "Party was not at fault for the delay",
        ],
        "examples": [
            "Court clerk gave wrong deadline information",
            "Attorney abandonment",
            "Medical emergency preventing filing",
            "Court office closed or inaccessible",
            "Opponent concealed critical information",
        ],
    },
    "nunc_pro_tunc_pa": {
        "rule": "42 Pa.C.S. § 5502; Pa.R.A.P. 105(b)",
        "time_limit": "granted for cause shown",
        "description": "PA courts can permit late appeals nunc pro tunc when delay was caused by extraordinary circumstances.",
        "requirements": [
            "Fraud or breakdown in court operations",
            "Non-negligent circumstances beyond party's control",
            "Filed promptly after discovering the ground for appeal",
            "Unique and compelling circumstances",
        ],
        "key_cases": [
            "Cook v. Unemployment Compensation Board of Review, 543 Pa. 381 (1996)",
            "Bass v. Commonwealth, 485 Pa. 256 (1979)",
        ],
    },
    "access_to_courts": {
        "rule": "U.S. Const. amend. I, XIV; Bounds v. Smith, 430 U.S. 817 (1977)",
        "time_limit": None,
        "description": "Fundamental right of access to courts. Government cannot erect barriers that effectively deny access to justice.",
        "key_cases": [
            "Bounds v. Smith, 430 U.S. 817 (1977)",
            "Christopher v. Harbury, 536 U.S. 403 (2002)",
        ],
    },
    "rule_60b4_6_reasonable_time": {
        "rule": "FRCP Rule 60(b)(4)-(6)",
        "time_limit": "reasonable time (no fixed limit)",
        "description": "Motions under Rule 60(b)(4) (void), (5) (satisfied/released), and (6) (other reason) have no fixed deadline — only must be filed within a reasonable time.",
    },
}


# BEEZLE_FN_UNUSED 2026-06-13: check_deadline_exceptions (no static refs in pipeline/)
def check_deadline_exceptions(case_data: dict) -> list[dict]:
    """Check if any deadline exceptions apply to the case."""
    applicable = []
    violations = case_data.get("violations", [])
    facts = case_data.get("facts", [])
    all_text = " ".join(str(v) for v in violations) + " " + " ".join(str(f) for f in facts)
    all_lower = all_text.lower()

    # Check each exception
    if any(k in all_lower for k in ["fraud", "misrepresent", "perjur", "fabricat", "conceal"]):
        applicable.append({"exception": "fraud_on_court", **DEADLINE_EXCEPTIONS["fraud_on_court"]})

    if any(k in all_lower for k in ["void", "jurisdiction", "no authority", "without jurisdiction"]):
        applicable.append({"exception": "void_judgment", **DEADLINE_EXCEPTIONS["void_judgment"]})

    if any(k in all_lower for k in ["no notice", "denied hearing", "ex parte", "without notice", "opportunity to be heard"]):
        applicable.append({"exception": "due_process_violation", **DEADLINE_EXCEPTIONS["due_process_violation"]})

    if any(k in all_lower for k in ["prevented", "unable to file", "extraordinary", "clerk error", "medical"]):
        applicable.append({"exception": "equitable_tolling", **DEADLINE_EXCEPTIONS["equitable_tolling"]})

    if any(k in all_lower for k in ["nunc pro tunc", "breakdown", "court operations"]):
        applicable.append({"exception": "nunc_pro_tunc_pa", **DEADLINE_EXCEPTIONS["nunc_pro_tunc_pa"]})

    return applicable


def calculate_deadline(
    event_type: str,
    event_date: str,
    court: str = "EDPA",
) -> dict:
    """Calculate a filing deadline.

    Args:
        event_type: type from FILING_DEADLINES keys
        event_date: date string (YYYY-MM-DD)
        court: court code for court-specific rules
    """
    info = FILING_DEADLINES.get(event_type)
    if not info:
        return {"error": f"Unknown event type: {event_type}"}

    try:
        start = datetime.strptime(event_date, "%Y-%m-%d")
    except ValueError:
        return {"error": f"Invalid date format: {event_date} (use YYYY-MM-DD)"}

    deadline = start + timedelta(days=info["days"])

    # Skip weekends for court deadlines
    if deadline.weekday() == 5:  # Saturday
        deadline += timedelta(days=2)
    elif deadline.weekday() == 6:  # Sunday
        deadline += timedelta(days=1)

    days_remaining = (deadline - datetime.now()).days
    if days_remaining < 0:
        status = "EXPIRED"
    elif days_remaining <= 7:
        status = "URGENT"
    elif days_remaining <= 14:
        status = "WARNING"
    else:
        status = "OK"

    return {
        "event_type": event_type,
        "event_date": event_date,
        "deadline": deadline.strftime("%Y-%m-%d"),
        "days_remaining": days_remaining,
        "status": status,
        "rule": info["rule"],
        "days_allowed": info["days"],
        "from": info["from"],
        "extensions": info.get("extensions", ""),
        "note": info.get("note", ""),
    }


def calculate_all_deadlines(
    judgment_date: str,
    court: str = "EDPA",
) -> list[dict]:
    """Calculate ALL possible deadlines from a judgment date."""
    results = []

    # Determine which deadlines apply based on court
    if court in ("EDPA", "CA3", "SCOTUS"):
        applicable = [
            "motion_reconsideration_federal",
            "notice_of_appeal_federal",
            "rule_60b_motion",
            "cert_petition_scotus",
        ]
    elif court in ("PA_CP_PHILA", "PA_CP_OTHER"):
        applicable = [
            "reconsideration_pa",
            "notice_of_appeal_pa",
            "rule_1925_statement",
            "petition_for_allowance_pa_sup",
        ]
    elif court in ("PA_SUPER", "PA_CMWLTH"):
        applicable = [
            "reconsideration_pa",
            "petition_for_allowance_pa_sup",
        ]
    else:
        applicable = list(FILING_DEADLINES.keys())

    for event_type in applicable:
        r = calculate_deadline(event_type, judgment_date, court)
        if not r.get("error"):
            results.append(r)

    # Sort by deadline
    results.sort(key=lambda x: x.get("deadline", "9999"))
    return results


def format_deadline_report(deadlines: list[dict]) -> str:
    """Format deadlines as a readable report."""
    lines = []
    lines.append("# FILING DEADLINE CALCULATOR")
    lines.append("")

    for d in deadlines:
        status_icon = {"EXPIRED": "❌", "URGENT": "🔴", "WARNING": "🟡", "OK": "🟢"}.get(d["status"], "")
        lines.append(f"## {status_icon} {d['event_type'].replace('_', ' ').title()}")
        lines.append(f"  **Deadline:** {d['deadline']} ({d['days_remaining']} days remaining)")
        lines.append(f"  **Rule:** {d['rule']}")
        lines.append(f"  **From:** {d['from']}")
        if d.get("extensions"):
            lines.append(f"  **Extensions:** {d['extensions']}")
        if d.get("note"):
            lines.append(f"  **Note:** {d['note']}")
        lines.append("")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
# PART 3: SCORECARD INTEGRATION
# ═══════════════════════════════════════════════════════════

# BEEZLE_FN_UNUSED 2026-06-13: enhance_scorecard_with_pins (no static refs in pipeline/)
def enhance_scorecard_with_pins(scorecard_data: dict, violations: list[ViolationPin]) -> dict:
    """Add violation pins to existing scorecard data."""
    pin_section = []
    for v in violations:
        pin_section.append(v.to_dict())

    scorecard_data["violation_pins"] = pin_section
    scorecard_data["pin_count"] = len(pin_section)
    scorecard_data["fatal_pins"] = len([v for v in violations if v.severity == "FATAL"])
    scorecard_data["serious_pins"] = len([v for v in violations if v.severity == "SERIOUS"])

    return scorecard_data


def format_pinpointed_violations(violations: list[ViolationPin]) -> str:
    """Format all violations as a detailed report."""
    if not violations:
        return "No violations detected."

    lines = []
    lines.append("# VIOLATION PINPOINTER REPORT")
    lines.append(f"Total violations: {len(violations)}")
    lines.append(f"Fatal: {len([v for v in violations if v.severity == 'FATAL'])}")
    lines.append(f"Serious: {len([v for v in violations if v.severity == 'SERIOUS'])}")
    lines.append("")

    for i, v in enumerate(violations, 1):
        lines.append(f"---")
        lines.append(f"### Violation {i}: {v.violation_type}")
        lines.append(f"**Severity:** {v.severity}")
        lines.append(f"**Rule:** {v.rule_citation}")
        lines.append(f"**Location:** {v.format_citation()}")
        if v.header:
            lines.append(f"**Section:** {v.header}")
        if v.quote:
            lines.append(f'**Quote:** "{v.quote}"')
        lines.append(f"**Violation:** {v.explanation}")
        lines.append(f"**Required:** {v.fix}")
        lines.append("")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    args = sys.argv[1:]

    if not args or args[0] == "help":
        print("Usage:")
        print("  python -m pipeline.block14_pinpointer scan FILE       # scan a filing for violations")
        print("  python -m pipeline.block14_pinpointer deadline TYPE DATE  # calculate deadline")
        print("  python -m pipeline.block14_pinpointer deadlines DATE [COURT]  # all deadlines from date")
        print("  python -m pipeline.block14_pinpointer types          # list deadline types")
        sys.exit(0)

    if args[0] == "scan":
        filepath = Path(args[1])
        text = filepath.read_text(encoding="utf-8", errors="replace")
        source = args[2] if len(args) > 2 else filepath.stem
        violations = scan_document_for_violations(text, source)
        print(format_pinpointed_violations(violations))

    elif args[0] == "deadline":
        event_type = args[1]
        event_date = args[2]
        court = args[3] if len(args) > 3 else "EDPA"
        r = calculate_deadline(event_type, event_date, court)
        print(json.dumps(r, indent=2))

    elif args[0] == "deadlines":
        date = args[1]
        court = args[2] if len(args) > 2 else "EDPA"
        results = calculate_all_deadlines(date, court)
        print(format_deadline_report(results))

    elif args[0] == "types":
        for k, v in FILING_DEADLINES.items():
            print(f"  {k}: {v['days']} days — {v['rule']}")


# === BEEZLE_BLOCK14_BACKFILL_V1 ===
# Runtime backfill of rule_text / explanation / fix for patterns that omit
# them. Uses setdefault() so existing keys are never overwritten.
try:
    _BEEZLE_BACKFILL = {
        'BILL_OF_ATTAINDER_MISLABEL': {
            'rule_text': 'U.S. Const. art. I, \u00a7\u00a7 9, 10. '
                         'United States v. Brown, 381 U.S. 437 (1965); '
                         'Nixon v. Administrator of Gen. Servs., 433 U.S. 425 (1977).',
            'explanation': (
                'Court has affixed a punitive label \u2014 vexatious, frivolous, '
                'sovereign citizen \u2014 that functions as judicial punishment '
                'without trial, restricting court access without notice and an '
                'opportunity to be heard. The label, applied by judicial fiat '
                'rather than after Oliver findings, implicates the prohibition '
                'on bills of attainder.'
            ),
            'fix': (
                'Object on the record. Move to strike the mislabel. Demand '
                'the four In re Oliver findings (frequency, content, motive, '
                'effect) on the record before any restriction. Preserve for '
                'appeal as procedural and substantive due-process error.'
            ),
        },
        'IMMUNITY_WAIVER_TRIGGERED': {
            'rule_text': 'Forrester v. White, 484 U.S. 219 (1988); '
                         'Stump v. Sparkman, 435 U.S. 349 (1978); '
                         'Pulliam v. Allen, 466 U.S. 522 (1984).',
            'explanation': (
                'Conduct alleged falls outside the scope of acts taken in a '
                'judicial capacity, or constitutes clear absence of all '
                'jurisdiction, removing the qualified protection of judicial '
                'immunity. Administrative acts and acts in clear absence of '
                'jurisdiction do not enjoy immunity.'
            ),
            'fix': (
                'Plead the specific non-judicial or jurisdictionally-absent '
                'acts with particularity. Cite Forrester (administrative '
                'acts) or the Stump clear-absence-of-jurisdiction exception. '
                'Seek injunctive or declaratory relief under Pulliam where '
                'damages immunity attaches.'
            ),
        },
        'VOID_JUDGMENT_RULE_60B4': {
            'rule_text': 'Fed. R. Civ. P. 60(b)(4); U.S. Const. amend. V '
                         '(due process); United Student Aid Funds v. '
                         'Espinosa, 559 U.S. 260 (2010).',
            'explanation': (
                'Judgment is void where the court lacked subject-matter '
                'jurisdiction, lacked personal jurisdiction, or acted in a '
                'manner inconsistent with due process. Rule 60(b)(4) relief '
                'is mandatory, not discretionary, where the judgment is in '
                'fact void.'
            ),
            'fix': (
                'Move under Fed. R. Civ. P. 60(b)(4) for relief from void '
                'judgment. No time limit applies. Cite Espinosa for the '
                'narrow but mandatory standard. Identify the specific '
                'jurisdictional or due-process defect that voids the '
                'judgment.'
            ),
        },
        'SUBJECT_MATTER_JURISDICTION_DEFECT': {
            'rule_text': 'Fed. R. Civ. P. 12(h)(3); 28 U.S.C. \u00a7\u00a7 1331, '
                         '1332, 1441, 1447(c); Kokkonen v. Guardian Life '
                         'Ins., 511 U.S. 375 (1994); Arbaugh v. Y&H Corp., '
                         '546 U.S. 500 (2006).',
            'explanation': (
                'Federal courts are courts of limited jurisdiction; the '
                'party invoking federal jurisdiction bears the burden of '
                'establishing it. Defects in subject-matter jurisdiction '
                'may be raised at any time, by any party, or by the court '
                'sua sponte, and cannot be waived.'
            ),
            'fix': (
                'Identify the specific jurisdictional defect (no federal '
                'question, non-diverse parties, amount in controversy below '
                'threshold, etc.). Move to dismiss under Rule 12(b)(1) or '
                'to remand under 28 U.S.C. \u00a7 1447(c). For removed '
                'actions, request attorney\u2019s fees where removal had no '
                'objectively reasonable basis.'
            ),
        },
        'STANDING_DEFECT': {
            'rule_text': 'Lujan v. Defenders of Wildlife, 504 U.S. 555 '
                         '(1992); Spokeo, Inc. v. Robins, 578 U.S. 330 '
                         '(2016); TransUnion LLC v. Ramirez, 594 U.S. 413 '
                         '(2021).',
            'explanation': (
                'Article III standing requires (1) a concrete and '
                'particularized injury-in-fact, (2) a causal connection to '
                'the challenged conduct, and (3) likelihood that a '
                'favorable decision will redress the injury. Standing must '
                'be established for each form of relief sought and at every '
                'stage of the litigation.'
            ),
            'fix': (
                'Plead each Lujan element with specificity. For statutory '
                'violations, identify the concrete harm beyond the bare '
                'procedural violation. Cite TransUnion for the concrete-'
                'harm requirement. Address standing element-by-element on '
                'the record before any merits ruling.'
            ),
        },
        'DEPRIVATION_OF_RIGHTS_COLOR_OF_LAW': {
            'rule_text': '18 U.S.C. \u00a7 242; 42 U.S.C. \u00a7 1983; Monroe v. '
                         'Pape, 365 U.S. 167 (1961); West v. Atkins, 487 '
                         'U.S. 42 (1988).',
            'explanation': (
                'A state actor, acting under apparent authority of office, '
                'has deprived a person of a constitutional or federal '
                'statutory right. Civil remedy lies under 42 U.S.C. \u00a7 '
                '1983; criminal liability under 18 U.S.C. \u00a7 242 '
                'requires willful deprivation.'
            ),
            'fix': (
                'Plead with specificity (a) the right deprived, (b) the '
                'state-actor status of the defendant, and (c) the action '
                'taken under color of law. For criminal referral, document '
                'willfulness and submit to U.S. Attorney\u2019s Office or '
                'DOJ Civil Rights Division.'
            ),
        },
        'OBSTRUCTION_OF_JUSTICE': {
            'rule_text': '18 U.S.C. \u00a7\u00a7 1503, 1505, 1512, 1519; '
                         'Marinello v. United States, 584 U.S. 1 (2018); '
                         'Arthur Andersen LLP v. United States, 544 U.S. '
                         '696 (2005).',
            'explanation': (
                'Conduct corruptly endeavors to influence, obstruct, or '
                'impede the due administration of justice in a pending or '
                'reasonably foreseeable judicial proceeding. Includes '
                'witness tampering, document destruction, false statements '
                'to a tribunal, and concealment of material evidence.'
            ),
            'fix': (
                'Document the obstructive act with particularity (who, '
                'what, when, where, how, supporting record evidence). '
                'Preserve all relevant materials. Refer to U.S. '
                'Attorney\u2019s Office or appropriate disciplinary '
                'authority for investigation.'
            ),
        },
        'STARE_DECISIS_VIOLATION': {
            'rule_text': 'Hutto v. Davis, 454 U.S. 370 (1982) (per '
                         'curiam); Rodriguez de Quijas v. Shearson/American '
                         'Express, 490 U.S. 477, 484 (1989); Agostini v. '
                         'Felton, 521 U.S. 203 (1997).',
            'explanation': (
                'Lower court has reached a result inconsistent with '
                'controlling precedent of a binding higher court. Lower '
                'courts are bound to follow Supreme Court precedent '
                'directly on point until the Supreme Court itself '
                'overrules it.'
            ),
            'fix': (
                'Identify the binding precedent and the inconsistent '
                'ruling. Cite Hutto v. Davis. Frame on motion or appeal '
                'as reversible error of law. Preserve the issue with a '
                'clear on-record objection.'
            ),
        },
        'PRIMA_FACIE_EVIDENCE_IGNORED': {
            'rule_text': 'Fed. R. Evid. 201(b)(2), 201(c)(2); Anderson v. '
                         'Liberty Lobby, Inc., 477 U.S. 242 (1986); '
                         'Adickes v. S.H. Kress & Co., 398 U.S. 144 '
                         '(1970).',
            'explanation': (
                'Items of record subject to mandatory judicial notice \u2014 '
                'admissions, defaults, court orders, agency findings, '
                'statements against interest, unrebutted affidavits \u2014 '
                'have been disregarded without explanation. Rule 201(c)(2) '
                'makes notice mandatory upon party request supported by '
                'adequate showing.'
            ),
            'fix': (
                'File a formal request for judicial notice identifying '
                'each item, its source, and its evidentiary significance. '
                'Cite FRE 201(b)(2) and (c)(2). Where the items establish '
                'elements of a claim or defense, move for summary '
                'disposition.'
            ),
        },
        'JUDICIAL_DISQUALIFICATION_DUTY': {
            'rule_text': '28 U.S.C. \u00a7 455; Liljeberg v. Health Servs. '
                         'Acquisition Corp., 486 U.S. 847 (1988); Caperton '
                         'v. A.T. Massey Coal Co., 556 U.S. 868 (2009).',
            'explanation': (
                'Judge must disqualify in any proceeding in which '
                'impartiality might reasonably be questioned, or where '
                'one of the enumerated grounds applies (personal bias, '
                'financial interest, prior involvement, family '
                'relationship, etc.). The duty is self-executing and '
                'does not depend on a party motion.'
            ),
            'fix': (
                'File motion to disqualify under 28 U.S.C. \u00a7 455 with '
                'specific factual allegations supporting reasonable '
                'question of impartiality. Preserve the issue for '
                'appellate review under Liljeberg.'
            ),
        },
        'PROCEDURAL_DUE_PROCESS_VIOLATION': {
            'rule_text': 'U.S. Const. amends. V, XIV; Mullane v. Central '
                         'Hanover Bank & Trust Co., 339 U.S. 306 (1950); '
                         'Mathews v. Eldridge, 424 U.S. 319 (1976); '
                         'Armstrong v. Manzo, 380 U.S. 545 (1965).',
            'explanation': (
                'Deprivation of life, liberty, or property has occurred '
                'without adequate notice or a meaningful opportunity to '
                'be heard. The Mathews three-factor balancing test '
                '(private interest, risk of erroneous deprivation, '
                'government interest) governs the level of process due.'
            ),
            'fix': (
                'Identify (a) the protected interest, (b) the deprivation, '
                'and (c) the procedural inadequacy (insufficient notice, '
                'absent hearing, foreclosed opportunity). Move for '
                'additional process under Mathews. Preserve for appeal.'
            ),
        },
        'JUDICIAL_CANON_VIOLATION': {
            'rule_text': 'Code of Conduct for U.S. Judges, Canons 1\u20135; '
                         '28 U.S.C. \u00a7 351 (Judicial Conduct and '
                         'Disability Act); Rules for Judicial-Conduct and '
                         'Judicial-Disability Proceedings.',
            'explanation': (
                'Conduct departs from the canons governing federal '
                'judicial behavior \u2014 competence, faithfulness to law, '
                'propriety, impartiality, or financial probity. Each '
                'canon establishes a distinct duty enforceable through '
                'the Judicial Council complaint process.'
            ),
            'fix': (
                'File a complaint of judicial misconduct under 28 U.S.C. '
                '\u00a7 351 with the relevant Circuit Judicial Council. '
                'Document the specific canon implicated, the conduct at '
                'issue, and supporting record evidence.'
            ),
        },
        'LAWYER_RULE_VIOLATION': {
            'rule_text': 'Pa. R.P.C. 3.1 (Meritorious Claims and '
                         'Contentions); 3.3 (Candor Toward the Tribunal); '
                         '3.4 (Fairness to Opposing Party and Counsel); '
                         '8.4 (Misconduct). ABA Model Rules of '
                         'Professional Conduct corresponding provisions.',
            'explanation': (
                'Counsel has filed material lacking colorable legal or '
                'factual basis, has made false statements to the '
                'tribunal, has obstructed an opposing party\u2019s access to '
                'evidence, or has engaged in conduct involving dishonesty, '
                'fraud, deceit, or misrepresentation in connection with '
                'this matter.'
            ),
            'fix': (
                'File a complaint with the Disciplinary Board of the '
                'Supreme Court of Pennsylvania (or the equivalent state '
                'bar disciplinary body). Cite the specific rule violated '
                'and the supporting record citations. Preserve all '
                'documentary evidence.'
            ),
        },
        'REMOVAL_DEFECTIVE': {
            'rule_text': '28 U.S.C. \u00a7\u00a7 1441, 1446, 1447(c); Murphy '
                         'Bros. v. Michetti Pipe & Foundry Co., 526 U.S. '
                         '344 (1999); Shamrock Oil & Gas Corp. v. '
                         'Sheets, 313 U.S. 100 (1941); Caterpillar Inc. '
                         'v. Lewis, 519 U.S. 61 (1996).',
            'explanation': (
                'Removal has failed to satisfy a statutory prerequisite '
                '\u2014 typically the unanimity-of-defendants requirement '
                'under \u00a7 1446(b)(2)(A), the 30-day deadline from formal '
                'service, or the in-state-defendant bar in diversity '
                'actions. Removal statutes are strictly construed and '
                'doubts are resolved against removal.'
            ),
            'fix': (
                'Move to remand under 28 U.S.C. \u00a7 1447(c) identifying '
                'the specific procedural defect. Request attorney\u2019s '
                'fees under \u00a7 1447(c) where removal lacked an '
                'objectively reasonable basis. Cite Murphy Bros. for '
                'the service-trigger rule and Shamrock for the strict-'
                'construction canon.'
            ),
        },
        'FRAUD_ON_THE_COURT': {
            'rule_text': 'Fed. R. Civ. P. 60(d)(3); Hazel-Atlas Glass Co. '
                         'v. Hartford-Empire Co., 322 U.S. 238 (1944); '
                         'Universal Oil Products Co. v. Root Refining '
                         'Co., 328 U.S. 575 (1946); Chambers v. NASCO, '
                         'Inc., 501 U.S. 32 (1991).',
            'explanation': (
                'Conduct by an officer of the court \u2014 counsel, juror, '
                'judge, or court personnel \u2014 has defiled the integrity '
                'of the judicial process. Includes fabricated evidence, '
                'perjured testimony procured or knowingly used by '
                'counsel, undisclosed conflicts, or systematic deception '
                'of the court bearing on a central issue.'
            ),
            'fix': (
                'Move under Fed. R. Civ. P. 60(d)(3) to set aside any '
                'judgment so obtained. No time limit applies. Cite '
                'Hazel-Atlas. Request referral for disciplinary action '
                'under Pa. R.P.C. 8.4 and, where warranted, criminal '
                'investigation under 18 U.S.C. \u00a7\u00a7 1503 or 1623.'
            ),
        },
        'SANCTIONS_TRIGGERED': {
            'rule_text': 'Fed. R. Civ. P. 11 (Signing of Pleadings; '
                         'Sanctions); 28 U.S.C. \u00a7 1927 (Counsel\u2019s '
                         'Liability for Excessive Costs); Chambers v. '
                         'NASCO, Inc., 501 U.S. 32 (1991); Goodyear Tire '
                         '& Rubber Co. v. Haeger, 581 U.S. 101 (2017).',
            'explanation': (
                'Conduct meets the standard for sanctions: Rule 11 '
                '(frivolous pleading or improper purpose), \u00a7 1927 '
                '(unreasonable and vexatious multiplication of '
                'proceedings), or inherent power (bad-faith conduct). '
                'Each track has distinct procedural requirements that '
                'must be satisfied.'
            ),
            'fix': (
                'Identify (a) the conduct, (b) the applicable sanctions '
                'track, and (c) the procedural prerequisites: Rule 11 '
                '21-day safe harbor for party-initiated motions; \u00a7 '
                '1927 bad-faith finding; inherent-power compensatory-'
                'harm causal linkage under Goodyear. File with '
                'specificity and request a reasoned ruling on the '
                'record.'
            ),
        },
        'SUA_SPONTE_RULING': {
            'rule_text': 'Day v. McDonough, 547 U.S. 198 (2006); '
                         'Fed. R. Civ. P. 12(h)(3) (sua sponte dismissal '
                         'for lack of subject-matter jurisdiction); '
                         'Bank of America v. United States, 23 F.3d '
                         '380 (Fed. Cir. 1994).',
            'explanation': (
                'Court has issued a substantive ruling on an issue not '
                'raised by any party, without affording the parties '
                'notice and opportunity to be heard. Sua sponte action '
                'is appropriate for jurisdictional defects but generally '
                'requires fair process for substantive issues.'
            ),
            'fix': (
                'Object on the record promptly. Move for reconsideration '
                'with full briefing of the issue. Cite Day v. McDonough '
                'for the notice-and-opportunity requirement. Preserve '
                'for appeal as procedural error where merits-based sua '
                'sponte ruling lacked notice and hearing.'
            ),
        },
        'SOVEREIGN_CITIZEN_LABEL': {
            'rule_text': 'U.S. Const. amend. V (due process); Caperton v. '
                         'A.T. Massey Coal Co., 556 U.S. 868 (2009); In '
                         're Oliver, 333 U.S. 257 (1948); 28 U.S.C. \u00a7 '
                         '455.',
            'explanation': (
                'Court has affixed the "sovereign citizen" label or an '
                'analogous ideological designation to a pro se litigant '
                'without evidentiary basis. The term has a precise '
                'referent \u2014 adherents of a specific anti-government '
                'movement \u2014 and misapplication functions as a '
                'discriminatory pre-filing exclusion violating due '
                'process and judicial-impartiality norms.'
            ),
            'fix': (
                'Object on the record. Demand identification of the '
                'specific filings, statements, or conduct alleged to '
                'demonstrate sovereign-citizen ideology. Move to strike '
                'the label and any rulings predicated on it. Seek '
                'disqualification under 28 U.S.C. \u00a7 455 where the '
                'label was applied without evidentiary support.'
            ),
        },
    }

    for _vt, _fills in _BEEZLE_BACKFILL.items():
        _entry = RULE_VIOLATION_PATTERNS.get(_vt)
        if isinstance(_entry, dict):
            for _k, _v in _fills.items():
                _entry.setdefault(_k, _v)
    del _BEEZLE_BACKFILL
except Exception as _e:
    import logging as _logging
    _logging.getLogger('beezle.block14').warning(
        f'BEEZLE_BLOCK14_BACKFILL_V1 supplement failed: {_e}'
    )

