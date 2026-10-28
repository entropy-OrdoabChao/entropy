---
name: void-judgment-analyzer
description: Determine if a judgment or court order is void ab initio due to jurisdictional defects, due process violations, or procedural failures. Use when challenging existing orders or judgments.
version: 1.0.0
metadata:
  hermes:
    tags: [legal, void-judgment, jurisdiction, due-process, collateral-attack]
    requires_toolsets: [terminal]
    grounding_status: partial
    related_skills: [void-order-spec, jurisdiction-check, beeze-judicial-immunity, due-process-violation-engine]
---

# Void Judgment Analyzer

Analyze whether a judgment or court order is void ab initio (from the beginning) and therefore subject to collateral attack at any time. Void judgments are legal nullities — they have no legal effect and no time limit for challenge.

## When to Use

- User says "the judgment is invalid", "the court had no authority"
- User wants to challenge an existing order or judgment
- User believes due process was violated
- User identifies jurisdictional defects in a prior proceeding
- Motion to vacate under Rule 60(b)(4) or Pa.R.C.P. 3051

## Void vs. Voidable

**VOID** — Court lacked jurisdiction or authority. No legal effect. Can be challenged at ANY time. Not subject to waiver, estoppel, or time limits.

**VOIDABLE** — Court had jurisdiction but made an error. Must be challenged through direct appeal within time limits. Becomes final if not appealed.

**Critical distinction:** A void judgment is a legal nullity. A voidable judgment is valid until reversed.

## Three Grounds for Void Judgment

### Ground 1: LACK OF SUBJECT MATTER JURISDICTION

The court had no power over the type of case.

**Test:**
- Was there a federal question or diversity basis? (if federal)
- Was the court the correct level? (trial vs. appellate)
- Was the subject matter within the court's statutory authority?

**Key law:**
- *Steel Co. v. Citizens for Better Environment*, 523 U.S. 83 (1998) — jurisdiction cannot be assumed
  (principle supported: "consent of counsel will not confer jurisdiction" — corpus verified)
- Subject matter jurisdiction cannot be waived or consented to
- Can be raised at any time, even on appeal

```bash
# Use: mcp__beezle__search_chunks with query "void judgment lack subject matter jurisdiction"
# Or: see beezle-mcp-routing skill for tool routing
```

### Ground 2: LACK OF PERSONAL JURISDICTION

The court had no power over the defendant.

**Test:**
- Was defendant properly served? (check service rules)
- Did defendant have minimum contacts? (*Int'l Shoe*)
- Was long-arm statute satisfied?
- Did defendant waive by appearing without objection?

**WARNING:** Unlike subject matter jurisdiction, personal jurisdiction CAN be waived by failing to timely object. Check if objection was preserved.

### Ground 3: DUE PROCESS VIOLATION

The proceeding violated fundamental fairness.

**Test:**
- Was proper notice given? (*Mullane v. Central Hanover*)
- Was opportunity to be heard provided?
- Was the proceeding fundamentally unfair?
- Was there fraud on the court? (Rule 60(d)(3))

**Due process requires:**
1. Adequate notice of the proceedings
2. Opportunity to be heard at a meaningful time and in a meaningful manner
3. Impartial tribunal

## Procedure

### Step 1: Obtain the Judgment/Order

Get the specific order being challenged. Identify:
- Court that issued it
- Date issued
- Type of order (default judgment, summary judgment, final order, etc.)
- Whether it was a signed order vs. proposed order

### Step 2: Check Each Ground

Run through all three grounds systematically.

### Step 3: Output

```
VOID JUDGMENT ANALYSIS

Order: [description]
Court: [name]
Date: [date]
Type: [default/summary/final/other]

GROUND 1 — SUBJECT MATTER JURISDICTION:
  Court's jurisdictional basis: [citation]
  Defect identified: [specific defect or NONE]
  Result: VOID / NOT VOID on this ground

GROUND 2 — PERSONAL JURISDICTION:
  Service method: [how defendant was served]
  Service defect: [specific defect or NONE]
  Minimum contacts: ESTABLISHED / NOT ESTABLISHED
  Waiver: WAIVED / PRESERVED / N/A
  Result: VOID / NOT VOID on this ground

GROUND 3 — DUE PROCESS:
  Notice: ADEQUATE / INADEQUATE — [details]
  Opportunity to be heard: PROVIDED / DENIED — [details]
  Fraud on court: YES / NO — [details]
  Result: VOID / NOT VOID on this ground

DETERMINATION:
  Judgment is: VOID / VOIDABLE / VALID
  Grounds: [list specific grounds if void]
  Time limit for challenge: NONE (if void) / [deadline] (if voidable)

RECOMMENDED FILING:
  [Motion to Vacate under Rule 60(b)(4)]
  [Motion for Relief from Judgment]
  [Independent Action under Rule 60(d)]
  [Collateral Attack in New Proceeding]
```

## Challenge Vehicles

| Ground | Federal | PA State |
|--------|---------|----------|
| Void judgment | Rule 60(b)(4) | Pa.R.C.P. 3051 (NOTE: Pa.R.C.P. 3051 text NOT IN CORPUS — verify before use) |
| Fraud on court | Rule 60(d)(3) | Inherent power |
| Independent action | Rule 60(d)(1) | Equity |
| Due process | 14th Amendment | PA Const. Art. I § 11 |

**Corpus-verified authorities:**
- *Marshall v. Holmes*, 141 U.S. 600 — FOUND in corpus; discusses state court judgments obtained by fraud/collusion being open to collateral attack
- *Espinoza v. United Student Aid Funds Inc.*, 559 U.S. 260 — FOUND in corpus; void judgment under Rule 60(b)(4)
- "Consent of counsel will not confer jurisdiction" — corpus verified
- "Jurisdiction of inferior courts not of record must be affirmatively shown" — corpus verified (McClurg v. Deming)

**NOT IN CORPUS:**
- Pa.R.C.P. 3051 — no direct match for this specific rule text found; verify before citing
- *Mullane v. Central Hanover* — not directly found in corpus search results; verify before citing

## Key Principle

A void judgment may be attacked at any time, in any court, in any proceeding. There is no time limit. The judgment is treated as if it never existed.

*"A void judgment is, in legal effect, no judgment. By it no rights are divested. From it no rights can be obtained. Being worthless in itself, all proceedings founded upon it are equally worthless."* — historical principle

## Verification

A complete void judgment analysis must identify the specific order, test each ground with citations, and produce a binary determination. If void, identify the specific challenge vehicle and draft the motion.
