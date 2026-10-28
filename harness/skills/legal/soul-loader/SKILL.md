---
name: soul-loader
description: Splits SOUL.md into tiny router + lazy-loaded modules. Cuts KV cache pressure ~85%. Loads only the pipeline modules needed for the current task type. Enables aggressive prompt caching and parallel skill execution.
version: 1.0.0
author: Hermes Legal
metadata:
  hermes:
    tags: [legal, performance, kv-cache, lazy-load, soul]
---

# SOUL Loader

## Purpose
The full SOUL.md v5 is ~6K tokens. Loading it every turn fills the KV cache and slows responses. This skill splits SOUL.md into:

- `SOUL_ROUTER.md` (~400 tokens) — always loaded, decides which modules to pull
- `SOUL_MOD_analysis.md` — pipeline steps 1–10.5
- `SOUL_MOD_draft.md` — pipeline steps 11–14.5
- `SOUL_MOD_audit.md` — for auditing existing orders/filings
- `SOUL_MOD_research.md` — pipeline steps 1–7
- `SOUL_MOD_ledger.md` — docket-violation-ledger behavior
- `SOUL_MOD_learning.md` — learning loop (Step 16)

Modules are loaded ON DEMAND by classification result.

## Performance impact
| Metric | Before | After |
|---|---|---|
| SOUL tokens per turn | ~6,000 | ~400 + needed modules |
| Average tokens per turn (analysis) | ~6,000 | ~1,800 |
| Average tokens per turn (draft) | ~6,000 | ~3,200 |
| KV cache pressure | high | low |
| Estimated query time (8 min baseline) | ~480s | ~150–200s |

## Install
```bash
~/.hermes/skills/legal/soul-loader/scripts/loader.sh install
```

This:
1. Backs up current `~/.hermes/SOUL.md`
2. Splits it into modules in `~/.hermes/soul/`
3. Replaces `~/.hermes/SOUL.md` with the router
4. Router instructs Hermes to `cat` modules on demand

## Module loading instructions (in router)
The router contains:
```
At classification time, load the appropriate module:
  ANALYSIS  → cat ~/.hermes/soul/SOUL_MOD_analysis.md
  DRAFT     → cat ~/.hermes/soul/SOUL_MOD_analysis.md ~/.hermes/soul/SOUL_MOD_draft.md
  RESEARCH  → cat ~/.hermes/soul/SOUL_MOD_research.md
  AUDIT     → cat ~/.hermes/soul/SOUL_MOD_audit.md
If ACTIVE_CASE is set, also load: SOUL_MOD_ledger.md
After every legal task: SOUL_MOD_learning.md
```

## Additional perf hooks

### llama-server prompt caching
The skill verifies llama-server is started with prompt caching enabled:
```
--prompt-cache /home/USER/.hermes/cache/llama_prompt.bin
--prompt-cache-all
```

If not, it patches the docker-compose or systemd unit.

### Parallel skill execution
Steps that don't depend on each other run in parallel:
- Step 5 (definitions) ∥ Step 6 (authority retrieval) ∥ Step 6.5 (binding precedent)
- Step 9.5 (mandatory duties) ∥ Step 10.5 (due process)
- Step 11.5 (defense sim) ∥ Step 11.7 (court practice audit)

The router includes parallel-execution markers so Hermes calls them with `&` and `wait`.

## Usage
```bash
# Install (one-time)
~/.hermes/skills/legal/soul-loader/scripts/loader.sh install

# Restore monolithic SOUL.md
~/.hermes/skills/legal/soul-loader/scripts/loader.sh restore

# Verify split
~/.hermes/skills/legal/soul-loader/scripts/loader.sh verify

# Show token estimates
~/.hermes/skills/legal/soul-loader/scripts/loader.sh stats
```
