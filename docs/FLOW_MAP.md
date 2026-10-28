# Entropy — main flow and missing links (audit 2026-09-27)

Built from three sources: the HTML link graph of `ui/index.html` (every `data-view` / `data-go` / `data-tab` / `data-open` /
`data-api`), the package's module graph (graphify `graph.json`, entropy nodes only), and `vault_contract.yaml` matched against
the code that writes each file. Read with `DESK_API.md`.

## 1. The main flow as built

```
 entropy.sh / entropy.ps1
   └─ pipeline.bootstrap        creates HOME: registry, case_map, .env, vault seed (memory.md, AGENTS.md), drop/
   └─ pipeline.intake_flow      drop/ → intake_router.route → ocr_intake.process_case → timeline_build.build → case_state.main
                                writes: intake/, CASE_TIMELINE.json, CASE_STATE.md, logs/intake_flow_last.json
   (model-driven, later)        case_facts_refresh → CASE_FACTS.md · motion_drafter_v2 / m1 → drafts/ · filing_composer, filing_status
 ui/index.html (the desk)       reads the case folder per vault_contract.yaml; today it is a mockup with example cases
```

Desk navigation (all reachable): rail → Today · Get started · Cases · Inbox · Research · Settings (+ recent cases);
links → Add documents (load) · Send · Start a case · Setup · Case. Case tabs (all have a tab-bar button): Overview · Story ·
Documents · Defects · Drafts · Deadlines · Mail · Escalate. Today, Inbox, Send, Start and Load deep-link into case tabs.

## 2. Missing links

| # | Where | What is missing | Severity |
|---|---|---|---|
| 1 | `main.py` → `core/server.py` | `main.py` starts `core/server.py` and imports `core.server.create_app`, but **`core/server.py` is not in the package**. `core/server_endpoints_workspace.py` (4 routes) exists and nothing mounts it. The desk has no server; `python main.py` fails at the UI step. | **closed 2026-09-27** — `core/server.py` added: serves the desk, mounts workspace routes, `/api/health`, `/api/deadlines`, `/api/rule_check/{slug}`; every other planned endpoint answers 501 by name |
| 2 | UI → API | 28 endpoints named by `data-api` in the desk; **4 served** (`/api/cases`, `/api/cases/{id}`, `/api/scorecards`, `/api/file/{name}`), 25 not (list in `DESK_API.md`). | expected (README: desk API not included) |
| 3 | vault contract → producers | 9 of 31 contract files have no writer in the package. Exist in live Beezle, not copied: `MATTER_BRIEF.md` (`pipeline/m0_matter_brief.py`), `compliance_events.md`, `defect_inventory.md`, `enforcement_inventory.md`, `preservation_ledger.md` (all `pipeline/hermes_writeback.py`). Exist nowhere yet: `_TRICK_INVENTORY.md`, `filing_readiness_checklist.md`, `audit_reports/`, **`rule_check.json`** (needs the movant rule-requirements detector), **`deadlines.json`** (deadline_tracker stores one `dl_*.json` per deadline under `lifecycle/deadlines/`; a per-case export is needed or the contract should point at that store). | **partly closed 2026-09-27** — `hermes_writeback.py` + `m0_matter_brief.py` copied and scrubbed (paths via `entropy_paths`); `pipeline/rule_check.py` writes `rule_check.json` (verified on a real PO: exhibits missing); `deadline_tracker.export_case_deadlines()` writes `deadlines.json`. Still no producer: `_TRICK_INVENTORY.md`, `filing_readiness_checklist.md`, `audit_reports/` |
| 4 | Defects → Research | A defect names a rule; nothing links from the defect card or the rule check to the Research view to read that rule in the library. Research is reachable only from the rail. | closed — "Read the rule" on every defect and rule-check header → Research |
| 5 | Send → Drafts | Send links to Escalate, Mail, Cases; no way back to the draft being sent. | closed — "Back to the draft" on Send |
| 6 | Setup → Today | Setup's only exit is Add documents; finishing setup should land on Today. | open — Setup has no primary finish button yet (steps end in "Your first case"); add `data-go="today"` when that step's finish control is designed |
| 7 | Deadlines (theirs) → Send | A missed opposing deadline links to the consequence (Defects/Escalate) but not to the filing that claims it. | closed — "Prepare the filing" on a missed opposing deadline |
| 8 | graphify | `ui/index.html` has no node in the graph, and a query about the entropy flow returns llama.cpp vendor nodes first (50% of the graph). The graph cannot answer questions about the desk until `.graphifyignore` excludes vendored and dead trees (v3 plan P6). | tooling |

## 3. What is wired correctly

- Every view and pane is reachable; no `data-go` / `data-tab` / `data-open` / `data-see` target is missing.
- Every button either navigates, or names its endpoint (`data-api`), or is a mock the desk announces; 0 dead buttons.
- `intake_flow` is a real chain (route → OCR → timeline → state), not a stub; `CASE_TIMELINE.json` and `CASE_STATE.md` — the
  contract's required files — are produced by it; `CASE_FACTS.md` by `case_facts_refresh` once a model is configured.
- `core/entropy_paths.py` is the single path hub (131 in-edges); `document_scanner.py` is the read engine (13 in-edges).

## 4. Order to close the gaps (proposal) — steps 1, 2, 4, 5 done 2026-09-27; step 3 done as `pipeline/rule_check.py` (checker; the contract still comes from `rule_requirements`)

1. A minimal `core/server.py`: mount `server_endpoints_workspace`, serve `ui/index.html`, add the two read endpoints the desk
   needs first (`/api/deadlines?owed_by=`, `/api/file/{name}` already exists). Fixes #1 and turns the mockup into a viewer.
2. Copy `hermes_writeback.py` and `m0_matter_brief.py` from Beezle (person-agnostic pass) — closes 5 of the 9 in #3.
3. `rule_check.json`: the movant rule-requirements detector (same `rule_requirements` module, run on the opposing filing).
4. `deadlines.json`: export from `deadline_tracker.list_tracked_deadlines(case_id, owed_by="")`.
5. Soft links #4–#7: one `data-go` / `data-tab` each.
