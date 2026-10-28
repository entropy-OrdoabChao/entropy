"""
judicial_order_scanner.py — Block 15: Stance-Aware Judicial Order Analyzer
==========================================================================

Scans judge orders for constitutional violations and frames EVERY finding
from YOUR position as plaintiff:
  "Judge violated X → YOU file Y → citing Z"

Violation categories:
  1. First Amendment prior restraint ("do not file", filing injunctions)
  2. Due process (ex parte rulings, no hearing, no notice)
  3. Access to courts (Bounds v. Smith, Christopher v. Harbury)
  4. Judicial bias (Canon 2.2, 28 U.S.C. § 455)
  5. Improper sanctions without Rule 11 safe harbor
  6. Sua sponte dismissal without leave to amend
  7. Filing injunction without Safir/Chipps factors
  8. Violation of right to be heard
  9. Improper vexatious litigant designation
  10. Failure to state reasons (FRCP 52/58)

Integrates with:
  - order_engine.py (void/voidable doctrines)
  - block14_pinpointer.py (violation pinpointing)
  - baseline_loop.py (engine slot 13: judicial_order_scanner)
  - ChromaDB (case law enrichment for filing injunctions, prior restraint, mandamus)

Logs to: <home>\\logs\\engines\\judicial_order_scanner.log
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

# ─── Paths ───
BASE_PATH = _eroot()
PIPELINE_DIR = BASE_PATH / "pipeline"
LOGS_DIR = BASE_PATH / "logs" / "engines"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# ─── Logging ───
log = logging.getLogger("judicial_order_scanner")
if not log.handlers:
    log.setLevel(logging.INFO)
    fh = logging.FileHandler(LOGS_DIR / "judicial_order_scanner.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"))
    log.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("[%(name)s] %(message)s"))
    log.addHandler(sh)


# ════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ════════════════════════════════════════════════════════════════════

@dataclass
class StanceAction:
    """What YOU (plaintiff) file in response to this violation."""
    motion_title: str               # e.g. "Emergency Motion for Recusal"
    rule_basis: str                  # e.g. "28 U.S.C. § 455(a)"
    filing_vehicle: str             # e.g. "Motion", "Petition for Writ of Mandamus"
    forum: str                      # e.g. "same court", "court of appeals", "SCOTUS"
    timing: str                     # e.g. "Immediately", "Within 14 days"
    key_authorities: list[str] = field(default_factory=list)
    argument_template: str = ""     # one-paragraph argument skeleton
    escalation: str = ""            # what to do if the motion is denied

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class JudicialViolation:
    """A single detected constitutional/procedural violation in a judge's order."""
    violation_id: str               # e.g. "PRIOR_RESTRAINT_FILING_BAN"
    category: str                   # e.g. "First Amendment", "Due Process"
    severity: str                   # CONSTITUTIONAL / FATAL / SERIOUS / STANDARD
    display_name: str
    constitutional_basis: str       # e.g. "U.S. Const. amend. I"
    rule_citation: str              # e.g. "28 U.S.C. § 1651(a)"
    description: str                # what the judge did wrong
    quote: str                      # offending language from the order
    source_doc: str                 # e.g. "Order dated 2025-01-15"
    page: Optional[int] = None
    line_start: Optional[int] = None
    line_end: Optional[int] = None
    stance_actions: list[StanceAction] = field(default_factory=list)
    db_enrichment: dict = field(default_factory=dict)  # ChromaDB results

    def to_dict(self) -> dict:
        d = asdict(self)
        d["stance_actions"] = [a.to_dict() if hasattr(a, "to_dict") else a for a in self.stance_actions]
        return d

    def format_full(self) -> str:
        """Format as a stance-aware violation report."""
        lines = []
        sev_icon = {"CONSTITUTIONAL": "🔴", "FATAL": "🟠", "SERIOUS": "🟡", "STANDARD": "⚪"}.get(self.severity, "")
        lines.append(f"## {sev_icon} {self.display_name} [{self.severity}]")
        lines.append(f"**Category:** {self.category}")
        lines.append(f"**Constitutional basis:** {self.constitutional_basis}")
        lines.append(f"**Rule:** {self.rule_citation}")
        lines.append(f"**Source:** {self.source_doc}" +
                      (f", p. {self.page}" if self.page else "") +
                      (f", ll. {self.line_start}-{self.line_end}" if self.line_start else ""))
        if self.quote:
            lines.append(f'**Offending language:** "{self.quote}"')
        lines.append(f"**Violation:** {self.description}")
        lines.append("")
        for i, action in enumerate(self.stance_actions, 1):
            lines.append(f"### → YOUR RESPONSE #{i}: {action.motion_title}")
            lines.append(f"  **File:** {action.filing_vehicle}")
            lines.append(f"  **Under:** {action.rule_basis}")
            lines.append(f"  **Forum:** {action.forum}")
            lines.append(f"  **Timing:** {action.timing}")
            if action.key_authorities:
                lines.append(f"  **Citing:** {'; '.join(action.key_authorities)}")
            if action.argument_template:
                lines.append(f"  **Argument:** {action.argument_template}")
            if action.escalation:
                lines.append(f"  **If denied:** {action.escalation}")
        lines.append("")
        return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════
# VIOLATION DEFINITIONS — each with stance-aware response actions
# ════════════════════════════════════════════════════════════════════

JUDICIAL_VIOLATIONS = {

    # ─── 1. FIRST AMENDMENT: PRIOR RESTRAINT / FILING BANS ───
    "PRIOR_RESTRAINT_FILING_BAN": {
        "category": "First Amendment",
        "severity": "CONSTITUTIONAL",
        "display_name": "Prior Restraint — Filing Injunction Without Constitutional Safeguards",
        "constitutional_basis": "U.S. Const. amend. I; U.S. Const. amend. XIV § 1",
        "rule_citation": "28 U.S.C. § 1651(a); In re Safir, 2021 WL 5906089 (3d Cir.)",
        "description": "Court imposed a filing injunction or ordered plaintiff not to file without applying required constitutional factors (Safir/Chipps test in Third Circuit)",
        "triggers": [
            r"(?:do\s+not|shall\s+not|may\s+not|is\s+(?:hereby\s+)?(?:prohibited|barred|enjoined)\s+from)\s+fil(?:e|ing)",
            r"(?:filing\s+(?:injunction|restriction|ban|bar|prohibition))",
            r"(?:pre[-\s]?filing\s+(?:injunction|review|screening|restriction))",
            r"(?:leave\s+of\s+(?:court|this\s+court)\s+(?:before|prior\s+to|to)\s+fil(?:e|ing))",
            r"(?:barred|prohibited|enjoined|restricted)\s+from\s+(?:commencing|initiating|filing|bringing)",
            r"(?:no\s+(?:further|additional|new)\s+(?:actions?|complaints?|lawsuits?|filings?|motions?))",
            r"(?:vexatious\s+litigant.*(?:injunction|order|restriction|filing))",
        ],
        "stance_actions": [
            {
                "motion_title": "Emergency Motion to Vacate Filing Injunction as Unconstitutional Prior Restraint",
                "rule_basis": "U.S. Const. amend. I; In re Safir, 2021 WL 5906089 (3d Cir. 2021)",
                "filing_vehicle": "Motion under FRCP 60(b)(4) (void order)",
                "forum": "same court",
                "timing": "Immediately — no time bar on void orders",
                "key_authorities": [
                    "In re Safir, 2021 WL 5906089 (3d Cir. 2021) — four-factor test required before imposing filing injunction",
                    "Chipps v. U.S. Dist. Court, 882 F.2d 72 (3d Cir. 1989) — injunction requires notice, opportunity to respond, adequate factual findings, narrowly tailored",
                    "In re Oliver, 682 F.2d 443 (3d Cir. 1982) — right to litigate is constitutionally protected",
                    "Alexander v. United States, 509 U.S. 544 (1993) — prior restraints bear heavy presumption against constitutional validity",
                    "Nebraska Press Ass'n v. Stuart, 427 U.S. 539 (1976) — prior restraint is the most serious and least tolerable infringement on First Amendment rights",
                ],
                "argument_template": "The Court's order constitutes an unconstitutional prior restraint on Plaintiff's First Amendment right of access to courts. Under the Third Circuit's binding precedent in Safir and Chipps, a filing injunction may only be imposed after: (1) the litigant received notice and opportunity to respond; (2) the court compiled an adequate factual record; (3) the court made substantive findings of frivolousness or harassment; and (4) the injunction is narrowly tailored. None of these safeguards were applied here. The order is void under FRCP 60(b)(4).",
                "escalation": "Petition for Writ of Mandamus to Third Circuit under 28 U.S.C. § 1651(a); cite In re Cenarrusa (mandamus appropriate to prevent unconstitutional prior restraint on filing rights)",
            },
        ],
    },

    "DO_NOT_FILE_LANGUAGE": {
        "category": "First Amendment",
        "severity": "CONSTITUTIONAL",
        "display_name": "Direct Order Prohibiting Filing — Prior Restraint",
        "constitutional_basis": "U.S. Const. amend. I; Petition Clause",
        "rule_citation": "Bill Johnson's Restaurants v. NLRB, 461 U.S. 731 (1983)",
        "description": "Court directly ordered plaintiff not to file a complaint, motion, or lawsuit — the most naked form of prior restraint on the right to petition",
        "triggers": [
            r"(?:you|plaintiff|petitioner)\s+(?:shall|must|are\s+(?:ordered|directed|instructed))\s+(?:to\s+)?(?:not|refrain\s+from)\s+fil(?:e|ing)",
            r"(?:do\s+not\s+file)",
            r"(?:ordered\s+(?:to\s+)?(?:not|refrain)\s+(?:from\s+)?fil(?:e|ing))",
            r"(?:plaintiff|petitioner)\s+is\s+(?:warned|advised|cautioned)\s+(?:not\s+to|against)\s+fil(?:e|ing)",
        ],
        "stance_actions": [
            {
                "motion_title": "Motion to Vacate Void Order — Prior Restraint on Right to Petition",
                "rule_basis": "U.S. Const. amend. I (Petition Clause); FRCP 60(b)(4)",
                "filing_vehicle": "Motion to Vacate under FRCP 60(b)(4)",
                "forum": "same court (or file in the court where you were barred from filing)",
                "timing": "Immediately — void orders have no time bar",
                "key_authorities": [
                    "Bill Johnson's Restaurants v. NLRB, 461 U.S. 731, 741 (1983) — right to petition courts is protected by the First Amendment",
                    "BE&K Construction Co. v. NLRB, 536 U.S. 516 (2002) — filing lawsuits is protected petitioning activity",
                    "California Motor Transport Co. v. Trucking Unlimited, 404 U.S. 508 (1972) — right of access to courts is an aspect of the First Amendment",
                    "Borough of Duryea v. Guarnieri, 564 U.S. 379 (2011) — Petition Clause protects the right to file lawsuits",
                ],
                "argument_template": "The Court's directive ordering Plaintiff not to file constitutes an unlawful prior restraint on Plaintiff's First Amendment right to petition the government for redress of grievances. The Supreme Court has unequivocally held that 'the right of access to the courts is an aspect of the First Amendment right to petition.' No court may order a party not to file without first applying the Safir/Chipps safeguards required in this Circuit. This order is void ab initio.",
                "escalation": "Mandamus to Court of Appeals; file complaint with judicial conduct commission; seek 42 U.S.C. § 1983 action against clerk who enforces unconstitutional order",
            },
        ],
    },

    # ─── 2. DUE PROCESS VIOLATIONS ───
    "EX_PARTE_RULING": {
        "category": "Due Process",
        "severity": "CONSTITUTIONAL",
        "display_name": "Ex Parte Ruling Without Notice or Hearing",
        "constitutional_basis": "U.S. Const. amend. V; U.S. Const. amend. XIV § 1",
        "rule_citation": "Mullane v. Central Hanover Bank, 339 U.S. 306 (1950); Mathews v. Eldridge, 424 U.S. 319 (1976)",
        "description": "Court ruled on a dispositive matter without providing notice or opportunity to be heard",
        "triggers": [
            r"(?:ex\s+parte\s+(?:order|ruling|decision|hearing|motion))",
            r"(?:without\s+(?:notice|hearing|opportunity)\s+(?:to|for)\s+(?:the\s+)?(?:plaintiff|petitioner|party|parties))",
            r"(?:no\s+(?:notice|hearing|opportunity)\s+(?:was\s+)?(?:given|provided|afforded))",
            r"(?:sua\s+sponte.*without\s+(?:notice|hearing|briefing))",
            r"(?:denied\s+(?:opportunity|right)\s+to\s+(?:be\s+heard|respond|brief|argue))",
        ],
        "stance_actions": [
            {
                "motion_title": "Motion to Vacate Void Order — Due Process Violation (No Notice/Hearing)",
                "rule_basis": "U.S. Const. amend. V, XIV; FRCP 60(b)(4)",
                "filing_vehicle": "Motion to Vacate under FRCP 60(b)(4)",
                "forum": "same court",
                "timing": "No time limit — void for lack of due process",
                "key_authorities": [
                    "Mullane v. Central Hanover Bank, 339 U.S. 306 (1950) — notice must be reasonably calculated to inform",
                    "Mathews v. Eldridge, 424 U.S. 319 (1976) — three-factor due process balancing test",
                    "Armstrong v. Manzo, 380 U.S. 545 (1965) — failure to give notice voids the judgment",
                    "Peralta v. Heights Medical Center, 485 U.S. 80 (1988) — judgment taken without notice is void and must be set aside",
                ],
                "argument_template": "The order entered on [DATE] is void because Plaintiff was denied constitutionally mandated notice and opportunity to be heard. Under Mullane, due process requires 'notice reasonably calculated, under all the circumstances, to apprise interested parties of the pendency of the action.' No such notice was provided. Under Peralta, a judgment entered without notice 'deprives a defendant of due process' and 'must be set aside.'",
                "escalation": "Appeal under 28 U.S.C. § 1291; mandamus under 28 U.S.C. § 1651(a); file separate 42 U.S.C. § 1983 action for deprivation of due process under color of law",
            },
        ],
    },

    "DENIED_OPPORTUNITY_TO_RESPOND": {
        "category": "Due Process",
        "severity": "FATAL",
        "display_name": "Denied Opportunity to Respond to Dispositive Motion",
        "constitutional_basis": "U.S. Const. amend. XIV § 1",
        "rule_citation": "FRCP 6(c)(1); E.D.Pa. Local Rule 7.1(c)",
        "description": "Court ruled on motion before response deadline expired or without allowing response",
        "triggers": [
            r"(?:before\s+(?:response|opposition|reply)\s+(?:was\s+)?(?:due|filed|received))",
            r"(?:without\s+(?:waiting|allowing)\s+(?:for\s+)?(?:a\s+)?(?:response|opposition|reply|briefing))",
            r"(?:no\s+(?:response|opposition)\s+(?:period|time|opportunity))",
            r"(?:ruled\s+(?:on|upon).*(?:before|prior\s+to).*(?:response|deadline))",
        ],
        "stance_actions": [
            {
                "motion_title": "Motion for Reconsideration — Premature Ruling Without Response Period",
                "rule_basis": "FRCP 59(e); E.D.Pa. Local Rule 7.1(c)",
                "filing_vehicle": "Motion for Reconsideration under FRCP 59(e) or Motion to Vacate under FRCP 60(b)(1)",
                "forum": "same court",
                "timing": "28 days from entry of order (FRCP 59(e))",
                "key_authorities": [
                    "Solis v. Current Development Corp., 557 F.3d 772 (7th Cir. 2009) — ruling before response deadline is abuse of discretion",
                    "FRCP 6(c)(1) — 14-day response period for motions",
                    "E.D.Pa. Local Rule 7.1(c) — response time for motions in EDPA",
                ],
                "argument_template": "The Court ruled on Defendant's motion on [DATE], before Plaintiff's response deadline of [DEADLINE DATE] had expired. This denied Plaintiff the procedural right to respond guaranteed by FRCP 6(c)(1) and Local Rule 7.1(c). Plaintiff's proposed response, attached hereto, demonstrates genuine disputes of material fact that preclude summary disposition.",
                "escalation": "Appeal to Third Circuit — abuse of discretion standard",
            },
        ],
    },

    # ─── 3. ACCESS TO COURTS ───
    "ACCESS_TO_COURTS_DENIAL": {
        "category": "Access to Courts",
        "severity": "CONSTITUTIONAL",
        "display_name": "Denial of Fundamental Right of Access to Courts",
        "constitutional_basis": "U.S. Const. amend. I, XIV; Bounds v. Smith, 430 U.S. 817 (1977)",
        "rule_citation": "Christopher v. Harbury, 536 U.S. 403 (2002)",
        "description": "Court erected barriers that effectively deny meaningful access to the judicial system",
        "triggers": [
            r"(?:denied\s+(?:access|entry)\s+(?:to\s+)?(?:the\s+)?court)",
            r"(?:stricken|struck|strike)\s+(?:from\s+(?:the\s+)?(?:docket|record|filing))",
            r"(?:refuse[sd]?\s+to\s+(?:docket|file|accept|consider))",
            r"(?:returned?\s+(?:unfiled|without\s+filing))",
            r"(?:clerk\s+(?:refused|declined|rejected))",
            r"(?:may\s+not\s+(?:access|use|appear))",
            r"(?:(?:permanent|indefinite).*(?:bar|ban|prohibition).*(?:court|filing|access))",
        ],
        "stance_actions": [
            {
                "motion_title": "Emergency Motion to Restore Access to Courts",
                "rule_basis": "U.S. Const. amend. I, XIV; 42 U.S.C. § 1983",
                "filing_vehicle": "Emergency Motion + Separate Civil Rights Action if needed",
                "forum": "same court; or file in a different court if access is blocked",
                "timing": "Immediately",
                "key_authorities": [
                    "Bounds v. Smith, 430 U.S. 817 (1977) — fundamental right of access to courts",
                    "Christopher v. Harbury, 536 U.S. 403 (2002) — access-to-courts claim elements",
                    "Lewis v. Casey, 518 U.S. 343 (1996) — actual injury requirement for access claims",
                    "Monroe v. Beard, 536 F.3d 198 (3d Cir. 2008) — actual injury shown by inability to pursue nonfrivolous claim",
                ],
                "argument_template": "The Court's order denies Plaintiff the fundamental constitutional right of access to courts guaranteed by the First and Fourteenth Amendments. Under Bounds v. Smith, the government may not erect barriers that effectively deny access to the judicial system. Plaintiff has a nonfrivolous underlying claim [DESCRIBE], satisfying the actual-injury requirement of Lewis v. Casey. The Court must vacate the order and restore Plaintiff's access.",
                "escalation": "Mandamus petition to Court of Appeals; file 42 U.S.C. § 1983 action in a different court for deprivation of access to courts under color of law",
            },
        ],
    },

    # ─── 4. JUDICIAL BIAS ───
    "JUDICIAL_BIAS_LANGUAGE": {
        "category": "Judicial Bias",
        "severity": "CONSTITUTIONAL",
        "display_name": "Judicial Bias — Disqualification Required Under 28 U.S.C. § 455",
        "constitutional_basis": "U.S. Const. amend. XIV § 1 (impartial tribunal)",
        "rule_citation": "28 U.S.C. § 455(a); Pa. Code of Judicial Conduct Canon 2.2; Liteky v. United States, 510 U.S. 540 (1994)",
        "description": "Court's language or conduct demonstrates bias, prejudice, or appearance of partiality requiring recusal",
        "triggers": [
            r"(?:you\s+(?:people|pro\s+se))",
            r"(?:(?:these|those)\s+(?:types?\s+of\s+)?(?:cases?|litigants?|plaintiffs?|filings?))",
            r"(?:waste|wasting)\s+(?:the\s+)?court(?:'s)?\s+(?:time|resources)",
            r"(?:frivolous|vexatious|harassing|abusive).*(?:without|no)\s+(?:analysis|explanation|basis|finding)",
            r"(?:i\s+(?:am\s+)?(?:tired|sick|fed\s+up)\s+(?:of|with))",
            r"(?:no\s+(?:amount|number)\s+of\s+(?:filings?|motions?|complaints?))",
            r"(?:(?:never|will\s+not)\s+(?:succeed|prevail|win))",
            r"(?:(?:warned|told|advised)\s+(?:you|plaintiff).*(?:before|previously|repeatedly))",
        ],
        "stance_actions": [
            {
                "motion_title": "Motion for Recusal Under 28 U.S.C. § 455(a)",
                "rule_basis": "28 U.S.C. § 455(a); Canon 2.2",
                "filing_vehicle": "Motion for Recusal/Disqualification",
                "forum": "same court (judge must rule first)",
                "timing": "As soon as bias becomes apparent — delay may waive",
                "key_authorities": [
                    "Liteky v. United States, 510 U.S. 540 (1994) — bias must be from extrajudicial source or demonstrate deep-seated antagonism making fair judgment impossible",
                    "In re Kensington Int'l Ltd., 368 F.3d 289 (3d Cir. 2004) — objective standard: whether a reasonable person would question impartiality",
                    "Liljeberg v. Health Services Acquisition Corp., 486 U.S. 847 (1988) — § 455 must be construed broadly to preserve public confidence",
                    "United States v. Bertoli, 40 F.3d 1384 (3d Cir. 1994) — pervasive bias warrants mandamus",
                ],
                "argument_template": "The Court's language demonstrates such deep-seated antagonism toward Plaintiff that fair judgment is impossible. Under Liteky, 'expressions of impatience, dissatisfaction, annoyance, and even anger' do not require recusal — but when the court's hostility permeates proceedings and is directed at a party's status (here, as a pro se litigant), recusal is required. A reasonable observer would question the Court's impartiality. 28 U.S.C. § 455(a) mandates recusal whenever impartiality 'might reasonably be questioned.'",
                "escalation": "Petition for Writ of Mandamus to Third Circuit citing United States v. Bertoli; file judicial misconduct complaint under 28 U.S.C. §§ 351-364 (Judicial Conduct and Disability Act)",
            },
        ],
    },

    # ─── 5. IMPROPER SANCTIONS ───
    "SANCTIONS_NO_SAFE_HARBOR": {
        "category": "Improper Sanctions",
        "severity": "FATAL",
        "display_name": "Rule 11 Sanctions Imposed Without Safe Harbor Compliance",
        "constitutional_basis": "U.S. Const. amend. V (due process in sanctions)",
        "rule_citation": "FRCP 11(c)(2); Simmerman v. Corino, 27 F.3d 58 (3d Cir. 1994)",
        "description": "Court imposed sanctions under Rule 11 without the required 21-day safe harbor period or without proper motion procedure",
        "triggers": [
            r"(?:sanction(?:s|ed)?.*(?:without|no|absent)\s+(?:safe\s+harbor|21[-\s]?day|notice))",
            r"(?:sanction(?:s|ed)?.*(?:sua\s+sponte|on\s+(?:its|the\s+court's)\s+own))",
            r"(?:monetary\s+sanction.*(?:without|no)\s+(?:hearing|opportunity))",
            r"(?:rule\s+11.*(?:without|absent|no)\s+(?:motion|service))",
            r"(?:attorney(?:'s)?\s+fees?\s+(?:as\s+)?sanction.*(?:without|no)\s+(?:notice|hearing|safe\s+harbor))",
        ],
        "stance_actions": [
            {
                "motion_title": "Motion to Vacate Sanctions — Rule 11(c)(2) Safe Harbor Violation",
                "rule_basis": "FRCP 11(c)(2); Simmerman v. Corino, 27 F.3d 58 (3d Cir. 1994)",
                "filing_vehicle": "Motion to Vacate under FRCP 60(b)(4) or (6)",
                "forum": "same court",
                "timing": "Within 28 days (FRCP 59(e)) or reasonable time (FRCP 60(b))",
                "key_authorities": [
                    "Simmerman v. Corino, 27 F.3d 58 (3d Cir. 1994) — safe harbor is mandatory; sanctions without it are vacated",
                    "FRCP 11(c)(2) — 21-day safe harbor required before filing sanctions motion",
                    "Hadges v. Yonkers Racing Corp., 48 F.3d 1320 (2d Cir. 1995) — safe harbor requirement is strictly enforced",
                    "Brickwood Contractors v. Datapoint Corp., 369 F.3d 385 (4th Cir. 2004) — failure to serve safe harbor letter = sanctions vacated",
                ],
                "argument_template": "The sanctions imposed by this Court must be vacated because the movant failed to comply with the mandatory safe harbor provision of FRCP 11(c)(2). Under binding Third Circuit precedent in Simmerman, the 21-day safe harbor is a 'strict procedural prerequisite' that cannot be waived. No safe harbor notice was served on Plaintiff, depriving Plaintiff of the opportunity to withdraw or correct the challenged paper as contemplated by the Rule.",
                "escalation": "Appeal to Third Circuit — review de novo for procedural compliance with Rule 11",
            },
        ],
    },

    # ─── 6. SUA SPONTE DISMISSAL WITHOUT LEAVE TO AMEND ───
    "SUA_SPONTE_NO_LEAVE": {
        "category": "Due Process / Sua Sponte Dismissal",
        "severity": "FATAL",
        "display_name": "Sua Sponte Dismissal Without Leave to Amend or Notice",
        "constitutional_basis": "U.S. Const. amend. XIV § 1",
        "rule_citation": "FRCP 12(b)(6); Grayson v. Mayview State Hospital, 293 F.3d 103 (3d Cir. 2002)",
        "description": "Court dismissed the complaint sua sponte without providing notice, opportunity to respond, or leave to amend",
        "triggers": [
            r"(?:dismiss(?:ed|es|al)?.*sua\s+sponte)",
            r"(?:sua\s+sponte.*dismiss(?:ed|al))",
            r"(?:dismiss(?:ed)?.*(?:without|no)\s+(?:leave|opportunity)\s+to\s+amend)",
            r"(?:dismiss(?:ed)?.*with\s+prejudice.*(?:without|no|absent)\s+(?:notice|hearing|leave))",
            r"(?:(?:on\s+(?:its|the\s+court's)\s+own\s+(?:motion|initiative)).*dismiss)",
        ],
        "stance_actions": [
            {
                "motion_title": "Motion for Reconsideration — Sua Sponte Dismissal Without Leave to Amend",
                "rule_basis": "FRCP 59(e); Grayson v. Mayview State Hospital, 293 F.3d 103 (3d Cir. 2002)",
                "filing_vehicle": "Motion for Reconsideration under FRCP 59(e)",
                "forum": "same court",
                "timing": "28 days from entry of dismissal",
                "key_authorities": [
                    "Grayson v. Mayview State Hospital, 293 F.3d 103, 108 (3d Cir. 2002) — leave to amend must be granted unless amendment would be inequitable or futile",
                    "Fletcher-Harlee Corp. v. Pote Concrete Contractors, 482 F.3d 247 (3d Cir. 2007) — district court must permit curative amendment before dismissing for failure to state a claim",
                    "Phillips v. County of Allegheny, 515 F.3d 224, 236 (3d Cir. 2008) — Third Circuit mandates leave to amend unless futility, bad faith, dilatory motive, or undue prejudice",
                    "Alston v. Parker, 363 F.3d 229 (3d Cir. 2004) — even if complaint deficient, leave to amend must be offered in pro se cases",
                ],
                "argument_template": "The Court's sua sponte dismissal without providing leave to amend violates the Third Circuit's binding mandate in Grayson and Fletcher-Harlee. In this Circuit, 'if a complaint is subject to a Rule 12(b)(6) dismissal, a district court must permit a curative amendment unless such an amendment would be inequitable or futile.' The Court made no futility finding. Plaintiff attaches a proposed amended complaint that cures any identified deficiencies.",
                "escalation": "Appeal to Third Circuit — standard of review is abuse of discretion; Alston provides per se reversal rule for pro se denial of leave to amend",
            },
        ],
    },

    # ─── 7. VEXATIOUS LITIGANT DESIGNATION WITHOUT PROCESS ───
    "VEXATIOUS_DESIGNATION_NO_PROCESS": {
        "category": "Access to Courts / Due Process",
        "severity": "CONSTITUTIONAL",
        "display_name": "Vexatious Litigant Designation Without Required Process",
        "constitutional_basis": "U.S. Const. amend. I, XIV",
        "rule_citation": "In re Safir, 2021 WL 5906089 (3d Cir. 2021); Abdul-Akbar v. McKelvie, 239 F.3d 307 (3d Cir. 2001)",
        "description": "Court designated plaintiff as vexatious litigant without adequate notice, factual record, or findings as required by the Safir/Chipps framework",
        "triggers": [
            r"(?:vexatious\s+litigant)",
            r"(?:(?:declare[sd]?|designate[sd]?|declare[sd]?|found|deemed)\s+(?:to\s+be\s+)?(?:a\s+)?vexatious)",
            r"(?:frequent\s+filer|serial\s+(?:litigant|filer))",
            r"(?:pattern\s+of\s+(?:frivolous|abusive|vexatious)\s+(?:litigation|filing|lawsuits?))",
            r"(?:history\s+of\s+(?:frivolous|abusive|vexatious))",
        ],
        "stance_actions": [
            {
                "motion_title": "Motion to Vacate Vexatious Litigant Designation — Procedural Deficiency",
                "rule_basis": "In re Safir; Abdul-Akbar v. McKelvie, 239 F.3d 307 (3d Cir. 2001)",
                "filing_vehicle": "Motion to Vacate under FRCP 60(b)(4) (void) or 60(b)(6) (other reason)",
                "forum": "same court",
                "timing": "Immediately — constitutional deficiency renders order void",
                "key_authorities": [
                    "In re Safir, 2021 WL 5906089 (3d Cir. 2021) — four prerequisites for filing injunction",
                    "Abdul-Akbar v. McKelvie, 239 F.3d 307 (3d Cir. 2001) — pre-filing injunctions in Third Circuit",
                    "Chipps v. U.S. Dist. Court, 882 F.2d 72 (3d Cir. 1989) — must give notice, opportunity to respond, make findings, and narrowly tailor",
                    "In re Green, 669 F.2d 779 (D.C. Cir. 1981) — prefiling order must list specific cases found frivolous",
                ],
                "argument_template": "The vexatious litigant designation must be vacated because the Court failed to satisfy the mandatory prerequisites of Safir/Chipps/Abdul-Akbar: (1) Plaintiff was not given adequate notice of the proposed designation; (2) Plaintiff was not given a meaningful opportunity to oppose; (3) the Court did not compile an adequate record listing the specific cases found frivolous; (4) the restriction is not narrowly tailored. This designation imposes a severe burden on constitutionally protected access to courts and is void without these safeguards.",
                "escalation": "Mandamus to Third Circuit; 42 U.S.C. § 1983 action for unconstitutional restriction on access to courts",
            },
        ],
    },

    # ─── 8. FAILURE TO STATE REASONS ───
    "NO_REASONS_STATED": {
        "category": "Procedural Violation",
        "severity": "SERIOUS",
        "display_name": "Failure to State Reasons for Ruling",
        "constitutional_basis": "U.S. Const. amend. V, XIV (due process requires reasoned decision)",
        "rule_citation": "FRCP 52(a); FRCP 58",
        "description": "Court entered order or judgment without stating factual findings or legal conclusions as required",
        "triggers": [
            r"(?:denied|dismissed|granted)(?:\s+(?:without|with\s+no|absent))?\s*(?:\.|\s*$)",
            r"(?:(?:without|no)\s+(?:explanation|reasoning|analysis|findings?|opinion|basis|discussion))",
            r"(?:order\s+(?:is\s+)?(?:entered|issued).*(?:for\s+(?:the\s+)?reasons?\s+(?:stated|set\s+forth)))",
            r"(?:(?:it\s+is\s+)?(?:so\s+)?ordered(?:\s*\.)?$)",
        ],
        "stance_actions": [
            {
                "motion_title": "Motion for Statement of Reasons Under FRCP 52(a)",
                "rule_basis": "FRCP 52(a)(1); Bright v. Westmoreland County, 443 F.3d 276 (3d Cir. 2006)",
                "filing_vehicle": "Motion under FRCP 52(a) or Motion for Reconsideration under FRCP 59(e)",
                "forum": "same court",
                "timing": "28 days from entry (if FRCP 59(e)); otherwise reasonable time",
                "key_authorities": [
                    "Bright v. Westmoreland County, 443 F.3d 276 (3d Cir. 2006) — court must articulate reasoning sufficient for appellate review",
                    "Anderson v. City of Bessemer City, 470 U.S. 564 (1985) — findings must be sufficient to indicate the factual basis for ultimate conclusion",
                    "FRCP 52(a)(1) — court must find facts specially and state conclusions of law separately in non-jury trials",
                ],
                "argument_template": "The Court's order fails to state the factual findings and legal conclusions required by FRCP 52(a). The Third Circuit has repeatedly held that a district court must 'articulate reasoning sufficient for appellate review.' Bright, 443 F.3d at 282. Without stated reasons, Plaintiff cannot meaningfully exercise the right to appeal, and the appellate court cannot perform its review function. Plaintiff respectfully requests the Court issue findings and conclusions.",
                "escalation": "Appeal to Third Circuit — court will remand for findings under Bright if trial court fails to state reasons",
            },
        ],
    },

    # ─── 9. PRO SE HEIGHTENED STANDARD MISAPPLICATION ───
    "PRO_SE_HEIGHTENED_STANDARD": {
        "category": "Pro Se Rights",
        "severity": "FATAL",
        "display_name": "Failure to Liberally Construe Pro Se Pleading",
        "constitutional_basis": "U.S. Const. amend. XIV § 1 (equal protection / access)",
        "rule_citation": "Haines v. Kerner, 404 U.S. 519 (1972); Erickson v. Pardus, 551 U.S. 89 (2007)",
        "description": "Court held pro se litigant to attorney standard or failed to liberally construe the complaint",
        "triggers": [
            r"(?:pro\s+se.*(?:same|identical|equal)\s+standard)",
            r"(?:(?:despite|notwithstanding|regardless).*pro\s+se\s+status)",
            r"(?:pro\s+se.*(?:not\s+(?:an?\s+)?excuse|no\s+exception|must\s+(?:still\s+)?comply\s+(?:with\s+)?(?:all|every)))",
            r"(?:(?:lack\s+of|without)\s+(?:legal\s+)?(?:training|education|counsel).*(?:does\s+not|cannot)\s+excuse)",
            r"(?:pro\s+se.*(?:held|bound|subject)\s+(?:to\s+)?(?:strict|rigorous|full)\s+compliance)",
        ],
        "stance_actions": [
            {
                "motion_title": "Motion for Reconsideration — Failure to Apply Liberal Construction Standard",
                "rule_basis": "Haines v. Kerner, 404 U.S. 519 (1972); Erickson v. Pardus, 551 U.S. 89 (2007)",
                "filing_vehicle": "Motion for Reconsideration under FRCP 59(e)",
                "forum": "same court",
                "timing": "28 days from entry of order",
                "key_authorities": [
                    "Haines v. Kerner, 404 U.S. 519 (1972) — pro se complaints held to less stringent standards",
                    "Erickson v. Pardus, 551 U.S. 89 (2007) — document filed pro se must be liberally construed",
                    "Dluhos v. Strasberg, 321 F.3d 365, 369 (3d Cir. 2003) — court must identify strongest arguments in pro se filing",
                    "Higgs v. Attorney General, 655 F.3d 333, 339 (3d Cir. 2011) — obligation to construe pro se pleadings liberally",
                ],
                "argument_template": "The Court failed to apply the mandatory liberal construction standard for pro se pleadings required by Haines v. Kerner and reaffirmed in Erickson v. Pardus. The Court's opinion holds Plaintiff to the same standards as an attorney, which the Supreme Court has explicitly prohibited. The Third Circuit has further held that courts must 'identify the strongest arguments' a pro se litigant's filing suggests. Dluhos, 321 F.3d at 369. The Court did not perform this analysis.",
                "escalation": "Appeal to Third Circuit under Dluhos; raise as separate error on appeal",
            },
        ],
    },

    # ─── 10. IMPROPER CONVERSION OF MOTION ───
    "IMPROPER_CONVERSION_12_TO_56": {
        "category": "Procedural Violation",
        "severity": "SERIOUS",
        "display_name": "Improper Conversion from 12(b)(6) to Summary Judgment Without Notice",
        "constitutional_basis": "U.S. Const. amend. XIV § 1",
        "rule_citation": "FRCP 12(d); Rose v. Bartle, 871 F.2d 331 (3d Cir. 1989)",
        "description": "Court considered matters outside the pleadings without converting to summary judgment and providing required notice",
        "triggers": [
            r"(?:(?:consider(?:ed|ing)|relied?\s+(?:on|upon)).*(?:outside|beyond|extrinsic|extraneous).*(?:the\s+)?pleadings?)",
            r"(?:(?:exhibit|document|evidence|declaration|affidavit).*(?:not\s+(?:attached|incorporated|referenced)).*(?:the\s+)?complaint)",
            r"(?:(?:convert(?:ed|ing)?|treat(?:ed|ing)?).*(?:as\s+)?(?:summary\s+judgment|rule\s+56).*(?:without|no|absent)\s+notice)",
        ],
        "stance_actions": [
            {
                "motion_title": "Motion for Reconsideration — Improper Conversion Without Notice Under FRCP 12(d)",
                "rule_basis": "FRCP 12(d); Rose v. Bartle, 871 F.2d 331 (3d Cir. 1989)",
                "filing_vehicle": "Motion for Reconsideration under FRCP 59(e)",
                "forum": "same court",
                "timing": "28 days from entry",
                "key_authorities": [
                    "FRCP 12(d) — if matters outside pleadings are considered, motion must be treated as Rule 56 with all parties given reasonable opportunity to present materials",
                    "Rose v. Bartle, 871 F.2d 331, 340 (3d Cir. 1989) — failure to provide notice of conversion is reversible error",
                    "In re Burlington Coat Factory Securities Litigation, 114 F.3d 1410, 1426 (3d Cir. 1997) — documents integral to complaint may be considered, but extrinsic evidence triggers conversion",
                ],
                "argument_template": "The Court considered matters outside the pleadings — specifically [DESCRIBE MATERIALS] — without converting the motion to dismiss into one for summary judgment under FRCP 12(d) and without providing Plaintiff the required notice and opportunity for discovery. Under Rose v. Bartle, this is reversible error. Plaintiff was prejudiced because, had proper notice been given, Plaintiff would have submitted [DESCRIBE EVIDENCE].",
                "escalation": "Appeal to Third Circuit under Rose — de novo review of legal error",
            },
        ],
    },

    # ──── ADDED: Order-level + Constitutional violations ────

    "DISMISSED_WITH_PREJUDICE": {
        "category": "PROCEDURAL",
        "severity": "FATAL",
        "display_name": "Dismissed With Prejudice - Voidable",
        "constitutional_basis": "Due Process; FRCP 15(a)(2)",
        "rule_citation": "FRCP 12(b)(6); 15(a)(2)",
        "description": "Claim dismissed with prejudice",
        "triggers": [
            r"dismissed\s+with\s+prejudice",
        ],
        "stance_actions": [{"motion_title": "Motion for Reconsideration", "rule_basis": "FRCP 59(e)", "filing_vehicle": "Motion for Reconsideration", "forum": "Same court", "timing": "28 days", "key_authorities": ["Grayson v. Mayview, 293 F.3d 103 (3d Cir. 2002)"], "argument_template": "Dismissed with prejudice without leave to amend.", "escalation": "Appeal"}],
    },

    "REMAND_DENIED": {
        "category": "JURISDICTIONAL",
        "severity": "CONSTITUTIONAL",
        "display_name": "Remand Denied - Jurisdiction Void",
        "constitutional_basis": "28 U.S.C. 1447(c); Article III",
        "rule_citation": "28 U.S.C. 1441, 1447",
        "description": "Court denied remand",
        "triggers": [
            r"remand.*(?:is\s+)?denied",
            r"denying.*remand",
        ],
        "stance_actions": [{"motion_title": "Mandamus - Denial of Remand", "rule_basis": "28 U.S.C. 1447(c)", "filing_vehicle": "Mandamus", "forum": "Circuit Court", "timing": "Promptly", "key_authorities": ["Shamrock Oil v. Sheets, 313 U.S. 100 (1941)"], "argument_template": "Removal jurisdiction strictly construed.", "escalation": "Mandamus"}],
    },

    "TWOMBLY_IQBAL_MISAPPLIED": {
        "category": "PROCEDURAL",
        "severity": "FATAL",
        "display_name": "Twombly/Iqbal Misapplied to Pro Se",
        "constitutional_basis": "Due Process; Haines v. Kerner",
        "rule_citation": "FRCP 12(b)(6)",
        "description": "Heightened pleading standard without pro se construction",
        "triggers": [
            r"FAILURE TO STATE A CLAIM",
            r"MOTION TO DISMISS FOR FAILURE",
        ],
        "stance_actions": [{"motion_title": "Reconsideration - Pro Se Standard", "rule_basis": "Haines v. Kerner", "filing_vehicle": "Motion for Reconsideration", "forum": "Same court", "timing": "28 days", "key_authorities": ["Haines v. Kerner, 404 U.S. 519 (1972)", "Mala v. Crown Bay Marina, 704 F.3d 239 (3d Cir. 2013)"], "argument_template": "Pro se pleadings must be liberally construed.", "escalation": "Appeal"}],
    },

    "DEFAULT_MOTION_DENIED": {
        "category": "PROCEDURAL",
        "severity": "SERIOUS",
        "display_name": "Default Denied",
        "constitutional_basis": "FRCP 55(a)",
        "rule_citation": "FRCP 55(a), (b)",
        "description": "Default denied despite untimely response",
        "triggers": [
            r"default.*denied",
            r"MOTIONS FOR DEFAULT.*ARE DENIED",
        ],
        "stance_actions": [{"motion_title": "Reconsideration of Default Denial", "rule_basis": "FRCP 55(a)", "filing_vehicle": "Motion for Reconsideration", "forum": "Same court", "timing": "28 days", "key_authorities": ["Hritz v. Woma Corp., 732 F.2d 1178 (3d Cir. 1984)"], "argument_template": "FRCP 55(a): clerk SHALL enter default.", "escalation": "Appeal"}],
    },

    "DENIED_WITHOUT_REASONS": {
        "category": "PROCEDURAL",
        "severity": "SERIOUS",
        "display_name": "Denied Without Reasons",
        "constitutional_basis": "Due Process; FRCP 52(a)",
        "rule_citation": "FRCP 52(a)",
        "description": "Denied without individualized reasoning",
        "triggers": [
            r"DENIED FOR THE REASONS PREVIOUSLY STATED",
            r"NO FURTHER CLARIFICATION IS NECESSARY",
            r"DENIED.*SEE\s*\(?\s*ECF",
        ],
        "stance_actions": [{"motion_title": "Motion for Statement of Reasons", "rule_basis": "FRCP 52(a)", "filing_vehicle": "Motion", "forum": "Same court", "timing": "Promptly", "key_authorities": ["Anderson v. Bessemer City, 470 U.S. 564 (1985)"], "argument_template": "Court must state reasons.", "escalation": "Appeal"}],
    },

    "DEPRIVATION_RIGHTS_COLOR_OF_LAW": {
        "category": "CONSTITUTIONAL",
        "severity": "CONSTITUTIONAL",
        "display_name": "Deprivation of Rights - 42 USC 1983",
        "constitutional_basis": "42 U.S.C. 1983; 14th Amendment",
        "rule_citation": "42 U.S.C. 1983; 18 U.S.C. 242",
        "description": "State actor deprived rights under color of law",
        "triggers": [
            r"under color of (?:state )?law",
            r"deprivation of rights",
            r"acting (?:outside|beyond) (?:jurisdiction|authority)",
        ],
        "stance_actions": [{"motion_title": "42 USC 1983 Complaint", "rule_basis": "42 U.S.C. 1983", "filing_vehicle": "Federal Civil Rights Complaint", "forum": "Federal District Court", "timing": "2 years (PA)", "key_authorities": ["Monroe v. Pape, 365 U.S. 167 (1961)"], "argument_template": "Deprivation of constitutional rights under color of law.", "escalation": "DOJ referral"}],
    },

    "RICO_PATTERN": {
        "category": "CRIMINAL",
        "severity": "CONSTITUTIONAL",
        "display_name": "RICO Pattern",
        "constitutional_basis": "18 U.S.C. 1961-1968",
        "rule_citation": "18 U.S.C. 1962(c), (d)",
        "description": "Pattern of racketeering activity",
        "triggers": [
            r"pattern of (?:racketeering|misconduct|fraud)",
            r"(?:enterprise|conspiracy) to (?:defraud|deprive)",
            r"scheme to (?:defraud|deprive)",
        ],
        "stance_actions": [{"motion_title": "Civil RICO Complaint", "rule_basis": "18 U.S.C. 1964(c)", "filing_vehicle": "Federal RICO Complaint", "forum": "Federal District Court", "timing": "4 years", "key_authorities": ["Sedima v. Imrex Co., 473 U.S. 479 (1985)"], "argument_template": "Pattern of racketeering. Treble damages.", "escalation": "DOJ/FBI referral"}],
    },

    "CANON_VIOLATION": {
        "category": "ETHICAL",
        "severity": "SERIOUS",
        "display_name": "Judicial Canon Violation",
        "constitutional_basis": "Code of Judicial Conduct; 28 U.S.C. 455",
        "rule_citation": "Canon 1, 2, 3; 28 U.S.C. 455",
        "description": "Judge violated canons of judicial conduct",
        "triggers": [
            r"(?:appearance|perception) of (?:bias|impropriety)",
            r"ex parte communication",
            r"prejudg(?:ed|ing|ment)",
        ],
        "stance_actions": [{"motion_title": "Recusal Motion + Judicial Conduct Complaint", "rule_basis": "28 U.S.C. 455", "filing_vehicle": "Motion + Complaint to Circuit", "forum": "Same court + Circuit", "timing": "Promptly", "key_authorities": ["Liteky v. United States, 510 U.S. 540 (1994)"], "argument_template": "Canon violation. Recusal mandatory.", "escalation": "Judicial Council complaint"}],
    },

    "FRAUD_ON_COURT_JUDICIAL": {
        "category": "VOID",
        "severity": "CONSTITUTIONAL",
        "display_name": "Fraud on the Court - VOID",
        "constitutional_basis": "FRCP 60(d)(3)",
        "rule_citation": "FRCP 60(d)(3)",
        "description": "Fraud on court - judgment void, no time limit",
        "triggers": [
            r"fraud (?:on|upon) (?:the )?court",
            r"fabricat(?:ed|ion)",
            r"perjur(?:y|ed|ious)",
            r"falsif(?:ied|ication)",
            r"misrepresent(?:ed|ation) (?:to|before) (?:the )?court",
        ],
        "stance_actions": [{"motion_title": "Independent Action - Fraud on Court", "rule_basis": "FRCP 60(d)(3)", "filing_vehicle": "Independent Action", "forum": "Same or higher court", "timing": "NO TIME LIMIT", "key_authorities": ["Hazel-Atlas Glass v. Hartford-Empire, 322 U.S. 238 (1944)"], "argument_template": "Fraud on court. Judgment VOID. No time limit.", "escalation": "Criminal referral; bar complaint"}],
    },

    # ──── NEW: 4 defect classes from dkt 173 mass denial gap analysis ────

    "CONCLUSORY_DISPOSITION_FAILURE_TO_STATE_REASONS": {
        "category": "PROCEDURAL",
        "severity": "FATAL",
        "display_name": "Conclusory Disposition — Failure to State Reasons",
        "constitutional_basis": "Due Process; FRCP 52(a); FRCP 58",
        "rule_citation": "FRCP 52(a)(1); FRCP 58; 28 U.S.C. § 2106",
        "description": "Court denied a motion as 'premature' or 'without prejudice' without articulating specific reasons, failing the duty of reasoned decision-making required by FRCP 52 and the baseline of due process. A conclusory order that denies a motion without addressing the controlling authority or well-pleaded facts forecloses meaningful review.",
        "triggers": [
            r"denied.*as\s+premature",
            r"premature.*denied",
            r"denied\s+without\s+(?:further\s+)?clarification",
            r"denied\s+for\s+the\s+reasons\s+previously\s+stated",
            r"no\s+further\s+explanation\s+necessary",
            r"denied\s+without\s+individualized\s+reasoning",
            r"denied\s+without\s+stating\s+reasons",
            r"dismissed\s+without\s+prejudice.*premature",
        ],
        "stance_actions": [
            {
                "motion_title": "Motion for Statement of Reasons and Findings — Conclusory Denial",
                "rule_basis": "FRCP 52(a)(1); Due Process",
                "filing_vehicle": "Motion for Statement of Reasons",
                "forum": "Same court",
                "timing": "Immediately — preserve for appellate review",
                "key_authorities": [
                    "Anderson v. Bessemer City, 470 U.S. 564 (1985) — findings must be sufficient for meaningful review",
                    "FRCP 52(a)(1) — court must state findings of fact and conclusions of law",
                    "FRCP 58 — judgment must set forth the judgment",
                    "Haines v. Kerner, 404 U.S. 519 (1972) — pro se pleadings liberally construed",
                    "Erickson v. Pardus, 551 U.S. 89 (2007) — liberal construction of pro se filings",
                ],
                "argument_template": "The Court denied Plaintiff's motion as '{quote}' without articulating any specific reasons. Under FRCP 52(a)(1), the court must state its findings of fact and conclusions of law sufficient to permit meaningful appellate review. A conclusory denial forecloses that review. Plaintiff respectfully requests the Court to state the specific reasons for this denial and to address the controlling authorities cited in Plaintiff's motion.",
                "escalation": "Appeal under 28 U.S.C. § 2106 — reversible error if no statement of reasons provided",
            }
        ],
    },

    "DENIAL_OF_54B_CERTIFICATION_WITHOUT_CURTISS_WRIGHT_ANALYSIS": {
        "category": "PROCEDURAL",
        "severity": "FATAL",
        "display_name": "Denial of Rule 54(b) Certification — No Curtiss-Wright Analysis",
        "constitutional_basis": "28 U.S.C. § 1291 — Final Judgment Rule; FRCP 54(b)",
        "rule_citation": "FRCP 54(b); Curtiss-Wright Corp. v. General Elec. Co., 446 U.S. 1 (1980)",
        "description": "Court denied or failed to address a request for Rule 54(b) certification without making the express determination that there is 'no just reason for delay' as required by FRCP 54(b). The controlling authority Curtiss-Wright establishes that the court must analyze whether there is no just reason for delay before denying certification. Without this analysis, the denial is reversible error.",
        "triggers": [
            r"54\(b\).*denied",
            r"certification.*denied",
            r"no\s+just\s+reason\s+for\s+delay",
            r"express\s+determination.*no\s+just\s+reason",
            r"failure\s+to\s+certify.*54\(b\)",
            r"partial\s+judgment.*not\s+final",
            r"54\(b\).*not\s+certified",
            r"denied.*54\(b\)",
            r"denied.*certification",
        ],
        "stance_actions": [
            {
                "motion_title": "Motion for Certification of Final Judgment Under Rule 54(b) — Curtiss-Wright Analysis",
                "rule_basis": "FRCP 54(b); Curtiss-Wright Corp. v. General Elec. Co., 446 U.S. 1 (1980)",
                "filing_vehicle": "Motion for Rule 54(b) Certification",
                "forum": "Same court",
                "timing": "Immediately — appeal clock starts on certification",
                "key_authorities": [
                    "Curtiss-Wright Corp. v. General Elec. Co., 446 U.S. 1 (1980) — four-factor analysis for 54(b) certification",
                    "FRCP 54(b) — court must make express determination of no just reason for delay",
                    "Sussex Drug Prods. v. Kanasco, Ltd., 920 F.2d 1150 (3d Cir. 1990) — Third Circuit applies Curtiss-Wright factors",
                    "Berkeley Inv. Group v. Colkitt, 455 F.3d 195 (3d Cir. 2006) — factors for 54(b) certification in Third Circuit",
                    "28 U.S.C. § 1291 — final judgment rule requires certification for partial dismissals",
                ],
                "argument_template": "Under FRCP 54(b) and Curtiss-Wright, the court must make an express determination that there is no just reason for delay before denying certification. The Court's silence on this threshold question is reversible error. Plaintiff certifies that Example Card Issuer has been dismissed with prejudice, creating an independently appealable judgment. Under the Curtiss-Wright factors, there is no just reason for delay because: (1) there is no risk of inconsistent adjudications; (2) the dismissed claims are separate from remaining claims; (3) delay would prejudice Plaintiff's right to immediate appeal; and (4) the court has already ruled on the Example Card Issuer claims.",
                "escalation": "Appeal to Third Circuit if certification denied — file notice of appeal within 30 days of denial order",
            }
        ],
    },

    "PREMATURE_DENIAL_CATCH22": {
        "category": "PROCEDURAL",
        "severity": "FATAL",
        "display_name": "Premature Denial Catch-22 — SJ Denied as Premature While Discovery Denied",
        "constitutional_basis": "Due Process; FRCP 56; FRCP 37",
        "rule_citation": "FRCP 56; FRCP 37(a); FRCP 26(b)(1)",
        "description": "Court denied summary judgment as 'premature' while simultaneously denying discovery motions, creating a procedural catch-22: the movant cannot meet its summary judgment burden because discovery was denied, and the discovery denial is justified by 'prematurity.' This self-contradictory ruling requires a motion for statement of reasons AND discovery relief.",
        "triggers": [
            r"premature.*discovery",
            r"discovery.*premature",
            r"premature.*denied.*discovery",
            r"denied.*premature.*while.*discovery",
            r"catch\s*22",
            r"self\s*contradictor",
            r"inability\s+to\s+meet\s+(?:burden|standard)",
            r"cannot\s+produce.*evidence.*without.*discovery",
            r"discovery\s+denied.*sj\s+premature",
            r"sj\s+premature.*discovery\s+denied",
        ],
        "stance_actions": [
            {
                "motion_title": "Motion to Compel Discovery + Motion for Statement of Reasons — Premature Denial Catch-22",
                "rule_basis": "FRCP 37(a); FRCP 56; FRCP 26(b)(1)",
                "filing_vehicle": "Motion to Compel Discovery + Motion for Statement of Reasons",
                "forum": "Same court",
                "timing": "Immediately — concurrent filings",
                "key_authorities": [
                    "FRCP 37(a) — motion to compel discovery with good-faith conference certification",
                    "FRCP 56 — summary judgment standard: no genuine dispute of material fact",
                    "FRCP 26(b)(1) — discovery proportionality standard",
                    "Celotex v. Catrett, 477 U.S. 317 (1986) — moving party's initial burden under FRCP 56",
                    "Anderson v. Matsushita, 477 U.S. 535 (1986) — burden shifts to non-movant after initial showing",
                    "Hunt v. Washington State Apple Advertising Comm., 432 U.S. 333 (1977) — factual disputes must be genuine",
                ],
                "argument_template": "The Court denied Plaintiff's motion for summary judgment as 'premature' while simultaneously denying Plaintiff's discovery motions. This creates an impossible catch-22: Plaintiff cannot meet its summary judgment burden because discovery was denied, and the denial is justified by 'prematurity.' Under FRCP 56, Plaintiff has met its initial burden by identifying the absence of evidence supporting the non-moving party's case. The Court's contradictory rulings require a statement of reasons under FRCP 52(a)(1) and immediate discovery relief under FRCP 37(a).",
                "escalation": "Appeal under 28 U.S.C. § 1291 — reversible error if catch-22 prevents meaningful adjudication",
            }
        ],
    },

    "FRIVOLOUS_PREMATURE_LABELS_AS_BIAS_INDICATOR": {
        "category": "ETHICAL",
        "severity": "CONSTITUTIONAL",
        "display_name": "Frivolous/Premature Labels as Bias Indicator — Liteky/Sec. 455(a)",
        "constitutional_basis": "28 U.S.C. § 455(a); Liteky v. United States",
        "rule_citation": "Liteky v. United States, 510 U.S. 540 (1994); 28 U.S.C. § 455(a)",
        "description": "A pattern of labeling procedurally-proper filings as 'frivolous' or 'premature' without analysis, without citing authority, and while granting opposing relief on bare assertions IS a bias indicator under 28 U.S.C. § 455(a) because it creates the appearance that impartiality might reasonably be questioned. This is distinct from individual rulings (which Liteky protects) — it is the PATTERN of conclusory denials that creates the appearance of bias.",
        "triggers": [
            r"frivolous.*motion",
            r"premature.*motion.*denied",
            r"pattern\s+of\s+(?:frivolous|conclusory).*denials",
            r"conclusory.*denial.*pattern",
            r"repeated\s+denial\s+without\s+analysis",
            r"labeling\s+proper\s+filings\s+as\s+(?:frivolous|premature)",
            r"denied\s+as\s+frivolous\s+without\s+reasoning",
            r"frivolous.*without\s+(?:citing|addressing|analyzing)",
        ],
        "stance_actions": [
            {
                "motion_title": "Motion for Recusal Under 28 U.S.C. § 455(a) — Pattern of Conclusory Denials",
                "rule_basis": "28 U.S.C. § 455(a); Liteky v. United States, 510 U.S. 540 (1994)",
                "filing_vehicle": "Motion for Recusal + Affidavit of Bias under 28 U.S.C. § 144",
                "forum": "Same court (file with different judge if possible)",
                "timing": "Immediately — delay may waive",
                "key_authorities": [
                    "Liteky v. United States, 510 U.S. 540, 548 (1994) — bias must show deep-seated favoritism or antagonism",
                    "28 U.S.C. § 455(a) — impartiality might reasonably be questioned",
                    "28 U.S.C. § 455(b)(1) — personal bias or prejudice",
                    "Caperton v. A.T. Massey Coal Co., 556 U.S. 868 (2009) — appearance of bias",
                    "In re Murchison, 349 U.S. 133 (1955) — judge as accuser and adjudicator",
                    "Tumey v. Ohio, 273 U.S. 510 (1927) — due process requires impartial adjudicator",
                ],
                "argument_template": "Under Liteky, individual rulings are not bias. But a PATTERN of labeling procedurally-proper filings as 'frivolous' or 'premature' without analysis, while granting opposing relief on bare assertions, IS a bias indicator under § 455(a) because it creates the appearance that impartiality might reasonably be questioned. The record shows {count} such denials. This pattern is precisely the kind of 'deep-seated antagonism' that § 455(a) was designed to address. Plaintiff respectfully requests recusal under § 455(a) and files an affidavit of bias under § 144.",
                "escalation": "Appeal under 28 U.S.C. § 1291; file complaint with judicial conduct commission; preserve record for 28 U.S.C. § 351/JCB filing",
            }
        ],
    },


# ──── FACT-LEVEL VIOLATIONS (scan case descriptions, not just judge quotes) ────

    "STARE_DECISIS_VIOLATION": {
        "category": "PROCEDURAL",
        "severity": "FATAL",
        "display_name": "Stare Decisis Violation — Non-Binding Authority Applied",
        "constitutional_basis": "Article III — Judicial Power; Due Process",
        "rule_citation": "Binding precedent doctrine",
        "description": "Court or opposing party relied on non-binding, out-of-circuit, or unpublished authority as if controlling",
        "triggers": [
            r"(?:stare\s+decisis\s+violation)",
            r"(?:fatal\s+(?:stare\s+decisis|violation))",
            r"(?:non[-\s]?binding\s+(?:authority|precedent|citation))",
            r"(?:out\s+of\s+circuit)",
            r"(?:cited\s+\d+.{1,20}as\s+(?:if\s+)?(?:controlling|binding))",
            r"(?:presented\s+as\s+binding)",
            r"(?:unpublished\s+(?:opinion|decision)\s+(?:cited|relied))",
            r"(?:\d+\s+fatal\s+(?:stare\s+decisis\s+)?violations)",
        ],
        "stance_actions": [
            {
                "motion_title": "Motion for Reconsideration — Non-Binding Authority",
                "rule_basis": "FRCP 59(e)",
                "filing_vehicle": "Motion for Reconsideration",
                "forum": "Same court",
                "timing": "28 days from order",
                "key_authorities": ["Hicks v. Feeney, 850 F.2d 152 (3d Cir. 1988)"],
                "argument_template": "The court/opposing party relied on {quote} which is not binding on this court. Binding Third Circuit authority requires...",
                "escalation": "Appeal to Third Circuit",
            }
        ],
    },

    "IMPROPER_REMOVAL_FACT": {
        "category": "JURISDICTIONAL",
        "severity": "FATAL",
        "display_name": "Improper Removal — Case Should Be Remanded",
        "constitutional_basis": "28 U.S.C. § 1447(c)",
        "rule_citation": "28 U.S.C. § 1441, § 1447",
        "description": "Case was improperly removed from state court without proper basis",
        "triggers": [
            r"(?:improper\s+removal)",
            r"(?:removed\s+(?:from|to)\s+(?:state|federal)\s+court)",
            r"(?:forum\s+shopping)",
            r"(?:removal\s+(?:was\s+)?(?:defective|improper|procedurally))",
            r"(?:RMEDC)",
            r"(?:remand\s+(?:should|must|is\s+required))",
        ],
        "stance_actions": [
            {
                "motion_title": "Motion to Remand",
                "rule_basis": "28 U.S.C. § 1447(c)",
                "filing_vehicle": "Motion to Remand",
                "forum": "Federal court (where case was removed to)",
                "timing": "30 days from removal notice",
                "key_authorities": ["Shamrock Oil & Gas Co. v. Sheets, 313 U.S. 100 (1941)"],
                "argument_template": "Removal was improper because {quote}. Under 28 U.S.C. § 1447(c), this case must be remanded to state court.",
                "escalation": "Mandamus if denied",
            }
        ],
    },

    "AGENCY_FAILURE_FACT": {
        "category": "REGULATORY",
        "severity": "SERIOUS",
        "display_name": "Agency/Regulator Failure to Act",
        "constitutional_basis": "5 U.S.C. § 706(1) — APA",
        "rule_citation": "5 U.S.C. § 706(1); 28 U.S.C. § 1361",
        "description": "Regulatory agency failed to investigate, respond, or act on mandatory duty",
        "triggers": [
            r"(?:failed\s+to\s+investigate)",
            r"(?:failed\s+(?:to\s+)?(?:conduct|perform)\s+reasonable\s+(?:re)?investigation)",
            r"(?:no\s+reply\s+received)",
            r"(?:constructive\s+denial)",
            r"(?:agency\s+fail)",
            r"(?:regulator\s+fail)",
            r"(?:complaint\s+ignored)",
            r"(?:failed\s+to\s+(?:respond|act|process))",
            r"(?:CFPB|cfpb).*(?:fail|ignor|no\s+(?:response|action))",
        ],
        "stance_actions": [
            {
                "motion_title": "Mandamus / APA Complaint — Compel Agency Action",
                "rule_basis": "5 U.S.C. § 706(1); 28 U.S.C. § 1361",
                "filing_vehicle": "Complaint under APA or Mandamus Petition",
                "forum": "Federal district court",
                "timing": "After exhaustion of administrative remedies",
                "key_authorities": ["Norton v. Southern Utah Wilderness Alliance, 542 U.S. 55 (2004)"],
                "argument_template": "The agency had a mandatory, non-discretionary duty to {duty} and failed to act. Under APA § 706(1), the court shall compel agency action unlawfully withheld.",
                "escalation": "Appeal; EAJA attorney fees",
            }
        ],
    },

    "IFP_DENIAL_CONSTRUCTIVE": {
        "category": "ACCESS_TO_COURTS",
        "severity": "CONSTITUTIONAL",
        "display_name": "IFP Constructive Denial — No Response to Application",
        "constitutional_basis": "U.S. Const. amend. I, XIV; 28 U.S.C. § 1915",
        "rule_citation": "28 U.S.C. § 1915",
        "description": "IFP application filed but no ruling received — constructive denial of access to courts",
        "triggers": [
            r"(?:IFP\s+(?:appeal|application|motion)\s+(?:filed|pending))",
            r"(?:IFP.*no\s+(?:reply|response|ruling|decision))",
            r"(?:in\s+forma\s+pauperis.*(?:pending|no\s+(?:response|ruling)))",
            r"(?:appellate.*ifp.*pending)",
        ],
        "stance_actions": [
            {
                "motion_title": "Motion to Compel Ruling on IFP Application",
                "rule_basis": "28 U.S.C. § 1915; FRAP 24",
                "filing_vehicle": "Motion / Mandamus",
                "forum": "Appellate court",
                "timing": "Immediately — constitutional right at stake",
                "key_authorities": ["Bounds v. Smith, 430 U.S. 817 (1977)", "Neitzke v. Williams, 490 U.S. 319 (1989)"],
                "argument_template": "Plaintiff filed IFP application on {date}. No ruling has been issued. This constructive denial violates the right of access to courts.",
                "escalation": "Mandamus to circuit court",
            }
        ],
    },

    "WILLFUL_NONCOMPLIANCE_FACT": {
        "category": "SUBSTANTIVE",
        "severity": "FATAL",
        "display_name": "Willful Statutory Noncompliance by Defendant",
        "constitutional_basis": "15 U.S.C. § 1681n (FCRA); 15 U.S.C. § 1692k (FDCPA)",
        "rule_citation": "FCRA § 1681n; FDCPA § 1692k",
        "description": "Defendant willfully violated federal statute — enhanced damages available",
        "triggers": [
            r"(?:willful\s+(?:violation|noncompliance))",
            r"(?:willful\s+noncompliance\s+under)",
            r"(?:negligent\s+noncompliance)",
            r"(?:knowing(?:ly)?\s+(?:violat|noncompl))",
        ],
        "stance_actions": [
            {
                "motion_title": "Motion for Summary Judgment on Willfulness",
                "rule_basis": "15 U.S.C. § 1681n; FRCP 56",
                "filing_vehicle": "Motion for Summary Judgment / Partial Summary Judgment",
                "forum": "Trial court",
                "timing": "After discovery",
                "key_authorities": ["Safeco Ins. Co. v. Burr, 551 U.S. 47 (2007)"],
                "argument_template": "Defendants' conduct constitutes willful noncompliance under {statute} because {quote}. Statutory damages of $100-$1,000 per violation plus punitive damages are mandatory.",
                "escalation": "Trial on damages",
            }
        ],
    },

}


# ════════════════════════════════════════════════════════════════════
# DB ENRICHMENT — query ChromaDB for case law on violations found
# ════════════════════════════════════════════════════════════════════

DB_QUERY_MAP = {
    "PRIOR_RESTRAINT_FILING_BAN": [
        "filing injunction prior restraint constitutional safeguards",
        "Safir Chipps filing injunction factors Third Circuit",
        "pre-filing injunction vexatious litigant due process",
    ],
    "DO_NOT_FILE_LANGUAGE": [
        "prior restraint right to petition First Amendment filing",
        "Bill Johnson Restaurants right to file lawsuit First Amendment",
        "court order prohibiting filing constitutionality",
    ],
    "EX_PARTE_RULING": [
        "ex parte order due process void judgment",
        "Mullane notice reasonably calculated due process",
        "void judgment no notice opportunity to be heard",
    ],
    "ACCESS_TO_COURTS_DENIAL": [
        "access to courts Bounds Smith fundamental right",
        "Christopher Harbury access courts claim elements",
        "denial access courts 1983 civil rights",
    ],
    "JUDICIAL_BIAS_LANGUAGE": [
        "judicial recusal 28 USC 455 impartiality bias",
        "Liteky bias extrajudicial source deep-seated antagonism",
        "mandamus recusal judge bias Third Circuit",
    ],
    "SANCTIONS_NO_SAFE_HARBOR": [
        "Rule 11 safe harbor 21 day sanctions mandatory",
        "Simmerman safe harbor sanctions vacated Third Circuit",
        "sanctions without notice due process violation",
    ],
    "SUA_SPONTE_NO_LEAVE": [
        "sua sponte dismissal leave to amend Grayson",
        "Fletcher-Harlee curative amendment dismissal Third Circuit",
        "pro se dismissal leave to amend mandatory",
    ],
    "VEXATIOUS_DESIGNATION_NO_PROCESS": [
        "vexatious litigant designation due process requirements",
        "Abdul-Akbar pre-filing injunction Third Circuit",
        "Safir four factors filing injunction vexatious",
    ],
    "PRO_SE_HEIGHTENED_STANDARD": [
        "Haines Kerner pro se liberal construction",
        "pro se pleading less stringent standard binding",
        "Dluhos strongest arguments pro se Third Circuit",
    ],
    "CONCLUSORY_DISPOSITION_FAILURE_TO_STATE_REASONS": [
        "FRCP 52(a) findings of fact conclusions of law reasoned decision",
        "FRCP 58 separate judgment conclusory denial reversible error",
        "conclusory order denial without reasons due process",
    ],
    "DENIAL_OF_54B_CERTIFICATION_WITHOUT_CURTISS_WRIGHT_ANALYSIS": [
        "FRCP 54(b) certification no just reason for delay Curtiss-Wright",
        "partial final judgment certification Third Circuit factors",
        "Curtiss-Wright General Electric 446 U.S. 1 five factors 54(b)",
    ],
    "PREMATURE_DENIAL_CATCH22": [
        "summary judgment premature discovery denied catch-22 FRCP 56",
        "discovery denial summary judgment premature contradictory ruling",
        "FRCP 37 motion to compel discovery sanctions",
    ],
    "FRIVOLOUS_PREMATURE_LABELS_AS_BIAS_INDICATOR": [
        "Liteky bias pattern of frivolous labels 28 USC 455(a)",
        "judicial bias pattern of conclusory denials appearance",
        "deep-seated antagonism Liteky 510 U.S. 540 bias indicator",
    ],
}


def _enrich_violations_from_db(violations: list[JudicialViolation]) -> None:
    """Enrich each detected violation with REAL supporting authority from the
    TurboVec corpus (semantic search over authority_vectors_768.npy). Retrieval
    only — every cite is a real authority in the corpus; weak matches are
    dropped by a similarity floor. Replaces the retired ChromaDB enrichment."""
    try:
        from pipeline.turbovec_search import search_authorities
    except Exception as e:
        log.error(f"TurboVec enrichment unavailable: {e}")
        return
    for v in violations:
        q = f"{getattr(v, 'display_name', '')} {getattr(v, 'constitutional_basis', '')}".strip()
        if not q:
            continue
        try:
            hits = search_authorities(q, top_k=4)
        except Exception as e:
            log.error(f"TurboVec search failed for {getattr(v,'violation_id','?')}: {e}")
            continue
        if hits:
            v.db_enrichment = {
                "source": "turbovec",
                "authorities_found": len(hits),
                "supporting_authorities": [
                    {"citation": h["citation"], "similarity": h["similarity"]}
                    for h in hits
                ],
            }
    return  # legacy ChromaDB body below is dead and unreachable
    try:
        from pipeline.db_connector import LegalDB
        db = LegalDB()
    except Exception as e:
        log.error(f"DB enrichment unavailable: {e}")
        return

    for v in violations:
        queries = DB_QUERY_MAP.get(v.violation_id, [])
        if not queries:
            queries = [f"{v.display_name} {v.constitutional_basis}"]

        all_hits = []
        for q in queries[:3]:
            try:
                hits = db.query_text(q, n_results=3)
                for h in hits:
                    txt = h.get("text", h.get("document", ""))
                    if txt and len(txt) > 50:
                        all_hits.append(txt[:600])
            except Exception as e:
                log.error(f"DB query failed for {v.violation_id}: {e}")

        if all_hits:
            v.db_enrichment = {
                "chunks_found": len(all_hits),
                "case_law_text": "\n\n".join(all_hits[:4]),
            }
            log.info(f"  DB enriched {v.violation_id}: {len(all_hits)} chunks")


# ════════════════════════════════════════════════════════════════════
# MAIN SCANNER
# ════════════════════════════════════════════════════════════════════

def scan_order(
    text: str,
    source_doc: str = "Judge's Order",
    role: str = "plaintiff",
    enrich_db: bool = True,
) -> dict:
    """Scan a judge's order for constitutional/procedural violations.

    Args:
        text: full text of the order
        source_doc: document identifier (e.g. "Order dated 2025-01-15")
        role: "plaintiff" — all stance actions framed from this position
        enrich_db: whether to query ChromaDB for case law

    Returns:
        Structured analysis with violations + stance-aware actions
    """
    # ── document_role_classifier guard (Bundle 10 / fix v4 + Patch B) ──────
    # Filename classification + inline content-probe escalation for "unknown".
    # The `text` parameter is already extracted by the caller, so checking it
    # for judicial markers adds no I/O cost.
    _JUDICIAL_MARKERS_INLINE = (
        "it is ordered",
        "it is hereby ordered",
        "and now, this",
        "by the court:",
        "memorandum opinion",
        "u.s. district judge",
        "united states district judge",
        "magistrate judge",
        "circuit judge",
        "is hereby denied",
        "is hereby granted",
        "the motion is granted",
        "the motion is denied",
        "ordered, adjudged",
        "ordered and adjudged",
        "is denied as moot",
        "shall be dismissed",
    )
    try:
        import os as _os
        from pathlib import Path as _Path
        from pipeline.document_role_classifier import classify as _classify_role
        _allowed = set((_os.environ.get(
            "BEEZLE_JOS_ALLOWED_ROLES", "judicial_output"
        )).split(","))
        _doc_path = _Path(source_doc) if source_doc else None
        if _doc_path is not None:
            _rc = _classify_role(_doc_path, case_id=None, probe_content=False)
            _role = _rc.role
            _method = _rc.method
            # Escalation: if filename says "unknown" and unknown is not allowed,
            # check the already-extracted `text` for judicial markers. ≥2 markers
            # promotes to judicial_output; otherwise skip.
            if _role == "unknown" and "unknown" not in _allowed:
                _text_lower = (text or "").lower()[:8000]
                _markers_hit = sum(
                    1 for _m in _JUDICIAL_MARKERS_INLINE if _m in _text_lower
                )
                import logging as _logging
                if _markers_hit >= 2:
                    _logging.getLogger("judicial_order_scanner").info(
                        f"[role_guard] PROMOTED {_doc_path.name}: "
                        f"unknown→judicial_output (content_markers={_markers_hit})"
                    )
                    _role = "judicial_output"
                    _method = "content_marker_escalation"
                else:
                    _logging.getLogger("judicial_order_scanner").info(
                        f"[role_guard] SKIPPED {_doc_path.name}: role=unknown "
                        f"(content_markers={_markers_hit}/2 required); "
                        f"allowed={sorted(_allowed)}"
                    )
                    return []
            elif _role not in _allowed:
                import logging as _logging
                _logging.getLogger("judicial_order_scanner").info(
                    f"[role_guard] SKIPPED {_doc_path.name}: role={_role} "
                    f"(confidence={_rc.confidence}, method={_method}); "
                    f"allowed={sorted(_allowed)}"
                )
                return []
    except Exception as _e:
        import logging as _logging
        _logging.getLogger("judicial_order_scanner").debug(
            f"[role_guard] failed (continuing without guard): {_e}"
        )
    # ── end role guard ─────────────────────────────────────────────────────
    t0 = time.perf_counter()
    text_lower = text.lower()
    lines = text.split("\n")
    violations: list[JudicialViolation] = []

    for vid, vdef in JUDICIAL_VIOLATIONS.items():
        triggers = vdef["triggers"]
        for trigger in triggers:
            match = re.search(trigger, text, re.IGNORECASE | re.MULTILINE | re.DOTALL)
            if match:
                # Locate in document
                char_pos = match.start()
                line_num = text[:char_pos].count("\n") + 1
                page_num = (line_num // 50) + 1

                # Extract quote with context
                ctx_start = max(0, match.start() - 50)
                ctx_end = min(len(text), match.end() + 80)
                quote = text[ctx_start:ctx_end].replace("\n", " ").strip()
                if ctx_start > 0:
                    quote = "..." + quote
                if ctx_end < len(text):
                    quote = quote + "..."

                # Build stance actions
                stance_actions = []
                for sa_def in vdef.get("stance_actions", []):
                    stance_actions.append(StanceAction(**sa_def))

                v = JudicialViolation(
                    violation_id=vid,
                    category=vdef["category"],
                    severity=vdef["severity"],
                    display_name=vdef["display_name"],
                    constitutional_basis=vdef["constitutional_basis"],
                    rule_citation=vdef["rule_citation"],
                    description=vdef["description"],
                    quote=quote,
                    source_doc=source_doc,
                    page=page_num,
                    line_start=line_num,
                    line_end=min(line_num + 3, len(lines)),
                    stance_actions=stance_actions,
                )
                violations.append(v)
                break  # one match per violation type

    # Deduplicate by violation_id
    seen = set()
    unique = []
    for v in violations:
        if v.violation_id not in seen:
            seen.add(v.violation_id)
            unique.append(v)
    violations = unique

    # Sort by severity
    severity_order = {"CONSTITUTIONAL": 0, "FATAL": 1, "SERIOUS": 2, "STANDARD": 3}
    violations.sort(key=lambda v: severity_order.get(v.severity, 99))

    # DB enrichment
    if enrich_db and violations:
        _enrich_violations_from_db(violations)

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    log.info(f"Scanned {source_doc}: {len(violations)} judicial violations in {elapsed_ms}ms")

    # Strategy synthesis
    strategy = []
    constitutional_count = len([v for v in violations if v.severity == "CONSTITUTIONAL"])
    fatal_count = len([v for v in violations if v.severity == "FATAL"])

    if constitutional_count > 0:
        strategy.append(f"🔴 {constitutional_count} CONSTITUTIONAL violation(s) — these are void ab initio, no time bar. FILE IMMEDIATELY.")
    if fatal_count > 0:
        strategy.append(f"🟠 {fatal_count} FATAL procedural violation(s) — reversible error on appeal.")
    if any(v.violation_id in ("PRIOR_RESTRAINT_FILING_BAN", "DO_NOT_FILE_LANGUAGE") for v in violations):
        strategy.append("⚠️  PRIOR RESTRAINT DETECTED — consider emergency mandamus to Third Circuit under 28 U.S.C. § 1651(a).")
    if any(v.violation_id == "JUDICIAL_BIAS_LANGUAGE" for v in violations):
        strategy.append("⚠️  JUDICIAL BIAS — file recusal motion ASAP; delay may waive. Also consider judicial misconduct complaint.")
    if any(v.violation_id == "EX_PARTE_RULING" for v in violations):
        strategy.append("⚠️  EX PARTE RULING — order is VOID under Mullane/Peralta. Attack under FRCP 60(b)(4) at ANY time.")
    if any(v.violation_id == "VEXATIOUS_DESIGNATION_NO_PROCESS" for v in violations):
        strategy.append("⚠️  VEXATIOUS DESIGNATION without Safir/Chipps process — attack as unconstitutional filing injunction.")
    if len(violations) >= 3:
        strategy.append(f"📋 {len(violations)} total violations — consider bundling into omnibus Motion to Vacate or comprehensive appellate brief.")

    return {
        "source_doc": source_doc,
        "role": role,
        "violations_count": len(violations),
        "violations": [v.to_dict() for v in violations],
        "severity_summary": {
            "constitutional": constitutional_count,
            "fatal": fatal_count,
            "serious": len([v for v in violations if v.severity == "SERIOUS"]),
            "standard": len([v for v in violations if v.severity == "STANDARD"]),
        },
        "strategy": strategy,
        "stance_actions_total": sum(len(v.stance_actions) for v in violations),
        "db_enriched": sum(1 for v in violations if v.db_enrichment),
        "telemetry": {
            "runtime_ms": elapsed_ms,
            "patterns_checked": len(JUDICIAL_VIOLATIONS),
        },
    }


def scan_order_text_from_file(filepath: str | Path, **kwargs) -> dict:
    """Convenience: read order text from file and scan."""
    p = Path(filepath)
    if not p.exists():
        return {"error": f"File not found: {filepath}"}

    if p.suffix.lower() == ".pdf":
        try:
            from pipeline.doc_reader import extract_text
            text = extract_text(p)
        except ImportError:
            return {"error": "doc_reader not available for PDF extraction"}
    else:
        text = p.read_text(encoding="utf-8", errors="replace")

    source = kwargs.pop("source_doc", p.stem)
    return scan_order(text, source_doc=source, **kwargs)


# ════════════════════════════════════════════════════════════════════
# BASELINE LOOP INTEGRATION
# ════════════════════════════════════════════════════════════════════

# BEEZLE_FN_UNUSED 2026-06-13: run_judicial_scanner_for_case (no static refs in pipeline/)
def run_judicial_scanner_for_case(case: dict) -> dict:
    """Integration point for baseline_loop.py — run scanner on case facts.

    Looks for order text in:
      1. case["order_text"] — raw text of the order
      2. case["order_file"] — path to order file
      3. case["facts"] — fall back to fact pattern (will catch some triggers)
    """
    order_text = case.get("order_text", "")
    if not order_text and case.get("order_file"):
        result = scan_order_text_from_file(
            case["order_file"],
            role=case.get("role", "plaintiff"),
        )
        return result

    if not order_text:
        # Fall back to scanning facts for order-like language
        order_text = "\n".join(case.get("facts", []))

    return scan_order(
        text=order_text,
        source_doc=case.get("order_doc_id", "Case Facts"),
        role=case.get("role", "plaintiff"),
        enrich_db=True,
    )


# ════════════════════════════════════════════════════════════════════
# REPORT FORMATTING
# ════════════════════════════════════════════════════════════════════

def format_report(result: dict) -> str:
    """Format full stance-aware report."""
    lines = []
    lines.append("# JUDICIAL ORDER SCANNER — STANCE-AWARE ANALYSIS")
    lines.append(f"**Source:** {result['source_doc']}")
    lines.append(f"**Role:** {result['role'].upper()} (all actions framed from this position)")
    lines.append(f"**Violations found:** {result['violations_count']}")
    sev = result["severity_summary"]
    lines.append(f"**Severity:** {sev['constitutional']} constitutional · "
                  f"{sev['fatal']} fatal · {sev['serious']} serious · {sev['standard']} standard")
    lines.append(f"**Total response actions:** {result['stance_actions_total']}")
    lines.append("")

    if result.get("strategy"):
        lines.append("## STRATEGY")
        for s in result["strategy"]:
            lines.append(f"- {s}")
        lines.append("")

    for v_data in result["violations"]:
        # Reconstruct for formatting
        v = JudicialViolation(**{k: v_data[k] for k in [
            "violation_id", "category", "severity", "display_name",
            "constitutional_basis", "rule_citation", "description",
            "quote", "source_doc", "page", "line_start", "line_end",
        ]})
        v.stance_actions = [StanceAction(**sa) for sa in v_data.get("stance_actions", [])]
        v.db_enrichment = v_data.get("db_enrichment", {})
        lines.append(v.format_full())
        lines.append("---")

    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Block 15 — Stance-Aware Judicial Order Scanner")
    parser.add_argument("input", nargs="?",
                        help="Path to order file (PDF/TXT/DOCX) or raw text")
    parser.add_argument("--role", default="plaintiff",
                        choices=["plaintiff", "defendant"])
    parser.add_argument("--no-db", action="store_true",
                        help="Skip ChromaDB enrichment")
    parser.add_argument("--output", help="Path to write JSON result")
    parser.add_argument("--format", choices=["json", "md"], default="md")
    args = parser.parse_args()

    if not args.input:
        # Demo mode — test with "do not file" language
        demo_text = (
            "ORDER\n\n"
            "AND NOW, this 15th day of January, 2025, upon consideration of "
            "Plaintiff's Motion for Reconsideration, IT IS HEREBY ORDERED that "
            "said motion is DENIED.\n\n"
            "Plaintiff is further ORDERED to not file any further motions in this "
            "matter without first obtaining leave of this Court. Plaintiff's repeated "
            "filings constitute a waste of this Court's time and resources. These types "
            "of cases brought by pro se litigants are frivolous without any basis in "
            "law or fact.\n\n"
            "This matter is dismissed with prejudice without leave to amend.\n\n"
            "IT IS SO ORDERED."
        )
        print("Demo mode — scanning sample order with 'do not file' language:\n")
        print(f"---\n{demo_text}\n---\n")
        result = scan_order(demo_text, source_doc="Demo Order",
                            role=args.role, enrich_db=not args.no_db)
    else:
        input_path = Path(args.input)
        if input_path.exists():
            result = scan_order_text_from_file(input_path, role=args.role,
                                                enrich_db=not args.no_db)
        else:
            result = scan_order(args.input, role=args.role,
                                enrich_db=not args.no_db)

    if args.format == "md":
        print(format_report(result))
    else:
        print(json.dumps(result, indent=2, default=str))

    if args.output:
        Path(args.output).write_text(
            json.dumps(result, indent=2, default=str), encoding="utf-8")
        print(f"\nJSON written to {args.output}")


if __name__ == "__main__":
    main()
