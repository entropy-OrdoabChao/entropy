# -*- coding: utf-8 -*-
"""
statute_capacity — definition-first STATUTORY APPLICABILITY by CAPACITY.

Answers: does this statute reach THIS person, in the capacity they actually acted? A regulatory statute
reaches a person "only to the extent that the person acts in [the regulated] capacity" (12 U.S.C.
§ 5481(6)-style; general rule). So the question is never "did you drive?" but "did you act in the
COMMERCIAL capacity the term is defined by?" — because terms like motor vehicle / driver / transportation
are frequently defined commercially (Am. Jur. 2d Automobiles § 175: a motor carrier provides transport
"for compensation"; a license is authority to pursue a "trade, profession, or calling" — Black's), while
the public right to travel is "absolute and paramount" (Am. Jur. 2d Highways § 147).

METHOD (never common-language): for each key term, PULL ITS DEFINITION from the corpus, classify the
CAPACITY it regulates (commercial vs general), and test it against the actor's actual capacity. Grounded;
the statutory DEFINITION decides, not assumption. This is the capacity leg of authority (with standing +
jurisdiction) applied to the merits question of who a statute binds.
"""
from __future__ import annotations
import re

_COMMERCIAL = re.compile(
    r"\bfor hire\b|\bfor compensation\b|\bin commerce\b|\bcommercial\b|\bcarrier\b|\bfor profit\b|"
    r"\bbusiness\b|\btrade\b|\bprofession\b|\bcalling\b|transportation of (passengers|property|persons)|"
    r"\bregulated\b|\blivery\b|\bhire or reward\b", re.I)
_GENERAL = re.compile(
    r"\bany person\b|\bnatural person\b|\bprivate\b|right to travel|\bparamount\b|traveling public|"
    r"\babsolute\b|common right|for personal use|not for (hire|compensation)", re.I)

CITES = {
    "capacity_only": "a statute reaches a person only to the extent the person acts in the regulated "
                     "capacity (12 U.S.C. 'only to the extent that the person acts in such capacity')",
    "motor_carrier": "Am. Jur. 2d Automobiles s 175 — a motor carrier provides motor-vehicle "
                     "transportation FOR COMPENSATION (for-hire vs private)",
    "license_trade": "Black's Law Dictionary — a license is authority to pursue a trade, profession, or "
                     "calling (a regulated commercial privilege)",
    "right_travel": "Am. Jur. 2d Highways s 147 — the public right to use a street for travel is absolute "
                    "and paramount",
    "context": "definitions apply 'unless the context clearly indicates otherwise' (statutory construction)",
}


def capacity_class(def_text: str) -> str:
    c, g = bool(_COMMERCIAL.search(def_text or "")), bool(_GENERAL.search(def_text or ""))
    if c and not g:
        return "COMMERCIAL"
    if g and not c:
        return "GENERAL"
    if c and g:
        return "CONTEXT"          # both signals present -> the specific definition/context decides
    return "UNSPECIFIED"


def pull_definition(term: str):
    """Definition-first: pull the term's DEFINITION from the corpus (not a common-language read)."""
    try:
        from pipeline.drafter_grounding import _corpus_hits
    except Exception:
        return {"text": "", "cite": None, "score": 0.0}
    head = (term or "").split()[0].lower() if term else ""
    for h in _corpus_hits("%s defined definition means term of art" % term, k=5):
        t = (h.get("text") or "").strip()
        if (h.get("score") or 0) >= 0.5 and head and head in t.lower():
            return {"text": t[:420], "cite": h.get("citation") or h.get("source") or h.get("id"),
                    "score": round(h.get("score") or 0, 3)}
    return {"text": "", "cite": None, "score": 0.0}


def analyze(terms, actor_capacity: str, pull=True) -> dict:
    """terms: key statutory terms (e.g. ['motor vehicle','driver','transportation']).
    actor_capacity: how the person actually acted ('private travel' | 'commercial delivery' | ...).
    pull=False lets a caller pass pre-fetched definitions via terms=[{'term','definition'}]."""
    per, regulated = [], set()
    for item in terms:
        if isinstance(item, dict):
            term, deftext, cite = item.get("term", ""), item.get("definition", ""), item.get("cite")
        else:
            term = item
            d = pull_definition(term) if pull else {"text": "", "cite": None}
            deftext, cite = d["text"], d["cite"]
        cls = capacity_class(deftext)
        per.append({"term": term, "capacity_regulated": cls, "definition": deftext[:220], "cite": cite})
        if cls in ("COMMERCIAL", "CONTEXT"):
            regulated.add("COMMERCIAL")
        if cls in ("GENERAL", "CONTEXT"):
            regulated.add("GENERAL")

    actor = (actor_capacity or "").lower()
    actor_commercial = bool(_COMMERCIAL.search(actor)) or any(
        w in actor for w in ("commercial", "for hire", "delivery", "compensation", "business", "carrier"))
    commercial_reg = "COMMERCIAL" in regulated

    if commercial_reg and not actor_commercial:
        applies, verdict = False, "DOES NOT APPLY"
        proof = ("LAW: %s; the key term(s) are defined by COMMERCIAL capacity [%s], and a license is a "
                 "commercial privilege [%s], while the public right to travel is absolute and paramount [%s]. "
                 "RECORD/FACT: the actor acted in a PRIVATE, non-commercial capacity (%s). THEREFORE: the "
                 "commercial statute does not reach this act; a commercial-offense charge does not stand "
                 "against a non-commercial actor. Test the SPECIFIC statute's definition %s."
                 % (CITES["capacity_only"], CITES["motor_carrier"], CITES["license_trade"],
                    CITES["right_travel"], actor or "private", CITES["context"]))
    elif commercial_reg and actor_commercial:
        applies, verdict = True, "APPLIES"
        proof = ("The actor acted in the COMMERCIAL capacity the statute regulates (%s); the statute applies "
                 "in that capacity [%s]." % (actor, CITES["capacity_only"]))
    else:
        applies, verdict = None, "PULL THE STATUTE'S OWN DEFINITION"
        proof = ("The term(s) are not clearly commercial on the corpus definition; pull the SPECIFIC "
                 "charging statute's definition and apply the capacity/context test %s." % CITES["context"])

    return {"terms": per, "regulated_capacity": sorted(regulated), "actor_capacity": actor,
            "applies_to_actor": applies, "verdict": verdict, "proof": proof}


if __name__ == "__main__":
    import pprint, sys
    pprint.pprint(analyze(["motor vehicle", "driver", "transportation"],
                          sys.argv[1] if len(sys.argv) > 1 else "private travel to a friend's house"))
