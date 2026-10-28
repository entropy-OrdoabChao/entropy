# IRS Tax Recovery: Key Research Summary

**Date Created:** 2026-05-23  
**Source:** Manual browser research (MCP tools `chromadb-search` and `beezle-research` NOT AVAILABLE)  
**Full Report:** `{{ENTROPY_HOME}}\obsidian_sync\studies\irs_tax_recovery\IRS_TAX_RECOVERY_RESEARCH_REPORT.md`

---

## Quick Reference: Forms and Statutes

| Document | Purpose | Key Sections |
|----------|---------|--------------|
| **Form 1040-X** | Amended individual income tax return | Correct errors, claim new credits |
| **Form 843** | Claim for refund or abatement | Overpayment refunds, penalty abatement |
| **Form 1099-C** | Cancellation of debt reporting | Section 108 tax exclusions |
| **26 USC § 7422** | Civil actions for refund | File administrative claim BEFORE suit |
| **26 USC § 6330** | Collection Due Process hearings | Right to hearing before levy |

---

## Critical Requirements

### 26 USC § 7422 (Refund Claims)

**MANDATORY SEQUENCE:**
1. File administrative claim for refund (Form 843 or 1040-X)
2. Wait for IRS response (60-120 days)
3. File suit in district court ONLY if denied

**Statute of Limitations:** 2 years from claim allowance OR 2 years from payment date (whichever is later)

### 26 USC § 6330 (CDP Hearings)

**Levy Protection:**
- IRS must provide 30-day written notice before levy
- Taxpayer entitled to one hearing with Appeals officer
- Can raise: (1) whether tax is legally owed, (2) whether levy is appropriate given finances

**CDP Hearing Matters:**
- Underlying liability challenge
- Financial hardship evidence
- Installment agreement options
- Offer in compromise discussion
- Collection alternatives (lien withdrawal, levy release)

---

## Section 108 Debt Relief (Form 1099-C)

### Exclusion Codes

| Code | Type | Eligibility |
|------|------|-------------|
| 1A | Bankruptcy | Title 11 case |
| 1B | Insolvency | Assets < Liabilities |
| 2A | Qualified Farm Debt | Agricultural property |
| 2B | Qualified Business Debt | Business real property |
| 3A | Principal Residence | Primary home mortgage |
| 4A | Student Loan (Title 13) | Chapter 13 repayment |
| 4B | Qualified Student Loan | Federal program discharge |
| 5A | Nonrecourse Farm Debt | Qualified farm/ranch property |
| 5B | Business Asset Real Property | Qualified business debt |

### Evidence Required

- Financial statement (Form 656 or equivalent)
- Bankruptcy documentation (if applicable)
- Balance sheet showing insolvency
- Proof of qualified debt type
- **Form 982** (Reduction of Tax Attribute)

---

## Form 843: Abatement Requests

### When to Use

- Claim refund of overpayment
- Request abatement of penalties
- Request abatement of interest
- Claim refund of excise taxes

### Key Lines

- **Line 1:** Tax type
- **Line 2:** Penalty
- **Line 3:** Interest  
- **Line 4:** Other (specify)
- **Line 5:** Amount
- **Line 6-8:** Taxpayer info

### Common Uses

1. **Collection penalty abatement** — reasonable cause, not willful
2. **Overpayment refund** — original return filed in error
3. **Interest abatement** — limited circumstances

---

## Taxpayer Advocate Service (TAS)

### Mission
Independent IRS office helping taxpayers with unresolved problems.

### Eligibility

- **Financial hardship** due to IRS actions
- **Economic injury** from IRS law violations
- **No effective remedy** through normal channels
- **Imminent threat** of adverse action

### How to Request

1. Complete Form TA-1
2. Contact local Taxpayer Advocate
3. Call: 1-877-777-4778

### Success Factors

- Document prior resolution attempts
- Provide detailed hardship explanation
- Include financial documentation
- Act promptly

---

## Amended Return (1040-X) Strategy

### When to File

✅ **File 1040-X when:**
- IRS incorrectly assessed tax
- New information qualifies for refundable credits
- Discovering unreported deductions/credits
- Need to claim Section 108 debt relief
- Contesting penalty assessment

❌ **Do NOT amend when:**
- Need to report additional income
- Need to pay additional tax
- Responding to audit findings requiring payment

### Processing Timeline

- File as soon as error is identified
- Expect 6-12 weeks for refund processing
- Track status via IRS2Go or phone

---

## Evidence Compilation Checklist

### Essential Documents

- [ ] Original tax returns (last 3-7 years)
- [ ] All IRS notices and correspondence
- [ ] Levy notices (if applicable)
- [ ] Collection Due Process notices
- [ ] Payment records

### For 26 USC § 7422 Claims

- [ ] Administrative claim for refund
- [ ] IRS response to claim
- [ ] Overpayment calculation evidence
- [ ] Tax payment documentation

### For Form 843 Abatement

- [ ] Proof of reasonable cause
- [ ] Financial statements
- [ ] Good faith correspondence
- [ ] Non-willful conduct evidence

### For Section 108 Relief

- [ ] Financial statement (Form 656)
- [ ] Bankruptcy documentation (if applicable)
- [ ] Insolvency balance sheet
- [ ] Qualified debt proof
- [ ] Completed Form 982

### For CDP Hearings

- [ ] Financial statement (Form 433-A or 433-B)
- [ ] Proof of attempted income
- [ ] Medical documentation (if applicable)
- [ ] Employment verification
- [ ] Levy hardship evidence

---

## MCP Tool Fallback Pattern

**When `chromadb-search` or `beezle-research` NOT FOUND:**

1. Navigate to authoritative source manually
2. Extract statutory/regulatory text
3. Document URL and access date
4. Use `db-ingest` skill to add to ChromaDB
5. Proceed with task using retrieved authority

**Key sources:**
- Statutes: https://www.law.cornell.edu/uscode/text/26
- Forms: https://www.irs.gov/pub/irs-pdf/
- Instructions: https://www.irs.gov/instructions/

---

## Action Items for Future Sessions

1. Verify MCP tool availability before task
2. If tools unavailable, activate manual browser workflow
3. Document tool failures in session log
4. Ingest research findings via `db-ingest`
5. Reference this summary for quick lookup

---

**Note:** This is a condensed knowledge bank. For complete research, see full report linked above.