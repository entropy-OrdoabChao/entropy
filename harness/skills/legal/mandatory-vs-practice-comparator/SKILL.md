---
name: mandatory-vs-practice-comparator
description: For every "shall/must/required/prohibited" duty in a statute or rule, compares against actual court practice. Outputs deviations as enforceable violations with statutory + constitutional consequences.
version: 1.0.0
author: Hermes Legal
metadata:
  hermes:
    tags: [legal, mandatory-duty, shall, statutory, practice-deviation]
    related_skills: [court-practice-auditor, due-process-violation-engine, legal-scorecard]
prerequisites:
  files: [~/.hermes/legal_config.json]
---

# Mandatory vs Practice Comparator

## Purpose
"Shall" means shall. When a statute or rule imposes a mandatory duty and court practice deviates from it, the deviation is enforceable as statutory violation AND (where the duty protects a constitutional interest) as a due process violation.

This skill is the engine that makes a court's deviation visible and actionable.

## Mandatory verbs (binding)
- `shall` / `must` / `required` / `prohibited` / `may not` / `is entitled to` / `has the right to`

## Discretionary verbs (NOT binding)
- `may` / `should` / `appropriate` / `in the court's discretion`

## Authorities for "shall = mandatory"
- **Lexecon Inc. v. Milberg Weiss**, 523 U.S. 26 (1998) — "shall" is mandatory
- **Anderson v. Yungkau**, 329 U.S. 482 (1947) — distinction between shall and may
- **Kingdomware Technologies v. United States**, 579 U.S. 162 (2016) — "shall" creates obligation

## Usage
```bash
# Compare a statute against observed practice
~/.hermes/skills/legal/mandatory-vs-practice-comparator/scripts/compare.sh \
  --statute "Fed. R. Civ. P. 52(a)" \
  --statute-text-file /tmp/frcp52.txt \
  --practice-file /tmp/order.txt \
  --jurisdiction edpa \
  --output /tmp/mandatory_compare.json
```

## Output Format
```json
{
  "statute": "Fed. R. Civ. P. 52(a)",
  "verbatim_text": "...",
  "duties_extracted": [
    {
      "duty_id": 1,
      "verbatim": "the court must find the facts specially and state its conclusions of law separately",
      "duty_type": "MANDATORY",
      "owed_by": "court",
      "verb": "must"
    }
  ],
  "practice_observed": "...",
  "deviations": [
    {
      "duty_id": 1,
      "deviation": "court issued summary denial without findings of fact or conclusions of law",
      "consequence_statutory": "violation of Rule 52(a) — appellate reversal",
      "consequence_constitutional": "Mathews factor #5 (decision on the record) — due process violation",
      "binding_authority_enforcing_duty": [
        "Lexecon Inc. v. Milberg Weiss, 523 U.S. 26 (1998)",
        "Kingdomware, 579 U.S. 162 (2016)"
      ]
    }
  ],
  "court_must_explain_or_be_reversed": true
}
```

## Rules
1. Parse only enacted statute or rule text — no commentary
2. Extract every "shall/must/required" — no skipping
3. Match each duty to the practice deviation with exact text
4. Never label "may" as mandatory
5. Always tag who the duty is owed by (court / clerk / party)
