#!/usr/bin/env python3
"""ledger_feed -- overdue EXPECTED.json rows + mailing_tracker proof-of-delivery
-> MINISTERIAL_SILENCE evidence cells for m2 (and a human report).

Why (user-locked 2026-10-16): an overdue ministerial duty (Hall's Rule 1925(a) opinion,
the Superior Court's Rule 123 application, a DOBI/OCC/CFPB non-response) must become
evidence DETERMINISTICALLY -- duty + trigger date + our proof of delivery + their silence
is an element of mandamus / tort. Until now nothing under pipeline/ read EXPECTED.json
or the tracker; the finding lived only in chat.

Cells are FACTS ONLY (no legal conclusion): the row verbatim, the duty text as declared,
the LetterStream job ids that prove delivery, and the days of silence. Idempotent via
output/audit/ledger_feed_state.json keyed (case, sent, what).

CLI:  python3 -m pipeline.ledger_feed [--case <slug>] [--dry-run] [--report]
"""
from __future__ import annotations
import argparse, json, re, sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "obsidian_sync" / "cases"
TRACKER = ROOT / "output" / "audit" / "mailing_tracker.json"
STATE = ROOT / "output" / "audit" / "ledger_feed_state.json"
CELLS_ROOT = ROOT / "output" / "research"
REPORT_DIR = ROOT / "obsidian_sync" / "00_system"
CELL_TYPE = "MINISTERIAL_SILENCE"


def _d(s):
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def slug_to_case_ids(slug: str) -> set[str]:
    """Bridge slug dir -> every case_id form the tracker has used (canonical colon form
    plus the legacy underscore forms). 'phila_ccp_260100000' -> {'phila_ccp:260100000', ...}."""
    out = {slug}
    m = re.match(r"^(fed|phila_ccp|pa_super|ca3)_(.+)$", slug)
    if m:
        fam, num = m.groups()
        out |= {f"{fam}:{num}", f"{fam}:2:{num}" if fam == "fed" else f"{fam}:{num}"}
    return out


def load_tracker() -> dict:
    try:
        return json.loads(TRACKER.read_text(encoding="utf-8")).get("items", {})
    except Exception:
        return {}


_KW = {"court": ("court", "prothonotary", "clerk", "judge", "chambers", "superior", "supreme", "ojr", "judicial records"),
       "agency": ("dobi", "banking", "occ", "cfpb", "irs", "treasury", "attorney general", "department", "bureau", "board"),
       "opponent": ("esq", "counsel", "llp", "llc", "company", "inc", "bank", "trust")}


def proof_of_delivery(items: dict, case_ids: set[str], sent: date, who: str) -> list[dict]:
    """Tracker rows for this case mailed within 3 days of `sent` to a recipient of the
    EXPECTED row's class (court / agency / opponent). Facts only: job id + status + recipient."""
    hits = []
    kws = _KW.get((who or "").lower().strip(), ())
    for tid, it in items.items():
        if it.get("case_id") not in case_ids:
            continue
        if it.get("status") not in ("SENT", "CONFIRMED", "FILED"):
            continue
        when = None
        for h in it.get("history") or []:
            if isinstance(h, (list, tuple)) and len(h) >= 2 and h[1] in ("SENT", "CONFIRMED", "FILED", "IN_CART"):
                when = _d(h[0]); break
        if when is None and it.get("history"):
            when = _d(it["history"][0][0])
        if when and abs((when - sent).days) > 3:
            continue
        recips = [r if isinstance(r, str) else (r.get("name") or "") for r in (it.get("recipients") or [])]
        blob = " ".join(recips).lower()
        if kws and not any(k in blob for k in kws):
            continue
        hits.append({"tracker_id": tid, "job_id": it.get("letterstream_job_id"), "status": it["status"],
                     "recipients": recips, "document": str(it.get("document", ""))[-80:],
                     "class": it.get("class"), "dated": when.isoformat() if when else None})
    return hits


def open_rows(slug: str, today: date):
    ef = CASES / slug / "EXPECTED.json"
    if not ef.exists():
        return []
    try:
        rows = json.loads(ef.read_text(encoding="utf-8"))
    except Exception:
        return []
    rows = rows.get("expected", []) if isinstance(rows, dict) else rows
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict) or r.get("received"):
            continue
        exp = _d(r.get("expect_by")); sent = _d(r.get("sent"))
        if not exp or not sent:
            continue
        fu = _d(r.get("follow_up")) or (exp + timedelta(days=7))
        out.append((r, sent, exp, fu, (today - fu).days))
    return out


def build_cell(slug: str, r: dict, sent: date, exp: date, fu: date, silent: int, proof: list, today: date) -> dict:
    return {
        "bot": "ledger_feed", "cell_type": CELL_TYPE, "status": "FACT", "level": None,
        "claim": f"{slug}|{sent.isoformat()}|{r.get('what','')[:80]}",
        "case": slug, "duty": r.get("rule") or "declared",
        "owed_by": r.get("from"), "what": r.get("what"),
        "trigger_date": sent.isoformat(), "expected_by": exp.isoformat(), "follow_up": fu.isoformat(),
        "as_of": today.isoformat(), "days_silent_past_follow_up": silent,
        "days_since_trigger": (today - sent).days,
        "proof_of_delivery": proof,
        "action_if_silent": r.get("action"),
        "source_row": r, "evidence_tags": ["[doc]", "[tracker]"],
        "excerpt": f"{r.get('from','?')} silent {silent}d past follow-up on: {r.get('what','')[:100]}",
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--case"); ap.add_argument("--dry-run", action="store_true"); ap.add_argument("--report", action="store_true")
    ap.add_argument("--today")
    a = ap.parse_args(argv)
    today = _d(a.today) or date.today()
    items = load_tracker()
    state = json.loads(STATE.read_text()) if STATE.exists() else {"emitted": {}}
    slugs = [a.case] if a.case else sorted(p.name for p in CASES.iterdir() if (p / "EXPECTED.json").exists())
    emitted, table = [], []
    for slug in slugs:
        cids = slug_to_case_ids(slug)
        for r, sent, exp, fu, silent in open_rows(slug, today):
            late = silent > 0
            proof = proof_of_delivery(items, cids, sent, r.get("from", ""))
            table.append((slug, "LATE" if late else "open", fu.isoformat(), silent, r.get("from"), r.get("what", "")[:70], len(proof)))
            if not late:
                continue
            key = f"{slug}|{sent.isoformat()}|{r.get('what','')}"
            cell = build_cell(slug, r, sent, exp, fu, silent, proof, today)
            prev = state["emitted"].get(key)
            if prev and not a.dry_run:
                # refresh days_silent in place (facts move), never duplicate the cell
                p = Path(prev["path"])
                if p.exists():
                    lines = [l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
                    lines = [l for l in lines if json.loads(l).get("claim") != cell["claim"]]
                    lines.append(json.dumps(cell)); p.write_text("\n".join(lines) + "\n", encoding="utf-8")
                continue
            outdir = CELLS_ROOT / f"m2_{slug}_ledger"
            if not a.dry_run:
                outdir.mkdir(parents=True, exist_ok=True)
                with (outdir / "cells.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps(cell) + "\n")
                state["emitted"][key] = {"path": str(outdir / "cells.jsonl"), "first": today.isoformat()}
            emitted.append(cell)
    if not a.dry_run:
        STATE.parent.mkdir(parents=True, exist_ok=True); STATE.write_text(json.dumps(state, indent=1))
    print(f"{'case':26} {'st':5} {'follow_up':10} {'late':>5} {'from':9} what{'':66} proof")
    for t in sorted(table, key=lambda t: t[2]):
        print(f"{t[0]:26} {t[1]:5} {t[2]:10} {t[3]:>5} {str(t[4])[:9]:9} {t[5]:70} {t[6]}")
    print(f"\n{CELL_TYPE} cells emitted now: {len(emitted)} (state: {len(state['emitted'])} total)")
    if a.report:
        L = [f"# MINISTERIAL SILENCE — {today.isoformat()}", "", "Facts only: duty as declared in EXPECTED.json [doc], delivery per mailing_tracker/LetterStream [tracker]. No legal conclusion here.", "",
             "| case | late (d) | owed by | duty | what | proof of delivery (LS job / status) | action if silent |", "|---|---|---|---|---|---|---|"]
        for slug in slugs:
            for r, sent, exp, fu, silent in open_rows(slug, today):
                if silent <= 0:
                    continue
                proof = proof_of_delivery(items, slug_to_case_ids(slug), sent, r.get("from", ""))
                pj = ", ".join(f"{p['job_id']}/{p['status']}" for p in proof if p.get("job_id")) or "e-filed / see tracker"
                L.append(f"| {slug} | {silent} | {r.get('from')} | {r.get('rule','')} | {r.get('what','')} | {pj} | {r.get('action','')} |")
        out = REPORT_DIR / f"MINISTERIAL_SILENCE_{today.strftime('%Y%m%d')}.md"
        out.write_text("\n".join(L) + "\n", encoding="utf-8"); print("report:", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
