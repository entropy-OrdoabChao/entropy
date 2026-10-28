"""constitutional_consent_engine — the LAST m0 loop: the Act-umbrella + consent + constitutional check.

For an adverse item (an order, a cited statute, a claimed obligation), it answers, data-driven
(framework in constitutional_framework.yaml) + m0 reasoning grounded in the corpus:

  1. LAW TYPE     — is this constitutional POSITIVE LAW (applies by force) or CONSENT/CONTRACT-based?
  2. UMBRELLA ACT — which enacted Act does it fall under (queried from the beezle corpus)? A statute
                    works not only under its own title but under its umbrella arc.
  3. ACTOR        — public OFFICIAL (constrained by the Constitution; color-of-law) vs private CITIZEN
                    (bound only by consent/contract).
  4. CONSENT      — present / absent / unknown. A consent- or contract-based obligation does NOT bind
                    absent consent.
  5. VOID CHECK   — does it infringe a constitutional right? If so it is VOID to that extent — even a
                    contract (a right cannot be bargained away).

NOT sovereign-citizen: this is legitimate protection against government overreach. The output is a
reasoned constitutional posture the reply-brief / accountability layers can cite.
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import sys, json, re
from pathlib import Path

ROOT = next((p for p in (_eroot(), _eroot())
             if (p / "cases_index").exists()), _eroot())
sys.path.insert(0, str(ROOT))
import yaml as _yaml
_FW = _yaml.safe_load((Path(__file__).parent / "accountability_registry" / "constitutional_framework.yaml")
                      .read_text(encoding="utf-8"))


def _markers_hit(text: str, markers) -> list:
    t = (text or "").lower()
    return [m for m in markers if m.lower() in t]


def classify_law_type(text: str) -> dict:
    scores = {lt: len(_markers_hit(text, spec.get("markers", []))) for lt, spec in _FW["law_types"].items()}
    best = max(scores, key=scores.get)
    if scores[best] == 0:
        best = "consent_based"   # default: if it is not clearly positive law, treat as consent/contract
    return {"law_type": best, "binds": _FW["law_types"][best]["binds"], "marker_scores": scores}


def actor_type(text: str, role_hint: str = "") -> dict:
    blob = f"{role_hint} {text}"
    pub = _markers_hit(blob, _FW["actor_types"]["public_official"]["markers"])
    if pub:
        return {"actor": "public_official", **{"constrained_by": _FW["actor_types"]["public_official"]["constrained_by"]}, "markers": pub}
    return {"actor": "private_citizen", "bound_by": _FW["actor_types"]["private_citizen"]["bound_by"],
            "markers": _markers_hit(blob, _FW["actor_types"]["private_citizen"]["markers"])}


def consent_state(text: str, case_facts: str = "") -> str:
    blob = f"{text}\n{case_facts}"
    if _markers_hit(blob, _FW["consent_signals"]["absent"]):
        return "absent"
    if _markers_hit(blob, _FW["consent_signals"]["present"]):
        return "present"
    return "unknown"


def rights_implicated(text: str) -> list:
    out = []
    for right, spec in _FW["constitutional_rights"].items():
        hit = _markers_hit(text, spec.get("markers", []))
        if hit:
            out.append({"right": right, "source": spec["source"], "triggered_by": hit})
    return out


_CITE_RX = re.compile(r"\d+\s+U\.S\.C\.?\s+§*\s*\d+\w*|\d+\s+Pa\.?\s?C\.?S\.?\s+§*\s*\d+|\d+\s+P\.S\.\s+§*\s*\d+|§+\s*\d+\w*", re.I)


def umbrella_group(citations) -> dict:
    """Group scattered citations under their parent Act (anti-'weal-out'). Data-driven from the YAML.
    Returns {act: [cites...]} so a filing cites the umbrella Act, not loose sections."""
    from collections import defaultdict
    buckets, ungrouped = defaultdict(list), []
    for c in citations:
        cl = c.lower()
        act = next((u["act"] for u in _FW.get("umbrella_acts", [])
                    if any(m in cl for m in u["match"])), None)
        (buckets[act].append(c) if act else ungrouped.append(c))
    return {"grouped": {a: v for a, v in buckets.items()}, "ungrouped": ungrouped}


def umbrella_act(text: str) -> dict | None:
    """The umbrella Act(s) for the citations in an item — fast/deterministic from the YAML map."""
    cites = sorted(set(m.group(0).strip() for m in _CITE_RX.finditer(text or "")))
    if not cites:
        return None
    g = umbrella_group(cites)
    return {"umbrella_acts": list(g["grouped"].keys()), "grouping": g["grouped"],
            "ungrouped": g["ungrouped"]} if g["grouped"] else None


def needed_in_filing(item: str) -> dict:
    """m0 judgment gate: is the constitutional/consent argument actually NEEDED here? Over-using it
    weakens it (and invites the sov-cit smear). Recommend INCLUDE only when a right is implicated or
    consent is genuinely contested; otherwise hold it for the learning loop, not the filing."""
    rights = rights_implicated(item)
    consent = consent_state(item)
    include = bool(rights) or consent == "absent"
    why = ("a constitutional right is implicated" if rights else
           "consent is affirmatively absent" if consent == "absent" else
           "no right implicated and consent not contested — would dilute the argument")
    return {"include_in_filing": include, "why": why,
            "rights": [r["right"] for r in rights], "consent": consent}


def _m0_reason(llm, item, law, actor, consent, rights, umbrella) -> str | None:
    if llm is None:
        return None
    sysmsg = ("You write the constitutional/consent reasoning. Given an adverse item and a structured "
              "analysis (law type, actor type, consent state, constitutional rights implicated, umbrella "
              "act), reason in 4-7 sentences: whether it binds (positive law applies by force; consent/"
              "contract binds only with consent), and whether it is VOID to the extent it violates a "
              "right. RULES: rely only on the analysis + cited sources given; cite no other authority; "
              "do NOT use sovereign-citizen framing; output prose only.")
    usr = (f"Adverse item: {item[:600]}\nLaw type: {law['law_type']} ({law['binds']})\n"
           f"Actor: {actor['actor']}\nConsent: {consent}\n"
           f"Rights implicated: {[r['right']+' ('+r['source']+')' for r in rights] or 'none detected'}\n"
           f"Umbrella act: {umbrella or 'n/a'}\nWrite the reasoning.")
    try:
        out = llm.chat(messages=[{"role": "system", "content": sysmsg}, {"role": "user", "content": usr}],
                       thinking=False, n_predict=380, temperature=0.2)
        return (out or "").strip() or None
    except Exception:
        return None


def analyze(item: str, case_facts: str = "", role_hint: str = "", llm=None) -> dict:
    law = classify_law_type(item)
    actor = actor_type(item, role_hint)
    consent = consent_state(item, case_facts)
    rights = rights_implicated(item)
    umbrella = umbrella_act(item)
    # deterministic verdict
    if rights:
        verdict = ("VOID to the extent it violates " +
                   ", ".join(f"{r['right']} ({r['source']})" for r in rights) +
                   " — a right cannot be bargained away or overridden by order.")
    elif law["law_type"] == "positive_law":
        verdict = "Applies by force of law IF constitutional (positive law / enacted statute)."
    else:
        if consent == "absent":
            verdict = f"Does NOT bind — {law['law_type']} obligation and consent is ABSENT."
        elif consent == "unknown":
            verdict = (f"Binds only on proof of consent/contract — {law['law_type']}; consent UNKNOWN, "
                       "demand the consent instrument.")
        else:
            verdict = f"Binds — {law['law_type']} and consent is present."
    return {
        "item": item[:300], "law_type": law["law_type"], "binds": law["binds"],
        "actor": actor["actor"], "consent": consent,
        "rights_implicated": rights, "umbrella_act": umbrella,
        "needed_in_filing": needed_in_filing(item),
        "verdict": verdict, "reasoning": _m0_reason(llm, item, law, actor, consent, rights, umbrella),
        "not_sovereign_citizen": True,
    }


if __name__ == "__main__":
    demo = [
        "The Court summarily denied the motion ex parte and without a hearing.",
        "Plaintiff is enjoined from filing further documents (pre-filing review).",
        "You are the account holder under the cardmember agreement and consented to arbitration.",
        "Writ of execution to garnish the account without compensation.",
    ]
    for it in demo:
        r = analyze(it)
        print(f"\nITEM: {it}\n  law_type={r['law_type']} actor={r['actor']} consent={r['consent']}")
        print(f"  rights={[x['right'] for x in r['rights_implicated']] or 'none'}")
        print(f"  VERDICT: {r['verdict']}")
