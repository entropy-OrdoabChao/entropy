#!/usr/bin/env python3
"""
hermes_writeback.py — Hermes write-back to Obsidian + knowledge-graph query layer.

The bridge between Hermes (agent runtime) and Obsidian (durable knowledge store).
Hermes calls these functions to:
  - Persist learnings extracted from a session (record_learning)
  - Update per-case ledgers (defect / preservation / enforcement inventory)
  - Append new nodes to knowledge graphs as they're discovered
  - Query knowledge graphs to drive composition (route_defect, get_defect, etc.)

All writes are markdown or YAML — human-readable, git-friendly, Obsidian-indexable.

Functions:
  record_learning(case_id, learning_type, data) -> Path
  update_defect_inventory(case_id, defects: list[dict])
  update_preservation_ledger(case_id, issue: str, status: dict)
  update_enforcement_inventory(case_id, entry: dict)
  log_compliance_event(case_id, event: dict)

  append_to_kg(graph_name, node_id, node_data) -> dict
  query_kg(graph_name, **filters) -> list
  route_defect(defect_id, case_id=None) -> list   # defect → ordered bodies+vehicles
  get_defect(defect_id) -> dict
  get_complaint_vehicle(vehicle_id) -> dict
  get_body(body_id) -> dict

CLI:
  python3 -m pipeline.hermes_writeback list-defects [--category CAT]
  python3 -m pipeline.hermes_writeback route-defect --defect-id D_FCRA_NO_REINVEST
  python3 -m pipeline.hermes_writeback record --case-id ... --type ... --data-json '{...}'
"""
from __future__ import annotations
import argparse, json, sys
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    import yaml  # type: ignore
except ImportError:
    yaml = None

from core.entropy_paths import entropy_root as _entropy_root  # entropy: HOME, never a fixed path
_ROOT_WSL = _entropy_root()
_ROOT_WIN = _entropy_root()
ROOT = _ROOT_WSL if _ROOT_WSL.exists() else _ROOT_WIN

OBSIDIAN  = ROOT / "obsidian_sync"
KG_DIR    = OBSIDIAN / "knowledge_graphs"
LEARN_DIR = OBSIDIAN / "_hermes_learnings"
CASES_DIR = OBSIDIAN / "cases"

CASE_MAP = {
    "fed:2:26-cv-00123":   "fed_26-cv-00123",
    "fed:2:26-cv-00456":   "fed_26-cv-00456",
    "fed:2:26-cv-00789":   "fed_26-cv-00789",
    "phila_ccp:260100000": "phila_ccp_260100000",
}


def _slug(case_id: str) -> str:
    return CASE_MAP.get(case_id, case_id.replace(":", "_").replace("2_", ""))


def _require_yaml():
    if yaml is None:
        raise RuntimeError("pyyaml not installed. Run: pip install --break-system-packages pyyaml")


def _load_kg(name: str) -> dict:
    _require_yaml()
    p = KG_DIR / f"{name}.yaml"
    if not p.exists():
        return {}
    with open(p, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _save_kg(name: str, data: dict) -> Path:
    _require_yaml()
    p = KG_DIR / f"{name}.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True, default_flow_style=False)
    return p


# ============================================================================
# WRITE-BACK FUNCTIONS  (Hermes → Obsidian)
# ============================================================================

def record_learning(case_id: Optional[str], learning_type: str, data: dict) -> Path:
    """Append a learning to obsidian_sync/_hermes_learnings/<YYYYMMDD>_<type>.md.
    Used by Hermes to persist anything notable from a session — defect patterns
    discovered, authority chains validated, opposing-counsel moves observed."""
    LEARN_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now()
    fname = f"{ts.strftime('%Y%m%d')}_{learning_type}.md"
    p = LEARN_DIR / fname
    block = (
        f"\n## {ts.strftime('%Y-%m-%d %H:%M:%S')}"
        + (f" — {case_id}" if case_id else "") + "\n\n"
        f"**Type:** `{learning_type}`\n\n"
    )
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, (list, dict)):
                block += f"**{k}:**\n```yaml\n"
                if yaml is not None:
                    block += yaml.safe_dump(v, sort_keys=False, allow_unicode=True,
                                            default_flow_style=False)
                else:
                    block += json.dumps(v, indent=2, default=str)
                block += "```\n\n"
            else:
                block += f"- **{k}:** {v}\n"
    else:
        block += f"```\n{data}\n```\n"
    block += "\n---\n"

    if not p.exists():
        p.write_text(f"# Hermes learnings — {ts.strftime('%Y-%m-%d')} — {learning_type}\n", encoding="utf-8")
    with open(p, "a", encoding="utf-8") as f:
        f.write(block)
    return p


def _append_md_section(path: Path, header: str, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(f"# {path.stem.replace('_', ' ').title()}\n\n"
                        f"_Auto-maintained by Hermes. Append-only ledger._\n", encoding="utf-8")
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"\n## {ts} — {header}\n\n{content}\n")


# BEEZLE_FN_UNUSED 2026-06-13: update_defect_inventory (no static refs in pipeline/)
def update_defect_inventory(case_id: str, defects: list) -> Path:
    """Append defects to obsidian_sync/cases/<slug>/defect_inventory.md.
    Each defect: {defect_id, source_docket_entry, controlling_rule, asserted_count, notes}."""
    p = CASES_DIR / _slug(case_id) / "defect_inventory.md"
    lines = []
    for d in defects:
        did = d.get("defect_id", "?")
        src = d.get("source_docket_entry", "")
        rule = d.get("controlling_rule", "")
        cnt = d.get("asserted_count", 1)
        notes = d.get("notes", "")
        lines.append(f"- **`{did}`** — src: `{src}` · rule: `{rule}` · count: {cnt}"
                     + (f"\n  - {notes}" if notes else ""))
    _append_md_section(p, f"defects added (n={len(defects)})", "\n".join(lines))
    return p


# BEEZLE_FN_UNUSED 2026-06-13: update_preservation_ledger (no static refs in pipeline/)
def update_preservation_ledger(case_id: str, issue: str, status: dict) -> Path:
    """Append a preservation-status update to obsidian_sync/cases/<slug>/preservation_ledger.md.
    status: {raised_status, rule_status, appellate_status, record_count, cross_filing_count,
             deadline_iso?, authority_basis?}"""
    p = CASES_DIR / _slug(case_id) / "preservation_ledger.md"
    body = (
        f"**Issue:** `{issue}`\n\n"
        f"- raised:        `{status.get('raised_status', '?')}`\n"
        f"- rule_status:   `{status.get('rule_status', '?')}`\n"
        f"- appellate:     `{status.get('appellate_status', '?')}`\n"
        f"- record_count:  {status.get('record_count', 0)}\n"
        f"- cross_filing:  {status.get('cross_filing_count', 0)}\n"
    )
    if "deadline_iso" in status:
        body += f"- deadline:     `{status['deadline_iso']}`\n"
    if "authority_basis" in status:
        body += f"- authority:    `{status['authority_basis']}`\n"
    _append_md_section(p, f"preservation: {issue}", body)
    return p


# BEEZLE_FN_UNUSED 2026-06-13: update_enforcement_inventory (no static refs in pipeline/)
def update_enforcement_inventory(case_id: str, entry: dict) -> Path:
    """Append a parallel-track enforcement filing to obsidian_sync/cases/<slug>/enforcement_inventory.md.
    entry: {track, body_id, vehicle_id, defect_id, status, filed_iso?, response_iso?, notes?}"""
    p = CASES_DIR / _slug(case_id) / "enforcement_inventory.md"
    body = (
        f"- **track:**       `{entry.get('track', '?')}`\n"
        f"- **body:**        `{entry.get('body_id', '?')}`\n"
        f"- **vehicle:**     `{entry.get('vehicle_id', '?')}`\n"
        f"- **defect:**      `{entry.get('defect_id', '?')}`\n"
        f"- **status:**      `{entry.get('status', '?')}`\n"
    )
    for k in ("filed_iso", "response_iso", "notes"):
        if k in entry:
            body += f"- **{k}:** {entry[k]}\n"
    _append_md_section(p, f"enforcement: {entry.get('body_id', '?')} / {entry.get('vehicle_id', '?')}", body)
    return p


# BEEZLE_FN_UNUSED 2026-06-13: log_compliance_event (no static refs in pipeline/)
def log_compliance_event(case_id: str, event: dict) -> Path:
    """Append a compliance event (court ruled, opposing answered, etc.) for ΔCOMPLIANCE accounting.
    event: {event_type, motion_id?, ruling?, days_demanded_to_compliance?, notes?}"""
    p = CASES_DIR / _slug(case_id) / "compliance_events.md"
    body = (
        f"- **event_type:** `{event.get('event_type', '?')}`\n"
    )
    for k in ("motion_id", "ruling", "days_demanded_to_compliance", "notes"):
        if k in event:
            body += f"- **{k}:** {event[k]}\n"
    _append_md_section(p, f"compliance: {event.get('event_type', '?')}", body)
    return p


# ============================================================================
# KNOWLEDGE-GRAPH API  (read + append-only write)
# ============================================================================

# BEEZLE_FN_UNUSED 2026-06-13: append_to_kg (no static refs in pipeline/)
def append_to_kg(graph_name: str, node_id: str, node_data: dict) -> dict:
    """Append (or last-write-wins update) a node in a knowledge graph YAML.
    Returns {status, path, action}."""
    kg = _load_kg(graph_name)
    # The top-level list key varies by graph. Sniff it.
    list_keys = [k for k, v in kg.items() if isinstance(v, list)]
    if not list_keys:
        # Initialize a defaults list_key if KG is empty
        kg["entries"] = []
        list_key = "entries"
    else:
        list_key = list_keys[0]

    entries = kg[list_key]
    action = "appended"
    found = None
    for i, e in enumerate(entries):
        if isinstance(e, dict) and e.get("id") == node_id:
            found = i
            break
    payload = dict(node_data)
    payload.setdefault("id", node_id)
    payload["_hermes_updated"] = datetime.now().isoformat()

    if found is not None:
        # Last-write-wins merge: overlay payload over existing
        merged = {**entries[found], **payload}
        entries[found] = merged
        action = "updated"
    else:
        entries.append(payload)

    path = _save_kg(graph_name, kg)
    return {"status": "ok", "path": str(path), "action": action,
            "graph": graph_name, "node_id": node_id, "list_key": list_key}


def query_kg(graph_name: str, **filters) -> list:
    """Filter a knowledge-graph YAML by exact-match field constraints."""
    kg = _load_kg(graph_name)
    list_keys = [k for k, v in kg.items() if isinstance(v, list)]
    if not list_keys:
        return []
    entries = kg[list_keys[0]]
    out = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        if all(e.get(k) == v for k, v in filters.items()):
            out.append(e)
    return out


def get_defect(defect_id: str) -> Optional[dict]:
    rows = query_kg("defect_ontology", id=defect_id)
    return rows[0] if rows else None


def get_body(body_id: str) -> Optional[dict]:
    rows = query_kg("bodies", id=body_id)
    return rows[0] if rows else None


def get_complaint_vehicle(vehicle_id: str) -> Optional[dict]:
    rows = query_kg("complaint_vehicles", id=vehicle_id)
    return rows[0] if rows else None


def route_defect(defect_id: str, case_id: Optional[str] = None) -> dict:
    """For a defect_id, return ordered routing recommendations from enforcement_routes.yaml,
    enriched with body + vehicle metadata. This is the composer/case_reanalysis lookup."""
    routes_kg = _load_kg("enforcement_routes")
    routes_map = routes_kg.get("routes", {}) if isinstance(routes_kg, dict) else {}
    raw = routes_map.get(defect_id, [])
    if not raw:
        return {"defect_id": defect_id, "case_id": case_id, "routes": [],
                "note": "no routes defined; add to enforcement_routes.yaml"}

    enriched = []
    for r in raw:
        # Backward-compat: accept both body_id/vehicle_id (new) and body/vehicle (legacy).
        body_id = r.get("body_id") or r.get("body")
        vehicle_id = r.get("vehicle_id") or r.get("vehicle")
        track = r.get("track") or r.get("priority") or "?"
        priority = r.get("priority") or r.get("track")
        enriched.append({
            "track":      track,
            "body_id":    body_id,
            "vehicle_id": vehicle_id,
            "priority":   priority,
            "body":       get_body(body_id) if body_id else None,
            "vehicle":    get_complaint_vehicle(vehicle_id) if vehicle_id else None,
        })
    return {
        "defect_id": defect_id,
        "defect":    get_defect(defect_id),
        "case_id":   case_id,
        "n_routes":  len(enriched),
        "routes":    enriched,
    }


# ============================================================================
# CLI
# ============================================================================

def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list-defects")
    p_list.add_argument("--category", default=None)

    p_route = sub.add_parser("route-defect")
    p_route.add_argument("--defect-id", required=True)
    p_route.add_argument("--case-id", default=None)

    p_get = sub.add_parser("get")
    p_get.add_argument("--graph", required=True,
                       choices=["defect_ontology", "bodies", "complaint_vehicles",
                                "enforcement_routes", "latin_maxims",
                                "authority_hierarchy", "doctrine_stubs"])
    p_get.add_argument("--id", required=True)

    p_rec = sub.add_parser("record")
    p_rec.add_argument("--case-id", default=None)
    p_rec.add_argument("--type", required=True)
    p_rec.add_argument("--data-json", required=True)

    p_qkg = sub.add_parser("query")
    p_qkg.add_argument("--graph", required=True)
    p_qkg.add_argument("--filter", action="append", default=[],
                       help="key=value (repeatable)")

    p_stat = sub.add_parser("status",
                            help="Show counts for all knowledge graphs")

    args = ap.parse_args()

    if args.cmd == "list-defects":
        kg = _load_kg("defect_ontology")
        defects = kg.get("defects", []) if isinstance(kg, dict) else []
        if args.category:
            defects = [d for d in defects if d.get("category") == args.category]
        print(f"\n  defects: {len(defects)}")
        for d in defects:
            sev = d.get("severity", "?")
            print(f"    [{sev:<8}] {d.get('id', '?'):<35} {d.get('name', '')}")

    elif args.cmd == "route-defect":
        r = route_defect(args.defect_id, args.case_id)
        print(json.dumps(r, indent=2, default=str))

    elif args.cmd == "get":
        kg = _load_kg(args.graph)
        list_keys = [k for k, v in kg.items() if isinstance(v, list)]
        if not list_keys:
            print("empty graph"); return 1
        entries = kg[list_keys[0]]
        match = next((e for e in entries if isinstance(e, dict) and e.get("id") == args.id), None)
        if match is None:
            print(f"not found: {args.id}"); return 1
        print(json.dumps(match, indent=2, default=str))

    elif args.cmd == "record":
        data = json.loads(args.data_json)
        p = record_learning(args.case_id, args.type, data)
        print(f"wrote: {p}")

    elif args.cmd == "query":
        kw = {}
        for f in args.filter:
            if "=" not in f: continue
            k, v = f.split("=", 1)
            kw[k] = v
        rows = query_kg(args.graph, **kw)
        print(json.dumps(rows, indent=2, default=str))
        print(f"\n  matched: {len(rows)}", file=sys.stderr)

    elif args.cmd == "status":
        for name in ["defect_ontology", "bodies", "complaint_vehicles",
                     "enforcement_routes", "latin_maxims",
                     "authority_hierarchy", "doctrine_stubs"]:
            kg = _load_kg(name)
            if not kg:
                print(f"  {name:<25} (not installed)")
                continue
            list_keys = [k for k, v in kg.items() if isinstance(v, list)]
            n = sum(len(kg[k]) for k in list_keys)
            print(f"  {name:<25} {n:>4} entries  ({', '.join(list_keys)})")
        print(f"\n  KG dir:     {KG_DIR}")
        print(f"  learnings:  {LEARN_DIR}")
        print(f"  cases dir:  {CASES_DIR}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
