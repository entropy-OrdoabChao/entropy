#!/usr/bin/env python3
"""e2e_trigger — F1: ONE trigger path, no hand step.

    docket notification (email / intake PDF)  ->  case dir  ->  CASE_FACTS + CASE_TIMELINE upsert
      ->  deadlines (deadline_compute_all on the order's ENTRY date -> DEADLINES.json -> deadlines.json)
      ->  m2 order room (scripts/rooms/dispatch_tmux.py; ROOM_DRY=1 = fake lanes, no LLM)
      ->  stub + pipeline.m1 draft  ->  pipeline.m1_filing_gate  ->  _WORKLOG.md row

usage:
    python3 -m pipeline.e2e_trigger --intake <file>   [--order ORDER_17] [--entry-date YYYY-MM-DD] [--ftype motion_to_vacate]
    python3 -m pipeline.e2e_trigger --case  <canonical|slug> --order ORDER_17 [--entry-date ...]
    env: ROOM_DRY=1 (room runs the roster's dry lane command — fake_lane.py — under THIS root's ledger; m1 runs against the
         EXISTING stub), E2E_LIVE=1 (required, together with root == <home>, before the LIVE room/chain is started),
         BEEZLE_ROOT (data root; default = the tree this file lives in, so a sandbox copy never writes outside itself).

Every step is idempotent (re-running on the same intake reports `skipped` with the reason) and logged to
<root>/output/audit/e2e_trigger.log + <case>/drafts/_e2e_state.json (what e2e_status reads). Exit 0 on success; otherwise the
exit code names the failing step (see STEP_RC) and the last line printed is `E2E FAIL <step>: <why>`.

Code comes from LIVE (<home>: intake_router, deadline_tracker, pipeline.deadline_watch, pipeline.m1, m1_filing_gate,
scripts/rooms/dispatch_tmux.py — none of them edited here); DATA stays under BEEZLE_ROOT. Deterministic — no LLM call of its own.

ADMIN / REGULATOR ROOM trigger (F2, 2026-11-10 — NOTE ONLY, no code here yet; the room is wired as its own chain):
    The `room` step above runs the ORDER room (roster order_m2room.yaml) because the trigger is a court order. The second roster
    on the same dispatcher, obsidian_sync/00_system/rooms/admin_room.yaml (chain: scripts/chain_admin_room.sh), is selected by the
    ACTOR SET, not by an order: when intake_router files a drop whose actors (CASE_FACTS '## DEFENDANT POSTURES' rows + the
    sender/addressee of the intake — a CRA, furnisher, servicer, insurer, debt collector, or an agency such as CFPB / FTC / HUD /
    PA DOBI / PA Insurance Dept / a records custodian) include any member of the regulator set
        {CRA, furnisher, servicer, insurer, debt_collector, agency, records_custodian}
    and the intake is NOT a judicial order (no ECF/Control No. order entry), the routing is:
        intake_router (case match)  ->  scripts/chain_admin_room.sh  (CASE_SLUG=<slug> BUNDLE=_bundleNN_admin; DRY=1 = fake lanes)
    i.e. the `deadlines` step is replaced by the room's own deadline-tracker lane (deadline_compute_all / deadline_track per clock)
    and the `m1` step is replaced by the HANDOFF: failure-escalation cards -> the ORDER room / m1 as CARD + GROUND+ lines. A judicial
    order whose actor set ALSO contains a regulator member runs BOTH rosters (order room first; admin room as a sibling bundle).
    The actor-set brief is deterministic (scripts/rooms/admin_actor_set.py: CASE_FACTS postures + drafts/*/agency_complaints/*.md).
    Build log: obsidian_sync/00_system/rooms/ADMIN_ROOM_BUILD.md.
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

LIVE = _eroot()
ROOT = Path(os.environ.get("BEEZLE_ROOT") or Path(__file__).resolve().parent.parent)
PY = "/usr/bin/python3"
STEPS = ["resolve", "upsert", "deadlines", "room", "m1", "gate", "worklog", "admin"]   # admin (F2.2) appended: rc 9; earlier codes unchanged
STEP_RC = {s: i + 2 for i, s in enumerate(STEPS)}          # resolve=2 … worklog=8

# order-class clocks per jurisdiction (deadline_tracker rule keys). The compute_all result is kept whole beside the case;
# only these are DECLARED into DEADLINES.json (deadline_watch.scan_declared then carries them into deadlines.json).
ORDER_RULE_KEYS = {
    "pa_state": ["reconsideration", "appeal_ccp_to_superior", "answer_complaint"],
    "federal": ["appeal_final_judgment", "reply_to_motion"],
}

if str(LIVE) not in sys.path:
    sys.path.insert(0, str(LIVE))
# `python3 -m pipeline.e2e_trigger` from a sandbox cwd binds the `pipeline` package to the SANDBOX copy (a namespace
# package holding only the e2e files); the organs below (pipeline.deadline_watch, pipeline.case_registry via
# intake_router) live in LIVE. Unbind so the next `import pipeline.*` resolves through sys.path[0] = LIVE.
_pk = sys.modules.get("pipeline")
if _pk is not None and not str(list(getattr(_pk, "__path__", []) or [""])[0]).startswith(str(LIVE)):
    try:                                                                # namespace or regular package: extend its search path
        _pk.__path__.append(str(LIVE / "pipeline"))
    except Exception:
        for _k in [k for k in sys.modules if (k == "pipeline" or k.startswith("pipeline.")) and k != __name__]:
            del sys.modules[_k]


# ----------------------------------------------------------------------------------------------------------------------- log
class Log:
    def __init__(self, root: Path, tag: str):
        self.path = root / "output/audit/e2e_trigger.log"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.tag = tag

    def __call__(self, step: str, msg: str):
        line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {self.tag} | {step:9} | {msg}"
        print(line, flush=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def sha12(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:12]


def read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="ignore") if p.exists() else ""


def load_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=1, ensure_ascii=False), encoding="utf-8")


def backup(p: Path) -> Path | None:
    """.bak.<TS> beside any file about to be mutated (repo rule)."""
    if not p.exists():
        return None
    b = p.with_name(p.name + ".bak." + datetime.now().strftime("%Y%m%d_%H%M%S"))
    b.write_bytes(p.read_bytes())
    return b


# ------------------------------------------------------------------------------------------------------------------- resolve
def registry_entries() -> list[dict]:
    reg = load_json(LIVE / "cases_index/case_registry.json", {})
    return [dict(v, _key=k) for k, v in reg.items() if isinstance(v, dict) and v.get("canonical_id")]


def ocr_text_for(intake: Path) -> str:
    """the notification's text: sibling .doctext.txt / .txt, or intake/_ocr/<name>.txt."""
    for cand in (intake.with_name(intake.name + ".doctext.txt"), intake.with_name(intake.name + ".txt"),
                 intake.parent / "_ocr" / (intake.name + ".txt"), intake.parent / "_ocr" / (intake.name + ".doctext.txt.txt")):
        if cand.exists():
            return read_text(cand)
    return read_text(intake) if intake.suffix.lower() in (".txt", ".md") else ""


def resolve_case(intake: Path | None, case: str | None) -> dict:
    """canonical id + slug + court. intake_router.resolve_canonical_id first; then the registry's own canonical ids
    (bare docket in the filename / text) — data-driven, never a hardcoded case."""
    ents = registry_entries()
    canonical, method = None, ""
    if case:
        for e in ents:
            if case in (e["canonical_id"], e.get("slug"), e["_key"]):
                canonical, method = e["canonical_id"], f"arg:{case}"
                break
        if not canonical:
            raise LookupError(f"--case {case!r} is not a canonical id / slug in cases_index/case_registry.json")
    else:
        fname = intake.name
        try:
            import intake_router as ir                                  # LIVE root-level router (read-only use)
            r = ir.resolve_canonical_id(fname, ir.load_case_registry(), ir.load_case_map())
            if r and r[0]:
                canonical, method = r[0], f"intake_router:{r[2]}"
        except Exception as ex:                                         # the router must never block the trigger
            method = f"intake_router_error:{type(ex).__name__}"
        if not canonical:
            text = (fname + "\n" + ocr_text_for(intake)).lower()
            hits = {e["canonical_id"] for e in ents if (e["canonical_id"].split(":")[-1] or "#").lower() in text}
            if len(hits) == 1:
                canonical, method = hits.pop(), "registry_docket_in_filename_or_text"
            elif len(hits) > 1:
                raise LookupError(f"ambiguous docket hits in {fname}: {sorted(hits)}")
            else:
                raise LookupError(f"no registered docket in {fname} (intake_router: {method or 'unresolved'})")
    mine = [e for e in ents if e["canonical_id"] == canonical]
    slug = next((e["slug"] for e in mine if e.get("slug") and (ROOT / "obsidian_sync/cases" / e["slug"] / "CASE_FACTS.md").exists()), None)
    if not slug:
        raise LookupError(f"{canonical}: no registry slug with CASE_FACTS.md under {ROOT}/obsidian_sync/cases")
    court = next((e.get("case_court") for e in mine if e.get("case_court") and "_" in e.get("case_court")), "PA_CP_PHILA" if canonical.startswith("phila_ccp") else "EDPA")
    caption = next((e.get("caption") for e in mine if e.get("caption")), slug)
    return {"canonical": canonical, "slug": slug, "court": court, "caption": caption, "method": method,
            "case_dir": str(ROOT / "obsidian_sync/cases" / slug), "jurisdiction": "pa_state" if court.startswith("PA_") else "federal"}


def order_seq_from_text(text: str, canonical: str) -> int | None:
    """FJD barcode = last 8 docket digits + 5-digit docket sequence (ORDER_17 fixture: '6090058600017'). Rigid, so parse it."""
    bare = canonical.split(":")[-1]
    if not bare.isdigit() or len(bare) < 8:
        return None
    m = re.search(r"\b" + re.escape(bare[-8:]) + r"(\d{5})\b", text)
    return int(m.group(1)) if m else None


def entry_date_for(case_dir: Path, order: str, control_no: str | None, ocr: str, explicit: str | None) -> tuple[str, str]:
    """(iso date, source). --entry-date > CASE_TIMELINE COURT_ORDER row naming the order/control no. ('entered'/'docketed')
    > the order's own signed date (flagged: verify the docket)."""
    if explicit:
        return explicit, "arg"
    tl = load_json(case_dir / "CASE_TIMELINE.json", {})
    rows = (tl.get("timeline") or []) + (tl.get("entries") or []) if isinstance(tl, dict) else list(tl)
    for r in rows:
        blob = json.dumps(r)
        if (order in blob or (control_no and control_no in blob)) and re.search(r"ENTERED|DOCKETED|entered|docketed", blob) and r.get("date"):
            return r["date"][:10], f"CASE_TIMELINE:{r.get('id') or r.get('type') or str(r.get('event', ''))[:40]}"
    m = re.search(r"this\s+(\d{1,2})\*?\s*(?:st|nd|rd|th)?\s+day of\s+([A-Za-z]+),\s*(\d{4})", ocr)
    if m:
        try:
            d = datetime.strptime(f"{m.group(2)} {m.group(1)} {m.group(3)}", "%B %d %Y").date().isoformat()
            return d, "order_signed_date (VERIFY docket entry date — Rule 236 notice controls)"
        except ValueError:
            pass
    return date.today().isoformat(), "today (no date found — VERIFY)"


# -------------------------------------------------------------------------------------------------------------------- upsert
def upsert_record(case_dir: Path, ctx: dict, log: Log) -> dict:
    """CASE_TIMELINE: one E2E_INTAKE row keyed by id; CASE_FACTS: one line in '## INTAKE LEDGER (auto, e2e_trigger)'."""
    tlp = case_dir / "CASE_TIMELINE.json"
    tl = load_json(tlp, {"case_slug": case_dir.name, "timeline": [], "entries": []})
    rid = f"e2e_intake_{ctx['order']}_{ctx['entry_date']}"
    rows = tl.setdefault("timeline", [])
    did = []
    if not any(r.get("id") == rid for r in rows):
        rows.append({"id": rid, "date": ctx["entry_date"], "type": "E2E_INTAKE", "docket_ref": ctx["canonical"],
                     "event": f"{ctx['order']} notification routed by e2e_trigger — {ctx['caption']}; entry date {ctx['entry_date']} "
                              f"({ctx['entry_date_source']}); control no. {ctx.get('control_no') or 'n/a'}; source {ctx['intake_rel']}",
                     "source": ctx["intake_rel"], "recorded_by": "pipeline.e2e_trigger", "sha": ctx["intake_sha"]})
        tl["updated"] = datetime.now().isoformat(timespec="seconds")
        backup(tlp); write_json(tlp, tl); did.append("CASE_TIMELINE")
    cfp = case_dir / "CASE_FACTS.md"
    cf = read_text(cfp)
    head = "## INTAKE LEDGER (auto-maintained by pipeline.e2e_trigger)"
    line = f"- {ctx['entry_date']} | {ctx['order']} | {ctx['canonical']} | control {ctx.get('control_no') or 'n/a'} | {ctx['intake_rel']} | sha {ctx['intake_sha']}"
    if line not in cf:
        backup(cfp)
        if head in cf:
            cf = cf.replace(head + "\n", head + "\n" + line + "\n", 1)
        else:
            cf = cf.rstrip() + f"\n\n{head}\n{line}\n"
        cfp.write_text(cf, encoding="utf-8"); did.append("CASE_FACTS")
    log("upsert", f"{'wrote ' + '+'.join(did) if did else 'skipped (row + ledger line already present)'} id={rid}")
    return {"status": "done" if did else "skipped", "timeline_id": rid, "wrote": did}


# ----------------------------------------------------------------------------------------------------------------- deadlines
def _import_deadline_tracker():
    """deadline_tracker.py (the body of the MCP tools deadline_compute_all / deadline_track) mkdirs a WINDOWS literal
    (`<home>\\lifecycle\\deadlines`) at import — relative under WSL. Import from a scratch cwd so the junk dir lands there."""
    scratch = ROOT / "output/_scratch"; scratch.mkdir(parents=True, exist_ok=True)
    cwd = os.getcwd(); os.chdir(scratch)
    try:
        import deadline_tracker as dt
    finally:
        os.chdir(cwd)
    return dt


def deadlines_step(case_dir: Path, ctx: dict, log: Log) -> dict:
    dt = _import_deadline_tracker()
    res = dt.compute_multiple_deadlines(ctx["canonical"], ctx["entry_date"], ctx["jurisdiction"])   # == MCP deadline_compute_all
    outp = case_dir / f"deadlines_{ctx['order']}_compute_all.json"
    write_json(outp, res)
    keys = ORDER_RULE_KEYS.get(ctx["jurisdiction"], [])
    declp = case_dir / "DEADLINES.json"
    decl = load_json(declp, [])
    if isinstance(decl, dict):
        decl = decl.get("deadlines") or []
    added = []
    for d in res["deadlines"]:
        if d.get("rule_key") not in keys:
            continue
        rule = f"{d['citation']} ({ctx['order']} entered {ctx['entry_date']})"
        if any(x.get("rule") == rule and x.get("due") == d["deadline"] for x in decl):
            continue
        decl.append({"due": d["deadline"], "event_date": ctx["entry_date"], "rule": rule,
                     "what": f"{d['description']} — clock from {ctx['order']} entry ({ctx['entry_date_source']}) [deadline_compute_all {d['rule_key']} {ctx['jurisdiction']}]",
                     "queue_lead_days": 10, "declared_by": "pipeline.e2e_trigger", "source": ctx["intake_rel"]})
        added.append(f"{d['rule_key']}={d['deadline']}")
    if added:
        backup(declp); write_json(declp, decl)
    # carry declared rows into <root>/output/audit/deadlines.json with deadline_watch's OWN scanners (no kanban, no LLM)
    import pipeline.deadline_watch as dw
    dw.RECORD_ROOTS[:] = [ROOT / "obsidian_sync/cases", ROOT / "obsidian_sync/claim_candidates"]
    dw.CASES = dw.RECORD_ROOTS[0]
    out = ROOT / "output/audit/deadlines.json"
    db = dw.load(out) if out.exists() else {"active": [], "done": [], "history": [], "items": [], "deadlines": []}
    known = {x["key"] for x in db["active"]} | {x["key"] for x in db["done"]}
    found = []
    for fn in (dw.scan_declared, dw.scan):
        try:
            found += fn()
        except Exception as ex:
            log("deadlines", f"deadline_watch.{fn.__name__} error {type(ex).__name__}: {ex}")
    new = [f for f in found if f["key"] not in known and f["case"] == case_dir.name]
    for n in new:
        n["status"] = "OPEN"; n["carded"] = False
        db["active"].append(n)
        db.setdefault("history", []).append({"ts": datetime.now().isoformat(), "op": "detected", "key": n["key"], "by": "e2e_trigger"})
    if new:
        backup(out); db["updated"] = datetime.now().isoformat(timespec="seconds"); out.write_text(json.dumps(db, indent=1))
    nxt = sorted((x["due"], x["what"]) for x in db["active"] if x.get("case") == case_dir.name and x.get("status") == "OPEN" and x.get("due", "") >= date.today().isoformat())
    log("deadlines", f"compute_all {res['count']} rules from {ctx['entry_date']} -> {outp.name}; declared +{len(added)} {added}; "
                     f"deadlines.json +{len(new)} row(s) (active for case: {sum(1 for x in db['active'] if x.get('case') == case_dir.name and x.get('status') == 'OPEN')}); next {nxt[0] if nxt else None}")
    return {"status": "done" if (added or new) else "skipped", "computed": res["count"], "declared_added": added,
            "deadlines_json_added": [n["key"] for n in new], "next": nxt[0] if nxt else None, "file": str(outp)}


# ---------------------------------------------------------------------------------------------------------------------- room
def room_state(cards_dir: Path, tag: str) -> dict | None:
    j = load_json(cards_dir / f"{tag}_relay.json", None)
    if not j:
        return None
    return {"tag": tag, "exit": j.get("exit"), "wall_s": j.get("wall_s"), "dry_run": j.get("dry_run"),
            "legs": len(j.get("rooms") or []), "cards": sum(int(r.get("cards") or 0) for r in j.get("rooms") or [])}


def room_step(ctx: dict, state: dict, log: Log, dry: bool, rerun: bool) -> dict:
    cards_dir = ROOT / "output/research/rooms_ab"; ledger_dir = cards_dir / "ledger"
    prev = state.get("room", {}).get("tag") if state.get("intake_sha") == ctx["intake_sha"] else None
    if prev and not rerun:
        st = room_state(cards_dir, prev)
        if st and st.get("exit") == 0:
            log("room", f"skipped: ledger for {prev} already closed exit 0 ({st['legs']} legs, {st['cards']} cards)")
            return dict(st, status="skipped")
    tag = ctx["tag"]
    inputs = Path(ctx["bundle"]) / "room_inputs_m2room"
    roster = ROOT / "obsidian_sync/00_system/rooms/order_m2room.yaml"
    if not roster.exists():
        roster = LIVE / "obsidian_sync/00_system/rooms/order_m2room.yaml"
    env = dict(os.environ); env.pop("PYTHONPATH", None); env["CUDA_VISIBLE_DEVICES"] = ""
    env.update(ROOM_LEDGER=str(ledger_dir), ROOM_OUT=str(cards_dir), ROOM_STAGGER_S="0",
               ROOM_CASE=ctx["canonical"], ROOM_CAPTION=f"{ctx['caption']} — {ctx['order']} (entered {ctx['entry_date']})")
    logp = cards_dir / f"rooms_ab_{tag}_relay.log"
    cards_dir.mkdir(parents=True, exist_ok=True); ledger_dir.mkdir(parents=True, exist_ok=True)
    if dry:
        if not inputs.is_dir():
            raise RuntimeError(f"room inputs missing: {inputs} (dry mode re-uses the bundle's room_inputs_m2room)")
        fake = ROOT / "scripts/rooms/fake_lane.py"
        if not fake.exists():
            fake = LIVE / "scripts/rooms/fake_lane.py"
        env["ROOM_LANE_COMMAND_DRY"] = f"env -u PYTHONPATH {PY} {fake} {{lane}} {{prompt}}"
        cmd = [PY, "-u", str(LIVE / "scripts/rooms/dispatch_tmux.py"), tag, str(inputs), "--roster", str(roster), "--dry-run", "--log", str(logp)]
        log("room", f"DRY start tag={tag} inputs={inputs} roster={roster.name} lane={fake}")
    else:
        if ROOT.resolve() != LIVE.resolve() or os.environ.get("E2E_LIVE") != "1":
            raise RuntimeError(f"live room refused: needs E2E_LIVE=1 and root == {LIVE} (root={ROOT}); use ROOM_DRY=1 for the dry path")
        env.update(ORDER=ctx["order"], SLUG=ctx["slug"], CASE_ID=ctx["canonical"], COURT=ctx["court"], FTYPE=ctx["ftype"],
                   BUNDLE=Path(ctx["bundle"]).name, ORDER_OCR=ctx["ocr_path"], TAG=tag,
                   ORDER_HDR=f"# {ctx['order']} — {ctx['caption']}, control no. {ctx.get('control_no') or 'n/a'}, entered {ctx['entry_date']} ({ctx['intake_rel']})")
        if ctx.get("derive_vehicle"):
            env["DERIVE_VEHICLE"] = "1"                 # F6c: no vehicle pinned -> the room finds every door and STOPS at the user's pick
        cmd = ["bash", str(LIVE / "scripts/chain_order_m2room.sh"), tag]    # the chain runs rooms -> cells -> stub -> m1 -> packet itself
        log("room", f"LIVE chain start tag={tag} (chain_order_m2room.sh; ORDER={ctx['order']} BUNDLE={Path(ctx['bundle']).name})")
    t0 = time.time()
    with (cards_dir / f"rooms_ab_{tag}_dispatch.out").open("w") as fh:
        rc = subprocess.run(cmd, env=env, cwd=str(LIVE), stdout=fh, stderr=subprocess.STDOUT, timeout=int(os.environ.get("E2E_ROOM_TIMEOUT_S", "7200"))).returncode
    st = room_state(cards_dir, tag) or {"tag": tag, "exit": rc}
    st["wall_s"] = st.get("wall_s") or round(time.time() - t0)
    relay = read_text(logp)
    st["relay_exit"] = (re.findall(r"RELAY EXIT (\d+)", relay) or ["none"])[-1]
    log("room", f"done rc={rc} relay_exit={st['relay_exit']} wall={st['wall_s']}s legs={st.get('legs')} cards={st.get('cards')} ledger={ledger_dir / tag}")
    if rc != 0 or str(st["relay_exit"]) != "0":
        raise RuntimeError(f"room {tag} rc={rc} relay_exit={st['relay_exit']} — see {logp}")
    return dict(st, status="done")


# ------------------------------------------------------------------------------------------------------------------ stub + m1
def m1_step(ctx: dict, log: Log, dry: bool, force: bool) -> dict:
    mdir = Path(ctx["bundle"]) / "motions" / f"{ctx['mslug']}_m2room"
    stub = mdir / f"{ctx['stub']}.md"; draft = mdir / f"{ctx['stub']}_DRAFT.md"
    if not stub.exists():
        raise RuntimeError(f"stub missing: {stub} — the room's cells_to_stub step writes it (live chain); dry mode needs an existing stub")
    if draft.exists() and draft.stat().st_mtime >= stub.stat().st_mtime and not force:
        log("m1", f"skipped: {draft.name} present and newer than the stub ({datetime.fromtimestamp(draft.stat().st_mtime):%Y-%m-%d %H:%M})")
        return {"status": "skipped", "stub": str(stub), "draft": str(draft), "rc": 0}
    env = dict(os.environ); env.pop("PYTHONPATH", None)
    logp = mdir / f"m1_{ctx['tag']}.log"
    cmd = [PY, "-u", "-m", "pipeline.m1", str(stub), "--court", ctx["court"], "--type", ctx["ftype"], "--case", ctx["slug"], "--out", str(draft)]
    if os.environ.get("BEEZLE_LLAMA_URL"):
        cmd += ["--llm-url", os.environ["BEEZLE_LLAMA_URL"]]
    log("m1", f"start {' '.join(shlex.quote(c) for c in cmd[3:])} ({'DRY room: existing stub' if dry else 'chain stub'})")
    t0 = time.time()
    with logp.open("w") as fh:
        rc = subprocess.run(cmd, env=env, cwd=str(LIVE), stdout=fh, stderr=subprocess.STDOUT, timeout=1800).returncode
    tail = " ".join(l.strip()[:160] for l in read_text(logp).splitlines() if re.search(r"assembled|cite-gate|\[gate\]|Traceback|Error", l))[:600]
    log("m1", f"rc={rc} wall={round(time.time() - t0)}s draft={'present' if draft.exists() else 'MISSING'} | {tail}")
    if rc != 0 or not draft.exists():
        raise RuntimeError(f"pipeline.m1 rc={rc} draft={'present' if draft.exists() else 'missing'} — {logp}")
    return {"status": "done", "stub": str(stub), "draft": str(draft), "rc": rc, "log": str(logp), "wall_s": round(time.time() - t0)}


def gate_step(ctx: dict, m1: dict, log: Log) -> dict:
    env = dict(os.environ); env.pop("PYTHONPATH", None)
    cmd = [PY, "-m", "pipeline.m1_filing_gate", m1["draft"], "--court", ctx["court"], "--type", ctx["ftype"], "--stub", m1["stub"]]
    r = subprocess.run(cmd, env=env, cwd=str(LIVE), capture_output=True, text=True, timeout=900)
    out = (r.stdout + r.stderr)
    verdict = "PASS" if r.returncode == 0 else "FAIL"
    fails = [l.strip()[:200] for l in out.splitlines() if re.search(r"FAIL|MISSING|WRONG", l)][:8]
    Path(m1["draft"]).with_name(Path(m1["draft"]).stem + f".gate_{ctx['tag']}.txt").write_text(out, encoding="utf-8")
    log("gate", f"{verdict} rc={r.returncode} {'; '.join(fails) if fails else ''}".strip())
    return {"status": "done", "verdict": verdict, "rc": r.returncode, "fails": fails}


# ------------------------------------------------------------------------------------------------------------------- worklog
def worklog_step(case_dir: Path, ctx: dict, results: dict, log: Log) -> dict:
    wl = case_dir / "drafts/_WORKLOG.md"
    wl.parent.mkdir(parents=True, exist_ok=True)
    txt = read_text(wl)
    n = len(re.findall(r"\] RUN e2e_\d+ \(", txt)) + 1
    room = results.get("room", {}); m1 = results.get("m1", {}); gate = results.get("gate", {}); dl = results.get("deadlines", {})
    row = (f"[{datetime.now():%Y-%m-%d %H:%M}] RUN e2e_{n} ({ctx['tag']}{', DRY' if ctx['dry'] else ''}) | DEFECT F1-e2e-wire: order->draft had hand steps | "
           f"FIX pipeline/e2e_trigger.py --intake {ctx['intake_rel']} | RESULT case={ctx['canonical']} {ctx['order']} entered {ctx['entry_date']} "
           f"({ctx['entry_date_source']}); deadlines +{len(dl.get('declared_added', []))} declared/+{len(dl.get('deadlines_json_added', []))} tracked, next {dl.get('next')}; "
           f"room {room.get('tag')} {room.get('status')} exit={room.get('exit')} legs={room.get('legs')} cards={room.get('cards')}; "
           f"m1 {m1.get('status')} rc={m1.get('rc')} draft={Path(m1.get('draft', '')).name}; gate {gate.get('verdict')}")
    if ctx["tag"] in txt:
        log("worklog", "skipped: row for this tag already present")
        return {"status": "skipped", "row": row}
    with wl.open("a", encoding="utf-8") as fh:
        fh.write(("\n" if txt and not txt.endswith("\n") else "") + row + "\n")
    log("worklog", f"row appended -> {wl}")
    return {"status": "done", "row": row, "path": str(wl)}


# ---------------------------------------------------------------------------------------------------------------------- main
def admin_step(ctx: dict, log: Log, dry: bool, admin_tag: str | None, receipt: dict | None) -> dict:
    """F2.2: the admin / regulator room branch. Runs when the trigger names an admin actor set (--admin [cra,furnisher,…] — the
    docket-notification resolver has no actor classifier of its own, so the actor set is declared by the caller / intake router,
    never guessed from keywords here) or when a mail receipt arrives (--receipt). Order: chain_admin_room.sh <tag> (ROOM_DRY=1 ->
    the chain's dry path; LIVE needs E2E_LIVE=1 + root == LIVE like room_step) -> pipeline.admin_clocks (clock cards -> tracked
    deadlines only where the ledger holds a receipt; else PENDING MAIL)."""
    import subprocess
    from pipeline import admin_clocks
    case_dir = Path(ctx["case_dir"]); tag = admin_tag or f"admin_{ctx['canonical'].replace(':', '').replace('-', '')}_1"
    res = {"status": "done", "tag": tag}
    if receipt is None:                       # a receipt only restarts clocks; it never re-runs the room
        if dry:
            log("admin", f"DRY admin room tag={tag} (chain_admin_room.sh not started; clocks from the cards already on disk)")
            res["room"] = "dry"
        else:
            if ROOT.resolve() != LIVE.resolve() or os.environ.get("E2E_LIVE") != "1":
                raise RuntimeError(f"live admin room refused: needs E2E_LIVE=1 and root == {LIVE} (root={ROOT}); use ROOM_DRY=1")
            env = {**os.environ, "CASE_SLUG": ctx["slug"]}
            log("admin", f"LIVE admin chain start tag={tag} (chain_admin_room.sh)")
            rc = subprocess.call(["bash", str(LIVE / "scripts/chain_admin_room.sh"), tag], env=env, cwd=str(LIVE))
            res["room"] = f"chain rc={rc}"
            if rc != 0:
                raise RuntimeError(f"chain_admin_room.sh rc={rc}")
    summ = admin_clocks.run(case_dir, ctx["canonical"], tag, receipt, log=lambda m: log("admin", m))
    res.update(clocks=summ["clocks"], tracked=len(summ["tracked"]), pending_mail=len(summ["pending_mail"]), deadlines_json_added=summ["deadlines_json_added"])
    return res


def find_bundle(case_dir: Path, seq: int | None, order: str) -> Path:
    drafts = case_dir / "drafts"
    if seq is not None:
        for d in sorted(drafts.glob(f"_bundle*_order{seq}")):
            return d
    for d in sorted(drafts.glob(f"_bundle*{order.lower()}*")):
        return d
    return drafts / f"_bundle_e2e_{order.lower()}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    # F6 (2026-11-11): --intake and --case may be combined — scan_intake resolves the case from the document's page text /
    # case-record references and passes both so the trigger does not re-guess from a scanner filename. One of them is required.
    ap.add_argument("--intake", help="the docket notification file (PDF/txt) already in the case's intake/ or an intake drop")
    ap.add_argument("--case", help="canonical id or slug (then --order names the order; with --intake: the resolved case for that document)")
    ap.add_argument("--order", default=None, help="ORDER_<docket seq> (default: parsed from the FJD barcode in the notification text)")
    ap.add_argument("--entry-date", default=None, help="ISO entry (docketing) date; default: CASE_TIMELINE row, else the signed date (flagged)")
    ap.add_argument("--control-no", default=None)
    ap.add_argument("--ftype", default=os.environ.get("FTYPE") or None, help="vehicle (filing_types yaml); omitted = DERIVE_VEHICLE room (bots list the doors, user picks)")
    ap.add_argument("--mslug", default=os.environ.get("MSLUG", "vacate"))
    ap.add_argument("--stub", default=os.environ.get("STUB", "motion_to_vacate"))
    ap.add_argument("--tag", default=None); ap.add_argument("--rerun-room", action="store_true"); ap.add_argument("--force-m1", action="store_true")
    ap.add_argument("--steps", default=",".join(STEPS), help="comma subset of " + ",".join(STEPS))
    ap.add_argument("--admin", default=None, metavar="ACTORS", help="F2.2: run the admin/regulator branch for this actor set (e.g. cra,furnisher); implies step admin")
    ap.add_argument("--admin-tag", default=None)
    ap.add_argument("--receipt", default=None, help="F2.4: mail-receipt JSON {addressee,date,basis,...}; runs ONLY admin_clocks for --case")
    a = ap.parse_args(argv)
    if not a.intake and not a.case:
        ap.error("one of --intake / --case is required")
    if a.receipt:                                  # mail-receipt entry point: no order resolution, no room
        from pipeline import admin_clocks
        ctx = resolve_case(None, a.case) if a.case else None
        if ctx is None:
            raise SystemExit("--receipt needs --case")
        rec = json.loads(Path(a.receipt).read_text())
        summ = admin_clocks.run(Path(ctx["case_dir"]), ctx["canonical"], a.admin_tag, rec)
        print(admin_clocks.table(summ)); return 0
    dry = os.environ.get("ROOM_DRY", "0") == "1"
    want = [s for s in STEPS if s in a.steps.split(",")]
    if a.admin is None and a.steps == ",".join(STEPS):
        want = [s for s in want if s != "admin"]          # the admin step runs only when an actor set is declared
    elif a.admin is not None and "admin" not in want:
        want.append("admin")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    results, ctx, step = {}, {}, "resolve"
    log = Log(ROOT, f"e2e_{ts}")
    try:
        intake = Path(a.intake).resolve() if a.intake else None
        if intake and not intake.exists():
            raise FileNotFoundError(f"--intake {intake} does not exist")
        ctx = resolve_case(intake, a.case)
        case_dir = Path(ctx["case_dir"])
        admin_only = a.admin is not None and intake is None and not any(s in want for s in ("upsert", "deadlines", "room", "m1", "gate"))
        if intake is None and not admin_only:          # --case: the newest order notification in the case intake stands as the trigger document
            cands = sorted(case_dir.glob("intake/*order*.pdf"), key=lambda p: p.stat().st_mtime)
            if not cands:
                raise LookupError(f"--case {a.case}: no *order*.pdf under {case_dir}/intake — pass --intake")
            intake = cands[-1]
        if admin_only:                                  # F2.2: an actor-set trigger has no judicial order to parse
            intake = case_dir / "CASE_FACTS.md"; ocr = ""; seq = None
        else:
            ocr = ocr_text_for(intake)
            seq = order_seq_from_text(ocr, ctx["canonical"])
        order = a.order or (f"ORDER_{seq}" if seq else (f"ADMIN_{a.admin.replace(',', '_')}" if admin_only else f"ORDER_{intake.stem[:24]}"))
        control = a.control_no or (re.search(r"Control No\.?\s*([0-9]{6,})", ocr) or [None, None])[1]
        entry, src = (date.today().isoformat(), "admin trigger (no order)") if admin_only else entry_date_for(case_dir, order, control, ocr, a.entry_date)
        case_key = ctx["canonical"].replace(":", "_").replace("/", "-")
        ctx.update(order=order, control_no=control, entry_date=entry, entry_date_source=src, intake=str(intake),
                   intake_rel=str(intake.relative_to(ROOT)) if str(intake).startswith(str(ROOT)) else str(intake),
                   intake_sha=sha12(intake), ocr_path=str(next((p for p in (intake.with_name(intake.name + ".doctext.txt"), intake.parent / "_ocr" / (intake.name + ".txt")) if p.exists()), intake)),
                   ftype=a.ftype or "motion_to_vacate", derive_vehicle=not a.ftype and not dry, mslug=a.mslug, stub=a.stub, dry=dry, tag=a.tag or f"e2e_{case_key}_{ts}",
                   bundle=str(next(iter(sorted(case_dir.glob("drafts/_bundle*_admin"))), case_dir / "drafts/_bundle_admin") if admin_only else find_bundle(case_dir, seq, order)))
        log = Log(ROOT, ctx["tag"])
        log("resolve", f"{intake.name} -> {ctx['canonical']} ({ctx['method']}) slug={ctx['slug']} court={ctx['court']} {order} control={control} "
                       f"entered {entry} [{src}] bundle={Path(ctx['bundle']).name} dry={dry} root={ROOT}")
        results["resolve"] = {"status": "done", **{k: ctx[k] for k in ("canonical", "slug", "court", "order", "control_no", "entry_date", "entry_date_source", "intake_sha", "bundle")}}
        statep = case_dir / "drafts/_e2e_state.json"
        state = load_json(statep, {})
        def save():
            state.update(tag=ctx["tag"], dry=dry, intake=ctx["intake_rel"], intake_sha=ctx["intake_sha"], order=order, entry_date=entry,
                         updated=datetime.now().isoformat(timespec="seconds"), steps=results, canonical=ctx["canonical"], slug=ctx["slug"])
            for k in ("room", "m1", "gate", "deadlines"):
                if k in results and results[k].get("status") != "skipped" or k not in state:
                    state[k] = results.get(k, state.get(k, {}))
            write_json(statep, state)
        save()
        if "upsert" in want:
            step = "upsert"; results[step] = upsert_record(case_dir, ctx, log); save()
        if "deadlines" in want:
            step = "deadlines"; results[step] = deadlines_step(case_dir, ctx, log); save()
        if "room" in want:
            step = "room"; results[step] = room_step(ctx, state, log, dry, a.rerun_room); save()
        if "m1" in want:
            step = "m1"; results[step] = m1_step(ctx, log, dry, a.force_m1); save()
        if "gate" in want:
            step = "gate"; results[step] = gate_step(ctx, results.get("m1") or state.get("m1") or {}, log); save()
        if "worklog" in want:
            step = "worklog"; results[step] = worklog_step(case_dir, ctx, {**state.get("steps", {}), **results} if "room" not in results else results, log); save()
        if "admin" in want:
            step = "admin"; results[step] = admin_step({**ctx, "case_dir": str(case_dir)}, log, dry, a.admin_tag, None); save()
        log("done", "E2E OK " + " ".join(f"{k}={v.get('status')}" for k, v in results.items()))
        state["updated_ok"] = datetime.now().isoformat(timespec="seconds"); write_json(statep, state)
        return 0
    except Exception as ex:
        results[step] = {"status": "failed", "error": f"{type(ex).__name__}: {ex}"}
        try:
            if ctx.get("case_dir"):
                statep = Path(ctx["case_dir"]) / "drafts/_e2e_state.json"
                st = load_json(statep, {})
                st["last_failure"] = {"tag": ctx.get("tag"), "step": step, "error": results[step]["error"][:300], "ts": datetime.now().isoformat(timespec="seconds")}
                st["updated"] = st["last_failure"]["ts"]
                write_json(statep, st)
        except Exception:
            pass
        log(step, f"FAILED {type(ex).__name__}: {ex}")
        print(f"E2E FAIL {step}: {ex}", file=sys.stderr)
        return STEP_RC.get(step, 1)


if __name__ == "__main__":
    sys.exit(main())
