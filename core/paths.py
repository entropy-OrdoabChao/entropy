"""
Central Path Manager — ALL modules import paths from here.
Maps to <home> on Windows. Adapts per-platform.
"""
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import os
import sys

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

# Detect platform
if sys.platform == "win32":
    BASE_PATH = str(_BEEZLE_ROOT)
else:
    BASE_PATH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Directory tree
CORE_DIR       = os.path.join(BASE_PATH, "core")
PIPELINE_DIR   = os.path.join(BASE_PATH, "pipeline")
GENERATION_DIR = os.path.join(BASE_PATH, "generation")
REGULATORY_DIR = os.path.join(BASE_PATH, "regulatory")
MEMORY_DIR     = os.path.join(BASE_PATH, "memory")
ACTIVE_CASES   = os.path.join(MEMORY_DIR, "active_cases")
LOGS_DIR       = os.path.join(BASE_PATH, "logs")
OBSIDIAN_DIR   = os.path.join(BASE_PATH, "obsidian_sync")
OBSIDIAN_TMPL  = os.path.join(OBSIDIAN_DIR, "templates")
DATA_DIR       = os.path.join(BASE_PATH, "data")
OUTPUT_DIR     = os.path.join(BASE_PATH, "output")
UTILS_DIR      = os.path.join(BASE_PATH, "utils")

# Key files
MEMORY_FILE = os.path.join(MEMORY_DIR, "memory.md")
LOG_FILE    = os.path.join(LOGS_DIR, "execution.log")

ALL_DIRS = [
    CORE_DIR, PIPELINE_DIR, GENERATION_DIR, REGULATORY_DIR,
    MEMORY_DIR, ACTIVE_CASES, LOGS_DIR, OBSIDIAN_DIR, OBSIDIAN_TMPL,
    DATA_DIR, OUTPUT_DIR, UTILS_DIR,
]

def ensure_dirs():
    for d in ALL_DIRS:
        os.makedirs(d, exist_ok=True)

ensure_dirs()
