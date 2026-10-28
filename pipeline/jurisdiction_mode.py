# -*- coding: utf-8 -*-
"""
jurisdiction_mode — the 10th authority leg: which MODE of judicial power governs (LAW / EQUITY /
ADMIRALTY / ecclesiastical), and did the court or opposing counsel SWITCH modes to strip the litigant's
rights. Additive companion to pipeline.authority_triad (does NOT modify it).

It reads the CASE RECORD, because the switch is pulled in the ORDERS and OPPOSING DRAFTS, not in the
claim list:
  - court ORDERS         output/audit/order_indexes/orders_<slug>.json  (rulings: subject + outcome)
  - opposing filings     audit['opposing_facts'] + audit['opposing_counsel_misconduct']
  - judicial officers    audit['judicial_officers']  (real signers -> the immunity test runs on real acts)
  - litigation posture   audit['litigation_posture'] (contested_void_orders, clerk_nonfiling) + _jx_summary
  - facts / history      obsidian_sync/cases/<slug>/CASE_FACTS.md + CASE_TIMELINE.json  (posture context)

Corpus-grounded doctrine (cites in CITES): LAW -> 7th Am. jury if demanded (Ransom v. Staso Milling;
Fed.R.Civ.P. 38/39; not demanding only WAIVES the jury, it does not forfeit common law). EQUITY -> no
jury, clean-hands maxims, and jurisdiction ONLY where no adequate remedy at law (Story/Pomeroy).
ADMIRALTY -> no jury, but saving-to-suitors (28 U.S.C. 1333(1)) preserves the law option. IMMUNITY ->
needs jurisdiction + a JUDICIAL act (Am.Jur.2d Judges s61); fails for a non-judicial/ministerial/
commercial act or an act in clear absence of jurisdiction -- and a court transacting commerce "descends
to the level of a private citizen" (Clearfield/Planters). SUPREME -> the Constitution (incl. the 7th Am.)
sits atop every mode (Am.Jur.2d Constitutions s46); a legal claim cannot be re-labeled "equitable" to
deny the jury.
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import sys, re, json
from pathlib import Path

ROOT = next((p for p in (_eroot(), _eroot())
             if (p / "cases_index").exists()), _eroot())
AUDIT = ROOT / "output" / "audit"
ORDERS_DIR = AUDIT / "order_indexes"
CASES_OBS = ROOT / "obsidian_sync" / "cases"

_LEGAL = re.compile(r"\b(damages|money|monetary|debt|restitution|penalt|statutory damages|"
                    r"compensat|actual damages|punitive|judgment for a sum|fees and costs)\b", re.I)
_EQUITABLE = re.compile(r"\b(injunction|injunctive|specific performance|quiet title|rescission|"
                        r"rescind|cancellation|cancel the|accounting|constructive trust|reformation|"
                        r"set aside|declaratory relief|disgorge)\b", re.I)
_MARITIME = re.compile(r"\b(bill of lading|maritime|admiralty|vessel|charter party|cargo|"
                       r"carriage of goods|salvage|general average)\b", re.I)

# SWITCH signals — only fire on a genuine attempt (by court/opposing) to move a legal claim OUT of law
# or to defeat suit by mode/immunity. NOT the bare word "equitable" (which appears in captions/denials).
_SW_RECHAR = re.compile(
    # a recharacterization/classification verb that TARGETS equity/admiralty (not a bare "recharacterize")
    r"(recharacteriz\w+|reclassif\w+|construe[d]?|convert\w*|treat\w*)[^.\n]{0,35}"
    r"(equitab\w*|in equity|admiralt\w*|maritime)|"
    # OR the claim itself is asserted to lie/sound in equity or admiralty (not at law)
    r"(claims?|actions?|counts?|matter)[^.\n]{0,45}(sounds? in|lies? in|is (properly )?|are (properly )?|"
    r"in nature[^.\n]{0,6})[^.\n]{0,10}(equit\w+|admiralt\w+)|"
    r"(equitab\w*|admiralt\w*)[^.\n]{0,20}(in nature|not (a |an )?legal|rather than (at )?law)", re.I)
_SW_JURYSTRIP = re.compile(
    r"(no|not|denied?|deny|strik\w+|improper|waiv\w+|preclud\w+)[^.\n]{0,28}(right to a? ?)?jury[ _]?(trial|demand)?|"
    r"jury[ _]?(trial|demand)?[^.\n]{0,28}(is |be )?(improper|struck|stricken|denied|waived|"
    r"not (available|permitted|warranted|entitled))", re.I)
_SW_EQUITY_JX = re.compile(
    r"(this )?(court|matter)[^.\n]{0,20}sitting in equity|invok\w+[^.\n]{0,18}equit\w+ jurisdiction|"
    r"no adequate remedy at law|proceed(s|ing)? in equity|solely (an )?equitable claim", re.I)
_SW_ADMIRALTY = re.compile(
    r"(admiralty|maritime) jurisdiction|proceed(s|ing)? in admiralty|in rem\b|libel in|"
    r"supplemental rule [a-e]\b", re.I)
_SW_IMMUNITY = re.compile(
    r"(judicial|absolute|sovereign|quasi-judicial|qualified|eleventh amendment)[^.\n]{0,18}immun\w+|"
    r"immune from (suit|liability|damages)|barred by[^.\n]{0,20}immunity", re.I)
# a court/officer TRANSACTING COMMERCE (Clearfield/Planters -> descends to a private citizen; no immunity for that act)
_COMMERCIAL_ACT = re.compile(
    r"(filing|docket\w*|court|user|convenience)[ _-]?fee|fee[s]? (were |was )?(collect|paid|assess|charg|requir)|"
    r"cost[s]? (assessed|collected|taxed|imposed)|in forma pauperis[^.\n]{0,20}(denied|fee|pay)|"
    r"pay\w*[^.\n]{0,15}fee|remit\w*[^.\n]{0,15}(payment|fee)", re.I)

# proof-of-facts scaffold: each switch = LAW element -> RECORD conduct -> VIOLATION -> LEVER
_PROOF = {
    "claim-RECHARACTERIZED": {
        "rule": "A claim for damages is a claim AT LAW; its legal character is fixed by the relief sought "
                "and cannot be re-labeled 'equitable' to defeat the jury (%s).",
        "violation": "opposing/court recharacterized a legal (damages) claim as equitable/admiralty",
        "lever": "The recharacterization is improper; the claim remains at law and the 7th Amendment jury "
                 "attaches. Demand the jury (Fed.R.Civ.P. 38, or 39(b))."},
    "JURY-stripped": {
        "rule": "The 7th Amendment right to a jury on a legal claim cannot be denied, struck, or waived "
                "away by the court/opposing over the litigant's demand (%s).",
        "violation": "opposing/court moved to deny, strike, or preclude the jury on a legal claim",
        "lever": "Preserve and re-assert the demand (39(b)); denial of a jury on a legal claim is "
                 "reversible constitutional error."},
    "EQUITY-jurisdiction": {
        "rule": "Equity has jurisdiction ONLY where there is no plain, adequate, and complete remedy at "
                "law; it cannot be invoked where damages suffice (%s).",
        "violation": "opposing/court invoked equity jurisdiction while an adequate legal remedy exists",
        "lever": "Show the adequate remedy at law (damages) — equity has no jurisdiction; back to law + jury."},
    "ADMIRALTY-invoked": {
        "rule": "Admiralty jurisdiction requires a maritime nexus; the saving-to-suitors clause preserves "
                "the litigant's option to proceed at law with a common-law remedy (%s).",
        "violation": "opposing/court invoked admiralty",
        "lever": "Invoke saving-to-suitors (28 U.S.C. 1333) to keep the law-side remedy and the jury; if no "
                 "maritime nexus, admiralty does not lie at all."},
    "IMMUNITY-asserted": {
        "rule": "Immunity protects only a JUDICIAL act taken within jurisdiction; it does not shield a "
                "non-judicial/ministerial/commercial act or an act in clear absence of jurisdiction (%s).",
        "violation": "opposing/court asserted immunity as a bar to suit",
        "lever": "Rebut: identify the non-judicial/ministerial/commercial character of the act or the "
                 "absence of jurisdiction — immunity does not attach."},
}
_PROOF_CITE = {
    "claim-RECHARACTERIZED": "%s; %s", "JURY-stripped": "%s",
    "EQUITY-jurisdiction": "%s", "ADMIRALTY-invoked": "%s", "IMMUNITY-asserted": "%s; %s",
}

CITES = {
    "law_jury": "Ransom v. Staso Milling, 2 F.R.D. 128; Fed.R.Civ.P. 38(a), 39(b)",
    "equity_boundary": "Story/Pomeroy Eq. Jur. — equity only where no adequate remedy at law",
    "admiralty_saving": "28 U.S.C. 1333(1) saving-to-suitors clause",
    "immunity": "Am. Jur. 2d Judges s 61 — immunity needs jurisdiction + a JUDICIAL act",
    "commercial_descends": "Clearfield Trust v. United States; Bank of U.S. v. Planters' Bank",
    "supreme": "Am. Jur. 2d Constitutions s 46; U.S. Const. amend. VII",
    # accountability — the routes immunity does NOT block (the shield's edges)
    "imm_damages_only": "Am. Jur. 2d Judges s 61 — immunity bars MONEY DAMAGES for judicial acts only",
    "imm_discipline": "28 U.S.C. 351 (Judicial Conduct & Disability Act) — any person may file a "
                      "misconduct complaint; it is oversight, not a damages suit, so immunity is no bar",
    "imm_prospective": "Ex parte Young; Pulliam v. Allen — immunity does not bar prospective "
                       "injunctive/declaratory relief",
    "imm_recusal": "28 U.S.C. 455 — disqualification",
    "ministerial": "Forrester v. White, 484 U.S. 219; Stump v. Sparkman — ministerial/non-judicial acts "
                   "and acts in clear absence of jurisdiction are not immune",
    "counsel_no_immunity": "Am. Jur. 2d Attorneys at Law (discipline for dishonesty/false filings); "
                           "Fed. R. Civ. P. 11 — opposing counsel has NO immunity",
}

# Accountability routes that a shield of immunity does NOT block (bad actors don't get to ignore the rules).
_ROUTE_JUDGE = (
    "Immunity (if valid) bars ONLY money damages for a JUDICIAL act [%s]; it does NOT block accountability: "
    "(1) JUDICIAL-CONDUCT COMPLAINT — Third Circuit Judicial Council, form AO 310 [%s]; "
    "(2) RECUSAL/disqualification [%s]; (3) APPEAL or MANDAMUS of the ruling itself; "
    "(4) PROSPECTIVE injunctive/declaratory relief [%s]. And immunity does not even bar DAMAGES where the act "
    "was NON-judicial / ministerial / in clear absence of jurisdiction [%s], OR was COMMERCIAL: when a court "
    "or officer TRANSACTS COMMERCE (collects/administers fees, does business), it 'descends to the level of a "
    "private citizen' and 'divests itself of its sovereign character,' so that commercial act is not a "
    "protected judicial act [%s]."
) % (CITES["imm_damages_only"], CITES["imm_discipline"], CITES["imm_recusal"],
     CITES["imm_prospective"], CITES["ministerial"], CITES["commercial_descends"])
_ROUTE_CLERK = (
    "A clerk's filing duty is MINISTERIAL — no judicial immunity [%s]. Route: MANDAMUS to compel the "
    "filing + damages for the ministerial breach + a misconduct complaint."
) % CITES["ministerial"]
_ROUTE_COUNSEL = (
    "Opposing counsel has NO immunity [%s]. Route: attorney DISCIPLINARY board (PA ODC / state bar) + "
    "Fed. R. Civ. P. 11 sanctions; if a government attorney, DOJ Office of Professional Responsibility."
) % CITES["counsel_no_immunity"]


_AFFIDAVIT = re.compile(r"affidavit|declaration|declared under penalt|verif\w+|under penalty of perjury|"
                        r"sworn (to|statement|affidavit)|personal knowledge|attest\w*|"
                        r"certif\w+ under penalt|1746", re.I)
_LEGAL_TOPIC = {"authority_cited", "legal_standard", "rule", "argument", "case_law", "citation",
                "legal_argument", "standard", "law"}


def counsel_fact_gaps(data: dict) -> list:
    """COUNSEL_FACT_WITHOUT_KNOWLEDGE — opposing filings that assert FACTS with no supporting affidavit /
    verification on personal knowledge. Statements of counsel are not evidence (3 Am. Jur. 2d Affidavits
    s 14); such assertions are a nullity for factual purposes and, if asserted as first-hand and untrue,
    are misrepresentation (37 Am. Jur. 2d Fraud s 29). Candidate flag — verify against the actual filing."""
    from collections import defaultdict
    by_doc = defaultdict(lambda: {"facts": [], "affid": False})
    for f in (data.get("opposing_facts") or []):
        if not isinstance(f, dict):
            continue
        doc = f.get("source_doc") or "?"
        topic = (f.get("topic") or "").lower()
        text = f.get("text") or ""
        if _AFFIDAVIT.search(text):
            by_doc[doc]["affid"] = True
        if text and topic not in _LEGAL_TOPIC and "author" not in topic and "cit" not in topic:
            by_doc[doc]["facts"].append({"topic": topic, "text": text[:130]})
    gaps = []
    for doc, d in by_doc.items():
        if d["facts"] and not d["affid"]:
            gaps.append({
                "document": doc[:60], "factual_assertions": len(d["facts"]), "sample": d["facts"][:3],
                "proof": ("LAW: statements of counsel are not evidence; a factual assertion requires an "
                          "affidavit/verification on PERSONAL KNOWLEDGE [3 Am. Jur. 2d Affidavits s 14]. "
                          "RECORD: '%s' asserts %d fact(s) with NO supporting affidavit/verification of a "
                          "person with knowledge. THEREFORE: no evidentiary support -- a nullity for factual "
                          "purposes; if asserted as first-hand and untrue, negligent/fraudulent "
                          "misrepresentation [37 Am. Jur. 2d Fraud s 29]. LEVER: strike/disregard; the motion "
                          "cannot stand on it; Fed. R. Civ. P. 11 + disciplinary referral."
                          % (doc[:40], len(d["facts"]))),
            })
    return gaps


def _remedy_text(claim: dict) -> str:
    return " ".join(str(claim.get(k, "")) for k in
                    ("relief", "remedy", "prayer", "claim_type", "category", "duty_type",
                     "breach", "why", "description", "violation_type"))


def classify_mode(claim: dict) -> dict:
    t = _remedy_text(claim)
    legal, equit, marit = bool(_LEGAL.search(t)), bool(_EQUITABLE.search(t)), bool(_MARITIME.search(t))
    if marit:
        return {"mode": "ADMIRALTY", "jury": False, "cite": CITES["admiralty_saving"],
                "note": "maritime nexus; NO jury in admiralty, but saving-to-suitors preserves the law option."}
    if equit and not legal:
        return {"mode": "EQUITY", "jury": False, "cite": CITES["equity_boundary"],
                "note": "equitable remedy; NO jury, clean-hands maxims; equity has jurisdiction ONLY if "
                        "no adequate remedy at law."}
    return {"mode": "LAW", "jury": True, "cite": CITES["law_jury"],
            "note": "legal claim; 7th Amendment jury AVAILABLE if timely demanded; not demanding only "
                    "waives the jury, it does not forfeit common law."}


def _load_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _obs_slug(slug: str) -> str:
    # audit "fed_2_26-cv-00123" -> obsidian "fed_26-cv-00123"; leave others alone
    return re.sub(r"^fed_\d+_(\d+-cv-\d+)$", r"fed_\1", slug)


_COMPANY = re.compile(r"\b(LLC|L\.L\.C|N\.?A\.?|Inc\b|Incorporated|Corp|Corporation|Company|Co\.|Bank|"
                      r"Services|Servicing|Insurance|Mortgage|Financial|Esquire|Esq\.|Attorney|"
                      r"P\.C\.|LLP|Group)\b", re.I)


def _filer_source(filer: str, judge_names) -> str:
    """Attribute a docket filer to COURT (judge/clerk), OPPOSING (defendant/counsel), or SELF (the pro-se
    litigant or unattributed). Data-driven — the litigant is not named; anything that isn't a known judge
    or a company/counsel is treated as SELF and excluded from the switch scan."""
    f = (filer or "").lower()
    if not f:
        return "SELF"
    if any(j and j.lower() in f for j in judge_names) or ", j." in f or "uscourts.gov" in f or "clerk" in f:
        return "COURT"
    if _COMPANY.search(filer or ""):
        return "OPPOSING"
    return "SELF"


_SIG_CITES = {
    "claim-RECHARACTERIZED": (CITES["supreme"], CITES["law_jury"]),
    "JURY-stripped": (CITES["law_jury"],),
    "EQUITY-jurisdiction": (CITES["equity_boundary"],),
    "ADMIRALTY-invoked": (CITES["admiralty_saving"],),
    "IMMUNITY-asserted": (CITES["immunity"], CITES["commercial_descends"]),
}


def _scan(text: str, source: str):
    """Return PROOF-structured findings: each is rule (law) -> conduct (record) -> violation -> lever."""
    hits = []
    for label, rx in (("claim-RECHARACTERIZED", _SW_RECHAR), ("JURY-stripped", _SW_JURYSTRIP),
                      ("EQUITY-jurisdiction", _SW_EQUITY_JX), ("ADMIRALTY-invoked", _SW_ADMIRALTY),
                      ("IMMUNITY-asserted", _SW_IMMUNITY)):
        m = rx.search(text or "")
        if not m:
            continue
        frag = re.sub(r"\s+", " ", text[max(0, m.start() - 45): m.end() + 70])
        p = _PROOF[label]
        rule = p["rule"] % (_PROOF_CITE[label] % _SIG_CITES[label])
        hits.append({"signal": label, "source": source, "conduct_excerpt": frag[:160],
                     "rule": rule, "violation": p["violation"], "lever": p["lever"]})
    return hits


def analyze(slug: str) -> dict:
    data = _load_json(AUDIT / ("data_%s.json" % slug)) or {}
    orders = _load_json(ORDERS_DIR / ("orders_%s.json" % slug)) or {}
    claims = (data.get("claims_found") or data.get("duties_found")
              or (data.get("scorecard") or {}).get("violations")
              or data.get("procedural_violations") or [])

    # 1) proper mode per claim
    classified, any_legal = [], False
    for c in (claims if isinstance(claims, list) else []):
        cc = c if isinstance(c, dict) else {"description": str(c)}
        m = classify_mode(cc)
        classified.append({"claim": (cc.get("category") or cc.get("duty_type")
                                     or cc.get("violation_type") or "claim"),
                           "proper_mode": m["mode"], "jury": m["jury"]})
        any_legal = any_legal or m["mode"] == "LAW"

    # 2) scan the ORDERS/docket — but ONLY entries filed by the COURT (judge) or OPPOSING counsel.
    #    The switch is something pulled AGAINST the litigant; the litigant's own filings are excluded.
    jo = data.get("judicial_officers") or {}
    judge_names = [str(off.get("officer") or off.get("named_in_record") or "")
                   for off in (jo.get("officers") or [])]
    order_findings, order_list = [], (orders.get("orders") if isinstance(orders, dict) else orders) or []
    scanned_court = scanned_opp = skipped_self = 0
    for o in (order_list if isinstance(order_list, list) else []):
        filer = (o.get("filer") if isinstance(o, dict) else "") or ""
        src = _filer_source(filer, judge_names)
        if src == "SELF":
            skipped_self += 1
            continue                                  # the litigant's OWN filing — never a switch against them
        if src == "COURT":
            scanned_court += 1
        else:
            scanned_opp += 1
        blob = json.dumps(o) if isinstance(o, (dict, list)) else str(o)
        doc = (o.get("doc_id") if isinstance(o, dict) else "") or "order"
        for h in _scan(blob, "%s[%s]:%s" % (src, filer[:22], doc[:34])):
            h["by"] = src
            order_findings.append(h)

    # 3) scan OPPOSING drafts
    opp_findings = []
    for f in (data.get("opposing_facts") or []):
        txt = f.get("text", "") if isinstance(f, dict) else str(f)
        for h in _scan(txt, "OPPOSING:%s" % (f.get("source_doc", "") if isinstance(f, dict) else "")[:30]):
            opp_findings.append(h)
    misc = data.get("opposing_counsel_misconduct") or {}
    for att, findings in (misc.get("by_attorney") or {}).items():
        for fn in (findings or []):
            for h in _scan(json.dumps(fn), "OPP-MISCONDUCT:%s" % att[:20]):
                opp_findings.append(h)

    # 4) immunity from the REAL record
    immunity = []
    jo = data.get("judicial_officers") or {}
    posture = data.get("litigation_posture") or {}
    void_orders = posture.get("contested_void_orders")
    clerk_nonfiling = posture.get("clerk_nonfiling")
    accountability = []
    for off in (jo.get("officers") or []):
        immunity.append({
            "officer": off.get("officer") or off.get("named_in_record"),
            "classification": off.get("classification"),
            "test": "Immunity needs jurisdiction over the subject matter AND a JUDICIAL act. [%s]" % CITES["immunity"],
            "accountability_route": _ROUTE_JUDGE,
        })
    if void_orders:
        immunity.append({"officer": "(order signer)", "classification": "n/a",
                         "flag": "Record contains CONTESTED VOID ORDER(S) -> an order entered in clear "
                                 "absence of jurisdiction is not a protected judicial act; immunity fails "
                                 "for it (damages available). [%s]" % CITES["ministerial"],
                         "record": str(void_orders)[:160], "accountability_route": _ROUTE_JUDGE})
    if clerk_nonfiling:
        immunity.append({"officer": "(clerk)", "classification": "ministerial",
                         "flag": "Record shows CLERK NON-FILING -> a clerk's filing duty is MINISTERIAL, "
                                 "not judicial; no judicial immunity attaches to it. [%s]" % CITES["ministerial"],
                         "record": str(clerk_nonfiling)[:160], "accountability_route": _ROUTE_CLERK})
    # COMMERCIAL path — a court transacting commerce (fee assessment/collection) descends to a private citizen
    _cblob = (json.dumps(order_list)[:300000] + " " + json.dumps(posture))
    _cm = _COMMERCIAL_ACT.search(_cblob)
    if _cm:
        immunity.append({"officer": "(court — commercial capacity)", "classification": "commercial",
                         "flag": "Record shows the court TRANSACTING COMMERCE (fee assessment/collection) -> "
                                 "when a court/officer transacts commerce it 'descends to the level of a "
                                 "private citizen' and 'divests itself of its sovereign character'; that "
                                 "COMMERCIAL act is not a protected judicial act and carries no immunity. [%s]"
                                 % CITES["commercial_descends"],
                         "record": re.sub(r"\s+", " ", _cblob[max(0, _cm.start() - 25): _cm.end() + 40])[:120],
                         "accountability_route": _ROUTE_JUDGE})
        accountability.append({"actor": "court (commercial capacity)", "actor_type": "court/commercial",
                               "route": _ROUTE_JUDGE})

    # court actors -> accountability route despite immunity
    for off in (jo.get("officers") or []):
        accountability.append({"actor": off.get("officer") or off.get("named_in_record"),
                               "actor_type": "court/judge", "route": _ROUTE_JUDGE})
    if clerk_nonfiling:
        accountability.append({"actor": "clerk", "actor_type": "court/clerk", "route": _ROUTE_CLERK})
    # opposing counsel -> NO immunity; disciplinary + Rule 11 route, carrying the misconduct already found
    misc = data.get("opposing_counsel_misconduct") or {}
    for att, findings in (misc.get("by_attorney") or {}).items():
        kinds = sorted({(fn.get("detected_as") or fn.get("rule_breached") or "misconduct")
                        for fn in (findings or []) if isinstance(fn, dict)})
        accountability.append({"actor": att, "actor_type": "opposing_counsel",
                               "misconduct_count": len(findings or []), "misconduct_kinds": list(kinds)[:6],
                               "route": _ROUTE_COUNSEL})

    # 5) posture / history context
    jx = data.get("_jx_summary") or {}
    facts_p = CASES_OBS / _obs_slug(slug) / "CASE_FACTS.md"
    timeline_p = CASES_OBS / _obs_slug(slug) / "CASE_TIMELINE.json"
    posture_ctx = {"removed": jx.get("removed"), "federal_claim_on_face": jx.get("federal_claim_on_face"),
                   "case_facts_read": facts_p.exists(), "timeline_read": timeline_p.exists()}

    # 6) SWITCHES as proofs — law -> record conduct -> violation -> lever (only court/opposing conduct)
    switches, seen = [], set()
    for h in (order_findings + opp_findings):
        key = (h["signal"], h["source"][:24])
        if key in seen:
            continue
        seen.add(key)
        switches.append(
            "PROOF [%s] by %s\n  LAW: %s\n  RECORD (%s): \"%s\"\n  THEREFORE: %s\n  LEVER: %s"
            % (h["signal"], h.get("by", "OPPOSING"), h["rule"], h["source"], h["conduct_excerpt"],
               h["violation"], h["lever"]))

    # 7) PERSONA / STATUS — which kind of person the record treats the litigant as (S-capacity-status).
    #    Additive; its verified switch proofs join improper_switches so m0 drafting carries them.
    try:
        from pipeline.persona_status import analyze as _persona
        persona = _persona(slug, data)
        switches.extend(persona.get("switch_proofs") or [])
    except Exception as _pe:                      # never break the audit; report the miss instead
        persona = {"error": "persona_status skipped: %s" % _pe}

    levers = []
    if any_legal:
        levers.append("Legal claims present -> demand the 7th Amendment JURY (Fed.R.Civ.P. 38, or 39(b) "
                      "if the deadline passed). [%s]" % CITES["law_jury"])

    return {
        "modes_by_claim": classified,
        "record_read": {"docket_entries": len(order_list) if isinstance(order_list, list) else 0,
                        "scanned_court_orders": scanned_court, "scanned_opposing": scanned_opp,
                        "skipped_own_filings": skipped_self,
                        "opposing_facts_scanned": len(data.get("opposing_facts") or []),
                        "judicial_officers": len(jo.get("officers") or []),
                        "posture": posture_ctx},
        "order_switch_findings": order_findings,
        "opposing_switch_findings": opp_findings,
        "improper_switches": switches,
        "persona_status": persona,
        "immunity_analysis": immunity,
        "accountability": accountability,
        "counsel_evidentiary_gaps": counsel_fact_gaps(data),
        "levers": levers,
        "constitution": "The Constitution (incl. the 7th Amendment jury right) sits atop every mode; no "
                        "court may use equity/admiralty labeling to circumvent it. [%s]" % CITES["supreme"],
    }


def write_audit(slug: str) -> dict:
    fp = AUDIT / ("data_%s.json" % slug)
    if not fp.exists():
        return {}
    data = json.loads(fp.read_text(encoding="utf-8"))
    data["jurisdiction_mode"] = analyze(slug)
    fp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data["jurisdiction_mode"]


if __name__ == "__main__":
    import pprint
    s = next((a for a in sys.argv[1:] if not a.startswith("-")), "fed_2_26-cv-00123")
    pprint.pprint(analyze(s))
