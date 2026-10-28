#!/usr/bin/env python3
"""efile_artifact — the e-file artifact step of the packet tail (after assemble_packet).

Writes, for one rendered packet:
  motions/chain_<name>/<stub>_FILING.pdf     = PACKET/00_*.pdf + every exhibit PDF, in index order (what FJD gets)
  motions/chain_<name>/<stub>_DRAFT.md       = copy of the draft (discovery reads it for the title / PII scan)
  motions/chain_<name>/EFILE_PART3.json      = the portal's "motion on an order" page-3 answers, ALL FROM DATA:
      judge          pipeline/case_judge.resolve: CASE_FACTS.md -> intake DOCKET_REPORT -> order signature -> registry mirror
      order_signed   units header "signed YYYY-MM-DD"  (m2/ADVERSE_POINTS.md line 1)
      order_entered  units header "entered YYYY-MM-DD" (Rule 236 date)
      other_decided  Y if CASE_TIMELINE holds any ORDER ENTERED event   (the portal asks "has another motion been decided")
      pending        Y if CASE_TIMELINE holds a plaintiff/defendant MOTION/PETITION e-filed AFTER the order with no later
                     ORDER event disposing of it
      deadline       filing-type YAML `deadline_days` (+ 1 Pa.C.S. § 1908 weekend/holiday roll) from order_entered;
                     absent -> null (the portal field is optional; the chain's deadline ledger is the real clock)
      extended       N unless CASE_TIMELINE holds an "extended" order after order_entered
  A missing judge is NOT guessed: judge=null + a WARNING line; efile_run.py refuses to continue past page 3 for a
  motion-on-order document until cases_index/case_registry.json carries it (S-efile-part3-source, 2026-10-26).

Usage:  python3 -m pipeline.packet.efile_artifact --root <ROOT> --case <slug> --m <motion dir> --stub <stub> \
            [--court PA_CP_PHILA] [--ftype petition_341c] [--units <ADVERSE_POINTS.md>]
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None

_HOLIDAYS_FIXED = {(1, 1), (7, 4), (11, 11), (12, 25)}   # 1 Pa.C.S. § 1908 roll: weekend / fixed legal holidays


def _roll(d: _dt.date) -> _dt.date:
    while d.weekday() >= 5 or (d.month, d.day) in _HOLIDAYS_FIXED:
        d += _dt.timedelta(days=1)
    return d


def _case_judge(root: Path, slug: str) -> tuple[str | None, str]:
    from pipeline.case_judge import resolve
    r = resolve(root, slug)
    return r["judge"], r["source"]


def _units_header(units: Path) -> dict:
    h = units.read_text(encoding="utf-8").splitlines()[0] if units.exists() else ""
    g = lambda rx: (re.search(rx, h) or [None, None])[1]
    return {"control_no": g(r"Control No\.?\s*(\d{6,})"), "signed": g(r"signed\s+(\d{4}-\d{2}-\d{2})"),
            "entered": g(r"entered\s+(\d{4}-\d{2}-\d{2})"), "label": g(r"\b(ORDER_\d+)\b")}


def _timeline(root: Path, slug: str) -> list[dict]:
    p = root / "obsidian_sync" / "cases" / slug / "CASE_TIMELINE.json"
    if not p.exists():
        return []
    d = json.loads(p.read_text(encoding="utf-8"))
    ents = d.get("entries") if isinstance(d, dict) else d
    return [e for e in (ents or []) if isinstance(e, dict) and e.get("date")]


def _us(iso: str | None) -> str | None:
    if not iso:
        return None
    y, m, d = iso.split("-")
    return f"{m}/{d}/{y}"


def build_part3(root: Path, slug: str, units: Path, ftype: str | None) -> dict:
    judge, jsrc = _case_judge(root, slug)
    hdr = _units_header(units)
    tl = _timeline(root, slug)
    ent = hdr["entered"]
    orders = [e for e in tl if re.search(r"\bORDER ENTERED\b", e.get("event", ""), re.I)]
    after = [e for e in tl if ent and e["date"] > ent]
    motions = [e for e in after if re.search(r"\b(E-?FILED|FILED)\b.*\b(MOTION|PETITION|APPLICATION)\b", e.get("event", ""), re.I)]
    disposed = [e for e in after if re.search(r"\bORDER\b.*\b(GRANT|DENI|DISPOS|MOOT)", e.get("event", ""), re.I)]
    pending = bool(motions) and len(motions) > len(disposed)
    extended = any(re.search(r"\bextend(ed|s|ing)\b", e.get("event", ""), re.I) and re.search(r"\bORDER\b", e.get("event", ""), re.I) for e in after)
    deadline = None
    days = None
    if ftype and yaml is not None:
        y = root / "obsidian_sync" / "templates" / "filing_types" / f"{ftype}.yaml"
        if y.exists():
            try:
                days = (yaml.safe_load(y.read_text(encoding="utf-8")) or {}).get("deadline_days")
            except Exception:
                days = None
    if ent and days:
        deadline = _us(_roll(_dt.date.fromisoformat(ent) + _dt.timedelta(days=int(days))).isoformat())
    out = {
        "judge": judge, "order_signed": _us(hdr["signed"]), "order_entered": _us(ent),
        "other_decided": "Y" if orders else "N", "decided_judge": judge if orders else None,
        "pending": "Y" if pending else "N", "pending_judge": judge if pending else None,
        "deadline": deadline, "extended": "Y" if extended else "N",
        "_source": {"judge": jsrc,
                    "order": f"{units.name} header ({hdr['label']}, Control No. {hdr['control_no']})",
                    "pending": [e["date"] + " " + e["event"][:80] for e in motions],
                    "deadline": f"{ftype}.yaml deadline_days={days} from {ent} (1 Pa.C.S. § 1908 roll)" if deadline else "no deadline_days in filing-type YAML"},
    }
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--case", required=True)
    ap.add_argument("--m", required=True, help="motion dir holding PACKET/ and <stub>_DRAFT.md")
    ap.add_argument("--stub", required=True)
    ap.add_argument("--court", default="")
    ap.add_argument("--ftype", default="")
    ap.add_argument("--units", default="", help="m2/ADVERSE_POINTS.md (default: <bundle>/m2/ADVERSE_POINTS.md)")
    a = ap.parse_args(argv)
    root, m = Path(a.root).resolve(), Path(a.m).resolve()
    pdir = m / "PACKET"
    chain = m.parent / f"chain_{m.name}"
    chain.mkdir(parents=True, exist_ok=True)
    units = Path(a.units) if a.units else (m.parent.parent / "m2" / "ADVERSE_POINTS.md")

    # 1. FILING.pdf = numbered PACKET pdfs in index order (00_, 01_, …)
    pdfs = sorted(p for p in pdir.glob("[0-9][0-9]_*.pdf") if not p.name.startswith("99_"))
    if not pdfs:
        print(f"  [efile_artifact] STOP: no numbered PDFs in {pdir}"); return 1
    out_pdf = chain / f"{a.stub}_FILING.pdf"
    if out_pdf.exists():
        shutil.copy(out_pdf, str(out_pdf) + ".bak." + _dt.datetime.now().strftime("%Y%m%d_%H%M%S"))
    r = subprocess.run(["pdfunite", *map(str, pdfs), str(out_pdf)], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  [efile_artifact] STOP: pdfunite failed: {r.stderr[:200]}"); return 1
    draft = m / f"{a.stub}_DRAFT.md"
    if draft.exists():
        shutil.copy(draft, chain / draft.name)
    (chain / "README.md").write_text(f"e-file artifact dir; source = ../{m.name} (PACKET/). FILING.pdf = {' + '.join(p.name for p in pdfs)}.\n", encoding="utf-8")

    # 2. EFILE_PART3.json — all from data
    p3 = build_part3(root, a.case, units, a.ftype or None)
    (chain / "EFILE_PART3.json").write_text(json.dumps(p3, indent=1, ensure_ascii=False), encoding="utf-8")
    np = subprocess.run(["pdfinfo", str(out_pdf)], capture_output=True, text=True).stdout
    pages = (re.search(r"Pages:\s+(\d+)", np) or [None, "?"])[1]
    print(f"  [efile_artifact] {out_pdf.relative_to(root)} ({pages} pp from {len(pdfs)} PDFs) + EFILE_PART3.json")
    print(f"  [efile_artifact] part3: judge={p3['judge']!r} order={p3['order_signed']}/{p3['order_entered']} decided={p3['other_decided']} pending={p3['pending']} deadline={p3['deadline']}")
    if not p3["judge"]:
        print(f"  [efile_artifact] WARNING: judge NOT IN CASE FILE for {a.case} ({p3['_source']['judge']}); efile_run will refuse page 3")
    return 0


if __name__ == "__main__":
    sys.exit(main())
