"""core/server_endpoints_desk.py — live read endpoints that feed the desk's panes.

Mirrors what Beezle's build_desk.py derives at build time, but served live from the Entropy HOME so the UI can
replace its demo arrays with real data on load. Every endpoint is read-only and returns the SAME shapes the UI's
own render functions already consume (CASES[], STATES[], deadline/mail rows), so no markup changes are needed.

    GET /api/desk          cases + deadlines + mail + inbox + notes (one call, the Today/Cases/Inbox/Send/Research panes)
    GET /api/states        every state pack in corpus_packs/states/*.yaml with its real status (loaded|draft|plan)
    GET /api/states/{ST}   the pack's pieces as plan-table rows (name, source, cite, check)
    GET /api/identity      the runtime user (from HOME/.env) — the UI shows the name/state in the rail
"""
from __future__ import annotations
import json, re
from datetime import date, datetime
from pathlib import Path
from core import identity as _identity

from fastapi import FastAPI, HTTPException

from core.entropy_paths import entropy_root, package_root

COURT_LABEL = {"EDPA": "E.D. Pa. (district)", "CA3": "Third Circuit", "PA_CP_PHILA": "Phila. C.C.P.", "PA_SUPER": "Pa. Superior Court"}


def _jload(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _state_loaded(home: Path, st: str) -> bool:
    """A state is 'loaded' when its vector collection exists (chromadb/<ST>/ or corpus/states/<ST>/ with content)."""
    for cand in (home / "chromadb" / st.lower(), home / "chromadb" / f"state_{st.lower()}", home / "corpus" / "states" / st):
        if cand.exists() and any(cand.iterdir()):
            return True
    return False


def _states(home: Path):
    out = []
    for y in sorted((package_root() / "corpus_packs" / "states").glob("*.yaml")):
        st = y.stem.upper()
        try:
            import yaml
            d = yaml.safe_load(y.read_text(encoding="utf-8")) or {}
            d = d.get(st) or next(iter(d.values()), {}) if isinstance(d, dict) else {}
        except Exception:
            d = {}
        name = (d.get("name") if isinstance(d, dict) else None) or st
        verified = str((d.get("status") if isinstance(d, dict) else "") or "")
        status = "loaded" if _state_loaded(home, st) else ("draft" if "verified" in verified else "plan")
        out.append({"code": st, "name": name, "status": status, "verified": verified, "todo": (d.get("todo") if isinstance(d, dict) else "") or ""})
    return out


def _state_plan(st: str):
    y = package_root() / "corpus_packs" / "states" / f"{st.upper()}.yaml"
    if not y.exists():
        raise HTTPException(404, f"no pack for {st}")
    import yaml
    d = yaml.safe_load(y.read_text(encoding="utf-8")) or {}
    d = d.get(st.upper()) or next(iter(d.values()), {})
    rows = []
    for k, v in d.items():
        if not isinstance(v, dict) or k in ("browser_checks",):
            continue
        src = v.get("source") or v.get("code_source") or v.get("register_source") or ""
        rows.append({"piece": k.replace("_", " "), "name": v.get("name") or v.get("trial") or v.get("supreme") or "",
                     "source": src, "cite": v.get("cite", ""), "check": v.get("check", "")})
    return {"code": st.upper(), "name": d.get("name", st), "status": d.get("status", ""), "rows": rows}


def _desk(home: Path):
    today = date.today()
    cases_dir = home / "obsidian_sync" / "cases"
    case_map = _jload(home / "cases_index" / "case_map.json", {})
    deadlines = _jload(home / "output" / "audit" / "deadlines.json", {"active": []})
    tracker = _jload(home / "output" / "audit" / "mailing_tracker.json", {"items": {}})
    dirs = {d.name: d for d in cases_dir.iterdir() if d.is_dir()} if cases_dir.exists() else {}

    def slug_of(key, v=None):
        for c in (key.replace(":", "_"), re.sub(r"^fed_2_", "fed_", key.replace(":", "_")), key.split(":")[-1]):
            if c in dirs:
                return c
        tail = key.split(":")[-1]
        hit = next((n for n in dirs if tail and tail in n), None)
        if hit:
            return hit
        if v:  # explicit slug in the registry, else the title as a folder name (Example v. Example -> example_v_example)
            if v.get("slug") in dirs:
                return v["slug"]
            t = re.sub(r"[^a-z0-9]+", "_", (v.get("title") or "").lower()).strip("_").replace("_vs_", "_v_")
            if t in dirs:
                return t
            pref = next((n for n in sorted(dirs, key=len, reverse=True) if t.startswith(n) or n.startswith(t)), None)
            if pref:
                return pref
        return key.replace(":", "_")

    next_by_slug, dl_rows = {}, []
    for d in deadlines.get("active", []):
        if d.get("status") == "DONE":
            continue
        due = d.get("due")
        if due and due != "READ-ORDER":
            left = (date.fromisoformat(due) - today).days
            cls = "bad" if left <= 7 else ("warn" if left <= 14 else "accent"); chip = f"{due} ({left}d)"
        else:
            cls, chip = "warn", "read the order"
        dl_rows.append({"case": d.get("case", ""), "what": d.get("what", "?"), "rule": d.get("rule", ""), "chip": chip, "cls": cls, "due": due or "9999"})
        s = d.get("case", "")
        if s not in next_by_slug or (due or "9999") < next_by_slug[s][1]:
            next_by_slug[s] = (f'{d.get("what","")} · {chip}', due or "9999")
    dl_rows.sort(key=lambda r: r["due"])

    cases = []
    for key, v in case_map.items():
        if not isinstance(v, dict) or not v.get("title"):
            continue
        slug = slug_of(key, v)
        title = v.get("title", "").title().replace("Vs", "v.").replace("Etal", "et al.")
        court = COURT_LABEL.get(v.get("case_court", ""), v.get("case_court", "") or v.get("jurisdiction", "forum"))
        stations = [{"court": court, "num": v.get("case_id", key), "state": "Active", "cls": "now"}]
        for anum, a in (v.get("appeals") or {}).items() if isinstance(v.get("appeals"), dict) else []:
            st = (a.get("status", "") if isinstance(a, dict) else str(a))[:60]
            acls = "done" if any(w in st.lower() for w in ("dismissed", "closed", "denied")) else "now"
            if acls == "now":
                stations[0]["cls"] = "done"
            stations.append({"court": COURT_LABEL.get((a.get("court") if isinstance(a, dict) else "") or "CA3", "Appeal"), "num": anum, "state": st or "open", "cls": acls})
        defendants = v.get("all_defendants") or []
        base = dirs.get(slug)
        docs = []
        if base and (base / "drafts").exists():
            for f in sorted((base / "drafts").rglob("*_FILING.docx"), key=lambda p: p.stat().st_mtime, reverse=True)[:8]:
                docs.append({"name": f.name, "when": datetime.fromtimestamp(f.stat().st_mtime).strftime("%b %d"), "rel": str(f.relative_to(home))})
        tl = _jload(base / "CASE_TIMELINE.json", None) if base else None
        timeline = sorted(tl["entries"], key=lambda e: e.get("date_filed") or (e.get("mtime_iso") or "")[:10], reverse=True)[:15] if tl and tl.get("entries") else []
        facts = (base / "CASE_FACTS.md").read_text(encoding="utf-8", errors="replace").splitlines()[:80] if base and (base / "CASE_FACTS.md").exists() else []
        cases.append({"slug": slug, "key": key, "name": title[:70], "filed": v.get("filed", True),
                      "about": ("Defendants: " + ", ".join(defendants[:6])) if defendants else court,
                      "next": next_by_slug.get(slug, ("quiet — no deadline tracked", ""))[0], "next_due": next_by_slug.get(slug, ("", "9999"))[1],
                      "people": title[:70], "stations": stations, "docs": docs, "timeline": timeline, "facts": facts})
    cases.sort(key=lambda c: c["next_due"])

    mail = []
    for k, it in tracker.get("items", {}).items():
        hist = it.get("history") or []
        mail.append({"ts": hist[-1][0] if hist else "", "document": it.get("document", "?"), "case_id": it.get("case_id", ""),
                     "status": it.get("status", "?"), "job": it.get("letterstream_job_id") or it.get("job_id") or ""})
    mail.sort(key=lambda r: r["ts"], reverse=True)

    scans = sorted((home / "output" / "email_scans").glob("incoming_*/*.ocr.txt"), key=lambda p: p.stat().st_mtime, reverse=True)[:8] if (home / "output" / "email_scans").exists() else []
    inbox = [{"name": f.stem.replace(".ocr", ""), "when": datetime.fromtimestamp(f.stat().st_mtime).strftime("%b %d"), "folder": f.parent.name} for f in scans]
    notes_dir = home / "obsidian_sync" / "analysis"
    notes = [{"name": f.stem.replace("_", " "), "when": datetime.fromtimestamp(f.stat().st_mtime).strftime("%b %d"), "rel": str(f.relative_to(home))}
             for f in (sorted(notes_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:12] if notes_dir.exists() else [])]
    return {"built": datetime.now().strftime("%Y-%m-%d %H:%M"), "generated_at": datetime.now().isoformat(timespec="seconds"),
            "home": home.name, "identity": {"name": _identity.name(), "state": _identity.state()}, "cases": cases, "deadlines": dl_rows[:20],
            "mail": mail[:16], "inbox": inbox, "notes": notes}


def register_desk_endpoints(app: FastAPI) -> None:
    home = entropy_root()

    @app.get("/api/desk")
    async def desk():
        return _desk(home)

    @app.get("/api/states")
    async def states():
        return {"states": _states(home)}

    @app.get("/api/states/{st}")
    async def state_plan(st: str):
        return _state_plan(st)

    @app.get("/api/identity")
    async def identity():
        from core import identity as ident
        return {"name": ident.name(), "state": ident.state(), "county": ident.get("COUNTY"), "email": ident.email(),
                "address": ident.get("ADDRESS") or ident.get("STREET"), "phone": ident.get("PHONE"), "home": home.name}
