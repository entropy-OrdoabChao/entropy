"""judicial_oath_breach — EXPOSE the breach. Maps the violations the audit already detected to the
SPECIFIC oath duty the presiding judge swore and broke, with the record cite and the authority/
evidence ignored. Produces a record-ready Statement of Judicial Oath Breach per judge — usable as
the factual predicate for a 28 U.S.C. 455 recusal, a 28 U.S.C. 351 misconduct complaint, or to
preserve the error for appeal. Deterministic (R7): logic maps detected facts -> sworn duty; it does
not invent misconduct, only exposes what the audit found against what the judge swore.

  python -m pipeline.judicial_oath_breach            # all cases
  python -m pipeline.judicial_oath_breach fed:2:26-cv-00456
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import json, re, sys
from pathlib import Path

ROOT = next((p for p in (_eroot(), _eroot()) if (p / "cases_index").exists()),
            _eroot())
AUDIT = ROOT / "output" / "audit"
OUT = ROOT / "output" / "oath_breach"

OATH_453 = ('"I will administer justice without respect to persons, and do equal right to the poor '
            'and to the rich, and ... faithfully and impartially discharge and perform all the duties '
            'incumbent upon me ... under the Constitution and laws of the United States." — 28 U.S.C. § 453')
OATH_PA = ('"I will support, obey and defend the Constitution of the United States and the Constitution '
           'of this Commonwealth and ... discharge the duties of my office with fidelity." — Pa. Const. '
           'art. VI § 3')

# detected violation (category / violation_type / dismissal summary keyword) -> sworn duty breached
# + the accountability vehicle. Matched by keyword so new categories fall through to a generic duty.
BREACH_MAP = [
    (r"NON_BINDING_AS_CONTROLLING|TRIAL_COURT_AS_APPELLATE|STARE_DECISIS",
     "the duty to decide UNDER THE CONSTITUTION AND LAWS — i.e., to follow binding precedent",
     "Applied non-binding / wrong-court authority as controlling, contrary to stare decisis.",
     ["28 U.S.C. § 455 (impartiality)", "appeal — error of law preserved"]),
    (r"PRIMA_FACIE_EVIDENCE_IGNORED",
     "the duty to FAITHFULLY discharge — to accept well-pleaded facts as true and weigh the record",
     "Ignored prima facie evidence in the record that satisfied the elements.",
     ["appeal — Twombly/Iqbal accept-as-true", "28 U.S.C. § 351 (pattern)"]),
    (r"TWOMBLY_IQBAL_MISAPPLICATION",
     "the duty to apply the correct Rule 12(b)(6) standard under the law",
     "Misapplied Twombly/Iqbal to dismiss claims that were adequately pleaded.",
     ["appeal — de novo legal error", "28 U.S.C. § 455"]),
    (r"CONCLUSORY",
     "the duty to FAITHFULLY discharge — to give reasoned analysis of each element",
     "Entered a CONCLUSORY dismissal without element-by-element analysis.",
     ["appeal — reversible error", "28 U.S.C. § 351"]),
    (r"BIAS|PARTIAL|DISQUALIFICATION",
     "the duty to act WITHOUT RESPECT TO PERSONS and IMPARTIALLY",
     "Record shows bias / partiality indicators.",
     ["28 U.S.C. § 455(a) — impartiality reasonably questioned", "28 U.S.C. § 351"]),
    (r"DUE_PROCESS|NOTICE_INADEQUATE|SERVICE_DEFECTIVE|DEPRIVATION_OF_RIGHTS",
     "the duty to decide UNDER THE CONSTITUTION — due process",
     "Procedural due-process / notice defect not cured.",
     ["28 U.S.C. § 455", "appeal — constitutional error", "28 U.S.C. § 351"]),
    (r"VOID_JUDGMENT|RULE_60B4|SUBJECT_MATTER_JURISDICTION",
     "the duty to act only WITHIN lawful authority (under the Constitution and laws)",
     "Order issued without / in excess of jurisdiction — void (Rule 60(b)(4)).",
     ["Fed. R. Civ. P. 60(b)(4) — void judgment", "appeal"]),
]


def _audit(case_id):
    for p in AUDIT.glob("data_*.json"):
        if any(d in p.stem for d in re.findall(r"\d{4,}", case_id)):
            try:
                return json.loads(p.read_text(encoding="utf-8")), p
            except Exception:
                return {}, p
    return {}, None


# A litigant's draft is TITLED with its type ("Motion…", "Opposition…", "Proposed Order…") or is a
# .docx. A judge's order may MENTION "motion" mid-title ("Judge rejected motion to vacate…") — so the
# litigant patterns are anchored to the START of the filename and the unanchored hits are .docx /
# "proposed order" / correspondence, which a court order never is.
_LITIGANT_DOC = re.compile(
    r"\.docx$|proposed\s*ord|trackletter|gmail|fax\s*template|"
    r"^\s*(motion|opposition|reply|rebuttal|response|brief|memorandum|declaration|affidavit|"
    r"petition|notice|conditional|unified|consolidated|verified|supplement|exhibit|statement)",
    re.I)


def _ecfnorm(x):
    m = re.search(r"\d+", str(x or ""))
    return m.group(0) if m else None


def _judge_orders(case_id: str, judge: str):
    """The ECF numbers + doc basenames of orders the JUDGE actually authored (order_index filer ==
    the judge). Only violations tied to these are the judge's oath breach; party filings are not."""
    surname = (judge or "").strip().split()[-1].lower() if judge else ""
    ecfs, docs = set(), set()
    oidir = AUDIT / "order_indexes"
    of = next((p for p in oidir.glob("orders_*.json")
               if any(dd in p.stem for dd in re.findall(r"\d{4,}", case_id))), None) if oidir.exists() else None
    if of:
        try:
            data = json.loads(of.read_text(encoding="utf-8"))
            orders = data if isinstance(data, list) else (
                data.get("orders") or next((v for v in data.values() if isinstance(v, list)), []))
            for o in orders if isinstance(orders, list) else []:
                if isinstance(o, dict) and surname and surname in (o.get("filer", "").lower()):
                    if _ecfnorm(o.get("ecf_number")):
                        ecfs.add(_ecfnorm(o.get("ecf_number")))
                    if o.get("doc_id"):
                        docs.add(o["doc_id"].lower())
        except Exception:
            pass
    return ecfs, docs


def _map(label: str):
    for pat, duty, did, vehicles in BREACH_MAP:
        if re.search(pat, label or "", re.I):
            return duty, did, vehicles
    return ("the duty to faithfully and impartially discharge the office under the Constitution and laws",
            f"Detected violation: {label}.", ["appeal", "28 U.S.C. § 455 / § 351"])


def build_breach_record(case_id: str, write: bool = True) -> dict:
    d, ap = _audit(case_id)
    jo = d.get("judicial_officers") or {}
    officer = next((o for o in jo.get("officers", []) if o.get("verified")), None)
    judge = (officer or {}).get("officer") or (jo.get("officers", [{}])[0] or {}).get("named_in_record") or "the presiding judge"
    is_state = (officer or {}).get("classification", "").lower().find("state") >= 0 or "phila" in case_id.lower()
    oath = OATH_PA if is_state else OATH_453

    findings, seen = [], set()
    judge_ecfs, judge_docs = _judge_orders(case_id, judge)
    jsur = (judge or "").strip().split()[-1].lower() if judge else ""

    def _judge_authored(v, src_key):
        """Is this violation in the JUDGE'S OWN order? Returns 'judge' | 'party' | 'unverified'.
        Authoritative = the order index (filer==judge, by ECF/doc). The attribution engine's
        document_role is a SECONDARY signal but mis-tags litigant drafts (a 'motion to vacate' /
        'proposed order' .docx as judicial_output), so a judicial_output tag is trusted ONLY when
        the source is NOT a litigant-draft filename. A court order is an ECF-stamped PDF, not a
        .docx motion/brief."""
        ecf = _ecfnorm(v.get("source_ecf"))
        fname = Path(v.get(src_key) or "").name
        # 0. litigant-draft filename (a "proposed order", motion, brief, opposition, .docx, etc.) is
        #    NEVER the judge's order — even if the ECF stamper mis-stamped it OR the order index
        #    wrongly listed it (the order engine tags a litigant's proposed order with the judge's
        #    name because that doc names the judge). The filename wins.
        if _LITIGANT_DOC.search(fname):
            return "party"
        # 1. authoritative: the order index says the JUDGE filed this ECF / document
        if (ecf and ecf in judge_ecfs) or (fname.lower() in judge_docs):
            return "judge"
        role = (v.get("document_role") or "").lower()
        actor = (v.get("attributed_actor") or "").lower()
        # 2. explicit party / plaintiff attribution
        if role in ("pro_se_plaintiff_filing", "plaintiff", "opposing_party_filing", "opposing_counsel"):
            return "party"
        # 3. judicial_output — but reject misclassified litigant drafts by filename
        if role in ("judicial_output", "judge") or (jsur and jsur in actor and "judge" in actor):
            return "party" if _LITIGANT_DOC.search(fname) else "judge"
        return "unverified"

    def add(label, cite, detail_extra=""):
        duty, did, vehicles = _map(label)
        key = (duty, label.split()[0] if label else "")
        if key in seen:
            return
        seen.add(key)
        findings.append({"sworn_duty": duty, "what_the_judge_did": did + (f" {detail_extra}" if detail_extra else ""),
                         "detected_as": label, "record_cite": cite, "accountability_vehicles": vehicles})

    # Attribute to the JUDGE only what the judge authored. Party filings -> excluded (separate track).
    # Unresolved source -> counted as 'unverified' (honest), not blamed on the judge.
    skipped_party = skipped_unverified = 0
    for v in (d.get("scorecard") or {}).get("violations", []) or []:
        who = _judge_authored(v, "source_file")
        if who == "party":
            skipped_party += 1; continue
        if who == "unverified":
            skipped_unverified += 1; continue
        add(v.get("category") or "", f"ECF No. {v.get('source_ecf')}" if v.get("source_ecf")
            else (Path(v.get("source_file") or "").name or "(record)"))
    for v in d.get("procedural_violations") or []:
        who = _judge_authored(v, "file")
        if who == "party":
            skipped_party += 1; continue
        if who == "unverified":
            skipped_unverified += 1; continue
        cnt = v.get("count")
        add(v.get("violation_type") or "", f"ECF No. {v.get('source_ecf')}" if v.get("source_ecf")
            else (Path(v.get("file") or "").name or "(record)"),
            f"(detected {cnt}x)" if cnt and cnt > 1 else "")
    # dismissal reversibility — only when the dismissal doc is the judge's order
    for x in d.get("dismissal") or []:
        if Path(x.get("source", "")).name.lower() not in judge_docs:
            continue
        s = (x.get("summary") or "")
        for kw in ("CONCLUSORY", "BIAS"):
            if kw.lower() in s.lower():
                add(kw, f"order: {Path(x.get('source','')).name} (reversibility {x.get('score')})", "")

    out = {"case_id": case_id, "judge": judge, "oath": oath, "is_state": is_state,
           "breach_count": len(findings), "breaches": findings,
           "judge_orders_identified": sorted(judge_ecfs),
           "party_filing_violations_excluded": skipped_party,
           "source_unverified_excluded": skipped_unverified}

    if write and findings:
        OUT.mkdir(parents=True, exist_ok=True)
        md = [f"# STATEMENT OF JUDICIAL OATH BREACH — {case_id}\n",
              f"**Presiding judge:** {judge}",
              f"**The oath sworn:** {oath}\n",
              "Each item below measures a violation the case audit detected against the specific sworn "
              "duty it breaches. These are the factual predicates for a motion to disqualify (28 U.S.C. "
              "§ 455), a judicial-misconduct complaint (28 U.S.C. § 351), and/or preserved error on appeal. "
              "Nothing here claims the court lacks authority over the litigant; it documents what the judge "
              "DID against what the judge SWORE.\n",
              "---\n"]
        for i, f in enumerate(findings, 1):
            md.append(f"## {i}. Breach of {f['sworn_duty']}")
            md.append(f"- **What the judge did:** {f['what_the_judge_did']}")
            md.append(f"- **Detected as:** `{f['detected_as']}`  |  **Record:** {f['record_cite']}")
            md.append(f"- **Vehicle:** {', '.join(f['accountability_vehicles'])}\n")
        slug = re.sub(r"[^a-z0-9]+", "_", case_id.lower()).strip("_")
        fp = OUT / f"oath_breach_{slug}.md"
        fp.write_text("\n".join(md), encoding="utf-8")
        out["_written_to"] = str(fp)
        # into audit + graph
        if ap:
            try:
                d["judicial_oath_breach"] = out
                ap.write_text(json.dumps(d, indent=2), encoding="utf-8")
            except Exception:
                pass
        try:
            from pipeline.connected_notes import write_note
            write_note("\n".join(md), tags=["oath-breach", "28 usc 455", "28 usc 351", "recusal",
                       "judicial-misconduct", case_id], source="judicial-oath-breach")
        except Exception:
            pass
    return out


# opposing-counsel misconduct: detected violation -> professional rule breached + vehicle
PRO_RULE_MAP = [
    (r"NON_BINDING_AS_CONTROLLING|TRIAL_COURT_AS_APPELLATE|STARE_DECISIS|FRIVOLOUS",
     "Fed. R. Civ. P. 11(b)(2) (claims warranted by existing law) + Pa.R.P.C. 3.1 (meritless contentions)"),
    (r"FRAUD_ON_THE_COURT|MISREPRESENT|FALSE",
     "Pa.R.P.C. 3.3 (candor toward the tribunal) + Fed. R. Civ. P. 11(b)(3) (factual contentions) + R.P.C. 8.4(c)"),
    (r"PRIMA_FACIE_EVIDENCE_IGNORED|TWOMBLY_IQBAL",
     "Fed. R. Civ. P. 11(b)(2) + Pa.R.P.C. 3.1 (misstating the 12(b)(6) standard)"),
    (r"SOVEREIGN_CITIZEN_LABEL|BIAS",
     "Pa.R.P.C. 3.3 / 8.4(d) (mischaracterizing a pro se litigant; conduct prejudicial)"),
    (r"VERIFICATION_MISSING|SERVICE_DEFECTIVE|FILING_DEADLINE",
     "Fed. R. Civ. P. 11(a)/(b) (signing/verification, procedural compliance)"),
    (r"SANCTIONS_TRIGGERED|OBSTRUCTION",
     "Fed. R. Civ. P. 11(c) (sanctions) + Pa.R.P.C. 8.4 (misconduct)"),
]


def _pro_rule(label):
    for pat, rule in PRO_RULE_MAP:
        if re.search(pat, label or "", re.I):
            return rule
    return "Fed. R. Civ. P. 11(b) + Pa.R.P.C. 3.1 / 8.4"


def build_opposing_counsel_record(case_id: str, write: bool = True) -> dict:
    """Route the violations attributed to OPPOSING COUNSEL into their own misconduct record — grouped
    by attorney/firm, mapped to the professional rule breached. Vehicle: Rule 11 motion for sanctions
    and/or a disciplinary (ODC / state bar) complaint — a DIFFERENT track from the judge's oath breach
    and from the litigant's own filings (which are excluded)."""
    d, ap = _audit(case_id)
    by_actor = {}
    for v in (d.get("procedural_violations") or []) + ((d.get("scorecard") or {}).get("violations", []) or []):
        role = (v.get("document_role") or "").lower()
        actor = v.get("attributed_actor") or ""
        # opposing counsel ONLY — not the judge, not the litigant, not unknown/unverified
        if role not in ("opposing_party_filing", "opposing_counsel") and "(for " not in actor:
            continue
        if "plaintiff" in actor.lower() or "judge" in actor.lower() or "unverified" in actor.lower():
            continue
        fn = Path(v.get("file") or v.get("source_file") or "").name
        # opposing counsel files via ECF/PDF; a .docx is the pro se litigant's own draft mis-tagged.
        if fn.lower().endswith(".docx"):
            continue
        label = v.get("violation_type") or v.get("category") or ""
        ecf = _ecfnorm(v.get("source_ecf"))
        cite = f"ECF No. {ecf}" if ecf else (Path(v.get("file") or v.get("source_file") or "").name or "(record)")
        by_actor.setdefault(actor, []).append({"detected_as": label, "rule_breached": _pro_rule(label), "record_cite": cite})
    out = {"case_id": case_id, "attorneys": len(by_actor),
           "total_findings": sum(len(x) for x in by_actor.values()), "by_attorney": by_actor}
    if write and by_actor:
        OUT.mkdir(parents=True, exist_ok=True)
        md = [f"# OPPOSING-COUNSEL MISCONDUCT RECORD — {case_id}\n",
              "Violations attributed by the audit to OPPOSING COUNSEL (not the judge, not the litigant). "
              "Vehicle: Fed. R. Civ. P. 11 motion for sanctions and/or a disciplinary complaint to the "
              "Office of Disciplinary Counsel / state bar. Separate track from the judicial oath-breach record.\n",
              "---\n"]
        for actor, items in by_actor.items():
            md.append(f"## {actor}")
            seen = set()
            for it in items:
                k = (it["detected_as"], it["record_cite"])
                if k in seen:
                    continue
                seen.add(k)
                md.append(f"- **{it['detected_as']}** — {it['record_cite']}  →  {it['rule_breached']}")
            md.append("")
        slug = re.sub(r"[^a-z0-9]+", "_", case_id.lower()).strip("_")
        fp = OUT / f"opposing_counsel_misconduct_{slug}.md"
        fp.write_text("\n".join(md), encoding="utf-8")
        out["_written_to"] = str(fp)
        if ap:
            try:
                d["opposing_counsel_misconduct"] = out
                ap.write_text(json.dumps(d, indent=2), encoding="utf-8")
            except Exception:
                pass
    return out


def _cases():
    try:
        return list(json.loads((ROOT / "cases_index" / "case_registry.json").read_text(encoding="utf-8")).keys())
    except Exception:
        return ["fed:2:26-cv-00123", "fed:2:26-cv-00456", "fed:2:26-cv-00789", "phila_ccp:260100000"]


if __name__ == "__main__":
    for cid in (sys.argv[1:] or _cases()):
        r = build_breach_record(cid, write=True)
        oc = build_opposing_counsel_record(cid, write=True)
        print(f"\n=== {cid} — {r.get('judge')} ===")
        print(f"  JUDGE oath breaches (in the judge's orders): {r.get('breach_count', 0)}"
              f"  -> {r.get('_written_to', '(none)')}")
        print(f"  OPPOSING-COUNSEL misconduct: {oc.get('total_findings', 0)} findings across "
              f"{oc.get('attorneys', 0)} attorney(s)  -> {oc.get('_written_to', '(none)')}")
