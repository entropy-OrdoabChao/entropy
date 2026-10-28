# NIGHTLY CRON: Filing Type Discovery & Template Generator
# Place at: {{ENTROPY_HOME}}\obsidian_sync\system\cron_filing_type_discovery.md
# Schedule: Run nightly at 2:00 AM via Hermes kanban auto-queue

## PURPOSE
Scan case registry and diagnose results for filing types that don't have
YAML templates yet. When found, generate a new YAML template following
the established pattern.

## TRIGGER CONDITIONS
1. `filing_composer_v2.py diagnose <case_id>` returns defect categories
   that don't match any existing trigger_defects in filing_types/
2. A task references a filing type not in `filing_composer_v2.py list-types`
3. New case registered with novel posture (e.g., interlocutory appeal,
   habeas, mandamus to state court)

## TEMPLATE GENERATION RULES
1. Every YAML MUST have: id, title, description, court, required_authorities,
   trigger_defects, structure
2. Structure blocks MUST reference existing blocks in templates/blocks/
3. If a needed block doesn't exist, create it in the appropriate subfolder:
   - arguments/ for legal argument blocks
   - boilerplate/ for caption, signature, certificate blocks
   - sections/ for structural sections (intro, conclusion, etc.)
4. Block .md files MUST have YAML frontmatter with: id, title,
   applies_to_defects, required_facts, required_authorities, output_format
5. Use `when` clauses that reference diagnosis context variables:
   - is_federal (bool)
   - defect_categories.{CATEGORY} (int count)
   - cross_case_count (int)
   - block_count (int)

## EXISTING FILING TYPES (do not recreate)
- motion_for_garnishee_default
- motion_for_reconsideration
- motion_for_recusal
- motion_to_remand
- motion_to_strike_federal
- motion_to_strike_state
- motion_to_vacate
- notice_of_supplemental_authority
- petition_for_writ_of_mandamus
- ifp_objection
- appellate_reply_brief
- motion_to_certify_54b
- praecipe_to_enter_default
- motion_to_compel_discovery
- opposition_to_motion_to_dismiss
- motion_for_default_judgment
- motion_for_summary_judgment

## EXISTING BLOCKS (do not recreate)
### arguments/
candor_violation, equity_maxims_argument, failure_to_state_claim,
haines_v_kerner_standard, jurisdiction_attack, lack_of_standing,
non_binding_authority, procedural_defect, recusal_bias,
sanctions_request, void_order_misuse, ifp_financial_inability,
ifp_not_frivolous

### boilerplate/
caption_appellate_3rd_cir, caption_federal, caption_state_pa_ccp,
certificate_of_service, proposed_order, signature_block_pro_se,
verification

### sections/
conclusion, introduction, prayer_for_relief, procedural_history,
standard_of_review, statement_of_facts, summary_of_argument,
table_of_contents, table_of_authorities

## VALIDATION
After creating a new template, run:
```
python {{ENTROPY_HOME}}\filing_composer_v2.py list-types
```
Confirm the new type appears. Then run:
```
python {{ENTROPY_HOME}}\filing_composer_v2.py compose "<test_case_id>" <new_type_id>
```
Confirm it composes without errors.

## OUTPUT
Save new YAML to: {{ENTROPY_HOME}}\obsidian_sync\templates\filing_types\<id>.yaml
Save new blocks to: {{ENTROPY_HOME}}\obsidian_sync\templates\blocks\<category>\<id>.md
Log discovery to: {{ENTROPY_HOME}}\obsidian_sync\system\filing_type_discovery_log.md
