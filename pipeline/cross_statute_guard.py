#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pipeline/cross_statute_guard.py — the wire the Obsidian link-graph exists FOR (2026-07-09).

THE POINT (user): a definition from ONE statute must not be silently used in ANOTHER unless that other
statute cross-references it. The deep-web boundary-map (each record carries the term's DEFINITION + scope
means/includes + WHERE IT GOVERNS) is the graph that lets us DETECT this. This module reads that map and:
  - check_text(text): flags where the text ties a term to statute B but the term's CONTROLLING definition
    lives in statute A (closed 'means' scope) and B does not cross-reference A -> a cross-statute-import
    defect (the opponent's, or our own).
  - grounding_block(text): feeds m0's PROMPT the statute-specific controlling definitions + a do-not-import
    instruction, so m0 argues the RIGHT definition for THIS filing's statute (systematic, every draft).

Source of the map = the deep-web boundary records (output/deep_web_results.jsonl, source=fable-deep-web) —
which mirror the Obsidian connected-notes graph. Deterministic; no LLM; never raises.
"""
from __future__ import annotations
import json, re
from pathlib import Path
from typing import Dict, List

REPO = Path(__file__).resolve().parents[1]
RESULTS = REPO / "output" / "deep_web_results.jsonl"

# a citation -> its statute "home" (title/system), for comparing where a term governs vs where it's used
_CITE = re.compile(r"(\d+)\s*U\.?\s?S\.?\s?C\.?|Title\s+(\d+)|(\d+)\s*C\.?\s?F\.?\s?R\.?|"
                   r"(\d+)\s*Pa\.?\s?C\.?\s?S\.?|(UCC)\s*\d|Pa\.?R\.?[CAE]\.?P?\.?|Fed\.?\s?R\.?", re.I)


def _statute_home(text: str) -> List[str]:
    """the distinct statute 'homes' named in a text fragment (e.g. '26 usc', 'ucc', '31 cfr')."""
    homes = []
    for m in re.finditer(r"(\d+)\s*U\.?\s?S\.?\s?C\.?|(\d+)\s*C\.?\s?F\.?\s?R\.?|(\d+)\s*Pa\.?\s?C\.?\s?S\.?|"
                         r"\bUCC\b|\bTitle\s+(\d+)\b", text, re.I):
        g = next((x for x in m.groups() if x), None)
        if g:
            homes.append(f"t{g}")
        elif m.group(0).upper().startswith("UCC"):
            homes.append("ucc")
    return list(dict.fromkeys(homes))


_MAP: Dict[str, dict] = {}


def _load_map() -> Dict[str, dict]:
    """term(lower) -> {term, definition, scope(means/includes/other), governs(list of homes), cite}."""
    global _MAP
    if _MAP:
        return _MAP
    if not RESULTS.exists():
        return {}
    for ln in RESULTS.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            rec = json.loads(ln)["record"]
        except Exception:
            continue
        term = None
        mterm = re.search(r"TERM/RULE:\s*([^\n]+)", rec)
        if mterm:
            term = mterm.group(1).strip().lower()
        if not term:
            continue
        mdef = re.search(r"DEFINITION[^:]*:\s*([^\n]+)", rec, re.I)
        mwhere = re.search(r"WHERE(?:\s+IT)?\s+GOVERNS[^:]*:\s*([^\n]+)", rec, re.I)
        defn = (mdef.group(1).strip() if mdef else "")
        where = (mwhere.group(1).strip() if mwhere else "")
        # C5: 'includes' is expansive ONLY with an expansion clause (7701(c)-style 'not deemed to exclude' /
        # 'including but not limited to'); a BARE 'includes' is CLOSED (enumerated, argued under the canons).
        _dl = defn.lower()
        _expansive = bool(re.search(r"not\b.{0,25}\bdeemed\s+to\s+exclude|including\s+but\s+not\s+limited\s+to", _dl))
        scope = ("means" if re.search(r"\bMEANS\b", defn) and "includes" not in _dl
                 else "includes-only" if "includes only" in _dl
                 else "includes-expansive" if (_expansive and re.search(r"\binclud", _dl))
                 else "includes-closed" if re.search(r"\bincludes\b", _dl)
                 else "other")
        governs = _statute_home(where + " " + defn)
        cite = ""
        mc = re.search(r"\(([^)]*(?:U\.?S\.?C|C\.?F\.?R|Pa\.?C\.?S|UCC|Black)[^)]*)\)", defn)
        if mc:
            cite = mc.group(1)[:60]
        prev = _MAP.get(term)
        # prefer the record with a real governs + closed scope (more decisive)
        if prev is None or (not prev.get("governs") and governs) or (prev.get("scope") == "other" and scope != "other"):
            _MAP[term] = {"term": term, "definition": defn[:280], "scope": scope,
                          "governs": governs, "cite": cite}
    return _MAP


def check_text(text: str, max_flags: int = 12) -> List[dict]:
    """Flag cross-statute-import risks: a term whose controlling definition is closed ('means') and homed in
    statute A, used in a sentence that cites statute B (A != B), with no cross-reference. Corpus-grounded map;
    the drafter/analysis decides. Returns [{term, governs, used_in, scope, note, cite}]."""
    m = _load_map()
    if not m or not text:
        return []
    flags = []
    for sent in re.split(r"(?<=[.;])\s+", text):
        used = _statute_home(sent)
        if not used:
            continue
        low = sent.lower()
        for term, info in m.items():
            if len(term) < 4 or term not in low:
                continue
            gov = info.get("governs") or []
            if not gov:
                continue
            # a term homed only in A, used in a sentence citing B (and not A), closed scope -> import risk
            foreign = [u for u in used if u not in gov]
            if foreign and info.get("scope") in ("means", "includes-only", "includes-closed") and not any(g in used for g in gov):   # C5: bare includes = closed
                flags.append({
                    "term": info["term"], "scope": info["scope"], "governs": gov,
                    "used_with": foreign, "cite": info.get("cite", ""),
                    "note": (f"'{info['term']}' has a CLOSED ({info['scope']}) definition homed in "
                             f"{','.join(gov)}, but here it is used with {','.join(foreign)}. Unless "
                             f"{','.join(foreign)} cross-references {','.join(gov)}, importing this "
                             f"definition is UNAUTHORIZED — apply {','.join(foreign)}'s own definition or none.")})
                if len(flags) >= max_flags:
                    return flags
    return flags


def grounding_block(text: str, max_terms: int = 8) -> str:
    """m0-INPUT: the statute-specific controlling definitions for the operative terms in this filing, with a
    do-NOT-import-across-statutes instruction — so m0 argues the RIGHT definition for THIS statute every
    draft (the systematic 1-U.S.C.-1-style reasoning, graph-driven). '' if nothing applies; never raises."""
    try:
        m = _load_map()
        if not m or not text:
            return ""
        low = text.lower()
        picked = []
        for term, info in m.items():
            if len(term) >= 4 and term in low and info.get("definition") and info.get("scope") != "other":
                picked.append(info)
            if len(picked) >= max_terms:
                break
        if not picked:
            return ""
        head = ("## CONTROLLING DEFINITIONS — statute-specific (do NOT import a definition across statutes "
                "unless the other statute cross-references it; else apply the operative statute's OWN "
                "definition or argue the canons — a closed 'means' definition does not travel)\n")
        lines = []
        for info in picked:
            gov = ",".join(info.get("governs") or []) or "its own title"
            lines.append(f"- **{info['term']}** [{info['scope']}] governs in {gov}: {info['definition']}"
                         + (f" ({info['cite']})" if info.get("cite") else ""))
        return head + "\n".join(lines) + "\n"
    except Exception:
        return ""


if __name__ == "__main__":
    import sys
    txt = (Path(sys.argv[1]).read_text(encoding="utf-8", errors="ignore") if len(sys.argv) > 1
           else "Under 26 U.S.C. the term person includes the taxpayer; the note is a security under 15 U.S.C. 77b.")
    print(f"map terms: {len(_load_map())}")
    print("=== FLAGS ===")
    for f in check_text(txt):
        print(" -", f["note"])
    print("=== GROUNDING BLOCK ===")
    print(grounding_block(txt)[:900])
