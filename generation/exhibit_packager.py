"""
generation/exhibit_packager.py - Build filing package with exhibit cover pages.
Restored from April 19 working version with corrected pattern dict.
"""
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import os, sys, time
from pathlib import Path
from typing import List, Dict, Optional

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def build_filing_package(complaint_docx, exhibits, uploaded_files, output_dir=None,
                         case_id="case", plaintiff_name="Filer Babah Filer"):
    from pypdf import PdfReader, PdfWriter
    if output_dir is None:
        output_dir = str(Path(complaint_docx).parent)
    ts = time.strftime("%Y%m%d_%H%M%S")
    exhibit_file_map = _match_files_to_exhibits(exhibits, uploaded_files)
    writer = PdfWriter()
    matched = 0
    for exhibit in exhibits:
        letter = exhibit["letter"]
        desc = exhibit["description"]
        date = exhibit.get("date", "")
        source_file = exhibit_file_map.get(letter)
        cover = _create_exhibit_cover_page(letter, desc, date, case_id, plaintiff_name)
        if cover:
            try:
                for page in PdfReader(cover).pages:
                    writer.add_page(page)
            except: pass
        if source_file and os.path.exists(source_file):
            try:
                for page in PdfReader(source_file).pages:
                    writer.add_page(page)
                matched += 1
                print(f"[EXHIBIT] {letter}: {os.path.basename(source_file)}")
            except Exception as e:
                print(f"[EXHIBIT] {letter}: could not read {source_file}: {e}")
        else:
            print(f"[EXHIBIT] {letter}: NO SOURCE — cover only")
    exhibits_path = os.path.join(output_dir, f"exhibits_{case_id}_{ts}.pdf")
    if len(writer.pages) > 0:
        with open(exhibits_path, "wb") as f:
            writer.write(f)
        print(f"[EXHIBIT] Generated: {exhibits_path} ({len(writer.pages)} pages, {matched}/{len(exhibits)} files matched)")
    else:
        exhibits_path = None
    return {"status": "complete", "complaint": complaint_docx,
            "exhibits_pdf": exhibits_path, "exhibits_count": len(exhibits),
            "files_matched": matched, "total_pages": len(writer.pages) if writer.pages else 0}


def _match_files_to_exhibits(exhibits, uploaded_files):
    if not uploaded_files: return {}

    # Deduplicate: group by canonical name, keep latest by mtime
    groups = {}
    for fpath in uploaded_files:
        fname = os.path.basename(fpath).lower()
        if "_" in fname and len(fname.split("_")[0]) == 8:
            fname_clean = "_".join(fname.split("_")[1:])
        else:
            fname_clean = fname
        try:
            mtime = os.path.getmtime(fpath)
        except OSError:
            mtime = 0
        if fname_clean not in groups or mtime > groups[fname_clean][0]:
            groups[fname_clean] = (mtime, fpath)

    file_map = {k: v[1] for k, v in groups.items()}

    # CORRECTED PATTERNS — explicit prefixes for exhibit_X_*.pdf files in data/
    # plus fallback keywords. Higher score = more specific match.
    patterns = {
        "A": ["exhibit_a_foia_request", "request_for_manual_lookup", "manual lookup", "446-5514079-703"],
        "B": ["acknowledgement", "acknowledgment", "fee waiver"],
        "C": ["1st party affirmation", "affirmation letter", "first party"],
        "D": ["exhibit_d_notarized", "affirmation-declaration", "declaration form", "notarized", "08.14.2024"],
        "E": ["exhibit_e_admin_appeal", "admin_appeal", "constructive denial", "original_request", "endorsement, termination"],
        "F": ["exhibit_f_appeal_ack", "foia_acknowledgement_letter", "26-a-c-00014; 26-fi-hq-00985 m. filer 2026.02.11"],
        "G": ["foia appeal response", "appeal response 26-a-c-00014", "2026.03.06"],
        "H": ["exhibit_h_post_remand", "post_remand", "rendered_h_2026-03-09", "gmail hud"],
    }

    # Negative filters per slot — file must NOT match any of these
    negatives = {
        "A": ["1st party", "fee waiver", "appeal", "scotus_vol", "show_temp"],
        "B": ["1st party", "appeal", "scotus_vol"],
        "C": ["fee waiver", "scotus_vol"],
        "D": ["scotus_vol", "show_temp"],
        "E": ["response", "scotus_vol", "rendered"],
        "F": ["111.pdf", "scotus_vol", "appeal response"],
        "G": ["acknowledgement_letter", "scotus_vol"],
        "H": ["scotus_vol", "show_temp"],
    }

    result = {}
    used = set()
    for letter, keywords in patterns.items():
        neg = negatives.get(letter, [])
        best_match = None
        best_score = 0
        for fname_clean, fpath in file_map.items():
            if fpath in used: continue
            if any(n in fname_clean for n in neg): continue
            score = sum(1 for kw in keywords if kw in fname_clean)
            if score > best_score:
                best_score = score
                best_match = fpath
        if best_match and best_score > 0:
            result[letter] = best_match
            used.add(best_match)
    return result


def _create_exhibit_cover_page(letter, description, date, case_id, plaintiff_name,
                               court_lines=None, defendant_line=None):
    """Exhibit cover. CAPTION IS DATA (2026-09-02 fix): the old version
    hardcoded 'US DISTRICT COURT / HUD, et al.' from a legacy case and stamped
    that caption onto EVERY case's exhibits — caught in the Progressive CCP
    packet pre-filing gate. Callers pass the real caption; absent one, the
    cover carries NO court/party block (letter + description only), never a
    wrong one."""
    try:
        from reportlab.lib.pagesizes import letter as LETTER_SIZE
        from reportlab.lib.units import inch
        from reportlab.pdfgen import canvas
        tmp_dir = os.environ.get("TEMP", str(_eroot() / "output"))
        os.makedirs(tmp_dir, exist_ok=True)
        tmp = os.path.join(tmp_dir, f"exhibit_cover_{letter}.pdf")
        c = canvas.Canvas(tmp, pagesize=LETTER_SIZE)
        width, height = LETTER_SIZE
        y = height / 2 + 2 * inch
        if court_lines:
            c.setFont("Times-Roman", 12)
            for ln in court_lines:
                c.drawCentredString(width/2, y, ln)
                y -= 18
            y -= 18
            c.setFont("Times-Roman", 11)
            c.drawCentredString(width/2, y, f"{plaintiff_name}, Plaintiff")
            y -= 16
            c.drawCentredString(width/2, y, "v.")
            y -= 16
            c.drawCentredString(width/2, y, defendant_line or "Defendant(s)")
            y -= 60
        c.setFont("Times-Bold", 48)
        c.drawCentredString(width/2, y, f"EXHIBIT {letter}")
        y -= 50
        c.setFont("Times-Roman", 14)
        c.drawCentredString(width/2, y, description)
        y -= 24
        if date:
            c.setFont("Times-Italic", 12)
            c.drawCentredString(width/2, y, f"Dated: {date}")
        c.save()
        return tmp
    except Exception as e:
        print(f"[EXHIBIT] Cover page error: {e}")
        return None


def package_with_exhibits(complaint_docx, case_id, data_dir=None):
    if data_dir is None:
        data_dir = str(Path(complaint_docx).parent.parent / "data")
    data_path = Path(data_dir)
    if not data_path.exists():
        return {"status": "no_data_dir", "data_dir": str(data_path)}
    recent_pdfs = sorted(data_path.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
    # Filter junk
    exhibit_pdfs = []
    for p in recent_pdfs:
        fn = p.name.lower()
        if "scotus_vol" in fn: continue
        if "credit report" in fn: continue
        if fn == "show_temp.pdf": continue
        if fn == "111.pdf": continue
        if fn.startswith("of4w") or fn.startswith("ui1p"): continue
        if "_" in fn:
            after_hash = "_".join(fn.split("_")[1:])
            if len(after_hash) < 4: continue
        exhibit_pdfs.append(p)
    exhibits = [
        {"letter": "A", "description": "FOIA Request", "date": "January 8, 2026"},
        {"letter": "B", "description": "HUD Acknowledgment/Fee Waiver Letter", "date": "January 13, 2026"},
        {"letter": "C", "description": "HUD First-Party Affirmation Request", "date": "February 25, 2026"},
        {"letter": "D", "description": "Notarized Affirmation/Declaration Form", "date": "February 26, 2026"},
        {"letter": "E", "description": "Administrative Appeal", "date": "February 7, 2026"},
        {"letter": "F", "description": "Appeal Acknowledgment", "date": "February 11, 2026"},
        {"letter": "G", "description": "Appeal Determination (constructive denial)", "date": "March 6, 2026"},
        {"letter": "H", "description": "Post-Remand Correspondence", "date": "March 9, 2026"},
    ]
    return build_filing_package(complaint_docx, exhibits, [str(p) for p in exhibit_pdfs],
                                str(Path(complaint_docx).parent), case_id)
