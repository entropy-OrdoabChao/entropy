# Beezle Case Registry Structure

**Location**: `{{ENTROPY_HOME}}/cases_index/case_registry.json`

## Schema

```json
{
  "canonical_id": "{{CASE_ID}}",
  "case_court": "PA_CP_PHILA",
  "caption": "{{USER_LAST}} VS {{OPPOSING_PARTY}} ETAL",
  "filed": "2024-10-21",
  "status": "judgment_by_default_active_motions_to_strike",
  "case_type": "contracts_other",
  "judge": "{{JUDGE}}",
  "docket_aliases": ["{{CASE_ID}}", "{{CASE_ID}}", "{{CASE_ID}}"],
  "federal_cross_ref": null,
  "state_origin_docket": null,
  "parties": ["{{USER_LAST}}", "{{OPPOSING_PARTY}}", "state_farm", "northern_trust"],
  "related_cases": ["{{CASE_ID}}"],
  "notes": ["..."],
  "engine_facts": ["agreement existed", "performance not received", ...],
  "factual_application": {"_default": "...", "breach_of_contract_general": "..."},
  "counsel_of_record": [{"name": "...", "role": "..."}],
  "claim_priority": ["breach_of_contract_general", "conversion", ...],
  "filing_court_address": "..."
}
```

## Canonical ID Format

- **State (PA CCP)**: `{county_prefix}:{case_number}` → `{{CASE_ID}}`
- **Federal**: `fed:{case_number}` → `{{CASE_ID}}`

## Status Values (examples)

- `pre_filing` — Not yet filed
- `active_discovery` — Discovery phase
- `judgment_by_default_active_motions_to_strike` — Default entered, motions pending
- `appellate_ifp_pending` — In forma pauperis appeal pending
- `appellate_briefing_scheduled` — Briefing dates set
- `merits_mandamus_pending` — Writ of mandamus pending

## Court Codes

- `PA_CP_PHILA` — Philadelphia County Court of Common Pleas
- `EDPA` — Eastern District of Pennsylvania
- `CA3` — Third Circuit Court of Appeals

## Other Registry Files

- `case_map.json` — Low-level document-to-case mapping
- `docket_sheets/` — Docket sheet snapshots per case
- `unassigned_documents.json` — Documents not yet linked to a case
