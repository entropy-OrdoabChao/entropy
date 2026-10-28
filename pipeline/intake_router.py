"""intake_router — triage the root intake/ DROP ZONE into the correct case's intake/ folder.

The person drops documents into <ENTROPY_HOME>/drop. This router matches each drop to a registered
case by its docket identifiers (data-driven from cases_index/case_registry.json — R2) and files it
into vault/cases/<slug>/intake/ (the folder the analysis reads). Anything that matches no
case is left in place and flagged as a potential NEW claim (for the new-claim engine / review).

Matching priority (high -> low confidence):
  1. a case docket identifier (docket_aliases, state_origin_docket, federal_cross_ref, canonical id)
     appears in the filename, or
  2. same, found in the document text (OCR-backed), or
  3. (weak, review-only) a distinctive defendant token from the caption.
Docket-number matches are exact-ish (normalized substring); party matches are flagged review_only
because the four cases share the same plaintiff.

DRY-RUN by default. Pass --apply to actually move files.

Usage:
"""
from __future__ import annotations
import json, re, sys, shutil
from pathlib import Path

from pipeline import paths as _P
ROOT = _P.HOME
INTAKE = _P.DROP
CASES = _P.CASES
_REG = json.loads(_P.REGISTRY.read_text(encoding="utf-8")) if _P.REGISTRY.exists() else {}

APPLY = "--apply" in sys.argv


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _rebase(fp) -> "Path | None":
    """Resolve a stored folder_path; relative paths are under the Entropy home."""
    if not fp:
        return None
    s = str(fp).replace("\\", "/")
    p = Path(fp)
    return p if p.is_absolute() else (ROOT / fp)


# distinctive defendant tokens per case (weak fallback; the plaintiff is shared across cases so excluded)
def _defendant_tokens(caption: str) -> list[str]:
    tail = re.split(r"\s+vs\.?\s+|\s+v\.\s+", caption or "", maxsplit=1, flags=re.I)
    if len(tail) < 2:
        return []
    out = []
    for w in re.findall(r"[A-Za-z]{4,}", tail[1]):
        wl = w.lower()
        if wl not in ("information", "services", "corporation", "mortgage", "etal", "llc", "inc", "company"):
            out.append(wl)
    return out[:3]


def build_index():
    """identifier (normalized) -> (slug, confidence). Docket ids = exact; party tokens = review."""
    docket_idx, party_idx, slug_dir = {}, {}, {}
    for cid, e in _REG.items():
        if not isinstance(e, dict):
            continue
        slug = e.get("slug") or ("fed_" + cid.split(":")[-1] if cid.startswith("fed") else cid.replace(":", "_"))
        slug_dir[slug] = _rebase(e.get("folder_path")) or (CASES / slug)
        ids = set(e.get("docket_aliases") or [])
        ids.add(cid.split(":")[-1])
        for k in ("state_origin_docket", "federal_cross_ref"):
            if e.get(k):
                ids.add(str(e[k]))
        for raw in ids:
            n = _norm(raw)
            if len(n) >= 7:                       # specific enough to avoid false hits
                docket_idx[n] = slug
        for tok in _defendant_tokens(e.get("caption", "")):
            party_idx.setdefault(tok, slug)
    return docket_idx, party_idx, slug_dir


def _text_of(p: Path) -> str:
    ext = p.suffix.lower()
    try:
        if ext in (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"):
            # photographed document -> OCR the image so a docket number on it can route the file
            try:
                import pytesseract
                from PIL import Image
                return pytesseract.image_to_string(Image.open(p))[:4000]
            except Exception:
                return ""
        from pipeline.ocr_intake import EXTRACTORS
        fn = EXTRACTORS.get(ext)
        return fn(p)[0][:4000] if fn else ""
    except Exception:
        return ""


def match_file(p: Path, docket_idx, party_idx) -> tuple[str | None, str]:
    fname_n = _norm(p.name)
    # 1) docket id in filename (longest id first = most specific)
    for ident in sorted(docket_idx, key=len, reverse=True):
        if ident in fname_n:
            return docket_idx[ident], f"docket id '{ident}' in filename"
    # 2) docket id in document text
    text_n = _norm(_text_of(p))
    for ident in sorted(docket_idx, key=len, reverse=True):
        if ident in text_n:
            return docket_idx[ident], f"docket id '{ident}' in document text"
    # 3) distinctive defendant token (weak — review only)
    blob = (p.name + " " + _text_of(p)).lower()
    for tok, slug in party_idx.items():
        if re.search(rf"\b{re.escape(tok)}\b", blob):
            return slug, f"REVIEW: party token '{tok}' (no docket id found)"
    return None, "no case match -> potential NEW claim"


def _stage_new_claims(candidates: list[tuple[Path, str]]):
    """Cross-reference: an unrouted drop is a NEW-claim candidate. Write a manifest the new-claim
    engine (universal_intake Track B / m0_new_claim_engine) consumes. Non-destructive — files stay
    in intake/ for review; we only record them as candidates."""
    if not candidates:
        return None
    stage_dir = ROOT / "output" / "new_claims_intake"
    stage_dir.mkdir(parents=True, exist_ok=True)
    manifest = stage_dir / "candidates.json"
    rows = [{"file": str(p), "name": p.name, "snippet": _text_of(p)[:300]} for p, _ in candidates]
    manifest.write_text(json.dumps({"candidates": rows}, indent=2, default=str), encoding="utf-8")
    return manifest


def _validate_file(f: Path) -> list[str]:
    """Validate an intake file for quality. Returns a list of defect warnings.
    
    Checks:
    - File size < 1KB: likely fake/corrupted/garbage (e.g. the 23-byte "FAKE state farm reply" file)
    - PDF header check for .pdf files: must start with %PDF- to be a real PDF
    """
    defects = []
    try:
        size = f.stat().st_size
        if size < 1024:  # 1KB threshold
            defects.append(f"TINY_FILE: {size:,} bytes (likely fake/corrupted/garbage)")
        
        if f.suffix.lower() == ".pdf":
            # Check PDF header — real PDFs start with %PDF-
            try:
                with open(f, "rb") as fh:
                    header = fh.read(8)
                if not header.startswith(b"%PDF-"):
                    # Try to read as text to show what it actually contains
                    try:
                        text_preview = header.decode("utf-8", errors="replace").strip()[:50]
                        defects.append(f"INVALID_PDF_HEADER: not a real PDF (starts with: {repr(text_preview)})")
                    except Exception:
                        defects.append(f"INVALID_PDF_HEADER: not a real PDF (binary garbage)")
            except Exception:
                defects.append("INVALID_PDF_HEADER: could not read file header")
    except Exception as e:
        defects.append(f"VALIDATION_ERROR: {e}")
    return defects


def reconcile_placeholder_slugs(apply: bool = False) -> int:
    """V5 self-heal: an OCR/email namer that could not parse a case number leaves a placeholder case dir
    (e.g. `phila_ccp_?`). The number IS recoverable from the document's OCR text ("No. 260100000" on a CCP
    order). This sweep reads each placeholder file's `.doctext.txt` (or the file itself), recovers the real
    docket number, and re-homes the file into the matching REGISTERED case dir — data-driven (R2), no
    hardcoded case. Then removes the emptied placeholder dir. Idempotent. Returns files moved."""
    if not CASES.exists():
        return 0
    moved = 0
    for pdir in sorted(CASES.glob("*_?")):        # placeholder dirs: slug ends with an unresolved "?"
        prefix = pdir.name[:-1]                    # e.g. "phila_ccp_"  (drop the trailing "?")
        idir = pdir / "intake"
        files = [f for f in sorted(idir.glob("*"))
                 if f.is_file() and not f.name.endswith(".doctext.txt")] if idir.exists() else []
        if not files:
            # already-drained placeholder: nothing canonical left, so retire it (ignore _ocr byproducts)
            if apply and not [p for p in pdir.rglob("*") if p.is_file() and "_ocr" not in p.parts]:
                shutil.rmtree(pdir); print(f"  [reconcile/APPLY] removed drained placeholder {pdir.name}")
            continue
        # Resolve ONE registered target for the whole dir from the strongest signal: a court-caption
        # "No. <digits>" that maps to a registered case. A placeholder dir is one filing event, so all its
        # files share the matter — never guess from loose digit runs (those grab e-file/control numbers).
        nums = {}
        for f in files:
            dt = f.with_suffix(f.suffix + ".doctext.txt")
            text = dt.read_text(encoding="utf-8", errors="ignore") if dt.exists() else ""
            for mm in re.finditer(r"\bNo\.\s*([0-9]{6,})", text):
                cand = mm.group(1)
                if (CASES / f"{prefix}{cand}").exists():   # only count REGISTERED case dirs
                    nums[cand] = nums.get(cand, 0) + 1
        if len(nums) != 1:
            print(f"  [reconcile] {pdir.name}: {'ambiguous' if nums else 'no'} registered case number in "
                  f"{len(files)} file(s) ({sorted(nums) or 'none'}); left in place")
            continue
        num = next(iter(nums))
        target = CASES / f"{prefix}{num}"
        for f in files:
            dt = f.with_suffix(f.suffix + ".doctext.txt")
            new_name = f.name.replace("_?_", f"_{num}_").replace("?", num)
            dest = target / "intake" / new_name
            print(f"  [reconcile{'/APPLY' if apply else ''}] {f.name[:44]:44} -> {target.name}/intake/{new_name}")
            if apply:
                (target / "intake").mkdir(parents=True, exist_ok=True)
                if not dest.exists():
                    shutil.move(str(f), str(dest)); moved += 1
                if dt.exists():
                    ddest = dest.with_suffix(dest.suffix + ".doctext.txt")
                    if not ddest.exists():
                        shutil.move(str(dt), str(ddest))
        # drop the placeholder dir once it holds nothing but empty intake/_ocr scaffolding
        if apply:
            try:
                # _ocr/ holds regenerable OCR byproducts (rebuilt when OCR runs on the moved originals) —
                # not canonical, so it doesn't keep the placeholder alive.
                leftover = [p for p in pdir.rglob("*") if p.is_file() and "_ocr" not in p.parts]
                if not leftover:
                    shutil.rmtree(pdir); print(f"  [reconcile/APPLY] removed empty placeholder {pdir.name}")
            except Exception as e:
                print(f"  [reconcile] could not remove {pdir.name}: {e}")
    return moved


def route(apply: bool = False):
    reconcile_placeholder_slugs(apply)   # V5: heal any prior unresolved-case-number placeholder dirs first
    if not INTAKE.exists():
        print(f"no intake dir at {INTAKE}")
        return
    docket_idx, party_idx, slug_dir = build_index()
    files = [f for f in INTAKE.iterdir() if f.is_file() and not f.name.startswith('.') and f.name.lower() not in ('readme.txt', 'desktop.ini', 'thumbs.db')]
    matched = unmatched = review = 0
    new_claim_candidates: list[tuple[Path, str]] = []
    quality_defects: list[tuple[Path, list[str]]] = []
    print(f"=== intake drop-zone routing ({'APPLY' if apply else 'DRY-RUN'}) — {len(files)} files ===")
    for f in sorted(files):
        # Validate file quality before routing
        file_defects = _validate_file(f)
        if file_defects:
            quality_defects.append((f, file_defects))
            defect_str = "; ".join(file_defects)
            print(f"  [DEFECT] {f.name[:50]:50} — {defect_str}")
        slug, why = match_file(f, docket_idx, party_idx)
        if slug is None:
            unmatched += 1
            new_claim_candidates.append((f, why))
            print(f"  [NEW?]   {f.name[:60]:60}  {why}")
            continue
        is_review = why.startswith("REVIEW")
        review += is_review
        matched += not is_review
        dest_dir = slug_dir.get(slug, CASES / slug) / "intake"
        dest = dest_dir / f.name
        tag = "would route" if not apply else "ROUTED"
        if apply and not is_review:                 # only auto-move confident docket matches
            dest_dir.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                print(f"  [skip]   {f.name[:50]:50} -> {slug} (already there)"); continue
            shutil.move(str(f), str(dest))
        print(f"  [{tag}] {f.name[:50]:50} -> {slug}  ({why})")
    manifest = _stage_new_claims(new_claim_candidates)
    print(f"\nmatched(docket)={matched}  review(party)={review}  unmatched(new?)={unmatched}")
    if manifest:
        print(f"cross-reference: {unmatched} unrouted drop(s) staged as NEW-claim candidates -> {manifest}")
    if quality_defects:
        print(f"\nQUALITY DEFECTS: {len(quality_defects)} file(s) with issues:")
        for f, defects in quality_defects:
            print(f"  - {f.name}: {'; '.join(defects)}")
    if not apply:
        print("DRY-RUN — re-run with --apply to move confident docket matches (party/REVIEW are never auto-moved).")


if __name__ == "__main__":
    route(apply=APPLY)
