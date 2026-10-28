"""new_claim_drafter — the LLM step: turn each queued NEW claim (from new_claim_promoter) into a
ready-to-file COMPLAINT SHELL. This is the "new complaint generator from email contents."

Honest design (R7 — LLM never invents facts; all law is deterministic):
  - The LEGAL frame is deterministic black-letter: cause-of-action ELEMENTS + statutory umbrella +
    forum/venue/removal posture all come from m0_new_claim_engine + the ELEMENTS table below.
  - The FACTS stay EXPLICIT PLACEHOLDERS the owner fills from the actual adverse-action / denial
    notice (the evidence_emails are listed as the source). A new pre-suit complaint is intentionally
    NOT file-ready until the owner supplies the denial details — that is correct, not a defect.
  - m0 (if available) writes only the generic NATURE-OF-THE-ACTION paragraph for coherence; it adds
    no facts. Run with --no-llm to skip even that.

  reads  output/new_claims_queue.json   (promoted: status=pending_m0_draft)
  writes output/new_claim_drafts/<domain>/complaint_shell.md  (status: needs owner facts)
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import sys, json, re
from pathlib import Path

ROOT = next((p for p in (_eroot(), _eroot())
             if (p / "cases_index").exists()), _eroot())
sys.path.insert(0, str(ROOT))

QUEUE = ROOT / "output" / "new_claims_queue.json"
OUTDIR = ROOT / "output" / "new_claim_drafts"
COMPLAINANT = {"name": "Filer", "status": "Plaintiff, pro se"}


def _plaintiff_identity():
    """Plaintiff name + one-line address for companion forms, from efiling/mail_config.json
    return_address (filled once). Never fabricated — returns placeholders if unset. (R2)"""
    try:
        cfg = json.loads((ROOT / "efiling" / "mail_config.json").read_text(encoding="utf-8"))
        ra = cfg.get("return_address", {})
        if ra.get("name") and ra.get("line1"):
            tail = f"{ra.get('city','')}, {ra.get('state','')} {ra.get('zip','')}".strip(", ")
            addr = ", ".join([x for x in (ra.get("line1"), ra.get("line2"), tail) if x])
            return ra["name"], addr
    except Exception:
        pass
    return "[Plaintiff name — fill efiling/mail_config.json]", "[Plaintiff address]"


def _case_court_for(court_key: str) -> str:
    return {"phila_ccp": "PA_CP_PHILA", "fed": "EDPA"}.get(court_key or "", "")

# black-letter ELEMENTS per claim_type (what the owner must plead/prove). DETERMINISTIC law.
ELEMENTS = {
    "ecoa": [
        "Plaintiff is an applicant who applied for credit (15 U.S.C. § 1691a(b)).",
        "Defendant is a creditor that regularly extends credit (15 U.S.C. § 1691a(e)).",
        "Defendant took ADVERSE ACTION on the application — denial / revocation / unfavorable change (15 U.S.C. § 1691(d)(6); Reg. B, 12 C.F.R. § 1002.2(c)).",
        "Defendant failed to provide a written statement of the SPECIFIC reasons for the adverse action within 30 days (15 U.S.C. § 1691(d)(2)-(3)) — and/or discriminated on a prohibited basis (§ 1691(a)).",
        "Plaintiff was damaged; ECOA provides actual + punitive damages up to $10,000 and fees (15 U.S.C. § 1691e).",
    ],
    "credit_reporting": [
        "A consumer report concerning Plaintiff was prepared/used (15 U.S.C. § 1681a(d)).",
        "The report contained an INACCURACY traceable to Defendant.",
        "Plaintiff disputed the inaccuracy (to the CRA and/or the furnisher).",
        "Defendant failed to conduct a reasonable REINVESTIGATION (15 U.S.C. § 1681i; furnisher duty § 1681s-2(b)).",
        "Plaintiff was damaged; FCRA provides actual damages (negligent, § 1681o) or statutory + punitive (willful, § 1681n) and fees.",
    ],
    "fcra_furnisher": [
        "Defendant furnished information about Plaintiff to a consumer reporting agency (15 U.S.C. § 1681s-2).",
        "Plaintiff disputed the information with the CRA, triggering an ACDV to Defendant.",
        "Defendant failed its § 1681s-2(b) duties: investigate, review, and correct/delete inaccurate data.",
        "Plaintiff was damaged; private right of action lies under § 1681s-2(b) (negligent § 1681o / willful § 1681n).",
    ],
    "debt_collection": [
        "Plaintiff is a consumer and the obligation is a 'debt' (15 U.S.C. § 1692a(3),(5)).",
        "Defendant is a 'debt collector' (15 U.S.C. § 1692a(6)).",
        "Defendant engaged in a prohibited practice — harassment (§ 1692d), false/misleading rep (§ 1692e), unfair means (§ 1692f), or failed validation (§ 1692g).",
        "FDCPA is strict-liability; statutory damages up to $1,000 + actual damages + fees (15 U.S.C. § 1692k).",
    ],
    "credit_card": [
        "Defendant is a creditor extending open-end consumer credit (TILA, 15 U.S.C. § 1602).",
        "A BILLING ERROR occurred (unauthorized/incorrect charge) (15 U.S.C. § 1666(b)).",
        "Plaintiff gave timely written notice of the billing error (within 60 days).",
        "Defendant failed to investigate/correct within two billing cycles (15 U.S.C. § 1666(a)) — and/or unauthorized-use liability exceeded $50 (§ 1643).",
        "TILA/FCBA provides actual + statutory damages + fees (15 U.S.C. § 1640).",
    ],
    "insurance_bad_faith": [
        "Defendant is an insurer that issued a policy under which Plaintiff was an insured/beneficiary.",
        "Defendant DENIED a benefit/claim owed under the policy.",
        "Defendant lacked a REASONABLE BASIS for the denial, and knew or recklessly disregarded that lack of basis (Terletsky standard).",
        "42 Pa.C.S. § 8371 provides interest, punitive damages, and fees for bad faith; the contract claim adds the policy benefit.",
    ],
    "medical_billing": [
        "Plaintiff was billed for medical services / the debt was placed for collection.",
        "The bill/collection was inaccurate or the collector violated the FDCPA (15 U.S.C. § 1692) / PA UTPCPL (73 P.S. § 201-1).",
        "Plaintiff disputed; Defendant failed to correct/validate.",
        "Remedies under the FDCPA and the UTPCPL (treble damages, 73 P.S. § 201-9.2).",
    ],
    "consumer_protection": [
        "Defendant engaged in unfair or deceptive acts/practices affecting Plaintiff (73 P.S. § 201-2,-3).",
        "Plaintiff justifiably relied / suffered ascertainable loss (73 P.S. § 201-9.2).",
        "UTPCPL provides actual damages (treble in the court's discretion) + fees.",
    ],
}

# umbrella Act per claim_type (anti-weal-out — cite the parent Act, not loose sections)
UMBRELLA = {
    "ecoa": "Equal Credit Opportunity Act, 15 U.S.C. § 1691 et seq. (Reg. B, 12 C.F.R. pt. 1002)",
    "credit_reporting": "Fair Credit Reporting Act, 15 U.S.C. § 1681 et seq.",
    "fcra_furnisher": "Fair Credit Reporting Act (furnisher duties), 15 U.S.C. § 1681s-2",
    "debt_collection": "Fair Debt Collection Practices Act, 15 U.S.C. § 1692 et seq.",
    "credit_card": "Truth in Lending Act / Fair Credit Billing Act, 15 U.S.C. §§ 1601, 1666",
    "insurance_bad_faith": "PA bad-faith remedy, 42 Pa.C.S. § 8371 + the policy (breach of contract)",
    "medical_billing": "FDCPA, 15 U.S.C. § 1692 + PA UTPCPL, 73 P.S. § 201-1 et seq.",
    "consumer_protection": "PA Unfair Trade Practices & Consumer Protection Law, 73 P.S. § 201-1 et seq.",
}

# operative cite whose duty the proof-of-fact questionnaire poses, + the duty phrase, per claim_type. The
# verbatim statute TEXT is pulled from the corpus (rules_index) by duties_from_claim_statutes — not asserted
# here (R7: law comes from the database). These are generic legal-reference pairings, not case data (R2).
_CTYPE_STATUTE = {
    "ecoa": "15 U.S.C. § 1691(d)", "credit_reporting": "15 U.S.C. § 1681i",
    "fcra_furnisher": "15 U.S.C. § 1681s-2(b)", "debt_collection": "15 U.S.C. § 1692g",
    "credit_card": "15 U.S.C. § 1666", "insurance_bad_faith": "42 Pa.C.S. § 8371",
    "medical_billing": "15 U.S.C. § 1692g", "consumer_protection": "73 P.S. § 201-2",
}
_CTYPE_DUTY = {
    "ecoa": "provide a written statement of the specific reasons for the adverse action within 30 days",
    "credit_reporting": "conduct a reasonable reinvestigation of the disputed information",
    "fcra_furnisher": "investigate, review, and correct or delete inaccurate information after a dispute",
    "debt_collection": "validate the debt on request and cease prohibited collection practices",
    "credit_card": "investigate and correct a billing error within two billing cycles",
    "insurance_bad_faith": "act in good faith and not deny a benefit owed without a reasonable basis",
    "medical_billing": "validate the debt and not use unfair or deceptive collection practices",
    "consumer_protection": "refrain from unfair or deceptive acts or practices",
}


def _claims_data_for(c: dict, facts: dict) -> list:
    """Convert a queued new-claim + its extracted record facts into the claims_data the IMPROVED drafter
    (generation.complaint_drafter.build_complaint_from_facts) consumes — so an email-found claim gets the
    same proof-of-fact questionnaire + enacting-law footnotes as every other filing. Verbatim statute text
    is supplied downstream by duties_from_claim_statutes (rules_index/corpus), not invented here."""
    ctype = c.get("claim_type", "consumer_protection")
    cite = _CTYPE_STATUTE.get(ctype, "")
    duty = _CTYPE_DUTY.get(ctype, "perform the duty the governing consumer-protection law imposes")
    quotes = [f.get("quote", "") for f in (facts.get("facts") or []) if f.get("quote")]
    umbrella = UMBRELLA.get(ctype, ctype)
    return [{
        "title": "VIOLATION OF THE " + re.sub(r",.*$", "", umbrella).upper(),
        "statute": cite,
        "controlling_law": [],   # duties_from_claim_statutes -> rules_index.lookup(cite) supplies verbatim text
        "duties": [{"text": duty, "citation": cite}],
        "elements": [{"desc": e} for e in ELEMENTS.get(ctype, ELEMENTS["consumer_protection"])],
        "facts": quotes,
        "breach_duty": duty,
        "breach_conduct": (quotes[0] if quotes else ""),
        "relief": [f"Damages, statutory and actual, and attorney's fees and costs under the {umbrella}."],
    }]


EVIDENCE_DIR = ROOT / "obsidian_sync" / "unmatched_emails"
PLAINTIFF_ALIASES = [w for w in re.split(r"\s+", COMPLAINANT["name"].lower()) if len(w) > 2]

# STRICT markers of a COMPLETED adverse action (the operative fact a claim needs) — per claim type.
# Deliberately NOT conditional/marketing language: "could result in a decline", "still pending",
# "we got your application", "apply now" do NOT count. The fact must have actually happened.
ADVERSE_MARKERS = {
    "ecoa": [r"your application (was|has been|is) (declined|denied|not approved|rejected)",
             r"we (are|were) (unable|not able) to (approve|extend|offer)", r"we have declined",
             r"decision[:\s].{0,25}(declined|denied|not approved)", r"adverse action (was )?(taken|notice)",
             r"unfortunately,? .{0,40}(declined|denied|not able to approve)"],
    "credit_reporting": [r"results of your (re-?)?investigation", r"we (verified|could not verify|deleted)",
                         r"the (disputed )?information (was|has been) (verified|deleted|updated)",
                         r"your dispute (was|has been) (completed|resolved|reviewed)"],
    "fcra_furnisher": [r"we (verified|are reporting|continue to report) .{0,30}(accurate|as reported)",
                       r"the information (was|has been) verified as accurate"],
    "debt_collection": [r"this is an attempt to collect a debt", r"you (currently )?owe", r"amount due[:\s]*\$",
                        r"your account .{0,30}(is in|placed in|been placed for) collection",
                        r"balance .{0,15}(due|owed)[:\s]*\$"],
    "credit_card": [r"we (have )?(determined|found) (that )?(no|there was no) billing error",
                    r"your (billing )?dispute (was|has been) (denied|resolved|closed)",
                    r"we are unable to (adjust|credit|reverse)"],
    "insurance_bad_faith": [r"your claim (was|has been|is) (denied|not covered|rejected)",
                            r"we (are|have) den(y|ied|ying) (your|the) claim", r"coverage (is|was|has been) denied",
                            r"claim (decision|status)[:\s].{0,20}(denied|closed without payment)",
                            r"no coverage (is|exists) (available|under)"],
}
ADVERSE_MARKERS["medical_billing"] = ADVERSE_MARKERS["debt_collection"]


def _company(domain: str) -> str:
    base = re.sub(r"^(mail|email|updates?|offers?|e|notifications?)\.", "", (domain or "").lower())
    return base.split(".")[0].replace("-", " ").title() if base else domain


def _load_email(name: str) -> dict | None:
    """Resolve an evidence email by (possibly truncated) name and return its parsed fields."""
    cand = list(EVIDENCE_DIR.glob(name + "*")) if not (EVIDENCE_DIR / name).exists() else [EVIDENCE_DIR / name]
    cand = [p for p in cand if p.suffix == ".md" and "_reply" not in p.name]
    if not cand:
        return None
    txt = cand[0].read_text(encoding="utf-8", errors="ignore")
    def fm(key):
        m = re.search(rf"^{key}:\s*\"?(.+?)\"?\s*$", txt, re.M | re.I)
        return m.group(1).strip() if m else ""
    body = txt.split("## Body", 1)[-1]
    body = re.split(r"##\s+(Detected|Violations|Case Reference)", body)[0]
    # addressee: a greeting line near the top of the body ("Dear X", "X,\n", or an ALL-CAPS NAME line)
    addressee = ""
    for m in re.finditer(r"(?:^|\n)\s*(?:Dear\s+)?([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,2})\s*,", body[:400]):
        addressee = m.group(1).strip(); break
    if not addressee:
        m = re.search(r"\n\s*([A-Z]{2,}(?:\s+[A-Z]{2,}){0,2})\s*,", body[:400])
        addressee = m.group(1).strip() if m else ""
    return {"file": cand[0].name, "date": fm("date"), "from": fm("from"),
            "subject": fm("subject"), "body": re.sub(r"\s+", " ", body).strip(), "addressee": addressee}


def _extract_facts(ctype: str, claim: dict) -> dict:
    """Read the actual evidence emails and extract the operative facts. The claim is VIABLE only if a
    COMPLETED adverse action (per ADVERSE_MARKERS) actually appears in the record. Also flags when the
    notice is addressed to someone other than the plaintiff. No invented facts — quotes are verbatim."""
    markers = [re.compile(p, re.I) for p in ADVERSE_MARKERS.get(ctype, [])]
    hits, wrong_addressee, loaded = [], set(), 0
    for name in (claim.get("evidence_emails") or []):
        e = _load_email(name)
        if not e:
            continue
        loaded += 1
        if e["addressee"] and not any(a in e["addressee"].lower() for a in PLAINTIFF_ALIASES) \
                and len(e["addressee"].split()) >= 2:
            wrong_addressee.add(e["addressee"])
        for rx in markers:
            m = rx.search(e["body"])
            if m:
                s = max(0, m.start() - 60); q = e["body"][s:m.end() + 90].strip()
                hits.append({"quote": q, "date": e["date"], "source": e["file"], "subject": e["subject"]})
                break
    return {"viable": bool(hits), "facts": hits, "loaded": loaded,
            "wrong_addressee": sorted(wrong_addressee)}


def _nature_paragraph(claim, llm=None) -> str:
    ctype = claim["claim_type"]
    deterministic = (f"This is a civil action under the {UMBRELLA.get(ctype, ctype)}. "
                     f"Plaintiff alleges that {claim['_company']} took the adverse action described below "
                     f"in violation of that Act, entitling Plaintiff to the statutory remedies it provides.")
    if llm is None:
        return deterministic
    try:
        msgs = [{"role": "system", "content": "You write ONE neutral sentence introducing a consumer-law "
                 "complaint. State only the statute and the type of claim. Invent NO facts, NO dates, NO amounts."},
                {"role": "user", "content": f"Claim type: {ctype}. Act: {UMBRELLA.get(ctype)}. "
                 f"Defendant: {claim['_company']}. One sentence."}]
        out = (llm.chat(msgs, thinking=False, n_predict=120) or "").strip()
        return out if 20 < len(out) < 400 and "fact" not in out.lower()[:10] else deterministic
    except Exception:
        return deterministic


def _shell(claim, facts, llm=None) -> str:
    ctype = claim["claim_type"]
    claim["_company"] = _company(claim["defendant_domain"])
    co = claim["_company"]
    elems = ELEMENTS.get(ctype, ELEMENTS["consumer_protection"])
    L = []
    L.append(f"# COMPLAINT — {co}")
    L.append(f"*claim type: {ctype} · forum: {claim['court']} · venue: {claim['venue']}*")
    L.append(f"*facts extracted from {facts['loaded']} source email(s); "
             f"{len(facts['facts'])} adverse-action fact(s) on the record*\n")
    L.append(f"**IN THE {claim['court'].split(':',1)[-1].upper()}**\n")
    L.append(f"{COMPLAINANT['name']}, Plaintiff,\n  v.\n{co}, Defendant.\n\nCOMPLAINT\n")
    L.append("## NATURE OF THE ACTION")
    L.append(_nature_paragraph(claim, llm) + "\n")
    L.append("## I. PARTIES")
    L.append(f"1. Plaintiff {COMPLAINANT['name']} is an adult individual residing in Philadelphia County, PA.")
    L.append(f"2. Defendant {co} is a company that does business in Pennsylvania (confirm the exact "
             f"registered legal name + agent with the PA Dept. of State before service).\n")
    L.append("## II. JURISDICTION & VENUE")
    L.append(f"3. {claim.get('basis','This Court has jurisdiction over the consumer-protection claim.')}")
    L.append(f"4. Venue is proper under {claim['venue']}.")
    L.append(f"5. Removal posture: {claim.get('removal_resistance','')}\n")
    L.append("## III. STATEMENT OF FACTS")
    n = 6
    for f in facts["facts"]:
        when = f"On or about {f['date'][:11]}, " if f.get("date") else ""
        L.append(f"{n}. {when}Defendant {co} issued the following to Plaintiff: "
                 f"“{f['quote']}” (source: {f['source']}).")
        n += 1
    L.append(f"{n}. As a result of that adverse action, Plaintiff was injured (denied credit/coverage and "
             f"the attendant consequences), entitling Plaintiff to the statutory remedies below.")
    L.append("")
    L.append(f"## IV. CAUSE OF ACTION — {UMBRELLA.get(ctype, ctype)}")
    L.append("Plaintiff must establish each element (black-letter law; facts above supply the proof):")
    for i, e in enumerate(elems, 1):
        L.append(f"  {i}. {e}")
    L.append("")
    L.append("## V. PRAYER FOR RELIEF")
    L.append("WHEREFORE, Plaintiff requests judgment against Defendant for the statutory and actual damages, "
             "punitive damages where the Act allows, costs, and reasonable fees, and such other relief as is just.\n")
    L.append("Respectfully submitted,")
    L.append(f"/s/ {COMPLAINANT['name']}\n{COMPLAINANT['name']}, {COMPLAINANT['status']}\n")
    if facts.get("wrong_addressee"):
        L.append(f"> ⚠ The source notice(s) appear addressed to: {', '.join(facts['wrong_addressee'])} — "
                 f"confirm this concerns the Plaintiff and not another person before filing.\n")
    L.append("---")
    L.append("## VERIFICATION (Pa.R.C.P. 1024 / Fed. R. Civ. P. 11)")
    L.append("The facts above are extracted verbatim from the cited record. By LAW the Plaintiff must "
             "personally verify them as true before filing — the system cannot swear to them for you:")
    L.append(f"\nI, {COMPLAINANT['name']}, verify that the facts above are true and correct to the best of my "
             "knowledge, information, and belief. I understand false statements are made subject to the "
             "penalties of 18 Pa.C.S. § 4904 relating to unsworn falsification to authorities.")
    L.append(f"\n/s/ ____________________   Date: __________")
    L.append("\n*Attach the cited source email(s) as Exhibit A. Confirm the limitations period has not run "
             "(ECOA/FCRA ~2 yrs from discovery; § 8371 2 yrs).*")
    return "\n".join(L)


def draft(llm=None):
    if not QUEUE.exists():
        print(f"no queue at {QUEUE} — run pipeline.new_claim_promoter first"); return
    q = json.loads(QUEUE.read_text(encoding="utf-8"))
    claims = [c for c in q.get("promoted", []) if c.get("status") == "pending_m0_draft"]
    if not claims:
        print("queue has no pending_m0_draft claims (nothing auto-promoted)."); return
    OUTDIR.mkdir(parents=True, exist_ok=True)
    drafted = rejected = 0
    for c in claims:
        facts = _extract_facts(c["claim_type"], c)
        d = OUTDIR / re.sub(r"[^a-z0-9]+", "_", c["defendant_domain"].lower())
        d.mkdir(parents=True, exist_ok=True)
        if not facts["viable"]:
            # the operative fact (a completed adverse action) is NOT in the record — no claim, do not draft
            why = (f"No completed {c['claim_type']} adverse action found in the {facts['loaded']} source "
                   f"email(s) (the signal fired on marketing/conditional language, not an actual decision).")
            if facts["wrong_addressee"]:
                why += f" Source notice(s) addressed to: {', '.join(facts['wrong_addressee'])} — not the Plaintiff."
            (d / "NOT_A_CLAIM.md").write_text(
                f"# {_company(c['defendant_domain'])} [{c['violation_category']}] — NO VIABLE CLAIM\n\n{why}\n\n"
                f"Source emails checked: {', '.join(c.get('evidence_emails', []))}\n", encoding="utf-8")
            print(f"   REJECTED {c['defendant_domain']:26} [{c['violation_category']}] — {why[:70]}")
            rejected += 1
            continue
        body = _shell(c, facts, llm=llm)
        (d / "complaint.md").write_text(body, encoding="utf-8")
        try:  # NATIVE improved drafter — the SAME one draft_document/route_draft uses. Emits the mail-ready
              # complaint .docx WITH the proof-of-fact questionnaire (duties from the pleaded statutes) +
              # enacting-law footnotes. Additive: the .md above still feeds the existing verify/gate below.
            from generation.complaint_drafter import build_complaint_from_facts as _bcf
            _pn, _pa = _plaintiff_identity()
            _cc = _case_court_for(c.get("court"))
            _fed = (c.get("court") == "fed")
            _defs = [{"name": _company(c["defendant_domain"]),
                      "conduct": "is the party named in the record correspondence at issue."}]
            _docx = d / "complaint_FILING.docx"
            _bcf(plaintiff_name=_pn, plaintiff_address=_pa, defendants=_defs,
                 court=("IN THE UNITED STATES DISTRICT COURT" if _fed else "IN THE COURT OF COMMON PLEAS"),
                 district=("FOR THE EASTERN DISTRICT OF PENNSYLVANIA" if _fed else "PHILADELPHIA COUNTY"),
                 complaint_title="COMPLAINT",
                 facts=[f.get("quote", "") for f in facts.get("facts", [])],
                 claims_data=_claims_data_for(c, facts), exhibits=[],
                 output_path=_docx, case_court=_cc)
            print(f"      + improved complaint (proof-of-fact + enacting-law) -> {_docx.name}")
        except Exception as _be:
            print(f"   improved docx skipped (non-fatal): {_be}")
        try:  # BRIDGE: turn this viable claim into a REGISTERED VAULT CASE (folder + initial CASE_FACTS +
              # add-only registry entry) so case_facts_refresh + case_readiness work on it natively. Only
              # fires here, inside the viable-claim path (a completed adverse action is on the record).
            from pipeline.new_case_registrar import register_new_case
            _reg = register_new_case(c, facts=facts.get("facts"))
            print(f"      + vault case registered: {_reg.get('canonical_id')} "
                  f"(facts={_reg.get('case_facts_written')}, registry_added={_reg.get('registry_added')})")
        except Exception as _rce:
            print(f"   vault registration skipped (non-fatal): {_rce}")
        try:  # companion forms — draft the court-required IFP/cover sheet the route named (M0 gap)
            from generation.companion_forms import draft_companion_forms_for_route
            _cc = _case_court_for(c.get("court"))
            if _cc:
                _pn, _pa = _plaintiff_identity()
                _forms = draft_companion_forms_for_route(
                    c.get("required_forms", []), _pn, _pa,
                    [{"name": _company(c["defendant_domain"])}], _cc,
                    "____________ TERM, 20____   No. ____________",
                    output_dir=d, amount_cap=int(c.get("amount") or 0),
                    case_type=c.get("claim_type", ""), jury_demand=True)
                if _forms:
                    print(f"      + companion forms: {', '.join(sorted(_forms))} -> {d}")
        except Exception as _cf:
            print(f"   companion forms skipped: {_cf}")
        try:  # universal pre-file gate (GO step 9): definitions + adversarial + authority check
            from pipeline.pre_file_review import gate_draft
            gate_draft(str(d / "complaint.md"))
        except Exception as _ge:
            print(f"   gate_draft skipped: {_ge}")
        try:  # cite-support verification (corpus-grounded) — the same gate m0 motions get. Drops a
              # .NEEDS_CITE_REVIEW marker beside a complaint whose cites are not SUPPORTED; the mailing
              # consolidator / e-filer already refuse to file past that marker. Async-marker pattern (no
              # OOM-prone correction loop). Set NEW_CLAIM_CITE_VERIFY=0 to skip (it is LLM+corpus, ~minutes).
            import os as _os
            if _os.environ.get("NEW_CLAIM_CITE_VERIFY", "1") != "0":
                from pipeline import claim_support_check as _csc
                _cpath = d / "complaint.md"
                _sheet, _rows, _tally = _csc.verify_draft(_cpath.read_text(encoding="utf-8"))
                _cpath.with_name(_cpath.stem + "_PROOF_SHEET.md").write_text(_sheet, encoding="utf-8")
                _bad = {k: v for k, v in _tally.items() if k in ("UNBACKED", "OVERSTATED", "WRONG_POINT")}
                _cmark = _cpath.with_name(".NEEDS_CITE_REVIEW")
                if _bad:
                    _cmark.write_text(f"{_cpath.name}: " + " · ".join(f"{k}:{v}" for k, v in sorted(_bad.items()))
                                      + f"  (see {_cpath.stem}_PROOF_SHEET.md)\n", encoding="utf-8")
                    print(f"   cite-review: {sum(_bad.values())} non-SUPPORTED cite(s) -- NOT filing-ready")
                elif _cmark.exists():
                    _cmark.unlink()
                print(f"   cite-support: {len(_rows)} claim(s) checked "
                      f"({' · '.join(f'{k}:{v}' for k, v in sorted(_tally.items()))})")
        except Exception as _cse:
            print(f"   cite-support skipped (non-fatal): {_cse}")
        flag = "  ⚠ addressee" if facts["wrong_addressee"] else ""
        print(f"   DRAFTED  {c['defendant_domain']:26} [{c['violation_category']}] — "
              f"{len(facts['facts'])} record fact(s){flag} -> {d / 'complaint.md'}")
        drafted += 1
    print(f"\n{drafted} fact-based complaint(s) + {rejected} rejected (no adverse action on record) -> {OUTDIR}")
    print("Drafted complaints have facts extracted from the record; the only remaining step is the "
          "Plaintiff's verification SIGNATURE (required by law — the system cannot swear for you).")


if __name__ == "__main__":
    llm = None
    if "--no-llm" not in sys.argv:
        try:
            from pipeline.llm_layer import LlamaLLM
            llm = LlamaLLM()
        except Exception as e:
            print(f"(m0 unavailable, deterministic nature paragraph: {e})")
    draft(llm=llm)
