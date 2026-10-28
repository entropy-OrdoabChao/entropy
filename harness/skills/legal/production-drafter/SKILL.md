---
name: production-drafter
description: Run the Beezle batch drafter for an existing audited case to produce a motion + scorecard exhibit. Use this when the user names a canonical case ID (fed:2:25-cv-NNNNN format) and asks for a specific filing type. This skill writes a manifest JSON to ~/.hermes/bin/ and invokes draft_batch.py — it does NOT call the MCP compose_filing tool, and does NOT trigger the legal-drafter classification flow. For brand-new cases without audit JSON, use beezle-pipeline-runner instead.
category: legal
triggers:
  - "fed:2:25-cv-"
  - "draft_batch"
  - "production drafter"
  - "batch draft"
  - "draft batch"
  - "run the batch drafter"
  - "motion to reinstate"
  - "motion to certify 54b"
  - "petition for mandamus ca3"
  - "notice of ca3 mandamus"
  - "draft for fed:"
  - "draft for case fed:"
  - "drafts for case fed:"
  - "draft motion for fed:"
  - "scorecard exhibit for fed:"
  - "draft a motion for reconsider for fed:"
  - "draft a motion for reconsideration for fed:"
  - "draft a motion to vacate for fed:"
  - "draft a motion to strike for fed:"
---

# Skill: Production Drafter (Beezle deterministic pipeline)

## When to use this skill

Trigger when the user asks for any of:
- A motion or filing for a case in `{{ENTROPY_HOME}}/cases_index/case_registry.json`
- "Draft a motion to <X>"
- "Generate the <reconsider/strike/vacate/reinstate/mandamus> for <case>"
- "Run a batch of drafts" / "draft for cases A, B, C"
- Scorecard exhibits for an existing case

**DO NOT freeform-draft these motions yourself.** Use this pipeline.

## When NOT to use this skill

- Brand new case with no audit JSON yet (run `pipeline_intake` first)
- Document types not in the valid filing_types list below (escalate to user)
- Pure research / case analysis / questions about a draft (answer normally)
- Case is in appellate stage — motion for reconsideration is procedurally improper

## POSTURE CHECK (always run first)

Before calling `production-drafter`:
1. `mcp_beezle_case_audit_summary(canonical_id)` — get the case status.
2. If status contains "appellate" or "briefing", the case is in appellate
   stage. A motion for reconsideration is procedurally improper — the trial
   court phase is over. Recommend appellate brief, notice of appeal, or
   reply brief instead.
3. If status contains "trial", "pending", "merits", or similar — motion
   for reconsideration is appropriate (within 10 days of the order).
4. If the case has no audit JSON yet (NO_AUDIT), run `pipeline_intake` first.

If you skip this step and draft a reconsider motion for an appellate case,
the filing will be procedurally void. The user will have to refile.

## How it works

The production drafter is `beezle.generation.drafter.draft_for_case` —
a deterministic Python function that loads audit data, populates claims via
the audit→claims bridge (load step), and renders a filing via the appropriate
builder (procedural / recusal / reply / brief). Outputs .md and .docx files.

You don't call `draft_for_case` directly. You write a manifest and shell
out to the batch runner.

## Invocation

1. Write a manifest JSON to `~/.hermes/bin/manifests/<label>.json`
2. Run: `python3 ~/.hermes/bin/draft_batch.py <manifest_path>`
3. Read the heartbeat file (sibling of manifest, named `<label>.heartbeat.md`)
4. Report status to user with file paths

## Manifest schema

```json{
"label": "draft_run_2026-XX-XX",
"defaults": {
"base_path": "{{ENTROPY_HOME}}",
"vault_path": "{{ENTROPY_HOME}}/obsidian_sync",
"mode": "audit"
},
"jobs": [
{
"canonical_id": "{{CASE_ID}}",
"slug": "{{CASE_SLUG}}",
"filing_type": "motion_to_reconsider",
"with_scorecard": true
}
]

## Status codes from the runner

- **OK** — motion > 5KB, ready for user review
- **THIN** — motion < 5KB, likely empty Section II (known bug, see lessons_learned 2026-05-25)
- **EMPTY** — motion produced but zero useful content
- **NO_AUDIT** — case has no audit JSON; run `pipeline_intake` first
- **FAIL** — exception during draft; check heartbeat for traceback

## How to pick filing_type for a user request

1. Read the case's CASE_FACTS.md and CASE_TIMELINE.md
2. Run `mcp_beezle_case_audit_summary(canonical_id)` to confirm procedural posture
3. Identify: what court order is being challenged? what relief does plaintiff want?
4. Map intent to filing_type:
   - Overturn recent ruling within 10 days → `motion_to_reconsider`
   - Vacate older judgment as void → `motion_to_vacate`
   - Strike opposing filing → `motion_to_strike`
   - Certify partial judgment for appeal → `motion_to_certify_54b`
   - Reinstate dismissed case → `motion_to_reinstate`
   - Mandamus to Third Circuit → `petition_for_mandamus_ca3`
   - Appellate stage → recommend appellate brief or reply brief, not reconsideration
5. When in doubt, ASK the user before drafting. Don't guess on procedural posture.

## FILING INFRASTRUCTURE DIAGNOSTIC

If `list_filing_types` returns empty, the Beezle filing template engine is
not populated. This means:
- `compose_filing` will fail or produce empty output
- `list_filing_blocks` will return empty
- The draft_batch.py runner may also fail

Diagnostic steps:
1. Check if the Obsidian vault has templates at `obsidian_sync/templates/filing_types/`
2. If the templates folder is empty, the filing system needs to be reseeded
3. Report this to the user before attempting any compose_filing call

## Output locations

By default, drafts land at:
`{{ENTROPY_HOME}}/obsidian_sync/cases/<slug>/drafts/<filing_type>_<safe_canonical_id>_<timestamp>.md`

Plus matching `.docx`. Plus optional `scorecard_exhibit_<safe_id>_<timestamp>.docx` when `with_scorecard: true`.

Heartbeat log: `<manifest_path_parent>/<label>.heartbeat.md`

## What to tell the user after running

Report:
- Status code per motion (OK/THIN/EMPTY/NO_AUDIT/FAIL)
- File paths to .md and .docx in the vault
- Any warnings from the run (citation mismatches, missing facts to verify)
- If any motion is THIN or NO_AUDIT, explain that's a known wiring gap, not a Hermes failure

## Known gaps (as of 2026-05-25)

1. `motion_to_strike` and `motion_to_vacate` produce THIN output because `_gather_scorecard_facts` in `beezle/generation/classifier.py` returns empty list even when audit scorecard has populated violations. Audit→claims bridge fixed the intro count but not Section II body.
2. HUD FOIA case has no audit JSON; `pipeline_intake` must run before drafting.
3. Case registry has some D.N.J. cases labeled "NJ_SUP" (NJ Superior) when they should be "D.N.J." (federal district). Affects motion text; one-time registry fix.
4. `compose_recommend` returns empty recommendations even with 67+ violations when the filing type YAML templates are not loaded — this is an infrastructure issue, not a defect issue.

## Related files

- Runner: `~/.hermes/bin/draft_batch.py`
- Example manifest: `~/.hermes/bin/batch_2026-05-25.manifest.json`
- Bridge module: `{{ENTROPY_HOME}}/beezle/generation/audit_to_claims_bridge.py`
- Lessons: `{{ENTROPY_HOME}}/obsidian_sync/00_system/lessons_learned.md` entry of 2026-05-25


## CANONICAL DRAFTING FLOW (authoritative — matches Pipeline A)

This is the single correct tool-calling flow for producing a motion/filing.
Both the agent path and `pipeline_a_mcp.py` use it identically.

1. **m0 — pick the filing type.** Call `recommend_filing_types(case_id, top_n=1)`
   (pipeline/filing_composer). This reads case posture + defect signature and
   returns the ranked filing_type id. Use the top id. Do NOT hand-guess the
   filing type; let m0 decide unless the user named one explicitly.

2. **Draft via the v2 LLM orchestrator.**
   `beezle.generation.drafter.draft_for_case(canonical_id, base_path, output_dir,
   with_scorecard=True, mode="pipeline", filing_type=<m0 id>)`.
   - `mode="pipeline"` builds from pipeline context (works even when the case
     `status` field is empty).
   - This routes to `build_procedural_motion`, which uses the LLM at the
     fact-application / definition-comprehension spots
     (`_llm_compose_fact_application`). Legal **definitions remain LOCKED and
     verbatim** from the curated definition engine — the LLM never invents law,
     it composes the application of locked law to the audited facts.
   - Emits .md + .docx + scorecard exhibit.

3. **Citation gate AFTER drafting.** Run the existence/binding check on the
   produced cites. Code owns the veto; the LLM only composes prose. A confirmed
   fabricated cite blocks the filing; a local-miss warns (corpus may be incomplete).

### Engine status (important)
- `beezle.generation.drafter.draft_for_case` (v2 orchestrator) — **CANONICAL.**
- `generation/reply_brief_drafter.py` (v1) — fallback only; **being deprecated**
  as the procedural engine subsumes it.
- `compose_filing` / `case_full_filing_package` (filing_composer block-stitcher)
  — **LEGACY, deterministic, no LLM.** Use only with `--engine legacy`. Not the
  default path anymore.
