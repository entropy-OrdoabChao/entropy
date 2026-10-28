---
name: case-analyzer
description: Audit ANY case for legal/financial defects and the remedy path — what the law REQUIRES vs what is MISSING, the avenues, the tax/1099 + double-recovery analysis, and each actor's duty + escalation. Use when asked to analyze/audit a case, find violations/gaps, or when new case intake lands. Product-agnostic (mortgage, credit card, deposit, auto).
---

# case-analyzer

A deterministic engine does the heavy reasoning in small, grounded pieces and writes linked Obsidian notes. You pick the case and report the results — you do NOT re-derive the law yourself.

## When to use
- "analyze / audit case <slug>", "find the violations / the gaps", "what does the law require here", or new intake arrives in a case folder.

## How to run
From the repo root with the project python:

```
cd {{ENTROPY_HOME}}
PYTHONPATH={{ENTROPY_HOME}} python3 -u -m pipeline.case_engine <slug> [phase]
```

- `<slug>` = the case folder under `obsidian_sync/cases/` (e.g. `{{CASE_SLUG}}`). List them with `ls obsidian_sync/cases/`.
- `phase` (optional): `requirements` | `accounting` | `avenues` | `tax` | `accountability` | `all` (default `all`). (`accounting` = the A/R, A/P, who it's payable to, and whether the books balance / double recovery.)
- It runs long (each step is its own fresh grounded run on the local-gpu/262k model) and writes results **incrementally** to `obsidian_sync/_hermes_learnings/notes/case-<phase>-<slug>.md` as each lands — tail those to report progress. Start with `requirements` for the fastest signal.

### Two backends (same engine, same grounded retrieval)
- **Hermes agent (default)** — deep, can call the corpus MCP, local-gpu/262k. Best for a thorough audit.
- **m0 (the pipeline's local LlamaLLM)** — lighter, inline, no agent loop, faster + cheaper. Use for cron sweeps, quick passes, or inside the deterministic drafting pipeline. Select it with:
```
CASE_ENGINE_BACKEND=m0 PYTHONPATH={{ENTROPY_HOME}} python3 -u -m pipeline.case_engine <slug> [phase]
```
The law is retrieved the same way either way, so both stay grounded; m0 just reasons lighter. The drafter can also import `pipeline.case_engine` / `pipeline.case_comprehension._reason` to pull the requirements/gaps inline while drafting.

## What it produces (all grounded in the corpus + cross-linked)
- **requirements** — what the law REQUIRES for each product present, then a GAP-CHECK: which required filings/structures are documented vs MISSING. A missing required step is the exploited gap.
- **avenues** — defects the engine surfaces itself, each graded TWO-AXIS (legal merit vs evidence status).
- **tax** — the 1099 series (A/C/OID/B) + government-guarantee double-recovery, via IRC / IRM / TFM / 24 CFR.
- **accountability** — per actor (IRS, HUD, CFPB, bank, AG/DOJ, judge, police, FinCEN): duty + authority + how to invoke + who to escalate to if they don't act.

## Rules (non-negotiable)
- Report what the corpus actually says; cite it. NEVER label a position (e.g. "frivolous") from outside the corpus — if the law characterizes it, quote that text; otherwise analyze on the law's own terms.
- A per-se violation (the law REQUIRES X and X did not happen) is STRONG on legal merit even if the proving document is not yet in hand — that is the EVIDENCE axis, separate.
- Reason from primary documents, not the person's own drafts (the engine filters this).
