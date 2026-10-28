"""case_comprehension — FORENSIC case analysis through the Hermes rights-enforcer AGENT.

The point (per the user): do NOT feed the AI the conclusory letters/memos — those just get parroted
back. Feed it the PRIMARY EVIDENCE (the actual loan documents + the Ginnie Mae trust disclosure data)
and the governing LAW from the corpus, then make it INDEPENDENTLY determine each finding. The user's
own theory is a HYPOTHESIS to verify, never a fact to accept. If the evidence does not establish a
point, the analysis must say so.

Pipeline per question:
  1. assemble the relevant PRIMARY-evidence excerpts (loan docs + GNMA pool data, by relevance hint)
  2. retrieve grounded LAW from the corpus via turbovec (semantic)
  3. route the reasoning through Hermes `rights-enforcer` (the agent + beezle MCP corpus tools + SOUL,
     which enforces ground-in-law / cite / invent-nothing) via `hermes -p rights-enforcer -z`
  4. write a grounded, cited determination

  python3 -m pipeline.case_comprehension fed_26-cv-00456
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import sys, json, sqlite3, subprocess, os
from pathlib import Path
import numpy as np
sys.path.insert(0, str(_epkg()))

TV = (_eroot() / "corpus")
CHUNK_DB = TV / "chunk_text.db"
# FULL-corpus semantic index (3,054,274 chunks) — the WHOLE corpus, not the partial new-chunks set.
# (The old new_chunks_b4.tv held only the recent re-embeds; searching it meant the engine could not
# "see" most of the 3M-chunk corpus — the root cause of grounding gaps.)
NEW_INDEX = TV / "chunk_index_b4.idx"
NEW_IDS = TV / "chunk_ids.json"
# the recent re-embed delta (forms/TFM) — searched IN ADDITION so nothing ingested after the full
# index was built is missed on the semantic side.
_DELTA_INDEX = TV / "new_chunks_b4.tv"
_DELTA_IDS = TV / "new_chunk_ids.json"
_delta_cache = None


def _delta():
    global _delta_cache
    if _delta_cache is None:
        try:
            import turbovec
            _delta_cache = (turbovec.TurboQuantIndex.load(str(_DELTA_INDEX)),
                            json.loads(_DELTA_IDS.read_text()))
        except Exception:
            _delta_cache = (None, None)
    return _delta_cache
CASES = (_eroot() / "obsidian_sync/cases")
NOTES = (_eroot() / "obsidian_sync/_hermes_learnings/notes")
QPREFIX = "Represent this sentence for searching relevant passages: "
HERMES = str(Path.home() / ".local/bin/hermes")
HERMES_ENV = {
    "PATH": str(Path.home() / ".local/bin:~/.hermes/hermes-agent/venv/bin:/usr/bin:/bin"),
    "HOME": str(Path.home()),
}
# Context-aware GPU routing. The local-gpu (:8000) serves 131k; the local-gpu (:8080) serves 262k. These forensic
# agent calls run a long tool-using reasoning loop, so they go to the high-context GPU (local-gpu) to avoid
# overflow/stall on the local-gpu. Override via env. ~4 chars/token; keep total prompt well under the budget.
HERMES_PROVIDER = os.environ.get("HERMES_PROVIDER", "local-gpu")
HERMES_MODEL = os.environ.get("HERMES_MODEL", "local-model")
CTX_TOKENS = int(os.environ.get("HERMES_CTX", "262144"))
PROMPT_CHAR_BUDGET = int(CTX_TOKENS * 4 * 0.45)  # leave headroom for SOUL + tool calls + reasoning + output

# ---- the PRIMARY EVIDENCE for the mortgage case (raw documents only — NOT the conclusory letters).
# substrings matched against the _ocr/*.txt filenames; each labeled for the prompt.
EVIDENCE = [
    ("Promissory Note",            ["Exhibit '1' - Note.pdf.txt"]),
    ("Recorded Mortgage",          ["Exhibit '2' - Recorded Mortgage.pdf.ocr.txt"]),
    ("Notice of Servicing Transfer", ["Notice of Servicing Transfer.pdf.txt"]),
    ("GNMA 2022-125 Announcement", ["GNMA-2022-125-@ANC.txt.txt"]),
    ("GNMA 2022-125 Final Data Statement (trust collateral)", ["GNMA-2022-125-@FDS.pdf.txt"]),
    ("GNMA 2022-125 Relay / loan-pool data", ["GNMA-2022-125-@RLY.pdf.txt"]),
    ("GNMA pool MA8149 disclosure",["MA8149MSF.pdf.txt"]),
    ("1099-C cancellation of debt (AmeriSave)", ["amerisave 1099c.pdf.txt", "amerisave 22 1099C.pdf.txt", "amerisave 24 1099C.pdf.txt"]),
    ("IRS Wage & Income Transcript (1099-C + mortgage principal, IRS-sourced)",
     ["IRS Wage and Income Transcript 2022.pdf.txt", "IRS Wage and Income Transcript 2023.pdf.txt"]),
    ("IRS Account Transcript", ["IRS Account Transcript.pdf.txt"]),
    ("US Bank / trustee reply + note", ["us bank reply + promisory note.pdf.txt"]),
    ("Planet Home Lending production (full re-OCR) + exhibits",
     ["PHL reply-compressed.pdf.hq.txt", "Exhibit A.pdf.hq.txt"]),
    ("FHA Case 446-5514079-703 — HUD FOIA (endorsement/termination/claim/DSRS)",
     ["FOIA Request", "FHA Case 446-5514079"]),
    ("AmeriSave monthly statement with detachable payment coupon",
     ["Amerisave billing statement", "First Payment Letter"]),
]

# ---- the user's actual questions, recast as EVIDENCE-DRIVEN findings to test independently.
# evidence_keys index into EVIDENCE labels (substring ok); law_query drives turbovec retrieval.
from pipeline.jx_questions import UNIVERSAL_JX_QUESTIONS, derive_case_questions, is_mortgage_domain

QUESTIONS = [
    dict(id="loan-identity",
         q="Identify the loan from the primary documents: borrower, lender, principal, rate, dates, "
           "loan/MIN/FHA-case numbers. Reconcile any differing loan numbers across the Note and the "
           "servicing-transfer notice. Is this an FHA-insured loan (Ginnie Mae-eligible)?",
         evidence=["Promissory Note", "Notice of Servicing Transfer", "Recorded Mortgage"],
         law="FHA insured mortgage Ginnie Mae eligibility note holder person entitled to enforce"),
    dict(id="pool-membership",
         q="From the GNMA trust disclosure data ONLY, what collateral backs REMIC Trust 2022-125 — "
           "individual loans or MBS pools? Does the disclosure show THIS borrower's loan, or only "
           "pool-level securities (e.g. MA8149/MA8102/MA8152)? State whether loan-level membership in "
           "the trust can be proven from these documents, and what document would be needed if not.",
         evidence=["GNMA 2022-125 Final Data Statement", "GNMA 2022-125 Relay", "GNMA 2022-125 Announcement", "GNMA pool MA8149"],
         law="Ginnie Mae REMIC trust collateral pooling servicing agreement MBS pool loan level disclosure"),
    dict(id="remic-timeline",
         q="Establish the timeline from the documents: REMIC trust startup/settlement date, the pool "
           "issue date(s), the note date, and the servicing-transfer date(s). Then assess: is there "
           "evidence of a contribution of this loan to the REMIC AFTER its startup day (a prohibited "
           "transaction under 26 U.S.C. 860G(d)(1)), or is that not established by these documents?",
         evidence=["GNMA 2022-125 Announcement", "GNMA 2022-125 Final Data Statement", "Promissory Note", "Notice of Servicing Transfer"],
         law="REMIC startup day prohibited transaction contribution after startup 100 percent tax 26 USC 860G(d) section 860G"),
    dict(id="chain-of-title",
         q="From the Note and any trustee/servicer replies, is there a valid endorsement/allonge chain "
           "making the current claimant the 'person entitled to enforce' the note under UCC Article 3 "
           "(3-201, 3-203, 3-301)? Is an allonge or endorsement missing? Who, on this record, is the "
           "holder?",
         evidence=["Promissory Note", "US Bank / trustee reply", "Planet Home Lending reply"],
         law="person entitled to enforce note holder indorsement allonge transfer UCC 3-301 3-203 3-201 blank special"),
    dict(id="discharge-1099c",
         q="A 1099-C was filed. From the 1099-C evidence, what creditor, amount, identifiable-event code, "
           "and date were reported to the IRS? Under IRC 6050P and governing case law, does filing a "
           "1099-C cancel/discharge the underlying debt, or is it an information return that does not by "
           "itself extinguish the obligation? Determine from the law.",
         evidence=["1099-C cancellation of debt"],
         law="1099-C cancellation of debt income 6050P identifiable event does not discharge obligation creditor reporting"),
    dict(id="tender-discharge",
         q="Assess the UCC 3-603 / 3-310 tender-and-discharge theory: do these documents show a valid "
           "tender of payment that was accepted or wrongfully refused so as to discharge the obligation, "
           "or does the law not support discharge on these facts? Apply the law as written.",
         evidence=["US Bank / trustee reply", "Planet Home Lending reply"],
         law="tender of payment discharge UCC 3-603 payment 3-310 effect of instrument refusal of tender"),
    dict(id="1099c-contradiction",
         q="The IRS Wage & Income Transcript shows a Form 1099-C from AmeriSave: 'Date Canceled "
           "06-22-2022, Amount Discharged $14,185.00, Debt Description MORTGAGE LOAN# 1481262085, "
           "Identifiable Event Code: Other Actual Discharge before Identifiable Event' — while the same "
           "loan was placed in REMIC Trust 2022-125 (settled 7/29/2022) and Planet later claimed "
           "$158,114.78 still owed. Is there a contradiction between reporting a debt cancelled to the "
           "IRS and continuing to securitize/collect it? Address: (a) IRC 6050P — a 1099-C is the "
           "creditor's own report that a discharge occurred; (b) whether a creditor can both treat debt "
           "as cancelled for tax purposes and enforce it; (c) FCRA accuracy of any contrary furnishing. "
           "Determine what the IRS-sourced evidence establishes.",
         evidence=["IRS Wage & Income Transcript", "1099-C cancellation of debt (AmeriSave)", "GNMA 2022-125 Announcement", "Planet Home Lending reply"],
         law="1099-C cancellation of debt income 6050P creditor admission discharge cannot enforce inconsistent position FCRA accuracy furnisher"),
    dict(id="financial-flow",
         q="Do the NUMBERS reconcile? From the IRS transcript and loan/trust data, lay out the money "
           "flow: original principal ($166,920), amounts the IRS shows cancelled by 1099-C (AmeriSave "
           "$14,185; ANGI $15,600; and any 2023/2024 entries), the payoff Planet claims ($158,114.78), "
           "and how the loan was monetized through securitization (Ginnie Mae pool -> REMIC trust -> "
           "certificate holders). Who provided value, who received the cancellation tax benefit, and is "
           "the same obligation being recovered more than once? State only what the figures support.",
         evidence=["IRS Wage & Income Transcript", "Promissory Note", "GNMA 2022-125 Final Data Statement", "Planet Home Lending reply", "IRS Account Transcript"],
         law="securitization monetization 1099-C tax benefit double recovery payoff principal cancellation of indebtedness who provides value"),
    dict(id="true-creditor",
         q="On this record, who is entitled to receive the principal and interest payments — the servicer "
           "(AmeriSave/DMI/Planet), the Ginnie Mae MBS pool, or the REMIC trust certificate holders? "
           "Distinguish servicer vs. holder vs. investor. Where do the P&I cash flows actually go per the "
           "trust data?",
         evidence=["Notice of Servicing Transfer", "GNMA 2022-125 Final Data Statement", "GNMA pool MA8149", "Promissory Note"],
         law="servicer collects payments for trust real party in interest holder investor certificate principal interest pass-through"),
    dict(id="payment-instruments",
         q="Apply the banking-dictionary and UCC definitions to the paper in this loan. (a) The monthly "
           "statement carries a detachable PAYMENT COUPON — applying Black's Law 'coupon' and UCC 3-104 "
           "(13 Pa.C.S. 3104), test element-by-element whether a payment coupon meets the negotiable-"
           "instrument / draft / payment-order definition (writing; signature; unconditional promise or "
           "order; fixed amount; payable to bearer or order; on demand). Which elements are met, which "
           "are not? (b) If the coupon can function as a negotiable instrument and the servicer ALSO "
           "demands a separate cash payment, is that potentially a double payment on the same period? "
           "(c) More broadly, when do instruments, drafts, and the note itself function as collateral or "
           "as money/credit under the UCC + banking definitions? State what the definitions support and "
           "where the argument is strong vs. weak. Do not assert; test.",
         evidence=["AmeriSave monthly statement", "Promissory Note", "Notice of Servicing Transfer"],
         law="coupon negotiable instrument definition UCC 3-104 draft payment order bearer on demand method of payment collateral money credit double payment 13 Pa.C.S. 3104"),
    dict(id="mail-handling",
         q="Examine the Planet Home Lending production + exhibits for IMPROPER MAIL/NOTICE HANDLING: are "
           "there documents or notices ADDRESSED TO THE BORROWER that were instead faxed/routed to or "
           "between the servicers (AmeriSave/DMI/Planet), such that the borrower did not receive them "
           "until requested? Identify any fax-transmittal stamps, cover sheets, or routing showing "
           "borrower correspondence diverted. Then assess the law: (a) RESPA 12 U.S.C. 2605 + 12 C.F.R. "
           "1024 — which notices MUST be delivered to the borrower; (b) 18 U.S.C. 1702 obstruction of "
           "correspondence and 1708 — the high bar; (c) whether this is a documented delivery failure or "
           "just ordinary inter-servicer file transfer. If the specific fax pages are not legible in the "
           "evidence, say so and name what page/exhibit to pull. Do not assert; test.",
         evidence=["Planet Home Lending production", "Notice of Servicing Transfer", "AmeriSave monthly statement"],
         law="RESPA notice to borrower servicing transfer 2605 delivery obstruction of correspondence 18 USC 1702 1708 mail addressed to another"),
    dict(id="monetization-hypothesis",
         q="Test the monetization thesis: how many times was value extracted from this ONE obligation? "
           "Separate the DOCUMENTED from the SPECULATIVE. Documented on this record: the note was sold "
           "for value into Ginnie Mae trust 2022-125 (securitization), a 1099-C discharge was reported "
           "to the IRS, and the loan is FHA-insured. SPECULATIVE unless a record exists: that this "
           "specific note was pledged or deposited at the Federal Reserve discount window to create "
           "money via fractional reserve. State what the law permits a bank to do with a note (book as "
           "asset, sell, securitize) and what the corpus supports re: the Fed-window step. Conclude with "
           "the WINNABLE version (double/triple recovery from what IS documented) vs. the version that "
           "needs a document. Do not assert the Fed-window step unless evidence supports it.",
         evidence=["IRS Wage & Income Transcript", "Promissory Note", "GNMA 2022-125 Final Data Statement", "Planet Home Lending reply"],
         law="securitization sale of note for value fractional reserve money creation Federal Reserve discount window pledge collateral double recovery one obligation"),
    dict(id="fha-hud-termination",
         q="FHA / HUD link: this is an FHA-insured loan (Case 446-5514079-703, MIC/endorsement). The "
           "borrower's HUD FOIA seeks the Form HUD-27050-A termination report, the FHA claim records, "
           "and DSRS/HERMIT/CHUMS notes. Determine: (a) do these records appear in the evidence, or are "
           "they an unresolved gap? (b) If an FHA insurance CLAIM was paid to the lender for default "
           "while the SAME debt was reported discharged by 1099-C and sold into the Ginnie Mae trust, "
           "what is the legal significance (multiple recovery, FHA single-family claim rules, who bears "
           "the loss)? (c) How do FHA insurance, the Ginnie Mae guaranty, and the REMIC interact, and "
           "how does the missing termination affect the chain of title / enforceability under UCC?",
         evidence=["FHA Case", "FOIA", "Promissory Note", "Recorded Mortgage", "IRS Wage & Income Transcript"],
         law="FHA mortgage insurance termination HUD 27050 single family default claim Ginnie Mae guaranty REMIC FHA insured discharge who bears loss"),
]


def _load_evidence(ocr_dir: Path):
    """label -> concatenated OCR text (skips failed/empty OCR)."""
    out = {}
    for label, pats in EVIDENCE:
        texts = []
        for pat in pats:
            for f in ocr_dir.glob("*"):
                if pat.lower() in f.name.lower() and f.is_file():
                    try:
                        t = f.read_text(encoding="utf-8", errors="ignore").strip()
                    except Exception:
                        continue
                    if t and "OCR FAILED" not in t and len(t) > 40:
                        texts.append(t)
        if texts:
            out[label] = "\n".join(texts)
    return out


# domain -> (authoritative source LIKE, keyword that must appear) so retrieval reaches the actual code
# text (UCC = PA Title 13, IRC = Title 26 USCODE/eCFR, agency rules = eCFR) instead of dictionaries.
_AUTH_DOMAINS = [
    (("ucc", "negotiable", "indors", "allonge", "3-104", "3-106", "3-201", "3-203", "3-301", "9-203", "holder in due"),
     "%pa_consolidated%", "negotiable instrument"),
    (("6050p", "1099", "discharge of indebt", "108", "61(a)(12)", "cancellation of debt", "oid", "6045", "6050j"),
     "%title26%", ""),
    # IRS form INSTRUCTIONS describe HOW/WHO/WHEN to file each form — the practical rules, often clearer
    # than the Code. Pull them for any form/1099/1098 query.
    (("1099", "1098", "5498", "w-2", "who must file", "when to file", "instructions for", "form ",
      "identifiable event", "schedule", "original issue discount", "1040"), "%irs_forms%", ""),
    (("fha", "203.", "hud-27050", "mortgage insurance", "subrogat", "national housing act"), "%title24%", ""),
    (("respa", "2605", "1024.", "qualified written request", "servicing transfer"), "%title12%", "servicer"),
    (("tila", "1635", "1026.", "rescission", "finance charge", "annual percentage"), "%title12%", "finance charge"),
    (("fcra", "1681", "furnisher", "reinvestigat", "credit report"), "%title15%", "consumer report"),
    (("treasury", "disburse", "tfm", "settlement of accounts", "payment of public money"), "%tfm%", ""),
    # newly ingested US Code (LII) — FOIA/APA, IFP/mandamus/judiciary, criminal, bankruptcy, False Claims
    # NOTE: '%title5' (ENDS with) not '%title5%' — the latter over-matches title50-54 (war, etc.).
    # The FOIA/APA core is in USCODE-LII-title5 (ends in 'title5').
    (("foia", "freedom of information", "552", "apa", "706", "702", "agency action", "arbitrary",
      "unreasonably delayed", "exhaust", "judicial review of agency"), "%LII-title5", ""),
    (("forma pauperis", "ifp", "1915", "1914", "mandamus", "1361", "1651", "recusal", "455",
      "filing fee", "1331"), "%title28%", ""),
    (("mail fraud", "1341", "wire fraud", "1343", "obstruction of correspondence", "1702", "mail theft",
      "1708"), "%title18%", ""),
    (("bankruptcy", "discharge in bankruptcy", "524", "727", "automatic stay", "362"), "%title11%", ""),
    (("false claims", "3729", "qui tam", "3730", "relator"), "%title31%", ""),
    # PLEADING STANDARD (Rule 12(b)(6) / Twombly / Iqbal) — case law, scattered across sources, so pull by
    # bm25 across ALL sources (src_like '%'). Fixes: the plausibility standard is IN the corpus (Twombly 414,
    # Iqbal 750, plausibility 9633) but semantic retrieval was losing it to Black's dictionary noise.
    (("12(b)(6)", "12b6", "motion to dismiss", "twombly", "iqbal", "plausib", "plausibility",
      "fail to state a claim", "failure to state a claim", "well-pleaded", "well pleaded",
      "notice pleading"), "%", "plausib"),
]


def _auth_pull(con, query, k=6):
    """Keyword pull from AUTHORITATIVE sources for EVERY domain the query touches — guarantees the actual
    UCC / IRC code + the IRS form INSTRUCTIONS (how/who/when to file) + agency-CFR + TFM reach the law
    block, not just semantic dictionary hits. Collects from each matching domain (no early break)."""
    q = query.lower()
    out = []
    for triggers, src_like, kw in _AUTH_DOMAINS:
        hit = [t for t in triggers if t in q]
        if not hit:
            continue
        # RELEVANCE (bm25), not arbitrary LIMIT 2: MATCH the domain keyword + the matched trigger words.
        terms = ([kw] if kw else []) + [t for t in hit
                 if len(t) > 2 and t.replace("-", "").replace(".", "").replace(" ", "").isalnum()][:3]
        match = " OR ".join('"%s"' % t for t in terms) if terms else None
        rows = []
        if match:  # bm25 relevance (own try so a FTS error can't skip the fallback)
            try:
                rows = con.execute(
                    "SELECT text, source_id FROM chunk_text_fts WHERE chunk_text_fts MATCH ? "
                    "AND source_id LIKE ? ORDER BY bm25(chunk_text_fts) LIMIT 2", (match, src_like)).fetchall()
            except Exception:
                rows = []
        if not rows:  # fallback: source/text LIKE (source_id may not match the pattern, or FTS unavailable)
            try:
                sql = "SELECT text, source FROM chunk_text WHERE source LIKE ?"
                args = [src_like]
                if kw:
                    sql += " AND text LIKE ?"; args.append("%" + kw + "%")
                rows = con.execute(sql + " LIMIT 2", args).fetchall()
            except Exception:
                rows = []
        for txt, src in rows:
            if txt:
                # bounded score: enrich the law block, but never outrank a real semantic definition
                out.append({"source": src or "?", "text": txt[:650], "score": 0.62})
    return out[:k]


def _search_one(idx, ids, qv, con, k):
    out = []
    if idx is None or not ids:
        return out
    try:
        scores, pos = idx.search(qv, k)
    except Exception:
        return out
    for s, p in zip(np.atleast_2d(scores)[0], np.atleast_2d(pos)[0]):
        p = int(p)
        if 0 <= p < len(ids):
            r = con.execute("SELECT text, source FROM chunk_text WHERE chunk_id=?", (ids[p],)).fetchone()
            if r and r[0]:
                out.append({"source": r[1] or "?", "text": r[0][:650], "score": float(s)})
    return out


# Source classes that ARE the written law (rules/statutes/regs/constitution/official reporters) —
# sampled from chunk_text.db source names. Used ONLY when a caller opts in via prefer_authority:
# rule-anchor queries were top-hitting semantically-similar but wrong-provenance chunks (IRS manual,
# Fed-Chicago paper at 0.68-0.70 for Rule 11 / 12(b)(6) queries). Dictionaries stay unboosted here —
# they are authority for DEFINITIONS, not rule anchors.
_AUTHORITY_SRC = None  # compiled lazily


def _authority_re():
    global _AUTHORITY_SRC
    if _AUTHORITY_SRC is None:
        import re as _re
        _AUTHORITY_SRC = _re.compile(
            r"(?i)^(e?cfr|usc(ode)?|comps-|usreports-|plaw|public_law|pa_code|pa_consolidated"
            r"|edpa_local_rules|frcp|frap|ucc|constitution)")
    return _AUTHORITY_SRC


_DEMOTE_NOISE_RE = None
def _authority_boost(source, prefer_authority):
    """Rank PRIMARY authority (SCOTUS opinions + statutes/rules) above dictionary/definition noise for
    rule-anchor (prefer_authority) queries. This is the fix for retrieval surfacing Black's Law Dictionary
    (frb_chicago_fractional, ~31k chunks) OVER the 3-4-chunk Supreme Court opinions that answer the query.
    Definition lookups (prefer_authority=False) are untouched -- they legitimately want the dictionary."""
    if not prefer_authority:
        return 0.0
    global _DEMOTE_NOISE_RE
    if _DEMOTE_NOISE_RE is None:
        import re as _re
        _DEMOTE_NOISE_RE = _re.compile(
            r"(?i)^(frb_chicago_fractional|blacks_law|bouvier|banking_dictionary|amjur_and_dictionaries|dictionary)")
    s = (source or "").lower()
    b = 0.0
    if s.startswith("scotus_") or _authority_re().match(s):
        b += 0.22          # primary law: SCOTUS opinions + USC/CFR/rules/constitution/pa_consolidated
    elif s.startswith("amjur") or s.startswith("cjs"):
        b += 0.06          # secondary: encyclopedia/treatise
    if _DEMOTE_NOISE_RE.match(s):
        b -= 0.20          # dictionary / definition noise (would otherwise dominate by sheer chunk count)
    return b


def _retrieve(idx, ids, con, model, query, k=4, prefer_authority=False):
    """Hybrid retrieval over the WHOLE corpus: full 3M-chunk semantic index + the recent re-embed delta
    + authoritative code/instruction text (FTS). The fix for grounding from the entire corpus.
    prefer_authority=True (opt-in, GO step 10): rank written-law sources above manuals/papers when
    scores are comparable — for rule-anchor queries only; default behavior is unchanged."""
    qv = model.encode([QPREFIX + query], normalize_embeddings=True).astype("float32")
    fetch = k * 3 if prefer_authority else k
    hits = _search_one(idx, ids, qv, con, fetch)          # full corpus (3,054,274 chunks)
    di, dids = _delta()
    hits += _search_one(di, dids, qv, con, max(2, k // 2))  # recent re-embeds (forms/TFM)
    # dedupe by (source, prefix), keep best score
    best = {}
    for h in hits:
        key = (h["source"], h["text"][:80])
        if key not in best or h["score"] > best[key]["score"]:
            best[key] = h
    rank = lambda h: -(h["score"] + _authority_boost(h.get("source") or "", prefer_authority))
    hits = sorted(best.values(), key=rank)[:k + 2]
    # Authoritative code text (UCC/IRC/CFR/TFM) is APPENDED after the ranked semantic hits — guaranteed
    # present in the law block, but never dominating. (Fix 2026-07-25: it was PREPENDED, so arbitrary
    # LIMIT-2 statute chunks at a flat 0.99 buried the real definition/holding at the top of every
    # definitional query. Semantic hits now rank first; relevant code follows.)
    auth = _auth_pull(con, query)
    seen = {(h["source"], h["text"][:80]) for h in hits}
    merged = hits + [a for a in auth if (a["source"], a["text"][:80]) not in seen]
    return merged[:k + 2 + len(auth)]


_M0 = None  # cached LlamaLLM client


def _m0_reason(prompt: str, n_predict=750) -> str:
    """m0 = the COMPREHENSION model (the pipeline's LlamaLLM on :8000). Its job is to UNDERSTAND MEANING —
    what a phrase or a legal definition means, what a document says, whether a case fact satisfies a
    required step — which deterministic code cannot do. It is lighter than the full Hermes agent (fewer
    tools/turns), but it is NOT deterministic: comprehension is always an LLM task. The DETERMINISTIC side
    supplies the rules + the retrieved law (_retrieve); m0 comprehends the case text against them."""
    global _M0
    try:
        if _M0 is None:
            from pipeline.llm_layer import LlamaLLM
            _M0 = LlamaLLM()
        return (_M0.chat([{"role": "user", "content": prompt}], thinking=False, n_predict=n_predict) or "").strip()
    except Exception as e:
        return f"(m0 error: {e})"


def _reason(prompt: str, timeout=1500) -> str:
    """Backend dispatch. CASE_ENGINE_BACKEND=hermes (default) -> rights-enforcer agent (deep, grounded,
    local-gpu/262k, can call corpus MCP). CASE_ENGINE_BACKEND=m0 -> the pipeline's LlamaLLM (light, inline)."""
    if os.environ.get("CASE_ENGINE_BACKEND", "hermes").lower() == "m0":
        return _m0_reason(prompt)
    return _ask_hermes(prompt, timeout=timeout)


# Second FULL-AGENT lane (GO step 5, 2026-07-08): the 14 forensic questions are independent, so two
# can run at once — one on the local-gpu (:8080, 262k) and one on the local-gpu (:8000, 131k) via the
# manifest-fast provider. BOTH lanes are the complete rights-enforcer agent with corpus tools —
# definition checking is reasoning+context work and is NEVER degraded to a tool-less raw LLM call.
FAST_PROVIDER = os.environ.get("HERMES_FAST_PROVIDER", "manifest-fast")
FAST_MODEL = os.environ.get("HERMES_FAST_MODEL", "local-model")
FAST_CTX = int(os.environ.get("HERMES_FAST_CTX", "131072"))


def _ask_hermes(prompt: str, timeout=900, provider=None, model_name=None, ctx_tokens=None) -> str:
    # context guard: never feed a prompt that, with agent overhead, would blow the chosen GPU's budget
    budget = int(ctx_tokens * 4 * 0.45) if ctx_tokens else PROMPT_CHAR_BUDGET
    if len(prompt) > budget:
        prompt = prompt[:budget] + "\n\n[evidence truncated to fit context budget]"
    prov = provider if provider is not None else HERMES_PROVIDER
    mdl = model_name if model_name is not None else HERMES_MODEL
    cmd = [HERMES, "-p", "rights-enforcer"]
    if prov:
        cmd += ["--provider", prov, "-m", mdl]
    cmd += ["-z", prompt]
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=HERMES_ENV)
        out = (cp.stdout or "").strip()
        if not out:
            out = f"(hermes empty output; stderr tail: {(cp.stderr or '')[-300:]})"
        return out
    except subprocess.TimeoutExpired:
        return "(hermes timed out)"
    except Exception as e:
        return f"(hermes error: {e})"


_HEADWORD_SET = None


def _headword_set():
    """The corpus's OWN dictionary of defined legal terms (the definitions-index headwords), cached. This is
    the NON-HARDCODED vocabulary of terms of art: whatever the corpus defines is what counts, so depth
    extends to any domain (insurance, tax, UCC, equity, ...) without an allowlist."""
    global _HEADWORD_SET
    if _HEADWORD_SET is not None:
        return _HEADWORD_SET
    _HEADWORD_SET = set()
    try:
        import sqlite3
        from pipeline import definitions_index as DI
        con = sqlite3.connect(DI.DB)
        _HEADWORD_SET = {r[0] for r in con.execute("SELECT term FROM headwords") if r[0]}
        con.close()
    except Exception:
        pass
    return _HEADWORD_SET


def _definitions_block(*texts):
    """DEPTH, not hardcoded: discover the operative legal terms in the text by matching it against the
    corpus's OWN dictionary of defined terms (definitions-index headwords) — any phrase that IS an indexed
    headword is a term of art — then DEFINE each from the index. Corpus-driven, so it works for any claim /
    domain, not just the mortgage case. Multiword terms of art are preferred over single words. Returns
    (bullet block, operative-terms list); ('', []) if unavailable. The caller adds the equity & maxims from
    the corpus and seeds each term into the learning cycle to CONTINUE the journey."""
    import re
    hw = _headword_set()
    if not hw:
        return "", []
    try:
        from pipeline import definitions_index as DI
    except Exception:
        return "", []
    blob = " ".join(t for t in texts if t).lower()[:6000]
    words = re.findall(r"[a-z][a-z'-]{1,}", blob)
    cands, seen_c = [], set()
    for n in (4, 3, 2):                       # multiword terms of art first
        for i in range(len(words) - n + 1):
            g = " ".join(words[i:i + n])
            if g in hw and g not in seen_c:
                seen_c.add(g); cands.append(g)
    for w in words:                            # then salient single words that are indexed terms
        if len(w) >= 5 and w in hw and w not in seen_c:
            seen_c.add(w); cands.append(w)
    lines, terms_found, seen = [], [], set()
    for cand in cands:
        if len(terms_found) >= 12:
            break
        try:
            hits = DI.lookup(cand, limit=1)
        except Exception:
            hits = []
        if not hits:
            continue
        d = hits[0]
        key = d["term"].strip().lower()
        if key in seen:
            continue
        seen.add(key); terms_found.append(d["term"].strip())
        cite = f" ({d['cite'][:40]})" if d.get("cite") else ""
        lines.append(f"- {d['term']} [{d['kind']}]: {(d['definition'] or '').strip()[:260]}{cite}")
    return "\n".join(lines), terms_found


_SEEDED_TERMS = set()


def _seed_term_journey(terms):
    """NO-HARDCODE + GRANULARITY: the seed question set only STARTS the journey. Hand each operative term to
    the EXISTING learning cycle as one small seed so the continuous learner CONTINUES it — deepening the term
    (statutory/term-of-art definition, its EQUITABLE meaning, the maxims of law that govern it), then
    following the terms IT references and the process it triggers, linking each in the graph over later
    passes. Equity and maxims express the basis of the law but are not binding — the cited authority is; the
    corpus decides what is correct, not any asserted proposition. Deduped per process (cheap append)."""
    try:
        from pipeline import connected_notes as CN
    except Exception:
        return
    for t in terms or []:
        key = (t or "").strip().lower()
        if not key or key in _SEEDED_TERMS:
            continue
        _SEEDED_TERMS.add(key)
        try:
            CN.record_learning(
                text=(f"UNDERSTAND operative legal term '{t}' fully FROM THE CORPUS (not the lay sense): "
                      f"WHO it applies to (parties/capacity), WHAT it means (term-of-art definition, "
                      f"means/includes discipline), WHEN it triggers (the process/lifecycle), WHERE it "
                      f"governs (jurisdiction/forum), and WHY (its basis — equity and maxims express the "
                      f"nature of the law but are not binding; the statute/code/case is). Then follow the "
                      f"terms it references. Derive what is correct from the corpus; assert nothing."),
                tags=["term-journey", "definition-first", "who-what-when-where-why", "equity", "maxims"],
                source="case-comprehension",
            )
        except Exception:
            pass


def run(slug: str, only_ids=None, question_set=None):
    # question_set lets the SAME deterministic per-question loop analyze ANY claim: pass a claim-derived
    # list of {id,q,evidence,law} dicts (e.g. from the new-claim engine's causes of action) instead of the
    # mortgage-specific QUESTIONS. Default None keeps existing behavior. This is why the tool "works with
    # new claims" — the machinery is general; only the questions are per-claim.
    import turbovec
    from sentence_transformers import SentenceTransformer
    ocr_dir = CASES / slug / "intake" / "_ocr"
    if not ocr_dir.exists():
        print(f"no OCR dir for {slug}: {ocr_dir}"); return
    if question_set is not None:
        _base = list(question_set)
    elif is_mortgage_domain(slug):
        _base = QUESTIONS
    else:
        _base = derive_case_questions(slug)
    qset = list(UNIVERSAL_JX_QUESTIONS) + _base
    questions = [q for q in qset if (not only_ids or q["id"] in only_ids)]
    ev = _load_evidence(ocr_dir)
    print(f"loaded {len(ev)} primary-evidence document sets: {', '.join(ev)}")
    idx = turbovec.TurboQuantIndex.load(str(NEW_INDEX))
    ids = json.loads(NEW_IDS.read_text())
    con = sqlite3.connect(str(CHUNK_DB))
    model = _get_shared_embedder()

    sysmsg = (
        "You are a forensic legal analyst examining a borrower's ACTUAL loan documents and the Ginnie "
        "Mae trust disclosure data. Determine the QUESTION using ONLY the PRIMARY EVIDENCE and the LAW "
        "provided (you may call the beezle corpus tools to verify a citation). Method, strictly:\n"
        "1) State what the evidence shows — cite each document by its [label].\n"
        "2) State the governing law — cite it.\n"
        "3) UNDERSTAND each operative legal term fully from the corpus (not the lay sense) — WHO it applies "
        "to (parties/capacity), WHAT it means (term-of-art definition, means/includes discipline), WHEN it "
        "triggers (the process/timeline), WHERE it governs (jurisdiction/forum), and WHY (its basis — the "
        "equity and maxims express the NATURE of the law here; they are NOT themselves binding, the cited "
        "authority is). Do not assert a meaning; derive it from the definitions/law provided.\n"
        "4) Give your independent determination, APPLYING the understood terms to the evidence, and how "
        "confident the evidence makes it. Derive what is correct from the law and evidence — assume nothing.\n"
        "Do NOT assume any conclusion. If the evidence does not establish the point, SAY SO plainly and "
        "name the document that would resolve the gap. Invent no fact or citation. Apply the law as "
        "written; no sovereign-citizen framing. Be concise and specific."
    )

    # PHASE A (sequential, main thread — sqlite + embedder are not shared across threads):
    # build every question's full prompt (evidence + law + definitions/maxims depth blocks).
    prepared = []
    for qd in questions:
        ev_block = "\n\n".join(f"[{lbl}]\n{ev[lbl][:2200]}" for lbl in ev
                               if any(h in lbl for h in qd["evidence"])) or "(no matching evidence found)"
        # rule-anchor retrieval prefers written-law sources (statutes/rules/regs) over manuals/papers
        law = _retrieve(idx, ids, con, model, qd["law"], prefer_authority=True)
        law_block = "\n".join(f"[{i+1}] ({h['source']}) {h['text']}" for i, h in enumerate(law)) or "(no strong corpus hits)"
        # DEPTH: define the operative terms (index) + pull the equity & maxims that express their basis
        # (corpus, not a hardcoded list; equity/maxims inform understanding, they are not binding authority),
        # then seed each term into the existing learning cycle to continue the journey.
        def_block, op_terms = _definitions_block(qd["q"], ev_block, law_block)
        mq = ("maxims of law and principles of equity governing " +
              (", ".join(op_terms[:6]) if op_terms else qd["q"][:80]))
        maxh = _retrieve(idx, ids, con, model, mq)
        max_block = "\n".join(f"[{i+1}] ({h['source']}) {h['text']}" for i, h in enumerate(maxh)) or "(no strong corpus hits)"
        depth_block = ((def_block or "(no indexed definitions — define the operative terms from the corpus first)")
                       + "\n-- equity & maxims of law --\n" + max_block)
        _seed_term_journey(op_terms)
        prompt = (f"{sysmsg}\n\n=== QUESTION ===\n{qd['q']}\n\n=== PRIMARY EVIDENCE ===\n{ev_block}\n\n"
                  f"=== LAW (corpus) ===\n{law_block}\n\n"
                  f"=== DEFINITIONS + EQUITY/MAXIMS (operative terms of art; the definitions/law are the "
                  f"binding authority, the equity & maxims express their basis — understand, do not assert) "
                  f"===\n{depth_block}\n\n"
                  f"=== DETERMINATION (define each operative term FIRST, then apply it to the evidence; "
                  f"evidence-cited + law-cited) ===")
        prepared.append((qd, prompt, law))

    # PHASE B (GO step 5): dispatch the independent agent calls TWO at a time — lane 0 = local-gpu (:8080),
    # lane 1 = manifest-fast (local-gpu, :8000). Both are the FULL rights-enforcer agent with corpus tools.
    # BEEZLE_CC_PARALLEL=0 restores the old serial behavior.
    findings = []
    parallel = os.environ.get("BEEZLE_CC_PARALLEL", "1") != "0" and len(prepared) > 1
    if parallel:
        from concurrent.futures import ThreadPoolExecutor

        def _dispatch(i_item):
            i, (qd, prompt, law) = i_item
            if i % 2 == 0:
                ans = _ask_hermes(prompt)  # local-gpu lane (262k)
            else:
                ans = _ask_hermes(prompt, provider=FAST_PROVIDER, model_name=FAST_MODEL,
                                  ctx_tokens=FAST_CTX)  # local-gpu lane (131k), full agent
            return i, qd, ans, law

        print(f"\n>>> dispatching {len(prepared)} questions on 2 full-agent lanes (local-gpu + local-gpu) ...", flush=True)
        results = {}
        with ThreadPoolExecutor(max_workers=2) as pool:
            for i, qd, ans, law in pool.map(_dispatch, enumerate(prepared)):
                results[i] = (qd, ans, law)
                print(f"\n>>> {qd['id']} done (lane {'local-gpu' if i % 2 == 0 else 'local-gpu'})", flush=True)
                print(ans[:600], flush=True)
        findings = [results[i] for i in sorted(results)]
    else:
        for qd, prompt, law in prepared:
            print(f"\n>>> {qd['id']}: asking Hermes rights-enforcer ...", flush=True)
            ans = _ask_hermes(prompt)
            findings.append((qd, ans, law))
            print(ans[:600], flush=True)

    # write the consolidated forensic note
    NOTES.mkdir(parents=True, exist_ok=True)
    out = [f"---\nid: case-forensics-{slug}\ntitle: \"Forensic analysis (Hermes-derived): {slug}\"\n"
           f"concepts: [remic, securitization, chain-of-title, discharge, 1099c, ginnie-mae, standing, real-party]\n"
           f"type: case-analysis\nsource: hermes-rights-enforcer\n---\n",
           f"# Forensic analysis — {slug}\n",
           "Findings derived **independently by the Hermes `rights-enforcer` agent** from the PRIMARY "
           "evidence (loan documents + Ginnie Mae trust data) and the corpus law. The borrower's own "
           "theory was treated as a hypothesis to verify, not a fact. Each finding cites the evidence "
           "and the governing law.\n",
           f"Primary evidence in record: {', '.join(ev)}.\n"]
    for qd, ans, law in findings:
        out.append(f"## {qd['id']}\n\n**Question:** {qd['q']}\n\n{ans}\n\n"
                   f"*law grounded in: " + "; ".join(f"{h['source']}({h['score']:.2f})" for h in law) + "*\n")
    suffix = "" if not only_ids else "-" + "-".join(only_ids)[:40]
    note = NOTES / f"case-forensics-{slug}{suffix}.md"
    note.write_text("\n".join(out), encoding="utf-8")
    print(f"\nwrote {note}")
    try:
        from pipeline import connected_notes as CN
        CN.relink()
    except Exception:
        pass


def synthesize(slug: str):
    """Capstone: read EVERY forensic finding already produced for the case and collapse it into the two
    questions that decide it — (1) is the obligation enforceable, (2) who is entitled to enforce it and
    who is it on. Holds all the threads so the litigant doesn't have to."""
    notes = sorted(NOTES.glob(f"case-forensics-{slug}*.md"))
    findings = "\n\n".join(n.read_text(encoding="utf-8", errors="ignore") for n in notes)
    if not findings.strip():
        print("no forensic findings to synthesize"); return
    prompt = (
        "You are a forensic legal analyst and the borrower's advocate. Below are ALL the forensic "
        "findings already derived for this case from the primary evidence and the law. The dollar math "
        "is secondary. SYNTHESIZE everything into the TWO questions that decide the case:\n"
        "  (1) Is the obligation ENFORCEABLE on this record — yes / no / only if X? \n"
        "  (2) WHO is entitled to enforce it (the real party in interest / person entitled to enforce "
        "under UCC 3-301), and WHO is the obligation actually on?\n"
        "Tie the threads together explicitly: chain of title / missing allonge, the 1099-C discharge, "
        "the FHA insurance + missing HUD termination, the sale into the Ginnie Mae trust / "
        "securitization, and standing. Give the borrower's STRONGEST grounds, RANKED, each in one plain "
        "sentence a pro se litigant can hold in their head. Then list the few documents that would close "
        "the remaining gaps. Ground every point in the findings; invent nothing; concede what the record "
        "does not support. End with a 3-sentence bottom line.\n\n=== ALL FINDINGS ===\n" + findings[:14000] +
        "\n\n=== SYNTHESIS: enforceable? by whom? against whom? ===")
    print("synthesizing all findings via Hermes rights-enforcer ...", flush=True)
    ans = _ask_hermes(prompt, timeout=900)
    print(ans[:800], flush=True)
    out = (f"---\nid: case-bottomline-{slug}\ntitle: \"Bottom line — enforceable? by whom? (Hermes synthesis): {slug}\"\n"
           f"concepts: [enforceability, real-party, standing, chain-of-title, discharge, fha, securitization]\n"
           f"type: case-analysis\nsource: hermes-rights-enforcer\n---\n\n# Bottom line — {slug}\n\n"
           "Hermes synthesized every forensic finding into the two questions that decide the case: is the "
           "obligation enforceable, and who is it on. Grounded in the findings; gaps named.\n\n" + ans +
           "\n\n## Related\n- [[case-forensics-" + slug + "]]\n")
    note = NOTES / f"case-bottomline-{slug}.md"
    note.write_text(out, encoding="utf-8")
    print(f"\nwrote {note}")
    try:
        from pipeline import connected_notes as CN
        CN.relink()
    except Exception:
        pass


if __name__ == "__main__":
    slug = sys.argv[1] if len(sys.argv) > 1 else "fed_26-cv-00456"
    arg2 = sys.argv[2] if len(sys.argv) > 2 else None
    if arg2 == "--synthesize":
        synthesize(slug)
    else:
        run(slug, arg2.split(",") if arg2 else None)


def _get_shared_embedder():
    # GO step 6: one embedding model per process (pipeline.llm_layer singleton)
    from pipeline.llm_layer import get_embedder
    return get_embedder()
