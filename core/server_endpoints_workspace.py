"""
core/server_endpoints_workspace.py — Workspace tab endpoints (patch_state_fix).

Adds the endpoints needed by the new UI tabs:
  GET /api/cases               — list canonical + history cases
  GET /api/cases/{canonical_id} — case detail (violations + history)
  GET /api/scorecards          — list scorecard exhibit files
  GET /api/file/{name}         — download a file from output/

These are imported by core/server.py via register_workspace_endpoints(app).
"""
from __future__ import annotations
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import JSONResponse, FileResponse


def register_workspace_endpoints(app):
    """Register workspace tab endpoints on the FastAPI app."""
    from core.path_manager import BASE_PATH, OUTPUT_DIR, OBSIDIAN_DIR

    REGISTRY_PATH = BASE_PATH / "cases_index" / "case_registry.json"
    AUDIT_DIR = OUTPUT_DIR / "audit"
    REPLY_BRIEFS_DIR = OUTPUT_DIR / "reply_briefs"

    # ── GET /api/cases ──────────────────────────────────────────────────
    @app.get("/api/cases")
    async def list_cases():
        """List canonical cases (from registry) and historical cases (from Obsidian)."""
        canonical = []
        history = []

        # Path A: registry
        if REGISTRY_PATH.exists():
            try:
                registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
                for cid, entry in registry.items():
                    record = {
                        "canonical_id": cid,
                        "id": cid,
                        "caption": entry.get("caption", cid),
                        "case_court": entry.get("case_court", ""),
                        "court": entry.get("case_court", ""),
                        "docket_id": cid.split(":", 1)[-1],
                        "case_no": cid.split(":", 1)[-1],
                        "judge": entry.get("judge", ""),
                        "case_type": entry.get("case_type", ""),
                    }
                    # Pull score + fatal_count from latest audit data if exists
                    safe = cid.replace(":", "_").replace("/", "_")
                    audit_path = AUDIT_DIR / f"data_{safe}.json"
                    if audit_path.exists():
                        try:
                            audit = json.loads(audit_path.read_text(encoding="utf-8"))
                            sc = audit.get("scorecard", {})
                            hier = audit.get("hierarchy", {})
                            record["score"] = sc.get("score", sc.get("reversibility_score", 0))
                            record["rank"] = sc.get("rank", "")
                            record["fatal_count"] = hier.get("fatal_count", 0)
                        except Exception:
                            pass
                    canonical.append(record)
            except Exception as e:
                pass

        # Path B: Obsidian case logs (history)
        canonical_ids = {c["canonical_id"] for c in canonical}
        if OBSIDIAN_DIR.exists():
            try:
                seen = set()
                for f in sorted(
                    OBSIDIAN_DIR.glob("case_*.md"),
                    key=lambda p: p.stat().st_mtime if p.exists() else 0,
                    reverse=True,
                )[:50]:
                    try:
                        text = f.read_text(encoding="utf-8")
                        # Quick header parse without using the broken schema
                        case_name = ""
                        case_id = ""
                        court = ""
                        score = 0
                        fatal = 0
                        rank = ""
                        for line in text.splitlines()[:20]:
                            line = line.strip()
                            if line.startswith("# "):
                                case_name = line[2:].strip()
                            elif line.startswith("- **Case ID:**"):
                                case_id = line.split("**Case ID:**", 1)[-1].strip()
                            elif line.startswith("- **Court:**"):
                                court = line.split("**Court:**", 1)[-1].strip()
                            elif line.startswith("- **Score:**"):
                                raw = line.split("**Score:**", 1)[-1].strip()
                                # Parse "87/100" → 87
                                try:
                                    score = int(raw.split("/")[0].split()[0])
                                except (ValueError, IndexError):
                                    score = 0
                            elif line.startswith("- **FATAL violations:**"):
                                try:
                                    fatal = int(line.split("**FATAL violations:**", 1)[-1].strip())
                                except (ValueError, IndexError):
                                    fatal = 0
                            elif line.startswith("- **Rank:**"):
                                rank = line.split("**Rank:**", 1)[-1].strip()

                        # Skip if this case ID matches a canonical case (avoid dupe in history)
                        derived_id = ""
                        if case_id:
                            if "cv-" in case_id.lower():
                                derived_id = f"fed:{case_id}"
                            elif case_id.isdigit():
                                derived_id = f"phila_ccp:{case_id}"
                        if derived_id and derived_id in canonical_ids:
                            continue
                        if derived_id and derived_id in seen:
                            continue
                        seen.add(derived_id or f.name)

                        history.append({
                            "id": derived_id or f.stem,
                            "canonical_id": derived_id or f.stem,
                            "caption": case_name or f.stem,
                            "case_no": case_id,
                            "docket_id": case_id,
                            "case_court": court,
                            "court": court,
                            "score": score,
                            "rank": rank,
                            "fatal_count": fatal,
                            "file": f.name,
                        })
                    except Exception:
                        pass
            except Exception:
                pass

        return JSONResponse({
            "canonical": canonical,
            "history": history[:30],
            "canonical_count": len(canonical),
            "history_count": len(history),
        })

    # ── GET /api/cases/{canonical_id} ───────────────────────────────────
    @app.get("/api/cases/{canonical_id:path}")
    async def case_detail(canonical_id: str):
        """Return full case detail: violations + audit history."""
        result = {
            "canonical_id": canonical_id,
            "caption": canonical_id,
            "violations": [],
            "history": [],
        }

        # Pull from registry if it's a canonical case
        if REGISTRY_PATH.exists():
            try:
                registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
                if canonical_id in registry:
                    entry = registry[canonical_id]
                    result["caption"] = entry.get("caption", canonical_id)
                    result["case_court"] = entry.get("case_court", "")
                    result["judge"] = entry.get("judge", "")
                    result["case_type"] = entry.get("case_type", "")
                    result["docket_id"] = canonical_id.split(":", 1)[-1]
            except Exception:
                pass

        # Pull violations from latest audit data
        safe = canonical_id.replace(":", "_").replace("/", "_")
        audit_path = AUDIT_DIR / f"data_{safe}.json"
        if audit_path.exists():
            try:
                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                hier = audit.get("hierarchy", {})
                sc = audit.get("scorecard", {})
                result["score"] = sc.get("score", sc.get("reversibility_score", 0))
                result["rank"] = sc.get("rank", "")
                result["fatal_count"] = hier.get("fatal_count", 0)
                result["serious_count"] = hier.get("serious_count", 0)
                # Flatten by_file violations into a single list
                violations = []
                for fpath, viols in (hier.get("by_file", {}) or {}).items():
                    fname = Path(fpath).name
                    for v in viols:
                        if v.get("severity") in ("fatal", "serious"):
                            violations.append({
                                "citation": v.get("citation", ""),
                                "cited_court": v.get("cited_court", ""),
                                "severity": v.get("severity", ""),
                                "source_filename": fname,
                                "rule_violated": v.get("rule_violated", ""),
                            })
                # Sort fatal first
                violations.sort(key=lambda v: 0 if v["severity"] == "fatal" else 1)
                result["violations"] = violations[:200]
            except Exception as e:
                result["error"] = str(e)

        # Pull audit history (audit_pre_* directories)
        if OUTPUT_DIR.exists():
            try:
                histories = []
                for d in sorted(
                    OUTPUT_DIR.glob("audit_pre_*"),
                    key=lambda p: p.stat().st_mtime if p.exists() else 0,
                    reverse=True,
                )[:10]:
                    histories.append({
                        "timestamp": d.name.replace("audit_pre_", ""),
                        "summary": f"Audit snapshot saved {d.name.replace('audit_pre_', '')}",
                    })
                result["history"] = histories
            except Exception:
                pass

        return JSONResponse(result)

    # ── GET /api/scorecards ─────────────────────────────────────────────
    @app.get("/api/scorecards")
    async def list_scorecards():
        """List scorecard exhibit files in output/ and output/reply_briefs/."""
        scorecards = []
        for search_dir in [OUTPUT_DIR, REPLY_BRIEFS_DIR]:
            if not search_dir.exists():
                continue
            for f in sorted(
                search_dir.glob("scorecard_exhibit_*.docx"),
                key=lambda p: p.stat().st_mtime if p.exists() else 0,
                reverse=True,
            )[:20]:
                if f.is_file():
                    mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                    scorecards.append({
                        "name": f.name,
                        "size": f.stat().st_size,
                        "modified": mtime,
                        "path": str(f.relative_to(OUTPUT_DIR)) if f.is_relative_to(OUTPUT_DIR) else f.name,
                    })
        return JSONResponse({"scorecards": scorecards, "count": len(scorecards)})

    # ── GET /api/file/{name} ────────────────────────────────────────────
    @app.get("/api/file/{name:path}")
    async def download_file(name: str):
        """Download a file from output/ or output/reply_briefs/."""
        # Sanitize: no directory traversal
        if ".." in name or name.startswith("/") or name.startswith("\\"):
            raise HTTPException(status_code=400, detail="Invalid path")
        # Try output/ first, then output/reply_briefs/
        for base in [OUTPUT_DIR, REPLY_BRIEFS_DIR]:
            candidate = base / name
            if candidate.exists() and candidate.is_file():
                return FileResponse(
                    str(candidate),
                    filename=candidate.name,
                    media_type="application/octet-stream",
                )
        raise HTTPException(status_code=404, detail=f"File not found: {name}")
