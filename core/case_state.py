"""
core/case_state.py — Persistent case state between pipeline steps.
V3: adds preliminary_statement, jurisdiction, fact_sections, exhaustion,
    prayer_for_relief, admissions, district, plaintiff.email/role,
    defendant.title/department/email/conduct fields.

Each MCP tool reads/writes this instead of relying on LLM memory.
State lives on disk, not in context.
"""

from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import json
import time
import sys
from pathlib import Path
from typing import Dict, List, Optional

# === BEEZLE_ROOT_SHIM (WSL-safe; auto-injected) ===
import os as _os_broot
from pathlib import Path as _Path_broot
def _resolve_beezle_root():
    _e = _os_broot.environ.get("BEEZLE_ROOT")
    if _e:
        return _Path_broot(_e)
    _d = _Path_broot(__file__).resolve().parent
    for _ in range(5):
        if (_d / "pipeline").exists() or (_d / "obsidian_sync").exists():
            return _d
        _d = _d.parent
    return _Path_broot(str(_eroot()))
_BEEZLE_ROOT = _resolve_beezle_root()
# === END BEEZLE_ROOT_SHIM ===

if sys.platform == "win32":
    STATE_DIR = (_BEEZLE_ROOT / "memory" / "active_cases")
else:
    STATE_DIR = Path(__file__).parent.parent / "memory" / "active_cases"


def _state_path(case_id: str) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / f"{case_id}.json"


def create_state(case_id: str) -> Dict:
    """Create a new empty case state with all V3 fields."""
    state = {
        "case_id": case_id,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "step": "created",
        "locked": False,

        # Step 1: Intake
        "plaintiff": {},
        "defendants": [],
        "facts": [],
        "timeline": [],
        "documents": [],
        "exhibits": [],

        # Step 1 V3: new structured fields
        "preliminary_statement": [],
        "jurisdiction_paragraphs": [],
        "fact_sections": {},
        "exhaustion_paragraphs": [],
        "prayer_for_relief": [],
        "admissions": [],
        "district": "FOR THE EASTERN DISTRICT OF PENNSYLVANIA",

        # Step 2: Classification
        "case_type": "",
        "case_type_confidence": 0.0,
        "court": "",
        "jurisdiction": "",
        "statutes": [],

        # Step 3: Elements
        "counts": [],
        "elements_satisfied": False,
        "missing_elements": [],

        # Step 4: Validation
        "validated": False,
        "validation_result": {},

        # Step 5: Draft
        "draft_path": "",
        "draft_status": "",

        # User overrides
        "damages": "",
        "relief": [],
    }
    save_state(case_id, state)
    return state


def load_state(case_id: str) -> Optional[Dict]:
    """Load case state from disk. Migrates old states to V3 schema."""
    p = _state_path(case_id)
    if p.exists():
        state = json.loads(p.read_text(encoding="utf-8"))
        # Migrate: ensure V3 fields exist
        v3_defaults = {
            "preliminary_statement": [],
            "jurisdiction_paragraphs": [],
            "fact_sections": {},
            "exhaustion_paragraphs": [],
            "prayer_for_relief": [],
            "admissions": [],
            "district": "FOR THE EASTERN DISTRICT OF PENNSYLVANIA",
        }
        migrated = False
        for key, default in v3_defaults.items():
            if key not in state:
                state[key] = default
                migrated = True
        if migrated:
            save_state(case_id, state)
        return state
    return None


def save_state(case_id: str, state: Dict):
    """Save case state to disk."""
    state["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _state_path(case_id).write_text(
        json.dumps(state, indent=2, default=str), encoding="utf-8"
    )


def list_active_cases() -> List[str]:
    """List all active case states."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return [f.stem for f in STATE_DIR.glob("*.json")]


def get_state_summary(case_id: str) -> str:
    """Get a compact summary for LLM context (~200 chars)."""
    state = load_state(case_id)
    if not state:
        return ""
    parts = []
    if state.get("case_type"):
        parts.append(f"type={state['case_type']}")
    if state.get("court"):
        parts.append(f"court={state['court']}")
    if state.get("step"):
        parts.append(f"step={state['step']}")
    if state.get("plaintiff"):
        parts.append(f"plaintiff={state['plaintiff'].get('name', '')}")
    if state.get("defendants"):
        parts.append(f"defendants={len(state['defendants'])}")
    if state.get("damages"):
        parts.append(f"damages={state['damages']}")
    if state.get("draft_path"):
        parts.append(f"draft={state['draft_status']}")
    return " | ".join(parts)
