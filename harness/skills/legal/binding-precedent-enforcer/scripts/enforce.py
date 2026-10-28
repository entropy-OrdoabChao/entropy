#!/usr/bin/env python3
"""
binding-precedent-enforcer
Retrieves and locks binding precedent for a legal element from the controlling
jurisdiction defined in ~/.hermes/legal_config.json.
"""
import argparse
import json
import os
import sys
from pathlib import Path

CONFIG_PATH = Path.home() / ".hermes" / "legal_config.json"
CHROMA_SEARCH = Path.home() / ".hermes/skills/legal/chromadb-search/scripts/search_chromadb.sh"
COURTLISTENER = Path.home() / ".hermes/skills/legal/courtlistener-search/scripts/courtlistener.sh"


def load_config():
    if not CONFIG_PATH.exists():
        sys.exit(f"ERROR: missing {CONFIG_PATH}")
    return json.loads(CONFIG_PATH.read_text())


def binding_levels(cfg, jurisdiction):
    chain = cfg.get("binding_chain", {}).get(jurisdiction)
    if not chain:
        sys.exit(f"ERROR: no binding chain for jurisdiction '{jurisdiction}'")
    return chain


def search_db(query, level, n=5):
    """Search ChromaDB filtered by authority level."""
    import subprocess
    cmd = [str(CHROMA_SEARCH), f"{query} {level}", str(n)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return out.stdout
    except Exception as e:
        return f"DB_ERROR: {e}"


def search_courtlistener(query, court=None):
    import subprocess
    cmd = [str(COURTLISTENER), "search", query]
    if court:
        cmd += ["--court", court]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return out.stdout
    except Exception as e:
        return f"CL_ERROR: {e}"


def enforce_element(element, jurisdiction, cfg):
    chain = binding_levels(cfg, jurisdiction)
    result = {
        "element": element,
        "jurisdiction": jurisdiction,
        "binding_chain": [],
        "rule_synthesized": None,
        "duty_on_court": None,
        "if_ignored": None,
        "preservation_note": None,
        "search_trail": [],
    }

    for level in chain:
        db_hits = search_db(element, level, n=5)
        result["search_trail"].append({"level": level, "source": "db", "raw": db_hits[:2000]})

        # Federal layers also try CourtListener
        if level in ("scotus", "ca3", "us_code"):
            cl_hits = search_courtlistener(
                element,
                court="scotus" if level == "scotus" else ("ca3" if level == "ca3" else None),
            )
            result["search_trail"].append({"level": level, "source": "courtlistener", "raw": cl_hits[:2000]})

    # Stub: structured extraction left for downstream LLM call (Hermes itself)
    # The agent reads search_trail and fills binding_chain[] with verbatim holdings
    if not result["binding_chain"]:
        result["binding_chain"] = "AGENT_MUST_EXTRACT_FROM_search_trail"

    return result


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--element", help="Single element/issue to enforce")
    p.add_argument("--elements-file", help="JSON file with array of elements")
    p.add_argument("--jurisdiction", required=True, help="e.g., edpa, pa-state, ca3")
    p.add_argument("--output", default="/tmp/precedent_wall.json")
    args = p.parse_args()

    cfg = load_config()

    if args.elements_file:
        elements = json.loads(Path(args.elements_file).read_text())
    elif args.element:
        elements = [args.element]
    else:
        sys.exit("ERROR: provide --element or --elements-file")

    walls = [enforce_element(e, args.jurisdiction, cfg) for e in elements]

    Path(args.output).write_text(json.dumps(walls, indent=2))
    print(f"WROTE: {args.output}")
    print(f"ELEMENTS PROCESSED: {len(walls)}")


if __name__ == "__main__":
    main()
