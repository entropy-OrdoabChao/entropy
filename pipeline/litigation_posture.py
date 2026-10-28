"""litigation_posture — ANALYSIS-LAYER producer of the case's litigation posture, so the
facts that downstream engines (m0, dependency, posture, conflict, drafter) need are
DETECTED/RECORDED once, upstream, and flow down via the audit JSON (single source of truth).

It consolidates, per case:
  - default_status        : who defaulted (incl. untimely answers) vs answered timely
  - admissions_by_conduct : e.g., defendant removed tradeline + reimbursed -> SJ basis
  - contested_void_orders : adverse orders the litigant attacks as VOID (not a bar)
  - clerk_nonfiling       : filings missing from the docket due to clerk error
  - summary_judgment_basis: prima facie + stare decisis present

Sources: the CASE_FACTS structured blocks (litigant ground truth) + the audit JSON signals
(PRIMA_FACIE_EVIDENCE_IGNORED, dismissals). Writes `litigation_posture` into the audit JSON.

THE RULE (per 2026-06-17): every fact m0 relies on must be produced HERE so it can't slip
through the cracks and so an m0 requirement trickles upstream into analysis.
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import json, re
from pathlib import Path

ROOT = next((p for p in (_eroot(), _eroot()) if (p / "cases_index").exists()),
            _eroot())
_AUDIT = ROOT / "output" / "audit"
_CASES = ROOT / "obsidian_sync" / "cases"


def _case_facts(case_id):
    digits = re.findall(r"\d{4,}", case_id)
    for cf in _CASES.glob("*/CASE_FACTS*.md"):
        if any(d in cf.parent.name for d in digits):
            return cf.read_text(encoding="utf-8", errors="ignore")
    return ""


def _audit_path(case_id):
    digits = re.findall(r"\d{4,}", case_id)
    for p in _AUDIT.glob("data_*.json"):
        if any(d in p.stem for d in digits):
            return p
    return None


def _line_after(txt, label):
    m = re.search(label + r":\**\s*(.+)", txt, re.I)
    return m.group(1).strip() if m else ""


def extract(case_id, write=True):
    cf = _case_facts(case_id)
    posture = {
        "default_status": {
            "defaulted": _line_after(cf, "DEFAULTED"),
            "not_in_default": _line_after(cf, "NOT in default"),
        },
        "admissions_by_conduct": bool(re.search(r"removed.*tradeline|reimbursed|admission by conduct", cf, re.I)),
        "contested_void_orders": bool(re.search(r"contested as void|challenged as void|void order", cf, re.I)),
        "clerk_nonfiling": bool(re.search(r"clerk non-?filing|clerk error|did(n.t| not) (file|docket)", cf, re.I)),
        "summary_judgment_basis": False,
        # ── Non-conceding dismissal stance (2026-06-21). Standing STRATEGY posture (not case-
        #    specific judge/party data — R2-safe): the litigant concedes NO dismissal as valid.
        #    Every claim is corpus-backed; an adverse 12(b)(6)/void dismissal is challenged as
        #    ERROR, and m0 FINDS the controlling/appellate authority (corpus + Obsidian graph)
        #    that restores it. The drafter must never frame a claim as the "sole surviving" one
        #    or treat a dismissal as accepted fact.
        "concede_dismissals": False,
        "dismissals_are_error": True,
        "all_claims_corpus_backed": True,
    }
    # misconduct-complaint escalation trickles upstream so m0/drafter see a DOCKETED complaint
    # (recusal corroboration + supplement-not-duplicate routing). Data-driven via the engine.
    try:
        try:
            from pipeline.misconduct_complaint_engine import complaint_for, recusal_support, recommend_complaints
        except ImportError:
            from misconduct_complaint_engine import complaint_for, recusal_support, recommend_complaints
        mc = complaint_for(case_id)
        if mc:
            posture["misconduct_complaint"] = mc
            posture["recusal_support"] = recusal_support(case_id)
        # forward trigger->route: which complaints are warranted + where/to whom they go
        recs = recommend_complaints(case_id, posture=posture)
        if recs:
            posture["complaint_recommendations"] = recs
    except Exception:
        pass
    ap = _audit_path(case_id)
    if ap:
        try:
            d = json.loads(ap.read_text(encoding="utf-8"))
        except Exception:
            d = {}
        pv = d.get("procedural_violations") or []
        prima = sum(1 for v in pv if "PRIMA_FACIE" in (v.get("violation_type") or "").upper())
        sc = (d.get("scorecard") or {}).get("violations", []) or []
        stare = sorted({v.get("category", "") for v in sc}
                       & {"NON_BINDING_AS_CONTROLLING", "TRIAL_COURT_AS_APPELLATE_AUTHORITY"})
        posture["summary_judgment_basis"] = bool(prima and stare) or posture["admissions_by_conduct"]
        posture["prima_facie_count"] = prima
        posture["stare_decisis_misuse"] = stare
        # Reversibility-flagged dismissals already detected and stored in the audit's `dismissal`
        # array (conclusory dismissal w/o element analysis, leave-to-amend offered, reversibility
        # score). This data EXISTED but wasn't being consumed — surface it so the posture + drafter
        # contest each dismissal instead of conceding a "sole surviving claim." Data-driven (R2).
        dz = d.get("dismissal") or []
        rev = [x for x in dz if isinstance(x, dict) and (
                (x.get("score") or 0) >= 50
                or "LEAVE TO AMEND" in (x.get("summary") or "").upper()
                or "CONCLUSORY" in (x.get("summary") or "").upper())]
        posture["reversible_dismissals"] = [
            {"summary": (x.get("summary") or "")[:240], "score": x.get("score")} for x in rev[:6]]
        # data-driven corroboration that dismissals are being contested: prima facie evidence was
        # ignored, void orders challenged, and/or reversibility-flagged dismissals exist. m0 still
        # FINDS the reversing authority.
        posture["dismissals_contested"] = bool(prima) or posture["contested_void_orders"] or bool(rev)
        if write:
            d["litigation_posture"] = posture
            ap.write_text(json.dumps(d, indent=2), encoding="utf-8")
            posture["_written_to"] = ap.name
    return posture


if __name__ == "__main__":
    import sys
    for cid in (sys.argv[1:] or ["fed:2:26-cv-00123", "fed:2:26-cv-00456",
                                 "fed:2:26-cv-00789", "phila_ccp:260100000"]):
        p = extract(cid, write=True)
        print(f"\n{cid}")
        print(f"  defaulted: {p['default_status']['defaulted'][:80]}")
        print(f"  not_in_default: {p['default_status']['not_in_default'][:60]}")
        print(f"  admissions_by_conduct: {p['admissions_by_conduct']} | SJ basis: {p['summary_judgment_basis']} "
              f"| void-orders: {p['contested_void_orders']} | clerk-nonfiling: {p['clerk_nonfiling']}")
        print(f"  written: {p.get('_written_to', '(no audit file)')}")
