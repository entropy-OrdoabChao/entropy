# Canonical ID and Obsidian Folder Naming Conventions

## Registry Canonical IDs

The Beezle registry uses canonical IDs in the format `{court_prefix}:{case_number}`.

### Court Prefixes
- `fed:` — Federal district court cases (EDPA, etc.)
- `phila_ccp:` — Philadelphia County Court of Common Pleas
- `cfpb:` — Consumer Financial Protection Bureau FOIA cases
- `state:` — State courts (Pennsylvania, etc.)

### Case Number Formats
- **Federal**: `{{CASE_ID}}` (year:term-civil-docket)
- **Phila CCP**: `{{CASE_ID}}` (8 digits: YYYYDDDDDD)
- **CFPB**: `2026-0041` (year-sequence)

### Examples
- `{{CASE_ID}}` — EDPA FCRA case ({{USER_LAST}} v. Equifax)
- `{{CASE_ID}}` — Philadelphia CCP case ({{OPPOSING_PARTY}})
- `cfpb:2026-0041` — CFPB FOIA case ({{USER_LAST}} v. CFPB)

## Obsidian Folder Naming

Obsidian folders in `~/.hermes/obsidian_sync/cases/` use the pattern `{case_type}_{short_name}`.

### Case Type Prefixes
| Case Type | Prefix | Example |
|-----------|--------|---------|
| FCRA fraud | `fraud_fcra_` | `fraud_fcra_{{USER_LAST}}_v_equifax` |
| FOIA | `{{CASE_SLUG}}_` | `{{CASE_SLUG}}_{{USER_LAST}}_v_cfpb` |
| Insurance | `insurance_badfaith_` | `insurance_badfaith_{{USER_LAST}}_v_{{CASE_SLUG}}` |
| Mortgage fraud | `mortgage_fraud_` | `mortgage_fraud_{{USER_LAST}}_v_amerisave` |

### Short Name Construction
- **Full caption**: `{plaintiff}_v_{defendant}` (e.g., `{{USER_LAST}}_v_equifax`)
- **Case number**: Use docket number for CCP cases (e.g., `{{CASE_SLUG}}`)
- **Abbreviated**: Single party name when opponent is obvious (e.g., `{{OPPOSING_PARTY}}` for the Philadelphia CCP case)

### Mapping Table
| Canonical ID | Obsidian Folder | Case Type |
|--------------|----------------|------------|
| `{{CASE_ID}}` | `fraud_fcra_{{USER_LAST}}_v_equifax` | FCRA fraud |
| `{{CASE_ID}}` | `{{CASE_SLUG}}` or `{{OPPOSING_PARTY}}` | CCP case |
| `cfpb:2026-0041` | `{{CASE_SLUG}}_{{USER_LAST}}_v_cfpb` | CFPB FOIA |

## Discovery Checklist

When you have a canonical ID but cannot locate the Obsidian folder:

1. **Check case_type field**: Query the registry with `mcp_beezle_case_audit_summary` and look at the `case_type` field
2. **Search by docket number**: Look for folders containing the numeric portion (e.g., `2822`, `{{CASE_ID}}`)
3. **Try canonical ID prefix**: Search for folders starting with `fed_`, `phila_ccp_`, `cfpb_`
4. **Try full caption**: Search for folders containing `{{USER_LAST}}`, `equifax`, `transunion`
5. **Verify with meta.json**: Once located, check `meta.json` for the `canonical_id` field

## Common Pitfalls

**Pitfall 1**: Assuming canonical ID prefix matches Obsidian folder prefix
- `{{CASE_ID}}` does NOT map to `fed_{{CASE_ID}}`
- Instead: `fed:` → `fraud_fcra_` (case type determines prefix)

**Pitfall 2**: Using full caption with special characters
- Bad: `{{USER_LAST}} v. equifax, llc`
- Good: `{{USER_LAST}}_v_equifax`

**Pitfall 3**: Confusing consolidated cases
- `{{CASE_ID}}` consolidated `{{CASE_ID}}` (Avant case)
- Both share the same Obsidian folder: `fraud_fcra_{{USER_LAST}}_v_equifax`

## Audit Data Structure

Once the Obsidian folder is located, audit data resides in:
```
~/.hermes/obsidian_sync/cases/{slug}/audit/
├── data_{slug}.json        # Master audit findings
├── hierarchy.by_file.json   # Hierarchical violation structure
├── orders_{slug}.json       # Court order analysis
└── history_events_{slug}.jsonl  # Event log (post-audit)
```

**Critical**: The `data_{slug}.json` file contains the `canonical_id` field that matches the Beezle registry entry.

## Session Reference

**{{USER_LAST}} v. Equifax** ({{CASE_ID}})
- Canonical ID: `{{CASE_ID}}`
- Obsidian folder: `fraud_fcra_{{USER_LAST}}_v_equifax`
- Parties: {{USER_LAST}} (P) vs. Equifax, TransUnion, Experian, Example Card Issuer, Greensky, Avant (D)
- Audit status: 93 fatal stare decisis violations detected
- Current state: Appellate ifp pending (Third Circuit)

**CFPB FOIA** (2026-0041)
- Canonical ID: `cfpb:2026-0041`
- Obsidian folder: `{{CASE_SLUG}}_{{USER_LAST}}_v_cfpb`
- Status: Active FOIA litigation
- Current state: Constructive denial claims pending