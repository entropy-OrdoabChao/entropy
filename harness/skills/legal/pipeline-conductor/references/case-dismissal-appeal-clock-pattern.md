# Case Dismissal + Appeal Clock Pattern

## Problem
When a case is dismissed, the 30-day appeal clock starts. A Notice of Appeal may or may not be filed. This must be tracked because the clock is unforgiving.

## Example (2026-07-09, fed_26-cv-03268)

- **Dismissal order:** July 9, 2026 (EDPA)
- **30-day appeal deadline:** August 8, 2026
- **Status:** NO Notice of Appeal filed yet
- **Ground truth stale:** CASE_GROUND_TRUTH.yaml still says "ifp_pending" — case was dismissed, status needs update
- **Follow-up:** File Notice of Appeal + IFP application within 30 days

## Detection
- ECF notification subject contains "Order Dismissing Case" or "Dismissal"
- Read the order to confirm dismissal and extract the effective date
- Compute 30-day appeal deadline from the effective date
- Check if a Notice of Appeal exists in intake/ or drafts/
- Update CASE_GROUND_TRUTH.yaml status (e.g., "dismissed; appeal pending" or "dismissed; no appeal filed")

## PITFALL
The 30-day appeal clock starts from the **effective date of the dismissal order**, not from the date the Notice of Dismissal is filed. Check the order text for the effective date. Also check if the dismissal was with or without prejudice — this affects whether a new action can be filed.

## Related
- `references/critical-appellate-signals.md` — Case Closed/Dismissed signal in beeze-ecf-monitoring
- `references/usca-appeal-reinstatement-pattern.md` — USCA reinstatement after dismissal
