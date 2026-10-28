"""
pipeline/draft_pipeline.py — Upgraded pipeline_draft for MCP server.

Drop-in replacement for the pipeline_draft tool in beezle_mcp_server.py.
Routes to case-type-specific builders that populate ALL drafter fields.

To integrate: replace the pipeline_draft and _build_claims_for_type functions
in beezle_mcp_server.py with imports from this module.

Integration patch for beezle_mcp_server.py:
    # Replace the existing pipeline_draft tool with:
    from pipeline.draft_pipeline import pipeline_draft_v3
    
    @mcp.tool
    def pipeline_draft(case_id: str) -> dict:
        return pipeline_draft_v3(case_id)
"""

from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
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
import time
import json
from pathlib import Path
from typing import Dict, List


def pipeline_draft_v3(case_id: str) -> dict:
    """
    STEP 3 (V3): Generate complaint .docx from state.
    Reads everything from disk. Uses case-type-specific builders
    to populate ALL complaint sections.
    
    Returns: dict with status, file path, size
    """
    # Late imports to avoid circular deps
    import sys
    base = _BEEZLE_ROOT if sys.platform == "win32" else Path(__file__).parent.parent
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))
    
    from core.case_state import load_state, save_state
    from generation.complaint_drafter import build_complaint_from_facts
    
    state = load_state(case_id)
    if not state:
        return {"error": f"No state for {case_id}"}
    if not state.get("locked"):
        return {"error": "Case type not locked. Run pipeline_classify first."}
    
    ct = state["case_type"]
    
    # ── Route to case-type-specific builder ──
    if ct == "foia":
        kwargs = _build_foia_kwargs(state)
    else:
        # Fallback: generic builder (original behavior)
        kwargs = _build_generic_kwargs(ct, state)
    
    # ── Generate output path ──
    ts = time.strftime("%Y%m%d_%H%M%S")
    out = base / "output" / f"complaint_{case_id}_{ts}.docx"
    kwargs["output_path"] = out
    
    # ── Version control: check existing versions ──
    _manage_versions(base / "output", case_id)
    
    try:
        path = build_complaint_from_facts(**kwargs)
        state["draft_path"] = str(path)
        state["draft_status"] = "complete"
        state["step"] = "drafted"
        save_state(case_id, state)
        
        return {
            "status": "drafted",
            "file": str(path),
            "size": path.stat().st_size,
            "version": ts,
        }
    except Exception as e:
        import traceback
        return {
            "error": str(e),
            "trace": traceback.format_exc()[:1000],
        }


def _build_foia_kwargs(state: Dict) -> Dict:
    """Build full kwargs for FOIA complaint using foia_builder."""
    from pipeline.foia_builder import build_foia_complaint_args
    
    # Get claims from Obsidian first
    obsidian_claims = _get_obsidian_claims(state["case_type"])
    
    # Inject facts from state
    for c in obsidian_claims:
        if not c.get("facts") or c["facts"] == []:
            c["facts"] = state.get("facts", [])
    
    return build_foia_complaint_args(state, obsidian_claims)


def _build_generic_kwargs(case_type: str, state: Dict) -> Dict:
    """Fallback generic builder — original V1 behavior."""
    claims = _get_obsidian_claims(case_type)
    
    # Inject facts
    for c in claims:
        c["facts"] = state.get("facts", [])
    
    # If no Obsidian claims, try DB + LLM
    if not claims:
        claims = _build_claims_from_db(case_type, state)
    
    return {
        "plaintiff_name": state["plaintiff"].get("name", ""),
        "plaintiff_address": state["plaintiff"].get("address", ""),
        "defendants": state["defendants"],
        "court": "IN THE UNITED STATES DISTRICT COURT",
        "district": state.get("district", "FOR THE EASTERN DISTRICT OF PENNSYLVANIA"),
        "complaint_title": "COMPLAINT FOR INJUNCTIVE RELIEF, DECLARATORY JUDGMENT, DAMAGES, AND MANDAMUS",
        "facts": [],
        "claims_data": claims,
        "exhibits": state.get("exhibits", []),
    }


def _get_obsidian_claims(case_type: str) -> List[Dict]:
    """Load pre-structured claims from Obsidian template."""
    import sys
    base = _BEEZLE_ROOT if sys.platform == "win32" else Path(__file__).parent.parent
    
    ct_file = base / "obsidian_sync" / "templates" / f"{case_type}_case_type.md"
    if ct_file.exists():
        try:
            from core.obsidian_parser import parse_obsidian_counts
            claims = parse_obsidian_counts(str(ct_file))
            if claims and len(claims) > 0:
                return claims
        except Exception:
            pass
    return []


def _build_claims_from_db(case_type: str, state: Dict) -> List[Dict]:
    """Build claims from DB + LLM when no Obsidian template exists.
    This is the original _build_claims_for_type logic."""
    try:
        from pipeline.db_connector import LegalDB
        
        statutes = state.get("statutes", [])
        if not statutes:
            return []
        
        db = LegalDB()
        claims = []
        facts_text = " ".join(state.get("facts", []))[:1500]
        
        for i, statute in enumerate(statutes):
            raw_chunks = db.query_text(
                f"{statute} duties elements requirements shall must", 
                n_results=5
            )
            raw_text = "\n".join([
                h.get("text", h.get("document", ""))[:300] 
                for h in raw_chunks
            ])
            
            if raw_text.strip():
                # Fallback: minimal claim structure
                claims.append({
                    "title": f"VIOLATION - {statute}",
                    "statute": statute,
                    "controlling_law": [statute],
                    "duties": [{"text": f"Compliance with {statute}", "citation": statute}],
                    "elements": [{"desc": "Statutory requirements met", "if": "Requirements apply", "and": "Facts establish violation"}],
                    "facts": [facts_text[:200] if facts_text else "See complaint"],
                    "breach_duty": statute,
                    "breach_conduct": "Failed to comply with statutory requirements",
                    "decision_trigger": {"if": "duties apply", "and": "elements satisfied", "then": f"Court SHALL enforce {statute}"},
                    "relief": ["Appropriate relief under the statute"],
                })
        
        return claims
    except Exception:
        return []


def _manage_versions(output_dir: Path, case_id: str, max_versions: int = 3):
    """Keep only the last N versions of a case's drafts."""
    output_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(
        output_dir.glob(f"complaint_{case_id}_*.docx"),
        key=lambda p: p.stat().st_mtime,
    )
    # Delete oldest if we're at the limit
    while len(existing) >= max_versions:
        oldest = existing.pop(0)
        try:
            oldest.unlink()
        except Exception:
            pass


# Unified alias
draft_document = pipeline_draft_v3
