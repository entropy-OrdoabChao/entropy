"""m0 rebuttal gate — holistic, claim-by-claim attack on an adverse filing or order.

The point (per the user, 2026-06-17): m0 must look at the case AS A WHOLE and attack
EACH ground the opponent/court raises — not take the order at face value. For every
ground it:
  1. RECORD CHECK   — is the factual premise contradicted by the record? (quote it)
  2. AUTHORITY CHECK — is each cited case binding on this forum, and is it applied for
                       what it actually holds, or misapplied? (deterministic hierarchy
                       + m0 reasoning)
  3. TRAP CHECK      — would "curing" or conceding the ground damage another part of the
                       case (e.g. renaming a garnishee that severs a custodian link)?
  4. REBUTTAL        — the grounded counter, tied to the record + binding authority.

Division of labor (R7): m0 (LLM) parses the filing and reasons per ground; the binding
hierarchy and record are deterministic inputs it must ground in. Mirrors m0_appeal_gate
(same server discipline, same enable_thinking:false fix as record_rebuttal).
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import json, re
from pathlib import Path

import requests

ROOT = next((p for p in (_eroot(), _eroot())
             if (p / "cases_index").exists()), _eroot())
_CASES = ROOT / "obsidian_sync" / "cases"

import os as _os
M0_URL = _os.environ.get("M0_URL") or _os.environ.get("BEEZLE_LLM_URL", "").replace("/v1", "") or "http://localhost:8000"          # local-gpu default (faster/stable)


def _b70_gateway() -> str:
    import subprocess
    try:
        out = subprocess.run(["ip", "route"], capture_output=True, text=True, timeout=3).stdout
        m = re.search(r"default via (\S+)", out)
        if m:
            return f"http://{m.group(1)}:8080"
    except Exception:
        pass
    return "http://localhost:8080"


# --- deterministic binding hierarchy -------------------------------------------------
# For a given forum, which courts issue MANDATORY (binding) authority. Everything else
# is persuasive only. Trial-court opinions never bind. (Stare-decisis truth, not a guess.)
_BINDING = {
    "PA_CCP": ["U.S. Supreme Court", "Supreme Court of Pennsylvania",
               "Superior Court of Pennsylvania", "Commonwealth Court of Pennsylvania"],
    "PA_SUPERIOR": ["U.S. Supreme Court", "Supreme Court of Pennsylvania"],
    "EDPA": ["U.S. Supreme Court", "U.S. Court of Appeals for the Third Circuit"],
    "CA3": ["U.S. Supreme Court", "U.S. Court of Appeals for the Third Circuit"],  # own en banc
}


def _forum_of(case_id: str) -> str:
    return "EDPA" if case_id.startswith("fed") else "PA_CCP"


def _court_from_cite(cite: str) -> str:
    """Identify the issuing court from a citation string, incl. reporter abbreviations
    (so '2013 PA Super 239' -> Superior Court, '410 Pa. 436' -> PA Supreme)."""
    c = " " + (cite or "").lower() + " "
    if any(t in c for t in ("pa.r.c.p", "pa. r. civ. p", "pa.r.a.p", "pa. r. app",
                            "fed. r. civ. p", "frcp", "u.s.c", "pa.c.s", "pa. code",
                            "p.s.", "rule ")):
        return "RULE"
    if "s. ct." in c or re.search(r"\d+\s+u\.s\.\s", c):
        return "US_SUPREME"
    if "3d cir" in c or "3rd cir" in c or ("f.3d" in c and "cir" in c) or "f. app'x" in c:
        return "CA3"
    if any(t in c for t in ("f. supp", "e.d. pa", "m.d. pa", "w.d. pa")):
        return "FED_DISTRICT"
    if "pa. super" in c or "pa super" in c or "a.2d" in c and "super" in c or "a.3d" in c and "super" in c:
        return "PA_SUPERIOR"
    if "pa. cmwlth" in c or "commonwealth court" in c:
        return "PA_CMWLTH"
    if "common pleas" in c or "pa. c.p." in c or "c.c.p." in c:
        return "PA_CCP_TRIAL"
    if re.search(r"\d+\s+pa\.\s", c) or "supreme court of pennsylvania" in c:
        return "PA_SUPREME"      # e.g. "410 Pa. 436" is the PA Supreme reporter
    return "UNKNOWN"


def binding_status(court_label: str, forum: str) -> str:
    """binding | binding(rule) | persuasive | not_binding(trial-level)."""
    court = _court_from_cite(court_label)
    if court == "RULE":
        return "binding(rule)"
    if court == "US_SUPREME":
        return "binding"
    if court in ("FED_DISTRICT", "PA_CCP_TRIAL"):
        return "not_binding(trial-level)"
    if forum.startswith("PA"):
        if court in ("PA_SUPREME", "PA_SUPERIOR", "PA_CMWLTH"):
            return "binding"
        if court == "CA3":
            return "persuasive"          # federal on state procedure = persuasive
    if forum in ("EDPA", "CA3"):
        if court == "CA3":
            return "binding"
        if court in ("PA_SUPREME", "PA_SUPERIOR"):
            return "binding"             # Erie: state substantive law binds federal
    return "persuasive"


def _case_theory(case_id: str) -> str:
    slug = case_id.replace(":", "_")
    for cand in (_CASES / slug / "CASE_FACTS.md",
                 *(_CASES.glob(f"*/CASE_FACTS*.md"))):
        try:
            if cand.exists() and (slug.split("_", 1)[-1] in cand.read_text(encoding="utf-8", errors="ignore")
                                  or cand.parent.name == slug):
                return cand.read_text(encoding="utf-8", errors="ignore")[:9000]
        except Exception:
            continue
    return ""


M0_REBUT_SYSTEM = (
    "You are m0, Beezle's filing-decision reasoner, protecting a PRO SE / SUI JURIS "
    "litigant. You are given (1) the litigant's CASE AS A WHOLE (record/theory) and "
    "(2) an ADVERSE filing or order. Do NOT take the adverse document at face value. "
    "Break it into its DISCRETE grounds/claims and attack EACH one, looking at the whole "
    "case:\n"
    " - RECORD CHECK: is the ground's factual premise CONTRADICTED by the record? Quote "
    "the contradicting fact. (e.g. 'no certified mail' when the record shows certified "
    "mail; 'unaware' when the party appeared.)\n"
    " - AUTHORITY CHECK: for each case the adverse document cites, is it BINDING on this "
    "forum, and is it applied for what it actually HOLDS or MISAPPLIED? Use the "
    "binding_hierarchy provided. A strike/dismissal standard that requires a 'fatal defect "
    "on the face of the record' is MISAPPLIED when used against a merits/evidentiary "
    "dispute.\n"
    " - TRAP CHECK: would conceding or 'curing' this ground damage another part of the "
    "case? (e.g. renaming a garnishee to a narrower entity that severs a custodian link). "
    "If so, flag it and give the move that does NOT spring the trap.\n"
    " - REBUTTAL: the grounded counter, tied to the record + binding authority + the "
    "litigant's overall theory. Pro se filings are construed liberally.\n"
    "Rules come from the record/authority provided, not memory. Output STRICT JSON:\n"
    '{"grounds":[{"ground":"<the adverse ground>","record_contradiction":"<quote/none>",'
    '"authority":[{"cite":"<case>","binding":"binding|persuasive|not_binding","misapplied":true|false,"why":"<short>"}],'
    '"trap":"<trap + safe move, or none>","rebuttal":"<grounded counter>","dismissible":false}],'
    '"traps":["<case-wide traps>"],"theory_through_line":"<one line tying it to the whole case>",'
    '"order_features":{"grants_injunction":true|false,"exceeds_relief_requested":true|false,'
    '"void_or_jurisdiction_defect":true|false,"final_as_to_a_party":true|false,'
    '"is_adverse_order_not_just_motion":true|false}}'
)


def _m0_json(system: str, user: str, url: str = M0_URL, timeout: int = 360) -> dict:
    r = requests.post(f"{url}/v1/chat/completions", json={
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0.2, "max_tokens": 2600, "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},   # answer in content, not reasoning
    }, timeout=timeout)
    r.raise_for_status()
    msg = r.json()["choices"][0]["message"]
    content = (msg.get("content") or "").strip() or (msg.get("reasoning_content") or "")
    m = re.search(r"\{.*\}", content, re.S)
    return json.loads(m.group(0)) if m else {}


def select_attack_vehicle(case_id: str, feat: dict) -> dict:
    """Deterministic vehicle selection for an ADVERSE ORDER (not just a motion).
    The point m0 must encode: if the order GRANTS AN INJUNCTION it is appealable AS OF
    RIGHT — that bypasses the (hostile) trial judge and is the PRIMARY vehicle, not
    reconsideration. A VOID / excess-of-authority order is attackable ANY TIME by motion
    to vacate. Reconsideration is a discretionary HEDGE that does NOT toll the appeal
    clock unless expressly granted — never the primary."""
    feat = feat or {}
    pa = _forum_of(case_id).startswith("PA")
    appeal_rule = "Pa.R.A.P. 311(a)(4)" if pa else "28 U.S.C. § 1292(a)(1)"
    appeal_court = "Superior Court of Pennsylvania" if pa else "Third Circuit"
    appeal_stub = "notice_of_appeal_superior_court" if pa else "notice_of_appeal"
    vehicles = []

    # --- TRIAL-COURT vehicles FILE FIRST (build the record). An appeal divests the
    # trial court of jurisdiction (Pa.R.A.P. 1701 / fed equivalent), so anything you want
    # the trial court to do must be done BEFORE the notice of appeal. ---
    if feat.get("void_or_jurisdiction_defect") or feat.get("exceeds_relief_requested"):
        vehicles.append({
            "vehicle": "motion_to_vacate_void", "filing_order": 1,
            "rule": "PA void-order doctrine" if pa else "Fed. R. Civ. P. 60(b)(4)",
            "goes_through_trial_judge": True, "stub": "motion_to_vacate_void_order",
            "why": "FILE FIRST. A void/excess order is attackable any time; letting the trial "
                   "court DENY it strengthens the appeal (court had the chance to cure and refused), "
                   "and a denial 'refusing to dissolve' the injunction is itself appealable under "
                   + appeal_rule + "."})
    vehicles.append({
        "vehicle": "reconsideration_hedge", "filing_order": 1, "hedge": True,
        "rule": "Pa.R.A.P. 1701(b)(3)" if pa else "local rule",
        "goes_through_trial_judge": True, "stub": "motion_for_reconsideration",
        "why": "parallel trial-court hedge; discretionary, denial not appealable, does NOT toll "
               "the appeal clock unless expressly granted — never rely on it."})

    # --- APPEAL goes LAST (it freezes the court below). Strongest vehicle, filed last. ---
    if feat.get("grants_injunction"):
        vehicles.append({
            "vehicle": "appeal_as_of_right_injunction", "filing_order": 99, "file_last": True,
            "rule": appeal_rule, "court": appeal_court, "goes_through_trial_judge": False,
            "stub": appeal_stub, "ultimate": True,
            "why": "STRONGEST (bypasses the trial judge) but FILE LAST — an appeal divests the "
                   "trial court of jurisdiction (Pa.R.A.P. 1701), halting the trial-court motions. "
                   "Appeal the DENIAL (refusal to dissolve the injunction) under " + appeal_rule +
                   " for a fresh clock + stronger record.",
            "timing": "PROTECTIVE BACKSTOP: if the trial court has not ruled by ~day 25 of the "
                      "original order's appeal period, file the notice of appeal of the original "
                      "order before the 30-day deadline to preserve the right."})
    elif feat.get("final_as_to_a_party"):
        vehicles.append({
            "vehicle": "appeal_as_of_right_final", "filing_order": 99, "file_last": True,
            "rule": "Pa.R.A.P. 341" if pa else "28 U.S.C. § 1291", "court": appeal_court,
            "goes_through_trial_judge": False, "stub": appeal_stub, "ultimate": True,
            "why": "final order appealable as of right; file LAST (divests trial-court jurisdiction)."})

    vehicles.sort(key=lambda v: v.get("filing_order", 50))
    file_first = next((v for v in vehicles if v.get("filing_order") == 1), vehicles[0] if vehicles else None)
    ultimate = next((v for v in vehicles if v.get("ultimate")), None)
    return {
        "file_first": file_first, "ultimate": ultimate, "all": vehicles,
        "stubs": [v["stub"] for v in vehicles],
        "sequence_note": ("Trial-court motions (vacate first, reconsideration in parallel) → "
                          "let them be DENIED → notice of appeal LAST. Rationale: an appeal "
                          "divests the trial court of jurisdiction (Pa.R.A.P. 1701), so file all "
                          "trial-court attacks before appealing. Protective backstop: appeal by "
                          "day 30 if the trial court has not ruled."),
        "primary": file_first}     # back-compat: 'primary' = what to file first


def rebut_adverse(case_id: str, adverse_text: str, *, url: str = M0_URL) -> dict:
    forum = _forum_of(case_id)
    payload = {
        "case_id": case_id,
        "forum": forum,
        "binding_hierarchy": {forum: _BINDING.get(forum, [])},
        "case_as_a_whole": _case_theory(case_id),
        "adverse_document": adverse_text[:14000],
    }
    try:
        out = _m0_json(M0_REBUT_SYSTEM, json.dumps(payload, default=str), url=url)
    except Exception as e:
        return {"source": "fallback", "error": f"m0 offline ({e})", "grounds": []}
    # deterministic pass: stamp binding_status on every cited authority
    for g in out.get("grounds", []) or []:
        for a in g.get("authority", []) or []:
            a["binding_status_checked"] = binding_status(a.get("cite", ""), forum)
    # deterministic: pick the correct attack vehicle from the order's features
    out["attack_vehicle"] = select_attack_vehicle(case_id, out.get("order_features", {}))
    out["source"] = "m0"
    out["forum"] = forum
    return out


if __name__ == "__main__":
    import sys
    cid = sys.argv[1] if len(sys.argv) > 1 else "phila_ccp:260100000"
    doc_path = sys.argv[2] if len(sys.argv) > 2 else None
    txt = Path(doc_path).read_text(encoding="utf-8", errors="ignore") if doc_path else \
        "TEST: motion to strike on misnomer + no certified mail + improper interrogatories."
    print(json.dumps(rebut_adverse(cid, txt), indent=1, default=str))
