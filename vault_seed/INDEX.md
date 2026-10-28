# INDEX

Master case index. Auto-generated from case_registry.json. Hermes reads this
to resolve case_id <-> slug <-> folder path. To enumerate all cases
programmatically, call the list_cases() MCP tool.

Last update: 2026-05-14T22:07:10

## Quick lookup

| Case ID | Slug | Folder | Caption | Status | Has CASE_FACTS | Has TIMELINE |
|---|---|---|---|---|:-:|:-:|
| `{{CASE_SLUG}}` | `{{CASE_SLUG}}` | `cases/{{CASE_SLUG}}/` |  |  | Y | Y |
| `{{CASE_ID}}` | `{{CASE_SLUG}}` | `cases/{{CASE_SLUG}}/` | {{USER_LAST}} VS EQUIFAX INFORMATION SERVICES, LLC ETAL | trial_court_pending_FRCP_54b_certification | Y | Y |
| `{{CASE_ID}}` | `fed_amerisave` | `cases/fed_amerisave/` | {{USER_LAST}} VS AMERISAVE MORTGAGE CORPORATION ETAL | appellate_briefing_scheduled | Y | Y |
| `{{CASE_ID}}` | `fed_statefarm` | `cases/fed_statefarm/` | {{USER_LAST}} VS STATE FARM ETAL | merits_mandamus_pending | Y | Y |
| `{{CASE_SLUG}}` | `{{CASE_SLUG}}` | `cases/{{CASE_SLUG}}/` |  |  | - | - |
| `{{CASE_SLUG}}` | `{{CASE_SLUG}}` | `cases/{{CASE_SLUG}}/` |  |  | Y | - |
| `{{CASE_SLUG}}` | `{{CASE_SLUG}}` | `cases/{{CASE_SLUG}}/` |  |  | Y | Y |
| `{{CASE_ID}}` | `state_lippi` | `cases/state_lippi/` | {{USER_LAST}} VS {{OPPOSING_PARTY}} ETAL | judgment_by_default_active_motions_to_strike | Y | Y |
| `{{CASE_SLUG}}` | `{{CASE_SLUG}}` | `cases/{{CASE_SLUG}}/` |  |  | Y | Y |

## Files in each case folder

- `CASE_FACTS.md` - strategy + posture (hand-curated + Hermes write-back). Read first.
- `CASE_TIMELINE.json` - structured timeline (cron-managed)
- `CASE_TIMELINE.md` - human-readable timeline
- `intake/` - raw documents (orders, motions, exhibits)
- `drafts/` - generated motions awaiting review/filing

## Slug rule

Federal/court cases: slug is the docket number with a court-type prefix,
e.g. case_id `{{CASE_ID}}` -> slug `{{CASE_SLUG}}`.
Regulatory/FOIA matters with no court docket: slug is a short agency identifier.

## Templates

- [[short-demand-email-reply]] — 5-sentence demand email for single wrongs
- [[foia_case_type]] — FOIA/Privacy Act case type definition
- [[foia_case_type_cfpb]] — FOIA case type for CFPB matters
- [[schema_contract]] — Schema contract reference

## Workflow

1. New document -> `cases/<slug>/intake/`
2. `analyze_intake_and_update` cron updates CASE_TIMELINE
3. `refresh_case_facts` cron updates cron-managed sections of CASE_FACTS
4. Hermes: list_cases() or INDEX -> CASE_FACTS -> CASE_TIMELINE -> drafts
5. User reviews and files
