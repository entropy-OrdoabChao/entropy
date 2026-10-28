"""docket_index — build a free docket index (ECF # -> title -> filer -> date) from the NEF emails
(and local docket sheets). NO PACER downloads — the NEFs list the docket number + filing title +
filer already. Used to fill missing ECF cites and resolve UNVERIFIED attributions in the complaints.

  build(case_id)            -> {ecf: {title, filer, date}} , saved to output/docket_index_<slug>.json
  match_ecf(case_id, text)  -> the ECF # whose title best matches the given text (or None)
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import sys, re, json, base64, datetime
from pathlib import Path

ROOT = next((p for p in (_eroot(), _eroot())
             if (p / "cases_index").exists()), _eroot())
sys.path.insert(0, str(ROOT))

_DKT = re.compile(r"Document Number:\s*(\d+)", re.I)
# PA CCP (Philadelphia Office of Judicial Records) does NOT issue a federal
# "Document Number:" — its e-filing notices stamp "Control No.: NNNNNNNN" in the
# page footer, alongside "Case ID: NNNNNNNNN". Without this the CCP docket index
# built empty and tie_docket reported "+0 ECF# into CASE_TIMELINE.json", so a
# filed CCP document never entered the timeline and the posture read as stale.
# Forum-agnostic: whichever identifier the notice carries is the one used.
_CTRL = re.compile(r"Control\s*(?:No|Number)\.?\s*:?\s*(\d{6,10})", re.I)
_CASEID = re.compile(r"Case\s*ID\s*:?\s*(\d{6,12})", re.I)


def _doc_number(text: str) -> str:
    """The filing's identifier for THIS forum: federal Document Number, else
    PA CCP Control No. Returns '' when the text carries neither."""
    m = _DKT.search(text or "")
    if m:
        return m.group(1)
    m = _CTRL.search(text or "")
    return m.group(1) if m else ""
_DTEXT = re.compile(r"Docket Text:\s*(.{0,160})", re.I | re.S)
_FILER = re.compile(r"Filer:\s*(.+?)\s*(?:Document Number|Docket Text|\n)", re.I)


def _slug(case_id):
    return ("fed_" + case_id.split(":", 1)[-1]) if case_id.startswith("fed") else case_id.replace(":", "_")


def _docket_num(case_id):
    return case_id.split(":", 1)[-1]


def _body(payload):
    out = []
    def w(p):
        if p.get("body", {}).get("data"):
            try: out.append(base64.urlsafe_b64decode(p["body"]["data"]).decode("utf-8", "ignore"))
            except Exception: pass
        for s in p.get("parts", []) or []: w(s)
    w(payload); return "\n".join(out)


def _local_entries(case_id) -> dict:
    """Parse the LOCAL docket sheet OCR in the case intake ('N  TITLE ... by FILER') — the obsidian
    docket folder has the ECF #s + filer even when the NEFs are absent (e.g. AmeriSave)."""
    import glob
    slug = _slug(case_id)
    base = ROOT / "obsidian_sync" / "cases" / slug / "intake"
    idx = {}
    for p in glob.glob(str(base / "**" / "*ocket*.txt"), recursive=True):
        try:
            txt = Path(p).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for m in re.finditer(r"^\s*(\d{1,3})\s+([A-Z][^\n]{5,90})", txt, re.M):
            ecf, line = m.group(1), m.group(2).strip()
            bm = re.search(r"\bby\s+([A-Z][A-Za-z .,'\-]{3,40})", line)
            filer = bm.group(1).strip() if bm else ""
            if ecf not in idx or len(line) > len(idx[ecf]["title"]):
                idx[ecf] = {"title": line[:140], "filer": filer, "date": "", "src": "local docket OCR"}
    # PA CCP e-filing notices are not docket SHEETS and never match *ocket*.txt —
    # they arrive as their own intake documents ('ccp_order_*', '*e_fil*', '*notice*')
    # carrying "Control No.:" in the page footer. Read them so a CCP filing lands in
    # the index the same way a federal NEF does. Forum-agnostic by construction:
    # the identifier is whichever of Document Number / Control No. the notice bears.
    for p in glob.glob(str(base / "**" / "*.txt"), recursive=True):
        name = Path(p).name.lower()
        if "ocket" in name or name == "index.json":
            continue
        if not any(k in name for k in ("ccp", "e_fil", "efil", "notice", "accept")):
            continue
        try:
            txt = Path(p).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        num = _doc_number(txt)
        if not num:
            continue
        # title: the document's own caption line, else its filename
        tm = re.search(r"^\s*(NOTICE[^\n]{0,90}|ORDER|PRELIMINARY OBJECTIONS[^\n]{0,70}|"
                       r"MOTION[^\n]{0,80}|ANSWER[^\n]{0,80}|COMPLAINT[^\n]{0,60})\s*$",
                       txt, re.M | re.I)
        title = (tm.group(1).strip() if tm else Path(p).stem.replace("_", " "))[:140]
        dm = re.search(r"\b(\d{1,2}\s+[A-Z]{3}\s+20\d\d)\b", txt)
        date = ""
        if dm:
            try:
                date = datetime.datetime.strptime(dm.group(1), "%d %b %Y").date().isoformat()
            except Exception:
                date = ""
        if num not in idx or len(title) > len(idx[num].get("title", "")):
            idx[num] = {"title": title, "filer": "", "date": date,
                        "src": "CCP e-filing notice"}
    return idx


def build(case_id) -> dict:
    from scrapers.email_scraper import _get_gmail_service
    svc = _get_gmail_service()
    docket = _docket_num(case_id)
    ids = svc.users().messages().list(userId="me", q=f"from:paed.uscourts.gov {docket}",
                                      maxResults=80).execute().get("messages", [])
    idx = {}
    for m in ids:
        full = svc.users().messages().get(userId="me", id=m["id"], format="full").execute()
        hdrs = {h["name"].lower(): h["value"] for h in full["payload"].get("headers", [])}
        clean = re.sub(r"<[^>]+>", " ", _body(full["payload"]).replace("&nbsp;", " "))
        dk = _doc_number(clean)
        if not dk:
            continue
        ecf = dk
        title = (_DTEXT.search(clean).group(1).strip() if _DTEXT.search(clean) else hdrs.get("subject", ""))[:140]
        filer = (_FILER.search(clean).group(1).strip()[:60] if _FILER.search(clean) else "")
        if "document number" in filer.lower() or not filer:
            filer = "court" if re.match(r"\s*(ORDER|AND NOW|IT IS|JUDGMENT)", title, re.I) else ""
        try:
            date = datetime.datetime.strptime(hdrs.get("date", "")[:16].strip(), "%a, %d %b %Y").date().isoformat()
        except Exception:
            date = ""
        if ecf not in idx or len(title) > len(idx[ecf]["title"]):
            idx[ecf] = {"title": title, "filer": filer, "date": date}
    # merge the LOCAL docket OCR (fills ECF#s the NEFs lack — the obsidian docket folder)
    for ecf, e in _local_entries(case_id).items():
        if ecf not in idx or not idx[ecf].get("title"):
            idx[ecf] = e
        elif not idx[ecf].get("filer") and e.get("filer"):
            idx[ecf]["filer"] = e["filer"]
    out = ROOT / "output" / f"docket_index_{_slug(case_id)}.json"
    out.write_text(json.dumps(idx, indent=2, default=str), encoding="utf-8")
    return idx


def load(case_id) -> dict:
    p = ROOT / "output" / f"docket_index_{_slug(case_id)}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def match_ecf(case_id, text) -> str | None:
    """Best ECF whose title shares the most distinctive words with `text` (e.g. a filing/source name)."""
    idx = load(case_id)
    if not idx or not text:
        return None
    words = set(re.findall(r"[a-z]{4,}", text.lower()))
    best, score = None, 0
    for ecf, e in idx.items():
        tw = set(re.findall(r"[a-z]{4,}", e["title"].lower()))
        ov = len(words & tw)
        if ov > score:
            best, score = ecf, ov
    return best if score >= 2 else None


if __name__ == "__main__":
    for cid in (sys.argv[1:] or ["fed:2:26-cv-00123", "fed:2:26-cv-00456", "fed:2:26-cv-00789"]):
        idx = build(cid)
        print(f"{cid}: {len(idx)} docket entries indexed (ECF# -> title -> filer)")
        for ecf in sorted(idx, key=lambda x: int(x))[-4:]:
            print(f"   ECF {ecf}: {idx[ecf]['title'][:60]} | filer: {idx[ecf]['filer'][:30]}")
