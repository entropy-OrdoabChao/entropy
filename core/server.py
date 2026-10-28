"""core/server.py — the desk's server (minimal, first cut).

`main.py` starts this. It serves `ui/index.html` and the read endpoints the desk needs, mounting the existing
`core.server_endpoints_workspace` routes (`/api/cases`, `/api/cases/{id}`, `/api/scorecards`, `/api/file/{name}`) and adding:

    GET /api/health                       liveness + which HOME this desk reads
    GET /api/deadlines?case_id=&owed_by=  yours and theirs, from deadline_tracker (status active/met/missed/extended/waived)
    GET /api/rule_check/{slug}            cases/<slug>/rule_check.json if the detector has written it

Everything else the desk names in a `data-api` attribute is listed in docs/DESK_API.md and returns 501 with that name,
so a missing endpoint is a visible fact, never a silent failure.
"""
from __future__ import annotations
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from core.entropy_paths import entropy_root, package_root


def create_app() -> FastAPI:
    app = FastAPI(title="Entropy desk", docs_url=None, redoc_url=None)
    home = entropy_root()
    ui = package_root() / "ui" / "index.html"

    try:
        from core.server_endpoints_workspace import register_workspace_endpoints
        register_workspace_endpoints(app)
    except Exception as e:  # the desk still serves without the workspace routes; say so
        app.state.workspace_error = str(e)
    try:
        from core.server_endpoints_desk import register_desk_endpoints   # /api/desk, /api/states, /api/identity
        register_desk_endpoints(app)
    except Exception as e:
        app.state.desk_error = str(e)

    @app.get("/")
    async def desk():
        if not ui.exists():
            raise HTTPException(404, f"desk not found at {ui}")
        return FileResponse(str(ui), media_type="text/html")

    @app.get("/api/health")
    async def health():
        return {"ok": True, "home": str(home), "ui": ui.exists(), "workspace_routes": not hasattr(app.state, "workspace_error"),
                "workspace_error": getattr(app.state, "workspace_error", None),
                "desk_routes": not hasattr(app.state, "desk_error"), "desk_error": getattr(app.state, "desk_error", None)}

    @app.get("/api/deadlines")
    async def deadlines(case_id: str = "", owed_by: str = "", status: str = ""):
        try:
            import deadline_tracker as dt
        except Exception as e:
            raise HTTPException(501, f"deadline_tracker unavailable: {e}")
        return dt.list_tracked_deadlines(case_id=case_id, status=status, days_ahead=9999, owed_by=owed_by)

    @app.get("/api/rule_check/{slug}")
    async def rule_check(slug: str):
        p = home / "obsidian_sync" / "cases" / slug / "rule_check.json"
        if not p.exists():
            return JSONResponse({"slug": slug, "filings": [], "note": "no rule_check.json yet — run pipeline.rule_check on the adverse filing"}, status_code=404)
        return json.loads(p.read_text(encoding="utf-8"))

    # every other endpoint the desk names: a visible 501, never a silent failure
    _planned = {}
    try:
        doc = (package_root() / "docs" / "DESK_API.md").read_text(encoding="utf-8")
        import re
        for verb, path in re.findall(r"\| `(GET|POST) (/api/[^`|?]+)", doc):
            _planned[path.split("{")[0].rstrip("/")] = f"{verb} {path}"
    except Exception:
        pass

    @app.api_route("/api/{rest:path}", methods=["GET", "POST"])
    async def not_yet(rest: str):
        key = "/api/" + rest.split("?")[0]
        for base, name in _planned.items():
            if key.startswith(base):
                raise HTTPException(501, f"planned, not built: {name} (docs/DESK_API.md)")
        raise HTTPException(404, f"unknown endpoint /api/{rest}")

    return app


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(create_app(), host="127.0.0.1", port=8646, log_level="info")
