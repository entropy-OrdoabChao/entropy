#!/usr/bin/python3
"""fetch_exhibits.py — fetch the record emails named in the stub's exhibit rows and save each as a cleaned text exhibit.
Facts for exhibit descriptions are taken ONLY from the saved documents (printed below).
Promoted from $M/_fetch_gmail_exhibits.py (2026-10-25); cleaning + header + key-field logic unchanged.

A row is fetched when its [doc:] ends in .txt and its **Source:** names Gmail ids (16 hex). --letters restricts which
(default D,E,G,I = today's ORDER_17 run; F's text was produced by another step and is not re-fetched).

Run: cd <home> && env PYTHONPATH=$PWD /usr/bin/python3 -m pipeline.packet.fetch_exhibits --m <motion dir> [--stub ...]
     [--outdir <dir>] [--letters D,E,G,I] [--facts-json _gmail_exhibit_facts.json]
"""
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if not (ROOT / "pipeline" / "m1.py").exists():
    ROOT = _eroot()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.packet.manifest import read_manifest, default_stub  # noqa: E402

DEFAULT_M = ROOT / "obsidian_sync/cases/example_v_insurer/drafts/_bundle13_order17/motions/vacate_m2room"
DEFAULT_LETTERS = "D,E,G,I"


def clean(body: str) -> str:
    body = body.replace("&zwnj;", " ").replace("\u200c", "").replace("\u034f", "").replace("&mdash;", "—")
    body = body.replace("&#8203;", "").replace("&#39;", "'").replace("&amp;", "&")
    out = []
    for ln in body.replace("\r", "").split("\n"):
        s = ln.strip()
        if not s:
            out.append("")
            continue
        if re.match(r"^<?https?://", s) or s.startswith("tel:"):
            continue
        if re.fullmatch(r"[\s.:~+=?,I7B3NG4S<>-]+", s):   # ascii-art footer
            continue
        out.append(s)
    txt = "\n".join(out)
    txt = re.sub(r"\n{3,}", "\n\n", txt).strip()
    return txt


KEY_FIELDS = [("effective", r"Effective Date:\s*([0-9/]+)"), ("conf", r"Confirmation #:\s*(\d+)"),
              ("refund", r"Refund due:\s*\$?([0-9.,]+)"), ("rate", r"Rate Change:\s*\$?([0-9.,]+\s*\w+)"),
              ("amount", r"Payment amount:\s*\$([0-9.,]+)"), ("reversal", r"Reversal date:\s*([0-9/]+)"),
              ("carvana_amt", r"\$([0-9,]+\.\d\d)\s*$"), ("vehicle", r"(20\d\d MAZDA CX-5)"),
              ("policy", r"(Pennsylvania Auto\s+\d+)"), ("balance", r"Balance due:\s*\$?([0-9.,]+)"),
              ("amount_due", r"Amount due:\s*\$([0-9.,]+)")]


def fetch(rows: list, outdir: Path, letters: set, facts_json: Path | None) -> dict:
    from scrapers.email_scraper import _get_gmail_service  # readonly OAuth token already on disk
    from beezle_mcp_server import _gmail_headers, _gmail_body_text  # same decoders the MCP tool uses
    svc = _get_gmail_service()
    outdir.mkdir(parents=True, exist_ok=True)
    summary = {}
    for r in rows:
        letter = r["letter"]
        if letters and letter not in letters:
            continue
        if not r["doc"].lower().endswith(".txt") or not r["gmail_ids"]:
            continue
        ids = r["gmail_ids"]
        parts, facts = [], []
        for mid in ids:
            msg = svc.users().messages().get(userId="me", id=mid, format="full").execute()
            h = _gmail_headers(msg)
            body = clean(_gmail_body_text(msg.get("payload", {})))
            hdr = (f"Gmail message id: {mid}\nFrom: {h.get('from','')}\nTo: {h.get('to','')}\n"
                   f"Date: {h.get('date','')}\nSubject: {h.get('subject','')}\n")
            parts.append(hdr + "\n" + body)
            kf = {"id": mid, "date": h.get("date", ""), "subject": h.get("subject", "")}
            for lab, rx in KEY_FIELDS:
                m = re.search(rx, body, re.M)
                if m:
                    kf[lab] = m.group(1).strip()
            facts.append(kf)
        out = outdir / Path(r["doc"]).name
        out.write_text(("\n\n" + "=" * 78 + "\n\n").join(parts) + "\n", encoding="utf-8")
        summary[letter] = {"file": out.name, "messages": facts}
        print(f"[{letter}] {out.name}  ({len(ids)} message(s), {out.stat().st_size} bytes)")
    print(json.dumps(summary, indent=1))
    if facts_json:
        facts_json.write_text(json.dumps(summary, indent=1), encoding="utf-8")
    return summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Gmail ids in the stub's exhibit rows -> cleaned text exhibits")
    ap.add_argument("--m", default=str(DEFAULT_M), help="motion dir (stub + exhibit files)")
    ap.add_argument("--stub", default=None, help="stub/manifest with the NEW EVIDENCE BLOCK (default <M>/EXHIBITS.md else <M>/motion_to_vacate.md)")
    ap.add_argument("--outdir", default=None, help="where the .txt exhibits land (default <M>)")
    ap.add_argument("--letters", default=DEFAULT_LETTERS, help="comma list of exhibit letters to fetch ('' = every .txt row with Gmail ids)")
    ap.add_argument("--facts-json", default="_gmail_exhibit_facts.json", help="key-field summary file name in outdir ('' = none)")
    a = ap.parse_args(argv)
    M = Path(a.m)
    stub = Path(a.stub) if a.stub else default_stub(M)
    outdir = Path(a.outdir) if a.outdir else M
    rows = read_manifest(stub, bases=[M])
    letters = {x.strip() for x in a.letters.split(",") if x.strip()}
    fetch(rows, outdir, letters, (outdir / a.facts_json) if a.facts_json else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
