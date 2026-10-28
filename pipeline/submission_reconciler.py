"""submission_reconciler — teach the system what WE ALREADY FILED so it stops flagging filed
documents as "stuck / overdue / no files". The healer was screaming false CRITICALs because nothing
paired the ECF e-mail notifications (which include OUR OWN filings) to the docket/deadline state.

What it does (NIGHTLY — not all day):
  1. Reads ECF NOTIFICATION metadata from output/email_scans/*.json. **Never pulls the PDFs.**
  2. Parses each court/CM-ECF notice: case, docket seq (de_seq_num), document title, FILER, date.
  3. Merges any NEW docket entry into output/docket_index_<case>.json (filer/title/date/source).
  4. Records OUR filings (filer == the plaintiff, data-driven from case_registry — R2) in a per-case
     ledger output/filings/our_filings_<slug>.json = the authoritative "what we submitted" list.
  5. Reconciles our drafted/validated outputs + open deadlines against that ledger and writes
     output/filings/reconciliation_report.md (filed/cleared vs genuinely pending) — the source of
     truth the deadline tracker / healer should consult instead of guessing "overdue".

Idempotent: keyed by ECF message_id + docket seq; re-runs only fold in genuinely new notices.

CLI:
  python -m pipeline.submission_reconciler            # all cases, write
  python -m pipeline.submission_reconciler --dry-run  # show, don't write
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import json, re, sys, glob, html
from pathlib import Path
from datetime import datetime

ROOT = next((p for p in (_eroot(), _eroot()) if (p / "cases_index").exists()),
            _eroot())
SCANS = ROOT / "output" / "email_scans"
DOCKETS = ROOT / "output"                       # docket_index_<case>.json live here
FILINGS = ROOT / "output" / "filings"
STATE = FILINGS / "_reconciled_msgids.json"     # idempotency ledger (processed ECF message_ids)
_REG_P = ROOT / "cases_index" / "case_registry.json"
_REG = json.loads(_REG_P.read_text(encoding="utf-8")) if _REG_P.exists() else {}   # fresh home: no registry yet, import must not fail

# court / CM-ECF notification senders (extend as new courts appear)
_ECF_SENDERS = ("uscourts.gov", "courts.phila.gov", "ecf_", "cmecf", "efiling", "pacourts")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


_STOP = {"plaintiff", "petitioner", "defendant", "request", "order", "motion", "notice", "case",
         "activity", "document", "amended", "complaint", "court", "filed", "filer", "estate", "etal",
         "individually", "information", "services", "corporation", "mortgage", "llc", "inc", "company",
         "that", "this", "with", "from", "shall", "been", "will", "have", "they", "name", "number",
         "file", "honorable", "judge", "magistrate", "district", "clerk", "memorandum", "amas",
         # docket/action words that leaked into learned name tokens (keep tokens NAME-only)
         "address", "denied", "evidence", "form", "praecipe", "terminate", "default", "service",
         "summary", "judgment", "reconsideration", "certify", "vacate", "strike", "remand", "recusal",
         "support", "opposition", "response", "exhibit", "affidavit", "statement", "petition", "application"}


def _defendant_tokens() -> set[str]:
    out: set[str] = set()
    for e in _REG.values():
        if isinstance(e, dict) and isinstance(e.get("parties"), list):
            for d in e["parties"][1:]:                       # parties[0] is the plaintiff
                out |= {_norm(w) for w in re.split(r"[_\s]", str(d)) if len(w) >= 4}
    return out


def _docket_path_for(cid: str) -> Path:
    """docket_index file is named with canonical_id, first ':' -> '_' (e.g. fed:2:26-cv-00456 ->
    docket_index_fed_2:26-cv-00456.json)."""
    return DOCKETS / f"docket_index_{cid.replace(':', '_', 1)}.json"


def _our_tokens() -> set[str]:
    """OUR-side name tokens, learned data-drivenly (R2 — nothing about the person hardcoded).
    Seed = registry plaintiff SURNAME (parties[0], e.g. 'filer'). Then learn the pro-se filer's given
    names by capturing ONLY the NAME PHRASE that ends in the surname in docket titles (e.g.
    'MALIK-FARDY BABAH FILER'), and taking its word tokens — NOT the whole title (which would learn
    'motion', 'judge', even the judge's name). Tokenizing on word boundaries handles the FILER/MALIK
    spelling variants (shared token 'fardy')."""
    seeds = {str(e["parties"][0]).lower() for e in _REG.values()
             if isinstance(e, dict) and isinstance(e.get("parties"), list) and e["parties"]}
    seeds = {s for s in seeds if len(s) >= 4}
    toks = set(seeds)
    for e in _REG.values():
        if not isinstance(e, dict) or not e.get("canonical_id"):
            continue
        dp = _docket_path_for(e["canonical_id"])
        if not dp.exists():
            continue
        try:
            docket = json.loads(dp.read_text(encoding="utf-8"))
        except Exception:
            continue
        for ent in docket.values():
            title = (ent or {}).get("title", "") if isinstance(ent, dict) else ""
            for seed in seeds:
                # up to 3 capitalized name words immediately preceding the surname + the surname itself
                for m in re.finditer(r"([A-Za-z][\w.'\-]*(?:\s+[A-Za-z][\w.'\-]*){0,3}\s+%s\w*)" % re.escape(seed),
                                     title, re.I):
                    for w in re.findall(r"[A-Za-z]{4,}", m.group(1)):
                        if w.lower() not in _STOP:
                            toks.add(w.lower())
    return toks


def _canon(cid_or_match: str | None) -> tuple[str | None, str | None]:
    """Resolve any matched_case / canonical form to (canonical_id, slug) via the registry."""
    if not cid_or_match:
        return None, None
    n = _norm(cid_or_match)
    for cid, e in _REG.items():
        if not isinstance(e, dict):
            continue
        forms = {cid, e.get("canonical_id", ""), e.get("slug", "")}
        if any(f and _norm(f) == n for f in forms):
            return e.get("canonical_id") or cid, e.get("slug") or cid.replace(":", "_")
    # fall back: docket number substring (>=7 chars to avoid false hits)
    for cid, e in _REG.items():
        if not isinstance(e, dict):
            continue
        for ident in (set(e.get("docket_aliases") or []) | {cid.split(":")[-1]}):
            if ident and len(_norm(ident)) >= 7 and _norm(ident) in n:
                return e.get("canonical_id") or cid, e.get("slug") or cid.replace(":", "_")
    return None, None


def _case_for(email: dict):
    cid, slug = _canon(email.get("matched_case"))
    if slug:
        return cid, slug
    blob = email.get("subject", "") + " " + " ".join(email.get("case_references") or [])
    for c, e in _REG.items():
        if not isinstance(e, dict):
            continue
        for ident in (set(e.get("docket_aliases") or []) | {c.split(":")[-1]}):
            if ident and len(_norm(ident)) >= 7 and _norm(ident) in _norm(blob):
                return e.get("canonical_id") or c, e.get("slug") or c.replace(":", "_")
    return None, None


def _parse_ecf(email: dict) -> dict | None:
    frm = (email.get("from") or "").lower()
    if not any(s in frm for s in _ECF_SENDERS):
        return None
    refs = " ".join(email.get("case_references") or [])
    body = email.get("body_preview") or ""
    subj = email.get("subject") or ""
    seq = None
    m = re.search(r"de_seq_num=(\d+)", refs + body)
    if m:
        seq = m.group(1)
    filer = None
    m = re.search(r"Filer:\s*</strong>\s*</td>\s*<td>\s*([A-Z][A-Za-z\-\. ]+)", refs + body)
    if not m:
        m = re.search(r"Filer:[^A-Za-z]*([A-Z][A-Za-z\-\.]+(?:\s+[A-Z][A-Za-z\-\.]+){0,3})", refs + body)
    if m:
        filer = html.unescape(m.group(1)).strip()
    # document title: the tail of the ECF subject after the caption (e.g., "... et al Motion to Remand")
    title = re.sub(r"^Activity in Case\s+\S+\s+", "", subj)
    title = re.sub(r"Notice of order on Case\s*#?\d+", "Order", title).strip() or subj
    cid, slug = _case_for(email)
    return {
        "message_id": email.get("message_id"), "date": email.get("date"),
        "cid": cid, "case": slug, "seq": seq, "title": title[:200],
        "filer": filer or ("court" if "order" in subj.lower() else None),
        "from": email.get("from"),
    }


def _ours(filer: str | None, ptoks: set[str]) -> bool:
    """OURS if any whole word-token of the filer is one of our learned name tokens. Token-split (not
    substring) so 'EQUIFAX'/'court' never partial-match a name token."""
    if not filer:
        return False
    ftoks = set(re.findall(r"[a-z]{4,}", filer.lower()))
    return bool(ftoks & ptoks)


def reconcile(write: bool = True) -> dict:
    ptoks = _our_tokens()
    processed = set()
    if STATE.exists():
        try:
            processed = set(json.loads(STATE.read_text(encoding="utf-8")))
        except Exception:
            processed = set()
    # email_scan_*.json are full snapshots, so the same ECF notice recurs across files. Dedupe by the
    # DOCKET identity (case, seq) — that is unique per filing. The scraper's message_id is NOT reliably
    # unique (it collides across distinct notices), so keying on it collapsed real filings; (case,seq)
    # does not. Fall back to message_id only when a notice carries no seq.
    def _key(rec):
        if rec.get("case") and rec.get("seq"):
            return ("ds", rec["case"], rec["seq"])
        return ("mid", rec.get("message_id") or id(rec))
    uniq: dict = {}
    for f in sorted(glob.glob(str(SCANS / "email_scan_*.json"))):
        try:
            data = json.loads(Path(f).read_text(encoding="utf-8"))
        except Exception:
            continue
        for em in data.get("results", []):
            rec = _parse_ecf(em)
            if not rec or not (rec.get("seq") or rec.get("message_id")):
                continue
            k = _key(rec)
            if k not in uniq or (rec.get("filer") and not uniq[k].get("filer")):
                uniq[k] = rec
    notices, new_msgids = list(uniq.values()), set()

    by_case: dict[str, list] = {}
    for n in notices:
        if not n["case"]:
            continue
        n["ours"] = _ours(n["filer"], ptoks)
        by_case.setdefault(n["case"], []).append(n)
        new_msgids.add(n["message_id"])

    summary = {"cases": {}, "our_filings": 0, "new_docket_entries": 0}
    FILINGS.mkdir(parents=True, exist_ok=True)
    report = [f"# Submission reconciliation — {datetime.now():%Y-%m-%d %H:%M}",
              "What WE filed (from ECF notifications; PDFs NOT pulled). Source of truth for 'filed vs stuck'.", ""]

    for case, recs in sorted(by_case.items()):
        cid = next((n.get("cid") for n in recs if n.get("cid")), None)
        di_path = _docket_path_for(cid) if cid else (DOCKETS / f"docket_index_{case}.json")
        docket = {}
        if di_path.exists():
            try:
                docket = json.loads(di_path.read_text(encoding="utf-8"))
            except Exception:
                docket = {}
        added = 0
        ours = []
        for n in recs:
            if n["seq"] and n["seq"] not in docket:
                docket[n["seq"]] = {"title": n["title"], "filer": n["filer"] or "unknown",
                                    "date": n["date"], "source": "ecf_notification"}
                added += 1
            if n["ours"]:
                ours.append({"seq": n["seq"], "title": n["title"], "date": n["date"],
                             "message_id": n["message_id"]})
        # write our-filings ledger (dedup by message_id)
        ledger_path = FILINGS / f"our_filings_{case}.json"
        existing = []
        if ledger_path.exists():
            try:
                existing = json.loads(ledger_path.read_text(encoding="utf-8"))
            except Exception:
                existing = []
        # dedupe a FILING by docket seq (one seq = one filing, even if ECF sent several notices);
        # fall back to message_id when a notice carries no seq.
        def _key(o):
            return f"seq:{o['seq']}" if o.get("seq") else f"mid:{o.get('message_id')}"
        merged, seen = [], set()
        for o in existing + ours:
            k = _key(o)
            if k not in seen:
                seen.add(k); merged.append(o)
        if write:
            if added:
                di_path.write_text(json.dumps(docket, indent=2), encoding="utf-8")
            ledger_path.write_text(json.dumps(merged, indent=2), encoding="utf-8")
        summary["cases"][case] = {"notices": len(recs), "ours": len(ours), "new_docket_entries": added}
        summary["our_filings"] += len(ours)
        summary["new_docket_entries"] += added
        report.append(f"## {case}")
        report.append(f"- ECF notices seen: {len(recs)} | OUR filings: {len(merged)} | new docket entries merged: {added}")
        for o in merged[-8:]:
            report.append(f"  - [FILED by us] seq {o.get('seq')}  {(o.get('title') or '')[:80]}  ({o.get('date')})")
        report.append("")

    if write:
        (FILINGS / "reconciliation_report.md").write_text("\n".join(report), encoding="utf-8")
        STATE.write_text(json.dumps(sorted(processed | new_msgids)), encoding="utf-8")
    return summary


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    s = reconcile(write=not dry)
    print(("DRY-RUN " if dry else "") + "reconcile:",
          f"{s['our_filings']} of-our filings across {len(s['cases'])} cases; "
          f"{s['new_docket_entries']} new docket entries merged.")
    for c, st in s["cases"].items():
        print(f"  {c}: notices={st['notices']} ours={st['ours']} new_entries={st['new_docket_entries']}")
    if not dry:
        print(f"  -> {FILINGS / 'reconciliation_report.md'}")
