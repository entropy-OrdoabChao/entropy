"""Where an Entropy install keeps its data. ENTROPY_HOME (env) wins; else ./data next to the package.
Layout (created by bootstrap; mirrors Beezle so the copied engine runs unchanged):  intake/ (drop)  obsidian_sync/cases/<slug>/{intake,_ocr,drafts,...}  cases_index/  corpus/  output/  logs/  .env"""
import os
from pathlib import Path
def home() -> Path:
    h = os.environ.get("ENTROPY_HOME") or os.environ.get("BEEZLE_ROOT")
    p = Path(h) if h else Path(__file__).resolve().parents[1] / "data"
    os.environ.setdefault("BEEZLE_ROOT", str(p)); os.environ.setdefault("ENTROPY_HOME", str(p))
    return p
HOME = home()
DROP = HOME / "intake"                     # the drop folder: the person drops anything here (the desk's Inbox reads it)
VAULT = HOME / "obsidian_sync"             # the vault (memory.md, personality.md, AGENTS.md, cases/); Beezle's folder name kept so the copied engine runs unchanged
CASES = VAULT / "cases"                    # cases/<slug>/intake is what analysis reads
CASES_INDEX = HOME / "cases_index"         # case_registry.json, case_map.json
REGISTRY = CASES_INDEX / "case_registry.json"
LIBRARY = HOME / "corpus"                  # corpus packs + index (chunk_text.db, defs_index.db, corpus_graph.db)
OUTPUT = HOME / "output"                   # audit, new_claims_intake, drafts staging
LOGS = HOME / "logs"
ENV = HOME / ".env"
TRACKER = OUTPUT / "audit" / "mailing_tracker.json"
OUTBOX = HOME / "efiling" / "letterstream_outbox"
EMAILS = VAULT / "emails_full"
