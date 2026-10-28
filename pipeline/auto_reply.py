"""auto_reply — gate -> draft. Given a NEW adverse filing, produce the right vehicle and save it.

This is the A-layer wire: m0_reply_gate.decide() says WHAT to file (opposition / reply / motion to
reconsider-vacate / appeal); this module produces it for the opposition/reply vehicles by feeding the
FULL adverse-document text (resolved from the downloaded/OCR'd PDF, not the one-line docket text) into
generation.argument_rebuttal.build_effective_reply_brief (the m0 point-by-point rebuttal). Motions and
appeals route to their existing filing-pipeline builders (not duplicated here).

Caption is data-driven from cases_index/case_registry.json (R2 — no hardcoded party/judge).
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import json, re, datetime
from pathlib import Path

ROOT = _eroot()  # WSL canonical (matches motion_drafter_v2); never the literal C:\ — a stray <home> dir under WSL made the old .exists() check pick the wrong root and drop docs
CASES = ROOT / "obsidian_sync" / "cases"
_REG_PATH = ROOT / "cases_index" / "case_registry.json"
_REG = json.loads(_REG_PATH.read_text(encoding="utf-8")) if _REG_PATH.exists() else {}


def caption_for(case_id: str) -> dict:
    e = _REG.get(case_id, {}) or {}
    raw = e.get("caption", "") or ""
    plaintiff = defendants = ""
    parts = re.split(r"\s+VS\.?\s+|\s+v\.?\s+", raw, maxsplit=1, flags=re.I)
    if len(parts) == 2:
        plaintiff, defendants = parts[0].strip().title(), parts[1].strip().title()
    court = e.get("case_court") or (
        "United States District Court for the Eastern District of Pennsylvania"
        if case_id.startswith("fed") else "Court of Common Pleas of Philadelphia County")
    return {"court": court, "case_no": (e.get("canonical_id") or case_id).split(":", 1)[-1], "judge": e.get("judge"),
            "plaintiff": plaintiff or "Plaintiff", "defendants": defendants or "Defendant(s)",
            "date": datetime.date.today().strftime("%B %d, %Y")}


def _ocr_with_fitz(path: Path, limit: int = 16000) -> str:
    """OCR a scanned PDF without poppler: PyMuPDF rasterizes each page, pytesseract reads it."""
    try:
        import fitz, io, pytesseract
        from PIL import Image
    except Exception:
        return ""
    out = []
    try:
        doc = fitz.open(str(path))
        for pg in doc:
            pix = pg.get_pixmap(dpi=200)
            out.append(pytesseract.image_to_string(Image.open(io.BytesIO(pix.tobytes("png")))))
            if sum(len(x) for x in out) > limit:
                break
    except Exception:
        return ""
    return "\n".join(out)[:limit]


def _doc_text(path: Path, limit: int = 16000) -> str:
    """Extract text from a PDF/docx/txt — pdfplumber first, tesseract OCR fallback for scanned PDFs
    (reuses pipeline.ocr_engine.extract_text_smart). Caches OCR output to a .ocr.txt sidecar."""
    sidecar = path.with_suffix(path.suffix + ".ocr.txt")
    if sidecar.exists():
        body = sidecar.read_text(encoding="utf-8", errors="ignore")
        if len(body.strip()) > 80:
            return body[:limit]
    try:
        from pipeline.ocr_engine import extract_text_smart
        body = extract_text_smart(path) or ""
    except Exception:
        body = ""
    # OCR fallback that does NOT need poppler: rasterize with PyMuPDF, OCR with pytesseract.
    if len(body.strip()) < 80 and path.suffix.lower() == ".pdf":
        body = _ocr_with_fitz(path, limit) or body
    if len(body.strip()) > 80 and path.suffix.lower() == ".pdf":
        try:
            sidecar.write_text(body, encoding="utf-8")   # cache so we OCR each doc once
        except Exception:
            pass
    return body[:limit]


def adverse_text_for(slug: str, dkt: str, hint: str = "", fallback: str = "") -> tuple[str, str]:
    """Resolve the FULL text of the adverse filing for this dkt. Prefers an OCR/text sidecar, then
    the PDF (pdfplumber + OCR fallback). Returns (text, source_filename). Falls back to docket text."""
    intake = CASES / slug / "intake"
    if not intake.exists():
        return fallback, "(docket text — no intake dir)"
    dkt = str(dkt)
    hint_l = (hint or "").lower()
    pdfs, texts = [], []
    for f in intake.iterdir():
        if not f.is_file():
            continue
        n = f.name.lower()
        match = (f"dkt_{dkt}_" in n or f"dkt {dkt} " in n or f"dkt {dkt}." in n
                 or re.search(rf"\bdkt\.?\s*{dkt}\b", n) or (len(hint_l) > 8 and hint_l[:20] in n))
        if not match:
            continue
        if n.endswith((".ocr.txt", ".txt")):
            texts.append(f)
        elif n.endswith(".pdf"):
            pdfs.append(f)
    for t in texts:                              # prefer extracted text
        body = t.read_text(encoding="utf-8", errors="ignore")
        if len(body.strip()) > 80:
            return body[:16000], t.name
    for p in pdfs:
        body = _doc_text(p)
        if len(body.strip()) > 80:
            return body, p.name
    return fallback, "(docket text — no document found for this dkt)"


def _motion_title(text: str) -> str:
    """Pull a clean motion title from the adverse text / docket text."""
    m = re.search(r"((?:DEFENDANT|PLAINTIFF)[^\n.]{0,80}?MOTION[^\n.]{0,80})", text or "", re.I)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip().title()
    m = re.search(r"\b(MOTION TO [A-Z][^\n.]{0,60})", text or "", re.I)
    return (re.sub(r"\s+", " ", m.group(1)).strip().title() if m else "Defendant's Motion")


def _save_draft(slug: str, vehicle: str, dkt: str, body: str) -> str:
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = CASES / slug / "drafts" / f"run_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{vehicle}_dkt_{dkt}_{ts}.md"
    path.write_text(body, encoding="utf-8")
    return str(path)


def draft_for_new_filing(case_id: str, slug: str, filing: dict, decision: dict, save: bool = True) -> dict:
    """Produce the filing for one gate decision THROUGH THE m0 ENGINE (motion_drafter_v2) — NOT a sidecar.
    opposition/reply -> refresh audit (latest filing -> scorecard) -> compose the m0 stub -> process_stub
    (dedup + anti-repeat + corpus + grounding + correct proposed order). Single drafter path; no
    build_effective_reply_brief, no run_<ts> dump."""
    vehicle = decision.get("vehicle")
    dkt = str(filing.get("dkt", "0"))
    _v = (vehicle or decision.get("action") or "").lower()
    _act = (decision.get("action") or "").lower()
    # Any gate vehicle that maps to a filing we can compose goes through m0; reference-only does not.
    _DRAFTABLE = ("opposition", "reply", "reconsider", "vacate", "strike", "summary", "msj", "statement",
                  "reasons", "default", "appeal", "appellate", "certify", "54b", "reinstate", "response")
    if _act in ("reference_only", "no_action", "none") or not any(t in _v for t in _DRAFTABLE):
        return {"dkt": dkt, "vehicle": vehicle, "draft_path": None,
                "note": f"gate action={_act!r} vehicle={_v!r}: reference-only / not a draftable filing"}
    # 1. ANALYSIS — refresh the audit so the latest filing's text -> scorecard (m0 learns the new filing).
    #    skip_if_fresh: if the audit already postdates the newest intake file it reflects this filing
    #    (a NEW filing always carries a newer intake mtime, forcing the rebuild) — kills the double-build.
    try:
        from pipeline import audit_writer
        court = "EDPA" if case_id.startswith("fed") else "PA_CP_PHILA"
        audit_writer.build_audit_data(case_id, court=court, persist=True, skip_if_fresh=True)
    except Exception as e:
        print(f"[draft_for_new_filing] audit refresh failed: {e}")
    # 2. m0 DRAFT — compose the stub for the gate's vehicle and run it through motion_drafter_v2
    from pipeline import filing_composer, motion_drafter_v2
    suf = "federal" if case_id.startswith("fed") else "state"
    v = _v
    ft = ("motion_for_statement_of_reasons" if ("statement" in v or "reasons" in v) else
          "motion_for_reconsideration" if "reconsider" in v else
          "motion_to_vacate" if "vacate" in v else
          "motion_to_certify_54b" if ("certify" in v or "54b" in v) else
          "motion_to_reinstate_appeal" if "reinstate" in v else
          f"motion_to_strike_{suf}" if "strike" in v else
          "motion_for_summary_judgment" if ("summary" in v or "msj" in v) else
          "motion_for_default_judgment" if "default" in v else
          "appellate_brief_3rd_cir" if ("appeal" in v or "appellate" in v) else
          f"opposition_{suf}")
    out_dir = ROOT / "obsidian_sync" / "cases" / slug / "drafts" / "_replies"
    out_dir.mkdir(parents=True, exist_ok=True)
    comp = filing_composer.compose_filing(case_id, ft)
    stub_md = filing_composer.render_composition_to_markdown(comp)
    stub_path = out_dir / f"reply_dkt_{dkt}_{ft}.md"
    stub_path.write_text(stub_md, encoding="utf-8")
    draft_path = str(stub_path)
    if save:
        motion_drafter_v2.process_stub(str(stub_path), dry_run=False)
        cand = stub_path.with_name(stub_path.stem + "_DRAFT.md")
        if cand.exists():
            draft_path = str(cand)
    return {"dkt": dkt, "vehicle": vehicle, "filing_type": ft, "draft_path": draft_path,
            "engine": "m0/motion_drafter_v2", "also_file": decision.get("also_file")}


def run_for_case(case_id: str, slug: str, filings: list[dict], today: str | None = None,
                 pro_se_name: str = "", us_is_party: bool = False, save: bool = True) -> list[dict]:
    """Gate the filings; if a NEW (latest, unreplied) adverse filing exists, run the FULL m0 engine
    instead of the light point-by-point rebutter. The gate (m0_reply_gate) already tracks what's been
    replied to and flags only the latest filings (commit=True => nothing re-drafted). Then:
      1. ANALYSIS — audit_writer.build_audit_data: block14_pinpointer pulls each opposing filing's
         TEXT (the latest is already in intake; no re-fetch) -> scorecard. This is how m0 learns the
         new filing's issues.
      2. m0 DRAFT — full_reanalysis (whole-case context): motion_drafter_v2 drafts from the scorecard
         with corpus + Obsidian -> normalize -> score -> select. The drafter is the output point.
    """
    from pipeline import m0_reply_gate as gate
    today = today or datetime.date.today().isoformat()
    res = gate.process_case(slug, filings, today, pro_se_name=pro_se_name,
                            us_is_party=us_is_party, commit=save)
    reply_actions = [a for a in res.get("actions", [])
                     if a.get("action") == "reply" and a.get("vehicle") in ("opposition", "reply")]
    if not reply_actions:
        return []
    # 1. ANALYSIS: refresh the audit so the latest filing's text -> scorecard (no re-fetch; intake-only).
    #    skip_if_fresh dedups the build when draft_for_new_filing (or a prior action) already refreshed it.
    try:
        from pipeline import audit_writer
        court = "EDPA" if case_id.startswith("fed") else "PA_CP_PHILA"
        audit_writer.build_audit_data(case_id, court=court, persist=True, skip_if_fresh=True)
    except Exception as e:
        print(f"[run_for_case] audit refresh failed: {e}")
    # 2. m0 DRAFT — ONLY the latest filing's response, NOT a whole-case re-draft. Compose the one
    #    response stub for this filing and run it through the m0 engine (process_stub = corpus +
    #    grounding + obsidian). The PRIMARY response to a defendant dispositive motion (Rule 12(c)
    #    judgment on the pleadings, 12(b)(6), Rule 56) is a REPLY IN OPPOSITION (opposition_federal /
    #    opposition_state). A cross-motion to strike is a SEPARATE also_file when the filing has defects.
    from pipeline import filing_composer, motion_drafter_v2
    is_fed = case_id.startswith("fed")
    out_dir = ROOT / "obsidian_sync" / "cases" / slug / "drafts" / "_replies"
    out_dir.mkdir(parents=True, exist_ok=True)
    suf = "federal" if is_fed else "state"

    def _ft(vehicle):
        """Map the gate's chosen vehicle -> an existing m0 filing-type. m0/the gate DECIDES the
        vehicle(s); this only resolves the decision to a template. Opposition is the default reply."""
        v = (vehicle or "opposition").lower()
        if "strike" in v:
            return f"motion_to_strike_{suf}"
        if "summary" in v or "msj" in v:
            return "motion_for_summary_judgment"
        if "default" in v:
            return "motion_for_default_judgment"
        if "appeal" in v or "appellate" in v:
            return "appellate_reply_brief"
        return f"opposition_{suf}"  # opposition / reply / response in opposition

    drafted = []
    for a in reply_actions:
        dkt = str(a.get("dkt", "0"))
        # m0/gate DECIDES the vehicle(s): the primary reply, PLUS any cross-motion it flagged
        # (e.g. a motion to strike when the adverse filing carries scorecard/procedural defects).
        fts = [a.get("filing_type") or _ft(a.get("vehicle"))]
        af = a.get("also_file") or a.get("cross_motion")
        if isinstance(af, dict) and af.get("vehicle"):
            fts.append(_ft(af["vehicle"]))
        elif isinstance(af, str):
            fts.append(_ft(af))
        for ft in dict.fromkeys(fts):  # de-dup, preserve order
            try:
                comp = filing_composer.compose_filing(case_id, ft)
                stub_md = filing_composer.render_composition_to_markdown(comp)
                stub_path = out_dir / f"reply_dkt_{dkt}_{ft}.md"
                stub_path.write_text(stub_md, encoding="utf-8")
                if save:
                    motion_drafter_v2.process_stub(str(stub_path), dry_run=False)
                drafted.append({"dkt": dkt, "vehicle": a.get("vehicle"), "filing_type": ft,
                                "stub": str(stub_path), "engine": "m0/motion_drafter_v2"})
            except Exception as e:
                drafted.append({"dkt": dkt, "filing_type": ft, "error": str(e)})
    return drafted


if __name__ == "__main__":
    import sys
    case_id = sys.argv[1] if len(sys.argv) > 1 else "fed:2:26-cv-00123"
    slug = sys.argv[2] if len(sys.argv) > 2 else "fed_26-cv-00123"
    dkt = sys.argv[3] if len(sys.argv) > 3 else "142"
    filing = {"dkt": dkt, "date": datetime.date.today().isoformat(),
              "text": "DEFENDANT CAPITAL ONE'S MOTION TO DISMISS", "filer": "Example Card Issuer", "has_defects": True}
    decision = {"action": "reply", "vehicle": "opposition", "also_file": {"vehicle": "motion_to_strike"}}
    r = draft_for_new_filing(case_id, slug, filing, decision)
    print(json.dumps(r, indent=2))
