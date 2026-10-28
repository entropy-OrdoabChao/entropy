"""m0_reply_gate — decides, per docket entry, whether m0 should GENERATE A REPLY, do NOTHING,
or switch to APPEAL. Data-driven (R2: no hardcoded judge/party). Citations are black-letter.

THE RULE THE USER SET:
  * m0 replies ONLY to filings docketed from the cutoff (default = today) FORWARD.
    Anything before the cutoff is REFERENCE ONLY (cite it, attach it, never auto-reply to it).
  * Keep replying to each new adverse filing UNTIL the case is finally resolved (certified/closed).
  * When an order is APPEALABLE, stop replying at the trial level and emit an APPEAL action with
    the correct deadline.

POSTURE (read from the docket text, never assumed):
  active     -> respond to new adverse motions/orders
  appealable -> a FINAL/appealable order exists; emit appeal action (deadline running)
  terminal   -> case closed AND appeal window passed -> nothing further

APPEAL TRIGGERS (28 U.S.C. + FRAP):
  - FINAL judgment / dismissal of the WHOLE case / "case closed"  -> 28 U.S.C. 1291, appeal as of
    right. Notice of appeal: FRAP 4(a)(1)(A) 30 days; FRAP 4(a)(1)(B) 60 days if the U.S. or a U.S.
    agency/officer is a party.
  - Order DENYING REMAND                                          -> NOT immediately appealable;
    seek 28 U.S.C. 1292(b) certification (controlling question, substantial ground, advance term.).
  - Injunction granted/denied/modified                           -> 28 U.S.C. 1292(a)(1), 30 days.
  - Partial dismissal / fewer than all claims-parties             -> NOT final unless Rule 54(b)
    certification entered; otherwise interlocutory, no appeal yet.
  - A timely Rule 59/60 motion (incl. reconsideration)            -> tolls the appeal clock; FRAP
    4(a)(4): the clock runs from the order disposing of that motion.
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import os, re, json, datetime

CASES_BASE = (str(_eroot() / "obsidian_sync/cases")
              if os.path.isdir(str(_eroot() / "obsidian_sync/cases"))
              else str(_eroot() / "obsidian_sync/cases"))

# --- classification of a single docket entry from its text -----------------------------------
FINAL_RE   = re.compile(r"\b(JUDGMENT|CASE CLOSED|DISMISS(?:ED|ING)?\b.*\b(WITH PREJUDICE|ENTIRE|ALL CLAIMS)|"
                        r"CLOSING THE CASE|FINAL JUDGMENT)\b", re.I)
DISMISS_RE = re.compile(r"\bDISMISS", re.I)
REMAND_DENY_RE = re.compile(r"\b(DEN(?:Y|IED|YING))\b.{0,40}\bREMAND\b|\bREMAND\b.{0,20}\bDEN", re.I)
INJUNCT_RE = re.compile(r"\b(INJUNCTION|RESTRAINING ORDER|TRO)\b", re.I)
RECON_RE   = re.compile(r"\b(RECONSIDERAT|RULE 59|RULE 60|ALTER OR AMEND)\b", re.I)
MOTION_RE  = re.compile(r"\bMOTION\b", re.I)
ORDER_RE   = re.compile(r"\bORDER\b", re.I)
GRANTED_RE = re.compile(r"\bGRANT(?:ED|ING)?\b", re.I)
DENIED_RE  = re.compile(r"\bDEN(?:Y|IED|YING)\b", re.I)
# A trial-court order that COMMANDS a Concise Statement of Errors Complained of on Appeal
# (Pa.R.A.P. 1925(b)). Not appealable, not a denial to log -- a mandatory, deadline-bearing
# compliance filing whose default is WAIVER of the appeal. Applies to any PA case, data-driven.
CONCISE_1925_RE = re.compile(
    r"(?:1925\s*\(\s*b\s*\)"
    r"|(?:concise )?statement of (?:the )?(?:errors|matters) complained of on appeal"
    r"|rule of appellate procedure 1925)", re.I)
# filer-side: an entry we filed ourselves needs no reply (data-driven via the pro-se name passed in)


_ORDER_LEAD = re.compile(r"^\s*(?:\d+\s+)?(?:AND NOW[, ]|IT IS (?:HEREBY )?ORDERED|ORDER\b)", re.I)
_RULING_VERB = re.compile(r"\b(GRANT(?:ED|ING)?|DEN(?:Y|IED|YING)|DISMISS|SIGNED BY|IT IS (?:SO )?ORDERED|"
                          r"VACAT|REMAND|ENTERED)\b", re.I)

# Authorship by the ACTOR, not the subject. The COURT *issues* orders/judgments ("SIGNED BY ... JUDGE",
# "IT IS ORDERED", "BY THE COURT", "ORDER OF USCA", "PER CURIAM"); a PARTY *files* motions/responses/
# notices ("... filed by <party>"). So "Motion for Judgment on the Pleadings filed by TransUnion" is a
# party motion to OPPOSE — never an appealable judgment, however its free-form title reads. This is the
# opposing-counsel-vs-judge distinction: identify who AUTHORED the entry, don't keyword-scan the title.
_JUDGE_SIG = re.compile(r"\bSIGNED BY\b.{0,60}\b(JUDGE|JUSTICE|MAGISTRATE)\b|\bBY THE COURT\b|"
                        r"\bIT IS (?:HEREBY |SO )?ORDERED\b|\bORDER OF USCA\b|\bPER CURIAM\b", re.I)
_PARTY_FILED = re.compile(r"\bfiled by\b", re.I)


def classify(text: str) -> dict:
    t = text or ""
    # A party "files"; the court "signs/orders". If the entry was filed by a party and carries no
    # judicial signature, it is a party filing (motion/response/notice) regardless of subject keywords.
    party_filed = bool(_PARTY_FILED.search(t)) and not _JUDGE_SIG.search(t)
    is_court_order = (not party_filed) and bool(
        _ORDER_LEAD.search(t) or _JUDGE_SIG.search(t) or (ORDER_RE.search(t) and _RULING_VERB.search(t)))
    kind = "order" if is_court_order else ("motion" if MOTION_RE.search(t) else "other")
    return {
        "kind": kind,
        "authored_by": "court" if is_court_order else ("party" if party_filed else "unknown"),
        "is_final": bool(FINAL_RE.search(t)),
        "is_remand_denial": bool(REMAND_DENY_RE.search(t)),
        "is_injunction": bool(INJUNCT_RE.search(t)),
        "is_reconsideration": bool(RECON_RE.search(t)),
        "granted": bool(GRANTED_RE.search(t)),
        "denied": bool(DENIED_RE.search(t)),
        "dismiss": bool(DISMISS_RE.search(t)),
    }


# --- FORUM-NAMESPACE ROUTER --------------------------------------------------------------------
# Which drafting ENGINE answers a served document is forced by the document's own forum namespace —
# its matter-number grammar + issuing-office letterhead — NOT by an LLM judgment (R7: the LLM only
# OCR'd the text; the router is deterministic code). A court docket => m0 (opposition/vacate/reply/
# appeal). A judicial-conduct or attorney-discipline matter => the accountability engine (Rule 18
# petition for review / new Rule 6 complaint / JCB / ODC). Accountability forums are tested FIRST
# because such a letter routinely also cites the underlying court case number.
_JC_MATTER_RE   = re.compile(r"\bJ\.?C\.?\s*(?:No\.?|Docket)\s*(\d{2}-\d{2}-\d{4,5})", re.I)
_JC_FEDERAL_RE  = re.compile(
    r"\bJ\.?C\.?\s*(?:No\.?|Docket)\s*\d{2}-\d{2}-\d{4,5}"
    r"|Office of the Circuit Executive"
    r"|Judicial Council of the (?:\w+\s+){0,3}Circuit"
    r"|Rules? for Judicial[- ]Conduct(?: and Judicial[- ]Disability)?", re.I)
_JCB_STATE_RE   = re.compile(
    r"\bJudicial Conduct Board\b|Court of Judicial Discipline", re.I)
_ODC_RE         = re.compile(
    r"Office of Disciplinary Counsel|Disciplinary Board of the Supreme Court", re.I)


def classify_forum(text: str) -> dict:
    """Route a served document to its drafting engine by FORUM. Returns
    {forum, engine, matter, authority}. engine 'accountability' -> pipeline.accountability_drafter;
    engine 'm0' -> pipeline.motion_drafter_v2 (the default: a court docket entry)."""
    t = text or ""
    if _JC_FEDERAL_RE.search(t):
        m = _JC_MATTER_RE.search(t)
        return {"forum": "judicial_council_federal", "engine": "accountability",
                "matter": (m.group(1) if m else ""), "authority": "28 U.S.C. §§ 351-364"}
    if _JCB_STATE_RE.search(t):
        return {"forum": "judicial_conduct_board_state", "engine": "accountability",
                "matter": "", "authority": "Pa. Const. art. V § 18"}
    if _ODC_RE.search(t):
        return {"forum": "attorney_discipline_odc", "engine": "accountability",
                "matter": "", "authority": "PA Rules of Professional Conduct; Disciplinary Board"}
    return {"forum": "court", "engine": "m0", "matter": "", "authority": ""}


# --- ACCOUNTABILITY POSTURE: within an accountability forum, which vehicle -----------------------
_JC_DISMISSED_RE = re.compile(
    r"\bdismiss(?:ed|ing|al)\b|\bconcluded?\b|\bterminat(?:e|ed|ion)\b|Petition for Review", re.I)
_RULE18_ELECTION_RE = re.compile(r"Petition for Review|Rule 18|Rule 6\b", re.I)


def accountability_vehicle(text: str, forum: dict) -> dict:
    """Given an accountability-forum document, pick the vehicle by posture. A dismissal/conclusion that
    offers a Rule 18 election -> Rule 18 Petition for Review; otherwise the initial complaint vehicle."""
    t = text or ""
    if forum.get("forum") == "judicial_council_federal" and _JC_DISMISSED_RE.search(t):
        return {"action": "reply", "vehicle": "rule18_petition_for_review",
                "engine": "accountability", "forum": forum,
                "reason": "Judicial-Council complaint dismissed; Rule 18 Petition for Review to the "
                          "Council (28 U.S.C. § 352(c) / Rule 18) is the responsive vehicle."}
    return {"action": "reply", "vehicle": "accountability_complaint",
            "engine": "accountability", "forum": forum,
            "reason": f"{forum.get('forum')} matter -> accountability filing"}


def appeal_posture(text: str, us_is_party: bool = False) -> dict | None:
    """Return an appeal action dict if the entry is appealable / certify-eligible, else None.
    Only a COURT-issued order can be appealable; a party's motion (whatever its title) never is."""
    c = classify(text)
    if c["kind"] != "order":
        return None
    if c["is_final"] or (c["dismiss"] and re.search(r"\b(ALL|ENTIRE|WITH PREJUDICE)\b", text or "", re.I)):
        days = 60 if us_is_party else 30
        return {"action": "appeal", "route": "appeal as of right",
                "authority": "28 U.S.C. 1291",
                "deadline_rule": f"FRAP 4(a)(1)({'B' if us_is_party else 'A'}): notice of appeal within {days} days",
                "days": days,
                "note": "If a timely Rule 59/60 motion is pending, FRAP 4(a)(4) runs the clock from its disposition."}
    if c["is_remand_denial"]:
        return {"action": "certify_interlocutory", "route": "not appealable as of right",
                "authority": "28 U.S.C. 1292(b)",
                "deadline_rule": "move the district court to certify (controlling question of law, "
                                 "substantial ground for difference, materially advance termination); "
                                 "then FRAP 5 petition within 10 days of certification",
                "days": 10}
    if c["is_injunction"]:
        return {"action": "appeal", "route": "interlocutory appeal of right",
                "authority": "28 U.S.C. 1292(a)(1)",
                "deadline_rule": "FRAP 4(a)(1): notice of appeal within 30 days", "days": 30}
    return None


# --- per-case reply state (one small JSON per case, lives next to the case) --------------------
def _state_path(slug: str, make: bool = False) -> str:
    d = os.path.join(CASES_BASE, slug)
    if make:
        os.makedirs(d, exist_ok=True)
    return os.path.join(d, "reply_state.json")


def load_state(slug: str, today: str) -> dict:
    p = _state_path(slug)            # read-only: never create a folder on load
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    # first time we see this case: cutoff = today. Everything earlier is reference-only.
    return {"slug": slug, "reply_cutoff": today, "replied": [], "appealed": [], "status": "active"}


def save_state(slug: str, state: dict) -> None:
    with open(_state_path(slug, make=True), "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


RESPONSE_RE = re.compile(r"\b(response in opposition|response to (?:plaintiff'?s? )?motion|"
                         r"opposition to (?:plaintiff|the motion)|reply in support)\b", re.I)

# A motion that, if GRANTED, ends the whole case: its grant is a final, appealable order (28 U.S.C.
# 1291). We do NOT reflexively oppose these -- absent false claims to correct, a granted dispositive
# order is the route to appeal, so we preserve by rebuttal and let it happen.
DISPOSITIVE_RE = re.compile(r"\b(summary judgment|judgment on the pleadings|rule 56|"
                            r"12\(b\)\(6\)|12\(c\)|dismiss\w*\b[^.]{0,40}\b(all|entire|"
                            r"with prejudice|complaint|action))\b", re.I)


def decide(filing: dict, state: dict, today: str, pro_se_name: str = "", us_is_party: bool = False,
           has_defects: bool = False, has_false_claims: bool = False) -> dict:
    """filing: {dkt, date (YYYY-MM-DD), text, filer}. Returns the action m0 should take, naming the
    precise VEHICLE (opposition vs rebuttal vs reply vs motion vs appeal) and any cross-motion that can
    ALSO be filed.
      has_defects       = the filing has procedural/stare-decisis defects (-> cross-motion to strike).
      has_false_claims  = the filing asserts false statements of fact against us (-> we OPPOSE to
                          correct the record). Absent false claims, an adverse motion is NOT reflexively
                          opposed: a dispositive one is let to proceed (rebut + appeal); else rebut.
    Both signals may also be supplied on the filing dict (audit-enriched docket)."""
    dkt = str(filing.get("dkt", "0"))
    date = filing.get("date") or ""
    text = filing.get("text") or ""
    filer = (filing.get("filer") or "")
    has_defects = has_defects or bool(filing.get("has_defects"))
    has_false_claims = has_false_claims or bool(filing.get("has_false_claims"))

    # appeal evaluation runs REGARDLESS of cutoff — the appeal clock is already ticking.
    ap = appeal_posture(text, us_is_party)
    if ap:
        if dkt in state.get("appealed", []):
            return {"dkt": dkt, "action": "none", "reason": "appeal/cert already recorded for this order"}
        return {"dkt": dkt, **ap, "reason": f"appealable order at dkt {dkt}",
                "trigger_date": date, "reference_only_below_cutoff": True}

    # reference-only: anything before the cutoff is NOT auto-replied to.
    if date and date < state.get("reply_cutoff", today):
        return {"dkt": dkt, "action": "reference_only",
                "reason": f"docketed {date} < cutoff {state.get('reply_cutoff')}; cite/attach, do not reply"}

    if dkt in state.get("replied", []):
        return {"dkt": dkt, "action": "none", "reason": "already replied to this filing"}

    if state.get("status") == "terminal":
        return {"dkt": dkt, "action": "none", "reason": "case terminal; appeal window closed"}

    c = classify(text)
    # L-OWN FIX: our own filings need no reply. Prefer the robust `ours` flag the fetcher already computed
    # (submission_reconciler token-match — catches 'Filer' when pro_se_name parsed only 'Filer'),
    # then the same matcher directly; the fragile surname-substring is only the last-resort fallback.
    _is_ours = filing.get("ours")
    if _is_ours is None:
        try:
            from pipeline.submission_reconciler import _ours as _recon_ours, _our_tokens
            _is_ours = _recon_ours(filer, _our_tokens())
        except Exception:
            _is_ours = bool(pro_se_name and pro_se_name.lower() in filer.lower())
    if _is_ours:
        return {"dkt": dkt, "action": "none", "reason": "our own filing"}
    # A court order COMMANDING a mandatory, deadline-bearing responsive filing: a Pa.R.A.P. 1925(b)
    # order directing a Concise Statement of Errors Complained of on Appeal. This is neither an
    # appealable order nor a mere denial to log -- it is a compliance obligation whose default is
    # WAIVER of the entire appeal (1925(b)(4)(vii)). Highest priority; it can only issue from the court.
    if c["kind"] == "order" and CONCISE_1925_RE.search(text):
        return {"dkt": dkt, "action": "reply", "vehicle": "concise_statement_1925b",
                "generator": "filing_composer.compose_filing",
                "deadline_rule": "Pa.R.A.P. 1925(b)(2): file of record AND concurrently serve the trial "
                                 "judge within the period the order sets (ordinarily 21 days); any issue "
                                 "omitted is waived (1925(b)(4)(vii)).",
                "reason": "trial court ordered a Concise Statement of Errors under Pa.R.A.P. 1925(b) -> "
                          "mandatory responsive filing: comprehensive (waive nothing) yet concise "
                          "(1925(b)(4)(ii),(iv)); preserves the appeal instead of defaulting it."}
    # opposing brief: their RESPONSE to our motion -> we file a REPLY; their fresh MOTION -> we
    # file an OPPOSITION. Both render through build_effective_reply_brief (the m0 rebuttal spine).
    if c["kind"] == "motion" or RESPONSE_RE.search(text):
        is_reply = bool(RESPONSE_RE.search(text))
        gen = "generation.argument_rebuttal.build_effective_reply_brief"

        # their RESPONSE to OUR motion -> we get the movant's last word: a REPLY in support.
        if is_reply:
            return {"dkt": dkt, "action": "reply", "vehicle": "reply", "generator": gen,
                    "reason": "their response to our motion -> reply (movant's last word)"}

        # An opposing MOTION is not reflexively opposed. m0 decides by what is IN it and where it LEADS.
        is_dispositive = bool(DISPOSITIVE_RE.search(text))
        if has_false_claims:
            # false statements of fact against us -> we MUST correct the record: oppose.
            vehicle = "opposition"
            reason = "opposing motion asserts false claims against plaintiff -> oppose to correct the record"
        elif is_dispositive:
            # no false claims to correct; if granted, this disposes of the whole case and becomes a final,
            # appealable order. Preserve objections by rebuttal and tee up the appeal -- do NOT oppose.
            vehicle = "rebuttal"
            reason = ("dispositive motion, no false claims to correct -> do not oppose; if granted it "
                      "disposes of the whole case and is appealable as of right (28 U.S.C. 1291). "
                      "Preserve objections by rebuttal and tee up the appeal.")
        else:
            vehicle = "rebuttal"
            reason = "opposing motion raises no false claims against plaintiff -> rebut to preserve, not oppose"

        out = {"dkt": dkt, "action": "reply", "vehicle": vehicle, "generator": gen, "reason": reason}
        if is_dispositive and not has_false_claims:
            out["posture"] = "non-opposition (strategic): allow the final order to enter, then appeal"
            out["appeal_if_granted"] = {
                "action": "appeal", "route": "appeal as of right", "authority": "28 U.S.C. 1291",
                "deadline_rule": f"FRAP 4(a)(1)({'B' if us_is_party else 'A'}): notice of appeal within "
                                 f"{60 if us_is_party else 30} days of the final order"}
        # whatever the primary vehicle, a procedurally / stare-decisis DEFECTIVE filing can ALSO be struck.
        if has_defects:
            out["also_file"] = {"vehicle": "motion_to_strike",
                                "basis": "the filing's own procedural/stare-decisis defects (scorecard)",
                                "note": "offensive cross-motion, filed alongside the primary response"}
        return out
    # adverse interlocutory ORDER. A VOID / excess-of-authority order -> motion to vacate (strong).
    # Otherwise the DEFAULT is the ACCOUNTABILITY LEDGER, NOT reconsideration: m0's motions are airtight,
    # so a bare/conclusory denial is the COURT's error -> log it to the scorecard + judicial oath/duty-failure
    # record, preserve for appeal, and feed the 28 U.S.C. 351 / JCB complaint. Reconsideration (the weakest
    # motion -- re-arguing to the judge who just denied you) fires ONLY on a narrow special ground shown on
    # the face of the order (overlooked controlling authority / new dispositive evidence / manifest error).
    is_void = bool(re.search(r"\b(void|without jurisdiction|exceed)", text, re.I))
    if c["kind"] == "order" and (c["denied"] or is_void or (c["granted"] and not c["is_final"])):
        if is_void:
            return {"dkt": dkt, "action": "reply", "vehicle": "motion_to_vacate",
                    "reason": "void / excess-of-authority order -> motion to vacate"}
        _recon_ground = bool(re.search(
            r"\b(overlook|did not (?:address|consider)|new (?:evidence|fact)|manifest (?:error|injustice)|"
            r"intervening (?:change|decision|authority))", text, re.I))
        if _recon_ground:
            return {"dkt": dkt, "action": "reply", "vehicle": "motion_to_reconsider",
                    "reason": "narrow special ground on the order's face (overlooked authority / new evidence / "
                              "manifest error) -> reconsideration warranted"}
        return {"dkt": dkt, "action": "reply", "vehicle": "statement_of_reasons",
                "reason": "conclusory adverse order -> demand a statement of reasons + pose corpus-grounded "
                          "questions (the active squeeze); the order is ALSO logged to the ledger and feeds the "
                          "28 U.S.C. 351 / JCB oath/duty-failure record. Reconsideration declined as the weakest motion."}
    return {"dkt": dkt, "action": "none", "reason": "no responsive obligation triggered"}


def process_case(slug, filings, today, pro_se_name="", us_is_party=False, commit=False):
    """filings: list of {dkt,date,text,filer} (e.g. straight from docket_email_fetch). A filing may
    carry 'has_defects' (scorecard/procedural defects -> cross-motion to strike). Returns the
    actions; if commit=True, records replies/appeals into the state so they aren't repeated."""
    state = load_state(slug, today)
    actions = []
    for f in sorted(filings, key=lambda x: int(str(x.get("dkt", 0)) or 0)):
        a = decide(f, state, today, pro_se_name, us_is_party, has_defects=bool(f.get("has_defects")))
        actions.append(a)
        if commit:
            if a["action"] == "reply":
                state.setdefault("replied", []).append(a["dkt"])
            elif a["action"] in ("appeal", "certify_interlocutory"):
                state.setdefault("appealed", []).append(a["dkt"])
                state["status"] = "appealable"
    if commit:
        save_state(slug, state)
    return {"slug": slug, "cutoff": state["reply_cutoff"], "actions": actions}


def draft_for_decision(case_id: str, filing: dict, decision: dict, caption: dict | None = None,
                       case_facts: list | None = None) -> str | None:
    """Connect a gate decision to its generator. Opposition/reply -> the m0 point-by-point rebuttal
    brief. Motions to reconsider/vacate and appeals route to their existing filing-pipeline builders
    (not duplicated here); this returns None for those so the caller dispatches them downstream."""
    if decision.get("action") == "reply" and decision.get("vehicle") in ("opposition", "reply"):
        from generation.argument_rebuttal import build_effective_reply_brief
        return build_effective_reply_brief(case_id, filing.get("text", ""), caption=caption,
                                           case_facts=case_facts)
    return None


if __name__ == "__main__":
    today = "2026-06-19"
    demo = [
        {"dkt": "145", "date": "2026-06-01", "text": "ORDER THAT MOTION FOR RECONSIDERATION (DKT 144) IS DENIED. SIGNED BY DISTRICT JUDGE.", "filer": "court"},
        {"dkt": "150", "date": "2026-06-20", "text": "DEFENDANT'S MOTION TO DISMISS PLAINTIFF'S AMENDED COMPLAINT", "filer": "defendant"},
        {"dkt": "151", "date": "2026-06-25", "text": "ORDER DENYING PLAINTIFF'S MOTION TO REMAND", "filer": "court"},
        {"dkt": "160", "date": "2026-07-10", "text": "JUDGMENT ENTERED. CASE CLOSED. ALL CLAIMS DISMISSED WITH PREJUDICE.", "filer": "court"},
    ]
    import pprint
    pprint.pprint(process_case("fed_2_26-cv-00123", demo, today, pro_se_name="Filer", us_is_party=False))
