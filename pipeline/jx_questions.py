"""pipeline/jx_questions.py — case-AGNOSTIC question derivation for case_comprehension.

Two exports:
  UNIVERSAL_JX_QUESTIONS  — jurisdictional-propriety questions that run for EVERY case
                             (well-pleaded complaint / removal basis / diversity / SMJ-void).
  derive_case_questions(slug) — builds a claim-derived question set from the case registry
                             (case_type + engine_facts) so non-mortgage cases are NO LONGER
                             analyzed through the hardcoded mortgage lens.
  is_mortgage_domain(slug) — data-driven: True when the case's engine_facts show a mortgage/
                             note/foreclosure posture (so the rich mortgage QUESTIONS still apply,
                             preserving existing behavior — regression-safe).

Question dicts match case_comprehension's shape exactly: dict(id=, q=, evidence=[...], law=).
Grounding phrases ('law=') hit authorities now in the corpus (Hain, Royal Canin, Franchise Tax
Board, Tennessee v. Union & Planters, Carden, Zambelli, Shamrock).
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import json, re
from pathlib import Path

_REGISTRY = _eroot() / "cases_index" / "case_registry.json"
# WSL fallback (the corpus/analysis run under WSL)
if not _REGISTRY.exists():
    _REGISTRY = (_eroot() / "cases_index/case_registry.json")

UNIVERSAL_JX_QUESTIONS = [
    dict(id="jx-federal-claim",
         q="Does the complaint plead any FEDERAL cause of action ON ITS FACE? List each count and state, "
           "for each, whether it arises under federal or state law. A federal defense or federal preemption "
           "raised by a defendant does NOT count and does NOT create federal-question jurisdiction.",
         evidence=["Complaint", "Petition", "Notice of Removal"],
         law="well-pleaded complaint rule federal question must appear on face of plaintiff's properly pleaded "
             "complaint not a defense Tennessee v Union Planters Bank Franchise Tax Board Construction Laborers"),
    dict(id="jx-removal-basis",
         q="Was this case removed from state court? State the removing party and the stated basis (federal "
           "question or diversity). If it was removed on a federal-question theory but no federal claim appears "
           "on the face of the complaint, was removal proper?",
         evidence=["Notice of Removal", "Order"],
         law="28 U.S.C. 1441 1446 removal strictly construed against removal all doubts resolved for remand "
             "Shamrock Oil & Gas v Sheets burden on the removing party"),
    dict(id="jx-diversity",
         q="If removal rests on diversity, is COMPLETE diversity present? For any LLC or partnership defendant, "
           "is EVERY member's citizenship diverse from the plaintiff, and who bears the burden of proving it?",
         evidence=["Notice of Removal", "Complaint"],
         law="28 U.S.C. 1332 complete diversity citizenship of a limited liability company is the citizenship of "
             "each of its members Carden v Arkoma Associates Zambelli Fireworks v Wood Hertz v Friend burden on remover"),
    dict(id="jx-smj-void",
         q="If there is NO federal question and NO complete diversity, does the federal court lack subject-matter "
           "jurisdiction, rendering any judgment void and requiring remand? Was the court's duty to police its own "
           "jurisdiction discharged?",
         evidence=["Order", "Dismissal", "Notice of Removal"],
         law="subject matter jurisdiction a court cannot create jurisdiction through its own mistakes Hain Celestial "
             "v Palmquist judgment void remand Royal Canin v Wullschleger 28 U.S.C. 1447(c) FRCP 12(h)(3) must dismiss"),
]

_MORTGAGE_MARKERS = ("chain of title", "foreclos", "servicer", "note", "mers", "qwr",
                     "qualified written request", "deed", "mortgage", "fha", "ginnie")

def _load_entry(slug: str) -> dict:
    try:
        reg = json.loads(_REGISTRY.read_text(encoding="utf-8"))
    except Exception:
        return {}
    norm = re.sub(r"[^a-z0-9]", "", (slug or "").lower())
    for cid, e in reg.items():
        for cand in (e.get("slug"), cid, cid.replace(":", "_")):
            if cand and re.sub(r"[^a-z0-9]", "", cand.lower()) == norm:
                return e
        # loose: match the trailing docket number
        if norm and norm.endswith(re.sub(r"[^a-z0-9]", "", cid.split(":")[-1].lower())):
            return e
    return {}

def is_mortgage_domain(slug: str) -> bool:
    """Data-driven: the case carries mortgage/note/foreclosure engine_facts (regression: 04767 -> True)."""
    e = _load_entry(slug)
    facts = " ".join(e.get("engine_facts") or []).lower() + " " + (e.get("case_type") or "").lower()
    return any(m in facts for m in _MORTGAGE_MARKERS)

def derive_case_questions(slug: str) -> list:
    """Agnostic: build questions from the case's own claims (case_type + engine_facts).
    One synthesis question per distinct engine-fact lever, plus a claim-nature anchor. No mortgage lens."""
    e = _load_entry(slug)
    ctype = (e.get("case_type") or "claim").replace("_", " ")
    facts = [f for f in (e.get("engine_facts") or []) if f and len(f) > 3]
    qs = [dict(id="claim-nature",
               q=f"This case is pleaded as '{ctype}'. Identify each cause of action actually pleaded, its "
                 f"elements, and the governing statute or common-law rule. Determine each element from the law.",
               evidence=["Complaint", "Petition"],
               law=f"{ctype} elements cause of action governing statute prima facie case")]
    # group engine_facts into up to 8 lever-questions (dedup, keep it bounded)
    seen = set()
    for i, f in enumerate(facts[:8]):
        key = f.lower().strip()
        if key in seen:
            continue
        seen.add(key)
        qs.append(dict(id=f"lever-{i}",
                       q=f"Assess the lever '{f}': do the primary documents and the law establish it, and what "
                         f"relief follows? Apply the law as written.",
                       evidence=["Complaint", "Order", "Defendant filing"],
                       law=f"{ctype} {f} elements remedy governing law"))
    return qs
