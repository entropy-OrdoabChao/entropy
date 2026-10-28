"""
pipeline/foia_builder.py — Deterministic FOIA complaint builder.

Takes case state and produces a fully populated ComplaintData dict
for the V3 complaint drafter. Closes the V1→V2 gap.

This module is called by pipeline_draft when case_type == "foia".
It replaces the generic _build_claims_for_type path with FOIA-specific
logic that populates ALL sections the drafter supports:
  - Preliminary statement
  - Jurisdiction & venue
  - Defendant separation with individual roles
  - Statement of facts (timeline sections with exhibit refs)
  - Exhaustion of administrative remedies
  - Itemized damages
  - Consolidated prayer for relief

ALL text comes from case_state or Obsidian templates. NO LLM calls.
"""

from typing import Dict, List, Optional


def build_foia_complaint_args(state: Dict, obsidian_claims: List[Dict]) -> Dict:
    """
    Build the full kwargs dict for build_complaint_from_facts().
    
    Args:
        state: case_state dict (from disk)
        obsidian_claims: parsed claims from obsidian_parser.parse_obsidian_counts()
    
    Returns:
        dict ready to pass as **kwargs to build_complaint_from_facts()
    """
    plaintiff = state.get("plaintiff", {})
    p_name = plaintiff.get("name", "")
    p_addr = plaintiff.get("address", "")
    p_email = plaintiff.get("email", "")
    
    # ── Defendants: separate with individual roles ──
    defendants = _build_defendants(state.get("defendants", []))
    
    # ── Preliminary Statement ──
    prelim = _build_preliminary_statement(state, defendants)
    
    # ── Jurisdiction & Venue ──
    jurisdiction = _build_jurisdiction(state)
    
    # ── Statement of Facts ──
    fact_sections = _build_fact_sections(state)
    
    # ── Exhaustion ──
    exhaustion = _build_exhaustion(state)
    
    # ── Claims: inject facts from state into obsidian claims ──
    claims_data = _enrich_claims(obsidian_claims, state)
    
    # ── Exhibits ──
    exhibits = _build_exhibits(state)
    
    # ── Prayer for Relief ──
    prayer = _build_prayer(state, claims_data)
    
    # ── Complaint title ──
    statutes = state.get("statutes", [])
    statute_names = []
    if "5 U.S.C. 552" in " ".join(statutes) or any("552" in s for s in statutes):
        statute_names.append("THE FREEDOM OF INFORMATION ACT")
    if "552a" in " ".join(statutes):
        statute_names.append("THE PRIVACY ACT OF 1974")
    if "1361" in " ".join(statutes):
        statute_names.append("28 U.S.C. \u00a7 1361")
    
    title_suffix = ", ".join(statute_names) if statute_names else "FEDERAL LAW"
    complaint_title = (
        f"COMPLAINT FOR INJUNCTIVE RELIEF, DECLARATORY JUDGMENT, "
        f"DAMAGES, AND MANDAMUS UNDER {title_suffix}"
    )
    
    return {
        "plaintiff_name": p_name,
        "plaintiff_address": p_addr,
        "plaintiff_email": p_email,
        "plaintiff_role": plaintiff.get("role", "a natural person, proceeding pro se"),
        "defendants": defendants,
        "court": "IN THE UNITED STATES DISTRICT COURT",
        "district": state.get("district", "FOR THE EASTERN DISTRICT OF PENNSYLVANIA"),
        "complaint_title": complaint_title,
        "facts": [],  # facts go through fact_sections now
        "claims_data": claims_data,
        "exhibits": exhibits,
        "preliminary_statement": prelim,
        "jurisdiction_paragraphs": jurisdiction,
        "fact_sections": fact_sections,
        "exhaustion_paragraphs": exhaustion,
        "prayer_for_relief": prayer,
        "verification": True,
        "cert_of_service": True,
    }


# ═══════════════════════════════════════════════════════
# SECTION BUILDERS
# ═══════════════════════════════════════════════════════

def _build_defendants(raw_defendants: List[Dict]) -> List[Dict]:
    """
    Separate defendants into individual entries with proper fields.
    
    Input format (from state):
        [{"name": "HUD", "capacity": "official capacity"}]
        or
        [{"name": "HUD", "title": "...", "department": "...", "capacity": "...", "conduct": "..."}]
    
    If defendants are already separated (have title/department), pass through.
    If they're just names, keep as-is — the pipeline_intake upgrade will handle separation.
    """
    result = []
    for d in raw_defendants:
        entry = {
            "name": d.get("name", ""),
            "capacity": d.get("capacity", ""),
            "role": d.get("role", ""),
            "title": d.get("title", ""),
            "department": d.get("department", ""),
            "email": d.get("email", ""),
            "conduct": d.get("conduct", ""),
        }
        result.append(entry)
    return result


def _build_preliminary_statement(state: Dict, defendants: List[Dict]) -> List[str]:
    """Build preliminary statement paragraphs from state."""
    prelim = state.get("preliminary_statement", [])
    if prelim:
        return prelim
    
    # Auto-generate from available info
    p_name = state.get("plaintiff", {}).get("name", "Plaintiff")
    case_desc = " ".join(state.get("facts", []))
    
    # Only auto-generate if we have enough info
    if not case_desc or len(case_desc) < 20:
        return []
    
    paras = []
    
    # Para 1: who is bringing what
    agency_name = ""
    for d in defendants:
        if any(kw in d["name"].upper() for kw in ["DEPARTMENT", "AGENCY", "HUD", "DOJ", "FDA", "EPA"]):
            agency_name = d["name"]
            break
    
    if agency_name:
        paras.append(
            f'Plaintiff {p_name} (\u201cPlaintiff\u201d), proceeding pro se, brings this action '
            f'against {agency_name} and the named individual Defendants in their official '
            f'capacities to compel production of agency records unlawfully withheld in violation '
            f'of the Freedom of Information Act (\u201cFOIA\u201d), 5 U.S.C. \u00a7 552, and the '
            f'Privacy Act of 1974, 5 U.S.C. \u00a7 552a, and to recover damages for Defendants\u2019 '
            f'intentional and willful refusal to comply with mandatory statutory duties.'
        )
    
    return paras


def _build_jurisdiction(state: Dict) -> List[str]:
    """Build jurisdiction and venue paragraphs."""
    jur = state.get("jurisdiction_paragraphs", [])
    if jur:
        return jur
    
    court = state.get("court", "EDPA")
    
    # FOIA-specific jurisdiction
    paras = [
        'This Court has subject matter jurisdiction under 5 U.S.C. \u00a7 552(a)(4)(B) (FOIA), '
        '5 U.S.C. \u00a7 552a(g)(1) (Privacy Act), 28 U.S.C. \u00a7 1331 (federal question), '
        'and 28 U.S.C. \u00a7 1361 (mandamus).',
        
        'Venue is proper under 5 U.S.C. \u00a7 552(a)(4)(B) because Plaintiff resides '
        'in the Eastern District of Pennsylvania.',
        
        'This Court has authority to issue injunctive and declaratory relief under '
        '5 U.S.C. \u00a7 552(a)(4)(B) and 28 U.S.C. \u00a7\u00a7 2201\u20132202.',
    ]
    
    return paras


def _build_fact_sections(state: Dict) -> Dict[str, List[str]]:
    """Build statement of facts from state timeline and documents."""
    # If state already has structured fact_sections, use them
    existing = state.get("fact_sections", {})
    if existing:
        return existing
    
    # Build from timeline entries
    timeline = state.get("timeline", [])
    facts = state.get("facts", [])
    documents = state.get("documents", [])
    
    sections = {}
    
    # If we have timeline entries, organize by phase
    if timeline:
        for entry in timeline:
            section = entry.get("section", "General")
            if section not in sections:
                sections[section] = []
            text = entry.get("text", "")
            if entry.get("exhibit"):
                text += f" (Ex. {entry['exhibit']}.)"
            if entry.get("date"):
                text = f"On {entry['date']}, {text}" if not text.startswith("On ") else text
            sections[section].append(text)
    
    # If no timeline but we have facts, put them in a single section
    elif facts and len(facts) > 0 and len(facts[0]) > 30:
        sections["General"] = facts
    
    return sections


def _build_exhaustion(state: Dict) -> List[str]:
    """Build exhaustion of administrative remedies paragraphs."""
    existing = state.get("exhaustion_paragraphs", [])
    if existing:
        return existing
    
    # Auto-generate for FOIA
    paras = []
    
    # Check if state has appeal info
    timeline = state.get("timeline", [])
    appeal_info = [t for t in timeline if "appeal" in t.get("text", "").lower()]
    
    if appeal_info:
        paras.append(
            'Plaintiff filed a timely administrative appeal. The agency issued its '
            'appeal determination confirming the constructive denial and expressly advising '
            'Plaintiff of his right to judicial review in this Court. Administrative remedies '
            'are exhausted.'
        )
    
    paras.append(
        'Under 5 U.S.C. \u00a7 552(a)(6)(C)(i), when an agency fails to comply with '
        'applicable time limits, the requester is deemed to have exhausted administrative '
        'remedies. The agency\u2019s failure to respond within 20 working days independently '
        'exhausts remedies regardless of the appeal.'
    )
    
    return paras


def _enrich_claims(obsidian_claims: List[Dict], state: Dict) -> List[Dict]:
    """Inject facts, admissions, and exhibit refs from state into Obsidian claims."""
    facts = state.get("facts", [])
    timeline = state.get("timeline", [])
    
    for claim in obsidian_claims:
        # Inject facts
        if not claim.get("facts") or claim["facts"] == []:
            claim["facts"] = facts
        
        # Inject admissions from state
        if state.get("admissions"):
            claim["admissions"] = state["admissions"]
    
    return obsidian_claims


def _build_exhibits(state: Dict) -> List[Dict]:
    """Build exhibit list from state documents."""
    existing = state.get("exhibits", [])
    if existing:
        return existing
    
    # Build from documents if available
    documents = state.get("documents", [])
    exhibits = []
    for i, doc in enumerate(documents):
        letter = chr(65 + i)  # A, B, C, ...
        exhibits.append({
            "letter": letter,
            "description": doc.get("description", doc.get("name", f"Document {i+1}")),
            "date": doc.get("date", ""),
        })
    
    return exhibits


def _build_prayer(state: Dict, claims_data: List[Dict]) -> List[str]:
    """Build consolidated prayer for relief."""
    existing = state.get("prayer_for_relief", [])
    if existing:
        return existing
    
    # FOIA-specific prayer
    prayer = [
        'Declare that Defendants violated the Freedom of Information Act, 5 U.S.C. \u00a7 552, '
        'by failing to make a timely determination and by improperly withholding agency records',
        
        'Declare that Defendants violated the Privacy Act of 1974, 5 U.S.C. \u00a7 552a, '
        'by refusing to provide Plaintiff access to records about himself maintained in systems of records',
        
        'Order Defendants to conduct an adequate search for all records responsive to '
        'Plaintiff\u2019s FOIA request and to produce all non-exempt responsive records within '
        'fourteen (14) days',
        
        'Order Defendants to produce a Vaughn index for any records or portions withheld '
        'under claimed FOIA exemptions',
        
        'Issue a writ of mandamus compelling the named Defendants to immediately perform '
        'their non-discretionary duties under FOIA and the Privacy Act',
        
        'Order court-supervised compliance with periodic reporting requirements until '
        'Defendants have fully produced all responsive records',
        
        'Award Plaintiff actual damages pursuant to 5 U.S.C. \u00a7 552a(g)(4)(A) in an '
        'amount to be determined at trial, including substantial compensatory damages, '
        'statutory damages (minimum $1,000), and any additional relief necessary to fully '
        'redress the harm and ensure compliance with federal law',
        
        'Award Plaintiff reasonable attorney\u2019s fees and litigation costs pursuant to '
        '5 U.S.C. \u00a7 552(a)(4)(E), or pro se costs to the extent recoverable',
        
        'Retain jurisdiction until Defendants have fully complied with this Court\u2019s '
        'Orders and the requirements of FOIA and the Privacy Act',
    ]
    
    return prayer
