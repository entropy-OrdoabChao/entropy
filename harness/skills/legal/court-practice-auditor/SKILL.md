---
name: court-practice-auditor
description: Audits court local rules, standing orders, and documented practices against binding higher authority (statutes, FRCP, controlling precedent, due process). Surfaces practices that conflict with binding law as due process violations.
version: 1.0.0
author: Hermes Legal
metadata:
  hermes:
    tags: [legal, local-rules, standing-orders, due-process, jurisdiction-aware]
    related_skills: [due-process-violation-engine, binding-precedent-enforcer, mandatory-vs-practice-comparator]
prerequisites:
  files: [~/.hermes/legal_config.json]
---

# Court Practice Auditor

## Purpose
Court practice that contradicts a higher-authority rule is not just inconvenient — it is a deviation from binding law. This skill surfaces every such deviation and ties it to a remedy.

## Sources audited
- Federal local rules (e.g., L.R. EDPA)
- State court local rules
- Standing orders (per-judge)
- Court "practice manuals" / pro se handbooks
- Filing fee schedules / clerk procedures
- Scheduling orders that impose burdens beyond FRCP

## Tests applied to each practice
1. **Higher-rule conflict** — does practice add requirements not in FRCP / state procedural rules?
2. **Statutory conflict** — does practice contradict an enacted statute?
3. **Binding precedent conflict** — does practice contradict SCOTUS / controlling circuit?
4. **Due process conflict** — does practice impair notice, hearing, neutral arbiter, or appellate review?
5. **Equal protection** — does practice burden pro se differently from represented parties?
6. **Access to courts** — does practice impair *Bounds v. Smith*, 430 U.S. 817 (1977) / *Christopher v. Harbury*, 536 U.S. 403 (2002)?

## Usage
```bash
# Audit a single rule/order
~/.hermes/skills/legal/court-practice-auditor/scripts/audit.sh \
  --practice-file /tmp/local_rule_7.1.txt \
  --jurisdiction edpa \
  --output /tmp/practice_audit.json

# Audit an entire local rules document
~/.hermes/skills/legal/court-practice-auditor/scripts/audit.sh \
  --practice-dir /data/courts/edpa/local_rules/ \
  --jurisdiction edpa \
  --output /tmp/edpa_audit.json
```

## Output Format
```json
{
  "practice": "verbatim text of rule/order",
  "source": {"court": "E.D. Pa.", "judge": null, "document": "L.R. 7.1(c)"},
  "tests": {
    "higher_rule_conflict": {"violated": true, "higher_rule": "Fed. R. Civ. P. 7", "explanation": "..."},
    "statutory_conflict": {"violated": false},
    "binding_precedent_conflict": {"violated": true, "case": "...", "cite": "..."},
    "due_process_conflict": {"violated": true, "prong": "right_to_be_heard"},
    "equal_protection": {"violated": false},
    "access_to_courts": {"violated": false}
  },
  "verdict": "DEVIATION",
  "remedies": [
    {"type": "rule_29_motion", "auth": "FRAP 29 / local rule challenge"},
    {"type": "1983", "auth": "42 U.S.C. § 1983 (against court actor)"},
    {"type": "mandamus", "auth": "28 U.S.C. § 1651"}
  ]
}
```

## Rules
1. Audit verbatim text only — no paraphrase of practices
2. Where practice ≠ binding rule → state the deviation, do not soften
3. Per-judge standing orders are audited individually
4. Output is jurisdiction-scoped via `--jurisdiction`
