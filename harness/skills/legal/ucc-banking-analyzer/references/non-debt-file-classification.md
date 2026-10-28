# Non-Debt File Classification — Decision Tree

## Quick Decision Tree

When a new file appears in intake, ask:

1. **Is it a court document?** (docket activity, order, brief, complaint, motion)
   - → NOT a debt instrument. Classify as "court document."
   - Example: `dkt_###_activity_in_case_*.pdf`, `3dCir_{{CASE_ID}}_ORDER_briefing_schedule_*.pdf`

2. **Is it an IRS form?** (1099-C, Account Transcript, Wage and Income Transcript)
   - → NOT a negotiable instrument. Classify as "IRS informational document."
   - Note: 1099-C is an IRS form, NOT a bill of exchange under UCC 3-104.

3. **Is it a credit card statement?** (QuicksilverOne statement, charge-off notice)
   - → NOT a negotiable instrument. Credit card statements are informational documents.
   - They fail ALL five UCC 3-104 criteria: no unconditional promise, no fixed amount, not payable to bearer/order, no maturity date, contains additional instructions.

4. **Is it a SEC prospectus or merger proxy?** (424(b) prospectus, merger proxy statement)
   - → NOT a debt instrument. These are securities registration documents.
   - They describe the issuer's business, capital structure, and transaction mechanics — not a debt obligation owed by the plaintiff.

5. **Is it a power of attorney / affidavit?**
   - → NOT a debt instrument. These are legal documents establishing authority or status.
   - Example: POA FINAL + SECURED.pdf, Affidavit of Status as Secured Party and Creditor

6. **Is it a UCC-1 Financing Statement?**
   - → NOT a debt instrument. This is a filing document, not a debt obligation.
   - Note: Self-filed UCC-1 (debtor = secured party) is legally ineffective under UCC § 9-203.

7. **Is it a promissory note?** (contains "I promise to pay" language)
   - → POTENTIALLY a negotiable instrument. Apply UCC 3-104 test.
   - But check for non-negotiable instrument clause (e.g., Section 20 of LoanContract.pdf).

8. **Is it a mortgage / security instrument?**
   - → Real property collateral. NOT governed by UCC Article 9.
   - Perfection occurs through real property recording, not UCC-1 filing.

9. **Is it a credit card charge-off notice?**
   - → NOT a 1099-C form. Charge-off is an accounting action, not debt cancellation.
   - Verify whether the creditor actually issued Form 1099-C for the charged-off balance.

## Summary

If the document does not contain a direct promise/order to pay a fixed sum of money from one party to another, it is NOT a debt instrument for UCC 3-104 purposes.
