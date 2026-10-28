---
name: due-process-violation-engine
description: Tests every court action, ruling, or practice against procedural and substantive due process baselines (Mathews v. Eldridge, Mullane, Goldberg, Caperton). Identifies violations, classifies severity (structural / fundamental / harmless), and maps to remedy.
version: 1.0.0
author: Hermes Legal
metadata:
  hermes:
    tags: [legal, due-process, mathews, structural-error, 1983, mandamus]
    related_skills: [binding-precedent-enforcer, court-practice-auditor, judicial-bias-pattern-detector]
prerequisites:
  files: [~/.hermes/legal_config.json]
---

# Due Process Violation Engine

## Purpose
For every court action (ruling, order, practice, omission), test against the binding due process canon. Output a violation record that ties the deviation to a SCOTUS holding and an enforcement remedy.

## Canon (binding on all courts)
- **Mathews v. Eldridge**, 424 U.S. 319 (1976) — three-factor procedural due process test
- **Mullane v. Central Hanover**, 339 U.S. 306 (1950) — notice "reasonably calculated"
- **Goldberg v. Kelly**, 397 U.S. 254 (1970) — pre-deprivation hearing
- **Caperton v. A.T. Massey**, 556 U.S. 868 (2009) — recusal due to bias
- **Tumey v. Ohio**, 273 U.S. 510 (1927) — financial interest = structural disqualification
- **Ward v. Monroeville**, 409 U.S. 57 (1972) — pecuniary interest, structural
- **In re Murchison**, 349 U.S. 133 (1955) — judge cannot be prosecutor
- **Marshall v. Jerrico**, 446 U.S. 238 (1980) — neutral arbiter requirement
- **Hamdi v. Rumsfeld**, 542 U.S. 507 (2004) — meaningful opportunity to be heard
- **Connecticut v. Doehr**, 501 U.S. 1 (1991) — pre-attachment due process

## Procedural Due Process Checklist (Mathews factors + structural)
1. **Notice** — adequate, timely, specific to the deprivation
2. **Hearing** — meaningful, before deprivation where possible
3. **Neutral arbiter** — no bias, no financial interest, no role conflict
4. **Right to be heard** — present evidence, cross-examine, respond to adverse evidence
5. **Decision on the record** — written findings of fact + conclusions of law
6. **Right to counsel** (where applicable)
7. **Right to appellate review**

## Substantive Due Process Checklist
1. Deprivation of life, liberty, or property?
2. Government action?
3. Fundamental right involved?
4. Strict scrutiny applied?

## When to Use
- Step 10.5 of pipeline (after logic chain, before scorecard)
- Auditing any court order
- Drafting motions for reconsideration / mandamus / §1983

## Usage
```bash
~/.hermes/skills/legal/due-process-violation-engine/scripts/dpv.sh \
  --action /tmp/court_order.txt \
  --record /tmp/case_record.json \
  --jurisdiction edpa \
  --output /tmp/dpv_report.json
```

## Output Format
```json
{
  "court_action": "string",
  "due_process_prong": "Mathews factor #2 (hearing)",
  "binding_authority_violated": "Goldberg v. Kelly, 397 U.S. 254 (1970)",
  "verbatim_holding": "...",
  "record_evidence": "Exhibit C, p. 4",
  "violation_type": "structural | fundamental | harmless",
  "preservation_required": ["objection at hearing", "post-hearing motion"],
  "remedies": [
    {"type": "motion_reconsideration", "rule": "Fed. R. Civ. P. 59(e)"},
    {"type": "mandamus", "authority": "28 U.S.C. § 1651"},
    {"type": "1983_action", "elements": ["state actor", "deprivation", "causation"]},
    {"type": "appeal", "issue": "preserved structural error"}
  ],
  "court_must_explain": "the court must articulate findings explaining the deviation OR be reversed"
}
```

## Rules
1. Every violation maps to a verbatim SCOTUS or controlling-circuit holding
2. Structural errors are NEVER harmless (Arizona v. Fulminante, 499 U.S. 279)
3. Recusal violations trigger Caperton analysis automatically
4. Never soften: if court ignored binding precedent → "ignored binding precedent"
5. Never write "the court may have erred" — write "the court erred under [cite]"
