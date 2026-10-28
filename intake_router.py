r"""
intake_router.py — Phase 1 (routing only)

Watches <home>\intake\ for new files. For each file:
  1. Extracts docket number / case identifier from filename or PDF text (best effort)
  2. Resolves to canonical_id via case_registry
  3. Copies to Obsidian case dir: obsidian_sync/cases/<case_dir>/intake/
  4. Updates a manifest in the case dir noting the new file + timestamp
  5. Writes a trigger file in <home>\intake\_triggers\<case_id>.json
     that the Hermes-side script reads to decide what to do
  6. Moves the original to intake/_processed/<timestamp>_<filename>

Phase 2 (next): a Hermes skill reads the trigger file, runs case_diagnose +
build_scorecard_exhibit, decides motion type, drafts, drops in for_review/.

Run modes:
  python intake_router.py          # one-shot scan
  python intake_router.py --watch  # poll every 60s (for cron we use one-shot)
"""

from core import identity as _ident
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

# ============================================================
# Config
# ============================================================
BEEZLE_ROOT = _eroot()
INTAKE_DIR = BEEZLE_ROOT / "intake"
PROCESSED_DIR = INTAKE_DIR / "_processed"
UNSORTED_DIR = INTAKE_DIR / "_unsorted"
TRIGGERS_DIR = INTAKE_DIR / "_triggers"
OBSIDIAN_CASES = BEEZLE_ROOT / "obsidian_sync" / "cases"
CASE_MAP_PATH = BEEZLE_ROOT / "cases_index" / "case_map.json"
CASE_REGISTRY_PATH = BEEZLE_ROOT / "cases_index" / "case_registry.json"

# Add Beezle to import path so we can use case_registry's short-form lookup
sys.path.insert(0, str(BEEZLE_ROOT))

TRIGGERS_DIR.mkdir(parents=True, exist_ok=True)

# ============================================================
# Case registry adapter
# ============================================================
def load_case_registry():
    """Load case_registry.json + case_map.json and use beezle's CaseRegistry class."""
    try:
        from pipeline.case_registry import CaseRegistry
        reg = CaseRegistry(str(CASE_REGISTRY_PATH))
        return reg
    except Exception as e:
        print(f"[warn] could not load CaseRegistry: {e}")
        return None

def load_case_map():
    if CASE_MAP_PATH.exists():
        return json.loads(CASE_MAP_PATH.read_text(encoding="utf-8"))
    return {}

# ============================================================
# Filename → docket extraction
# ============================================================
DOCKET_PATTERNS = [
    # 2:26-cv-00123 or 2-26-cv-00123 (federal full form)
    re.compile(r"(\d):(\d{2})-cv-(\d{4,6})", re.IGNORECASE),
    re.compile(r"(\d)-(\d{2})-cv-(\d{4,6})", re.IGNORECASE),
    # 25-2822 (federal short form)
    re.compile(r"\b(\d{2})-(\d{4,6})\b"),
    # Philadelphia CCP 9-digit
    re.compile(r"\b(\d{9})\b"),
    # ECF/Dkt/Doc N
    re.compile(r"\b(?:ecf|dkt|doc|de)[\s_-]*(\d{1,4})", re.IGNORECASE),
]

def extract_docket_candidates(filename):
    """Return a list of candidate docket strings from a filename."""
    candidates = []
    name = filename.lower()
    for pat in DOCKET_PATTERNS:
        for m in pat.finditer(filename):
            candidates.append(m.group(0))
    # Also keyword-based party hints
    party_hints = []
    party_keywords = {
        "equifax": "fed:2:26-cv-00123",
        "transunion": "fed:2:26-cv-00123",
        "experian": "fed:2:26-cv-00123",
        "capital one": "fed:2:26-cv-00123",
        "greensky": "fed:2:26-cv-00123",
        "avant": "fed:2:26-cv-00123",
        "amerisave": "fed:2:26-cv-00456",
        "planet home": "fed:2:26-cv-00456",
        "dovenmuehle": "fed:2:26-cv-00456",
        "state farm": "fed:2:26-cv-00789",
        "allstate": "fed:2:26-cv-00789",
        "progressive": "fed:2:26-cv-00789",
        "lippi": "phila_ccp:260100000",
        
    }
    for kw, canonical in party_keywords.items():
        if kw in name:
            party_hints.append(canonical)
    return candidates, party_hints

# ============================================================
# Canonical case_id resolution
# ============================================================
# Phase E LLM-driven case resolution fallback
def _phase_e_resolve_via_llm(filename, registry, case_map):
    """When filename/regex heuristics fail, try Phase E LLM extraction
    on the file text to identify the case. Returns canonical_id or None.
    Soft-fails (returns None) on any error.
    """
    try:
        import sys as _sys
        if str(_eroot()) not in _sys.path:
            _sys.path.insert(0, str(_epkg()))
        from pipeline.intake_engine import extract_facts

        pdf_path = INTAKE_DIR / filename
        if not pdf_path.exists():
            return None

        text = ""
        suffix = pdf_path.suffix.lower()
        if suffix == ".pdf":
            try:
                import PyPDF2
                with open(pdf_path, "rb") as fh:
                    reader = PyPDF2.PdfReader(fh)
                    text = "\n".join((page.extract_text() or "") for page in reader.pages[:5])
            except Exception:
                pass
        elif suffix in (".txt", ".eml", ".md"):
            text = pdf_path.read_text(encoding="utf-8", errors="ignore")[:12000]

        if not text or len(text) < 50:
            return None

        result = extract_facts(text, use_llm=True)
        llm = result.get("llm_facts") or {}

        # Match LLM-extracted case_numbers against registry
        for cn in (llm.get("case_numbers") or []):
            cn_norm = str(cn).strip()
            for canonical_id, entry in registry.items():
                aliases = (entry.get("docket_aliases") or []) + [entry.get("case_number", "")]
                if any(cn_norm in str(a) or str(a) in cn_norm for a in aliases if a):
                    return canonical_id

        # Match LLM-extracted defendants against case captions
        defendants = [d.lower() for d in (llm.get("defendants") or [])]
        if defendants:
            for canonical_id, entry in registry.items():
                cap = str(entry.get("case_caption", "") or entry.get("title", "")).lower()
                if any(d in cap for d in defendants if len(d) > 4):
                    return canonical_id

        return None
    except Exception:
        return None


def resolve_canonical_id(filename, registry, case_map):
    """
    Multi-strategy resolution.
    Returns (canonical_id, confidence, method) or (None, 0, 'unresolved').
    """
    candidates, party_hints = extract_docket_candidates(filename)
    
    # Strategy 1: Try docket patterns via registry's lookup_canonical
    if registry is not None:
        for cand in candidates:
            canon = registry.lookup_canonical(cand)
            if canon:
                return canon, 90, f"docket:{cand}"
    
    # Strategy 2: Direct match against case_map keys
    for canon_id in case_map.keys():
        # Strip "fed:2:" or "phila_ccp:" prefix to get bare case number
        bare = canon_id.split(":")[-1]
        if bare.lower() in filename.lower():
            return canon_id, 85, f"case_map_key:{bare}"
    
    # Strategy 3: Party-name hint (lower confidence)
    if party_hints:
        # If multiple party hints all agree, higher confidence
        unique = set(party_hints)
        if len(unique) == 1:
            return party_hints[0], 60, f"party_hint:{filename}"
        # Multiple cases hit: ambiguous, pick the most recent one (highest case number)
        return None, 0, f"ambiguous_party_hints:{','.join(unique)}"
    
    # Phase E LLM fallback (added by Phase F)
    llm_match = _phase_e_resolve_via_llm(filename, registry, case_map)
    if llm_match:
        return llm_match
    return None, 0, "unresolved"

# ============================================================
# Case dir lookup
# ============================================================
def case_dir_for(canonical_id, case_map):
    """Map canonical_id to its Obsidian case dir name."""
    meta = case_map.get(canonical_id, {})
    case_id = meta.get("case_id", canonical_id.split(":")[-1])
    party = meta.get("party_tag", "unknown")
    jur = meta.get("jurisdiction", "fed")
    
    # Naming convention from existing dirs: <jur>_<case_id>_" + _ident.party_key() + "_v_<party>
    # e.g. fed_26-cv-00123, phila_ccp_260100000_" + _ident.party_key() + "_v_lippi
    case_id_safe = case_id.replace(":", "-").replace("/", "-")
    return f"{jur}_{case_id_safe}_{_ident.party_key()}_v_{party}"

def find_existing_case_dir(canonical_id, case_map):
    """Try to find the exact existing case dir on disk."""
    expected = case_dir_for(canonical_id, case_map)
    expected_path = OBSIDIAN_CASES / expected
    if expected_path.exists():
        return expected_path
    
    # Fuzzy fallback: match by case_id substring
    meta = case_map.get(canonical_id, {})
    case_id_bare = meta.get("case_id", canonical_id.split(":")[-1]).replace(":", "-")
    for d in OBSIDIAN_CASES.iterdir():
        if d.is_dir() and case_id_bare in d.name:
            return d
    return None

# ============================================================
# Main scan
# ============================================================
def scan_intake():
    registry = load_case_registry()
    case_map = load_case_map()
    
    files = [f for f in INTAKE_DIR.iterdir()
             if f.is_file() and not f.name.startswith("_")]
    
    if not files:
        print(f"[{datetime.now().isoformat()}] intake empty.")
        return {"scanned": 0, "routed": 0, "unsorted": 0}
    
    print(f"[{datetime.now().isoformat()}] Scanning {len(files)} files in intake/")
    
    routed = 0
    unsorted = 0
    routed_by_case = {}
    
    for f in files:
        canonical_id, confidence, method = resolve_canonical_id(f.name, registry, case_map)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        if canonical_id is None:
            print(f"  [unsorted] {f.name}  reason={method}")
            dest = UNSORTED_DIR / f"{ts}_{f.name}"
            shutil.move(str(f), str(dest))
            unsorted += 1
            continue
        
        case_dir = find_existing_case_dir(canonical_id, case_map)
        if case_dir is None:
            print(f"  [unsorted] {f.name}  reason=no_case_dir_for_{canonical_id}")
            dest = UNSORTED_DIR / f"{ts}_{f.name}"
            shutil.move(str(f), str(dest))
            unsorted += 1
            continue
        
        # Copy to case_dir/intake/
        case_intake = case_dir / "intake"
        case_intake.mkdir(exist_ok=True)
        dest = case_intake / f"{ts}_{f.name}"
        shutil.copy2(str(f), str(dest))
        print(f"  [routed]   {f.name}  ->  {canonical_id}  (conf={confidence}, method={method})")
        
        # Append to case-level intake log
        log_file = case_dir / "intake" / "_intake_log.md"
        log_file.touch(exist_ok=True)
        with log_file.open("a", encoding="utf-8") as logf:
            logf.write(f"- {datetime.now().isoformat()}  `{f.name}` -> `{dest.name}`  conf={confidence}  method={method}\n")
        
        # Move original to processed/
        proc_dest = PROCESSED_DIR / f"{ts}_{f.name}"
        shutil.move(str(f), str(proc_dest))
        
        routed += 1
        routed_by_case.setdefault(canonical_id, []).append(f.name)
    
    # Emit per-case trigger files for the Hermes drafting cron
    for canonical_id, fnames in routed_by_case.items():
        trigger_path = TRIGGERS_DIR / f"{canonical_id.replace(':', '_')}.json"
        existing = {}
        if trigger_path.exists():
            try:
                existing = json.loads(trigger_path.read_text(encoding="utf-8"))
            except Exception:
                existing = {}
        existing.setdefault("pending_files", []).extend(fnames)
        existing["case_id"] = canonical_id
        existing["last_intake"] = datetime.now().isoformat()
        existing.setdefault("status", "pending_review")
        trigger_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    
    summary = {"scanned": len(files), "routed": routed, "unsorted": unsorted}
    print(f"[done] routed={routed} unsorted={unsorted}")
    return summary

if __name__ == "__main__":
    if "--watch" in sys.argv:
        while True:
            try:
                scan_intake()
            except Exception as e:
                print(f"[error] {e}")
            time.sleep(60)
    else:
        scan_intake()
