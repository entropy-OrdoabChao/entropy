---
id: garnishee_identification
title: Identification of the Garnishee
applies_to_defects:
  - GARNISHEE_INTERROGATORIES_UNSERVED
  - GARNISHEE_INTERROGATORIES_UNRESPONSIVE
  - GARNISHEE_INTERROGATORIES_UNVERIFIED
  - GARNISHEE_DIRECT_CLAIM_IGNORED
required_authorities:
  - Pa.R.C.P. 3101 — "The judgment creditor may proceed against property of judgment debtor in hands of a third person by proceeding under this part."
required_facts:
  - case_id
  - caption
  - garnishee.name
  - garnishee.role
  - garnished_property
output_format: argument_section
---

## Identification of the Garnishee

### A. The Third Person Holding the Judgment Debtor's Property

Pa.R.C.P. 3101 authorizes the judgment creditor to "proceed against property of judgment debtor in hands of a third person by proceeding under this part." The third person so designated is the **garnishee**.

Here, the garnishee is {{garnishee.name}}, acting in the capacity of {{garnishee.role}}. The property subject to this garnishment process is:

> {{garnished_property}}

### B. Why This Party Is Properly Before the Court as Garnishee

The garnishee's relationship to the judgment debtor's property makes it the proper party to hold that property for execution:

1. **Possession or control.** {{garnishee.name}} holds, controls, or owes {{garnished_property}} to the judgment debtor — the very property Pa.R.C.P. 3101 reaches when it is "in hands of a third person."

2. **Named in the process.** The writ of execution and the garnishee interrogatories were directed to {{garnishee.name}}; that designation fixes its role in this proceeding.

3. **Duty to answer.** Once served with the writ of execution and interrogatories, the garnishee owes the duty to respond under oath (Pa.R.C.P. 3145(b)) — a duty that attaches by service, not by the garnishee's consent.

Because {{garnishee.name}} stands in possession of or owes property to the judgment debtor, it is the garnishee for purposes of Part 3145–3146 of the Pennsylvania Rules of Civil Procedure, and this motion proceeds against it in that capacity.
