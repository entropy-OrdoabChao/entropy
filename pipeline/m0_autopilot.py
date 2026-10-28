#!/usr/bin/env python3
"""m0_autopilot — the hands-off chain. Drop files in, get prepped claims out.

USER DOCTRINE (2026-08-31, verbatim anchors):
  "pull all my emails. find violations > start foia request > admin process >
   gather documents and prep for new claims" / "now I need it to be hands off.
   I drop my files in"

DESIGN — thin orchestrator over EXISTING organs (piece-meal, never rebuild):
  STAGE 1  intake_router.route(apply=True)      files -> case intake/ folders
  STAGE 2  gmail_full_pull (incremental)        new mail -> emails_full/
  STAGE 3  m0_claim_scan.scan(batch=N)          agnostic duty-breach discovery
  STAGE 4  promote mature obligor-groups        scan hits -> case candidacy
  STAGE 5  m0_exposure_map per candidate        whole-pie violation routing
  STAGE 6  duty_engine regulator complaint +    admin process starts (FOIA/
           foia_builder (records-request lane)  DOI/CFPB drafts, not filings)
  STAGE 7  accountability_map per candidate     duty/authority/record-as-exhibit
  STAGE 8  report                               NEW_CLAIMS_REPORT.md in vault

Every stage: bounded (batch/N caps), resumable (state json), loud on failure
(no silent skips — a failed stage prints and halts its lane, others continue),
section-by-section (no giant passes). The chain STOPS at prepared drafts —
filing is always the human's act (prepare-only gate, locked).

State: output/autopilot_state.json  {stage_name: {last_run, counts, errors}}
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import json, os, re, subprocess, sys, time
from datetime import datetime
from pathlib import Path

ROOT = Path(os.environ.get("BEEZLE_ROOT", str(_eroot()))).resolve()
# NOTE: env-pinned, not __file__-relative — a sandboxed copy must still see
# the real tree (and a missing tree must fail LOUD below, not silent-empty)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "pipeline") not in sys.path:
    sys.path.insert(0, str(ROOT / "pipeline"))

STATE = ROOT / "output" / "autopilot_state.json"
VAULT = ROOT / "obsidian_sync"
REPORT = VAULT / "NEW_CLAIMS_REPORT.md"
SCAN_OUT = ROOT / "output" / "m0_claim_scan.json"

# a claim-group is "mature" (worth the full m0 chain) when the same obligor
# shows this many independent duty-breach hits — below that it stays on the
# watch list. Tunable; deterministic.
MATURITY = int(os.environ.get("AUTOPILOT_MATURITY", "3"))
CAND_CAP = int(os.environ.get("AUTOPILOT_CAND_CAP", "3"))   # per run

def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def _state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}

def _save(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=2))

def _mark(st: dict, stage: str, **kw) -> None:
    st.setdefault(stage, {})
    st[stage].update({"last_run": _now(), **kw})
    _save(st)

def _run(cmd: list[str], timeout: int = 1500) -> tuple[int, str]:
    """Run a stage as a subprocess (isolation: one stage's crash never takes
    the chain down; memory is returned to the OS between stages)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, cwd=str(ROOT))
        return p.returncode, (p.stdout + p.stderr)[-4000:]
    except subprocess.TimeoutExpired:
        return -9, f"TIMEOUT after {timeout}s"

# ---------------------------------------------------------------- stages

def stage_intake(st: dict) -> None:
    """Files dropped in intake/ route to their case folders (or flag NEW)."""
    from pipeline.intake_router import route
    try:
        res = route(apply=True)
        _mark(st, "intake", ok=True, result=str(res)[:500])
        print(f"[autopilot] intake: routed (apply=True)")
    except Exception as e:
        _mark(st, "intake", ok=False, error=str(e)[:300])
        print(f"[autopilot] intake FAILED: {e}")

def stage_mail(st: dict) -> None:
    """Incremental mail pull (gmail_full_pull is checkpointed; new mail only)."""
    rc, out = _run([sys.executable, "-m", "pipeline.gmail_full_pull"], timeout=1200)
    ok = rc == 0
    _mark(st, "mail", ok=ok, rc=rc, tail=out[-300:])
    print(f"[autopilot] mail pull: rc={rc}")

def stage_scan(st: dict, batch: int = 150) -> None:
    """Agnostic duty-breach discovery over un-scanned originals (checkpointed)."""
    rc, out = _run([sys.executable, "-c",
                    "from pipeline.m0_claim_scan import scan; import json; "
                    f"print(json.dumps(scan(batch={batch})))"], timeout=1700)
    ok = rc == 0
    _mark(st, "scan", ok=ok, rc=rc, tail=out[-300:])
    print(f"[autopilot] claim scan: rc={rc}")

def _mature_groups() -> list[dict]:
    """Obligor-groups from the scan with >= MATURITY independent hits that are
    NOT already an active case (registry check keeps Progressive et al. out)."""
    if not SCAN_OUT.exists():
        # LOUD: a missing scan is a broken chain, not an empty result
        raise FileNotFoundError(f"claim-scan output missing: {SCAN_OUT} — run stage_scan first")
    d = json.loads(SCAN_OUT.read_text())
    reg = {}
    reg_p = ROOT / "cases_index" / "case_registry.json"
    if reg_p.exists():
        try:
            reg = json.loads(reg_p.read_text())
        except Exception:
            pass
    active_tokens = set()
    _GENERIC = {"credit", "reporting", "company", "insurance", "bank", "corp",
                "llc", "services", "service", "group", "financial", "national",
                "management", "agency", "county", "court", "case", "claim"}
    for slug, meta in (reg.get("cases", reg) if isinstance(reg, dict) else {}).items():
        # DISTINCTIVE party tokens only (slug + defendant/party fields) — the
        # full-metadata dump leaked generic words ('credit', 'insurance') and
        # suppressed every candidate (sandbox probe 1: 0 groups from 1,768 hits)
        src = slug + " " + " ".join(str(meta.get(k, "")) for k in
                                    ("defendant", "defendants", "party",
                                     "caption", "name", "title")
                                    if isinstance(meta, dict))
        for w in re.findall(r"[a-z]{5,}", src.lower()):
            if w not in _GENERIC:
                active_tokens.add(w)
    # co-defendants hide behind "ETAL" in registry captions (v-sandbox probe:
    # TRANSUNION LLC survived while being a 2822 defendant). The case folders
    # name every party — harvest defendant lines from each CASE_FACTS.
    for cf in (VAULT / "cases").glob("*/CASE_FACTS.md"):
        try:
            head = cf.read_text(errors="ignore")[:4000]
        except Exception:
            continue
        for ln in head.splitlines():
            if re.search(r"(?i)defendant|respondent|v\.\s|vs\.?\s", ln):
                for w in re.findall(r"[a-z]{5,}", ln.lower()):
                    if w not in _GENERIC:
                        active_tokens.add(w)
    # never sue yourself / the regulator you complain to
    _SELF = {"filer", "filer", "cfpb", "treasury", "department", "fdic",
             "commissioner", "attorney"}
    groups: dict[str, list] = {}
    for c in d.get("claims_found", []):
        ob = (c.get("obligor") or "").strip()
        if not ob or ob.lower() in ("unknown", "n/a"):
            continue
        groups.setdefault(ob, []).append(c)
    out = []
    for ob, hits in groups.items():
        if len(hits) < MATURITY:
            continue
        tok = {w for w in re.findall(r"[a-z]{4,}", ob.lower())}
        if tok & active_tokens:
            continue        # already an active case — its own lane handles it
        if tok & _SELF:
            continue        # self / regulator — not a defendant
        # generic descriptors aren't obligors ("credit reporting company",
        # "The company", "the company named in the complaint") — require a
        # distinctive name token and reject descriptor phrases
        if not (tok - _GENERIC - {"the"}) or re.match(r"(?i)^the\s", ob):
            continue
        out.append({"obligor": ob, "hits": len(hits),
                    "categories": sorted({h.get("category", "") for h in hits})[:6],
                    "files": [h.get("file", "") for h in hits][:10]})
    return sorted(out, key=lambda g: -g["hits"])

def stage_candidates(st: dict) -> list[dict]:
    cands = _mature_groups()[:CAND_CAP]
    _mark(st, "candidates", ok=True, count=len(cands),
          obligors=[c["obligor"] for c in cands])
    print(f"[autopilot] mature candidates: {[c['obligor'] for c in cands]}")
    return cands

def _cand_dir(ob: str) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "_", ob.lower()).strip("_")[:40]
    d = VAULT / "claim_candidates" / slug
    d.mkdir(parents=True, exist_ok=True)
    return d

def stage_exposure(st: dict, cands: list[dict]) -> None:
    """Whole-pie exposure map per candidate. Needs a facts file: synthesize a
    CASE_FACTS-shaped seed from the scan's own grounded findings — the scan
    record carries breach/duty/why per email (deterministic weave; the email
    files use a different naming scheme and may live in unmatched/, so we
    never chase paths — the scan already read them)."""
    done = []
    scan = json.loads(SCAN_OUT.read_text()) if SCAN_OUT.exists() else {}
    by_file = {}
    for h in scan.get("claims_found", []):
        ob = (h.get("obligor") or "").strip()
        by_file.setdefault(ob, []).append(h)
    for c in cands:
        d = _cand_dir(c["obligor"])
        cf = d / "CASE_FACTS.md"
        if not cf.exists():
            hits = by_file.get(c["obligor"], [])
            body = [f"# CASE FACTS — {c['obligor']} (autopilot candidate)",
                    f"*Seeded {_now()} from m0_claim_scan; {c['hits']} duty-breach hits.*",
                    "", "## OPERATIVE FACTS", ""]
            for i, h in enumerate(hits[:25], 1):
                dt = (re.match(r"(\d{4}-\d{2}-\d{2})_", h.get("file", "")) or [None, ""])[1]
                body.append(
                    f"{i}. {('On ' + dt + ', ') if dt and not dt.startswith('0000') else ''}"
                    f"{c['obligor']} — {h.get('breach', 'duty breach recorded')} "
                    f"(duty: {h.get('duty_type', '?')}; source email: `{h.get('file', '?')}`)")
            body += ["", "## THE THEORY", ""]
            cats = {}
            for h in hits:
                cats.setdefault(h.get("category", "uncategorized"), h.get("why", ""))
            for j, (cat, why) in enumerate(sorted(cats.items()), 1):
                body.append(f"{j}. {cat}: {why[:300]}")
            body += ["", "## CLAIM CANDIDATES (from scan)", ""]
            body += [f"- {cat}" for cat in c["categories"]]
            cf.write_text("\n".join(body))
        rc, out = _run([sys.executable, "-m", "pipeline.m0_exposure_map", str(d)],
                       timeout=1700)
        done.append({"obligor": c["obligor"], "rc": rc})
        print(f"[autopilot] exposure[{c['obligor']}]: rc={rc}")
    _mark(st, "exposure", ok=all(x["rc"] == 0 for x in done), results=done)

def stage_admin(st: dict, cands: list[dict]) -> None:
    """Admin-process starters: duty_engine regulator complaint per candidate.
    Actions derive from the exposure map's own [duty_map: id] grounding tags —
    the map already decided which duties fired; we hand those duties' own
    triggering_actions back to the engine (deterministic round-trip, no
    guessing). Draft only, never sent."""
    from pipeline.duty_engine import run_duty_analysis, load_duty_map
    _PFX = {"ins": "insurance_company", "dc": "debt_collector", "bk": "bank_making_loan",
            "ms": "mortgage_servicer", "cra": "credit_reporting_agency", "cmn": "common_defendant",
            "crt": "court", "jud": "judge", "opp": "opposing_counsel", "proc": "procedural_rules",
            "const": "constitutional"}
    done = []
    for c in cands:
        d = _cand_dir(c["obligor"])
        expo = d / "EXPOSURE_MAP.md"
        if not expo.exists():
            print(f"[autopilot] admin[{c['obligor']}]: no exposure map — skip (loud)")
            done.append({"obligor": c["obligor"], "skipped": "no exposure map"})
            continue
        text = expo.read_text(errors="ignore")
        ids = set(re.findall(r"\[duty_map: ([a-z_0-9]+)\]", text))
        if not ids:
            print(f"[autopilot] admin[{c['obligor']}]: exposure map carries no duty-map "
                  f"violations — nothing to refer yet (facts too thin; deepen intake)")
            done.append({"obligor": c["obligor"], "skipped": "no duty-map violations"})
            continue
        by_ent: dict[str, set] = {}
        for did in ids:
            pfx = did.split("_")[0]
            ent = _PFX.get(pfx)
            if ent:
                by_ent.setdefault(ent, set()).add(did)
        drafts = []
        facts = (d / "CASE_FACTS.md").read_text(errors="ignore")[:4000]
        for ent, dids in list(by_ent.items())[:3]:
            try:
                dm = load_duty_map(ent)
                acts = set()
                for duty in dm.get("duties", []):
                    if duty.get("id") in dids:
                        acts.update(duty.get("triggering_actions") or [])
                r = run_duty_analysis(entity_type=ent, actions=sorted(acts),
                                      fact_pattern=facts)
                rc_d = r.regulator_complaint
                if rc_d:
                    out_p = d / f"ADMIN_DRAFT_{ent}.md"
                    out_p.write_text(json.dumps(rc_d, indent=2) if isinstance(rc_d, dict) else str(rc_d))
                    drafts.append(out_p.name)
            except Exception as e:
                print(f"[autopilot] admin[{c['obligor']}/{ent}] FAILED: {e}")
        done.append({"obligor": c["obligor"], "drafts": drafts})
        print(f"[autopilot] admin[{c['obligor']}]: drafts={drafts}")
    _mark(st, "admin", ok=True, results=done)

def stage_accountability(st: dict, cands: list[dict]) -> None:
    for c in cands:
        d = _cand_dir(c["obligor"])
        if not (d / "EXPOSURE_MAP.md").exists():
            continue
        slug = d.name
        rc, out = _run([sys.executable, "-u", "-m", "pipeline.accountability_map",
                        f"claim_candidates/{slug}"], timeout=3000)
        print(f"[autopilot] accountability[{c['obligor']}]: rc={rc}")
    _mark(st, "accountability", ok=True)

def stage_report(st: dict, cands: list[dict]) -> None:
    lines = [f"# NEW CLAIMS REPORT — autopilot {_now()}", ""]
    lines += ["The chain ran: intake -> mail -> scan -> exposure -> admin -> accountability.",
              "Everything below is PREPARED, nothing filed. Filing is yours.", ""]
    for c in cands:
        d = _cand_dir(c["obligor"])
        lines += [f"## {c['obligor']} — {c['hits']} duty-breach hits", ""]
        for p in sorted(d.glob("*.md")):
            lines.append(f"- [[{p.relative_to(VAULT)}]]")
        lines.append("")
    watch = [g for g in _mature_groups()[CAND_CAP:]][:10]
    if watch:
        lines += ["## Watch list (below candidacy cap this run)", ""]
        lines += [f"- {g['obligor']}: {g['hits']} hits" for g in watch]
    REPORT.write_text("\n".join(lines))
    _mark(st, "report", ok=True, path=str(REPORT))
    print(f"[autopilot] report -> {REPORT}")

def main() -> int:
    st = _state()
    stages = os.environ.get("AUTOPILOT_STAGES", "intake,mail,scan,candidates,exposure,admin,accountability,report").split(",")
    print(f"[autopilot] run {_now()} — stages: {stages}")
    cands: list[dict] = []
    if "intake" in stages:
        stage_intake(st)
    if "mail" in stages:
        stage_mail(st)
    if "scan" in stages:
        stage_scan(st)
    if "candidates" in stages:
        cands = stage_candidates(st)
    if "exposure" in stages and cands:
        stage_exposure(st, cands)
    if "admin" in stages and cands:
        stage_admin(st, cands)
    if "accountability" in stages and cands:
        stage_accountability(st, cands)
    if "report" in stages:
        stage_report(st, cands)
    print("[autopilot] done")
    return 0

if __name__ == "__main__":
    sys.exit(main())
