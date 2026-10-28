"""case_engine — ONE case-AGNOSTIC legal-analysis engine. Point it at ANY case folder; it auto-discovers
the evidence, builds a FACTS-ONLY digest itself, and runs the analysis phases — all grounded in the
corpus (authoritative code via citation-aware retrieval), written to linked Obsidian notes. NOTHING
about any specific case is hardcoded. Hermes runs this itself (see the `case-analyzer` skill); a human
never has to.

  python3 -u -m pipeline.case_engine <slug> [phase]
  phase: requirements | accounting | avenues | tax | accountability | all   (default all)
  (requirements = what the law REQUIRES per product + gap-check; accounting = A/R, A/P, who it's
   payable to, do the books balance; avenues = self-discovered defects; tax = the 1099 series +
   guarantee double-recovery; accountability = each actor's duty + escalation. Email is a SEPARATE
   tool, not here.)

Division of labor (locked): DETERMINISTIC = the rules — what the law REQUIRES (the path), the gap-check
structure, the citation retrieval, the validation; it applies rules, it never interprets meaning. The
LLM (m0 comprehension model, or the Hermes agent) = understands MEANING — what a phrase or definition
means, what a document says, whether a case fact satisfies a required step. Deterministic supplies the
rules + retrieved law; the LLM comprehends the case text against them.

Design (the locked patterns from this project, now generic):
- small pieces: every comprehension step is its own fresh full-context run (rights-enforcer local-gpu/262k,
  or m0 via CASE_ENGINE_BACKEND=m0)
- grounded: each step gets authoritative law via _retrieve (UCC/IRC/CFR/TFM) — deterministic; never invents
- honest: two-axis grading (LEGAL MERIT vs EVIDENCE STATUS); per-se violation = STRONG even if the
  proving document is not yet in hand; report what the corpus says, never pre-judge from outside it
- incremental: each result is written to disk the moment it lands; nothing lost to a timeout
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import sys, json, sqlite3, re
from pathlib import Path
sys.path.insert(0, str(_epkg()))
from pipeline.case_comprehension import (
    _retrieve, _reason, CASES, NOTES, NEW_INDEX, NEW_IDS, CHUNK_DB,
)

ADVOCATE = ("You are a forensic legal + tax analyst and the person's advocate, grounded in the full "
            "system (UCC, the Internal Revenue Code + IRM, the Treasury Financial Manual, Title 12/15/24 "
            "consumer + housing law, the CFR, and civil procedure/standing). REPORT WHAT THE LAW ACTUALLY "
            "SAYS — quote and cite the corpus; never pre-judge or label a position from outside the "
            "corpus; if the law characterizes a position, cite that exact text, else analyze it on the "
            "law's own terms. Invent nothing; omit nothing the corpus supports. No sovereign-citizen "
            "framing. Follow the money mechanically.")

# Every legal requirement reduces to the five W's, and nothing stands alone — laws are not random; each
# requirement links statute -> regulation -> form + its instructions -> agency -> duty -> consequence.
# This schema makes the complexity followable and the Obsidian graph linkable.
FIVE_W = ("Structure EACH item as: **WHO** (must act/file) | **WHAT** (the required filing/step) | "
          "**WHEN** (the trigger/deadline) | **WHERE** (the agency/venue/where it is filed) | **WHY** "
          "(the rule + the purpose it serves) | **LINK CHAIN** (statute -> regulation -> form + its "
          "instructions -> agency -> consequence; show the connected authorities by name/cite — laws are "
          "not random, surface the links) | **GAP** (documented in this case, or MISSING).")

# generic facts digest builder ------------------------------------------------------------------
# the person's OWN work product (drafts/briefs/motions) is skipped for the FACTS digest so the analysis
# reasons from primary documents, not from conclusions. everything else is treated as primary evidence.
_WORKPRODUCT = re.compile(r"(draft|memorandum of law|complaint|opposition|motion|brief|praecipe|"
                          r"affidavit|notice of|cover letter|rebuttal|petition|proposed order|"
                          r"certificate of service|civil-cover|table of contents|appeal)", re.I)
_PRIMARY_HINT = re.compile(r"(note|mortgage|1099|1098|transcript|statement|disclosure|deed|security "
                           r"instrument|agreement|gnma|fds|rly|servic|payoff|escrow|recorded|title|"
                           r"claim|termination|reply|response|letter|w-2|prospectus|10-k|8-k|ledger|"
                           r"account|settlement|assignment|allonge)", re.I)


def _load_evidence(ocr_dir: Path, budget=20000):
    """generic: gather primary-document OCR text from any case's _ocr folder, skip work product + junk,
    prioritize primary-looking docs, cap to a char budget. returns [(name, text)]."""
    cands = []
    for f in sorted(ocr_dir.glob("*.txt")):
        nm = f.name
        if _WORKPRODUCT.search(nm):
            continue
        try:
            t = f.read_text(encoding="utf-8", errors="ignore").strip()
        except Exception:
            continue
        if not t or "OCR FAILED" in t or len(t) < 120:
            continue
        label = re.sub(r"\.(pdf|docx|txt|ocr)", "", nm, flags=re.I).strip()
        cands.append((1 if _PRIMARY_HINT.search(nm) else 0, label, t))
    cands.sort(key=lambda c: -c[0])
    out, used = [], 0
    for _, label, t in cands:
        take = t[:1600]
        if used + len(take) > budget:
            break
        out.append((label, take)); used += len(take)
    return out


def build_digest(slug, evidence):
    ev = "\n\n".join(f"[{lbl}]\n{txt}" for lbl, txt in evidence) or "(no evidence)"
    return _reason(
        f"{ADVOCATE}\n\nFrom the CASE DOCUMENTS below, write a FACTS-ONLY digest: parties, account/loan/"
        "case numbers, amounts, rates, ALL dates, the transaction + money chain, any securitization, any "
        "tax forms (1099/1098) and what each reports, any servicing/assignment, any agency/guarantee. "
        "State only what the documents say; draw NO legal conclusions.\n\n=== CASE DOCUMENTS ===\n"
        + ev[:18000] + "\n\n=== FACTS-ONLY DIGEST ===", timeout=900)


def _get_digest(slug, model_pack=None):
    """reuse a cached digest from any prior case-engine note, else build it."""
    for cand in (f"case-engine-digest-{slug}.md", f"case-avenues-{slug}.md"):
        p = NOTES / cand
        if p.exists():
            t = p.read_text(encoding="utf-8", errors="ignore")
            if "FACTS-ONLY DIGEST" in t or "Facts-only digest" in t:
                seg = re.split(r"FACTS-?ONLY DIGEST|Facts-only digest", t, 1, re.I)[-1]
                return seg.split("\n## ", 1)[0].strip()
    return None


# generic phase prompt builders ----------------------------------------------------------------
def _grade_block(label):
    return ("(3) TWO-AXIS verdict: LEGAL MERIT (STRONG = per-se — the law REQUIRES X and X did not "
            "happen / the claim wins on the law; MODERATE = fact-dependent or split; WEAK = loses even if "
            "proven) and EVIDENCE STATUS (IN-RECORD / ONE-DOC-AWAY name it / SPECULATIVE). Do NOT "
            "downgrade LEGAL MERIT because a proving document is missing. HEADLINE = legal merit. "
            "(4) the one document that moves EVIDENCE to IN-RECORD.")


def _discover(idx, ids, con, model, digest):
    out = _reason(
        f"{ADVOCATE}\n\nUsing the FACTS DIGEST and everything you know about the law, IDENTIFY every "
        "legal/financial AVENUE, defect, or theory worth investigating — across ALL doctrines (negotiable "
        "instruments + whether any paper is itself an instrument/collateral/payment and whether a "
        "separate demand is a double payment; discharge / 1099 reporting; chain of title / holder; "
        "securitization + who owns the obligation; government guarantee + subrogation + double recovery; "
        "FCRA/FDCPA/TILA/RESPA; standing / real party / jurisdiction). Do NOT decide them yet; just "
        "surface them. Output ONLY a numbered list, max 14, each one short testable proposition.\n\n"
        "=== FACTS DIGEST ===\n" + digest[:4500] + "\n\n=== NUMBERED AVENUES ===", timeout=900)
    avs = []
    for ln in out.splitlines():
        m = re.match(r"\s*\d+[\.\)]\s+(.*)", ln)
        if m and len(m.group(1).strip()) > 8:
            avs.append(m.group(1).replace("**", "").strip())
    return avs[:14]


# generic universal topic sets (law is the same for every case) --------------------------------
TAX_TOPICS = [
    ("1099C-when-required", "1099-C cancellation of debt: per IRC 6050P + Treas. Reg. 1.6050P-1 + the IRM, "
     "WHEN must a 1099-C be filed (identifiable events / 36-month testing / charge-off), what does filing "
     "mean vs NOT mean, and does any 1099-C in this record fit a legitimate identifiable event?",
     "1099-C cancellation of debt 6050P identifiable event 36-month testing charge-off IRM discharge of indebtedness"),
    ("1099A-acquisition", "1099-A (acquisition/abandonment of secured property): per IRC 6050J + IRM, when "
     "required, how it sequences with a 1099-C, and whether one was required/filed on this record.",
     "1099-A acquisition abandonment secured property 6050J foreclosure 1099-C relationship IRM"),
    ("1099OID-original-issue", "1099-OID (original issue discount): per IRC 1271-1275 + 6049 + IRM, what OID "
     "is, which instruments/securities carry it, and WHO must report it. In a securitization do the issued "
     "securities carry reportable OID? If the corpus characterizes any 1099-OID position, quote+cite it; "
     "do not import a label from outside the corpus.",
     "original issue discount 1099-OID 1271 1272 1275 6049 issuer reporting REMIC OID information return"),
    ("1099B-sale", "1099-B (broker proceeds): per IRC 6045 + IRM, when the sale of a debt instrument/"
     "security must be reported, and whether a sale into a securitization on this record triggered it.",
     "1099-B proceeds broker sale of securities 6045 basis information reporting securitization sale"),
    ("guarantee-double-recovery", "Government-guarantee double recovery, on the law's own terms: if a loan is "
     "federally insured/guaranteed (FHA / Ginnie / VA), trace from the National Housing Act + 24 CFR Part "
     "203 + the guaranty + the TFM: (a) who pays the lender on default; (b) whether the payor is SUBROGATED "
     "to the lender's rights (24 CFR 203.350-203.368) so the lender no longer holds the debt; (c) whether a "
     "party already made whole that still collects from the borrower commits double recovery / unjust "
     "enrichment / a false claim. Cite the regs + TFM; the documents decide.",
     "FHA insurance claim mortgagee subrogation 24 CFR 203.350 203.368 National Housing Act guaranty Treasury disbursement double recovery unjust enrichment false claim"),
    ("tax-reconciliation", "Reconcile the full 1099 sequence (A/C/OID/B) for this transaction: the correct "
     "order/treatment, what the person's IRS wage-&-income / account transcripts SHOULD show, and what the "
     "presence of some forms but absence of others indicates. End with the exact records to pull.",
     "1099 series reconciliation A C OID B information return matching IRP wage and income transcript IRM"),
]

ACTORS = [
    ("IRS", "If information returns are wrong/missing or books don't reconcile: the IRS's DUTY to act on a "
     "referral + correct the record; AUTHORITY (IRC 6201, 6020, 6050P, 6721/6722, 7434, 7623); HOW to "
     "invoke (Form 3949-A, Form 211, amended-1099 dispute, AUR/CP2000 response, IRP transcripts); "
     "ESCALATION if it doesn't act (TIGTA, Treasury IG, Taxpayer Advocate, APA/mandamus).",
     "IRS duty information return penalty 6721 6722 7434 false return 7623 whistleblower 3949-A TIGTA Taxpayer Advocate"),
    ("HUD-FHA", "On a federally insured loan where a claim may have been paid AND the loan still collected: "
     "HUD's DUTY re claim/termination/subrogation; AUTHORITY (National Housing Act 12 USC 1709/1715u; 24 "
     "CFR 203); HOW to invoke (FHA Resource Center, HUD FOIA, HUD OIG); ESCALATION (HUD OIG, CFPB, APA/"
     "mandamus).",
     "FHA insurance claim subrogation termination 24 CFR 203 National Housing Act 1709 1715u HUD OIG duty"),
    ("CFPB", "RESPA/TILA/FCRA/FDCPA servicing + furnishing: CFPB DUTY/authority (12 USC 5481, 5531, 5536 "
     "UDAAP); HOW to invoke (CFPB complaint + servicer response duty); the person's PRIVATE right of action "
     "(RESPA 2605(f), TILA 1640, FCRA 1681n/o, FDCPA 1692k); ESCALATION (CFPB Ombudsman, OCC, private suit).",
     "CFPB enforce UDAAP 5536 RESPA 2605 private right of action TILA 1640 FCRA 1681n FDCPA 1692k complaint"),
    ("servicer-bank", "The bank/servicer mandatory DUTIES + breach liability: RESPA error-resolution/QWR (12 "
     "USC 2605(e)), FCRA furnisher accuracy/reinvestigation (15 USC 1681s-2(b)), TILA/FDCPA, accurate tax "
     "reporting. Which are MANDATORY ('shall'), the liability, the direct enforcement mechanism.",
     "servicer duty RESPA 2605 QWR FCRA furnisher 1681s-2 accuracy reinvestigation FDCPA liability shall"),
    ("state-AG-DOJ", "State AG + U.S. DOJ for consumer fraud / false claims / double recovery: AUTHORITY (PA "
     "UTPCPL 73 P.S. 201; False Claims Act 31 USC 3729 where federal money/guarantee involved; mail/wire "
     "fraud 18 USC 1341/1343); HOW to refer (AG complaint, qui tam, DOJ referral); ESCALATION if declined "
     "(UTPCPL private action, qui tam relator).",
     "UTPCPL 201 unfair deceptive False Claims Act 3729 qui tam mail fraud 1341 attorney general referral"),
    ("court-judge", "The judge's DUTIES + the person's remedies if the court won't act/appears biased: duty "
     "to rule + recusal (28 USC 455); HOW to invoke (motion for recusal, motion to compel ruling, preserve "
     "the record); ESCALATION (mandamus to the Court of Appeals 28 USC 1361/1651, judicial-misconduct "
     "complaint 28 USC 351, appeal).",
     "judicial duty recusal 28 USC 455 disqualification mandamus 1651 1361 judicial misconduct 351"),
    ("law-enforcement", "Postal inspectors / police / FBI for obstruction of correspondence, mail theft, or "
     "mail/wire fraud: the offenses + who investigates (18 USC 1341/1343/1702/1708; USPIS, FBI/IC3); HOW to "
     "report; ESCALATION if declined.",
     "mail fraud 1341 obstruction of correspondence 1702 mail theft 1708 postal inspection FBI IC3 wire fraud 1343"),
    ("FinCEN-Treasury", "FinCEN/Treasury for suspicious activity around securitization + guarantee payments: "
     "the bank's SAR duty (31 USC 5318(g); 31 CFR 1020/1010) and Treasury's role in the payment flow (TFM); "
     "HOW to flag it; the limits (SAR confidentiality) + ESCALATION.",
     "FinCEN suspicious activity report BSA 5318 31 CFR 1020 Treasury Financial Manual disbursement guarantee"),
]


# the CORE phase: what the law REQUIRES to happen for each product, then gap-check the case. This is the
# "cover the gaps" map — the required filings/structures the system is complex enough that people miss.
# Generic across products (mortgage / credit card / deposit / installment); the digest tells which apply.
REQ_TOPICS = [
    ("required-IRS-filings", "When debt is originated, sold, securitized, discharged, or paid, which IRS "
     "information returns MUST be filed AGAINST THE PERSON'S SSN and BY WHOM (1098 mortgage interest; "
     "1099-A acquisition/abandonment; 1099-C cancellation; 1099-OID; 1099-B sale) per IRC 6041-6050. List "
     "each REQUIRED filing + who must file it + when, then GAP-CHECK the case facts/transcripts: which are "
     "present and which are MISSING. A required filing that is missing is a per-se reporting gap — name it.",
     "information return required 6041 6049 6050 6050J 6050P 1098 1099-A 1099-C 1099-OID 1099-B who must file"),
    ("mortgage-securitization-required", "If a mortgage was sold into a securitization / REMIC trust, what "
     "does the law REQUIRE: the REMIC election + startup-day timing (IRC 860D), the Pooling & Servicing "
     "Agreement + a trustee, the loan delivery + schedule, the assignment + recording, and the resulting "
     "tax reporting. List each required step with its rule, then GAP-CHECK what is documented vs MISSING.",
     "REMIC election 860D startup day pooling and servicing agreement trustee loan delivery schedule assignment recording required"),
    ("trust-beneficiary-trustee", "In a securitization trust, WHO must be the trustee and beneficiaries, and "
     "what fiduciary + reporting DUTIES does the law impose (trust law, the PSA, the Trust Indenture Act, "
     "the REMIC rules, certificate-holder reporting)? List the required roles + duties, then GAP-CHECK who "
     "is named/missing on this record.",
     "trustee beneficiary fiduciary duty pooling servicing agreement Trust Indenture Act REMIC certificate holder required reporting"),
    ("product-specific-required", "Identify the financial product(s) here (mortgage / credit-card account / "
     "deposit account / auto or installment). For EACH present, state the required legal process the law "
     "prescribes — ownership + agreement, any securitization of the receivable, the BSA/FinCEN duties for "
     "accounts, the FCRA/TILA/RESPA duties, the required reporting — then GAP-CHECK the case for missing "
     "required steps.",
     "credit card receivable securitization cardmember agreement deposit account BSA FinCEN CTR SAR Regulation E FCRA TILA RESPA required"),
]


# ACCOUNTING fundamentals — the double-entry reality under every obligation: the receivable (who is
# OWED), the payable (the obligor), who it is payable TO now (holder/true creditor vs servicer), and
# whether the BOOKS BALANCE (sum of all recoveries on the one obligation vs the original amount; excess
# = double recovery / the receivable was already satisfied). Grounded in banking-dictionary + UCC + TFM.
ACCT_TOPICS = [
    ("receivable-payable", "Identify, on this transaction, the ACCOUNT RECEIVABLE (the asset — who holds "
     "it / who is OWED) and the ACCOUNT PAYABLE (the obligor's liability), per the banking-dictionary + "
     "UCC + accounting definitions. State who the obligation is payable TO now (the holder / true "
     "creditor) versus who merely COLLECTS (the servicer). Cite each definition.",
     "account receivable account payable holder true creditor servicer obligor negotiable instrument owed banking definition"),
    ("who-receives-interest", "Trace WHO is entitled to the principal + interest as the receivable moves: "
     "on sale/securitization the receivable transfers with the instrument (UCC 3-203/9-203). Distinguish "
     "owner vs servicer vs investor and show where the cash actually flows. Cite.",
     "transfer of receivable security interest 3-203 9-203 pass-through investor servicer principal interest who owns the account"),
    ("books-balance", "Do the BOOKS BALANCE on this one obligation? Build the ledger: sum every recovery "
     "— borrower payments, any insurance/guarantee claim paid to the lender, securitization sale "
     "proceeds, any 1099-C charge-off/tax benefit — against the original principal. If total recovery "
     "exceeds the obligation, the books do NOT balance and the receivable is over-satisfied = double "
     "recovery. Show the ledger; the documents + figures decide, no opinion.",
     "double entry ledger recovery satisfaction of debt insurance proceeds securitization 1099-C charge-off over-recovery balance receivable extinguished"),
    ("post-satisfaction-duties", "Once a receivable is satisfied / sold / charged off, what MUST happen on "
     "the books and WHO must report it (zero-out, release of lien, the required 1099s, FCRA-accurate "
     "furnishing) — and gap-check what is missing here.",
     "satisfaction release of lien charge off zero balance required reporting 1099-C FCRA furnishing accuracy gap"),
]


# CAPACITY — who is a natural human vs an artificial person/corporation, WHICH law applies to whom and
# in WHAT capacity, and any ambiguous-term misclassification. Grounded in the Dictionary Act (1 U.S.C. 1),
# Black's natural/artificial person, the Commerce Clause, and the construction canons (expressio unius,
# includes/means). The defenses against being mis-regulated as a commercial entity.
CAPACITY_TOPICS = [
    ("party-capacity", "Identify each party's CAPACITY: is the person a NATURAL PERSON (living human, "
     "constitutional protections in life/liberty/property) or an ARTIFICIAL person/corporation? Is the "
     "opponent a corporation/commercial entity? Apply the Dictionary Act (1 U.S.C. 1 - 'person' includes "
     "corporations; 'individual' = the human) + Black's natural vs artificial person. State each party's "
     "capacity and cite.",
     "natural person artificial person corporation individual capacity Dictionary Act 1 USC 1 person includes corporations Black's"),
    ("which-law-applies", "WHO does each law cited against the person apply to, and in WHAT CAPACITY? "
     "Distinguish (a) constitutional protections (natural persons) from (b) commercial/regulatory statutes "
     "under the Commerce Clause (activities in commerce/business). Is the person being regulated in a "
     "COMMERCIAL capacity, or are they a natural person whose conduct is not interstate commerce? Apply "
     "expressio unius (does the statute actually LIST this person/activity?) + includes/means; and note "
     "post-Loper-Bright the court (not the agency) decides what the statute reaches.",
     "commerce clause regulate commerce natural person private capacity who does the statute apply to expressio unius Loper Bright"),
    ("misclassification-trick", "Flag any AMBIGUOUS TERM used to MISCLASSIFY the person for advantage: a "
     "natural human called a statutory 'person' to apply commercial regulation; 'you'/'Borrower'/"
     "'Consumer' defined to impose obligations; a private act treated as 'commerce.' For each, give the "
     "correct classification under the grounded definitions and the consequence.",
     "ambiguous term misclassification natural person statutory person commercial regulation defined term you borrower consumer"),
    ("definitions-reach", "Extract the controlling DEFINITIONS: how is the person/party DEFINED in each "
     "statute and instrument (means = closed vs includes = expansive), and does the definition actually "
     "REACH this person? If the definition does not include the person, the statute does not apply "
     "(expressio unius). Cite the definitional source.",
     "definition means includes closed expansive statutory definition reaches person scope expressio unius"),
]


def _note_header(slug, kind, blurb):
    return (f"---\nid: case-{kind}-{slug}\ntitle: \"{kind} (agnostic engine): {slug}\"\n"
            f"concepts: [case-analysis, {kind}, grounded, obsidian-linked]\ntype: case-analysis\n"
            f"source: hermes-rights-enforcer\n---\n\n# {kind} — {slug}\n\n{blurb}\n")


def run(slug, phase="all"):
    import turbovec
    from sentence_transformers import SentenceTransformer
    ocr_dir = CASES / slug / "intake" / "_ocr"
    if not ocr_dir.exists():
        print(f"no evidence folder for {slug}: {ocr_dir}"); return
    idx = turbovec.TurboQuantIndex.load(str(NEW_INDEX))
    ids = json.loads(NEW_IDS.read_text())
    con = sqlite3.connect(str(CHUNK_DB))
    model = _get_shared_embedder()

    digest = _get_digest(slug)
    if not digest:
        ev = _load_evidence(ocr_dir)
        print(f"[{slug}] building digest from {len(ev)} primary documents", flush=True)
        digest = build_digest(slug, ev)
        (NOTES / f"case-engine-digest-{slug}.md").write_text(
            _note_header(slug, "digest", "Auto-built facts-only digest.") + "\nFACTS-ONLY DIGEST\n\n" + digest,
            encoding="utf-8")
    print(f"[{slug}] digest ready ({len(digest)} chars)", flush=True)

    def _emit(kind, blurb, items, fmt=""):
        note = NOTES / f"case-{kind}-{slug}.md"
        note.write_text(_note_header(slug, kind, blurb), encoding="utf-8")
        for title, q, law_q in items:
            law = _retrieve(idx, ids, con, model, law_q)
            lb = "\n".join(f"[{j+1}] ({h['source']}) {h['text']}" for j, h in enumerate(law)) or "(no hits)"
            gctx = ""
            try:
                from pipeline.connected_notes import graph_context
                gctx = graph_context(f"{title} {law_q}", k=3, max_chars=2200)
            except Exception:
                pass
            prompt = (f"{ADVOCATE}\n\n{q}\n\n{fmt}\n\nGround ONLY in the LAW + CASE FACTS (you may call "
                      "beezle corpus tools to verify). You may BUILD ON the PRIOR UNDERSTANDING below "
                      "(cite [[notes]]) but verify it against the LAW. Cite each source.\n\n"
                      "=== CASE FACTS ===\n" + digest[:3500] + "\n\n=== LAW ===\n" + lb
                      + (("\n\n=== PRIOR UNDERSTANDING (knowledge graph) ===\n" + gctx) if gctx else "")
                      + "\n\n=== GROUNDED ANALYSIS ===")
            print(f"  [{kind}] {title}", flush=True)
            ans = _reason(prompt, timeout=1500)
            with note.open("a", encoding="utf-8") as f:
                f.write(f"\n## {title}\n\n{ans}\n\n*law: "
                        + "; ".join(f"{h['source']}({h['score']:.2f})" for h in law) + "*\n")
        print(f"  wrote {note}", flush=True)

    if phase in ("requirements", "all"):
        _emit("requirements", "What the LAW REQUIRES to happen for each product here, then a GAP-CHECK of "
              "the case: which required filings/structures are documented vs MISSING. A missing required "
              "step is where the system is exploited. Each requirement as who/what/when/where/why + its "
              "link chain.",
              [(t[0], "TOPIC:\n" + t[1], t[2]) for t in REQ_TOPICS], fmt=FIVE_W)
    if phase in ("capacity", "all"):
        _emit("capacity", "Party capacity (natural human vs artificial person/corporation), WHICH law "
              "applies to whom and in WHAT capacity, and any ambiguous-term misclassification — grounded "
              "in the Dictionary Act (1 U.S.C. 1), Black's, the Commerce Clause, and the construction "
              "canons (expressio unius, includes/means, contra proferentem, Loper Bright).",
              [(t[0], "TOPIC:\n" + t[1], t[2]) for t in CAPACITY_TOPICS], fmt=FIVE_W)
    if phase in ("accounting", "all"):
        _emit("accounting", "The double-entry reality: the receivable (who is OWED), the payable, who it "
              "is payable TO (holder vs servicer), and whether the BOOKS BALANCE (all recoveries vs the "
              "obligation; excess = double recovery). Grounded in banking + UCC + TFM.",
              [(t[0], "TOPIC:\n" + t[1], t[2]) for t in ACCT_TOPICS], fmt=FIVE_W)
    if phase in ("avenues", "all"):
        avs = _discover(idx, ids, con, model, digest)
        print(f"[{slug}] discovered {len(avs)} avenues", flush=True)
        _emit("avenues", "Avenues Hermes identified itself, graded two-axis (legal merit vs evidence).",
              [(f"avenue-{i+1}", f"AVENUE TO TEST:\n\"{a}\"\nTest it. Method: (1) governing rule + cite; "
                f"(2) apply element-by-element [cite evidence]; {_grade_block(a)}", a)
               for i, a in enumerate(avs)])
    if phase in ("tax", "all"):
        _emit("tax", "1099 series (A/C/OID/B) + government-guarantee double recovery, through IRC/IRM/TFM/"
              "24CFR. Reports what the law says; positions characterized only as the corpus characterizes them.",
              [(t[0], "TOPIC:\n" + t[1], t[2]) for t in TAX_TOPICS])
    if phase in ("accountability", "all"):
        _emit("accountability", "Per actor: DUTY + AUTHORITY + how to invoke + who to escalate to if they "
              "don't act. Mandatory ('shall') vs discretionary ('may') distinguished.",
              [(a[0], f"ACTOR: {a[0]}\n{a[1]}\nStructure: DUTY (mandatory/discretionary + cite) | AUTHORITY "
                "(cite) | HOW TO INVOKE (exact form/office/deadline) | IF THEY DON'T ACT (next office + "
                "the person's mechanism).", a[2]) for a in ACTORS])

    try:
        from pipeline import connected_notes as CN
        CN.relink()
    except Exception:
        pass
    print(f"[{slug}] done.", flush=True)


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "", sys.argv[2] if len(sys.argv) > 2 else "all")


def _get_shared_embedder():
    # GO step 6: one embedding model per process (pipeline.llm_layer singleton)
    from pipeline.llm_layer import get_embedder
    return get_embedder()
