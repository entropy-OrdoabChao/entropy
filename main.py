#!/usr/bin/env python3
"""
main.py — Beezle AI Unified Launcher.

Single command to start the entire system:
    python main.py              # Start all services
    python main.py --ui-only    # Start UI server only
    python main.py --status     # Check service health
    python main.py --strategy   # Run strategy on all cases

Services launched (in order):
    1. ChromaDB        :8100  (external — must be running)
    2. LLM (llama.cpp) :8000  (external — must be running)
    3. Sidecar API     :8201  (background)
    4. Hermes API      :8644  (background)
    5. MCP Server      :8200  (background)
    6. UI Server       :8646  (foreground — main process)

All services auto-detect and report health on startup.
"""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(str(PROJECT_ROOT))


# ======================================================================
# SERVICE DEFINITIONS
# ======================================================================

SERVICES = [
    {
        "name": "ChromaDB",
        "port": 8100,
        "health_url": "http://localhost:8100/api/v1/heartbeat",
        "managed": False,  # external — user starts this
        "required": True,
    },
    {
        "name": "LLM (llama.cpp)",
        "port": 8000,
        "health_url": "http://localhost:8000/health",
        "managed": False,  # external — user starts this
        "required": False,  # system works without LLM (deterministic only)
    },
    {
        "name": "Sidecar API",
        "port": 8201,
        "health_url": "http://localhost:8201/health",
        "managed": True,
        "script": "sidecar_api.py",
    },
    {
        "name": "Hermes API",
        "port": 8644,
        "health_url": "http://localhost:8644/health",
        "managed": True,
        "script": "core/hermes_api.py",
    },
    {
        "name": "MCP Server",
        "port": 8200,
        "health_url": "http://localhost:8200/health",
        "managed": True,
        "script": "beezle_mcp_server.py",
    },
    {
        "name": "UI Server",
        "port": 8646,
        "health_url": "http://localhost:8646/health",
        "managed": True,
        "script": "core/server.py",
        "foreground": True,  # this one runs in foreground
    },
]


def check_health(url: str, timeout: float = 3.0) -> bool:
    """Check if a service is responding."""
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def check_port(port: int) -> bool:
    """Check if something is listening on a port."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(("localhost", port)) == 0


def print_status():
    """Print health status of all services."""
    print("\n  BEEZLE AI — Service Status")
    print("  " + "=" * 50)
    for svc in SERVICES:
        alive = check_port(svc["port"])
        healthy = check_health(svc["health_url"]) if alive else False
        icon = "●" if healthy else ("◐" if alive else "○")
        status = "HEALTHY" if healthy else ("RESPONDING" if alive else "DOWN")
        managed = "" if svc.get("managed") else " (external)"
        req = " [REQUIRED]" if svc.get("required") and not alive else ""
        print(f"  {icon} {svc['name']:20s} :{svc['port']}  {status}{managed}{req}")
    print()


def start_background(script: str, name: str) -> subprocess.Popen:
    """Start a Python script as a background process."""
    cmd = [sys.executable, str(PROJECT_ROOT / script)]
    log_file = PROJECT_ROOT / "logs" / f"{name.lower().replace(' ', '_')}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    proc = subprocess.Popen(
        cmd,
        stdout=open(log_file, "a", encoding="utf-8"),
        stderr=subprocess.STDOUT,
        cwd=str(PROJECT_ROOT),
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    return proc


def start_all():
    """Start all managed services, then run UI in foreground."""
    from core.path_manager import ensure_directories
    ensure_directories()

    print("\n  BEEZLE AI — Legal Decision Engine")
    print("  " + "=" * 50)
    print(f"  Project: {PROJECT_ROOT}")
    print(f"  Python:  {sys.version.split()[0]}")
    print()

    # Check external services
    for svc in SERVICES:
        if not svc.get("managed"):
            alive = check_port(svc["port"])
            icon = "●" if alive else "○"
            status = "running" if alive else "NOT RUNNING"
            print(f"  {icon} {svc['name']:20s} :{svc['port']}  {status}")
            if svc.get("required") and not alive:
                print(f"\n  ERROR: {svc['name']} is required but not running.")
                print(f"  Start it first, then re-run main.py")
                return 1

    # Start managed background services
    procs = []
    for svc in SERVICES:
        if not svc.get("managed") or svc.get("foreground"):
            continue

        if check_port(svc["port"]):
            print(f"  ● {svc['name']:20s} :{svc['port']}  already running")
            continue

        print(f"  ◌ {svc['name']:20s} :{svc['port']}  starting...", end="", flush=True)
        proc = start_background(svc["script"], svc["name"])
        procs.append((svc, proc))

        # Wait for health
        for i in range(15):
            time.sleep(1)
            if check_port(svc["port"]):
                print(f" ● ready")
                break
        else:
            print(f" ○ timeout (check logs/{svc['name'].lower().replace(' ', '_')}.log)")

    # Initialize MCP session for UI proxy
    try:
        import urllib.request, json as _json
        _init = _json.dumps({"jsonrpc": "2.0", "method": "initialize", "id": "init-main",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                "clientInfo": {"name": "beezle-ui", "version": "1.0"}}}).encode()
        _req = urllib.request.Request("http://localhost:8200/mcp", data=_init,
            headers={"Content-Type": "application/json",
                "Accept": "application/json, text/event-stream"})
        _resp = urllib.request.urlopen(_req, timeout=5)
        _sid = _resp.headers.get("mcp-session-id", _resp.headers.get("Mcp-Session", ""))
        if _sid:
            # Store session ID for the proxy to use
            os.environ["MCP_SESSION_ID"] = _sid
            # Send initialized notification
            _notif = _json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode()
            _req2 = urllib.request.Request("http://localhost:8200/mcp", data=_notif,
                headers={"Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                    "mcp-session-id": _sid})
            urllib.request.urlopen(_req2, timeout=5)
            print(f"  ● MCP Session          initialized (session={_sid[:12]}...)")
        else:
            print(f"  ○ MCP Session          no session ID returned")
    except Exception as _e:
        print(f"  ○ MCP Session          failed ({_e})")

    # Start UI server in foreground
    print(f"\n  Starting UI server on http://localhost:8646")
    print(f"  " + "-" * 50)
    print(f"  Open browser: http://localhost:8646")
    print(f"  Press Ctrl+C to stop all services")
    print()

    try:
        import uvicorn
        # Import the app
        from core.server import create_app
        app = create_app()
        uvicorn.run(app, host="0.0.0.0", port=8646, log_level="info")
    except KeyboardInterrupt:
        print("\n  Shutting down...")
    finally:
        for svc, proc in procs:
            try:
                proc.terminate()
                print(f"  Stopped {svc['name']}")
            except Exception:
                pass


def run_strategy():
    """Run strategy on all registered cases."""
    from pipeline.reasoning_orchestrator import build_strategy, save_strategy
    from pipeline.case_loader import load_all_cases

    cases = load_all_cases()
    print(f"\n  Running strategy on {len(cases)} cases...\n")

    for case in cases:
        cid = case["canonical_id"]
        print(f"  {cid}...", end=" ", flush=True)
        try:
            s = build_strategy(cid, include_case_law=False)
            save_strategy(s)
            gates = s.get("gates", {}).get("overall", "?")
            posture = s.get("posture", "?")
            actions = len(s.get("recommended_actions", []))
            print(f"gates={gates}, posture={posture}, actions={actions}")
        except Exception as e:
            print(f"FAILED: {e}")


# ======================================================================
# MAIN
# ======================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Beezle AI — Legal Decision Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python main.py              Start all services + UI
    python main.py --status     Check service health
    python main.py --strategy   Run strategy on all cases
    python main.py --ui-only    Start UI server only
        """,
    )
    parser.add_argument("--status", action="store_true", help="Check service health and exit")
    parser.add_argument("--strategy", action="store_true", help="Run strategy on all cases")
    parser.add_argument("--ui-only", action="store_true", help="Start UI server only (no background services)")
    parser.add_argument("--port", type=int, default=8646, help="UI server port")
    args = parser.parse_args()

    if args.status:
        print_status()
        return 0

    if args.strategy:
        run_strategy()
        return 0

    if args.ui_only:
        import uvicorn
        from core.server import create_app
        app = create_app()
        uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="info")
        return 0

    return start_all()


if __name__ == "__main__":
    sys.exit(main() or 0)