# -*- coding: utf-8 -*-
"""
regulatory/mcp_tools_filing_atoms.py
======================================

Atomic MCP tools for the composable filing system.

Tools registered:
  beezle.case_diagnose             — full state of case (defects + cross-case + strategy + score)
  beezle.list_filing_blocks        — what argument/section/boilerplate blocks exist
  beezle.list_filing_types         — what YAML filing-type configs exist
  beezle.recommend_filing_types    — which filings best match this case's defects
  beezle.compose_filing            — load YAML + blocks, hydrate with case data
  beezle.draft_filing              — render composition into .md + .docx
  beezle.build_scorecard_exhibit   — scorecard as .docx
  beezle.finalize_filing           — caption + citations + cert of service + PDF
  beezle.assemble_filing_packet    — bundle motion + exhibits + scorecard with checklist
  beezle.enqueue_for_filing        — add to filing_queue
  beezle.register_outcome          — feed learning loop (separate file, no DB impact)

Hermes calls these in sequence based on user intent.
"""

from __future__ import annotations
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
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

_ROOT_WSL = _eroot()
_ROOT_WIN = _BEEZLE_ROOT
ROOT = _ROOT_WSL if _ROOT_WSL.exists() else _ROOT_WIN
TEMPLATES_DIR = ROOT / "obsidian_sync" / "templates"
LEARNING_DIR = ROOT / "obsidian_sync" / "learning"
OUTPUT_DIR = ROOT / "output" / "filings"

log = logging.getLogger("filing_atoms")


def _ensure_dirs():
    LEARNING_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def register_filing_atom_tools(mcp) -> int:
    """Register all atomic filing tools. Returns count."""
    _ensure_dirs()
    count = 0

    # ── 1. case_diagnose ────────────────────────────────────────────────────
    @mcp.tool
    def case_diagnose(case_id: str) -> dict:
        """One-stop case diagnosis. Returns defects, cross-case findings,
        strategy, posture, score — everything Hermes needs to decide what to file.

        Args:
          case_id: canonical id like "phila_ccp:260100000", "fed:2:26-cv-00789"

        Returns:
          {case_id, caption, court, posture, case_type, is_federal,
           defect_total, fatal_count, defect_categories: {AUTHORITY: 25, ...},
           cross_case_count, cross_case_summary,
           top_defects[:10], strategy_summary, score, recommended_filings[:3]}
        """
        try:
            from pipeline.filing_composer import build_context, recommend_filing_types
            ctx = build_context(case_id)
            recs = recommend_filing_types(case_id, top_n=3)
            top_defects = []
            for d in (ctx.get("defects") or [])[:10]:
                if isinstance(d, dict):
                    top_defects.append({
                        "category": d.get("category", ""),
                        "severity": d.get("severity", ""),
                        "defect": d.get("defect", "")[:200],
                        "source": d.get("source", ""),
                    })
            cross_summary = []
            for d in (ctx.get("cross_case_defects") or [])[:5]:
                if isinstance(d, dict):
                    cross_summary.append({
                        "related_case": d.get("related_case", ""),
                        "source": d.get("source", ""),
                        "matched": (d.get("matched_text") or "")[:150],
                    })
            return {
                "case_id": case_id,
                "caption": ctx.get("caption", ""),
                "court": ctx.get("court", ""),
                "posture": ctx.get("posture", ""),
                "case_type": ctx.get("case_type", ""),
                "is_federal": ctx.get("is_federal", False),
                "defect_total": ctx.get("defect_total", 0),
                "fatal_count": ctx.get("fatal_count", 0),
                "defect_categories": ctx.get("defect_categories", {}),
                "cross_case_count": ctx.get("cross_case_count", 0),
                "cross_case_summary": cross_summary,
                "top_defects": top_defects,
                "recommended_filings": recs,
            }
        except Exception as e:
            import traceback
            return {"error": str(e), "trace": traceback.format_exc()[:1500], "case_id": case_id}
    count += 1

    # ── 2. list_filing_blocks ───────────────────────────────────────────────
    @mcp.tool
    def list_filing_blocks(category: str = "") -> dict:
        """List available template blocks. Hermes uses to see what's reusable.

        Args:
          category: 'arguments', 'sections', 'boilerplate', or '' for all

        Returns: {blocks: [{id, relative_path, title, category, applies_to_defects}]}
        """
        try:
            from pipeline.filing_composer import list_all_blocks
            return {"blocks": list_all_blocks(category)}
        except Exception as e:
            return {"error": str(e), "blocks": []}
    count += 1

    # ── 3. list_filing_types ────────────────────────────────────────────────
    @mcp.tool
    def list_filing_types(filter: str = "") -> dict:
        """List available filing-type configs (YAML compositions).

        Args:
          filter: optional substring to filter ids

        Returns: {filing_types: [{id, court, title, trigger_defects, block_count}]}
        """
        try:
            from pipeline.filing_composer import list_all_filing_types
            all_fts = list_all_filing_types()
            if filter:
                all_fts = [f for f in all_fts if filter.lower() in f.get("id", "").lower()]
            return {"filing_types": all_fts}
        except Exception as e:
            return {"error": str(e), "filing_types": []}
    count += 1

    # ── 4. recommend_filing_types ───────────────────────────────────────────
    @mcp.tool
    def recommend_filing_types(case_id: str, top_n: int = 3) -> dict:
        """Score filing_types against the case's defect profile. Returns ranked list.

        Hermes calls this after case_diagnose to decide which filing(s) to draft.
        """
        try:
            from pipeline.filing_composer import recommend_filing_types as _rec
            return {"recommendations": _rec(case_id, top_n=top_n)}
        except Exception as e:
            return {"error": str(e), "recommendations": []}
    count += 1

    # ── 5. compose_filing ───────────────────────────────────────────────────
    @mcp.tool
    def compose_filing(case_id: str, filing_type: str) -> dict:
        """Compose a filing from blocks based on case diagnosis.

        Loads filing_type YAML, evaluates `when` clauses, hydrates placeholders
        from case data. Returns structured composition.

        Args:
          case_id: canonical id
          filing_type: id from list_filing_types (e.g. 'motion_to_strike_state')

        Returns:
          {case_id, filing_type, court, title, blocks: [{block_id, rendered_text, ...}],
           skipped_blocks, diagnosis_snapshot, warnings, errors}
        """
        try:
            from pipeline.filing_composer import compose_filing as _compose
            from dataclasses import asdict
            c = _compose(case_id, filing_type)
            return asdict(c)
        except Exception as e:
            import traceback
            return {"error": str(e), "trace": traceback.format_exc()[:1500]}
    count += 1

    # ── 6. draft_filing ─────────────────────────────────────────────────────
    @mcp.tool
    def draft_filing(case_id: str, filing_type: str) -> dict:
        """Compose + render to .md and .docx.

        Returns: {md_path, docx_path, filing_type, block_count, errors}
        """
        try:
            from pipeline.filing_composer import compose_filing, render_composition_to_markdown
            from datetime import datetime as _dt

            c = compose_filing(case_id, filing_type)
            if c.errors:
                return {"errors": c.errors, "case_id": case_id, "filing_type": filing_type}

            md_text = render_composition_to_markdown(c)
            ts = _dt.now().strftime("%Y%m%d_%H%M%S")
            safe_case = case_id.replace(":", "_").replace("/", "_")
            out_dir = OUTPUT_DIR / safe_case
            out_dir.mkdir(parents=True, exist_ok=True)
            md_path = out_dir / f"{filing_type}_{ts}.md"
            md_path.write_text(md_text, encoding="utf-8")

            # Build .docx via python-docx (deterministic, no LLM)
            docx_path = None
            try:
                from docx import Document
                from docx.shared import Pt
                doc = Document()
                style = doc.styles["Normal"]
                style.font.name = "Times New Roman"
                style.font.size = Pt(12)
                for cb in c.blocks:
                    for line in cb.rendered_text.split("\n"):
                        line = line.strip()
                        if not line:
                            doc.add_paragraph()
                        elif line.startswith("## "):
                            doc.add_heading(line[3:], level=2)
                        elif line.startswith("# "):
                            doc.add_heading(line[2:], level=1)
                        else:
                            doc.add_paragraph(line)
                docx_path = out_dir / f"{filing_type}_{ts}.docx"
                doc.save(str(docx_path))
            except Exception as e:
                log.warning(f".docx generation failed: {e}")

            return {
                "case_id": case_id,
                "filing_type": filing_type,
                "md_path": str(md_path),
                "docx_path": str(docx_path) if docx_path else None,
                "block_count": len(c.blocks),
                "skipped_count": len(c.skipped_blocks),
                "warnings": c.warnings,
            }
        except Exception as e:
            import traceback
            return {"error": str(e), "trace": traceback.format_exc()[:1500]}
    count += 1

    # ── 7. build_scorecard_exhibit ──────────────────────────────────────────
    @mcp.tool
    def build_scorecard_exhibit(case_id: str) -> dict:
        """Build the violation scorecard as .docx exhibit (Exhibit A).

        Returns: {docx_path, md_path, case_id}
        """
        try:
            from generation.scorecard_v2 import generate_scorecard_v2
            md = generate_scorecard_v2(case_id)
            safe = case_id.replace(":", "_")
            out_dir = OUTPUT_DIR / safe
            out_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            md_path = out_dir / f"scorecard_{ts}.md"
            md_path.write_text(md, encoding="utf-8")

            # Try to generate .docx via build_scorecard_exhibit if data shape works
            docx_path = None
            try:
                # Best-effort: load audit data and pass to build_scorecard_exhibit
                # If signature mismatches we fall through to "md only"
                from generation.scorecard_exhibit_ui import _build_data_from_audit  # type: ignore
                from generation.scorecard_exhibit import build_scorecard_exhibit as _build_sc
                data = _build_data_from_audit(case_id, ROOT)
                if data is not None:
                    docx_path = out_dir / f"scorecard_{ts}.docx"
                    _build_sc(data, docx_path)
            except Exception as e:
                log.warning(f"scorecard .docx generation failed: {e}")

            return {
                "case_id": case_id,
                "md_path": str(md_path),
                "docx_path": str(docx_path) if docx_path else None,
            }
        except Exception as e:
            import traceback
            return {"error": str(e), "trace": traceback.format_exc()[:1500]}
    count += 1

    # ── 8. finalize_filing ──────────────────────────────────────────────────
    @mcp.tool
    def finalize_filing(docx_path: str, case_id: str) -> dict:
        """Run finalize_brief operations: caption verify, citation conversion,
        certificate of service, optional PDF conversion.

        Returns: {docx_path, pdf_path, errors, warnings}
        """
        try:
            from generation.finalize_brief import finalize as _finalize
            # finalize() takes canonical_id and brief_override
            r = _finalize(canonical_id=case_id, brief_override=Path(docx_path))
            if isinstance(r, dict):
                return r
            return {"docx_path": str(r) if r else docx_path, "case_id": case_id}
        except Exception as e:
            import traceback
            return {
                "error": str(e),
                "trace": traceback.format_exc()[:1500],
                "docx_path": docx_path,
                "note": "finalize_brief may need full pipeline state; check logs.",
            }
    count += 1

    # ── 9. assemble_filing_packet ───────────────────────────────────────────
    @mcp.tool
    def assemble_filing_packet(
        case_id: str,
        motion_path: str,
        exhibits: Optional[list[str]] = None,
        scorecard_path: str = "",
        cert_of_service_path: str = "",
    ) -> dict:
        """Bundle the motion + exhibits + scorecard + cert into one folder
        with a checklist.md ready for filing.

        Returns: {packet_dir, checklist_path, files: [...]}
        """
        try:
            safe = case_id.replace(":", "_")
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            packet = OUTPUT_DIR / safe / f"packet_{ts}"
            packet.mkdir(parents=True, exist_ok=True)
            files_in = []

            def _copy(src_str: str, label: str):
                if not src_str:
                    return
                src = Path(src_str)
                if src.exists():
                    dst = packet / src.name
                    dst.write_bytes(src.read_bytes())
                    files_in.append({"label": label, "name": dst.name, "path": str(dst)})

            _copy(motion_path, "motion")
            _copy(scorecard_path, "scorecard")
            _copy(cert_of_service_path, "certificate_of_service")
            for i, e in enumerate(exhibits or [], 1):
                _copy(e, f"exhibit_{chr(65+i-1)}")

            # Build checklist.md
            checklist = [
                f"# Filing Checklist — {case_id}",
                f"Generated: {ts}",
                "",
                "## Files Included",
            ]
            for f in files_in:
                checklist.append(f"- [ ] **{f['label']}**: `{f['name']}`")
            checklist += [
                "",
                "## Pre-Filing Tasks",
                "- [ ] Read motion top-to-bottom",
                "- [ ] Verify caption matches court's official spelling",
                "- [ ] Verify all case-number references are correct",
                "- [ ] Confirm cited authorities are still good law",
                "- [ ] Sign and date in all signature blocks",
                "- [ ] Sign and date verification (if required)",
                "- [ ] Confirm certificate of service date matches filing date",
                "- [ ] Convert .docx to .pdf for ECF upload",
                "- [ ] Confirm ECF login works and you have permissions for this docket",
                "- [ ] Stage all attachments in correct order",
                "",
                "## Post-Filing Tasks",
                "- [ ] Save ECF receipt confirmation",
                "- [ ] Calendar any response deadlines triggered by this filing",
                "- [ ] Notify Hermes via `register_outcome` once filed",
            ]
            checklist_path = packet / "checklist.md"
            checklist_path.write_text("\n".join(checklist), encoding="utf-8")

            return {
                "case_id": case_id,
                "packet_dir": str(packet),
                "checklist_path": str(checklist_path),
                "files": files_in,
            }
        except Exception as e:
            import traceback
            return {"error": str(e), "trace": traceback.format_exc()[:1500]}
    count += 1

    # ── 10. enqueue_for_filing ──────────────────────────────────────────────
    @mcp.tool
    def enqueue_for_filing(
        case_id: str,
        packet_dir: str,
        deadline: str = "",
        court: str = "",
        priority: str = "",
    ) -> dict:
        """Add this packet to the filing queue.

        Returns: {queue_id, priority, deadline, files}
        """
        try:
            from filing_queue import enqueue_document
            packet = Path(packet_dir)
            files = [str(p) for p in packet.iterdir() if p.is_file()]
            r = enqueue_document(
                doc_id=f"{case_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
                case_id=case_id,
                deadline=deadline,
                court=court,
                title=packet.name,
                files=files,
                filing_notes=f"Auto-queued from packet at {packet_dir}",
            )
            return r if isinstance(r, dict) else {"queue_result": str(r), "case_id": case_id}
        except Exception as e:
            import traceback
            return {"error": str(e), "trace": traceback.format_exc()[:1500]}
    count += 1

    # ── 11. register_outcome ────────────────────────────────────────────────
    @mcp.tool
    def register_outcome(
        case_id: str,
        doc_id: str = "",
        filed: bool = True,
        outcome: str = "",
        notes: str = "",
    ) -> dict:
        """Log a filing outcome to the learning loop. Stored in
        obsidian_sync/learning/outcomes.jsonl — does NOT affect the main DB.

        Args:
          case_id: which case
          doc_id: filing identifier (queue_id or packet folder name)
          filed: whether the filing was submitted
          outcome: "granted", "denied", "stricken", "pending", etc.
          notes: free-form notes for the learning loop

        Returns: {logged: True, log_path}
        """
        try:
            LEARNING_DIR.mkdir(parents=True, exist_ok=True)
            log_path = LEARNING_DIR / "outcomes.jsonl"
            entry = {
                "timestamp": datetime.now().isoformat(),
                "case_id": case_id,
                "doc_id": doc_id,
                "filed": filed,
                "outcome": outcome,
                "notes": notes,
            }
            with log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
            return {"logged": True, "log_path": str(log_path), "entry": entry}
        except Exception as e:
            return {"error": str(e), "logged": False}
    count += 1

    return count
