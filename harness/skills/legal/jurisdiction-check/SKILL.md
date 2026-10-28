---
name: jurisdiction-check
description: Determine if a court has subject matter and personal jurisdiction. Checks federal question, diversity, removal defects. Use FIRST before any filing analysis.
version: 1.0.0
metadata:
  hermes:
    tags: [legal, jurisdiction, federal-question, diversity, removal]
    requires_toolsets: [terminal]
    grounding: partial
    related_skills: [jurisdiction-spec, fed-rules-spec, void-judgment-analyzer, legal-scorecard]
---

# Jurisdiction Check

Determine whether a court has jurisdiction to hear a case. This is the FIRST check in any legal analysis — if jurisdiction fails, nothing else matters.

## When to Use

- ALWAYS before any filing analysis
- User asks "can I file in federal court?"
- User asks about removal from state to federal
- Opposing party challenges jurisdiction
- Any motion to dismiss for lack of jurisdiction

## Three Jurisdiction Types

### A. Federal Question (28 USC 1331)

**Test:** Does the claim "arise under" federal law?

**Key cases:**
- *Louisville & Nashville R.R. v. Mottley*, 211 U.S. 149 (1908) — well-pleaded complaint rule [NOT IN CORPUS — no dedicated source; only indirect references in other opinions]
- *Grable & Sons v. Darue Eng'g*, 545 U.S. 308 (2005) — substantial federal question [corpus: cap_us_545_308_grable_sons_metal_products_inc_v_darue_e]
- *Gunn v. Minton*, 568 U.S. 251 (2013) — four-part test for embedded federal issues [corpus: cap_us_568_251_gunn_v_minton]

**Grable test (embedded federal question):**
1. Federal issue is necessarily raised
2. Federal issue is actually disputed
3. Federal issue is substantial
4. Federal-state balance favors federal jurisdiction

```bash
# Primary: source-name fetch (fast, precise)
sqlite3 /home/{{USER_FIRST}}/turbovec/chunk_text.db "SELECT text FROM chunk_text WHERE source='usc_t28_s1331'"
# Fallback: FTS (one MATCH per query; OR of two MATCH breaks)
sqlite3 /home/{{USER_FIRST}}/turbovec/chunk_text.db "SELECT source_id FROM chunk_text_fts WHERE chunk_text_fts MATCH 'federal question jurisdiction' LIMIT 5"
```

### B. Diversity Jurisdiction (28 USC 1332)

**Test:** Complete diversity + amount in controversy > $75,000

**Key cases:**
- *Strawbridge v. Curtiss*, 7 U.S. 267 (1806) — complete diversity required [NOT IN CORPUS — only cap_us_569_978_strawbridge_v_lord (2013 cert denial) exists; the 1806 opinion has no dedicated source]
- *St. Paul Mercury v. Red Cab*, 303 U.S. 283 (1938) — legal certainty test for amount [corpus: scotus_st_paul_mercury_indemnity_co_v_red_cab_co_303us283]

**Corporate citizenship** [corpus: usc_t28_s1332 § 1332(c)(1)]: "a corporation shall be deemed to be a citizen of every State and foreign state by which it has been incorporated and of the State or foreign state where it has its principal place of business"

**Checklist:**
- [ ] Plaintiff's citizenship: [state]
- [ ] Defendant's citizenship: [state]
- [ ] Complete diversity exists: YES / NO
- [ ] Amount in controversy: $[amount]
- [ ] Exceeds $75,000: YES / NO
- [ ] Corporate citizenship (if applicable): state of incorporation + principal place of business

### C. Removal (28 USC 1441-1446)

**Test:** Was removal proper?

**Defects to check:**
1. **Timeliness** — filed within 30 days of service (§ 1446(b)(1)) [corpus: usc_t28_s1446 — "shall be filed within 30 days after the receipt by the defendant"]
2. **Forum defendant rule** — if ANY defendant is citizen of forum state, removal barred for diversity cases (§ 1441(b)(2)) [PARTIAL — usc_t28_s1441(b)(2) heading present but operative text truncated in corpus chunk]
3. **All defendants joined** — unanimous consent required (*Shamrock Oil & Gas Corp. v. Byrd*, 337 U.S. 210 (1949)) [corpus: scotus_shamrock_oil_gas_corp_v_sheets_313us100 + usc_t28_s1446(b)(2)(A)]
4. **One-year bar** — diversity cases cannot be removed after 1 year (§ 1446(c)(1)) [corpus: usc_t28_s1446 — "may not be removed ... more than 1 year after commencement"]
5. **CAFA exception** — class actions have different rules (28 USC 1332(d)) [corpus: usc_t28_s1332(d) — $5M threshold, 2/3 rule, exclusions]

```bash
# Primary: source-name fetch (fast, precise)
sqlite3 /home/{{USER_FIRST}}/turbovec/chunk_text.db "SELECT text FROM chunk_text WHERE source='usc_t28_s1446'"
sqlite3 /home/{{USER_FIRST}}/turbovec/chunk_text.db "SELECT text FROM chunk_text WHERE source='usc_t28_s1441'"
# Fallback: FTS (one MATCH per query; OR of two MATCH breaks)
sqlite3 /home/{{USER_FIRST}}/turbovec/chunk_text.db "SELECT source_id FROM chunk_text_fts WHERE chunk_text_fts MATCH 'removal procedure defects' LIMIT 5"
```

## Procedure

### Step 1: Identify the Court

```
Court: [name]
Level: [federal district / state trial / state appellate]
Location: [district or county]
```

### Step 2: Test Each Basis

Run through federal question, diversity, AND removal (if applicable).

### Step 3: Output

```
JURISDICTION ANALYSIS

Court: [name]
Case: [caption]

FEDERAL QUESTION (28 USC 1331):
  Federal statute at issue: [citation or NONE]
  Arises under: YES / NO
  Well-pleaded complaint: [analysis]
  Result: ESTABLISHED / NOT ESTABLISHED

DIVERSITY (28 USC 1332):
  Plaintiff citizenship: [state]
  Defendant citizenship: [state]
  Complete diversity: YES / NO
  Amount in controversy: $[amount] — EXCEEDS $75K / DOES NOT EXCEED
  Result: ESTABLISHED / NOT ESTABLISHED

REMOVAL (28 USC 1441) [if applicable]:
  Original court: [state court]
  Removed to: [federal court]
  Date of service: [date]
  Date of removal: [date]
  Timely (30 days): YES / NO
  Forum defendant rule: VIOLATED / NOT VIOLATED
  All defendants joined: YES / NO
  One-year bar: APPLICABLE / NOT APPLICABLE
  Result: PROPER / DEFECTIVE — [specific defect]

OVERALL JURISDICTION: ESTABLISHED / NOT ESTABLISHED / DEFECTIVE REMOVAL
RECOMMENDED ACTION: [proceed / move to dismiss / move to remand / amend]
```

## Attack Vectors (for challenging jurisdiction)

1. **Motion to Remand** — removal was defective (timeliness, forum defendant, consent)
2. **12(b)(1) Motion** — no subject matter jurisdiction
3. **12(b)(2) Motion** — no personal jurisdiction
4. **Challenge Amount** — legal certainty that amount does not exceed $75K

## Verification

Jurisdiction analysis must cite specific statutes and at least one controlling case. Every factual assertion about citizenship, amount, or timing must come from the user's provided facts or court record.
