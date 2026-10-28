#!/usr/bin/env python3
"""mailing_tracker.py — single source of truth for outbound mail (m0 organ).

USER DIRECTIVE (2026-08-27): "we need a better tracking system for mailing."
The old mailing_ledger.json was scraped Gmail subjects with no lifecycle.
This organ tracks each mailing as a RECORD with a strict lifecycle:

    DRAFTED -> GATED -> LOADED -> AWAITING_APPROVAL -> SENT -> CONFIRMED
                                        (LetterStream)        (Gmail notice)

Registry: output/audit/mailing_tracker.json
  items keyed by mail_id (case+doc+recipient hash), each carrying:
    case_id, document (artifact path), recipients[], court, batch,
    letterstream_job_id, status, history[(ts, status, note)],
    gate_verdict, docket_deduped (bool), class (first-class/certified)

Rules baked in:
  - An item can only advance FORWARD in the lifecycle (no silent regressions;
    a redraft resets to DRAFTED explicitly with a note).
  - Nothing reaches LOADED without gate_verdict == PASS and docket_deduped.
  - reconcile() reads Gmail-scraped statuses (mailing_ledger.json import) and
    LetterStream job ids; stale drafts (artifact mtime newer than LOADED ts)
    are flagged REDRAFT_NEEDED.
  - board() prints the whole pipeline state grouped by batch.

PUSH-MARKER INGEST + CONFIRM PATH (class-level fix, card t_1a52fd92, 2026-10-12):
  The push flow (efiling/letterstream_mail.py) wrote .pushed markers with the
  LetterStream job number but never told this organ, so the board was blind to
  the cart and the Gmail confirm engine could not match by job id. Now:
  - link_dir(dirpath, job_id): called by the push flow THE MOMENT a job lands —
    links the marker to an existing item (fuzzy doc match / exact job id) or
    adds an IN_CART item with letterstream_job_id + dated history row.
  - sync_markers(reg): the same link-or-add over EVERY .pushed marker on disk
    (efiling/letterstream_consolidated/ and efiling/letterstream_final/);
    idempotent — a second pass adds nothing. Runs inside board and reconcile.
  - confirm_sweep(reg): a SENT item whose job id has a scraped confirmation
    email in reg["job_events"] (Order Confirmation OR Job Completed,
    case-insensitive) advances to CONFIRMED with a dated history row.
  - refresh_markers_for(job_id): rewrites a stale .pushed first line
    ('IN CART (Payment Required)' / 'READY FOR PAYMENT') once the tracker item
    has passed that stage; annotation lines below the first line are preserved.
  - `confirm <mail_id>` CLI: manual SENT->CONFIRMED; no-op if already CONFIRMED.
  - `link [--job <id>]` CLI: ingest push markers now (idempotent).
  - board auto-syncs markers + confirm sweep before rendering (pure read with
    --no-sync). The board can never be blind to the cart again.

CLI:
  python3 -m pipeline.mailing_tracker board [--no-sync]
  python3 -m pipeline.mailing_tracker add --case X --doc path --recipients "A;B" --batch A
  python3 -m pipeline.mailing_tracker advance <mail_id> <STATUS> [--note ...] [--job-id ...]
  python3 -m pipeline.mailing_tracker confirm <mail_id> [--note ...]
  python3 -m pipeline.mailing_tracker link [--job <id>]
  python3 -m pipeline.mailing_tracker reconcile   # import legacy ledger + push markers + confirm sweep + flag stale
"""
import json, sys, re, hashlib, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REG = ROOT / "output" / "audit" / "mailing_tracker.json"
PUSHED_DIRS = [ROOT / "efiling" / "letterstream_consolidated",
               ROOT / "efiling" / "letterstream_final"]
PUSHED_JOB_RE = re.compile(r"job\s*#?\s*(\d{6,})", re.I)
# Both confirmation subject lines, case-insensitive (jurist item t_1a52fd92):
# Order Confirmation = paid/entered production; Job Completed = delivered.
CONFIRM_RE = re.compile(r"order\s+confirmation|job\s+completed", re.I)
LIFECYCLE = ["DRAFTED", "GATED", "LOADED", "IN_CART", "AWAITING_APPROVAL",
             "SENT", "CONFIRMED", "FILED"]
# IN_CART  = loaded into the LetterStream cart, awaiting the user's payment (cart-stop).
# FILED    = terminal for items whose delivery was an e-filing, not a mailing.
# Both were already being written into output/audit/mailing_tracker.json by other
# organs; board() crashed on them (ValueError: not in list). Registered here so the
# lifecycle index is total over the statuses that actually occur in the registry.

def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")

def _load():
    if REG.exists():
        return json.loads(REG.read_text())
    return {"created": _now(), "items": {}}

def _save(reg):
    reg["updated"] = _now()
    REG.write_text(json.dumps(reg, indent=1))

def mail_id(case_id, doc, recipient_key):
    h = hashlib.sha1(f"{case_id}|{Path(doc).name}|{recipient_key}".encode()).hexdigest()[:10]
    return f"mail_{h}"

def add(case_id, doc, recipients, batch="", court="", mail_class="first-class",
        status="DRAFTED", job_id=None, note=""):
    reg = _load()
    rk = ";".join(sorted(recipients)) if isinstance(recipients, list) else str(recipients)
    mid = mail_id(case_id, doc, rk)
    it = reg["items"].get(mid) or {"history": []}
    it.update({
        "case_id": case_id, "document": str(doc), "recipients": recipients if isinstance(recipients, list) else [recipients],
        "batch": batch, "court": court, "class": mail_class,
        "status": status, "letterstream_job_id": job_id,
    })
    it["history"].append([_now(), status, note or "added/updated"])
    reg["items"][mid] = it
    _save(reg)
    return mid

def advance(mid, status, note="", job_id=None):
    reg = _load()
    it = reg["items"].get(mid)
    if not it:
        print(f"unknown mail_id {mid}"); return False
    cur = it.get("status", "DRAFTED")
    if status not in LIFECYCLE:
        print(f"bad status {status}; lifecycle = {LIFECYCLE}"); return False
    if LIFECYCLE.index(status) < LIFECYCLE.index(cur) and status != "DRAFTED":
        print(f"refusing silent regression {cur} -> {status}; use DRAFTED with a note for redrafts"); return False
    if status == "LOADED":
        if it.get("gate_verdict") != "PASS":
            print(f"BLOCKED: {mid} cannot LOAD without gate_verdict=PASS (have {it.get('gate_verdict')})"); return False
        if not it.get("docket_deduped"):
            print(f"BLOCKED: {mid} cannot LOAD without docket_deduped=true"); return False
    it["status"] = status
    if job_id: it["letterstream_job_id"] = job_id
    it["history"].append([_now(), status, note])
    _save(reg)
    # class-level (t_1a52fd92): once an advance carries the item past the cart,
    # keep the .pushed marker text honest in the same pass (the mail-watch cron
    # advances SENT/CONFIRMED from Gmail confirmations — that path refreshes too).
    if status in ("SENT", "CONFIRMED", "FILED") and it.get("letterstream_job_id"):
        try:
            refresh_markers_for(it["letterstream_job_id"])
        except Exception:
            pass
    return True

def set_gate(mid, verdict, deduped=None):
    reg = _load()
    it = reg["items"].get(mid)
    if not it: return False
    it["gate_verdict"] = verdict
    if deduped is not None: it["docket_deduped"] = bool(deduped)
    it["history"].append([_now(), it.get("status", "?"), f"gate={verdict} deduped={deduped}"])
    _save(reg)
    return True

# ── PUSH-MARKER INGEST (card t_1a52fd92, class-level fix 2026-10-12) ─────────
def _stem_tokens(name):
    """Identity tokens of a document/filing name (extension, suffixes and
    stop-words stripped) for fuzzy push-marker matching."""
    n = Path(str(name)).name.lower()
    n = re.sub(r"\.[a-z0-9]{1,5}$", "", n)
    for suf in ("_filing", "_draft", "_finished", "_fil", "_m0"):
        if n.endswith(suf):
            n = n[:-len(suf)]
    n = re.sub(r"[_\-.\\/ ]+", " ", n)
    n = re.sub(r"[^a-z0-9]+", " ", n)
    toks = set(n.split())
    toks.difference_update({"packet", "consolidated", "counsel", "copy",
                            "the", "a", "of", "md"})
    return toks

def _match_item(reg, tokens, job_id):
    """Best existing item for a push marker.

    Exact: an item already carrying this exact letterstream_job_id (re-push).
    Fuzzy: token-Jaccard >= 0.5 between the item's document name and the
    marker's FILING tokens. Returns mail_id or None."""
    for mid, it in reg["items"].items():
        if str(it.get("letterstream_job_id") or "") == str(job_id):
            return mid
    best, best_j = None, 0.0
    for mid, it in reg["items"].items():
        toks = _stem_tokens(it.get("document") or "")
        if not toks or not tokens:
            continue
        union = len(toks | tokens)
        j = len(toks & tokens) / union if union else 0.0
        if j > best_j:
            best, best_j = mid, j
    return best if best_j >= 0.5 else None

def _pm_record(dirpath):
    """Build the same marker record scan_pushed() yields for one directory."""
    dp = Path(dirpath)
    man = {}
    mp = dp / "manifest.json"
    if mp.exists():
        try:
            man = json.loads(mp.read_text())
        except Exception:
            man = {}
    parts = dp.name.split("__")
    pm_path = dp / ".pushed"
    marker = ""
    if pm_path.exists():
        try:
            marker = (pm_path.read_text().strip().splitlines() or [""])[0]
        except Exception:
            marker = ""
    return {"path": pm_path, "dir": dp.name,
            "filing": man.get("filing") or (parts[0] if parts else dp.name),
            "recipient": (lambda r: r.get("name") if isinstance(r, dict) else (r or None))(man.get("recipient")) or (parts[1] if len(parts) > 1 else dp.name),   # consolidate_final writes a str, older manifests a dict
            "case": man.get("case") or "",
            "court": man.get("court") or man.get("court_match") or "",
            "mail_class": man.get("mail_class") or "first-class",
            "marker": marker, "mtime": pm_path.stat().st_mtime if pm_path.exists() else 0}

def scan_pushed():
    """Every .pushed marker on disk that names a LetterStream job number
    (newest mtime last, so on duplicate markers the newer job wins)."""
    out = []
    for d in PUSHED_DIRS:
        if not d.exists():
            continue
        for p in sorted(d.glob("*/.pushed")):
            try:
                text = p.read_text()
            except Exception:
                continue
            m = PUSHED_JOB_RE.search(text)
            if not m:
                continue
            rec = _pm_record(p.parent)
            rec["job_id"] = m.group(1)
            out.append(rec)
    out.sort(key=lambda r: r.get("mtime") or 0)
    return out

def _link_one(pm, reg, now, job_index):
    """Link-or-add ONE push marker into `reg` (mutates, caller saves).
    Idempotent: a marker whose job id is already registered only refreshes
    the marker text. Returns a report row."""
    jid = str(pm["job_id"])
    if jid in job_index:
        _refresh_marker(pm, job_index[jid], reg, now)
        return {"job": jid, "action": "present",
                "mail_id": job_index[jid][0], "dir": pm["dir"]}
    mid = _match_item(reg, _stem_tokens(pm["filing"]), jid)
    if mid is not None:
        it = reg["items"][mid]
        it["letterstream_job_id"] = jid
        it.setdefault("history", []).append(
            [now, "LINKED", f"job #{jid} linked from .pushed marker (card t_1a52fd92)"])
        cur = it.get("status", "DRAFTED")
        if cur in LIFECYCLE and LIFECYCLE.index(cur) < LIFECYCLE.index("IN_CART"):
            it["status"] = "IN_CART"
            it["history"].append(
                [now, "IN_CART", f"push marker: {pm['marker']!r} (job #{jid})"])
        job_index[jid] = [mid]
        _refresh_marker(pm, [mid], reg, now)
        return {"job": jid, "action": "linked", "mail_id": mid, "dir": pm["dir"]}
    it = {
        "case_id": pm["case"] or pm["filing"],
        "document": pm["filing"],
        "recipients": [pm["recipient"]] if pm["recipient"] else [],
        "batch": "letterstream-cart",
        "court": pm["court"],
        "class": pm["mail_class"],
        "status": "IN_CART",
        "letterstream_job_id": jid,
        "history": [[now, "IN_CART",
                     f"ingested from .pushed marker: {pm['marker']!r} (card t_1a52fd92)"]],
    }
    mid = mail_id(it["case_id"], pm["filing"], pm["recipient"])
    reg["items"][mid] = it
    job_index[jid] = [mid]
    _refresh_marker(pm, [mid], reg, now)
    return {"job": jid, "action": "added", "mail_id": mid, "dir": pm["dir"]}

def sync_markers(reg, now=None, only_job=None):
    """Ingest every .pushed marker into `reg` (mutates; caller saves).
    Idempotent — card t_1a52fd92 acceptance: after this runs, every job number
    in the .pushed scan exists in the tracker; a second pass adds nothing."""
    now = now or _now()
    job_index = {}
    for mid, it in reg["items"].items():
        if it.get("letterstream_job_id"):
            job_index.setdefault(str(it["letterstream_job_id"]), []).append(mid)
    report = []
    for pm in scan_pushed():
        if only_job and pm["job_id"] != str(only_job):
            continue
        report.append(_link_one(pm, reg, now, job_index))
    return report

def confirm_sweep(reg, now=None):
    """SENT items whose LS job has a scraped confirmation email -> CONFIRMED.
    Matches BOTH subject patterns, case-insensitive — the 260100000 batch was
    stuck because only one pattern was checked (jurist item t_1a52fd92).
    Mutates reg (caller saves); returns the count confirmed."""
    now = now or _now()
    events = reg.get("job_events") or {}
    n = 0
    for mid, it in reg["items"].items():
        if it.get("status") != "SENT":
            continue
        jid = str(it.get("letterstream_job_id") or "")
        if not jid:
            continue
        subj = None
        for _ts, s in (events.get(jid) or []):
            if CONFIRM_RE.search(s or ""):
                subj = s
                break
        if subj is None:
            continue
        it["status"] = "CONFIRMED"
        it.setdefault("history", []).append(
            [now, "CONFIRMED", f"reconcile sweep: {subj[:70]!r} for job #{jid}"])
        for pm in scan_pushed():
            if pm["job_id"] == jid:
                _refresh_marker(pm, [mid], reg, now)
        n += 1
    return n

def _refresh_marker(pm, mids, reg, now):
    """Rewrite a stale .pushed first line to match the tracker status once the
    item has passed the cart. Annotation lines below the first line (e.g. the
    strategist note on the PA_AG marker) are preserved. No-op when the tracker
    is not further along than the marker already says."""
    statuses = [(reg.get("items") or {}).get(m, {}).get("status") for m in mids]
    furthest = None
    for st in statuses:
        if st in LIFECYCLE and (furthest is None or LIFECYCLE.index(st) > LIFECYCLE.index(furthest)):
            furthest = st
    if furthest is None or LIFECYCLE.index(furthest) < LIFECYCLE.index("IN_CART"):
        return
    try:
        text = pm["path"].read_text()
    except Exception:
        return
    lines = text.splitlines()
    if not lines:
        return
    first = lines[0]
    m = PUSHED_JOB_RE.search(first)
    if not m or m.group(1) != str(pm["job_id"]):
        return
    if furthest.replace("_", " ") in first.replace("_", " "):
        return
    lines[0] = f"LetterStream job #{pm['job_id']}, {furthest.replace('_', ' ')} {now[:10]} (tracker-synced)"
    try:
        pm["path"].write_text("\n".join(lines) + ("\n" if text.endswith("\n") else ""))
    except Exception:
        pass

def refresh_markers_for(job_id, reg=None):
    """Rewrite the stale first line of one job's .pushed marker once its tracker
    item has passed that stage. Called after advances/confirm so the marker text
    can't lie about the cart state. No registry mutation."""
    jid = str(job_id or "")
    if not jid:
        return
    reg = reg if reg is not None else _load()
    mids = [m for m, it in reg["items"].items()
            if str(it.get("letterstream_job_id") or "") == jid]
    if not mids:
        return
    for pm in scan_pushed():
        if pm["job_id"] == jid:
            _refresh_marker(pm, mids, reg, _now())

def link_dir(dirpath, job_id=None):
    """Push-time hook (efiling/letterstream_mail.py): link ONE push directory's
    .pushed marker into the registry the moment the job lands in the cart, so
    the board can never be blind to it. Idempotent (sync_markers re-runs the
    same link-or-add over every directory; a second pass is a no-op)."""
    dp = Path(dirpath)
    if not (dp / ".pushed").exists():
        return {"ok": False, "reason": "no .pushed marker"}
    reg = _load()
    now = _now()
    if not job_id:
        m = PUSHED_JOB_RE.search((dp / ".pushed").read_text())
        job_id = m.group(1) if m else None
    if not job_id:
        return {"ok": False, "reason": "no job id in .pushed marker"}
    pm = _pm_record(dp)
    pm["job_id"] = str(job_id)
    job_index = {}
    for mid, it in reg["items"].items():
        if it.get("letterstream_job_id"):
            job_index.setdefault(str(it["letterstream_job_id"]), []).append(mid)
    rep = _link_one(pm, reg, now, job_index)
    _save(reg)
    return {"ok": True, **rep}

def link(job=None):
    """CLI wrapper: ingest push markers now (idempotent) + confirm sweep."""
    reg = _load()
    now = _now()
    rep = sync_markers(reg, now=now, only_job=job)
    nconf = confirm_sweep(reg, now=now)
    if rep or nconf:
        _save(reg)
    print(json.dumps({"linked": rep, "confirmed": nconf}, indent=1))
    return rep

def confirm(mid, note=""):
    """Manual SENT->CONFIRMED (user confirmation). No-op if already CONFIRMED.
    Advances even if the status is earlier (user is the authority); a regression
    below SENT is the one place a backstep is allowed by human order."""
    reg = _load()
    it = reg["items"].get(mid)
    if not it:
        cands = [m for m in reg["items"] if m.endswith(mid)]
        if len(cands) == 1:
            it = reg["items"][cands[0]]
        else:
            print(f"unknown mail_id {mid}"); return False
    if it.get("status") == "CONFIRMED":
        print(f"no-op: {mid} already CONFIRMED"); return True
    if it.get("status") == "FILED":
        print(f"no-op: {mid} already FILED (terminal)"); return True
    it["status"] = "CONFIRMED"
    it.setdefault("history", []).append(
        [_now(), "CONFIRMED", note or "user confirmed delivery (CLI)"])
    _save(reg)
    if it.get("letterstream_job_id"):
        try:
            refresh_markers_for(it["letterstream_job_id"])
        except Exception:
            pass
    print(f"confirmed {mid}")
    return True

# ─────────────────────────────────────────────────────────────────────────────
# IDENTITY DEDUPE (class-level fix, card t_40130a34, 2026-10-17) ──────────────
# Two ingestion paths (packet_*.pdf push/Gmail-scrape and COVER_*.md payment
# reconciliation) create items for the SAME physical mailing under different
# mail_ids, so the board simultaneously asserts a mailing is paid and unpaid.
# A LetterStream job id IS the physical mailing: a group of items sharing a
# non-empty job id that are all MAILING items (not clock objects, real
# recipients) is ONE mailing. Collapsed to the item furthest along the
# lifecycle (first-seen on a tie); losers get a SUPERSEDED history row naming
# the survivor. A recipient+doc-token join catches pairs whose job was deleted
# at LetterStream (e.g. IRS Austin, job 15050595). Idempotent: a second pass
# supersedes nothing.

def _recipient_key(it):
    """Canonical first recipient token (case/stop-insensitive) for a mailing
    item, or '' when the item carries no real recipient (clock items, scans)."""
    rcpt = it.get("recipients") or it.get("recipient") or []
    if isinstance(rcpt, str):
        rcpt = [rcpt]
    for r in rcpt:
        r = str(r).strip()
        if not r:
            continue
        toks = re.sub(r"[^a-z0-9]+", " ", r.lower()).split()
        if toks and toks[0] not in ("unsent", "unsentrecipient"):
            return toks[0]
    return ""

def _is_mailing_item(it):
    """True when the item is an actual mailing (not a clock object riding a
    job id, not a scan import with no recipient)."""
    if "clock" in it:
        return False
    if not _recipient_key(it):
        return False
    return True

def _doc_tokens(doc):
    """Token set of a document path (extension + cover_/packet_ prefixes +
    stop-words stripped) for the recipient+doc identity join."""
    n = Path(str(doc or "")).name.lower()
    n = re.sub(r"\.[a-z0-9]{1,5}$", "", n)
    for pfx in ("cover_", "packet_"):
        if n.startswith(pfx):
            n = n[len(pfx):]
    n = re.sub(r"[_\-.\s]+", " ", n)
    toks = set(n.split())
    toks.difference_update({"packet", "consolidated", "counsel", "copy",
                            "the", "a", "of", "md", "pdf"})
    return toks

def _dedupe_identity(reg, now=None):
    """Collapse duplicate-identity items in `reg` (mutates; caller saves).
    Returns [(loser_mail_id, survivor_mail_id, why), ...]. Never touches clock
    objects; only folds items that share a job id (mailing items) or share
    recipient+doc tokens."""
    now = now or _now()
    items = reg.get("items", {})
    order = [m for m in items if items[m].get("status") != "SUPERSEDED"]
    report = []
    survivors = {}  # loser -> survivor
    for i, a_mid in enumerate(order):
        a = items.get(a_mid)
        if not a or not _is_mailing_item(a):
            continue
        ca = (a.get("case_id") or "").strip()
        ja = str(a.get("letterstream_job_id") or "")
        ta = _doc_tokens(a.get("document"))
        ra = _recipient_key(a)
        for b_mid in order[i + 1:]:
            if b_mid in survivors:
                continue
            b = items.get(b_mid)
            if not b or not _is_mailing_item(b) or b.get("status") == "SUPERSEDED":
                continue
            cb = (b.get("case_id") or "").strip()
            if not ca or ca != cb:
                continue
            jb = str(b.get("letterstream_job_id") or "")
            why = None
            if ja and ja == jb:
                why = "same job id"
            elif ra and ra == _recipient_key(b):
                tb = _doc_tokens(b.get("document"))
                union = len(ta | tb)
                jac = len(ta & tb) / union if union else 0.0
                if jac >= 0.5:
                    why = "recipient+doc"
            if not why:
                continue
            ia = LIFECYCLE.index(a.get("status", "DRAFTED")) if a.get("status") in LIFECYCLE else -1
            ib = LIFECYCLE.index(b.get("status", "DRAFTED")) if b.get("status") in LIFECYCLE else -1
            survivor, loser = (a, b) if ia >= ib else (b, a)
            surv_mid, lose_mid = (a_mid, b_mid) if survivor is a else (b_mid, a_mid)
            loser["status"] = "SUPERSEDED"
            loser.setdefault("history", []).append(
                [now, "SUPERSEDED",
                 f"identity dedupe: same physical mailing as {surv_mid} ({why}; card t_40130a34)"])
            # union recipients onto the survivor so no address is lost
            s_rec = survivor.get("recipients")
            if not isinstance(s_rec, list):
                s_rec = [s_rec] if s_rec else []
                survivor["recipients"] = s_rec
            l_rec = loser.get("recipients") or loser.get("recipient") or []
            if isinstance(l_rec, str):
                l_rec = [l_rec]
            for r in l_rec:
                if r and r not in s_rec:
                    s_rec.append(r)
            survivors[lose_mid] = surv_mid
            report.append((lose_mid, surv_mid, why))
    return report

def reconcile():
    """Import legacy ledger statuses + flag stale artifacts, then:
      - ingest .pushed markers (class-level fix, card t_1a52fd92)
      - confirm sweep: SENT + scraped confirmation email -> CONFIRMED"""
    reg = _load()
    flags = []
    led_p = ROOT / "output" / "audit" / "mailing_ledger.json"
    legacy = json.loads(led_p.read_text()).get("items", {}) if led_p.exists() else {}
    for mid, it in reg["items"].items():
        # stale-draft check: artifact newer than the LOADED/SENT timestamp?
        doc = ROOT / it["document"] if not Path(it["document"]).is_absolute() else Path(it["document"])
        if doc.exists() and it["status"] in ("LOADED", "AWAITING_APPROVAL"):
            loaded_ts = next((h[0] for h in reversed(it["history"]) if h[1] in ("LOADED", "AWAITING_APPROVAL")), None)
            if loaded_ts and datetime.datetime.fromtimestamp(doc.stat().st_mtime).isoformat() > loaded_ts:
                flags.append((mid, "REDRAFT_NEEDED: artifact newer than loaded copy — reload before approval"))
        # legacy Gmail linkage by fuzzy name
        stem = Path(it["document"]).stem.replace("_FILING", "").replace("_FINISHED", "")[:28]
        for k, v in legacy.items():
            if stem and stem.lower()[:20] in k.lower().replace(" ", "_"):
                ls = v.get("last_status")
                if ls == "completed" and it["status"] != "CONFIRMED":
                    it["status"] = "CONFIRMED"
                    it["history"].append([_now(), "CONFIRMED", f"gmail: {k[:50]}"])
                elif ls == "pending" and it["status"] in ("LOADED",):
                    it["status"] = "AWAITING_APPROVAL"
                    it["history"].append([_now(), "AWAITING_APPROVAL", f"gmail: {k[:50]}"])
    # push-marker ingest + SENT->CONFIRMED sweep (card t_1a52fd92)
    marker_rep = sync_markers(reg)
    nconf = confirm_sweep(reg)
    # identity dedupe: collapse duplicate-identity items (card t_40130a34)
    dedupe_rep = _dedupe_identity(reg)
    _save(reg)
    for mid, f in flags:
        print(f"  FLAG {mid}: {f}")
    for loser, surv, why in dedupe_rep:
        print(f"  SUPERSEDED {loser} -> {surv} ({why})")
    print(f"reconciled {len(reg['items'])} item(s); {len(flags)} flag(s); "
          f"{len(marker_rep)} push marker(s) processed; {nconf} confirmed; "
          f"{len(dedupe_rep)} superseded")
    return flags

def board(no_sync=False):
    reg = _load()
    if not no_sync:
        # class-level fix (card t_1a52fd92): the board can never be blind to the
        # cart — ingest + SENT->CONFIRMED sweep before rendering. Idempotent: a
        # no-op when the registry is already in sync with the markers.
        now = _now()
        rep = sync_markers(reg, now=now)
        nconf = confirm_sweep(reg, now=now)
        # identity dedupe (card t_40130a34): the board view must never show the
        # same physical mailing as both paid and unpaid — collapse before render.
        deduped = _dedupe_identity(reg, now=now)
        if rep or nconf or deduped:
            _save(reg)
    from collections import defaultdict
    by_batch = defaultdict(list)
    for mid, it in reg["items"].items():
        by_batch[it.get("batch") or "?"].append((mid, it))
    print(f"=== MAILING BOARD ({_now()}) — {len(reg['items'])} tracked ===")
    for b in sorted(by_batch):
        print(f"\n[{b}]")

        def _sort_key(mid, it):
            st = it.get("status", "DRAFTED")
            if st == "SUPERSEDED":
                return 99  # superseded items sink to the bottom of their batch
            return LIFECYCLE.index(st) if st in LIFECYCLE else 50
        for mid, it in sorted(by_batch[b], key=lambda x: _sort_key(x[0], x[1])):
            gate = it.get("gate_verdict", "-")
            job = it.get("letterstream_job_id") or "-"
            doc = Path(str(it.get("document") or "?")).name[:44]
            rcpt = it.get("recipients") or it.get("recipient") or []
            if isinstance(rcpt, str):
                rcpt = [rcpt]
            rstr = "; ".join(str(r) for r in rcpt)[:40] or "(no recipients recorded)"
            print(f"  {it.get('status','?'):18} gate={gate:5} job={job:9} {doc:46} -> {rstr}")


# ── RESPONSE-CLOCK LEDGER (2026-09-02, hashmap layer — ONE FLOW: clocks ride
# the SAME registry as the mailing lifecycle, keyed like mail items. rule_id →
# pre-computed due date; blown clocks surface themselves on the board. No organ
# re-parses CASE_FACTS prose for deadline dates ever again.)
CLOCK_RULES = {
    # rule_id: (days, authority description, what blowing means)
    "ojr_efile_review":  (2,   "FJD confirmation page: OJR approval/rejection email within 2 business days",
                          "call (215) 686-6652 / OJRCivil@courts.phila.gov — their own stated window passed"),
    "irs_ltr_response":  (90,  "IRS letter's own 'complete response within 90 days' commitment",
                          "their own written commitment broken — exhibit"),
    "irc_6532_refund":   (183, "26 U.S.C. 6532(a)(1): 6 months from refund-claim filing [corpus: usc_t26_s6532]",
                          "suit for recovery lies immediately (7422)"),
    "foia_552":          (20,  "5 U.S.C. 552(a)(6)(A)(i): 20 business days FOIA determination",
                          "constructive denial — administrative appeal/suit lies"),
    "respa_qwr_ack":     (5,   "12 U.S.C. 2605(e)(1): servicer QWR acknowledgment",
                          "2605(f) damages accrue"),
    "respa_qwr_answer":  (30,  "12 U.S.C. 2605(e)(2): servicer QWR substantive answer",
                          "2605(f) damages accrue"),
    "fdcpa_validation":  (30,  "15 U.S.C. 1692g: validation window after written dispute",
                          "collection must cease until validation provided"),
    "dishonor_notice":   (30,  "response window stated in the dishonor/notice instrument itself",
                          "silence = record admission per the notice's own terms"),
    "puc_dispute":       (30,  "52 Pa. Code ch. 56: utility must investigate + report result",
                          "termination stayed; ch. 56 violation on the record"),
}


def clock_start(case_id, rule_id, trigger_doc, start_date=None, note=""):
    """Register a response clock (inbound trigger like LTR 2645C, or outbound
    instrument like a dishonor notice). One line at send/receive time."""
    if rule_id not in CLOCK_RULES:
        raise SystemExit(f"unknown clock rule: {rule_id} (add to CLOCK_RULES)")
    days, desc, blown_means = CLOCK_RULES[rule_id]
    start = start_date or _now()[:10]
    d0 = datetime.date.fromisoformat(start)
    due = d0 + datetime.timedelta(days=days)
    reg = _load()
    mid = mail_id(case_id, trigger_doc, rule_id)
    it = reg["items"].get(mid) or {"history": [], "status": "DRAFTED",
                                    "document": trigger_doc, "recipients": [rule_id],
                                    "case_id": case_id}
    it["clock"] = {"rule": rule_id, "authority": desc, "start": start,
                   "due": due.isoformat(), "blown_means": blown_means,
                   "status": "RUNNING", "note": note}
    it["history"].append([_now(), "CLOCK_START", f"{rule_id} due {due} {note}"[:120]])
    reg["items"][mid] = it
    _save(reg)
    print(f"[clock] {mid} {rule_id} started {start} -> due {due}")
    return mid


def clock_answered(mid, note=""):
    reg = _load()
    it = reg["items"].get(mid)
    if not it or "clock" not in it:
        raise SystemExit(f"no clock on {mid}")
    it["clock"]["status"] = "ANSWERED"
    it["history"].append([_now(), "CLOCK_ANSWERED", note])
    _save(reg)
    print(f"[clock] {mid} answered")


def clocks():
    """Clock board: every clock, days remaining; blown ones LOUD with the lever."""
    reg = _load()
    today = datetime.date.today()
    rows = []
    for mid, it in reg["items"].items():
        ck = it.get("clock")
        if not ck:
            continue
        due = datetime.date.fromisoformat(ck["due"])
        left = (due - today).days
        state = ck["status"]
        if state == "RUNNING" and left < 0:
            state = ck["status"] = "BLOWN"
        rows.append((left if state == "RUNNING" else 9999, mid, state, ck, it))
    _save(reg)
    print(f"=== RESPONSE CLOCKS ({today}) ===")
    for left, mid, state, ck, it in sorted(rows):
        mark = "🔴 BLOWN" if state == "BLOWN" else (f"✅ ansd") if state == "ANSWERED" else f"{left:4d}d  "
        line = f"{mark} {ck['rule']:18} {it.get('case_id','?'):24} due {ck['due']}"
        if state == "BLOWN":
            line += f"  => {ck['blown_means']}"
        print(line)
    return rows

def main():
    args = sys.argv[1:]
    if not args or args[0] == "board":
        board(no_sync="--no-sync" in args)
        return
    if args[0] == "clocks":
        clocks(); return
    if args[0] == "clock-start" and len(args) >= 4:
        # clock-start <case_id> <rule_id> <trigger_doc> [--start YYYY-MM-DD] [--note ...]
        start = None; note = ""
        rest = args[4:]
        while rest:
            if rest[0] == "--start": start = rest[1]; rest = rest[2:]
            elif rest[0] == "--note": note = rest[1]; rest = rest[2:]
            else: rest = rest[1:]
        clock_start(args[1], args[2], args[3], start_date=start, note=note); return
    if args[0] == "clock-answered" and len(args) >= 2:
        clock_answered(args[1], note=" ".join(args[2:])); return
    if args[0] == "reconcile":
        reconcile(); return
    if args[0] == "confirm" and len(args) >= 2:
        # confirm <mail_id> [--note ...]
        note = ""
        if "--note" in args:
            note = args[args.index("--note") + 1]
        confirm(args[1], note=note); return
    if args[0] == "link":
        job = None
        if "--job" in args:
            job = args[args.index("--job") + 1]
        link(job=job); return
    if args[0] == "advance" and len(args) >= 3:
        note = ""; job = None
        rest = args[3:]
        while rest:
            if rest[0] == "--note": note = rest[1]; rest = rest[2:]
            elif rest[0] == "--job-id": job = rest[1]; rest = rest[2:]
            else: rest = rest[1:]
        ok = advance(args[1], args[2], note=note, job_id=job)
        print("ok" if ok else "failed"); return
    if args[0] == "add":
        import argparse
        ap = argparse.ArgumentParser(prog="mailing_tracker add")
        ap.add_argument("add"); ap.add_argument("--case", required=True); ap.add_argument("--doc", required=True)
        ap.add_argument("--recipients", required=True); ap.add_argument("--batch", default="")
        ap.add_argument("--court", default=""); ap.add_argument("--class", dest="mclass", default="first-class")
        ap.add_argument("--status", default="DRAFTED"); ap.add_argument("--job-id", default=None)
        a = ap.parse_args(args)
        mid = add(a.case, a.doc, a.recipients.split(";"), batch=a.batch, court=a.court,
                  mail_class=a.mclass, status=a.status, job_id=a.job_id)
        print(mid); return
    print(__doc__)

if __name__ == "__main__":
    main()
