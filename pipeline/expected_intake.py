#!/usr/bin/env python3
"""expected_intake -- close the tracker loop in CODE: inbound mail/scans that answer an AWAITING row in
<record>/EXPECTED.json get `received` set, so deadline_watch stops the clock and followup_drafter does not
draft a status inquiry for something that already arrived.

Before this, step 2b of the beezle-mail-watch cron prompt did it by hand (Hermes reading mail) - the autonomy
lock says the pipeline runs the chain without Hermes.

Per run:
  feed     = new Gmail ids in output/gmail_full_pull_done.json (notes in obsidian_sync/emails_full/<id>__*.md)
             + scan OCR text in output/hermes_email_intake/*.ocr.txt
  guardrail (candidate narrowing, deterministic): a document is offered to a row only if it is dated on/after the
             row's `sent` date AND it shares an identifier with the row or its case (docket / e-file / LS job /
             control numbers, case-slug digits). Nothing is decided here.
  comprehension (LLM, local-gpu lane, reasoning on): does this document ANSWER that awaited item? It must return a
             verbatim evidence line from the document.
  gate     (deterministic): the evidence line must appear verbatim in the document; else nothing is written.
  write    : EXPECTED.json row gets received / received_note / received_doc / received_by (backup .bak.<TS>).

    python3 -m pipeline.expected_intake            # live
    python3 -m pipeline.expected_intake --dry      # show matches, write nothing
    python3 -m pipeline.expected_intake --since 2026-10-01 --max 60
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import datetime as dt
import email.utils
import json
import os
import re
import shutil
import sys
from pathlib import Path

ROOT = _eroot()
sys.path.insert(0, str(ROOT))
from pipeline.deadline_watch import RECORD_ROOTS, parse_date  # noqa: E402

DONE_IDS = ROOT / "output/gmail_full_pull_done.json"
EMAILS = ROOT / "obsidian_sync/emails_full"
SCANS = ROOT / "output/hermes_email_intake"
STATE = ROOT / "output/audit/expected_intake_state.json"
LOG = ROOT / "output/audit/expected_intake.log"
DRY = "--dry" in sys.argv
MAX_DOCS = int(sys.argv[sys.argv.index("--max") + 1]) if "--max" in sys.argv else 80

# identifiers: digit-bearing tokens long enough to be a docket / e-file / job / control / claim number
ID_RX = re.compile(r"\b(?:\d{1,4}[-\s]?(?:cv|cr|EDA|WDA|MDA|EDM)[-\s]?\d{2,6}(?:\s\d{4})?|\d{2}-\d{3,5}|[A-Z0-9]{0,3}\d{5,12})\b", re.I)


def _ids(text: str) -> set:
    return {re.sub(r"[\s-]+", "", m.group(0)).lower() for m in ID_RX.finditer(text or "")}


def open_rows() -> list:
    rows = []
    for root in RECORD_ROOTS:
        for ef in root.glob("*/EXPECTED.json"):
            try:
                data = json.loads(ef.read_text())
            except Exception:
                continue
            items = data.get("expected", []) if isinstance(data, dict) else data
            case_ids = _ids(ef.parent.name.replace("_", " "))
            for i, it in enumerate(items if isinstance(items, list) else []):
                if not isinstance(it, dict) or (it.get("received") and str(it.get("received")) != "None"):
                    continue
                sent = parse_date(str(it.get("sent", "")))
                if not sent:
                    continue
                blob = " ".join(str(it.get(k, "")) for k in ("what", "rule", "action", "evidence"))
                rows.append({"file": ef, "case": ef.parent.name, "idx": i, "item": it, "sent": sent,
                             "ids": _ids(blob) | case_ids})
    return rows


def _doc_date(fm: str):
    m = re.search(r"^date:\s*(.+)$", fm, re.M)
    if not m:
        return None
    try:
        return email.utils.parsedate_to_datetime(m.group(1).strip()).date()
    except Exception:
        return parse_date(m.group(1))


def docs(state: dict, since: dt.date) -> list:
    out = []
    seen = set(state.get("seen", []))
    ids = json.loads(DONE_IDS.read_text()) if DONE_IDS.exists() else []
    new_ids = [i for i in ids if i not in seen][-2000:]
    if new_ids:
        names = {}
        with os.scandir(EMAILS) as it:
            want = set(new_ids)
            for e in it:
                mid = e.name.split("__", 1)[0]
                if mid in want:
                    names[mid] = e.path
        for mid in new_ids:
            p = names.get(mid)
            if not p:
                continue
            t = Path(p).read_text(encoding="utf-8", errors="replace")
            d = _doc_date(t[:1500])
            if d and d >= since:
                out.append({"key": mid, "path": p, "date": d, "text": t})
    for p in sorted(SCANS.glob("*.ocr.txt")):
        if p.name in seen:
            continue
        d = dt.date.fromtimestamp(p.stat().st_mtime)
        if d >= since:
            out.append({"key": p.name, "path": str(p), "date": d, "text": p.read_text(encoding="utf-8", errors="replace")})
    return out


def _llm(system: str, user: str) -> dict:
    from pipeline.m2 import analysis_runner as A
    model = A.MODEL or A._get(f"{A.LANE}/models", 10)["data"][0]["id"]
    body = {"model": model, "temperature": 0, "max_tokens": 3000, "chat_template_kwargs": {"enable_thinking": True},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    raw = A._post(f"{A.LANE}/chat/completions", body, timeout=900)
    msg = raw["choices"][0].get("message", {})
    return A.parse_json(msg.get("content") or msg.get("reasoning_content") or "")


SYSTEM = """You close a litigation tracker. You get ONE inbound document (email or scanned letter) and a numbered list
of items we are AWAITING (each: what we sent, who owes the reply, what reply we expect). Decide, from the document's
own words, which items (if any) it ANSWERS - i.e. it IS the awaited order / acceptance / rejection / decision /
acknowledgment / schedule, or it states that the awaited act happened. A document that merely mentions the case,
is our own outgoing mail, is a newsletter or a payment receipt for our postage, does NOT answer an item.
A PROPOSED order (blank date "this ___ day of ___", unsigned, attached to a party's motion) is the party's request,
NOT a ruling - it never answers an awaited ruling. An opponent's motion, brief, or reply is not the court's decision.
Reply with ONE JSON object: {"answers": [{"n": <item number>, "received": "YYYY-MM-DD" (date the reply issued per the
document), "note": "<who did what, one sentence>", "evidence": "<one line copied VERBATIM from the document that
proves it>"}]}  - use {"answers": []} when it answers nothing."""


def _date_in_text(d: dt.date, text: str) -> bool:
    """2026-10-17: a blank PROPOSED order attached to Progressive's POs was read as the ruling and dated by the scan's
    file time. A real reply prints its own date - require it."""
    t = _norm(text)
    forms = {d.isoformat(), f"{d.month}/{d.day}/{d.year}", f"{d.month:02d}/{d.day:02d}/{d.year}",
             f"{d.month}/{d.day}/{d.year % 100:02d}", f"{d.month:02d}/{d.day:02d}/{d.year % 100:02d}",
             f"{d.strftime('%B').lower()} {d.day}, {d.year}", f"{d.strftime('%b').lower()} {d.day}, {d.year}",
             f"{d.strftime('%b').lower()}. {d.day}, {d.year}", f"{d.day} {d.strftime('%B').lower()} {d.year}"}
    return any(f in t for f in forms)


def _norm(s: str) -> str:
    """Whitespace + markdown-neutral (blockquote '>' prefixes, emphasis, curly quotes) so a wrapped quoted email line
    still matches the evidence the LLM copied; words themselves must still match exactly."""
    s = re.sub(r"(?m)^\s*>+\s?", "", s or "")
    s = s.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')
    s = re.sub(r"[*_`]+", "", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def main() -> int:
    state = json.loads(STATE.read_text()) if STATE.exists() else {"seen": [], "closed": []}
    rows = open_rows()
    if not rows:
        print("[expected_intake] no open EXPECTED rows"); return 0
    since = dt.date.fromisoformat(sys.argv[sys.argv.index("--since") + 1]) if "--since" in sys.argv else min(r["sent"] for r in rows)
    feed = docs(state, since)
    done_now, offered = [], 0
    for d in feed:
        if offered >= MAX_DOCS:
            break
        dids = _ids(d["text"][:20000])
        cand = [r for r in rows if d["date"] >= r["sent"] and (r["ids"] & dids)]
        if cand:
            offered += 1
            listing = "\n".join(f"[{n}] case {r['case']} | sent {r['sent']} | from {r['item'].get('from','?')} | "
                                f"awaiting: {r['item'].get('what','')} | rule: {r['item'].get('rule','')}" for n, r in enumerate(cand))
            try:
                v = _llm(SYSTEM, f"AWAITING ITEMS:\n{listing}\n\nDOCUMENT ({d['key']}, dated {d['date']}):\n{d['text'][:9000]}")
            except Exception as e:
                print(f"[expected_intake] {d['key']}: lane error {e} - left unseen, retried next run"); continue
            for a in (v.get("answers") or []):
                n = a.get("n")
                if not isinstance(n, int) or not (0 <= n < len(cand)):
                    continue
                ev = a.get("evidence", "")
                if len(_norm(ev)) < 15 or _norm(ev) not in _norm(d["text"]):        # gate: proof must be verbatim
                    print(f"[expected_intake] GATE_FAIL {d['key']} -> {cand[n]['case']}: evidence not verbatim"); continue
                r = cand[n]
                rec = parse_date(str(a.get("received", "")))
                if not rec or not _date_in_text(rec, d["text"]):   # gate: the reply's date must be printed in the document
                    print(f"[expected_intake] GATE_FAIL {d['key']} -> {r['case']}: received date {a.get('received')} not in the document"); continue
                if rec < r["sent"]:
                    print(f"[expected_intake] GATE_FAIL {d['key']} -> {r['case']}: received {rec} predates sent {r['sent']}"); continue
                done_now.append((r, rec, a, d))
                rows = [x for x in rows if x is not r]
        if not DRY:
            state["seen"].append(d["key"])
    by_file = {}
    for r, rec, a, d in done_now:
        print(f"[expected_intake] {'DRY ' if DRY else ''}CLOSE {r['case']} #{r['idx']} '{r['item'].get('what','')[:70]}' "
              f"<- {d['key']} {rec}: {a.get('note','')[:120]}")
        by_file.setdefault(r["file"], []).append((r, rec, a, d))
    if not DRY:
        ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        for ef, hits in by_file.items():
            shutil.copy2(ef, ef.with_name(ef.name + f".bak.{ts}"))
            data = json.loads(ef.read_text())
            items = data.get("expected", []) if isinstance(data, dict) else data
            for r, rec, a, d in hits:
                it = items[r["idx"]]
                it.update(received=rec.isoformat(), received_note=f"{a.get('note','')} Evidence: \"{a.get('evidence','')}\"",
                          received_doc=d["path"], received_by="pipeline.expected_intake (m0 LLM + id/verbatim gates)")
                state["closed"].append({"ts": ts, "case": r["case"], "what": it.get("what"), "doc": d["path"]})
            ef.write_text(json.dumps(data, indent=1, ensure_ascii=False))
        STATE.write_text(json.dumps(state, indent=1))
    msg = (f"[expected_intake] {dt.datetime.now().isoformat(timespec='seconds')}: {len(feed)} new docs, {offered} offered "
           f"to the LLM, {len(done_now)} row(s) closed{' (DRY)' if DRY else ''}")
    print(msg)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(msg + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
