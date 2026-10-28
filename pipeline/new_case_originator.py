# -*- coding: utf-8 -*-
"""
new_case_originator.py
======================
Track B: Violations without existing cases → new case origination packets.

Workflow:
  1. Receive violation + email context
  2. Match originator taxonomy (HUD / FCRA / ECOA / FDCPA / TILA / etc.)
  3. Extract defendant from agency/from fields
  4. Run chain_runner agents 1-6 for drafting
  5. Assemble ORIGINATOR packet:
     - 00_PREAMBLE.md        — plain-language summary
     - 01_agency_complaint.md — HUD/CFPB/FTC complaint draft
     - 02_demand_letter.md    — pre-suit demand (30 days)
     - 03_complaint_draft.md  — federal/state civil complaint
     - 04_evidence_list.md    — what to attach
     - packet.json            — metadata
"""

from __future__ import annotations
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

import json
import logging
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = _BEEZLE_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.chain_runner import ChainRunner
from pipeline.originator_taxonomy import (
    ORIGINATORS, match_originators, get_originator
)

log = logging.getLogger("new_case_originator")

ORIGINATORS_DIR = ROOT / "output" / "new_case_packets"
ORIGINATORS_DIR.mkdir(parents=True, exist_ok=True)


def _extract_defendant(row: Dict[str, Any]) -> Dict[str, str]:
    """Pull defendant name + domain + sender from the email row."""
    agency = row.get("agency_name") or ""
    domain = row.get("agency_domain") or ""
    sender = row.get("from") or ""
    # Best guess at entity name
    name = agency
    if not name and domain:
        # strip subdomains: "savings.lendingtree.com" -> "lendingtree"
        parts = domain.lower().split(".")
        if len(parts) >= 2:
            name = parts[-2].title()
    if not name:
        m = re.search(r"([A-Za-z][A-Za-z&\s]+?)\s*<", sender)
        if m:
            name = m.group(1).strip()
    return {
        "name":   name or "Unknown Respondent",
        "domain": domain,
        "sender": sender,
    }


def _date_plus(days: int) -> str:
    return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")


def generate_originator_packet(
    row: Dict[str, Any],
    originator_key: str,
    runner: Optional[ChainRunner] = None,
) -> Dict[str, Any]:
    """Generate a full new-case packet for one violation."""
    spec = get_originator(originator_key)
    if not spec:
        return {"error": f"unknown originator key: {originator_key}"}

    defendant = _extract_defendant(row)
    packet_id = f"NEW-{datetime.now().strftime('%Y%m%d_%H%M%S')}-{originator_key[:12]}"
    safe_def = re.sub(r"[^A-Za-z0-9_-]", "_", defendant["name"])[:30]
    packet_dir = ORIGINATORS_DIR / f"{packet_id}_{safe_def}"
    packet_dir.mkdir(parents=True, exist_ok=True)

    # Run chain — skip 7, 9 (case-specific), keep 1, 2, 3, 4, 5, 6, 8, 10
    if runner is None:
        runner = ChainRunner(vault=False, log_each=False)

    body = row.get("body_preview", "")
    subject = row.get("subject", "")
    violation = row.get("violation") or {}

    chain_payload = {
        "text":           f"{subject}\n\n{body}",
        "subject":        subject,
        "body":           body,
        "from":           row.get("from"),
        "agency":         row.get("agency_name"),
        "violation_type": originator_key,
        "category":       originator_key,
        "statute_cited":  ", ".join(spec["statutes"]),
        "entity":         defendant["name"],
        "date":           row.get("date"),
        "deadlines":      row.get("deadlines") or [],
    }

    chain_state = None
    try:
        chain_state = runner.run(chain_payload, case_id=None,
                                 skip=[7, 9])  # skip case-specific agents
    except Exception as e:
        log.error("chain failed for originator %s: %s", originator_key, e)

    # ---- 00 PREAMBLE ----
    preamble = f"""# NEW CASE PACKET — {originator_key.replace('_', ' ').title()}

**Packet ID:** {packet_id}
**Generated:** {datetime.now().isoformat()}
**Urgency:** {spec['urgency']}

## The Violation
**What happened:** {subject or '(see email)'}
**Respondent:** {defendant['name']}
**Contact:** {defendant['sender']}
**Statutes violated:** {', '.join(spec['statutes'])}
**Cause of action:** {spec['cause_of_action']}

## Statute of Limitations
**SOL:** {spec['sol_years']} years from date of violation.
**Computed deadline:** {_date_plus(spec['sol_years'] * 365)}

## Potential Damages
{chr(10).join(f'- {d}' for d in spec['damages'])}

## Execution Plan (in order)

### Day 1 — Agency Complaint
- File with {spec['agency_complaint']['agency']}
- Portal: {spec['agency_complaint'].get('url', 'see agency site')}
- Form: {spec['agency_complaint'].get('form', '(none)')}
- Use `01_agency_complaint.md` as content

### Day 1 — Pre-Suit Demand
{'- Required by statute' if spec['pre_suit_demand'] else '- Not required (strict liability), but recommended for settlement leverage'}
- {spec['demand_days']}-day response window
- Send via USPS Certified Mail, return receipt requested
- Use `02_demand_letter.md`

### Day {spec['demand_days'] + 1} — File Civil Complaint (if demand unanswered)
- Court: {spec['court']}
- Defendants: {defendant['name']}
- Use `03_complaint_draft.md`
- Attachments: see `04_evidence_list.md`

## Files in this Packet
- `00_PREAMBLE.md` (this file)
- `01_agency_complaint.md`
- `02_demand_letter.md`
- `03_complaint_draft.md`
- `04_evidence_list.md`
- `packet.json`
- `chain_state.json`
"""
    (packet_dir / "00_PREAMBLE.md").write_text(preamble, encoding="utf-8")

    # ---- 01 AGENCY COMPLAINT ----
    agency_content = f"""# {spec['agency_complaint']['agency']} Complaint Draft

**Submit via:** {spec['agency_complaint'].get('url', 'agency portal')}

---

**Complainant:** [YOUR NAME]
**Respondent:** {defendant['name']}
**Respondent contact:** {defendant['sender']}
**Date of violation:** {row.get('date', '[DATE]')}

## Violation
{spec['cause_of_action']} in violation of {', '.join(spec['statutes'])}.

## Facts
On {row.get('date', '[DATE]')}, respondent {defendant['name']} {subject.lower() if subject else '[describe action]'}.

Specific conduct:
{body[:1500] if body else '[describe in detail what the respondent did, when, and the impact]'}

## Requested Relief
- Investigation of respondent
- Order to cease and desist
- Restitution / remediation
- Civil penalty enforcement

## Evidence Attached
- Copy of email/correspondence dated {row.get('date', '[DATE]')}
- [Additional documentation as available]

**Complainant signature:** ________________________ **Date:** ____________
"""
    (packet_dir / "01_agency_complaint.md").write_text(agency_content, encoding="utf-8")

    # ---- 02 DEMAND LETTER ----
    demand_content = f"""# Pre-Suit Demand Letter

[YOUR NAME]
[YOUR ADDRESS]
[CITY, STATE ZIP]

{datetime.now().strftime('%B %d, %Y')}

**VIA CERTIFIED MAIL, RRR**

{defendant['name']}
Legal Department
[ADDRESS]

**RE: Demand for Cure — {spec['cause_of_action']}**
**Statutes:** {', '.join(spec['statutes'])}

Dear Legal Counsel:

This letter serves as formal pre-suit notice and demand under the above-cited statutes.

## Background

On {row.get('date', '[DATE]')}, your organization {subject.lower() if subject else '[describe action]'}.

Specifically:
{body[:1500] if body else '[describe the violation in detail]'}

This conduct constitutes {spec['cause_of_action']} in violation of {', '.join(spec['statutes'])}.

## Demand

Within {spec['demand_days']} days of your receipt of this letter, I demand:

1. Full cure of the violation (specific to facts above)
2. Written confirmation of corrective action taken
3. Preservation of all records pertaining to this matter
4. Good-faith settlement offer addressing actual damages, statutory damages, and attorneys' fees

## Consequence of Non-Response

If I have not received a substantive response within {spec['demand_days']} days, I will:

1. File a complaint with {spec['agency_complaint']['agency']}
2. Commence civil litigation in {spec['court']}
3. Seek the following damages available under {spec['statutes'][0]}:
{chr(10).join(f'   - {d.replace("_", " ").title()}' for d in spec['damages'])}

This letter does not waive any rights, claims, or remedies, all of which are expressly reserved.

Sincerely,

________________________
[YOUR NAME]

cc: File
"""
    (packet_dir / "02_demand_letter.md").write_text(demand_content, encoding="utf-8")

    # ---- 03 COMPLAINT DRAFT ----
    a6 = (chain_state.agent_json(6) if chain_state else {}) or {}
    chain_draft = a6.get("draft_text", "")

    complaint_content = f"""# CIVIL COMPLAINT (DRAFT)

**IN THE {('UNITED STATES DISTRICT COURT FOR THE EASTERN DISTRICT OF PENNSYLVANIA' if spec['court'] == 'EDPA' else 'COURT OF COMMON PLEAS OF PHILADELPHIA COUNTY')}**

[YOUR NAME],
    Plaintiff,

vs.                                            Civil Action No. ___________

{defendant['name'].upper()},
    Defendant.

---

## COMPLAINT

Plaintiff, [YOUR NAME], {'pro se,' if True else 'by and through undersigned counsel,'} brings this action against Defendant {defendant['name']} and alleges as follows:

## JURISDICTION AND VENUE

1. Jurisdiction is proper under {'28 USC 1331 (federal question)' if spec['court'] == 'EDPA' else '42 Pa.C.S. 931 (general trial court jurisdiction)'}.
2. Venue is proper in this district pursuant to 28 USC 1391(b).

## PARTIES

3. Plaintiff [YOUR NAME] is an adult individual residing in Philadelphia, PA.
4. Defendant {defendant['name']} is {'a corporation doing business in this district' if True else '[describe]'}.

## FACTUAL ALLEGATIONS

5. On {row.get('date', '[DATE]')}, Defendant {subject.lower() if subject else '[describe action]'}.

6. {body[:800] if body else '[Detailed factual narrative of the violation]'}

7. As a direct and proximate result of Defendant's conduct, Plaintiff has suffered damages.

## COUNT I — {spec['cause_of_action'].upper()} ({spec['statutes'][0]})

8. Plaintiff incorporates paragraphs 1-7 as though fully set forth herein.
9. Defendant's conduct violates {spec['statutes'][0]}.
10. Defendant's violation was {'willful and knowing' if 'punitive' in spec['damages'] else 'negligent'}.
11. Plaintiff is entitled to damages including: {', '.join(spec['damages'])}.

{'## COUNT II — ' + spec['statutes'][1] + chr(10) + chr(10) + '12. Plaintiff incorporates all prior paragraphs.' + chr(10) + '13. Defendant additionally violated ' + spec['statutes'][1] + '.' + chr(10) if len(spec['statutes']) > 1 else ''}

## PRAYER FOR RELIEF

WHEREFORE, Plaintiff respectfully requests that this Court enter judgment in Plaintiff's favor and against Defendant for:

a. Actual damages in an amount to be proven at trial;
b. Statutory damages as provided by {spec['statutes'][0]};
{'c. Punitive damages;' if 'punitive' in ' '.join(spec['damages']) else ''}
{'d. Treble damages;' if 'treble' in ' '.join(spec['damages']) else ''}
e. Reasonable attorneys' fees and costs;
f. Pre-judgment and post-judgment interest;
g. Such other and further relief as this Court deems just and proper.

## JURY DEMAND

Plaintiff demands a trial by jury on all issues so triable.

Dated: {datetime.now().strftime('%B %d, %Y')}

Respectfully submitted,

________________________
[YOUR NAME], Pro Se
[ADDRESS]
[PHONE] / [EMAIL]

---

## AI-Generated Drafting Notes (from chain_runner)

{chain_draft if chain_draft else '(chain draft unavailable)'}
"""
    (packet_dir / "03_complaint_draft.md").write_text(complaint_content, encoding="utf-8")

    # ---- 04 EVIDENCE LIST ----
    evidence_content = f"""# Evidence Checklist — {originator_key}

## Primary Evidence (attach to all filings)
- [ ] Copy of email dated {row.get('date', '[DATE]')} from {defendant['sender']}
- [ ] Full email headers (message_id: {row.get('message_id', 'N/A')})
- [ ] Any attachments referenced in the email

## Supporting Evidence (gather before filing)
- [ ] Screenshots of relevant account/application portals
- [ ] Prior correspondence with respondent
- [ ] Credit reports (if credit-related)
- [ ] Payment records / bank statements (if financial)
- [ ] Photographs (if housing-related)
- [ ] Witness statements (if applicable)

## Damages Documentation
- [ ] Records showing actual monetary loss
- [ ] Records of time spent on disputes (hourly rate × hours)
- [ ] Medical records for emotional distress (if claimed)
- [ ] Proof of impact on credit / housing / employment

## Procedural Records
- [ ] Certified mail receipts
- [ ] Return receipt (green card) for demand letter
- [ ] Agency complaint confirmation number
- [ ] Filing fee receipt or IFP application

## Source Email (reference)
Subject: {subject}
From: {row.get('from', 'N/A')}
Date: {row.get('date', 'N/A')}
Agency match reason: {(row.get('agency_match_reason') or '(unrecorded)')}
"""
    (packet_dir / "04_evidence_list.md").write_text(evidence_content, encoding="utf-8")

    # ---- packet.json ----
    packet_meta = {
        "packet_id":       packet_id,
        "packet_type":     "NEW_CASE_ORIGINATOR",
        "originator_key":  originator_key,
        "urgency":         spec["urgency"],
        "generated":       datetime.now().isoformat(),
        "defendant":       defendant,
        "source_email": {
            "message_id": row.get("message_id"),
            "date":       row.get("date"),
            "subject":    subject,
            "from":       row.get("from"),
        },
        "cause_of_action": spec["cause_of_action"],
        "statutes":        spec["statutes"],
        "court":           spec["court"],
        "sol_years":       spec["sol_years"],
        "sol_deadline":    _date_plus(spec["sol_years"] * 365),
        "pre_suit_demand": spec["pre_suit_demand"],
        "demand_days":     spec["demand_days"],
        "agency_complaint": spec["agency_complaint"],
        "damages":         spec["damages"],
        "output_folder":   str(packet_dir),
        "files": [
            "00_PREAMBLE.md",
            "01_agency_complaint.md",
            "02_demand_letter.md",
            "03_complaint_draft.md",
            "04_evidence_list.md",
        ],
    }
    (packet_dir / "packet.json").write_text(
        json.dumps(packet_meta, indent=2, default=str), encoding="utf-8"
    )

    # Chain state (if we got one)
    if chain_state:
        def _safe(o):
            if isinstance(o, (str, int, float, bool, type(None))): return o
            if isinstance(o, dict): return {k: _safe(v) for k, v in o.items()}
            if isinstance(o, (list, tuple)): return [_safe(x) for x in o]
            if hasattr(o, "__dict__"):
                return {k: _safe(v) for k, v in o.__dict__.items() if not k.startswith("_")}
            return str(o)
        (packet_dir / "chain_state.json").write_text(
            json.dumps(_safe(chain_state.to_dict()), indent=2, default=str),
            encoding="utf-8"
        )

    log.info("NEW CASE packet: %s / %s  →  %s",
             originator_key, defendant["name"], packet_dir.name)
    return packet_meta
