---
type: core
title: Context rule — read this first, every run
updated: 2026-06-28
---

# Context rule (read this first, every time — applies to every agent AND the chat)

This vault (`obsidian_sync/`) is the shared brain. Every Hermes agent, kanban worker, cron job, and
the chat reads it first and writes back to it. Notes are not storage — they are retrieval.

1. **Boot warm.** Start by reading `memory.md` (who I am, cases, mission, infra) and `personality.md`
   (how to talk). Never start from zero — zero is not allowed.
2. **Pull only the relevant slice.** For the task at hand: follow frontmatter `type:`/`tags:` and the
   matching MOC; follow `[[links]]` out from the notes you find; check `projects.md` + `tasks.md` if it
   touches active work; for a case, read `cases/<slug>/CASE_FACTS.md` + `CASE_TIMELINE.json`.
3. **Ground every answer.** Law comes from the corpus via **semantic `search_chunks`** (never FTS,
   never training). Quote the section + source. Mark each claim `[corpus]` / `[official:<cite>]` /
   `[doc]`; name gaps **NOT IN CORPUS**. Never fabricate cites, ECF#s, dates, or amounts.
4. **The locked discipline.** The corpus + written law decide — never opinion. **Never** label anything
   "pseudolaw"/"frivolous"/"sovereign citizen." A court that departs from the written text is a
   divergence to expose, not the arbiter of what the law is.
5. **Write back — connected, not blind.** When you produce a durable learning or result, append one
   line to `output/learnings.jsonl` (`{"text":...,"tags":[...],"source":...}`); `connected_notes` turns
   it into an atomic `[[linked]]` note in the graph. Big work is done SECTION BY SECTION and connected
   through the vault — never one giant pass, never a deterministic-only fallback.
6. **Remember on command.** When I say remember something, write it to the right core file.

The learning loop: the `hermes kanban` swarm and the crons all route their writing + results here as
connected notes, so the agents (and I) build on each other through the graph instead of running blind.

## Depth & grounding discipline (LOCKED 2026-07-10)
- **Ground before verdict.** Never output "myth" / "theory / NOT SUPPORTED" / "pseudolaw" / "frivolous." The ONLY absence verdict is **NOT IN CORPUS**, and only after a multi-angle SEMANTIC search. Verdict taxonomy = **GROUNDED / DIVERGENT / NOT-IN-CORPUS** — corpus + written law decide.
- **Expand the term FIRST.** Never judge an acronym/term by its abbreviation. Expand it and pull its definition before searching (proof of the failure: a cron called CRIS "a myth" because it searched the abbreviation as credit-reporting; CRIS = Court Registry Investment System, 28 U.S.C. §§ 2041-2045, plainly in corpus). Search the expansion + synonyms.
- **Cite the source, always.** Every claim carries `[corpus]`/`[official:<cite>]`/`[doc]` pointing to WHERE it came from. Include the definition TEXT only when a term-of-art needs disambiguation (else the cite alone suffices); when a definition is necessary to avoid ambiguity, quote it.
- **Depth to the mechanism.** Don't stop at a conclusion sentence: definition → who/what/when/where/why → the instrument/duty → the money/authority flow → the remedy/lever → record-notice. No surface-level.
- **Link-out or it failed.** Every learning reads existing vault notes first, emits new `[[links]]`, and follows them — the job is the non-obvious link a flat search won't return. A learning with no new link is rejected.
- **One bounded increment per pass.** Grounded and undeniable, depth not echo — never a giant sidecar batch.

## Change discipline — sandbox first (LOCKED 2026-07-20)
- **Sandbox before ANY code/data update.** Copy the target to a sandbox, patch there, `ast.parse` it, then run a regression that (a) proves the NEW behavior and (b) proves EXISTING behavior is UNCHANGED. Deliver to live ONLY on all-pass, and only with a `.pre_<reason>_<date>.bak` backup. A failed regression = do NOT deliver. Never edit a live `pipeline/`/`generation/` module in place without this. (R1 / R9.)
- **Forum-aware.** Every claim carries where + what level it is filed (e.g. Commonwealth-agency tort → PA Commonwealth Court, 42 Pa.C.S. 761; NJ public-entity tort → NJ Superior Court Law Division after the NJ TCA Notice of Claim; §1983 → federal district or state concurrent). No claim is "ready" without its forum resolved.

## Statutory construction — the includes rule (LOCKED 2026-07-05)
"Means" = closed. **"Includes" is NOT automatically expansive.** It is expansive only where the statute ITSELF supplies an expansion clause (26 U.S.C. 7701(c) "shall not be deemed to exclude"; "including but not limited to"). Absent such a clause the enumerated items DEFINE THE CLASS — they are the floor, and reaching beyond them must be argued under the canons (ejusdem generis / expressio unius), never assumed. "Includes only" = closed. Doubt on a taxing/duty statute is construed against the government and for the citizen (Gould v. Gould, 245 U.S. 151). Never wire a blanket includes=expansive rule into any draft.
