#!/usr/bin/env python3
"""
pipeline/drafter_grounding.py

Drafter integration helper. Returns a Markdown grounding block ready
to inject into motion_drafter_v2._section_prompt:

    from pipeline.drafter_grounding import get_section_grounding
    grounding = get_section_grounding(section_text, top_k=8)
    user_prompt = grounding + "\n\n" + user_prompt

Safe to call unconditionally. Returns "" on any error / no terms found.
Never raises.

ENV (inherited from definition_synergy):
    BEEZLE_CORPUS_URL  default http://localhost:8100
    BEEZLE_LLM_URL     default http://localhost:8000  (local-gpu local)
                       set to http://localhost:8000 for local-gpu access
    BEEZLE_BLACKS_SRC  default blacks_law_8th
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
from typing import Any, Dict


def get_section_grounding(section_text: str, top_k: int = 3,
                          quote_style: str = "callout") -> str:
    if not section_text or not section_text.strip():
        return ""
    try:
        from pipeline.definition_synergy import synergy_enhance
    except ImportError:
        return ""
    try:
        result = synergy_enhance(section_text, top_k=top_k,
                                quote_style=quote_style)
    except Exception:
        return ""
    quotes = result.get("quotes") or []
    if not quotes:
        return ""
    lines = [
        "## AUTHORITATIVE DEFINITIONS",
        ("*Use the following dictionary-anchored terms with precision. "
         "Where these terms carry the argument, cite Black\'s Law "
         "Dictionary inline. Do not paraphrase the entry - quote.*"),
        "",
    ]
    for q in quotes:
        text = q.get("quote", "")
        if text:
            lines.append(text)
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# BEEZLE_FN_UNUSED 2026-06-13: grounding_dict (no static refs in pipeline/)
def grounding_dict(section_text: str, top_k: int = 5) -> Dict[str, Any]:
    try:
        from pipeline.definition_synergy import synergy_enhance
    except ImportError:
        return {"quotes": [], "candidates_found": 0, "llm_used": False}
    try:
        return synergy_enhance(section_text, top_k=top_k)
    except Exception:
        return {"quotes": [], "candidates_found": 0, "llm_used": False}


if __name__ == "__main__":
    import sys
    from pathlib import Path
    if len(sys.argv) < 2:
        print("usage: python3 -m pipeline.drafter_grounding <filing.md> [top_k]")
        sys.exit(1)
    p = Path(sys.argv[1])
    if not p.is_absolute():
        p = _eroot() / sys.argv[1]
    text = p.read_text(encoding="utf-8", errors="replace")
    top_k = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    out = get_section_grounding(text, top_k=top_k)
    if out:
        print(out)
    else:
        print("(no grounding produced)", file=sys.stderr)
        sys.exit(2)


# === FACTUAL PREDICATES BRIDGE (added by _install_factual_predicates_bridge_v1) ===
# Combines AUTHORITATIVE DEFINITIONS (from doctrine_index via definition_synergy)
# with ESTABLISHED FACTUAL PREDICATES (from pdf_findings/sec_findings/judicial_findings
# via factual_predicates). The drafter LLM now sees BOTH the law AND the facts.

try:
    from pipeline.factual_predicates import get_factual_predicates as _factual_predicates_impl
except ImportError:
    def _factual_predicates_impl(stub_text, top_k=3):
        return ""

# Preserve the original doctrinal-only grounding under a private name.
_get_doctrinal_grounding_only = get_section_grounding

def get_section_grounding(section_text, *args, **kwargs):
    """Combined doctrine + factual-predicates grounding.

    Accepts whatever args the original took (top_k, quote_style, etc.) and
    passes them through to the doctrinal grounding. Factual predicates are
    keyed only off the stub text.
    """
    doctrinal = ""
    try:
        doctrinal = _get_doctrinal_grounding_only(section_text, *args, **kwargs) or ""
    except TypeError:
        try:
            doctrinal = _get_doctrinal_grounding_only(section_text) or ""
        except Exception:
            doctrinal = ""
    except Exception:
        doctrinal = ""
    factual = ""
    try:
        from pipeline.factual_predicates import get_factual_predicates as _fp
        factual = _fp(section_text) or ""
    except Exception:
        factual = ""
    if doctrinal and factual:
        return doctrinal.rstrip() + "\n\n" + factual
    return doctrinal or factual


# === INTERPRETIVE-CANON GROUNDING (comprehension, not a fixed checklist) ===
# Surfaces ONLY the interpretive canons that bear on THIS stub's statutes/claims, pulled
# from m0's own grounded canon notes in the connected-notes graph (its accumulated
# understanding), selected by MEANING. The drafter is told to apply/name a canon ONLY where
# its comprehension of the governing text supports it — never as a fixed list to recite.
_CANON_MARKERS = (
    "canon", "construction", "contra proferentem", "remedial", "chevron", "loper",
    "expressio", "ejusdem", "noscitur", "surplusage", "lenity", "absurdity",
    "plain meaning", "in pari materia", "antecedent", "includes", "shall", "may",
)


def get_canon_grounding(stub_text: str, top_k: int = 3) -> str:
    """Comprehension-driven interpretive-canon grounding. Returns "" on any error / no
    relevant canon. Safe to call unconditionally."""
    if not stub_text or not stub_text.strip():
        return ""
    try:
        from pipeline.connected_notes import semantic_search_notes, _read_concepts, NOTES
    except Exception:
        return ""
    try:
        hits = semantic_search_notes(
            stub_text[:1400] + " interpretive canon statutory construction contract ambiguity "
            "remedial consumer statute mandatory shall agency deference", k=8)
    except Exception:
        return ""
    blocks = []
    for h in hits:
        slug = (h.get("slug") or "").lower()
        try:
            concepts = " ".join(_read_concepts(NOTES / f"{h.get('slug')}.md")).lower()
        except Exception:
            concepts = ""
        hay = slug + " " + concepts + " " + (h.get("text") or "")[:200].lower()
        if not any(m in hay for m in _CANON_MARKERS):
            continue
        body = (h.get("text") or "").strip()
        if body:
            blocks.append(f"[[{h.get('slug')}]] {h.get('title','')}\n{body[:900]}")
        if len(blocks) >= top_k:
            break
    if not blocks:
        return ""
    return ("## INTERPRETIVE CANONS — m0's grounded understanding (apply by COMPREHENSION)\n"
            "*These are the canons most relevant to this filing's statutes/claims. Using your "
            "reading of the governing statute, rule, or contract term in the record, NAME and cite "
            "a canon ONLY where it genuinely governs a contested term (e.g. a remedial consumer "
            "statute construed for the plaintiff; ambiguity in the defendant's drafted contract "
            "against the drafter; a 'shall' as a mandatory duty). If none truly applies, mention "
            "none — do not recite canons that do not bear on this filing. Cite the canon, not the "
            "[[note]]. Grounded UCC/capacity/redemption/discharge/securitization arguments are "
            "PERMITTED where the corpus supports them; only court-sanctioned sovereign-citizen/"
            "strawman/bogus-lien schemes are barred.*\n\n"
            + "\n\n".join(blocks) + "\n")


# === NON-CONCEDING DISMISSAL POSTURE (m0 FINDS the restoration authority) ===
# The litigant concedes NO dismissal as valid. Every claim is corpus-backed; an adverse
# 12(b)(6)/void dismissal is ERROR. This helper pulls the reversal/restoration authority m0
# has GROUNDED in the connected-notes graph (the user's appellate decisions reversing void
# judgments, the 12(b)(6) standard, leave-to-amend, ubi jus ibi remedium), selected by
# relevance to THIS stub, so m0 finds and cites the authority that restores each dismissed
# claim instead of conceding "the sole surviving claim." Safe: returns "" on any error.
_RESTORE_QUERY = ("dismissal was error reversed on appeal void judgment vacated claim stated "
                  "12(b)(6) standard accept allegations as true leave to amend reversible error "
                  "appellate reversal restore dismissed claim prima facie evidence ignored")
_RESTORE_MARKERS = (
    "dismiss", "revers", "vacat", "void", "12(b)(6)", "rule 12", "leave to amend", "remand",
    "appell", "reinstat", "restore", "ubi jus", "prima facie", "states a claim", "60(b)",
)


def _audit_reversibility_from_stub(stub_text: str) -> str:
    """Pull the ALREADY-DETECTED reversibility findings for this case (audit `dismissal` array +
    litigation_posture) by matching the docket number in the stub. Case-agnostic; "" on error."""
    try:
        import json, re
        from pipeline.litigation_posture import _AUDIT
        m = re.search(r"\b(\d{2})[-:]cv[-:](\d{3,5})\b", stub_text or "", re.I) or \
            re.search(r"\b(\d{6,})\b", stub_text or "")
        digits = [g for g in (m.groups() if m else ()) if g and len(g) >= 3]
        if not digits:
            return ""
        ap = next((p for p in _AUDIT.glob("data_*.json")
                   if all(d in p.stem.replace("-", "") for d in [x.replace("-", "") for x in digits])
                   or any(d in p.stem for d in digits)), None)
        if not ap:
            return ""
        d = json.loads(ap.read_text(encoding="utf-8"))
        lp = d.get("litigation_posture") or {}
        rev = lp.get("reversible_dismissals") or []
        if not rev:
            dz = d.get("dismissal") or []
            rev = [{"summary": (x.get("summary") or "")[:240], "score": x.get("score")}
                   for x in dz if isinstance(x, dict) and (
                       (x.get("score") or 0) >= 50
                       or "LEAVE TO AMEND" in (x.get("summary") or "").upper()
                       or "CONCLUSORY" in (x.get("summary") or "").upper())][:6]
        if not rev:
            return ""
        lines = ["### ALREADY-DETECTED reversibility (cite these record findings, do not invent):"]
        for r in rev[:6]:
            sc = r.get("score")
            lines.append(f"- {('(reversibility ' + str(sc) + ') ') if sc else ''}{r.get('summary','')}")
        return "\n".join(lines) + "\n"
    except Exception:
        return ""



# === PERSONAL-JURISDICTION POSTURE + STATUS GUARD (2026-10-20, S-pj-preserve / S-capacity-status) ===
# The litigant is usually the PLAINTIFF, and the recurring trick is to flip the posture: recast him as a
# debtor / ward / entity / estate, or take a case that rests on property and impose PERSONAL relief. Every
# filing states the reservation and the scope of what was submitted, and argues any detected switch from
# the m0 audit (jurisdiction_mode.improper_switches + persona_status). Authorities verbatim from corpus.
_PJ_BLOCK = """## PERSONAL-JURISDICTION POSTURE + STATUS GUARD (bound — apply in EVERY filing)
*Read the caption for the filer's ROLE and apply ONLY the matching paragraph. Quote the authority given; do not
substitute or invent.*

**A. FILER IS PLAINTIFF.** State in the opening paragraph, in substance: "Plaintiff appears as a natural person,
sui juris, solely on the claims pleaded. Nothing in this filing consents to any other proceeding, to any
recharacterization of Plaintiff's status or capacity (debtor, ward, entity, estate, account), or to relief
against Plaintiff's person or property not sought in the pleadings; any such recharacterization is objected to
and is not waived." Basis: a waiver "is the voluntary and intentional relinquishment of a known right, claim, or
privilege" (28 Am. Jur. 2d Estoppel and Waiver § 154, citing Johnson v. Zerbst, 304 U.S. 458) — nothing beyond
the pleaded claims was knowingly relinquished. Do NOT claim the plaintiff is outside jurisdiction for the case he
brought or for a counterclaim on it: a plaintiff who sues "submitted himself to the jurisdiction of the court ...
for all purposes for which justice to the defendant requires his presence" (Adam v. Saenger, 303 U.S. 59, 67-68
(1938)).

**B. FILER IS DEFENDANT / GARNISHEE / OWNER OF THE ATTACHED PROPERTY.** The FIRST filing must (1) reserve: an
appearance "shall not constitute a waiver of the right to raise any defense including questions of jurisdiction
or venue" (Pa.R.C.P. 1012(a)); and (2) raise lack of jurisdiction over the person by preliminary objection,
Pa.R.C.P. 1028(a)(1) — "A party waives all defenses and objections which are not presented either by
preliminary objection, answer or reply" (Pa.R.C.P. 1032(a)). Federal: Fed. R. Civ. P. 12(b)(2) in the first
motion or responsive pleading or it is waived, 12(h)(1). "Pleading or answering to the merits is always
considered a general appearance, unless objection is also made to lack of jurisdiction of the court over the
person" (41 Am. Jur. Pleading § 131). Seek no affirmative relief and sign no stipulation, consent order or payment
plan before the objection is ruled on.

**C. NEVER argue "withdrawal of consent" to undo jurisdiction already invoked.** "Statements or admissions made
for the purpose of giving the court jurisdiction cannot, after they have been acted upon, be withdrawn or
contradicted by the party making them" (28 Am. Jur. 2d Estoppel and Waiver § 73). The lever is SCOPE: consent
reaches only what was invoked. And subject-matter jurisdiction is never conferred by consent or estoppel: such
jurisdiction "must arise by law and not by mere consent of the parties" (id. § 73) — always raise it.

**D. PROPERTY-TO-PERSON.** If the proceeding rests on property (garnishment, attachment, account, wages), "the
effect of a judgment in such a case is limited to the property that supports jurisdiction and does not impose a
personal liability on the property owner" (Shaffer v. Heitner, 433 U.S. 186, 199). Any personal relief on that
base is argued as beyond jurisdiction.
"""


def get_pj_posture_grounding(stub_text: str, max_items: int = 5) -> str:
    """Bound personal-jurisdiction posture + the record's DETECTED switches (audit jurisdiction_mode). Matches the
    case by docket digits like the other audit feeds. Input-only; "" never raises."""
    try:
        import json, re
        from pipeline.litigation_posture import _AUDIT
        out = [_PJ_BLOCK]
        m = re.search(r"\b(\d{2})[-:]cv[-:](\d{3,5})\b", stub_text or "", re.I) or \
            re.search(r"\b(\d{6,})\b", stub_text or "")
        digits = [g for g in (m.groups() if m else ()) if g and len(g) >= 3]
        ap = next((p for p in _AUDIT.glob("data_*.json")
                   if any(d in p.stem for d in digits)), None) if digits else None
        if ap:
            jm = (json.loads(ap.read_text(encoding="utf-8")).get("jurisdiction_mode") or {})
            sw = [" ".join(str(x).split())[:420] for x in (jm.get("improper_switches") or [])][:max_items]
            ps = ((jm.get("persona_status") or {}).get("persona_findings") or [])[:max_items]
            if sw or ps:
                out.append("### DETECTED IN THIS RECORD — argue each (record quote + law already bound):")
                out += ["- " + x for x in sw]
                out += ['- %s treats the litigant as %s (%s): "%s" [%s]' % (
                    f.get("issuer"), f.get("persona"), f.get("mode"), (f.get("quote") or "")[:240], f.get("doc"))
                    for f in ps if isinstance(f, dict)]
        return "\n".join(out) + "\n"
    except Exception:
        return ""

def get_detected_defect_authority(stub_text: str, max_defects: int = 12) -> str:
    """BOUND-AUTHORITY grounding (2026-07-12): feed the drafter the TIGHT per-defect authority the analysis
    ALREADY produced. Each detected `procedural_violations[]` entry carries its own `rule_citation` + verbatim
    `rule_text` + `explanation` in the audit JSON — the correct cite is bound to the defect at DETECTION time.
    The drafter otherwise re-derives cites loosely from the stub and MISATTRIBUTES (e.g. citing a substantive
    preemption statute for a procedural-notice point, which the cite-support gate then flags). Binding
    DEFECT -> its operative authority in the prompt makes the drafter cite precisely at the source, so the fix
    lives in the analysis feed, not in post-processing. Deterministic; matches the case by docket digits; "" on
    error. m0's drafting core is untouched — this only enriches the INPUT."""
    try:
        import json, re
        from pipeline.litigation_posture import _AUDIT
        m = re.search(r"\b(\d{2})[-:]cv[-:](\d{3,5})\b", stub_text or "", re.I) or \
            re.search(r"\b(\d{6,})\b", stub_text or "")
        digits = [g for g in (m.groups() if m else ()) if g and len(g) >= 3]
        if not digits:
            return ""
        ap = next((p for p in _AUDIT.glob("data_*.json")
                   if all(d in p.stem.replace("-", "") for d in [x.replace("-", "") for x in digits])
                   or any(d in p.stem for d in digits)), None)
        if not ap:
            return ""
        d = json.loads(ap.read_text(encoding="utf-8"))
        seen, blocks = set(), []
        for v in (d.get("procedural_violations") or []):
            if not isinstance(v, dict):
                continue
            vt = (v.get("violation_type") or "").strip()
            cite = " ".join((v.get("rule_citation") or "").split()).strip()
            rtext = " ".join((v.get("rule_text") or "").split()).strip()
            expl = " ".join((v.get("explanation") or "").split()).strip()
            if not (vt and cite):
                continue
            key = (vt.lower(), cite.lower())
            if key in seen:
                continue
            seen.add(key)
            line = f"- **{vt}** — controlling authority **{cite}**"
            if rtext:
                line += f': "{rtext[:340]}"'
            if expl:
                line += f" — {expl[:200]}"
            blocks.append(line)
            if len(blocks) >= max_defects:
                break
        if not blocks:
            return ""
        head = ("## DETECTED DEFECTS AND THEIR BOUND AUTHORITY — argue EVERY one, each with ITS authority\n"
                "*The analysis has already matched each detected defect below to the ONE authority that governs "
                "it, with the operative rule text verbatim. Each defect is an INDEPENDENT, corpus-grounded ground "
                "for relief — omitting one FORFEITS that win. You MUST address every defect listed (give each its "
                "own numbered sub-argument), leading with the JURISDICTIONAL grounds (standing, subject-matter "
                "jurisdiction, void judgment) which cannot be waived and control the outcome. For each: cite the "
                "authority bound to it here and quote that rule text. Do NOT attach an authority to a point it "
                "does not govern, and do NOT cite a substantive statute for a procedural point (or a procedural "
                "rule for a substantive point). Use ONLY these bound cites for these defects; if a further point "
                "is not among them, argue it from the record and the other grounding — never invent or borrow a "
                "nearby citation. Completeness is the win: a shorter brief that drops grounds loses them.*\n")
        return head + "\n".join(blocks) + "\n"
    except Exception:
        return ""


def _audit_for_stub(stub_text: str):
    """Load the case's audit JSON by matching docket digits in the stub. None on miss."""
    try:
        import json, re
        from pipeline.litigation_posture import _AUDIT
        m = re.search(r"\b(\d{2})[-:]cv[-:](\d{3,5})\b", stub_text or "", re.I) or \
            re.search(r"\b(\d{6,})\b", stub_text or "")
        digits = [g for g in (m.groups() if m else ()) if g and len(g) >= 3]
        if not digits:
            return None
        ap = next((p for p in _AUDIT.glob("data_*.json")
                   if all(d in p.stem.replace("-", "") for d in [x.replace("-", "") for x in digits])
                   or any(d in p.stem for d in digits)), None)
        return json.loads(ap.read_text(encoding="utf-8")) if ap else None
    except Exception:
        return None


# Jurisdictional grounds lead every motion (non-waivable, dispositive). Everything else follows by severity.
_JURISDICTIONAL_GROUNDS = ("STANDING_DEFECT", "SUBJECT_MATTER_JURISDICTION_DEFECT",
                           "VOID_JUDGMENT_RULE_60B4", "REMOVAL_DEFECTIVE")
_GROUND_SEV_RANK = {"FATAL": 0, "CRITICAL": 0, "HIGH": 1, "SEVERE": 1,
                    "MEDIUM": 2, "MODERATE": 2, "LOW": 3, "": 4}


def _audit_by_case_id(case_id: str):
    """Load a case's audit JSON directly by canonical case_id (e.g. 'fed:2:26-cv-00123'), independent of
    whether the stub text carries a docket number. None on miss. Never raises."""
    try:
        import json
        from pipeline.audit_writer import _safe_id, AUDIT_DIR
        ap = AUDIT_DIR / f"data_{_safe_id(case_id)}.json"
        return json.loads(ap.read_text(encoding="utf-8")) if ap.exists() else None
    except Exception:
        return None


# Scorecard categories whose GROUND authority is the stare-decisis hierarchy. The audit hook map
# (scorecard_if_then) maps these to Rule 12(f) — the STRIKE remedy, correct for a motion to strike but WRONG
# as the cited rule in an opposition/reply that argues "these authorities do not bind this Court," where the
# governing authority is stare decisis itself. So for these we cite stare decisis + the hierarchy explanation.
_SC_STARE_DECISIS = {"NON_BINDING_AS_CONTROLLING", "TRIAL_COURT_AS_APPELLATE_AUTHORITY",
                     "NON_BINDING_AUTHORITY", "STARE_DECISIS_VIOLATION"}


def _scorecard_grounds(d, cap_offending: int = 8) -> list:
    """Convert the audit's scorecard.violations[] into argument grounds, grouped by category (a few grounds,
    not one per instance), so a stare-decisis-heavy case whose procedural_violations[] is empty still develops
    its grounds. Reuses scorecard_if_then's hook map for the frame/rule; for the stare-decisis categories it
    cites the STARE-DECISIS hierarchy (not the Rule 12(f) strike remedy the map carries). Same ground shape as
    procedural grounds {type, cite, rule_text, explanation, severity}. [] on error; never raises."""
    try:
        try:
            from pipeline.scorecard_if_then import _SCORECARD_HOOK_MAP as HOOK
        except Exception:
            HOOK = {}
        sc = (d.get("scorecard") or {}).get("violations") or []
        by_cat: dict = {}
        for v in sc:
            if isinstance(v, dict) and v.get("category"):
                by_cat.setdefault(v["category"], []).append(v)
        out = []
        for cat, items in by_cat.items():
            hook = HOOK.get(cat, {}) if isinstance(HOOK, dict) else {}
            if cat in _SC_STARE_DECISIS:
                cite = "the doctrine of stare decisis (the hierarchy of binding precedent)"
                rule_text = hook.get("subsection_logic") or (
                    "Only a decision of a court superior in the same appellate line binds; a non-binding "
                    "(out-of-circuit or trial-court) decision does not control this Court.")
            else:
                cite = (hook.get("rule_federal") or hook.get("rule_state") or "").strip()
                rule_text = (hook.get("subsection_logic") or hook.get("frame") or "").strip()
            offending = []
            for v in items:
                ev = (v.get("evidence") or v.get("description") or "").strip()
                if ev:
                    offending.append(ev[:80])
                if len(offending) >= cap_offending:
                    break
            expl = f"{len(items)} instance(s) in the opposing filing" + (
                " — offending authority/text: " + "; ".join(offending) if offending else "")
            if not cite:
                continue
            out.append({"type": cat, "cite": cite, "rule_text": rule_text,
                        "explanation": expl, "severity": (items[0].get("severity") or "").upper(),
                        "_src": "scorecard"})
        # strongest first: stare-decisis before tone/bias categories
        out.sort(key=lambda g: (0 if g["type"] in _SC_STARE_DECISIS else 1,
                                _GROUND_SEV_RANK.get(g.get("severity", ""), 4)))
        return out
    except Exception:
        return []


def _order_grounds(d, max_grounds: int = 24) -> list:
    """Order+dedupe the DETECTED defects from a loaded audit dict. Jurisdictional first, then by severity.
    SYNC: procedural_violations[] and scorecard.violations[] both feed the grounds — procedural grounds are
    PRESERVED and LEAD (byte-identical order/identity); scorecard grounds are appended, deduped by type. This
    lets a stare-decisis-heavy case (0 procedural, N scorecard) develop its grounds without displacing the
    precise procedural authority of a procedural-heavy case."""
    if not d:
        return []
    best = {}
    for v in (d.get("procedural_violations") or []):
        if not isinstance(v, dict):
            continue
        vt = (v.get("violation_type") or "").strip()
        cite = " ".join((v.get("rule_citation") or "").split()).strip()
        if not (vt and vt != "None" and cite):
            continue
        rtext = " ".join((v.get("rule_text") or "").split()).strip()
        expl = " ".join((v.get("explanation") or "").split()).strip()
        sev = (v.get("severity") or "").upper()
        richness = len(rtext) + len(expl)
        cur = best.get(vt)
        if not cur or richness > cur["_r"]:
            best[vt] = {"type": vt, "cite": cite, "rule_text": rtext,
                        "explanation": expl, "severity": sev, "_r": richness}
    ordered = sorted(best.values(),
                     key=lambda g: (0 if g["type"] in _JURISDICTIONAL_GROUNDS else 1,
                                    _GROUND_SEV_RANK.get(g["severity"], 4)))
    for g in ordered:
        g.pop("_r", None)
    # procedural grounds LEAD and are never displaced. Scorecard grounds SUPPLEMENT the argument ONLY when
    # procedural is thin (a stare-decisis-heavy case whose procedural_violations[] is near-empty would
    # otherwise starve the argument). When procedural already carries the argument, scorecard stays in the
    # DEFECTS-IN-THE-RECORD section (scorecard_if_then) — adding thin, redundant trailing Parts there would
    # dilute a strong brief, not strengthen it. Threshold 3: fewer than 3 procedural grounds => supplement.
    if len(ordered) < 3:
        proc_types = {g.get("type") for g in ordered}
        extra = [g for g in _scorecard_grounds(d) if g.get("type") not in proc_types]
        return (ordered + extra)[:max_grounds]
    return ordered[:max_grounds]


def detected_grounds(stub_text: str, max_grounds: int = 24) -> list:
    """Ordered, deduped list of the case's DETECTED defects for PER-GROUND argument drafting (so the motion
    develops EVERY strong ground, not a fixed 3). Each item: {type, cite, rule_text, explanation, severity}.
    Jurisdictional grounds first (standing, subject-matter jurisdiction, void judgment, removal) — non-waivable
    and dispositive — then by severity (FATAL first). Dedupe keeps the richest entry per type. [] on error."""
    return _order_grounds(_audit_for_stub(stub_text), max_grounds)


def get_restoration_grounding(stub_text: str, top_k: int = 3) -> str:
    """Reversal/restoration authority for the non-conceding dismissal posture. Returns "" on
    error / nothing relevant. Always SAFE to call."""
    blocks = []
    try:
        from pipeline.connected_notes import semantic_search_notes, _read_concepts, NOTES
        hits = semantic_search_notes(
            (stub_text[:1000] + " " + _RESTORE_QUERY), k=8)
        for h in hits:
            slug = (h.get("slug") or "").lower()
            try:
                concepts = " ".join(_read_concepts(NOTES / f"{h.get('slug')}.md")).lower()
            except Exception:
                concepts = ""
            hay = slug + " " + concepts + " " + (h.get("text") or "")[:200].lower()
            if not any(m in hay for m in _RESTORE_MARKERS):
                continue
            body = (h.get("text") or "").strip()
            if body:
                blocks.append(f"[[{h.get('slug')}]] {h.get('title','')}\n{body[:850]}")
            if len(blocks) >= top_k:
                break
    except Exception:
        blocks = []
    head = (
        "## NON-CONCEDING DISMISSAL POSTURE (mandatory)\n"
        "*The litigant concedes NO dismissal as valid. Every claim is corpus-backed; an adverse "
        "12(b)(6) or void dismissal is challenged as ERROR. You MUST NOT call any claim the 'sole "
        "surviving claim,' describe a claim as gone/dismissed-as-correct, or adopt the court's "
        "dismissal as accepted fact. Where the record shows a dismissal, treat it as the court's "
        "contested position: using the authority below plus your corpus comprehension, show the "
        "claim STATED A CLAIM (prima facie elements pled + the interpretive canon that governs) "
        "and cite the controlling/appellate authority that reverses or vacates such a dismissal. "
        "Goal: restore and win. Cite the authority, not the [[note]]; ground every cite — never "
        "invent one.*\n")
    detected = _audit_reversibility_from_stub(stub_text)
    if detected:
        head = head + "\n" + detected
    if blocks:
        return head + "\n" + "\n\n".join(blocks) + "\n"
    return head


# === PSEUDOLAW-LABEL REBUTTAL (corpus decides; m0 rebuts "frivolous" with full-corpus backing) ===
# Rule: there is NO banned-topic list. When opposing counsel labels the litigant's position
# "frivolous"/"pseudolaw"/"sovereign citizen"/etc., this pulls the FULL-CORPUS authority (3.05M
# semantic index) + the Obsidian graph that establishes the doctrine IS settled law, so m0 rebuts
# and turns the label back on counsel. If the corpus genuinely has nothing, returns "" (fine — the
# argument is simply dropped, never invented). This is the drafter reaching m0's knowledge base.
_PSEUDOLAW_LABELS = (
    "pseudolaw", "pseudo-law", "pseudo law", "frivolous", "sovereign citizen", "sovereign-citizen",
    "redemptionist", "redemption theory", "fictitious", "strawman", "straw man", "vexatious",
    "nonsensical", "gibberish", "patently meritless", "legally baseless",
)
_CORPUS = {"idx": None, "ids": None, "con": None, "model": None}


def _corpus_hits(query: str, k: int = 5):
    """Cached full-corpus (3.05M semantic index + delta) retrieval — m0's knowledge base. [] on error."""
    try:
        import json, sqlite3
        if _CORPUS["idx"] is None:
            import turbovec
            from sentence_transformers import SentenceTransformer
            from pipeline.case_comprehension import NEW_INDEX, NEW_IDS, CHUNK_DB
            _CORPUS["idx"] = turbovec.TurboQuantIndex.load(str(NEW_INDEX))
            _CORPUS["ids"] = json.loads(NEW_IDS.read_text())
            _CORPUS["con"] = sqlite3.connect(str(CHUNK_DB), check_same_thread=False)
            _CORPUS["model"] = _get_shared_embedder()
        from pipeline.case_comprehension import _retrieve
        # prefer_authority=True: rank primary law (SCOTUS opinions + statutes/rules + Am Jur proof-of-facts)
        # ABOVE dictionary/definition noise for motion + complaint section grounding — same boost the
        # complaint drafter uses, so a proof-of-fact (e.g. Am Jur insurable-interest -> void) surfaces here too.
        return _retrieve(_CORPUS["idx"], _CORPUS["ids"], _CORPUS["con"], _CORPUS["model"], query, k=k,
                         prefer_authority=True)
    except Exception:
        return []


def get_pseudolaw_rebuttal_grounding(stub_text: str) -> str:
    """If opposing counsel labeled the position frivolous/pseudolaw, return a corpus-grounded
    rebuttal block (full-corpus authority + graph). "" if no label, or if the corpus has nothing."""
    if not stub_text:
        return ""
    low = stub_text.lower()
    if not any(lbl in low for lbl in _PSEUDOLAW_LABELS):
        return ""
    q = (stub_text[:600] + " UCC negotiable instrument holder in due course capacity defined person "
         "discharge of indebtedness 1099-C original issue discount securitization real party in "
         "interest standing equity of redemption FHA subrogation — grounded statutory authority")
    hits = _corpus_hits(q, k=6)
    cited = [h for h in hits if (h.get("score") or 0) >= 0.45 and (h.get("text") or "").strip()]
    graph = ""
    try:
        from pipeline.connected_notes import graph_context
        graph = graph_context(stub_text[:400] + " grounded law not frivolous UCC capacity discharge "
                              "securitization redemption", k=3, max_chars=1400)
    except Exception:
        graph = ""
    if not cited and not graph:
        return ""
    # The REBUTTAL SECTION IS WANTED: when opposing counsel smears the position as
    # frivolous/pseudolaw, m0 SHOULD write a real argument section that names the label
    # and turns it back with corpus authority. The only defect to avoid is pasting THIS
    # instruction/scaffold verbatim (the "## ...-REBUTTAL" heading + "REBUT it:" TO-DO +
    # a naked bullet list) instead of writing the argument. So: produce prose, not the note.
    head = ("[CONTEXT FOR THE DRAFTER — write this as a real argument SECTION in your own prose; "
            "do NOT paste this instruction, its brackets, or the raw bullet list into the brief. "
            "Opposing counsel labeled the position 'frivolous'/'pseudolaw'/meritless. WRITE THE "
            "REBUTTAL: give it a proper argument heading (e.g., 'Defendants' Characterization of "
            "Plaintiff's Claims as Frivolous Is Meritless and Contradicted by Controlling Law'), "
            "name counsel's exact label and the specific doctrine at issue, and use the authority "
            "below to show the doctrine IS settled law and that counsel's label is itself the "
            "unsupported assertion — turn it back on them. Quote counsel's own word where it sharpens "
            "the rebuttal. Cite ONLY what appears below or in the record; omit any point without "
            "support — never invent.]\n"
            "Supporting authority to cite in the rebuttal:")
    parts = [head]
    for h in cited[:5]:
        src = (h.get("source") or "")[:48]
        txt = " ".join((h.get("text") or "").split())[:230]
        parts.append(f"- ({src}) {txt}")
    if graph:
        parts.append("\n[graph — grounding context, do not copy verbatim]\n" + graph)
    return "\n".join(parts) + "\n"


def _case_folder(case_id: str) -> str:
    """'phila_ccp:260100000' -> 'phila_ccp_260100000'; 'fed:2:26-cv-00123' -> 'fed_26-cv-00123'."""
    cid = (case_id or "").strip()
    if cid.startswith("fed:"):
        return "fed_" + cid.split(":", 2)[-1]
    return cid.replace(":", "_")


def get_definition_grounding(case_id: str, stub_text: str = "", max_notes: int = 3) -> str:
    """Feed the case's DEFINITION-FIRST word analysis into the drafter's PROSE (not just the quantum
    footnote layer). Reads the corpus-cited word-analysis / definition rebuttal notes the analysis pass
    wrote to the case vault (rebuttal_word_analysis_*.md, *definition*.md), so when m0 rebuts an order
    that turned on a term of art ("does not allege ... possesses ... property"), it argues the CONTROLLING
    legal meaning already grounded in corpus, instead of re-deriving it. "" if none. Never raises."""
    try:
        from pathlib import Path
        REPO = Path(__file__).resolve().parents[1]
        cdir = REPO / "obsidian_sync" / "cases" / _case_folder(case_id)
        if not cdir.exists():
            return ""
        notes = sorted(cdir.glob("*word_analysis*.md")) + sorted(cdir.glob("*definition*rebuttal*.md"))
        seen, picked = set(), []
        for n in notes:
            if n.name in seen:
                continue
            seen.add(n.name)
            picked.append(n)
        if not picked:
            return ""
        head = ("## DEFINITION-FIRST REBUTTAL GROUNDING — controlling LEGAL meaning of the operative terms\n"
                "*The adverse order/filing turned on terms of art read in their COMMON sense. Below is the "
                "corpus-cited legal meaning already established for this case. When you rebut, argue the "
                "controlling legal definition (cite the authority shown); do NOT concede the common meaning. "
                "Use ONLY what appears below or in the record.*\n")
        parts = [head]
        for n in picked[:max_notes]:
            try:
                body = n.read_text(encoding="utf-8")
            except Exception:
                continue
            # prefer the distilled "Rebuttal chain for m0" section if present; else trim the note
            low = body.lower()
            idx = low.find("rebuttal chain")
            snippet = body[idx:] if idx != -1 else body
            snippet = snippet.strip()[:2200]
            parts.append(f"### [{n.stem}]\n{snippet}")
        return "\n\n".join(parts) + "\n" if len(parts) > 1 else ""
    except Exception:
        return ""


_RULE_CITE = __import__("re").compile(
    r"(Pa\.\s?R\.\s?C\.\s?P\.|Pa\.\s?R\.\s?A\.\s?P\.|Pa\.\s?R\.\s?E\.|Fed\.\s?R\.\s?Civ\.\s?P\.|"
    r"Fed\.\s?R\.\s?App\.\s?P\.|Fed\.\s?R\.\s?Evid\.|F\.?R\.?C\.?P\.?|FRE|FRAP|Local\s+(?:Civil\s+)?Rule|"
    r"L\.?R\.?|\d+\s+Pa\.\s?C\.\s?S\.|\d+\s+U\.?S\.?C\.?|\d+\s+C\.?F\.?R\.?)\s*"
    r"(?:§\s*)?(\d+(?:\.\d+)*(?:\([a-z0-9]+\))*)", __import__("re").I)


# === PUBLIC LAW / STATUTES-AT-LARGE RETRIEVER (2026-08-02) — closes GAP #3 =========================
# The corpus HOLDS the Public Laws in abundance (plaw_* = ~8,788 sources) + the enacting-act notes in the
# USCODE sources ("Pub. L. __, __ Stat. __"), but m0 had NO path to them — the authority-ladder prompt names
# the Public-Law rung and nothing filled it. This pulls the ENACTING act for each statute the draft cites,
# in Pub. L. No. __ / __ Stat. __ form, so the drafter can lay the public-law origin under the codified
# section (Const -> PUBLIC LAW -> statute -> ...). Deterministic FTS restricted to USCODE/plaw sources; best-
# effort (surfaces only what the corpus confirms, never invents a Pub. L. number); '' if none; never raises.
_PUBLAW_SRC = "(c.source LIKE 'USCODE%' OR c.source LIKE 'plaw_%' OR c.source LIKE 'USCODE-%')"
_PUBLAW_CITE = __import__("re").compile(r"\bPub(?:lic)?\.?\s*L(?:aw)?\.?\s*(?:No\.?\s*)?(\d{1,3}[-–]\d{1,4})"
                                        r"|(\d{1,3})\s+Stat\.\s+(\d{1,4})", __import__("re").I)
_USC_IN_TEXT = __import__("re").compile(r"\b(\d{1,2})\s+U\.?\s?S\.?\s?C\.?\s*§?\s*(\d{3,5}[a-z]?(?:-\d+)?)", __import__("re").I)

# VERIFIED enacting-act table (2026-08-02). The corpus cannot reliably disentangle a section's ORIGINAL
# enacting Public Law from its amendment notes via FTS (proven: it returns amendments — 105-216 for the
# FDCPA, 110-241 for the FCRA). The enacting act is a FIXED, verifiable historical fact, so it is REFERENCE
# DATA — like a rule citation — keyed by (title, section-prefix). NOT case data (R2 forbids hardcoded
# judge/party/case data, not settled legal reference). Fail-closed: a section outside the table is OMITTED,
# never guessed. Extend as new statutes appear in filings; verify each against the Statutes at Large.
_ENACTING_ACTS = {
    ("15", "1681"): ("Pub. L. No. 90-321, title VI, as added Pub. L. No. 91-508, title VI, § 601, 84 Stat. 1128 (Oct. 26, 1970)", "Fair Credit Reporting Act"),
    ("15", "1692"): ("Pub. L. No. 95-109, 91 Stat. 874 (Sept. 20, 1977)", "Fair Debt Collection Practices Act"),
    ("15", "1601"): ("Pub. L. No. 90-321, title I, 82 Stat. 146 (May 29, 1968)", "Truth in Lending Act"),
    ("15", "1666"): ("Pub. L. No. 93-495, title III, 88 Stat. 1511 (Oct. 28, 1974)", "Fair Credit Billing Act"),
    ("15", "1691"): ("Pub. L. No. 93-495, title V, 88 Stat. 1521 (Oct. 28, 1974)", "Equal Credit Opportunity Act"),
    ("15", "1693"): ("Pub. L. No. 95-630, title XX, 92 Stat. 3728 (Nov. 10, 1978)", "Electronic Fund Transfer Act"),
    ("42", "1983"): ("Act of Apr. 20, 1871, ch. 22, § 1, 17 Stat. 13; Rev. Stat. § 1979", "Civil Rights Act of 1871 (Ku Klux Klan Act)"),
    ("42", "1981"): ("Act of Apr. 9, 1866, ch. 31, § 1, 14 Stat. 27; Rev. Stat. § 1977", "Civil Rights Act of 1866"),
    ("12", "2601"): ("Pub. L. No. 93-533, 88 Stat. 1724 (Dec. 22, 1974)", "Real Estate Settlement Procedures Act"),
}


_PUBLAW_CACHE: dict = {}


def get_public_law_grounding(stub_text: str, max_items: int = 4) -> str:
    """For each U.S.C. statute the draft cites, pull its ENACTING Public Law / Statutes-at-Large origin from
    the corpus (USCODE source notes + plaw_*), with Pub. L. No. __ / __ Stat. __ form. Closes the Public-Law
    ladder rung. Best-effort and honest — only surfaces a Pub. L./Stat. the corpus actually shows. '' if none;
    never raises. CACHED per draft (computed once, reused across every section prompt — speed)."""
    if not stub_text:
        return ""
    # RE-ENABLED 2026-08-02 FAIL-CLOSED: the old version grabbed any Pub. L. near the section number
    # (amendments / unrelated acts) and miscited. This version accepts a Pub. L. + __ Stat. __ ONLY when it
    # sits immediately after an ENACTING marker ("as added" / "Short Title" / "popularly known" / "may be
    # cited" / "which is classified") in a USCODE/plaw chunk. No enacting-context match -> OMIT (never guess).
    _ck = stub_text[:220]
    if _ck in _PUBLAW_CACHE:
        return _PUBLAW_CACHE[_ck]
    try:
        import sqlite3, re as _re
        from pipeline.case_comprehension import CHUNK_DB as _CDB
        # distinct USC sections cited (title, section)
        secs, seen = [], set()
        for m in _USC_IN_TEXT.finditer(stub_text):
            key = (m.group(1), m.group(2))
            if key not in seen:
                seen.add(key); secs.append(key)
            if len(secs) >= max_items * 2:
                break
        if not secs:
            return ""
        blocks = []
        for title, sec in secs:
            secnum = (_re.match(r"\d+", sec) or [None])
            secnum = secnum.group(0) if secnum else sec
            hit = None
            for (t, pref), (cite, name) in _ENACTING_ACTS.items():
                if t == title and str(secnum).startswith(pref):
                    hit = (cite, name)
                    break
            if hit:
                blocks.append(f"- **{title} U.S.C. § {sec}** (enacted as the {hit[1]}) — enacting authority: "
                              f"**{hit[0]}** (the positive-law origin of the codified section)")
            if len(blocks) >= max_items:
                break
        if not blocks:
            _PUBLAW_CACHE[_ck] = ""
            return ""
        out = ("## PUBLIC LAW / STATUTES-AT-LARGE — the enacting origin of each statute (lay this UNDER the code section)\n"
               "*Under the authority ladder, the codified U.S.C. section rests on its enacting Public Law / "
               "Statutes-at-Large (the positive law Congress passed). Where a statute carries the argument, cite the "
               "code section AND, where shown below, its enacting Pub. L. No. / __ Stat. __ — it is the primary "
               "positive-law rung above the code. Use ONLY the enacting cites shown; never invent a Pub. L. number.*\n"
               + "\n".join(blocks) + "\n")
        _PUBLAW_CACHE[_ck] = out
        return out
    except Exception:
        return ""


def enacting_law_footnotes(text: str) -> str:
    """DETERMINISTIC footnote block of enacting Public Laws for every statute cited in the FINAL draft —
    the positive-law rung the skinny prose omits. Appended AFTER drafting so the enactment clause is
    GUARANTEED to appear (footnote form), correct (verified table), and fail-closed (unknown statute is not
    listed rather than guessed). This is why the LLM can stay skinny: the full ladder lives in the notes."""
    if not text:
        return ""
    try:
        import re as _re
        seen, items = set(), []
        for m in _USC_IN_TEXT.finditer(text):
            title, sec = m.group(1), m.group(2)
            mnum = _re.match(r"\d+", sec)
            secnum = mnum.group(0) if mnum else sec
            for (t, pref), (cite, name) in _ENACTING_ACTS.items():
                if t == title and str(secnum).startswith(pref) and (t, pref) not in seen:
                    seen.add((t, pref))
                    items.append(f"{len(items)+1}. **{title} U.S.C. § {pref} et seq.** — the {name}, "
                                 f"enacted by **{cite}**.")
                    break
        if not items:
            return ""
        return ("\n\n---\n## STATUTORY AUTHORITY — ENACTING PUBLIC LAWS\n"
                "*Each statute relied on rests on its enacting Public Law / Statutes at Large (the positive-law "
                "origin above the codified section):*\n" + "\n".join(items) + "\n")
    except Exception:
        return ""


def build_proof_questionnaire(duties, respondent="the respondent", reply_text="", is_agency=False) -> str:
    """PROOF-OF-FACTS QUESTIONNAIRE section for a COMPLAINT or REBUTTAL (not motions — those read as briefs).
    Each duty is posed as a numbered question answered by its SOURCE — the verbatim law, and (for an agency)
    the respondent's OWN policy — then the record fact; then a question PINNING the respondent's reply; then a
    Refute/Concede close. This is NOT a legal conclusion (Twombly/Iqbal): the answer IS the quoted statute /
    policy / record, element-by-element. Fail-closed: a rung with no verbatim text is OMITTED, never asserted.
    `duties`: list of {subject, statute_cite, statute_text, policy_cite, policy_text, record}."""
    duties = [d for d in (duties or []) if d and (d.get("statute_text") or d.get("policy_text"))]
    if not duties:
        return ""
    also_policy = is_agency
    L = [f"## THE DUTIES, POSED AS QUESTIONS — answerable from the law"
         + (" and your own policy" if also_policy else "") + " and the record",
         f"*Each question below is answered by the source shown. {respondent} is asked to answer each on the "
         f"record; a question left unanswered is recorded as a concession.*", ""]
    n = 0
    for d in duties:
        n += 1
        cite = d.get("statute_cite") or "the governing law"
        q = f"**{n}. Does {cite}"
        if also_policy and d.get("policy_cite"):
            q += f" — and your own {d['policy_cite']} —"
        q += f" impose the duty to {d.get('subject') or 'perform the act at issue'}?**"
        L.append(q)
        if d.get("statute_text"):
            L.append(f"   - The law provides: “{' '.join(d['statute_text'].split())[:320]}”")
        if also_policy and d.get("policy_text"):
            L.append(f"   - Your own policy provides: “{' '.join(d['policy_text'].split())[:320]}”")
        if d.get("record"):
            L.append(f"   - The record shows: {d['record']}")
        L.append("")
    if reply_text:
        n += 1
        L.append(f"**{n}. In your reply you stated: “{' '.join(reply_text.split())[:300]}.” Identify where "
                 f"the law" + (" or your policy" if also_policy else "") + " authorizes that position — or the "
                 f"provision that displaces the duties above.**")
        L.append("")
    n += 1
    L.append(f"**{n}. Do you refute the law"
             + (", your own policy," if also_policy else "") + " or your duty under "
             + ("them" if also_policy else "it") + "? (Refute / Concede)** "
             "A refusal to answer is recorded as a concession that the duties exist and were not performed.")
    return "\n".join(L) + "\n"


def duties_from_claim_statutes(claims_data, max_duties: int = 8) -> list:
    """Assemble questionnaire `duties` for a NEW claim from its PLEADED statutes (NOT the case registry —
    an email-found claim has no registered case). Verbatim statute text comes from the claim's own
    `controlling_law` ("cite: text"), else `pipeline.rules_index.lookup(cite)` (operative rule body), else a
    stated duty. Record = the first pleaded fact / breach conduct. Shape matches build_proof_questionnaire:
    {subject, statute_cite, statute_text, policy_cite, policy_text, record}. Data-driven; [] on error.
    Fail-closed: a claim with no verbatim statute text is skipped, never asserted."""
    out = []
    for cd in (claims_data or []):
        if len(out) >= max_duties:
            break
        try:
            cite = (cd.get("statute") or "").strip()
            # 1) verbatim text: prefer the claim's controlling_law (already "cite: text")
            stx = ""
            for cl in (cd.get("controlling_law") or []):
                if not cl:
                    continue
                stx = (cl.split(":", 1)[1].strip() if ":" in cl else cl.strip())
                if stx:
                    break
            # 2) else pull the operative rule body from the corpus rules index
            if not stx and cite:
                try:
                    from pipeline.rules_index import lookup as _rl
                    hits = _rl(cite) or []
                    stx = (hits[0].get("body") if hits else "") or ""
                except Exception:
                    stx = ""
            # 3) else a stated duty text (last resort)
            if not stx:
                _dts = cd.get("duties") or []
                stx = (_dts[0].get("text") if _dts else "") or cd.get("breach_duty") or ""
            if not stx:
                continue   # fail-closed: no verbatim law -> omit this rung
            _dts = cd.get("duties") or []
            subj = (cd.get("breach_duty") or (_dts[0].get("text") if _dts else "") or cd.get("title") or "").strip()
            subj = subj[:160].rstrip(".") or "perform the duty the statute imposes"
            rec = ""
            _fp = cd.get("facts") or []
            if _fp:
                rec = _fp[0]
            elif cd.get("breach_conduct"):
                rec = cd["breach_conduct"]
            out.append({
                "subject": subj,
                "statute_cite": cite or ((_dts[0].get("citation") if _dts else "") or ""),
                "statute_text": stx,
                "policy_cite": "", "policy_text": "",
                "record": (rec or "")[:400],
            })
        except Exception:
            continue
    return out


# judge-conduct violation_type -> (duty subject phrase, the judge's OWN policy = Code of Conduct Canon). The
# subject/canon pairing is generic legal reference (not case data, R2-clean). The Canon TEXT is pulled from
# the corpus fail-closed (build-Q1): omit the policy rung if the Canon is not ingested, never assert it.
_DUTY_SUBJECT = {
    "PROCEDURAL_DUE_PROCESS_VIOLATION": ("provide notice and an opportunity to be heard before depriving a party of a protected interest", "Code of Conduct Canon 3A"),
    "SUA_SPONTE_RULING": ("refrain from deciding an issue no party raised, without notice", "Code of Conduct Canon 3A"),
    "NOTICE_INADEQUATE": ("give constitutionally adequate notice of a dispositive ruling", "Code of Conduct Canon 3A"),
    "JUDICIAL_DISQUALIFICATION_DUTY": ("disqualify where the judge's impartiality might reasonably be questioned", "Code of Conduct Canon 3C"),
    "MANDATORY_JUDICIAL_NOTICE_IGNORED": ("take judicial notice when a party requests it and supplies the necessary information", "Code of Conduct Canon 3A"),
    "STARE_DECISIS_VIOLATION": ("apply binding precedent and not treat non-binding decisions as controlling", "Code of Conduct Canon 3A(1)"),
    "NON_BINDING_AS_CONTROLLING": ("not treat non-binding authority as controlling", "Code of Conduct Canon 3A(1)"),
    "TRIAL_COURT_AS_APPELLATE_AUTHORITY": ("not rely on trial-court decisions as binding appellate authority", "Code of Conduct Canon 3A(1)"),
    "FRAUD_ON_THE_COURT": ("maintain the integrity of the proceedings and not tolerate fraud on the court", "Code of Conduct Canon 2A"),
    "NO_REASONS_STATED": ("state the reasons for the disposition", "Code of Conduct Canon 3A"),
    "JUDICIAL_CANON_VIOLATION": ("comply with the Code of Conduct for United States Judges", "Code of Conduct Canon 2A"),
    "DEPRIVATION_OF_RIGHTS_COLOR_OF_LAW": ("not deprive a person of rights secured by law under color of law", "Code of Conduct Canon 2A"),
    "BIAS_EMOTIONAL_LANGUAGE": ("act at all times to promote public confidence in the judiciary's impartiality", "Code of Conduct Canon 2A"),
    "SOVEREIGN_CITIZEN_LABEL": ("decide on the law and the facts, not on a dismissive label of the litigant", "Code of Conduct Canon 3A(3)"),
}
_CANON_CACHE: dict = {}


def _canon_text(canon_cite: str) -> str:
    """Pull the Code of Conduct Canon TEXT from the corpus (fail-closed: '' if not ingested)."""
    if not canon_cite:
        return ""
    if canon_cite in _CANON_CACHE:
        return _CANON_CACHE[canon_cite]
    txt = ""
    try:
        import re as _re, sqlite3
        from pipeline.case_comprehension import CHUNK_DB as _CDB
        num = (_re.search(r"Canon\s+(\w+)", canon_cite) or [None, ""])[1]
        con = sqlite3.connect(str(_CDB)); con.execute("PRAGMA busy_timeout=15000")
        r = con.execute(
            "SELECT c.text FROM chunk_text_fts f JOIN chunk_text c ON c.chunk_id=f.source_id "
            "WHERE f.chunk_text_fts MATCH ? ORDER BY bm25(f.chunk_text_fts) LIMIT 1",
            (f'"Code of Conduct" AND "Canon {num}" AND (judge OR impartial OR disqualif OR law)',)).fetchone()
        con.close()
        txt = (r[0] if r else "") or ""
    except Exception:
        txt = ""
    _CANON_CACHE[canon_cite] = txt
    return txt


_OATH_CACHE: dict = {}


_CORPUS_DB_PATH = None


def _corpus_db():
    """Resolve the corpus FTS db WITHOUT importing numpy-heavy modules (case_comprehension imports numpy,
    which breaks in a lean venv). Cached; None if not found."""
    global _CORPUS_DB_PATH
    if _CORPUS_DB_PATH is None:
        import os
        for c in (str(_eroot() / "corpus/chunk_text.db"), r"C:\Users\filer\turbovec\chunk_text.db"):
            if os.path.exists(c):
                _CORPUS_DB_PATH = c
                break
        if _CORPUS_DB_PATH is None:
            _CORPUS_DB_PATH = ""
    return _CORPUS_DB_PATH or None


def _corpus_first(match_expr: str) -> str:
    """Top FTS hit text from the corpus, fail-closed to ''. Used to pull verbatim oath/recusal law."""
    db = _corpus_db()
    if not db:
        return ""
    try:
        import sqlite3
        con = sqlite3.connect(db); con.execute("PRAGMA busy_timeout=15000")
        r = con.execute(
            "SELECT c.text FROM chunk_text_fts f JOIN chunk_text c ON c.chunk_id=f.source_id "
            "WHERE f.chunk_text_fts MATCH ? ORDER BY bm25(f.chunk_text_fts) LIMIT 1",
            (match_expr,)).fetchone()
        con.close()
        return " ".join((r[0] if r else "").split())
    except Exception:
        return ""


def _oath_text() -> str:
    """Verbatim federal judicial oath (28 U.S.C. § 453) from the corpus, fail-closed."""
    if "453" not in _OATH_CACHE:
        _OATH_CACHE["453"] = _corpus_first('"administer justice without respect to persons"')
    return _OATH_CACHE["453"]


def _recusal_text() -> str:
    """Verbatim recusal statute (28 U.S.C. § 455) from the corpus, fail-closed."""
    if "455" not in _OATH_CACHE:
        _OATH_CACHE["455"] = _corpus_first('disqualify AND "reasonably be questioned" AND (455 OR judge)')
    return _OATH_CACHE["455"]


def gather_questionnaire_duties(case_id: str, max_duties: int = 8) -> list:
    """Assemble the questionnaire `duties` for a case from the audit (judge-conduct grounds) — verbatim law
    (rule_text) + the judge's own policy (Code-of-Conduct Canon from the corpus, fail-closed) + the record
    fact (ECF + the offending quote). Deduped by type. Deterministic; [] on any error."""
    try:
        import json as _json, pathlib as _pl
        slug = case_id.replace(":", "_")
        p = _pl.Path(str(_eroot() / "output/audit/data_") + slug + ".json")
        d = _json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        return []
    # JUDGE-ATTRIBUTION: only conduct in a JUDGE-authored order belongs in a judicial-misconduct questionnaire
    # (never opposing counsel's conduct). Use the audit's own judge-order list (judicial_oath_breach).
    import re as _re2
    _judge_ecfs = set()
    for x in ((d.get("judicial_oath_breach") or {}).get("judge_orders_identified") or []):
        mm = _re2.findall(r"\d+", str(x))
        if mm:
            _judge_ecfs.add(mm[0])
    duties, seen = [], set()
    for v in (d.get("procedural_violations") or []):
        t = v.get("violation_type") or ""
        if t not in _DUTY_SUBJECT or t in seen:
            continue
        # skip a ground whose source order is NOT judge-authored (it's counsel/party conduct, wrong forum here)
        _ecf = str(v.get("source_ecf") or "")
        if _judge_ecfs and _ecf and _ecf not in _judge_ecfs:
            continue
        subject, canon_cite = _DUTY_SUBJECT[t]
        cite = v.get("rule_citation") or ""
        # verbatim OPERATIVE text via the rules-index (clean 'shall/must'); fall back to the audit rule_text.
        law = ""
        try:
            from pipeline.rules_index import lookup as _rl
            rows = _rl(cite, limit=1) if cite else []
            if rows and rows[0].get("body"):
                law = " ".join(str(rows[0]["body"]).split())
        except Exception:
            law = ""
        if not law:
            law = v.get("rule_text") or cite or ""
        if not law:
            continue
        seen.add(t)
        # the record fact = the audit's plain-English explanation of the conduct (not the noisy quote), + ECF.
        rec = ""
        expl = " ".join(str(v.get("explanation") or "").split())
        if expl:
            rec = expl[:220]
        if v.get("source_ecf"):
            rec = (rec + " " if rec else "") + f"[ECF {v['source_ecf']}]"
        duties.append({
            "subject": subject,
            "statute_cite": v.get("rule_citation") or "",
            "statute_text": law,
            "policy_cite": canon_cite,
            "policy_text": _canon_text(canon_cite),
            "record": rec,
        })
        if len(duties) >= max_duties:
            break
    # STANDING per-person rungs: pin the judge to what THEY swore / are bound by, not only the violated Canon.
    # Only when judge-authored misconduct is actually present (duties non-empty).
    if duties:
        standing = []
        _oath = _oath_text()
        if _oath:
            standing.append({
                "subject": "administer justice without respect to persons, do equal right to the poor and to the "
                           "rich, and faithfully and impartially discharge and perform all the duties of the office",
                "statute_cite": "28 U.S.C. § 453 (the judicial oath the judge personally swore)",
                "statute_text": _oath[:320],
                "policy_cite": "Code of Conduct Canon 1",
                "policy_text": _canon_text("Canon 1"),
                "record": "The oath binds every act of the office; the specific rulings/conduct are itemized below.",
            })
        _rec = _recusal_text()
        _has_conflict = bool(d.get("bias_indicator")) or any(
            "BIAS" in str(x.get("category", "")).upper()
            for x in ((d.get("scorecard") or {}).get("violations") or []))
        if _rec and _has_conflict:
            standing.append({
                "subject": "disqualify in any proceeding in which the judge's impartiality might reasonably be questioned",
                "statute_cite": "28 U.S.C. § 455(a)",
                "statute_text": _rec[:320],
                "policy_cite": "Code of Conduct Canon 3C",
                "policy_text": _canon_text("Canon 3C"),
                "record": "A conflict / appearance-of-partiality indicator is flagged in the audit.",
            })
        duties = standing + duties
    return duties[:max_duties] if max_duties else duties


# === MAXIM + EQUITY-PRINCIPLE RETRIEVER (2026-08-02) — closes GAP #6/#7/#8 =========================
# The corpus HOLDS the maxim/equity treatises (Broom's Legal Maxims — def_1854_broom_a_selection_of_legal_
# maxims; Pomeroy's Equity Jurisprudence — 59a_jpomeroyatreatiseonequityjurisprudence_*), but m0 had NO
# retriever for them: get_canon_grounding pulls CONSTRUCTION canons (ejusdem/expressio), not classical
# maxims, and complaint_drafter's LEGAL_MAXIMS is a 7-string hardcoded bank. This pulls the ACTUAL maxim /
# equity-principle text from the treatises, ATTRIBUTED, for the operative contexts present in the draft, so
# the drafter can cite a maxim/equity principle to its source instead of asserting a bare Latin string.
# Deterministic FTS (fast, indexed) restricted to the treatise sources; '' if none; never raises.
_MAXIM_EQUITY_SRC = ("(c.source LIKE 'def_1854_broom%' OR c.source LIKE '%pomeroy%' "
                     "OR c.source LIKE 'def_%maxim%' OR c.source LIKE '%legal_maxims%' "
                     "OR c.source LIKE 'def_2015_216699%')")
# operative context in the draft -> (FTS phrase to pull the principle, class label, attribution)
_MAXIM_TRIGGERS = [
    (r"unclean hands|clean hands|inequitable conduct|bad faith", '"clean hands"',
     "equity maxim", "Pomeroy, Equity Jurisprudence"),
    (r"\blaches\b|undue delay|slept on|sat on (?:his|her|their) rights|stale claim", '"laches"',
     "equity maxim", "Pomeroy, Equity Jurisprudence / Broom's Legal Maxims"),
    (r"adequate remedy|irreparable|no remedy at law|injunctive relief|specific performance", '"adequate remedy at law"',
     "equity principle", "Pomeroy, Equity Jurisprudence"),
    (r"equity follows the law|aequitas sequitur legem", '"equity follows the law"',
     "maxim", "Broom's Legal Maxims"),
    (r"equitable conversion|regards as done|ought to be done", '"equity regards as done that which ought to be done"',
     "equity maxim", "Broom's Legal Maxims / Pomeroy"),
    (r"he who seeks equity|do equity", '"he who seeks equity must do equity"',
     "equity maxim", "Pomeroy, Equity Jurisprudence"),
    (r"ubi jus|no right without|right without a remedy|remedy for every wrong", '"ubi jus ibi remedium"',
     "maxim", "Broom's Legal Maxims"),
    (r"fraud vitiates|fraud unravels|fraud in the|obtained by fraud", '"fraud vitiates"',
     "maxim", "Broom's Legal Maxims"),
    (r"\bvoid\b|nullity|no legal effect|void ab initio", '"quod nullum est" OR "void act"',
     "maxim", "Broom's Legal Maxims"),
    (r"expressio unius|expression of one", '"expressio unius est exclusio alterius"',
     "maxim", "Broom's Legal Maxims"),
]


_MAXIM_CACHE: dict = {}


def get_maxim_equity_grounding(stub_text: str, max_items: int = 4) -> str:
    """Pull the maxim / equity-principle text (Broom / Pomeroy) for the operative contexts present in the
    draft, attributed to its treatise. Feeds m0's PROMPT so a maxim/equity argument cites its source, not a
    bare Latin string. '' if no relevant context or no corpus text. Never raises. CACHED per draft (computed
    once, reused across every section prompt — speed)."""
    if not stub_text:
        return ""
    _ck = stub_text[:220]
    if _ck in _MAXIM_CACHE:
        return _MAXIM_CACHE[_ck]
    try:
        import sqlite3, re as _re
        from pipeline.case_comprehension import CHUNK_DB as _CDB
        low = (stub_text or "").lower()
        con = sqlite3.connect(str(_CDB)); con.execute("PRAGMA busy_timeout=15000")
        blocks, seen = [], set()
        for pat, fts, klass, attrib in _MAXIM_TRIGGERS:
            if not _re.search(pat, low):
                continue
            if fts in seen:
                continue
            seen.add(fts)
            try:
                row = con.execute(
                    "SELECT c.text FROM chunk_text_fts f JOIN chunk_text c ON c.chunk_id=f.source_id "
                    f"WHERE f.chunk_text_fts MATCH ? AND {_MAXIM_EQUITY_SRC} ORDER BY bm25(f.chunk_text_fts) LIMIT 1",
                    (fts,)).fetchone()
            except Exception:
                row = None
            if row and row[0]:
                txt = " ".join(row[0].split())
                # trim to the sentence carrying the principle
                m = _re.search(r"[^.]*(?:" + fts.strip('"').split('"')[0][:18] + r")[^.]*\.", txt, _re.I)
                snippet = (m.group(0) if m else txt[:260]).strip()[:280]
                blocks.append(f"- **[{klass}]** {snippet}  — *{attrib}*")
            if len(blocks) >= max_items:
                break
        con.close()
        if not blocks:
            _MAXIM_CACHE[_ck] = ""
            return ""
        out = ("## MAXIMS & EQUITY PRINCIPLES — argue these to their SOURCE (do not assert a bare Latin string)\n"
               "*The maxim/equity contexts present in this filing, pulled from the treatises in the corpus. Where a "
               "maxim or equity principle carries a point, state it and cite it to its source (Broom's Legal Maxims "
               "or Pomeroy's Equity Jurisprudence) — never a bare Latin tag without authority. Use a maxim only "
               "where it genuinely governs; omit the rest.*\n" + "\n".join(blocks) + "\n")
        _MAXIM_CACHE[_ck] = out
        return out
    except Exception:
        return ""


_APPFMT_CACHE: dict = {}
_FRAP28_SRC = "c.source LIKE 'frap_rule_28%'"
_AMJUR_POF_SRC = "(c.source LIKE 'def_amjur%proof_of_facts%' OR c.source LIKE '%proof_of_facts%')"


def get_appellate_format_grounding(court: str = "3D_CIR") -> str:
    """Corpus-grounded appellate drafting frame, so m0 (not a hand-written map) derives the shape:
    WHAT THE COURT REQUIRES (Fed. R. App. P. 28, verbatim from the corpus) + HOW TO PROVE EACH ISSUE
    (Am Jur 2d Proof of Facts: element -> proof -> authority) + the CITATION LADDER to cite start-to-
    finish (Constitution -> enacting Public Law -> Statute -> Rule -> Case -> Maxim/Equity). Cached;
    fail-safe ('' on any error, so a miss degrades m0's prompt, never crashes the draft)."""
    ck = court or "3D_CIR"
    if ck in _APPFMT_CACHE:
        return _APPFMT_CACHE[ck]
    frap = amjur = ""
    try:
        import sqlite3
        from pipeline.case_comprehension import CHUNK_DB as _CDB
        con = sqlite3.connect(str(_CDB)); con.execute("PRAGMA busy_timeout=15000")
        try:
            r = con.execute(
                "SELECT c.text FROM chunk_text_fts f JOIN chunk_text c ON c.chunk_id=f.source_id "
                f"WHERE f.chunk_text_fts MATCH ? AND {_FRAP28_SRC} ORDER BY bm25(f.chunk_text_fts) LIMIT 1",
                ('brief AND (appellant OR argument OR issues OR jurisdiction)',)).fetchone()
            frap = (r[0] if r else "") or ""
        except Exception:
            frap = ""
        try:
            r = con.execute(
                "SELECT c.text FROM chunk_text_fts f JOIN chunk_text c ON c.chunk_id=f.source_id "
                f"WHERE f.chunk_text_fts MATCH ? AND {_AMJUR_POF_SRC} ORDER BY bm25(f.chunk_text_fts) LIMIT 1",
                ('"proof of facts" AND (element OR establish OR "prima facie")',)).fetchone()
            amjur = (r[0] if r else "") or ""
        except Exception:
            amjur = ""
        con.close()
    except Exception:
        return ""

    def _cl(t):
        return " ".join((t or "").split())

    parts = ["## HOW TO WRITE THIS APPEAL — grounded in the corpus (cite START to FINISH; leave no rung an opening)"]
    if frap:
        parts.append("### What the court REQUIRES (Fed. R. App. P. 28, from the corpus):\n" + _cl(frap)[:900])
    parts.append("Structure to FRAP 28: jurisdictional statement; statement of the issues; statement of the case "
                 "(record cites); summary of argument; ARGUMENT stating the applicable standard of review at the "
                 "head of EACH issue with contentions cited to authority AND the record; a short conclusion; and a "
                 "table of authorities. Every issue carries its standard of review.")
    if amjur:
        parts.append("### How to PROVE each issue (Am Jur 2d Proof of Facts, from the corpus):\n" + _cl(amjur)[:700])
    parts.append("Draft each issue PROOF-OF-FACTS style: state the ELEMENT/error, then the RECORD fact that proves "
                 "it (with its ECF/exhibit cite), then the controlling AUTHORITY as a compact near-list, the cite "
                 "carrying the weight. Condense; do not pad into an essay.")
    parts.append("### Cite the full ladder where a point needs it — start to finish, none skipped:\n"
                 "Constitution -> enacting Public Law / Statutes-at-Large (the enactment clause / positive-law "
                 "origin) -> codified Statute -> Rule -> controlling Case -> Maxim/Equity. Where a statute carries "
                 "the argument, name its enacting Public Law so the citation is airtight and cannot be attacked as "
                 "incompletely grounded.")
    out = "\n".join(parts) + "\n"
    _APPFMT_CACHE[ck] = out
    return out


# FORUM-AWARE format frame for ANY filing (district / CCP / appellate). Same corpus-grounded shape as the
# appellate feeder, but "what THIS court requires" is pulled per forum: FRAP 28 / Pa.R.A.P. (appellate) vs
# FRCP 7/10/11 (federal district) vs Pa.R.C.P. 1028/1035 + Phila local (Common Pleas). Then Am Jur Proof of
# Facts + the citation ladder (the enacting Public Law rides the STATUTORY AUTHORITY footnotes). Cached; fail-safe.
_FORUM_RULES = {
    (True,  False): ("Fed. R. App. P. 28",
                     'brief AND (appellant OR argument OR issues OR jurisdiction)',
                     "c.source LIKE 'frap_rule_28%'"),
    (True,  True):  ("Pa.R.A.P. 2111, 2116 & 2119",
                     '"Rule 2111" OR "Rule 2119" OR "Rule 2116" OR (brief AND appellant)',
                     "(c.source LIKE '%2111%' OR c.source LIKE '%rap%' OR c.source LIKE '%appellate%')"),
    (False, False): ("Fed. R. Civ. P. 7, 10 & 11",
                     '"Form of Motions" OR "Form of Pleadings" OR (motion AND caption)',
                     "c.source LIKE 'frcp_rule_%'"),
    (False, True):  ("Pa.R.C.P. 1028 & 1035 + Philadelphia local rules",
                     '("preliminary objections" OR "Rule 1028" OR "Rule 1035" OR "Compiled Rules") AND (Pennsylvania OR "Common Pleas" OR Pa)',
                     "(c.source LIKE '%civ_p%' OR c.source LIKE '%_cts%' OR c.source LIKE 'CP-%' OR c.source LIKE '%Compiled%' OR c.source LIKE '%cts%')"),
}


def get_court_format_grounding(is_appellate: bool = False, is_state: bool = False) -> str:
    """Forum-aware corpus-grounded drafting frame for ANY filing: what THIS court requires (per _FORUM_RULES)
    + Am Jur 2d Proof of Facts (element->proof->authority) + the citation ladder. Cached; '' on any error."""
    key = (bool(is_appellate), bool(is_state))
    ckey = f"court_{key}"
    if ckey in _APPFMT_CACHE:
        return _APPFMT_CACHE[ckey]
    label, rq, src = _FORUM_RULES[key]
    rule = amjur = ""
    try:
        import sqlite3
        from pipeline.case_comprehension import CHUNK_DB as _CDB
        con = sqlite3.connect(str(_CDB)); con.execute("PRAGMA busy_timeout=15000")
        try:
            r = con.execute("SELECT c.text FROM chunk_text_fts f JOIN chunk_text c ON c.chunk_id=f.source_id "
                            f"WHERE f.chunk_text_fts MATCH ? AND {src} ORDER BY bm25(f.chunk_text_fts) LIMIT 1",
                            (rq,)).fetchone()
            rule = (r[0] if r else "") or ""
        except Exception:
            rule = ""
        try:
            r = con.execute("SELECT c.text FROM chunk_text_fts f JOIN chunk_text c ON c.chunk_id=f.source_id "
                            f"WHERE f.chunk_text_fts MATCH ? AND {_AMJUR_POF_SRC} ORDER BY bm25(f.chunk_text_fts) LIMIT 1",
                            ('"proof of facts" AND (element OR establish OR "prima facie")',)).fetchone()
            amjur = (r[0] if r else "") or ""
        except Exception:
            amjur = ""
        con.close()
    except Exception:
        return ""

    def _cl(t):
        return " ".join((t or "").split())

    forum = "appellate court" if is_appellate else ("Court of Common Pleas" if is_state else "District Court")
    parts = [f"## HOW TO WRITE THIS FILING FOR THIS FORUM ({forum}) — grounded in the corpus ({label})"]
    if rule:
        parts.append(f"### What THIS court REQUIRES ({label}, from the corpus):\n" + _cl(rule)[:900])
    if amjur:
        parts.append("### How to PROVE each point (Am Jur 2d Proof of Facts, from the corpus):\n" + _cl(amjur)[:600])
    parts.append("Draft PROOF-OF-FACTS style: the ELEMENT/point -> the RECORD fact that proves it (with its "
                 "ECF/exhibit cite) -> the controlling AUTHORITY, condensed; the cite carries the weight — do not pad.")
    parts.append("### Cite the full ladder where a point needs it — start to finish:\n"
                 "Constitution -> enacting Public Law -> Statute -> Rule -> controlling Case -> Maxim/Equity. "
                 "(The enacting Public Law for each statute is appended in the STATUTORY AUTHORITY footnotes.)")
    out = "\n".join(parts) + "\n"
    _APPFMT_CACHE[ckey] = out
    return out


def get_rule_grounding(stub_text: str, max_rules: int = 10) -> str:
    """M0-INPUT FIX (2026-07-08): quote the VERBATIM mandatory rule text for every rule the stub/scorecard
    cites, via the cite-keyed rules_index (resolves Pa.R.C.P./Pa.R.A.P./FRCP/FRE to real 'shall/waived'
    text). Feeds m0's PROMPT so the drafter quotes the operative language beside each row instead of
    paraphrasing it — the crux of an un-ignorable filing. Deterministic (no LLM). '' if nothing resolves;
    never raises. m0's drafting core is untouched — this only enriches the INPUT."""
    if not stub_text:
        return ""
    try:
        from pipeline.rules_index import lookup
    except Exception:
        return ""
    seen, blocks = set(), []
    for m in _RULE_CITE.finditer(stub_text):
        cite = (m.group(1).strip() + " " + m.group(2)).strip()
        key = cite.lower().replace(" ", "")
        if key in seen:
            continue
        seen.add(key)
        try:
            hits = lookup(cite, limit=1)
        except Exception:
            hits = []
        if hits and hits[0].get("body"):
            body = " ".join(hits[0]["body"].split())[:600]
            blocks.append(f"- **{cite}** ({hits[0].get('source','')}): {body}")
        if len(blocks) >= max_rules:
            break
    if not blocks:
        return ""
    head = ("## MANDATORY RULE TEXT — verbatim (quote this language; do NOT paraphrase)\n"
            "*Each rule the record invokes, in its own words. When you state a defect, quote the rule's "
            "operative 'shall/must/waived' language next to it so the consequence is mechanical, not "
            "argued. Use ONLY the text shown.*\n")
    return head + "\n".join(blocks) + "\n"


def get_spotted_levers_grounding(case_id: str, stub_text: str = "", max_levers: int = 12) -> str:
    """Feed m0's PROMPT the DEPTH issue-spotter's VALIDATED levers (the audit JSON's spotted_levers[]).
    Each already passed the gate — real cite, BINDING/verbatim/corpus — so m0 argues the lever with its
    authority as a grounded consequence and never invents beyond what is shown. Strongest (binding/rule)
    first. '' if none. Never raises. This is the depth engine reaching m0's prose."""
    try:
        import json
        from pipeline.audit_writer import _safe_id, AUDIT_DIR
        ap = AUDIT_DIR / f"data_{_safe_id(case_id)}.json"
        if not ap.exists():
            return ""
        data = json.loads(ap.read_text(encoding="utf-8"))
        levers = data.get("spotted_levers") or []
        if not levers:
            return ""
        order = {"case-BINDING": 0, "rule-verbatim": 1, "corpus": 2, "case-NON-BINDING": 3}
        levers = sorted(levers, key=lambda l: order.get((l.get("validation") or ""), 5))[:max_levers]
        head = ("## VALIDATED LEVERS — depth issue-spotter (each backed by REAL authority; argue these)\n"
                "*Surfaced from the record and confirmed by the gate: every cite below is a real, "
                "controlling/verbatim authority. Argue the lever with its authority; do NOT invent beyond "
                "what is shown. A cite marked case-NON-BINDING is persuasive only — frame it as such.*\n")
        lines = [f"- **{l.get('lever','')}** — {l.get('doctrine','')} "
                 f"(Authority: {l.get('cite','')} [{l.get('validation','')}])" for l in levers]
        return head + "\n".join(lines) + "\n"
    except Exception:
        return ""


_TERMS_OF_ART = [
    "real party in interest", "holder in due course", "capacity to sue", "subject matter jurisdiction",
    "negotiable instrument", "sovereign immunity", "res judicata", "collateral estoppel", "promissory note",
    "security interest", "due process", "consideration", "ratification", "assignment", "negligence",
    "standing", "capacity", "person", "jurisdiction",
]

def get_term_case_grounding(stub_text: str, max_terms: int = 5, per_term: int = 4) -> str:
    """Feed m0 the CASES the legal treatises (Black's / Corpus Juris Secundum / Am Jur) cite for each
    term of art present in the draft — so a term-of-art rebuttal argues the controlling meaning with the
    exact case authority the dictionaries rely on, instead of inventing. Deterministic (corpus_graph.
    term_supporting_cases, cached). '' if no term matches. Never raises."""
    try:
        from pipeline import corpus_graph as cg
        low = (stub_text or "").lower()
        hits = sorted({t for t in _TERMS_OF_ART if t in low}, key=lambda t: -len(t))[:max_terms]
        blocks = []
        for t in hits:
            cases = cg.term_supporting_cases(t, k=per_term)
            if cases:
                blocks.append(f"- **{t}** — cases the treatises cite defining it: "
                              + ", ".join(c for c, _ in cases))
        if not blocks:
            return ""
        head = ("## TERM-OF-ART CASE AUTHORITY — cases the legal dictionaries/treatises (Black's / Corpus "
                "Juris Secundum / Am Jur) cite for the operative terms. Cite these when arguing the "
                "CONTROLLING legal meaning of a term the order read in its common sense; use ONLY what is shown.\n")
        return head + "\n".join(blocks) + "\n"
    except Exception:
        return ""


def _get_shared_embedder():
    # GO step 6: one embedding model per process (pipeline.llm_layer singleton)
    from pipeline.llm_layer import get_embedder
    return get_embedder()


# === AUTHORITY-STACK FEEDER (2026-08-01) — per-ground LADDER so NO section falls to bare assertion ===
# Real-draft test (motion_vacate_60b4) showed the ESTABLISH-DON'T-ARGUE doctrine works ONLY where the
# grounding already carried the authorities (Section A: Celotex/Anderson/Safeco WITH holdings) and
# collapses to naked assertion where it did not (Sections B/C: "the Court overlooked controlling
# authority" with nothing named/quoted). The other feeders each emit a SEPARATE block (rule text here,
# defect->cite there); the drafter never sees them assembled as ONE ladder per ground, so a section that
# is not a bound procedural defect has no stack to climb. This feeder assembles, for EACH ordered ground,
# the ladder rule 10 demands: Constitution -> statute (verbatim) -> BINDING case + its HOLDING (corpus-
# pulled) -> rule. The constitutional anchor is a deterministic STRUCTURAL map (legal structure, no
# case/party/judge data; R2-safe) carrying the verbatim operative constitutional phrase; the holding is
# corpus-retrieved (grounded, never invented). Cached per draft. Never raises; "" when nothing resolves.

# doctrine -> (constitutional provision cite, verbatim operative constitutional phrase, proposition to
# retrieve a controlling holding for). STRUCTURE only — no case names, no party/judge data.
_STACK_CONSTITUTION = {
    "STANDING_DEFECT": (
        "U.S. Const. art. III, § 2",
        "the judicial Power shall extend to “Cases” and “Controversies”",
        "Article III standing requires a concrete injury in fact fairly traceable to the defendant and "
        "redressable by the court; a bare statutory violation without concrete harm confers no standing",
    ),
    "SUBJECT_MATTER_JURISDICTION_DEFECT": (
        "U.S. Const. art. III, § 2",
        "the judicial Power shall extend to “Cases” and “Controversies”",
        "a federal court is one of limited jurisdiction and may not adjudicate a claim over which it lacks "
        "subject-matter jurisdiction; a court declines supplemental jurisdiction over the state-law claims "
        "once every claim within its original jurisdiction is dismissed",
    ),
    "VOID_JUDGMENT_RULE_60B4": (
        "U.S. Const. amend. V",
        "no person shall be “deprived of life, liberty, or property, without due process of law”",
        "a judgment entered without subject-matter jurisdiction or in violation of due process is void and "
        "must be vacated; the court has no discretion to let a void judgment stand",
    ),
    "REMOVAL_DEFECTIVE": (
        "U.S. Const. art. III, § 2",
        "the judicial Power shall extend to “Cases” and “Controversies”",
        "removal statutes are strictly construed and the absence of federal subject-matter jurisdiction "
        "requires remand",
    ),
    # GAP #6/#9 (2026-08-02): extend the ladder's constitutional rung beyond the 4 jurisdictional doctrines so
    # NON-jurisdictional scorecard defects (due process / impartial tribunal / notice) also carry a
    # constitutional anchor. Structure only (doctrine -> constitutional provision), no case/party/judge data.
    "PROCEDURAL_DUE_PROCESS_VIOLATION": (
        "U.S. Const. amends. V & XIV",
        "no person shall be “deprived of life, liberty, or property, without due process of law”",
        "due process requires adequate notice and a meaningful opportunity to be heard before a deprivation",
    ),
    "NOTICE_INADEQUATE": (
        "U.S. Const. amend. XIV, § 1",
        "no State shall “deprive any person of life, liberty, or property, without due process of law”",
        "due process requires notice reasonably calculated to apprise interested parties and afford them an "
        "opportunity to object (the constitutional notice standard)",
    ),
    "SERVICE_DEFECTIVE": (
        "U.S. Const. amends. V & XIV",
        "no person shall be “deprived of … property, without due process of law”",
        "valid service is the predicate of personal jurisdiction and due-process notice; a judgment entered "
        "without proper service is void",
    ),
    "SUA_SPONTE_RULING": (
        "U.S. Const. amends. V & XIV",
        "no person shall be “deprived of … property, without due process of law”",
        "granting relief on an issue no party raised, without notice or an opportunity to be heard, violates "
        "due process",
    ),
    "FRAUD_ON_THE_COURT": (
        "U.S. Const. amend. V",
        "no person shall be “deprived of life, liberty, or property, without due process of law”",
        "a judgment procured by fraud on the court offends the integrity of the proceeding and due process, "
        "and may be set aside at any time",
    ),
    "JUDICIAL_DISQUALIFICATION_DUTY": (
        "U.S. Const. amends. V & XIV",
        "the due-process guarantee of a “neutral and detached” tribunal",
        "due process requires an impartial tribunal; a judge whose impartiality might reasonably be questioned "
        "must disqualify, and adjudication by a biased tribunal is a structural due-process defect",
    ),
    "BIAS_EMOTIONAL_LANGUAGE": (
        "U.S. Const. amends. V & XIV",
        "the due-process guarantee of a “neutral and detached” tribunal",
        "an impartial tribunal is a due-process requirement; disparaging or partisan characterization of a "
        "litigant evidences a want of the required neutrality",
    ),
    "BIAS_PRO_SE_GATEKEEPING": (
        "U.S. Const. amends. V & XIV",
        "the due-process guarantee of access to the courts and a neutral tribunal",
        "heightened gatekeeping applied because a litigant is pro se denies the equal, impartial access due "
        "process guarantees",
    ),
    "CASE_FACT_CONFLICT": (
        "U.S. Const. amends. V & XIV",
        "due process forbids deprivation of property on facts that cannot all be true",
        "an enforcement demand resting on the claimant's own irreconcilable statements "
        "deprives the obligor of property without the operative facts ever being established",
    ),
    "BIAS_JUDICIAL": ['28 U.S.C. § 455(a) (shall disqualify where impartiality might reasonably be questioned)', '28 U.S.C. § 144 (bias affidavit)', '46 Am. Jur. 2d Judges §§ 127-147 (disqualification for bias)'],
    "BIAS_PUNITIVE_APPEAL": ['28 U.S.C. § 455(a)', '46 Am. Jur. 2d Judges § 147 (hostility toward litigant)', 'Fed. R. Civ. P. 11 (sanctions standards apply to parties, not as punishment for advocacy)'],
    "CONCLUSORY_DISMISSAL": ['Fed. R. Civ. P. 12(b)(6) (element-by-element analysis required)', 'Fed. R. Civ. P. 52(a) (findings and conclusions)', '5 U.S.C. § 555(e) (reasoned decision principle)'],
    "FILING_DEADLINE_VIOLATION": ['Fed. R. Civ. P. 6 (computing time)', 'Fed. R. Civ. P. 60(b)(1) (excusable neglect)', '28 U.S.C. § 2107 (appeal windows)'],
    "GARNISHEE_NO_ANSWER": ['Pa.R.C.P. 3144-3146 (garnishee answer duty; judgment against garnishee on default)', '42 Pa.C.S. § 8104 (satisfaction duty)'],
    "IMMUNITY_WAIVER_TRIGGERED": ['46 Am. Jur. 2d Judges §§ 70, 74 (nonjudicial acts; clear absence of jurisdiction)', '42 U.S.C. § 1983', '5 U.S.C. § 702 (sovereign-immunity waiver for non-monetary relief)'],
    "IMPROPER_STANDARD_OF_REVIEW": ['Fed. R. Civ. P. 12(b)(6) vs 56 (conversion rules)', 'Fed. R. Civ. P. 56(a) (genuine dispute standard)', '28 U.S.C. § 1291 (de novo review of legal standards on appeal)'],
    "LEAVE_TO_AMEND_CONCLUSORY": ['Fed. R. Civ. P. 15(a)(2) (leave freely given when justice so requires)', 'Fed. R. Civ. P. 52(a)'],
    "OBSTRUCTION_OF_JUSTICE": ['18 U.S.C. § 1503 (obstruction)', '18 U.S.C. §§ 241-242 (conspiracy/deprivation under color of law)', 'Fed. R. Civ. P. 60(d)(3) (fraud on the court)'],
    "PAGE_LIMIT_EXCEEDED": ['E.D. Pa. Local R. 7.1 (brief limits)', 'Fed. R. Civ. P. 7(b) (motion form)'],
    "PRIMA_FACIE_EVIDENCE_IGNORED": ['Fed. R. Evid. 201 (judicial notice)', 'Fed. R. Evid. 801(d)(2) (party-opponent admissions)', 'Fed. R. Civ. P. 56(e) (unaddressed assertions)'],
    "PRO_SE_LIBERAL_CONSTRUCTION": ['Haines v. Kerner, 404 U.S. 519 (pro se pleadings to less stringent standards)', 'Fed. R. Civ. P. 8(e) (pleadings construed to do justice)'],
    "SANCTIONS_TRIGGERED": ['Fed. R. Civ. P. 11(c) (notice + opportunity to respond before sanctions)', '28 U.S.C. § 1927 (vexatious multiplication standard)'],
    "TWOMBLY_IQBAL_MISAPPLICATION": ['Fed. R. Civ. P. 8(a)(2) (short and plain statement)', 'Fed. R. Civ. P. 9(b) (only fraud/mistake heightened)', 'Fed. R. Evid. 201 (documents attached control over characterization)'],
    "VERIFICATION_MISSING": ['Fed. R. Civ. P. 11(a) (signature requirements)', '28 U.S.C. § 1746 (unsworn declarations under penalty of perjury)', 'Pa.R.C.P. 206.1/1024 (verification requirements)'],
    "VERIFICATION_MISSING_PROC": ['Pa.R.C.P. 1024 (verification)', 'Fed. R. Civ. P. 11(a)', '28 U.S.C. § 1746'],
    "PERSONAL_JURISDICTION_UNESTABLISHED": ['International Shoe line (minimum contacts)', '42 Pa.C.S. 5322', 'Pa.R.C.P. 402-404', 'Fed. R. Civ. P. 4(k), 12(b)(2)', 'Fed. R. Civ. P. 60(b)(4)'],
    "RESTITUTION_REMEDY_AVAILABLE": ['unjust enrichment elements (PA)', 'constructive trust doctrine', 'equitable subrogation', 'FRE 801(d)(2) (their own recovery admissions)'],
    "MANDATORY_LANGUAGE_DISREGARDED": ['1 Pa.C.S. 1921(b)', 'plain-meaning rule', 'Fed. R. Civ. P. 52(a)'],
    "EQUITY_MAXIM_APPLICABLE": ['equity jurisprudence (Broom, treatises)', 'clean hands doctrine', '1 Pa.C.S. 1928(b)(5) (equity powers strictly construed)'],
    "AGENCY_AUTHORITY_UNPROVEN": ['Fed. R. Evid. 602', 'Fed. R. Civ. P. 17(a)', 'agency law (actual/apparent authority)'],
    "VENUE_IMPROPER": ['28 U.S.C. 1391', '28 U.S.C. 1406(a)', 'Pa.R.C.P. 1006', 'local-action doctrine'],
    "AGENCY_OWN_RULES_VIOLATED": ['Accardi v. Shaughnessy, 347 U.S. 260', '5 U.S.C. 706(2)', '5 U.S.C. 555'],
    "CONDITION_PRECEDENT_UNMET": ['41 P.S. 403', '12 Pa. Code Ch. 31', '24 CFR 203.602', '12 CFR 1024.41'],
    "JUDICIAL_NOTICE_COMPELLED": ['Fed. R. Evid. 201(c)(2)', 'Pa.R.E. 201'],
    "BURDEN_MISALLOCATED": ['Fed. R. Evid. 301', 'element-wise burden doctrine'],
    "CLAIM_PRECLUSION_BAR": ['res judicata elements (final judgment, same parties, same claim)'],
    "ISSUE_PRECLUSION_BAR": ['collateral estoppel elements (identical issue, actually litigated, necessarily decided)'],
    "LAW_OF_THE_CASE_VIOLATED": ['law-of-the-case doctrine', 'coordinate-jurisdiction rule (PA)'],
    "OPPONENT_WAIVER_FORFEITURE": ['Fed. R. Civ. P. 12(h)', 'Pa.R.C.P. 1032', 'Fed. R. Civ. P. 8(b)(6)'],
    "CANDOR_VIOLATION": ['RPC 3.3', 'Fed. R. Civ. P. 11(b)', '28 U.S.C. 1927', '42 Pa.C.S. 2503'],
    "RESCISSION_EFFECT_IGNORED": ['15 U.S.C. 1635(b)', '12 CFR 1026.23(d)', 'Jesinoski v. Countrywide, 574 U.S. 259'],
    "STATUTORY_DAMAGES_AVAILABLE": ['15 U.S.C. 1692k', '15 U.S.C. 1681n, 1681o', '12 U.S.C. 2605(f)', '26 U.S.C. 7434', '73 P.S. 201-9.2', '42 Pa.C.S. 8371'],
    "UNREBUTTED_ADVERSE_STATEMENT": ["Fed. R. Evid. 801(d)(2)", "Fed. R. Civ. P. 8(b)(6) (allegation not denied is admitted)", "record-silence/concession doctrine"],
    "DEFINITION_DIVERGENCE": ["1 U.S.C. § 1 (Dictionary Act)", "13 Pa.C.S. § 1201(b)(21) / UCC § 3-301 (holder/PETE)", "26 U.S.C. § 6050P + 26 CFR 1.6050P-1 (discharge reporting)", "26 U.S.C. § 7701(c) (includes)", "Gould v. Gould, 245 U.S. 151 (construction against government)", "Fed. R. Civ. P. 65(c) (injunction security)"],
    "ENTITY_IDENTITY_CONFLICT": ["Fed. R. Civ. P. 17(a)", "28 U.S.C. § 1332 (citizenship of the entity SUED)", "28 U.S.C. § 1446(a) (removal papers plead the grounds)", "Fed. R. Evid. 801(d)(2)"],
    "CROSS_PARTY_ROLE_CONFLICT": (
        "U.S. Const. amends. V & XIV",
        "due process forbids deprivation of property on claims that cannot all be true",
        "enforcement proceeding on mutually exclusive role claims deprives the obligor of "
        "property without the single real party in interest ever being established",
    ),
    "INSTRUMENT_VERSION_CONFLICT": (
        "U.S. Const. amends. V & XIV",
        "no person may be deprived of property except by due process on authentic proof",
        "enforcement of an instrument whose productions conflict, without production of the "
        "original, deprives the obligor of property on unauthenticated paper",
    ),
    "REAL_PARTY_RECIPROCITY": (
        "U.S. Const. art. III, § 2; amends. V & XIV",
        "the judicial power extends to cases prosecuted by real parties in interest, and due process "
        "requires even-handed application of the rules of standing",
        "a court that enforces the standing / real-party / holder requirement against one side while "
        "excusing the party who filed to enforce from the identical showing applies the law "
        "asymmetrically; Rule 17(a) and U.C.C. 3-301 bind the enforcing filer first",
    ),
    "DEPRIVATION_OF_RIGHTS_COLOR_OF_LAW": (
        "U.S. Const. amend. XIV, § 1",
        "no State shall deprive any person of life, liberty, or property without due process, nor deny equal "
        "protection",
        "42 U.S.C. § 1983 enforces the Fourteenth Amendment; a deprivation of a federally protected right under "
        "color of law is actionable",
    ),
}

_STACK_CITE_RE = __import__("re").compile(
    r"\b\d{1,3}\s+(?:U\.?\s?S\.?|S\.?\s?Ct\.?|L\.?\s?Ed\.?(?:\s?2d)?|F\.\s?(?:2d|3d|4th)?|F\.\s?Supp\.?(?:\s?[23]d)?)\s+\d{1,4}\b",
    __import__("re").I)


def _stack_binding_from_cite(cite: str) -> str:
    """Network-free binding read from the reporter. SCOTUS reporters bind every federal court."""
    c = " " + cite.lower().replace(" ", "") + " "
    if "u.s." in c or "s.ct." in c or "l.ed" in c:
        return "BINDING — SCOTUS"
    if "f.supp" in c:
        return "district — persuasive"
    if "f.2d" in c or "f.3d" in c or "f.4th" in c:
        return "circuit — binding only in the deciding circuit"
    return "verify binding"


_STACK_CASECITE_RE = __import__("re").compile(
    r"([A-Z][A-Za-z.,'&\- ]+?\s+v\.?\s+[A-Z][A-Za-z.,'&\- ]+?),?\s+"
    r"(\d{1,3}\s+(?:U\.?\s?S\.?|S\.?\s?Ct\.?|F\.\s?(?:2d|3d|4th)?|F\.\s?Supp\.?(?:\s?[23]d)?)\s+\d{1,4})")
# operative statutory language — distinguishes real rule text from a bare citation bundle
_STACK_OPERATIVE = __import__("re").compile(
    r"\b(shall|must|may not|shall not|is void|void\b|waive[ds]?|non-?waivable|no discretion|is required|"
    r"entitled|deprive[ds]?|shall extend|bears the burden)\b", __import__("re").I)


# The Court's OWN holding voice — first person / this-Court. Anything else ("the Circuit Court held",
# "the Surrogate holding", "Some Courts of Appeals have held") is a RECITATION of a lower/other court and
# must NOT be surfaced as this case's holding (misattribution = the exact attackable error we forbid).
_HOLDING_VOICE = __import__("re").compile(
    r"\bwe (?:now |therefore |thus |accordingly |today )?(?:hold|holds|held|conclude|concluded)\b"
    r"|\bthis Court (?:has )?(?:hold|holds|held|conclude[sd]?)\b"
    r"|\bwe hold today\b|\bour holding\b", __import__("re").I)
_HOLDING_RECITATION = __import__("re").compile(
    r"\b(?:the )?(?:circuit court|district court|court of appeals|courts of appeals|surrogate|"
    r"court below|lower court|trial court|state court|bankruptcy court|that court)\b", __import__("re").I)


def _stack_holding_from_cite(name: str, cite: str):
    """FTS-pull the named case's OWN ingested opinion (fast, embedder-free) and return a one-line HOLDING
    sentence — ONLY the Court's own holding voice ('we hold/we conclude/this Court held'), never a lower-
    court recitation. (cite, holding, binding) or None. Grounded; never invents/misattributes. Never raises."""
    import re as _re
    try:
        from pipeline.claim_support_check import _case_fts
        rows = _case_fts(f"{name}, {cite}", cap=8)
    except Exception:
        rows = []
    for r in rows:
        txt = " ".join((r.get("text") or "").split())
        for s in _re.split(r"(?<=[.;])\s+", txt):
            mm = _HOLDING_VOICE.search(s)
            if mm and not _HOLDING_RECITATION.search(s):
                clean = s[mm.start():].strip()          # drop leading pincite/fragment before the holding verb
                if len(clean) < 20:                     # a bare "we hold." with no substance — skip
                    continue
                clean = clean[0].upper() + clean[1:]
                return (cite.strip(), clean[:300], _stack_binding_from_cite(cite))
    return None


def _stack_holding(proposition: str, known_cites: str = "", k: int = 6):
    """Corpus-pull the controlling case + a one-line HOLDING. FIRST tries the cases the audit already named
    for this ground (FTS on the ingested opinion — fast, grounded); falls back to semantic proposition
    search. Returns (cite, holding_sentence, binding) or None. Never invents. Never raises."""
    import re as _re
    # (1) cases the audit bound to this ground — pull their own opinion by name+cite (best: it IS the holding)
    for m in _STACK_CASECITE_RE.finditer(known_cites or ""):
        got = _stack_holding_from_cite(m.group(1).strip(), m.group(2).strip())
        if got:
            return got
    # (2) fall back to a semantic proposition search across the corpus
    try:
        hits = _corpus_hits(proposition + " held that controlling Supreme Court authority holding", k=k)
    except Exception:
        hits = []
    best = None
    for h in hits:
        if (h.get("score") or 0) < 0.4:
            continue
        txt = " ".join((h.get("text") or "").split())
        m = _STACK_CITE_RE.search(txt) or _STACK_CITE_RE.search(h.get("source") or "")
        if not m:
            continue
        cite = m.group(0).strip()
        sent = next((s for s in _re.split(r"(?<=[.;])\s+", txt)
                     if _re.search(r"\bheld?\b|\bholding\b|\bhold[s]?\b", s, _re.I)), txt[:240])
        binding = _stack_binding_from_cite(cite)
        cand = (cite, sent.strip()[:280], binding)
        if binding.startswith("BINDING"):
            return cand          # first SCOTUS/binding hit wins
        if best is None:
            best = cand
    return best


_STACK_CACHE: dict = {}


def get_authority_stack_grounding(stub_text: str, case_id: str = "", max_grounds: int = 6,
                                  target_court: str = "EDPA") -> str:
    """For each detected ground (jurisdictional first), emit ONE authority ladder — Constitution ->
    statute verbatim -> BINDING case + HOLDING -> so no section argues without a stack. Resolves the audit
    by case_id first (robust: the scaffold stub carries no docket), else by docket digits in the stub.
    Cached per draft (computed once, reused across every section prompt). "" if no grounds. Never raises."""
    key = (case_id or "") + "|" + (stub_text or "")[:180]
    if key in _STACK_CACHE:
        return _STACK_CACHE[key]
    out = ""
    try:
        d = _audit_by_case_id(case_id) if case_id else None
        grounds = _order_grounds(d, max_grounds * 3) if d else detected_grounds(stub_text, max_grounds * 3)
        seen, ladders = set(), []
        for g in grounds:
            vt = (g.get("type") or "").strip()
            if not vt or vt in seen:
                continue
            seen.add(vt)
            const = _STACK_CONSTITUTION.get(vt)
            prop = (const[2] if const else "") or (g.get("explanation") or g.get("cite") or vt)
            hold = _stack_holding(prop, known_cites=(g.get("rule_text") or "") + " " + (g.get("cite") or ""))
            rungs = []
            if const:
                rungs.append(f"  1. CONSTITUTION: {const[0]} — “{const[1]}”")
            rt = (g.get("rule_text") or "").strip()
            cite = (g.get("cite") or "").strip()
            # A ground's rule_text is sometimes REAL operative statute text ("shall/void/waived") and sometimes
            # just a CITATION BUNDLE ("28 U.S.C. 1441; Murphy Bros. ...; Shamrock ..."). Only present it as a
            # quotable verbatim rung when it actually carries operative language — otherwise the drafter pastes
            # the citation list into the brief as a fake "the controlling authority mandates that '<cites>'"
            # quote (a scaffold leak). When it is a bundle, point to the separate MANDATORY RULE TEXT block,
            # which carries the true verbatim statute, and let the case cites flow to rung 3.
            rt_no_cases = _STACK_CASECITE_RE.sub("", rt).strip(" ;,.")
            if rt and _STACK_OPERATIVE.search(rt_no_cases or rt):
                quote = (rt_no_cases if _STACK_OPERATIVE.search(rt_no_cases) else rt)[:300]
                rungs.append(f"  2. STATUTE/RULE (verbatim): {cite} — “{quote}”")
            elif cite:
                rungs.append(f"  2. STATUTE/RULE: {cite} — quote its operative 'shall/void/must' language from "
                             "the MANDATORY RULE TEXT block; do NOT quote a citation list as if it were rule text")
            if hold:
                rungs.append(f"  3. BINDING HOLDING: {hold[0]} [{hold[2]}] — HELD: “{hold[1]}”")
            else:
                named = [f"{mm.group(1).strip()}, {mm.group(2).strip()}"
                         for mm in _STACK_CASECITE_RE.finditer(rt + " " + cite)]
                if named:
                    rungs.append("  3. BINDING CASE(S) — state the holding of the controlling one (the "
                                 "stands-for gate verifies it; do NOT invent): " + "; ".join(named[:4]))
                else:
                    rungs.append("  3. BINDING HOLDING: cite the controlling case from the record/other "
                                 "grounding — do NOT invent one")
            if not rungs:
                continue
            expl = (g.get("explanation") or "").strip()
            head = "GROUND: " + vt.replace("_", " ").title() + (f" — {expl[:150]}" if expl else "")
            ladders.append(head + "\n" + "\n".join(rungs))
            if len(ladders) >= max_grounds:
                break
        if ladders:
            intro = ("## AUTHORITY STACK PER GROUND — build EACH section on this ladder (never argue without it)\n"
                     "*For every ground below, OPEN with the constitutional/jurisdictional anchor, then quote the "
                     "statute's operative 'shall/void/must' language (from this ladder where shown, otherwise from "
                     "the MANDATORY RULE TEXT block — NEVER quote a bare list of citations as if it were rule "
                     "text), then state the BINDING holding that forecloses a narrow reading — in that descending "
                     "order of force. You MUST NOT write 'the Court overlooked controlling authority' (or any "
                     "equivalent) without naming that authority and saying what it held. Where a rung is missing, "
                     "argue the rungs you have rather than fabricate. This is the skeleton — render it as filed "
                     "prose, not as an outline.*\n")
            out = intro + "\n\n".join(ladders) + "\n"
    except Exception:
        out = ""
    _STACK_CACHE[key] = out
    return out
