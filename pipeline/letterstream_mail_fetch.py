#!/usr/bin/env python3
r"""
pipeline/letterstream_mail_fetch.py

The "letter stream": read LetterStream confirmation emails from Gmail so m0 knows what was actually MAILED
(and stops re-drafting it). No LetterStream API/2FA needed — this is email-side, on the SAME Gmail service
the docket-email cron already uses (scrapers.email_scraper._get_gmail_service).

LetterStream sends, from support@letterstream.com:
  - "Order Confirmation - <your label>"      => the package was submitted for mailing
  - "Letterstream Job Completed - <label>"   => the mail piece was produced/sent
The <label> is the free-text name you typed when ordering (e.g. "superior court CPP", "Third cir Appeal",
"EDCPA filling 7/7 ..."). We record each to output/letterstream_mailed.jsonl; filing_status matches a
consolidated package to a confirmation by token overlap between the label and the package's court/case.

Run:  python -m pipeline.letterstream_mail_fetch            # fetch + append to the ledger
      python -m pipeline.letterstream_mail_fetch --show     # print the ledger
"""
from __future__ import annotations
import re
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LEDGER = REPO / "output" / "letterstream_mailed.jsonl"
SENDER = "support@letterstream.com"


def fetch(days: int = 180) -> list:
    sys.path.insert(0, str(REPO))
    from scrapers.email_scraper import _get_gmail_service
    svc = _get_gmail_service()
    q = (f"newer_than:{days}d from:{SENDER} "
         f'(subject:("Order Confirmation") OR subject:("Job Completed") OR subject:("has been mailed") '
         f'OR subject:("Job Pending"))')
    ids = svc.users().messages().list(userId="me", q=q, maxResults=200).execute().get("messages", [])
    rows = []
    for m in ids:
        full = svc.users().messages().get(userId="me", id=m["id"], format="metadata",
                                          metadataHeaders=["From", "Subject", "Date"]).execute()
        h = {x["name"].lower(): x["value"] for x in full.get("payload", {}).get("headers", [])}
        subj = h.get("subject", "")
        kind = ("completed" if re.search(r"job completed", subj, re.I)
                else "pending" if re.search(r"job pending", subj, re.I)       # in the cart, awaiting approval — NOT mailed
                else "confirmation" if re.search(r"order confirmation", subj, re.I) else "mailed")
        label = re.sub(r"^\s*(?:order confirmation|letterstream job completed|job pending)\s*[-:]\s*", "", subj, flags=re.I).strip()
        rows.append({"msg_id": m["id"], "date": h.get("date", ""), "subject": subj,
                     "kind": kind, "label": label})
    return rows


def _load_ledger() -> dict:
    out = {}
    if LEDGER.exists():
        for ln in LEDGER.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if ln:
                try:
                    r = json.loads(ln); out[r["msg_id"]] = r
                except Exception:
                    pass
    return out


def sync(days: int = 180) -> int:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    have = _load_ledger()
    added = 0
    for r in fetch(days):
        if r["msg_id"] not in have:
            have[r["msg_id"]] = r
            added += 1
    with open(LEDGER, "w", encoding="utf-8") as fh:
        for r in have.values():
            fh.write(json.dumps(r) + "\n")
    print(f"letterstream: {len(have)} confirmations in ledger (+{added} new) -> {LEDGER}")
    return added


# --- matching for filing_status ---
_STOP = {"the", "and", "for", "filling", "filing", "appeal", "court", "of", "in", "to", "cir",
         "district", "no", "a"}


def _tokens(s: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]{3,}", (s or "").lower()) if w not in _STOP}


def mailed_labels() -> list:
    """[{label, date, kind, tokens}] from the ledger — the confirmed-mailed pieces."""
    out = []
    for r in _load_ledger().values():
        out.append({**r, "tokens": _tokens(r["label"])})
    return out


def is_mailed(*descriptors: str) -> dict | None:
    """Conservative match: a confirmation label must share >=2 salient tokens with the descriptors AND at
    least one must be DISTINCTIVE (>=6 chars). Ties break to the most RECENT confirmation. Returns the
    match or None — we prefer a MISS (fall back to the package-manifest skip) over a false attribution,
    because these labels are terse free text. NOTE: when the system mails a package via LetterStream it sets
    the label to the package id, so future matches are exact; this fuzzy path is only for hand-typed labels."""
    want = set()
    for d in descriptors:
        want |= _tokens(d)
    best = None  # (n_shared, has_distinctive, date, row)
    for r in mailed_labels():
        if r.get("kind") == "pending":     # awaiting approval in the cart -> NOT mailed; never count as filed
            continue
        shared = r["tokens"] & want
        if len(shared) >= 2 and any(len(t) >= 6 for t in shared):
            key = (len(shared), r.get("date", ""))
            if best is None or key > best[0]:
                best = (key, r)
    return best[1] if best else None


def pending_jobs() -> list:
    """[{label, date}] — LetterStream jobs AWAITING APPROVAL (in the cart, NOT yet mailed/paid) whose label
    is NOT already covered by a later 'completed' (so a job that was pending then mailed drops off). This is
    the ACTUAL to-pay list; the tracker uses it instead of a stale hand-written print queue."""
    def _casenum(s):
        m = re.search(r"\b(\d{5})\b", s or "")   # docket case number: 02822 / 04767 / 06077 / 41002969->41002
        return m.group(1) if m else None
    rows = list(_load_ledger().values())
    completed = [r for r in rows if r.get("kind") == "completed"]
    completed_tok = [_tokens(r["label"]) for r in completed]
    completed_case = {_casenum(r["label"]) for r in completed} - {None}
    seen, out = set(), []
    for r in rows:
        if r.get("kind") != "pending":
            continue
        lbl = r["label"]
        pt = _tokens(lbl)
        cn = _casenum(lbl)
        # a job is DONE if a completed shares >=2 salient tokens OR the SAME docket case-number (the
        # consolidated per-case/court packet mailed that case's pieces together — e.g. ECF 174 opposition).
        if any(len(pt & ct) >= 2 for ct in completed_tok) or (cn and cn in completed_case):
            continue
        key = re.sub(r"\s+", " ", lbl.lower())[:40]
        if key in seen:                      # collapse duplicate pending emails (same job, multiple recipients)
            continue
        seen.add(key)
        out.append({"label": lbl, "date": r.get("date", "")})
    return sorted(out, key=lambda x: x.get("date", ""))


if __name__ == "__main__":
    if "--pending" in sys.argv:
        print("LetterStream PENDING (awaiting approval — not mailed):")
        for r in pending_jobs():
            print(f"  {r['date'][:16]:16} | {r['label']}")
    elif "--show" in sys.argv:
        for r in sorted(_load_ledger().values(), key=lambda x: x.get("date", "")):
            print(f"  [{r['kind']:12}] {r['date'][:22]:22} | {r['label']}")
    else:
        sync()
