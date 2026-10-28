# IFP Cure Deadline Pattern

## Problem
When a federal court denies an IFP motion without prejudice (28 U.S.C. § 1915(a)(3)), it typically grants the plaintiff a cure period (30 days) to either pay the filing fee or submit a proper IFP form. If the plaintiff misses this deadline, the court may dismiss the case for failure to prosecute.

## Pattern (2026-07-30, fed_26-cv-03268)

### Detection
1. Search intake/ for orders mentioning "IFP" or "In Forma Pauperis" and "denied without prejudice"
2. Extract the cure period (typically 30 days) and the deadline date
3. Check CASE_TIMELINE.md — if it's stale (last update > 7 days), the timeline may not reflect newer docket entries
4. Cross-reference with CASE_FACTS.md — it may have the deadline info but in narrative form

### Example (fed_26-cv-03268)
- **Order:** Docket 6, dated 05/18/2026 — IFP motion denied without prejudice
- **Cure period:** 30 days (pay $405 or submit proper IFP form)
- **Cure deadline:** 06/17/2026
- **Appeal deadline:** 60 days from order = 07/17/2026
- **Current date:** 07/30/2026 — BOTH deadlines MISSED
- **Risk:** Case may have been dismissed for failure to prosecute

### Action Items
1. Flag as CRITICAL if cure deadline has been missed
2. Flag as URGENT if appeal deadline has been missed
3. Check if the case was actually dismissed (look for dismissal orders in intake/)
4. If dismissed, the appeal deadline may be 60 days from the dismissal order (different clock)
5. Recommend user to either: (a) submit IFP form if still within cure period, or (b) appeal if within appeal period

### PITFALL
- The CASE_TIMELINE.md may be stale and not reflect the latest docket entries. Always check the intake/ folder for newer PDFs that aren't in the timeline.
- The appeal deadline for IFP denial (60 days from the denial order) is DIFFERENT from the appeal deadline for a dismissal (60 days from the dismissal order). Don't conflate them.
- The 30-day cure period and the 60-day appeal period may overlap. If the user cures the deficiency, the appeal of the initial IFP denial becomes moot.

### Location
- Order typically in intake/ with name like `dkt_<NNN>_activity_in_case_*.pdf`
- Deadline info in CASE_FACTS.md under "CONTROLLING AUTHORITIES" or "ADVERSE / APPEALABLE / DISPOSITIVE ORDERS"
- CASE_TIMELINE.md may be stale — verify against intake/ files
