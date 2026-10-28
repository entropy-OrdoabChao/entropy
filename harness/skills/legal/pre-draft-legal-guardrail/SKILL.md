---
name: pre-draft-legal-guardrail
description: Pre-draft legal guardrails for FCRA claims, 60(b)(4) escalation, and INTAKE PROCESSING. Requires session log generation and read_obsidian_note (CASE_FACTS.md) before drafting or analysis.
version: 3.12
metadata:
  hermes:
    tags: [legal, fcra, 60b4, guardrail, drafting, intake-processing]
    category: legal
    requires_toolsets: [file]
    linked_files:
       references:
         - references/cron_job_intake_workflow.md
         - references/cron_job_intake_execution_2026-05-20.md
         - references/intake-processing-patterns.md
         - references/intake_complete_skip_logic.md
         - references/intake_processing_run_2026-05-22.md
         - references/metadata_only_analysis.md
         - references/sidecar_first_extraction.md
         - references/scheduled_cron_job_execution.md
         - references/terminal_approval_blocking.md
         - references/gate-enforcement-checklist.md
         - references/empty_ocr_sidecar_validation.md
         - references/filing_queue_health_check.md
         - references/email-processing-workflow-2026-05-23.md
         - references/cron_intake_processing_2026-06-21.md
         - references/pdf-binary-ocr-txt-detection.md
         - references/intake-file-count-staleness-20260626.md
         - references/pre-draft-legal-guardrail-duplicate-sections-20260626.md
         - references/ecf-activity-in-case-metadata-only-20260629.md
         - references/brother-scanner-pdf-no-text-layer.md
         - references/fake-pdf-detection.md
         - references/jpeg-mpo-ocr-handling.md
         - references/mpo-jpeg-ocr-garbled-output-20260725.md
         - references/miscategorized-intake-files-20260727.md
         - references/pattern-23-miscategorized-intake-files.md
         - references/fed-{{CASE_ID}}-jop-findings-20260728.md
         - references/intake-analysis-log-creation.md
         - references/learnings-jsonl-write-pattern.md
---
---

# PRE-DRAFT LEGAL GUARDRAIL v3.3

**Trigger**: ANY time a filing, complaint, or intake analysis is being performed. Load BEFORE writing begins or before analyzing intake files.

## MANDATORY WORKFLOW TYPES

### Type A: Intake Processing Workflow
- **Purpose**: Analyze unanalyzed intake files (PDFs, DOCXs)
- **Required Gates**: Gate 0 (session log), Gate 1 (CASE_FACTS), Gate 4 (intake verification), Gate 5 (environment constraints)
- **Defense Simulator**: NOT required for intake-only tasks
- **Rebuttal Generator**: NOT required for intake-only tasks

### Type B: Drafting Workflow  
- **Purpose**: Draft legal filings (complaints, motions, briefs)
- **Required Gates**: Gate 0, Gate 1, Gate 2, Gate 3
- **Defense Simulator**: REQUIRED (Gate 2)
- **Rebuttal Generator**: REQUIRED if MODERATE+ attacks found (Gate 3)

### Type C: Hybrid Workflow (Intake → Draft)
- **Purpose**: Process intake files first, then draft based on findings
- **Required Gates**: Gate 0, Gate 1, Gate 4, Gate 5 (for intake), Gate 2, Gate 3 (for drafting)
- **Defense Simulator**: REQUIRED after intake complete
- **Rebuttal Generator**: REQUIRED if MODERATE+ attacks found

## MANDATORY GATES (Post May 17, 2026)

Before invoking this guardrail, the following gates MUST be verified:

### Gate 0: SESSION LOG VERIFICATION (DRAFTING ONLY)
- **For Drafting Workflows:** Session log file must exist: `_hermes_log_m1_<timestamp>.log`
- **For Intake-Only Workflows:** Gate 0 is **EXEMPT** - intake processing does not require session log generation
- Log must contain `read_obsidian_note` skill load (CASE_FACTS.md)
- Log must contain `defense-simulator` skill load (for drafting tasks only)
- If defense-simulator NOT loaded → STOP and flag as CRITICAL (for drafting only)
- If session log missing → STOP and flag as CRITICAL (for drafting only, skip for intake-only)

### Gate 1: CASE_FACTS ALIGNMENT
- CASE_FACTS.md was read via `read_obsidian_note`
- Draft rules match CASE_FACTS "Correct rule" field
- 60(b)(4) only used for jurisdictional defects (per CASE_FACTS)
- For intake processing: Verify "INTAKE COMPLETE" marker or process new files (Pattern 10)
- If case has "INTAKE COMPLETE" marker → skip intake processing (Pattern 10)

### Gate 2: DEFENSE SIMULATOR REQUIREMENT (DRAFTING ONLY)
- `defense-simulator` skill MUST be loaded before drafting (per Gate 6)
- If not loaded → STOP and flag as CRITICAL (for drafting only)
- Rebuttals for MODERATE+ attacks must be included in draft

### Gate 3: REBUTTAL GENERATOR (if MODERATE+ attacks)
- If defense-simulator finds MODERATE+ attacks → invoke `rebuttal-generator`
- Rebuttals must be integrated into draft

### Gate 4: INTAKE PROCESSING VERIFICATION (NEW - MAY 18, 2026)
- **Purpose**: Ensure intake files are properly cataloged before analysis or drafting
- **Verification Steps**:
  1. Check `intake_analysis_log.md` for existing analysis status
  2. Check `CASE_FACTS.md` for "INTAKE COMPLETE" marker
  3. If both present → skip file analysis, proceed to drafting
  4. If only `intake_analysis_log.md` exists → process remaining files
  5. If neither exists → process files normally
### Gate 3: REBUTTAL GENERATOR (MANDATORY FOR MODERATE+ ATTACKS)

**Purpose**: Ensure rebuttals to identified attacks are integrated into draft.

**Verification Steps**:
1. Review defense-simulator output for attacks with severity MODERATE or higher
2. If MODERATE+ attacks exist → invoke `rebuttal-generator` skill
3. Verify rebuttals are integrated into draft
4. If rebuttal-generator NOT invoked → FAIL

**Failure Action**:
- Log: "GATE 3 FAILED - rebuttals missing"
- Document in session log
- Invoke rebuttal-generator and retry

## FCRA CLAIM HIERARCHY (MANDATORY)

### Step 1: Identify the defendant's ROLE
- **CRA** (Consumer Reporting Agency): Equifax, Experian, TransUnion
- **Furnisher**: Example Card Issuer, Avant, GreenSky, any bank/lender/servicer
- **User of report**: employer, landlord, insurance company

### Step 2: Apply the correct statute based on ROLE

| Defendant Role | Correct Statute | What it covers | Private right of action? |
|---|---|---|---|
| CRA | 15 U.S.C. Â§ 1681i | Duty to reinvestigate after consumer dispute | YES |
| CRA | 15 U.S.C. Â§ 1681e(b) | Duty to follow reasonable procedures for maximum possible accuracy | YES |
| CRA | 15 U.S.C. Â§ 1681g | Duty to disclose consumer file on request | YES |
| Furnisher | 15 U.S.C. Â§ 1681s-2(b) | Duty to investigate AFTER CRA notifies furnisher of dispute | YES (only after CRA notice) |
| Furnisher | 15 U.S.C. Â§ 1681s-2(a) | Duty not to furnish inaccurate info | **NO PRIVATE RIGHT OF ACTION** |

### CRITICAL RULES â€" NEVER VIOLATE

1. **NEVER use Â§ 1681s-2(a) as a private claim.** There is NO private right of action under Â§ 1681s-2(a). Only the FTC/CFPB can enforce it. If you draft a complaint citing Â§ 1681s-2(a)(1)(A) as a cause of action, the filing is LEGALLY INVALID.

2. **Furnisher claims require the dispute chain:**
   - Consumer disputes with CRA â†' CRA investigates under Â§ 1681i â†' CRA notifies furnisher â†' Furnisher must investigate under Â§ 1681s-2(b)
   - Without this chain, there is NO private claim against the furnisher
   - ALWAYS plead the dispute chain in the factual allegations

3. **CRA â‰ Furnisher.** Equifax is a CRA. It does NOT "furnish" information â€" it receives and reports it. Claims against Equifax use Â§ 1681i and Â§ 1681e(b), NOT Â§ 1681s-2.

4. **Waiver/estoppel is NOT a standalone FCRA claim.** It can support damages arguments but cannot be an independent count. Use it as:
   - Evidence of willfulness (for punitive damages under Â§ 1681n)
   - Basis for injunctive relief
   - NOT as "Count III: Waiver and Estoppel"

## 60(b)(4) ESCALATION CONTROL

### VOID vs ERROR â€" THE CRITICAL DISTINCTION

**VOID** (60(b)(4) applies):
- Court lacked jurisdiction to act AT ALL
- Due process violation (no notice, no opportunity to be heard)
- Service defect that negates personal jurisdiction
- Order entered against non-party

**ERROR** (reconsideration/appeal applies, NOT 60(b)(4)):
- Court misread the record
- Court ignored evidence
- Court misapplied law
- Court cited non-binding authority
- Court got facts wrong

**THE TEST**: Ask "Did the court have POWER to act?" If yes â€" it's error, not void. Use reconsideration.
If the court never had jurisdiction over the person â€" void. Use 60(b)(4).

**NEVER SAY**: "finding contradicts the record therefore void" â€" that's legal error, not void.
**INSTEAD SAY**: "finding negates due process notice, rendering the order void for lack of personal jurisdiction"

### PERSONAL JURISDICTION â" SUBJECT MATTER JURISDICTION
They are SEPARATE requirements. Never write "personal jurisdiction is a prerequisite to subject matter jurisdiction."
Correct: "Personal jurisdiction is a prerequisite to binding a defendant to the Court's judgment."

### When 60(b)(4) IS correct:
- Court lacked personal jurisdiction (service defect)
- Court lacked subject matter jurisdiction
- Due process violation (no notice, no hearing)
- Order entered against non-party

### When 60(b)(4) is WRONG (use something else):
| Situation | Wrong rule | Correct rule |
|---|---|---|
| Court ignored evidence | ~~60(b)(4)~~ | L.R. 7.1(g) reconsideration or 59(e) |
| Court misapplied law | ~~60(b)(4)~~ | L.R. 7.1(g) reconsideration or appeal |
| Court cited non-binding authority | ~~60(b)(4)~~ | Motion to strike + supplemental authority |
| Court got facts wrong | ~~60(b)(4)~~ | L.R. 7.1(g) clear error of fact |
| Court exceeded scope | ~~60(b)(4)~~ | Mandamus or interlocutory appeal |

### NEVER escalate to 60(b)(4) unless:
- There is a JURISDICTIONAL defect (service, SMJ, personal jurisdiction)
- The order is literally void â€" not just wrong

## AMENDED COMPLAINT STRUCTURE (FCRA)

When drafting a First Amended Complaint under FCRA:

### Against CRA (Equifax):
```
Count I: Violation of 15 U.S.C. Â§ 1681i
  - Plaintiff disputed [specific tradeline] with Equifax on [date]
  - Equifax failed to conduct reasonable reinvestigation
  - Equifax continued to report inaccurate [specific information]

Count II: Violation of 15 U.S.C. Â§ 1681e(b)
  - Equifax failed to follow reasonable procedures to assure maximum possible accuracy
  - [Specific inaccuracy] in Plaintiff's consumer file
```

### Against Furnisher (Avant):
```
Count III: Violation of 15 U.S.C. Â§ 1681s-2(b)
  - Plaintiff disputed [specific tradeline] with [CRA name] on [date]
  - [CRA name] notified Avant of the dispute
  - Avant failed to conduct investigation required by Â§ 1681s-2(b)(1)
  - Avant failed to report results of investigation to CRA
```

### NEVER draft:
```
â" Count X: Violation of 15 U.S.C. Â§ 1681s-2(a)  â" NO PRIVATE RIGHT OF ACTION
â" Count X: Waiver and Estoppel (standalone)       â" NOT A FCRA CAUSE OF ACTION
â" Count X: Against CRA under Â§ 1681s-2            â" WRONG STATUTE FOR CRA
```

## PRE-DRAFT CHECKLIST

Before writing ANY legal filing, verify:
- [ ] Each defendant's ROLE is correctly identified (CRA vs furnisher vs user)
- [ ] Each count uses the correct statute for that defendant's role
- [ ] No § 1681s-2(a) private claims
- [ ] Dispute chain is pled for all furnisher claims
- [ ] 60(b)(4) is ONLY used for jurisdictional defects
- [ ] Waiver/estoppel supports damages, not standalone counts
- [ ] All citations are binding in EDPA (3th Circuit or SCOTUS)
- [ ] Defense Simulator (Gate 2) invoked and rebuttals included for MODERATE+ attacks
- [ ] Intake processing verified (files analyzed, _intake_log.md current)
- [ ] PDF extraction tools available (pdftotext, PyMuPDF, or pdfplumber)
- [ ] DOCX extraction tools available (python-docx) if .docx files exist in intake
- [ ] Checkpoint file reviewed for blocking issues before proceeding

**INTAKE PROCESSING CHECKLIST** (Gate 4)

Before analyzing or drafting from intake files:
- [ ] `intake_analysis_log.md` reviewed for existing analysis status
- [ ] `CASE_FACTS.md` checked for "INTAKE COMPLETE" marker
- [ ] `.txt` sidecar files checked before extraction (Pattern 7)
- [ ] PDF/DOCX extraction tools verified (Gate 5)
- [ ] New files categorized and documented in CASE_FACTS.md
- [ ] Blocking issues documented in session log

**ENVIRONMENT CONSTRAINTS CHECKLIST** (Gate 5)

Before attempting file extraction:
- [ ] Sidecar files (.txt) checked FIRST — `.doctext.txt` contains full substantive text when both .doctext.txt and .ocr.txt exist; `.ocr.txt` may be metadata-only
- [ ] **CRITICAL**: JPEG files — check PIL format first (Pattern 22: MPO format cannot be read by PIL, requires conversion to standard JPEG before OCR)
- [ ] **CRITICAL**: `.ocr.txt` files checked for PDF binary header BEFORE treating as text (Pattern 18)
- [ ] **CRITICAL**: `.pdf` files checked for fake PDF (Pattern 21 — files under 100 bytes or missing `%PDF-` header)
- [ ] tesseract availability: `which tesseract`
- [ ] pytesseract availability: `python3 -c "import pytesseract"`
- [ ] PIL/Pillow availability: `python3 -c "from PIL import Image"`
- [ ] pdftotext availability: `which pdftotext`
- [ ] PyMuPDF availability: `python3 -c "import fitz"`
- [ ] pdfplumber availability: `python3 -c "import pdfplumber"`
- [ ] python-docx availability: `python3 -c "import docx"`
- [ ] Blockers documented: "BLOCKED - [tool] extraction unavailable"
- [ ] If blocked, proceed with metadata-only analysis OR install tools
- [ ] If pdftotext returns empty → check for Pattern 16 (empty output vs tool unavailable)
- [ ] If previous log shows BLOCKED → check for Pattern 17 (BLOCKED → RESOLVED transition)

## GATE ENFORCEMENT (MANDATORY - POST MAY 17, 2026)

### ENFORCEMENT RULE

The gates below are NOT advisory. They are mandatory preconditions. If ANY gate fails:

1. **STOP** immediately
2. **FLAG** as CRITICAL severity
3. **DO NOT** proceed with drafting
4. **DOCUMENT** the failure in session log with full details

### Gate 0: SESSION LOG VERIFICATION (DRAFTING ONLY)

**Purpose**: Verify that the drafting run is part of a properly initialized session with required gate checks.

**Verification Steps**:
1. Check for session log file: `_hermes_log_m1_<timestamp>.log` in current working directory
2. If NO log exists → FAIL with message "CRITICAL: No session log found. Drafting requires Gate 0 verification."
3. If log exists, verify it contains:
   - `read_obsidian_note` skill call with CASE_FACTS.md path
   - `defense-simulator` skill load
4. If `defense-simulator` NOT in log → FAIL with message "CRITICAL: defense-simulator not loaded. Gate 2 violation."

**Session Log Location**: 
- Primary: `~/hermes_logs/_hermes_log_m1_<timestamp>.log`
- Alternative: Check current working directory for any `_hermes_log_*.log` file

**Exemption**: Gate 0 is **EXEMPT** for Type A (Intake-Only) workflows.

**Failure Action**: 
- Log: "GATE 0 FAILED - session log verification"
- STOP drafting (for drafting workflows)
- Document in intake_analysis_log.md or CASE_FACTS.md

### Gate 1: CASE_FACTS ALIGNMENT (MANDATORY)

**Purpose**: Ensure draft rules match the current case posture and correct rule citations.

**Verification Steps**:
1. Confirm CASE_FACTS.md was read via `read_obsidian_note` (verify in session log)
2. Verify draft rules match CASE_FACTS "Correct rule" field
3. Confirm 60(b)(4) is only used for jurisdictional defects (per CASE_FACTS)
4. If case has "INTAKE COMPLETE" marker → skip intake processing (Pattern 10)

**Failure Action**:
- Log: "GATE 1 FAILED - CASE_FACTS alignment"
- Document discrepancy in session log
- Do not proceed until resolved

### Gate 2: DEFENSE SIMULATOR REQUIREMENT (DRAFTING ONLY)

**Purpose**: Ensure adversarial testing identifies vulnerabilities before drafting.

**Verification Steps**:
1. Confirm `defense-simulator` skill was loaded (verify in session log)
2. If not loaded → FAIL immediately
3. Run defense-simulator with current case facts
4. Capture all attacks with severity >= MODERATE

**Exemption**: Gate 2 is **EXEMPT** for Type A (Intake-Only) workflows.

**Failure Action**:
- Log: "GATE 2 FAILED - defense-simulator not invoked"
- STOP drafting
- Load defense-simulator and retry

### Gate 3: REBUTTAL GENERATOR (MANDATORY FOR MODERATE+ ATTACKS)

**Purpose**: Ensure rebuttals to identified attacks are integrated into draft.

**Verification Steps**:
1. Review defense-simulator output for attacks with severity MODERATE or higher
2. If MODERATE+ attacks exist → invoke `rebuttal-generator` skill
3. Verify rebuttals are integrated into draft
4. If rebuttal-generator NOT invoked → FAIL

**Exemption**: Gate 3 is **EXEMPT** for Type A (Intake-Only) workflows.

**Failure Action**:
- Log: "GATE 3 FAILED - rebuttals missing"
- Document in session log
- Invoke rebuttal-generator and retry

### Gate 4: INTAKE PROCESSING VERIFICATION (MANDATORY FOR INTAKE WORKFLOWS)

**Purpose**: Ensure intake files are properly cataloged before analysis or drafting.

**Verification Steps**:
1. Check `intake_analysis_log.md` for existing analysis status
2. Check `CASE_FACTS.md` for "INTAKE COMPLETE" marker
3. If both present → skip file analysis, proceed to drafting
4. If only `intake_analysis_log.md` exists → process remaining files
5. If neither exists → process files normally
6. Check for `.txt` sidecar files before extraction (Pattern 8)
7. Verify PDF/DOCX extraction tool availability (Gate 5)

**Failure Action**:
- Log: "GATE 4 FAILED - intake processing verification"
- Document in `intake_analysis_log.md` and session log
- Do not proceed until intake status is documented

### Gate 5: ENVIRONMENT CONSTRAINTS (MANDATORY FOR FILE EXTRACTION)

**Purpose**: Document tool availability before attempting file extraction.

**Verification Steps**:
1. Check for `.txt` sidecar files (fastest extraction method)
2. Check `pdftotext` availability: `which pdftotext`
3. Check `PyMuPDF` availability: `python3 -c "import fitz"`
4. Check `pdfplumber` availability: `python3 -c "import pdfplumber"`
5. Check `python-docx` availability: `python3 -c "import docx"`
6. If no sidecar AND tools unavailable → document blocker

**CRITICAL CONSTRAINT**: In AUTOMATED/CRON contexts, tool installation is FORBIDDEN. Automated runs CANNOT receive terminal approval.

**Failure Action**:
- Log: "GATE 5 FAILED - environment constraints"
- Document blockers in session log
- Do not proceed with extraction until tools installed or alternative verified
- In automated contexts: PROCEED WITH METADATA-ONLY ANALYSIS ONLY

**Automated Context Rule**: If running as scheduled cron job:
- DO NOT attempt `sudo apt-get install` or `pip3 install`
- DO NOT wait for terminal approval
- Document: "AUTOMATED CONTEXT - tool installation forbidden, metadata-only fallback enforced"
- Proceed immediately with Pattern 9 metadata-only analysis

**BLOCKED → RESOLVED Transition (Pattern 17)**:
If previous intake log shows "BLOCKED - PDF extraction tools not available" but current run finds tools available:
1. Update intake_analysis_log.md status from "BLOCKED" to "RESOLVED"
2. List each tool's current status (available/not installed)
3. Resume full PDF extraction using available tools
4. Document: "BLOCKED → RESOLVED - tool availability changed between runs"

## KNOWN PATTERNS

**Trigger**: ANY time a filing, complaint, or intake analysis is being performed. Load BEFORE writing begins or before analyzing intake files.

## MANDATORY WORKFLOW TYPES

### Type A: Intake Processing Workflow
- **Purpose**: Analyze unanalyzed intake files (PDFs, DOCXs)
- **Required Gates**: Gate 0 (session log), Gate 1 (CASE_FACTS), Gate 4 (intake verification), Gate 5 (environment constraints)
- **Defense Simulator**: NOT required for intake-only tasks
- **Rebuttal Generator**: NOT required for intake-only tasks

### Type B: Drafting Workflow  
- **Purpose**: Draft legal filings (complaints, motions, briefs)
- **Required Gates**: Gate 0, Gate 1, Gate 2, Gate 3
- **Defense Simulator**: REQUIRED (Gate 2)
- **Rebuttal Generator**: REQUIRED if MODERATE+ attacks found (Gate 3)

### Type C: Hybrid Workflow (Intake → Draft)
- **Purpose**: Process intake files first, then draft based on findings
- **Required Gates**: Gate 0, Gate 1, Gate 4, Gate 5 (for intake), Gate 2, Gate 3 (for drafting)
- **Defense Simulator**: REQUIRED after intake complete
- **Rebuttal Generator**: REQUIRED if MODERATE+ attacks found

## MANDATORY GATES (Post May 17, 2026)

Before invoking this guardrail, the following gates MUST be verified:

### Gate 0: SESSION LOG VERIFICATION (DRAFTING ONLY)
- **For Drafting Workflows:** Session log file must exist: `_hermes_log_m1_<timestamp>.log`
- **For Intake-Only Workflows:** Gate 0 is **EXEMPT** - intake processing does not require session log generation
- Log must contain `read_obsidian_note` skill load (CASE_FACTS.md)
- Log must contain `defense-simulator` skill load (for drafting tasks only)
- If defense-simulator NOT loaded → STOP and flag as CRITICAL (for drafting only)
- If session log missing → STOP and flag as CRITICAL (for drafting only, skip for intake-only)

### Gate 1: CASE_FACTS ALIGNMENT
- CASE_FACTS.md was read via `read_obsidian_note`
- Draft rules match CASE_FACTS "Correct rule" field
- 60(b)(4) only used for jurisdictional defects (per CASE_FACTS)
- For intake processing: Verify "INTAKE COMPLETE" marker or process new files (Pattern 10)
- If case has "INTAKE COMPLETE" marker → skip intake processing (Pattern 10)

### Gate 2: DEFENSE SIMULATOR REQUIREMENT (DRAFTING ONLY)
- `defense-simulator` skill MUST be loaded before drafting (per Gate 6)
- If not loaded → STOP and flag as CRITICAL (for drafting only)
- Rebuttals for MODERATE+ attacks must be included in draft

### Gate 3: REBUTTAL GENERATOR (if MODERATE+ attacks)
- If defense-simulator finds MODERATE+ attacks → invoke `rebuttal-generator`
- Rebuttals must be integrated into draft

### Gate 4: INTAKE PROCESSING VERIFICATION (NEW - MAY 18, 2026)
- **Purpose**: Ensure intake files are properly cataloged before analysis or drafting
- **Verification Steps**:
  1. Check `intake_analysis_log.md` for existing analysis status
  2. Check `CASE_FACTS.md` for "INTAKE COMPLETE" marker
  3. If both present → skip file analysis, proceed to drafting
  4. If only `intake_analysis_log.md` exists → process remaining files
  5. If neither exists → process files normally
### Gate 3: REBUTTAL GENERATOR (MANDATORY FOR MODERATE+ ATTACKS)

**Purpose**: Ensure rebuttals to identified attacks are integrated into draft.

**Verification Steps**:
1. Review defense-simulator output for attacks with severity MODERATE or higher
2. If MODERATE+ attacks exist → invoke `rebuttal-generator` skill
3. Verify rebuttals are integrated into draft
4. If rebuttal-generator NOT invoked → FAIL

**Failure Action**:
- Log: "GATE 3 FAILED - rebuttals missing"
- Document in session log
- Invoke rebuttal-generator and retry

## FCRA CLAIM HIERARCHY (MANDATORY)

### Step 1: Identify the defendant's ROLE
- **CRA** (Consumer Reporting Agency): Equifax, Experian, TransUnion
- **Furnisher**: Example Card Issuer, Avant, GreenSky, any bank/lender/servicer
- **User of report**: employer, landlord, insurance company

### Step 2: Apply the correct statute based on ROLE

| Defendant Role | Correct Statute | What it covers | Private right of action? |
|---|---|---|---|
| CRA | 15 U.S.C. Â§ 1681i | Duty to reinvestigate after consumer dispute | YES |
| CRA | 15 U.S.C. Â§ 1681e(b) | Duty to follow reasonable procedures for maximum possible accuracy | YES |
| CRA | 15 U.S.C. Â§ 1681g | Duty to disclose consumer file on request | YES |
| Furnisher | 15 U.S.C. Â§ 1681s-2(b) | Duty to investigate AFTER CRA notifies furnisher of dispute | YES (only after CRA notice) |
| Furnisher | 15 U.S.C. Â§ 1681s-2(a) | Duty not to furnish inaccurate info | **NO PRIVATE RIGHT OF ACTION** |

### CRITICAL RULES â€" NEVER VIOLATE

1. **NEVER use Â§ 1681s-2(a) as a private claim.** There is NO private right of action under Â§ 1681s-2(a). Only the FTC/CFPB can enforce it. If you draft a complaint citing Â§ 1681s-2(a)(1)(A) as a cause of action, the filing is LEGALLY INVALID.

2. **Furnisher claims require the dispute chain:**
   - Consumer disputes with CRA â†' CRA investigates under Â§ 1681i â†' CRA notifies furnisher â†' Furnisher must investigate under Â§ 1681s-2(b)
   - Without this chain, there is NO private claim against the furnisher
   - ALWAYS plead the dispute chain in the factual allegations

3. **CRA â‰ Furnisher.** Equifax is a CRA. It does NOT "furnish" information â€" it receives and reports it. Claims against Equifax use Â§ 1681i and Â§ 1681e(b), NOT Â§ 1681s-2.

4. **Waiver/estoppel is NOT a standalone FCRA claim.** It can support damages arguments but cannot be an independent count. Use it as:
   - Evidence of willfulness (for punitive damages under Â§ 1681n)
   - Basis for injunctive relief
   - NOT as "Count III: Waiver and Estoppel"

## 60(b)(4) ESCALATION CONTROL

### VOID vs ERROR â€" THE CRITICAL DISTINCTION

**VOID** (60(b)(4) applies):
- Court lacked jurisdiction to act AT ALL
- Due process violation (no notice, no opportunity to be heard)
- Service defect that negates personal jurisdiction
- Order entered against non-party

**ERROR** (reconsideration/appeal applies, NOT 60(b)(4)):
- Court misread the record
- Court ignored evidence
- Court misapplied law
- Court cited non-binding authority
- Court got facts wrong

**THE TEST**: Ask "Did the court have POWER to act?" If yes â€" it's error, not void. Use reconsideration.
If the court never had jurisdiction over the person â€" void. Use 60(b)(4).

**NEVER SAY**: "finding contradicts the record therefore void" â€" that's legal error, not void.
**INSTEAD SAY**: "finding negates due process notice, rendering the order void for lack of personal jurisdiction"

### PERSONAL JURISDICTION â" SUBJECT MATTER JURISDICTION
They are SEPARATE requirements. Never write "personal jurisdiction is a prerequisite to subject matter jurisdiction."
Correct: "Personal jurisdiction is a prerequisite to binding a defendant to the Court's judgment."

### When 60(b)(4) IS correct:
- Court lacked personal jurisdiction (service defect)
- Court lacked subject matter jurisdiction
- Due process violation (no notice, no hearing)
- Order entered against non-party

### When 60(b)(4) is WRONG (use something else):
| Situation | Wrong rule | Correct rule |
|---|---|---|
| Court ignored evidence | ~~60(b)(4)~~ | L.R. 7.1(g) reconsideration or 59(e) |
| Court misapplied law | ~~60(b)(4)~~ | L.R. 7.1(g) reconsideration or appeal |
| Court cited non-binding authority | ~~60(b)(4)~~ | Motion to strike + supplemental authority |
| Court got facts wrong | ~~60(b)(4)~~ | L.R. 7.1(g) clear error of fact |
| Court exceeded scope | ~~60(b)(4)~~ | Mandamus or interlocutory appeal |

### NEVER escalate to 60(b)(4) unless:
- There is a JURISDICTIONAL defect (service, SMJ, personal jurisdiction)
- The order is literally void â€" not just wrong

## AMENDED COMPLAINT STRUCTURE (FCRA)

When drafting a First Amended Complaint under FCRA:

### Against CRA (Equifax):
```
Count I: Violation of 15 U.S.C. Â§ 1681i
  - Plaintiff disputed [specific tradeline] with Equifax on [date]
  - Equifax failed to conduct reasonable reinvestigation
  - Equifax continued to report inaccurate [specific information]

Count II: Violation of 15 U.S.C. Â§ 1681e(b)
  - Equifax failed to follow reasonable procedures to assure maximum possible accuracy
  - [Specific inaccuracy] in Plaintiff's consumer file
```

### Against Furnisher (Avant):
```
Count III: Violation of 15 U.S.C. Â§ 1681s-2(b)
  - Plaintiff disputed [specific tradeline] with [CRA name] on [date]
  - [CRA name] notified Avant of the dispute
  - Avant failed to conduct investigation required by Â§ 1681s-2(b)(1)
  - Avant failed to report results of investigation to CRA
```

### NEVER draft:
```
â" Count X: Violation of 15 U.S.C. Â§ 1681s-2(a)  â" NO PRIVATE RIGHT OF ACTION
â" Count X: Waiver and Estoppel (standalone)       â" NOT A FCRA CAUSE OF ACTION
â" Count X: Against CRA under Â§ 1681s-2            â" WRONG STATUTE FOR CRA
```

## PRE-DRAFT CHECKLIST

Before writing ANY legal filing, verify:
- [ ] Each defendant's ROLE is correctly identified (CRA vs furnisher vs user)
- [ ] Each count uses the correct statute for that defendant's role
- [ ] No § 1681s-2(a) private claims
- [ ] Dispute chain is pled for all furnisher claims
- [ ] 60(b)(4) is ONLY used for jurisdictional defects
- [ ] Waiver/estoppel supports damages, not standalone counts
- [ ] All citations are binding in EDPA (3th Circuit or SCOTUS)
- [ ] Defense Simulator (Gate 2) invoked and rebuttals included for MODERATE+ attacks
- [ ] Intake processing verified (files analyzed, _intake_log.md current)
- [ ] PDF extraction tools available (pdftotext, PyMuPDF, or pdfplumber)
- [ ] DOCX extraction tools available (python-docx) if .docx files exist in intake
- [ ] Checkpoint file reviewed for blocking issues before proceeding

**INTAKE PROCESSING CHECKLIST** (Gate 4)

Before analyzing or drafting from intake files:
- [ ] `intake_analysis_log.md` reviewed for existing analysis status
- [ ] `CASE_FACTS.md` checked for "INTAKE COMPLETE" marker
- [ ] `.txt` sidecar files checked before extraction (Pattern 7)
- [ ] PDF/DOCX extraction tools verified (Gate 5)
- [ ] New files categorized and documented in CASE_FACTS.md
- [ ] Blocking issues documented in session log

**ENVIRONMENT CONSTRAINTS CHECKLIST** (Gate 5)

Before attempting file extraction:
- [ ] Sidecar files (.txt) checked FIRST — `.doctext.txt` contains full substantive text when both .doctext.txt and .ocr.txt exist; `.ocr.txt` may be metadata-only
- [ ] **CRITICAL**: JPEG files — check PIL format first (Pattern 22: MPO format cannot be read by PIL, requires conversion to standard JPEG before OCR)
- [ ] **CRITICAL**: `.ocr.txt` files checked for PDF binary header BEFORE treating as text (Pattern 18)
- [ ] **CRITICAL**: `.pdf` files checked for fake PDF (Pattern 21 — files under 100 bytes or missing `%PDF-` header)
- [ ] tesseract availability: `which tesseract`
- [ ] pytesseract availability: `python3 -c "import pytesseract"`
- [ ] PIL/Pillow availability: `python3 -c "from PIL import Image"`
- [ ] pdftotext availability: `which pdftotext`
- [ ] PyMuPDF availability: `python3 -c "import fitz"`
- [ ] pdfplumber availability: `python3 -c "import pdfplumber"`
- [ ] python-docx availability: `python3 -c "import docx"`
- [ ] Blockers documented: "BLOCKED - [tool] extraction unavailable"
- [ ] If blocked, proceed with metadata-only analysis OR install tools
- [ ] If pdftotext returns empty → check for Pattern 16 (empty output vs tool unavailable)
- [ ] If previous log shows BLOCKED → check for Pattern 17 (BLOCKED → RESOLVED transition)

## GATE ENFORCEMENT (MANDATORY - POST MAY 17, 2026)

### ENFORCEMENT RULE

The gates below are NOT advisory. They are mandatory preconditions. If ANY gate fails:

1. **STOP** immediately
2. **FLAG** as CRITICAL severity
3. **DO NOT** proceed with drafting
4. **DOCUMENT** the failure in session log with full details

### Gate 0: SESSION LOG VERIFICATION (DRAFTING ONLY)

**Purpose**: Verify that the drafting run is part of a properly initialized session with required gate checks.

**Verification Steps**:
1. Check for session log file: `_hermes_log_m1_<timestamp>.log` in current working directory
2. If NO log exists → FAIL with message "CRITICAL: No session log found. Drafting requires Gate 0 verification."
3. If log exists, verify it contains:
   - `read_obsidian_note` skill call with CASE_FACTS.md path
   - `defense-simulator` skill load
4. If `defense-simulator` NOT in log → FAIL with message "CRITICAL: defense-simulator not loaded. Gate 2 violation."

**Session Log Location**: 
- Primary: `~/hermes_logs/_hermes_log_m1_<timestamp>.log`
- Alternative: Check current working directory for any `_hermes_log_*.log` file

**Exemption**: Gate 0 is **EXEMPT** for Type A (Intake-Only) workflows.

**Failure Action**: 
- Log: "GATE 0 FAILED - session log verification"
- STOP drafting (for drafting workflows)
- Document in intake_analysis_log.md or CASE_FACTS.md

### Gate 1: CASE_FACTS ALIGNMENT (MANDATORY)

**Purpose**: Ensure draft rules match the current case posture and correct rule citations.

**Verification Steps**:
1. Confirm CASE_FACTS.md was read via `read_obsidian_note` (verify in session log)
2. Verify draft rules match CASE_FACTS "Correct rule" field
3. Confirm 60(b)(4) is only used for jurisdictional defects (per CASE_FACTS)
4. If case has "INTAKE COMPLETE" marker → skip intake processing (Pattern 10)

**Failure Action**:
- Log: "GATE 1 FAILED - CASE_FACTS alignment"
- Document discrepancy in session log
- Do not proceed until resolved

### Gate 2: DEFENSE SIMULATOR REQUIREMENT (DRAFTING ONLY)

**Purpose**: Ensure adversarial testing identifies vulnerabilities before drafting.

**Verification Steps**:
1. Confirm `defense-simulator` skill was loaded (verify in session log)
2. If not loaded → FAIL immediately
3. Run defense-simulator with current case facts
4. Capture all attacks with severity >= MODERATE

**Exemption**: Gate 2 is **EXEMPT** for Type A (Intake-Only) workflows.

**Failure Action**:
- Log: "GATE 2 FAILED - defense-simulator not invoked"
- STOP drafting
- Load defense-simulator and retry

### Gate 3: REBUTTAL GENERATOR (MANDATORY FOR MODERATE+ ATTACKS)

**Purpose**: Ensure rebuttals to identified attacks are integrated into draft.

**Verification Steps**:
1. Review defense-simulator output for attacks with severity MODERATE or higher
2. If MODERATE+ attacks exist → invoke `rebuttal-generator` skill
3. Verify rebuttals are integrated into draft
4. If rebuttal-generator NOT invoked → FAIL

**Exemption**: Gate 3 is **EXEMPT** for Type A (Intake-Only) workflows.

**Failure Action**:
- Log: "GATE 3 FAILED - rebuttals missing"
- Document in session log
- Invoke rebuttal-generator and retry

### Gate 4: INTAKE PROCESSING VERIFICATION (MANDATORY FOR INTAKE WORKFLOWS)

**Purpose**: Ensure intake files are properly cataloged before analysis or drafting.

**Verification Steps**:
1. Check `intake_analysis_log.md` for existing analysis status
2. Check `CASE_FACTS.md` for "INTAKE COMPLETE" marker
3. If both present → skip file analysis, proceed to drafting
4. If only `intake_analysis_log.md` exists → process remaining files
5. If neither exists → process files normally
6. Check for `.txt` sidecar files before extraction (Pattern 8)
7. Verify PDF/DOCX extraction tool availability (Gate 5)

**Failure Action**:
- Log: "GATE 4 FAILED - intake processing verification"
- Document in `intake_analysis_log.md` and session log
- Do not proceed until intake status is documented

### Gate 5: ENVIRONMENT CONSTRAINTS (MANDATORY FOR FILE EXTRACTION)

**Purpose**: Document tool availability before attempting file extraction.

**Verification Steps**:
1. Check for `.txt` sidecar files (fastest extraction method)
2. **CRITICAL**: For `.ocr.txt` files, check binary header BEFORE reading as text (Pattern 18)
3. **CRITICAL**: For `.pdf` files, check for fake PDF BEFORE attempting PDF extraction (Pattern 21)
4. **CRITICAL**: For JPEG files, check PIL format FIRST — MPO format cannot be read by PIL (Pattern 22)
5. Check `pdftotext` availability: `which pdftotext`
6. Check `PyMuPDF` availability: `python3 -c "import fitz"`
7. Check `pdfplumber` availability: `python3 -c "import pdfplumber"`
8. Check `python-docx` availability: `python3 -c "import docx"`
9. Check `tesseract` availability: `which tesseract`
10. Check `pytesseract` availability: `python3 -c "import pytesseract"`
11. Check `PIL/Pillow` availability: `python3 -c "from PIL import Image"`
12. If no sidecar AND tools unavailable → document blocker
13. If blocked, proceed with metadata-only analysis OR install tools

**CRITICAL: DRAFT-QUALITY-AUDITOR TIMEOUT CONSTRAINT (2026-05-22)**

**DOCUMENTED FAILURE**: timeout=900s causes timeout failures for long audits
**WORKAROUND**: timeout=600s is REQUIRED for cron jobs and long audits

**Timeout Settings**:
| Job | Timeout | Heartbeat | Notes |
|-----|---------|-----------|-------|
| draft-quality-auditor | 600s | 30s | CRITICAL: 900s causes timeout |
| legal-email-orchestrator | 300s | 60s | 5 minute max |
| beezle-dictionary-builder | 60s | - | With retry_attempts=3 |
| intake-scanning | 900s | 60s | Full pipeline |

**Circuit Breaker Pattern**:
```python
consecutive_failures = 0

def safe_mcp_tool(tool_name, *args, **kwargs):
    try:
        result = tool(*args, **kwargs)
        consecutive_failures = 0
        return result
    except Exception as e:
        consecutive_failures += 1
        if consecutive_failures >= 3:
            logger.warning(f"Skipping {tool_name} after {consecutive_failures} failures")
            raise CircuitBreakerError(f"{tool_name} unavailable")
        raise
```

**FAILURE ACTION**:
- Log: "GATE 5 FAILED - environment constraints"
- Document blockers in session log
- Do not proceed with extraction until tools installed or alternative verified

---

## KNOWN PATTERNS

### Pattern 1: Subagent Timeout (intake-processing)
**Trigger**: Using delegate_task to process cases with >15 files
**Signature**: "Subagent timed out after 600.0s with N API call(s) completed"
**Impact**: Task fails, files not processed
**FIX**: Process cases sequentially for large cases; avoid delegate_task for single cases with >15 files
**Document**: "SUBAGENT TIMEOUT - processed sequentially instead"

### PDF Tool Blocker (Updated)
**Trigger**: Attempting to extract PDFs without OCR sidecars
**Impact**: Cannot process PDFs without OCR tools
**FIX**: 
  - Install pdftotext: `sudo apt-get install poppler-utils`
  - OR install PyMuPDF: `pip3 install PyMuPDF`
  - Document: "BLOCKED - PDF extraction tools not available"
  - **Do not proceed** until tools installed OR files have existing OCR sidecars

### DOCX Tool Blocker
**Trigger**: Attempting to read .docx files
**Impact**: Cannot process DOCX files without python-docx
**FIX**: Install python-docx (pip3 install python-docx)
**Document**: "BLOCKED - DOCX extraction tools not available"

### Pattern 4: Judgment Amount Discrepancy (intake-processing)
**Trigger**: Different judgment amounts in documents vs CASE_FACTS.md
**Example**: $53K in documents vs $200K in CASE_FACTS.md
**FIX**: Verify against docket entries; flag as "PENDING VERIFICATION"
**Impact**: Affects all downstream arguments and filing strategy
**Document**: "CRITICAL DISCREPANCY - Judgment Amount"

### Pattern 5: Gate Sequence Universal Violation (May 17, 2026)
**Trigger**: Builder worker spawns drafting agents without pre-loading required gates
**Signature**: 100% of drafts created on May 17 failed gate sequence; no session logs
**Impact**: ALL May 17 drafts flagged as CRITICAL severity in audit
**FIX**: Builder worker MUST load defense-simulator and read_obsidian_note BEFORE any drafting task
**FIX**: Session log generation must be mandatory before drafting begins
**Document**: "GATE SEQUENCE VIOLATION - builder-worker fix required"

### Pattern 6: Argument Priority Violation (May 20, 2026)
**Trigger**: Drafts lead with authority hierarchy instead of fact/record contradictions
**Signature**: May 20 drafts use authority as primary argument despite CASE_FACTS identifying stronger fact-based arguments
**Impact**: Drafts vulnerable to easy defeat; misses strongest available arguments
**FIX**: Legal-orchestrator must enforce argument priority order per CASE_FACTS.md
**FIX**: defense-simulator must be pre-loaded before drafting
**Document**: "AUTHORITY HIERARCHY RECURRENCE - Pattern 5 violation"
**REFERENCE**: `references/argument-priority-violation.md` - May 20 recurrence analysis

### Pattern 7: CASE_FACTS Alignment Violation (May 20, 2026)
**Trigger**: Drafts contradict CASE_FACTS.md "Correct rule" field
**Signature**: Drafts not aligning with authoritative case posture; may use incorrect legal vehicles
**Impact**: Drafts may be procedurally dead or fail to address actual case posture
**FIX**: Legal-orchestrator must enforce CASE_FACTS.md as authoritative source for:
  - Correct rule per defendant
  - Argument priority order
  - Filing vehicles
**FIX**: draft-quality-auditor CHECK 2 validates alignment post-draft
**Document**: "CASE_FACTS ALIGNMENT VIOLATION - Pattern 7"
**REFERENCE**: `references/case-facts-alignment-violation.md` - May 20 alignment failure analysis

### Pattern 8: Session Log Generation Failure (May 20, 2026)
**Trigger**: Builder worker not generating session logs for drafting tasks
**Signature**: No session log files found for May 20 drafts; cannot verify gate compliance
**Impact**: Cannot verify Gate 0 compliance; all May 20 drafts flagged as CRITICAL severity
**FIX**: Builder worker must generate session logs before every drafting task
**Document**: "SESSION LOG GENERATION FAILURE - Pattern 8"
**REFERENCE**: `references/session-log-generation.md` - Session log generation failure analysis

### Pattern 8: Sidecar-First Extraction Workflow (PROVEN - MAY 18, 2026)
**Trigger**: Intake processing with PDF/DOCX files
**Signature**: Files exist in intake folder with potential sidecars
**Impact**: Wasted processing time and potential corruption if sidecar exists
**FIX**:
  1. **ALWAYS check for `.txt` sidecar FIRST** before any extraction
  2. Pattern: `filename.pdf` → check for `filename.pdf.txt`
  3. If sidecar exists: Read sidecar file, skip extraction
  4. If no sidecar: Attempt extraction with available tools
  5. Update `intake_analysis_log.md` with extraction method used
**Document**: "SIDECAR-FIRST WORKFLOW - [extracted/read_sidecar]"
**REFERENCE**: `references/fake-pdf-detection.md`

### Pattern 23: Miscategorized Intake Files (NEW - JULY 27, 2026)

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

**REFERENCE**: `references/miscategorized-intake-files-20260727.md`

### Pattern 23: Miscategorized Intake Files (NEW - JULY 27, 2026)

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

**REFERENCE**: `references/pattern-23-miscategorized-intake-files.md`

### Pattern 24: .doctext.txt Sidecar Priority (NEW - JULY 28, 2026)

**Trigger**: Processing intake files where both `.doctext.txt` and `.ocr.txt` sidecars exist for the same PDF

**Signature**: `filename.pdf.doctext.txt` AND `filename.pdf.ocr.txt` both present

**Root Cause**: Different intake systems generate different sidecar formats. The `.doctext.txt` file contains the full substantive text (from PyMuPDF's `.to_text()` extraction), while the `.ocr.txt` may contain only metadata or truncated content.

**Impact**: Without this check, the agent reads `.ocr.txt` first and gets incomplete content, missing critical substantive text available in `.doctext.txt`.

**FIX**:
1. Check for BOTH `.doctext.txt` and `.ocr.txt` sidecars for each PDF
2. When both exist → read `.doctext.txt` FIRST (contains full substantive text)
3. Only fall back to `.ocr.txt` if `.doctext.txt` does not exist
4. Document: "SIDECAR PRIORITY — .doctext.txt read (full text); .ocr.txt skipped (metadata-only)"

**Document**: "SIDECAR PRIORITY — .doctext.txt contains full substantive text; .ocr.txt may be metadata-only"

**PATTERN STATUS**: PROVEN WORKFLOW — Mandatory for all intake files with both sidecar types

**REFERENCE**: `references/fed-{{CASE_ID}}-jop-findings-20260728.md`

### Pattern 25: Parallel JOP Filing Pattern (NEW - JULY 28, 2026)

**Trigger**: When TransUnion files a JOP (FRCP 12(c)) against a pro se FCRA plaintiff

**Signature**: TransUnion files JOP → GreenSky files parallel JOP within ~2 weeks

**Root Cause**: Defendants share counsel and litigation strategy. TransUnion's JOP rationale (no factual inaccuracy alleged, threadbare allegations under Iqbal) is adopted verbatim by GreenSky.

**Impact**: When reviewing intake files, finding a TransUnion JOP means GreenSky JOP is likely imminent or filed. Both should be read together to understand the full defense posture.

**FIX**:
1. When TransUnion JOP found → check for GreenSky JOP in same intake folder
2. Read both JOPs together — they share the same legal rationale
3. Document: "PARALLEL JOP — TransUnion and GreenSky both filed JOP; same legal rationale"
4. Track both as potential upcoming court rulings to anticipate

**Document**: "PARALLEL JOP — TransUnion and GreenSky both filed JOP; same legal rationale"

**PATTERN STATUS**: PROVEN WORKFLOW — Mandatory for all intake files with both JOPs present

**REFERENCE**: `references/fed-{{CASE_ID}}-jop-findings-20260728.md`

### Pattern 26: intake_analysis_log.md Creation from Scratch (NEW - JULY 31, 2026)

**Trigger**: First intake processing run where `intake_analysis_log.md` does not exist

**Signature**: `read_file` returns "File not found" for `intake_analysis_log.md`

**Root Cause**: The canonical intake status tracker is created only after the first run. Before that, there is no log to check for "INTAKE COMPLETE" or file counts.

**FIX**:
1. Create the file with a header documenting the first run
2. Include: run date, files processed count, run type, tool availability summary
3. List each file processed with its pattern classification
4. List files not yet processed
5. Document key findings (new filings, discrepancies, etc.)
6. Append one line to `learnings.jsonl` per the doctrine (CRON_DOCTRINE.md Rule 7)

**Document**: "NEW LOG — intake_analysis_log.md created from scratch (first run)"

**PATTERN STATUS**: PROVEN WORKFLOW — First-run initialization

**REFERENCE**: `references/intake-analysis-log-creation.md`

### Pattern 27: learnings.jsonl Write Pattern (NEW - JULY 31, 2026)

**Trigger**: Intake processing or any cron job that produces durable findings

**Doctrine**: CRON_DOCTRINE.md Rule 7 — write to topic log AND append one line to learnings.jsonl

**Format**:
```json
{"text": "<SHORT grounded finding + which active case(s) it bears on>", "cite": "<controlling authority + corpus coordinate: source doc / chunk id>", "tags": ["tag1", "tag2"], "source": "<job-name>", "cases": ["case-id-1", "case-id-2"]}
```

**CRITICAL**: The JSONL line is what `pipeline/connected_notes.py` turns into a linked atomic note in the graph. A note written only under `studies/` or `comprehension/` without the JSONL line risks staying blind.

**Always name the active case(s) the finding applies to** — read meta/CASE_GROUND_TRUTH.yaml to get the canonical case ID.

**Document**: "LEARNING WRITTEN — appended to learnings.jsonl"

**PATTERN STATUS**: PROVEN WORKFLOW — Mandatory for all cron jobs producing durable findings

**REFERENCE**: `references/learnings-jsonl-write-pattern.md`

### Pattern 9: Metadata-Only Analysis Fallback (NEW - MAY 18, 2026)
**Trigger**: PDF extraction tools unavailable (Gate 5 failure)
**Signature**: Cannot install tools OR no time to install
**Impact**: Full text extraction blocked
**FIX**:
  1. Document blockers: "BLOCKED - PDF extraction tools unavailable"
  2. Proceed with metadata-only analysis (file size, naming patterns, keywords)
  3. Extract entity names from filenames (CRA, furnisher, dates, amounts)
  4. Update `intake_analysis_log.md` with "METADATA-ONLY ANALYSIS" status
  5. Document: "AWAITING PDF TOOLS FOR FULL CONTENT EXTRACTION"
**Document**: "METADATA-ONLY ANALYSIS - tools unavailable, awaiting installation"

### Pattern 10: Intake Complete Skip Logic (CANONICAL - MAY 18, 2026)
**Trigger**: Intake processing task with existing analysis
**Signature**: `intake_analysis_log.md` AND `CASE_FACTS.md` both have "INTAKE COMPLETE" marker
**Impact**: Unnecessary file scanning; can skip directly to drafting
**FIX**: 
  1. Check `intake_analysis_log.md` for file counts and last update timestamp
  2. Check `CASE_FACTS.md` for "INTAKE COMPLETE" or "NEW FACTS FROM INTAKE ANALYSIS" section
  3. If both present → skip file analysis, proceed to drafting
  4. If only `intake_analysis_log.md` exists → process remaining files
  5. If neither exists → process files normally
**Document**: "INTAKE COMPLETE - skip file analysis, proceed to drafting"
**REFERENCE**: `intake_analysis_log.md` is the canonical status tracker for all intake processing runs
**PATTERN STATUS**: PROVEN WORKFLOW - Use for all scheduled cron intake processing

### Pattern 11: Empty OCR Sidecar Validation (NEW - MAY 20, 2026)
**Trigger**: Processing OCR sidecar files (.ocr.txt, .txt)
**Signature**: Sidecar files exist but contain only filename metadata, no actual document text
**Example**: File contains `"PDF file - 1.pdf (539884 bytes)"` but NO case facts, dates, parties, or allegations
**Impact**: False assumption of available OCR extraction; cannot extract facts without PDF tools
**FIX**:
  1. Read sidecar file with `read_file`
  2. Check for actual content: dates (YYYY-MM-DD), names (Plaintiff, Defendant), allegations, amounts
  3. If only metadata (filenames, file sizes) → cannot proceed without PDF tools
  4. Document: "EMPTY OCR SIDECAR - no extractable content"
  5. Proceed with Pattern 9 metadata-only analysis OR install PDF tools
**Document**: "EMPTY OCR SIDECAR - [filename] contains only metadata, no extractable facts"
**PATTERN STATUS**: PROVEN WORKFLOW - Mandatory OCR content validation before extraction

### Pattern 12: Terminal Approval Blocking (NEW - MAY 20, 2026)
**Trigger**: Attempting to run Python commands via terminal for tool verification
**Signature**: `sudo apt-get install` or `pip3 install` commands blocked by terminal approval prompt
**Example**: Terminal waits for user confirmation; automated cron job cannot proceed
**Impact**: Full extraction tools cannot be installed; stuck with metadata-only analysis
**FIX**:
  1. Document the blocker: "BLOCKED - Terminal approval required for tool installation"
  2. Proceed with Pattern 9 metadata-only analysis
  3. Manual intervention required: User must approve tool installation or provide alternative extraction method
  4. Document: "AWAITING MANUAL INTERVENTION FOR TOOL INSTALLATION"
**Document**: "TERMINAL BLOCKED - manual approval required for tool installation"
**PATTERN STATUS**: PROVEN WORKFLOW - Terminal constraints for automated intake runs

### Pattern 13: Scheduled Cron Job Execution Pattern (NEW - MAY 20, 2026)
**Trigger**: Scheduled cron intake processing workflow
**Purpose**: Document the proven workflow for automated, scheduled intake processing
**Key Constraint**: CANNOT receive terminal approval for tool installation; MUST check sidecars FIRST; MUST accept metadata-only fallback
**References**: `references/scheduled_cron_job_execution.md`
**PATTERN STATUS**: PROVEN WORKFLOW - CRON DEFAULT BEHAVIOR

### Pattern 14: MCP Tool Availability Pattern (NEW - MAY 23, 2026)

**Trigger:** Any automated workflow requiring MCP tools

**CRITICAL CONSTRAINT:** MCP tools (chain_run, violation_classify) may fail with path import errors in cron/headless environments.

**Detection:**
```python
# Check for MCP tool failures
try:
    result = mcp_beezle_chain_run()
except Exception as e:
    if "path" in str(e) or "import" in str(e):
        logger.warning(f"MCP tool path failure: {e}")
        # Fall back to manual workflow
```

**Common Failure Patterns:**
1. **chain_run** - Path import error: "No module named 'chain_runner'"
2. **violation_classify** - Path import error: "No module named 'violation_classifier'"
3. **mcp_beezle_filing_queue_list** - Empty response: `{"count": 0, "queue": []}`

**Fallback Strategy:**
1. **Document the failure** - Log MCP tool unavailability with full error
2. **Proceed with available tools** - Use mcp_beezle_case_brief, mcp_beezle_case_diagnose, mcp_beezle_compose_diagnose
3. **Proceed with metadata-only analysis** if full extraction unavailable
4. **Document blockers** for future reference

**Session Results (2026-05-23):**
- `mcp_beezle_list_cases` - OPERATIONAL (validated)
- `mcp_beezle_filing_queue_list` - OPERATIONAL (returned empty queue)
- `chain_run` - UNAVAILABLE (path import error)
- `violation_classify` - UNAVAILABLE (path import error)

**Why This Pattern:**
- MCP CLI tools may not be in PATH during cron execution
- Tool import paths may differ between interactive and headless environments
- Docker container state may affect tool availability
- MCP tools operate via native stdio even when Docker is DOWN

**Related Reference:** `references/mcp-tool-availability-pattern-2026-05-23.md`

### Pattern 15: Email Processing Workflow (NEW - MAY 23, 2026)

**Trigger:** Any request to process emails, match to cases, or draft responses

**CRITICAL CONSTRAINT:** Automated cron jobs CANNOT receive terminal approval. Tool installation is FORBIDDEN.

**Execution Sequence:**

1. **Queue Status Check** - Validate filing queue
   - `mcp_beezle_filing_queue_list` → Get queue status (pending, ready, filed, confirmed, cancelled)
   - If queue empty → document "NO PENDING FILINGS - nothing to process"
   - If queue has items → proceed to Step 2

2. **Email Processing Path** (if unmatched emails exist):
   - Check for unmatched emails in intake folder
   - Match emails to cases via docket numbers, case IDs
   - Extract email content for response drafting
   - Generate scorecard exhibits for evidence

3. **MCP Tool Availability Check** (CRITICAL):
   - `mcp_beezle_list_cases` → case enumeration (required for all workflows)
   - `mcp_beezle_filing_queue_list` → queue status (required for email processing)
   - `chain_run` → email chain processing (MAY FAIL - see Pattern 14)
   - `violation_classify` → email violation classification (MAY FAIL - see Pattern 14)

4. **Fallback Logic**:
   - If MCP tools operational → proceed with full workflow
   - If chain_run/violation_classify unavailable → document "MCP TOOL UNAVAILABLE" and proceed with metadata-only analysis
   - If all MCP tools fail → use `mcp_beezle_case_brief` for case context instead

5. **Session Delivery Instructions**:
   - **CRITICAL:** If running as scheduled cron job, use [SILENT] delivery instruction
   - Format: "[SILENT]" at end of response suppresses auto-delivery
   - Never combine [SILENT] with content — either report normally OR say [SILENT] only
   - Document: "AUTOMATED DELIVERY - [SILENT] instruction enforced"

**Why This Pattern:**
- Email processing is a common automated cron task
- MCP tools may fail due to path/import issues in cron environments
- User delivery preferences must be respected (e.g., [SILENT] for cron jobs)
- Metadata-only fallback is acceptable when full extraction unavailable

**Related Reference:** `references/email-processing-workflow-2026-05-23.md`

### Pattern 16: pdftotext Empty Output (NEW - JUNE 21, 2026)

**Trigger:** pdftotext returns empty output for a PDF that was expected to have text

**Signature:** pdftotext exits with code 0 but produces no output

**Root Causes (distinct from tool unavailability):**
1. **Scanned image PDF** — no text layer, only raster images (requires OCR tools)
2. **Password-protected PDF** — cannot extract without password
3. **Corrupted PDF** — malformed file
4. **Empty PDF** — blank pages

**Distinction from Pattern 9 (Metadata-Only Fallback):**
- Pattern 9: Tools are MISSING entirely → metadata-only analysis
- Pattern 16: Tools are AVAILABLE but output is EMPTY → still requires OCR or visual inspection

**FIX:**
1. Document: "EMPTY OUTPUT - pdftotext returned no content (likely scanned PDF)"
2. Check if OCR sidecar exists for this file → use it
3. If no sidecar → file requires OCR tools or visual inspection
4. Do NOT treat this as "tools unavailable" — the tools are present, the PDF has no extractable text

**Document:** "EMPTY OUTPUT - pdftotext returned no content (likely scanned PDF or password-protected)"

### Pattern 17: Gate 5 BLOCKED → RESOLVED Transition (NEW - JUNE 21, 2026)

**Trigger:** Tool availability changes between cron runs (e.g., tools installed between runs)

**Signature:** Previous intake log shows BLOCKED status, but current run finds tools available

**FIX:**
1. Update the intake_analysis_log.md to reflect current tool availability
2. Change status from "BLOCKED - PDF extraction tools not available" to "RESOLVED — PDF extraction tools now AVAILABLE"
3. List each tool's current status (available/not installed)
4. Resume full PDF extraction using available tools

**Document:** "BLOCKED → RESOLVED - tool availability changed between runs"

### Pattern 18: PDF Binary Misnamed as .ocr.txt (NEW - JUNE 22, 2026)

**Trigger:** Processing `.ocr.txt` files in intake folder

**Signature:** `.ocr.txt` file contains PDF binary data, not OCR text content

**Root Cause:** PDF binaries with version-header format (e.g., `1.4\n%\n`, `1.6\r%\n`) are misnamed with `.ocr.txt` extension instead of `.pdf`. The `read_file` tool reads them as text and gets garbage PDF header data.

**Detection (CRITICAL — must check BEFORE treating as text sidecar):**
```python
# Check binary header of .ocr.txt file
with open(fpath, 'rb') as fp:
    header = fp.read(20)

# PDF version-header format: version number (e.g., "1.4") followed by newline and %
is_pdf = header[0:1].isdigit() and header[3:4] == b'\n' and header[4:5] == b'%'

# Standard PDF format: starts with %PDF-
is_standard_pdf = header[:5] == b'%PDF-'

if is_pdf or is_standard_pdf:
    # It's a PDF — use pdftotext, NOT read_file
    # Process via pdftotext terminal command
    pass
else:
    # It's a text sidecar — read as text
    pass
```

**PDF Header Formats to Detect:**
1. **Standard**: `b'%PDF-'` — starts with `%PDF-`
2. **Version-header (LF)**: `b'1.4\n%\n'` — version number, newline, percent sign (common in PDFs generated by Skia/PDF, Mozilla, Chrome)
3. **Version-header (CRLF)**: `b'1.6\r\n%\n'` — version number, carriage return + newline, percent sign (common in PDFs generated by Microsoft Word, Windows-based generators, Gmail/Outlook PDF export)

**CRITICAL:** Format 3 is NOT detected by the LF check alone. Always check for BOTH `\n` and `\r\n` version headers.

**Impact:** Without this check, the agent treats PDF binaries as text sidecars, reads garbage PDF header data, and cannot extract actual content.

**FIX:**
1. Check binary header of `.ocr.txt` file BEFORE reading as text
2. If PDF detected → use pdftotext (Pattern 16: may return empty for scanned PDFs)
3. If text detected → read as text sidecar (Pattern 8)
4. Document the detection method used

**Document:** "PDF BINARY DETECTED - [filename] is PDF binary, not OCR text sidecar"

**PATTERN STATUS:** PROVEN WORKFLOW — Mandatory for all `.ocr.txt` file processing

**REFERENCE:** `references/pdf-binary-ocr-txt-detection.md`

### Pattern 19: intake_analysis_log.md File Count Staleness (NEW - JUNE 26, 2026)

**Trigger:** Reading intake_analysis_log.md file counts during intake processing

**Signature:** intake_analysis_log.md reports a file count that does not match the actual intake directory count

**Root Cause:** intake_analysis_log.md file counts are inherently stale. New files accumulate in the intake directory between cron runs, but the log is only updated when a run processes files. The log may report 420 files while the actual directory contains 558 files.

**Detection:** Compare `total_files` or `Total PDFs in intake` from intake_analysis_log.md against the actual count from `ls intake/ | wc -l` or `os.listdir(intake_dir)`.

**Impact:** The agent may under-count unanalyzed files, incorrectly conclude intake is more complete than it is, or skip files that were added since the last run.

**FIX:**
1. ALWAYS verify file counts against the actual intake directory on each run
2. Do NOT trust intake_analysis_log.md file counts — use them only for status markers (INTAKE COMPLETE, BLOCKED, etc.)
3. Document the actual count: "intake_analysis_log.md reports N files; actual count: M files"
4. Process based on actual directory listing, not logged counts

**Document:** "FILE COUNT STALE - intake_analysis_log.md reports N files; actual count: M files"

**PATTERN STATUS:** PROVEN WORKFLOW — Mandatory for all intake processing runs

**REFERENCE:** `references/intake-file-count-staleness-20260626.md`

### Pattern 20: ECF Activity-in-Case PDFs Produce Non-Empty but Metadata-Only Output (NEW - JUNE 29, 2026)

**Trigger:** pdftotext returns non-empty output for a PDF that was expected to have substantive text

**Signature:** pdftotext exits with code 0, produces output, but all output consists of repeating page headers only (e.g., "Case {{CASE_ID}} / Document 146 / Filed 06/22/26 / Page 1 of 14")

**Root Cause:** These are ECF "Activity in Case" reports — PDFs generated by PACER/ECF that contain only page headers repeated per page, with no substantive text content. The pdftotext output is NOT empty (unlike Pattern 16), but it is still metadata-only.

**Distinction from Pattern 16 (Empty Output):**
- Pattern 16: pdftotext returns EMPTY (0 bytes) — scanned PDFs with no text layer
- Pattern 20: pdftotext returns NON-EMPTY output (repeating page headers) — ECF metadata-only PDFs

**Distinction from Substantive Content:**
- Substantive PDF: output contains varied text (case names, dates, amounts, legal text, etc.)
- Metadata-only: output is ONLY repeating patterns like "Case X / Document Y / Filed Z / Page W of V"

**Detection (CRITICAL — must check BEFORE treating as useful content):**
```python
# After running pdftotext, check if output is metadata-only
content = subprocess.run(['pdftotext', fpath, '-'], capture_output=True, text=True).stdout
lines = content.strip().split('\n')

# Filter out blank lines
non_blank = [l for l in lines if l.strip()]

# If ALL non-blank lines match the pattern "Document N / Filed D / Page P of Q", it's metadata-only
import re
doc_pattern = re.compile(r'^Document\s+\d+$')
file_pattern = re.compile(r'^Filed\s+\d{2}/\d{2}/\d{2}$')
page_pattern = re.compile(r'^Page\s+\d+\s+of\s+\d+$')

header_lines = sum(1 for l in non_blank if doc_pattern.match(l.strip()) or file_pattern.match(l.strip()) or page_pattern.match(l.strip()))

if header_lines > len(non_blank) * 0.8:
    # >80% of lines are page headers — metadata-only
    print("METADATA-ONLY ECF OUTPUT — skip substantive analysis")
else:
    # Has substantive text — process normally
    pass
```

**Impact:** Without this check, the agent treats the non-empty pdftotext output as useful content, wasting processing cycles and potentially drawing false conclusions from page headers.

**FIX:**
1. After pdftotext returns non-empty output, check if >80% of non-blank lines are page headers
2. If metadata-only → document: "METADATA-ONLY ECF OUTPUT — [filename] contains only page headers"
3. Do NOT extract "facts" from page headers
4. Skip substantive analysis of this file
5. Note: PDF metadata fields (title, author, creation date) may contain docket event descriptions — requires pdfplumber metadata extraction, not pdftotext

**Document:** "METADATA-ONLY ECF OUTPUT - [filename] pdftotext returned non-empty content but all lines are page headers (Document X / Filed Y / Page Z of W)"

**PATTERN STATUS:** PROVEN WORKFLOW — Mandatory for all ECF "Activity in Case" file processing

**REFERENCE:** `references/ecf-activity-in-case-metadata-only-20260629.md`

### Pattern 22: JPEG MPO Format — PIL Cannot Read Directly (NEW - JULY 24, 2026)

**Trigger**: Processing `.jpeg`/`.jpg` files in intake folder

**Signature**: PIL/Pillow raises `Unsupported image format/type` when opening the file

**Root Cause**: iPhone stereo photos use MPO (Multi-Picture Object) format, not standard JPEG. PIL cannot read MPO format directly.

**Detection**:
```python
from PIL import Image
img = Image.open(fpath)
if img.format == 'MPO':
    # MPO format — cannot process with PIL
```

**FIX**:
1. Convert MPO to standard JPEG using PIL: `img.save('/tmp/converted.jpg', 'JPEG')`
2. Then run tesseract on the converted file: `subprocess.run(['tesseract', '/tmp/converted.jpg', 'stdout', '--psm', '3'])`
3. **CRITICAL**: Validate OCR output for meaningful content — search for dates (YYYY-MM-DD), amounts ($X,XXX.XX), and names (Plaintiff, Defendant, Court). If none found, OCR is garbled.
4. Document: "JPEG MPO CONVERTED — OCR quality may be degraded"

**Caveat**: Even after conversion, OCR quality may be poor for complex layouts or low-resolution photos.

**CRITICAL NOTE**: The old guideline "JPEG files (>2MB) — use pdfplumber, NOT PyMuPDF" is WRONG — pdfplumber processes PDFs, not JPEGs. JPEGs require OCR tools (tesseract), not PDF tools.

**REFERENCE**: `references/jpeg-mpo-ocr-handling.md`

**OBSERVED**: All 24 MPO JPEGs across root intake and extracted ZIP produced garbled OCR — zero meaningful content (no dates, no amounts, no names) in any OCR output. See `references/mpo-jpeg-ocr-garbled-output-20260725.md`.

### Pattern 21: Fake PDF Files in Intake Root (NEW - JULY 20, 2026)

**Trigger:** Processing PDF files in the intake root directory

**Signature:** PDF file is extremely small (19-33 bytes) and contains non-PDF content when read as binary

**Root Cause:** Test files or other non-PDF content was saved with a `.pdf` extension. The file is NOT a valid PDF — it contains plain text like "FAKE TEST FILE for routing test" or "FAKE {{JUDGE}} order".

**Detection (CRITICAL — must check BEFORE treating as PDF):**
```python
# Check binary header of .pdf file BEFORE reading as PDF
with open(fpath, 'rb') as fp:
    header = fp.read(100)

# Standard PDF format: starts with %PDF-
is_standard_pdf = header[:5] == b'%PDF-'

# Version-header PDF (less common): version number followed by newline and %
is_version_pdf = header[0:1].isdigit() and header[3:4] == b'\n' and header[4:5] == b'%'

# Tiny file (under 100 bytes) — almost certainly not a real PDF
is_tiny = os.path.getsize(fpath) < 100

if not (is_standard_pdf or is_version_pdf) or is_tiny:
    # Not a real PDF — read as text
    print(f"FAKE PDF DETECTED - [filename] is not a real PDF file")
    # Read as text sidecar instead
    pass
else:
    # Process as PDF via pdftotext
    pass
```

**PDF Header Formats to Detect:**
1. **Standard**: `b'%PDF-'` — starts with `%PDF-`
2. **Version-header (LF)**: `b'1.4\n%\n'` — version number, newline, percent sign
3. **Version-header (CRLF)**: `b'1.6\r\n%\n'` — version number, carriage return + newline, percent sign

**Impact:** Without this check, the agent attempts to process the file as a PDF (via pdftotext, PyMuPDF, or pdfplumber), wasting processing cycles and potentially producing garbage output from non-PDF content.

**FIX:**
1. Check binary header of `.pdf` file BEFORE attempting PDF extraction
2. If not a real PDF → read as text sidecar (Pattern 8)
3. Document the detection method used

**Document:** "FAKE PDF DETECTED - [filename] is not a real PDF file, read as text sidecar instead"

**PATTERN STATUS:** PROVEN WORKFLOW — Mandatory for all `.pdf` file processing in intake

**REFERENCE**: `references/fake-pdf-detection.md`

### Pattern 23: Miscategorized Intake Files (NEW - JULY 27, 2026)

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

**REFERENCE**: `references/miscategorized-intake-files-20260727.md`

### Pattern 23: Miscategorized Intake Files (NEW - JULY 27, 2026)

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

**REFERENCE**: `references/pattern-23-miscategorized-intake-files.md`

### Pattern 24: .doctext.txt Sidecar Priority (NEW - JULY 28, 2026)

**Trigger**: Processing intake files where both `.doctext.txt` and `.ocr.txt` sidecars exist for the same PDF

**Signature**: `filename.pdf.doctext.txt` AND `filename.pdf.ocr.txt` both present

**Root Cause**: Different intake systems generate different sidecar formats. The `.doctext.txt` file contains the full substantive text (from PyMuPDF's `.to_text()` extraction), while the `.ocr.txt` may contain only metadata or truncated content.

**Impact**: Without this check, the agent reads `.ocr.txt` first and gets incomplete content, missing critical substantive text available in `.doctext.txt`.

**FIX**:
1. Check for BOTH `.doctext.txt` and `.ocr.txt` sidecars for each PDF
2. When both exist → read `.doctext.txt` FIRST (contains full substantive text)
3. Only fall back to `.ocr.txt` if `.doctext.txt` does not exist
4. Document: "SIDECAR PRIORITY — .doctext.txt read (full text); .ocr.txt skipped (metadata-only)"

**Document**: "SIDECAR PRIORITY — .doctext.txt contains full substantive text; .ocr.txt may be metadata-only"

**PATTERN STATUS**: PROVEN WORKFLOW — Mandatory for all intake files with both sidecar types

**REFERENCE**: `references/fed-{{CASE_ID}}-jop-findings-20260728.md`

### Pattern 25: Parallel JOP Filing Pattern (NEW - JULY 28, 2026)

**Trigger**: When TransUnion files a JOP (FRCP 12(c)) against a pro se FCRA plaintiff

**Signature**: TransUnion files JOP → GreenSky files parallel JOP within ~2 weeks

**Root Cause**: Defendants share counsel and litigation strategy. TransUnion's JOP rationale (no factual inaccuracy alleged, threadbare allegations under Iqbal) is adopted verbatim by GreenSky.

**Impact**: When reviewing intake files, finding a TransUnion JOP means GreenSky JOP is likely imminent or filed. Both should be read together to understand the full defense posture.

**FIX**:
1. When TransUnion JOP found → check for GreenSky JOP in same intake folder
2. Read both JOPs together — they share the same legal rationale
3. Document: "PARALLEL JOP — TransUnion and GreenSky both filed JOP; same legal rationale"
4. Track both as potential upcoming court rulings to anticipate

**Document**: "PARALLEL JOP — TransUnion and GreenSky both filed JOP; same legal rationale"

**PATTERN STATUS**: PROVEN WORKFLOW — Mandatory for all intake files with both JOPs present

**REFERENCE**: `references/fed-{{CASE_ID}}-jop-findings-20260728.md`

### Pattern 26: intake_analysis_log.md Creation from Scratch (NEW - JULY 31, 2026)

**Trigger**: First intake processing run where `intake_analysis_log.md` does not exist

**Signature**: `read_file` returns "File not found" for `intake_analysis_log.md`

**Root Cause**: The canonical intake status tracker is created only after the first run. Before that, there is no log to check for "INTAKE COMPLETE" or file counts.

**FIX**:
1. Create the file with a header documenting the first run
2. Include: run date, files processed count, run type, tool availability summary
3. List each file processed with its pattern classification
4. List files not yet processed
5. Document key findings (new filings, discrepancies, etc.)
6. Append one line to `learnings.jsonl` per the doctrine (CRON_DOCTRINE.md Rule 7)

**Document**: "NEW LOG — intake_analysis_log.md created from scratch (first run)"

**PATTERN STATUS**: PROVEN WORKFLOW — First-run initialization

**REFERENCE**: `references/intake-analysis-log-creation.md`

### Pattern 27: learnings.jsonl Write Pattern (NEW - JULY 31, 2026)

**Trigger**: Intake processing or any cron job that produces durable findings

**Doctrine**: CRON_DOCTRINE.md Rule 7 — write to topic log AND append one line to learnings.jsonl

**Format**:
```json
{"text": "<SHORT grounded finding + which active case(s) it bears on>", "cite": "<controlling authority + corpus coordinate: source doc / chunk id>", "tags": ["tag1", "tag2"], "source": "<job-name>", "cases": ["case-id-1", "case-id-2"]}
```

**CRITICAL**: The JSONL line is what `pipeline/connected_notes.py` turns into a linked atomic note in the graph. A note written only under `studies/` or `comprehension/` without the JSONL line risks staying blind.

**Always name the active case(s) the finding applies to** — read meta/CASE_GROUND_TRUTH.yaml to get the canonical case ID.

**Document**: "LEARNING WRITTEN — appended to learnings.jsonl"

**PATTERN STATUS**: PROVEN WORKFLOW — Mandatory for all cron jobs producing durable findings

**REFERENCE**: `references/learnings-jsonl-write-pattern.md`

### Pattern 9: Metadata-Only Analysis Fallback (NEW - MAY 18, 2026)
**Trigger**: PDF extraction tools unavailable (Gate 5 failure)
**Signature**: Cannot install tools OR no time to install
**Impact**: Full text extraction blocked
**FIX**:
  1. Document blockers: "BLOCKED - PDF extraction tools unavailable"
  2. Proceed with metadata-only analysis (file size, naming patterns, keywords)
  3. Extract entity names from filenames (CRA, furnisher, dates, amounts)
  4. Update `intake_analysis_log.md` with "METADATA-ONLY ANALYSIS" status
  5. Document: "AWAITING PDF TOOLS FOR FULL CONTENT EXTRACTION"
**Document**: "METADATA-ONLY ANALYSIS - tools unavailable, awaiting installation"

### Pattern 10: Intake Complete Skip Logic (CANONICAL - MAY 18, 2026)
**Trigger**: Intake processing task with existing analysis
**Signature**: `intake_analysis_log.md` AND `CASE_FACTS.md` both have "INTAKE COMPLETE" marker
**Impact**: Unnecessary file scanning; can skip directly to drafting
**FIX**: 
  1. Check `intake_analysis_log.md` for file counts and last update timestamp
  2. Check `CASE_FACTS.md` for "INTAKE COMPLETE" or "NEW FACTS FROM INTAKE ANALYSIS" section
  3. If both present → skip file analysis, proceed to drafting
  4. If only `intake_analysis_log.md` exists → process remaining files
  5. If neither exists → process files normally
**Document**: "INTAKE COMPLETE - skip file analysis, proceed to drafting"
**REFERENCE**: `intake_analysis_log.md` is the canonical status tracker for all intake processing runs
**PATTERN STATUS**: PROVEN WORKFLOW - Use for all scheduled cron intake processing

### Pattern 11: Empty OCR Sidecar Validation (NEW - MAY 20, 2026)
**Trigger**: Processing OCR sidecar files (.ocr.txt, .txt)
**Signature**: Sidecar files exist but contain only filename metadata, no actual document text
**Example**: File contains `"PDF file - 1.pdf (539884 bytes)"` but NO case facts, dates, parties, or allegations
**Impact**: False assumption of available OCR extraction; cannot extract facts without PDF tools
**FIX**:
  1. Read sidecar file with `read_file`
  2. Check for actual content: dates (YYYY-MM-DD), names (Plaintiff, Defendant), allegations, amounts
  3. If only metadata (filenames, file sizes) → cannot proceed without PDF tools
  4. Document: "EMPTY OCR SIDECAR - no extractable content"
  5. Proceed with Pattern 9 metadata-only analysis OR install PDF tools
**Document**: "EMPTY OCR SIDECAR - [filename] contains only metadata, no extractable facts"
**PATTERN STATUS**: PROVEN WORKFLOW - Mandatory OCR content validation before extraction

### Pattern 12: Terminal Approval Blocking (NEW - MAY 20, 2026)
**Trigger**: Attempting to run Python commands via terminal for tool verification
**Signature**: `sudo apt-get install` or `pip3 install` commands blocked by terminal approval prompt
**Example**: Terminal waits for user confirmation; automated cron job cannot proceed
**Impact**: Full extraction tools cannot be installed; stuck with metadata-only analysis
**FIX**:
  1. Document the blocker: "BLOCKED - Terminal approval required for tool installation"
  2. Proceed with Pattern 9 metadata-only analysis
  3. Manual intervention required: User must approve tool installation or provide alternative extraction method
  4. Document: "AWAITING MANUAL INTERVENTION FOR TOOL INSTALLATION"
**Document**: "TERMINAL BLOCKED - manual approval required for tool installation"
**PATTERN STATUS**: PROVEN WORKFLOW - Terminal constraints for automated intake runs

### Pattern 13: Scheduled Cron Job Execution Pattern (NEW - MAY 20, 2026)
**Trigger**: Scheduled cron intake processing workflow
**Purpose**: Document the proven workflow for automated, scheduled intake processing
**Key Constraint**: CANNOT receive terminal approval for tool installation; MUST check sidecars FIRST; MUST accept metadata-only fallback
**References**: `references/scheduled_cron_job_execution.md`
**PATTERN STATUS**: PROVEN WORKFLOW - CRON DEFAULT BEHAVIOR

### Pattern 14: Pattern Numbering Cleanup (MAY 21, 2026)
**Note**: Previous duplicate entries consolidated. The patterns above (8-13) are now the canonical set for intake processing workflows.

### PDF Extraction Tools
**Available Methods (in order of priority):**
1. `.txt` sidecar file exists → read it (fastest)
2. `pdftotext` via terminal → if `sudo apt-get install poppler-utils` available
3. PyMuPDF via python3 → if `pip3 install PyMuPDF` available
4. pdfplumber via python3 → if `pip3 install pdfplumber` available

**When Blocked**:
- No `.txt` sidecar exists AND extraction tools not available
- **FIX**: `sudo apt-get install poppler-utils` for pdftotext, or `pip3 install PyMuPDF` for PyMuPDF
- **Document**: "BLOCKED - PDF extraction tools not available"

### DOCX Extraction Tools
**Available Methods**:
1. `python-docx` via python3 → if `pip3 install python-docx` available
2. Manual XML parsing (last resort)

**When Blocked**:
- No `.txt` sidecar exists AND python-docx not available
- **FIX**: `pip3 install python-docx`
- **Document**: "BLOCKED - DOCX extraction tools not available"

**RECOMMENDATION**: Always check for `.txt` sidecar files FIRST before attempting extraction. This avoids tool dependency issues and speeds up processing.

---

## GATE ENFORCEMENT CHECKLIST

**Quick Reference**: See `references/gate-enforcement-checklist.md` for the complete Gate 0-5 verification checklist with workflow type summaries and enforcement rules.

### GATE ENFORCEMENT CHECKLIST SUMMARY

**Gate 0 (Session Log)**: Required for drafting workflows. Verify `_hermes_log_m1_<timestamp>.log` exists with `read_obsidian_note` and `defense-simulator` skill loads.

**Gate 1 (CASE_FACTS Alignment)**: Required for ALL workflows. Verify CASE_FACTS.md was read and draft rules match "Correct rule" field. 60(b)(4) only for jurisdictional defects.

**Gate 2 (Defense Simulator)**: Required for drafting workflows. `defense-simulator` MUST be loaded before drafting. All MODERATE+ attacks must have rebuttals.

**Gate 3 (Rebuttal Generator)**: Required when MODERATE+ attacks found. `rebuttal-generator` must be invoked and rebuttals integrated.

**Gate 4 (Intake Verification)**: Required for intake workflows. Check `intake_analysis_log.md` and CASE_FACTS "INTAKE COMPLETE" marker. Sidecar-first extraction (Pattern 8).

**Gate 5 (Environment Constraints)**: Required for file extraction. Check sidecars first, then tool availability (pdftotext, PyMuPDF, pdfplumber, python-docx, tesseract, pytesseract, PIL). **CRITICAL**: Check `.ocr.txt` files for PDF binary header before treating as text (Pattern 18). Check `.pdf` files for fake PDF (Pattern 21). For JPEG files, check PIL format first — MPO cannot be read by PIL (Pattern 22). Document blockers.

**Critical Patterns**: Pattern 5 (Gate Sequence), Pattern 6 (Argument Priority), Pattern 7 (CASE_FACTS Alignment), Pattern 8 (Session Log), Pattern 11 (Empty OCR), Pattern 12 (Terminal Blocking), Pattern 13 (Cron Execution), Pattern 18 (PDF Binary Detection), Pattern 22 (JPEG MPO).

**Workflow Summary**:
- Intake-Only: Gates 1,4,5 (skip G0,G2,G3)
- Drafting: Gates 0,1,2,3 (skip G4,G5)
- Hybrid: All gates

**CRON EXECUTION DEFAULT**: Pattern 13 applies to all scheduled cron intake runs:
1. Check sidecars FIRST (Pattern 8)
2. **CRITICAL**: Check `.ocr.txt` files for PDF binary header (Pattern 18)
3. Validate OCR content (Pattern 11)
4. If blocked, use metadata-only analysis (Pattern 9)
5. If terminal blocked, document and proceed with metadata-only (Pattern 12)
6. DO NOT attempt tool installation without manual approval
7. **AUTOMATED CONTEXT RULE**: Tool installation is FORBIDDEN. If Gate 5 fails in automated context, immediately proceed with metadata-only analysis.

**Session Results** (2026-05-21):

- `intake-scanning` skill - Complete nightly pipeline workflow (Pattern 8, 9, 10, 11, 12, 13)
- `pre-draft-legal-guardrail` skill - Pre-draft legal guardrails (Gates 0-5)
- `AGENTS.md` Section 2 - MANDATORY RETRIEVAL SEQUENCE (INDEX.md → CASE_FACTS.md → TIMELINE → intake → ChromaDB)
- `AGENTS.md` Section 3A - MULTI-CHAIN REQUIREMENT (Procedural/Jurisdictional/Constitutional chains)
- `AGENTS.md` Section 3B - AFFIRMATIVE RELIEF DISCOVERY (Default judgment FRCP 55, SJ FRCP 56, Sanctions)
- `AGENTS.md` Section 3C - FULL CASE FILING SLATE (Pending defensive, affirmative, procedural motions)
- `intake_analysis_log.md` - Canonical intake analysis status (Pattern 10 skip logic)
- `references/sidecar-first-extraction.md` - Pattern 8: Sidecar-first workflow
- `references/metadata-only-analysis.md` - Pattern 9: Metadata-only fallback
- `references/empty-ocr-sidecar-validation.md` - Pattern 11: OCR content validation
- `references/terminal-approval-blocking.md` - Pattern 12: Terminal constraints
- `references/scheduled-cron-job-execution.md` - Pattern 13: Cron execution workflow
- `references/intake-complete-skip-logic.md` - Pattern 10: Intake complete detection
- `references/gate-enforcement-checklist.md` - Gate 0-5 verification
- `CASE_FACTS.md` - Case facts and posture (must be read before any task)
- `_hermes_log_m1_<timestamp>.log` - Session log (Gate 0 verification for drafting workflows)

## CRON EXECUTION SUMMARY (MAY 22, 2026)

**Proven Workflow Pattern 13**: All scheduled cron intake runs follow this sequence:
1. **Health Check**: Verify MCP tools operational (mcp_beezle_list_cases, mcp_beezle_filing_queue_list)
2. **Sidecar-First**: Check for .txt sidecars before any extraction attempt (Pattern 8)
3. **OCR Validation**: Read sidecars and validate for actual content vs metadata-only (Pattern 11)
4. **Tool Verification**: Check pdftotext, PyMuPDF, pdfplumber, python-docx availability (Gate 5)
5. **Fallback Logic**: 
   - If sidecar has content → use it
   - If sidecar empty AND tools available → install tools
   - If sidecar empty AND tools unavailable → proceed with metadata-only analysis (Pattern 9)
   - If terminal approval required → document blocker, proceed with metadata-only (Pattern 12)
6. **Intake Complete Check**: Verify intake_analysis_log.md and CASE_FACTS.md markers (Pattern 10)
7. **Processing**: 10 files per run, append new findings to existing log

**Session Results** (2026-05-22):
- Cases reviewed: 7
- INTAKE COMPLETE: 2 cases ({{CASE_SLUG}}, {{CASE_SLUG}}, {{CASE_ID}})
- Cases processed: 4 (fed_26-cv-03268, {{CASE_SLUG}}, {{CASE_SLUG}}, {{CASE_SLUG}})
- MCP tools operational: YES (mcp_beezle_list_cases validated)
- Filing queue status: 10 pending filings (1 urgent, 9 soon)
- Sidecar validation: Sidecar-first workflow enforced
- Pattern compliance: 100% (Patterns 8, 9, 10, 13)

**Quality Gates Enforced**:
- Pattern 8: Sidecar-first extraction (verified for cases with sidecars)
- Pattern 9: Metadata-only analysis (enforced where tools unavailable)
- Pattern 10: Intake complete skip logic (applied to existing complete cases)
- Pattern 12: Terminal approval blocking (documented and enforced)
- Pattern 13: Cron execution pattern (validated)

**Key Learning**: MCP tools are OPERATIONAL for case enumeration (mcp_beezle_list_cases). Filing queue MCP tool requires additional validation. Automated runs MUST assume metadata-only fallback is acceptable for unprocessed cases.

## FILING QUEUE HEALTH CHECK PATTERN (2026-05-22)

**Trigger**: Scheduled health check, daily queue review, or when MCP tools operational status changes

**Required MCP Tool**:
1. `mcp_beezle_filing_queue_list` → Get queue status (pending, ready, filed, confirmed, cancelled)

**Health Check Output**:
```
mcp_beezle_filing_queue_list
Expected output:
- count: number of pending filings
- urgent count: filings with priority "urgent"
- soon count: filings with priority "soon"
- overdue count: filings past deadline
```

**Thresholds**:
- Urgent > 2: Alert
- Overdue > 0: Critical alert
- Queue > 20: Warning

**Session Results** (2026-05-22):
- 10 pending filings
- 1 urgent ({{CASE_ID}} motion - 3 days left)
- 9 soon priority (various motions/briefs due May 26 - June 2)

**Action Required**: Add `mcp_beezle_filing_queue_list` to health check pattern.

---

*Last updated: 2026-07-25*
**Changes**: 
- Pattern 22 reference updated: OCR output for MPO-converted JPEGs observed to be consistently garbled/non-Latin (all 24 MPO JPEGs processed). Added recommended psm modes (4, 6, 11) and retry guidance.
- Added reference file: mpo-jpeg-ocr-garbled-output-20260725.md
- Added reference file: miscategorized-intake-files-20260727.md — detection pattern for files with misleading filenames that don't match their actual case source
- Updated version to 3.11