#!/usr/bin/env python3
"""
judicial_probe — comprehensive judicial-conduct investigation engine.

CRITICAL NOTE: AOUSC and the federal Judiciary are NOT subject to FOIA.
The correct mechanisms for accessing federal-judicial records are:
  - 5 U.S.C. App. § 105 written requests (financial disclosures)
  - 28 U.S.C. § 351 misconduct complaints (Judicial Council)
  - AOUSC public-access policy (informal requests)
  - PACER for case dockets

This module never instructs the user to file FOIA against AOUSC.
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = _eroot()
DB_PATH = ROOT / "output" / "audit" / "emma_intel.db"
DRAFTS_DIR = ROOT / "output" / "judicial_probe"


# ────────────────────────────────────────────────────────────────────────
# Static reference data
# ────────────────────────────────────────────────────────────────────────
AOUSC_FINANCIAL_DISCLOSURE = """\
Administrative Office of the United States Courts
Financial Disclosure Office
One Columbus Circle NE
Washington, DC 20544
"""

AOUSC_PUBLIC_ACCESS = """\
Administrative Office of the United States Courts
Office of Public Affairs
One Columbus Circle NE
Washington, DC 20544
"""

# Federal judicial Code of Conduct — canons most relevant to motion practice
CANONS = {
    "canon_1": {
        "title": "A judge should uphold the integrity and independence of the judiciary",
        "text": "An independent and honorable judiciary is indispensable to justice in our society. A judge should maintain and enforce high standards of conduct and should personally observe those standards.",
        "authority": "Code of Conduct for U.S. Judges, Canon 1",
    },
    "canon_2": {
        "title": "A judge should avoid impropriety and the appearance of impropriety",
        "text": "A judge should respect and comply with the law and should act at all times in a manner that promotes public confidence in the integrity and impartiality of the judiciary.",
        "authority": "Code of Conduct for U.S. Judges, Canon 2(A)",
        "violations": [
            "ruling on matter involving an entity with which the court has financial ties",
            "failure to disclose financial relationship to litigants",
            "social, financial, or professional contact with a party not disclosed",
        ],
    },
    "canon_3a5": {
        "title": "A judge should dispose promptly of the business of the court",
        "text": "A judge should dispose promptly of the business of the court.",
        "authority": "Code of Conduct for U.S. Judges, Canon 3(A)(5)",
        "violations": [
            "motion pending > 6 months without ruling (CJRA Six-Month List threshold)",
            "ruling delayed without explanation while case is otherwise active",
            "sua sponte action that prolongs case without legal basis",
        ],
    },
    "canon_3c": {
        "title": "Disqualification",
        "text": "A judge shall disqualify himself or herself in a proceeding in which the judge's impartiality might reasonably be questioned.",
        "authority": "Code of Conduct for U.S. Judges, Canon 3(C); 28 U.S.C. § 455(a)",
        "violations": [
            "failure to disqualify when financial interest exists, however small (§ 455(b)(4))",
            "failure to disclose grounds for potential disqualification",
            "ruling on motion to recuse on the same matter the judge ruled on (Liljeberg)",
        ],
    },
}

# Order-analysis checklist — what to look for in an order to flag violations
ORDER_RED_FLAGS = [
    ("delay_over_6_months",
     "Order disposes of a motion that was pending > 180 days",
     "Canon 3(A)(5); CJRA six-month list",
     ["pending since", "filed on", "submitted on"]),
    ("sua_sponte_no_notice",
     "Order acts sua sponte without prior notice to parties",
     "Procedural due process; Phillips v. County of Allegheny, 515 F.3d 224, 245 (3d Cir. 2008)",
     ["sua sponte", "on its own motion", "the Court hereby"]),
    ("no_reasons_stated",
     "Order contains no statement of legal reasoning",
     "28 U.S.C. § 1746; Fed. R. Civ. P. 52(a); due process",
     ["denied", "dismissed", "stricken"]),
    ("financial_interest_not_disclosed",
     "Court has financial relationship with party / custodian but did not disclose",
     "28 U.S.C. § 455(b)(4); Caperton, 556 U.S. at 884",
     ["Example Trust Co", "Wells Fargo", "U.S. Bank", "BNY Mellon", "State Street"]),
    ("ruling_in_face_of_recusal_motion",
     "Court ruled on substantive matter while a recusal motion was pending",
     "Liljeberg v. Health Services, 486 U.S. 847 (1988); 28 U.S.C. § 455(a)",
     ["recusal", "disqualification", "§ 455", "Caperton"]),
    ("extrinsic_evidence_unconverted",
     "Order relied on facts outside the pleadings without Rule 12(d) conversion notice",
     "Fed. R. Civ. P. 12(d); Pension Benefit Guar. Corp. v. White Consolidated Indus., 998 F.2d 1192 (3d Cir. 1993)",
     ["the Court takes notice", "the record reflects", "as documented"]),
    ("non_discretionary_remedy_denied",
     "Court denied a remedy that is non-discretionary as a matter of law",
     "Pa.R.C.P. 3144(a); 28 U.S.C. § 1651 (All Writs); Marbury v. Madison, 5 U.S. 137, 163 (1803)",
     ["dissolved", "writ of execution", "garnishment", "mandamus"]),
]


def _now(): return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def init_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS judicial_findings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id TEXT, judge TEXT, order_path TEXT,
            flag TEXT, severity TEXT,
            description TEXT, authority TEXT,
            evidence TEXT, ts TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_jf_judge ON judicial_findings(judge);
        CREATE INDEX IF NOT EXISTS idx_jf_case  ON judicial_findings(case_id);
        CREATE TABLE IF NOT EXISTS judicial_probes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT, target TEXT, draft_path TEXT,
            status TEXT, sent_date TEXT, ts TEXT
        );
    """)
    conn.commit()
    return conn


# ────────────────────────────────────────────────────────────────────────
# disclosure-request — 5 U.S.C. App. § 105 written request letter
# ────────────────────────────────────────────────────────────────────────
def cmd_disclosure_request(args):
    judge = args.judge.strip()
    years = args.years.split(",")
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    out = DRAFTS_DIR / (
        "disclosure_request_" +
        re.sub(r"[^a-z0-9]+", "_", judge.lower()).strip("_") +
        f"_{datetime.utcnow().strftime('%Y-%m-%d')}.md"
    )
    body = f"""\
[YOUR NAME]
[YOUR ADDRESS LINE 1]
[YOUR ADDRESS LINE 2]
{datetime.utcnow().strftime("%B %d, %Y")}

{AOUSC_FINANCIAL_DISCLOSURE}

RE: Request for Annual Financial Disclosure Reports
    Subject Judge: {judge}
    Years Requested: {", ".join(years)}
    Authority: 5 U.S.C. App. § 105 (Ethics in Government Act)

Dear Records Officer:

Pursuant to 5 U.S.C. App. § 105, which provides that annual financial
disclosure reports filed by federal judges shall be made available to
any person upon request, I respectfully request copies of the annual
financial disclosure reports filed by the above-named federal judge for
the following calendar/fiscal years: {", ".join(years)}.

I understand that under 5 U.S.C. App. § 105(b)(2), the Administrative
Office is required to make these reports available within thirty (30)
days of receipt of a written request, and that the Administrative
Office may charge a reasonable fee to cover the costs of reproduction.

I agree to pay reasonable reproduction fees. Please notify me in
advance if you anticipate fees exceeding $50.00.

If any portion of the requested disclosure has been redacted under
5 U.S.C. App. § 105(b)(3) (security review), please so indicate.

I am requesting these records in connection with a pending federal
litigation matter in which an analysis of potential financial nexus
under 28 U.S.C. § 455 and the Caperton v. A.T. Massey Coal Co., 556
U.S. 868 (2009), framework is necessary.

Please direct any questions or fee notices to me at the address above
or by email at [YOUR EMAIL]. I appreciate the Administrative Office's
prompt attention to this request.

Respectfully,


[YOUR SIGNATURE]
[YOUR PRINTED NAME]
[YOUR PHONE]

Enclosure: Self-Addressed Stamped Envelope (if applicable)

---

*Note: This is a pro se litigant's request. Service is by U.S. Mail
to the address shown. AOUSC is NOT subject to FOIA; this request is
made under the explicit public-access provision of 5 U.S.C. App. § 105.*
"""
    out.write_text(body, encoding="utf-8")
    print(f"  draft -> {out}")
    conn = init_db()
    conn.execute(
        "INSERT INTO judicial_probes (kind, target, draft_path, status, ts) "
        "VALUES (?, ?, ?, ?, ?)",
        ("disclosure_request_5usc_app_105", judge, str(out), "draft", _now()),
    )
    conn.commit()
    print(f"  fill in [YOUR NAME] / [YOUR ADDRESS] etc. and mail to AOUSC")
    print(f"  statutory response window: 30 days")
    return 0


# ────────────────────────────────────────────────────────────────────────
# cris-probe — AOUSC CRIS custodian disclosure request
# ────────────────────────────────────────────────────────────────────────
def cmd_cris_probe(args):
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    court = args.court or "the United States District Court for the Eastern District of Pennsylvania"
    out = DRAFTS_DIR / f"cris_probe_request_{datetime.utcnow().strftime('%Y-%m-%d')}.md"
    body = f"""\
[YOUR NAME]
[YOUR ADDRESS LINE 1]
[YOUR ADDRESS LINE 2]
{datetime.utcnow().strftime("%B %d, %Y")}

{AOUSC_PUBLIC_ACCESS}

RE: Public-Access Request — Court Registry Investment System (CRIS)
    Custodian and Sub-Custodian Disclosures
    Pertaining To: {court}

Dear Office of Public Affairs:

Pursuant to the public-access policies of the Administrative Office
of the United States Courts, and consistent with the Judiciary's
practice of making non-confidential operational records available
upon request, I respectfully request the following records:

  1. The current Memorandum of Understanding or service agreement
     between the Administrative Office and the financial institution
     designated as custodian of the Court Registry Investment System
     ("CRIS") under 28 U.S.C. § 2041, including any sub-custodian
     arrangements that involve the named court.

  2. The identity of the primary custodian and any sub-custodians
     servicing CRIS deposits originating from {court}, for the
     period 2020 to present.

  3. The annual fee schedule paid by the Judiciary IT Fund (28
     U.S.C. § 1931) to CRIS custodians for the same period.

  4. The aggregate dollar value of CRIS deposits attributable to
     cases pending in {court} as of the most recent reporting
     period for which the Administrative Office maintains data.

  5. Any disclosure or recusal advisories issued by the
     Administrative Office's Codes of Conduct Committee to judges
     of {court} concerning CRIS custodian relationships or other
     financial-institution counterparties since January 1, 2020.

I acknowledge that the Judiciary is not subject to the Freedom of
Information Act (5 U.S.C. § 552), and this request is therefore
made under the AOUSC's discretionary public-access policy. I will
pay reasonable reproduction costs.

This information is requested for use in a pending federal-court
proceeding to evaluate possible financial-nexus questions under
28 U.S.C. § 455 and Caperton v. A.T. Massey Coal Co., 556 U.S. 868
(2009).

Respectfully,


[YOUR SIGNATURE]
[YOUR PRINTED NAME]
[YOUR PHONE / YOUR EMAIL]

---

*Note: This is a pro se litigant's discretionary public-access
request. The Judiciary's public-access policy is published at
www.uscourts.gov/about-federal-courts/governance-judicial-conference/
administrative-office.*
"""
    out.write_text(body, encoding="utf-8")
    print(f"  draft -> {out}")
    conn = init_db()
    conn.execute(
        "INSERT INTO judicial_probes (kind, target, draft_path, status, ts) "
        "VALUES (?, ?, ?, ?, ?)",
        ("cris_probe", court, str(out), "draft", _now()),
    )
    conn.commit()
    return 0


# ────────────────────────────────────────────────────────────────────────
# analyze-order — apply Canon-of-Conduct checklist to an order .md
# ────────────────────────────────────────────────────────────────────────
def cmd_analyze_order(args):
    p = Path(args.order_path).expanduser().resolve()
    if not p.exists():
        print(f"  not found: {p}")
        return 1
    text = p.read_text(encoding="utf-8")
    print(f"  analyzing {p.name} ({len(text)} chars)")
    findings = []
    for flag, desc, authority, signals in ORDER_RED_FLAGS:
        hits = []
        for sig in signals:
            if sig.lower() in text.lower():
                # capture surrounding context
                i = text.lower().find(sig.lower())
                start = max(0, i - 80)
                end = min(len(text), i + len(sig) + 80)
                snippet = text[start:end].replace("\n", " ").strip()
                hits.append({"signal": sig, "snippet": snippet[:200]})
        if hits:
            findings.append({
                "flag": flag,
                "description": desc,
                "authority": authority,
                "evidence": hits,
                "severity": "FATAL" if flag in (
                    "ruling_in_face_of_recusal_motion",
                    "financial_interest_not_disclosed",
                    "non_discretionary_remedy_denied",
                ) else "SERIOUS",
            })

    print(f"\n  ── {len(findings)} flag(s) raised ──\n")
    conn = init_db()
    case_id = args.case or ""
    judge = args.judge or ""
    # FORUM GUARD: cite the forum's own law (m0_authority_spine.forum_authority); foreign cites kept, labelled
    try:
        from pipeline.m0_authority_spine import forum_authority, forum_of
        _fo = forum_of(case_id)
        for f in findings:
            f["authority"] = forum_authority(f["flag"], _fo, f["authority"])["authority"]
    except Exception as e:
        print(f"  forum guard unavailable: {e}")
    for f in findings:
        sev = f["severity"]
        print(f"  [{sev:7s}] {f['flag']}")
        print(f"            {f['description']}")
        print(f"            authority: {f['authority']}")
        for ev in f["evidence"][:3]:
            print(f"            signal: \"{ev['signal']}\"")
            print(f"              -> ...{ev['snippet'][:150]}...")
        print()
        conn.execute(
            "INSERT INTO judicial_findings (case_id, judge, order_path, flag, "
            "severity, description, authority, evidence, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (case_id, judge, str(p), f["flag"], sev, f["description"],
             f["authority"], json.dumps(f["evidence"])[:2000], _now()),
        )
    conn.commit()

    if not findings:
        print("  no canon-of-conduct red flags detected by static checklist")
        print("  (this does not mean the order is sound — substantive review needed)")
        return 0

    # Output JSON for downstream use
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    out = DRAFTS_DIR / f"analysis_{p.stem}_{datetime.utcnow().strftime('%Y%m%d')}.json"
    out.write_text(json.dumps(findings, indent=2), encoding="utf-8")
    print(f"  full analysis JSON -> {out}")
    return 0


# ────────────────────────────────────────────────────────────────────────
# stalling-metrics — detect CJRA six-month list candidates
# ────────────────────────────────────────────────────────────────────────
def cmd_stalling_metrics(args):
    # Try multiple candidate slugs to handle fed:2:25-cv-XXXXX -> fed_25-cv-XXXXX
    parts = args.case.split(":")
    candidates = []
    candidates.append(args.case.replace(":", "_").replace("/", "_"))   # full underscore
    if len(parts) >= 2:
        candidates.append(f"{parts[0]}_{parts[-1]}")   # first + last (drop district)
    candidates.append(parts[-1])                        # last segment only
    candidates.append("_".join(parts))                   # join with underscore
    case_dir = None
    for slug in candidates:
        candidate = ROOT / "obsidian_sync" / "cases" / slug
        if candidate.exists():
            case_dir = candidate
            break
    if case_dir is None:
        print(f"  could not find case dir: tried {candidates}")
        return 1
    print(f"  scanning {case_dir}")

    # Look for orders or motion files with date metadata
    motion_files = list(case_dir.rglob("*motion*.md")) + list(case_dir.rglob("Motion_*.md"))
    order_files = list(case_dir.rglob("*order*.md")) + list(case_dir.rglob("Order_*.md"))
    print(f"  motions found: {len(motion_files)}")
    print(f"  orders found:  {len(order_files)}")

    today = datetime.utcnow().date()
    six_months_ago = today - timedelta(days=180)

    candidates = []
    for mf in motion_files:
        # try to extract a filing date from filename or frontmatter
        m = re.search(r"(\d{4}-?\d{2}-?\d{2})", mf.name)
        if not m:
            continue
        try:
            ds = m.group(1).replace("-", "")
            filed = datetime.strptime(ds, "%Y%m%d").date()
        except Exception:
            continue
        days_pending = (today - filed).days
        if days_pending > 180:
            candidates.append((mf, filed, days_pending))

    if candidates:
        print(f"\n  ── CJRA six-month-list candidates ({len(candidates)}) ──")
        for mf, filed, days in sorted(candidates, key=lambda x: -x[2]):
            print(f"    [{days:4d} days] filed {filed}  {mf.relative_to(case_dir)}")
        print(f"\n  these motions, if still pending without ruling, are required")
        print(f"  to appear on the Administrative Office's quarterly Six-Month")
        print(f"  List under 28 U.S.C. § 476. Their absence from the published")
        print(f"  list is a reportable irregularity. Their presence is documented")
        print(f"  pattern evidence of Canon 3(A)(5) delay.")
    else:
        print(f"\n  no motions pending > 180 days detected (based on filenames)")
    return 0


# ────────────────────────────────────────────────────────────────────────
# misconduct-complaint — draft 28 U.S.C. § 351 complaint
# ────────────────────────────────────────────────────────────────────────
def cmd_misconduct_complaint(args):
    judge = args.judge.strip()
    circuit = args.circuit or "Third"
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    out = DRAFTS_DIR / (
        "misconduct_complaint_" +
        re.sub(r"[^a-z0-9]+", "_", judge.lower()).strip("_") +
        f"_{datetime.utcnow().strftime('%Y-%m-%d')}.md"
    )
    body = f"""\
JUDICIAL COUNCIL OF THE {circuit.upper()} CIRCUIT
CHIEF JUDGE
[CIRCUIT CLERK ADDRESS]

COMPLAINT OF JUDICIAL MISCONDUCT OR DISABILITY

Authority: 28 U.S.C. §§ 351-364
Rules: Judicial-Conduct and Judicial-Disability Rules ({circuit} Circuit)
Date: {datetime.utcnow().strftime("%B %d, %Y")}

I. COMPLAINANT

  Name:      [YOUR PRINTED NAME]
  Address:   [YOUR ADDRESS]
  Phone:     [YOUR PHONE]
  Email:     [YOUR EMAIL]
  Status:    Pro se litigant
  Cases:     [LIST CASE NUMBERS BEFORE THIS JUDGE]

II. SUBJECT JUDGE

  Name:      {judge}
  Court:     [SUBJECT JUDGE'S COURT]

III. STATEMENT OF FACTS GIVING RISE TO COMPLAINT

  [INSERT NUMBERED FACTUAL ALLEGATIONS, EACH WITH:
   - Date of conduct
   - Case number and caption
   - Specific conduct alleged
   - Citation to docket entry or transcript page]

IV. ALLEGED MISCONDUCT

  This complaint alleges conduct prejudicial to the effective and
  expeditious administration of the business of the courts under
  28 U.S.C. § 351(a), specifically:

  (1) Failure to dispose of motions promptly, in violation of
      Canon 3(A)(5) of the Code of Conduct for U.S. Judges:

      [SPECIFY MOTIONS PENDING > 180 DAYS WITH DATES]

  (2) Failure to disqualify or disclose, where impartiality might
      reasonably be questioned, in violation of 28 U.S.C. § 455(a)
      and Canon 3(C) of the Code of Conduct:

      [SPECIFY FINANCIAL NEXUS OR APPEARANCE-OF-IMPROPRIETY
       FACTS, e.g., Example Trust Co's documented custodial and
       investment-management relationship with the Philadelphia
       Board of Pensions and Retirement Fund per the City of
       Philadelphia 2025 GO Bonds Official Statement at pp. 302,
       308, 315 (P31456958-P31130735-P31543547, on file with the
       Municipal Securities Rulemaking Board)]

  (3) [Other specific allegations as supported by record evidence]

V. RELIEF REQUESTED

  Pursuant to 28 U.S.C. § 354, the Complainant respectfully requests
  that the Chief Judge:

  (1) Refer this matter to a special committee for investigation;

  (2) In the alternative, order such corrective action as the Chief
      Judge finds appropriate under § 352(b)(1)(B)(ii); and

  (3) Notify the Complainant of any action taken under § 352(b)(2).

VI. SUPPORTING DOCUMENTS

  Attached:
    Exhibit A: [Order(s) at issue]
    Exhibit B: [Financial-nexus evidence — bond disclosures]
    Exhibit C: [Docket pages showing motion-pending intervals]
    Exhibit D: [Any prior recusal motions and responses]

VII. CERTIFICATION

  I certify under penalty of perjury that the foregoing is true and
  correct to the best of my knowledge.

  Executed on {datetime.utcnow().strftime("%B %d, %Y")}.


  _______________________________
  [YOUR PRINTED NAME], Complainant

---

*Note: Filing instructions and any required forms are published by
the circuit. Confirm filing address and any local form requirements
on the {circuit} Circuit's website before mailing.*
"""
    out.write_text(body, encoding="utf-8")
    print(f"  draft -> {out}")
    conn = init_db()
    conn.execute(
        "INSERT INTO judicial_probes (kind, target, draft_path, status, ts) "
        "VALUES (?, ?, ?, ?, ?)",
        ("misconduct_complaint_28usc_351", judge, str(out), "draft", _now()),
    )
    conn.commit()
    return 0


# ────────────────────────────────────────────────────────────────────────
# nexus-probe — cross-ref court + party + custodian
# ────────────────────────────────────────────────────────────────────────
def cmd_nexus_probe(args):
    """Surface every documented financial relationship between the named
    party/entity and any court/judge in the case set."""
    conn = init_db()
    party = args.party
    # Pull from pdf_findings (EMMA), sec_findings (SEC EDGAR), and
    # judicial_findings (analyze-order outputs)
    print(f"\n  NEXUS PROBE — {party}\n")
    print(f"  ── EMMA disclosures referencing {party} ──")
    try:
        rows = conn.execute(
            "SELECT pdf_path, page, line, context FROM pdf_findings "
            "WHERE entity LIKE ? OR context LIKE ? ORDER BY pdf_path, page",
            (f"%{party}%", f"%{party}%"),
        ).fetchall()
        if rows:
            for path, page, line, ctx in rows[:15]:
                name = Path(path).name
                snip = (ctx or "").replace("\n", " ")[:140]
                print(f"    {name} p{page} L{line}: {snip}...")
        else:
            print(f"    no EMMA hits")
    except sqlite3.OperationalError:
        print(f"    pdf_findings table not yet populated")

    print(f"\n  ── SEC EDGAR filings referencing {party} ──")
    try:
        rows = conn.execute(
            "SELECT filer_name, form_type, line, context FROM sec_findings "
            "WHERE entity LIKE ? OR context LIKE ? ORDER BY filer_name",
            (f"%{party}%", f"%{party}%"),
        ).fetchall()
        if rows:
            for filer, form, line, ctx in rows[:10]:
                snip = (ctx or "").replace("\n", " ")[:140]
                print(f"    {filer} {form} L{line}: {snip}...")
        else:
            print(f"    no SEC hits")
    except sqlite3.OperationalError:
        print(f"    sec_findings table not yet populated")

    print(f"\n  ── Recorded judicial findings (order analysis) ──")
    try:
        rows = conn.execute(
            "SELECT case_id, judge, flag, severity, description FROM judicial_findings "
            "WHERE evidence LIKE ?",
            (f"%{party}%",),
        ).fetchall()
        if rows:
            for case, judge, flag, sev, desc in rows[:10]:
                print(f"    [{sev}] {case}  judge={judge}  flag={flag}")
                print(f"          {desc}")
        else:
            print(f"    no order-analysis findings reference {party}")
    except sqlite3.OperationalError:
        print(f"    judicial_findings table not yet populated")

    print(f"\n  ── Caperton-inquiry summary ──")
    print(f"  If the party '{party}' (a) appears as a custodian/agent/manager")
    print(f"  for an entity that funds or administratively supports the court,")
    print(f"  AND (b) the court has ruled in a way that benefits that party,")
    print(f"  Caperton v. A.T. Massey Coal Co., 556 U.S. 868 (2009) supplies")
    print(f"  the constitutional grounds for vacatur. Liljeberg v. Health")
    print(f"  Services, 486 U.S. 847 (1988) supplies the federal § 455")
    print(f"  framework for retroactive vacatur.")
    return 0


# ────────────────────────────────────────────────────────────────────────
# status
# ────────────────────────────────────────────────────────────────────────
def cmd_reforum(args):
    """Backfill: re-run the FORUM GUARD over stored judicial_findings (idempotent; back up the DB first)."""
    from pipeline.m0_authority_spine import forum_authority, forum_of, _con
    conn = init_db(); con = _con(); n = 0
    for rid, case_id, flag, auth in conn.execute("SELECT id, case_id, flag, authority FROM judicial_findings").fetchall():
        base = re.sub(r"\s*\[not binding in a [^\]]*\]$", "", auth or "")
        m = re.search(r"\[not binding in a [^,]*, kept for reference: ([^\]]*)\]$", auth or "")
        if m: base = base + "; " + m.group(1)
        r = forum_authority(flag, forum_of(case_id or ""), base, con)
        if r["authority"] != auth:
            print(f"  #{rid} {case_id} {flag}\n     was: {auth}\n     now: {r['authority']}")
            if not args.dry:
                conn.execute("UPDATE judicial_findings SET authority=? WHERE id=?", (r["authority"], rid)); n += 1
    conn.commit(); print(f"  {n} row(s) updated{' (dry run)' if args.dry else ''}")
    return 0


def cmd_status(args):
    conn = init_db()
    nf = conn.execute("SELECT COUNT(*) FROM judicial_findings").fetchone()[0]
    np_ = conn.execute("SELECT COUNT(*) FROM judicial_probes").fetchone()[0]
    print(f"  judicial_findings rows: {nf}")
    print(f"  judicial_probes rows:   {np_}")
    print(f"  drafts dir: {DRAFTS_DIR}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("disclosure-request",
                       help="generate AOUSC 5 U.S.C. App. § 105 request for judge's financial disclosures")
    p.add_argument("judge")
    p.add_argument("--years", default="2023,2024,2025")
    p.set_defaults(func=cmd_disclosure_request)

    p = sub.add_parser("cris-probe",
                       help="generate AOUSC CRIS-custodian public-access request")
    p.add_argument("--court", default="")
    p.set_defaults(func=cmd_cris_probe)

    p = sub.add_parser("analyze-order",
                       help="apply Canon-of-Conduct checklist to an order .md")
    p.add_argument("order_path")
    p.add_argument("--case", default="")
    p.add_argument("--judge", default="")
    p.set_defaults(func=cmd_analyze_order)

    p = sub.add_parser("stalling-metrics",
                       help="detect CJRA six-month-list candidates in a case dir")
    p.add_argument("case")
    p.set_defaults(func=cmd_stalling_metrics)

    p = sub.add_parser("misconduct-complaint",
                       help="draft a 28 U.S.C. § 351 judicial misconduct complaint")
    p.add_argument("judge")
    p.add_argument("--circuit", default="Third")
    p.set_defaults(func=cmd_misconduct_complaint)

    p = sub.add_parser("nexus-probe",
                       help="cross-ref a party across EMMA + SEC + judicial findings")
    p.add_argument("party")
    p.set_defaults(func=cmd_nexus_probe)

    p = sub.add_parser("reforum", help="re-apply the forum guard to stored judicial_findings")
    p.add_argument("--dry", action="store_true")
    p.set_defaults(func=cmd_reforum)

    p = sub.add_parser("status")
    p.set_defaults(func=cmd_status)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main() or 0)
