---
id: haines_v_kerner_standard
title: Haines v. Kerner Dismissal Standard
applies_to_defects:
  - SERIOUS/DISMISSAL
  - FATAL/DISMISSAL
required_authorities:
  - Haines v. Kerner, 404 U.S. 519 (1972)
  - FRCP 12(b)
  - Pa.R.C.P. 1031
required_facts:
  - case_id
  - caption
  - court
  - dismissal_type
output_format: argument_section
mode: deterministic_if_then
---

## Dismissal Fails the Haines v. Kerner Standard

**Rule.** When a dismissal is challenged, the trial court must apply the standard set forth in *Haines v. Kerner*, 404 U.S. 519 (1972). Under *Haines*, dismissal is improper unless "no set of facts could entitle the plaintiff to relief." This standard requires the court to evaluate whether the complaint's allegations, taken as true, could support a valid claim -- not whether the plaintiff will ultimately prevail.

**Application.**

> **IF** the dismissal in **{{caption}}** ({{court}}) was challenged by motion to dismiss,
> **AND** the record shows that the dismissal was entered without determining whether any set of facts could entitle relief,
> **THEN** the dismissal fails to meet the *Haines* standard and must be vacated or reconsidered.

> **IF** the challenged dismissal disregards the plaintiff's factual allegations or misstates the legal standard for dismissal,
> **AND** the dismissal was entered without proper application of *Haines*,
> **THEN** the dismissal constitutes reversible error and the proper remedy is to remand for reconsideration under the correct standard.

**Authority.** The *Haines* standard is the controlling standard for dismissal challenges under FRCP 12(b) and Pa.R.C.P. 1031. See *Haines v. Kerner*, 404 U.S. 519 (1972); see also *Conley v. Gibson*, 355 U.S. 41 (1957) (abrogated but *Haines* preserves the substance of the notice pleading standard).

**Conclusion.** The dismissal in this case fails to meet the *Haines* standard. Plaintiff respectfully submits that the dismissal should be vacated and the matter remanded for reconsideration consistent with *Haines v. Kerner*.
