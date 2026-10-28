---
name: baseline-memorandum
description: Generate a Standing Legal Notice and Memorandum of Law that establishes controlling authority, procedural requirements, definition control, and mandatory duties for a case. Attaches to every filing. Pre-labels violations for scorecard and appellate tracker integration.
version: 1.0.0
metadata:
  hermes:
    tags: [legal, memorandum, notice, procedure, baseline, enforcement, filing]
    requires_toolsets: [terminal]
---

# Baseline Memorandum — Standing Legal Notice

Generate a persistent procedural enforcement document that travels with every case. This is NOT argument — it is notice. It establishes:

1. What law controls
2. What procedures are mandatory
3. What definitions apply
4. What happens when rules are violated

## When to Use

- At the START of any new case or matter
- When filing a complaint, answer, or initial motion
- When you need to establish the rules of engagement
- When opposing counsel or court is not following procedure
- Attach to every major filing as an exhibit or appendix

## What This Document Does

It does NOT force compliance. It **creates a record that compliance was required**.

That record feeds:
- `legal-scorecard` — every rule becomes a scorable violation
- `appellate-violation-tracker` — every ignored requirement is logged
- `legal-draft` — later filings reference this memorandum

## Document Structure

### SECTION 1 — Statement of Standing & Notice

```
This Memorandum of Law is filed to establish controlling authority, procedural
requirements, and binding obligations applicable to all parties and this Court.

All parties are hereby on notice that:
1. The controlling authority identified herein governs this proceeding.
2. Mandatory procedural requirements must be satisfied.
3. Failure to comply will be documented and preserved for appellate review.
```

### SECTION 2 — Governing Authority

Populated from `law-lookup` and `authority-hierarchy-check`:

```
This matter is governed by:

CONSTITUTIONAL:
  [U.S. Const. Amend. XIV — Due Process]
  [PA Const. Art. I § 11 — Open Courts]

STATUTORY:
  [Primary statute — e.g., 42 Pa.C.S.A. § 8371]
  [Secondary statutes — e.g., 73 P.S. § 201-1]

PROCEDURAL:
  [FRCP Rules / Pa.R.C.P. Rules applicable to this case]

BINDING CASE LAW:
  [Key controlling cases]

No party may rely on policy, custom, or discretionary practices that
contradict the controlling authority identified above.
```

### SECTION 3 — Mandatory Procedural Requirements

Populated from `legal-analysis` and `procedural-attack-builder`:

```
The following procedural requirements are mandatory in this matter:

1. VERIFICATION: All filings containing averments of fact must be verified
   under oath where required by [Pa.R.C.P. 1024 / applicable rule].

2. AUTHORITY TO ACT: Any claim, defense, or assertion made on behalf of a
   corporate entity must be supported by a statement from a competent
   representative with personal knowledge.

3. NOTICE OF APPEARANCE: Attorneys must file proper notice of appearance
   in accordance with [governing rule].

4. SERVICE: All filings must be served in compliance with [FRCP 4 / Pa.R.C.P. 400].

5. CERTIFICATION: All filings must comply with [FRCP 11 / applicable rule]
   certification requirements.

6. TIMELINESS: All deadlines established by rule or court order are mandatory.
   [List specific deadlines if known]

Any document submitted without required verification, authority, or proper
service shall be treated as procedurally defective.
```

### SECTION 4 — Definition Control

Populated from `definition-lookup`:

```
All terms used in this proceeding are governed by their controlling
statutory definitions as follows:

TERM: [term]
DEFINITION: [statutory definition text]
SOURCE: [citation]
SCOPE: [applies within Title X only]

Where a statute defines a term, that definition controls exclusively
within its scope. No party may substitute dictionary, colloquial, or
alternative definitions where a statutory definition exists.
```

### SECTION 5 — Mandatory Duty Identification

Populated from `rule-chain-builder`:

```
The following mandatory duties are identified in the controlling authority:

1. [Party] S{{JUDGE}} [duty] — [citation] — MANDATORY
2. [Party] MUST [duty] — [citation] — MANDATORY
3. [Party] is PROHIBITED from [action] — [citation] — MANDATORY

Terms "shall", "must", and "required" impose non-discretionary obligations.
Failure to comply constitutes a violation of governing law, not a matter
of judicial discretion.
```

### SECTION 6 — Pre-Labeled Violations

These feed directly into `legal-scorecard`:

```
Non-compliance with the requirements stated herein will constitute:

PROCEDURAL VIOLATIONS:
  - Filing without required verification → [Pa.R.C.P. 1024]
  - Failure to serve properly → [FRCP 4 / Pa.R.C.P. 400]
  - Filing without Rule 11 basis → [FRCP 11]

SUBSTANTIVE VIOLATIONS:
  - [Statute-specific violations identified in analysis]

DUE PROCESS VIOLATIONS:
  - Denial of notice → [U.S. Const. Amend. XIV]
  - Denial of opportunity to respond → [Mullane]

Each violation will be:
  1. Documented with record citation
  2. Scored using the legal violation scorecard
  3. Preserved for appellate review
  4. Tracked by judge and case number
```

### SECTION 7 — Reservation of Rights

```
All rights, claims, defenses, and procedural rights are expressly reserved.
Nothing in this memorandum constitutes a waiver of any right or claim.
This notice is filed to preserve the record and ensure compliance with
controlling authority.
```

## Procedure

### 1. Generate the Memorandum

Before drafting any filing, run the pipeline through Step 7 (authority validation) to identify:
- Governing law (Step 2)
- Definitions (Step 5)
- Controlling authority (Step 7)
- Mandatory duties (from rule extraction)

Then generate the memorandum using the template above, filling in case-specific authority.

### 2. Attach to Filing

The memorandum is attached as:
- **Exhibit A** to a complaint or motion, OR
- **Appendix** to any filing, OR
- **Standalone filing** as "Memorandum of Law and Procedural Notice"

### 3. Reference in Later Filings

All subsequent filings should reference:
```
As established in the Standing Legal Notice and Memorandum of Law
filed on [date] (Docket Entry [X]), the following mandatory requirements
were identified...
```

### 4. Feed Into Scorecard

Each procedural requirement in Section 3 becomes a scorecard rule:

```json
{
  "rule": "verification required",
  "authority": "Pa.R.C.P. 1024",
  "violated": true,
  "record_cite": "Docket Entry 15 — unverified filing",
  "category": "procedure"
}
```

### 5. Feed Into Appellate Tracker

```bash
python3 ~/.hermes/skills/legal/appellate-violation-tracker/scripts/tracker.py log \
  --judge "Judge Name" \
  --case "2024-CV-12345" \
  --violation "Accepted unverified filing despite memorandum notice" \
  --authority "Pa.R.C.P. 1024" \
  --record "Docket Entry 15"
```

## Integration with Pipeline

```
Steps 1-7 (identify law, definitions, authority)
    ↓
baseline-memorandum (generate standing notice)  ← THIS SKILL
    ↓
Attach to filing
    ↓
Steps 8-14 (analysis, chain, scorecard, draft)
    ↓
Later filings reference memorandum
    ↓
Violations tracked against memorandum requirements
```

## Anti-Hallucination

- Every section MUST be populated from retrieved authority, not training knowledge
- If a section cannot be populated (no authority found), mark it: "TO BE SUPPLEMENTED — authority not yet identified"
- Do NOT generate procedural requirements that don't exist in the governing rules

## Verification

A complete memorandum has:
- Every governing authority cited with full citation
- Every definition traced to its statutory source
- Every mandatory duty labeled with "shall/must/required" language from the actual statute
- Every pre-labeled violation tied to a specific rule
- Reservation of rights clause included
