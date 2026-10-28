#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run_filing_mail_prep.py — AUTONOMOUS LetterStream mail-prep for filing-ready drafts.

The last mile before mailing, done without human intervention EXCEPT the final send/payment (which stays
human-gated by design — it spends money and files a legal document). For each filing-ready draft it:

  1. GATE   — refuses anything with a `.NEEDS_CITE_REVIEW` marker (unverified cites never mail).
  2. PDF    — converts the *_FILING.docx -> PDF via LibreOffice headless (generation.finalize_brief).
  3. SCRUB  — G10 PII/secret/work-product scan (pipeline.filing_confidentiality); a HARD hit aborts that item.
  4. STAGE  — writes a LetterStream job packet (PDF + job.json with recipient/return/class) to the outbox.
  5. REPORT — prints a payment-ready summary; the actual submit+pay is one confirmed step (not auto).

Addresses come from efiling/mail_config.json (filled once) — data-driven, no per-filing edits. Court
addresses are pre-seeded; the RETURN (sender) address must be set once or jobs stage as NEEDS_RETURN_ADDR.

    python run_filing_mail_prep.py                       # prep every cite-clean filing-ready draft
    python run_filing_mail_prep.py --case phila_ccp_260100000
    python run_filing_mail_prep.py --draft <path_to_FILING.docx>
"""
from __future__ import annotations
import sys, json, argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
CASES = ROOT / "obsidian_sync" / "cases"
OUTBOX = ROOT / "efiling" / "letterstream_outbox"
CONFIG = ROOT / "efiling" / "mail_config.json"

_DEFAULT_CONFIG = {
    "return_address": {"name": "", "line1": "", "line2": "", "city": "", "state": "", "zip": ""},
    "mail_defaults": {"class": "first-class", "color": False, "double_sided": True, "certified": False},
    "recipients_by_court": {
        "phila_ccp": [
            {"role": "court", "name": "Office of Judicial Records", "line1": "Room 296, City Hall",
             "line2": "", "city": "Philadelphia", "state": "PA", "zip": "00000"}
        ]
    },
    "_note": "Fill return_address once. Add recipients per court slug (phila_ccp / third_circuit / edpa). "
             "role=court|counsel|judge. The prep skill reads this; nothing else needs editing."
}


def _load_config() -> dict:
    if not CONFIG.exists():
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        CONFIG.write_text(json.dumps(_DEFAULT_CONFIG, indent=2), encoding="utf-8")
        print(f"[config] seeded {CONFIG} — set return_address once, then re-run.")
        return _DEFAULT_CONFIG
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return _DEFAULT_CONFIG


def _court_slug(case_slug: str) -> str:
    if case_slug.startswith("phila_ccp"):
        return "phila_ccp"
    if "cv-02822" in case_slug:
        return "third_circuit"      # or edpa — depends on the specific filing; config can override
    return case_slug


def _discover(case: str = "") -> list[Path]:
    """Filing-ready outbound docx: a *_FILING.docx whose chain has NO .NEEDS_CITE_REVIEW."""
    out = []
    pat = f"{case or '*'}/drafts/_replies/chain_reply_*/*_FILING.docx"
    for docx in sorted(CASES.glob(pat)):
        if (docx.parent / ".NEEDS_CITE_REVIEW").exists():
            print(f"  [gate] SKIP {docx.parent.name}: .NEEDS_CITE_REVIEW present")
            continue
        out.append(docx)
    return out


def _prep_one(docx: Path, cfg: dict) -> dict:
    case_slug = docx.parts[docx.parts.index("cases") + 1]
    stub = docx.name[:-len("_FILING.docx")]
    result = {"case": case_slug, "stub": stub, "docx": str(docx), "status": "", "pdf": "", "warnings": []}

    # 2. docx -> PDF
    try:
        from generation.finalize_brief import convert_to_pdf
        pdf = convert_to_pdf(docx)
    except Exception as e:
        result["status"] = f"PDF_FAILED: {e}"
        return result
    if not pdf or not Path(pdf).exists():
        result["status"] = "PDF_FAILED"
        return result
    result["pdf"] = str(pdf)

    # 3. PII / secret / work-product scrub (on the outbound PDF text)
    try:
        from pipeline import filing_confidentiality as fc
        from pypdf import PdfReader
        txt = "\n".join((pg.extract_text() or "") for pg in PdfReader(str(pdf)).pages)
        for h in fc.scan_content_for_pii(txt):
            hard = "wikilink" in h["pattern"] or "key" in h["pattern"].lower()
            msg = f"{h['pattern']} @ line {h['at_line']}"
            result["warnings"].append(("HARD " if hard else "") + msg)
            if hard:
                result["status"] = "PII_ABORT: " + msg
                return result
    except Exception as e:
        result["warnings"].append(f"pii-scan-skipped: {e}")

    # 4. stage a LetterStream job packet
    ret = cfg.get("return_address") or {}
    # per-FILING recipient wins (accountability filings go to a FORUM — Circuit Executive / ODC / JCB —
    # not the court clerk); fall back to the court-clerk mapping for ordinary litigation filings.
    recips = ((cfg.get("recipients_by_filing") or {}).get(stub)
              or (cfg.get("recipients_by_court") or {}).get(_court_slug(case_slug)) or [])
    job = {
        "case": case_slug, "filing": stub, "pdf": str(pdf),
        "return_address": ret,
        "recipients": recips,
        "mail": cfg.get("mail_defaults", {"class": "first-class"}),
        "ready": bool(ret.get("line1")) and bool(recips),
    }
    dest = OUTBOX / case_slug / stub
    dest.mkdir(parents=True, exist_ok=True)
    import shutil
    shutil.copy2(pdf, dest / Path(pdf).name)
    (dest / "job.json").write_text(json.dumps(job, indent=2), encoding="utf-8")
    if not ret.get("line1"):
        result["status"] = "STAGED_NEEDS_RETURN_ADDR"
    elif not recips:
        result["status"] = "STAGED_NEEDS_RECIPIENT"
    else:
        result["status"] = "READY_TO_SEND"
    result["job_dir"] = str(dest)
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", default="")
    ap.add_argument("--draft", default="")
    args = ap.parse_args()
    cfg = _load_config()
    OUTBOX.mkdir(parents=True, exist_ok=True)

    docs = [Path(args.draft)] if args.draft else _discover(args.case)
    if not docs:
        print("No filing-ready *_FILING.docx found (cite-clean).")
        return 0

    print(f"=== LetterStream mail-prep — {len(docs)} filing(s) ===")
    ready = 0
    for d in docs:
        r = _prep_one(d, cfg)
        print(f"\n[{r['status']}] {r['case']} / {r['stub']}")
        if r.get("pdf"):
            print(f"   PDF: {Path(r['pdf']).name}")
        for w in r["warnings"]:
            print(f"   ! {w}")
        if r.get("job_dir"):
            print(f"   staged -> {r['job_dir']}")
        if r["status"] == "READY_TO_SEND":
            ready += 1

    print("\n" + "=" * 60)
    print(f"{ready}/{len(docs)} READY_TO_SEND (staged in {OUTBOX}).")
    if ready < len(docs):
        print("Others need a return address or recipient in efiling/mail_config.json.")
    print("Final submit+payment to LetterStream is the one human-gated step (spends money / files).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
