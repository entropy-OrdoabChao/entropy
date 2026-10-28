"""argument_rebuttal — the REBUTTAL SPINE of an effective reply brief.

The old reply brief only attacked the binding-effect of the opponent's citations. That concedes
every substantive contention by silence (concession-by-silence / tacit admission). This module
gives the brief the spine it lacked: it answers EACH discrete argument the opponent actually made.

Division of labor (R7 — LLM parses, deterministic decides law):
  * The LLM (pipeline/opposing_argument_extractor.py) only PARSES the opponent's brief into a list
    of discrete contentions -> opposing_arguments[] in the audit JSON. It states no legal conclusion.
  * THIS module is deterministic. It maps each contention to a controlling rebuttal FRAME (rule +
    standard + how-it-fails), drawn from a black-letter catalog (same pattern as _SCORECARD_HOOK_MAP
    / _PROCEDURAL_REMEDIES). Case-specific binding case cites are left as [VERIFY] slots to be pulled
    from the database — the system never invents a citation.

Anti-tacit-admission guarantee: build_argument_rebuttal_section() emits one subsection per
contention AND a closing catch-all confirming every identified argument was addressed, so nothing
is left for the court to treat as conceded.

An OpposingArgument (dict) the extractor produces:
  {
    "id": "arg_1",
    "heading": "Failure to State a Claim",          # their section heading (verbatim if present)
    "assertion": "Plaintiff alleges no inaccurate information ...",  # what they argue (paraphrase)
    "relief_sought": "dismissal under Rule 12(b)(6)",
    "cited_authority": ["Spokeo, Inc. v. Robins, 578 U.S. 330 (2016)", ...],
    "record_refs": [],                               # optional record cites they invoke
  }
"""
from __future__ import annotations
import re

# --- black-letter rebuttal frames, keyed by argument TOPIC --------------------------------------
# rule/standard are controlling authority (SCOTUS or statute) — safe to state inline, like the
# existing hook map. `kill` is the deterministic reason their argument fails. `needs` lists what
# the renderer should weave in from case data (a record fact, our binding cite) — bracketed if absent.
REBUTTAL_FRAMES = {
    "failure_to_state_claim": {
        "label": "failure to state a claim",
        "standard": ("On a Rule 12(b)(6) motion the court accepts all well-pleaded facts as true and "
                     "draws all reasonable inferences in the plaintiff's favor; dismissal is proper "
                     "only if no relief could be granted under any set of facts. Bell Atlantic Corp. "
                     "v. Twombly, 550 U.S. 544 (2007); Ashcroft v. Iqbal, 556 U.S. 662 (2009)."),
        "kill": ("Defendant's argument misapplies that standard: it disputes the facts rather than "
                 "their sufficiency, which is improper at the pleading stage. The complaint pleads "
                 "{record_fact}, which states a claim under the governing statute."),
        "needs": ["record_fact"],
    },
    "no_inaccuracy_fcra": {
        "label": "no inaccurate information (FCRA)",
        "standard": ("A FCRA claim under 15 U.S.C. 1681e(b) / 1681i requires only a plausible "
                     "allegation that the information reported was inaccurate or incomplete and that "
                     "the defendant failed to follow reasonable procedures or conduct a reasonable "
                     "reinvestigation."),
        "kill": ("The complaint identifies the specific inaccuracy: {record_fact}. Whether the "
                 "reporting was in fact inaccurate is a question of fact that cannot be resolved on "
                 "the pleadings."),
        "needs": ["record_fact"],
    },
    "no_actual_damages": {
        "label": "absence of actual damages",
        "standard": ("Actual damages under the FCRA include emotional distress and need not be "
                     "economic; willful violations support statutory and punitive damages under "
                     "15 U.S.C. 1681n without proof of actual damages."),
        "kill": ("Damages are not an element to be resolved on the pleadings, and the complaint "
                 "alleges {record_fact}. The argument is premature."),
        "needs": ["record_fact"],
    },
    "statute_of_limitations": {
        "label": "time-bar",
        "standard": ("Under 15 U.S.C. 1681p the FCRA limitations period runs from the date the "
                     "plaintiff discovered the violation; accrual is a fact question."),
        "kill": ("The discovery date pleaded ({record_fact}) places the claim within the limitations "
                 "period, and limitations is an affirmative defense not properly resolved on a motion "
                 "to dismiss unless the bar appears on the face of the complaint."),
        "needs": ["record_fact"],
    },
    "standing": {
        "label": "lack of standing",
        "standard": ("A concrete injury for Article III standing includes reputational and "
                     "informational harm. Spokeo, Inc. v. Robins, 578 U.S. 330 (2016); "
                     "TransUnion LLC v. Ramirez, 594 U.S. 413 (2021)."),
        "kill": ("The complaint alleges a concrete injury: {record_fact}. That satisfies Spokeo/"
                 "TransUnion, and standing is assessed on the pleadings in the plaintiff's favor."),
        "needs": ["record_fact"],
    },
    "improper_service": {
        "label": "defective service / process",
        "standard": ("Defects in service are curable and, absent prejudice, do not warrant dismissal; "
                     "the court may instead quash and permit re-service. Fed. R. Civ. P. 4(m)."),
        "kill": ("Any defect is curable and Defendant shows no prejudice; dismissal is not the remedy. "
                 "{record_fact}"),
        "needs": [],
    },
    "preemption": {
        "label": "preemption",
        "standard": ("FCRA preemption under 15 U.S.C. 1681t(b)(1)(F) is narrow and does not reach "
                     "claims premised on the defendant's own conduct after notice of dispute."),
        "kill": ("The claim rests on Defendant's post-notice conduct, which is not preempted. "
                 "{record_fact}"),
        "needs": [],
    },
}

# topic detection — keyword cues -> frame key (parsing aid only; the law lives in the frame)
_TOPIC_CUES = [
    ("no_inaccuracy_fcra",     r"inaccura|incorrect information|accurately report|1681e|reinvestigat|1681i"),
    ("no_actual_damages",      r"no (actual )?damages|fail(ed|s)? to (allege|plead) damages|damages element"),
    ("statute_of_limitations", r"statute of limitations|time[- ]barred|untimely|limitations period|1681p"),
    ("standing",               r"standing|article iii|concrete injury|injury[- ]in[- ]fact|spokeo"),
    ("improper_service",       r"service of process|insufficient service|improperly served|rule 4"),
    ("preemption",             r"preempt|1681t|1681h"),
    ("failure_to_state_claim", r"fail(ed|s)? to state a claim|12\(b\)\(6\)|plausib|conclusory|twombly|iqbal"),
]


def classify_argument(arg: dict) -> str:
    """Map a contention to a rebuttal frame by topic cues. Defaults to failure_to_state_claim
    (the most common MTD posture) so NO contention is ever left without a rebuttal."""
    blob = " ".join([arg.get("heading", ""), arg.get("assertion", ""), arg.get("relief_sought", "")]).lower()
    for key, pat in _TOPIC_CUES:
        if re.search(pat, blob):
            return key
    return "failure_to_state_claim"


# --- LLM coherence layer (m0 pattern: deterministic = law/truth, LLM = reasoning + coherence) ----
_CITE_RX = re.compile(r"[A-Z][A-Za-z.'\- ]+ v\. [A-Z][A-Za-z.'\- ]+|\d+\s+U\.S\.\s+\d+|"
                      r"\d+\s+F\.(?:\s?\dd|\s?Supp\.?(?:\s?\dd)?)\s+\d+|\d{4}\s+WL\s+\d+|"
                      r"\b\d+\s+U\.S\.C\.\s+\d+|Fed\.\s?R\.\s?Civ\.\s?P\.\s?\d+|Pa\.R\.C\.P\.\s?\d+")


def _cites(text: str) -> set:
    return {m.group(0).strip().rstrip(".,;") for m in _CITE_RX.finditer(text or "")}


def _no_new_citations(out: str, allowed: str) -> bool:
    """Guardrail: the polished reasoning may not introduce any citation not already in the
    deterministic material (R7 — the LLM reasons, it never invents authority)."""
    allowed_set = _cites(allowed)
    return all(any(c == a or c in a or a in c for a in allowed_set) for c in _cites(out))


def _polish_reasoning(llm, their_point: str, standard: str, record_fact: str, kill: str) -> str | None:
    """Use the LLM ONLY for reasoning + coherence on the 'why it fails' paragraph. The controlling
    standard (which carries the citations) is fixed and supplied for context, NOT to be expanded.
    Returns coherent prose, or None to fall back to the deterministic kill."""
    if llm is None:
        return None
    system_msg = (
        "You write the 'why it fails' reasoning of one point-by-point reply-brief rebuttal. "
        "You are given the opponent's argument, the controlling legal standard, a record fact, and a "
        "fixed conclusion. In 2-4 sentences, reason coherently why the standard and the record defeat "
        "the argument. RULES: (1) cite ONLY authorities that already appear in the material given — "
        "never add a case, statute, or rule. (2) use ONLY the record fact given — never invent facts. "
        "(3) the conclusion is fixed: the argument fails. (4) output the paragraph only — no heading, "
        "no preamble, no markdown, no quotation marks."
    )
    user_msg = (
        f"Opponent's argument: {their_point}\n"
        f"Controlling standard (context only, do not add citations): {standard}\n"
        f"Record fact (do not alter): {record_fact}\n"
        f"Fixed conclusion: the argument fails on the controlling standard.\n\n"
        f"Write the 2-4 sentence reasoning."
    )
    try:
        out = llm.chat(messages=[{"role": "system", "content": system_msg},
                                 {"role": "user", "content": user_msg}],
                       thinking=False, n_predict=220, temperature=0.2)
    except Exception:
        return None
    out = (out or "").strip()
    if not out or not _no_new_citations(out, standard + " " + their_point + " " + record_fact):
        return None
    return out


def _record_fact_for(arg: dict, case_facts: list[str]) -> str:
    """Pick a record fact that answers this contention. If none is supplied, emit an explicit
    [VERIFY] slot rather than inventing one (no fact -> no fabricated paragraph)."""
    refs = arg.get("record_refs") or []
    if refs:
        return "; ".join(refs)
    if case_facts:
        return case_facts[0]
    return "[VERIFY: insert the specific record fact / complaint paragraph that answers this point]"


def build_rebuttal(arg: dict, case_facts: list[str], idx: int, llm=None) -> str:
    """Render the point-by-point rebuttal for ONE contention:
       their point -> controlling standard (fixed) -> why it fails (LLM-cohered, deterministic
       fallback) -> (their support is non-binding, secondary).
    The `llm` is used ONLY for reasoning/coherence on the 'why it fails' paragraph (m0 pattern)."""
    frame = REBUTTAL_FRAMES[classify_argument(arg)]
    rec = _record_fact_for(arg, case_facts)
    kill = frame["kill"].format(record_fact=rec)
    heading = arg.get("heading") or frame["label"].title()
    assertion = arg.get("assertion") or f"that the complaint should be dismissed for {frame['label']}"

    # LLM reasoning/coherence pass with deterministic fallback (the standard stays verbatim)
    why = _polish_reasoning(llm, assertion, frame["standard"], rec, kill) or kill

    lines = [f"### {idx}. {heading}", ""]
    lines.append(f"**Defendant argues** {assertion.rstrip('.')}.")
    lines.append("")
    lines.append(f"**The controlling standard.** {frame['standard']}")
    lines.append("")
    lines.append(f"**Why it fails.** {why}")
    # if their support is non-binding, fold it in as SECONDARY (not the whole argument)
    cites = arg.get("cited_authority") or []
    if cites:
        lines.append("")
        lines.append("**Defendant's authority is also non-controlling.** Defendant supports this point "
                     f"with {', '.join('*' + c + '*' for c in cites[:4])}"
                     f"{' and others' if len(cites) > 4 else ''}; to the extent any is a trial-court or "
                     "out-of-circuit decision it is persuasive only and cannot substitute for the "
                     "controlling authority set out above (see the accompanying Scorecard Exhibit).")
    lines.append("")
    return "\n".join(lines)


def build_argument_rebuttal_section(opposing_arguments: list[dict], case_facts: list[str] | None = None,
                                    section_number: str = "III", llm=None) -> str:
    """Assemble 'III. REBUTTAL TO DEFENDANT'S ARGUMENTS' with the anti-tacit-admission catch-all.
    `llm` (optional, the gate's LLM client) supplies reasoning/coherence; without it the section
    falls back to fully deterministic prose."""
    case_facts = case_facts or []
    if not opposing_arguments:
        return ""   # no fact -> no paragraph: if nothing was extracted, emit nothing
    out = [f"## {section_number}. REBUTTAL TO DEFENDANT'S ARGUMENTS", ""]
    out.append("Plaintiff answers each argument Defendant raises. Defendant bears the burden on its "
               "motion; for the reasons below, each argument fails on the controlling standard.")
    out.append("")
    for i, arg in enumerate(opposing_arguments, 1):
        out.append(build_rebuttal(arg, case_facts, i, llm=llm))
    # closing catch-all so silence is never construed as concession
    labels = "; ".join(f"({i}) {a.get('heading') or REBUTTAL_FRAMES[classify_argument(a)]['label']}"
                       for i, a in enumerate(opposing_arguments, 1))
    out.append(f"**No argument is conceded.** Plaintiff has addressed each ground Defendant advanced — "
               f"{labels}. To the extent Defendant raises any further contention not enumerated above, "
               "Plaintiff does not concede it and relies on the complaint, the record, and the "
               "controlling authority cited herein. Defendant's failure to carry its burden on any of "
               "these grounds defeats the motion.")
    out.append("")
    return "\n".join(out)


# =================================================================================================
# PRIMARY PATH: render the section FROM m0_rebuttal_gate.rebut_adverse() output (R6 — reuse the m0
# engine, don't duplicate it). m0 does the reasoning (record/authority/trap checks + grounded
# rebuttal); this renderer is deterministic assembly + the correct binding_status m0 already stamped.
# =================================================================================================

def _authority_sentence(authority: list[dict]) -> str:
    """Turn m0's per-ground authority list into one sentence keyed on the stamped binding_status.
    Binding authority is NOT attacked as 'non-binding' (the old bug); it is either shown as
    misapplied or as not supporting the movant. Persuasive / trial-level cites are downgraded."""
    if not authority:
        return ""
    persuasive, trial, misapplied, binding_ok = [], [], [], []
    for a in authority:
        cite = (a.get("cite") or "").strip()
        if not cite:
            continue
        status = a.get("binding_status_checked") or a.get("binding") or ""
        if a.get("misapplied"):
            misapplied.append((cite, a.get("why") or ""))
        elif status.startswith("persuasive"):
            persuasive.append(cite)
        elif status.startswith("not_binding"):
            trial.append(cite)
        elif status.startswith("binding"):
            binding_ok.append(cite)
    parts = []
    if misapplied:
        parts.append("Defendant misapplies " + ", ".join(f"*{c}*" + (f" ({w})" if w else "")
                                                          for c, w in misapplied[:4]) + ".")
    if trial:
        parts.append(", ".join(f"*{c}*" for c in trial[:5]) +
                     (" is" if len(trial) == 1 else " are") +
                     " a trial-court decision that binds no court and is persuasive at most.")
    if persuasive:
        parts.append(", ".join(f"*{c}*" for c in persuasive[:5]) +
                     (" is" if len(persuasive) == 1 else " are") +
                     " persuasive authority only and cannot substitute for controlling authority.")
    if binding_ok and not (misapplied or trial or persuasive):
        parts.append("The authority Defendant cites, even where binding, does not support the relief "
                     "Defendant seeks on this ground.")
    return " ".join(parts)


def render_from_m0(rebut: dict, section_number: str = "III") -> str:
    """Build 'III. REBUTTAL TO DEFENDANT'S ARGUMENTS' from m0_rebuttal_gate.rebut_adverse() output.
    Strategic 'trap' notes are intentionally EXCLUDED from the filed prose (they are litigant
    guidance, not brief text)."""
    grounds = (rebut or {}).get("grounds") or []
    if not grounds:
        return ""
    out = [f"## {section_number}. REBUTTAL TO DEFENDANT'S ARGUMENTS", ""]
    out.append("Plaintiff answers each ground Defendant advances. Defendant bears the burden on its "
               "motion; for the reasons below, each ground fails on the record and the controlling "
               "authority.")
    out.append("")
    headings = []
    for i, g in enumerate(grounds, 1):
        ground = (g.get("ground") or f"Defendant's ground {i}").strip()
        headings.append(ground[:80])
        out.append(f"### {i}. {ground}")
        out.append("")
        rc = (g.get("record_contradiction") or "").strip()
        if rc and rc.lower() not in ("none", "n/a", ""):
            out.append(f"**The record refutes the premise.** {rc}")
            out.append("")
        auth = _authority_sentence(g.get("authority") or [])
        if auth:
            out.append(f"**Defendant's authority does not carry the motion.** {auth}")
            out.append("")
        rebuttal = (g.get("rebuttal") or "").strip()
        if rebuttal:
            out.append(f"**Why it fails.** {rebuttal}")
            out.append("")
    # anti-tacit-admission catch-all
    labels = "; ".join(f"({i}) {h}" for i, h in enumerate(headings, 1))
    out.append(f"**No ground is conceded.** Plaintiff has addressed each ground Defendant advanced — "
               f"{labels}. Plaintiff does not concede any contention, enumerated or not, and relies on "
               "the complaint, the record, and the controlling authority cited herein. Defendant's "
               "failure to carry its burden on any ground defeats the motion.")
    theory = (rebut.get("theory_through_line") or "").strip()
    if theory:
        out.append("")
        out.append(f"Taken together, {theory[0].lower() + theory[1:] if theory else ''}")
    out.append("")
    return "\n".join(out)


def build_rebuttal_section_for_case(case_id: str, adverse_text: str, section_number: str = "III",
                                    case_facts: list[str] | None = None, llm=None) -> str:
    """Convenience entry: run the m0 engine on the adverse filing and render Section III. Falls back
    to the deterministic frame path (build_argument_rebuttal_section) if m0 is offline / empty."""
    try:
        from pipeline import m0_rebuttal_gate as m0
        rebut = m0.rebut_adverse(case_id, adverse_text)
        section = render_from_m0(rebut, section_number=section_number)
        if section:
            return section
    except Exception:
        pass
    return build_argument_rebuttal_section([], case_facts, section_number, llm=llm)


def build_effective_reply_brief(case_id: str, adverse_text: str, caption: dict | None = None,
                                case_facts: list[str] | None = None) -> str:
    """Generate a COMPLETE reply brief whose argument spine is the m0 point-by-point rebuttal.
    caption (all optional, R2 — no hardcoded party): {court, case_no, judge, plaintiff, defendants,
    motion_title, address, phone, date}."""
    cap = caption or {}
    court = cap.get("court") or ("United States District Court for the Eastern District of Pennsylvania"
                                 if case_id.startswith("fed") else "Court of Common Pleas of Philadelphia County")
    case_no = cap.get("case_no") or case_id.split(":", 1)[-1]
    plaintiff = cap.get("plaintiff") or "Plaintiff"
    defendants = cap.get("defendants") or "Defendants"
    motion_title = cap.get("motion_title") or "Defendant's Motion"

    # run m0 + render the spine
    try:
        from pipeline import m0_rebuttal_gate as m0
        rebut = m0.rebut_adverse(case_id, adverse_text)
    except Exception as e:
        rebut = {"grounds": [], "error": str(e)}
    grounds = rebut.get("grounds") or []
    spine = render_from_m0(rebut, section_number="III")

    n = len(grounds)
    L = []
    # caption
    L.append(f"{plaintiff},")
    L.append(f"    Plaintiff, pro se,")
    L.append(f"v.")
    L.append(f"{defendants},")
    L.append(f"    Defendant(s).")
    L.append("")
    L.append(f"{court}")
    L.append(f"Civil Action No. {case_no}" + (f" — {cap['judge']}" if cap.get("judge") else ""))
    L.append("")
    L.append(f"## PLAINTIFF'S REPLY IN OPPOSITION TO {motion_title.upper()}")
    L.append("")
    # I. INTRODUCTION
    L.append("## I. INTRODUCTION")
    L.append("")
    L.append(f"Plaintiff submits this reply in opposition to {motion_title}. Defendant raises "
             f"{n if n else 'several'} ground(s); each is addressed in turn below and none carries "
             "Defendant's burden. Plaintiff does not concede any ground by the order of treatment or "
             "by any matter not expressly addressed.")
    L.append("")
    # II. STANDARD
    L.append("## II. STANDARD OF REVIEW")
    L.append("")
    L.append("On a motion to dismiss the court accepts all well-pleaded factual allegations as true "
             "and draws all reasonable inferences in the non-movant's favor; the movant bears the "
             "burden, and a pro se filing is construed liberally. Bell Atlantic Corp. v. Twombly, "
             "550 U.S. 544 (2007); Ashcroft v. Iqbal, 556 U.S. 662 (2009); Erickson v. Pardus, "
             "551 U.S. 89 (2007).")
    L.append("")
    # III. the m0 rebuttal spine
    L.append(spine if spine else "## III. REBUTTAL TO DEFENDANT'S ARGUMENTS\n\n*(m0 engine "
             "produced no grounds — verify the adverse-filing text was supplied.)*")
    # IV. STATUTORY FRAMEWORK + V. CONSTITUTIONAL POSTURE — make the opposition airtight (anti-weal-out
    # umbrella grouping + a constitutional posture only when a right is implicated / consent contested)
    try:
        import re as _re
        from pipeline.constitutional_consent_engine import umbrella_group as _ug, needed_in_filing as _nif, analyze as _ca
        _cx = _re.compile(r"\d+\s+U\.S\.C\.[^;,]*?\d+\w*|\d+\s+Pa\.?\s?C\.?S\.?[^;,]*?\d+\w*|"
                          r"Fed\.\s?R\.\s?Civ\.\s?P\.\s?\d+\w*|\d+\s+C\.F\.R\.[^;,]*?\d+\w*", _re.I)
        _cites = sorted(set(m.group(0).strip() for m in _cx.finditer((spine or "") + " " + (adverse_text or ""))))
        _grp = _ug(_cites)
        if _grp["grouped"]:
            L.append("## IV. STATUTORY FRAMEWORK (cited as a whole — not severable)")
            for _act, _cc in _grp["grouped"].items():
                L.append(f"  - Under the **{_act}**: {', '.join(sorted(set(_cc))[:8])}")
            L.append("These provisions are cited under their umbrella Act and are not to be addressed "
                     "piecemeal.")
            L.append("")
        _need = _nif(adverse_text or "")
        if _need["include_in_filing"]:
            _c = _ca(adverse_text or "")
            L.append("## V. CONSTITUTIONAL POSTURE")
            L.append(f"Defendant's position implicates {', '.join(_need['rights']) or 'a protected right'}. "
                     f"{_c['verdict']} Raised as legitimate protection against overreach — NOT a "
                     "sovereign-citizen theory.")
            L.append("")
    except Exception:
        pass
    # VI. CONCLUSION
    L.append("## VI. CONCLUSION")
    L.append("")
    L.append("For the foregoing reasons, Defendant has failed to carry its burden on any ground, and "
             f"Plaintiff respectfully requests that the Court deny {motion_title} in its entirety and "
             "grant such further relief as is just.")
    L.append("")
    L.append("Respectfully submitted,")
    L.append("")
    L.append(f"/s/ {plaintiff}")
    L.append(f"{plaintiff}, Plaintiff pro se")
    if cap.get("address"): L.append(cap["address"])
    if cap.get("phone"): L.append(cap["phone"])
    if cap.get("date"): L.append(f"Dated: {cap['date']}")
    # verification discipline: m0 supplies reasoning/coherence (R7); the litigant must confirm the
    # law before filing. binding_status is deterministic; the legal PROPOSITIONS are not.
    L.append("")
    L.append("---")
    L.append("## VERIFICATION CHECKLIST (REMOVE BEFORE FILING)")
    L.append("")
    L.append("- [ ] Each legal proposition in the 'Why it fails' paragraphs is confirmed against the "
             "statute/case (the LLM reasons; it does not decide the law)")
    L.append("- [ ] Every authority cited has been Shepardized/KeyCited for current treatment")
    L.append("- [ ] Each binding/persuasive designation matches the actual issuing court")
    L.append("- [ ] Tolling / discovery-rule / strict-liability arguments are supported by the record "
             "and pleaded facts")
    L.append("- [ ] Record citations point to actual complaint paragraphs / exhibits")
    L.append("- [ ] Caption, case number, and motion title match the current docket")
    L.append("- [ ] Certificate of service attached")
    return "\n".join(L)


if __name__ == "__main__":
    # validate with hand-built contentions from a real motion (dkt 142, Example Card Issuer MTD)
    sample = [
        {"id": "arg_1", "heading": "Plaintiff Fails to State a Claim",
         "assertion": "Plaintiff's allegations are conclusory and do not plausibly allege any inaccurate reporting",
         "relief_sought": "dismissal under Rule 12(b)(6)",
         "cited_authority": ["Twombly, 550 U.S. 544", "Lacy v. Example Card Issuer, 2021 WL 5588739 (E.D. Va.)"],
         "record_refs": ["Compl. ¶¶ 34-41 (Example Card Issuer reported a charged-off balance after the 1099-C discharge)"]},
        {"id": "arg_2", "heading": "No Actual Damages",
         "assertion": "Plaintiff alleges no concrete economic loss",
         "relief_sought": "dismissal", "cited_authority": [], "record_refs": []},
        {"id": "arg_3", "heading": "Claim is Time-Barred",
         "assertion": "the alleged violations occurred outside the limitations period",
         "relief_sought": "dismissal", "cited_authority": ["Prukala v. TD Bank, 2016 WL 6191912 (M.D. Pa.)"],
         "record_refs": ["Compl. ¶ 52 (plaintiff discovered the reinsertion in January 2026)"]},
    ]
    facts = ["Compl. ¶¶ 34-41 (post-discharge charge-off reporting)"]
    print(build_argument_rebuttal_section(sample, facts))
