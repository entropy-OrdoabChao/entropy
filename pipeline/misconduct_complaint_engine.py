"""misconduct_complaint_engine — models a DOCKETED judicial-misconduct complaint so m0
stops treating each §351 as a fresh filing and instead:

  1. RECUSAL SUPPORT  — a docketed complaint against the presiding judge corroborates
     28 U.S.C. § 455(a) ("impartiality might reasonably be questioned"). It is NOT
     self-sufficient (a litigant can't manufacture recusal by filing a complaint), so the
     engine returns the corroboration paired with the required caveat: it must ride on the
     documented record (the scorecard pattern).
  2. SUPPLEMENT ROUTING — once a complaint is docketed, the right move is to SUPPLEMENT that
     docket (reference the number) and cc the circuit, NOT file a duplicate. Federal judges
     -> circuit Judicial Council (Office of the Circuit Executive); state judges -> the state
     judicial conduct board.
  3. NO-DUPLICATE GATE — for any §351/misconduct stem, if a complaint is already docketed,
     the action becomes SUPPLEMENT (ref docket) instead of FILE.

Data-driven (R2): everything is read from the CASE_FACTS "JUDICIAL MISCONDUCT COMPLAINT"
block — never a hardcoded judge/case. Produced upstream by litigation_posture so it flows
into the audit JSON (single source of truth)."""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import re
from pathlib import Path

ROOT = next((p for p in (_eroot(), _eroot()) if (p / "cases_index").exists()),
            _eroot())
_CASES = ROOT / "obsidian_sync" / "cases"

# misconduct stems this engine governs (the ones that must become SUPPLEMENT once docketed)
_MISCONDUCT_STEMS = ("judicial_council_complaint", "28_usc_351", "_351", "judicial_misconduct")


def _case_facts(case_id):
    digits = re.findall(r"\d{4,}", str(case_id))
    for cf in _CASES.glob("*/CASE_FACTS*.md"):
        if cf.name != "CASE_FACTS.md":          # canonical file only (skip .from_*/.bak/.placeholder)
            continue
        if digits and any(d in cf.parent.name for d in digits):
            return cf.read_text(encoding="utf-8", errors="ignore")
    return ""


def _field(txt, label):
    m = re.search(r"\*\*" + label + r"\*\*\s*:\s*(.+)", txt, re.I)
    return m.group(1).strip() if m else ""


def is_federal(case_id):
    return str(case_id).lower().startswith("fed") or "cv-" in str(case_id).lower()


def complaint_for(case_id):
    """Return the docketed misconduct complaint recorded for this case, or {} if none."""
    cf = _case_facts(case_id)
    if "JUDICIAL MISCONDUCT COMPLAINT" not in cf:
        return {}
    num = re.search(r"J\.?C\.?\s*No\.?\s*[\d\-]+", cf)
    if not num:
        return {}
    return {
        "number": num.group(0),
        "forum": _field(cf, "Forum"),
        "docketed": _field(cf, "Docketed"),
        "scope": _field(cf, "Scope"),
        "history": _field(cf, "History"),
        "exhibit": _field(cf, "Exhibit"),
    }


def supplement_routing(case_id):
    """Where a supplement goes + the cc, plus the supplement-not-duplicate rule."""
    c = complaint_for(case_id)
    if not c:
        return {}
    if is_federal(case_id):
        dest = ("Office of the Circuit Executive, U.S. Court of Appeals for the Third Circuit "
                "(601 Market St, 22409 U.S. Courthouse, Philadelphia PA 19106-1790)")
        cc = "Clerk / Chief Judge, U.S. Court of Appeals for the Third Circuit"
    else:
        dest = "Pennsylvania Judicial Conduct Board (Pa. Judicial Center, 601 Commonwealth Ave, Suite 3500, P.O. Box 62525, Harrisburg PA 17106-2525)"
        cc = "President Judge of the Court of Common Pleas (administrative notice)"
    return {"number": c["number"], "destination": dest, "cc": cc,
            "rule": f"SUPPLEMENT {c['number']} (reference the docket; transmit the scorecard + new findings) — do NOT file a duplicate complaint"}


def recusal_support(case_id):
    """§455(a) corroboration prose + the mandatory caveat. Empty if no docketed complaint."""
    c = complaint_for(case_id)
    if not c:
        return {}
    return {
        "number": c["number"],
        "ground": "28 U.S.C. § 455(a)" if is_federal(case_id)
                  else "Pa. Code of Judicial Conduct Rule 2.11 / appearance of impropriety",
        "corroboration": (
            f"The presiding judge is the subject of a DOCKETED complaint of judicial misconduct "
            f"({c['number']}), filed by this litigant and accepted for processing by the "
            f"{c['forum'] or 'circuit'}. A reasonable observer aware of a docketed misconduct "
            f"complaint, together with the record pattern, would question the court's impartiality."),
        "caveat": ("A pending complaint, standing alone, does not compel disqualification — a "
                   "litigant cannot manufacture recusal by filing one. It must be presented "
                   "WITH the documented record (the compliance scorecard pattern) as corroboration."),
    }


# ---- forward direction: detect a trigger, decide the complaint, resolve where/to whom ----
import json

# institutional routing (forum address, not judge/party data — R2-safe). [verify ODC on filing]
RECIPIENTS = {
    ("judge", "federal"): {
        "complaint": "Complaint of Judicial Misconduct (28 U.S.C. § 351)",
        "recipient": "Office of the Circuit Executive, U.S. Court of Appeals for the Third Circuit",
        "address": "601 Market Street, 22409 U.S. Courthouse, Philadelphia, PA 19106-1790",
        "cc": "Clerk / Chief Judge, U.S. Court of Appeals for the Third Circuit"},
    ("judge", "state"): {
        "complaint": "Complaint to the Judicial Conduct Board (Pa. Code of Judicial Conduct)",
        "recipient": "Pennsylvania Judicial Conduct Board",
        "address": "Pennsylvania Judicial Center, 601 Commonwealth Ave, Suite 3500, P.O. Box 62525, Harrisburg, PA 17106-2525",
        "cc": ""},
    ("attorney", None): {
        "complaint": "Complaint to the Office of Disciplinary Counsel (Pa. RPC)",
        "recipient": "Pennsylvania Office of Disciplinary Counsel, District I",
        "address": "Seven Penn Center, 1635 Market Street, Suite 1300, Philadelphia, PA 19103 [verify]",
        "cc": ""},
}

# audit signals that TRIGGER each complaint (grounded in the real audit vocabulary)
_JUDGE_TRIGGERS = {"PROCEDURAL_DUE_PROCESS_VIOLATION", "PRIMA_FACIE_EVIDENCE_IGNORED",
                   "VOID_JUDGMENT_RULE_60B4", "FRAUD_ON_THE_COURT"}
_ATTORNEY_TRIGGERS = {"VERIFICATION_MISSING"}
_STARE_MISUSE = {"NON_BINDING_AS_CONTROLLING", "TRIAL_COURT_AS_APPELLATE_AUTHORITY"}
_STARE_PATTERN_MIN = 10            # systematic stare-decisis misuse => bias indicator


def _audit(case_id):
    digits = re.findall(r"\d{4,}", str(case_id))
    aud = ROOT / "output" / "audit"
    for p in aud.glob("data_*.json"):
        if any(d in p.stem for d in digits):
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return {}
    return {}


def recommend_complaints(case_id, posture=None):
    """Forward trigger->route: read the audit findings, decide which misconduct complaints are
    warranted, and resolve the recipient + address. Returns [] when no trigger fires.
    `posture` lets the upstream producer pass its freshly-built litigation_posture (avoids a
    stale read while the audit JSON is being rewritten)."""
    d = _audit(case_id)
    if not d:
        return []
    sc = (d.get("scorecard") or {}).get("violations", []) or []
    pv = d.get("procedural_violations", []) or []
    lp = posture if posture is not None else (d.get("litigation_posture") or {})
    proc = {(v.get("violation_type") or "").upper() for v in pv}
    cats = {(v.get("category") or "").upper() for v in sc}
    stare_ct = sum(1 for v in sc if (v.get("category") or "").upper() in _STARE_MISUSE)
    fed = is_federal(case_id)

    out = []
    # 1) judge misconduct
    jt = sorted((proc & _JUDGE_TRIGGERS)
                | {c for c in cats if c.startswith("BIAS_")}
                | ({"CONTESTED_VOID_ORDER"} if lp.get("contested_void_orders") else set())
                | ({f"SYSTEMATIC_STARE_MISUSE({stare_ct})"} if stare_ct >= _STARE_PATTERN_MIN else set()))
    if jt:
        r = dict(RECIPIENTS[("judge", "federal" if fed else "state")])
        act = filing_action(case_id, "judicial_council_complaint_28_usc_351") if fed else {"action": "FILE"}
        r.update({"target": "presiding judge", "forum": "federal" if fed else "state",
                  "triggers": jt, "action": (act or {}).get("action", "FILE"),
                  "reference": (act or {}).get("reference")})
        out.append(r)
    # 2) attorney misconduct
    at = sorted(proc & _ATTORNEY_TRIGGERS)
    if at:
        r = dict(RECIPIENTS[("attorney", None)])
        r.update({"target": "opposing counsel", "forum": "state", "triggers": at, "action": "FILE",
                  "reference": None})
        out.append(r)
    return out


def filing_action(case_id, stem):
    """For a misconduct/§351 stem: SUPPLEMENT (ref docket) if one is docketed, else FILE."""
    s = stem.lower()
    if not any(tok in s for tok in _MISCONDUCT_STEMS):
        return None
    c = complaint_for(case_id)
    if c:
        return {"action": "SUPPLEMENT", "reference": c["number"],
                "note": f"a complaint is already docketed ({c['number']}); supplement it + cc the circuit, do not duplicate"}
    return {"action": "FILE", "reference": None, "note": "no docketed complaint on record — file fresh"}


if __name__ == "__main__":
    import sys
    for cid in (sys.argv[1:] or ["fed:2:26-cv-00123", "fed:2:26-cv-00456",
                                 "fed:2:26-cv-00789", "phila_ccp:260100000"]):
        c = complaint_for(cid)
        print(f"\n=== {cid} ===")
        if not c:
            print("  (no docketed misconduct complaint recorded)")
            continue
        print(f"  complaint: {c['number']}  docketed: {c['docketed'][:40]}")
        r = supplement_routing(cid)
        print(f"  supplement -> {r['destination'][:70]}")
        print(f"        cc -> {r['cc']}")
        print(f"      rule: {r['rule'][:90]}")
        rs = recusal_support(cid)
        print(f"  recusal ground: {rs['ground']}")
        print(f"  caveat: {rs['caveat'][:90]}")
