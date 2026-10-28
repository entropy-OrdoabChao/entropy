"""m0_posture_fit — corpus-grounded check of whether each planned motion FITS the case's
posture RIGHT NOW. Goal = WIN, not restrict: m0 keeps the full arsenal and only flags a
motion DEFER if controlling law clearly makes it improper/premature at this posture (e.g.
default judgment against a party that APPEARED; summary judgment on a claim already
dismissed). Otherwise FILE (on-path) or SEQUENCE (fits, must be ordered/framed).

m0 reasons from corpus law that is RETRIEVED and handed to it (R7) — not from memory.
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import json, re, urllib.parse, urllib.request
import requests
from pathlib import Path

ROOT = next((p for p in (_eroot(), _eroot()) if (p / "cases_index").exists()),
            _eroot())
M0_URL = "http://localhost:8000"
CORPUS = "http://localhost:8100"


def _case(case_id):
    d = json.loads((ROOT / "cases_index/case_registry.json").read_text(encoding="utf-8"))
    return d.get(case_id, {}) if isinstance(d, dict) else {}


def _corpus(q, k=1):
    try:
        u = f"{CORPUS}/search_text?{urllib.parse.urlencode({'query': q, 'k': k})}"
        with urllib.request.urlopen(u, timeout=10) as r:
            hits = json.loads(r.read().decode("utf-8", "ignore")).get("hits", [])
        return (hits[0].get("text") or "")[:300] if hits else ""
    except Exception:
        return ""


# law to ground the recurring posture questions — INCLUDING the litigant's affirmative
# strengths (prima facie + stare decisis -> SJ; untimely reply -> default despite appearance)
_LAW_QUERIES = {
    "summary_judgment": "prima facie case unrebutted binding stare decisis summary judgment entitled to judgment as a matter of law Rule 56",
    "default_judgment": "default judgment Rule 55 untimely answer late response failure to plead within required time appearance does not cure untimeliness",
    "amended_complaint": "amended complaint after dismissal leave to amend not exercised waiver",
    "remand": "28 U.S.C. 1447 remand lack of subject matter jurisdiction any time",
    "rule_54b": "Rule 54(b) final judgment as to fewer than all claims no just reason for delay",
    "compel_discovery": "motion to compel discovery Rule 37 pending action",
}


def _strengths(case_id):
    """Pull the litigant's AFFIRMATIVE strengths from the audit so m0 cannot forget them:
    stare-decisis violations, prima-facie / evidence-ignored findings, untimely-response
    (default) indicators."""
    digits = re.findall(r"\d{4,}", case_id)            # e.g. ['25', '02822'] -> match on '02822'
    out = {"stare_decisis": [], "prima_facie_or_evidence_ignored": [], "untimely_default": []}
    for cand in (ROOT / "output/audit").glob("data_*.json"):
        s = cand.stem.lower()
        if not any(dg in s for dg in digits):
            continue
        try:
            d = json.loads(cand.read_text(encoding="utf-8"))
        except Exception:
            continue
        sc = (d.get("scorecard") or {}).get("violations", []) or []
        out["stare_decisis"] = sorted({v.get("category", "") for v in sc})[:20]
        for v in (d.get("procedural_violations") or []):
            vt = (v.get("violation_type") or "").upper()
            if "PRIMA_FACIE" in vt or "EVIDENCE_IGNORED" in vt or "PRIMA FACIE" in vt:
                out["prima_facie_or_evidence_ignored"].append(vt)
            if "UNTIMELY" in vt or "LATE" in vt or "DEFAULT" in vt:
                out["untimely_default"].append(vt)
        break
    return out


def _retrieve(stems):
    law = {}
    for key, q in _LAW_QUERIES.items():
        if any(key in s.lower() or key.replace("_", "") in s.lower().replace("_", "") for s in stems):
            t = _corpus(q)
            if t:
                law[key] = t
    return law


SYSTEM = (
    "You are m0, Beezle's filing-decision reasoner for a PRO SE litigant whose goal is to WIN. "
    "The litigant followed the law and the opposing violations are clear; the only blocker is "
    "judicial bias/discretion. You are NOT here to restrict — keep the full arsenal. You are given "
    "ONE motion at a time; decide for THAT motion, grounded in the case POSTURE and the CONTROLLING "
    "LAW provided:\n"
    " - FILE: it fits the posture and advances the remedy now.\n"
    " - SEQUENCE: it fits but must be ordered/framed to avoid conflict (say how — e.g. discovery "
    "before summary judgment; merits 'without waiving' a remand objection; appeal last).\n"
    " - DEFER: controlling law CLEARLY makes it improper/premature at THIS posture (say the rule "
    "and why). Use DEFER sparingly — only when the law plainly bars it now, not on preference.\n"
    "WEIGH THE LITIGANT'S AFFIRMATIVE STRENGTHS (provided as affirmative_strengths) — do NOT "
    "forget them:\n"
    " (a) PRIMA FACIE evidence + binding STARE DECISIS support SUMMARY JUDGMENT — verdict FILE "
    "(an unrebutted prima facie case under controlling precedent is entitled to judgment as a "
    "matter of law); do NOT DEFER SJ as a generic 'fact dispute'.\n"
    " (b) An UNTIMELY / late response does NOT cure default — a party that appeared but answered "
    "late is still DEFAULTABLE; do NOT DEFER default merely because the party 'appeared'.\n"
    " (c) ACCOUNTABILITY complaints (judicial-council §351, ODC/JCB, §1983) are a PARALLEL "
    "record-building track, not relief in the litigation — verdict FILE; judicial immunity is a "
    "merits defense to a §1983 suit, not a bar to filing the misconduct record.\n"
    " (d) A facially VOID order (no jurisdiction / due-process denial) supports a void-judgment "
    "attack regardless of 'finality' — do not DEFER the void/jurisdiction attack as premature.\n"
    "Ground your conclusion in the posture + provided law + the strengths, not memory. Output STRICT "
    'JSON for the SINGLE motion: {"verdict":"FILE|SEQUENCE|DEFER","why":"<grounded reason>"}'
)

# motion-type token -> which corpus law query to attach for that one motion
_TYPE_LAW = {
    "summary_judgment": "summary_judgment", "default_judgment": "default_judgment",
    "entry_of_default": "default_judgment", "amended_complaint": "amended_complaint",
    "remand": "remand", "rule_54b": "rule_54b", "54b": "rule_54b",
    "compel_discovery": "compel_discovery", "compel": "compel_discovery",
}


def _law_for(motion_type):
    mt = motion_type.lower()
    for tok, key in _TYPE_LAW.items():
        if tok in mt:
            return {key: _corpus(_LAW_QUERIES[key])}
    return {}


def _m0(system, user, timeout=360):
    r = requests.post(f"{M0_URL}/v1/chat/completions", json={
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0.2, "max_tokens": 2600, "stream": False,
        "chat_template_kwargs": {"enable_thinking": False}}, timeout=timeout)
    r.raise_for_status()
    m = r.json()["choices"][0]["message"]
    c = (m.get("content") or "").strip() or (m.get("reasoning_content") or "")
    mm = re.search(r"\{.*\}", c, re.S)
    return json.loads(mm.group(0)) if mm else {}


def _fit_one(case_id, motion_type, posture, strengths):
    """Evaluate ONE motion type — small prompt, no context overflow (broken into pieces)."""
    payload = {"case_id": case_id, "motion": motion_type, "posture": posture,
               "affirmative_strengths": strengths, "controlling_law_from_corpus": _law_for(motion_type)}
    try:
        out = _m0(SYSTEM, json.dumps(payload, default=str), timeout=120)
        return {"verdict": out.get("verdict", "FILE"), "why": out.get("why", "")}
    except Exception as e:
        # on m0 error default to FILE (non-restrictive — keep the arsenal)
        return {"verdict": "FILE", "why": f"(m0 unavailable: {type(e).__name__}; kept by default)"}


def posture_fit(case_id, stems):
    c = _case(case_id)
    posture = {"status": c.get("status"), "judgment_status": c.get("judgment_status")}
    strengths = _strengths(case_id)
    types = sorted({re.sub(r"^\d\d?_", "", s.split("__")[-1]) for s in stems})
    motions = []
    for mt in types:
        v = _fit_one(case_id, mt, posture, strengths)
        motions.append({"motion": mt, **v})
    return {"source": "m0_piecewise", "posture": posture, "motions": motions,
            "strength_signals": {k: len(v) if isinstance(v, list) else v for k, v in strengths.items()}}
