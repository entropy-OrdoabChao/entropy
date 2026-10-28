#!/usr/bin/env python3
"""harness/smoke.py — Entropy cold-start smoke test. The release gate.

Proves, on a THROWAWAY home, that a fresh install does what a new user expects:
  1. every module under core/ pipeline/ generation/ harness/ imports
  2. bootstrap creates the home layout; the seeded vault contains no placeholder/personal leaks
  3. intake_flow runs end-to-end on an empty home
  4. the server boots and the four desk endpoints answer with the right shapes
  5. the served UI loads with zero JS errors and the live-data script took over (Playwright, if installed)
  6. a state pack is listable and loadable

Exit 0 = stable. Exit 1 = a failure is listed. Run:  python3 -m harness.smoke   (from the package root)
No model is needed; nothing here calls an LLM.
"""
from __future__ import annotations

import importlib
import json
import os
import pkgutil
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
# Leak scan. The package must never contain a prior owner's identity, so this list is NOT hardcoded here:
# it is the generic placeholder set plus whatever ENTROPY_LEAK_PATTERNS (a regex) the packager exports at gate time.
# Generic placeholders alone catch the mockup leaks; the packager's private pattern catches their own PII.
_GENERIC = r"you@example\.com|260100001|1400 Walnut St|\(215\) 555-0142"  # demo defendant "Example v. Example Lending" is a legitimate neutral placeholder
BANNED = re.compile("|".join(x for x in (_GENERIC, os.environ.get("ENTROPY_LEAK_PATTERNS", "")) if x), re.I)


def _free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def _get(url: str, timeout: float = 10) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def main() -> int:
    fails: list[str] = []
    ok = lambda msg: print(f"  ✓ {msg}")
    bad = lambda msg: (fails.append(msg), print(f"  ✗ {msg}"))

    home = Path(tempfile.mkdtemp(prefix="entropy_smoke_"))
    os.environ["ENTROPY_HOME"] = str(home)
    sys.path.insert(0, str(PKG))
    print(f"Entropy smoke · package {PKG} · home {home}")

    # 1. imports
    n = 0; broken = []
    for root in ("core", "pipeline", "generation", "harness"):
        if not (PKG / root).is_dir():
            continue
        for m in pkgutil.iter_modules([str(PKG / root)]):
            if m.name == "smoke":
                continue
            n += 1
            try:
                importlib.import_module(f"{root}.{m.name}")
            except Exception as e:  # noqa: BLE001
                broken.append(f"{root}.{m.name}: {type(e).__name__}: {str(e)[:80]}")
    (ok if not broken else bad)(f"imports: {n - len(broken)}/{n} modules" + ("" if not broken else " — " + "; ".join(broken[:4])))

    # 2. bootstrap + leak scan
    r = subprocess.run([sys.executable, "-m", "pipeline.bootstrap"], cwd=PKG, capture_output=True, text=True, timeout=120)
    (ok if r.returncode == 0 else bad)(f"bootstrap exit {r.returncode}" + ("" if r.returncode == 0 else " — " + r.stderr[-200:]))
    vault = home / "obsidian_sync"
    leaks = [str(p.relative_to(home)) for p in vault.rglob("*") if p.is_file() and p.suffix in (".md", ".json", ".yaml", ".txt") and BANNED.search(p.read_text(errors="replace"))]
    (ok if not leaks else bad)(f"seeded vault leak scan: {len(leaks)} hits" + ("" if not leaks else " — " + ", ".join(leaks[:3])))
    for d in ("intake", "corpus", "obsidian_sync/cases", "logs"):
        (ok if (home / d).exists() else bad)(f"home/{d} exists")

    # 3. intake on empty home
    r = subprocess.run([sys.executable, "-m", "pipeline.intake_flow", "--apply"], cwd=PKG, capture_output=True, text=True, timeout=300)
    (ok if r.returncode == 0 else bad)(f"intake_flow --apply exit {r.returncode}" + ("" if r.returncode == 0 else " — " + r.stderr[-200:]))

    # 4. server + endpoints
    port = _free_port()
    srv = subprocess.Popen([sys.executable, "-c",
                            f"import sys; sys.path.insert(0, {str(PKG)!r}); import uvicorn; from core.server import create_app; "
                            f"uvicorn.run(create_app(), host='127.0.0.1', port={port}, log_level='error')"],
                           cwd=PKG, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    base = f"http://127.0.0.1:{port}"
    up = False
    for _ in range(60):
        try:
            h = _get(base + "/api/health", 2); up = True; break
        except Exception:
            time.sleep(0.5)
    if not up:
        bad("server did not come up in 30s — " + (srv.stderr.read()[-300:] if srv.stderr else ""))
    else:
        (ok if h.get("desk_routes") else bad)(f"health: desk_routes={h.get('desk_routes')}")
        try:
            d = _get(base + "/api/desk")
            need = {"cases", "deadlines", "inbox", "identity", "generated_at"}
            miss = need - set(d)
            (ok if not miss else bad)(f"/api/desk shape ({len(d.get('cases', []))} cases)" + ("" if not miss else f" missing {sorted(miss)}"))
            (ok if not BANNED.search(json.dumps(d)) else bad)("/api/desk leak scan")
        except Exception as e:  # noqa: BLE001
            bad(f"/api/desk: {e}")
        try:
            st = _get(base + "/api/states")
            (ok if len(st.get("states", [])) >= 50 else bad)(f"/api/states: {len(st.get('states', []))} packs, default {st.get('default')}")
            first = (st.get("states") or [{}])[0].get("code", "PA")
            one = _get(f"{base}/api/states/{first}")
            (ok if one.get("rows") else bad)(f"/api/states/{first}: {len(one.get('rows', []))} plan rows")
        except Exception as e:  # noqa: BLE001
            bad(f"/api/states: {e}")
        try:
            ident = _get(base + "/api/identity")
            (ok if "name" in ident else bad)("/api/identity shape")
        except Exception as e:  # noqa: BLE001
            bad(f"/api/identity: {e}")

        # 5. UI in a real browser (optional)
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
            with sync_playwright() as p:
                b = p.chromium.launch(); pg = b.new_page(); errs: list[str] = []
                pg.on("pageerror", lambda e: errs.append(str(e)))
                pg.goto(base + "/", wait_until="networkidle", timeout=30000); pg.wait_for_timeout(1500)
                live = pg.evaluate("!!document.getElementById('entropy-live')")
                txt = pg.inner_text("body")
                (ok if not errs else bad)(f"UI: {len(errs)} JS errors" + ("" if not errs else " — " + errs[0][:120]))
                (ok if live else bad)("UI: live-data script present")
                (ok if not BANNED.search(txt) else bad)("UI: rendered text leak scan")
                b.close()
        except ImportError:
            print("  · UI browser check skipped (playwright not installed)")
        except Exception as e:  # noqa: BLE001
            bad(f"UI browser check: {str(e)[:160]}")

    srv.terminate()
    try:
        srv.wait(5)
    except Exception:
        srv.kill()
    shutil.rmtree(home, ignore_errors=True)

    print()
    if fails:
        print(f"UNSTABLE — {len(fails)} failure(s):"); [print("  -", f) for f in fails]
        return 1
    print("STABLE — all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
