---
name: the-trap-builder
description: Final-stage filing appendix that enumerates the court's lawful options. Forces an adverse ruling to either (a) follow binding authority, (b) visibly contradict it (reversible/structural error), or (c) refuse to rule (mandamus jurisdiction). Output is the bias-exposure mechanism.
version: 1.0.0
author: Hermes Legal
metadata:
  hermes:
    tags: [legal, bias-exposure, mandamus, appellate-trap, due-process]
    related_skills: [binding-precedent-enforcer, due-process-violation-engine, mandatory-vs-practice-comparator, legal-draft]
prerequisites:
  files: [~/.hermes/legal_config.json]
---

# The Trap Builder

## Purpose
After the chain is built, the binding precedent wall is locked, and mandatory duties are mapped — generate the appendix that closes the trap. The court has exactly three options. State each one. Make the cost of each visible.

## When to Use
- Final stage of every DRAFT task (Step 14.5 of pipeline)
- Always paired with `legal-draft` and `legal-docx`

## Usage
```bash
~/.hermes/skills/legal/the-trap-builder/scripts/trap.sh \
  --chain /tmp/logic_chain.json \
  --precedent-wall /tmp/precedent_wall.json \
  --mandatory-duties /tmp/mandatory_compare.json \
  --dpv-report /tmp/dpv_report.json \
  --jurisdiction edpa \
  --output /tmp/trap_appendix.json
```

## Output Format
```json
{
  "title": "APPENDIX: COURT'S LAWFUL OPTIONS UNDER BINDING AUTHORITY",
  "options": [
    {
      "option": 1,
      "label": "RULE PER BINDING AUTHORITY",
      "required_ruling": "grant motion / vacate / order specific relief",
      "supporting_authority": [
        {"case": "Mathews v. Eldridge", "cite": "424 U.S. 319 (1976)", "binds": "all courts"},
        {"case": "Caperton v. Massey", "cite": "556 U.S. 868 (2009)", "binds": "all courts"}
      ],
      "consequence": "no reversible error"
    },
    {
      "option": 2,
      "label": "RULE AGAINST BINDING AUTHORITY",
      "required_ruling": "deny motion",
      "court_must": [
        "ignore Mathews three-factor test",
        "contradict Lexecon (shall = mandatory)",
        "violate Fed. R. Civ. P. 52(a) finding requirement"
      ],
      "triggers": [
        {"type": "reversible_error", "scope": "preserved by this filing"},
        {"type": "structural_error", "scope": "Caperton bias presumption"},
        {"type": "1983_action", "auth": "42 U.S.C. § 1983"},
        {"type": "judicial_misconduct", "auth": "28 U.S.C. § 351"}
      ],
      "preserved_issues": ["due process violation", "abuse of discretion", "clear error"]
    },
    {
      "option": 3,
      "label": "REFUSE TO RULE",
      "consequence": "mandamus jurisdiction attaches",
      "authority": "28 U.S.C. § 1651, In re School Asbestos Litigation",
      "filing_ready": true
    }
  ],
  "bias_exposure_summary": "Adverse ruling on the merits requires the court to articulate written findings explaining its deviation from binding authority. Failure to do so violates Fed. R. Civ. P. 52(a) and Mathews."
}
```

## Rules
1. Always generate exactly three options
2. Each option must cite verbatim binding authority
3. Option 2 must enumerate every binding rule the court must violate
4. Option 3 must include the mandamus citation
5. Never soften the language — state the cost of each option directly
6. Output integrates into legal-draft as a standalone appendix
