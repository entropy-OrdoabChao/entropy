#!/usr/bin/env python3
"""followup_drafter -- GLUE, not a new organ (user 2026-10-17: "if there are late replies ... it should
draft a follow up using the rules"). Reuses the organs that already exist:
  deadline_watch.scan_expected()  -> the inbound EXPECTED rows and their follow_up dates (the tracker)
  ledger_feed.proof_of_delivery() -> LetterStream / e-file proof for the thing we sent (the facts)
  the stub format (CAPTION/POSTURE/NEW EVIDENCE BLOCK/GROUNDS/RELIEF SHAPE/Drafting rules)
  pipeline.m1.run()               -> APPLICATION lane via the status_inquiry / letter_to_clerk stems
For every EXPECTED row whose follow_up date has passed, no reply received, no 'followed_up' marker,
and owed by a court/opponent: write ONE stub under <case>/drafts/_bundle_followup/motions/chain_followup/
and run m1 on it. Idempotent: state in output/audit/followup_drafter_state.json keyed like deadline_watch.
Agency rows whose action is a phone call / TAS (from != court|opponent) are listed as MANUAL, not drafted.
"""
import argparse, json, re, subprocess, sys
from datetime import date
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
from pipeline import deadline_watch as dw                      # noqa: E402
from pipeline import ledger_feed as lf                         # noqa: E402

STATE = BASE / "output" / "audit" / "followup_drafter_state.json"
CLAIM_WINDOW_DAYS = 120   # instruments older than this are history, not live deadlines
SIG = "/s/ FILER NAME, sui juris, 123 Main St, Suite 100, Anytown, ST 00000"


def court_for(slug: str, row: dict) -> str | None:
    """Court code the way m1/m1_filing_gate key their buckets. None = no court lane (agency)."""
    frm = (row.get("from") or "").lower(); txt = f"{row.get('rule','')} {row.get('what','')}"
    if frm not in ("court", "opponent") and "court" not in frm:
        return None
    if slug.startswith("fed_") or slug.startswith("fed-"):
        return "3D_CIR" if re.search(r"\bCA3\b|Third Circuit|FRAP", txt) else "EDPA"
    if re.search(r"Superior Court|Supreme Court|EDA 20\d\d|1925\(a\)|1935", txt):
        return "PA_SUPER"      # 1925(a) silence: the lever is the appellate prothonotary's 1935(a) delinquency notice
    if re.search(r"trial court|Judge |OJR|Prothonotary|Control No", txt):
        return "PA_CP_PHILA"
    if "Pa.R.A.P." in txt:
        return "PA_SUPER"
    return "PA_CP_PHILA"


def stem_for(row: dict) -> str:
    act = (row.get("action") or "").lower()
    if "application" in act or "pa.r.a.p. 123" in act:
        return "application_for_relief"
    if "clerk" in act or "prothonotary" in act or "ojr" in act:
        return "letter_to_clerk"
    return "status_inquiry"


def controlling_authority(rule: str) -> list[str]:
    """Verbatim rule text from the corpus for every cite in the EXPECTED row's `rule` (deterministic guardrail:
    m0_authority_spine._verbatim). A cite we cannot pull is written NOT IN CORPUS -- never paraphrased."""
    from pipeline import m0_authority_spine as sp
    out = []
    try:
        con = sp._con()
    except Exception as e:
        return [f"- NOT IN CORPUS (chunk DB unavailable: {e})"]
    for m in re.finditer(r"Pa\.R\.A\.P\.\s*(\d{3,4})", rule):
        n = m.group(1); ch = n[:-2] if len(n) == 4 else n[0]
        q = sp._verbatim(con, f"pa_code_t210_ch{ch}", [rf"Rule {n}\. [^.]{{3,80}}\.(.{{0,900}})"])
        out.append(f"- Pa.R.A.P. {n}: \"{q[0][:900]}\" [corpus: pa_code_t210_ch{ch}]" if q else f"- Pa.R.A.P. {n}: NOT IN CORPUS (pa_code_t210_ch{ch} scoped miss -- verify before drafting)")
    for m in re.finditer(r"42 Pa\.C\.S\.\s*§?\s*(\d{3,5})", rule):
        n = m.group(1); q = sp._verbatim(con, "pa_cs_t42", [rf"§ {n}\. [^.]{{3,80}}\.(.{{0,700}})"])
        out.append(f"- 42 Pa.C.S. § {n}: \"{q[0][:700]}\" [corpus: pa_cs_t42]" if q else f"- 42 Pa.C.S. § {n}: NOT IN CORPUS (pa_cs_t42 scoped miss)")
    for m in re.finditer(r"(\d+) U\.S\.C\.\s*§?\s*(\d+[a-z]?)", rule):
        t, s = m.groups(); q = sp._verbatim(con, f"usc_t{t}_s{s}", [r"§\s*" + s + r"\.(.{0,700})"])
        out.append(f"- {t} U.S.C. § {s}: \"{q[0][:700]}\" [corpus: usc_t{t}_s{s}]" if q else f"- {t} U.S.C. § {s}: NOT IN CORPUS (usc_t{t}_s{s} scoped miss)")
    for m in re.finditer(r"(?:FRAP|Fed\. ?R\. ?App\. ?P\.)\s*(\d+)", rule):
        n = m.group(1); q = sp._verbatim(con, "frap", [rf"Rule {n}\. [^.]{{3,80}}\.(.{{0,700}})"])
        out.append(f"- Fed. R. App. P. {n}: \"{q[0][:700]}\" [corpus: frap]" if q else f"- Fed. R. App. P. {n}: NOT IN CORPUS (frap scoped miss)")
    return out or [f"- declared duty, no statutory cite in the tracker row: \"{rule}\" -- NOT IN CORPUS until m0 names the rule"]


def write_stub(slug: str, court: str, group: list, today: date) -> Path:
    """ONE stub per (case, court) -- user lock: one consolidated packet per court. `group` = [(row, fu, silent, proof), ...]."""
    stem = stem_for(group[0][0]) if len(group) == 1 else "status_inquiry"
    d = dw.CASES / slug / "drafts" / "_bundle_followup" / "motions" / "chain_followup"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{today.strftime('%Y%m%d')}_{stem}_{court.lower()}_{len(group)}items.md"
    def _pj(proof):
        return "; ".join(f"LS job {x['job_id']} ({x['status']}, {x.get('dated') or '?'})" for x in proof if x.get("job_id")) or "e-filed / tracker entry (see mailing_tracker)"
    whats = "; ".join((r.get("what") or "")[:70] for r, *_ in group)
    L = [f"# STUB: {stem.replace('_',' ').title()} -- {len(group)} outstanding duties -- {whats[:120]}", "",
         "## CAPTION", f"To: {group[0][0].get('from')} (address of record per the case serve list)",
         f"Re: {whats[:200]}", "",
         "## POSTURE"]
    for i, (row, fu, silent, proof) in enumerate(group, 1):
        L.append(f"{i}. On {row.get('sent')} the undersigned filed/served: {row.get('what')}. Duty to act: {row.get('rule') or 'declared'}. "
                 f"Expected by {row.get('expect_by')}; follow-up date {fu.isoformat()}; as of {today.isoformat()} nothing received -- "
                 f"{silent} days past follow-up, {(today - lf._d(row.get('sent'))).days} days since the trigger.")
    L += ["", "## NEW EVIDENCE BLOCK"]
    for i, (row, fu, silent, proof) in enumerate(group, 1):
        L.append(f"- E{i}: {row.get('what','')[:80]} -- delivery: {_pj(proof)}; tracker row sent {row.get('sent')}, expect_by {row.get('expect_by')}, received: none [tracker]")
    L += ["", "## CONTROLLING AUTHORITY (m0-verified shape: verbatim rule text from corpus; rules first)"]
    for i, (row, fu, silent, proof) in enumerate(group, 1):
        L.append(f"### Duty {i}: {row.get('rule') or 'declared duty'}")
        L += [f"- PLEAD: {q[2:] if q.startswith('- ') else q}" for q in controlling_authority(row.get("rule") or "")]
        L.append(f"- PLEAD: The instrument ({row.get('what','')[:80]}) was delivered {row.get('sent')} ({_pj(proof)}); no action has issued as of {today.isoformat()}, {silent} days past the follow-up date. [tracker]")
    L += ["", "## GROUNDS",
          "1. Each duty above is quoted verbatim from the governing rule; where a line reads NOT IN CORPUS the drafter must not supply the words.",
          "2. The triggers, deliveries and silence are record facts (E-block). Silence past a duty's own window is a finding, not a wait.",
          "3. Requested action per duty: " + " | ".join(f"({i}) {r.get('action') or 'issue the action the rule requires'}" for i, (r, *_rest) in enumerate(group, 1)), "",
          "## RELIEF SHAPE",
          # m1_application.relief_from_stub parses NUMBERED/BULLETED lines only; a prose paragraph here
          # made m1 refuse every follow-up ("ZERO relief items parsed") -- list form is the contract.
          "1. Written confirmation of receipt and docketing of each instrument identified above.",
          "2. The action each quoted rule requires on each instrument, or a written statement of when it will be taken.",
          "3. Identification in writing of any claimed deficiency in any instrument, so it can be cured.", "",
          "## Drafting rules (locked)",
          "- Status-inquiry register: facts, duty, dates, delivery proof. No merits argumentation; no demand-table aggression.",
          f"- Signature: {SIG}. Never 'pro se'.",
          "- Tone: letter-humanizer voice; formal, calm. Every rule cite [corpus]/[official] or NOT IN CORPUS.", ""]
    p.write_text("\n".join(L), encoding="utf-8")
    return p


def register_claims(dry: bool) -> list:
    """Link the tracker to claims it never saw (user 2026-10-17: 'start tracking tort claims that have been
    drafted or pulls from the email watch'). Two deterministic sources, nothing invented:
      1. LetterStream history (mailing_backfill.collect = the mail-watch pull of LS confirmations): every
         claim-type job (notice/claim/demand/dispute/intent/default/tender) with a CONFIRMED date and no
         EXPECTED row naming its job id -> one EXPECTED row in the tied case (or claim_candidates/<slug>).
         expect_by = sent+30 is a PLACEHOLDER flagged rule_status=UNVERIFIED; the stub's CONTROLLING
         AUTHORITY will read NOT IN CORPUS until the real rule is named, so nothing drafts on a guess.
      2. Drafted claims never mailed: cases/*/drafts/COMPLAINT*.docx | *DEMAND*_FILING.docx with no tracker
         item -> mailing_tracker DRAFTED item, so the board shows the unshipped claim.
    Jobs with no confirmed date are reported NEEDS-DATE (never guessed)."""
    from datetime import timedelta
    from pipeline import mailing_backfill as mb
    from pipeline import mailing_tracker as mt
    out = []
    seen = " ".join(p.read_text(errors="ignore") for p in list(dw.CASES.glob("*/EXPECTED.json")) + list(dw.CLAIMS.glob("*/EXPECTED.json")))
    claim_re = re.compile(r"notice|claim|demand|dispute|intent|default|tender|refusal", re.I)
    reg = mt._load()["items"]
    live_cases = {it.get("case_id") for it in reg.values() if isinstance(it, dict)} | {p.parent.name for p in dw.CASES.glob("*/EXPECTED.json")}
    for jid, j in mb.collect().items():
        name = j.get("name") or ""
        if not claim_re.search(name) or re.search(r"clerk of court|office of judicial|motion|court|IRS_", name, re.I) or jid in seen:
            continue
        sent = lf._d(j.get("date_confirmed") or j.get("date_completed"))
        if not sent:
            out.append(("claims", "NEEDS-DATE", 0, f"LS {jid}", name[:60])); continue
        # RECENCY GATE (user 2026-10-17, main page flooded with 2025 notices as "-600d"): history is not a live
        # deadline. Only instruments mailed within CLAIM_WINDOW_DAYS become EXPECTED rows; older ones are
        # reported HISTORY for the case file, never put on the deadline board.
        if (date.today() - sent).days > CLAIM_WINDOW_DAYS:
            out.append(("claims", "HISTORY", (date.today() - sent).days, f"LS {jid}", name[:60])); continue
        # case match on the NAME and on the RECIPIENTS (an "intent to sue" addressed to Example Lender is 04767)
        slug = mb._case_for(name) or mb._case_for(" ".join(t.get("to", "") for t in j.get("tracking", [])))
        if slug and (slug in live_cases or (lf.slug_to_case_ids(slug) & live_cases)):
            continue   # pre-suit notice of a case now in litigation: its silence is already pleaded; the docket owns the clock
        root = dw.CASES / slug if slug else dw.CLAIMS / re.sub(r"[^a-z0-9]+", "_", name.split(" - ")[-1].lower()).strip("_")[:40]
        tos = sorted({t.get("to", "") for t in j.get("tracking", []) if t.get("to")}) or ["recipient per LS job"]
        row = {"sent": sent.isoformat(), "what": f"{name} (LS {jid})", "from": "; ".join(tos)[:120],
               "rule": "the instrument's own stated response window / governing statute -- UNVERIFIED placeholder 30d",
               "rule_status": "UNVERIFIED", "expect_by": (sent + timedelta(days=30)).isoformat(),
               "follow_up": (sent + timedelta(days=37)).isoformat(),
               "action": "follow-up notice citing the instrument's stated consequence; silence = admission/default lever per the instrument",
               "source": "letterstream_backfill", "received": None}
        if not dry:
            root.mkdir(parents=True, exist_ok=True); ef = root / "EXPECTED.json"
            rows = json.loads(ef.read_text()) if ef.exists() else []
            if isinstance(rows, dict): rows = rows.get("expected", [])
            rows.append(row); ef.write_text(json.dumps(rows, indent=1, ensure_ascii=False))
        out.append(("claims", "WOULD-ADD" if dry else "EXPECTED+", 0, root.name, row["what"][:60]))
    tracked = {Path(it.get("document", "")).stem.lower() for it in reg.values() if isinstance(it, dict)}
    # one row per claim, not per version: newest docx per (case, name-stem-before-date); cases already in flight
    # (tracker items or EXPECTED rows) are skipped -- their complaint was filed, the tracker already owns them
    newest = {}
    for p in list(dw.CASES.glob("*/drafts/COMPLAINT*.docx")) + list(dw.CASES.glob("*/drafts/*DEMAND*_FILING.docx")):
        case = p.parts[-3]
        if case in live_cases or "_DRAFT" in p.stem or re.search(r"CoverSheet|_IFP", p.stem):
            continue
        fam = re.sub(r"_?(v\d+_)?20\d{6}.*$", "", p.stem).lower()
        if (case, fam) not in newest or p.stat().st_mtime > newest[(case, fam)].stat().st_mtime:
            newest[(case, fam)] = p
    for (case, fam), p in sorted(newest.items()):
        if p.stem.lower() in tracked:
            continue
        if not dry:
            mt.add(case, p.name, ["UNSENT -- recipients per the complaint's serve list"], batch="drafted_claims",
                   status="DRAFTED", note="registered by followup_drafter --register (drafted, never mailed)")
        out.append(("claims", "WOULD-TRACK" if dry else "DRAFTED+", 0, case, p.name[:60]))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--case"); ap.add_argument("--dry-run", action="store_true"); ap.add_argument("--today")
    ap.add_argument("--no-m1", action="store_true", help="write stubs only")
    ap.add_argument("--register", action="store_true", help="link LS claim history + drafted complaints into tracker/EXPECTED first")
    a = ap.parse_args(argv)
    today = lf._d(a.today) or date.today()
    state = json.loads(STATE.read_text()) if STATE.exists() else {"drafted": {}}
    items = lf.load_tracker()
    rows = dw.scan_expected()
    out = register_claims(a.dry_run) if a.register else []
    groups = {}   # (slug, court) -> [(src, fu, silent, proof)], keys
    for r in rows:
        slug = r["case"]
        if a.case and slug != a.case:
            continue
        fu = date.fromisoformat(r["due"]); silent = (today - fu).days
        if silent <= 0:
            continue
        src = next((x for x in (json.loads(Path(r["src"]).read_text()) if Path(r["src"]).exists() else [])
                    if isinstance(x, dict) and not x.get("received") and str(x.get("sent")) == r["event_date"]
                    and (x.get("what") or "") in r["what"]), None)
        if not src:
            continue
        if src.get("followed_up"):
            out.append((slug, "SUPERSEDED", silent, src.get("followed_up"), src.get("what", "")[:60])); continue
        court = court_for(slug, src)
        if court is None:
            out.append((slug, "MANUAL", silent, src.get("action", "")[:50], src.get("what", "")[:60])); continue
        prev = state["drafted"].get(r["key"])
        if prev and (prev.get("draft") or prev.get("m1_rc") == 0):
            out.append((slug, "drafted", silent, prev["draft"][-40:], src.get("what", "")[:60])); continue
        proof = lf.proof_of_delivery(items, lf.slug_to_case_ids(slug), lf._d(src.get("sent")), src.get("from", ""))
        g = groups.setdefault((slug, court), {"rows": [], "keys": []})
        g["rows"].append((src, fu, silent, proof)); g["keys"].append(r["key"])
    for (slug, court), g in groups.items():
        maxlate = max(x[2] for x in g["rows"]); whats = "; ".join(x[0].get("what", "")[:40] for x in g["rows"])
        if a.dry_run:
            out.append((slug, "WOULD-DRAFT", maxlate, f"{court} x{len(g['rows'])}", whats[:70])); continue
        stub = write_stub(slug, court, g["rows"], today)
        rec = {"stub": str(stub), "court": court, "draft": "", "m1_rc": None, "at": today.isoformat(), "rows": len(g["rows"])}
        if not a.no_m1:
            cmd = [sys.executable, "-m", "pipeline.m1", str(stub), "--court", court, "--type", "status_inquiry"]
            p = subprocess.run(cmd, cwd=BASE, capture_output=True, text=True, timeout=1500)
            rec["m1_rc"] = p.returncode
            m = re.search(r"(\S+_DRAFT\.md)", p.stdout + p.stderr)
            rec["draft"] = m.group(1) if m else ""
            rec["m1_tail"] = (p.stdout + p.stderr)[-600:]
        for k in g["keys"]:
            state["drafted"][k] = rec
        STATE.write_text(json.dumps(state, indent=1))
        out.append((slug, "DRAFTED" if rec.get("draft") else f"STUB(m1 rc={rec['m1_rc']})", maxlate, rec.get("draft") or str(stub), whats[:70]))
    print(f"{'case':30} {'status':12} {'late':>4}  detail")
    for o in sorted(out, key=lambda t: -t[2]):
        print(f"{o[0]:30} {o[1]:12} {o[2]:>4}  {o[3]}  |  {o[4]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
