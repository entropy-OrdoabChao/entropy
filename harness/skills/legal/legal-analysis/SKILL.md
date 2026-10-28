---
name: legal-analysis
description: Perform structured legal element analysis — identify applicable statutes, test each element against facts, determine if elements are satisfied/failed/missing. Use for any "analyze my case" or "what are my claims" request.
version: 1.0.0
metadata:
  hermes:
    tags: [legal, analysis, elements, claims, defense]
    requires_toolsets: [terminal]
  grounding: partial
---

## Related Skills

- **authority-hierarchy-builder** — resolve authority conflicts (replaces hardcoded hierarchy)
- **jurisdiction-check** — verify subject matter/personal jurisdiction before analysis
- **legal-rule-routing** — convert element test results into IF→THEN logic chains

# Structured Legal Analysis

Perform rule-based legal element analysis. For each potential claim or defense, identify the statute, extract elements, test each element against the user's facts, and produce a determination.

## When to Use

- User says "analyze my case", "what claims do I have", "is this a violation"
- User provides facts and asks for legal assessment
- User asks about elements of a specific statute
- User wants to know if they can file a motion or complaint

## Output Format

For each claim/defense:

```
RULE: [Statute] — "[Quoted text of the law]"

ELEMENTS:
Element 1: [Description] — [Statute section]
Element 2: [Description] — [Statute section]
...

ELEMENT TEST:
IF [condition] → SATISFIED / FAILED | MISSING FACT: [if needed]

DETERMINATION:
IF ALL ELEMENTS SATISFIED → [conclusion]
IF ANY ELEMENT FAILED → [consequence]
```

## Procedure

### 0. PRE-CHECK: Authority Must Exist

```
BEFORE any analysis:
  1. Check if Step 6 (Retrieve) returned actual statute text
  2. If NO statute retrieved:
     → STOP — output "ANALYSIS BLOCKED — no governing statute retrieved"
     → List what is needed (jurisdiction, statute, regulation)
     → Do NOT proceed to element extraction
     → Do NOT identify violations
     → Do NOT draw any conclusions
  3. Only proceed if at least one actual enacted statute is available
```

### 1. Identify Applicable Law

Search the corpus for relevant statutes via Beezle MCP `search_chunks` (NOT the old ChromaDB HTTP endpoint — it returns 404):
```bash
# Use the Beezle MCP client — search_chunks is MCP-only
# Query by subject + jurisdiction, not bare citation numbers
```

If search returns no results or only general principles:
→ STOP — request jurisdiction from user
→ Do NOT proceed with "general principles"

### 2. Extract Elements

Parse the statute text to identify each required element. Elements are the individual conditions that must ALL be met for the statute to apply.

**You MUST have actual statute text to extract elements from. Do NOT invent elements from general knowledge.**

### 3. Test Each Element

For each element, check the user's facts:
- **SATISFIED**: Facts clearly establish this element
- **FAILED**: Facts contradict or cannot establish this element
- **MISSING FACT**: Element cannot be tested without additional information from user

### 4. Produce Determination

- All elements satisfied → claim/defense is viable
- Any element failed → claim/defense fails on that element
- Missing facts → list what additional information is needed
- **No statute retrieved → DO NOT produce a determination**

## Authority Hierarchy

When multiple sources conflict, use the authority-hierarchy-builder skill to resolve. Do NOT hardcode hierarchy — it is ungrounded. The hierarchy is corpus-verified via the `authority-hierarchy-check` MCP tool, which maps the 16-tier hierarchy (see authority-hierarchy-builder SKILL.md).

## Prohibited Reasoning

- Do NOT use practice-based reasoning ("courts generally", "typically")
- Do NOT cite secondary sources as authority (treatises, law review)
- Do NOT speculate about outcomes
- Do NOT simplify legal terminology
- Use ONLY statutory text, rule text, and binding case law

## Common Analyses

All topics below were corpus-checked 2026-09-17 (per-row grounding in the table). For subject matter jurisdiction, use the jurisdiction-check skill — do NOT hardcode 28 USC 1331/1332 without verifying the claim basis.

| Topic | Key Statutes | Grounding (verified 2026-09-17) |
|-------|-------------|-----------|
| Bad faith insurance | 42 Pa.C.S.A. § 8371 + Terletsky elements | PARTIAL — PA Title 42 has NO per-section source; operative text only in aggregate `pa_consolidated_statutes` (16 'unreasonable delay' hits) + `pa_legal_defs`; Terletsky = 2 case-law chunks only. Use content-FTS, not source-name fetch. |
| FDCPA violations | 15 USC 1692 | GROUNDED — `usc_t15_s1692` (+a–e) exist as discrete sources; m0: chain_runner, new_claim_drafter |
| FCRA violations | 15 USC 1681 | GROUNDED — `usc_t15_s1681` (+a–x) exist as discrete sources; m0: chain_runner, new_claim_drafter |
| Section 1983 | 42 USC 1983 + Monroe/Monell | PARTIAL — 42 USC is ONE aggregate source `usc_42` (no per-section source; 0 '1983' hits inside it); Monroe/Monell = 187 case-law chunks. m0: applicability_engine.py (Monell rule line 278), binding_authority_lookup.py (Monroe v. Pape) — `qualified-immunity-analyzer.py` does NOT exist in pipeline/ |
| Subject matter jurisdiction | 28 USC 1331, 1332 | PARTIAL — 28 USC is ONE aggregate source `usc_28` (TOC-level; 1331/1332 each 1 hit); use jurisdiction-check skill |
| Motion to dismiss | FRCP 12(b)(6) + Iqbal/Twombly | GROUNDED — `frcp_rule_12` exists; Iqbal/Twombly = 136 case-law chunks; m0: generation/section_builder.py, pipeline/block14_pinpointer.py (NOTE: `pipeline/taxonomy.py` does NOT exist) |
| RESPA violations | 12 USC 2605 | GROUNDED — `usc_t12_s2605` exists as discrete source; m0: chain_runner, db_provenance_enforcer |
| UTPCPL | 73 P.S. § 201-1 | PARTIAL — NO `pa_code_t73%` source exists; text only in aggregate `pa_consolidated_statutes` (27 'Unfair Trade Practices' hits). Use content-FTS. |

## Verification

A complete analysis includes: statute text (verbatim), numbered elements, element-by-element testing with fact citations, and a binary determination. If any element lacks a determination, the analysis is incomplete.

## CRITICAL ENFORCEMENT RULES

### MISSING FACT = HARD STOP
If ANY element result = MISSING FACT:
  → IMMEDIATELY STOP all analysis
  → Do NOT proceed to logic chain
  → Do NOT identify violations or draw conclusions
  → Output ONLY: elements tested so far, the specific missing fact, request for that information
  → Wait for user response before continuing

### MANDATORY OUTPUT FORMAT — NO EXCEPTIONS
Every rule analysis MUST use this exact structure:

RULE [ID] — [exact statute citation]
SOURCE: [retrieved from database / NOT ESTABLISHED]

  ID FORMAT: [category].[number][letter]
    1.x = Constitutional
    2.x = Federal statute (a=primary, b=secondary)
    3.x = State statute
    4.x = Regulation (CFR, state reg)
    5.x = Common law / case law
    6.x = Procedural rule

  Examples:
    RULE 2.1a — 15 U.S.C. § 1693f(a)
    RULE 3.1a — 40 P.S. § 1171.5(a)(10)
    RULE 5.1a — Unjust enrichment (Schenck v. K.E. David, Ltd.)

ELEMENT 1: [description from statute text]
  FACT: [specific fact from user's input]
  RESULT: TRUE / FALSE / MISSING FACT

ELEMENT 2: [description from statute text]
  FACT: [specific fact from user's input]
  RESULT: TRUE / FALSE / MISSING FACT

DETERMINATION: [all elements satisfied / element X failed / STOPPED — missing fact]

If no citation exists → do NOT assign a Rule ID → mark NOT ESTABLISHED

Do NOT use narrative paragraphs instead of this format.
Do NOT combine elements into summary statements.
Each element tested individually on its own line.
All subsequent steps (chain, scorecard, draft) MUST reference Rule IDs.

### DEFINITION RESTRICTIONS
Do NOT define terms yourself. Only use definitions from:
  1. The governing statute (same title/chapter)
  2. Case law interpreting the statute
  3. Black's Law Dictionary (labeled as dictionary, last resort)
If no sourced definition exists:
  → Write: "No statutory definition retrieved for [term]"
  → Do NOT create a descriptive definition

### NO GENERALIZATION
Do NOT say:
  - "all states require..."
  - "universally..."
  - "typically..."
  - "generally..."
  - "most jurisdictions..."
Instead:
  - Cite the specific statute OR
  - Write: "NOT ESTABLISHED — specific statute not retrieved"

### MANDATORY DUTY DETECTION
When statute text contains: "shall" / "must" / "required" → MANDATORY duty (no discretion)
When statute text contains: "prohibited" / "may not" → MANDATORY prohibition
Label these explicitly in element testing:
  Element 3: Insurer S{{JUDGE}} return funds — DUTY: MANDATORY

### ENTITY CLASSIFICATION
When statute uses "person", "you", "individual":
  1. Check if the statute defines the term
  2. If "person includes corporations" → label as CORPORATE + NATURAL
  3. If "individual" → label as NATURAL PERSON ONLY
  4. Do not assume — check the definitions section

### CROSS-REFERENCE DETECTION
When a statute references another statute:
  - Track it: "12 CFR § 1026.1 implements 15 U.S.C. § 1601"
  - Definitions from the USC carry into the CFR unless the CFR redefines them

### NO INTERPRETATION RULE
1. DO NOT infer meaning beyond what the statute says
2. DO NOT generalize definitions across authorities
3. DO NOT merge definitions from different sources
4. ALWAYS label the source of every definition used
5. IF multiple definitions exist → present ALL, label each, state which controls and why
6. IF the statute is silent on a term → say so, then check secondary sources (labeled)

### OUTPUT FEEDS legal-rule-routing
After element testing:
  - Pass all TRUE/FALSE results to legal-rule-routing
  - Pass all mandatory duties detected
  - Pass all definitions used (with sources)
  - legal-rule-routing converts these into IF→THEN logic

