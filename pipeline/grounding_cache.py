#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pipeline/grounding_cache.py — B: the drafter's CORPUS-RETRIEVAL grounding, moved UP to the analysis gate.

The drafter's canon / restoration / pseudolaw-rebuttal / definition grounding is corpus retrieval (disk-heavy,
several seconds each over the /mnt/c corpus DB). It was run at DRAFT time, adding the ~2-min CPU/disk prep before
the LLM. This module computes that grounding ONCE at ANALYSIS time (case_facts_refresh) from a case-level seed and
caches it per case; the drafter then READS it (instant) instead of retrieving live. If nothing is baked, the
drafter falls back to live retrieval — so this is strictly additive and never breaks the live path.

Deterministic; never raises to the caller.
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import os, re, json
from pathlib import Path

ROOT = _eroot() if os.name == "nt" else _eroot()
CACHE = ROOT / "output" / "grounding_cache"
KEYS = ("canon", "restoration", "pseudolaw", "definition")


def _key(case_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", (case_id or "").strip()).strip("_") or "unknown"


def bake(case_id: str, seed_text: str) -> int:
    """ANALYSIS-TIME: compute the corpus-retrieval grounding for a case from a case-level seed and cache it.
    Returns the number of non-empty grounding blocks baked. Seed = the case's full defect context (CASE_FACTS +
    audit violations) so the baked grounding is the case-level SUPERSET the drafter can draw from."""
    from pipeline.drafter_grounding import (get_canon_grounding, get_restoration_grounding,
                                            get_pseudolaw_rebuttal_grounding, get_definition_grounding)
    bundle = {}
    for k, fn in (("canon", lambda: get_canon_grounding(seed_text)),
                  ("restoration", lambda: get_restoration_grounding(seed_text)),
                  ("pseudolaw", lambda: get_pseudolaw_rebuttal_grounding(seed_text)),
                  ("definition", lambda: get_definition_grounding(case_id, seed_text))):
        try:
            bundle[k] = fn() or ""
        except Exception:
            bundle[k] = ""
    try:
        CACHE.mkdir(parents=True, exist_ok=True)
        (CACHE / f"{_key(case_id)}.json").write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
    except Exception:
        return 0
    return sum(1 for v in bundle.values() if (v or "").strip())


def read(case_id: str):
    """DRAFT-TIME: return the baked grounding bundle {canon,restoration,pseudolaw,definition} for a case, or
    None if nothing is baked (-> drafter retrieves live). None also when baking is in progress (guard against a
    stale read while the analysis gate is recomputing)."""
    if os.environ.get("BEEZLE_GROUNDING_BAKING") == "1":
        return None
    try:
        f = CACHE / f"{_key(case_id)}.json"
        if not f.exists():
            return None
        b = json.loads(f.read_text(encoding="utf-8"))
        return b if any((b.get(k) or "").strip() for k in KEYS) else None
    except Exception:
        return None
