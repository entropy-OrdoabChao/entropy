---
id: factual_statement_enforcement
title: Factual Statement (Post-Judgment / Enforcement Posture)
required_facts:
  - case_id
  - caption
  - latest_order
output_format: factual_section
mode: deterministic
---
## FACTUAL STATEMENT

### Underlying Judgment

On {{latest_order_date}}, the Honorable {{judge.name}} entered a money judgment against {{defendants_rendered}} in the captioned action, **{{caption}}**, No. {{registry.docket_aliases.0}}. {{latest_order_summary}}

### Garnishment Posture

Plaintiff initiated post-judgment enforcement proceedings pursuant to Pa.R.C.P. 3101 *et seq.* and 3146(b), naming the following parties as garnishees:

{{garnishees_rendered}}

### Pending Motions

The following motions are presently pending before the Court without ruling:

{{pending_motions_rendered}}

### Relief Sought

By this Motion, Plaintiff seeks the relief identified in the Prayer for Relief, on the factual and legal grounds set forth herein.
