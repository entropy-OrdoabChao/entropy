---
name: legal-docx
description: Generate court-formatted .docx documents from legal analysis text. Use when the user wants a downloadable Word document for filing — motions, complaints, rebuttals, demand letters, etc.
version: 1.0.0
metadata:
  hermes:
    tags: [legal, document-generation, docx, court-filing]
    requires_toolsets: [terminal]
    grounding_status: grounded
---

# Legal Document Generator (.docx)

Convert legal analysis or drafted text into court-formatted Word documents (.docx) with proper formatting: Times New Roman 12pt, double-spaced, 1" margins, caption block, signature block, certificate of service, and verification.

## When to Use

- User says "generate a document", "make a filing", "create a motion"
- User says "save as docx", "download", "make it court-ready"
- After completing a legal analysis or draft in chat

## Actual Generation Pipeline

The Beezble legal pipeline generates .docx via **MCP tools** in `beezle_mcp_server.py`, backed by `generation/md_to_docx.py` (the block-stitcher renderer, 1128 lines).

### Primary Method: MCP Tool — `build_motion_from_blocks`

```
Tool: mcp__beezle__build_motion_from_blocks
Args: case_id, filing_type, court_id, extra_context, body_md
Returns: {"ok": bool, "out_path": str, "blocks_used": [...]}
```

This reads `obsidian_sync/templates/filing_types/<filing_type>.yaml` for block order, resolves blocks from `obsidian_sync/templates/blocks/<category>/<block>.md`, and renders a court-aware .docx via `generation/md_to_docx.py`.

Supported court_id values: `EDPA`, `3D_CIR`, `SCOTUS`, `PA_CP_PHILA`, `PA_SUPER`, `PA_COMMWLTH`, `PA_SUPREME`.

### Secondary Methods

- **Exhibit cover**: `mcp__beezle__generate_exhibit_cover(letter, title, date, case_id, court_id)` — single-page exhibit cover sheet.
- **Exhibit package**: `mcp__beezle__build_exhibit_package(...)` — combined exhibits PDF for a case.
- **Legacy renderer**: `generation/md_to_docx.py::render_md_to_docx(md_text, out_path, court_id)` — callable directly from Python (backward compatible).

## Document Format Specifications

- Font: Times New Roman 12pt
- Spacing: Double-spaced body
- Margins: 1" all sides
- First line indent: 0.32"
- Alignment: Justified body, centered caption
- Page breaks before: Certificate of Service, Verification, Affidavit, Proposed Order, Exhibits, Appendix
- Page numbers: Bottom center
- Signature block: Right-aligned, includes "Respectfully submitted" + name + address

## Filing Requirements by Document Type

| Type | Verification | Proposed Order | Cert of Service |
|------|-------------|----------------|-----------------|
| Motion | If facts | Always | Always |
| Complaint | Mandatory | — | Always |
| Rebuttal | Only new facts | — | Always |
| Demand Letter | — | — | Certified mail |

## Pitfalls

- The block-stitcher renderer (`generation/md_to_docx.py`) reads from Obsidian templates — filing types must exist in `obsidian_sync/templates/filing_types/`.
- `CASE_GROUND_TRUTH.yaml` must exist for the case slug or the tool returns an error.
- Legacy `legal_docx_formatter.py` (standalone CLI script) does NOT exist — do not attempt to invoke it.
- If python-docx is not installed: `pip install python-docx`.

## Verification

A successful generation creates a .docx file that opens in Word with correct formatting. Check file size (should be >10KB for any real document). Verify the output path returned by the MCP tool.

## Related Skills

- `beeze-filing-generator` — Higher-level filing composition workflow
- `legal-pipeline-operations` — Complete legal filing pipeline lifecycle
- `legal-docx` (this skill) — Low-level .docx rendering mechanics
