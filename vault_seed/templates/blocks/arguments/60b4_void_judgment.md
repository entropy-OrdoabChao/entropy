---
id: 60b4_void_judgment
title: Void Judgment Argument — FRCP 60(b)(4)
applies_to_defects:
  - FATAL/VOID_JUDGMENT
  - FATAL/JURISDICTION
  - SERIOUS/VOID_JUDGMENT
  - judgment_contradicts_docket
  - void_order
required_authorities:
  - FRCP 60(b)(4) — Void judgment; no time limitation
  - United Student Aid Funds v. Espinosa, 559 U.S. 260 (2010) — Void judgment standard
  - 28 U.S.C. § 1655(a) — Judgment void for lack of jurisdiction
  - Fed. R. Civ. P. 4(l) — Proof of service
  - Fed. R. Evid. 201(b)(2) — Judicial notice
---

## Void Judgment Under FRCP 60(b)(4) — No Time Limit

### A. The Standard — Void vs. Voidable

A judgment is **void** (not merely voidable) when the court that entered it lacked:
1. **Subject-matter jurisdiction** — no power to hear the type of case; or
2. **Personal jurisdiction** — no power over the party; or
3. **Due process** — fundamental fairness was denied.

Per *United Student Aid Funds v. Espinosa*, 559 U.S. 260, 269 (2010), "a judgment is void if the rendering court lacked jurisdiction over the subject matter or the parties." A void judgment is a nullity — it has no legal force from its inception.

**Critically, FRCP 60(b)(4) carries NO TIME LIMIT.** Unlike subdivisions (b)(1)-(5) which are capped at one year, a void judgment can be challenged at any time. This is not a procedural technicality — it is a structural safeguard because a court without jurisdiction has no power to enter any judgment at all.

### B. Application — {{voidness_ground}}

{{#if_eq voidness_ground "docket_contradiction"}}
The judgment/order at issue rests on a factual finding directly contradicted by the court's own docket record. Where a court's factual finding is contradicted by a document filed of record in the same case, and no competent rebuttal evidence exists, the judgment lacks the foundation of factual accuracy and is void ab initio.

**IF** the court's judgment or order rests on a factual finding contradicted by the court's own docket record, AND no competent rebuttal evidence exists, THEN the judgment is void under FRCP 60(b)(4) and must be vacated.
{{/if_eq}}

{{#if_eq voidness_ground "lack_of_smj"}}
The court lacked subject-matter jurisdiction over this claim. Under 28 U.S.C. § 1331, federal question jurisdiction requires a valid federal claim. Where the underlying federal claim fails at the outset (e.g., the plaintiff has no standing, the claim is precluded, or the statute does not create a private right of action), the court has no subject-matter jurisdiction and any judgment it enters is void.

**IF** the court lacked subject-matter jurisdiction over the claim, THEN the judgment is void under FRCP 60(b)(4) regardless of when it was entered.
{{/if_eq}}

{{#if_eq voidness_ground "lack_of_pj"}}
The court lacked personal jurisdiction over the defendant. Under FRCP 4(h)(1)(B) and FRCP 4(e)(1), service on a corporation must comply with state law. Where proof of service is filed of record under FRCP 4(l), the docket itself establishes service unless rebutted by competent evidence.

**IF** the court's order finds that a party was not served when proof of service is filed of record, AND no rebuttal evidence exists, THEN the finding is contradicted by the record and the resulting judgment is void for lack of personal jurisdiction.
{{/if_eq}}

{{#if_eq voidness_ground "due_process_violation"}}
The court's order deprived the party of due process — the fundamental right to notice and an opportunity to be heard. Where a court's factual finding contradicts the court's own docket record, the party had no meaningful opportunity to defend against a finding that the record itself disproves.

**IF** the court's order relies on a factual finding contradicted by the court's own docket record, AND the party was denied an opportunity to present contrary evidence, THEN the order violates due process and is void under FRCP 60(b)(4).
{{/if_eq}}

### C. The Docket Contradiction — Direct Evidence of Voidness

The docket record is the court's own creation. Where a document is filed of record under FRCP 4(l) (proof of service), Fed. R. Evid. 201(b)(2) requires the court to take judicial notice of that record. A finding that contradicts the court's own record is not an error of judgment — it is an error of jurisdiction.

{{#if docket_contradiction_evidence}}
**Evidence of contradiction:** {{docket_contradiction_evidence}}
{{/if}}

### Authority

*United Student Aid Funds v. Espinosa*, 559 U.S. 260 (2010); *Hazel-Atlas Glass Co. v. Hartford-Empire Co.*, 322 U.S. 238 (1944); Fed. R. Civ. P. 60(b)(4), 60(c)(1); 28 U.S.C. § 1655(a); Fed. R. Civ. P. 4(l); Fed. R. Evid. 201(b)(2).
