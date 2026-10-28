# Pattern 23: Miscategorized Intake Files

**Trigger**: Processing intake files where the filename suggests one case but the content belongs to another

**Signature**: File filename contains a case identifier (e.g., "HUD_FOIA") but the extracted text reveals a different case number (e.g., "2:26-cv-03268")

**Root Cause**: Files were dragged from a different case's intake folder or misnamed during manual upload

**Impact**: False case attribution — intake analysis attributes findings to the wrong case

**FIX**:
1. Cross-reference case numbers after extraction
2. If mismatch found → document: "MISCATEGORIZED — filename says X but content is from case Y"
3. Move file to correct case intake folder, rename it, or flag for manual review
4. Check for ECF headers (repeating "Case X / Document Y / Filed Z / Page W of V") — these are metadata-only

**Document**: "MISCATEGORIZED - [filename] filename suggests case X but content is from case Y"

**PATTERN STATUS**: PROVEN WORKFLOW — Mandatory for all intake file processing

**REFERENCE**: `references/miscategorized-intake-files-20260727.md`
