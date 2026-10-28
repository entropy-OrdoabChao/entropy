---
name: authority-hierarchy-check
description: Validate and rank legal authorities by binding strength. Detect conflicts between sources. Use after retrieval, before drafting, to ensure only valid authority is cited.
version: 1.0.1
metadata:
  hermes:
    tags: [legal, authority, hierarchy, conflict-detection, citations]
    requires_toolsets: [terminal]
  evolution:
    date: 2026-05-22
    iteration: 1
    improvements: [enhanced_conflict_resolution, verification_workflow, edge_case_handling]
    score_improvement: pending
---

# Authority Hierarchy Check

Validate retrieved legal authorities, rank them by binding strength, and detect conflicts between sources. This skill prevents citing weak authority when stronger authority exists, and flags contradictions.

## When to Use

- After retrieving results from ChromaDB or CourtListener
- Before drafting any legal document
- When multiple authorities say different things
- When user asks "which law controls?"

## Authority Hierarchy (Descending Binding Strength)

```
Level 1: CONSTITUTIONAL PROVISIONS
         U.S. Constitution, PA Constitution Art. I
         → Supreme, cannot be overridden by statute

Level 2: SUPREME COURT HOLDINGS
         SCOTUS decisions, PA Supreme Court decisions
         → Binding on all lower courts in jurisdiction

Level 3: FEDERAL/STATE STATUTES
         USC titles, Pa.C.S.A., P.S.
         → Controlling unless unconstitutional

Level 4: APPELLATE HOLDINGS
         Circuit courts (3rd Cir.), PA Superior Court
         → Binding within circuit/state

Level 5: RULES & REGULATIONS
         FRCP, FRE, Pa.R.C.P., CFR
         → Procedural authority

Level 6: DISTRICT/TRIAL COURT
         E.D. Pa., Court of Common Pleas
         → Persuasive only, not binding

Level 7: SECONDARY SOURCES
         Treatises, law review, Restatements
         → NEVER cite as authority, reference only
```

## Procedure

### 1. Classify Each Retrieved Source

For each authority retrieved from ChromaDB or CourtListener:

```
AUTHORITY: [citation]
LEVEL: [1-7 from hierarchy]
JURISDICTION: [federal/state/PA]
BINDING ON: [which courts]
HOLDING: [one-sentence rule]
STATUS: GOOD LAW / OVERRULED / DISTINGUISHED / SUPERSEDED
```

### 2. Conflict Detection

Compare all retrieved authorities. Flag conflicts:

```
CONFLICT DETECTED:
  Source A: [citation] — says [X]
  Source B: [citation] — says [Y]
  RESOLUTION: Source [A/B] controls because [higher level / later date / more specific]
```

**Conflict resolution rules:**
1. Higher hierarchy level wins (Constitution > statute > case)
2. Later-in-time wins for same level (newer statute supersedes older)
3. More specific wins over general (specific statute > general statute)
4. Binding wins over persuasive (3rd Circuit > 9th Circuit in E.D. Pa.)
5. En banc wins over panel opinion
6. Majority wins over concurrence/dissent

### 3. Verify Case Law Status

For each case cited, check if it's still good law:

```bash
python3 ~/.hermes/skills/legal/courtlistener-search/scripts/courtlistener.py search "[case name]"
```

Look for:
- Has it been overruled?
- Has the statute it interprets been amended?
- Has a later Supreme Court case changed the standard?

### 4. Output Validated Authority Block

```
VALIDATED AUTHORITY FOR [issue]:

PRIMARY STATUTE:
  [citation] — Level [N]
  Text: "[relevant statutory language]"

CONTROLLING CASE:
  [citation] — Level [N]
  Holding: "[one-sentence holding]"
  Status: GOOD LAW

SUPPORTING AUTHORITY:
  [citation] — Level [N]
  Relevance: [why it supports]

DEFINITION (if applicable):
  [term]: "[definition]" — [source]

CONFLICTS: NONE / [describe conflict + resolution]
```

## Red Flags (Do NOT Cite)

- **Overruled cases** — check with CourtListener
- **Dicta** — statements not necessary to the holding
- **Dissenting opinions** — not law
- **Secondary sources as authority** — treatises, law reviews
- **Cases from non-binding jurisdictions** — unless no binding authority exists
- **Unpublished opinions** — check local rules for citability
- **Training data citations** — ALWAYS verify through search before citing

## Verification

Every citation in the final document must trace back to a ChromaDB result or CourtListener API response. If a citation cannot be verified, it must be removed or replaced with a verified alternative.
