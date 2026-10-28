# Fake PDF Detection — Pattern 21

**Date**: 2026-07-20

## Trigger
Processing PDF files in the intake root directory

## Problem
Non-PDF files saved with `.pdf` extension cause extraction failures or garbage output.

## Session Evidence
Found 2 fake PDF files in `{{ENTROPY_HOME}}/intake/`:

1. `20260511_190112_some_random_unrelated_file.pdf` (33 bytes)
   - Header: `FAKE TEST FILE for routing test\r\n`
   - Not a PDF — plain text misnamed as PDF

2. `20260511_190301_ecf 142 order {{JUDGE}}.pdf` (20 bytes)
   - Header: `FAKE {{JUDGE}} order\r\n`
   - Not a PDF — plain text misnamed as PDF

## Detection Logic
```python
with open(fpath, 'rb') as fp:
    header = fp.read(100)

is_standard_pdf = header[:5] == b'%PDF-'
is_version_pdf = header[0:1].isdigit() and header[3:4] == b'\n' and header[4:5] == b'%'
is_tiny = os.path.getsize(fpath) < 100

if not (is_standard_pdf or is_version_pdf) or is_tiny:
    # Not a real PDF — read as text sidecar
    pass
else:
    # Process as PDF via pdftotext
    pass
```

## PDF Header Formats to Detect
1. **Standard**: `b'%PDF-'` — starts with `%PDF-`
2. **Version-header (LF)**: `b'1.4\n%\n'` — version number, newline, percent sign
3. **Version-header (CRLF)**: `b'1.6\r\n%\n'` — version number, carriage return + newline, percent sign

## Key Insight
Files under 100 bytes are almost certainly NOT real PDFs. Real PDFs are typically 1KB+ minimum. This is a fast filter before attempting binary header checks.
