"""accountability_map — the REMEDY + ESCALATION chain, grounded in law. For each actor that touches this
case, determine FROM THE CORPUS (statutes / acts / CFR / TFM / IRM): (1) their DUTY, (2) their AUTHORITY
(the exact statute/reg granting it), (3) HOW THE PERSON INVOKES them (the filing / complaint / form),
and (4) ESCALATION — who oversees them and the person's mechanism if they FAIL to act. The point: the
system is so complex it isn't meant to be followed; this makes it followable, and shows where to go
next when an officer, agency, bank, AG, judge, or cop does not do their job.

REPORT WHAT THE LAW SAYS — no outside opinion, no pre-judging. Cite the corpus. Two-axis where it is a
claim (DUTY is mandatory/per-se vs discretionary; EVIDENCE in-record / one-doc-away / speculative).

  python3 -u -m pipeline.accountability_map fed_26-cv-00456
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import os, re, sys, json, sqlite3
from pathlib import Path
sys.path.insert(0, str(_epkg()))
from pipeline.case_comprehension import (
    _retrieve, _ask_hermes, CASES, NOTES, NEW_INDEX, NEW_IDS, CHUNK_DB,
)

ANALYST = ("You are a forensic legal analyst mapping accountability for a pro se person. REPORT WHAT THE "
           "LAW ACTUALLY SAYS — quote and cite the statute / act / CFR / TFM / IRM from the provided "
           "corpus. Do NOT pre-judge or import opinion from outside the corpus. Invent nothing; omit "
           "nothing the corpus supports. Distinguish a MANDATORY duty ('shall') from a DISCRETIONARY one "
           "('may'), and say which this is with the cite. Be concrete and usable at a 6th-grade level: "
           "the exact form/complaint to file, the exact office, the deadline, and the exact next office "
           "if they do not act.")

# Context tied to THIS case: a 1099-C was filed while the loan was securitized + (per Form 211) the FHA
# guarantee/securitization reporting is alleged incomplete — i.e. possible double recovery / unbalanced
# books. Each actor is asked relative to THAT.
ACTORS = [
    dict(id="IRS",
         q="The IRS. If information returns are wrong or missing (a 1099-C filed without the required "
           "1099-A/OID/B; a debt reported cancelled while still collected; books that don't reconcile): "
           "(1) the IRS's DUTY to act on a referral and to correct the record; (2) AUTHORITY (IRC 6201, "
           "6020, 6050P, 6721/6722, 7434, 7623); (3) HOW the person invokes it — Form 3949-A (referral), "
           "Form 211 (whistleblower), Form 4598/amended-1099 dispute, a CP2000/AUR response, requesting "
           "IRP/wage-&-income transcripts; (4) ESCALATION if the IRS does not act — TIGTA, the Treasury "
           "Inspector General, the Taxpayer Advocate Service, and APA/mandamus for unreasonable delay.",
         law="IRS duty assess information return penalty 6721 6722 7434 false return 7623 whistleblower 3949-A referral TIGTA Taxpayer Advocate"),
    dict(id="HUD-FHA",
         q="HUD / FHA. On an FHA-insured loan where an insurance claim may have been paid AND the loan "
           "still collected: (1) HUD's DUTY re: the insurance claim, termination (HUD-27050-A), and "
           "subrogation; (2) AUTHORITY (National Housing Act 12 U.S.C. 1709/1715u; 24 C.F.R. Part 203); "
           "(3) HOW the person invokes it — FHA Resource Center complaint, HUD FOIA for claim/termination/"
           "DSRS records, HUD OIG hotline; (4) ESCALATION if HUD does not act — HUD OIG, CFPB, the "
           "person's APA/mandamus remedy.",
         law="FHA mortgage insurance claim subrogation termination 24 CFR 203 National Housing Act 1709 1715u HUD OIG complaint duty"),
    dict(id="CFPB",
         q="The CFPB. For RESPA/TILA/FCRA/FDCPA servicing + furnishing violations: (1) DUTY/authority to "
           "supervise + enforce (12 U.S.C. 5481, 5531, 5536 UDAAP); (2) HOW the person invokes it — the "
           "CFPB consumer complaint + the servicer's duty to respond; (3) the person's OWN private right "
           "of action (RESPA 2605(f), TILA 1640, FCRA 1681n/o, FDCPA 1692k); (4) ESCALATION if the CFPB "
           "does not act — the CFPB Ombudsman, the prudential regulator (OCC), and the private suit.",
         law="CFPB authority enforce UDAAP 5536 RESPA 2605 private right of action TILA 1640 FCRA 1681n FDCPA 1692k consumer complaint"),
    dict(id="servicer-bank",
         q="The bank / servicer (AmeriSave / Dovenmuehle / Planet). Their mandatory DUTIES to the "
           "borrower and the consequence of breach: RESPA error-resolution + QWR (12 U.S.C. 2605(e)), "
           "FCRA furnisher accuracy + reinvestigation (15 U.S.C. 1681s-2(b)), TILA/FDCPA, and accurate "
           "tax reporting. State which are MANDATORY ('shall'), the liability for breach, and the "
           "person's direct enforcement mechanism.",
         law="servicer duty RESPA 2605 qualified written request FCRA furnisher 1681s-2 accuracy reinvestigation FDCPA liability shall"),
    dict(id="state-AG-DOJ",
         q="The State Attorney General (PA) and U.S. DOJ. For consumer fraud / false claims / double "
           "recovery: (1) the AG's DUTY + AUTHORITY (PA UTPCPL 73 P.S. 201-1 et seq.; the federal False "
           "Claims Act 31 U.S.C. 3729 where federal money/guarantee is involved; mail/wire fraud 18 "
           "U.S.C. 1341/1343); (2) HOW the person refers it — PA AG consumer complaint, a qui tam under "
           "the FCA, a DOJ referral; (3) ESCALATION if they decline — the person's own UTPCPL private "
           "action and the qui tam relator mechanism.",
         law="Pennsylvania UTPCPL 201 unfair deceptive False Claims Act 3729 qui tam relator mail fraud 1341 attorney general duty referral"),
    dict(id="court-judge",
         q="The court / judge. The judge's DUTIES and the person's remedies when the court does not "
           "act or appears biased: (1) duty to rule + the Code of Conduct / 28 U.S.C. 455 recusal "
           "standard; (2) HOW the person invokes it — a motion for recusal, a motion to compel ruling, "
           "objections preserving the record; (3) ESCALATION — a writ of mandamus to the Court of "
           "Appeals (28 U.S.C. 1361/1651), a judicial-misconduct complaint (28 U.S.C. 351), and appeal.",
         law="judicial duty recusal 28 USC 455 disqualification mandamus 1651 1361 judicial misconduct complaint 351 Code of Conduct"),
    dict(id="law-enforcement",
         q="Law enforcement (postal inspectors / police / FBI). For obstruction of correspondence, mail "
           "theft, or mail/wire fraud (e.g. borrower-addressed mail diverted to a servicer): (1) the "
           "governing offenses + who investigates (18 U.S.C. 1341/1343/1702/1708; USPIS for mail "
           "offenses, FBI for wire/financial fraud); (2) HOW the person reports it — a USPIS complaint, "
           "an FBI/IC3 referral, a local police report; (3) what duty, if any, attaches and the "
           "ESCALATION if declined.",
         law="mail fraud 1341 obstruction of correspondence 1702 mail theft 1708 postal inspection service FBI referral wire fraud 1343 report"),
    dict(id="FinCEN-Treasury",
         q="FinCEN / Treasury. For suspicious financial activity around the securitization + guarantee "
           "payments: (1) the bank's SAR duty under the BSA (31 U.S.C. 5318(g); 31 C.F.R. 1020/1010) and "
           "Treasury's role in the guarantee payment flow (the TFM); (2) HOW the person flags it — a "
           "FinCEN complaint / referral; (3) the limits (SARs are confidential) and the ESCALATION path.",
         law="FinCEN suspicious activity report BSA 5318 31 CFR 1020 1010 Treasury Financial Manual disbursement guarantee referral"),
    # --- Stage 11.75 additions: oath -> bond/insurance -> reporting hierarchy -> escalation -> impeachment
    dict(id="judicial-bond-oath-impeachment",
         q="The judge AS AN OFFICEHOLDER — the full accountability ladder beyond recusal. (1) the OATH the "
           "office requires (28 U.S.C. 453 federal; the state constitutional oath) and Art. VI cl. 3; (2) any "
           "BOND / surety / faithful-performance requirement for the office and HOW a person damaged by "
           "official misconduct reaches it (a civil action on the bond against the surety); (3) the REPORTING "
           "HIERARCHY + ESCALATION in order — disqualification (28 U.S.C. 455/144), then the federal Judicial "
           "Council complaint (28 U.S.C. 351-364) OR the PA Judicial Conduct Board (PA Const. Art. V 18) -> "
           "Court of Judicial Discipline, then certification to the House and IMPEACHMENT (U.S. Const. Art. II "
           "4 / Art. I 3; PA Const. Art. VI); (4) which rung fits a single ruling vs a pattern, and the LIMITS "
           "of judicial immunity (acts outside jurisdiction are void — Mireles v. Waco; Stump v. Sparkman).",
         law="judicial oath 28 USC 453 Article VI oath official bond surety faithful performance Judicial Council complaint 351 352 354 Judicial Conduct Board Court of Judicial Discipline impeachment Article II section 4 removal judicial immunity excess of jurisdiction void"),
    dict(id="opposing-counsel-discipline",
         q="Opposing counsel / an attorney of record. (1) the DUTY (Rules of Professional Conduct; candor to "
           "the tribunal); (2) the DISCIPLINARY forum + HOW to invoke it — the PA Office of Disciplinary "
           "Counsel under Pa.R.D.E. (Disciplinary Board of the PA Supreme Court), the federal district/circuit "
           "bar (E.D. Pa. L.R. 83), plus in-case Rule 11 sanctions and 28 U.S.C. 1927 personal liability for "
           "vexatious conduct; (3) the MALPRACTICE INSURANCE carrier as a recoverable party (PA disclosure "
           "Pa.R.P.C. 1.4(c)); (4) ESCALATION if discipline declines.",
         law="Office of Disciplinary Counsel Pennsylvania Rules of Disciplinary Enforcement professional conduct candor tribunal Rule 11 sanctions 1927 vexatious multiplication malpractice insurance attorney discipline disbarment"),
    dict(id="official-bonds",
         q="Federal/state officer BONDS as a direct recovery path. (1) the federal officer-bond regime (31 "
           "U.S.C. 9301-9309) and which offices must post a bond (clerks of court, U.S. Marshals, U.S. "
           "Trustees, fiscal/disbursing officers); (2) HOW a person damaged by an officer's breach of duty "
           "reaches the surety — a claim/action on the official bond; (3) the state analog for bonded state "
           "officers; (4) what records to FOIA to identify the bond + surety.",
         law="federal officer bond 31 USC 9301 9302 9303 9304 9305 9306 9308 surety faithful discharge of duties official bond claim action clerk of court marshal trustee disbursing officer state officer bond"),
]


def actors_from_exposure(slug):
    """DATA-DRIVEN actors (2026-08-31): when a case has an m0_exposure_map
    EXPOSURE_MAP.md, its REFER + CRIMINAL lanes DECIDE which administrative /
    enforcement processes to map — m0's corpus routing, not this file's
    hardcoded list. Each enforcer becomes one actor question asking corpus for
    DUTY / AUTHORITY / HOW-TO-INVOKE (exact form+office) / ESCALATION, so the
    resulting filings + agency responses become EXHIBITS in the civil case."""
    # slug may be a plain case slug (cases/<slug>) OR a vault-relative path
    # ("claim_candidates/<slug>" from m0_autopilot). Resolve both; never
    # assume cases/ (v-autopilot rc=1: candidates fell to the 11-actor
    # default list + slash-in-slug broke the note filename).
    _V = (_eroot() / "obsidian_sync")
    cand = [_V / "cases" / slug, _V / slug]
    for base in cand:
        exp = base / "EXPOSURE_MAP.md"
        if exp.exists():
            t = exp.read_text(encoding="utf-8", errors="ignore")
            break
    else:
        return []
    import re as _re
    # collect (cite, name, conduct, enforcer) from REFER + CRIMINAL lanes
    lanes = _re.split(r"^## ", t, flags=_re.M)
    rows = []
    for lane in lanes:
        # ALL lanes carry duty-bearing actors (2026-09-02 class fix: judicial +
        # counsel duties route EVIDENCE — Murchison/Ward/Rule 3.3/Rule 11 — so
        # REFER/CRIMINAL-only extraction silently exempted judges and opposing
        # counsel from accountability. The duty-map ID PREFIX identifies the
        # actor class agnostically; the lane only identifies the proof route.)
        if not lane.startswith(("🏛", "🚨", "🧾", "⚖")):
            continue
        for m in _re.finditer(
                r"### (?P<cite>[^\n—]+)—(?P<name>[^\n]*)\n"
                r"- \*\*Conduct:\*\* (?P<conduct>[^\n]*)\n"
                r"(?:- \*\*Why this lane:\*\* [^\n]*\n)?"
                r"- \*\*Enforcer:\*\* (?P<enf>[^\n]*)"
                r"(?:\n- \*\*Grounding:\*\* (?P<grounding>[^\n]*))?", lane):
            rows.append({k: (m.group(k) or "").strip()
                         for k in ("cite", "name", "conduct", "enf", "grounding")})
    # group by enforcer -> one actor per enforcement office (normalize labels
    # so 'PA_insurance_department' and 'PA DOI' merge into one actor).
    # STANDARDS BODIES (NAIC, NCOIL) are NOT enforcement offices — nobody
    # files a complaint with them; their model acts are EVIDENCE material.
    # Run 1 proved it: corpus honestly returned no duty/mechanism for either.
    _STANDARDS_BODIES = ("naic", "ncoil")
    def _canon(e):
        el = e.lower()
        if _re.sub(r"[^a-z]", "", el) in ("painsurancedepartment", "padoi") or \
           ("insurance" in el and ("department" in el or "doi" in el)) or el.strip() == "pa doi":
            return "PA Insurance Department"
        return e
    by_enf = {}
    # PREFIX→OVERSIGHT derivation (2026-09-02, agnostic): judicial/counsel duty
    # entries carry the generic enforcer 'private plaintiff / court' — the REAL
    # oversight body follows from the actor CLASS (duty-id prefix) + the FORUM
    # (federal/state, read from the case's own file — never hardcoded per case).
    def _forum(slug_):
        try:
            cf = ((_eroot() / "obsidian_sync/cases") / slug_ / "CASE_FACTS.md")
            head = cf.read_text(encoding="utf-8", errors="ignore")[:6000].lower()
            if _re.search(r"district court|edpa|third circuit|u\.s\.c\.a|usca", head):
                return "federal"
        except Exception:
            pass
        return "state"
    _f = _forum(slug)
    _OVERSIGHT = {
        ("jud", "federal"): "Judicial Council of the Circuit (28 U.S.C. § 351 complaint)",
        ("crt", "federal"): "Judicial Council of the Circuit (28 U.S.C. § 351 complaint)",
        ("jud", "state"):   "Judicial Conduct Board of Pennsylvania",
        ("crt", "state"):   "Judicial Conduct Board of Pennsylvania",
        ("opp", "federal"): "Disciplinary Board of the Supreme Court of Pennsylvania (Pa.R.D.E.; + the presiding court's own disciplinary authority)",
        ("opp", "state"):   "Disciplinary Board of the Supreme Court of Pennsylvania (Pa.R.D.E.)",
    }
    for r in rows:
        pfx = ""
        gm = _re.search(r"\[duty_map: ([a-z]+)_", r.get("grounding", "") or "")
        if gm:
            pfx = gm.group(1)
        enf_raw = r["enf"]
        if pfx in ("jud", "crt", "opp") or "private plaintiff" in enf_raw.lower():
            enf_raw = _OVERSIGHT.get((pfx, _f)) or enf_raw
        enf = _canon(enf_raw)
        if _re.sub(r"[^a-z]", "", enf.lower()) in _STANDARDS_BODIES:
            continue
        if "private plaintiff" in enf.lower():
            continue  # no oversight body resolvable — stays a pleading lane only
        by_enf.setdefault(enf, []).append(r)
    actors = []
    # complaint-MECHANISM statutes per office, corpus-verified 2026-08-31
    # (ingested this session; the guardrail below pulls their text verbatim):
    _MECH = {"cfpb": "12 U.S.C. § 5493 (mandatory complaint-intake unit, § 5493(b)(3)) and 12 U.S.C. § 5534 (covered person's duty to respond)",
             "doj": "28 U.S.C. § 547 (each United States attorney shall prosecute offenses against the United States)",
             "us-attorney": "28 U.S.C. § 547 (each United States attorney shall prosecute offenses against the United States)",
             "insurance department": "40 P.S. § 323.1 (examination-system purpose) and 40 P.S. § 323.3 (examination authority; five-year minimum cycle)"}
    for enf, items in by_enf.items():
        cites = "; ".join(f"{i['cite']} ({i['name']})" for i in items)
        conduct = " | ".join(i["conduct"][:200] for i in items[:4])
        _mech = next((v for k, v in _MECH.items() if k in enf.lower()), "")
        _mech_line = (f" Known complaint-mechanism authority to analyze: {_mech}." if _mech else "")
        actors.append(dict(
            id=_re.sub(r"[^A-Za-z0-9]+", "-", enf)[:40].strip("-") or "enforcer",
            q=(f"{enf}. The exposure map identifies these violations in their "
               f"jurisdiction: {cites}. Recorded conduct: {conduct}.{_mech_line} "
               "From the LAW "
               "below state: (1) this office's DUTY to act on a complaint/referral "
               "(mandatory 'shall' vs discretionary 'may', with the cite); "
               "(2) AUTHORITY (the exact statute/reg creating the complaint or "
               "referral mechanism); (3) HOW the person invokes it — the exact "
               "form/portal/office/address and any deadline; (4) what RECORD the "
               "process generates (complaint number, findings letter, "
               "right-to-sue/closure letter) and how that record is used as an "
               "EXHIBIT in a civil action; (5) ESCALATION if this office does "
               "not act — the next office and the person's own mechanism "
               "(ombudsman, OIG, APA/mandamus)."),
            law=" ".join(i["cite"] for i in items) + " "
                + " ".join(_re.sub(r"[^0-9A-Za-z.§ ]", " ", i["name"]) for i in items[:4])
                + " complaint referral investigation duty enforce penalty " + enf))
    return actors


def _digest(slug):
    note = NOTES / f"case-avenues-{slug}.md"
    if note.exists() and "## Facts-only digest" in (t := note.read_text(encoding="utf-8", errors="ignore")):
        return t.split("## Facts-only digest", 1)[1].split("\n## Avenue:", 1)[0].strip()
    return ""


def _ask_legal(prompt: str, timeout=900) -> str:
    """LEGAL-LANE LLM call (2026-08-31, user lane doctrine: the local-gpu handles
    ALL legal inference). Direct BEEZLE_LLAMA_URL call like every other legal
    organ — never the local-gpu chat/swarm lane, never the hermes CLI agent (no
    local-gpu provider is configured there). enable_thinking off: the law is
    supplied in-prompt; this is grounded extraction, not derivation."""
    import urllib.request
    url = os.environ.get("BEEZLE_LLAMA_URL", "http://localhost:8000/v1/chat/completions")
    model = os.environ.get("BEEZLE_CF_MODEL", "qwen3.8-27b")
    body = json.dumps({"model": model, "max_tokens": 4000, "temperature": 0.1,
                       "chat_template_kwargs": {"enable_thinking": False},
                       "messages": [{"role": "user", "content": prompt}]})
    req = urllib.request.Request(url, data=body.encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        raw = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        msg = raw["choices"][0].get("message", {})
        return (msg.get("content") or msg.get("reasoning") or "").strip() or "(empty response)"
    except Exception as e:
        return f"(legal-lane error: {e})"


def run(slug):
    import turbovec
    from sentence_transformers import SentenceTransformer
    digest = _digest(slug)
    # EXPOSURE_MAP-driven actors first (m0 decides from corpus routing).
    # The hardcoded ACTORS list is a fallback ONLY for legacy cases/ slugs
    # without a map — a claim CANDIDATE with an empty REFER/CRIMINAL lane
    # gets NO actors (mapping the 11-actor AmeriSave-era default onto a
    # consumer candidate is noise, not grounding).
    actors = actors_from_exposure(slug)
    if not actors:
        if slug.startswith("claim_candidates/"):
            print(f"[accountability] {slug}: exposure map has no REFER/CRIMINAL "
                  f"entries — no administrative actors to map yet (deepen facts first)", flush=True)
            return
        actors = ACTORS
    print(f"[accountability] {len(actors)} actors "
          f"({'exposure-map-driven' if actors is not ACTORS else 'default list'})", flush=True)
    idx = turbovec.TurboQuantIndex.load(str(NEW_INDEX))
    ids = json.loads(NEW_IDS.read_text())
    con = sqlite3.connect(str(CHUNK_DB))
    model = _get_shared_embedder()
    out_note = NOTES / f"case-accountability-{slug.replace('/', '_')}.md"
    out_note.write_text(
        f"---\nid: case-accountability-{slug}\ntitle: \"Accountability + escalation map (Hermes): {slug}\"\n"
        f"concepts: [accountability, duty, authority, escalation, irs, hud, cfpb, doj, judge, fincen, remedy, mandamus]\n"
        f"type: case-analysis\nsource: hermes-rights-enforcer\n---\n\n# Accountability + escalation map — {slug}\n\n"
        "For each actor: their DUTY, their AUTHORITY (cited), HOW to invoke them (the exact filing), and "
        "WHO to escalate to if they don't act. Grounded in the corpus; mandatory ('shall') duties "
        "distinguished from discretionary ('may'). When one actor fails, the next office is named.\n", encoding="utf-8")
    for i, a in enumerate(actors, 1):
        law = _retrieve(idx, ids, con, model, a["law"])
        # DETERMINISTIC CITE GUARDRAIL (2026-08-31): the semantic index lags
        # fresh ingests (turbovec re-embed delta). The actor's cites are KNOWN
        # — pull their statute text straight from sqlite and put it FIRST so
        # the mechanism statutes (e.g. 12 USC 5493(b)(3), 28 USC 547, 40 P.S.
        # 323.1/323.3) are always in the law block regardless of embedding lag.
        _seen_srcs = {h["source"] for h in law}
        for _cite in re.findall(r"\b(?:\d+\s+(?:U\.S\.C\.|P\.S\.|Pa\.C\.S\.)\s*§+\s*[\d.\-]+[a-z]?)", a["q"]):
            _num = re.search(r"§+\s*([\d.\-]+[a-z]?)", _cite).group(1)
            _title = re.search(r"(\d+)\s+(U\.S\.C\.|P\.S\.|Pa\.C\.S\.)", _cite)
            _pfx = {"U.S.C.": f"usc_t{_title.group(1)}_s", "P.S.": f"pa_ps_t{_title.group(1)}_s",
                    "Pa.C.S.": "pa_consolidated"}[_title.group(2)]
            _srcpat = _pfx + _num.replace(".", "_").replace("-", "_") + "%"
            for _src, _txt in con.execute(
                    "SELECT source, text FROM chunk_text WHERE source LIKE ? LIMIT 2", (_srcpat,)):
                if _src not in _seen_srcs:
                    _seen_srcs.add(_src)
                    law.insert(0, {"source": _src, "text": _txt[:1200], "score": 1.0})
        law = law[:8]
        law_block = "\n".join(f"[{j+1}] ({h['source']}) {h['text']}" for j, h in enumerate(law)) or "(no strong hits)"
        prompt = (f"{ANALYST}\n\nACTOR: {a['id']}\n\n{a['q']}\n\nGround ONLY in the LAW below + the CASE "
                  "FACTS. Structure: DUTY (mandatory/discretionary + cite) | AUTHORITY (cite) | HOW TO "
                  "INVOKE (exact form/office/deadline) | IF THEY DON'T ACT (the next office + the "
                  "person's mechanism). ~300 words.\n\n=== CASE FACTS ===\n" + digest[:3000] +
                  "\n\n=== LAW ===\n" + law_block + "\n\n=== GROUNDED MAP ===")
        print(f"\n>>> {i}/{len(actors)} {a['id']}", flush=True)
        ans = _ask_legal(prompt, timeout=1500)
        with out_note.open("a", encoding="utf-8") as f:
            f.write(f"\n## {a['id']}\n\n{ans}\n\n*law: "
                    + "; ".join(f"{h['source']}({h['score']:.2f})" for h in law) + "*\n")
        print(ans[:300], flush=True)
    print(f"\nwrote {out_note}", flush=True)
    try:
        from pipeline import connected_notes as CN
        CN.relink()
    except Exception:
        pass


def _get_shared_embedder():
    # GO step 6: one embedding model per process (pipeline.llm_layer singleton)
    from pipeline.llm_layer import get_embedder
    return get_embedder()


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "fed_26-cv-00456")
