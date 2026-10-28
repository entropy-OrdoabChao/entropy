---
name: regulatory-complaint
description: Build regulatory complaint packets from scorecard violations. Routes to correct agency (CFPB, OCC, FDIC, FTC, SEC, State AG, State Banking). Generates complaint with violation summary, mandatory duties, evidence index, and notice of legal pursuit. Tracks submissions and follow-ups.
version: 1.0.0
metadata:
  hermes:
    grounding: partial
    related_skills: [legal-scorecard, defense-simulator, legal-draft, exhibit-manager]
    tags: ["legal", "regulatory", "complaint", "CFPB", "OCC", "FDIC", "FTC", "escalation", "enforcement"]
    requires_toolsets: [terminal, file]
---

# Regulatory Complaint Builder

Converts proven violations into agency-ready complaint packets with automatic routing to the correct regulatory body. Includes notice of legal pursuit if the agency fails its duty.

## When to Use

- After `legal-scorecard` confirms violations
- When defendant is non-responsive or refuses to comply
- When user wants to escalate beyond private litigation
- When building a paper trail for willfulness/bad faith claims
- After every failed dispute (FCRA, FDCPA, TILA, RESPA, etc.)

## Agency Routing Table

Based on violation type, route to the correct agency:

### Federal Agencies

> ⚠️ **CORPUS GAP**: `12 USC 5534` (CFPB founding statute) returned 0 hits in search_text — NOT IN CORPUS. All other statutory citations verified by search_text (5 hits each). Verify all statutory references against corpus before use.

| Violation Type | Primary Agency | Contact | Backup Agency |
|---------------|---------------|---------|---------------|
| Credit reporting (FCRA) | CFPB | consumerfinance.gov/complaint | FTC |
| Debt collection (FDCPA) | CFPB | consumerfinance.gov/complaint | FTC, State AG |
| Truth in lending (TILA) | CFPB | consumerfinance.gov/complaint | OCC (if national bank) |
| Mortgage servicing (RESPA) | CFPB | consumerfinance.gov/complaint | HUD |
| Unfair/deceptive practices (UDAP) | FTC | reportfraud.ftc.gov | CFPB, State AG |
| National bank violations | OCC | helpwithmybank.gov | CFPB |
| FDIC-insured bank violations | FDIC | fdic.gov/resources/consumers | CFPB |
| Federal Reserve member bank | Fed Reserve | federalreserveconsumerhelp.gov | CFPB |
| Credit union violations | NCUA | mycreditunion.gov | CFPB |
| Securities violations | SEC | sec.gov/tcr | FINRA |
| Insurance (federal) | FIO (Treasury) | treasury.gov | State Insurance Dept |

### Pennsylvania State Agencies

| Violation Type | Agency | Contact |
|---------------|--------|---------|
| Insurance violations | PA Insurance Department | insurance.pa.gov |
| Banking violations | PA Dept of Banking & Securities | dobs.pa.gov |
| Consumer fraud (UTPCPL) | PA Attorney General | attorneygeneral.gov |
| Debt collection (state) | PA Attorney General | attorneygeneral.gov |

### How to Identify the Right Agency

```
1. What type of entity is the defendant?
   - National bank (OCC)
   - State bank, FDIC-insured (FDIC)
   - Credit union (NCUA)
   - Non-bank financial company (CFPB)
   - Insurance company (State Insurance Dept)
   - Any company (FTC for UDAP)

2. What statute was violated?
   - 15 USC 1681 (FCRA) → CFPB
   - 15 USC 1692 (FDCPA) → CFPB + FTC
   - 15 USC 1601 (TILA) → CFPB
   - 12 USC 2601 (RESPA) → CFPB
   - 73 P.S. § 201-1 (UTPCPL) → PA AG
   - Title 40 (PA Insurance) → PA Insurance Dept

3. ALWAYS file with BOTH:
   - Primary federal agency
   - State-level counterpart
```

## Complaint Packet Structure

> NOTE: User provides their own complaint text. This section lists the required structure.

Required sections (use user-provided content):
1. **Complaint Summary** — TO/FROM/DATE/RE, one-sentence violation description
2. **Violations** — From scorecard: description, statute, mandatory duty, status
3. **Mandatory Duties Breached** — "shall/must/required" language from statute, status: NOT PERFORMED
4. **Timeline** — User-provided dates/events
5. **Evidence Index** — Exhibit A/B/C with descriptions and page counts
6. **Notice of Conditional Escalation** — Preserves willfulness/bad faith record if agency fails to respond
7. **Agency Guidance** — When available: agency's own published standards confirming violations

See `cases/<slug>/drafts/` for complaint templates if needed.

## Contact Information

> NOTE: Use the agency URLs below. Contact details (emails, addresses, phone numbers) should be verified against the agency's current website before submission — they may change.

| Agency | Complaint URL |
|--------|--------------|
| CFPB | https://www.consumerfinance.gov/complaint/ |
| OCC | https://helpwithmybank.gov |
| FTC | https://reportfraud.ftc.gov |
| FDIC | https://www.fdic.gov/resources/consumers/ |
| NCUA | https://mycreditunion.gov/consumer-assistance-center |
| SEC | https://www.sec.gov/tcr |
| PA Insurance | https://www.insurance.pa.gov/Consumers/File%20a%20Complaint/Pages/default.aspx |
| PA Banking | https://www.dobs.pa.gov/consumers/filing-a-complaint/ |
| PA AG | https://www.attorneygeneral.gov/submit-a-complaint/ |

## Submission Recommendations

After generating the complaint packet, provide the user with:
- Primary agency URL (from the contact table above)
- Secondary agency URL (from the routing table below)
- State agency URL (from the routing table below)
- Timing: file primary TODAY, mail certified copy within 48 hours, secondary+state within 3 days
- Track complaint IDs from each submission

See routing table below for which agencies to file with.

## Follow-Up Generation

After the response deadline passes, generate a follow-up notice referencing:
- Original complaint date and agency
- Response deadline and status (NO SUBSTANTIVE RESPONSE)
- Items not addressed (from original complaint)
- Reiterate the Notice of Legal Pursuit from the original complaint

## Integration in Pipeline

legal-scorecard (violations confirmed) → regulatory-complaint (route + generate) → legal-draft (private litigation) → exhibit-manager (compile)

## Anti-Hallucination

- Agency contact info must come from the database above — do not guess URLs or addresses
- Violations must come from the scorecard — do not add violations not proven
- Mandatory duties must cite exact statutory language
- Timeline must come from user-provided facts — do not assume dates
- If agency contact info may be outdated, note: "Verify current contact information before submission"

## Persistent Tracking

Save complaint records to: `~/.hermes/legal_data/regulatory_complaints.json`

Required fields per complaint: id, case, defendant, agency, agency_complaint_id, filed_date, method, response_deadline, response_received, response_summary, follow_ups, status, escalated_to_litigation, duty_status (duty, authority, status, evidence_of_failure, days_overdue).
