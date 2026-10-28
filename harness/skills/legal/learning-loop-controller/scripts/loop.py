#!/usr/bin/env python3
"""
learning-loop-controller
Self-learning loop: detect gaps → auto-fetch → stage → approve → ingest → re-validate.
Supports dynamic jurisdiction discovery and Full Faith and Credit cross-jurisdictional retrieval.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

CONFIG_PATH = Path.home() / ".hermes" / "legal_config.json"
DATA_ROOT = Path.home() / ".hermes" / "data" / "learning"
STAGED = DATA_ROOT / "staged"
INGESTED = DATA_ROOT / "ingested"
REJECTED = DATA_ROOT / "rejected"

LAW_FETCHER = Path.home() / ".hermes/skills/legal/law-lookup/scripts/law_fetcher.sh"
COURTLISTENER = Path.home() / ".hermes/skills/legal/courtlistener-search/scripts/courtlistener.sh"
CHROMA_SEARCH = Path.home() / ".hermes/skills/legal/chromadb-search/scripts/search_chromadb.sh"
DB_INGEST_TEXT = Path.home() / ".hermes/skills/legal/db-ingest/scripts/ingest_text.py"

for d in (DATA_ROOT, STAGED, INGESTED, REJECTED):
    d.mkdir(parents=True, exist_ok=True)


def load_config():
    if not CONFIG_PATH.exists():
        return {"jurisdictions": {}, "binding_chain": {}, "default_jurisdictions": {}}
    return json.loads(CONFIG_PATH.read_text())


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def hash_content(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8", errors="ignore")).hexdigest()[:16]


def run(cmd, timeout=120):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        return type("R", (), {"returncode": 1, "stdout": "", "stderr": str(e)})()


# ─────────────────────────── Phase A: Detect ───────────────────────────

def detect_gaps(task_context_path, jurisdiction):
    """Read task context (element results, precedent walls, definition results) → emit gaps."""
    gaps = []
    if not Path(task_context_path).exists():
        print(f"WARN: no task context at {task_context_path}")
        return gaps

    ctx = json.loads(Path(task_context_path).read_text())

    # Element-level gaps from legal-analysis
    for el in ctx.get("elements", []):
        if el.get("result") in ("MISSING FACT", "MISSING AUTHORITY", "NOT ESTABLISHED"):
            gap = {
                "gap_id": hash_content(json.dumps(el, sort_keys=True)),
                "element": el.get("description", ""),
                "rule_cite": el.get("rule"),
                "jurisdiction": jurisdiction,
                "missing_authority_type": "binding_precedent" if el.get("result") == "NOT ESTABLISHED" else "statute_or_fact",
                "detected_at": datetime.utcnow().isoformat(),
                "search_attempted": [],
                "fetch_status": "pending",
                "fetched_sources": [],
                "approval_status": "pending",
            }
            gaps.append(gap)

    # Precedent wall gaps
    for wall in ctx.get("precedent_walls", []):
        if not wall.get("binding_chain") or wall.get("binding_chain") == "AGENT_MUST_EXTRACT_FROM_search_trail":
            gap = {
                "gap_id": hash_content(wall.get("element", "") + jurisdiction),
                "element": wall.get("element"),
                "jurisdiction": jurisdiction,
                "missing_authority_type": "binding_precedent",
                "detected_at": datetime.utcnow().isoformat(),
                "search_attempted": [],
                "fetch_status": "pending",
                "fetched_sources": [],
                "approval_status": "pending",
            }
            gaps.append(gap)

    # Definition gaps
    for d in ctx.get("definitions", []):
        if d.get("source") in ("NOT_FOUND", "NO_STATUTORY_DEFINITION"):
            gap = {
                "gap_id": hash_content("def:" + d.get("term", "")),
                "element": f"definition of '{d.get('term')}'",
                "jurisdiction": jurisdiction,
                "missing_authority_type": "definition",
                "detected_at": datetime.utcnow().isoformat(),
                "fetch_status": "pending",
                "fetched_sources": [],
                "approval_status": "pending",
            }
            gaps.append(gap)

    return gaps


# ─────────────────────────── Phase B: Fetch ───────────────────────────

def fetch_for_gap(gap, cfg, full_faith_credit=True):
    """Try every retrieval source for one gap. Stage results."""
    element = gap["element"]
    jurisdiction = gap["jurisdiction"]
    juris_cfg = cfg.get("jurisdictions", {}).get(jurisdiction, {})

    sources_tried = []
    fetched = []

    # 1. ChromaDB (verify it really isn't there with broader query)
    r = run([str(CHROMA_SEARCH), element, "10"])
    sources_tried.append("chromadb")
    if r.returncode == 0 and len(r.stdout.strip()) > 200:
        # If results came back this time, ChromaDB had it — note as found
        gap["fetch_status"] = "already_in_db"
        return gap

    # 2. CourtListener — binding court for this jurisdiction
    binding_courts = []
    if juris_cfg.get("type") == "federal_district":
        binding_courts = ["scotus", juris_cfg.get("circuit", ""), jurisdiction]
    elif juris_cfg.get("type") == "federal_circuit":
        binding_courts = ["scotus", jurisdiction]
    else:
        binding_courts = ["scotus", f"{jurisdiction}_supreme"]

    for court in binding_courts:
        if not court:
            continue
        r = run([str(COURTLISTENER), "search", element, "--court", court])
        sources_tried.append(f"courtlistener:{court}")
        if r.returncode == 0 and r.stdout.strip():
            stage_path = stage_content(r.stdout, {
                "gap_id": gap["gap_id"],
                "source": f"courtlistener:{court}",
                "element": element,
                "jurisdiction": jurisdiction,
                "binding_value": "binding" if court in ("scotus", juris_cfg.get("circuit")) else "persuasive",
            })
            fetched.append({"source": f"courtlistener:{court}", "staged": str(stage_path)})

    # 3. Statute fetcher
    fetcher = juris_cfg.get("fetcher")
    if fetcher == "palegis":
        # Try PA legislation
        r = run([str(LAW_FETCHER), "--pa-search", element])
        sources_tried.append("law_fetcher:palegis")
        if r.returncode == 0 and r.stdout.strip():
            stage_path = stage_content(r.stdout, {
                "gap_id": gap["gap_id"],
                "source": "law_fetcher:palegis",
                "element": element,
                "jurisdiction": jurisdiction,
            })
            fetched.append({"source": "law_fetcher:palegis", "staged": str(stage_path)})

    # 4. Federal statute search
    r = run([str(LAW_FETCHER), "--usc-search", element])
    sources_tried.append("law_fetcher:usc")
    if r.returncode == 0 and r.stdout.strip():
        stage_path = stage_content(r.stdout, {
            "gap_id": gap["gap_id"],
            "source": "law_fetcher:usc",
            "element": element,
            "jurisdiction": jurisdiction,
        })
        fetched.append({"source": "law_fetcher:usc", "staged": str(stage_path)})

    # 5. Full Faith and Credit — sister-state retrieval
    if full_faith_credit and not fetched:
        r = run([str(COURTLISTENER), "search", element])  # all courts
        sources_tried.append("courtlistener:full_faith_credit")
        if r.returncode == 0 and r.stdout.strip():
            stage_path = stage_content(r.stdout, {
                "gap_id": gap["gap_id"],
                "source": "courtlistener:full_faith_credit",
                "element": element,
                "jurisdiction": jurisdiction,
                "binding_value": "persuasive",
                "ffc_basis": "U.S. Const. art. IV § 1; 28 U.S.C. § 1738",
            })
            fetched.append({"source": "courtlistener:full_faith_credit", "staged": str(stage_path)})

    gap["search_attempted"] = sources_tried
    gap["fetched_sources"] = fetched
    gap["fetch_status"] = "staged" if fetched else "not_found"
    return gap


def stage_content(content, meta):
    h = hash_content(content)
    txt_path = STAGED / f"{h}.txt"
    meta_path = STAGED / f"{h}.meta.json"

    # Skip if already staged or already ingested
    if txt_path.exists() or (INGESTED / f"{h}.txt").exists():
        return txt_path

    txt_path.write_text(content)
    meta["hash"] = h
    meta["staged_at"] = datetime.utcnow().isoformat()
    meta["approval_status"] = "pending"
    meta_path.write_text(json.dumps(meta, indent=2))
    return txt_path


# ─────────────────────────── Phase C: Approve / Ingest ───────────────────────────

def list_staged():
    items = []
    for meta_file in STAGED.glob("*.meta.json"):
        items.append(json.loads(meta_file.read_text()))
    return items


def approve(hash_id):
    meta_path = STAGED / f"{hash_id}.meta.json"
    if not meta_path.exists():
        sys.exit(f"NOT FOUND: {hash_id}")
    meta = json.loads(meta_path.read_text())
    meta["approval_status"] = "approved"
    meta_path.write_text(json.dumps(meta, indent=2))
    print(f"APPROVED: {hash_id}")


def reject(hash_id):
    txt = STAGED / f"{hash_id}.txt"
    meta = STAGED / f"{hash_id}.meta.json"
    if txt.exists():
        txt.rename(REJECTED / txt.name)
    if meta.exists():
        meta.rename(REJECTED / meta.name)
    print(f"REJECTED: {hash_id}")


def ingest_staged():
    """Ingest only items with approval_status == approved."""
    venv_python = Path.home() / ".hermes/hermes-agent/venv/bin/python"
    ingested = 0
    for meta_file in STAGED.glob("*.meta.json"):
        meta = json.loads(meta_file.read_text())
        if meta.get("approval_status") != "approved":
            continue

        txt = STAGED / f"{meta['hash']}.txt"
        if not txt.exists():
            continue

        # Build metadata for ChromaDB
        chroma_meta = {
            "source": meta.get("source", "learning_loop"),
            "jurisdiction": meta.get("jurisdiction"),
            "authority_type": meta.get("binding_value", "unknown"),
            "ingested_at": datetime.utcnow().isoformat(),
            "gap_id": meta.get("gap_id"),
        }

        cmd = [
            str(venv_python), str(DB_INGEST_TEXT),
            "--file", str(txt),
            "--metadata", json.dumps(chroma_meta),
        ]
        r = run(cmd, timeout=300)
        if r.returncode == 0:
            txt.rename(INGESTED / txt.name)
            meta_file.rename(INGESTED / meta_file.name)
            ingested += 1
            print(f"INGESTED: {meta['hash']}")
        else:
            print(f"INGEST FAILED: {meta['hash']} — {r.stderr[:200]}")

    print(f"TOTAL INGESTED: {ingested}")


# ─────────────────────────── Phase D: Re-validate ───────────────────────────

def revalidate(chain_path):
    """Re-run searches for each chain link to see if newly ingested authority fills gaps."""
    if not Path(chain_path).exists():
        sys.exit(f"NO CHAIN: {chain_path}")

    chain = json.loads(Path(chain_path).read_text())
    updates = []

    for link in chain.get("links", []):
        if link.get("status") in ("NOT ESTABLISHED", "MISSING AUTHORITY"):
            element = link.get("element", "")
            r = run([str(CHROMA_SEARCH), element, "5"])
            if r.returncode == 0 and len(r.stdout.strip()) > 500:
                updates.append({
                    "element": element,
                    "previous_status": link["status"],
                    "new_status": "AUTHORITY_FOUND",
                    "search_excerpt": r.stdout[:1000],
                })
                link["status"] = "AUTHORITY_FOUND"
                link["needs_recheck"] = True

    out_path = chain_path.replace(".json", "_revalidated.json")
    Path(out_path).write_text(json.dumps({"chain": chain, "updates": updates}, indent=2))
    print(f"WROTE: {out_path}")
    print(f"UPDATES: {len(updates)}")


# ─────────────────────────── Jurisdiction discovery ───────────────────────────

def discover_jurisdiction(name, register=False):
    """
    Probe a new jurisdiction. If --register, append a stub entry to legal_config.json
    for the user to fill out (or auto-populate from CourtListener metadata).
    """
    cfg = load_config()
    if name in cfg.get("jurisdictions", {}):
        print(f"EXISTS: {name}")
        return

    # Probe CourtListener for the court
    r = run([str(COURTLISTENER), "court-info", name])
    info = {}
    if r.returncode == 0 and r.stdout.strip():
        try:
            info = json.loads(r.stdout)
        except Exception:
            info = {"raw": r.stdout[:500]}

    stub = {
        "name": info.get("full_name", name),
        "type": info.get("jurisdiction", "unknown"),
        "discovered_at": datetime.utcnow().isoformat(),
        "needs_completion": True,
        "probe_data": info,
    }

    if register:
        cfg.setdefault("jurisdictions", {})[name] = stub
        save_config(cfg)
        print(f"REGISTERED: {name}")
        print(f"  Edit {CONFIG_PATH} to complete the entry (binding_chain, fetcher, etc.)")
    else:
        print(json.dumps(stub, indent=2))


# ─────────────────────────── Main ───────────────────────────

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--task-context", help="Path to task context JSON (elements, walls, definitions)")
    p.add_argument("--gaps", help="Output path for detected gaps")
    p.add_argument("--jurisdiction", default="edpa")
    p.add_argument("--auto-fetch", action="store_true")
    p.add_argument("--no-full-faith-credit", action="store_true")
    p.add_argument("--stage-only", action="store_true", help="Stop after staging — do not auto-ingest")

    p.add_argument("--list-staged", action="store_true")
    p.add_argument("--approve", help="Hash ID to approve")
    p.add_argument("--approve-all", action="store_true")
    p.add_argument("--reject", help="Hash ID to reject")
    p.add_argument("--ingest-staged", action="store_true")

    p.add_argument("--revalidate", help="Path to logic chain to re-validate")

    p.add_argument("--discover-jurisdiction", help="Probe a new jurisdiction by code")
    p.add_argument("--register", action="store_true", help="Register discovered jurisdiction in config")

    args = p.parse_args()

    cfg = load_config()

    # Discovery
    if args.discover_jurisdiction:
        discover_jurisdiction(args.discover_jurisdiction, register=args.register)
        return

    # Staged management
    if args.list_staged:
        items = list_staged()
        print(json.dumps(items, indent=2))
        print(f"\nTOTAL STAGED: {len(items)}")
        return
    if args.approve:
        approve(args.approve)
        return
    if args.approve_all:
        for meta_file in STAGED.glob("*.meta.json"):
            meta = json.loads(meta_file.read_text())
            meta["approval_status"] = "approved"
            meta_file.write_text(json.dumps(meta, indent=2))
        print("APPROVED ALL STAGED")
        return
    if args.reject:
        reject(args.reject)
        return
    if args.ingest_staged:
        ingest_staged()
        return

    # Re-validation
    if args.revalidate:
        revalidate(args.revalidate)
        return

    # Detection + fetch
    if args.task_context:
        gaps = detect_gaps(args.task_context, args.jurisdiction)
        gaps_path = args.gaps or str(DATA_ROOT / f"gaps_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.jsonl")

        if args.auto_fetch:
            for i, g in enumerate(gaps):
                print(f"FETCHING gap {i+1}/{len(gaps)}: {g['element'][:80]}")
                fetch_for_gap(g, cfg, full_faith_credit=not args.no_full_faith_credit)

        with open(gaps_path, "w") as f:
            for g in gaps:
                f.write(json.dumps(g) + "\n")

        print(f"\nGAPS DETECTED: {len(gaps)}")
        print(f"GAPS LOG: {gaps_path}")
        staged_count = sum(1 for g in gaps if g.get("fetch_status") == "staged")
        print(f"STAGED: {staged_count}")
        if staged_count > 0 and not args.stage_only:
            print("\nReview staged items: --list-staged")
            print("Approve all:        --approve-all")
            print("Then ingest:        --ingest-staged")
        return

    p.print_help()


if __name__ == "__main__":
    main()
