"""
pipeline/scorecard_if_then.py — Path-A scorecard IF/THEN engine.

LIFTED from generation/reply_brief_drafter.py (which is being retired) so Path A
owns this logic and does not depend on that module. Produces:
  - Section II: named defect + record fact + rule + remedy (per category)
  - Section III: deterministic IF / AND / THEN ending in void/voidable/stricken

Reads detected violations from output/audit/data_<slug>.json (the same audit
file the analysis writes). WSL-safe paths.
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import json as _json
import os as _os
from pathlib import Path as _Path



# === SCORECARD_ECF_V1: resolve a source_file to a docket ECF number ===
import json as _json, re as _re_ecf
_DOCKET_PATH = str(_eroot() / "_docket_parsed.json")
_ECF_RULES = [
    (["verified", "complaint"], 124), (["verified", "felony"], 124),
    (["tentative", "default"], 36), (["default"], 22),
    (["objection"], 56), (["compel"], 58), (["remand"], 17),
    (["amended", "complaint"], 52), (["leave", "file"], 29),
    (["clarification"], 33), (["application", "petition"], 33),
    (["response"], 24), (["vacate"], 125),
]
def _resolve_source_to_ecf(source_file):
    """Best-effort source_file -> 'ECF No. X' via keyword. None if no match
    or if it's a non-federal (e.g. state CCP) doc."""
    if not source_file:
        return None
    s = source_file.lower()
    if "common pleas" in s or "not this one" in s:
        return None  # state-court doc, not a federal ECF entry
    for kws, ecf in _ECF_RULES:
        if all(k in s for k in kws):
            return f"ECF No. {ecf}"
    return None


# === CELL_CLEAN_V1 =========================================================
def _scrub_cell_text(text):
    """Strip raw-record contamination from a Record Basis cell. Removes:
      - 'Filed in <filename> on <date|None>' tails
      - 'Specifically, the record contains: "..."' tails
      - Trailing ellipses from upstream truncation
    The cell is a filed exhibit; describe the defect in clean prose. The ECF
    cite is added separately by _resolve_source_to_ecf. Agnostic."""
    if not text:
        return text
    import re as _r
    text = _r.sub(
        r"\s*Filed in [^\n]+?(?:\.pdf|\.docx|\.doc|\.txt)\s+on\s+[^.\n]+\.?\s*$",
        "", text, flags=_r.IGNORECASE)
    text = _r.sub(
        r"\s*Filed in [^\n]+?\s+on\s+(?:None|\d{4}-\d{2}-\d{2})\.?\s*$",
        "", text, flags=_r.IGNORECASE)
    text = _r.sub(
        r'\s*Specifically,\s+the\s+record\s+contains:\s*"[^"]*"?\.?\s*$',
        "", text, flags=_r.IGNORECASE)
    text = _r.sub(r"\.\.\.\s*$", "", text)
    return text.strip()
# === END CELL_CLEAN_V1 ====================================================


def _audit_dir():
    """Resolve <repo>/output/audit relative to this file (WSL-safe)."""
    here = _Path(__file__).resolve().parent          # .../pipeline
    repo = here.parent                                # .../AI_Beezle
    cand = repo / "output" / "audit"
    if cand.exists():
        return cand
    for alt in (_Path(str(_eroot() / "output/audit")),
                _Path.cwd() / "output" / "audit"):
        if alt.exists():
            return alt
    return cand



# REPLICATE_STACKS_V1
_PROCEDURAL_REMEDIES = {
    "Pa.R.C.P. 1024":  "the unverified pleading shall be stricken under Pa.R.C.P. 1028(a)(2).",
    "Pa.R.C.P. 1028":  "the non-conforming pleading shall be stricken.",
    "Pa.R.C.P. 440":   "the filing has no legal effect and must be stricken.",
    "Pa.R.C.P. 3145":  "the garnishee's failure to answer triggers default under Pa.R.C.P. 3146.",
    "Pa.R.C.P. 3146":  "default judgment against the garnishee is appropriate.",
    "Pa.R.C.P. 237.3": "the resulting judgment is void and subject to relief.",
    "Pa.R.C.P. 1023.1": "the certification requirement was violated; sanctions are warranted.",
    "Pa.R.C.P. 1023.4": "sanctions are warranted to deter recurrence.",
    "FRCP 4":          "service is defective and the filing is without legal effect.",
    "FRCP 5":          "service of subsequent papers is defective; the filing must be disregarded.",
    "FRCP 11":         "sanctions are warranted under Rule 11(c).",
    "FRCP 12(b)":      "the defense is improperly raised and must be dismissed.",
    "FRCP 12(f)":      "the immaterial or insufficient matter is properly stricken.",
    "FRCP 60(b)(4)":   "the void judgment shall be vacated.",
    "U.S. Const. amend. XIV § 1": "the resulting action lacks due process and is void.",
    "14th Amendment":  "the resulting action lacks due process and is void.",
}


_SCORECARD_HOOK_MAP = {
    "CASE_FACT_CONFLICT": {
        "name": "Irreconcilable Facts Across the Claimant's Own Filings",
        "rule_state": "Pa.R.E. 803(25); 13 Pa.C.S. § 3308; UTPCPL, 73 P.S. § 201-2(4)",
        "rule_federal": "Fed. R. Evid. 801(d)(2); 26 U.S.C. §§ 6050P, 7434; 12 U.S.C. § 2605(e); U.C.C. § 9-210",
        "remedy_state": "resolved against the claimant: election of one position, full accounting, and dismissal of enforcement built on the abandoned position",
        "remedy_federal": "resolved against the claimant: election of one position, full accounting, and dismissal of enforcement built on the abandoned position",
        "frame": ("the claimant's own documents assert facts that cannot all be true "
                  "(discharge reported while the same debt is billed; amounts that do "
                  "not reconcile; insurance kept active on a debt foreclosed as "
                  "defaulted); each statement is a party-opponent admission, and the "
                  "enforcement demand resting on them must be"),
        "subsection_logic": "Party-opponent statements are non-hearsay; mutually exclusive fact assertions mean at least one is false — a 6050P discharge report coexisting with a live demand exposes the filer to 26 U.S.C. 7434 or abandons the demand, and unreconciled accounting defeats the amount claimed.",
    },
    "PERSONAL_JURISDICTION_UNESTABLISHED": {
        "name": 'Personal Jurisdiction Never Established Over the Party',
        "rule_state": '42 Pa.C.S. 5322 (long-arm); Pa.R.C.P. 402-404 (service); due process minimum contacts',
        "rule_federal": 'Fed. R. Civ. P. 4(k); International Shoe minimum-contacts line; specific vs general jurisdiction; Fed. R. Civ. P. 12(b)(2)',
        "remedy_state": 'dismissed as to the party, or all acts against them void for want of personal jurisdiction',
        "remedy_federal": 'dismissed under 12(b)(2); judgment against the party void under 60(b)(4)',
        "frame": "a court's power over a PERSON requires valid service AND constitutionally sufficient contacts (in personam), or the res within the forum (in rem/quasi in rem); neither was established here — and a judgment without personal jurisdiction is void, not merely erroneous, so the affected rulings must be",
        "subsection_logic": 'IF service was defective or contacts insufficient under the minimum-contacts line, AND the party preserved the objection (Rule 12(h) runs BOTH ways), THEN personal jurisdiction fails and every act against that party is void.',
    },
    "RESTITUTION_REMEDY_AVAILABLE": {
        "name": 'Restitution / Constructive Trust / Subrogation Triggered by Double Recovery',
        "rule_state": 'PA unjust enrichment (benefit conferred, appreciation, inequitable retention); constructive trust; equitable subrogation',
        "rule_federal": "restitution doctrine; constructive trust as remedy for property obtained by fraud/mistake; subrogation (insurer steps into insured's shoes)",
        "remedy_state": 'disgorgement of the double recovery; constructive trust impressed on proceeds; subrogation accounting compelled',
        "remedy_federal": 'disgorgement; constructive trust on identifiable proceeds; accounting of all recoveries on the obligation',
        "frame": 'the record shows multiple recovery lanes on one obligation (cancellation reported, insurance premiums flowing, securitization proceeds taken, foreclosure pursued); where a party has recovered or positioned to recover more than once, restitution law supplies the AFFIRMATIVE remedy — the excess is held for the party who bore the loss, and must be',
        "subsection_logic": 'IF an obligation shows more than one recovery path exercised (1099-C + insurance + securitization + enforcement), AND the claimant retains benefits it would be inequitable to keep, THEN unjust enrichment/constructive trust/subrogation convert the double-recovery FINDING into affirmative RELIEF.',
    },
    "MANDATORY_LANGUAGE_DISREGARDED": {
        "name": "Mandatory 'Shall' Duty Treated as Optional",
        "rule_state": "1 Pa.C.S. 1921(b) (clear text not disregarded under pretext of its spirit); the statute's own command word",
        "rule_federal": "plain-meaning rule; the enacted text's command language (shall = mandatory)",
        "remedy_state": 'the mandatory duty performed or the ruling built on its disregard vacated',
        "remedy_federal": 'the mandatory duty performed or the ruling built on its disregard vacated',
        "frame": "the text says 'shall' and the ruling treats it as directory or weighs it as a factor; command words are not suggestions — 1 Pa.C.S. 1921(b) forbids disregarding clear text under the pretext of pursuing its spirit, and rulings resting on the disregard must be",
        "subsection_logic": "IF the governing text uses 'shall/must', AND the ruling declines the duty or converts it to discretion, THEN the ruling displaces enacted text with interpretation and cannot stand on that point.",
    },
    "EQUITY_MAXIM_APPLICABLE": {
        "name": 'Controlling Equity Maxim Engaged by the Record',
        "rule_state": 'equity jurisprudence (maxims operative in equitable proceedings); Pa. foreclosure/quiet title are equitable',
        "rule_federal": "equity jurisprudence (Broom's Maxims; equity treatises); clean hands, vigilance, substance over form",
        "remedy_state": 'the maxim applied as forum law: the offending party denied equitable relief',
        "remedy_federal": 'the maxim applied as forum law: the offending party denied equitable relief',
        "frame": "this is an equitable proceeding and the record engages a controlling maxim (clean hands against inconsistent 1099-C positions and conflicting productions; substance over form against label-deep claims; no profit from one's own wrong against self-created defaults); maxims are the operative law of the equity forum and must be",
        "subsection_logic": "IF the proceeding sounds in equity, AND the record shows the claimant's own inequity in the same transaction, THEN clean hands bars the remedy outright — the maxim decides, not discretion.",
    },
    "AGENCY_AUTHORITY_UNPROVEN": {
        "name": 'Act Taken Without Proven Agency or Authority',
        "rule_state": 'Restatement (agency); Pa.R.C.P. 1024 (verification by party with knowledge)',
        "rule_federal": 'actual/apparent authority doctrine; Fed. R. Evid. 602 (personal knowledge); Fed. R. Civ. P. 17(a)',
        "remedy_state": 'the act disregarded until the authority chain (appointment, scope, ratification) is proven of record',
        "remedy_federal": 'the act disregarded until the authority chain is proven of record',
        "frame": "a signer, servicer, or representative acted for a principal without record proof of authority (appointment, delegation, scope); acts of an unproven agent bind no one, and documents executed by them (allonges signed by 'Collateral Specialists', affidavits by servicer employees about owner records) are",
        "subsection_logic": 'IF an act (signature, affidavit, filing) is performed by a purported agent, AND no authority chain appears of record, THEN the act is ineffective as to the principal and the document it produced proves nothing until authority is shown.',
    },
    "VENUE_IMPROPER": {
        "name": 'Venue Improper or In-Rem Power Absent',
        "rule_state": 'Pa.R.C.P. 1006 (venue; actions on real property where the land lies)',
        "rule_federal": '28 U.S.C. 1391 (venue); local-action doctrine (real property actions in the district of the property)',
        "remedy_state": 'transferred to the proper county or dismissed',
        "remedy_federal": 'dismissed or transferred under 28 U.S.C. 1406(a)',
        "frame": 'the action or claim sits in a forum without venue over it — in particular, claims operating on real property belong where the property lies (local action), and a court elsewhere lacks the in-rem power the claim requires; the misplaced claims must be',
        "subsection_logic": 'IF a claim operates on real property (foreclosure, quiet title, title determination), AND the forum is not where the property lies, THEN venue/in-rem power is absent and transfer or dismissal follows.',
    },
    "AGENCY_OWN_RULES_VIOLATED": {
        "name": 'Agency Bound by Its Own Rules Departed From Them (Accardi)',
        "rule_state": '1 Pa. Code (agency procedural rules bind the agency); administrative-agency law',
        "rule_federal": 'United States ex rel. Accardi v. Shaughnessy, 347 U.S. 260; 5 U.S.C. 706(2)(A) (arbitrary/capricious set-aside); IRS IRM + TFM in corpus as agency-own-rules predicate sources',
        "remedy_state": 'the agency action set aside and the agency held to its published procedure',
        "remedy_federal": 'the agency action set aside under 706(2); mandamus under 1361 for withheld mandatory action',
        "frame": 'an agency (HUD, CFPB, servicer acting under agency program rules, court clerk under administrative procedures) departed from its own published rules; under Accardi an agency is bound by its own regulations while they remain in force, and action taken in violation of them must be',
        "subsection_logic": "IF the agency's own published rule prescribes a procedure, AND the record shows departure from it, THEN the resulting action is invalid without reaching the merits — the agency's own text decides.",
    },
    "CONDITION_PRECEDENT_UNMET": {
        "name": 'Statutory Condition Precedent Never Satisfied',
        "rule_state": '41 P.S. 403 (Act 6 notice); 35 P.S. 1680.402c seq. (Act 91 / HEMAP, 12 Pa. Code Ch. 31); Pa.R.C.P. 1147',
        "rule_federal": '24 CFR 203.602 seq. (FHA loss-mitigation/face-to-face prerequisites); 12 CFR 1024.41 (Reg X loss-mit before foreclosure referral)',
        "remedy_state": 'the action dismissed or stayed until every statutory prerequisite is proven satisfied',
        "remedy_federal": 'the action dismissed or the referral undone; Reg X and FHA servicing violations pled affirmatively',
        "frame": 'enforcement was commenced without proof that the statutory conditions precedent were performed (Act 6/Act 91 notices, FHA servicing prerequisites, Reg X loss-mitigation review); these are not technicalities — they are legislative gates on the power to foreclose, and an action commenced through an unopened gate must be',
        "subsection_logic": 'IF a statute conditions enforcement on prior notice or servicing steps, AND the record lacks proof of each step, THEN the enforcement action fails at the threshold regardless of the merits.',
    },
    "JUDICIAL_NOTICE_COMPELLED": {
        "name": 'Adjudicative Fact Compelling Judicial Notice',
        "rule_state": 'Pa.R.E. 201 (judicial notice; mandatory on request with necessary information)',
        "rule_federal": 'Fed. R. Evid. 201(c)(2) (court MUST take notice if requested and supplied with the necessary information)',
        "remedy_state": 'notice taken of the government-record fact and the analysis conducted on it',
        "remedy_federal": 'notice taken; refusal to notice a qualifying fact is reviewable error',
        "frame": 'facts from sources whose accuracy cannot reasonably be questioned (government databases, recorded instruments, court dockets, HUD/FHA system screens, IRS transcripts) qualify for judicial notice; on request with the material supplied, notice is MANDATORY under Rule 201(c)(2), and the qualifying facts here must be',
        "subsection_logic": "IF a fact comes from a source whose accuracy cannot reasonably be questioned, AND notice is requested with the material supplied, THEN the court must take notice — 'may' is not the rule's text; 'must' is.",
    },
    "BURDEN_MISALLOCATED": {
        "name": 'Burden of Proof Placed on the Wrong Party',
        "rule_state": 'burden follows the party asserting the claim/affirmative defense; Pa.R.C.P. 1029',
        "rule_federal": 'the proponent bears the burden on each element; presumptions shift production not persuasion (Fed. R. Evid. 301)',
        "remedy_state": 'the ruling re-made with the burden correctly allocated',
        "remedy_federal": 'the ruling re-made with the burden correctly allocated',
        "frame": 'the enforcing party was relieved of its burden (standing, holder status, authority, condition-precedent compliance) and the burden was shifted to the responding party to DISPROVE the claim; burden allocation is substantive law, and a ruling built on the inverted burden must be',
        "subsection_logic": 'IF the claimant bears the burden on an element (PETE status, conditions precedent, agency), AND the ruling demanded the respondent disprove it instead, THEN the ruling misapplied the law of burdens and cannot stand on that element.',
    },
    "CLAIM_PRECLUSION_BAR": {
        "name": 'Res Judicata / Claim Preclusion Bar',
        "rule_state": 'res judicata: final judgment on the merits, same parties/privies, same cause of action',
        "rule_federal": 'claim preclusion: final judgment on the merits bars claims that were or could have been raised',
        "remedy_state": 'the precluded claim dismissed; the prior judgment given its full effect',
        "remedy_federal": 'the precluded claim dismissed; the prior judgment given its full effect',
        "frame": 'a claim already concluded by final judgment between these parties is being relitigated (or an opponent splits claims to evade a prior loss); preclusion cuts BOTH ways — their prior losses and admissions in concluded proceedings bind them here, and the precluded matter must be',
        "subsection_logic": "IF a final judgment on the merits exists between the parties, AND the present claim was or could have been raised there, THEN it is barred — and the opponent's positions taken in the prior action bind it as admissions here.",
    },
    "ISSUE_PRECLUSION_BAR": {
        "name": 'Collateral Estoppel / Issue Preclusion Bar',
        "rule_state": 'issue actually litigated, determined, and essential to a prior final judgment binds the parties',
        "rule_federal": 'issue preclusion: identical issue, actually litigated, necessarily decided, full and fair opportunity',
        "remedy_state": 'the issue taken as established and not relitigated',
        "remedy_federal": 'the issue taken as established and not relitigated',
        "frame": 'an issue this record shows was actually litigated and decided (or that an opponent concedes by its own prior positions) is being re-argued as if open; the determined issue must be',
        "subsection_logic": 'IF an identical issue was actually litigated and necessarily decided in a prior proceeding with full opportunity, THEN it is established here and relitigation is barred.',
    },
    "LAW_OF_THE_CASE_VIOLATED": {
        "name": 'Law of the Case Disregarded',
        "rule_state": "coordinate-jurisdiction rule: a judge may not overrule another judge's prior decision in the same case absent new law/facts",
        "rule_federal": 'law-of-the-case doctrine: prior rulings govern subsequent stages absent extraordinary circumstances',
        "remedy_state": 'the departing ruling vacated and the prior ruling enforced',
        "remedy_federal": 'the departing ruling vacated and the prior ruling enforced',
        "frame": 'a later ruling silently departs from an earlier ruling in the same case (or a second judge overrules the first without new law or facts); the departure is error under the law of the case and the coordinate-jurisdiction rule, and the earlier ruling must be',
        "subsection_logic": 'IF an issue was decided earlier in this same case, AND a later ruling departs without new controlling law or materially new facts, THEN the departure is reversible error and the earlier ruling controls.',
    },
    "OPPONENT_WAIVER_FORFEITURE": {
        "name": "Opponent's Waiver or Forfeiture of the Objection",
        "rule_state": 'waiver = intentional relinquishment of a known right; objections not timely raised are waived (Pa.R.C.P. 1032)',
        "rule_federal": 'Fed. R. Civ. P. 12(h) (defenses waived if omitted); forfeiture by failure to timely assert',
        "remedy_state": 'the untimely objection disregarded as waived',
        "remedy_federal": 'the untimely objection disregarded as waived/forfeited',
        "frame": 'the opponent sat on the objection it now raises (or failed to deny, respond, or preserve in sequence) — waiver and forfeiture run against ENFORCERS and their counsel exactly as they are invoked against pro se litigants; the belated objection must be',
        "subsection_logic": "IF the objection was available and not timely raised (12(h); 1032; silence in response to averment), THEN it is waived or forfeited, and reciprocally the respondent's own preserved objections stand un-waived on this record.",
    },
    "CANDOR_VIOLATION": {
        "name": "Counsel's Duty of Candor Violated",
        "rule_state": 'Pa. RPC 3.3 (candor toward the tribunal); RPC 3.4 (fairness); RPC 8.4(c) (dishonesty)',
        "rule_federal": 'Model RPC 3.3; Fed. R. Civ. P. 11(b) (representations to the court); 28 U.S.C. 1927',
        "remedy_state": 'the offending assertion stricken; referral to the Disciplinary Board; fees under 42 Pa.C.S. 2503',
        "remedy_federal": 'the assertion stricken; Rule 11 motion after safe harbor; 1927 excess costs; disciplinary referral',
        "frame": "counsel asserted to the tribunal facts its own client's documents contradict (holder status against a blank allonge; verification by copies against an admitted off-site original; citizenship of an entity not the one sued); Rule 3.3 makes the LAWYER answerable for the falsity independent of the client, and the assertions must be",
        "subsection_logic": "IF counsel's filed statement conflicts with the client's own record documents, THEN the statement violates the candor duty and Rule 11(b), the contradiction is a party-opponent admission, and the remedy runs against counsel as well as the claim.",
    },
    "RESCISSION_EFFECT_IGNORED": {
        "name": "TILA Rescission's Automatic Effect Ignored",
        "rule_state": 'rescission under federal law displaces inconsistent state enforcement while effective',
        "rule_federal": '15 U.S.C. 1635(b): security interest becomes VOID on rescission by operation of law; 12 CFR 1026.23(d); Jesinoski v. Countrywide (notice suffices)',
        "remedy_state": 'enforcement of the voided security interest halted pending resolution of the rescission',
        "remedy_federal": 'the security interest treated as void per 1635(b) unless and until a court orders otherwise; foreclosure premised on it fails',
        "frame": 'a 1635 rescission notice was sent and the statutory sequence (void security interest, return of consideration, 20-day creditor duties) was never engaged or adjudicated; Jesinoski holds notice alone effects rescission — enforcement that simply ignores the outstanding rescission must be',
        "subsection_logic": "IF a rescission notice issued within the governing window, AND no court has adjudicated it invalid, THEN the security interest's claimed force is disputed at the root and foreclosure on it cannot proceed as if the notice never existed.",
    },
    "STATUTORY_DAMAGES_AVAILABLE": {
        "name": 'Statutory Damages Triggered on the Record',
        "rule_state": '73 P.S. 201-1 seq. (UTPCPL: actual damages, treble, fees); 42 Pa.C.S. 8371 (insurance bad faith)',
        "rule_federal": '15 U.S.C. 1692k (FDCPA); 15 U.S.C. 1681n/1681o (FCRA - Safeco v. Burr: willfulness includes reckless disregard); 12 U.S.C. 2605(f) (RESPA); 26 U.S.C. 7434 (false information returns)',
        "remedy_state": 'damages pled and computed per violation on the record evidence',
        "remedy_federal": 'statutory, actual, and punitive damages pled per violation; fees and costs',
        "frame": 'the record violations are not only defenses — each mapped violation carries its own statutory damages provision (FDCPA 1692k, FCRA 1681n/o, RESPA 2605(f), 7434 for false 1099s, UTPCPL treble, 8371 bad faith); the affirmative recovery must be',
        "subsection_logic": 'IF a statutory violation is established by the record, AND the statute carries a private damages remedy, THEN the same finding that defeats their claim FUNDS the counterclaim — pled, computed, and preserved.',
    },
    "UNREBUTTED_ADVERSE_STATEMENT": {
        "name":           "Adverse Record Statement Standing Unanswered",
        "rule_state":     "Pa.R.C.P. 1029(b) (averments not denied are admitted); the record-silence risk",
        "rule_federal":   "Fed. R. Civ. P. 8(b)(6) (allegation not denied is admitted); Fed. R. Evid. 801(d)(2)",
        "remedy_state":   "answered, distinguished, or moved against in the present filing",
        "remedy_federal": "answered, distinguished, or moved against in the present filing",
        "frame":  ("an adverse statement (court directive or opponent claim) sits in the record with "
                   "no responsive filing detected; unanswered adverse statements risk being treated as "
                   "conceded and constrain the appellate record, so each one identified here is now "
                   "expressly"),
        "subsection_logic": ("IF the statement ledger shows an adverse statement with no responsive "
                             "pro-se filing, THEN this filing answers it directly: rebutted with record "
                             "proof, distinguished on its terms, or moved against \u2014 preserving the "
                             "issue and preventing concession by silence."),
    },
        "DEFINITION_DIVERGENCE": {
        "name":           "Term of Art Used in Its Colloquial Sense Against the Controlling Definition",
        "rule_state":     "1 Pa.C.S. 1903 (words and phrases; technical words per their peculiar meaning); 13 Pa.C.S. 1201, 3301 (defined terms control)",
        "rule_federal":   "1 U.S.C. 1 (Dictionary Act); the controlling definitional statute per term (U.C.C. 1-201(b)(21), 26 U.S.C. 6050P, 15 U.S.C. 1692a); Gould v. Gould, 245 U.S. 151 (doubt construed against the government)",
        "remedy_state":   "the argument re-analyzed under the statutory definition, colloquial-sense reliance disregarded",
        "remedy_federal": "the argument re-analyzed under the statutory definition, colloquial-sense reliance disregarded",
        "frame":  ("the filing trades on the ordinary meaning of a defined term (discharge, holder, "
                   "includes, tender, creditor, verification) where the controlling text defines it "
                   "differently; statutory definitions are not optional \u2014 the argument must rise or "
                   "fall on the DEFINED meaning, and reliance on the colloquial sense is a "
                   "definitional misrepresentation that must be"),
        "subsection_logic": ("IF a term the statute defines is deployed in its ordinary sense, AND the "
                             "defined meaning materially differs (possession-based holder status; "
                             "6050P identifiable-event discharge reporting; means-closed vs includes-"
                             "expansive), THEN the assertion fails under the definition that governs it, "
                             "and the drafter states the definition first, then holds every use to it."),
    },
        "ENTITY_IDENTITY_CONFLICT": {
        "name":           "Party Litigating Under an Unproven Second Identity",
        "rule_state":     "Pa.R.C.P. 2002 (real party in interest); Pa.R.C.P. 1024 (verification)",
        "rule_federal":   "Fed. R. Civ. P. 17(a); 28 U.S.C. 1332, 1446(a) (citizenship of the entity actually sued)",
        "remedy_state":   "stricken unless the identity chain (registration, merger, succession) is proven of record",
        "remedy_federal": "stricken or the action remanded/dismissed unless the identity chain is proven of record",
        "frame":  ("a party appearing under a different identity than the entity named ('incorrectly "
                   "captioned as', a/k/a, f/k/a) has asserted its own non-identity: pleadings filed by "
                   "the unproven entity are a nullity as to the named party, and in removal the "
                   "citizenship actually pleaded belongs to the WRONG entity, defeating the "
                   "jurisdictional showing; the filings making the swap are party-opponent admissions "
                   "under FRE 801(d)(2) and must be"),
        "subsection_logic": ("IF a filing asserts the answering entity differs from the entity sued, "
                             "AND no corporate-identity proof (Secretary of State registration, merger or "
                             "succession documents) is of record, THEN claims and defenses asserted under "
                             "either name are impeached by the existence of the other, and any removal "
                             "premised on the unnamed entity's citizenship fails at the threshold."),
    },
        "CROSS_PARTY_ROLE_CONFLICT": {
        "name": "Mutually Exclusive Role Claims by Opposing Parties",
        "rule_state": "Pa.R.C.P. 2002; 13 Pa.C.S. § 3301; Pa.R.E. 803(25)",
        "rule_federal": "Fed. R. Civ. P. 17(a); U.C.C. § 3-301; Fed. R. Evid. 801(d)(2)",
        "remedy_state": "resolved against the claimants: each must prove its claimed role or the enforcement claim is dismissed",
        "remedy_federal": "resolved against the claimants: each must prove its claimed role or the enforcement claim is dismissed",
        "frame": ("the record contains mutually exclusive statements — each a party-opponent "
                  "admission — about who holds the enforceable interest; statements that "
                  "cannot all be true establish that at least one enforcement posture is "
                  "false, and the conflict must be"),
        "subsection_logic": "Party-opponent statements are non-hearsay (FRE 801(d)(2)); mutually exclusive role claims for the same instrument mean at least one claimant is not the real party in interest under Rule 17(a)/3301, and none may enforce until the conflict is resolved by proof.",
    },
    "INSTRUMENT_VERSION_CONFLICT": {
        "name": "Instrument Version Conflict Across Productions",
        "rule_state": "13 Pa.C.S. §§ 3204(a), 3205(b), 3301; Pa.R.E. 1003",
        "rule_federal": "U.C.C. §§ 3-204(a), 3-205(b), 3-301; Fed. R. Evid. 1003; Fed. R. Civ. P. 17(a)",
        "remedy_state": "resolved by compelled production of the original instrument, with enforcement reliance on the conflicting copies stricken",
        "remedy_federal": "resolved by compelled production of the original instrument, with enforcement reliance on the conflicting copies stricken",
        "frame": ("two productions of the same instrument in materially different "
                  "physical states cannot both be authentic; an indorsement exists only "
                  "on the instrument or a paper affixed to it, a blank indorsement makes "
                  "the paper enforceable only by its possessor, and a duplicate is "
                  "inadmissible once a genuine question of authenticity is raised, so the "
                  "conflict must be"),
        "subsection_logic": "An allonge that appears in one production and is absent from another was either never affixed (no indorsement, 13 Pa.C.S. 3204(a)) or created separately (authenticity failure, FRE 1003); on either horn the proponent is not shown to be a person entitled to enforce under 3301.",
    },
    # C3: jurisdiction / void-judgment / accountability categories — the CORRECT rule per each category's own
    # doctrine (needs_hook hint), corpus-verifiable via rules_index. Previously these fell through the default
    # STRIKE hook, rendering a jurisdictional defect under Pa.R.C.P. 1028(a)(2)/Fed.R.Civ.P. 12(f) — wrong.
    "REAL_PARTY_RECIPROCITY": {
        "name": "Standing / Real-Party Rule Applied Reciprocally to the Enforcing Filer",
        "rule_state": "Pa.R.C.P. 2002; 13 Pa.C.S. §§ 3203, 3205, 3301",
        "rule_federal": "Fed. R. Civ. P. 17(a); U.C.C. §§ 3-203, 3-205, 3-301",
        "remedy_state": "dismissed or the real party in interest joined",
        "remedy_federal": "dismissed or the real party in interest joined",
        "frame": "the real-party-in-interest requirement binds the party who FILED to enforce: an enforcing filer that cannot prove its own holder or person-entitled-to-enforce status is disqualified by the identical rule it invoked, and the enforcing filing must be",
        "subsection_logic": "Rule 17(a) and U.C.C. 3-301 are reciprocal: the same real-party / holder showing demanded of the pro se party is a threshold burden on the party who filed to enforce; a servicer that placed the note in the record without an allonge or indorsement, where the note is held by a securitization trust, is not the holder and cannot enforce.",
    },
    "STANDING_DEFECT": {
        "name": "Lack of Article III Standing",
        "rule_state": "standing doctrine (injury-in-fact, causation, redressability)",
        "rule_federal": "U.S. Const. art. III, § 2; Lujan v. Defenders of Wildlife, 504 U.S. 555",
        "remedy_state": "dismissed for lack of standing",
        "remedy_federal": "dismissed for lack of standing",
        "frame": "the party invoking the court's power lacks Article III standing, so the claim must be",
        "subsection_logic": "Article III standing requires a concrete, particularized injury-in-fact fairly traceable to the challenged conduct and redressable by a favorable decision; absent standing the court has no jurisdiction.",
    },
    "SUBJECT_MATTER_JURISDICTION_DEFECT": {
        "name": "Lack of Subject-Matter Jurisdiction",
        "rule_state": "42 Pa.C.S. (courts of limited statutory jurisdiction)",
        "rule_federal": "Fed. R. Civ. P. 12(b)(1); Fed. R. Civ. P. 12(h)(3)",
        "remedy_state": "dismissed for lack of subject-matter jurisdiction",
        "remedy_federal": "dismissed — the court must dismiss whenever it determines it lacks jurisdiction",
        "frame": "the court lacks subject-matter jurisdiction, which cannot be waived and may be raised at any time, so the action must be",
        "subsection_logic": "Federal courts are courts of limited jurisdiction; under Rule 12(h)(3) the court MUST dismiss whenever subject-matter jurisdiction is absent, at any stage.",
    },
    "VOID_JUDGMENT_RULE_60B4": {
        "name": "Void Judgment",
        "rule_state": "void-judgment doctrine (a void judgment is attackable at any time)",
        "rule_federal": "Fed. R. Civ. P. 60(b)(4)",
        "remedy_state": "vacated as void",
        "remedy_federal": "vacated as void",
        "frame": "the judgment is void for want of jurisdiction or a due-process deprivation, and a void judgment must be",
        "subsection_logic": "A judgment is void under Rule 60(b)(4) where the court lacked subject-matter or personal jurisdiction or acted inconsistently with due process; relief from a void judgment is not discretionary and is not time-barred.",
    },
    "REMOVAL_DEFECTIVE": {
        "name": "Defective Removal",
        "rule_state": "n/a (federal removal procedure)",
        "rule_federal": "28 U.S.C. § 1441; 28 U.S.C. § 1446(b)(2)(A) (rule of unanimity); 28 U.S.C. § 1447(c)",
        "remedy_state": "remanded",
        "remedy_federal": "remanded for a defect in the removal procedure",
        "frame": "removal failed a statutory prerequisite (typically the rule of unanimity), so the case must be",
        "subsection_logic": "All properly joined and served defendants must join in or consent to removal (28 U.S.C. § 1446(b)(2)(A)); a defect in the removal procedure requires remand under § 1447(c).",
    },
    "PROCEDURAL_DUE_PROCESS_VIOLATION": {
        "name": "Procedural Due-Process Violation",
        "rule_state": "Pa. Const. art. I, §§ 1, 9, 11",
        "rule_federal": "U.S. Const. amend. XIV, § 1 (and amend. V)",
        "remedy_state": "vacated for want of due process",
        "remedy_federal": "vacated for want of due process",
        "frame": "the ruling deprived a party of life, liberty, or property without adequate notice and an opportunity to be heard, and must be",
        "subsection_logic": "Procedural due process requires notice and a meaningful opportunity to be heard before a deprivation of a protected interest; a deprivation entered without them is constitutionally infirm.",
    },
    "SUA_SPONTE_RULING": {
        "name": "Sua Sponte Ruling Without Notice",
        "rule_state": "Pa. Const. art. I, § 9 (notice and opportunity to be heard)",
        "rule_federal": "U.S. Const. amend. XIV, § 1 (due process)",
        "remedy_state": "vacated (entered without notice or opportunity to be heard)",
        "remedy_federal": "vacated (entered without notice or opportunity to be heard)",
        "frame": "the court granted substantive relief on an issue no party raised, without notice or briefing, so the ruling must be",
        "subsection_logic": "A court may not grant substantive relief sua sponte on an issue no party raised without notice and an opportunity to be heard; doing so violates due process.",
    },
    "PRIMA_FACIE_EVIDENCE_IGNORED": {
        "name": "Mandatory Judicial Notice Ignored",
        "rule_state": "Pa.R.E. 201(b), (c)",
        "rule_federal": "Fed. R. Evid. 201(b), (c)",
        "remedy_state": "the court must take judicial notice of the record fact",
        "remedy_federal": "the court must take judicial notice of the record fact",
        "frame": "the court ignored record facts subject to mandatory judicial notice (admissions, defaults, court records), which it was required to notice, so the ruling must be",
        "subsection_logic": "Under Rule 201(c)(2) a court MUST take judicial notice when a party requests it and supplies the necessary information; record facts capable of ready and accurate determination are properly noticed.",
    },
    "FRAUD_ON_THE_COURT": {
        "name": "Fraud on the Court",
        "rule_state": "fraud-on-the-court / void-judgment doctrine",
        "rule_federal": "Fed. R. Civ. P. 60(d)(3)",
        "remedy_state": "judgment set aside for fraud on the court",
        "remedy_federal": "judgment set aside for fraud on the court",
        "frame": "an officer of the court perpetrated a fraud that defiled the court itself, and the resulting judgment must be",
        "subsection_logic": "Rule 60(d)(3) reaches conduct by an officer of the court that corrupts the judicial process itself; the court retains inherent power to set aside a judgment so obtained, without time limit.",
    },
    "JUDICIAL_DISQUALIFICATION_DUTY": {
        "name": "Disqualification Duty",
        "rule_state": "Code of Judicial Conduct, Rule 2.11",
        "rule_federal": "28 U.S.C. § 455; 28 U.S.C. § 144",
        "remedy_state": "recusal / disqualification",
        "remedy_federal": "recusal / disqualification",
        "frame": "the judge's impartiality might reasonably be questioned (including a financial or fiduciary interest), triggering the duty to",
        "subsection_logic": "28 U.S.C. § 455(a) requires disqualification where the judge's impartiality might reasonably be questioned, and § 455(b) where the judge has a financial interest; the standard is objective and self-executing.",
    },
    "DEPRIVATION_OF_RIGHTS_COLOR_OF_LAW": {
        "name": "Deprivation of Rights Under Color of Law",
        "rule_state": "42 U.S.C. § 1983",
        "rule_federal": "42 U.S.C. § 1983 (18 U.S.C. § 242 criminal analog)",
        "remedy_state": "civil liability / relief under § 1983",
        "remedy_federal": "civil liability / relief under § 1983",
        "frame": "a person acting under color of state law deprived a party of a federally protected right, giving rise to liability, and the deprivation must be",
        "subsection_logic": "42 U.S.C. § 1983 creates a cause of action against any person who, under color of state law, deprives another of rights secured by the Constitution and laws of the United States.",
    },
    "OBSTRUCTION_OF_JUSTICE": {
        "name": "Obstruction of Justice",
        "rule_state": "18 Pa.C.S. § 5101 (obstructing administration of law)",
        "rule_federal": "18 U.S.C. § 1503; 18 U.S.C. § 1512",
        "remedy_state": "referral for obstruction",
        "remedy_federal": "referral for obstruction",
        "frame": "the conduct corruptly endeavored to influence, obstruct, or impede the due administration of justice, warranting",
        "subsection_logic": "18 U.S.C. § 1503 reaches corrupt endeavors to obstruct the due administration of justice; § 1512 reaches tampering with a witness, victim, or evidence.",
    },
    "IMMUNITY_WAIVER_TRIGGERED": {
        "name": "Judicial Immunity Does Not Bar",
        "rule_state": "judicial-immunity limits (non-judicial acts; clear absence of jurisdiction)",
        "rule_federal": "Stump v. Sparkman, 435 U.S. 349; Bradley v. Fisher, 80 U.S. 335; Pulliam v. Allen, 466 U.S. 522",
        "remedy_state": "immunity does not bar the claim or relief",
        "remedy_federal": "immunity does not bar the claim or relief",
        "frame": "the act falls outside judicial immunity (a non-judicial act, an act in the clear absence of all jurisdiction, or a request for prospective relief), so immunity does not",
        "subsection_logic": "Judicial immunity protects only judicial acts taken with jurisdiction; it does not reach non-judicial acts or acts in the clear absence of all jurisdiction, and does not bar prospective injunctive or declaratory relief.",
    },
    "SANCTIONS_TRIGGERED": {
        "name": "Sanctionable Conduct",
        "rule_state": "Pa.R.C.P. 1023.1",
        "rule_federal": "Fed. R. Civ. P. 11(b), (c); 28 U.S.C. § 1927",
        "remedy_state": "sanctions",
        "remedy_federal": "sanctions",
        "frame": "the conduct meets the standard for sanctions (a frivolous or improper-purpose filing), warranting",
        "subsection_logic": "Rule 11(b) certifies a filing is not for an improper purpose and is warranted by law; a violation supports sanctions under Rule 11(c), and 28 U.S.C. § 1927 reaches vexatious multiplication of proceedings.",
    },
    "STARE_DECISIS_VIOLATION": {
        "name": "Departure From Controlling Precedent",
        "rule_state": "stare decisis (binding precedent of a superior court in the same line)",
        "rule_federal": "stare decisis (binding precedent of a superior court in the same line)",
        "remedy_state": "reversal for departing from controlling precedent",
        "remedy_federal": "reversal for departing from controlling precedent",
        "frame": "the ruling is inconsistent with controlling precedent that binds this court, and must be",
        "subsection_logic": "A lower court is bound by decisions of a court superior in the same appellate hierarchy; a result inconsistent with controlling precedent is reversible.",
    },
    "SOVEREIGN_CITIZEN_LABEL": {
        "name": "Ideological Label — Rebutted, Not Applied",
        "rule_state": "Neitzke v. Williams, 490 U.S. 319 (arguable-basis standard)",
        "rule_federal": "Neitzke v. Williams, 490 U.S. 319 (arguable-basis standard)",
        "remedy_state": "the label is rebutted; the claim is adjudicated on its corpus-grounded merits",
        "remedy_federal": "the label is rebutted; the claim is adjudicated on its corpus-grounded merits",
        "frame": "the court affixed an ideological label in lieu of merits analysis; a claim with an arguable basis in law is not frivolous, and the label must be",
        "subsection_logic": "A claim is frivolous only where it lacks an arguable basis in law or fact (Neitzke); an ideological label is not a substitute for that analysis and does not defeat a corpus-grounded claim.",
    },
    "NON_BINDING_AS_CONTROLLING": {
        "name":           "Non-Binding Authority Presented as Controlling",
        "rule_state":     "Pa.R.C.P. 1028(a)(2)",
        "rule_federal":   "Fed. R. Civ. P. 12(f)",
        "remedy_state":   "stricken",
        "remedy_federal": "stricken",
        "frame":  ("presenting non-binding authority as controlling is "
                   "procedurally defective and the affected portions must be"),
        "subsection_logic": (
            "Binding authority is limited to decisions of a court superior in the "
            "same appellate hierarchy; persuasive authority does not control. "
            "Citing non-binding authority as if controlling is a misstatement of "
            "the stare decisis hierarchy."
        ),
    },
    "TRIAL_COURT_AS_APPELLATE_AUTHORITY": {
        "name":           "Trial Court Authority Misused as Binding Precedent",
        "rule_state":     "Pa.R.C.P. 1028(a)(2)",
        "rule_federal":   "Fed. R. Civ. P. 12(f)",
        "remedy_state":   "stricken",
        "remedy_federal": "stricken",
        "frame":  ("trial court decisions are not binding precedent on this "
                   "Court; reliance on such decisions as controlling is "
                   "procedurally defective and the affected portions must be"),
        "subsection_logic": (
            "Under the hierarchy of stare decisis, a trial-court decision is not "
            "binding precedent; only decisions of a court superior in the same "
            "appellate line bind. Presenting non-binding authority as controlling "
            "misstates the governing law."
        ),
    },
    "BIAS_EMOTIONAL_LANGUAGE": {
        "name":           "Improper Argumentative / Prejudicial Language",
        "rule_state":     "Pa.R.C.P. 1023.1",
        "rule_federal":   "Fed. R. Civ. P. 11(b)",
        "remedy_state":   "stricken or sanctioned",
        "remedy_federal": "stricken or sanctioned",
        "frame":  ("filings must be well-grounded in fact and law and not "
                   "interposed for improper purpose; emotional or "
                   "characterological attacks on the litigant are subject to being"),
        # BIAS_STACK_V1
        "subsection_logic": (
            "Filings must be well-grounded in fact and law and may not be "
            "presented for any improper purpose such as to harass, cause "
            "delay, or needlessly increase the cost of litigation. "
            "Characterological attacks on a litigant, as distinguished from "
            "challenges to the legal sufficiency of the litigant's positions, "
            "are improper and subject to being stricken or sanctioned."
        ),
        "authority": [
            "In re Prudential Ins. Co. America Sales Practice Litig., 278 F.3d 175, 188 (3d Cir. 2002)",
            "Chambers v. NASCO, Inc., 501 U.S. 32, 45-46 (1991)",
        ],
    },
    "BIAS_PRO_SE_GATEKEEPING": {
        "name":           "Improper Pro Se Gatekeeping",
        "rule_state":     "Pa.R.C.P. 1023.1",
        "rule_federal":   "Fed. R. Civ. P. 11(b)",
        "remedy_state":   "stricken or sanctioned",
        "remedy_federal": "stricken or sanctioned",
        "frame":  ("pro se litigants are entitled to liberal construction of "
                   "their filings, and characterological attacks on a pro se "
                   "litigant are subject to being"),
        # BIAS_STACK_V1
        "subsection_logic": (
            "Pro se filings are held to less stringent standards than those "
            "drafted by lawyers and must be construed liberally. Imposing "
            "technical defects on a pro se litigant's submissions to defeat "
            "them on the merits is reversible error."
        ),
        "authority": [
            "Haines v. Kerner, 404 U.S. 519, 520 (1972)",
            "Erickson v. Pardus, 551 U.S. 89, 94 (2007)",
            "Estelle v. Gamble, 429 U.S. 97, 106 (1976)",
        ],
    },
    "BIAS_PUNITIVE_APPEAL": {
        "name":           "Punitive / Improper Appeals to the Court",
        "rule_state":     "Pa.R.C.P. 1023.1",
        "rule_federal":   "Fed. R. Civ. P. 11(b)",
        "remedy_state":   "stricken or sanctioned",
        "remedy_federal": "stricken or sanctioned",
        "frame":  ("requests for punitive or extraordinary relief untethered "
                   "to the merits are improper and subject to being"),
        # BIAS_STACK_V1
        "subsection_logic": (
            "Requests for extraordinary or punitive measures untethered to "
            "the merits of the dispute are improper. Sanctions and "
            "procedural penalties are reserved for conduct that is "
            "objectively unreasonable, brought in bad faith, or willfully "
            "obstructive -- not for the substance of an opponent's legal "
            "positions."
        ),
        "authority": [
            "Chambers v. NASCO, Inc., 501 U.S. 32, 45-46 (1991)",
            "In re Prudential Ins. Co. America Sales Practice Litig., 278 F.3d 175, 188 (3d Cir. 2002)",
        ],
    },
    "VERIFICATION_MISSING": {
        "name":           "Verification Requirement",
        "rule_state":     "Pa.R.C.P. 1024",
        "rule_federal":   "Fed. R. Civ. P. 11(a)",
        "remedy_state":   "stricken under Pa.R.C.P. 1028(a)(2)",
        "remedy_federal": "stricken",
        "frame":  ("verification is required at the threshold of judicial "
                   "review; unverified pleadings must be"),
    },
    "SERVICE_DEFECTIVE": {
        "name":           "Defective Service",
        "rule_state":     "Pa.R.C.P. 403, 424, 440",
        "rule_federal":   "Fed. R. Civ. P. 4(h)(1), 4(e)(1), 4(l)",
        "remedy_state":   "stricken or disregarded",
        "remedy_federal": "stricken or disregarded",
        "frame":  ("defective service renders the affected filing without "
                   "legal effect, and the filing must be"),
        # === REASONING_STACK_V1 (prototype) ===
        # Used when the DEFECT is an opponent/court ASSERTION of non-service that
        # the record contradicts. Maxim and equity are deliberately separate.
        "subsection_logic": (
            "For a corporation, LLC, partnership, or association, service is "
            "governed by Fed. R. Civ. P. 4(h)(1)(A) (incorporating Rule 4(e)(1) "
            "state-law methods, which in Pennsylvania permit service by certified "
            "mail under Pa.R.C.P. 403 and 424) or Rule 4(h)(1)(B) (delivery to an "
            "officer or managing/general agent). For an individual, Rule 4(e)(1) "
            "or 4(e)(2). Proof of service is governed by Rule 4(l)(1); and under "
            "Rule 4(l)(3), failure to PROVE service does not affect the VALIDITY "
            "of service actually made."
        ),
        "authority": [
            "Rosenthal v. Walker, 111 U.S. 185, 193 (1884)",
            "Rios v. Nicholson, 490 F.3d 928, 930-31 (Fed. Cir. 2007)",
        ],
        # MAXIMS_EQUITY_KEYS_V1: reference the DB by key (one source of truth).
        "maxim": ["presumption_of_regularity"],
        "equity": [
            "removal_service_burden_1448",
            "equity_aids_the_vigilant",
            "no_profit_from_own_wrong",
        ],
        "burden_shift": (
            "Proof of a properly addressed mailing that was not returned, "
            "corroborated by subsequent mailings received at the same address, "
            "raises a rebuttable presumption of receipt and shifts the burden to "
            "the party denying receipt to come forward with evidence of non-receipt."
        ),
        # === END REASONING_STACK_V1 ===
    },
    "GARNISHEE_NO_ANSWER": {
        "name":           "Garnishee Failure to Answer",
        "rule_state":     "Pa.R.C.P. 3145",
        "rule_federal":   "Fed. R. Civ. P. 69(a)(2)",
        "remedy_state":   "subject to default under Pa.R.C.P. 3146",
        "remedy_federal": "subject to default judgment",
        "frame":  ("a garnishee's failure to answer interrogatories is"),
    },
    "DUE_PROCESS": {
        "name":           "Procedural Due Process Violation",
        "rule_state":     "U.S. Const. amend. XIV § 1",
        "rule_federal":   "U.S. Const. amend. XIV § 1",
        "remedy_state":   "void",
        "remedy_federal": "void",
        "frame":  ("the constitutional guarantee of notice and an "
                   "opportunity to be heard is foundational; orders entered "
                   "in violation of that guarantee are"),
    },
    # === NEW_HOOK_CATEGORIES_V1 ===
    "LEAVE_TO_AMEND_CONCLUSORY": {
        "name":           "Failure to Address Leave to Amend",
        "rule_state":     "Pa.R.C.P. 1033",
        "rule_federal":   "Fed. R. Civ. P. 15(a)(2)",
        "remedy_state":   "addressed before any dismissal with prejudice may issue",
        "remedy_federal": "addressed before any dismissal with prejudice may issue",
        "frame":  ("leave to amend must be freely given when justice so "
                   "requires, and a conclusory denial without analysis is "
                   "reversible error; the request must be"),
        "authority": [
            "Foman v. Davis, 371 U.S. 178, 182 (1962)",
            "Fed. R. Civ. P. 15(a)(2)",
        ],
        "subsection_logic": (
            "Leave to amend shall be freely given when justice so requires; denial "
            "is justified only for enumerated reasons (undue delay, bad faith, "
            "repeated failure to cure, undue prejudice, or futility), and a "
            "conclusory denial without that analysis is an abuse of discretion."
        ),
        "equity": ["no_wrong_without_remedy"],
    },
    "CONCLUSORY_DISMISSAL": {
        "name":           "Conclusory Dismissal Without Required Analysis",
        "rule_state":     "Pa.R.C.P. 1028(a)(4)",
        "rule_federal":   "Fed. R. Civ. P. 12(b)(6)",
        "remedy_state":   "reconsidered with the analysis required by binding precedent",
        "remedy_federal": "reconsidered under the Twombly / Iqbal plausibility standard with element-by-element analysis",
        "frame":  ("dismissal of a claim requires a reasoned application of "
                   "the controlling pleading standard to the well-pleaded "
                   "factual allegations; a conclusory ruling lacking such "
                   "analysis must be"),
        "authority": [
            "Bell Atlantic Corp. v. Twombly, 550 U.S. 544, 555-56 (2007)",
            "Ashcroft v. Iqbal, 556 U.S. 662, 678-79 (2009)",
        ],
        "subsection_logic": (
            "Under the Twombly/Iqbal two-step, conclusory allegations are not "
            "entitled to the assumption of truth, but all well-pleaded factual "
            "allegations must be accepted as true and viewed in the light most "
            "favorable to the non-movant; dismissal requires an element-by-element "
            "plausibility analysis, not a conclusory ruling."
        ),
        "equity": ["no_wrong_without_remedy"],
    },
    # === END NEW_HOOK_CATEGORIES_V1 ===
    # === PATH_B_PROCEDURAL_HOOKS_V1 ===
    # Hooks for block14_pinpointer-detected procedural violations.
    "TWOMBLY_IQBAL_MISAPPLICATION": {
        "name":           "Twombly / Iqbal Misapplied",
        "rule_state":     "Pa.R.C.P. 1028(a)(4)",
        "rule_federal":   "Fed. R. Civ. P. 12(b)(6)",
        "remedy_state":   "reconsidered with all well-pleaded facts accepted as true",
        "remedy_federal": "reconsidered under the correct plausibility standard with all well-pleaded facts accepted as true",
        "frame":  ("the plausibility standard requires accepting all "
                   "well-pleaded factual allegations as true; treating "
                   "factual allegations as legal conclusions inverts the "
                   "rule and the resulting decision must be"),
        "authority": [
            "Bell Atlantic Corp. v. Twombly, 550 U.S. 544, 555-56 (2007)",
            "Ashcroft v. Iqbal, 556 U.S. 662, 678-79 (2009)",
        ],
        "subsection_logic": (
            "Treating well-pleaded factual allegations as legal conclusions "
            "inverts the Twombly/Iqbal standard; the court must accept all "
            "well-pleaded facts as true and draw reasonable inferences for the "
            "non-movant before testing plausibility."
        ),
    },
    "NOTICE_INADEQUATE": {
        "name":           "Inadequate Notice (Procedural Due Process)",
        "rule_state":     "U.S. Const. amend. XIV § 1; Pa.R.C.P. 440",
        "rule_federal":   "U.S. Const. amend. XIV § 1; Fed. R. Civ. P. 5",
        "remedy_state":   "void under Pa.R.C.P. 237.3 and Fed. R. Civ. P. 60(b)(4)",
        "remedy_federal": "void under Fed. R. Civ. P. 60(b)(4)",
        "frame":  ("notice and an opportunity to be heard are constitutional "
                   "minimums; orders entered without adequate notice are"),
        "authority": [
            "Mullane v. Central Hanover Bank & Trust Co., 339 U.S. 306, 314 (1950)",
        ],
        "subsection_logic": (
            "Due process requires notice reasonably calculated, under all the "
            "circumstances, to apprise interested parties of the pendency of the "
            "action and afford an opportunity to present objections; notice that "
            "is a mere gesture is not due process."
        ),
        "maxim": ["presumption_of_regularity"],
        "equity": ["no_wrong_without_remedy"],
    },
    "BIAS_JUDICIAL": {
        "name":           "Indicia of Judicial Bias",
        "rule_state":     "Code of Judicial Conduct, Canon 2",
        "rule_federal":   "28 U.S.C. § 455",
        "remedy_state":   "subject to recusal proceedings",
        "remedy_federal": "subject to recusal under 28 U.S.C. § 455",
        "frame":  ("a judicial officer must disqualify in any proceeding "
                   "in which impartiality might reasonably be questioned; "
                   "the conduct identified is"),
        # BIAS_STACK_V1
        "subsection_logic": (
            "A judicial officer must disqualify in any proceeding in which "
            "the officer's impartiality might reasonably be questioned. The "
            "test is objective: whether a reasonable, well-informed observer "
            "would harbor significant doubt about the judge's impartiality. "
            "Personal bias, extrajudicial knowledge of disputed evidentiary "
            "facts, or deep-seated favoritism or antagonism revealed through "
            "judicial conduct each compel recusal."
        ),
        "authority": [
            "28 U.S.C. § 455(a)",
            "Liteky v. United States, 510 U.S. 540, 555-56 (1994)",
            "Caperton v. A.T. Massey Coal Co., 556 U.S. 868, 884 (2009)",
            "Code of Judicial Conduct, Canon 2 (state-court parallel)",
        ],
        "equity": ["no_wrong_without_remedy"],
    },
    "VERIFICATION_MISSING_PROC": {
        # Note: VERIFICATION_MISSING already mapped above. This is a
        # block14_pinpointer alias kept for compatibility.
        "name":           "Verification Missing (Pinpointed)",
        "rule_state":     "Pa.R.C.P. 1024",
        "rule_federal":   "Fed. R. Civ. P. 11(a)",
        "remedy_state":   "stricken under Pa.R.C.P. 1028(a)(2)",
        "remedy_federal": "stricken",
        "frame":  ("verification is required at the threshold of judicial "
                   "review; unverified pleadings must be"),
    },
    "PRO_SE_LIBERAL_CONSTRUCTION": {
        "name":           "Failure to Apply Liberal Construction to Pro Se Filings",
        "rule_state":     "Erickson v. Pardus, 551 U.S. 89 (2007)",
        "rule_federal":   "Erickson v. Pardus, 551 U.S. 89 (2007); Haines v. Kerner, 404 U.S. 519 (1972)",
        "remedy_state":   "reconsidered with the liberal-construction standard applied",
        "remedy_federal": "reconsidered with the liberal-construction standard applied",
        "frame":  ("filings by pro se litigants are held to less stringent "
                   "standards than formal pleadings drafted by lawyers; "
                   "reliance on technical defects against a pro se filing must be"),
        # BIAS_STACK_V1
        "subsection_logic": (
            "Pro se filings are held to less stringent standards than formal "
            "pleadings drafted by lawyers. A court must construe a pro se "
            "litigant's submissions liberally and may not penalize the "
            "litigant for technical imperfections that would not bear on "
            "the substance of a counseled filing."
        ),
        "authority": [
            "Haines v. Kerner, 404 U.S. 519, 520 (1972)",
            "Erickson v. Pardus, 551 U.S. 89, 94 (2007)",
            "Estelle v. Gamble, 429 U.S. 97, 106 (1976)",
        ],
    },
    "IMPROPER_STANDARD_OF_REVIEW": {
        "name":           "Improper Standard of Review",
        "rule_state":     "Pa.R.C.P. 1028(a)(4)",
        "rule_federal":   "Fed. R. Civ. P. 12(b)(6); Twombly / Iqbal",
        "remedy_state":   "reconsidered under the correct standard",
        "remedy_federal": "reconsidered under the correct standard",
        "frame":  ("application of the wrong legal standard at the "
                   "preliminary objection / motion to dismiss stage is "
                   "reversible error; the affected ruling must be"),
    },
    "FILING_DEADLINE_VIOLATION": {
        "name":           "Filing Deadline Violation",
        "rule_state":     "Pa.R.C.P. 1026; 1028(a)",
        "rule_federal":   "Fed. R. Civ. P. 6; 12(a)",
        "remedy_state":   "stricken or disregarded as untimely",
        "remedy_federal": "stricken or disregarded as untimely",
        "frame":  ("filings made outside the time limits set by the "
                   "applicable rule are untimely; the affected filing must be"),
    },
    "PAGE_LIMIT_EXCEEDED": {
        "name":           "Page Limit Exceeded",
        "rule_state":     "local rules of court",
        "rule_federal":   "Local Rules of Civil Procedure (E.D. Pa. L.R. 7.1)",
        "remedy_state":   "stricken or returned for compliance",
        "remedy_federal": "stricken or returned for compliance",
        "frame":  ("filings exceeding the page or word limits set by the "
                   "applicable local rule are non-compliant; the affected filing must be"),
    },
    # === END PATH_B_PROCEDURAL_HOOKS_V1 ===
}


def _severity_to_weight(severity: str) -> int:
    """Convert text severity to numeric weight for sorting."""
    return {"FATAL": 3, "SERIOUS": 2, "STANDARD": 1, "MINOR": 0}.get(
        (severity or "").upper(), 1
    )




# ===PHASE_3F_STEP6C_CHUNKED_V1=== Agent 6 LLM enhancement
import os as _os_3f
import json as _json_3f
from datetime import datetime as _dt_3f


# BEEZLE_FN_UNUSED 2026-06-13: _has_bias_indicator (no static refs in pipeline/)
def _has_bias_indicator(data) -> bool:
    """Recusal requires explicit bias evidence."""
    try:
        BIAS_KEYWORDS = (
            "bias", "prejudice", "ex parte", "personal interest",
            "financial interest", "appearance of impropriety",
            "extrajudicial", "predetermined",
        )
        by_issue = getattr(data, "by_issue", None) or {}
        for grp in by_issue.values():
            for v in getattr(grp, "violations", []) or []:
                blob = (
                    str(getattr(v, "issue", "")) + " "
                    + str(getattr(v, "violation", "")) + " "
                    + str(getattr(v, "explanation", "")) + " "
                    + str(getattr(v, "category", ""))
                ).lower()
                if any(kw in blob for kw in BIAS_KEYWORDS):
                    return True
        return False
    except Exception:
        return False


# Map procedural rule source → standardized IF/THEN result text



import re as _re_labels

_COURT_LABEL_REPLACEMENTS = [
    # Order matters: longer / more specific patterns first
    (_re_labels.compile(r'\bPA_CP_PHILA\b'), 'the Philadelphia Court of Common Pleas'),
    (_re_labels.compile(r'\bPA_CP_OTHER\b'), 'a Pennsylvania trial court'),
    (_re_labels.compile(r'\bPA_SUPER\b'),    'the Pennsylvania Superior Court'),
    (_re_labels.compile(r'\bPA_SUP\b'),      'the Pennsylvania Supreme Court'),
    (_re_labels.compile(r'\bDPA_OTHER\b'),   'a different federal district court in Pennsylvania'),
    (_re_labels.compile(r'\bEDPA\b'),        'the U.S. District Court for the Eastern District of Pennsylvania'),
    (_re_labels.compile(r'\bMDPA\b'),        'the U.S. District Court for the Middle District of Pennsylvania'),
    (_re_labels.compile(r'\bWDPA\b'),        'the U.S. District Court for the Western District of Pennsylvania'),
    (_re_labels.compile(r'\bCA3\b'),         'the United States Court of Appeals for the Third Circuit'),
    (_re_labels.compile(r'\bCA_OTHER\b'),    'another federal circuit court'),
    (_re_labels.compile(r'\bDIST_OTHER\b'),  'a federal district court outside this jurisdiction'),
    (_re_labels.compile(r'\bNJ_SUP\b'),      'the New Jersey Supreme Court'),
    (_re_labels.compile(r'\bNY_SUP\b'),      'the New York Court of Appeals'),
    (_re_labels.compile(r'\bUNKNOWN\b'),     'an unidentified court'),
]


def _humanize_court_labels(text: str) -> str:
    """Replace raw audit court labels (DIST_OTHER, CA_OTHER, etc.) with
    readable phrasing for inclusion in motion prose."""
    if not text:
        return text
    for pattern, replacement in _COURT_LABEL_REPLACEMENTS:
        text = pattern.sub(replacement, text)
    return text
# === END COURT_LABEL_HUMANIZER_V1 ===



# === PATH_B_NEEDS_HOOK_LOGGER_V1 ===


def _default_hook_for_unknown_category(category: str) -> dict:
    """Generic legal hook for scorecard categories we haven't mapped yet.
    Allows new audit categories to flow into motions without code changes."""
    pretty = category.replace("_", " ").title()
    return {
        "name":           pretty,
        "rule_state":     "Pa.R.C.P. 1028(a)(2)",
        "rule_federal":   "Fed. R. Civ. P. 12(f)",
        "remedy_state":   "stricken",
        "remedy_federal": "stricken",
        "frame":  ("the conduct identified is procedurally defective and "
                   "the affected portions must be"),
    }


def build_proposed_findings(case_id: str, is_state: bool, max_items: int = 24) -> str:
    """CRACK-GAP G2 (2026-07-08): every sampled proposed order was one bare GRANTED/DENIED line — a
    court can rule with zero reviewable findings, and silence on any defect is unappealable. This
    renders a [PROPOSED] FINDINGS OF FACT AND CONCLUSIONS OF LAW block derived 1:1 from the audit
    JSON's OWN rows (scorecard + procedural violations) — data rendering, no invented allegations;
    each finding = a record fact the audit already established, each conclusion = that row's hook rule
    via the jurisdiction-safe resolver. AGNOSTIC: works for any case/audit, nothing matter-specific.
    Returned text is a STAGED DRAFT for human review before attaching (new filing prose is never
    auto-inserted into a filing body)."""
    import json as _json
    from pathlib import Path as _P
    safe = (case_id or "").replace(":", "_").replace("/", "_")
    ap = _P(f"<home>/output/audit/data_{safe}.json")
    if not ap.exists():
        return ""
    try:
        data = _json.loads(ap.read_text(encoding="utf-8"))
    except Exception:
        return ""
    facts = _gather_scorecard_facts(data, canonical_id=safe)
    if not facts:
        return ""
    # one finding per category (strongest fact each), so every defect gets its own reviewable ruling
    by_cat = {}
    for f in facts:
        cat = f.get("category", "")
        if cat and cat not in by_cat:
            by_cat[cat] = f
    lines = ["## [PROPOSED] FINDINGS OF FACT AND CONCLUSIONS OF LAW",
             "*(STAGED DRAFT — derived 1:1 from the audit record; review before attaching. Each numbered "
             "item demands its own ruling: an unaddressed item is preserved for review.)*", ""]
    n = 0
    for cat, f in list(by_cat.items())[:max_items]:
        hook = _SCORECARD_HOOK_MAP.get(cat)
        if hook is None:
            _log_unknown_category(cat, f)
            hook = _default_hook_for_unknown_category(cat)
        rule = _rule_for_jurisdiction(hook, is_state)
        remedy = hook["remedy_state"] if is_state else hook["remedy_federal"]
        fact_text = _humanize_court_labels((f.get("text") or "").strip()).rstrip(".")
        if not fact_text:
            continue
        n += 1
        lines.append(f"{n}. FINDING: {fact_text}.")
        lines.append(f"   CONCLUSION: under {rule}, the affected material shall be {remedy}.")
    if n == 0:
        return ""
    lines.append("")
    lines.append(f"{n + 1}. Each finding above rests on the record; each conclusion applies the cited "
                 "rule to that finding. The Court is requested to rule on each item individually.")
    return "\n".join(lines)


def _rule_for_jurisdiction(hook: dict, is_state: bool) -> str:
    """Jurisdiction-safe rule resolution (GO step 8, 2026-07-08). The old inline fallback
    `hook.get("rule")` ignored jurisdiction — a hook carrying a generic federal rule leaked
    `Fed. R. Civ. P. 11(b)` into a STATE IFP filing — and the final literal fallback was
    state-only (the reverse leak for federal cases). Rules must branch on is_state; a
    generic rule naming the WRONG jurisdiction's rule system falls back to that
    jurisdiction's default (same defaults _default_hook_for_unknown_category declares)."""
    rule = hook.get("rule_state" if is_state else "rule_federal") or hook.get("rule") or ""
    r = rule.strip()
    if is_state and r.startswith("Fed. R."):
        r = ""
    if not is_state and (r.startswith("Pa.R.") or r.startswith("Pa. R.")):
        r = ""
    return r or ("Pa.R.C.P. 1028(a)(2)" if is_state else "Fed. R. Civ. P. 12(f)")


# BEEZLE_FN_UNUSED 2026-06-13: _remedy_for (no static refs in pipeline/)
def _remedy_for(source: str) -> str:
    """Map a rule citation to its mandatory remedy text."""
    if not source:
        return "the filing must be stricken or appropriate relief entered."
    s = source.strip()
    # Try exact match first, then prefix matches
    if s in _PROCEDURAL_REMEDIES:
        return _PROCEDURAL_REMEDIES[s]
    for key, remedy in _PROCEDURAL_REMEDIES.items():
        if s.startswith(key) or key in s:
            return remedy
    return "the filing must be stricken or appropriate relief entered."


# REVIVED (GO step 8, 2026-07-08): was dead two ways — no callers AND a _Path/_P NameError swallowed
# by the except, so even a call would have been a silent no-op. Now called from both hook consumers.
def _log_unknown_category(category: str, _scorecard_hint=None) -> None:
    """Log unknown scorecard categories to needs_hook.log so they can be
    added to _SCORECARD_HOOK_MAP / YAML in a future patch."""
    try:
        from pathlib import Path as _P
        from datetime import datetime as _dt
        log_path = _P(str(_eroot() / "output/audit/needs_hook.log"))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # Don't double-log within one run
        existing = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        if f"category={category}" in existing.split("\n")[-50:].__str__():
            return  # already logged recently
        with log_path.open("a", encoding="utf-8") as f:
            ts = _dt.now().isoformat(timespec="seconds")
            hint = ""
            if _scorecard_hint and isinstance(_scorecard_hint, dict):
                hint_text = (_scorecard_hint.get("text") or "")[:80]
                hint = f"  hint: {hint_text}"
            f.write(f"{ts}  category={category}{hint}\n")
    except Exception:
        pass
# === END PATH_B_NEEDS_HOOK_LOGGER_V1 ===


def _gather_scorecard_facts(data, canonical_id: str = "") -> list:
    """Pull violations from BOTH stare-decisis AND procedural sources.

    Reads output/audit/data_<case>.json and merges:
      1. scorecard.violations[]      — stare-decisis findings
                                       (keyed by 'category', text in 'description')
      2. procedural_violations[]     — block14_pinpointer findings
                                       (keyed by 'violation_type', text in 'quote'+'explanation')

    Both are normalized into the same fact-record shape so the strict
    Section II builder treats them uniformly:
      {text, category, severity, source_file, evidence, weight, count}
    """
    # === PATH_B_DUAL_SOURCE_GATHER_V1 ===
    facts = []

    if not canonical_id:
        canonical_id = (
            getattr(data, "canonical_id", "")
            or getattr(data, "case_id", "")
            or ""
        )

    if not canonical_id:
        return facts

    slug = canonical_id.replace(":", "_")
    audit_path = _audit_dir() / f"data_{slug}.json"

    if not audit_path.exists():
        return facts

    # === VIOLATION_ATTRIBUTION_GATE_V1 ===
    # Tag each violation with author (plaintiff / judge / opposing) + an `attack`
    # flag so the drafter NEVER marshals the litigant's OWN filings, and the rest
    # are attributed to the correct actor. Falls back to the raw audit on failure.
    audit = None
    try:
        from pipeline.violation_attribution import attribute_violations
        audit, _ = attribute_violations(canonical_id)
    except Exception:
        audit = None
    if not audit:
        try:
            import json as _ej
            with open(audit_path, "r", encoding="utf-8") as f:
                audit = _ej.load(f)
        except Exception:
            return facts
    # === END VIOLATION_ATTRIBUTION_GATE_V1 ===

    # ---- SOURCE 1: stare-decisis violations (existing data) ----
    sc = audit.get("scorecard") or {}
    if isinstance(sc, dict):
        for v in (sc.get("violations") or []):
            if not isinstance(v, dict):
                continue
            if v.get("attack") is False:      # never attack your own filings / evidence
                continue
            text = str(v.get("description") or v.get("evidence") or "").strip()
            text = _scrub_cell_text(text)  # CELL_CLEAN_V1: strip filename/OCR tails
            if not text:
                continue
            facts.append({
                "text": text,
                "category": str(v.get("category", "")),
                "severity": str(v.get("severity", "")),
                "source_file": str(v.get("source_file", "")),
                "evidence": str(v.get("evidence", "")),
                "weight": v.get("weight", 0),
                "count": 1,
                "attributed_actor": v.get("attributed_actor", ""),
                "fiduciary": v.get("fiduciary", False),
                "fiduciary_breach": v.get("fiduciary_breach", []),
                "_source": "stare_decisis",
            })

    # ---- SOURCE 2: procedural violations (NEW from block14_pinpointer) ----
    for v in (audit.get("procedural_violations") or []):
        if not isinstance(v, dict):
            continue
        if v.get("attack") is False:      # never attack your own filings / evidence
            continue
        # Build readable fact text from quote + explanation
        quote = str(v.get("quote", "")).strip()
        explanation = str(v.get("explanation", "")).strip()
        rule_citation = str(v.get("rule_citation", "")).strip()
        source_file = str(v.get("file", v.get("source_doc", ""))).strip()

        # CELL_CLEAN_V1: never paste raw OCR quote into the cell. The cell is
        # a filed exhibit and must describe the defect in clean prose. The ECF
        # cite is appended by _build_strict_section_ii via _resolve_source_to_ecf.
        text = _scrub_cell_text(explanation)

        if not text:
            continue

        # Map ViolationPin's violation_type to our category slot
        # (so the same Section II builder code works for both sources)
        facts.append({
            "text": text,
            "category": str(v.get("violation_type", "")),
            "severity": str(v.get("severity", "STANDARD")),
            "source_file": source_file,
            "evidence": rule_citation,
            "weight": _severity_to_weight(v.get("severity", "STANDARD")),
            "count": v.get("count", 1),
            "attributed_actor": v.get("attributed_actor", ""),
            "fiduciary": v.get("fiduciary", False),
            "fiduciary_breach": v.get("fiduciary_breach", []),
            "_source": "procedural",
        })

    return facts


def _build_strict_section_ii(scorecard_facts: list, is_state: bool) -> list:
    """Section II as a compact defects table (markdown -> docx grid)."""
    if not scorecard_facts:
        return ["Plaintiff's audit of the record discloses procedural defects as enumerated below."]

    by_category = {}
    for f in scorecard_facts:
        cat = f.get("category", "")
        if not cat:
            continue
        by_category.setdefault(cat, []).append(f)
    if not by_category:
        return ["Plaintiff's audit of the record discloses procedural defects as enumerated below."]

    def cat_weight(cat):
        return -sum(f.get("weight", 0) for f in by_category[cat])
    sorted_categories = sorted(by_category.keys(), key=cat_weight)

    def _clip(s, n):
        s = (s or "").strip().replace("|", "/").replace("\n", " ")
        return s  # TRUNC_FIX_V1: render full cell text in a filed exhibit (no ellipsis)

    lines = [
        "Plaintiff identifies the following defects in opposing filings, each "
        "established by the record and each independently warranting the relief "
        "requested:",
        "",
        "| # | Defect | Controlling Rule | Count | Record Basis | Consequence |",
        "|---|---|---|---|---|---|",
    ]
    sev_rank = {"FATAL": 3, "SERIOUS": 2, "STANDARD": 1}
    counter = 1
    for cat in sorted_categories:
        facts_in_cat = by_category[cat]
        if not facts_in_cat:
            continue
        hook = _SCORECARD_HOOK_MAP.get(cat)
        if hook is None:
            _log_unknown_category(cat, facts_in_cat[0] if facts_in_cat else None)
            hook = _default_hook_for_unknown_category(cat)
        name = hook["name"]
        rule = _rule_for_jurisdiction(hook, is_state)
        remedy = hook["remedy_state"] if is_state else hook["remedy_federal"]
        facts_in_cat.sort(key=lambda f: (-sev_rank.get((f.get("severity") or "").upper(), 0), -(f.get("weight") or 0)))
        # FIDUCIARY_RENDER: surface the breach of fiduciary duty in the Consequence
        # cell (DB-backed: duty + authority come from the fiduciary engine duty maps).
        _fb = next((f.get("fiduciary_breach") for f in facts_in_cat if f.get("fiduciary_breach")), None)
        if _fb:
            _b = _fb[0]
            _actor = (facts_in_cat[0].get("attributed_actor") or "the responsible fiduciary").split(" (")[0]
            remedy = (f"{remedy}  This conduct breaches {_actor}'s fiduciary duty — "
                      f"{_b.get('duty','')} ({_b.get('source','')}).")
        fact_text = _humanize_court_labels((facts_in_cat[0].get("text") or "").strip())
        # SCORECARD_ECF_V1: append docket ECF cite if the source resolves
        _ecf = _resolve_source_to_ecf(facts_in_cat[0].get("source_file", ""))
        if _ecf and _ecf not in fact_text:
            fact_text = f"{fact_text} ({_ecf})"
        n = len(facts_in_cat)
        lines.append(
            f"| {counter} | {_clip(name, 40)} | {_clip(rule, 28)} | {n} | "
            f"{_clip(fact_text, 90)} | {_clip(remedy, 40)} |"
        )
        counter += 1
    lines.append("")
    return lines



def _build_strict_section_iii(scorecard_facts: list, is_state: bool) -> list:
    """Section III as a compact IF / AND / THEN table (1+1=2 deterministic)."""
    if not scorecard_facts:
        return ["The application of the controlling rules to the defects identified is "
                "deterministic. Each violation independently triggers the requested relief."]

    by_category = {}
    for f in scorecard_facts:
        cat = f.get("category", "")
        if cat and cat not in by_category:
            by_category[cat] = f
    if not by_category:
        return []

    def _clip(s, n):
        s = (s or "").strip().replace("|", "/").replace("\n", " ")
        return (s[:n - 1] + "\u2026") if len(s) > n else s

    lines = [
        "The application of the controlling rules to the defects established by the "
        "record is deterministic. Each defect independently triggers the stated "
        "consequence:",
        "",
        "| IF (established by the record) | AND (governing rule) | THEN (consequence) |",
        "|---|---|---|",
    ]
    for cat, f in by_category.items():
        hook = _SCORECARD_HOOK_MAP.get(cat)
        if hook is None:
            _log_unknown_category(cat, f)
            hook = _default_hook_for_unknown_category(cat)
        rule = _rule_for_jurisdiction(hook, is_state)
        remedy = hook["remedy_state"] if is_state else hook["remedy_federal"]
        fact_text = _humanize_court_labels((f.get("text") or "").strip())
        if fact_text and fact_text[-1] in ".!?":
            fact_text = fact_text[:-1]
        lines.append(
            f"| {_clip(fact_text, 95)} | {_clip(rule, 30)} | the affected material shall be {_clip(remedy, 55)} |"
        )
    lines.append("")
    lines.append("Each row applies the controlling rule to a fact established by the "
                 "record; the consequence follows as a matter of law, not discretion. "
                 "Where the affected order rests on such defects, that order is void or "
                 "voidable.")
    return lines



def gather_scorecard_facts(canonical_id):
    class _D:
        pass
    d = _D(); d.canonical_id = canonical_id; d.case_id = canonical_id
    return _gather_scorecard_facts(d, canonical_id=canonical_id)

def build_section_ii(facts, is_state=False):
    return _build_strict_section_ii(facts, is_state)

def build_section_iii(facts, is_state=False):
    return _build_strict_section_iii(facts, is_state)


# === REASONING_STACK_SCOPE_V1: per-category motion-type relevance ============
# Each scorecard category lists the motion-title keywords for which its Rule
# Application block is RELEVANT. A motion to strike non-binding authority
# does NOT need the Service / Rosenthal / 1448 / Pomeroy stack just because
# the case has a service defect on file. Match is case-insensitive substring.
# Categories absent from the map pass through (backward compatible).
_CATEGORY_MOTION_KEYWORDS = {
    "CASE_FACT_CONFLICT": (
        "dismiss", "strike", "compel", "opposition", "reconsider", "vacate",
        "accounting", "rehearing",
    ),
    "CROSS_PARTY_ROLE_CONFLICT": (
        "dismiss", "strike", "compel", "opposition", "reconsider", "vacate",
        "rehearing", "mandamus",
    ),
    "INSTRUMENT_VERSION_CONFLICT": (
        "dismiss", "strike", "compel", "opposition", "reconsider", "vacate",
        "rehearing", "mandamus",
    ),
    "REAL_PARTY_RECIPROCITY": (
        "dismiss", "opposition", "strike", "reconsider", "vacate",
        "rehearing", "cert", "mandamus",
    ),
    "SERVICE_DEFECTIVE": (
        "vacate", "default", "entry of default", "reconsider", "mandamus",
        "remand", "recall mandate", "reinstate appeal", "rehearing", "cert",
    ),
    "NOTICE_INADEQUATE": (
        "vacate", "reconsider", "due process", "notice", "recall mandate",
        "mandamus", "rehearing", "cert",
    ),
    "DUE_PROCESS": (
        "vacate", "reconsider", "due process", "notice", "recall mandate",
        "mandamus", "rehearing", "cert",
    ),
    "TWOMBLY_IQBAL_MISAPPLICATION": (
        "reconsider", "vacate", "rehearing", "cert", "mandamus",
        "interlocutory", "panel",
    ),
    "CONCLUSORY_DISMISSAL": (
        "reconsider", "vacate", "rehearing", "cert", "mandamus",
        "interlocutory", "panel",
    ),
    "LEAVE_TO_AMEND_CONCLUSORY": (
        "reconsider", "vacate", "rehearing", "cert", "amend",
        "mandamus", "interlocutory", "panel",
    ),
    "NON_BINDING_AS_CONTROLLING": (
        "strike", "rehearing", "reconsider", "cert", "mandamus", "vacate",
    ),
    "TRIAL_COURT_AS_APPELLATE_AUTHORITY": (
        "strike", "rehearing", "reconsider", "cert", "mandamus", "vacate",
    ),
    "VERIFICATION_MISSING": (
        "strike", "sanctions", "vacate", "reconsider",
    ),
    "VERIFICATION_MISSING_PROC": (
        "strike", "sanctions", "vacate", "reconsider",
    ),
    "BIAS_EMOTIONAL_LANGUAGE": (
        "recusal", "disqualify", "sanctions", "rehearing", "mandamus",
    ),
    "BIAS_PRO_SE_GATEKEEPING": (
        "recusal", "disqualify", "sanctions", "rehearing", "mandamus",
    ),
    "BIAS_PUNITIVE_APPEAL": (
        "recusal", "disqualify", "sanctions", "rehearing", "mandamus",
    ),
    "BIAS_JUDICIAL": (
        "recusal", "disqualify", "sanctions", "rehearing", "mandamus",
    ),
    "PRO_SE_LIBERAL_CONSTRUCTION": (
        "recusal", "reconsider", "vacate", "rehearing", "cert", "mandamus",
    ),
    "IMPROPER_STANDARD_OF_REVIEW": (
        "reconsider", "vacate", "rehearing", "cert", "mandamus",
    ),
    "FILING_DEADLINE_VIOLATION": (
        "strike", "sanctions", "vacate",
    ),
    "PAGE_LIMIT_EXCEEDED": (
        "strike", "sanctions", "vacate",
    ),
    "GARNISHEE_NO_ANSWER": (
        "default", "garnishee", "compel", "enforcement", "execution",
    ),
}


def _category_applies_to_motion(category, motion_title):
    """True if the reasoning-stack block for `category` is relevant to this
    motion. Pass-through (True) when category is unmapped or motion_title is
    empty -- backward compatible."""
    if not motion_title:
        return True
    kws = _CATEGORY_MOTION_KEYWORDS.get(category)
    if not kws:
        return True
    title_l = motion_title.lower()
    return any(k in title_l for k in kws)
# === END REASONING_STACK_SCOPE_V1 ============================================


# === REASONING_STACK_RENDER_V1 ===
def _build_reasoning_stack(scorecard_facts, is_state=False, motion_title=""):
    """For each violation category present that defines a reasoning stack
    (subsection_logic / authority / maxim / equity / burden_shift), emit a
    'Rule Application' block beneath the IF/THEN table. Maxim and equity are
    rendered as DISTINCT subsections. Categories without these fields are
    skipped (fully backward-compatible). Returns a list of markdown lines.

    REASONING_STACK_SCOPE_V1: when motion_title is provided, candidate
    categories are filtered against _CATEGORY_MOTION_KEYWORDS so irrelevant
    Rule Application blocks (e.g. Service in a motion to strike non-binding
    authority) are dropped."""
    if not scorecard_facts:
        return []
    seen = []
    for f in scorecard_facts:
        c = f.get("category", "")
        if c and c not in seen:
            seen.append(c)
    if motion_title:
        seen = [c for c in seen if _category_applies_to_motion(c, motion_title)]
    out = []
    for cat in seen:
        hook = _SCORECARD_HOOK_MAP.get(cat)
        if not hook:
            continue
        has_stack = any(hook.get(k) for k in
                        ("subsection_logic", "authority", "maxim", "equity", "burden_shift"))
        if not has_stack:
            continue
        out.append(f"### Rule Application — {hook.get('name', cat)}")
        out.append("")
        sub = hook.get("subsection_logic")
        if sub:
            out.append(f"**Operative rule.** {sub}")
            out.append("")
        auth = hook.get("authority") or []
        if auth:
            out.append("**Controlling authority.** " + "; ".join(auth) + ".")
            out.append("")
        # MAXIMS_EQUITY_KEYS_V1: resolve DB keys -> canonical text; fall back to
        # literal prose for any non-key entries (backward compatible).
        try:
            from pipeline.definition_engine import render_maxim as _rm, render_equity as _re_eq
        except Exception:
            _rm = _re_eq = (lambda k: "")
        def _resolve(items, kind):
            # RESOLVER_BOTH_STORES_V1: a key may live in CORE_MAXIMS or
            # EQUITY_PRINCIPLES regardless of which field lists it. Try both;
            # fall back to literal prose only if neither store knows the key.
            res = []
            for it in (items or []):
                txt = ""
                if isinstance(it, str):
                    txt = _rm(it) or _re_eq(it) or ""
                    if not txt:
                        txt = it  # literal prose, not a known key
                if txt:
                    res.append(txt if txt.endswith(".") else txt + ".")
            return res
        mx = _resolve(hook.get("maxim"), "maxim")
        if mx:
            out.append("**Governing maxim.** " + " ".join(mx))
            out.append("")
        eq = _resolve(hook.get("equity"), "equity")
        if eq:
            out.append("**Equitable principles.** " + " ".join(eq))
            out.append("")
        bs = hook.get("burden_shift")
        if bs:
            out.append(f"**Burden.** {bs}")
            out.append("")
    return out
# === END REASONING_STACK_RENDER_V1 ===

# === BEEZLE_PATCH_P1_MOTION_FILTER_V2 ===
# Returns every defect fact whose category supports the given motion's posture.
# No top-N cap by default. The motion body lists all defects supporting the
# motion. The scorecard exhibits (A and B) hold the full case ledger.
#
# Categories are matched against motion title keywords. Falls back to the full
# fact list (no filter) if no keyword match — so something always renders.

_MOTION_CATEGORY_KEYWORDS = {
    "strike":          ["strike", "scandal", "immaterial", "12f", "rule12f", "redundant"],
    "vacate":          ["void", "60b", "60(b)", "fraud_on_court", "60d3", "voidable", "vacat"],
    "dismiss":         ["12(b)(6)", "12b6", "iqbal", "twombly", "fail_to_state", "plausib"],
    "remand":          ["remand", "1447", "removal", "1446", "fraudulent_joinder"],
    "mandamus":        ["mandamus", "1361", "clear_right", "ministerial"],
    "summary":         ["56", "rule56", "summary_judgment", "genuine_dispute", "material_fact"],
    "reconsider":      ["59(e)", "59e", "reconsideration", "manifest_error"],
    "intervene":       ["24", "intervention", "permissive", "as_of_right"],
    "amend":           ["15(a)", "15a", "amend", "foman", "relation_back"],
    "compel":          ["37", "compel", "discovery", "rule_37"],
    "default":         ["55", "default_judgment", "55a", "55b"],
    "appeal":          ["1291", "1292", "1292(b)", "frap", "appellate"],
    "rehearing":       ["40", "35", "rehearing", "en_banc", "frap40", "frap35"],
}


def _motion_keywords(motion_title):
    title = (motion_title or "").lower()
    keys = []
    for trigger, kwlist in _MOTION_CATEGORY_KEYWORDS.items():
        if trigger in title:
            keys.extend(kwlist)
    return keys


def _fact_matches_motion(fact, motion_keywords):
    if not motion_keywords:
        return False
    blob_parts = []
    for key in ("category", "name", "rule", "remedy", "label", "type"):
        v = fact.get(key) if isinstance(fact, dict) else None
        if v:
            blob_parts.append(str(v).lower())
    blob = " ".join(blob_parts)
    if not blob:
        return False
    return any(kw in blob for kw in motion_keywords)


def gather_motion_specific_facts(canonical_id, motion_title="", max_facts=None):
    """Return every defect fact whose category supports this motion's posture.

    max_facts: None (default) = no cap, list ALL matching defects.
               int            = cap at that many (used only if caller asks).

    Falls back to the full fact list if no motion keyword matches.
    Returns [] only if the case has no scorecard facts at all.
    """
    all_facts = gather_scorecard_facts(canonical_id) or []
    if not all_facts:
        return []
    mkw = _motion_keywords(motion_title)
    if mkw:
        matched = [f for f in all_facts if _fact_matches_motion(f, mkw)]
        if matched:
            return matched if max_facts is None else matched[:max_facts]
    return all_facts if max_facts is None else all_facts[:max_facts]
# === END BEEZLE_PATCH_P1_MOTION_FILTER_V2 ===
