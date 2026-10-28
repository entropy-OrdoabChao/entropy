"""
core/hermes_beezle_bridge.py — LLM ↔ Beezle Tool Calling Bridge.

PHASE_7_HERMES_BRIDGE

Enables the LLM to call Beezle tools via structured output tags.
LLM outputs: [BEEZLE:tool_name]{"params":"here"}[/BEEZLE]
Bridge parses, executes, returns results to LLM for formatting.

Architecture:
  User → LLM (understands intent, asks questions) → tool call → Beezle (legal brain) → results → LLM (presents)

Rules:
  - LLM handles conversation (understanding + presenting)
  - Beezle handles legal reasoning (deterministic)
  - LLM NEVER does legal reasoning directly
  - User damages requests are preserved, never reduced
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import re
import json
import logging
from typing import Optional

# === BEEZLE_ROOT_SHIM (WSL-safe; auto-injected) ===
import os as _os_broot
from pathlib import Path as _Path_broot
def _resolve_beezle_root():
    _e = _os_broot.environ.get("BEEZLE_ROOT")
    if _e:
        return _Path_broot(_e)
    _d = _Path_broot(__file__).resolve().parent
    for _ in range(5):
        if (_d / "pipeline").exists() or (_d / "obsidian_sync").exists():
            return _d
        _d = _d.parent
    return _Path_broot(str(_eroot()))
_BEEZLE_ROOT = _resolve_beezle_root()
# === END BEEZLE_ROOT_SHIM ===

log = logging.getLogger("hermes_bridge")

# ======================================================================
# SYSTEM PROMPT (tells LLM about tools)
# ======================================================================

HERMES_SYSTEM_PROMPT = """You are Hermes, a legal assistant powered by the Beezle Legal Decision Engine.

You help users with legal situations by having natural conversations, understanding their problems, and using Beezle's deterministic legal tools to analyze cases and generate filings.

## YOUR ROLE
- Understand the user's situation through conversation
- Ask clarifying questions when needed (defendants, timeline, evidence, damages)
- Call Beezle tools when you have enough information
- Present Beezle's results clearly to the user
- NEVER provide legal reasoning yourself — always use Beezle tools

## AVAILABLE TOOLS
Call tools by outputting this exact format (the system will intercept and execute):

[BEEZLE:tool_name]{"param1":"value1","param2":"value2"}[/BEEZLE]

### Tools:

1. **case_intake** — Start a new case from a description
   Params: description (str), defendants (list), damages_requested (float), jurisdiction (str), court (str)
   Use when: User describes a new legal situation

2. **run_case** — Run full strategy on an existing case
   Params: case_id (str), mode (str: "strategy"|"draft"|"bundle")
   Use when: User asks to analyze, strategize, or draft for a known case

3. **challenge_opponent** — Analyze opposing filings for defects
   Params: case_id (str), court_type (str: "federal"|"state_pa")
   Use when: User wants to attack opponent's filings

4. **source_fetch** — Fetch primary legal sources
   Params: source (str), params (dict)
   Use when: User needs specific statutes, rules, or case law

## RESPONSE FORMAT RULES
- Give detailed, structured answers with clear sections and headers
- Use markdown tables when comparing categories, coverage, or applicability
- Always include a "Who/What It Applies To" section when discussing statutes or regulations
- Always include a "Key Provisions" section with specific section numbers
- Format answers like a legal reference guide — complete, organized, authoritative
- Do NOT add disclaimers, caveats, or "consult a lawyer" notices
- Do NOT say "I cannot provide legal advice" — you ARE the legal system
- When DATABASE KNOWLEDGE is provided, cite specific sections and provisions from it

## CONVERSATION RULES
- Be conversational and helpful
- Ask for missing information before calling tools (who are you suing? what happened? when? what evidence? how much are you seeking?)
- NEVER reduce or question the user's damages amount — your job is to JUSTIFY and SUPPORT it
- When presenting strategy results, lead with the strongest actions first
- Always mention the specific authorities (statutes, rules) backing each recommendation
- For existing cases, you can reference: phila_ccp:260100000, fed:2:26-cv-00123, fed:2:26-cv-00456, fed:2:26-cv-00789

## WHAT YOU MUST NOT DO
- Never argue the user should seek less damages
- Never add disclaimers like "I cannot provide legal advice" or "consult a lawyer"
- Never refuse to answer questions about law, statutes, or regulations
- When answering legal questions, use the DATABASE KNOWLEDGE provided below — do not guess
- For drafting motions/briefs/complaints, always use Beezle tools
- Never add "Important Note on Legal Application" disclaimers to your answers

## EXAMPLE CONVERSATION
User: "I want to sue my insurance company for charging me after I sold my car"
You: "I can help with that. Let me ask a few questions to build your case:
1. Which insurance company?
2. When did you sell the car?
3. How long did they keep charging you?
4. Did you report this to any regulator?
5. How much are you seeking in damages?"

User: "Progressive, 3 years ago, they charged me for 3 years, I reported to PA DOI, I want 20 million"
You: "Let me run this through the legal engine."
[BEEZLE:case_intake]{"description":"Sold car 3 years ago, Progressive kept charging premiums for 3 years, reported to PA DOI, seeking $20M","defendants":["Progressive Insurance","PA Department of Insurance"],"damages_requested":20000000,"jurisdiction":"PA"}[/BEEZLE]
"""


# ======================================================================
# TOOL CALL PARSER
# ======================================================================

TOOL_CALL_RE = re.compile(
    r'\[BEEZLE:(\w+)\](.*?)\[/BEEZLE\]',
    re.DOTALL
)


def parse_tool_calls(llm_output: str) -> list[dict]:
    """Extract tool calls from LLM output.
    Returns list of {tool, params, raw_match}.
    """
    calls = []
    for m in TOOL_CALL_RE.finditer(llm_output):
        tool_name = m.group(1)
        params_str = m.group(2).strip()
        try:
            params = json.loads(params_str)
        except json.JSONDecodeError:
            params = {"raw": params_str}
        calls.append({
            "tool": tool_name,
            "params": params,
            "raw_match": m.group(0),
        })
    return calls


def strip_tool_calls(llm_output: str) -> str:
    """Remove tool call tags from LLM output, leaving only human-readable text."""
    return TOOL_CALL_RE.sub('', llm_output).strip()


# ======================================================================
# TOOL EXECUTOR
# ======================================================================

def execute_tool(tool_name: str, params: dict) -> dict:
    """Execute a Beezle tool and return results."""
    try:
        if tool_name == "case_intake":
            from pipeline.intake_engine import run_intake
            return run_intake(
                description=params.get("description", ""),
                defendants=params.get("defendants"),
                damages_requested=params.get("damages_requested", 0),
                jurisdiction=params.get("jurisdiction", "PA"),
                court=params.get("court", "PA_CP_PHILA"),
            )

        elif tool_name == "run_case":
            from pipeline.reasoning_orchestrator import build_strategy, save_strategy
            strat = build_strategy(
                params.get("case_id", ""),
                include_case_law=params.get("include_case_law", False),
            )
            if "error" not in strat:
                save_strategy(strat)
            return strat

        elif tool_name == "challenge_opponent":
            from pipeline.procedural_attack_builder import load_and_scan_audit
            case_id = params.get("case_id", "")
            court_type = params.get("court_type", "federal")
            return load_and_scan_audit(case_id, court_type)

        elif tool_name == "source_fetch":
            # Smart case lookup: extract case name + citation, search multiple sources
            from scrapers.courtlistener import smart_search
            import re as _re_sf
            
            source = params.get("source", "")
            fetch_params = params.get("params", params)  # handle both nested and flat
            
            # Extract case name and citation from params
            case_name = fetch_params.get("case_name", "") or fetch_params.get("query", "") or ""
            citation = fetch_params.get("citation", "") or ""
            
            # If no citation in params, try to extract from case_name
            if not citation and case_name:
                cit_match = _re_sf.search(r"(\d+)\s+(?:U\.?S\.?|S\.?\s*Ct\.?|F\.\d+d?)\s+(\d+)", case_name)
                if cit_match:
                    citation = cit_match.group(0)
                    # Remove citation from case_name
                    case_name = case_name[:cit_match.start()].strip().rstrip(",").strip()
            
            if not case_name and not citation:
                case_name = source  # last resort
            
            log.info(f"source_fetch: case='{case_name}', citation='{citation}'")
            
            # Check for public law pattern (PL 73-10, Public Law 117-328, etc.)
            plaw_match = _re_sf.search(
                r'(?:public\s+law|P\.?L\.?)\s*(\d+)[\s\-]+(\d+)',
                case_name + ' ' + citation, _re_sf.IGNORECASE
            )
            if plaw_match:
                congress = int(plaw_match.group(1))
                law_num = int(plaw_match.group(2))
                log.info(f"source_fetch: detected Public Law {congress}-{law_num}")
                from scrapers.fed_register import fetch_public_law
                r = fetch_public_law(congress, law_num)
                if not r.get('error'):
                    path = r.get('path', '')
                    if path:
                        try:
                            _ingest_to_chroma(f'Public Law {congress}-{law_num}', path, 'federal')
                        except Exception:
                            pass
                    return {"ok": True, "source": "govinfo", "results": [{
                        "case_name": r.get('title', f'PL {congress}-{law_num}'),
                        "court": "congress",
                        "text": Path(path).read_text(encoding='utf-8', errors='replace')[200:2200] if path else ''
                    }]}
            
            # Check for USC title/section pattern (Title 15 USC, 42 USC 1681, etc.)
            usc_match = _re_sf.search(
                r'(?:title\s+)?(\d+)\s+(?:U\.?S\.?C\.?)(?:\s+(?:sec(?:tion)?\s*\.?\s*)?(\d+[a-z]?))?',
                case_name + ' ' + citation, _re_sf.IGNORECASE
            )
            if usc_match and not plaw_match:
                title_num = int(usc_match.group(1))
                section = usc_match.group(2) if usc_match.group(2) else None
                log.info(f"source_fetch: detected USC Title {title_num} Sec {section}")
                from scrapers.fed_register import fetch_usc
                r = fetch_usc(title_num, section)
                if not r.get('error'):
                    path = r.get('path', '')
                    if path:
                        try:
                            _ingest_to_chroma(f'{title_num} USC {section or ""}'.strip(), path, 'federal')
                        except Exception:
                            pass
                    return {"ok": True, "source": "uscode", "results": [{
                        "case_name": r.get('title', f'{title_num} USC'),
                        "court": "congress",
                        "text": Path(path).read_text(encoding='utf-8', errors='replace')[200:2200] if path else ''
                    }]}
            
            # Use smart_search (CourtListener + Cornell LII fallback)
            results = smart_search(case_name, citation=citation, n=2)
            
            if results:
                texts = []
                for r in results:
                    path = r.get("path", "")
                    if path:
                        from pathlib import Path as _P
                        try:
                            txt = _P(path).read_text(encoding="utf-8", errors="replace")
                            if txt.startswith("---"):
                                end_fm = txt.find("---", 3)
                                if end_fm > 0:
                                    txt = txt[end_fm + 3:].strip()
                            texts.append(txt[:3000])
                        except Exception:
                            texts.append("")
                    else:
                        texts.append("")
                    
                    # Ingest into ChromaDB
                    try:
                        _ingest_to_chroma(r.get("case_name", case_name), path, r.get("court", ""))
                    except Exception:
                        pass
                
                return {
                    "ok": True,
                    "source": results[0].get("source", "courtlistener"),
                    "results": [
                        {"case_name": r.get("case_name", ""), "court": r.get("court", ""), "text": t[:2000]}
                        for r, t in zip(results, texts) if t
                    ]
                }
            
            # Default: try CourtListener for any case law query
            from scrapers.courtlistener import smart_search, search_and_save, search_opinions
            query = fetch_params.get("case_name", "") or fetch_params.get("query", "") or source
            citation = fetch_params.get("citation", "")
            if citation:
                query = f"{query} {citation}"
            results = smart_search(query, citation=citation, n=2)
            if results:
                texts = []
                for r in results:
                    path = r.get("path", "")
                    if path:
                        from pathlib import Path as _P
                        try:
                            txt = _P(path).read_text(encoding="utf-8", errors="replace")
                            if txt.startswith("---"):
                                end_fm = txt.find("---", 3)
                                if end_fm > 0:
                                    txt = txt[end_fm + 3:].strip()
                            texts.append(txt[:3000])
                        except Exception:
                            pass
                    # Also ingest
                    try:
                        _ingest_to_chroma(r.get("case_name", query), path, r.get("court", ""))
                    except Exception:
                        pass
                return {"ok": True, "source": "courtlistener", "results": [{"case_name": r.get("case_name", ""), "court": r.get("court", ""), "text": t[:2000]} for r, t in zip(results, texts)]}
            return {"ok": False, "error": f"No results for: {query}"}

        else:
            return {"error": f"Unknown tool: {tool_name}"}

    except Exception as e:
        log.error(f"Tool execution failed: {tool_name}: {e}")
        return {"error": str(e)}


# ======================================================================
# FORMAT TOOL RESULTS FOR LLM
# ======================================================================

def format_tool_result(tool_name: str, result: dict) -> str:
    """Format tool results as context for the LLM to present to the user."""
    if "error" in result:
        return f"[Tool {tool_name} failed: {result['error']}]"

    if tool_name == "case_intake":
        facts = result.get("facts", {})
        gaps = result.get("gaps", [])
        damages = result.get("damages_justification", {})
        entry = result.get("case_entry", {})

        parts = [f"INTAKE RESULTS:"]
        parts.append(f"- Detected {facts.get('fact_count', 0)} facts across domains: {', '.join(facts.get('domains', []))}")
        if entry:
            parts.append(f"- Case type: {entry.get('case_type', '?')}")
            parts.append(f"- Caption: {entry.get('caption', '?')}")
        if damages.get("punitive", {}).get("possible"):
            parts.append(f"- Punitive damages: POSSIBLE")
        if damages.get("statutory", {}).get("basis"):
            parts.append(f"- Statutory basis: {'; '.join(damages['statutory']['basis'][:3])}")
        if gaps:
            parts.append(f"\nMISSING INFORMATION ({len(gaps)} items):")
            for g in gaps[:5]:
                parts.append(f"  - {g['question']}")
        return "\n".join(parts)

    elif tool_name == "run_case":
        summary = result.get("strategy_summary", "")
        if summary:
            return f"STRATEGY RESULTS:\n{summary}"
        return json.dumps(result, indent=2, default=str)[:3000]

    elif tool_name == "challenge_opponent":
        # v18k2: render structured defect list (not just counts)
        total = result.get("total", 0)
        fatal_count = result.get("fatal_count", 0)
        defects = result.get("defects", []) or []
        by_category = result.get("by_category", {}) or {}
        by_severity = result.get("by_severity", {}) or {}
        summary = result.get("summary", "")

        parts = []
        parts.append("CHALLENGE RESULTS:")
        parts.append("Total defects: " + str(total))
        parts.append("Fatal: " + str(fatal_count))
        if by_severity:
            sev_pairs = []
            for k, v in by_severity.items():
                sev_pairs.append(str(k) + "=" + str(v))
            parts.append("By severity: " + ", ".join(sev_pairs))
        if by_category:
            cat_pairs = []
            for k, v in by_category.items():
                cat_pairs.append(str(k) + "=" + str(v))
            parts.append("By category: " + ", ".join(cat_pairs))
        if summary:
            parts.append("Summary: " + str(summary))

        if defects:
            parts.append("")
            parts.append("=== STRUCTURED DEFECT LIST ===")
            for i, d in enumerate(defects, 1):
                if not isinstance(d, dict):
                    continue
                cat = d.get("category", "?")
                sev = d.get("severity", "?")
                defect_text = d.get("defect", "")
                cite = d.get("authority") or d.get("cite") or d.get("rule") or ""
                motion = d.get("motion") or d.get("recommended_motion") or ""
                source = d.get("source") or d.get("filing") or ""

                line = "[" + str(i) + "] [" + str(sev).upper() + "/" + str(cat) + "] " + str(defect_text)
                parts.append(line)
                if cite:
                    parts.append("    Authority: " + str(cite))
                if motion:
                    parts.append("    Motion: " + str(motion))
                if source:
                    parts.append("    Source: " + str(source))
        else:
            parts.append("")
            parts.append("(No structured defects array returned by tool)")

        return "\n".join(parts)


    if tool_name == "source_fetch":
        if result.get("ok") and result.get("results"):
            parts = ["SOURCE FETCH RESULTS:"]
            for r in result["results"]:
                parts.append(f"Case: {r.get('case_name', '?')} ({r.get('court', '?')})")
                text = r.get('text', '')[:2000]
                parts.append(text)
            return "\n\n".join(parts)
        return f"Source fetch: {result.get('error', 'no results')}"

    return json.dumps(result, indent=2, default=str)[:2000]


# ======================================================================
# RAG RETRIEVAL — inject DB knowledge into every LLM call
# ======================================================================

def rag_retrieve(query: str, n_results: int = 5, max_chars: int = 3000) -> str:
    """Query ChromaDB and return formatted context for LLM injection.
    
    Every user message gets relevant chunks from the 2.2M legal corpus
    so the LLM answers with real data instead of hallucinating.
    """
    try:
        from pipeline.db_connector import get_db
        db = get_db()

        # ── PROVENANCE: route definition queries through db_provenance_enforcer ──
        import re as _prov_re
        _ql = query.lower()
        _is_def = _prov_re.search(r'defini|what is .*(title|usc|u\.s\.c|section)|\d+\s*u\.?s\.?c', _ql)
        if _is_def:
            try:
                from pipeline.db_provenance_enforcer import get_definition_for_hermes
                _term = ''
                _title = ''
                _m = _prov_re.search(r'definition of (\w+)', _ql)
                if _m: _term = _m.group(1)
                _m2 = _prov_re.search(r'title\s*(\d+)', _ql)
                if _m2: _title = _m2.group(1)
                _m3 = _prov_re.search(r'(\d+)\s*u\.?s\.?c', _ql)
                if _m3: _title = _m3.group(1)
                if _term or _title:
                    _prov = get_definition_for_hermes(term=_term, title=_title, context=query)
                    if _prov and _prov.get('definition'):
                        _pb = []
                        _pb.append('[PROVENANCE: ' + str(_prov.get('provenance','?')) + ' | ' + str(_prov.get('section','')) + ']')
                        _pb.append('STATUTORY DEFINITION: ' + str(_prov['definition']))
                        if _prov.get('construction_flags'):
                            _pb.append('CONSTRUCTION: ' + ', '.join(str(f) for f in _prov['construction_flags']))
                        if _prov.get('conformity_flags'):
                            _pb.append('CFR-USC DIVERGENCE: ' + str(len(_prov['conformity_flags'])) + ' found')
                        if _prov.get('warnings'):
                            for _w in _prov['warnings']: _pb.append('WARNING: ' + str(_w))
                        _prov_ctx = '\n'.join(_pb)
                        # Continue to normal RAG but prepend provenance
                        results = db.query_text(query_text=query, n_results=n_results)
                        chunks = [_prov_ctx]
                        total_chars = len(_prov_ctx)
                        for r in (results or []):
                            text = r.get('text', '').strip()
                            source = r.get('source', 'unknown')
                            if len(text) > 600: text = text[:600] + '...'
                            chunk = '[Source: ' + source + ']\n' + text
                            if total_chars + len(chunk) > max_chars: break
                            chunks.append(chunk)
                            total_chars += len(chunk)
                        return '\n\n---\n\n'.join(chunks)
            except ImportError: pass
            except Exception: pass

        results = db.query_text(query_text=query, n_results=n_results)
        
        if not results:
            return ''
        
        chunks = []
        total_chars = 0
        for r in results:
            text = r.get('text', '').strip()
            source = r.get('source', 'unknown')
            distance = r.get('distance', 1.0)
            
            # Skip low-relevance results
            if distance > 0.75:
                continue
            
            # Truncate individual chunks
            if len(text) > 600:
                text = text[:600] + '...'
            
            chunk = f'[Source: {source} | Relevance: {1 - distance:.0%}]\n{text}'
            
            if total_chars + len(chunk) > max_chars:
                break
            
            chunks.append(chunk)
            total_chars += len(chunk)
        
        if not chunks:
            return ''
        
        return '\n\n'.join(chunks)
    except Exception as e:
        log.warning(f'RAG retrieval failed: {e}')
        return ''


def rag_retrieve_obsidian(query: str, max_results: int = 3) -> str:
    """Search Obsidian vault for relevant patterns and primary sources.

    Priority order:
      1. Direct node lookup (e.g., "title 6 usc" → usc_6.md)
      2. Frontmatter-enriched nodes (with construction rules, burden, etc.)
      3. Keyword search across patterns and sources
    """
    try:
        from pathlib import Path as _P
        import re as _re_obs
        results = []
        query_lower = query.lower()
        query_words = [w for w in query_lower.split() if len(w) > 3]

        sources_dir = _P(str((_BEEZLE_ROOT / "obsidian_sync" / "primary_sources")))

        # ── Direct node lookup for USC/CFR/statute patterns ──
        # "title 6 usc" → look for usc_6.md directly
        usc_m = _re_obs.search(r'(?:title\s+)?(\d+)\s+(?:u\.?\s*s\.?\s*c\.?|united\s+states\s+code)', query_lower)
        if usc_m:
            title_num = usc_m.group(1)
            for subdir in sources_dir.iterdir():
                if not subdir.is_dir():
                    continue
                candidate = subdir / f"usc_{title_num}.md"
                if candidate.exists():
                    text = candidate.read_text(encoding="utf-8", errors="replace")
                    # Strip frontmatter for display but note the enrichment
                    body = text
                    fm_info = ""
                    if text.startswith("---"):
                        parts = text.split("---", 2)
                        if len(parts) >= 3:
                            body = parts[2].strip()
                            # Extract useful frontmatter
                            fm_info = parts[1][:200]
                    results.append(f"[Vault: usc_{title_num}.md (direct match)]\n{body[:800]}")
                    break

        cfr_m = _re_obs.search(r'(?:title\s+)?(\d+)\s+c\.?\s*f\.?\s*r\.?', query_lower)
        if cfr_m and not results:
            title_num = cfr_m.group(1)
            for subdir in sources_dir.iterdir():
                if not subdir.is_dir():
                    continue
                for pattern in [f"cfr_{title_num}.md", f"ecfr_title{title_num}.md"]:
                    candidate = subdir / pattern
                    if candidate.exists():
                        text = candidate.read_text(encoding="utf-8", errors="replace")
                        body = text
                        if text.startswith("---"):
                            parts = text.split("---", 2)
                            if len(parts) >= 3:
                                body = parts[2].strip()
                        results.append(f"[Vault: {pattern} (direct match)]\n{body[:800]}")
                        break
                if results:
                    break

        # ── Search patterns directory ──
        if not results:
            patterns_dir = _P(str((_BEEZLE_ROOT / "obsidian_sync" / "patterns")))
            if patterns_dir.exists():
                for f in patterns_dir.glob("*.md"):
                    if f.name == "INDEX.md":
                        continue
                    text = f.read_text(encoding="utf-8", errors="replace")
                    if any(w in text.lower() for w in query_words):
                        results.append(f"[Pattern: {f.stem}]\n{text[:400]}")
                        if len(results) >= max_results:
                            break

        # ── Search primary sources (ALL subdirectories) ──
        if len(results) < max_results and sources_dir.exists():
            for subdir in sources_dir.iterdir():
                if not subdir.is_dir():
                    continue
                for f in list(subdir.glob("*.md"))[:200]:
                    name_lower = f.stem.lower().replace("_", " ").replace("-", " ")
                    if any(w in name_lower for w in query_words if w not in ("what", "does", "this", "that", "with", "from", "about")):
                        text = f.read_text(encoding="utf-8", errors="replace")
                        body = text
                        if text.startswith("---"):
                            parts = text.split("---", 2)
                            if len(parts) >= 3:
                                body = parts[2].strip()
                        results.append(f"[Source: {subdir.name}/{f.stem}]\n{body[:400]}")
                        if len(results) >= max_results:
                            break
                if len(results) >= max_results:
                    break

        # ── Search vault v2 statutes/rules by filename ──
        if len(results) < max_results:
            vault_dir = _P(str((_BEEZLE_ROOT / "obsidian_sync" / "beezle_vault" / "v2")))
            if vault_dir.exists():
                for subdir_name in ["statutes", "rules", "regulations"]:
                    sdir = vault_dir / subdir_name
                    if not sdir.exists():
                        continue
                    for f in list(sdir.glob("*.md"))[:50]:
                        name_lower = f.stem.lower().replace("_", " ").replace("-", " ")
                        if any(w in name_lower for w in query_words if w not in ("what", "does", "this", "that")):
                            text = f.read_text(encoding="utf-8", errors="replace")
                            results.append(f"[Vault/{subdir_name}: {f.stem}]\n{text[:400]}")
                            if len(results) >= max_results:
                                break
                    if len(results) >= max_results:
                        break

        return "\n\n".join(results[:max_results])
    except Exception as e:
        log.warning(f"Obsidian retrieval failed: {e}")
        return ""



def _auto_fetch_legal_source(query: str) -> str:
    """Detect what kind of legal source is needed and fetch it automatically.

    Routes to the correct scraper based on query patterns:
      - USC title/section → fed_register.fetch_usc
      - CFR title/part → fed_register.fetch_cfr
      - Public Law → fed_register.fetch_public_law
      - FRCP/FRE rules → fed_register.fetch_frcp
      - PA statutes → pa_legis.fetch_title
      - PA admin code → pa_code.fetch_title

    Fetched sources are saved to Obsidian + ingested to ChromaDB via save_primary_source.
    Returns formatted context string for immediate LLM use, or empty string if nothing found.
    """
    import re as _re_af
    query_lower = query.lower()

    # --- USC ---
    usc_m = _re_af.search(
        r'(?:title\s+)?(\d+)\s+(?:U\.?\s*S\.?\s*C\.?|united\s+states\s+code)'
        r'(?:\s+(?:sec(?:tion)?\s*\.?\s*|§\s*)?(\d+[a-z]?(?:\([a-z0-9]+\))?))?',
        query, _re_af.IGNORECASE
    )
    if usc_m:
        title_num = int(usc_m.group(1))
        section = usc_m.group(2) if usc_m.lastindex >= 2 and usc_m.group(2) else None
        try:
            from scrapers.fed_register import fetch_usc
            r = fetch_usc(title_num, section)
            if not r.get('error'):
                path = r.get('path', '')
                if path:
                    try:
                        _ingest_to_chroma(f'{title_num} USC {section or ""}'.strip(), path, 'federal')
                    except Exception:
                        pass
                    from pathlib import Path as _P
                    txt = _P(path).read_text(encoding='utf-8', errors='replace')
                    if txt.startswith('---'):
                        fm_end = txt.find('---', 3)
                        if fm_end > 0: txt = txt[fm_end+3:].strip()
                    sec_label = f' § {section}' if section else ''
                    log.info(f'Auto-fetch: got {title_num} USC{sec_label}: {len(txt)} chars')
                    return f'--- {title_num} U.S.C.{sec_label} (AUTO-FETCHED FROM SOURCE) ---\n{txt[:4000]}'
        except Exception as e:
            log.info(f'Auto-fetch USC failed: {e}')

    # --- CFR ---
    cfr_m = _re_af.search(
        r'(?:title\s+)?(\d+)\s+(?:C\.?\s*F\.?\s*R\.?|code\s+of\s+federal\s+regulations)'
        r'(?:\s+(?:part|sec(?:tion)?\s*\.?\s*|§\s*)?(\d+(?:\.\d+)?))?',
        query, _re_af.IGNORECASE
    )
    if cfr_m:
        title_num = int(cfr_m.group(1))
        part = cfr_m.group(2) if cfr_m.lastindex >= 2 and cfr_m.group(2) else None
        try:
            from scrapers.fed_register import fetch_cfr
            r = fetch_cfr(title_num, part)
            if not r.get('error'):
                path = r.get('path', '')
                if path:
                    try:
                        _ingest_to_chroma(f'{title_num} CFR {part or ""}'.strip(), path, 'federal')
                    except Exception:
                        pass
                    from pathlib import Path as _P
                    txt = _P(path).read_text(encoding='utf-8', errors='replace')
                    if txt.startswith('---'):
                        fm_end = txt.find('---', 3)
                        if fm_end > 0: txt = txt[fm_end+3:].strip()
                    part_label = f' Part {part}' if part else ''
                    log.info(f'Auto-fetch: got {title_num} CFR{part_label}: {len(txt)} chars')
                    return f'--- {title_num} C.F.R.{part_label} (AUTO-FETCHED FROM SOURCE) ---\n{txt[:4000]}'
        except Exception as e:
            log.info(f'Auto-fetch CFR failed: {e}')

    # --- Public Law ---
    plaw_m = _re_af.search(
        r'(?:public\s+law|P\.?L\.?)\s*(\d+)[\s\-]+(\d+)',
        query, _re_af.IGNORECASE
    )
    if plaw_m:
        congress = int(plaw_m.group(1))
        law_num = int(plaw_m.group(2))
        try:
            from scrapers.fed_register import fetch_public_law
            r = fetch_public_law(congress, law_num)
            if not r.get('error'):
                path = r.get('path', '')
                if path:
                    try:
                        _ingest_to_chroma(f'PL {congress}-{law_num}', path, 'federal')
                    except Exception:
                        pass
                    from pathlib import Path as _P
                    txt = _P(path).read_text(encoding='utf-8', errors='replace')
                    if txt.startswith('---'):
                        fm_end = txt.find('---', 3)
                        if fm_end > 0: txt = txt[fm_end+3:].strip()
                    log.info(f'Auto-fetch: got PL {congress}-{law_num}: {len(txt)} chars')
                    return f'--- Public Law {congress}-{law_num} (AUTO-FETCHED FROM SOURCE) ---\n{txt[:4000]}'
        except Exception as e:
            log.info(f'Auto-fetch public law failed: {e}')

    # --- FRCP / FRE ---
    frcp_m = _re_af.search(
        r'(?:federal\s+rule|FRCP|FRE|F\.?\s*R\.?\s*C\.?\s*P\.?|F\.?\s*R\.?\s*E\.?)'
        r'(?:\s+(?:of\s+)?(?:civil\s+procedure|evidence))?\s*(?:rule\s+)?(\d+)?',
        query, _re_af.IGNORECASE
    )
    if frcp_m:
        rule_num = frcp_m.group(1) if frcp_m.lastindex >= 1 and frcp_m.group(1) else None
        try:
            from scrapers.fed_register import fetch_frcp
            r = fetch_frcp(rule_num)
            if not r.get('error'):
                path = r.get('path', '')
                if path:
                    try:
                        _ingest_to_chroma(f'FRCP Rule {rule_num or "all"}', path, 'federal')
                    except Exception:
                        pass
                    from pathlib import Path as _P
                    txt = _P(path).read_text(encoding='utf-8', errors='replace')
                    if txt.startswith('---'):
                        fm_end = txt.find('---', 3)
                        if fm_end > 0: txt = txt[fm_end+3:].strip()
                    rule_label = f' Rule {rule_num}' if rule_num else ''
                    log.info(f'Auto-fetch: got FRCP{rule_label}: {len(txt)} chars')
                    return f'--- FRCP{rule_label} (AUTO-FETCHED FROM SOURCE) ---\n{txt[:4000]}'
        except Exception as e:
            log.info(f'Auto-fetch FRCP failed: {e}')

    # --- PA Statutes ---
    pa_stat_m = _re_af.search(
        r'(?:title\s+)?(\d+)\s+Pa\.?\s*(?:C\.?\s*S\.?|consolidated\s+statut)'
        r'(?:\s+(?:sec(?:tion)?\s*\.?\s*|§\s*)?(\d+))?',
        query, _re_af.IGNORECASE
    )
    if pa_stat_m:
        title_num = int(pa_stat_m.group(1))
        section = pa_stat_m.group(2) if pa_stat_m.lastindex >= 2 and pa_stat_m.group(2) else None
        try:
            from scrapers.pa_legis import fetch_title
            r = fetch_title(title_num, section)
            if not r.get('error'):
                path = r.get('path', '')
                if path:
                    try:
                        _ingest_to_chroma(f'{title_num} Pa.C.S. {section or ""}'.strip(), path, 'pennsylvania')
                    except Exception:
                        pass
                    from pathlib import Path as _P
                    txt = _P(path).read_text(encoding='utf-8', errors='replace')
                    if txt.startswith('---'):
                        fm_end = txt.find('---', 3)
                        if fm_end > 0: txt = txt[fm_end+3:].strip()
                    sec_label = f' § {section}' if section else ''
                    log.info(f'Auto-fetch: got {title_num} Pa.C.S.{sec_label}: {len(txt)} chars')
                    return f'--- {title_num} Pa.C.S.{sec_label} (AUTO-FETCHED FROM SOURCE) ---\n{txt[:4000]}'
        except Exception as e:
            log.info(f'Auto-fetch PA statute failed: {e}')

    # --- PA Admin Code ---
    pa_code_m = _re_af.search(
        r'(?:title\s+)?(\d+)\s+Pa\.?\s*(?:Code|admin)',
        query, _re_af.IGNORECASE
    )
    if pa_code_m:
        title_num = int(pa_code_m.group(1))
        try:
            from scrapers.pa_code import fetch_title
            r = fetch_title(title_num)
            if not r.get('error'):
                path = r.get('path', '')
                if path:
                    try:
                        _ingest_to_chroma(f'{title_num} Pa. Code', path, 'pennsylvania')
                    except Exception:
                        pass
                    from pathlib import Path as _P
                    txt = _P(path).read_text(encoding='utf-8', errors='replace')
                    if txt.startswith('---'):
                        fm_end = txt.find('---', 3)
                        if fm_end > 0: txt = txt[fm_end+3:].strip()
                    log.info(f'Auto-fetch: got {title_num} Pa. Code: {len(txt)} chars')
                    return f'--- {title_num} Pa. Code (AUTO-FETCHED FROM SOURCE) ---\n{txt[:4000]}'
        except Exception as e:
            log.info(f'Auto-fetch PA Code failed: {e}')

    # --- IRM (Internal Revenue Manual) ---
    irm_m = _re_af.search(
        r'(?:IRM|internal\s+revenue\s+manual)\s+(\d+)\.(\d+)\.(\d+)',
        query, _re_af.IGNORECASE
    )
    if irm_m:
        try:
            from scrapers.irs import fetch_irm
            r = fetch_irm(int(irm_m.group(1)), int(irm_m.group(2)), int(irm_m.group(3)))
            if not r.get('error'):
                path = r.get('path', '')
                if path:
                    try:
                        _ingest_to_chroma(f'IRM {irm_m.group(1)}.{irm_m.group(2)}.{irm_m.group(3)}', path, 'federal')
                    except Exception:
                        pass
                    from pathlib import Path as _P
                    txt = _P(path).read_text(encoding='utf-8', errors='replace')
                    if txt.startswith('---'):
                        fm_end = txt.find('---', 3)
                        if fm_end > 0: txt = txt[fm_end+3:].strip()
                    label = f'IRM {irm_m.group(1)}.{irm_m.group(2)}.{irm_m.group(3)}'
                    log.info(f'Auto-fetch: got {label}: {len(txt)} chars')
                    return f'--- {label} (AUTO-FETCHED FROM SOURCE) ---\n{txt[:4000]}'
        except Exception as e:
            log.info(f'Auto-fetch IRM failed: {e}')

    # --- IRC (Internal Revenue Code / 26 USC) ---
    irc_m = _re_af.search(
        r'(?:IRC|I\.?R\.?C\.?|internal\s+revenue\s+code)\s+(?:sec(?:tion)?\s*\.?\s*|§\s*)?(\d+[a-z]?)',
        query, _re_af.IGNORECASE
    )
    if irc_m:
        try:
            from scrapers.irs import fetch_irc_section
            r = fetch_irc_section(irc_m.group(1))
            if not r.get('error'):
                path = r.get('path', '')
                if path:
                    try:
                        _ingest_to_chroma(f'IRC § {irc_m.group(1)}', path, 'federal')
                    except Exception:
                        pass
                    from pathlib import Path as _P
                    txt = _P(path).read_text(encoding='utf-8', errors='replace')
                    if txt.startswith('---'):
                        fm_end = txt.find('---', 3)
                        if fm_end > 0: txt = txt[fm_end+3:].strip()
                    log.info(f'Auto-fetch: got IRC § {irc_m.group(1)}: {len(txt)} chars')
                    return f'--- IRC § {irc_m.group(1)} (AUTO-FETCHED FROM SOURCE) ---\n{txt[:4000]}'
        except Exception as e:
            log.info(f'Auto-fetch IRC failed: {e}')

    # --- Treasury Regulation (26 CFR) ---
    treas_reg_m = _re_af.search(
        r'(?:treas(?:ury)?\.?\s*reg|26\s+C\.?\s*F\.?\s*R\.?)(?:\s+(?:sec(?:tion)?\s*\.?\s*|§\s*)?(\d+\.\d+[\-\d]*))?',
        query, _re_af.IGNORECASE
    )
    if treas_reg_m:
        try:
            from scrapers.irs import fetch_treasury_reg
            sec = treas_reg_m.group(1) if treas_reg_m.lastindex >= 1 and treas_reg_m.group(1) else None
            r = fetch_treasury_reg(section=sec)
            if not r.get('error'):
                path = r.get('path', '')
                if path:
                    try:
                        _ingest_to_chroma(f'Treas. Reg. § {sec or "26 CFR"}', path, 'federal')
                    except Exception:
                        pass
                    from pathlib import Path as _P
                    txt = _P(path).read_text(encoding='utf-8', errors='replace')
                    if txt.startswith('---'):
                        fm_end = txt.find('---', 3)
                        if fm_end > 0: txt = txt[fm_end+3:].strip()
                    log.info(f'Auto-fetch: got Treas. Reg. {sec or ""}: {len(txt)} chars')
                    return f'--- Treas. Reg. § {sec or "26 CFR"} (AUTO-FETCHED FROM SOURCE) ---\n{txt[:4000]}'
        except Exception as e:
            log.info(f'Auto-fetch Treasury Reg failed: {e}')

    # --- OCC Bulletin ---
    occ_m = _re_af.search(
        r'OCC\s+(?:bulletin|bull?\.?)\s+(\d{4})[\s\-]+(\d+)',
        query, _re_af.IGNORECASE
    )
    if occ_m:
        try:
            from scrapers.treasury import fetch_occ_bulletin
            r = fetch_occ_bulletin(int(occ_m.group(1)), int(occ_m.group(2)))
            if not r.get('error'):
                path = r.get('path', '')
                if path:
                    try:
                        _ingest_to_chroma(f'OCC Bulletin {occ_m.group(1)}-{occ_m.group(2)}', path, 'federal')
                    except Exception:
                        pass
                    from pathlib import Path as _P
                    txt = _P(path).read_text(encoding='utf-8', errors='replace')
                    if txt.startswith('---'):
                        fm_end = txt.find('---', 3)
                        if fm_end > 0: txt = txt[fm_end+3:].strip()
                    log.info(f'Auto-fetch: got OCC Bulletin {occ_m.group(1)}-{occ_m.group(2)}: {len(txt)} chars')
                    return f'--- OCC Bulletin {occ_m.group(1)}-{occ_m.group(2)} (AUTO-FETCHED) ---\n{txt[:4000]}'
        except Exception as e:
            log.info(f'Auto-fetch OCC Bulletin failed: {e}')

    # --- OCC Handbook keyword match ---
    if 'comptroller' in query_lower and 'handbook' in query_lower:
        try:
            from scrapers.treasury import fetch_occ_handbook, OCC_HANDBOOK_BOOKLETS
            # Find best matching booklet slug from query
            best_slug = None
            for slug, name in OCC_HANDBOOK_BOOKLETS.items():
                if any(w in query_lower for w in name.lower().split() if len(w) > 4):
                    best_slug = slug
                    break
            if best_slug:
                r = fetch_occ_handbook(best_slug)
                if not r.get('error'):
                    path = r.get('path', '')
                    if path:
                        try:
                            _ingest_to_chroma(f'OCC Handbook {best_slug}', path, 'federal')
                        except Exception:
                            pass
                        from pathlib import Path as _P
                        txt = _P(path).read_text(encoding='utf-8', errors='replace')
                        if txt.startswith('---'):
                            fm_end = txt.find('---', 3)
                            if fm_end > 0: txt = txt[fm_end+3:].strip()
                        log.info(f'Auto-fetch: got OCC Handbook {best_slug}: {len(txt)} chars')
                        return f'--- Comptroller Handbook: {best_slug} (AUTO-FETCHED) ---\n{txt[:4000]}'
        except Exception as e:
            log.info(f'Auto-fetch OCC Handbook failed: {e}')

    return ""


def auto_scrape_and_ingest(query: str, max_results: int = 3) -> str:
    """When RAG returns nothing relevant, scrape from CourtListener.
    
    Flow:
    1. Search CourtListener for the query
    2. Fetch full opinion text
    3. Save to Obsidian primary_sources
    4. Chunk and ingest into ChromaDB
    5. Return the text for immediate LLM context
    
    Returns formatted context string (empty if nothing found).
    """
    try:
        from scrapers.courtlistener import smart_search, search_and_save
        
        results = search_and_save(query, n=max_results)
        if not results:
            log.info(f"Auto-scrape: no results for '{query[:50]}'")
            return ""
        
        chunks = []
        for r in results:
            case_name = r.get("case_name", "Unknown Case")
            court = r.get("court", "unknown")
            path = r.get("path", "")
            text_len = r.get("text_length", 0)
            
            # Read the saved file for context
            if path and Path(path).exists():
                text = Path(path).read_text(encoding="utf-8", errors="replace")
                # Skip YAML frontmatter
                if text.startswith("---"):
                    end_fm = text.find("---", 3)
                    if end_fm > 0:
                        text = text[end_fm + 3:].strip()
                # Truncate for context
                text = text[:1500]
                chunks.append(f"[SCRAPED: {case_name} ({court})]\n{text}")
            
            # Try to ingest into ChromaDB for future queries
            try:
                _ingest_to_chroma(case_name, path, court)
            except Exception as ie:
                log.warning(f"ChromaDB ingest failed for {case_name}: {ie}")
        
        if chunks:
            log.info(f"Auto-scrape: found {len(chunks)} results for '{query[:50]}'")
            return "\n\n".join(chunks[:max_results])
        return ""
    except Exception as e:
        log.warning(f"Auto-scrape failed: {e}")
        return ""


def _ingest_to_chroma(case_name: str, path: str, court: str = ""):
    from pathlib import Path
    """Chunk and ingest a scraped document into ChromaDB."""
    if not path or not Path(path).exists():
        return
    
    try:
        from pipeline.db_connector import get_db
        
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        # Skip YAML frontmatter
        if text.startswith("---"):
            end_fm = text.find("---", 3)
            if end_fm > 0:
                text = text[end_fm + 3:].strip()
        
        if len(text) < 100:
            return
        
        db = get_db()
        
        # Simple chunking: split into ~500 char chunks
        chunk_size = 500
        chunks = []
        for i in range(0, len(text), chunk_size):
            chunk = text[i:i + chunk_size].strip()
            if len(chunk) > 50:
                chunks.append(chunk)
        
        if not chunks:
            return
        
        # Generate IDs and metadata
        import hashlib
        ids = []
        texts = []
        metadatas = []
        for i, chunk in enumerate(chunks[:50]):  # max 50 chunks per doc
            chunk_id = hashlib.md5(f"{case_name}_{i}".encode()).hexdigest()
            ids.append(chunk_id)
            texts.append(chunk)
            metadatas.append({
                "source": f"courtlistener_{Path(path).stem}",
                "chunk_type": "text",
                "doc_type_classified": "case_law",
                "court": court,
            })
        
        # Use the DB's embedding model (768-dim bge-base-en-v1.5)
        db._connect()
        embeddings = []
        for t in texts:
            emb = db._embed_query(t)
            embeddings.append(emb)
        db._collection.add(
            ids=ids,
            documents=texts,
            embeddings=embeddings,
            metadatas=metadatas,
        )
        log.info(f"Ingested {len(chunks)} chunks from {case_name} into ChromaDB")
    except Exception as e:
        log.warning(f"ChromaDB ingest error: {e}")


# ======================================================================
# MAIN BRIDGE: LLM + TOOL EXECUTION LOOP
# ======================================================================

async def hermes_chat(message: str, history: list = None) -> dict:
    """Full Hermes chat with Beezle tool calling + RAG."""
    from pipeline.llm_layer import get_llm

    try:
        llm = get_llm()
        h = llm.health_check()
        if h.get("status") != "ok":
            raise RuntimeError(f"LLM not ok: {h}")
    except Exception as e:
        log.warning(f"LLM offline: {e}")
        return _fallback_deterministic(message)

    # ── STEP 1: Query Obsidian vault FIRST (enriched nodes with frontmatter) ──
    obsidian_context = rag_retrieve_obsidian(message, max_results=3)
    has_fetched_source = False

    # If Obsidian found a direct match (e.g., usc_6.md), it's authoritative
    obsidian_is_good = len(obsidian_context) >= 200 and 'direct match' in obsidian_context

    # ── STEP 2: If Obsidian didn't have a direct hit, query ChromaDB ──
    rag_context = ''
    if not obsidian_is_good:
        rag_context = rag_retrieve(message, n_results=5, max_chars=3000)

    # Combine: Obsidian takes priority, ChromaDB fills gaps
    combined_context = ''
    if obsidian_is_good:
        combined_context = obsidian_context
        log.info(f"Obsidian: direct match found ({len(obsidian_context)} chars) — using as primary")
    elif rag_context and len(rag_context) >= 200:
        combined_context = rag_context
        if obsidian_context:
            combined_context = obsidian_context + "\n\n" + rag_context
        log.info(f"RAG: DB had results ({len(rag_context)} chars)")
    elif obsidian_context:
        combined_context = obsidian_context
        log.info(f"Obsidian: keyword matches ({len(obsidian_context)} chars)")

    # Check if we have enough to answer
    db_is_good = len(combined_context) >= 200

    # ── STEP 3: If DB is thin, try to fetch from authoritative sources ──
    if not db_is_good:
        import re as _re_pre
        _fetched = ''

        # Public Law
        _plaw_m = _re_pre.search(r'(?:public\s+law|P\.?L\.?)\s*(\d+)[\s\-]+(\d+)', message, _re_pre.IGNORECASE)
        if _plaw_m and not _fetched:
            try:
                from scrapers.fed_register import fetch_public_law
                _pl_r = fetch_public_law(int(_plaw_m.group(1)), int(_plaw_m.group(2)))
                if not _pl_r.get('error'):
                    _pl_path = _pl_r.get('path', '')
                    if _pl_path:
                        from pathlib import Path as _PathPL
                        _pl_txt = _PathPL(_pl_path).read_text(encoding='utf-8', errors='replace')
                        if _pl_txt.startswith('---'):
                            _fm_end = _pl_txt.find('---', 3)
                            if _fm_end > 0: _pl_txt = _pl_txt[_fm_end+3:].strip()
                        _fetched = f'\n\n--- PUBLIC LAW {_plaw_m.group(1)}-{_plaw_m.group(2)} (FETCHED) ---\n' + _pl_txt[:3000]
                        try:
                            _ingest_to_chroma(f'PL {_plaw_m.group(1)}-{_plaw_m.group(2)}', _pl_path, 'federal')
                        except Exception:
                            pass
                        log.info(f'Fetch: got PL {_plaw_m.group(1)}-{_plaw_m.group(2)}: {len(_pl_txt)} chars')
            except Exception as _e:
                log.info(f'Fetch public law failed: {_e}')

        # USC
        if not _fetched:
            _usc_m = _re_pre.search(
                r'(?:title\s+)?(\d+)\s+(?:U\.?\s*S\.?\s*C\.?|united\s+states\s+code)'
                r'(?:\s+(?:sec(?:tion)?\s*\.?\s*)?(\d+[a-z]?))?',
                message, _re_pre.IGNORECASE
            )
            if _usc_m:
                _usc_title = int(_usc_m.group(1))
                _usc_sec = _usc_m.group(2) if _usc_m.lastindex >= 2 and _usc_m.group(2) else None
                try:
                    from scrapers.fed_register import fetch_usc
                    _usc_r = fetch_usc(_usc_title, _usc_sec)
                    if not _usc_r.get('error'):
                        _usc_path = _usc_r.get('path', '')
                        if _usc_path:
                            from pathlib import Path as _PathUSC
                            _usc_txt = _PathUSC(_usc_path).read_text(encoding='utf-8', errors='replace')
                            if _usc_txt.startswith('---'):
                                _fm_end = _usc_txt.find('---', 3)
                                if _fm_end > 0: _usc_txt = _usc_txt[_fm_end+3:].strip()
                            _sec_label = f' § {_usc_sec}' if _usc_sec else ''
                            _fetched = f'\n\n--- {_usc_title} U.S.C.{_sec_label} (FETCHED FROM SOURCE) ---\n' + _usc_txt[:4000]
                            try:
                                _ingest_to_chroma(f'{_usc_title} USC {_usc_sec or ""}'.strip(), _usc_path, 'federal')
                            except Exception:
                                pass
                            log.info(f'Fetch: got {_usc_title} USC {_usc_sec or "title"}: {len(_usc_txt)} chars')
                except Exception as _e:
                    log.info(f'Fetch USC failed: {_e}')

        # CFR
        if not _fetched:
            _cfr_m = _re_pre.search(
                r'(?:title\s+)?(\d+)\s+(?:C\.?\s*F\.?\s*R\.?|code\s+of\s+federal\s+regulations)'
                r'(?:\s+(?:part|sec(?:tion)?\s*\.?\s*)?(\d+(?:\.\d+)?))?',
                message, _re_pre.IGNORECASE
            )
            if _cfr_m:
                _cfr_title = int(_cfr_m.group(1))
                _cfr_part = _cfr_m.group(2) if _cfr_m.lastindex >= 2 and _cfr_m.group(2) else None
                try:
                    from scrapers.fed_register import fetch_cfr
                    _cfr_r = fetch_cfr(_cfr_title, _cfr_part)
                    if not _cfr_r.get('error'):
                        _cfr_path = _cfr_r.get('path', '')
                        if _cfr_path:
                            from pathlib import Path as _PathCFR
                            _cfr_txt = _PathCFR(_cfr_path).read_text(encoding='utf-8', errors='replace')
                            if _cfr_txt.startswith('---'):
                                _fm_end = _cfr_txt.find('---', 3)
                                if _fm_end > 0: _cfr_txt = _cfr_txt[_fm_end+3:].strip()
                            _part_label = f' Part {_cfr_part}' if _cfr_part else ''
                            _fetched = f'\n\n--- {_cfr_title} C.F.R.{_part_label} (FETCHED FROM SOURCE) ---\n' + _cfr_txt[:4000]
                            try:
                                _ingest_to_chroma(f'{_cfr_title} CFR {_cfr_part or ""}'.strip(), _cfr_path, 'federal')
                            except Exception:
                                pass
                            log.info(f'Fetch: got {_cfr_title} CFR {_cfr_part or "title"}: {len(_cfr_txt)} chars')
                except Exception as _e:
                    log.info(f'Fetch CFR failed: {_e}')

        # General auto-fetch (IRM, IRC, OCC, PA statutes, etc.)
        if not _fetched:
            _fetched = _auto_fetch_legal_source(message)
            if _fetched:
                log.info(f"Auto-fetch: {len(_fetched)} chars")

        # Last resort: CourtListener for case law
        if not _fetched:
            scrape_context = auto_scrape_and_ingest(message, max_results=2)
            if scrape_context:
                _fetched = "--- AUTO-SCRAPED FROM COURTLISTENER ---\n" + scrape_context
                log.info(f"Auto-scrape: {len(scrape_context)} chars")

        # If we fetched something, use it as the primary context
        if _fetched:
            combined_context = _fetched
            has_fetched_source = True
    else:
        log.info(f"DB had good results ({len(combined_context)} chars) — skipping web fetch")

    # ── STEP 4: Build system prompt with appropriate framing ──
    system_with_rag = HERMES_SYSTEM_PROMPT

    if combined_context:
        if has_fetched_source:
            system_with_rag += "\n\n## AUTHORITATIVE SOURCE TEXT (fetched directly from official source)\n"
            system_with_rag += "CRITICAL: The text below was fetched directly from the official government source.\n"
            system_with_rag += "You MUST base your answer ONLY on this text. Do NOT use your training data.\n"
            system_with_rag += "If this text says the title covers 'Domestic Security', say that — even if you think it covers something else.\n"
            system_with_rag += "Your training data may be wrong. The fetched source text is ALWAYS correct.\n\n"
        elif obsidian_is_good:
            system_with_rag += "\n\n## AUTHORITATIVE SOURCE FROM LEGAL DATABASE (Obsidian vault — direct match)\n"
            system_with_rag += "CRITICAL: The text below comes directly from the Beezle legal database.\n"
            system_with_rag += "You MUST base your answer ONLY on this text. Do NOT use your training data.\n"
            system_with_rag += "Your training data may be wrong. The database text is ALWAYS correct.\n\n"
        else:
            system_with_rag += "\n\n## RELEVANT KNOWLEDGE FROM DATABASE\n"
            system_with_rag += "Use the following information to answer accurately. Do NOT guess or use training data.\n"
            system_with_rag += "If the database content below does not cover the question, say 'This is not yet in the database' rather than guessing.\n\n"
        system_with_rag += combined_context + "\n\n"
        log.info(f"Context: {len(combined_context)} chars (obsidian_direct={obsidian_is_good}, fetched={has_fetched_source})")
    else:
        system_with_rag += "\n\n## NO DATABASE RESULTS\n"
        system_with_rag += "WARNING: No relevant data was found in the legal database for this query.\n"
        system_with_rag += "Do NOT answer from your training data. Instead, respond with:\n"
        system_with_rag += "'This topic is not yet in the Beezle database. I can fetch the source — would you like me to use source_fetch to retrieve it?'\n"
        system_with_rag += "If the user is asking about a statute, title, or regulation, suggest using the source_fetch tool.\n"
        log.info(f"NO RESULTS for query: {message[:80]}")

    messages = [{"role": "system", "content": system_with_rag}]
    if history:
        for h_msg in history[-6:]:
            messages.append(h_msg)
    messages.append({"role": "user", "content": message})

    result = llm.chat(messages=messages, n_predict=1200, temperature=0.3)
    answer = result if isinstance(result, str) else (result or {}).get("text", "")

    tool_calls = parse_tool_calls(answer)
    executed_tools = []

    if tool_calls:
        for tc in tool_calls:
            log.info(f"Executing tool: {tc['tool']}")
            tool_result = execute_tool(tc["tool"], tc["params"])
            formatted = format_tool_result(tc["tool"], tool_result)
            executed_tools.append({"tool": tc["tool"], "params": tc["params"], "result_summary": formatted})
            messages.append({"role": "assistant", "content": answer})
            messages.append({"role": "user", "content": f"[TOOL RESULTS]\n{formatted}\n\nPresent these results clearly."})
            result2 = llm.chat(messages=messages, n_predict=1500, temperature=0.3)
            answer = result2 if isinstance(result2, str) else (result2 or {}).get("text", "")

    clean_answer = strip_tool_calls(answer)

    # Strip disclaimers
    import re as _rc
    for pat in [
        r"\nNote:.*",
        r"\nImportant Note:.*",
        r"\nWhile .*legal advice.*",
        r"\nI cannot provide.*",
    ]:
        try:
            clean_answer = _rc.sub("", clean_answer, flags=_rc.IGNORECASE)
        except Exception:
            pass

    return {
        "answer": clean_answer or answer,
        "route": "hermes_beezle" if tool_calls else "hermes_general",
        "llm_used": True,
        "sources": executed_tools,
        "tool_calls": len(tool_calls),
    }


def _fallback_deterministic(message: str) -> dict:
    """When LLM is offline, try deterministic routing only."""
    msg_lower = message.lower()

    # Try to detect case IDs
    import re
    case_match = re.search(r'(fed:\S+|phila_ccp:\S+)', msg_lower)
    if case_match:
        try:
            from pipeline.reasoning_orchestrator import build_strategy, save_strategy
            strat = build_strategy(case_match.group(1))
            save_strategy(strat)
            return {
                "answer": strat.get("strategy_summary", "Strategy built."),
                "route": "beezle_deterministic",
                "llm_used": False,
                "sources": [],
                "tool_calls": 0,
            }
        except Exception as e:
            pass

    return {
        "answer": "LLM offline. For case analysis, include a case ID (e.g. fed:2:26-cv-00123).",
        "route": "llm_offline",
        "llm_used": False,
        "sources": [],
        "tool_calls": 0,
    }

# Unified alias — synchronous query handler for hermes_tab_endpoint
def handle_query(query: str) -> dict:
    """Synchronous entry point: try deterministic first, then return None for async."""
    result = _fallback_deterministic(query)
    if result and result.get("answer"):
        return result
    return None
