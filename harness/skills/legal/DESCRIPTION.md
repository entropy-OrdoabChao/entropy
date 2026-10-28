---
name: legal-drafter
description: Legal document drafting and case analysis. Use this skill when the user asks to draft complaints, motions, briefs, appeals, or any legal filing. Also triggers on FOIA, Privacy Act, FCRA, insurance bad faith, mortgage fraud, or any lawsuit-related task.
license: Proprietary
---

# Legal Drafter Skill v2

## HARD GATES (NEVER SKIP)

### GATE 1: CLASSIFICATION
- Call `discover_case_type` BEFORE any drafting
- IF case_type is UNKNOWN or confidence < 0.7 ? STOP and ask ONE question
- DO NOT override classification with your own reasoning
- DO NOT proceed to drafting without a locked case type
- DO NOT say "I see intake failed, let me just draft anyway"

### GATE 2: ELEMENTS
- After classification, query DB for statutory elements
- Check if uploaded documents satisfy each element
- IF all elements satisfied ? proceed to draft (DO NOT ask more questions)
- IF elements missing ? ask ONLY for the missing element

### GATE 3: TEMPLATE
- Every case type has a template structure
- Lock the template BEFORE drafting begins
- DO NOT change template mid-draft
- DO NOT mix templates from different case types

## DRAFTING RULES (ABSOLUTE)

### NO NARRATIVE
- DO NOT explain why something is a violation
- DO NOT cite case law unless the user provides it or DB returns it
- DO NOT use phrases like "Under established precedent..."
- DO NOT summarize legal theories

### IF/THEN ONLY
Every claim MUST follow this exact structure:
COUNT [N] ? [CAUSE OF ACTION]
[statute]
I. CONTROLLING LAW
This Court is bound by:

[statute]: [mandatory language with "shall/must"]

II. MANDATORY DUTIES

Defendant S{{JUDGE}} [duty]. [citation]

III. PRIMA FACIE ELEMENTS
(1) [Element]
IV. UNDISPUTED FACTS (RECORD-ANCHORED)

[Fact] (Ex. [letter])

V. ELEMENT-BY-ELEMENT APPLICATION
IF [legal requirement]
AND [fact from record]
THEN Element (N) is satisfied.
VI. BREACH
Duty violated: [specific]
Conduct: [what defendant did/failed]
This constitutes a breach as a matter of law.
VII. DECISION TRIGGER
IF [duties apply] AND [elements satisfied]
THEN Court S{{JUDGE}} [action].
No discretionary determination required.
VIII. RELIEF REQUESTED

[relief]


### FORBIDDEN BEHAVIORS
- Drafting when classification is uncertain
- Switching case types mid-draft  
- Proceeding without template lock
- Adding case law the user did not request
- Asking questions documents already answer
- Questioning the damages amount
- Mixing unrelated cases into a single-issue complaint
- Using phrases: "Let me explain", "The reason is", "It's important to note"

## TOOL CHAIN (EXECUTE IN ORDER)

### Step 1: Read Documents
Extract facts, timeline, parties, violations from uploaded files.
Build exhibit index: {letter: filename, type, date}

### Step 2: Classify Case
Call `discover_case_type` with situation description.
IF match found ? LOCK case type.
IF no match ? STOP, ask user to confirm.

### Step 3: Check Elements
Call `query_legal_db` for statute elements.
Map extracted facts to each element.
IF all elements satisfied from documents ? set auto_draft = true
IF elements missing ? ask ONE targeted question per missing element

### Step 4: Build Structured Data
Build complete JSON BEFORE any text generation:
{
  "case_type": "foia",
  "court": "federal_district",
  "plaintiff": {...},
  "defendants": [...],
  "timeline": [...],
  "counts": [
    {"title": "...", "statute": "...", "duties": [...], "elements": [...], "facts": [...], "relief": [...]}
  ],
  "exhibits": [...]
}

### Step 5: Draft Each Count
Call LLM ONCE per count with ONLY that count's data.
Use IF/THEN template. No narrative.

### Step 6: Assemble
Merge: caption + jurisdiction + parties + facts + counts + prayer + verification + cert of service + exhibit list
Output as ONE document.

## DEFENDANT ROLES BY COUNT

- FOIA (5 U.S.C. 552) ? Agency is defendant
- Privacy Act (5 U.S.C. 552a) ? United States is liable
- Mandamus (28 U.S.C. 1361) ? Individual officials in official capacity

## ADMISSION RULE
IF defendant admits violation in any document
THEN liability is established as a matter of law
AND elevate this admission to the top of every count it applies to

## COMMON CASE TYPES (always query DB for more)
- FOIA: 5 U.S.C. 552 + Privacy Act 552a + Mandamus 1361
- FCRA: 15 U.S.C. 1681
- Insurance Bad Faith: 42 Pa.C.S. 8371
- Mortgage Fraud: TILA, RESPA
- Due Process: 14th Amendment

## GENERAL CHAT
When user asks non-legal questions, respond normally.
Only apply legal rules when user asks about law, cases, or drafting.
