#!/usr/bin/env python3
"""
pipeline/filing_requirements.py

m0 FILING-COMPLETENESS CHECKLIST — every filing type has REQUIRED accompanying components, and a filing
is defective if any is missing. This registry encodes them per (category, court), grounded in the rules
(corpus-cited where confirmed; [PIN] where the rule is real but not yet cleanly in corpus). The checklist
verifies a package has everything and reports what is missing — so a package is never filed incomplete.

Examples the user named: a new PA complaint needs a NOTICE TO DEFEND; a PA appeal needs a NOTICE OF APPEAL
+ DOCKETING STATEMENT; a federal appeal needs a disclosure statement + information statement; motions need
a certificate of service; etc.

Deterministic; never raises. Extend the REGISTRY as new filing types are added — do not hardcode per case.
"""
from __future__ import annotations
import re
from typing import Dict, List, Any

# A requirement: (component key, human label, rule cite, required?/'required'|'conditional'|'recommended', note)
def _r(key, label, cite, level="required", note=""):
    return {"key": key, "label": label, "cite": cite, "level": level, "note": note}

# Registry keyed by (category, court). category is derived from the filing type; court is the destination.
REGISTRY: Dict[tuple, List[dict]] = {
    # ---- PA appeal to the Superior Court (from Common Pleas) ----
    ("appeal", "PA_SUPER"): [
        _r("notice_of_appeal", "Notice of Appeal", "Pa.R.A.P. 902-904 [corpus: Pa.R.A.P. 904 content]"),
        _r("docketing_statement", "Docketing Statement", "Pa.R.A.P. 3517 [corpus] — Prothonotary sends the form; complete & return within 10 days"),
        _r("proof_of_service", "Proof/Certificate of Service", "Pa.R.A.P. 906 / 121"),
        _r("orders_appealed", "Copy of the order(s) appealed", "Pa.R.A.P. 904(a) / 2111(b) (with the brief)"),
        _r("request_for_transcript", "Request for Transcript", "Pa.R.A.P. 904(c), 1911", "conditional", "if a transcript is needed"),
        _r("ifp", "IFP petition or continued-IFP", "Pa.R.C.P. 240 / Pa.R.A.P. 551-561", "conditional", "if the fee is not paid"),
        _r("appellate_brief", "Appellant's Brief + Reproduced Record", "Pa.R.A.P. 2111, 2135, 2151 [corpus]", "recommended", "filed LATER on the briefing schedule, not with the notice"),
    ],
    # ---- Federal appeal to the Third Circuit (from E.D. Pa.) ----
    ("appeal", "3D_CIR"): [
        _r("notice_of_appeal", "Notice of Appeal", "Fed. R. App. P. 3 [corpus] (filed in the district court)"),
        _r("disclosure_statement", "Corporate/Financial Disclosure Statement", "Fed. R. App. P. 26.1 [corpus]", "conditional", "nongovernmental corporate party; a pro se natural person is generally N/A"),
        _r("civil_appeal_info_statement", "Civil Appeal Information Statement", "3d Cir. L.A.R. 3.4 [corpus — ingested 2026-07-06 from ca3.uscourts.gov, source=ca3_local_appellate_rules]"),
        _r("transcript_order", "Transcript Purchase Order / Form", "Fed. R. App. P. 10(b) [corpus]", "conditional", "if a transcript is needed"),
        _r("ifp", "Motion to Proceed IFP on Appeal", "Fed. R. App. P. 24 [corpus]", "conditional", "if the fee is not paid"),
        _r("proof_of_service", "Certificate of Service", "Fed. R. App. P. 25(d)"),
        _r("appellate_brief", "Brief of Appellant + Appendix + Rule 32(g) certificate", "Fed. R. App. P. 28, 30, 32 [corpus]", "recommended", "filed LATER on the briefing schedule"),
    ],
    # ---- PA new civil complaint (Common Pleas) ----
    ("new_complaint", "PA_CP_PHILA"): [
        _r("notice_to_defend", "Notice to Defend", "Pa.R.C.P. 1018.1 [PIN]", "required", "the complaint MUST contain a Notice to Defend or it is defective"),
        _r("cover_sheet", "Civil Cover Sheet", "local rule / Phila. C.C.P."),
        _r("verification", "Verification", "Pa.R.C.P. 1024 [corpus]"),
        _r("proof_of_service", "Certificate of Service", "Pa.R.C.P. 440"),
    ],
    # ---- Federal new complaint (E.D. Pa.) ----
    ("new_complaint", "EDPA"): [
        _r("civil_cover_sheet", "Civil Cover Sheet (JS-44)", "28 U.S.C. § 1914 / local rule"),
        _r("summons", "Summons for each defendant", "Fed. R. Civ. P. 4"),
        _r("ifp", "IFP application or filing fee", "28 U.S.C. § 1915 (AO 240)", "conditional"),
        _r("proof_of_service", "Certificate of Service", "Fed. R. Civ. P. 5(d)"),
    ],
    # ---- Generic motion (any court) ----
    ("motion", "*"): [
        _r("proposed_order", "Proposed Order", "local practice"),
        _r("proof_of_service", "Certificate of Service", "Fed. R. Civ. P. 5(d) / Pa.R.C.P. 440"),
    ],
    # ---- Response/opposition (any court) ----
    ("response", "*"): [
        _r("proof_of_service", "Certificate of Service", "Fed. R. Civ. P. 5(d) / Pa.R.C.P. 440"),
    ],
    # ---- PA Common Pleas POST-ORDER vehicles (S-m1-vehicle-recon, 2026-10-24) ----
    # CLASS RULE: an adverse Common Pleas order has two 30-day vehicles, both clocked from ENTRY (the docket date of
    # the Rule 236 notice, Pa.R.C.P. 236(b) [corpus:pa_code_t231_ch200#c8c1d7a9e946fbc3de1ec9094c219749]):
    #   motion_for_reconsideration / motion_to_vacate — 42 Pa.C.S. § 5505 [corpus:pa_cs_t42#1efcb2666f0b4466]
    #     "a court upon notice to the parties may modify or rescind any order within 30 days after its entry ... if no
    #     appeal from such order has been taken or allowed."  Phila.Civ.R. *208.3(a)(3)
    #     [corpus:phila_civ_r_motion_rules_2004#f8d09e9eb9314d09] forwards it to the judge immediately on filing.
    #   petition_341c — Pa.R.A.P. 341(c)(1) [corpus:pa_code_t210_ch3#06ca5647f759e927e05738d8fac6d494] "An application
    #     for a determination of finality under subdivision (c) shall be filed within 30 days of entry of the order."
    # Defect fixed: m1 had NO vehicle for ORDER_17 (Phila CCP 260100001, POs sustained with prejudice, no reasons).
    # Motion contents: Pa.R.C.P. 208.2(a) [corpus:pa_code_t231_ch200#b1f5b1e1faaedd9d31d03ac9aba0362c] (caption with the
    # number of the action, numbered ¶¶, grounds + relief + PROPOSED ORDER, certificate of service). Phila. packet:
    # *208.3(b)(3)(A) [corpus:phila_civ_r_motion_rules_2004#57146842f05fd18f] cover sheet (i), proposed order (ii);
    # *208.3(b)(3)(B) [corpus:...#d938993ebfae390b] Control Number on the cover sheet. Pa.R.C.P. 1024 verification
    # [corpus:pa_code_t231_ch1000#552ca5bb58293b335ee8e1625990c6bb]. Phila.Civ.R. *205.2(b)(2) cover-sheet form text:
    # NOT IN CORPUS (only the *208.3 cross-reference is). Pa.R.A.P. 1701(b)(3) tolling: NOT IN CORPUS — not asserted.
    ("post_order_motion", "PA_CP_PHILA"): [
        _r("proposed_order", "Proposed Order (no reference to the party proposing it)", "Pa.R.C.P. 208.2(a)(3) [corpus]; Phila.Civ.R. *208.3(b)(3)(A)(ii) [corpus]"),
        _r("motion_cover_sheet", "Petition/Motion Cover Sheet (Control No. assigned here)", "Phila.Civ.R. *208.3(b)(3)(A)(i) -> *205.2(b)(2) [cross-ref in corpus; form text NOT IN CORPUS]"),
        _r("verification", "Verification", "Pa.R.C.P. 1024 [corpus]"),
        _r("proof_of_service", "Certificate of Service (manner of service stated)", "Pa.R.C.P. 208.2(a)(4) [corpus]; Pa.R.C.P. 440 [corpus]"),
        _r("order_challenged", "Copy of the order challenged (with its Rule 236 entry date)", "Phila.Civ.R. *208.3(b)(3)(E) [corpus]; Pa.R.C.P. 236(b) [corpus]"),
        _r("brief", "Brief or memorandum of law", "Phila.Civ.R. *208.3(b)(3)(A)(iii) [corpus] — *208.3(a)(3) reconsideration is initially considered without briefs", "recommended"),
    ],
}

# POST-ORDER VEHICLE MATRIX (DATA). posture text -> vehicle. Order matters: the first rule whose every `needs` token
# family matches wins; `blocks` names the vehicle a posture forbids (posture-vehicle-spec discipline).
POST_ORDER_VEHICLES = {
    "PA_CP_PHILA": [
        {"vehicle": "petition_341c", "relief": "express determination of finality (Pa.R.A.P. 341(c))",
         "needs": [("fewer than all", "less than all", "some of the claims", "some claims", "one of the claims", "partial", "not all claims", "not all parties")],
         "blocks": [],
         "why": "an order adjudicating fewer than all claims/parties is NOT final (341(c)); the 30-day application asks the court for the express determination"},
        {"vehicle": "motion_to_vacate", "relief": "vacate the order (42 Pa.C.S. § 5505)",
         "needs": [("void", "without jurisdiction", "no jurisdiction", "vacate")],
         "blocks": ["petition_341c"],
         "why": "a void order is rescinded, not certified for appeal"},
        {"vehicle": "motion_for_reconsideration", "relief": "modify or rescind the order (42 Pa.C.S. § 5505)",
         "needs": [("sustain", "dismiss", "deny", "grant", "order", "judgment")],
         "blocks": [],
         "why": "any adverse order within 30 days of entry with no appeal taken: the court may modify or rescind it (§ 5505)"},
    ],
}
POST_ORDER_CLOCK_DAYS = 30   # 42 Pa.C.S. § 5505 / Pa.R.A.P. 341(c)(1): both run from ENTRY (Rule 236(b) docket date)


def select_post_order_vehicle(posture: str, court: str = "PA_CP_PHILA", days_since_entry=None, appeal_taken: bool = False) -> Dict[str, Any]:
    """Pick the post-order vehicle from POST_ORDER_VEHICLES for a plain-language posture. Deterministic; never raises.
    Returns {vehicle, relief, why, blocked:[...], timely:bool|None, clock_days}. vehicle=None when nothing fits or the
    posture forbids every vehicle (appeal already taken -> § 5505 bars modification)."""
    p = (posture or "").lower()
    rows = POST_ORDER_VEHICLES.get(court) or []
    out = {"vehicle": None, "relief": None, "why": "no post-order vehicle registered for this court/posture",
           "blocked": [], "timely": None, "clock_days": POST_ORDER_CLOCK_DAYS, "court": court}
    if appeal_taken:
        out["why"] = "42 Pa.C.S. § 5505: the court may modify or rescind only 'if no appeal from such order has been taken or allowed'"
        out["blocked"] = [r["vehicle"] for r in rows if r["vehicle"] != "petition_341c"]
        return out
    for r in rows:
        if all(any(tok in p for tok in fam) for fam in r["needs"]):
            out.update({"vehicle": r["vehicle"], "relief": r["relief"], "why": r["why"], "blocked": list(r["blocks"])})
            break
    if days_since_entry is not None:
        try:
            out["timely"] = int(days_since_entry) <= POST_ORDER_CLOCK_DAYS
        except Exception:
            out["timely"] = None
    return out


# Draft-side token contract for a (category, court): tokens the paper MUST carry / MUST NOT carry. Fed by the same
# registry; m1_filing_gate's DB-REQUIRED check covers the literal artifacts, this covers the forum's caption idiom.
DRAFT_TOKENS: Dict[tuple, Dict[str, List[str]]] = {
    ("post_order_motion", "PA_CP_PHILA"): {
        "required": ["control no.", "proposed order", "certificate of service", "verification", "sui juris"],
        "forbidden": ["ecf no.", "dkt. no.", "pro se"],   # Control No., not ECF No.; signer is sui juris (locked anatomy)
    },
}


def draft_token_defects(filing_type: str, court: str, text: str) -> List[str]:
    """Findings for a draft against DRAFT_TOKENS: 'MISSING TOKEN: x' / 'FORBIDDEN TOKEN: y'. Empty = clean."""
    low = (text or "").lower()
    c = DRAFT_TOKENS.get((category_of(filing_type), court)) or {}
    out = [f"MISSING TOKEN: {t}" for t in c.get("required", []) if t not in low]
    out += [f"FORBIDDEN TOKEN: {t}" for t in c.get("forbidden", []) if t in low]
    return out

# Map a filing type / stem -> category.
def category_of(filing_type: str) -> str:
    t = (filing_type or "").lower()
    # post-order vehicles first: "petition_341c" must never fall to the complaint/appeal buckets (S-m1-vehicle-recon)
    if re.search(r"reconsider|petition_341c|341\(?c\)?|motion_to_vacate|\bvacate", t):
        return "post_order_motion"
    if re.search(r"appellate_brief|notice_of_appeal|amended_notice_of_appeal|frap_2\d|petition_for_writ|reproduced_record", t):
        return "appeal"
    if re.search(r"complaint", t) and not re.search(r"judicial_council|1983|misconduct|odc|jcb", t):
        return "new_complaint"
    if re.search(r"opposition|response|reply", t):
        return "response"
    return "motion"


def requirements(filing_type: str, court: str) -> List[dict]:
    """Required + recommended components for this filing type at this court. Falls back to the '*'
    (any-court) entry for the category."""
    cat = category_of(filing_type)
    return REGISTRY.get((cat, court)) or REGISTRY.get((cat, "*")) or []


def checklist(filing_type: str, court: str, present_keys: List[str]) -> Dict[str, Any]:
    """Compare the required components against what the package has. present_keys = component keys already
    drafted/attached. Returns {category, required, present, missing_required, missing_conditional}."""
    reqs = requirements(filing_type, court)
    have = {(k or "").lower() for k in (present_keys or [])}

    def _has(rk):
        return any(rk in h or h in rk for h in have)

    missing_req = [r for r in reqs if r["level"] == "required" and not _has(r["key"])]
    missing_cond = [r for r in reqs if r["level"] == "conditional" and not _has(r["key"])]
    return {
        "category": category_of(filing_type),
        "court": court,
        "required": [r for r in reqs if r["level"] == "required"],
        "conditional": [r for r in reqs if r["level"] == "conditional"],
        "recommended": [r for r in reqs if r["level"] == "recommended"],
        "missing_required": missing_req,
        "missing_conditional": missing_cond,
        "complete": not missing_req,
    }


if __name__ == "__main__":
    import sys
    ft = sys.argv[1] if len(sys.argv) > 1 else "notice_of_appeal_superior_court"
    ct = sys.argv[2] if len(sys.argv) > 2 else "PA_SUPER"
    have = sys.argv[3].split(",") if len(sys.argv) > 3 else []
    r = checklist(ft, ct, have)
    print(f"category={r['category']} court={ct} complete={r['complete']}")
    print("REQUIRED:")
    for x in r["required"]:
        mark = "  [MISSING]" if x in r["missing_required"] else "  [have/ok]"
        print(f"{mark} {x['label']} — {x['cite']}")
    if r["conditional"]:
        print("CONDITIONAL (include if applicable):")
        for x in r["conditional"]:
            print(f"   - {x['label']} — {x['cite']} ({x['note']})")
