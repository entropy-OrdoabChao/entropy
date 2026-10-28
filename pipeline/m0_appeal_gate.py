"""m0 appeal-vehicle gate — m0 decides the CORRECT appeal vehicle per case.

The appeal rules are clear and deterministic; choosing the vehicle should NOT be a
manual call. Given the case posture (finality, dismissals, judgment, pending
appeals, void indicators), m0 — the LLM reasoner with the corpus — decides:
interlocutory (§1292(b) fed / §702(b) PA) vs Rule 54(b) partial-final vs FULL
appeal as of right (§1291 → Third Circuit / Pa.R.A.P. 341 → Superior Court);
whether the judgment is VOID (attackable any time, no time bar); which higher
court; and whether to also file a certify HEDGE.

Routed to the local-gpu (:8000) — ALL legal inference on the local-gpu (user-locked 2026-08-27); local-gpu is Hermes chat lane. Serialize behind the per-case lock rather than cross onto
local-gpu (:8000). Both servers host the same local-model model. Same protective
fallback discipline as the amendment gate.
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import json, re
from pathlib import Path

import requests

ROOT = _eroot()
_CASES = ROOT / "obsidian_sync" / "cases"


# Default to the local-gpu (:8000) — faster + more stable, and WSL-reachable. local-gpu is
# only the contingency for when the local-gpu is busy with a drafter run; a caller can
# pass url=_b70_gateway() in that case. (They need not run at the same time.)
import os as _os
M0_URL = _os.environ.get("M0_URL") or _os.environ.get("BEEZLE_LLM_URL", "").replace("/v1", "") or "http://localhost:8000"


def _b70_gateway() -> str:
    """local-gpu gateway address for SWARM PROFILES ONLY (env-selected; this profile uses :8000):
    bridge from WSL, and the gateway may rotate). Use only when the local-gpu is busy."""
    import subprocess
    try:
        out = subprocess.run(["ip", "route"], capture_output=True, text=True, timeout=3).stdout
        m = re.search(r"default via (\S+)", out)
        if m:
            return f"http://{m.group(1)}:8080"
    except Exception:
        pass
    return "http://localhost:8080"

from pipeline.m0_amendment_gate import _gt_for, _load_audit, _gather_signals  # reuse


def _slug(case_id: str) -> str:
    return case_id.replace(":", "_")


def _casefacts(case_id: str) -> str:
    for d in sorted(_CASES.glob("*/")):
        cf = d / "CASE_FACTS.md"
        if cf.exists() and _slug(case_id).split("_", 1)[-1] in cf.read_text(encoding="utf-8", errors="ignore"):
            return cf.read_text(encoding="utf-8", errors="ignore")
    p = _CASES / _slug(case_id) / "CASE_FACTS.md"
    return p.read_text(encoding="utf-8", errors="ignore") if p.exists() else ""


_VOID_TYPES = {"PROCEDURAL_DUE_PROCESS_VIOLATION", "SUBJECT_MATTER_JURISDICTION_DEFECT",
               "VOID_JUDGMENT_RULE_60B4", "STANDING_DEFECT", "FRAUD_ON_THE_COURT",
               "REMOVAL_DEFECTIVE"}


def _appeal_posture(case_id: str) -> dict:
    gt = _gt_for(case_id)
    sig = _gather_signals(gt)
    audit = _load_audit(case_id)
    cf = _casefacts(case_id).lower()
    proc = audit.get("procedural_violations") or []
    void_hits = sorted({(v.get("violation_type") or "") for v in proc} & _VOID_TYPES)
    federal = case_id.startswith("fed")
    return {
        "case_id": case_id,
        "court_type": "federal_district" if federal else "pa_common_pleas",
        "higher_court": "U.S. Court of Appeals for the Third Circuit" if federal
                        else "Superior Court of Pennsylvania",
        "trial_judge": (gt.get("judge") or {}).get("name") if isinstance(gt.get("judge"), dict) else gt.get("judge"),
        "with_prejudice": sig.get("with_prejudice"),
        "without_prejudice_leave": sig.get("without_prejudice_leave"),
        "pending_challenge_motions": sig.get("pending_challenge_motions"),
        "appeal_number": sig.get("appeal_number"),
        # finality signals scraped from CASE_FACTS
        "judgment_entered": any(k in cf for k in ("judgment entered", "default judgment entered", "certified")),
        "notice_of_appeal_filed": "notice of appeal" in cf,
        "rule_54b_denied": "54(b) denied" in cf or "54b denied" in cf,
        "amend_deadline_open": "amend by" in cf or "amend deadline" in cf or "must amend" in cf,
        # injunction signal — § 1292(a)(1) is available ONLY if the record has an injunction order
        "has_injunction": any(k in cf for k in ("injunction", "enjoin", "restrain",
                              "temporary restraining", "1292(a)")),
        "void_indicator_types": void_hits,
        "void_indicator_count": sum(1 for v in proc if (v.get("violation_type") or "") in _VOID_TYPES),
        # ---- RECORD-ON-APPEAL DELINQUENCY (added 2026-09-29, Lippi/0000 EDA 2026) ----
        # An appeal can stall before any brief is due because the TRIAL COURT never
        # transmitted the record. That is not the appellant's defect and it is not cured
        # by re-filing anything in the trial court: Pa.R.A.P. 1931(b) puts the duty on the
        # trial judge and Pa.R.A.P. 1931(c) puts transmission on the trial court clerk, so
        # no step of it is the appellant's. The enforcement rule is APPELLATE — Pa.R.A.P. 1935 —
        # so the gate must see the delinquency to route to it.
        **_record_delinquency(case_id, federal),
    }


def _record_delinquency(case_id: str, federal: bool) -> dict:
    """Is the record on appeal overdue, and are the enforcement windows blown?

    Pa.R.A.P. 1931(a)(1): the record "shall be transmitted to the appellate court within
    60 days after the filing of the notice of appeal."  Pa.R.A.P. 1925(a)(1): the judge
    "shall within the period set forth in Pa.R.A.P. 1931(a)(1) file of record at least a
    brief opinion."  Pa.R.A.P. 1935(a): the APPELLATE prothonotary "shall give written
    notice to the trial court judge of any delinquency" within ten days after the date
    for filing the record, copy to the president judge.

    Deterministic date arithmetic only — the dates come from CASE_TIMELINE, never from
    the model, and never from a clerk. Returns {} for federal matters (FRAP 11 differs).
    """
    out = {"record_delinquent": False, "record_due": None, "record_days_late": 0,
           "opinion_1925a_filed": None, "proth_1935_window_closed": False,
           "record_transmitted": None}
    if federal:
        return out
    try:
        import datetime as _dt
        tl = _CASES / _slug(case_id) / "CASE_TIMELINE.json"
        if not tl.exists():
            return out
        d = json.loads(tl.read_text(encoding="utf-8", errors="ignore"))
        ev = d.get("timeline") or d.get("events") or []
        noa = None
        for e in ev:
            t = f"{e.get('event','')} {e.get('description','')}".lower()
            if "notice of appeal" in t and "deadline" not in t and e.get("date"):
                cand = str(e["date"])[:10]
                if noa is None or cand < noa:
                    noa = cand
        if not noa:
            return out
        blob = json.dumps(ev).lower()
        due = _dt.date.fromisoformat(noa) + _dt.timedelta(days=60)
        today = _dt.date.today()
        transmitted = any(k in blob for k in ("record transmitted", "record filed in the superior",
                                              "certified record received", "briefing schedule activated"))
        opinion = ("1925(a) opinion filed" in blob or "opinion filed of record" in blob)
        out.update({
            "record_due": due.isoformat(),
            "record_transmitted": transmitted,
            "opinion_1925a_filed": opinion,
            "record_delinquent": (not transmitted) and today > due,
            "record_days_late": max(0, (today - due).days) if not transmitted else 0,
            # 1935(a) gives the appellate prothonotary 10 days after the record due date
            "proth_1935_window_closed": (not transmitted) and today > due + _dt.timedelta(days=10),
        })
    except Exception:
        pass
    return out


# Canonical jurisdictional basis per (court_type, vehicle). Reference data — a statute lookup table,
# the deterministic complement to the LLM's vehicle DECISION. The drafter reads this so its Jurisdictional
# Statement cites the exact basis the gate chose (§ 1291 vs § 1292(a)(1) vs collateral vs § 1292(b) …).
APPEAL_JURISDICTION = {
    # ---- RECORD ENFORCEMENT (not a merits vehicle; it makes the appeal MOVE) ----
    # Added 2026-09-29. When the trial court is delinquent in transmitting the record, the
    # appellant does not brief and does not re-file in the trial court. Pa.R.A.P. 1935 puts
    # the duty on the APPELLATE prothonotary and the escalation on the Court Administrator,
    # so the vehicle is a Pa.R.A.P. 123 application addressed to the appellate court asking
    # it to enforce its own rule. No fee, no trial-court clerk, no e-filing menu code.
    ("pa_common_pleas", "record_transmission_enforcement_1935"):
        ("Pa.R.A.P. 1935 and Pa.R.A.P. 123",
         "to compel transmission of a delinquent record on appeal: Pa.R.A.P. 1931(a)(1) fixes "
         "transmission within 60 days after the filing of the notice of appeal; Pa.R.A.P. 1925(a)(1) "
         "requires the trial judge to file an opinion within that same period; Pa.R.A.P. 1935(a) "
         "directs the prothonotary of the appellate court to give written notice of any delinquency "
         "to the trial judge, with a copy to the president judge, within ten days after the date for "
         "filing the record, and Pa.R.A.P. 1935(b) directs escalation to the Administrative Office "
         "and a report of the neglect or refusal to the Supreme Court; relief is sought by "
         "application under Pa.R.A.P. 123. Pa.R.A.P. 1931(b) places the duty to prepare and transmit "
         "the record on the trial judge and Pa.R.A.P. 1931(c) places transmission on the trial court "
         "clerk, so no part of that duty belongs to the appellant, and "
         "Pa.R.A.P. 1701(b)(1) preserves the trial court's power to cause the record to be "
         "transcribed, approved, filed and transmitted notwithstanding the appeal"),

    ("federal_district", "full_appeal_as_of_right"):
        ("28 U.S.C. § 1291", "from a final judgment or order that disposes of all claims as to all parties"),
    ("federal_district", "interlocutory_1292a_injunction"):
        ("28 U.S.C. § 1292(a)(1)",
         "as of right from an interlocutory order granting, continuing, modifying, refusing, or dissolving "
         "an injunction — no district-court certification is required"),
    ("federal_district", "collateral_order"):
        ("28 U.S.C. § 1291 and the collateral-order doctrine (Cohen v. Beneficial Indus. Loan Corp., "
         "337 U.S. 541 (1949))",
         "from a collateral order that conclusively determines a disputed question separate from the merits "
         "and is effectively unreviewable on appeal from a final judgment"),
    ("federal_district", "interlocutory_1292b"):
        ("28 U.S.C. § 1292(b)", "from a controlling question of law the district court has certified"),
    ("federal_district", "rule_54b"):
        ("28 U.S.C. § 1291 (partial final judgment entered under Fed. R. Civ. P. 54(b))",
         "from a partial final judgment the district court directed be entered under Rule 54(b)"),
    ("pa_common_pleas", "full_appeal_as_of_right"):
        ("42 Pa.C.S. § 742 and Pa.R.A.P. 341", "from a final order"),
    ("pa_common_pleas", "interlocutory_1292a_injunction"):
        ("Pa.R.A.P. 311(a)(4)",
         "as of right from an interlocutory order granting, continuing, modifying, refusing, or dissolving "
         "an injunction"),
    ("pa_common_pleas", "interlocutory_attachment"):
        ("Pa.R.A.P. 311(a)(2)",
         "as of right from an order dissolving, confirming, or refusing to dissolve an attachment, "
         "garnishment, or similar matter affecting the possession or control of property"),
    ("pa_common_pleas", "collateral_order"):
        ("Pa.R.A.P. 313", "from a collateral order"),
    ("pa_common_pleas", "interlocutory_702b"):
        ("Pa.R.A.P. 1311 and 42 Pa.C.S. § 702(b)", "by permission from an interlocutory order the trial court certified"),
}

# drafter court id -> gate court_type
_COURT_TO_TYPE = {"3D_CIR": "federal_district", "SCOTUS": "federal_district", "PA_SUPER": "pa_common_pleas"}


def jurisdiction_cite(primary_vehicle, court):
    """(basis_cite, phrase) for a vehicle. `court` may be a drafter id (3D_CIR/PA_SUPER) or a gate
    court_type. Falls back to the final-appeal basis for that court."""
    ct = _COURT_TO_TYPE.get(court, court)
    v = primary_vehicle or "full_appeal_as_of_right"
    return (APPEAL_JURISDICTION.get((ct, v))
            or APPEAL_JURISDICTION.get((ct, "full_appeal_as_of_right"))
            or ("28 U.S.C. § 1291", "from a final judgment or order"))


def detect_appeal_type_deterministic(stub_text, case_id=""):
    """DETERMINISTIC fallback for the vehicle when the LLM gate decision is not cached (the LLM is the
    primary decider). Reads explicit stub signals, the order being appealed, and case finality."""
    low = (stub_text or "").lower()
    m = re.search(r"appeal[_ ]type\s*[:=]\s*([a-z_0-9()]+)", low)
    if m:
        t = m.group(1)
        if "injunction" in t or "1292(a)" in t or t == "interlocutory":
            return "interlocutory_1292a_injunction"
        if "collateral" in t:
            return "collateral_order"
        if "1292(b)" in t or "permission" in t or "702" in t:
            return "interlocutory_1292b"
        if "54" in t:
            return "rule_54b"
        if "final" in t:
            return "full_appeal_as_of_right"
    # RECORD-ON-APPEAL DELINQUENCY (2026-09-29, Lippi/0000 EDA 2026). An instrument whose own
    # subject is the MISSING RECORD is not a merits vehicle — there is nothing to brief until the
    # record is transmitted, and the whole transmission duty sits with the lower court under
    # Pa.R.A.P. 1931(b)-(c). Checked before the merits branches so a record-enforcement stub never gets dressed
    # as an appeal as of right. PA only: FRAP 11 puts this duty elsewhere.
    if not (case_id or "").startswith("fed") and re.search(
            r"transmi\w*\s+(?:of\s+)?(?:the\s+)?record|record\s+(?:on\s+appeal\s+)?(?:has\s+)?"
            r"(?:not|never)\s+been\s+transmitted|delinquen\w+\s+record|record\s+delinquen\w+|"
            r"compel\w*\s+(?:the\s+)?(?:transmi\w*|certification)\s+(?:of\s+)?(?:the\s+)?record|"
            r"\b1931\b|\b1935\b", low):
        return "record_transmission_enforcement_1935"
    # order dissolving a writ / attachment / garnishment -> appealable as of right (Pa.R.A.P. 311(a)(2)
    # in PA). Check this BEFORE the generic injunction test so a dissolved writ maps to the right rule.
    if re.search(r"dissolv\w*\s+(?:the\s+)?(?:writ|attachment|garnishment)|writ (?:of )?(?:execution|"
                 r"attachment) (?:dissolv|dissolut)|garnishment dissolv", low):
        return "interlocutory_attachment"
    if re.search(r"\b1292\(a\)|filing injunction|enjoin\w*|pre-?filing (?:order|injunction)|anti-?filing|"
                 r"injunction against", low):
        return "interlocutory_1292a_injunction"
    if re.search(r"collateral[- ]order|cohen v", low):
        return "collateral_order"
    if re.search(r"\b1292\(b\)|certif\w+ (?:the )?(?:controlling )?question", low):
        return "interlocutory_1292b"
    try:
        post = _appeal_posture(case_id) if case_id else {}
        if post.get("without_prejudice_leave") or post.get("pending_challenge_motions") or post.get("amend_deadline_open"):
            return "interlocutory_1292a_injunction" if re.search(r"injunction|enjoin", low) else "collateral_order"
        if post.get("judgment_entered"):
            return "full_appeal_as_of_right"
    except Exception:
        pass
    return "full_appeal_as_of_right"


def _decision_cache_path(case_id: str) -> Path:
    return ROOT / "output" / "audit" / f"appeal_decision_{_slug(case_id).replace(':', '_')}.json"


def load_cached_decision(case_id: str) -> dict:
    """Read the last LLM vehicle decision for this case (written by decide_appeal_vehicle), so the drafter
    can consume the LLM's choice cheaply without re-calling it. {} if none."""
    p = _decision_cache_path(case_id)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        return {}


M0_APPEAL_SYSTEM = (
    "You are m0, Beezle's filing-decision reasoner, protecting a PRO SE / SUI JURIS "
    "litigant. Decide the CORRECT vehicle to get this case's adverse rulings before a "
    "HIGHER court. The rules are clear — apply them to the posture; do not hedge "
    "unnecessarily.\n\n"
    "FEDERAL (U.S. District Court, E.D. Pa.):\n"
    "- A FINAL judgment disposing of all claims is appealable AS OF RIGHT under 28 U.S.C. "
    "1291 to the THIRD CIRCUIT. File a Notice of Appeal; the DISTRICT JUDGE DOES NOT DECIDE "
    "IT. No certification needed.\n"
    "- An interlocutory order GRANTING, CONTINUING, MODIFYING, REFUSING, or DISSOLVING an "
    "INJUNCTION is appealable AS OF RIGHT under 28 U.S.C. 1292(a)(1) — NO certification, the "
    "DISTRICT JUDGE DOES NOT DECIDE IT. A pre-filing / filing injunction barring the litigant "
    "from filing motions is such an order. This is the STRONGEST interlocutory route because it "
    "bypasses a hostile judge; PREFER it when a filing/anti-suit injunction exists.\n"
    "- A COLLATERAL ORDER (conclusively determines a disputed question separate from the merits, "
    "effectively unreviewable on final-judgment appeal, e.g. a filing injunction or immunity "
    "ruling) is immediately appealable under the collateral-order doctrine (Cohen), also WITHOUT "
    "certification.\n"
    "- PARTIAL final (some claims dismissed WITH prejudice, others still pending) -> Rule "
    "54(b) to certify the partial judgment for immediate appeal. If Rule 54(b) was DENIED, "
    "or the order is purely interlocutory -> 28 U.S.C. 1292(b) certification of a controlling "
    "question (this goes THROUGH the district judge, so it is a hedge, not the primary).\n\n"
    "PA (Court of Common Pleas):\n"
    "- A FINAL order is appealable AS OF RIGHT under Pa.R.A.P. 341 to the SUPERIOR COURT "
    "(Notice of Appeal within 30 days, Pa.R.A.P. 903). the judge DOES NOT DECIDE IT.\n"
    "- An interlocutory order granting/denying an INJUNCTION is appealable AS OF RIGHT under "
    "Pa.R.A.P. 311(a)(4); a COLLATERAL order under Pa.R.A.P. 313 — both WITHOUT permission.\n"
    "- A genuinely INTERLOCUTORY order (not an injunction/collateral) -> 42 Pa.C.S. 702(b)/"
    "Pa.R.A.P. 1311 by permission (goes THROUGH the trial judge; hedge only).\n\n"
    "VOID JUDGMENT/ORDER: if the record shows lack of jurisdiction or a denial of due "
    "process (sua sponte rulings without notice, subject-matter-jurisdiction/standing/"
    "removal defects, fraud on the court), the judgment is VOID — a nullity attackable AT "
    "ANY TIME (Fed.R.Civ.P. 60(b)(4); PA void-order doctrine), reviewed de novo. Timeliness "
    "does NOT bar it. The appeal then LEADS with voidness.\n\n"
    "PREFER the DIRECT appeal to the higher court (Third Circuit / Superior Court) to bypass "
    "a hostile trial judge. Use certification ONLY as a hedge or when the order is genuinely "
    "non-final. If an appeal is already filed but finality is defective, fix finality "
    "(Rule 54(b)) or pivot to 1292(b).\n\n"
    "Ground your rule statements in the controlling_law_from_corpus block provided with "
    "the posture (the authoritative text retrieved from the legal database) — do not rely "
    "on memory for the law. Reason from the posture + that law.\n\n"
    "THRESHOLD QUESTION — ASK IT FIRST. If the posture says record_delinquent is true, the "
    "record on appeal was never transmitted and there is nothing to brief yet: appellate "
    "review cannot begin without a record, and the delay is not the appellant's. Do NOT "
    "choose a merits vehicle in that situation merely because a judgment was entered or an "
    "injunction exists — those describe what will eventually be reviewed, not what makes the "
    "appeal move now. Choose record_transmission_enforcement_1935, whose relief is an "
    "application to the appellate court under Pa.R.A.P. 123 to enforce Pa.R.A.P. 1935 (the "
    "appellate prothonotary's mandatory delinquency notice) and Pa.R.A.P. 1931(a)(1)/1925(a)(1). "
    "Put the merits vehicle the posture otherwise supports in hedge_vehicle so nothing is lost. "
    "Pa.R.A.P. 1931(b) places the duty to prepare and transmit the record on the trial judge and "
    "Pa.R.A.P. 1931(c) places transmission on the trial court clerk; never treat the delinquency as the "
    "appellant's defect or as a reason to re-file in the trial court.\n\n"
    "Output STRICT JSON:\n"
    '{"primary_vehicle": "record_transmission_enforcement_1935 | full_appeal_as_of_right | '
    'interlocutory_1292a_injunction | '
    'collateral_order | rule_54b | interlocutory_1292b | interlocutory_702b", '
    '"higher_court": "<the appellate court that decides it>", '
    '"goes_through_trial_judge": true|false, '
    '"void_judgment": true|false, "void_basis": "<jurisdiction/due-process basis or empty>", '
    '"hedge_vehicle": "<secondary vehicle or null>", '
    '"controlling_questions": ["<pure questions of law, if certifying>"], '
    '"stubs": ["<stub filenames to generate, e.g. notice_of_appeal, appellate_brief_3rd_cir, '
    'motion_1292b_interlocutory_appeal, notice_of_appeal_superior_court, '
    'appellate_brief_superior_court, motion_to_certify_702b_interlocutory_appeal, '
    'rule_54b_certification>"], '
    '"reason": "<1-3 sentences naming the controlling posture facts>"}'
)


_CORPUS = "http://localhost:8100"


def _retrieve_appeal_law(court_type: str) -> list[dict]:
    """Ground the appeal RULES in the corpus (not hardcoded). Pull the controlling
    authorities for this court so m0 reasons from the law, not from memory."""
    fed = court_type.startswith("federal")
    qs = ([("28 U.S.C. 1291 final decision appeal as of right court of appeals", "1291"),
           ("28 U.S.C. 1292(b) certify controlling question interlocutory appeal", "1292b"),
           ("Rule 54(b) certification final judgment fewer than all claims appeal", "54b"),
           ("Fed. R. Civ. P. 60(b)(4) void judgment relief any time", "60b4")]
          if fed else
          [("Pa.R.A.P. 341 final order appeal as of right Superior Court", "341"),
           ("42 Pa.C.S. 702(b) Pa.R.A.P. 1311 interlocutory appeal by permission", "702b"),
           ("Pa.R.A.P. 903 notice of appeal 30 days", "903"),
           ("void order judgment attack any time jurisdiction due process Pennsylvania", "void")])
    out = []
    for q, key in qs:
        try:
            import urllib.parse, urllib.request
            u = f"{_CORPUS}/search_text?{urllib.parse.urlencode({'query': q, 'k': 1})}"
            with urllib.request.urlopen(u, timeout=10) as r:
                hits = json.loads(r.read().decode("utf-8", "ignore")).get("hits", [])
            if hits:
                out.append({"rule": key, "text": (hits[0].get("text") or "")[:240]})
        except Exception:
            pass
    return out


def _b70_json(system: str, user: str, url: str = M0_URL, timeout: int = 300) -> dict:
    r = requests.post(f"{url}/v1/chat/completions", json={
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0.2, "max_tokens": 1400, "stream": False,
        # answer in `content` (clean JSON), not `reasoning_content` — otherwise the model's scratchpad
        # leaks in and the JSON won't parse -> the gate falls back to a default vehicle (the bug that
        # made it decide 'full_appeal_as_of_right' for every case). Same fix as m0_rebuttal_gate.
        "chat_template_kwargs": {"enable_thinking": False},
    }, timeout=timeout)
    r.raise_for_status()
    msg = r.json()["choices"][0]["message"]
    content = (msg.get("content") or "").strip() or (msg.get("reasoning_content") or "")
    m = re.search(r"\{.*\}", content, re.S)
    return json.loads(m.group(0)) if m else {}


def decide_appeal_vehicle(case_id: str, *, url: str = M0_URL) -> dict:
    posture = _appeal_posture(case_id)
    law = _retrieve_appeal_law(posture["court_type"])   # DB-grounded controlling law
    user = json.dumps({"posture": posture, "controlling_law_from_corpus": law}, default=str)
    try:
        out = _b70_json(M0_APPEAL_SYSTEM, user, url=url)
    except Exception as e:
        # protective fallback: full appeal to the higher court (bypasses trial judge)
        return {"primary_vehicle": "full_appeal_as_of_right",
                "higher_court": posture["higher_court"], "void_judgment": bool(posture["void_indicator_count"]),
                "source": "fallback", "reason": f"m0 appeal gate offline ({e}); default to direct appeal to the higher court.",
                "posture": posture}
    if not out.get("primary_vehicle"):
        out["primary_vehicle"] = "full_appeal_as_of_right"
    # The COURT is deterministic from the case type (federal district -> Third Circuit; PA CCP -> Superior
    # Court). NEVER let the LLM override it (it hallucinated "Third Circuit" for a PA state case). Also
    # reject a vehicle that doesn't exist for this court_type (e.g. federal § 1292 for a PA case).
    _ct = posture.get("court_type", "federal_district")
    out["higher_court"] = posture["higher_court"]
    if (_ct, out["primary_vehicle"]) not in APPEAL_JURISDICTION:
        out["primary_vehicle"] = "full_appeal_as_of_right"
        out["vehicle_corrected"] = "LLM vehicle invalid for court_type; defaulted to appeal as of right"
    out["source"] = "m0_b70"
    out["posture"] = posture
    # HEDGE-VEHICLE (deterministic complement — the LLM's hedge pick varies run-to-run: 1292(a)/1292(b)/
    # collateral). Pin it to the record. An interlocutory vehicle is not a hedge for a FINAL judgment:
    #  - § 1292(a)(1) is available ONLY from an injunction order (posture.has_injunction);
    #  - when the judgment rests on PARTIAL dispositions with no injunction, the one grounded finality hedge
    #    is Rule 54(b) certification — the vehicle the Third Circuit itself names when partial claims are
    #    dismissed without a 54(b) order. Strip the mismatched interlocutory guesses and pin Rule 54(b).
    _stubs = [s for s in (out.get("stubs") or []) if s]
    _partial = bool(posture.get("with_prejudice") or posture.get("without_prejudice_leave"))
    # federal only — the § 1292 / Rule 54(b) hedge logic is federal; PA appeals hedge via Pa.R.A.P. 311/313.
    if posture.get("court_type") == "federal_district" and not posture.get("has_injunction"):
        _stubs = [s for s in _stubs if not any(x in s.lower()
                  for x in ("1292a", "1292(a)", "1292b", "1292(b)", "interlocutory", "collateral", "54b"))]
        if _partial:
            out["hedge_vehicle"] = "rule_54b"
            _stubs.append("motion_rule_54b_certification")   # canonical name (strip above removed any variant)
            out["hedge_corrected"] = ("no injunction + partial dispositions -> hedge pinned to Rule 54(b) "
                                      "(the finality gap the Third Circuit flags without a 54(b) order); "
                                      "interlocutory/collateral guesses stripped")
        else:
            out["hedge_vehicle"] = ""
            out["hedge_corrected"] = "no injunction and no partial disposition -> interlocutory hedge dropped"
        out["stubs"] = _stubs
    # attach the canonical jurisdictional cite for the chosen vehicle so the drafter reads ONE source
    _ct = posture.get("court_type", "federal_district")
    _basis, _phrase = jurisdiction_cite(out["primary_vehicle"], _ct)
    out["jurisdiction_basis"], out["jurisdiction_phrase"] = _basis, _phrase
    # cache so the drafter can consume the LLM decision cheaply (no re-call)
    try:
        p = _decision_cache_path(case_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(out, default=str, indent=1), encoding="utf-8")
    except Exception:
        pass
    return out


def _docket_ref(ev: dict) -> str:
    """The filing's own docket reference (' , E-File 2607036087'), or nothing.

    Quoting a paper without its filing receipt invites "which filing?"; the receipt number
    is what makes the exhibit checkable against the court's own system. Read from the
    timeline row, never composed: a fabricated E-File number would be a fabricated cite.
    """
    ref = str((ev or {}).get("docket_ref") or "").strip()
    return f" ({ref})" if ref else ""


def _tl_events(case_id: str) -> list:
    """The docket chain, read at RUN time. Never hardcoded: a stub that embeds dates
    copied by hand goes stale the moment the docket moves, and a stale date in a court
    filing is the defect we are complaining about in the first place."""
    import json as _json
    slug = (case_id or "").replace(":", "_").replace("/", "_")
    for cand in (ROOT / "obsidian_sync" / "cases" / slug / "CASE_TIMELINE.json",
                 ROOT / "obsidian_sync" / "cases" / slug.replace("phila_ccp_", "phila_ccp_")
                 / "CASE_TIMELINE.json"):
        if cand.exists():
            try:
                d = _json.loads(cand.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                return []
            return d.get("timeline") or d.get("events") or []
    return []


def _tl_find(events: list, *needles: str, field: str = "") -> dict:
    """First event whose text carries every needle (case-insensitive).

    Matches the EVENT LINE by default, not the whole record: `notes` narrate context
    ("STARTS the Pa.R.A.P. 1931 clock") and will steal a match from the event that
    actually IS the thing ("DEADLINE BLOWN — ... 1931(a)(1)"). Searching the event text
    first, then falling back to the full record, keeps a narrated mention from
    outranking the real one. Pass field= to force a single field.
    """
    def _blob(e, whole):
        if field:
            return str(e.get(field, "")).lower()
        if whole:
            return " ".join(str(v) for v in e.values()).lower()
        return f"{_ev_text(e)} {_ev_date(e)}".lower()

    for whole in (False, True):
        for e in events:
            b = _blob(e, whole)
            if all(n.lower() in b for n in needles):
                return e
        if field:
            break
    return {}


def _ev_date(e: dict) -> str:
    return str(e.get("date") or e.get("when") or "").strip()


def _ev_text(e: dict) -> str:
    return str(e.get("event") or e.get("what") or e.get("description") or "").strip()


def materialize_stubs(case_id: str, decision: dict, packet_dir) -> list:
    """Write the stub .md that m0's decision NAMED, in the format m1.parse_stub reads.

    Naming a stub inside a dict is not producing one. motion_drafter_v2 discovers work
    with `chain_dir.glob('*.md')`; a stub that exists only as a string is invisible to it,
    so the drafter finds nothing, writes nothing, and exits 0 — a silent empty success.

    Everything written here was decided upstream (vehicle by m0, dates by the docket,
    rule text by the corpus). No LLM call, no invention: this is transcription, and it
    refuses to overwrite an existing stub so human edits are never clobbered.
    """
    from pathlib import Path as _Path
    out_dir = _Path(packet_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []

    if decision.get("primary_vehicle") != "record_transmission_enforcement_1935":
        return written

    ev = _tl_events(case_id)
    d = decision.get("record_delinquency") or {}
    noa = _tl_find(ev, "notice of appeal", "filed")
    due = _tl_find(ev, "1931(a)(1)")
    lapse = _tl_find(ev, "1935(a)")
    letter = _tl_find(ev, "superior court letter")
    reject = _tl_find(ev, "rejected")
    complied = _tl_find(ev, "1925(b)", "complied")

    noa_date = _ev_date(noa) or d.get("notice_of_appeal_date", "")
    due_date = _ev_date(due) or d.get("record_due", "")
    lapse_date = _ev_date(lapse) or d.get("prothonotary_notice_due", "")
    days_late = d.get("days_late") or d.get("record_days_late") or ""

    appeal_no = decision.get("appellate_docket") or "0000 EDA 2026"
    trial_no = "October Term 2024, No. 002969"

    stub = out_dir / "01_application_for_relief_1935_transmit_record.md"
    if stub.exists():
        written.append(str(stub))
        return written

    body = f"""# DRAFT - SUPPLEMENT TO APPLICATION FOR RELIEF UNDER Pa.R.A.P. 123 TO ENFORCE Pa.R.A.P. 1935 AND COMPEL TRANSMISSION OF THE RECORD ON APPEAL

**Plaintiff:** FILER NAME, Appellant, sui juris
**Defendant:** Lippi, et al., Appellees
**Case:** {appeal_no} (Superior Court of Pennsylvania) — appeal from {trial_no}, Court of Common Pleas of Philadelphia County
**Filing type:** application_for_relief supplement
**Court:** PA_SUPERIOR

## Primary authority

> Pa.R.A.P. 1935(a): upon the failure of the lower court to transmit the record within the
> time prescribed, the prothonotary of the appellate court **shall** give written notice of
> the delinquency to the judge of the lower court, with a copy to the president judge.
> Pa.R.A.P. 1935(b) provides the escalation where the delinquency continues.
> The duty this supplement invokes is the APPELLATE prothonotary's own, and it is
> non-discretionary. That duty was not addressed in the application already pending.

## Claim taxonomy

record transmission delinquency, appellate prothonotary duty Pa.R.A.P. 1935, lower-court transmission duty Pa.R.A.P. 1931(b)-(c)

## CONTROLLING AUTHORITY

### RECORD TRANSMISSION IS OVERDUE AND THE DUTY IS NOT THE APPELLANT'S
- PLEAD: The record on appeal was due {due_date}, sixty days after the notice of appeal filed {noa_date}, under Pa.R.A.P. 1931(a)(1); it has not been transmitted, and as of the date of this supplement it is {days_late} days overdue.
- PLEAD: Pa.R.A.P. 1931(b) places the duty to prepare and transmit the record on the judge of the lower court and Pa.R.A.P. 1931(c) places on the clerk of the trial court the duty to transmit the record to the prothonotary of the appellate court when it is complete; no step of that duty belongs to the appellant.
- PLEAD: Appellant complied with every obligation the Rules place on him, including the concise statement of errors complained of on appeal served and filed {_ev_date(complied)} under Pa.R.A.P. 1925(b).
- PLEAD: The delinquency is therefore attributable entirely to the offices charged with transmission, and Appellant has neither the authority nor the means to cure it.

### THE APPELLATE PROTHONOTARY'S OWN DUTY UNDER Pa.R.A.P. 1935 HAS BEEN TRIGGERED
- PLEAD: Pa.R.A.P. 1935(a) directs that the prothonotary of the appellate court shall give written notice of the delinquency to the judge of the lower court, with a copy to the president judge, upon the lower court's failure to transmit the record within the prescribed time.
- PLEAD: That notice was due {lapse_date}, ten days after the record became overdue; Appellant has received no indication on the docket that any such notice issued.
- PLEAD: Pa.R.A.P. 1935(b) provides for further notice to the Administrative Office and to the Supreme Court where the delinquency is not cured, which is the relief this supplement asks the Court to set in motion.
- PLEAD: Because Pa.R.A.P. 1935 assigns this duty to the appellate prothonotary rather than to the trial court, the relief sought here does not depend on any further act by the court whose delinquency is the subject of the application.

### THE APPEAL CANNOT PROCEED AND THE DELAY IS COMPOUNDING
- PLEAD: Pa.R.A.P. 2185(a) measures the briefing schedule from the date the record is filed, so the absence of the record suspends the appeal indefinitely rather than merely postponing it.
- PLEAD: By letter dated {_ev_date(letter)} the Deputy Prothonotary of this Court confirmed in writing that the matter is presently pending and directed that briefs not be filed until a briefing schedule is activated, which confirms the record has never been docketed as filed.
- PLEAD: Pa.R.A.P. 1701(b)(1) preserves the lower court's authority to cause the record to be transcribed, approved, filed and transmitted notwithstanding the pending appeal, so no jurisdictional obstacle excuses the delinquency.

### THE BRIEF WAS RECEIVED AND RETURNED, AND NO RULE AUTHORIZED ITS RETURN
- PLEAD: Pa.R.A.P. 121(a) provides that papers permitted to be filed in an appellate court "shall be filed with the prothonotary," that filing "may be accomplished by mail addressed to the prothonotary," and that paperbooks "shall be deemed filed on the day of mailing" where first class mail is used; the duty to file what the office receives is stated in mandatory terms.
- PLEAD: This Court's own letter of {_ev_date(letter)} states that Appellant's briefs were "received" on August 28, 2026, so receipt by the Prothonotary is admitted of record and the Rule 121(a) duty attached on that date.
- PLEAD: Pa.R.A.P. 2101 supplies the only consequences the Rules attach to a brief that does not conform: such a brief "may be suppressed," and where the defects are substantial "the appeal or other matter may be quashed or dismissed." Each of those is an act of the Court, entered of record and reviewable; none is a return of the paper by the office of the Prothonotary.
- PLEAD: No provision of the Pennsylvania Rules of Appellate Procedure, and no provision of this Court's Internal Operating Procedures at 210 Pa. Code Ch. 65, authorizes the Prothonotary to return a received paper to the filer unfiled.
- PLEAD: The corresponding rule for the courts of common pleas forecloses the practice in terms: Pa.R.C.P. 205.2 provides that "[n]o pleading or other legal paper that complies with the Pennsylvania Rules of Civil Procedure shall be refused for filing by the prothonotary," and Pa.R.J.A. 103 requires that where a paper does not comply "the court shall alert the party to the specific provision at issue and provide a reasonable time for the party to comply." The settled architecture is that clerks docket, courts decide, and defects are cured on notice.
- PLEAD: Pa.R.A.P. 2185(a) fixes the date by which a brief must be served — "not later than" the date fixed or within forty days after the record is filed — and prescribes no earliest date before which a brief may not be filed, so the brief was not subject to suppression on that ground in any event.
- PLEAD: The return of the brief and the non-transmission of the record are the same failure viewed from two offices: the record this Court needs has not been sent up, and the paper Appellant did send was not docketed.

## THE THEORY

1. The record on appeal is overdue by {days_late} days, and every duty to transmit it belongs to the lower court and its clerk under Pa.R.A.P. 1931(b)-(c), not to the Appellant.
2. Appellant performed every act the Rules require of him, including the concise statement required by Pa.R.A.P. 1925(b), and no rule places any part of the transmission duty on him.
3. Pa.R.A.P. 1935(a) imposes a mandatory, non-discretionary duty on this Court's own prothonotary to notify the delinquent judge, and that duty was triggered when the record went overdue.
4. The application already pending before this Court sought transmission under Pa.R.A.P. 1931 and 1925 but did not invoke Pa.R.A.P. 1935; this supplement supplies that ground and does not repeat the relief already requested.
5. Appellant's attempt to obtain the same relief in the lower court was rejected without any rule being cited and without any defect being named, which demonstrates that relief from the court of origin is not available in practice.
6. Because Pa.R.A.P. 2185(a) runs the briefing schedule from the filing of the record, the appeal cannot move at all until the record is transmitted.

## PARTIES

1. Appellant FILER NAME appears sui juris and is the appellant of record in {appeal_no}, appealing from the orders entered in {trial_no}.
2. Appellees are the defendants of record in the Court of Common Pleas of Philadelphia County at {trial_no}.
3. The Court of Common Pleas of Philadelphia County and its Office of Judicial Records hold the record whose transmission is at issue; they are not parties, and this supplement seeks no relief against any appellee.

## Administrative history

- {noa_date} — Notice of appeal filed in the Court of Common Pleas, with proof of service.
- {_ev_date(_tl_find(ev, "transmitted to superior court"))} — Notice of appeal transmitted to the Superior Court of Pennsylvania.
- {_ev_date(complied)} — Concise statement of errors complained of on appeal filed under Pa.R.A.P. 1925(b); Appellant in full compliance.
- {due_date} — Record transmission under Pa.R.A.P. 1931(a)(1) and trial-court opinion under Pa.R.A.P. 1925(a)(1) both became overdue; neither has been filed.
- {lapse_date} — The ten-day window for the appellate prothonotary's delinquency notice under Pa.R.A.P. 1935(a) closed with no notice apparent of record.
- {_ev_date(letter)} — Superior Court letter confirming the matter is pending and directing that briefs await a briefing schedule.
- {_ev_date(_tl_find(ev, "praecipe", "submitted"))} — Praecipe to transmit the record submitted to the lower court through its electronic filing system.
- {_ev_date(reject)} — That submission was rejected without any rule being cited and without any defect being identified.

## NEW EVIDENCE BLOCK

### E1 — Superior Court letter confirming the record has never been filed _(high)_
**Element:** The record was never transmitted, and the Court's own records confirm it.
**Source:** Letter from the Deputy Prothonotary, Superior Court of Pennsylvania, dated {_ev_date(letter)}, at {appeal_no}.
**Quote:**
> The above-captioned matter is presently pending. Please refrain from filing briefs until a briefing schedule is activated.

### E2 — Rejection of the praecipe to transmit the record, no rule cited _(high)_
**Element:** Relief from the court of origin is unavailable in practice, which is why the appellate prothonotary's own duty must be enforced.
**Source:** Electronic filing rejection notice, Office of Judicial Records, {_ev_date(reject)}; {_ev_date(_tl_find(ev, 'praecipe', 'submitted'))} submission.
**Quote:**
> PLEASE CONTACT RECORDS MANAGEMENT OFFICE ABOUT SENDING THE APPEAL AT 215-686-7008.

### E3 — Appellant's own compliance with Pa.R.A.P. 1925(b) _(medium)_
**Element:** No act of the Appellant caused or contributed to the delinquency.
**Source:** Concise statement of errors complained of on appeal, filed {_ev_date(complied)} at {trial_no}{_docket_ref(complied)}, reviewed and accepted by the Office of Judicial Records the same day.
**Quote:**
> Pursuant to Pa.R.A.P. 1925(b), Appellant states the following errors complained of on appeal. Each error is stated concisely and with sufficient detail to identify the issue for the trial court, as required by Pa.R.A.P. 1925(b)(4)(ii).
> The legal paper you electronically presented for filing has been reviewed by the Office of Judicial Records and is deemed filed as noted below.
"""
    stub.write_text(body, encoding="utf-8")
    written.append(str(stub))
    return written


def decide_and_sequence(case_id: str, *, url: str = M0_URL, packet_dir=None) -> dict:
    """m0 phase: decide the appeal vehicle AND attach the deterministic filing order
    (anti-moot sequencing) from the filing_sequencer engine. If packet_dir is given,
    (re)writes FILING_INSTRUCTIONS.md there WITH the m0 vehicle header."""
    decision = decide_appeal_vehicle(case_id, url=url)
    # A record-enforcement vehicle needs its OWN stub. m0 reliably names the merits stubs
    # (that is what an appeal usually needs), but the paper that makes a delinquent appeal
    # move is the Pa.R.A.P. 123 application — and if the stub is missing the drafter has
    # nothing to write. Deterministic, because the vehicle already decided it.
    if decision.get("primary_vehicle") == "record_transmission_enforcement_1935":
        stubs = list(decision.get("stubs") or [])
        want = "application_for_relief_1935_transmit_record"
        if not any("1935" in s or "transmit_record" in s for s in stubs):
            decision["stubs"] = [want] + stubs
        decision["goes_through_trial_judge"] = False  # 1935 duty is the APPELLATE prothonotary's
    # MATERIALIZE the stub m0 just named. Naming a stub in a dict is not the same as
    # producing one: motion_drafter_v2 discovers work by globbing `chain_dir/*.md`, so a
    # stub that exists only as a string is a stub the drafter never sees — it finds no
    # work, writes nothing, and exits 0. That is the silent-empty class (exit-0 no-op),
    # the same failure shape as the dead-port word-count that shipped a wrong filing code.
    # Deterministic: every value written here was already decided upstream.
    if packet_dir and decision.get("primary_vehicle") == "record_transmission_enforcement_1935":
        try:
            decision["materialized_stubs"] = materialize_stubs(
                case_id, decision, packet_dir)
        except Exception as e:
            decision["materialize_error"] = f"{type(e).__name__}: {e}"

    try:
        from pipeline.filing_sequencer import sequence, render_instructions, write_instructions
        from pathlib import Path
        m0_hdr = {k: decision.get(k) for k in ("primary_vehicle", "higher_court",
                  "void_judgment", "hedge_vehicle", "controlling_questions", "reason")}
        if packet_dir:
            write_instructions(case_id, Path(packet_dir), m0=m0_hdr)
            decision["filing_instructions"] = str(Path(packet_dir) / "FILING_INSTRUCTIONS.md")
        else:
            # no packet on disk -> sequence whatever stubs the gate can see is left to caller
            decision["filing_sequence_note"] = (
                "call with packet_dir=<chain dir> to emit FILING_INSTRUCTIONS.md")
    except Exception as e:
        decision["filing_sequence_error"] = f"{type(e).__name__}: {e}"
    return decision


if __name__ == "__main__":
    import sys
    for cid in (sys.argv[1:] or ["fed:2:26-cv-00123", "fed:2:26-cv-00456",
                                 "fed:2:26-cv-00789", "phila_ccp:260100000"]):
        d = decide_appeal_vehicle(cid)
        print(f"\n{cid}")
        print(f"  vehicle : {d.get('primary_vehicle')}  -> {d.get('higher_court')}")
        print(f"  void    : {d.get('void_judgment')}  hedge: {d.get('hedge_vehicle')}  thru-judge: {d.get('goes_through_trial_judge')}")
        print(f"  stubs   : {d.get('stubs')}")
        print(f"  reason  : {(d.get('reason') or '')[:160]}")
