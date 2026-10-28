---
name: binding-precedent-enforcer
description: For each element/issue, retrieves and locks binding precedent from controlling jurisdiction. Builds the precedent wall the court must overcome to rule against you.
version: 1.0.0
author: Hermes Legal
metadata:
  hermes:
    tags: [legal, precedent, binding, due-process, ca3, edpa, pa]
    related_skills: [authority-hierarchy-check, legal-analysis, due-process-violation-engine]
prerequisites:
  files: [~/.hermes/legal_config.json]
  dbs: [chromadb:legal_docs]
---

# Binding Precedent Enforcer

## Purpose
Lock every legal element to **binding** authority from the controlling jurisdiction. Output is the wall the court must climb over to rule against the chain. Climbing it = visible reversible error.

## When to Use
- Step 6.5 of SOUL.md pipeline (after retrieval, before validation)
- Any element analysis where the rule must be enforceable on appeal
- Before drafting any motion that depends on a precedential holding

## When NOT to Use
- Pure statutory interpretation with no caselaw needed
- Historical or academic legal questions

## Hierarchy (from legal_config.json)
```
For state-court matter (pa-state):
  US Const → SCOTUS → PA Const → Pa. → Pa. Super. / Pa. Cmwlth.

For federal matter in EDPA:
  US Const → SCOTUS → 3rd Circuit → US Code → CFR → FRCP → EDPA Local Rules
```

Lower court holdings cannot override higher court holdings. Where lower court PRACTICE deviates from higher binding precedent → that is the violation, not the rule.

## Usage
```bash
~/.hermes/skills/legal/binding-precedent-enforcer/scripts/enforce.sh \
  --element "due process requires notice and opportunity to be heard" \
  --jurisdiction edpa \
  --output /tmp/precedent_wall.json
```

Multi-element batch:
```bash
~/.hermes/skills/legal/binding-precedent-enforcer/scripts/enforce.sh \
  --elements-file /tmp/elements.json \
  --jurisdiction pa-state \
  --output /tmp/precedent_wall.json
```

## Output Format
```json
{
  "element": "string",
  "jurisdiction": "edpa",
  "binding_chain": [
    {
      "level": "scotus",
      "case": "Mathews v. Eldridge",
      "citation": "424 U.S. 319 (1976)",
      "pinpoint": "335",
      "holding": "verbatim quote of binding holding",
      "binds": ["all_courts"]
    },
    {
      "level": "ca3",
      "case": "...",
      "citation": "...",
      "binds": ["edpa", "mdpa", "wdpa"]
    }
  ],
  "rule_synthesized": "the rule the court MUST apply",
  "duty_on_court": "mandatory action required by precedent",
  "if_ignored": "reversible error / structural error / void",
  "preservation_note": "how to preserve for appeal"
}
```

## Rules
1. Only cite cases retrievable from DB (or CourtListener live)
2. Verbatim holdings only — no paraphrase
3. If no binding precedent found → output `NO_BINDING_FOUND` with search trail
4. Never cite persuasive authority as binding
5. Never strip case opinions — binding precedent IS the weapon
