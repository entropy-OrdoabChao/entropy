# JPEG MPO Format OCR Handling (Pattern 22 — Updated)

**Date**: 2026-07-25

## Problem

iPhone stereo photos use MPO (Multi-Picture Object) format, not standard JPEG. PIL/Pillow cannot read MPO format directly — it raises `Unsupported image format/type`. tesseract can read MPO files directly but produces garbled OCR output.

## Detection

```python
from PIL import Image

img = Image.open(fpath)
if img.format == 'MPO':
    # MPO format — cannot process with PIL
    pass
```

## Fix

1. **Convert MPO to standard JPEG** using PIL:
   ```python
   img = Image.open(fpath)
   img.save('/tmp/converted.jpg', 'JPEG')
   ```

2. **Then run tesseract** on the converted file:
   ```python
   subprocess.run(['tesseract', '/tmp/converted.jpg', 'stdout', '--psm', '3'])
   ```

## WARNING: OCR Quality for MPO-Converted JPEGs (2026-07-25)

**Observed failure mode**: All 24 MPO JPEGs processed across root and newdocuments_extracted directories produced garbled/non-Latin OCR output. The content was not legible text — likely scanned images or photos where tesseract's psm 3 (automatic page segmentation) failed entirely.

**Recommended psm modes for MPO JPEGs**:
- `psm 3` (automatic) — DEFAULT, may produce garbled output for scanned images
- `psm 4` (single column) — better for columnar text
- `psm 6` (uniform block of text) — better for scanned documents
- `psm 11` (sparse text) — better for sparse layouts

**If OCR output is garbled**:
1. Try `psm 4` or `psm 6` instead of `psm 3`
2. Document: "JPEG MPO CONVERTED — OCR output garbled, retry with psm 4/6"
3. If still garbled, mark file as requiring visual inspection
4. Consider using pytesseract instead of subprocess for better error handling

**Caveat**: Even after conversion, OCR quality may be poor for complex layouts or low-resolution photos. Document: "JPEG MPO CONVERTED — OCR quality may be degraded"

## Tool Availability Check

- tesseract: `which tesseract`
- pytesseract: `python3 -c "import pytesseract"`
- PIL/Pillow: `python3 -c "from PIL import Image"`

If PIL is available, MPO conversion is possible. If not, JPEGs require manual review.
