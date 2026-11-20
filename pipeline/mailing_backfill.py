#!/usr/bin/env python3
"""mailing_backfill — rebuild the LetterStream job history from the mailbox already on disk
(obsidian_sync/emails_full/*Letterstream_Job_Completed* / *Order_Confirmation*) and register every
job the mailing_tracker never saw.  Deterministic guardrail (no LLM).  BACKLOG S-tracker-loop (1).

Writes:
  output/audit/letterstream_jobs.json      — every job: id, name, date_confirmed, date_completed,
                                             class, recipients, tracking[], case ties, outbox filing
  output/audit/mailing_tracker.json        — adds CONFIRMED items for jobs with no tracker entry
                                             (history carries the Gmail message ids as proof)
Usage:  python3 -m pipeline.mailing_backfill [--dry]  [--case fed_26-cv-00123]
"""
from __future__ import annotations
import json, re, sys, glob, shutil, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EMAILS = ROOT / "obsidian_sync" / "emails_full"
OUT = ROOT / "output" / "audit" / "letterstream_jobs.json"
TRACKER = ROOT / "output" / "audit" / "mailing_tracker.json"
OUTBOX = ROOT / "efiling" / "letterstream_outbox"

# caption / keyword → case slug.  Order matters: first hit wins.  Add a row, never a special case.
CASE_KEYS = [
    ("fed_26-cv-00123", r"02822|2822|2763|3064|2615|2960|third cir|3rd cir|dodszuweit|transunion|trans union|equifax|experian|greensky|avant|credit bureau|fcra|the-judge|circuit executive|jc03"),
    ("fed_26-cv-00456", r"04767|4767|example_lender|3075"),
    ("fed_26-cv-00789", r"06077|6077|state ?farm"),
    ("phila_ccp_260100000", r"260100000|example_creditor|example trust co|opposing counsel|forry|labletta|kohler|1934 ?eda|1935|superior court|hall"),
    ("phila_ccp_260100001", r"260100001|progressive|ctcorp|daniel j"),
    ("irs", r"\birs\b|ogden|austin|raivs|tas_|taxpayer advocate|2645c|916c|843|4506"),
    ("pgw", r"pgw|gas works|peco|water rev"),
]

def _fm(txt: str) -> dict:
    m = re.match(r"---\n(.*?)\n---", txt, re.S)
    d = {}
    if m:
        for ln in m.group(1).splitlines():
            if ":" in ln:
                k, v = ln.split(":", 1); d[k.strip()] = v.strip().strip('"')
    return d

def _field(txt: str, name: str) -> str:
    m = re.search(rf"{name}:\s*([^\r\n]+)", txt)
    return m.group(1).strip() if m else ""

def _date(s: str) -> str:
    try:
        return datetime.datetime.strptime(s[:31].strip(), "%a, %d %b %Y %H:%M:%S %z").date().isoformat()
    except Exception:
        try: return datetime.datetime.strptime(s[:25].strip(), "%a, %d %b %Y %H:%M:%S").date().isoformat()
        except Exception: return s[:16]

def _case_for(name: str) -> str:
    low = name.lower()
    for slug, pat in CASE_KEYS:
        if re.search(pat, low): return slug
    return ""

def _outbox_ties() -> dict:
    """filing stem → (case, pdf) from efiling/letterstream_outbox/*/*/job.json"""
    ties = {}
    for j in OUTBOX.glob("*/*/job.json"):
        try:
            d = json.loads(j.read_text()); ties[d.get("filing", j.parent.name)] = (d.get("case", ""), d.get("pdf", ""))
        except Exception: pass
    return ties

def collect() -> dict:
    jobs: dict[str, dict] = {}
    for f in sorted(EMAILS.glob("*.md")):
        n = f.name.lower()
        if "letterstream" not in n and "order_confirmation" not in n and "onlinecertifiedmail" not in n: continue
        txt = f.read_text(errors="ignore"); fm = _fm(txt)
        subj = fm.get("subject", ""); frm = fm.get("from", "").lower()
        if "letterstream" not in frm: continue
        jid = _field(txt, "Job ID") or (re.search(r"(\d{8})_invoice", txt) or [None, ""])[1]
        name = _field(txt, "Job Name") or re.sub(r"^.*?-\s*", "", subj, count=1)
        if not jid:
            # OnlineCertifiedMail completions carry no Job ID line; key by name+date
            jid = "ocm_" + re.sub(r"\W+", "_", name.lower())[:40] + "_" + _date(fm.get("date", ""))
        j = jobs.setdefault(jid, {"job_id": jid, "name": name, "class": "", "recipients": 0, "tracking": [],
                                  "date_confirmed": "", "date_completed": "", "gmail": [], "case": "", "filing": ""})
        j["gmail"].append(fm.get("message_id", f.stem.split("__")[0]))
        if "Job Completed" in subj:
            j["date_completed"] = _date(fm.get("date", ""))
            j["class"] = _field(txt, "Mailing Class") or j["class"]
            try: j["recipients"] = int(_field(txt, "Recipients") or 0) or j["recipients"]
            except ValueError: pass
            for t, who in re.findall(r"trackingid=(\d{16,26})\">\d+</a>\s*-\s*([^<\r\n]{0,80})", txt):
                if t not in [x["usps"] for x in j["tracking"]]: j["tracking"].append({"usps": t, "to": who.strip()})
        elif "Order Confirmation" in subj and not subj.lower().startswith("re:"):
            j["date_confirmed"] = _date(fm.get("date", ""))
        j["case"] = j["case"] or _case_for(name)
    ties = _outbox_ties()
    for j in jobs.values():
        stem = j["name"].split(" - ")[0].strip()
        if stem in ties:
            j["filing"] = stem; j["case"] = j["case"] or ties[stem][0]
    return jobs

def register(jobs: dict, dry: bool, only_case: str | None) -> list:
    sys.path.insert(0, str(ROOT))
    from pipeline import mailing_tracker as MT
    reg = MT._load()
    present = {str(it.get("letterstream_job_id")) for it in reg["items"].values() if it.get("letterstream_job_id")}
    added = []
    for jid, j in sorted(jobs.items(), key=lambda kv: kv[1]["date_completed"] or kv[1]["date_confirmed"]):
        if not j["case"] or jid in present or not j["date_completed"]: continue
        if only_case and j["case"] != only_case: continue
        if jid.startswith("ocm_"): continue  # certified-mail legacy: no job id; listed in letterstream_jobs.json only
        to = [t["to"] for t in j["tracking"]] or [j["name"].split(" - ", 1)[-1]]
        mid = MT.mail_id(j["case"], j["filing"] or j["name"], ";".join(sorted(to)))
        if mid in reg["items"]: continue
        reg["items"][mid] = {
            "case_id": j["case"], "document": j["filing"] or j["name"], "recipients": to, "batch": "backfill",
            "court": "", "class": j["class"].lower() or "first-class", "status": "CONFIRMED",
            "letterstream_job_id": jid, "tracking": [t["usps"] for t in j["tracking"]],
            "history": [[j["date_confirmed"] or j["date_completed"], "SENT", f"backfill: order confirmed (gmail {','.join(j['gmail'][:2])})"],
                        [j["date_completed"], "CONFIRMED", f"backfill: LS Job Completed {jid} {j['class']} recipients={j['recipients']}"]]}
        added.append((mid, jid, j["case"], j["date_completed"], j["name"][:50]))
    if not dry and added:
        shutil.copy(TRACKER, str(TRACKER) + ".pre_backfill." + datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
        reg["updated"] = MT._now(); MT._save(reg)
    return added

def prior_mailings(case: str, pattern: str = "") -> list:
    """Dated list of every mailing for a case (optionally filtered by regex on name/filing) — the
    PRIOR SUBMISSIONS block for drafters (BACKLOG S-resubmission step 1)."""
    jobs = json.loads(OUT.read_text()) if OUT.exists() else collect()
    rows = [j for j in jobs.values() if j["case"] == case and (not pattern or re.search(pattern, (j["name"] + " " + j["filing"]).lower()))]
    return sorted(rows, key=lambda j: j["date_completed"] or j["date_confirmed"])

def main():
    dry = "--dry" in sys.argv
    only = sys.argv[sys.argv.index("--case") + 1] if "--case" in sys.argv else None
    jobs = collect()
    OUT.write_text(json.dumps(jobs, indent=1))
    tied = sum(1 for j in jobs.values() if j["case"])
    print(f"letterstream_jobs.json: {len(jobs)} jobs, {tied} tied to a case → {OUT}")
    added = register(jobs, dry, only)
    print(f"tracker: {'would add' if dry else 'added'} {len(added)} item(s)")
    for a in added: print("  ", a)

if __name__ == "__main__":
    main()
