---
id: jurisdiction_smj_questionnaire
title: Jurisdiction / Subject-Matter Jurisdiction Questionnaire
required_facts:
  - case_id
  - court
  - caption
output_format: section
---

## II. JURISDICTIONAL AND SUBJECT-MATTER JURISDICTION QUESTIONNAIRE — Y/N

A court's authority to enter an order depends on the existence of subject-matter jurisdiction. The Court has an obligation to consider its own jurisdictional boundaries — it cannot waive jurisdiction by default.

### A. Subject-Matter Jurisdiction

> **Y/N:** Did the Court have subject-matter jurisdiction to enter the Order at Dkt {{latest_order.dkt_no}}?

The Court's subject-matter jurisdiction is anchored in the Constitution and federal statute. Under **Article III, § 2** of the U.S. Constitution, the judicial power extends to:
- Cases arising under the Constitution and laws of the United States;
- Cases of diversity of citizenship exceeding $75,000;
- Cases involving ambassadors, public ministers, and consuls;
- Controversies between the United States and a citizen, or between citizens of different states.

Under **28 U.S.C. § 1331**, the district courts have original jurisdiction over "all civil actions arising under the Constitution, laws, or treaties of the United States."

Under **28 U.S.C. § 1332**, the district courts have original jurisdiction over civil actions where the matter in controversy exceeds $75,000 and is between citizens of different States.

> **IF** the Court entered the Order at Dkt {{latest_order.dkt_no}} without a valid basis for subject-matter jurisdiction, AND the Court's oath demands that the judge act only within the bounds of the law, THEN the Order is void *ab initio*.

### B. Personal Jurisdiction

> **Y/N:** Did the Court have personal jurisdiction over the party or entity against which the Order at Dkt {{latest_order.dkt_no}} was directed?

Under **28 U.S.C. § 1391**, venue is proper where:
- Any defendant resides (if all defendants reside in the same State);
- A substantial part of the events or omissions giving rise to the claim occurred;
- If no venue is proper under the above, where any defendant is subject to the court's personal jurisdiction.

Under **International Shoe Co. v. Washington**, 326 U.S. 310 (1945), and its progeny, the minimum contacts test determines personal jurisdiction.

> **IF** the Court entered the Order at Dkt {{latest_order.dkt_no}} against a party over whom it lacked personal jurisdiction, THEN the Order is void.

### C. Supplemental Jurisdiction

> **Y/N:** Did the Court have supplemental jurisdiction under **28 U.S.C. § 1367** over any state-law claims or parties?

Under **28 U.S.C. § 1367(a)**, a district court has supplemental jurisdiction over claims that are so related to claims within its original jurisdiction that they form part of the same case or controversy under Article III.

Under **28 U.S.C. § 1367(c)**, a district court may decline supplemental jurisdiction in certain circumstances.

> **IF** the Court exercised supplemental jurisdiction without a valid Article III nexus, THEN the supplemental jurisdiction is improper.

### D. Removal Jurisdiction

> **Y/N:** If the Order at Dkt {{latest_order.dkt_no}} involves a removed claim, did the removing party satisfy **28 U.S.C. § 1441** and **§ 1446** requirements?

Under **28 U.S.C. § 1441**, any civil action brought in a State court of which the district courts of the United States have original jurisdiction may be removed by the defendant. Under **28 U.S.C. § 1446**, the removal petition must be filed within 30 days after receipt by the defendant of the initial pleading.

> **IF** the Court entered the Order at Dkt {{latest_order.dkt_no}} on a removed claim that was not properly removed under §§ 1441 and 1446, THEN the Court lacked jurisdiction to enter that Order.
