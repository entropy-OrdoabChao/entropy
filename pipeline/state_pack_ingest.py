#!/usr/bin/env python3
"""pipeline/state_pack_ingest.py — `entropy ingest state <ST>`: build a state's law library from its pack map.

The pack map (corpus_packs/states/<ST>.yaml) names, for each of the 14 slots Beezle was built on in Pennsylvania,
the state's equivalent: official source URL, citation form, and the corpus family the chunks live under. This command
walks those slots and pulls each one through the SAME official_source_puller the PA corpus was built with —
corpus-first check, official-publisher resolution, conservation ratio, operative-phrase probe — so a new state's
library is held to the same gate as the baseline. `--jurisdiction <ST>` tags every chunk, which is what the organs
filter on (the `state` key of the chunk store is the puller's jurisdiction column).

  python -m pipeline.state_pack_ingest NJ                 # plan + pull every slot that has a source
  python -m pipeline.state_pack_ingest NJ --plan          # show the plan, pull nothing
  python -m pipeline.state_pack_ingest NJ --slot civil_rules --slot statutes
  python -m pipeline.state_pack_ingest NJ --doctor        # pack check: files · library counts · probe cite per slot

Nothing is marked loaded by a download finishing: `--doctor` is what the desk's "verified · loaded" chip reads.
Writes <home>/corpus_packs/<ST>/manifest.json (per-slot result, counts, dates).
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from core.entropy_paths import entropy_root as _eroot, package_root as _epkg

PACKS = _epkg() / "corpus_packs" / "states"
HOME_PACKS = _eroot() / "corpus_packs"
CHUNK_DB = _eroot() / "corpus" / "chunk_text.db"

# the 14 slots + business registry; which ones are LAW TEXT (ingestable) vs ADDRESS/RECIPE (stored, not chunked)
LAW_SLOTS = ("constitution", "statutes", "admin_code", "register", "civil_rules", "appellate_rules",
             "consumer_statutes", "reporters", "filing_fees_ifp",
             # FORUM slots — the layer that bounces a filing before the merits (county/trial-court local rules, the
             # federal district's local rules, the judge's own procedures, mandatory forms/attachments). Law text,
             # ingested like the others; a state is "loaded" without them (floor = constitution+statutes+civil_rules)
             # but the forum gate (m1_filing_gate --court) reports NOT IN CORPUS for a court whose slot is empty.
             "local_rules_trial", "federal_district_rules", "judge_procedures", "forms_attachments")
FORUM_SLOTS = ("local_rules_trial", "federal_district_rules", "judge_procedures", "forms_attachments")
META_SLOTS = ("court_chain", "efiling", "financial_regulator", "judicial_conduct", "attorney_discipline", "business_registry")
KIND = {"constitution": "constitution", "statutes": "statute", "admin_code": "regulation", "register": "register",
        "civil_rules": "court_rule", "appellate_rules": "court_rule", "consumer_statutes": "statute",
        "reporters": "opinion_index", "filing_fees_ifp": "court_rule",
        "local_rules_trial": "court_rule", "federal_district_rules": "court_rule", "judge_procedures": "court_rule",
        "forms_attachments": "court_form"}


def load_pack(st: str) -> dict:
    """Per-state file first (corpus_packs/states/<ST>.yaml); the baseline map (state_equivalents.yaml → states.<ST>)
    for the states that live there (PA is the verified baseline and is only in the map)."""
    import yaml
    p = PACKS / f"{st}.yaml"
    if p.exists():
        d = yaml.safe_load(p.read_text(encoding="utf-8"))
        return d.get(st) or d
    eq = PACKS.parent / "state_equivalents.yaml"
    if eq.exists():
        d = yaml.safe_load(eq.read_text(encoding="utf-8")) or {}
        if st in (d.get("states") or {}):
            return d["states"][st]
    raise SystemExit(f"no pack map for {st}: {p} (nor in {eq.name})")


def plan(st: str, slots: list[str] | None = None) -> list[dict]:
    pk = load_pack(st)
    rows = []
    for slot in (slots or LAW_SLOTS + META_SLOTS):
        e = pk.get(slot) or {}
        src = e.get("source") or ""
        fam = (e.get("corpus_family") or f"{st.lower()}_{slot}")
        fam = fam.split("<")[0].rstrip("*").rstrip("_") or f"{st.lower()}_{slot}"   # templates → prefix
        rows.append({"slot": slot, "name": e.get("name", ""), "source": src, "cite": e.get("cite", ""), "family": fam,
                     "kind": KIND.get(slot, "meta"), "ingest": slot in LAW_SLOTS and bool(src),
                     "status": "mapped" if src else "unmapped", "check": e.get("check", ""),
                     # vendor-hosted OFFICIAL publisher (non-government host) vetted by a human in the pack → force path
                     "official_publisher": bool(e.get("official_publisher")), "pack_status": str(pk.get("status", ""))})
    return rows


def _family_count(fam: str, st: str = "") -> int:
    """Chunks under a slot's source family. The chunk store's state key is the `jurisdiction` column (what the
    puller's --jurisdiction writes); `source` carries the family prefix. Count by family, confirm by jurisdiction."""
    if not CHUNK_DB.exists():
        return 0
    con = sqlite3.connect(CHUNK_DB)
    try:
        q = "SELECT COUNT(*) FROM chunk_text WHERE source >= ? AND source < ?"
        n = con.execute(q, (fam, fam + "~")).fetchone()[0]
        if n == 0 and st:  # fall back: anything tagged with this state's jurisdiction under a looser family stem
            stem = fam.split("_")[0] + "_"
            n = con.execute(q + " AND lower(jurisdiction) IN (?, ?)", (stem, stem + "~", st.lower(), _STATE_NAMES.get(st, st).lower())).fetchone()[0]
        return n
    except sqlite3.Error:
        return 0
    finally:
        con.close()


_STATE_NAMES = {"PA": "pennsylvania", "NJ": "new jersey", "NY": "new york", "TX": "texas", "CA": "california", "FL": "florida"}


def pull_slot(st: str, row: dict, force: bool = False) -> dict:
    """One slot through official_source_puller.pull — the same gate as the PA baseline."""
    probe = re.sub(r"\s*\(.*?\)\s*$", "", row["cite"]) or row["name"][:40]
    cmd = [sys.executable, "-m", "pipeline.official_source_puller", "pull", "--source", row["family"], "--url", row["source"],
           "--kind", row["kind"], "--family", row["family"], "--jurisdiction", st, "--cite", row["cite"], "--phrase", probe]
    if force or row.get("official_publisher"):
        why = f"state pack {st} slot {row['slot']}" + (f" — pack marks official_publisher (pack status: {row.get('pack_status')})"
                                                       if row.get("official_publisher") else "")
        cmd += ["--force", "--reason", why, "--checked-against", row["family"]]
    t0 = time.time()
    r = subprocess.run(cmd, cwd=_epkg(), capture_output=True, text=True, timeout=1800)
    return {"slot": row["slot"], "family": row["family"], "exit": r.returncode, "seconds": round(time.time() - t0, 1),
            "chunks": _family_count(row["family"], st), "tail": (r.stdout + r.stderr)[-400:]}


def doctor(st: str) -> dict:
    """Pack check: (1) manifest files present (2) library holds the slot (3) a probe cite resolves. Never trusts the download."""
    man_p = HOME_PACKS / st / "manifest.json"
    man = json.loads(man_p.read_text()) if man_p.exists() else {}
    out = {"state": st, "checked": time.strftime("%Y-%m-%d %H:%M"), "slots": [], "loaded": False}
    try:
        from pipeline import rules_index  # probe resolver, if present in this build
    except Exception:
        rules_index = None
    for row in plan(st):
        n = _family_count(row["family"], st) if row["ingest"] else None
        probe_ok = None
        if row["ingest"] and rules_index is not None and row["cite"]:
            try:
                probe_ok = bool(rules_index.lookup(re.sub(r"\s*\(.*?\)\s*$", "", row["cite"])))
            except Exception:
                probe_ok = False
        in_manifest = row["slot"] in (man.get("slots") or {})
        if not row["ingest"]:
            ok = row["status"] == "mapped"          # address/recipe slots: mapped is all that can be checked here
        else:
            ok = bool(n) and probe_ok is not False  # law slots: chunks present AND the probe cite (if resolvable) resolves
        out["slots"].append({"slot": row["slot"], "status": row["status"], "chunks": n, "probe": probe_ok, "manifest": in_manifest,
                             "ok": ok, "cite": row["cite"]})
    law = [s for s in out["slots"] if s["chunks"] is not None]
    core = ("constitution", "statutes", "civil_rules")      # the floor: no pleading can be checked without these three
    out["incomplete"] = [s["slot"] for s in out["slots"] if not s["ok"]]
    out["loaded"] = bool(law) and all(s["ok"] for s in out["slots"] if s["slot"] in core)
    out["note"] = ("loaded = constitution + statutes + civil rules present and probe-resolvable; other slots listed under "
                   "incomplete still need pulls. A corpus built before pack maps (e.g. the PA baseline) may hold the law "
                   "under families the map does not name — see --doctor chunks per slot, not just loaded.")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="entropy ingest state <ST>")
    ap.add_argument("state")
    ap.add_argument("--plan", action="store_true"); ap.add_argument("--doctor", action="store_true")
    ap.add_argument("--slot", action="append"); ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    st = a.state.upper()
    if a.doctor:
        d = doctor(st); print(json.dumps(d, indent=1))
        print(("LOADED · verified" if d["loaded"] else f"INCOMPLETE: {', '.join(d['incomplete']) or 'nothing ingested'}"), file=sys.stderr)
        return 0 if d["loaded"] else 2
    rows = plan(st, a.slot)
    print(f"{st} pack plan — {sum(r['ingest'] for r in rows)} law slots to ingest, {sum(1 for r in rows if r['status']=='unmapped')} unmapped")
    for r in rows:
        print(f"  {r['slot']:20} {r['status']:8} {'INGEST' if r['ingest'] else 'store ':6} {r['family']:28} {r['source'][:60]}")
    if a.plan:
        return 0
    results = []
    for r in rows:
        if not r["ingest"]:
            continue
        print(f"\n→ {r['slot']} ← {r['source']}", flush=True)
        res = pull_slot(st, r, a.force); results.append(res)
        print(f"   exit {res['exit']} · {res['chunks']} chunks under {res['family']} · {res['seconds']}s")
        if res["exit"] != 0:
            print("   " + res["tail"].strip().splitlines()[-1][:200] if res["tail"].strip() else "   (no output)")
    HOME_PACKS.joinpath(st).mkdir(parents=True, exist_ok=True)
    man = {"state": st, "built": time.strftime("%Y-%m-%d %H:%M"), "jurisdiction_key": st,
           "slots": {r["slot"]: {**{k: r[k] for k in ("name", "source", "cite", "family", "kind", "status")},
                                 **({x["slot"]: x for x in results}.get(r["slot"], {}))} for r in rows}}
    (HOME_PACKS / st / "manifest.json").write_text(json.dumps(man, indent=1), encoding="utf-8")
    print(f"\nmanifest → {HOME_PACKS / st / 'manifest.json'}   (run --doctor to verify before trusting it)")
    return 0 if all(x["exit"] == 0 for x in results) else 1


if __name__ == "__main__":
    sys.exit(main())
