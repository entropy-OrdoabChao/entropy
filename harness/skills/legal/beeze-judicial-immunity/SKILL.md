---
name: beeze-judicial-immunity
description: Judicial immunity analysis and accountability mechanism mapping. Covers the four exceptions to absolute judicial immunity (when judges CAN be sued), immunity analysis for judges identified in case audits (when immunity applies), available causes of action, and comprehensive accountability mechanisms.
status: hold
---

# Beezle Judicial Immunity Analyzer

## Purpose
Identify when judges can be sued personally despite absolute judicial immunity. Analyzes judicial acts to determine if they fall outside the scope of immunity.

## What it does
- Takes a judge's actions or conduct and classifies whether immunity applies
- Identifies the four exceptions to absolute judicial immunity
- Provides case law precedent for each exception
- Returns structured immunity analysis with severity classification

## The Four Exceptions to Absolute Judicial Immunity

### 1. EXECUTIVE FUNCTION — Judge acts as law enforcement
When a judge personally conducts law enforcement activities rather than issuing judicial orders.

**Key precedent:** Gibson v. Goldston, No. 22-1757 (4th Cir. 2023) — NOT IN CORPUS (not found in search_chunks/resolve_citation). Judge personally led warrantless search of litigant's home, directed search party, approved seizures. Court held: "judges do not do double duty as sheriffs." Fifth time in 50 years a federal appeals court denied judicial immunity for exercising executive power. See `references/gibson-v-goldston-executive-function.md` for full details.

**How to prove:** Show the judge personally executed a search, directed law enforcement, seized property, or performed any function normally done by law enforcement officers.

**See:** `references/gibson-v-goldston-executive-function.md`

### 2. ADMINISTRATIVE FUNCTION — Judge acts as employer
When a judge makes employment decisions rather than judicial decisions.

**Key precedent:** Forrester v. White, 484 U.S. 219 (1988) — Judge fired probation officer. Supreme Court held: "judges do not have absolute immunity in performing administrative functions." Judge found liable for $81,818.80 in compensatory damages.

**How to prove:** Show the judge made hiring, firing, demotion, or other employment decisions rather than judicial decisions.

### 3. CLEAR ABSENCE OF ALL JURISDICTION — Judge acts with no jurisdiction at all
When the judge has no jurisdiction over the subject matter or the parties.

**Key precedent:** Stump v. Sparkman, 435 U.S. 349 (1978) — The exception. Judge approved sterilization of minor without hearing. Immunity applied because judge had subject matter jurisdiction over probate matters. The exception requires the judge to have NO jurisdiction whatsoever — not just excess of jurisdiction.

**How to prove:** Show the judge had no jurisdiction over the subject matter (e.g., a probate judge trying a felony) or over the parties.

**See:** `references/clear-absence-of-jurisdiction.md`

### 4. PERSONAL CAPACITY — Judge acts as private citizen
When the judge is not acting in any official capacity.

**How to prove:** Show the judge was acting as a private citizen — e.g., a traffic accident, personal business dispute, or any activity not connected to judicial duties.

## Key Cases

### Gibson v. Goldston, 22-1757 (4th Cir. 2023) — NOT IN CORPUS
- Judge personally led warrantless search of litigant's home during divorce
- Judge forced entry under threat of arrest, directed search party, approved seizures
- 4th Circuit: immunity does not shield judges who step outside judicial role
- "Judges do not do double duty as sheriffs"
- Judge later censured and fined by West Virginia Supreme Court

### Forrester v. White, 484 U.S. 219 (1988) — NOT IN CORPUS
- Judge fired probation officer
- Supreme Court: administrative functions not protected
- Judge found liable for $81,818.80 in compensatory damages

### Mireles v. Waco, 502 U.S. 9 (1991) — NOT IN CORPUS
- Judge ordered police to use excessive force to bring lawyer into courtroom
- Supreme Court: immunity applied — ordering police is a judicial act
- This is the EXCEPTION to the executive function exception — ordering police is still judicial

### Williams v. Pennsylvania, 579 U.S. 578 (2016) — NOT IN CORPUS
- Judge refused to recuse despite previously being prosecutor in case
- Supreme Court: due process violation, but did not eliminate judicial immunity

## Legal Framework
- 42 U.S.C. § 1983 — proper vehicle for suing state judges personally
- Compensatory and punitive damages available under § 1983
- Attorney's fees available under 42 U.S.C. § 1988 if prevailing
- Injunctive relief available under § 1983 (but NOT against judicial officers for judicial acts — only if declaratory decree was violated or declaratory relief unavailable)

## Workflow
1. Identify the judge's actions/conduct in question
2. Classify the action: judicial, executive, administrative, or personal
3. If executive/administrative/personal — immunity does not apply
4. If judicial — check for clear absence of all jurisdiction
5. If no jurisdiction — immunity does not apply
6. If jurisdiction exists — immunity applies (unless one of the four exceptions)

## Pitfalls
- **PITFALL:** Don't confuse "excess of jurisdiction" with "clear absence of all jurisdiction." A judge who exceeds jurisdiction is still immune (Stump v. Sparkman). The exception requires the judge to have NO jurisdiction whatsoever.
- **PITFALL:** Don't assume ordering police is an executive function. Mireles v. Waco holds that ordering police to bring someone into court is a judicial act.
- **PITFALL:** Don't try to sue a judge for a judicial act under § 1983. The only way is to prove one of the four exceptions.
- **PITFALL:** Don't forget that the Gibson case was only the fifth time in 50 years a federal appeals court denied judicial immunity for executive function. The courts are very reluctant to find that a judge was acting in an executive function.
- **PITFALL:** Immunity analysis assumes judge role — When running `analyze_immunity()`, the `defendant_type` must be 'judge' for judicial immunity analysis. Using 'government_official' will return qualified immunity analysis instead. Always verify the defendant type matches the role being analyzed.
- **PITFALL:** Existing reports in vault — Before running a new immunity analysis, check the vault for existing reports (`judicial_immunity_analysis_report.md`, `judicial-immunity-analysis-report.md`, `judicial_immunity_analysis.json`). These may contain prior analysis that should be referenced rather than re-run.
- **PITFALL:** All SCOTUS immunity cases are NOT IN CORPUS — Forrester v. White, 484 U.S. 219; Stump v. Sparkman, 435 U.S. 349; Mireles v. Waco, 502 U.S. 9; Williams v. Pennsylvania, 579 U.S. 578 — all returned `not_found` from `resolve_citation(mode='direct_lookup')`. These must be retrieved via CourtListener (mcp_beezle_search_case_law) for citation-resolution-speed grounding.

## Support Files

- `references/gibson-v-goldston-executive-function.md` — Full details of the Gibson v. Goldston precedent
- `references/clear-absence-of-jurisdiction.md` — Analysis of the jurisdictional exception
- `references/accountability-mechanisms-reference.md` — Comprehensive accountability mechanism reference when immunity applies