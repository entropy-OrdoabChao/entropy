"""docket_email_fetch.py — read court NEF / e-filing emails and pull the document the FREE way,
case-agnostically, into each case's intake/. Ledger-gated (one free look per filing, never twice).

  ECF / EDPA (ecf.paed.uscourts.gov)  -> PACER free-look via pacer_download.py (login + receipt
                                         + iframe flow), gated by pacer_download_ledger.json
  Phila CCP (courts.phila.gov / FJD)  -> the email link is FREE: direct download, no login

DEFAULT IS DRY-RUN: it only LISTS what would be fetched (and flags what's already in the ledger),
so no one-time free look is consumed. Pass --download to actually fetch.

Usage:
  python3 docket_email_fetch.py [days]              # dry-run (default 30 days)
  python3 docket_email_fetch.py [days] --download   # actually fetch the NEW ones
"""
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import sys, os, re, json, base64
import email.utils, datetime
import requests
sys.path.insert(0, str(_epkg()))
from scrapers.email_scraper import _get_gmail_service
import pacer_download as pd
from pipeline import m0_reply_gate as gate

GATE = "--gate" in sys.argv
DRAFT = "--draft" in sys.argv      # also auto-draft each opposition/reply via pipeline.auto_reply
TODAY = datetime.date.today().isoformat()
ANALYSIS_TIMEOUT = 2400   # m0 analysis budget per case (sections are bounded)
DOCKET_TEXT_RE = re.compile(r"Docket Text:\s*(.{0,400})", re.I | re.S)

INTAKE = str(_eroot() / "intake")
DAYS = int(next((a for a in sys.argv[1:] if a.isdigit()), 30))
DOWNLOAD = "--download" in sys.argv
MAX = int(next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--max=")), "8"))  # per-run cap (money backstop)
# --only 145,143  -> restrict the live fetch to these docket numbers (avoid re-spending free looks)
_only = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--only=")), None)
ONLY = set(x.strip() for x in _only.split(",")) if _only else None

CASE_RE = re.compile(r"\b(\d:\d{2}-cv-\d{4,5})\b")
# Phila CCP / state dockets are bare 9-digit numbers ("Case Number: 260100001"), which CASE_RE
# (federal-only) never matches -> case_num fell through to "?" and documents landed in a junk
# "phila_ccp_?" folder instead of the real case. Agnostic: number from the notice, folder from
# the registry (never a hardcoded slug).
CCP_CASE_RE = re.compile(r"(?:Case\s*(?:Number|ID)|No\.)\s*[:#]?\s*(\d{9})\b", re.I)


def ccp_slug_for(case_num):
    """Resolve a state docket number to its registered case folder via cases_index/case_map.json.
    Returns None when the registry has no row for it (caller then keeps the legacy fallback)."""
    try:
        cm = json.load(open(str(_eroot() / "cases_index/case_map.json"), encoding="utf-8"))
    except Exception:
        return None
    for slug, v in (cm.items() if isinstance(cm, dict) else []):
        if not isinstance(v, dict):
            continue
        if str(v.get("case_id", "")) == str(case_num):
            return slug
        if str(case_num) in [str(a) for a in (v.get("docket_aliases") or [])]:
            return slug
    return None
DKT_RE = re.compile(r"Document Number:\s*(\d+)", re.I)
# the DOCUMENT free-look link carries doc1/<id> + magic_num; DktRpt.pl is only the docket report.
ECF_DOC = re.compile(r"https?://ecf\.paed\.uscourts\.gov/doc1/\d+\?[^\s\"'<>]*magic_num=\d+", re.I)
PHILA_DOC = re.compile(r"https?://[^\s\"'<>]*(?:courts\.phila\.gov|fjdefile\.phila\.gov)[^\s\"'<>]*", re.I)
# the ACTUAL order opens via fjdefile .../open_doc?h=<hash> (a 3-hop form/JS chain to the PDF); the
# courts.phila.gov/pdf/NLR-FJD.pdf link is only the generic notice cover.
PHILA_OPENDOC = re.compile(r"https?://fjdefile\.phila\.gov/[^\s\"'<>]*open_doc\?h=[^\s\"'<>]+", re.I)


def _fetch_phila_pdf(url, timeout=45):
    """Phila FJD order links resolve to the PDF via a 3-hop chain (no login; the h= hash is the token):
    open_doc?h=... -> an auto-submit FORM POST to zk_elib.show_pdf_document -> a JS location.replace to
    /efs/temp/<id>.pdf. Follow all three in one session; return the PDF bytes or None."""
    s = requests.Session()
    r = s.get(url, timeout=timeout)
    if r.content[:4] == b"%PDF":
        return r.content
    m = re.search(r'action=["\']([^"\']+)', r.text)
    if not m:
        return None
    fields = dict(re.findall(r'name=["\']([^"\']+)["\']\s+value=["\']([^"\']*)', r.text))
    r2 = s.post("https://fjdefile.phila.gov/efsfjd/" + m.group(1), data=fields, timeout=timeout)
    if r2.content[:4] == b"%PDF":
        return r2.content
    m2 = re.search(r'location\.replace\(["\']([^"\']+)', r2.text) or re.search(r'URL=([^"\'\s>]+)', r2.text)
    if not m2:
        return None
    r3 = s.get(m2.group(1), timeout=timeout)
    return r3.content if r3.content[:4] == b"%PDF" else None


def _body(payload):
    out = []
    def walk(p):
        if p.get("body", {}).get("data"):
            try:
                out.append(base64.urlsafe_b64decode(p["body"]["data"]).decode("utf-8", "ignore"))
            except Exception:
                pass
        for sub in p.get("parts", []) or []:
            walk(sub)
    walk(payload)
    return "\n".join(out)


def slug_for(case_num):
    """Canonical case-folder slug: drop the district prefix (e.g. 2:26-cv-00123 -> fed_26-cv-00123),
    matching the existing obsidian_sync/cases/fed_<YY>-cv-<NNNNN> folders."""
    tail = case_num.split(":", 1)[-1]   # "2:26-cv-00123" -> "26-cv-00123"
    return "fed_" + tail


def _clean_filer(s):
    """NEF layout puts 'Filer:' immediately before 'Document Number:'; clerk/text-only notices have no
    real filer, so the regex can capture the label 'Document Number:' as noise. Blank those out so a
    missing filer never masquerades as an opposing party."""
    s = (s or "").strip().rstrip(":").strip()
    if not s or re.match(r"^(document\s*number|docket\s*text)\b", s, re.I):
        return ""
    return s


def scan():
    # SKIP-OURS gate (R2, data-driven): use the SAME robust matcher as submission_reconciler so the
    # puller never PACER-charges for our own filings (catches MALIK-FARDY etc., not just the surname).
    from pipeline.submission_reconciler import _ours as _recon_ours, _our_tokens
    ptoks = _our_tokens()
    svc = _get_gmail_service()
    # NEF sender is ecf_paed@paed.uscourts.gov (domain paed.uscourts.gov), not ecf.paed.uscourts.gov.
    q = f"newer_than:{DAYS}d (from:paed.uscourts.gov OR from:uscourts.gov OR from:courts.phila.gov OR subject:(Notice of Electronic Filing) OR subject:(Activity in Case))"
    ids = svc.users().messages().list(userId="me", q=q, maxResults=200).execute().get("messages", [])
    ledger = pd.load_ledger()
    tasks, phila, events, seen = [], [], [], set()
    for m in ids:
        full = svc.users().messages().get(userId="me", id=m["id"], format="full").execute()
        hdrs = {h["name"].lower(): h["value"] for h in full["payload"].get("headers", [])}
        raw = _body(full["payload"])                 # keep raw for href extraction
        clean = re.sub(r"<[^>]+>", " ", raw) + " " + hdrs.get("subject", "")  # tag-stripped for text fields
        subj = hdrs.get("subject", "")[:70]
        case = (CASE_RE.search(clean) or CASE_RE.search(hdrs.get("subject", "")))
        case_num = case.group(1) if case else "?"
        if case_num == "?":
            # state/CCP notice: bare 9-digit docket number
            _ccp = (CCP_CASE_RE.search(clean) or CCP_CASE_RE.search(hdrs.get("subject", "")))
            if _ccp:
                case_num = _ccp.group(1)
        dkt = DKT_RE.search(clean)
        dkt_num = dkt.group(1) if dkt else "0"
        ecf = ECF_DOC.search(raw)
        body = clean
        if ecf:
            base_key = f"{case_num}_dkt_{dkt_num}"
            if base_key in seen:        # dedup repeated NEFs for the same filing
                continue
            seen.add(base_key)
            in_ledger = any(k.startswith(base_key) for k in ledger.get("downloaded", {}))
            try:
                dt = email.utils.parsedate_to_datetime(hdrs.get("date", ""))
                date_iso = dt.date().isoformat()
            except Exception:
                date_iso = ""
            dtext = DOCKET_TEXT_RE.search(clean)
            docket_text = (dtext.group(1).strip() if dtext else subj)
            fm = re.search(r"Filer:\s*(.+?)\s*(?:Document Number|Docket Text|\n)", clean, re.I)
            filer = _clean_filer(fm.group(1) if fm else "")
            ours = _recon_ours(filer, ptoks) if filer else False   # robust token-match, not 'surname in filer'
            # US-party (60-day FRAP 4(a)(1)(B)) keys off the CAPTION only, not stray body text.
            us_party = bool(re.search(r"\bUNITED STATES\b|\bU\.S\.\s|\bUSA\b|DEPARTMENT OF|\bSECRETARY\b|\bCOMMISSIONER\b", subj, re.I))
            tasks.append({"case": case_num, "dkt": dkt_num, "slug": slug_for(case_num),
                          "desc": re.sub(r"[^a-z0-9]+", "_", subj.lower())[:40] or "doc",
                          "url": ecf.group(0), "subject": subj, "in_ledger": in_ledger,
                          "date": date_iso, "text": docket_text, "us_party": us_party,
                          "filer": filer, "ours": ours})
        elif "phila.gov" in raw.lower():
            opendoc = PHILA_OPENDOC.search(raw)          # the ACTUAL order (opens the PDF in a new window)
            link = opendoc or PHILA_DOC.search(raw)
            url = link.group(0) if link else "(no link)"
            # generic ONLY when there is no real order link (the NLR-FJD.pdf cover alone / no link)
            generic = (opendoc is None) and (url.lower().endswith("nlr-fjd.pdf") or url == "(no link)")
            phila.append({"case": case_num, "subject": subj, "url": url, "generic": generic,
                          "opendoc": bool(opendoc)})
        elif re.search(r"uscourts\.gov", hdrs.get("from", ""), re.I) or "uscourts.gov" in raw.lower():
            # NOTICE-ONLY NEF: no document free-look link, but a real docket EVENT — e.g. "Notice of
            # Docketing Record on Appeal", USCA notices, text-only orders. Previously dropped (needs a
            # doc1/magic_num link), so appeal/deadline events silently vanished. Capture it: no download,
            # no free look spent, but the case + deadline tracking must see it.
            dtext = DOCKET_TEXT_RE.search(clean)
            docket_text = (dtext.group(1).strip() if dtext else subj)
            # the 400-char DOTALL grab swallows the "Notice has been electronically mailed to:" mailing
            # list — cut it at that boundary and collapse whitespace so the logged text is the event only.
            docket_text = re.split(r"Notice has been electronically|Notice will be", docket_text, 1, re.I)[0]
            docket_text = re.sub(r"\s+", " ", docket_text).strip()
            try:
                _dt = email.utils.parsedate_to_datetime(hdrs.get("date", "")); date_iso = _dt.date().isoformat()
            except Exception:
                date_iso = ""
            dktrpt = re.search(r"https?://ecf\.[a-z]+\.uscourts\.gov/cgi-bin/DktRpt\.pl\?\d+", raw, re.I)
            appeal = bool(re.search(r"appeal|USCA|record on appeal|briefing|docketing", subj + " " + docket_text, re.I))
            # Clerk docketing notices carry no Filer; a real NOTICE OF APPEAL filing does. Also, the
            # notice PROSE often names the appellant directly ("Notice of Appeal filed by <Name>") and
            # the Third Circuit case number ("USCA Case Number 26-0002") — capture both.
            _fm = re.search(r"Filer:\s*(.+?)\s*(?:Document Number|Docket Text|\n)", clean, re.I)
            ev_filer = _clean_filer(_fm.group(1) if _fm else "")
            _fb = re.search(r"filed by\s+([A-Z][A-Za-z.\-]+(?:\s+[A-Z][A-Za-z.\-]+){0,3})", docket_text)
            if not ev_filer and _fb:
                ev_filer = _fb.group(1).strip()
            ev_ours = _recon_ours(ev_filer, ptoks) if ev_filer else None
            usca = re.search(r"USCA\s+Case\s+Number\s+([\d\-]+)", docket_text, re.I)
            events.append({"case": case_num, "dkt": dkt_num, "slug": slug_for(case_num), "subject": subj,
                           "text": docket_text, "date": date_iso, "filer": ev_filer, "ours": ev_ours,
                           "usca": usca.group(1) if usca else "",
                           "docket_report": dktrpt.group(0) if dktrpt else "", "appeal": appeal})
    return tasks, phila, events, ledger


def main():
    # LETTER STREAM: piggyback on this Gmail pull to refresh the record of what was physically MAILED
    # (LetterStream/OnlineCertifiedMail confirmations) so m0 skips re-drafting sent items. Non-fatal.
    try:
        from pipeline.letterstream_mail_fetch import sync as _ls_sync
        _ls_sync()
    except Exception as _e:
        print(f"  [letterstream-sync] skipped (non-fatal): {_e}")
    tasks, phila, events, ledger = scan()
    new_ecf = [t for t in tasks if not t["in_ledger"]]
    if ONLY:
        new_ecf = [t for t in new_ecf if t["dkt"] in ONLY]
    print(f"=== ECF/PACER (free-look) — {len(tasks)} found, {len(new_ecf)} NEW (not in ledger) ===")
    for t in tasks:
        print(f"  [{'NEW' if not t['in_ledger'] else 'have'}] {t['case']} dkt {t['dkt']} | {t['subject']}")
    # NOTICE-ONLY docket events (no document to download, but real docket activity — appeals, orders,
    # docketing notices). These were previously dropped; now logged to each case so deadlines update.
    print(f"\n=== DOCKET EVENTS (notice-only, no document) — {len(events)} ===")
    from pathlib import Path as _P

    def _appellant_for(case_num):
        """Resolve who filed the appeal for a case from the NOTICE OF APPEAL filing (the ecf-branch
        task carries the Filer + ours flag; the clerk docketing notice does not)."""
        for t in tasks:
            if t.get("case") == case_num and re.search(r"notice of appeal", t.get("text", ""), re.I):
                if t.get("ours"):
                    return "YOU (self — appellant; FRAP 31 opening-brief clock is yours)"
                if t.get("filer"):
                    return f"{t['filer']} (opposing — you are appellee/cross-clock)"
        return None

    for e in events:
        flag = "  *** APPEAL / DEADLINE-TRIGGER ***" if e.get("appeal") else ""
        # who appealed: from this notice's own Filer, else correlate to the NOTICE OF APPEAL filing
        appellant = None
        if e.get("appeal"):
            if e.get("ours"):
                appellant = "YOU (self — appellant; FRAP 31 opening-brief clock is yours)"
            elif e.get("filer"):
                appellant = f"{e['filer']} (opposing — you are appellee)"
            else:
                appellant = _appellant_for(e["case"])
        who = f"  [appellant: {appellant}]" if appellant else ""
        uc = f"  [USCA {e['usca']}]" if e.get("usca") else ""
        print(f"  {e['case']} dkt {e['dkt']} [{e['date']}] | {e['text'][:70]}{flag}{uc}{who}")
        try:
            ev_dir = _P(f"<home>/obsidian_sync/cases/{e['slug']}")
            if ev_dir.exists():
                ev_file = ev_dir / "docket_events.md"
                line = (f"- {e['date']} | dkt {e['dkt']} | {e['text']}"
                        f"{' | USCA ' + e['usca'] if e.get('usca') else ''} | {e.get('docket_report','')}"
                        f"{' | APPEAL/DEADLINE-TRIGGER (FRAP 31 briefing clock)' if e.get('appeal') else ''}"
                        f"{' | appellant: ' + appellant if appellant else ''}\n")
                # dedup: same date+dkt+text already logged (re-run safe — this runs on every email pull)
                sig = f"{e['date']} | dkt {e['dkt']} | {e['text']}"
                prior = ev_file.read_text(encoding="utf-8") if ev_file.exists() else ""
                if sig not in prior:
                    with open(ev_file, "a", encoding="utf-8") as f:
                        f.write(line)
        except Exception:
            pass
    print(f"\n=== Phila CCP — {len(phila)} notice email(s) ===")
    for p in phila:
        tag = "GENERIC notice only (order is behind the FJD portal)" if p["generic"] else p["url"][:60]
        print(f"  {p['case']} | {p['subject']} | {tag}")

    if GATE:
        print("\n=== m0 REPLY GATE (cutoff = today; older = reference-only) ===")
        by_case = {}
        for t in tasks:
            by_case.setdefault(t["slug"], []).append(t)
        for slug, fs in by_case.items():
            pro_se = (re.search(r"([A-Za-z]+)\s+v\.?\s", fs[0]["subject"]) or [None, ""])[1]
            us_party = any(f.get("us_party") for f in fs)
            res = gate.process_case(slug, fs, TODAY, pro_se_name=pro_se, us_is_party=us_party, commit=False)
            by_dkt = {str(t["dkt"]): t for t in fs}
            case_id = "fed:" + (fs[0].get("case") or "")
            print(f"\n  {slug}  (cutoff {res['cutoff']}, us_party={us_party})")
            for a in res["actions"]:
                extra = a.get("vehicle") or a.get("authority") or a.get("route") or ""
                also = f"  (+also {a['also_file']['vehicle']})" if a.get("also_file") else ""
                print(f"    dkt {a['dkt']:>4} -> {a['action']:<10} {extra}{also} | {a['reason'][:60]}")
                if DRAFT and a.get("action") == "reply":   # draft_for_new_filing self-guards on vehicle (m0)
                    try:
                        from pipeline import auto_reply
                        d = auto_reply.draft_for_new_filing(case_id, slug, by_dkt.get(str(a["dkt"]), {}), a)
                        print(f"           DRAFTED {d['vehicle']} via {d.get('engine','m0/motion_drafter_v2')}"
                              f" -> {d['draft_path']}")
                    except Exception as _de:
                        print(f"           DRAFT skipped (non-fatal): {_de}")

    if not DOWNLOAD:
        print("\nDRY-RUN (no free look spent). Re-run with --download to fetch the NEW ones.")
        return

    os.makedirs(INTAKE, exist_ok=True)
    # never PACER-download our OWN filings (already local) unless explicitly named via --only
    skip_ours = [t for t in new_ecf if t.get("ours") and not ONLY]
    for t in skip_ours:
        print(f"  SKIP (our own filing, already local): {t['case']} dkt {t['dkt']} filer={t['filer']!r}")
    new_ecf = [t for t in new_ecf if not (t.get("ours") and not ONLY)]
    if len(new_ecf) > MAX:
        print(f"  CAP: {len(new_ecf)} new adverse doc(s); limiting to {MAX} this run (money backstop). "
              f"Re-run with --max=N to raise.")
        new_ecf = new_ecf[:MAX]
    if new_ecf:
        s = pd.pacer_login(); print("PACER LOGIN OK")
        for t in new_ecf:
            save_dir = f"{pd.CASES_BASE}/{t['slug']}/intake"
            print(f"\n[{t['case']} dkt {t['dkt']}] {t['subject']}")
            pd.download_pacer_doc(s, t["url"], save_dir, t["case"], t["dkt"], t["desc"], ledger)
        pd.save_ledger(ledger)
        # STRAIGHT-LINE FIX (2026-07-10): the pull used to stop at saving the PDF, forking the flow — the
        # drafter (m0_draft_reply) reads CASE_TIMELINE.json and doc_reader reads the text, and the pull
        # updated neither. Now, for each case that got a new filing, REGISTER it into the timeline
        # (ecf_resolver.tie_docket) and PRE-EXTRACT/OCR its text (doc_reader -> ocr_engine fallback), so
        # pull -> register -> OCR -> draft is one straight line. Idempotent + non-fatal.
        for _slug in sorted({t["slug"] for t in new_ecf}):
            try:
                from pipeline import ecf_resolver
                ecf_resolver.tie_docket(_slug, verbose=False)
                print(f"  [straight-line] registered new dockets into CASE_TIMELINE: {_slug}")
            except Exception as _te:
                print(f"  [straight-line] tie_docket({_slug}) skipped (non-fatal): {_te}")
            try:
                import glob as _glob
                from pipeline import doc_reader
                for _pdf in _glob.glob(f"{pd.CASES_BASE}/{_slug}/intake/*.pdf"):
                    doc_reader.extract_text(_pdf)
                print(f"  [straight-line] text ready (cached) for: {_slug}")
            except Exception as _ee:
                print(f"  [straight-line] pre-extract({_slug}) skipped (non-fatal): {_ee}")
            # ONE FLOW: OCR is not the end of the line — the analysis is what the
            # drafting lane reads. Run it, then prepare the m1 feed.
            if run_analysis(_slug, "(new ECF filing)"):
                build_draft_feed(_slug)
    for p in phila:
        if p["generic"]:
            print(f"  Phila SKIP (generic notice, order is on the FJD portal): {p['subject']}")
            continue
        if p["url"].startswith("http"):
            try:
                if p.get("opendoc"):
                    pdf = _fetch_phila_pdf(p["url"])          # 3-hop order fetch
                else:
                    _r = requests.get(p["url"], timeout=30)
                    pdf = _r.content if _r.content[:4] == b"%PDF" else None
                if pdf:
                    # save into the CASE intake so the straight-line flow (register -> OCR -> facts -> m0
                    # draft) picks it up, just like PACER docs. Registry resolution first so the
                    # document lands in the REAL case folder; legacy prefix only as fallback.
                    _slug = ccp_slug_for(p["case"]) or ("phila_ccp_" + str(p["case"]).replace(":", "_"))
                    _save_dir = f"{pd.CASES_BASE}/{_slug}/intake"
                    os.makedirs(_save_dir, exist_ok=True)
                    fn = f"{_save_dir}/ccp_order_{p['case']}_{re.sub(r'[^a-z0-9]+','_',p['subject'].lower())[:24]}.pdf"
                    open(fn, "wb").write(pdf); print(f"  Phila SAVED order -> {os.path.basename(fn)}")
                    try:
                        from pipeline import ecf_resolver, doc_reader
                        ecf_resolver.tie_docket(_slug, verbose=False)
                        doc_reader.extract_text(fn)          # OCR/extract inline (straight line)
                        print(f"  [straight-line] registered + text ready: {_slug}")
                    except Exception as _ce:
                        print(f"  [straight-line] CCP register/extract skipped (non-fatal): {_ce}")
                    if run_analysis(_slug, "(new state-court filing)"):
                        build_draft_feed(_slug)
                else:
                    print(f"  Phila link not a fetchable PDF: {p['subject']}")
            except Exception as e:
                print(f"  Phila fetch error: {e}")


def run_analysis(slug, why=""):
    """THE MISSING LINK (2026-09-23). The straight line claimed 'pull -> register -> OCR ->
    draft' but stopped at OCR: nothing invoked the m0 analysis, so a new adverse filing sat
    in the case intake with no ADVERSE-FILING DEFECTS & LEVERS section, and the drafting
    lane had nothing to draft from. Every analysis run was manual.

    ONE FLOW: new filing -> case intake -> register -> OCR -> m0 ANALYSIS (this) -> m1 feed
    -> m1 draft. Bounded and non-fatal: a failure prints and the pull still succeeds.
    """
    import subprocess as _sp
    env = dict(os.environ)
    # per local-gpu-vllm-serving: case_facts_refresh reads BEEZLE_LLAMA_URL (FULL path) and
    # BEEZLE_CF_MODEL; the module default :8000 is dead on this box.
    env.setdefault("BEEZLE_LLAMA_URL", "http://localhost:8000/v1/chat/completions")
    env.setdefault("BEEZLE_CF_MODEL", "qwen3.8-27b")
    print(f"  [straight-line] m0 ANALYSIS -> {slug} {why}".rstrip())
    try:
        r = _sp.run([sys.executable, "-m", "pipeline.case_facts_refresh", slug],
                    cwd=str(_eroot()), env=env, capture_output=True, text=True,
                    timeout=ANALYSIS_TIMEOUT)
        tail = [l for l in (r.stdout or "").splitlines() if "section [" in l or "REJECTED" in l]
        for l in tail[-8:]:
            print(f"    {l.strip()}")
        if r.returncode != 0:
            print(f"  [straight-line] analysis exit={r.returncode} (non-fatal)")
            return False
        return True
    except _sp.TimeoutExpired:
        print(f"  [straight-line] analysis TIMEOUT after {ANALYSIS_TIMEOUT}s — rerun "
              f"`python3 -m pipeline.case_facts_refresh {slug}`")
        return False
    except Exception as _e:
        print(f"  [straight-line] analysis skipped (non-fatal): {str(_e)[:90]}")
        return False


def build_draft_feed(slug):
    """After analysis, derive the m1 rebuttal matrix + bank so the drafting lane is ready.
    Separate from drafting itself — preparing is automatic, filing stays the human's act."""
    try:
        from pipeline.m1_feed import build as _feed
        mp, bp = _feed(slug)
        print(f"  [straight-line] m1 feed ready: {os.path.basename(str(mp))} / "
              f"{os.path.basename(str(bp))}")
        return True
    except Exception as _e:
        print(f"  [straight-line] m1 feed skipped (non-fatal): {str(_e)[:90]}")
        return False


if __name__ == "__main__":
    main()
