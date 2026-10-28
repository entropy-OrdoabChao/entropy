---
name: procedural-attack-builder
description: Identify procedural defects in opposing filings and recommend specific motions to exploit them. Use when responding to any filing from opposing counsel or reviewing court procedure.
version: 1.0.1
metadata:
  hermes:
    tags: [legal, procedural, motions, attack, defects, defense]
    requires_toolsets: [terminal]
    grounding_status: partial
    related_skills: [jurisdiction-check, legal-docx, procedural-spec, filing-status-audit]
---

# Procedural Attack Builder

Systematically scan opposing filings and court proceedings for procedural defects, then recommend specific motions to exploit each defect. Procedural attacks are often more powerful than substantive arguments because they can dispose of a case without reaching the merits.

## When to Use

- User receives any filing from opposing counsel
- User wants to challenge a court's procedure
- User asks "how do I respond to this"
- User asks "what's wrong with this filing"
- Before filing any responsive pleading

## Defect Categories

### Category 1: SERVICE DEFECTS

**Check:**
- Was service made by authorized person?
- Was service at correct address / on correct person?
- Was timing of service proper?
- Was proof of service filed?
- Was substituted service properly authorized?

**PA Rules:** Pa.R.C.P. Ch. 400 (Service of Original Process)
- 400: Person to Make Service / Acceptance of Service
- 402: Manner of Service (individuals, corporations, minors)
- 403: Service by Mail
- 404: Service Outside the Commonwealth
- 405: Return of Service
- 410: Real Property Actions

> **[corpus]** Ch. 400 IS in the corpus with real rule text — source `pa_code_t231_ch400` (Rules 400/401/402/403/410 verified present). Fetch by source-name first.

**Federal:** FRCP 4 (service requirements)

**Attack:** Motion to Quash Service / Motion to Dismiss under 12(b)(5)

### Category 2: FILING DEFECTS

**Check:**
- Is the document verified (when required)? (Pa.R.C.P. 1019, 1023)
- Is there a certificate of service? (Pa.R.C.P. 405)
- Was it filed within the deadline?
- Does it conform to court rules (format, page limits)?
- Is it signed by authorized person? (Pa.R.C.P. 1023)
- Is a proposed order attached (when required)?
- Cover sheet filed (for complaints)?

**Attack:** Preliminary Objections (Pa.R.C.P. 1028(a)(2)) / Motion to Strike

> **NOTE:** Pa.R.C.P. 1028(a)(2) covers "failure of a pleading to conform to law or rule of court or inclusion of scandalous or impertinent matter." Verification and signing requirements are governed separately by Pa.R.C.P. 1019 (contents of pleadings) and 1023 (signing of documents). Do not conflate these.
>
> **[corpus] CAUTION:** Pa.R.C.P. 1028 / 1019 / 1023 are NOT retrievable as verbatim rule text in the corpus — sources `pa_code_t231_ch1028`, `pa_code_t231_ch1023`, `pa_code_t231_ch1019` are each a single 569-byte "No statutes or acts will be found" stub. The rule NUMBERS are corroborated (e.g. ch1000 real text references "Rule 1028(a)(1), (2), (5) and (6)"), but for verbatim PO-grounds text rely on the official Pa.R.C.P. and flag NOT IN CORPUS for any verbatim quote.

### Category 3: PLEADING DEFECTS

**Check:**
- Are allegations specific enough? (Pa.R.C.P. 1019 — "General and Specific Averments")
- Are legal conclusions stated without factual support? (Federal: Twombly/Iqbal — note: federal pleading standard differs from PA's Rule 1019)
- Are claims pled in the alternative properly?
- Is the prayer for relief specific?
- Are the correct parties named?
- Is capacity to sue established?

**Attack:** Preliminary Objections (demurrer) / 12(b)(6) Motion to Dismiss

### Category 4: JURISDICTIONAL DEFECTS

**Check:** (See `jurisdiction-check` skill for full analysis)
- Subject matter jurisdiction
- Personal jurisdiction
- Venue
- Removal defects

**Attack:** 12(b)(1), 12(b)(2), 12(b)(3) / Motion to Remand / Motion to Transfer

### Category 5: TIMING DEFECTS

**Check:**
- Was the statute of limitations met?
- Was the response filed within the deadline?
- Was removal timely (30 days from service/per receipt — 28 U.S.C. § 1446))?

> **[corpus] CAUTION:** 28 U.S.C. § 1446 appears in the corpus only at TOC/reference level (source `USCODE-2023-title28`, 18 mentions — all cross-references, not the operative 30-day subsection text). Treat the verbatim 30-day rule as NOT IN CORPUS until fetched elsewhere; the 30-day figure is standard but verify before citing verbatim.
- Were preliminary objections filed within applicable deadline?
- Was discovery deadline met?

**Attack:** Motion to Dismiss (statute of limitations) / Motion to Strike (untimely)

### Category 6: PROCEDURAL PREREQUISITES

**Check:**
- Were administrative remedies exhausted?
- Was demand letter sent (when required)?
- Was notice provided (when required by statute)?
- Was mediation attempted (when required by court)?
- Were discovery obligations met before summary judgment?

**Attack:** Motion to Dismiss for failure to exhaust / Motion to Stay

## Procedure

### Step 1: Receive and Classify the Filing

```
Document type: [complaint/motion/response/order]
Filed by: [party]
Date filed: [date]
Court: [name]
```

### Step 2: Run Each Defect Category

Check ALL six categories systematically. Document every defect found.

### Step 3: Prioritize Defects

Rank defects by impact:
1. **FATAL** — disposes of the case (jurisdiction, statute of limitations)
2. **MAJOR** — requires amendment or cure (pleading insufficiency, service defect)
3. **MINOR** — procedural irregularity (format, missing cert of service)

### Step 4: Output

```
PROCEDURAL ATTACK ANALYSIS

Filing reviewed: [document name]
Filed by: [party]
Date: [date]

DEFECTS FOUND:

1. [FATAL/MAJOR/MINOR] — [Category]: [specific defect]
   Rule violated: [citation]
   Attack: [specific motion]
   Deadline to file: [date or "any time" for jurisdictional]

2. [FATAL/MAJOR/MINOR] — [Category]: [specific defect]
   Rule violated: [citation]
   Attack: [specific motion]
   Deadline to file: [date]

...

RECOMMENDED FILING ORDER:
1. [Most impactful motion first]
2. [Second motion]
3. [Third motion]

STRATEGIC NOTE: [tactical advice on timing and sequencing]
```

## Common Attacks Quick Reference

| Defect | Federal Motion | PA Motion |
|--------|---------------|-----------|
| No jurisdiction | 12(b)(1) | Prelim. Obj. 1028(a)(1) |
| Bad service | 12(b)(5) | Prelim. Obj. 1028(a)(1) |
| Fails to state claim | 12(b)(6) | Prelim. Obj. 1028(a)(4) |
| Wrong venue | 12(b)(3) | Prelim. Obj. 1028(a)(1) |
| Not verified | — | Prelim. Obj. 1028(a)(2) |
| Untimely removal | Motion to Remand | — |
| Statute of limitations | 12(b)(6) | Prelim. Obj. 1028(a)(4) |
| Forum defendant | Motion to Remand | — |

## Pitfalls

- Some defects are waivable if not raised promptly (personal jurisdiction, venue, service)
- Subject matter jurisdiction is NEVER waivable
- Filing a responsive pleading before preliminary objections may waive some objections
- In PA, all preliminary objections must be filed together (Pa.R.C.P. 1028(b))
- Check local rules for specific deadlines and page limits

## Verification

Every defect must cite the specific rule or statute violated. Every recommended motion must have a legal basis. Deadlines must be calculated from the specific facts of the case.
