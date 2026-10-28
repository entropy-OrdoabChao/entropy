---
name: ucc-banking-analyzer
description: Build UCC and banking law claims — for suing banks and other
  financial institutions when they violate the UCC or banking laws. Never let
  a financial violation go unchallenged.
when_to_use: When a bank or financial institution has violated the UCC or
  banking laws. Always load before any banking challenge.
---

# UCC and Banking Law Analyzer

## Overview

The UCC provides multiple remedies for banks and financial institutions that
violate its provisions. This skill builds those claims.

## UCC Title Structure

### Title 1 — General Provisions
- Definitions and interpretation rules
- Scope of the UCC
- Conflict of laws rules

### Title 2 — Sales
- Contract formation
- Performance
- Remedies for breach

### Title 3 — Negotiable Instruments
- Negotiable instruments definition
- Transfer and negotiation
- Holder in due course
- Discharge of instruments

### Title 4 — Bank Deposits and Collections
- Bank's duty to customer
- Collection of items
- Presentment and payment
- Refusal to pay

### Title 5 — Letters of Credit
- Issuance and amendment
- Presentation and honor
- Discharge and reimbursement

### Title 6 — Bulk Transfers
- Bulk sales
- Transfer of business assets

### Title 7 — Documents of Title
- Warehouse receipts
- Bills of lading
- Document of title rules

### Title 8 — Investment Securities
- Issuance and registration
- Transfer and endorsement
- Adverse claims
- Protected purchasers

### Title 9 — Secured Transactions
- Attachment of security interests
- Perfection of security interests
- Priority of security interests
- Default and disposition
- Discharge of obligations

## Key Banking Concepts

### UCC Article 3 — Negotiable Instruments

Under Article 3, a negotiable instrument is:
- A signed writing
- Containing an unconditional promise or order
- To pay a fixed amount of money
- Payable on demand or at a definite time
- Payable to order or to bearer

Discharge under Article 3 occurs when:
- Payment is made to the holder
- The instrument is cancelled
- The instrument is surrendered
- The payor bank pays the instrument
- The payor bank cancels the instrument

### UCC Article 4 — Bank Deposits and Collections

Under Article 4, a bank's duties include:
- Acting with ordinary care in presenting items
- Promptly notifying customers of dishonor
- Refunding payments when a bank fails to exercise ordinary care

### UCC Article 9 — Secured Transactions

Under Article 9, a secured party's rights include:
- Taking possession of collateral after default
- Disposing of collateral after default
- Applying proceeds from disposition to the debt
- Deficiency judgment if proceeds are insufficient

### UCC Article 4A — Funds Transfer

Under Article 4A, a funds transfer is:
- An electronic transfer of funds
- From one bank to another
- Through an intermediary bank
- With confirmation of payment

### Ledger Balancing

Ledger balancing is the process of:
- Matching deposits and withdrawals
- Ensuring the account balance is accurate
- Reconciling transactions
- Identifying discrepancies

## Workflow

### Step 1: Query the corpus for the UCC/banking violation

    GET http://localhost:8100/search_text?query=UCC+VIOLATION+statute+text&top_k=10

### Step 2: Cross-reference with Beezle MCP

- mcp_beezle_analyze_burden — analyze burden of proof for the claim
- mcp_beezle_analyze_remedies — map available remedies under UCC/banking law
- mcp_beezle_analyze_triggers — extract trigger conditions for the claim

### Step 3: Build the claim

For each UCC/banking violation, document:
- The violation (what the bank did wrong)
- The UCC title (which title was violated)
- The citation (which statute or rule establishes the bank's duty)
- The damages (what harm resulted from the violation)

### Step 4: Write to Obsidian

Write the claim to Obsidian:
    write_obsidian_note(subdir=SUBDIR, filename=FILENAME, content=CONTENT)

### Step 5: Add wikilinks

Add wikilinks to the claim:
    obsidian_propose_link(from_note=CLAIM_NOTE, to_note=CASE_NOTE, edge_type=authority, citation=CITATION)

## Key patterns

### UCC Article 3 — Negotiable Instrument Discharge

1. GET /search_text?query=UCC+Article+3+negotiable+instrument+discharge
2. Document the discharge
3. Build the claim: Bank failed to properly discharge the instrument
4. Write to Obsidian: ucc_claim_article3_discharge.md

### UCC Article 4 — Bank's Duty to Customer

1. GET /search_text?query=UCC+Article+4+bank+customer+duty
2. Document the duty violation
3. Build the claim: Bank breached its duty to the customer
4. Write to Obsidian: ucc_claim_article4_duty.md

### UCC Article 9 — Secured Transaction Dispute

1. GET /search_text?query=UCC+Article+9+secured+transaction+dispute
2. Document the dispute
3. Build the claim: Bank improperly disposed of collateral
4. Write to Obsidian: ucc_claim_article9_dispute.md

### Ledger Balancing Dispute

1. GET /search_text?query=UCC+ledger+balancing+dispute
2. Document the dispute
3. Build the claim: Bank failed to properly balance the ledger
4. Write to Obsidian: ucc_claim_ledger_dispute.md

## Pitfalls

- Don't assume a bank's action is valid — verify the UCC provisions
- Don't assume a bank's action is final — verify the dispute process
- Don't assume a bank's action is willful — verify the bank's intent
- Don't build a claim without documenting the damages
- **UCC Art 9 applies only to personal property:** Real property mortgages (including FHA/VA mortgage notes) are NOT governed by UCC Article 9. Article 9 applies to personal property collateral. Perfection of real property security interests occurs through real property recording (county recorder's office), NOT UCC-1 filing. When analyzing a promissory note secured by real property, confirm the collateral type before applying Article 9 tests.
- **Debtor = secured party circular arrangement:** If a debtor files a UCC-1 financing statement against himself, claiming both debtor and secured party status, the security interest does NOT attach under UCC § 9-203. The debtor and secured party must be different parties for a valid Article 9 security interest. The filing may be accepted by the state but is legally ineffective. See `references/self-filed-ucc1.md` for the full pattern.
- **Non-negotiable instrument clauses:** Some promissory notes contain clauses explicitly stating "This Note is not a negotiable instrument." These clauses are valid and enforceable — the note is governed by general contract law, not UCC Article 3. Always check Section 20 or similar provisions for non-negotiable instrument declarations.
- **Non-debt files in intake:** New files may be court orders, intake logs, or other non-debt documents (e.g., 3dCir_{{CASE_ID}}_ORDER_briefing_schedule — 3rd Circuit court order; intake_analysis_log.md). These are NOT debt instruments and should NOT trigger debt discharge analysis. Verify file type before classifying. See `references/non-debt-file-classification.md` for the decision tree.

<!-- BEEZLE-BRIDGE-START -->
## 🔗 Wired to Beezle (synced 2026-06-12)

*This section is auto-generated by `_sync_hermes_beezle_v2.py`. Everything
above this marker is Hermes's own work and is preserved verbatim. The
Beezle modules below are the production implementations that execute what
this skill describes.*

**Category**: `specialized`

**Primary Beezle module**: `regulatory.mcp_tools_block_9a`

**Direct invocation**:

```bash
cd {{ENTROPY_HOME}}
python3 -m regulatory.mcp_tools_block_9a
```

**Also uses**:

- `regulatory.mcp_tools_block_9b`


**Tag**: `ucc_banking_violation` (see `{{ENTROPY_HOME}}/skills/skill_tags.yaml` for parameterized invocation)


**Registry entry**: `{{ENTROPY_HOME}}/skills/registry.json` (key: `ucc-banking-analyzer`)

**Master dispatcher**: `pipeline.chain_runner` (122 KB) — the production
orchestrator that calls all skills in sequence.

**Self-learning hook**: if this skill produces a filed motion, the outcome
is logged to `emma_intel.db.motion_outcomes` via `pipeline.outcome_writer`.
That table feeds back into `pipeline.learning_loop`.

**Source-of-truth principle**: Hermes documents what this skill DOES.
Beezle's pipeline IS what this skill does. Do not duplicate logic in
this SKILL.md — if behavior needs to change, edit the Beezle module.
<!-- BEEZLE-BRIDGE-END -->

