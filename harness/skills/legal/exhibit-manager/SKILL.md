---
name: exhibit-manager
description: Manage exhibits for court filings — catalog uploaded evidence, OCR scanned documents, auto-label (Exhibit A, B, C...), generate exhibit index, insert inline references in drafts, and build exhibit appendix with continuous page numbering (Page X of Y). Prevents evidence from being overlooked.
version: 1.0.0
metadata:
  hermes:
    tags: [legal, exhibits, evidence, documents, OCR, filing, appendix, index]
    requires_toolsets: [terminal, file, vision]
---

# Exhibit Manager

Manages all evidence exhibits for court filings. Catalogs, OCRs, labels, indexes, and integrates exhibits into the final document with continuous page numbering so NO evidence gets lost or ignored.

## Why This Matters

Clerks and courts can and do "overlook" exhibits that aren't:
- Clearly labeled
- Listed in an index
- Referenced in the body text
- Continuously page-numbered

This skill makes every exhibit traceable and impossible to ignore.

## When to Use

- When user uploads evidence documents (PDFs, images, screenshots, emails)
- Before `legal-draft` — to know what exhibits are available
- During `legal-draft` — to insert inline exhibit references
- After `legal-draft` — to build the exhibit appendix with page numbering
- Whenever user says "add this to my exhibits" or "here's my evidence"

## Workflow

```
User uploads evidence files
    ↓
1. CATALOG — list all files, extract metadata
    ↓
2. OCR — extract text from scanned PDFs and images
    ↓
3. LABEL — assign Exhibit A, B, C... in logical order
    ↓
4. SUMMARIZE — one-line description of each exhibit
    ↓
5. MATCH — identify which exhibits support which claims
    ↓
6. REFERENCE — insert "See Exhibit X" in draft body
    ↓
7. INDEX — generate Exhibit Index page
    ↓
8. APPENDIX — compile all exhibits with continuous page numbers
    ↓
9. FINAL — merge draft + index + appendix → single .docx with "Page X of Y"
```

## Step 1: CATALOG

When user uploads files, catalog them immediately:

```bash
# List all files in the evidence directory
ls -la /path/to/evidence/

# For each file, record:
# - filename
# - file type (PDF, PNG, JPG, TIFF, email)
# - file size
# - page count (for PDFs)
# - whether text is extractable or needs OCR
```

Store catalog as JSON:

```json
{
  "exhibits": [
    {
      "label": "A",
      "original_filename": "bank_statement_jan2024.pdf",
      "file_type": "pdf",
      "pages": 3,
      "needs_ocr": true,
      "description": "",
      "extracted_text": "",
      "relevance": "",
      "start_page": 0
    }
  ]
}
```

Save to: `/tmp/exhibit_catalog.json`

## Step 2: OCR (for scanned documents)

For PDFs with no extractable text and for images:

```bash
# Extract text from scanned PDF using pymupdf
python3 -c "
import fitz
doc = fitz.open('/path/to/exhibit.pdf')
for page in doc:
    # Try text extraction first
    text = page.get_text()
    if len(text.strip()) < 50:
        # Page is likely scanned — use OCR
        # Extract as image for vision tool or tesseract
        pix = page.get_pixmap(dpi=200)
        pix.save('/tmp/exhibit_page.png')
        print('OCR needed for page', page.number)
    else:
        print(text)
"
```

For images (screenshots, photos):
- Use Hermes `vision` tool to read the image content
- Or use `tesseract` if available: `tesseract image.png output`

**Store extracted text in the catalog for each exhibit.**

## Step 3: LABEL

Assign exhibit labels in logical order based on the argument structure:

```
Exhibit A — Primary evidence (the strongest piece)
Exhibit B — Supporting document
Exhibit C — Chronological next event
...
```

**Labeling rules:**
1. Order exhibits to match the narrative flow of the filing
2. Group related exhibits (e.g., all bank statements together)
3. If user has pre-labeled exhibits, respect their labels
4. Use letters A-Z, then AA, AB, etc. for >26 exhibits

## Step 4: SUMMARIZE

Generate a one-line description for each exhibit:

```
Exhibit A — Vehicle bill of sale dated January 15, 2024
Exhibit B — Bank statement showing continued insurance debits, February-August 2024
Exhibit C — Email to insurer requesting cancellation, February 1, 2024
Exhibit D — Insurer's response refusing cancellation, February 15, 2024
Exhibit E — Collection notice from ABC Collections, September 2024
```

## Step 5: MATCH TO CLAIMS

For each element in the legal analysis, identify which exhibit supports it:

```
Element: Vehicle sold (no ownership)
  → Supported by: Exhibit A (bill of sale)

Element: Insurer continued billing
  → Supported by: Exhibit B (bank statements)

Element: Insurer was notified
  → Supported by: Exhibit C (cancellation email)

Element: Collection attempt
  → Supported by: Exhibit E (collection notice)
```

## Step 6: INLINE REFERENCES

When `legal-draft` writes the Application to Facts section, insert exhibit references:

```
Plaintiff sold the insured vehicle on January 15, 2024.
(See Exhibit A, Bill of Sale, attached hereto at Page 8 of 26.)

Despite notice of the sale, Defendant continued to debit Plaintiff's
bank account for insurance premiums from February through August 2024.
(See Exhibit B, Bank Statements, attached hereto at Pages 9-14 of 26.)
```

**Format for inline references:**
```
(See Exhibit [LETTER], [Description], attached hereto at Page [X] of [Y].)
```

**Rules:**
- EVERY factual claim in the filing must reference its supporting exhibit
- If a claim has no exhibit → flag it: "NO EXHIBIT — factual assertion unsupported by record"
- Page numbers are continuous across the entire filing (body + appendix)

## Step 7: EXHIBIT INDEX PAGE

Generate an Exhibit Index page that goes BETWEEN the body text and the exhibits:

```
                        EXHIBIT INDEX

Exhibit   Description                              Page
────────────────────────────────────────────────────────
  A       Vehicle Bill of Sale (Jan 15, 2024)        8
  B       Bank Statements (Feb-Aug 2024)             9
  C       Email — Cancellation Request (Feb 1)       15
  D       Insurer Response (Feb 15, 2024)            16
  E       Collection Notice (Sep 2024)               18
  F       Proof of Mailing (Certified Mail)          20

                    Total Exhibits: 6
                    Total Pages: 26
```

## Step 8: EXHIBIT APPENDIX

Compile all exhibits into the appendix:

```
Each exhibit page gets:
  - Header: "Exhibit [LETTER] — [Description]"
  - Footer: "Page [X] of [Y]"
  - Exhibit separator page (optional, for clarity)
```

**For scanned PDFs and images:**
- Convert each page to image (PNG at 200 DPI)
- Insert as full-page images in the .docx
- Maintain original aspect ratio
- Add exhibit label header above each image

**For text-based documents:**
- Insert the text content with proper formatting
- Add exhibit label header

## Step 9: FINAL ASSEMBLY

The complete filing structure:

```
Page 1:     Caption / Cover Page
Page 2-N:   Body of Filing (motion, complaint, etc.)
            - Contains inline exhibit references
Page N+1:   Exhibit Index
Page N+2:   Exhibit A (pages as needed)
Page N+X:   Exhibit B (pages as needed)
...
Page Y:     Last page of last exhibit

Footer on EVERY page: "Page X of Y"
```

## Script: exhibit_compiler.py

```bash
python3 ~/.hermes/skills/legal/exhibit-manager/scripts/exhibit_compiler.py \
  --evidence-dir /path/to/evidence/ \
  --draft /tmp/draft.docx \
  --output /path/to/final_filing.docx \
  --catalog /tmp/exhibit_catalog.json
```

## Integration with Pipeline

```
Steps 1-11.6 (full analysis + adversarial testing)
    ↓
exhibit-manager: catalog + OCR + label + match     ← runs parallel to analysis
    ↓
baseline-memorandum (Step 12)
    ↓
legal-draft (Step 13) — inline exhibit references inserted here
    ↓
exhibit-manager: build index + appendix + page numbers  ← after draft
    ↓
legal-docx (Step 14) — final assembly with continuous pagination
    ↓
appellate-violation-tracker (Step 15)
```

## OpenWebUI Integration

When user uploads files in OpenWebUI:
1. Files are saved to `/mnt/user-data/uploads/`
2. Exhibit manager catalogs them automatically
3. When drafting begins, exhibits are available for inline reference
4. Final .docx includes everything with continuous page numbering

## Commands

```bash
# Catalog exhibits
python3 ~/.hermes/skills/legal/exhibit-manager/scripts/exhibit_compiler.py --catalog --evidence-dir /path/

# Generate exhibit index
python3 ~/.hermes/skills/legal/exhibit-manager/scripts/exhibit_compiler.py --index --catalog /tmp/exhibit_catalog.json

# Compile full filing with exhibits
python3 ~/.hermes/skills/legal/exhibit-manager/scripts/exhibit_compiler.py \
  --compile \
  --draft /tmp/draft.docx \
  --evidence-dir /path/ \
  --output /tmp/final_filing.docx
```

## Anti-Hallucination

- EVERY exhibit must come from user-provided files — never generate fake evidence
- OCR text is best-effort — if unreadable, note "Exhibit [X] — text not extractable, image attached"
- If a claim references an exhibit that doesn't exist → flag immediately
- Exhibit descriptions must match actual content, not assumed content
- Page numbers must be verified against actual compiled document

## EXHIBIT SOURCES (MANDATORY CHECK ALL)
When building exhibits, search ALL these locations:
1. Case intake folder: obsidian_sync/cases/<slug>/intake/*.pdf
2. User uploads in current session
3. Case attachments: obsidian_sync/cases/<slug>/attachments/
4. Unmatched emails with attachments
5. ChromaDB chunks tagged as evidence

For appellate replies: if opponent claims something wasn't done, search intake/ for the proof. The intake folder contains original filings, receipts, and correspondence that may rebut dismissal claims.
