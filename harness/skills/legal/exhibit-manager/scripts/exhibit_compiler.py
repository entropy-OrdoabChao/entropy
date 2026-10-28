#!/usr/bin/env python3
"""
exhibit_compiler.py — Catalog, label, and compile exhibits for court filings.

Usage:
  exhibit_compiler.py --catalog --evidence-dir /path/to/evidence/
  exhibit_compiler.py --index --catalog /tmp/exhibit_catalog.json
  exhibit_compiler.py --compile --draft draft.docx --evidence-dir /path/ --output final.docx
  exhibit_compiler.py --compile --draft draft.docx --catalog catalog.json --output final.docx
"""

import argparse, json, os, sys
from pathlib import Path
from datetime import datetime

def catalog_evidence(evidence_dir):
    """Scan directory and catalog all evidence files."""
    evidence_dir = Path(evidence_dir)
    if not evidence_dir.exists():
        print(f"ERROR: Directory not found: {evidence_dir}")
        return None

    exhibits = []
    label_idx = 0
    supported_ext = {'.pdf', '.png', '.jpg', '.jpeg', '.tiff', '.tif', '.bmp', '.gif', '.webp', '.eml', '.msg', '.txt', '.html'}

    for f in sorted(evidence_dir.iterdir()):
        if f.is_dir() or f.suffix.lower() not in supported_ext:
            continue

        label = _idx_to_label(label_idx)
        pages = 1
        needs_ocr = False
        extracted_text = ""

        if f.suffix.lower() == '.pdf':
            pages, needs_ocr, extracted_text = _analyze_pdf(f)
        elif f.suffix.lower() in {'.png', '.jpg', '.jpeg', '.tiff', '.tif', '.bmp', '.gif', '.webp'}:
            needs_ocr = True
            extracted_text = _ocr_image(f)
        elif f.suffix.lower() in {'.txt', '.html', '.eml'}:
            try:
                extracted_text = f.read_text(encoding='utf-8', errors='replace')[:5000]
            except:
                extracted_text = ""

        exhibit = {
            "label": label,
            "original_filename": f.name,
            "file_path": str(f.absolute()),
            "file_type": f.suffix.lower().lstrip('.'),
            "file_size": f.stat().st_size,
            "pages": pages,
            "needs_ocr": needs_ocr,
            "description": "",
            "extracted_text": extracted_text[:3000],
            "relevance": "",
            "start_page": 0
        }
        exhibits.append(exhibit)
        label_idx += 1

        print(f"  Exhibit {label}: {f.name} ({pages} page(s), {'OCR needed' if needs_ocr else 'text OK'})")

    catalog = {
        "created": datetime.now().isoformat(),
        "evidence_dir": str(evidence_dir),
        "exhibit_count": len(exhibits),
        "exhibits": exhibits
    }

    outfile = Path("/tmp/exhibit_catalog.json")
    with open(outfile, "w") as fp:
        json.dump(catalog, fp, indent=2)

    print(f"\nCatalog saved: {outfile}")
    print(f"Total exhibits: {len(exhibits)}")
    return catalog


def _idx_to_label(idx):
    """Convert index to letter label: 0=A, 25=Z, 26=AA, 27=AB..."""
    if idx < 26:
        return chr(65 + idx)
    first = chr(65 + (idx // 26) - 1)
    second = chr(65 + (idx % 26))
    return first + second


def _analyze_pdf(filepath):
    """Analyze PDF: count pages, check if OCR needed, extract text."""
    pages = 1
    needs_ocr = False
    text = ""

    try:
        import fitz
        doc = fitz.open(str(filepath))
        pages = len(doc)
        text_parts = []
        ocr_pages = 0

        for page in doc:
            t = page.get_text()
            if len(t.strip()) < 50:
                ocr_pages += 1
            text_parts.append(t)

        text = "\n".join(text_parts)
        needs_ocr = ocr_pages > (pages / 2)
        doc.close()
    except Exception as e:
        print(f"    PDF analysis error: {e}")

    return pages, needs_ocr, text[:3000]


def _ocr_image(filepath):
    """Try to OCR an image file."""
    try:
        import subprocess
        result = subprocess.run(
            ["tesseract", str(filepath), "stdout"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            return result.stdout[:3000]
    except:
        pass
    return "[Image — OCR not available. Visual content only.]"


def generate_index(catalog_path):
    """Generate exhibit index from catalog."""
    with open(catalog_path) as f:
        catalog = json.load(f)

    print("\n" + "=" * 60)
    print("                    EXHIBIT INDEX")
    print("=" * 60)
    print(f"{'Exhibit':<10} {'Description':<40} {'Pages':<6}")
    print("-" * 60)

    for ex in catalog["exhibits"]:
        desc = ex.get("description") or ex["original_filename"][:38]
        print(f"  {ex['label']:<8} {desc:<40} {ex['pages']:<6}")

    total_pages = sum(ex["pages"] for ex in catalog["exhibits"])
    print("-" * 60)
    print(f"  Total Exhibits: {catalog['exhibit_count']}")
    print(f"  Total Exhibit Pages: {total_pages}")
    print("=" * 60)


def compile_filing(draft_path, evidence_dir=None, catalog_path=None, output_path=None):
    """Compile draft + exhibits into single .docx with continuous page numbering."""
    try:
        from docx import Document
        from docx.shared import Inches, Pt, Cm
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.enum.section import WD_ORIENT
    except ImportError:
        print("ERROR: python-docx required. Install: pip install python-docx")
        return

    # Load catalog
    if catalog_path:
        with open(catalog_path) as f:
            catalog = json.load(f)
    elif evidence_dir:
        catalog = catalog_evidence(evidence_dir)
    else:
        print("ERROR: Provide --evidence-dir or --catalog")
        return

    if not catalog:
        return

    # Load draft document
    if draft_path and Path(draft_path).exists():
        doc = Document(draft_path)
    else:
        doc = Document()
        doc.add_paragraph("[Draft body would go here]")

    # Add exhibit index page
    doc.add_page_break()
    idx_title = doc.add_paragraph("EXHIBIT INDEX")
    idx_title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for run in idx_title.runs:
        run.bold = True
        run.font.size = Pt(14)

    doc.add_paragraph("")

    # Build index table
    table = doc.add_table(rows=1, cols=3)
    table.style = 'Table Grid'
    hdr = table.rows[0].cells
    hdr[0].text = "Exhibit"
    hdr[1].text = "Description"
    hdr[2].text = "Pages"
    for cell in hdr:
        for p in cell.paragraphs:
            for run in p.runs:
                run.bold = True

    for ex in catalog["exhibits"]:
        row = table.add_row().cells
        row[0].text = ex["label"]
        row[1].text = ex.get("description") or ex["original_filename"]
        row[2].text = str(ex["pages"])

    # Add each exhibit
    for ex in catalog["exhibits"]:
        doc.add_page_break()

        # Exhibit header
        header_text = f"EXHIBIT {ex['label']}"
        h = doc.add_paragraph(header_text)
        h.alignment = WD_ALIGN_PARAGRAPH.CENTER
        for run in h.runs:
            run.bold = True
            run.font.size = Pt(16)

        desc = ex.get("description") or ex["original_filename"]
        d = doc.add_paragraph(desc)
        d.alignment = WD_ALIGN_PARAGRAPH.CENTER

        doc.add_paragraph("")

        filepath = Path(ex["file_path"])

        if filepath.suffix.lower() == '.pdf':
            _insert_pdf_pages(doc, filepath)
        elif filepath.suffix.lower() in {'.png', '.jpg', '.jpeg', '.tiff', '.tif', '.bmp', '.gif', '.webp'}:
            _insert_image(doc, filepath)
        elif filepath.suffix.lower() in {'.txt', '.html', '.eml'}:
            try:
                text = filepath.read_text(encoding='utf-8', errors='replace')
                doc.add_paragraph(text[:10000])
            except:
                doc.add_paragraph("[Text extraction failed]")
        else:
            doc.add_paragraph(f"[File: {ex['original_filename']} — attached separately]")

    # Save
    if not output_path:
        output_path = "/tmp/filing_with_exhibits.docx"

    doc.save(output_path)
    print(f"\nFiled saved: {output_path}")
    print(f"Exhibits included: {catalog['exhibit_count']}")

    # Add page numbers (requires post-processing with docx XML)
    _add_page_numbers(output_path)

    return output_path


def _insert_pdf_pages(doc, filepath):
    """Convert PDF pages to images and insert into docx."""
    try:
        import fitz
        pdf = fitz.open(str(filepath))

        for i, page in enumerate(pdf):
            pix = page.get_pixmap(dpi=150)
            img_path = f"/tmp/exhibit_page_{filepath.stem}_{i}.png"
            pix.save(img_path)

            try:
                from docx.shared import Inches
                doc.add_picture(img_path, width=Inches(6.5))
            except Exception as e:
                doc.add_paragraph(f"[Page {i+1} — image insertion failed: {e}]")

            if i < len(pdf) - 1:
                doc.add_page_break()

        pdf.close()
    except ImportError:
        doc.add_paragraph("[PDF rendering requires pymupdf. Install: pip install pymupdf]")
    except Exception as e:
        doc.add_paragraph(f"[PDF rendering failed: {e}]")


def _insert_image(doc, filepath):
    """Insert image into docx, scaled to fit page."""
    try:
        from docx.shared import Inches
        doc.add_picture(str(filepath), width=Inches(6.5))
    except Exception as e:
        doc.add_paragraph(f"[Image insertion failed: {e}]")


def _add_page_numbers(docx_path):
    """Add 'Page X of Y' footer to all pages via XML manipulation."""
    try:
        from docx import Document
        from docx.oxml.ns import qn
        from lxml import etree

        doc = Document(docx_path)

        for section in doc.sections:
            footer = section.footer
            footer.is_linked_to_previous = False

            # Clear existing footer
            for p in footer.paragraphs:
                p.clear()

            p = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
            p.alignment = 1  # center

            # Add "Page " text
            run1 = p.add_run("Page ")
            run1.font.size = Pt(10)

            # Add PAGE field
            fld_page = etree.SubElement(run1._element, qn('w:fldChar'))
            fld_page.set(qn('w:fldCharType'), 'begin')
            run2 = p.add_run()
            fld_instr = etree.SubElement(run2._element, qn('w:instrText'))
            fld_instr.set(qn('xml:space'), 'preserve')
            fld_instr.text = ' PAGE '
            run3 = p.add_run()
            fld_end = etree.SubElement(run3._element, qn('w:fldChar'))
            fld_end.set(qn('w:fldCharType'), 'end')

            # Add " of " text
            run4 = p.add_run(" of ")
            run4.font.size = Pt(10)

            # Add NUMPAGES field
            run5 = p.add_run()
            fld_page2 = etree.SubElement(run5._element, qn('w:fldChar'))
            fld_page2.set(qn('w:fldCharType'), 'begin')
            run6 = p.add_run()
            fld_instr2 = etree.SubElement(run6._element, qn('w:instrText'))
            fld_instr2.set(qn('xml:space'), 'preserve')
            fld_instr2.text = ' NUMPAGES '
            run7 = p.add_run()
            fld_end2 = etree.SubElement(run7._element, qn('w:fldChar'))
            fld_end2.set(qn('w:fldCharType'), 'end')

        doc.save(docx_path)
        print(f"Page numbers added: 'Page X of Y' on all pages")

    except Exception as e:
        print(f"Warning: Could not add page numbers: {e}")
        print("Open the document in Word and add page numbers manually: Insert → Page Number → Bottom → Page X of Y")


def main():
    parser = argparse.ArgumentParser(description="Exhibit Compiler for Court Filings")
    parser.add_argument("--catalog", action="store_true", help="Catalog evidence files")
    parser.add_argument("--index", action="store_true", help="Generate exhibit index")
    parser.add_argument("--compile", action="store_true", help="Compile draft + exhibits into single .docx")
    parser.add_argument("--evidence-dir", default="", help="Directory containing evidence files")
    parser.add_argument("--draft", default="", help="Path to draft .docx")
    parser.add_argument("--output", default="/tmp/filing_with_exhibits.docx", help="Output path")
    parser.add_argument("--catalog-file", default="/tmp/exhibit_catalog.json", help="Path to catalog JSON")

    args = parser.parse_args()

    if args.catalog:
        if not args.evidence_dir:
            print("ERROR: --evidence-dir required for catalog")
            sys.exit(1)
        catalog_evidence(args.evidence_dir)

    elif args.index:
        generate_index(args.catalog_file)

    elif args.compile:
        compile_filing(
            draft_path=args.draft,
            evidence_dir=args.evidence_dir if args.evidence_dir else None,
            catalog_path=args.catalog_file if Path(args.catalog_file).exists() else None,
            output_path=args.output
        )

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
