# Desk API the mockup expects

`ui/index.html` is served by `core/server.py` (`python main.py`, or `python -m core.server`), which mounts the workspace routes and answers every planned endpoint below with **501 "planned, not built: <name>"** until it exists. Every button that needs it names its endpoint in a
`data-api` attribute, and the desk says so when tapped. This is the list, grouped by view, so the API can be built to the desk
instead of the other way round. Reads come from the vault per `vault_contract.yaml`; writes are the `desk_writes` there.

| Endpoint | Button(s) | View | Notes |
|---|---|---|---|
| `GET /api/file/{draft}.docx` | Download .docx | Case › Drafts · Send | exists in `core/server_endpoints_workspace.py` as `/api/file/{name}` |
| `GET /api/file/{packet}.zip` | Download packet | Case › Drafts · Send | same endpoint, packet zip |
| `GET /api/settings/env` | Open the .env file | Settings |  |
| `GET /api/settings/logins` | Manage | Settings |  |
| `POST /api/cases/link` | Link as a case | Settings |  |
| `POST /api/courts/check` | Check and add | Settings |  |
| `POST /api/courts/learn` | Save and learn this court | Settings |  |
| `GET /api/deadlines?owed_by=opposing` | Their deadlines list (Deadlines tab, Today card) | Case › Deadlines | **served** — `deadline_tracker.list_tracked_deadlines(owed_by=)`; per-case file via `export_case_deadlines()` → `deadlines.json` |
| `POST /api/deadlines/track` | (pipeline, on each new filing) | Case › Deadlines | `deadline_tracker.track_deadline(owed_by=, consequence=)` |
| `POST /api/documents/move` | Move to another case | Case › Documents |  |
| `POST /api/drafts/bureau_letter` | Draft the bureau letter | Start a case |  |
| `POST /api/efile/confirm` | Looks right | Send |  |
| `POST /api/efile/learn` | Learn a new site | Send |  |
| `POST /api/efile/start` | Start guided filing | Send | CCP e-file (Playwright) or ECF; user files, desk guides |
| `POST /api/email/disconnect` | Disconnect | Settings |  |
| `POST /api/exhibits/attach` | Attach all as Exhibit A | Case › Defects |  |
| `POST /api/forms/build` | Make the forms | Start a case |  |
| `POST /api/intake/confirm` | Confirm | Inbox | desk_writes: Inbox sort into intake/ |
| `POST /api/library/add` | Add to library as guidance | Research |  |
| `POST /api/notes` | Keep as a note, marked unverified | Research |  |
| `POST /api/parties/add` | Add another defendant | Start a case |  |
| `POST /api/parties/confirm` | Use this name and service address | Start a case |  |
| `POST /api/parties/search` | Not the right company, search again | Start a case |  |
| `POST /api/scheduler/pause` | Pause | Settings |  |
| `POST /api/scheduler/start` | Start | Settings |  |
| `POST /api/search` | Search | Research | semantic corpus search, never FTS |
| `POST /api/settings/change` | Change | Settings |  |
| `POST /api/settings/folder` | Choose folder | Settings |  |
| `POST /api/settings/reset` | Forget everything | Settings |  |
| `POST /api/settings/test` | Test and save, Test login and save | Settings |  |
| `POST /api/settings/test_send` | Send | Settings |  |

Routed inside the desk (no API): **Read and send** → Mail & tracking view; **Add proof** → Add documents view; **See the defect** → opens the defect card.

Also served: `GET /api/health`, `GET /api/rule_check/{slug}` (reads `cases/<slug>/rule_check.json` written by `pipeline.rule_check`).

Already served by the package: `GET /api/cases`, `GET /api/cases/{canonical_id}`, `GET /api/scorecards`, `GET /api/file/{name}` (`core/server_endpoints_workspace.py`).
