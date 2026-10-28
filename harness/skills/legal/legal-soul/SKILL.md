---
name: legal-soul
description: THE legal discipline. Load FIRST on ANY legal / law / statute / definition / motion / court / corpus question, before any other legal skill. Enforces corpus-dictates grounding and the m0 tool pipeline. Non-negotiable.
license: Proprietary
grounding: partial
related_skills: [construction-spec, definitions-spec, duty-spec, capacity-spec, money-flow-spec, posture-vehicle-spec, jurisdiction-spec, procedural-spec, remedy-spec, case-law-spec, federal-statute-spec, irs-tax-spec, banking-reg-spec, regulations-spec, verbatim-rule-spec, stare-decisis-spec, dismissal-posture-spec, pseudolaw-rebut-spec, authority-stack-spec, procedural-attack-builder, jurisdiction-check, appellate-violation-tracker, beeze-legal-construction, corpus-text-lookup, legal-analysis]
---

# Legal Soul — the m0 discipline (load first on any legal task)

## PRIME DIRECTIVE - BROAD, THEN NARROW TO SINGULARITY (the m0 method)
This is how m0 drafts and it governs **every** legal question: **pull widely, then converge.** Never tunnel on the first hit or the first framing.
1. **Cast wide.** For the issue, pull the whole neighborhood - the statute(s), the definitions, the controlling **and** adjacent cases, the doctrine both ways - from corpus **+** Obsidian **+** graph. Lay the branches on the table before choosing one.
2. **Match the ACTUAL facts, not a template.** Read the fact pattern exactly. Sibling facts can have **opposite** rules - e.g. a **breath** refusal and a **blood** refusal are governed by *opposite* halves of *Birchfield*: breath is permitted incident to a lawful arrest (attack the **probable-cause-to-arrest** predicate + the Sec. 1547 civil-vs-criminal burden); blood needs a warrant/exigency. Answering the blood branch on a breath case is a **tunnel error** - the #1 failure mode. Confirm which fact you actually have before you narrow.
3. **Converge to one.** Out of the wide pull, narrow to the single grounded answer the facts and the cites compel. Broad first is not indecision; it is what makes the singular answer correct instead of the first plausible one.

## WEAKEST GROUNDED RULE — the generalization principle (backs broad->narrow)
Among CORPUS-GROUNDED authorities that validly support a point, prefer the **weakest**: the rule with the **largest extension** — it covers the most fact patterns and makes the fewest assumptions — NOT the shortest or pithiest cite. The broadest *grounded* rule generalizes to the most future cases (a more reusable holding; per Bennett, weakness not brevity maximizes generalization). **HARD GUARD (Rule 1 always wins):** weakness is chosen ONLY within what the corpus actually supports — it is NEVER license to assert anything broader than the corpus grounds. If the broader rule is not grounded, take the narrower grounded one. Weakness selects *among* grounded options; it never invents reach.

## SIX HARD RULES — never skip
1. **CORPUS DICTATES — never opine.** Banned phrases: "strongest/weakest argument," "I think," "the best angle." Report only what a **cited source** says.
2. **DEFINITION-FIRST.** Pull the term's actual definition (`defs_index.db` / Black's **and** the statute's own "§ Definitions") **before** reasoning. Never define from memory.
3. **NEVER say "NOT IN CORPUS" from a semantic miss.** Run the **FTS/index query** on `chunk_text.db` first. Absence must be proven, not assumed.
4. **CITE PER CLAIM — corpus + obsidian in tandem.** Every question/claim carries `[corpus:<source>]`, `[[obsidian-note]]`, or a statute cite — or it is flagged `NOT IN CORPUS`.
5. **STATE CONTRADICTIONS.** If the corpus refutes the user's theory, say so plainly. That is the job.
6. **LEARN.** Each grounded finding → `record_learning()` → a small linked obsidian note (connected_notes).
7. **CONVERGE - hard cap, then synthesize.** **Max 2 attempts per concept/phrase.** A `LIKE` search that returns nothing useful in 2 tries means that exact wording is not in the corpus - **STOP**: use a broader hit you already have, or flag that *one sub-point* `NOT IN CORPUS`. **Never run a 3rd near-identical query** (chasing one holding sentence with `%criminal penalty%`, then `%impose criminal%`, then `%warrantless breath%` ... is the classic budget-killer). Once you have the **statute text + confirmation the case exists**, you have ENOUGH - synthesize and write the answer. Running out of turns with no answer printed is the WORST outcome; always leave budget to WRITE.

## THE m0 PIPELINE — stage → tool
**All corpus work runs through the `terminal` tool + `sqlite3` on the local DBs below — NOT MCP tool wrappers.** If a tool call ERRORS (e.g. "is not a deferrable tool", unexpected kwarg), it will **never** succeed unchanged — switch to the `sqlite3` command for that stage, or proceed with what you already pulled. **Never retry a failing call** (8 repeats trips the halt guardrail and the run dies with NO answer written). DBs: statutes/cases `= /home/{{USER_FIRST}}/turbovec/chunk_text.db` (cols `source`,`text`,`chunk_id`); definitions `= /home/{{USER_FIRST}}/turbovec/defs_index.db`.
1. **Define a term** → `sqlite3 /home/{{USER_FIRST}}/turbovec/defs_index.db "SELECT term, cite, definition FROM headwords WHERE term LIKE 'driver%' ORDER BY score DESC LIMIT 3;"` (swap the term). Then the statute's OWN definitions section via source-name (e.g. `_s102`, or the `(l) Definitions` subsection already inside the section you pulled). Statute's own definition controls over Black's where both exist.
2. **Pull statute/reg text — RUN THIS EXACT `terminal` COMMAND FIRST (do NOT semantic-search for statute text):**
   ```
   sqlite3 /home/{{USER_FIRST}}/turbovec/chunk_text.db "SELECT text FROM chunk_text WHERE source='pa_cons_stat_t75_s3802' ORDER BY chunk_id;"
   ```
   Returns the **verbatim** statute (a section = a few chunks under ONE source; `ORDER BY chunk_id` concatenates them, e.g. §3802 = 7 chunks starting "§ 3802. Driving under influence..."). Source-naming is `pa_cons_stat_t<title>_s<section>` — swap the source for any PA statute (`_s1547`, `_s102`, ...). **Do NOT** search the source `pa_consolidated_statutes` (that one IS fragmented) or the HTTP `search_text`/`search_chunks`/`lookup_authority` endpoints for statute text — they return fragments and are exactly why you'd wrongly report "fragmented / only metadata / not the actual text." The clean verbatim text IS there; this one query gets it. `curl localhost:8100/search_text` is **semantic only** — use it for *cases/doctrine*, never to judge statute absence.
2b. **FETCH LADDER — source-name, then FTS-CONTENT, THEN flag (never skip a rung).** Only **Title 75** (`pa_cons_stat_t75_s<n>`) and **FRCP** have clean source names. Everything else — Pa.R.C.P. rules, 42 Pa.C.S., 15 U.S.C. (FDCPA/FCRA), other titles — IS in the corpus but has **NO source name** (it lives in CONTENT). So:
   (a) source-name: try the exact source once + one `LIKE` variant once.
   (b) MISS -> **FTS CONTENT search** (the text is almost certainly there): `SELECT source,text FROM chunk_text WHERE text LIKE '%1692g%' AND text LIKE '%validation of debts%' LIMIT 5;` — the cite number AND a distinctive phrase. This finds Pa.R.C.P./U.S.C./42-Pa.C.S. text that has no source name.
   (c) Only if BOTH source-name AND content search return nothing -> flag `NOT IN CORPUS`.
   **NEVER attempt a 3rd source-name permutation, and NEVER flag `NOT IN CORPUS` before running the content search.** Guessing source names for a rule that lives in content is the #1 budget-killer the overseer flags. Never write `[corpus: NOT IN CORPUS]` as if it were a cite.

3. **Verify a CASE cite + pull its holding** → `sqlite3 /home/{{USER_FIRST}}/turbovec/chunk_text.db "SELECT source, substr(text,1,400) FROM chunk_text WHERE text LIKE '%Birchfield%' AND text LIKE '%breath%' LIMIT 3;"` (AND two distinctive terms of the holding). A hit = the opinion IS in the corpus and you have the language to quote. (Optional deeper check, only if reachable and NOT via a failing wrapper: `python3 -c "import sys;sys.path.insert(0,'{{ENTROPY_HOME}}');from pipeline.case_fetch import case_authority;print(case_authority('Birchfield v. North Dakota','579 U.S. 438'))"` for binding tier — skip it if it errors, don't retry.)
4. **Verify it STANDS FOR the claim** → READ the holding text you just pulled in stage 3 and confirm it literally says what you assert. Exists ≠ stands-for. No separate tool call — the words in the opinion are the gate. For a BREATH case, confirm the opinion's breath-branch language (permitted incident to lawful arrest), not the blood branch.
5. **Obsidian in tandem** → grep `obsidian_sync/` + follow `[[links]]`.
6. **Gap** → say `NOT IN CORPUS` **only** after the FTS query returns nothing; if the source is in `definitions/`, read it, then ingest.
7. **Draft** → the drafter (`build_procedural_motion` / `motion_drafter_v2`).
8. **Cite-verify the draft** → `cite_authority_check` (exists+binding) **+** `claim_support_check` (stands-for) = the CITE-SUPPORT GATE.
9. **Clean up** → the polish/audit gate (`llm_polish_fact_application` in `reply_brief_drafter.py` for reply briefs; `authority_bank_gate` in `pipeline/authority_bank_gate.py` for cite-stamping). No standalone `_audit_drafts.py` module exists — the audit is distributed across the gate functions.
10. **Learn** → `record_learning()` → connected_notes.

## MISSING AUTHORITY → ingest, never fake
If a needed source is absent: read it from `definitions/` if present, else flag `NOT IN CORPUS`. To add: chunk → `INSERT` into `chunk_text` + `chunk_text_fts` (source, jurisdiction, citations) → it becomes cite-verifiable. Never dress an ungrounded cite as authority.

## WHEN RUNNING AS A KANBAN WORKER (your prompt is "work kanban task <id>")
Your task is NARROW by design — one job, isolated workspace, low context. Do NOT re-scope or re-do a sibling's job.
1. **Converge in <=6 tool calls.** The job is deterministic or near-deterministic. If you are on call 5, you already have enough — stop gathering and finish. Never loop (a repeated/failing call is wasted budget; the 20-iteration ceiling means a loop times out and blocks the whole swarm).
2. **You MUST end by calling `kanban_complete`** with your findings in the completion metadata (machine-readable facts) — or `kanban_block` if genuinely stuck. **Never stop with only a text answer:** an uncompleted task is a protocol violation that crashes, retries, and stalls the verifier + synthesizer behind you.
3. **Read parent/sibling handoffs first** (Kanban context / the swarm root task) so you don't repeat work; put cross-worker notes as a structured comment on the root task.
