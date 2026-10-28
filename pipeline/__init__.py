# === BEEZLE_PDFMINER_SUPPRESSION_V1 ===
# Silence harmless PDF-extraction warnings that flood logs during scans.
# These are emitted by pdfminer / pdfplumber / pypdf for malformed font
# descriptors in scanned or third-party-generated PDFs; extraction still
# succeeds.  Set to ERROR (not CRITICAL) so real failures still surface.
try:
    import logging as _beezle_logging
    for _name in ('pdfminer', 'pdfminer.pdfinterp', 'pdfminer.converter',
                  'pdfminer.pdfdocument', 'pdfminer.cmapdb',
                  'pdfminer.psparser', 'pdfminer.pdffont',
                  'pdfminer.pdfpage', 'pdfminer.layout',
                  'pdfplumber', 'pypdf', 'PyPDF2', 'fontTools'):
        _beezle_logging.getLogger(_name).setLevel(_beezle_logging.ERROR)
    del _beezle_logging
except Exception:
    pass
# === END BEEZLE_PDFMINER_SUPPRESSION_V1 ===


