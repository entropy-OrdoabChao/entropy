#!/usr/bin/env python3
"""
case-manager
Master case organization for Hermes legal AI.
"""
import argparse
import json
import re
import sys
from datetime import datetime, timedelta, date
from pathlib import Path

DATA_ROOT = Path.home() / ".hermes" / "data" / "cases"
ACTIVE_FILE = Path.home() / ".hermes" / "data" / "active_case"
DATA_ROOT.mkdir(parents=True, exist_ok=True)


def slug(s):
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def case_dir(case_no):
    p = DATA_ROOT / slug(case_no)
    p.mkdir(parents=True, exist_ok=True)
    (p / "exhibits").mkdir(exist_ok=True)
    (p / "notes").mkdir(exist_ok=True)
    return p


def load_json(path, default=None):
    if not path.exists():
        return default if default is not None else {}
    return json.loads(path.read_text())


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2, default=str))


def append_jsonl(path, entry):
    with path.open("a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def load_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def list_all_cases():
    cases = []
    for d in DATA_ROOT.iterdir():
        if d.is_dir():
            meta = load_json(d / "meta.json")
            if meta:
                cases.append(meta)
    return cases


# ─────────────────────────── Commands ───────────────────────────

def cmd_init(args):
    cd = case_dir(args.case)
    meta_path = cd / "meta.json"
    if meta_path.exists():
        sys.exit(f"EXISTS: {args.case}")

    meta = {
        "case_no": args.case,
        "case_slug": slug(args.case),
        "caption": args.caption,
        "court": args.court,
        "judge": args.judge,
        "stage": args.stage,
        "filed_date": args.filed,
        "created_at": datetime.now().isoformat(),
        "appellate": None,
    }
    save_json(meta_path, meta)
    save_json(cd / "parties.json", {"parties": []})
    save_json(cd / "related_cases.json", {"related": []})
    print(f"INITIALIZED: {cd}")


def cmd_add_party(args):
    cd = case_dir(args.case)
    parties = load_json(cd / "parties.json", {"parties": []})
    parties["parties"].append({
        "name": args.name,
        "role": args.role,
        "counsel": args.counsel,
        "pro_se": args.pro_se,
        "added_at": datetime.now().isoformat(),
    })
    save_json(cd / "parties.json", parties)
    print(f"ADDED PARTY: {args.name} ({args.role})")


def cmd_add_deadline(args):
    cd = case_dir(args.case)
    entry = {
        "description": args.description,
        "due": args.due,
        "source_rule": args.source_rule,
        "priority": args.priority,
        "status": "pending",
        "created_at": datetime.now().isoformat(),
    }
    append_jsonl(cd / "deadlines.jsonl", entry)
    print(f"ADDED DEADLINE: {args.description} due {args.due}")


def cmd_add_event(args):
    cd = case_dir(args.case)
    entry = {
        "type": args.type,
        "description": args.description,
        "date": args.date,
        "time": args.time,
        "location": args.location,
        "created_at": datetime.now().isoformat(),
    }
    append_jsonl(cd / "calendar.jsonl", entry)
    print(f"ADDED EVENT: {args.description} on {args.date}")


def cmd_link(args):
    cd = case_dir(args.case)
    related_path = cd / "related_cases.json"
    related = load_json(related_path, {"related": []})
    related["related"].append({
        "case_no": args.related,
        "court": args.court,
        "relationship": args.relationship,
        "linked_at": datetime.now().isoformat(),
    })
    save_json(related_path, related)
    print(f"LINKED: {args.case} → {args.related} ({args.relationship})")

    # Bidirectional — only if the related case has been initialized
    related_dir = DATA_ROOT / slug(args.related)
    if related_dir.exists():
        rel_related = load_json(related_dir / "related_cases.json", {"related": []})
        # Inverse relationship mapping
        inverse = {
            "appeal_from": "appeal_of",
            "appeal_of": "appeal_from",
            "consolidated_with": "consolidated_with",
            "parallel_to": "parallel_to",
            "prior": "subsequent",
            "subsequent": "prior",
            "remand_from": "remanded_to",
            "remanded_to": "remand_from",
        }.get(args.relationship, args.relationship)
        rel_related["related"].append({
            "case_no": args.case,
            "relationship": inverse,
            "linked_at": datetime.now().isoformat(),
        })
        save_json(related_dir / "related_cases.json", rel_related)
        print(f"  ↔ bidirectional link added to {args.related}")


def cmd_promote_to_appeal(args):
    cd = case_dir(args.case)
    meta_path = cd / "meta.json"
    meta = load_json(meta_path)
    if not meta:
        sys.exit(f"Case not found: {args.case}")

    meta["appellate"] = {
        "appellate_no": args.appellate_no,
        "appellate_court": args.appellate_court,
        "notice_of_appeal_filed": args.notice_of_appeal_filed,
        "promoted_at": datetime.now().isoformat(),
    }
    meta["stage"] = "appellate"
    save_json(meta_path, meta)

    # Initialize appellate case as separate entry, linked back
    app_dir = case_dir(args.appellate_no)
    if not (app_dir / "meta.json").exists():
        app_meta = {
            "case_no": args.appellate_no,
            "case_slug": slug(args.appellate_no),
            "caption": meta.get("caption") + " (on appeal)",
            "court": args.appellate_court,
            "judge": "TBD",
            "stage": "appellate",
            "filed_date": args.notice_of_appeal_filed,
            "created_at": datetime.now().isoformat(),
            "trial_case_no": args.case,
        }
        save_json(app_dir / "meta.json", app_meta)
        save_json(app_dir / "parties.json", load_json(cd / "parties.json", {"parties": []}))
        save_json(app_dir / "related_cases.json", {"related": [
            {"case_no": args.case, "relationship": "appeal_from", "court": meta.get("court")}
        ]})

    # Link trial → appellate
    related = load_json(cd / "related_cases.json", {"related": []})
    related["related"].append({
        "case_no": args.appellate_no,
        "court": args.appellate_court,
        "relationship": "appeal_of",
        "linked_at": datetime.now().isoformat(),
    })
    save_json(cd / "related_cases.json", related)

    print(f"PROMOTED: {args.case} → appellate {args.appellate_no} in {args.appellate_court}")
    print(f"  Trial case marked stage=appellate")
    print(f"  New appellate case initialized at {app_dir}")


def cmd_list(args):
    cases = list_all_cases()
    if args.stage:
        cases = [c for c in cases if c.get("stage") == args.stage]
    if args.court:
        cases = [c for c in cases if c.get("court") == args.court]

    print(f"\n{len(cases)} case(s):\n")
    for c in cases:
        marker = " [ACTIVE]" if ACTIVE_FILE.exists() and ACTIVE_FILE.read_text().strip() == c["case_no"] else ""
        app_marker = f" → appeal: {c['appellate']['appellate_no']}" if c.get("appellate") else ""
        print(f"  [{c.get('stage', '?'):12}] {c['case_no']:25}  {c.get('caption', ''):40}{marker}{app_marker}")
    print()


def cmd_status(args):
    cd = case_dir(args.case)
    meta = load_json(cd / "meta.json")
    if not meta:
        sys.exit(f"NOT FOUND: {args.case}")

    parties = load_json(cd / "parties.json", {"parties": []})["parties"]
    deadlines = load_jsonl(cd / "deadlines.jsonl")
    calendar = load_jsonl(cd / "calendar.jsonl")
    related = load_json(cd / "related_cases.json", {"related": []})["related"]
    ledger_entries = load_jsonl(cd / "ledger.jsonl")

    print()
    print("═" * 60)
    print(f" CASE: {meta.get('caption')}")
    print(f" No: {meta.get('case_no')}")
    print(f" Court: {meta.get('court')}")
    print(f" Judge: {meta.get('judge')}")
    print(f" Stage: {meta.get('stage')} (filed {meta.get('filed_date')})")
    if meta.get("appellate"):
        a = meta["appellate"]
        print(f" Appellate: {a['appellate_no']} in {a['appellate_court']}")
    print("═" * 60)

    if parties:
        print("\nPARTIES")
        for p in parties:
            counsel = "pro se" if p.get("pro_se") else p.get("counsel", "—")
            print(f"  {p['role'].capitalize():12} {p['name']} (counsel: {counsel})")

    if related:
        print("\nRELATED")
        for r in related:
            print(f"  → {r['relationship']}: {r['case_no']} ({r.get('court', '')})")

    pending = [d for d in deadlines if d.get("status") == "pending"]
    if pending:
        today = date.today().isoformat()
        upcoming = sorted([d for d in pending if d["due"] >= today], key=lambda x: x["due"])
        overdue = sorted([d for d in pending if d["due"] < today], key=lambda x: x["due"])
        if overdue:
            print("\nOVERDUE DEADLINES")
            for d in overdue:
                print(f"  [OVERDUE] {d['due']}  {d['description']}  [{d['source_rule']}]")
        if upcoming:
            print("\nUPCOMING DEADLINES")
            for d in upcoming[:10]:
                print(f"  [{d.get('priority', 'med').upper():6}] {d['due']}  {d['description']}  [{d['source_rule']}]")

    if calendar:
        future = sorted([e for e in calendar if e["date"] >= date.today().isoformat()], key=lambda x: x["date"])
        if future:
            print("\nCALENDAR")
            for e in future[:10]:
                print(f"  {e['date']} {e.get('time', '')}  {e['description']}  ({e.get('location', '')})")

    if ledger_entries:
        total_score = sum(e.get("score", 0) for e in ledger_entries)
        last = ledger_entries[-1]
        print("\nLEDGER STATUS")
        print(f"  Cumulative violation score: {total_score}")
        print(f"  Last entry: Doc #{last.get('docket_no')} ({last.get('filer')}, {last.get('date')})")

    notes_dir = cd / "notes"
    notes = sorted(notes_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    if notes:
        print("\nRECENT NOTES")
        for n in notes[:5]:
            print(f"  {n.stem}")

    print("═" * 60)
    print()


def cmd_deadlines(args):
    today = date.today()
    cases = list_all_cases()
    if args.case:
        cases = [c for c in cases if c["case_no"] == args.case]

    all_deadlines = []
    for c in cases:
        cd = case_dir(c["case_no"])
        for d in load_jsonl(cd / "deadlines.jsonl"):
            if d.get("status") != "pending":
                continue
            d["_case"] = c["case_no"]
            d["_caption"] = c["caption"]
            all_deadlines.append(d)

    if args.overdue:
        filtered = [d for d in all_deadlines if d["due"] < today.isoformat()]
    elif args.window:
        cutoff = (today + timedelta(days=args.window)).isoformat()
        filtered = [d for d in all_deadlines if today.isoformat() <= d["due"] <= cutoff]
    else:
        filtered = all_deadlines

    filtered.sort(key=lambda x: x["due"])
    print(f"\n{len(filtered)} deadline(s):\n")
    for d in filtered:
        marker = "[OVERDUE]" if d["due"] < today.isoformat() else f"[{d.get('priority', 'med').upper()}]"
        print(f"  {marker:9} {d['due']}  {d['_case']:20}  {d['description']}")
        print(f"            └─ {d.get('source_rule', '')}")
    print()


def cmd_activate(args):
    cd = case_dir(args.case)
    if not (cd / "meta.json").exists():
        sys.exit(f"NOT FOUND: {args.case}")
    ACTIVE_FILE.write_text(args.case)
    print(f"ACTIVE_CASE: {args.case}")


def cmd_deactivate(args):
    if ACTIVE_FILE.exists():
        ACTIVE_FILE.unlink()
    print("ACTIVE_CASE cleared")


def cmd_active(args):
    if ACTIVE_FILE.exists():
        print(ACTIVE_FILE.read_text().strip())
    else:
        print("(none)")


def cmd_note(args):
    cd = case_dir(args.case)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    note_path = cd / "notes" / f"{ts}_{slug(args.title)}.md"
    note_path.write_text(f"# {args.title}\n\nDate: {datetime.now().isoformat()}\n\n{args.content}\n")
    print(f"WROTE: {note_path}")


# ─────────────────────────── Main ───────────────────────────

def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("init")
    pi.add_argument("--case", required=True)
    pi.add_argument("--caption", required=True)
    pi.add_argument("--court", required=True)
    pi.add_argument("--judge", required=True)
    pi.add_argument("--stage", default="trial",
                    choices=["pre-filing", "trial", "post-trial", "appellate", "post-appellate", "supreme", "closed"])
    pi.add_argument("--filed")

    pp = sub.add_parser("add-party")
    pp.add_argument("--case", required=True)
    pp.add_argument("--name", required=True)
    pp.add_argument("--role", required=True)
    pp.add_argument("--counsel", default="")
    pp.add_argument("--pro-se", action="store_true")

    pd = sub.add_parser("add-deadline")
    pd.add_argument("--case", required=True)
    pd.add_argument("--description", required=True)
    pd.add_argument("--due", required=True, help="YYYY-MM-DD")
    pd.add_argument("--source-rule", required=True)
    pd.add_argument("--priority", default="med", choices=["low", "med", "high", "critical"])

    pe = sub.add_parser("add-event")
    pe.add_argument("--case", required=True)
    pe.add_argument("--type", required=True)
    pe.add_argument("--description", required=True)
    pe.add_argument("--date", required=True)
    pe.add_argument("--time", default="")
    pe.add_argument("--location", default="")

    pl = sub.add_parser("link")
    pl.add_argument("--case", required=True)
    pl.add_argument("--related", required=True)
    pl.add_argument("--relationship", required=True,
                    choices=["appeal_from", "appeal_of", "consolidated_with", "parallel_to",
                             "prior", "subsequent", "remand_from", "remanded_to"])
    pl.add_argument("--court", default="")

    pp2 = sub.add_parser("promote-to-appeal")
    pp2.add_argument("--case", required=True)
    pp2.add_argument("--appellate-no", required=True)
    pp2.add_argument("--appellate-court", required=True)
    pp2.add_argument("--notice-of-appeal-filed", required=True)

    pls = sub.add_parser("list")
    pls.add_argument("--stage")
    pls.add_argument("--court")

    ps = sub.add_parser("status")
    ps.add_argument("--case", required=True)

    pdl = sub.add_parser("deadlines")
    pdl.add_argument("--case")
    pdl.add_argument("--window", type=int, help="Days ahead")
    pdl.add_argument("--overdue", action="store_true")

    pa = sub.add_parser("activate")
    pa.add_argument("--case", required=True)

    sub.add_parser("deactivate")
    sub.add_parser("active")

    pn = sub.add_parser("note")
    pn.add_argument("--case", required=True)
    pn.add_argument("--title", required=True)
    pn.add_argument("--content", required=True)

    args = p.parse_args()
    handlers = {
        "init": cmd_init, "add-party": cmd_add_party, "add-deadline": cmd_add_deadline,
        "add-event": cmd_add_event, "link": cmd_link, "promote-to-appeal": cmd_promote_to_appeal,
        "list": cmd_list, "status": cmd_status, "deadlines": cmd_deadlines,
        "activate": cmd_activate, "deactivate": cmd_deactivate, "active": cmd_active,
        "note": cmd_note,
    }
    handlers[args.cmd](args)


if __name__ == "__main__":
    main()
