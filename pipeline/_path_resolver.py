# === BEEZLE_PATH_RESOLVER_V1 ===
"""
pipeline/_path_resolver.py
==========================
Path resolver that handles legacy paths in cases_index/case_map.json.

Why this exists
---------------
case_map.json was originally generated on Windows with paths like
    C:/Users/malic/Documents/pdf/misc/Reordered_Default_Judgment_Packet.pdf
(or with backslashes) which Path.exists() returns False for under WSL.
Multiple downstream
scanners (block14_pinpointer via scorecard_orchestrator._scan_case,
citation_extractor.extract_citations_from_case) silently `continue` on
non-existent paths, producing zero violations / zero citations even
though the underlying PDFs exist on disk.

This module exposes resolve_path(raw) which tries, in order:
    1. The literal path
    2. Windows drive  C:/X  ->  /mnt/c/X  (backslashes also normalized)
    3. Backslash-normalized path
    4. Path relative to BEEZLE_ROOT
    5. Basename fallback via rglob under obsidian_sync/cases/ (cached)

Returns a Path if the file exists, else None.
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import logging
import os
from pathlib import Path

log = logging.getLogger("beezle.path_resolver")

_ROOT = Path(os.environ.get("BEEZLE_ROOT", str(_eroot())))

_basename_index = None
_resolve_cache: dict = {}


def _build_basename_index():
    global _basename_index
    if _basename_index is not None:
        return _basename_index
    idx: dict = {}
    bases = [_ROOT / "obsidian_sync" / "cases"]
    for base in bases:
        if not base.exists():
            continue
        try:
            for p in base.rglob("*"):
                try:
                    if p.is_file():
                        idx.setdefault(p.name.lower(), []).append(p)
                except OSError:
                    continue
        except OSError:
            continue
    _basename_index = idx
    return idx


def _try_windows_drive(s: str):
    """Convert C:\foo or C:/foo  ->  /mnt/c/foo."""
    if len(s) >= 2 and s[1] == ':' and s[0].isalpha():
        drive = s[0].lower()
        rest = s[2:].replace('\\', '/')
        if not rest.startswith('/'):
            rest = '/' + rest
        return f'/mnt/{drive}{rest}'
    return None


def resolve_path(raw):
    """Resolve a raw path string to an existing Path or None."""
    if not raw:
        return None
    key = str(raw)
    if key in _resolve_cache:
        return _resolve_cache[key]

    s = key.strip().strip('"').strip("'")

    candidates = []

    # 1. literal
    candidates.append(s)

    # 2. windows drive
    win = _try_windows_drive(s)
    if win:
        candidates.append(win)

    # 3. backslash normalize
    if '\\' in s:
        norm = s.replace('\\', '/')
        candidates.append(norm)
        win_norm = _try_windows_drive(norm)
        if win_norm:
            candidates.append(win_norm)

    # 4. relative-to-ROOT
    p_obj = Path(s)
    if not p_obj.is_absolute():
        candidates.append(str(_ROOT / s))

    for cand in candidates:
        try:
            p = Path(cand)
            if p.exists():
                _resolve_cache[key] = p
                return p
        except OSError:
            continue

    # 5. basename fallback
    try:
        name = Path(s.replace('\\', '/')).name
    except Exception:
        name = None

    if name:
        idx = _build_basename_index()
        hits = idx.get(name.lower())
        if hits:
            _resolve_cache[key] = hits[0]
            return hits[0]

    _resolve_cache[key] = None
    return None


def clear_cache():
    """Reset the resolver cache and basename index. Call between runs."""
    global _basename_index, _resolve_cache
    _basename_index = None
    _resolve_cache = {}


def stats():
    """Return a dict of (cached_lookups, basename_index_size)."""
    return {
        "cached_lookups": len(_resolve_cache),
        "basename_index_size": (
            sum(len(v) for v in _basename_index.values())
            if _basename_index is not None else 0
        ),
        "basename_index_keys": (
            len(_basename_index) if _basename_index is not None else 0
        ),
    }
