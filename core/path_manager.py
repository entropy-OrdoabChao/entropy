"""
core/path_manager.py — Single source of truth for all system paths.

All modules MUST import paths from here. No hardcoded paths anywhere else.
When deployed to Windows, change BASE_PATH only. Everything else derives from it.
"""

from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import os
from pathlib import Path

# ══════════════════════════════════════════════════════════════════════════════
# BASE PATH — Change this for deployment target
# ══════════════════════════════════════════════════════════════════════════════
import sys as _sys

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

def _resolve_base():
    # 1) explicit override always wins
    _env = os.environ.get("BEEZLE_ROOT")
    if _env:
        return Path(_env)
    # 2) derive from this file's location (core/ -> repo root); WSL-safe
    _derived = Path(__file__).resolve().parent.parent
    if (_derived / "pipeline").exists() or (_derived / "obsidian_sync").exists():
        return _derived
    # 3) platform default
    if _sys.platform == "win32":
        return _BEEZLE_ROOT
    return _eroot()

BASE_PATH = _resolve_base()

# ══════════════════════════════════════════════════════════════════════════════
# DIRECTORY TREE
# ══════════════════════════════════════════════════════════════════════════════
CORE_DIR        = BASE_PATH / "core"
PIPELINE_DIR    = BASE_PATH / "pipeline"
GENERATION_DIR  = BASE_PATH / "generation"
REGULATORY_DIR  = BASE_PATH / "regulatory"
MEMORY_DIR      = BASE_PATH / "memory"
LOGS_DIR        = BASE_PATH / "logs"
OBSIDIAN_DIR    = BASE_PATH / "obsidian_sync"
DATA_DIR        = BASE_PATH / "data"
OUTPUT_DIR      = BASE_PATH / "output"
UTILS_DIR       = BASE_PATH / "utils"

# ══════════════════════════════════════════════════════════════════════════════
# SPECIFIC FILES
# ══════════════════════════════════════════════════════════════════════════════
MEMORY_FILE     = MEMORY_DIR / "memory.md"
LOG_FILE        = LOGS_DIR / "beezle.log"
CONFIG_FILE     = BASE_PATH / "config.yaml"

# ══════════════════════════════════════════════════════════════════════════════
# DATABASE (IMMUTABLE — read-only references)
# ══════════════════════════════════════════════════════════════════════════════
CHROMA_HOST     = os.environ.get("CHROMA_HOST", "localhost")
CHROMA_PORT     = int(os.environ.get("CHROMA_PORT", 8100))
CHROMA_COLLECTION = "legal_docs"

# ══════════════════════════════════════════════════════════════════════════════
# EXTERNAL SERVICES
# ══════════════════════════════════════════════════════════════════════════════
COURTLISTENER_BASE = "https://www.courtlistener.com/api/rest/v3/"

# ══════════════════════════════════════════════════════════════════════════════
# OUTPUT VERSION CONTROL
# ══════════════════════════════════════════════════════════════════════════════
MAX_OUTPUT_VERSIONS = 3

# ══════════════════════════════════════════════════════════════════════════════
# ALL DIRECTORIES (for auto-creation)
# ══════════════════════════════════════════════════════════════════════════════
ALL_DIRS = [
    CORE_DIR, PIPELINE_DIR, GENERATION_DIR, REGULATORY_DIR,
    MEMORY_DIR, LOGS_DIR, OBSIDIAN_DIR, DATA_DIR, OUTPUT_DIR, UTILS_DIR,
]


def ensure_directories():
    """Create all required directories if they don't exist."""
    for d in ALL_DIRS:
        d.mkdir(parents=True, exist_ok=True)


def get_output_path(filename: str) -> Path:
    """Get versioned output path. Rotates last 3 versions."""
    ensure_directories()
    base = OUTPUT_DIR / filename
    stem = base.stem
    suffix = base.suffix

    # Find existing versions
    existing = sorted(
        OUTPUT_DIR.glob(f"{stem}_v*{suffix}"),
        key=lambda p: p.stat().st_mtime if p.exists() else 0,
        reverse=True,
    )

    # Delete oldest if we have MAX_OUTPUT_VERSIONS
    while len(existing) >= MAX_OUTPUT_VERSIONS:
        oldest = existing.pop()
        oldest.unlink(missing_ok=True)

    # Find next version number
    version = 1
    for p in existing:
        try:
            v = int(p.stem.split("_v")[-1])
            version = max(version, v + 1)
        except (ValueError, IndexError):
            pass

    return OUTPUT_DIR / f"{stem}_v{version}{suffix}"


def list_recent_outputs(limit: int = 3) -> list:
    """Return last N output files sorted by modification time."""
    ensure_directories()
    files = sorted(
        OUTPUT_DIR.glob("*"),
        key=lambda p: p.stat().st_mtime if p.exists() else 0,
        reverse=True,
    )
    return [
        {"name": f.name, "path": str(f), "size": f.stat().st_size}
        for f in files[:limit]
        if f.is_file()
    ]
