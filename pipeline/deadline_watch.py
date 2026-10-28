#!/usr/bin/env python3
"""deadline_watch.py — the wire between incoming court paper and the clock.

Root-cause fix for the Rule 18 untimely petition (drafted 23 days after the
42-day window ran; deadline was never computed on order arrival).

Deterministic. No LLM. Run by cron after each mail-watch pass and daily.

What it does:
 1. Scans every case's CASE_TIMELINE.json for dated events matching known
    deadline-bearing patterns (orders, dismissals, clerk letters, service).
 2. Computes response windows from a rules table (FRAP/JC&D/PACER/etc.).
 3. Maintains output/audit/deadlines.json (append-only history, active list).
 4. Creates a kanban card for any ACTIVE deadline <= 45 days out that has no card.
 5. Prints an urgency digest (cron output -> log; CRITICAL <= 7 days).

Runway rule (locked): a deadline entering the tracker also records
 draft_queue_by = due - 10d  (m0 draft+gates+mail needs ~10 days).
If today > draft_queue_by and status not DONE -> escalate CRITICAL.

CLOSE RULE (S-deadline-receipt-false-close, 2026-10-24; CLASS RULE "no DONE without
record evidence"): every transition to DONE goes through ONE function, close_deadline(),
which refuses unless the row carries non-empty evidence. A RECEIPT-keyed row (the
satisfaction is the court/addressee RECEIVING the paper -- FRAP 25(a)(2)(A)(i) receipt
rule, EXPECTED inbound replies, "received by" clocks) additionally needs evidence that
POINTS AT A RECORD: a path that exists on disk, or a docket / tracking identifier. A
LetterStream job id or "mailed" / "tracker SENT" is dispatch, not receipt, and is refused.
Date arrival alone sets MISSED, never DONE. load() asserts the invariant at startup and
reports every DONE row that lacks evidence; with downgrade=True it marks them UNVERIFIED.

CLI:
  python3 -m pipeline.deadline_watch                       # scan + digest (writes deadlines.json)
  python3 -m pipeline.deadline_watch check [--file F] [--downgrade] [--write]
  python3 -m pipeline.deadline_watch close "<key or what substring>" --evidence "<path|docket id>" [--note N] [--dry-run]
"""
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import json, os, re, subprocess, sys, hashlib
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = _eroot()
OUT = ROOT / "output/audit/deadlines.json"
CASES = ROOT / "obsidian_sync/cases"
CLAIMS = ROOT / "obsidian_sync/claim_candidates"
# Every record dir that can carry a clock. Court dockets live in cases/;
# non-litigation clocks (class-action opt-outs, agency gates, FOIA appeals)
# live in claim_candidates/. Both are scanned identically.
RECORD_ROOTS = [CASES, CLAIMS]

# pattern -> (window_days, rule, what)
RULES = [
    (r"dismiss.*judicial conduct|judicial conduct.*dismiss|J\.?C\.? No.*dismiss", 42,
     "JC&D Rule 18(b)", "Petition for review to Judicial Council"),
    (r"case opening letter|pay.*(docket|filing) fee|forma pauperis.*(14|fourteen) days?|IFP motion.*14", 14,
     "3d Cir. L.A.R. 3.3(a)/107.1(a)", "Pay fee or file IFP motion"),
    (r"final judgment|judgment entered|case closed", 30,
     "FRAP 4(a)(1)(A)", "Notice of appeal"),
    (r"order.*(deny|deni|grant).*reconsider|reconsideration.*(denied|granted)", 30,
     "FRAP 4(a)(1)(A)", "Notice of appeal (post-recon)"),
    (r"motion to dismiss.*filed|12\(b\)\(6\).*filed", 14,
     "E.D. Pa. L.R. 7.1(c)", "Opposition brief"),
    (r"show cause|rule to show cause", 14,
     "order-stated (verify)", "Show-cause response"),
    (r"brief.*due|briefing schedule", 0,
     "order-stated", "Brief per schedule — read the order for the date"),
    (r"default notice|final notice.*(payment|balance)", 21,
     "creditor-stated", "Payment or dispute"),
    (r"request for exclusion|opt[- ]?out deadline|exclusion deadline|claim form.*(due|deadline)|class action settlement.*notice", 0,
     "Fed. R. Civ. P. 23(c)(2)(B)", "Class notice — opt-out/claim/objection clock: read the notice and DECLARE it in DEADLINES.json"),
    (r"untimely|new complaint", 0,
     "JC&D Rule 6", "New complaint on post-dismissal conduct (no fixed clock; file promptly)"),
]

DATE_PATS = [r"(\d{4})-(\d{2})-(\d{2})", r"(\d{1,2})/(\d{1,2})/(\d{4})"]

def parse_date(s):
    m = re.search(DATE_PATS[0], s)
    if m: return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(DATE_PATS[1], s)
    if m: return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    return None

# ---------------------------------------------------------------------------
# CLOSE RULE (S-deadline-receipt-false-close, 2026-10-24). Defect of record: the two
# 2026-10-19 CA3 26-0003 Doc.7 rows were written status=DONE with done_note empty and a
# done_evidence that only recited "mailed 2026-10-16 (LS ...), tracker SENT x12" -- a
# dispatch claim, while the close condition of record
# (drafts/_bundle13_26-0003_doc7/DEADLINE_AUDIT_2763_ifp_receipt_20261018.md) was docket
# email OR delivered tracking OR hand delivery, none of which existed. Nothing in the
# tracker ever checked what "DONE" rested on. Everything below is deterministic; no LLM.
# ---------------------------------------------------------------------------

STATUS_DONE = "DONE"
STATUS_MISSED = "MISSED"          # date arrived, no evidence of satisfaction on the row
STATUS_UNVERIFIED = "UNVERIFIED"  # was DONE, but the row carries no record evidence

# How a row is RECEIPT-keyed (documented order of precedence):
#  (a) explicit flag in the row schema: `satisfaction: "receipt"` or `receipt_keyed: true`
#      (`satisfaction: "dispatch"` / `receipt_keyed: false` opt a row OUT explicitly);
#  (b) `direction: "inbound"` / key segment `|EXPECTED|` -- the clock is satisfied only
#      when the reply is RECEIVED;
#  (c) inferred from rule+what text: receipt words, or a court-filing rule (FRAP 25(a)(2)(A)(i)
#      makes filing = receipt by the clerk for everything but briefs/appendices; local
#      appellate rules, Pa.R.A.P., E.D. Pa. L.R., clerk letters, docketing fees).
_RECEIPT_WORDS = re.compile(
    r"receipt|receiv|deliver|arriv|must be (filed|received)|hard deadline|docket(ed|ing)? fee|"
    r"FRAP\s*2[15]|L\.?A\.?R\.?|3d Cir|Pa\.?R\.?A\.?P|E\.D\. Pa|Prothonotary|clerk|"
    r"file (either|an?|the|a motion)|pay fee or file", re.I)

def is_receipt_keyed(row):
    sat = str(row.get("satisfaction", "")).lower()
    if sat in ("receipt", "received"): return True
    if sat in ("dispatch", "mailing", "payment"): return False
    if "receipt_keyed" in row: return bool(row["receipt_keyed"])
    if row.get("direction") == "inbound" or "|EXPECTED|" in str(row.get("key", "")): return True
    return bool(_RECEIPT_WORDS.search(f"{row.get('rule','')} {row.get('what','')}"))

# Evidence that POINTS AT A RECORD. Dispatch artifacts (LetterStream job ids, "mailed",
# "tracker SENT") are deliberately NOT in this list: they prove we sent, not that it arrived.
# An identifier counts only when PAIRED with a receipt/docketing verb in the same evidence
# ("Doc 7" alone is a citation to the order that STARTED the clock, not proof it was satisfied --
# that is exactly how the 2026-10-19 Doc.7 rows read), and never when it is the row's own trigger.
_DOCKET_ID = re.compile(
    r"\b(ECF|Dkt\.?|Doc\.?|Document|Docket)\s*(No\.?)?\s*#?\s*\d{1,5}\b|"
    r"\bOrder[_\s#]*(No\.?\s*)?\d{1,4}\b|"                # ORDER_17 / Order No. 17 (FJD docket entries)
    r"\bE-?File\s*(No\.?)?\s*\d{6,}|\bControl\s*No\.?\s*\d{6,}|"
    r"\b(docket|docketed|received|delivered|tracking|usps)\s*[:=#]\s*\S+|"
    r"\b(9[234]\d{18,20})\b|"                       # USPS IMpb tracking number
    r"\b(NDA|Notice of Docket Activity)\b", re.I)
_RECEIPT_VERB = re.compile(r"\b(docketed|entered|received|receipt|delivered|delivery|accepted|filed on|stamped|"
                           r"NDA|Notice of Docket Activity|hand[- ]delivered)\b", re.I)
_DOC_TOKEN = re.compile(r"\b(?:ECF|Dkt\.?|Doc\.?|Document)\s*(?:No\.?)?\s*#?\s*(\d{1,5})\b", re.I)
_PATH_TOKEN = re.compile(r"(?:/mnt/c/[^\s,;)\]'\"#]+|(?:obsidian_sync|efiling|output|intake|drafts|_sandbox[^/\s]*)/[^\s,;)\]'\"#]+)")

def evidence_paths(evidence, root=None):
    """Return (existing_paths, missing_paths) for every path-like token in the evidence string.
    Relative tokens resolve against ROOT and, for `#received=` style suffixes, the suffix is stripped."""
    root = Path(root) if root else ROOT
    ok, missing = [], []
    for tok in _PATH_TOKEN.findall(str(evidence or "")):
        tok = tok.split("#", 1)[0].rstrip(".:")
        pth = Path(tok) if tok.startswith("/") else root / tok
        (ok if pth.exists() else missing).append(str(pth))
    return ok, missing

def evidence_verifies(evidence, root=None, row=None):
    """(bool, reason). True when the evidence names an on-disk record, OR a docket/tracking identifier paired with
    a receipt/docketing verb. A path token that is named but does NOT exist is a hard refusal (a dangling pointer
    is not a record). A doc number that also appears in the row's own rule/what/key is the TRIGGER, not evidence."""
    ev = str(evidence or "").strip()
    if not ev:
        return False, "evidence empty"
    ok, missing = evidence_paths(ev, root)
    if missing and not ok:
        return False, f"evidence path does not exist: {missing[0]}"
    if ok:
        return True, f"record path exists: {ok[0]}"
    ids = [m.group(0) for m in _DOCKET_ID.finditer(ev)]
    if ids:
        if row is not None:
            own = set(_DOC_TOKEN.findall(f"{row.get('rule','')} {row.get('what','')} {row.get('key','')}"))
            ids = [i for i in ids if not (_DOC_TOKEN.search(i) and _DOC_TOKEN.search(i).group(1) in own)]
            if not ids:
                return False, "only the row's own trigger document is cited -- that started the clock, it does not satisfy it"
        if _RECEIPT_VERB.search(ev):
            return True, "docket/tracking identifier paired with a receipt/docketing verb"
        return False, "identifier present but no receipt/docketing verb (docketed/entered/received/delivered/accepted) -- dispatch is not receipt"
    return False, "evidence names neither an existing record path nor a docket/tracking identifier (dispatch ids such as LetterStream job numbers do not prove receipt)"

def close_deadline(row, evidence, note="", ts=None, root=None, today=None):
    """THE one closer. Mutates `row` to DONE only when the evidence is sufficient; otherwise leaves the
    status untouched (or MISSED when the due date has passed) and returns (False, reason).
    Returns (True, reason) on close. Callers: main() inbound closer, the `close` CLI, any mail-watch /
    outcome_writer path that wants to mark DONE. No other code may assign the DONE status."""
    ev = str(evidence or "").strip()
    receipt = is_receipt_keyed(row)
    if not ev and not str(note or "").strip():
        reason = "refused: no evidence/note -- a deadline cannot close on date arrival"
        apply_date_arrival(row, today)
        return False, reason
    if receipt:
        good, why = evidence_verifies(ev or str(note or "").strip(), root, row)   # the note may carry the record pointer
        if not good:
            apply_date_arrival(row, today)
            return False, f"refused (receipt-keyed): {why}"
    else:
        why = "non-receipt row: non-empty note/evidence accepted"
    row["status"] = STATUS_DONE
    row["done_evidence"] = ev
    row["done_note"] = str(note or "").strip() or ev
    row["done_ts"] = ts or datetime.now().isoformat()
    row["receipt_keyed"] = receipt
    return True, f"closed: {why}"

def apply_date_arrival(row, today=None):
    """Date arrival alone sets MISSED, never DONE. Only touches OPEN/ACTIVE rows whose due date is a real date."""
    today = today or date.today()
    if row.get("status") in (STATUS_DONE, STATUS_UNVERIFIED): return row.get("status")
    due = parse_date(str(row.get("due", "")))
    if due and due < today and row.get("status") in (None, "", "OPEN", "ACTIVE"):
        row["status"] = STATUS_MISSED
        row["missed_on"] = today.isoformat()
    return row.get("status")

def done_violations(db, root=None):
    """Every DONE row that would violate the close rule. Returns list of (section, idx, key, tier, detail).
    tier EMPTY: done_note and done_evidence both empty (the backlog's literal defect; `received` alone does not count).
    tier UNVERIFIABLE: receipt-keyed and the evidence names no existing path / docket id."""
    out = []
    for sec in ("active", "done"):
        for i, d in enumerate(db.get(sec) or []):
            if not isinstance(d, dict) or d.get("status") != STATUS_DONE: continue
            note, ev = str(d.get("done_note") or "").strip(), str(d.get("done_evidence") or "").strip()
            if not note and not ev:
                out.append((sec, i, d.get("key", "?"), "EMPTY", "done_note and done_evidence empty"))
            elif is_receipt_keyed(d):
                good, why = evidence_verifies(ev or note, root, d)
                if not good:
                    out.append((sec, i, d.get("key", "?"), "UNVERIFIABLE", why))
    return out

def assert_done_invariant(db, downgrade=False, root=None, out=sys.stderr):
    """Startup assertion (acceptance 3): report every violating DONE row; with downgrade=True set them UNVERIFIED
    (history op 'downgrade_unverified'). Never silently accepts. Returns the violation list."""
    v = done_violations(db, root)
    for sec, i, key, tier, detail in v:
        print(f"[deadline_watch] DONE-INVARIANT {tier}: {key} -- {detail}", file=out)
        if downgrade:
            row = db[sec][i]
            row["status"] = STATUS_UNVERIFIED
            row["unverified_reason"] = f"{tier}: {detail}"
            db.setdefault("history", []).append({"ts": datetime.now().isoformat(), "op": "downgrade_unverified",
                                                 "key": key, "tier": tier})
    if v:
        print(f"[deadline_watch] {len(v)} DONE row(s) without record evidence"
              + (" -> downgraded to UNVERIFIED" if downgrade else " (run `check --downgrade` to mark UNVERIFIED)"), file=out)
    return v

def load(path=None, downgrade=None, root=None):
    """Load the tracker and run the DONE invariant. `downgrade` defaults to env DEADLINE_DOWNGRADE_UNVERIFIED=1."""
    path = Path(path) if path else OUT
    if downgrade is None:
        downgrade = os.environ.get("DEADLINE_DOWNGRADE_UNVERIFIED") == "1"
    db = json.loads(path.read_text()) if path.exists() else {"active": [], "done": [], "history": []}
    for k in ("active", "done", "history"):
        db.setdefault(k, [])
    assert_done_invariant(db, downgrade=downgrade, root=root)
    return db

def scan():
    """Pattern-infer clocks from timeline events across ALL record roots."""
    found = []
    for root in RECORD_ROOTS:
        for tl in root.glob("*/CASE_TIMELINE.json"):
            case = tl.parent.name
            try:
                events = json.loads(tl.read_text())
            except Exception:
                continue
            if isinstance(events, dict):
                events = events.get("events") or events.get("entries") or []
            for ev in events:
                blob = json.dumps(ev)
                evd = parse_date(blob)
                if not evd or evd < date.today() - timedelta(days=120):
                    continue
                for pat, days, rule, what in RULES:
                    if re.search(pat, blob, re.I):
                        due = evd + timedelta(days=days) if days else None
                        key = f"{case}|{rule}|{evd.isoformat()}"
                        found.append({"key": key, "case": case, "event_date": evd.isoformat(),
                                      "due": due.isoformat() if due else "READ-ORDER",
                                      "rule": rule, "what": what,
                                      "draft_queue_by": (due - timedelta(days=10)).isoformat() if due else None,
                                      "src": str(tl)})
                        break
    return found


def scan_declared():
    """Explicit clocks a human/agent DECLARED, from <record>/DEADLINES.json.

    Pattern-inference works for court events because the RULE supplies the
    window. It does NOT work where the instrument itself states the date --
    class notices, agency gates, FOIA appeals. Those are declared, never guessed.
    Schema: [{"due":"YYYY-MM-DD","what":str,"rule":str,
              "event_date":"YYYY-MM-DD"(opt),"queue_lead_days":int(opt)}]
    """
    found = []
    for root in RECORD_ROOTS:
        for df in root.glob("*/DEADLINES.json"):
            case = df.parent.name
            try:
                items = json.loads(df.read_text())
            except Exception:
                continue
            if isinstance(items, dict):
                items = items.get("deadlines") or []
            if not isinstance(items, list):
                continue
            for it in items:
                if not isinstance(it, dict):
                    continue
                due = parse_date(str(it.get("due", "")))
                if not due:
                    continue
                rule = it.get("rule") or "declared"
                evd = parse_date(str(it.get("event_date", ""))) or due
                try:
                    lead = int(it.get("queue_lead_days") or 10)
                except Exception:
                    lead = 10
                found.append({"key": f"{case}|{rule}|{due.isoformat()}",
                              "case": case, "event_date": evd.isoformat(),
                              "due": due.isoformat(),
                              "rule": rule,
                              "what": it.get("what") or "(declared deadline)",
                              "draft_queue_by": (due - timedelta(days=lead)).isoformat(),
                              "src": str(df)})
    return found


def what_id(case, what):
    """STABLE identity for an EXPECTED item (2026-10-25, S-expected-rule-rekey / card t_9279b087).
    The row key was built from RULE TEXT + due date, so correcting a row's `rule` in EXPECTED.json
    minted a SECOND active row (the orphan kept the ungrounded due date and kept firing; verizon /
    dentists 2026-10-18/19). The identity is (case, what, event_date) -- `what` names the instrument
    and is stable under rule corrections; the event date (sent) is the anchor. Rule text and due date
    are NEVER in the key."""
    base = re.sub(r"\s+", " ", str(what or "")).strip().lower()
    return hashlib.sha1(f"{case}::{base}".encode("utf-8")).hexdigest()[:10]


def rekey_expected_rows(db):
    """One-time in-place rekey of rule-text-keyed EXPECTED rows onto the stable key (S-expected-rule-rekey).
    Old key = `{case}|EXPECTED|{rule text}|{expect_by}` -- the RULE segment is split on, so parse with
    first-two / last pipe (rule text itself can contain '|'). case is taken from the row's own `src`
    path (authoritative), not re-parsed from the key. Match = source EXPECTED.json item with the same
    expect_by (the old key's LAST segment) whose `what` matches the row's rendered what (which carries
    the `AWAITING ` prefix scan_expected added -- strip it). Only rows whose key is the OLD rule-text
    format (last key segment is a date) are touched; rows already on the what_id format are left
    byte-identical, so the non-EXPECTED rows and any already-migrated rows are never re-minted. No
    match -> row untouched (a row without a satisfaction signal is never touched). Returns rekey count."""
    n = 0
    for sec in ("active", "done"):
        for d in db.get(sec) or []:
            if not isinstance(d, dict) or "|EXPECTED|" not in str(d.get("key", "")):
                continue
            key = str(d.get("key", ""))
            if key.split("|", 2)[1] != "EXPECTED":
                continue
            old_exp = key.rsplit("|", 1)[-1]
            try:
                old_exp_d = datetime.strptime(old_exp, "%Y-%m-%d").date()
            except ValueError:
                continue  # last segment is not a date -> already a what_id key
            src = d.get("src")
            if not src or not Path(src).exists():
                continue
            case = Path(src).parent.name
            try:
                items = json.loads(Path(src).read_text())
            except Exception:
                continue
            if isinstance(items, dict):
                items = items.get("expected") or []
            # scan_expected renders `what` as "AWAITING <src what>" -- strip that prefix for matching.
            what_head = re.sub(r"\s+", " ", str(d.get("what", ""))).strip()
            if what_head.startswith("AWAITING "):
                what_head = what_head[len("AWAITING "):]
            for it in (items if isinstance(items, list) else []):
                if not isinstance(it, dict):
                    continue
                if parse_date(str(it.get("expect_by", ""))) != old_exp_d:
                    continue
                head = re.sub(r"\s+", " ", str(it.get("what") or ""))
                if not what_head or not (what_head == head or what_head.startswith(head) or head.startswith(what_head)):
                    continue
                sent = parse_date(str(it.get("sent", ""))) or old_exp_d
                new_key = f"{case}|EXPECTED|{what_id(case, it.get('what'))}|{sent.isoformat()}"
                if new_key == key:
                    continue
                old = key
                d["key"] = new_key
                db.setdefault("history", []).append({"ts": datetime.now().isoformat(),
                                                     "op": "rekey_expected", "old_key": old, "key": new_key})
                n += 1
                break
    return n


def scan_expected():
    """INBOUND clocks -- what the COURT or OPPONENT owes us after something we filed/served,
    with a follow-up date. The tracker previously watched only OUR deadlines; a Superior Court
    that never answered a docketing statement, or an opponent whose response window ran, was
    invisible (user 2026-09-27: 'never got a reply from the superior court ... if they missed a
    deadline we need a tracker to follow up'). Declared per record in <record>/EXPECTED.json:
      [{"sent":"YYYY-MM-DD","what":str,"from":"court|opponent|agency","rule":str,
        "expect_by":"YYYY-MM-DD",            # when a reply is due/expected
        "follow_up":"YYYY-MM-DD"(opt),       # when WE act if silent (default expect_by+7)
        "action":str(opt),                   # what silence means / what to file (praecipe, status inquiry, motion)
        "received":"YYYY-MM-DD"(opt)}]       # set when it arrives -> DONE
    Their silence past follow_up is a FINDING (waiver/default lever), not just a wait.
    Key = stable (case, what, sent) via what_id -- rule text/due NEVER in the key (2026-10-25)."""
    found = []
    for root in RECORD_ROOTS:
        for ef in root.glob("*/EXPECTED.json"):
            case = ef.parent.name
            try:
                items = json.loads(ef.read_text())
            except Exception:
                continue
            if isinstance(items, dict):
                items = items.get("expected") or []
            for it in (items if isinstance(items, list) else []):
                if not isinstance(it, dict) or it.get("received"):
                    continue
                exp = parse_date(str(it.get("expect_by", "")))
                if not exp:
                    continue
                fu = parse_date(str(it.get("follow_up", ""))) or (exp + timedelta(days=7))
                sent = parse_date(str(it.get("sent", ""))) or exp
                found.append({"key": f"{case}|EXPECTED|{what_id(case, it.get('what'))}|{sent.isoformat()}",
                              "case": case, "event_date": sent.isoformat(),
                              "due": fu.isoformat(),          # OUR follow-up date is the clock the digest ranks on
                              "rule": f"EXPECTED from {it.get('from','?')}: {it.get('rule') or 'declared'}",
                              "what": f"AWAITING {it.get('what') or '(reply)'} -- expected by {exp.isoformat()}; "
                                      f"if silent: {it.get('action') or 'file status inquiry / praecipe'}",
                              "draft_queue_by": fu.isoformat(),
                              "direction": "inbound", "src": str(ef)})
    return found


def received_expected_keys():
    """Keys of EXPECTED rows that now carry `received` -> {key: (received, EXPECTED.json path)} (set by pipeline.expected_intake or by hand) -> their active
    clocks close. Before this, scan_expected only skipped received rows for NEW detection; an already-active
    clock stayed OPEN and followup_drafter could still draft a status inquiry for a reply that had arrived.
    Key = same stable (case, what, sent) formula as scan_expected (2026-10-25) -- previously this used the
    rule text + expect_by, which both orphaned on rule corrections AND never matched scan_expected's key
    (which uses `sent`), so inbound close could silently miss a row."""
    keys = {}
    for root in RECORD_ROOTS:
        for ef in root.glob("*/EXPECTED.json"):
            try:
                items = json.loads(ef.read_text())
            except Exception:
                continue
            if isinstance(items, dict):
                items = items.get("expected") or []
            for it in (items if isinstance(items, list) else []):
                if isinstance(it, dict) and it.get("received") and str(it.get("received")) != "None":
                    exp = parse_date(str(it.get("expect_by", "")))
                    if exp:
                        sent = parse_date(str(it.get("sent", ""))) or exp
                        keys[f"{ef.parent.name}|EXPECTED|{what_id(ef.parent.name, it.get('what'))}|{sent.isoformat()}"] = (str(it["received"])[:60], str(ef))
    return keys


def main(out_path=None, dry_run=False):
    out_path = Path(out_path) if out_path else OUT
    db = load(out_path)
    # One-time in-place rekey of rule-text-keyed EXPECTED rows onto the stable (case, what, sent) key.
    # MUST run before the inbound-close match and the new-row detection: otherwise a live row still
    # keyed on rule text won't match its source item (now on the stable key) and a corrected rule
    # mints a second active row instead of updating in place (S-expected-rule-rekey / t_9279b087).
    nk = rekey_expected_rows(db)
    if nk:
        db.setdefault("history", []).append({"ts": datetime.now().isoformat(), "op": "rekey_expected_pass", "count": nk})
        print(f"[deadline_watch] rekeyed {nk} EXPECTED row(s) onto the stable (case, what, sent) key")
    rec = received_expected_keys()
    for d in db["active"]:
        if d.get("status") != STATUS_DONE and d["key"] in rec:
            # inbound close routes through THE closer (2026-10-24): evidence = the EXPECTED.json record that
            # carries `received`, so the pointer is a path that exists, not a bare date.
            ok, why = close_deadline(d, f"{rec[d['key']][1]}#received={rec[d['key']][0]}",
                                     note=f"received {rec[d['key']][0]} per EXPECTED.json")
            d["received"] = rec[d["key"]][0]
            db["history"].append({"ts": datetime.now().isoformat(), "op": "received" if ok else "received_refused",
                                  "key": d["key"], "received": rec[d["key"]][0], "why": why})
    known = {d["key"] for d in db["active"]} | {d["key"] for d in db["done"]}
    new = [f for f in (scan() + scan_declared() + scan_expected()) if f["key"] not in known]
    for n in new:
        n["status"] = "OPEN"; n["carded"] = False
        db["active"].append(n)
        db["history"].append({"ts": datetime.now().isoformat(), "op": "detected", "key": n["key"]})
    today = date.today()
    lines = []
    for d in db["active"]:
        if d.get("status") == STATUS_DONE: continue
        apply_date_arrival(d, today)       # date arrival -> MISSED, never DONE (2026-10-24)
        if d["due"] == "READ-ORDER":
            lines.append(f"  READ-ORDER  {d['case']}: {d['what']} ({d['rule']}) ev {d['event_date']}")
            continue
        due = date.fromisoformat(d["due"]); left = (due - today).days
        qby = date.fromisoformat(d["draft_queue_by"]) if d.get("draft_queue_by") else due
        lvl = "CRITICAL" if left <= 7 or today > qby else ("URGENT" if left <= 14 else "open")
        tag = f" <{d['status']}>" if d.get("status") not in (None, "OPEN") else ""
        lines.append(f"  {lvl:8} {d['case']}: {d['what']} due {d['due']} ({left}d left; queue by {d['draft_queue_by']}){tag} [{d['rule']}]")
        if not dry_run and not d.get("carded") and left <= 45:
            try:
                r = subprocess.run(["hermes", "kanban", "create",
                    f"[deadline] {d['case']}: {d['what']} DUE {d['due']} ({d['rule']}) — draft must enter m0 queue by {d['draft_queue_by']}; mail certified if deadline-proof class. Source event {d['event_date']}."],
                    capture_output=True, text=True, timeout=60,
                    env={k: v for k, v in os.environ.items() if k != "HERMES_DELEGATED_CHILD_CONTEXT"})
                if r.returncode == 0: d["carded"] = True
            except Exception:
                pass
    if not dry_run:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(db, indent=1))
    print(f"deadline_watch {datetime.now().isoformat()}: {len(new)} new, {len([d for d in db['active'] if d.get('status')!=STATUS_DONE])} active")
    print("\n".join(lines) if lines else "  no active deadlines")


def cli_check(argv):
    """`check [--file F] [--downgrade] [--write]`: run the loader invariant on a tracker file. Read-only unless --write."""
    f = OUT; downgrade = "--downgrade" in argv; write = "--write" in argv
    if "--file" in argv: f = Path(argv[argv.index("--file") + 1])
    db = load(f, downgrade=downgrade)
    v = done_violations(db) if not downgrade else []
    if downgrade and write:
        f.write_text(json.dumps(db, indent=1))
    print(f"check {f}: {len(v) if not downgrade else 'downgraded'} violating DONE row(s)")
    return 1 if v else 0


def cli_close(argv):
    """`close "<key or what substring>" --evidence E [--note N] [--file F] [--dry-run]`: THE write path for marking DONE.
    Exactly one active row must match; refuses on zero/many matches and on insufficient evidence."""
    if not argv or argv[0].startswith("--"):
        print("usage: close <key-or-what-substring> --evidence <path|docket id> [--note N] [--file F] [--dry-run]"); return 2
    needle = argv[0]; f = OUT
    def opt(name, default=""):
        return argv[argv.index(name) + 1] if name in argv else default
    if "--file" in argv: f = Path(opt("--file"))
    evidence, note, dry = opt("--evidence"), opt("--note"), "--dry-run" in argv
    db = load(f)
    hits = [d for d in db["active"] if d.get("status") != STATUS_DONE and (needle in d.get("key", "") or needle in d.get("what", ""))]
    if len(hits) != 1:
        print(f"close: {len(hits)} active rows match {needle!r}; need exactly 1"); return 2
    ok, why = close_deadline(hits[0], evidence, note=note)
    print(f"close {hits[0]['key']}: {why}")
    if ok and not dry:
        db["history"].append({"ts": datetime.now().isoformat(), "op": "close", "key": hits[0]["key"], "evidence": evidence[:200]})
        f.write_text(json.dumps(db, indent=1))
    return 0 if ok else 1


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "check":
        sys.exit(cli_check(args[1:]))
    elif args and args[0] == "close":
        sys.exit(cli_close(args[1:]))
    else:
        main(dry_run="--dry-run" in args)
