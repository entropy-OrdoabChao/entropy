# USCA Appeal Reinstatement Pattern

## Problem
The USCA may reinstate a dismissed appeal and set new briefing deadlines. This is a major change in case posture.

## Example (2026-07-15, {{CASE_SLUG}})

- **USCA No. {{CASE_ID}}** — previously dismissed May 15, 2026 for failure to file brief/appx
- **Reinstatement order:** July 15, 2026 — appeal reinstated because appellant filed a brief on November 21, 2025
- **New deadline:** Appellees' brief due within 30 days of the reinstatement order = August 14, 2026
- **Follow-up:** Prepare reply brief; check if appellant brief has been filed

## Detection
- ECF notification subject contains "USCA Order" or "USCA Notice"
- Intake file named `dkt_<NNN>_USCA_Order_*.pdf`
- Read the order text to determine: (a) was the appeal reinstated or dismissed? (b) what are the new deadlines?
- Update CASE_GROUND_TRUTH.yaml status accordingly

## PITFALL
A USCA order may appear without a corresponding ECF notification email — it may only appear as an intake file. Always check intake/ for USCA orders even when no ECF notification is found.

## Related
- `references/critical-appellate-signals.md` — Appeal Reinstated signal in beeze-ecf-monitoring
- `references/case-folder-splitting-pattern.md` — USCA orders may land in split folders
