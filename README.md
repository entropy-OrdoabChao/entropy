# Entropy

A legal case desk that runs on your own computer. It reads your court papers, finds the claims and the defects, drafts filings with every citation checked against a law library, tracks deadlines, and helps you send and prove what you sent. Federal law is included; your state's law is added when you pick your state. Nothing leaves your machine unless you connect a service and press Send.

## Get started (10 minutes)

1. **Install.** Windows: run `entropy.ps1`. macOS or Linux: run `./entropy.sh`. It checks your computer, picks the right model, starts the desk, and opens your browser.
2. **Setup.** Six short steps; only the first is required.
   - About you: name, address, phone, the email courts write to, how you sign, your signature (`/s/ Name` by default, or upload a picture of your real one).
   - Your state: we guess it from your address. The state law pack downloads in the background.
   - Email (optional): Gmail with an app password, or any IMAP mailbox. Court notices become deadlines.
   - Model: if LM Studio is running we use it. Otherwise Entropy runs the model itself. Optional cloud model, off by default.
   - Services (optional): PACER, LetterStream, CourtListener, your court's e-filing login.
   - Your first case: load one you have, start a new one, or look around first.
3. **Check the list.** The **Get started** tab shows what's connected. Green everywhere means you're ready.

## The checklist

| Item | Green when |
|---|---|
| Desk running | the page opens at http://localhost:8646 |
| Law library connected | federal pack loaded and checked; search returns sections |
| Model connected | LM Studio, the bundled runner, Ollama, or a cloud model answers |
| Souls loaded | the discipline files are being sent to your model as its instructions |
| State pack | downloaded and checked; any pieces you must fetch yourself are listed with steps |
| Email watching | your mailbox is connected and notices land in Inbox |
| First case | a case exists (loaded or started) |

`entropy doctor` prints the same list in the terminal.

## Which model

- **LM Studio** (simplest): install LM Studio, load `Entropy-9B`, keep it running. The desk finds it at localhost:1234.
- **Let Entropy run it**: the installer downloads the model that fits your hardware (4B for no graphics card, 9B for 12 to 16 GB, 27B for 24 GB) and starts it with the desk.
- **Ollama, llama.cpp, or another server**: paste its URL in Settings.
- **Advanced: Docker with the fast stack**: the same vLLM setup the desk was built on, one container per card, for 24 GB cards. `docker/vllm/compose.yml` and `.env.example` are included.
- **Cloud model** (Claude, GPT, Gemini): optional, off by default, your own key. Only your question and the passages you choose are sent; never your library, your files, or your story.

## Where your things live

`.env` (logins and keys, encrypted) · `config/` (ports, model, courts) · `vault/` (your notes, cases, and the discipline files) · `corpus_packs/` (the law) · `intake/` and `documents/` (drop folders) · `cases/` · `recipes/` (learned filing sites) · `logs/`.

## Your state's law

Federal law works for everyone. Pick your state and the desk downloads its constitution, statutes, regulations, register, court rules, court structure, e-filing, consumer laws, regulators, complaint bodies, court opinions, fee rules and business registry, from official sites only. For some states a few pieces can't be fetched by a program; the desk lists them and walks you through getting each one in your own browser. See `docs/YOUR_STATE.md`.

## What the desk never does

File, send, or pay without your hand. Add anything to your law library that isn't official text that passed the check. Send your data anywhere on its own.


## What is in this package today (2026-09-20)

- `pipeline/`, `generation/`, `core/`, `scrapers/`, `regulatory/` and the root scripts: the engine, copied from Beezle and made person-agnostic. Every path resolves through `core/entropy_paths.py`; every name through `core/identity.py` (from your `.env`); every case through `cases_index/case_registry.json` in your home folder.
- `vault_seed/`: templates, agents, souls and system notes that bootstrap copies into your vault on first run.
- `harness/`: 114 skills and 45 profile souls/configs for a Hermes-style harness (optional).
- `legacy/`: the citation parser modules the resolver needs.
- `corpus_packs/`: the state maps (links only; you download your state's text on demand).
- `ui/index.html`: the desk mockup (Defects tab: rule check per adverse filing; looks: Glass / Modern / Classic / Terminal / Reel for phones). `vault_contract.yaml`: what the desk reads from your vault.
- `entropy.ps1` / `entropy.sh`: create the home folder, run the intake chain once.

Not included yet: the trained models, the law library packs, the desk API, the scheduler.

## Run the tests (no GPU, no network)

```
python _entropy_oob_intake_test.py     # fresh home: bootstrap, load a case, drop papers, route, read, timeline, state (12 steps)
python _entropy_import_smoke.py        # import every module in a fresh home
python _entropy_engine_smoke.py        # deterministic engine calls, MCP server over HTTP, skills/profiles, identity gate
```
Run them from the Beezle root for now; they set `ENTROPY_HOME` to a scratch folder and `PYTHONPATH` to this package.


## Your name, your case, your signature

Nothing in this package names a person. Templates carry `{{PLACEHOLDERS}}`; the engine fills them when it reads a template, from two places: your `.env` (name, address, phone, email, signature, state, county; written by Setup step 1 or `python -m pipeline.setup_env --name "..." --address "..." ...`) and the case's entry in your registry (case number, court, judge, opposing party). The signature block on every filing is the stub at `obsidian_sync/templates/blocks/boilerplate/signature_block_pro_se.md` in your vault, rendered for you; the desk shows the same text under Settings › You.


## License and credits

Entropy is free and open-source software under the GNU AGPL-3.0 (see `LICENSE`): use it, study it, change it, share it; anything you distribute or run as a service stays under the same license with source available, and the copyright notice stays. Entropy ships no legal texts; your library is yours, built from whatever you choose (see `CONTENT_NOTICE.md`). Everything it is built on keeps its own license; see `THIRD_PARTY_NOTICES.md`.
