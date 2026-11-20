"""
pipeline.admin_clocks — F2.4 (2026-11-10): the admin room's CLOCK cards become tracked deadlines ONLY when the demand/request
they run from has a mail receipt.

A CLOCK card (deadline-tracker lane) says: "CLOCK n: <addressee> — … (§ <statute>) — START UNDATED: <event> — RULE "…" — DUE … — MAIL …".
The statute on the card picks the deadline_tracker rule (the table carries the period and the corpus-pinned cite — the bot's quoted
period is never used for arithmetic); the START comes from the mail ledger (output/audit/mailing_tracker.json: an item for the case
whose recipients name the addressee and whose status is SENT/CONFIRMED/FILED; start = its delivered date when the ledger carries one,
else the SENT date with basis "mailed"; only items that ARE the admin bundle's demand/request count — never a name-token guess)
or from an explicit --receipt JSON. No receipt -> the clock is listed PENDING MAIL with the
addressee; no date is ever invented. Idempotent: the same (statute, addressee, start) is skipped on re-run.

Writes: cases/<slug>/DEADLINES.json (declared rows, same shape as e2e_trigger.deadlines_step) -> carried into
<root>/output/audit/deadlines.json by deadline_watch's own scanners; deadline_tracker.track_deadline lifecycle record;
<bundle>/m2/ADMIN_CLOCKS.json {tracked, pending_mail} for e2e_status.  Data root = BEEZLE_ROOT (sandbox never writes live).
"""
from __future__ import annotations
import json, os, re, sys
from datetime import datetime, date
from pathlib import Path

ROOT = Path(os.environ.get("BEEZLE_ROOT") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts/rooms"))

# statute named on the CLOCK card -> (deadline_tracker rule_key, jurisdiction). Identifier resolution, not comprehension:
# the room decided which statute governs; this only maps the cite it wrote to the rule row that carries the period.
STATUTE_RULES = [
    (re.compile(r"1681s[-–]2\(b\)"), ("fcra_furnisher_investigation", "federal")),
    (re.compile(r"1681i\(a\)\(6\)"), ("fcra_results_notice", "federal")),
    (re.compile(r"1681i"), ("fcra_reinvestigation", "federal")),
    (re.compile(r"2605\(e\)\(1\)"), ("respa_qwr_ack", "federal")),
    (re.compile(r"2605\(e\)"), ("respa_qwr_response", "federal")),
    (re.compile(r"552\(a\)\(6\)|\bFOIA\b|1070\.16|\b4\.11\b"), ("foia", "federal")),
    (re.compile(r"67\.1101"), ("rtkl_oor_appeal", "pa_state")),
    (re.compile(r"67\.13\d\d"), ("rtkl_judicial_review", "pa_state")),
    (re.compile(r"67\.901|RTKL|Right[- ]to[- ]Know", re.I), ("rtkl_response", "pa_state")),
]
RECEIPT_STATUSES = ("SENT", "CONFIRMED", "FILED")
STOP = {"inc", "llc", "the", "of", "and", "n.a", "na", "corp", "company", "co", "bank", "usa", "information", "solutions", "services", "board", "office"}


def _tok(s: str) -> set:
    return {t for t in re.split(r"[^a-z0-9]+", (s or "").lower()) if len(t) > 2 and t not in STOP}


def _load(p: Path, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def _backup(p: Path):
    if p.exists():
        b = p.with_name(p.name + ".bak." + datetime.now().strftime("%Y%m%d_%H%M%S")); b.write_bytes(p.read_bytes()); return b


def _deadline_tracker():
    """ROOT/deadline_tracker.py loaded by path (not by name: cards_to_cells may already hold the live module under that name);
    imported from a scratch cwd because it mkdirs a Windows-literal path at import."""
    import importlib.util
    scratch = ROOT / "output/_scratch"; scratch.mkdir(parents=True, exist_ok=True); cwd = os.getcwd(); os.chdir(scratch)
    try:
        spec = importlib.util.spec_from_file_location("beezle_deadline_tracker", ROOT / "deadline_tracker.py")
        mod = importlib.util.module_from_spec(spec); sys.modules[spec.name] = mod; spec.loader.exec_module(mod)   # dataclasses need it in sys.modules
    finally:
        os.chdir(cwd)
    return mod


def _is_admin_mailing(it: dict) -> bool:
    """A ledger item starts a statutory clock only if it IS the admin room's demand/request: its document lives under the admin
    bundle (drafts/_bundle*_admin/) or the item carries admin_clock metadata. Name-token guessing is refused on purpose — a
    'Notice of Appeal' mailed to Experian shares the word 'notice' with a 1681i dispute notice and is not one."""
    doc = str(it.get("document") or "")
    return bool(re.search(r"_bundle[^/]*_admin/", doc)) or bool(it.get("admin_clock"))


def rule_for(text: str):
    for rx, rule in STATUTE_RULES:
        if rx.search(text or ""):
            return rule
    return None


def clock_cards(case_dir: Path, tag: str | None, cards_root: Path) -> list[dict]:
    """CLOCK cards for the case: cells_room_admin.jsonl (kind=clock) first; then every deadline-tracker card in the ledger for
    the tag (skeptic-OPEN cards included, flagged verdict=OPEN — a clock the room could not seal is still a clock we must not
    forget; it is tracked only if the statute resolves to a rule AND a receipt exists)."""
    out, seen = [], set()
    for cells in case_dir.glob("drafts/_bundle*_admin/m2/cells_room_admin.jsonl"):
        for line in cells.read_text().splitlines():
            try:
                c = json.loads(line)
            except Exception:
                continue
            if c.get("kind") == "clock":
                out.append({"addressee": c.get("title"), "statute_text": " ".join(str(c.get(k) or "") for k in ("title", "proposition", "rule")),
                            "start": c.get("start"), "start_undated": c.get("start_undated"), "verdict": c.get("verdict"), "status": c.get("status"),
                            "mail": c.get("mail"), "relay": c.get("relay"), "n": c.get("claim"), "from": "cells"})
                seen.add(c.get("relay"))
    if tag:
        try:
            import cards_to_cells as c2c
        except Exception:
            c2c = None
        for cf in sorted((cards_root / "ledger" / tag).glob("r*/deadline-tracker.cards.md")):
            room = cf.parent.name[1:]
            for raw in cf.read_text().splitlines():
                if not raw.startswith("CARD: deadline-tracker"):
                    continue
                parts = [x.strip() for x in raw.split("|")]
                body = parts[2] if len(parts) > 2 else ""
                m = re.match(r"CLOCK\s+(\d+)", body)
                n = int(m.group(1)) if m else None
                relay = f"{tag} r{room} deadline-tracker#{n}"
                if relay in seen:
                    continue
                p = c2c.parse_admin("clock", body, "") if c2c else {"subject": body[:120]}
                if not p.get("start_undated") and not p.get("start"):
                    su = re.search(r"START UNDATED[:\s]+([^—]+)", body); p["start_undated"] = su.group(1).strip() if su else None
                out.append({"addressee": (p.get("subject") or body[:120]).split(" — ")[0].strip(), "statute_text": body, "start": p.get("start"),
                            "start_undated": p.get("start_undated"), "verdict": parts[3] if len(parts) > 3 else "?", "status": "LEDGER",
                            "mail": p.get("mail"), "relay": relay, "n": n, "from": "ledger"})
    return out


def ledger_receipt(case_ids: set, addressee: str, ledger: Path) -> dict | None:
    """Mail-ledger receipt for an addressee: newest SENT/CONFIRMED/FILED item of the case whose recipients share a name token."""
    items = (_load(ledger, {}) or {}).get("items", {})
    atok = _tok(addressee)
    best = None
    for mid, it in items.items():
        if it.get("case_id") not in case_ids or it.get("status") not in RECEIPT_STATUSES:
            continue
        rtok = _tok(" ".join(it.get("recipients") or []) if isinstance(it.get("recipients"), list) else str(it.get("recipients")))
        if not (atok & rtok):
            continue
        if not _is_admin_mailing(it):
            continue                      # a mailing to the addressee that is not the room's demand/request starts no statutory clock
        delivered = it.get("delivered") or it.get("usps_delivered")
        sent = next((h[0][:10] for h in it.get("history", []) if h[1] == "SENT"), None)
        d = (delivered or "")[:10] or sent
        if not d:
            continue
        rec = {"mail_id": mid, "date": d, "basis": "delivered" if delivered else "mailed", "document": it.get("document"),
               "job": it.get("letterstream_job_id"), "source": str(ledger.relative_to(ROOT)) if str(ledger).startswith(str(ROOT)) else str(ledger)}
        if best is None or rec["date"] > best["date"]:
            best = rec
    return best


def run(case_dir: Path, canonical: str, tag: str | None = None, receipt: dict | None = None, log=print,
        cards_root: Path | None = None, ledger: Path | None = None) -> dict:
    cards_root = cards_root or ROOT / "output/research/rooms_ab"
    ledger = ledger or ROOT / "output/audit/mailing_tracker.json"
    case_ids = {canonical, case_dir.name, canonical.replace(":", "_")}
    clocks = clock_cards(case_dir, tag, cards_root)
    declp = case_dir / "DEADLINES.json"
    decl = _load(declp, [])
    if isinstance(decl, dict):
        decl = decl.get("deadlines") or []
    tracked, pending, skipped = [], [], []
    dt = None
    for c in clocks:
        rule = rule_for(c["statute_text"])
        rec = None
        if receipt and (_tok(receipt.get("addressee", "")) & _tok(c["addressee"])):
            rec = {"mail_id": receipt.get("mail_id"), "date": receipt["date"][:10], "basis": receipt.get("basis", "delivered"),
                   "document": receipt.get("document"), "job": receipt.get("job"), "source": receipt.get("source", "--receipt")}
        rec = rec or ledger_receipt(case_ids, c["addressee"], ledger)
        row = {"clock": c["n"], "addressee": c["addressee"], "rule_key": rule[0] if rule else None, "jurisdiction": rule[1] if rule else None,
               "start_event": c.get("start_undated") or c.get("start"), "verdict": c.get("verdict"), "relay": c.get("relay"), "mail": c.get("mail")}
        if not rule:
            row["why"] = "statute on the card resolves to no deadline rule (NOT IN TABLE)"; pending.append(row); continue
        if not rec:
            row["why"] = "PENDING MAIL — no SENT/CONFIRMED ledger item names this addressee"; pending.append(row); continue
        if dt is None:
            dt = _deadline_tracker()
        comp = dt.compute_deadline(rec["date"], rule[0], rule[1])
        if "error" in comp:
            row["why"] = f"compute_deadline: {comp['error']}"; pending.append(row); continue
        rule_txt = f"{comp['citation']} (CLOCK {c['n']} {c['addressee']} — receipt {rec['date']} [{rec['basis']}])"
        row.update(start=rec["date"], basis=rec["basis"], due=comp["deadline"], citation=comp["citation"], receipt=rec)
        if any(x.get("rule") == rule_txt and x.get("due") == comp["deadline"] for x in decl):
            row["why"] = "already declared"; skipped.append(row); tracked.append(row); continue
        decl.append({"due": comp["deadline"], "event_date": rec["date"], "rule": rule_txt,
                     "what": f"{comp['description']} — admin CLOCK {c['n']} {c['addressee']}; start = mail receipt {rec['date']} ({rec['basis']}, {rec['source']}"
                             f"{', job ' + str(rec['job']) if rec.get('job') else ''}) [deadline_tracker {rule[0]} {rule[1]}]",
                     "queue_lead_days": 5, "declared_by": "pipeline.admin_clocks", "source": c.get("relay")})
        try:
            dt.track_deadline(canonical, rule[0], rec["date"], rule[1], notes=f"admin CLOCK {c['n']} — {c['addressee']} — {comp['citation']} — receipt {rec['date']} ({rec['basis']})")
        except Exception as ex:
            row["lifecycle"] = f"track_deadline error {type(ex).__name__}: {ex}"
        tracked.append(row)
    new_rows = [t for t in tracked if t.get("why") != "already declared"]
    if new_rows:
        _backup(declp); declp.write_text(json.dumps(decl, indent=1))
    carried = carry_to_audit(case_dir, log) if new_rows else []
    bundle = next(iter(sorted(case_dir.glob("drafts/_bundle*_admin/m2"))), case_dir / "drafts/_admin_m2"); bundle.mkdir(parents=True, exist_ok=True)
    summary = {"updated": datetime.now().isoformat(timespec="seconds"), "case": canonical, "tag": tag, "clocks": len(clocks), "tracked": tracked,
               "pending_mail": pending, "deadlines_json_added": carried}
    (bundle / "ADMIN_CLOCKS.json").write_text(json.dumps(summary, indent=1))
    log(f"admin_clocks {canonical}: clocks={len(clocks)} tracked={len(tracked)} (new {len(new_rows)}) pending_mail={len(pending)} -> {bundle / 'ADMIN_CLOCKS.json'}")
    return summary


def carry_to_audit(case_dir: Path, log=print) -> list:
    """Same carry e2e_trigger.deadlines_step does: declared rows -> <root>/output/audit/deadlines.json via deadline_watch's scanners."""
    import pipeline.deadline_watch as dw
    dw.RECORD_ROOTS[:] = [ROOT / "obsidian_sync/cases", ROOT / "obsidian_sync/claim_candidates"]; dw.CASES = dw.RECORD_ROOTS[0]
    out = ROOT / "output/audit/deadlines.json"; out.parent.mkdir(parents=True, exist_ok=True)
    db = dw.load(out) if out.exists() else {"active": [], "done": [], "history": [], "items": [], "deadlines": []}
    known = {x["key"] for x in db["active"]} | {x["key"] for x in db["done"]}
    found = []
    for fn in (dw.scan_declared,):
        try:
            found += fn()
        except Exception as ex:
            log(f"deadline_watch.{fn.__name__} error {type(ex).__name__}: {ex}")
    new = [f for f in found if f["key"] not in known and f["case"] == case_dir.name]
    for n in new:
        n["status"] = "OPEN"; n["carded"] = False; db["active"].append(n)
        db.setdefault("history", []).append({"ts": datetime.now().isoformat(), "op": "detected", "key": n["key"], "by": "admin_clocks"})
    if new:
        _backup(out); db["updated"] = datetime.now().isoformat(timespec="seconds"); out.write_text(json.dumps(db, indent=1))
    return [n["key"] for n in new]


def table(summary: dict) -> str:
    rows = [("CLOCK", "ADDRESSEE", "RULE", "START", "DUE", "STATE")]
    for t in summary["tracked"]:
        rows.append((str(t["clock"]), t["addressee"][:38], t["rule_key"], f"{t['start']} ({t['basis']})", t["due"], "TRACKED" + (" (dup)" if t.get("why") else "")))
    for p in summary["pending_mail"]:
        rows.append((str(p["clock"]), p["addressee"][:38], p["rule_key"] or "—", p.get("start_event") or "—", "—", p["why"][:40]))
    w = [max(len(str(r[i])) for r in rows) for i in range(6)]
    return "\n".join("  ".join(str(r[i]).ljust(w[i]) for i in range(6)) for r in rows)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--case", required=True, help="canonical id (fed:2:26-cv-00123) or slug")
    ap.add_argument("--tag", default=None, help="admin room tag whose ledger CLOCK cards are read (default: cells only)")
    ap.add_argument("--receipt", default=None, help="JSON file {addressee, date, basis, document, job, source} — a mail receipt to start clocks from")
    a = ap.parse_args(argv)
    sys.path.insert(0, str(ROOT))
    from pipeline.e2e_trigger import resolve_case
    ctx = resolve_case(None, a.case)
    rec = json.loads(Path(a.receipt).read_text()) if a.receipt else None
    s = run(Path(ctx["case_dir"]), ctx["canonical"], a.tag, rec)
    print(table(s))
    return 0


if __name__ == "__main__":
    sys.exit(main())
