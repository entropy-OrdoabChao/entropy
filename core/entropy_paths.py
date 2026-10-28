"""Entropy path shim. The DATA home (ENTROPY_HOME, else BEEZLE_ROOT, else ~/Entropy) mirrors Beezle's layout so the copied
engine runs unchanged: obsidian_sync/ (the vault), intake/ (the drop folder), cases_index/, output/, efiling/, corpus/, config.yaml, .env.
The PACKAGE root is where this code lives; shipped assets (duty maps, templates seed, skills) are read from there."""
import os
from pathlib import Path
PACKAGE = Path(__file__).resolve().parents[1]
def entropy_root() -> Path:
    h = os.environ.get("ENTROPY_HOME") or os.environ.get("BEEZLE_ROOT")
    p = Path(h) if h else Path.home() / "Entropy"
    os.environ.setdefault("BEEZLE_ROOT", str(p))
    return p
def package_root() -> Path:
    return PACKAGE
def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
