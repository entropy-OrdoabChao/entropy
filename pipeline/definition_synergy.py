#!/usr/bin/env python3
"""
pipeline/definition_synergy.py

Black's Law × foundational_terms × LLM analysis × filing-aware quoting.

Workflow:
    1. extract_filing_terms(text) — scan filing for every foundational term
       (canonical + aliases) defined in obsidian_sync/foundations/foundational_terms.yaml
    2. blacks_lookup(term) — pull Black's Law Dictionary entry from Docker
       corpus (source filter = "blacks_law_8th")
    3. llm_analyze_filing(text, candidates) — ask llama-server which terms
       most benefit from an authoritative dictionary citation in this filing
    4. format_blacks_quote(term, defn) — produce a citation-quality quote
       block, ready to paste under a section header
    5. synergy_enhance(text) — runs the full pipeline and returns
       {candidates, definitions, llm_rankings, formatted_quotes}

The drafter calls synergy_enhance() per-section to add definitional
support inline; the IF-THEN engine can use the same lookup to surface
authoritative definitions in callouts.

ENV:
    BEEZLE_CORPUS_URL  default http://localhost:8100
    BEEZLE_LLM_URL     default http://localhost:8000
    BEEZLE_BLACKS_SRC  default "blacks_law_8th"   # source LIKE filter
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Any

_ROOT = _eroot()
_CORPUS_URL = os.environ.get("BEEZLE_CORPUS_URL", "http://localhost:8100")
_LLM_URL = os.environ.get("BEEZLE_LLM_URL", "http://localhost:8000")
_BLACKS_SRC = os.environ.get("BEEZLE_BLACKS_SRC", "blacks_law_8th")
_FOUNDATIONAL_YAML = _ROOT / "obsidian_sync" / "foundations" / "foundational_terms.yaml"
_DOCTRINE_INDEX = _ROOT / "obsidian_sync" / "foundations" / "doctrine_index.json"


# ════════════════════════════════════════════════════════════════════════
# Corpus lookup (via Docker HTTP service)
# ════════════════════════════════════════════════════════════════════════
def _http_get(path: str, params: Dict, timeout: float = 10.0):
    url = f"{_CORPUS_URL}/{path.lstrip('/')}?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status == 200:
                return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None
    return None


def blacks_lookup(term: str, top_k: int = 3) -> Optional[Dict]:
    """v6: doctrine_index priority. Cornell-fetched Wex entries are curated
    and beat noisy Black's chunks for the terms we have. Falls through to
    Black's Law Dictionary for terms not in doctrine_index. Returns None
    (NOT full-corpus garbage) when neither source has a good match."""
    # v6: doctrine_index priority — curated Cornell Wex first
    doc = doctrine_lookup(term)
    if doc:
        return doc
    # Black's Law fallback (covers ~33K dictionary chunks)
    query = (term or "").replace("_", " ").strip()
    if not query:
        return None
    params = {"query": query, "top_k": top_k, "phrase": "true"}
    if _BLACKS_SRC:
        params["source"] = _BLACKS_SRC
    data = _http_get("search_text", params)
    if data and data.get("count", 0) > 0:
        return data["hits"][0]
    # Phrase miss -> broaden to token-AND with source filter
    if _BLACKS_SRC:
        params2 = {"query": query, "top_k": top_k, "source": _BLACKS_SRC}
        data = _http_get("search_text", params2)
        if data and data.get("count", 0) > 0:
            return data["hits"][0]
    # No full-corpus fallback — skip term cleanly if nothing definitional
    return None


def doctrine_lookup(term: str) -> Optional[Dict]:
    data = _http_get("lookup_term", {"term": term, "max_hits": 1})
    if data and data.get("count", 0) > 0:
        return data["hits"][0]
    return None


# ════════════════════════════════════════════════════════════════════════
# Term extraction from filing text
# ════════════════════════════════════════════════════════════════════════
def _load_foundational_terms() -> Dict[str, Dict]:
    """Return {term_key: {canonical, aliases, category}}."""
    if not _FOUNDATIONAL_YAML.exists():
        return {}
    try:
        import yaml
    except ImportError:
        return {}
    with open(_FOUNDATIONAL_YAML, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    out = {}
    container = data.get("terms") if isinstance(data, dict) else None
    if not container:
        for key in ("foundational_terms", "entries", "items"):
            if isinstance(data, dict) and key in data:
                container = data[key]
                break
    if isinstance(container, dict):
        out = container
    elif isinstance(container, list):
        for item in container:
            if isinstance(item, dict):
                k = item.get("term") or item.get("name")
                if k:
                    out[k] = item
    return out


def extract_filing_terms(filing_text: str) -> List[Dict]:
    """Find every foundational term whose canonical or alias appears in the filing."""
    foundational = _load_foundational_terms()
    text_lower = filing_text.lower()
    found = []
    for term_key, term_data in foundational.items():
        if not isinstance(term_data, dict):
            continue
        canonical = term_data.get("canonical", term_key.replace("_", " "))
        aliases = term_data.get("aliases", []) or []
        candidates = [canonical] + list(aliases) + [term_key.replace("_", " ")]
        matched_via = None
        for cand in candidates:
            if not cand:
                continue
            # Word-boundary match (case-insensitive)
            pattern = r"\b" + re.escape(cand.lower()) + r"\b"
            if re.search(pattern, text_lower):
                matched_via = cand
                break
        if matched_via:
            found.append({
                "term": term_key,
                "canonical": canonical,
                "category": term_data.get("category"),
                "matched_via": matched_via,
            })
    return found


# ════════════════════════════════════════════════════════════════════════
# LLM analysis
# ════════════════════════════════════════════════════════════════════════
def _llm_chat(messages: List[Dict], temperature: float = 0.0,
              max_tokens: int = 2000, timeout: float = 120.0) -> Optional[str]:
    """Call llama-server's OpenAI-compatible /v1/chat/completions."""
    payload = {
        "model": "default",
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    try:
        req = urllib.request.Request(
            (f"{_LLM_URL}/chat/completions" if _LLM_URL.rstrip("/").endswith("/v1") else f"{_LLM_URL}/v1/chat/completions"),
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]
    except Exception:
        return None


def llm_analyze_filing(filing_text: str, candidates: List[Dict],
                       top_k: int = 5) -> List[Dict]:
    """Ask the LLM to rank candidate terms by definitional importance."""
    if not candidates:
        return []

    excerpt = filing_text[:3500]
    cand_list = "\n".join(
        f"  - {c['term']} ({c.get('matched_via', c['term'])})"
        for c in candidates[:30]
    )

    system = (
        "You are a legal-writing assistant. You select which legal terms in a "
        "filing would benefit from authoritative dictionary citations (Black's "
        "Law Dictionary). Output ONLY valid JSON, no preamble, no markdown."
    )
    user = f"""FILING EXCERPT:
{excerpt}

CANDIDATE TERMS (found in this filing's foundational vocabulary):
{cand_list}

For the top {top_k} terms most useful to define authoritatively in this filing,
return a JSON object exactly matching:

{{
  "rankings": [
    {{"term": "<term_key>", "score": <0-10>, "reason": "<one sentence>"}}
  ]
}}

Score 8-10: central technical term; opposing counsel may dispute meaning.
Score 5-7:  helpful for the court but not load-bearing.
Score 0-4:  common term; defining would clutter the brief.

Return ONLY the JSON object. No prose before or after."""

    response = _llm_chat([
        {"role": "system", "content": system},
        {"role": "user",   "content": user},
    ])
    if not response:
        return []
    # Strip markdown fences if any
    cleaned = response.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```", 2)
        cleaned = cleaned[1] if len(cleaned) >= 2 else cleaned[0]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip("`\n ")
    try:
        parsed = json.loads(cleaned)
        return parsed.get("rankings", [])[:top_k]
    except Exception:
        # Try to grab the first {...} block
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group(0))
                return parsed.get("rankings", [])[:top_k]
            except Exception:
                pass
    return []


# ════════════════════════════════════════════════════════════════════════
# Quote rendering
# ════════════════════════════════════════════════════════════════════════
def _trim_definition(text: str, max_chars: int = 400) -> str:
    text = " ".join((text or "").split())
    if len(text) <= max_chars:
        return text
    trimmed = text[:max_chars]
    if "." in trimmed:
        trimmed = trimmed.rsplit(".", 1)[0] + "."
    return trimmed


def format_blacks_quote(term: str, defn: Dict, style: str = "footnote") -> str:
    """Render a Black's Law-style quote for inline insertion in a filing."""
    raw = defn.get("text", "") or defn.get("definition", "")
    body = _trim_definition(raw)
    src = (defn.get("metadata") or {}).get("source_doc") or "Black's Law Dictionary"
    if style == "inline":
        return f'{term} ("{body}") ({src})'
    if style == "block":
        return (f'> **{term}**: "{body}"\n'
                f'> ({src})')
    if style == "callout":
        return (f"### Operative definition: {term}\n\n"
                f'> "{body}"\n\n'
                f"*({src})*")
    # default: footnote
    return f'{term} is defined as: "{body}" ({src}).'


# ════════════════════════════════════════════════════════════════════════
# End-to-end synergy
# ════════════════════════════════════════════════════════════════════════
def synergy_enhance(filing_text: str, top_k: int = 5,
                    quote_style: str = "callout") -> Dict[str, Any]:
    """Extract candidate terms, look up Black's, LLM-rank, format top-K."""
    candidates = extract_filing_terms(filing_text)

    definitions = {}
    for c in candidates:
        term = c["term"]
        # Prefer Black's chunk; fall back to doctrine_index entry
        blacks = blacks_lookup(c.get("canonical") or term, top_k=1)
        if blacks:
            definitions[term] = blacks
        else:
            doc = doctrine_lookup(term)
            if doc:
                definitions[term] = doc

    rankings = llm_analyze_filing(filing_text, candidates, top_k=top_k)

    quotes = []
    if rankings:
        for r in rankings:
            term = r.get("term")
            defn = definitions.get(term)
            if defn:
                quotes.append({
                    "term": term,
                    "score": r.get("score"),
                    "reason": r.get("reason"),
                    "quote": format_blacks_quote(term, defn, style=quote_style),
                    "source": defn.get("source", "unknown"),
                    "chunk_id": defn.get("chunk_id"),
                })
    else:
        # LLM not reachable — emit top-K by category importance
        priority = {"jurisdictional": 10, "pleading_defense": 9,
                    "procedural": 8, "equitable": 7, "service": 6}
        ranked = sorted(
            candidates,
            key=lambda c: -priority.get(c.get("category") or "", 5),
        )[:top_k]
        for c in ranked:
            term = c["term"]
            defn = definitions.get(term)
            if defn:
                quotes.append({
                    "term": term,
                    "score": None,
                    "reason": "LLM unreachable; heuristic ranking by category",
                    "quote": format_blacks_quote(term, defn, style=quote_style),
                    "source": defn.get("source", "unknown"),
                    "chunk_id": defn.get("chunk_id"),
                })

    return {
        "candidates_found": len(candidates),
        "definitions_retrieved": len(definitions),
        "llm_used": bool(rankings),
        "rankings": rankings,
        "quotes": quotes,
    }


# ════════════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════════════
def _read_filing(path_str: str) -> str:
    p = Path(path_str)
    if not p.is_absolute():
        p = _ROOT / path_str
    if not p.exists():
        raise FileNotFoundError(p)
    return p.read_text(encoding="utf-8", errors="replace")


def _cmd_analyze(args):
    text = _read_filing(args.filing)
    candidates = extract_filing_terms(text)
    print()
    print(f"  found {len(candidates)} foundational terms in {args.filing}")
    print()
    for c in candidates[:50]:
        cat = c.get("category") or "—"
        print(f"    {c['term']:32s}  [{cat}]  matched as: '{c.get('matched_via')}'")
    if len(candidates) > 50:
        print(f"    ... and {len(candidates) - 50} more")
    return 0


def _cmd_quote(args):
    defn = blacks_lookup(args.term)
    if not defn:
        defn = doctrine_lookup(args.term)
    if not defn:
        print(f"  no definition for '{args.term}' in corpus or doctrine_index")
        return 1
    print()
    print(format_blacks_quote(args.term, defn, style=args.style))
    print()
    return 0


def _cmd_enhance(args):
    text = _read_filing(args.filing)
    result = synergy_enhance(text, top_k=args.top_k, quote_style=args.style)
    print()
    print(f"  candidates found       : {result['candidates_found']}")
    print(f"  definitions retrieved  : {result['definitions_retrieved']}")
    print(f"  LLM ranking used       : {result['llm_used']}")
    print(f"  formatted quotes       : {len(result['quotes'])}")
    print()
    for i, q in enumerate(result["quotes"], 1):
        print(f"  ── {i}. {q['term']}  (score={q.get('score')})")
        if q.get("reason"):
            print(f"     reason: {q['reason']}")
        print(f"     source: {q['source']}")
        print()
        for ln in q["quote"].split("\n"):
            print("     " + ln)
        print()

    if args.write:
        out = Path(args.write)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            f.write(f"# Definition Synergy Report\n\n")
            f.write(f"- Candidates found: {result['candidates_found']}\n")
            f.write(f"- Definitions retrieved: {result['definitions_retrieved']}\n")
            f.write(f"- LLM ranking used: {result['llm_used']}\n\n")
            f.write(f"## Recommended definitional callouts\n\n")
            for q in result["quotes"]:
                f.write(f"### {q['term']}\n\n")
                if q.get("reason"):
                    f.write(f"*Why*: {q['reason']}\n\n")
                f.write(q["quote"])
                f.write("\n\n---\n\n")
        print(f"  report -> {out}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="definition_synergy")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pa = sub.add_parser("analyze")
    pa.add_argument("filing")
    pa.set_defaults(func=_cmd_analyze)

    pq = sub.add_parser("quote")
    pq.add_argument("term")
    pq.add_argument("--style",
                    choices=["inline", "block", "callout", "footnote"],
                    default="callout")
    pq.set_defaults(func=_cmd_quote)

    pe = sub.add_parser("enhance")
    pe.add_argument("filing")
    pe.add_argument("--top-k", type=int, default=5)
    pe.add_argument("--style",
                    choices=["inline", "block", "callout", "footnote"],
                    default="callout")
    pe.add_argument("--write", help="output path for the report")
    pe.set_defaults(func=_cmd_enhance)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
