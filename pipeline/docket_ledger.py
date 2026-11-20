#!/usr/bin/env python3
"""docket_ledger -- ONE merged docket per case, from every ledger we keep. The UI (beezle_desk) and the
trackers read THIS; nobody keyword-guesses who filed what.

Sources (each event carries its source; filed_by is DERIVED FROM THE SOURCE, never from words):
  mailing_tracker.json      -> filed_by=you   (we mailed it; status SENT/DELIVERED/CONFIRMED; LS job id)
  efiling/efiled_receipts/  -> filed_by=you   (we e-filed it) + a second event filed_by=court when OJR
                               accepted/rejected it (date_accepted / status text)
  cases/<slug>/EXPECTED.json-> filed_by=you   for the trigger ("sent"), filed_by=court|them for `received`
  CASE_TIMELINE.json        -> both schemas (date_filed/type/filename  |  date/event/doc); filed_by from
                               explicit filed_by/filer/source fields if present, else from the doc's
                               rel_path family (intake/orders -> court; drafts/efiling -> you) else "them"
  deadlines.json (DONE)     -> filed_by=you  (done_evidence = what shipped)

Case identity: every case_map key + its aliases (case_id, slug variants, fragmented dirs) map to ONE
canonical key, so the 3x progressive / 3x nj_doi dirs all fold into the right docket.

CLI:  python3 -m pipeline.docket_ledger [--case <key|slug|id>] [--json]
API:  build() -> {canonical_key: [event,...]}   event = {date, filed_by, kind, title, ref, source, path}
"""
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import json, re, sys, argparse
from pathlib import Path
from datetime import datetime

ROOT = _eroot()
CASES = ROOT / "obsidian_sync/cases"
CASE_MAP = ROOT / "cases_index/case_map.json"
TRACKER = ROOT / "output/audit/mailing_tracker.json"
RECEIPTS = ROOT / "efiling/efiled_receipts"
DEADLINES = ROOT / "output/audit/deadlines.json"

COURT_WORDS = ("order", "opinion", "judgment", "notice", "schedul", "ojr", "accept", "reject", "docketed", "clerk", "prothonotary", "court_")
US_PATH = ("drafts/", "efiling/", "letterstream", "_bundle", "_FILING")
COURT_PATH = ("intake/", "orders/", "court/", "OJR_", "ORDER")


# ---- S-no-hardcode (2026-11-14): three literals keyed this ledger to ONE filer and FOUR case
# folders — the "_filer" key suffix, a four-name tuple of folder stems, and a "who is us" word
# list containing the filer's name. All three are now derived: the filer's own name tokens from
# cases_index/filer_identity.json, the folder stems from the case index keys themselves.
def _filer_toks() -> set:
    try:
        from pipeline.case_profile import filer_tokens
        return filer_tokens()
    except Exception:
        return set()


_FILER_SUFFIX_RX = "_(?:" + "|".join(sorted(_filer_toks()) or ["\0"]) + r").*$"
_US_ROLE_WORDS = ("plaintiff", "appellant", "petitioner", "mail-watch", "efiling")


def _US_WORDS() -> tuple:
    """Words meaning 'this side' in a filename/folder: the filer's own name tokens + role words."""
    return tuple(sorted(_filer_toks())) + _US_ROLE_WORDS


def _KEY_STEMS() -> set:
    """Leading stems of the case index's OWN keys (e.g. 'progressive' from
    'example_v_insurer'), so a folder is matched to a case without a literal list."""
    try:
        from pipeline.case_profile import all_case_keys
        toks = _filer_toks()
        out = set()
        for k in all_case_keys():
            stem = str(k).split(":")[-1].split("_")[0].lower()
            if len(stem) > 3 and not stem.isdigit() and stem not in toks:
                out.add(stem)
        return out
    except Exception:
        return set()


def _jl(p, d):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return d


def _day(s):
    if not s:
        return ""
    m = re.search(r"(20\d\d-\d\d-\d\d)", str(s))
    if m:
        return m.group(1)
    m = re.search(r"(\d{1,2})/(\d{1,2})/(20\d\d)", str(s))
    return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}" if m else ""


def aliases(case_map):
    """canonical key -> set of ids/slugs that mean this case (dirs, case_id, appeal numbers, user aliases)."""
    out = {}
    for key, v in case_map.items():
        if not isinstance(v, dict):
            continue
        a = {key, key.replace(":", "_"), re.sub(r"^fed_2_", "fed_", key.replace(":", "_")), key.split(":")[-1]}
        if v.get("case_id"):
            a.add(str(v["case_id"]))
        for an in (v.get("appeals") or {}):
            a.add(str(an))
        for x in (v.get("aliases") or []):
            a.add(str(x).lower())
        tail = key.split(":")[-1].lower()
        stem = re.sub(_FILER_SUFFIX_RX, "", key).lower()
        for d in CASES.iterdir():
            n = d.name.lower()
            if d.is_dir() and (tail in n or (len(stem) > 3 and n.startswith(stem)) or n.startswith(tail.split("_")[0] + "_") and tail.split("_")[0] in _KEY_STEMS()):
                a.add(d.name)
        out[key] = {s for s in a if s}
    return out


def canon_for(ident, alias_map):
    if not ident:
        return None
    s = str(ident).strip()
    for key, al in alias_map.items():
        if s in al or s.lower() in al or s.replace(":", "_") in al:
            return key
    sl = s.lower()
    for key, al in alias_map.items():
        if any(len(x) > 4 and x.lower() in sl for x in al):
            return key
    return None


def _ev(date, filed_by, kind, title, ref="", source="", path="", status=""):
    return {"date": _day(date) or "", "filed_by": filed_by, "kind": kind, "title": str(title)[:220],
            "ref": str(ref)[:80], "source": source, "path": str(path)[:160], "status": str(status)[:60]}


def build(case_map=None):
    case_map = case_map or _jl(CASE_MAP, {})
    al = aliases(case_map)
    docket = {k: [] for k in al}

    # 1. mailing tracker -> you
    items = _jl(TRACKER, {}).get("items", {})
    items = items.values() if isinstance(items, dict) else items
    for it in items:
        if not isinstance(it, dict):
            continue
        k = canon_for(it.get("case_id"), al) or canon_for(it.get("document"), al)
        if not k:
            continue
        hist = it.get("history") or []
        if isinstance(hist, str):
            hist = _jl_str(hist)
        sent = next((h[0] for h in hist if isinstance(h, (list, tuple)) and len(h) > 1 and str(h[1]).upper() in ("SENT", "MAILED", "CONFIRMED", "DELIVERED", "COMPLETED")), None) \
            or (hist[-1][0] if hist and isinstance(hist[-1], (list, tuple)) else "")
        doc = Path(str(it.get("document", ""))).name
        docket[k].append(_ev(sent, "you", "mailed", doc, f"LS {it.get('letterstream_job_id') or '?'}",
                             "mailing_tracker", it.get("document", ""), it.get("status", "")))

    # 2. e-file receipts -> you (+ court act on accept/reject)
    for p in sorted(RECEIPTS.glob("*.json")):
        r = _jl(p, {})
        k = canon_for(r.get("case_slug"), al) or canon_for(r.get("case_id"), al)
        if not k:
            continue
        docket[k].append(_ev(r.get("date_presented"), "you", "e-filed", r.get("fjd_document") or r.get("filing_type") or p.stem,
                             f"E-File {r.get('efile_no', '?')}", "efiled_receipt", r.get("document_pdf", ""), r.get("status", "")))
        st = str(r.get("status", "")).lower()
        acc = r.get("date_accepted") or r.get("date_reviewed") or r.get("date_deemed_filed")
        if acc or "accepted" in st or "rejected" in st:
            docket[k].append(_ev(acc or r.get("date_presented"), "court", "ojr-" + ("rejected" if "reject" in st else "accepted"),
                                 f"OJR {'rejected' if 'reject' in st else 'accepted'}: {r.get('fjd_document') or r.get('filing_type') or ''}",
                                 f"E-File {r.get('efile_no', '?')}", "efiled_receipt", str(p.relative_to(ROOT)), r.get("status", "")))

    # 3. EXPECTED rows -> trigger (you) + received (court/them)
    for ef in CASES.glob("*/EXPECTED.json"):
        k = canon_for(ef.parent.name, al)
        if not k:
            continue
        rows = _jl(ef, [])
        rows = rows.get("expected", []) if isinstance(rows, dict) else rows
        for r in rows:
            if not isinstance(r, dict):
                continue
            frm = str(r.get("from", "")).lower()
            who = "court" if any(w in frm for w in ("court", "judge", "clerk", "prothonotary", "ojr", "ca3", "circuit", "superior")) else "them"
            if r.get("received"):
                docket[k].append(_ev(r["received"], who, "received", f"{r.get('what', '')[:90]} -- {str(r.get('received_note') or r.get('received'))[:100]}",
                                     r.get("rule", ""), "EXPECTED", str(ef.relative_to(ROOT)), "RECEIVED"))
            else:
                docket[k].append(_ev(r.get("expect_by"), who, "awaiting", f"AWAITING {r.get('what', '')[:110]}",
                                     r.get("rule", ""), "EXPECTED", str(ef.relative_to(ROOT)), "OPEN"))

    # 4. CASE_TIMELINE.json (both schemas) -- fills the gaps the ledgers don't cover (orders, exhibits, correspondence)
    for tf in CASES.glob("*/CASE_TIMELINE.json"):
        k = canon_for(tf.parent.name, al)
        if not k:
            continue
        _tl = _jl(tf, {}) or {}
        _entries = _tl if isinstance(_tl, list) else _tl.get("entries", [])   # bare-list schema (planet) or {"entries": [...]}
        for e in _entries:
            if not isinstance(e, dict):
                continue
            d = e.get("date_filed") or e.get("date") or (e.get("mtime_iso") or "")[:10]
            title = e.get("event") or e.get("filename") or e.get("description") or e.get("doc") or "?"
            typ = str(e.get("type") or "").lower()
            path = str(e.get("rel_path") or e.get("doc") or "")
            fb = str(e.get("filed_by") or e.get("filer") or "").lower()
            lead = re.sub(r"[^A-Z ]", "", str(title)[:40].upper()).strip().split(" ")[0] if e.get("event") else ""
            is_evidence = (not e.get("event")) and typ in ("", "unknown") and "intake" in path.replace("\\", "/").lower()
            if is_evidence:
                who, typ = "evidence", "evidence"      # intake exhibits (Gmail PDFs, credit reports): record, not docket
            elif lead in ("OJR", "COURT", "ORDER", "JUDGE", "CLERK", "PROTHONOTARY", "SUPERIOR", "CA", "OPINION"):
                who = "court"                           # hand-written event names its actor in the lead word
            elif lead in ("MAILED", "PLAINTIFF", "APPELLANT", "EFILED", "FILED", "SERVED", "SENT", "WE"):
                who = "you"
            elif lead in ("DEFENDANT", "DEFENDANTS", "OPPOSING", "COUNSEL", "APPELLEE"):
                who = "them"
            elif fb:
                who = "court" if "court" in fb or "judge" in fb or "clerk" in fb else ("you" if any(x in fb for x in _US_WORDS()) else "them")
            elif any(w in typ for w in COURT_WORDS) or any(w in path for w in COURT_PATH):
                who = "court"
            elif any(w in path for w in US_PATH) or any(w in typ for w in ("notice_instrument", "service", "record_correct")):
                who = "you"
            else:
                who = "them" if typ not in ("", "unknown", "?") else "unknown"
            docket[k].append(_ev(d, who, typ or "document", title, e.get("docket") or e.get("docket_ref") or "", "CASE_TIMELINE", path))

    # 5. closed outbound deadlines -> you (what shipped, with evidence)
    for x in _jl(DEADLINES, {}).get("active", []):
        if x.get("status") == "DONE" and x.get("direction", "out") != "inbound":
            k = canon_for(x.get("case"), al)
            if k:
                docket[k].append(_ev(x.get("done_ts") or x.get("due"), "you", "deadline-met", x.get("what", ""), x.get("key", "").split("|")[1] if "|" in x.get("key", "") else "", "deadlines", "", x.get("done_evidence", "")[:60]))

    # dedupe (same day + same title prefix + same filed_by) and sort newest first
    for k, evs in docket.items():
        seen, out = set(), []
        for e in sorted(evs, key=lambda e: e["date"], reverse=True):
            sig = (e["date"], e["filed_by"], re.sub(r"\W+", "", e["title"].lower())[:40])
            if sig in seen:
                continue
            seen.add(sig); out.append(e)
        docket[k] = out
    return docket


def _jl_str(s):
    try:
        return json.loads(s.replace("'", '"'))
    except Exception:
        return []


def main(argv=None):
    ap = argparse.ArgumentParser(); ap.add_argument("--case"); ap.add_argument("--json", action="store_true"); a = ap.parse_args(argv)
    d = build()
    if a.case:
        al = aliases(_jl(CASE_MAP, {})); k = canon_for(a.case, al)
        d = {k: d.get(k, [])} if k else {}
    if a.json:
        print(json.dumps(d, indent=1, ensure_ascii=False)); return
    for k, evs in d.items():
        from collections import Counter
        c = Counter(e["filed_by"] for e in evs)
        print(f"== {k}  {len(evs)} events  you={c['you']} court={c['court']} them={c['them']} evidence={c['evidence']} unknown={c['unknown']}")
        for e in evs[:12]:
            print(f"  {e['date'] or '????-??-??'} {e['filed_by']:7} {e['kind'][:14]:14} {e['title'][:70]:70} {e['ref'][:28]}")


if __name__ == "__main__":
    main()
