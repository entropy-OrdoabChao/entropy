"""
beezle_mcp_server.py â€” Beezle MCP server for Hermes Agent (Nous Research)

Exposes Beezle's deterministic legal pipeline as MCP tools so Hermes can
orchestrate them. Hermes decides WHAT to do; Beezle does the deterministic
heavy lifting (definitions, IF/THEN chains, court hierarchy, scoring, drafting).

Layer 1 surface: 13 tools, mostly read-only. Drafting tools are idempotent
(re-runnable). No tool generates legal opinions â€” every output is grounded
in case_registry, mined audit data, locked definitions, or the deterministic
drafter.

Run:
    python beezle_mcp_server.py                      # SSE on port 8200 (default)
    python beezle_mcp_server.py --transport stdio    # stdio for local clients
    python beezle_mcp_server.py --port 8300          # custom SSE port

Hermes Agent (hosted) connects via SSE. For local-only testing use stdio.
For remote Hermes access, expose this port via cloudflared/ngrok tunnel.
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import argparse
import json
import sys
import traceback
from dataclasses import asdict
from pathlib import Path
from datetime import datetime
from typing import Optional

# â”€â”€â”€ Path setup so pipeline/* and generation/* import cleanly â”€â”€â”€
BASE_PATH = _eroot()
if str(BASE_PATH) not in sys.path:
    sys.path.insert(0, str(BASE_PATH))

OUTPUT_DIR = BASE_PATH / "output"
AUDIT_DIR = OUTPUT_DIR / "audit"
REPLY_BRIEFS_DIR = OUTPUT_DIR / "reply_briefs"
OBSIDIAN_VAULT = BASE_PATH / "obsidian_sync"
OBSIDIAN_BEEZLE = OBSIDIAN_VAULT / "beezle_managed"

# â”€â”€â”€ FastMCP â”€â”€â”€
from fastmcp import FastMCP
from generation.md_to_docx import render_md_to_docx

# Phase 2: citation resolver
from resolve_citation import resolve_citation_impl

mcp = FastMCP(
    name="beezle",
    instructions=(
        "Beezle is a deterministic legal analysis pipeline for pro se litigation. "
        "It audits court filings for stare decisis violations, classifies citations "
        "by binding authority, builds locked legal definitions and IF/THEN chains, "
        "and drafts reply briefs from cached audit data. Beezle FLAGS structural "
        "defects; it does NOT generate legal opinions on whether cases remain good "
        "law. Use search_chunks or search_case_law for retrieval, build_definitions "
        "and build_logic_chain for deterministic argument scaffolding, and "
        "draft_reply_brief to produce a filing-grade reply brief for a canonical case."
    ),
)


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# CASE REGISTRY TOOLS
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

@mcp.tool
def list_cases() -> list[dict]:
    """List all canonical cases in the Beezle registry.

    Returns one entry per case with canonical_id, caption, court, judge,
    case_type, filing date, status, and related cases. Use this first to
    discover what cases Beezle knows about before calling case-specific tools.
    """
    from pipeline.case_registry import get_registry
    reg = get_registry()
    return [asdict(e) for e in reg.entries.values()]


@mcp.tool
def case_filing_meta(case_id: str, filing_type: str = "", bundle: str = "") -> dict:
    """Filing metadata for a case FROM THE CASE FILE — the one source every agent (m2 room, efile, chat) reads.

    Returns: judge (+ provenance via pipeline.case_judge.resolve: CASE_FACTS -> DOCKET_REPORT -> order /s/ ->
    registry mirror; null = NOT IN CASE FILE, never guessed), forum (court / e-file channel / control-no convention),
    filing_type YAML (title, deadline_days, required blocks) when given, the latest EFILE_PART3.json for the
    bundle when present, and the efiled receipts. Nothing here is a literal; fix the case file, not the caller.
    """
    import json as _j, re as _re
    from pathlib import Path as _P
    ROOT = _P(__file__).resolve().parent
    from pipeline.case_registry import get_registry
    from pipeline.case_judge import resolve, slug_for
    reg = get_registry()
    entry = reg.entries.get(case_id) or next((e for e in reg.entries.values()
                 if case_id in (getattr(e, "canonical_id", None), getattr(e, "slug", None)) or case_id in (getattr(e, "docket_aliases", None) or [])), None)
    if not entry:
        return {"error": f"case not found: {case_id}"}
    e = asdict(entry)
    slug = slug_for(ROOT, e.get("canonical_id") or case_id) or ""
    out = {"canonical_id": e.get("canonical_id"), "slug": slug, "caption": e.get("caption"), "court": e.get("court"),
           "judge": resolve(ROOT, slug),
           "forum": {"efile_channel": "FJD e-filing (user submits at cart)" if str(e.get("canonical_id", "")).startswith("phila_ccp") else "CM/ECF or mail per court",
                     "docket_ref": "Control No." if str(e.get("canonical_id", "")).startswith("phila_ccp") else "ECF No."}}
    if filing_type:
        y = ROOT / "obsidian_sync" / "templates" / "filing_types" / f"{filing_type}.yaml"
        out["filing_type"] = {"path": str(y.relative_to(ROOT)), "exists": y.exists()}
        if y.exists():
            try:
                import yaml as _y
                d = _y.safe_load(y.read_text(encoding="utf-8")) or {}
                out["filing_type"].update({k: d.get(k) for k in ("title", "deadline_days", "court", "blocks", "relief") if k in d})
            except Exception as ex:
                out["filing_type"]["error"] = str(ex)[:120]
    cdir = ROOT / "obsidian_sync" / "cases" / slug / "drafts"
    p3s = sorted(cdir.glob(f"{bundle or '_bundle*'}/motions/chain_*/EFILE_PART3.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if p3s:
        out["efile_part3"] = {"path": str(p3s[0].relative_to(ROOT)), **_j.loads(p3s[0].read_text(encoding="utf-8"))}
    rec = sorted((ROOT / "efiling" / "efiled_receipts").glob(f"*{_re.sub(r'^phila_ccp:', '', str(e.get('canonical_id','')))}*.json"))
    out["efiled_receipts"] = [{"stub": _j.loads(r.read_text()).get("stub"), "efile_no": _j.loads(r.read_text()).get("efile_no"), "confirmation_no": _j.loads(r.read_text()).get("confirmation_no")} for r in rec]
    return out


@mcp.tool
def get_case(canonical_id: str) -> dict:
    """Get a single case from the registry by canonical_id.

    canonical_id format: 'phila_ccp:260100000', 'fed:2:26-cv-00456', etc.
    Returns the full registry entry including docket aliases, parties, judge,
    notes, and related cases. Returns {"error": ...} if not found.
    """
    from pipeline.case_registry import get_registry
    reg = get_registry()
    entry = reg.get_entry(canonical_id)
    if entry is None:
        return {"error": f"canonical_id not found: {canonical_id}"}
    return asdict(entry)


@mcp.tool
def lookup_case_by_docket(docket: str) -> dict:
    """Look up a canonical case by any docket alias.

    Accepts docket numbers in any common form: '260100000', '2024-10-02969',
    '2:26-cv-00456', 'No. 260100000', etc. Returns the matching canonical
    case entry, or {"error": ...} if no match.
    """
    from pipeline.case_registry import get_registry
    reg = get_registry()
    canonical_id = reg.lookup_canonical(docket)
    if not canonical_id:
        return {"error": f"no canonical case matches docket: {docket}"}
    return asdict(reg.get_entry(canonical_id))


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# AUDIT DATA TOOLS
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def _audit_path(canonical_id: str) -> Path:
    """Convert canonical_id to audit JSON filename."""
    safe = canonical_id.replace(":", "_")
    return AUDIT_DIR / f"data_{safe}.json"


@mcp.tool
def get_case_audit(canonical_id: str) -> dict:
    """Get the full mined audit data for a canonical case.

    Returns the cached audit JSON containing:
      - case: title, source folders
      - hierarchy: per-file violations + per-citation classifications
      - dismissal: dismissal-related findings
      - scorecard: total score, violation counts, severity breakdown

    Use this when you need the complete audit picture. For just violations,
    use get_case_violations. For just the scorecard summary, use get_case_score.
    """
    path = _audit_path(canonical_id)
    if not path.exists():
        return {"error": f"audit not found for {canonical_id}", "expected_path": str(path)}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"error": f"failed to read audit: {e}"}


@mcp.tool
def get_case_violations(canonical_id: str, severity: str = "all") -> dict:
    """Get flagged stare-decisis violations for a case.

    Args:
        canonical_id: e.g. 'phila_ccp:260100000'
        severity: 'all' (default), 'fatal', or 'serious'

    Returns a dict with:
      - count: total violations matching severity filter
      - violations: list of violation records (citation, cited_court,
        severity, source_file, issue)

    Fatal = facially non-binding under standard hierarchy rules.
    Serious = binding status requires verification.
    """
    path = _audit_path(canonical_id)
    if not path.exists():
        return {"error": f"audit not found for {canonical_id}"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        all_violations = data.get("scorecard", {}).get("violations", [])
        if severity == "all":
            filtered = all_violations
        else:
            filtered = [v for v in all_violations if v.get("severity") == severity]
        return {
            "canonical_id": canonical_id,
            "count": len(filtered),
            "severity_filter": severity,
            "violations": filtered,
        }
    except Exception as e:
        return {"error": f"failed to parse violations: {e}"}


@mcp.tool
def get_case_score(canonical_id: str) -> dict:
    """Get the scorecard summary for a case.

    Returns score (0-100), rank, total violations, fatal count, serious count,
    and discrete legal issues. Use this for a quick health check on a case
    without pulling the full audit.
    """
    path = _audit_path(canonical_id)
    if not path.exists():
        return {"error": f"audit not found for {canonical_id}"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        sc = data.get("scorecard", {})
        violations = sc.get("violations", [])
        return {
            "canonical_id": canonical_id,
            "score": sc.get("score") or sc.get("total_score", 0),
            "rank": sc.get("rank", "?"),
            "total_violations": len(violations),
            "fatal": sum(1 for v in violations if v.get("severity") == "fatal"),
            "serious": sum(1 for v in violations if v.get("severity") == "serious"),
            "title": sc.get("title", ""),
        }
    except Exception as e:
        return {"error": f"failed to parse scorecard: {e}"}


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# COURT HIERARCHY / CITATION CLASSIFICATION TOOLS
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

@mcp.tool
def classify_citation(citation: str) -> dict:
    """Classify a citation by its issuing court.

    Returns the court code (e.g. 'PA_SUPREME', 'CA3', 'EDPA', 'PA_CP_OTHER',
    'DPA_OTHER', 'UNKNOWN'). This is the structural classification used to
    determine binding authority â€” it does NOT verify that the case exists or
    remains good law.
    """
    from pipeline.court_hierarchy import classify_court
    try:
        court_code = classify_court(citation)
        return {"citation": citation, "court_code": court_code}
    except Exception as e:
        return {"error": f"classification failed: {e}", "citation": citation}


@mcp.tool
def is_binding_on_court(cited_court: str, target_court: str) -> dict:
    """Determine whether a cited court binds a target court under stare decisis.

    Args:
        cited_court: court code of the cited decision (e.g. 'PA_CP_OTHER')
        target_court: court code where the case is pending (e.g. 'PA_CP_PHILA')

    Returns:
        binding: True/False
        cited_court / target_court echoed back

    This is structural only â€” it does not address whether the cited case
    remains good law.
    """
    from pipeline.court_hierarchy import is_binding
    try:
        result = is_binding(cited_court, target_court)
        return {
            "cited_court": cited_court,
            "target_court": target_court,
            "binding": bool(result),
        }
    except Exception as e:
        return {"error": f"is_binding failed: {e}"}


@mcp.tool
def audit_citation(citation: str, target_court: str) -> dict:
    """Full audit of a single citation against a target court.

    Combines classify_citation + is_binding_on_court and returns the
    severity classification (fatal, serious, ok). Use this when you have
    one specific citation to test, rather than running a whole case audit.
    """
    from pipeline.court_hierarchy import audit_citation as _audit
    try:
        result = _audit(citation, target_court)
        if hasattr(result, "__dict__"):
            return asdict(result) if hasattr(result, "__dataclass_fields__") else dict(result.__dict__)
        return {"result": str(result)}
    except Exception as e:
        return {"error": f"audit_citation failed: {e}", "trace": traceback.format_exc()[:500]}


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# DETERMINISTIC ENGINES (definitions, IF/THEN chains)
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

@mcp.tool
def build_definitions(issue: str) -> dict:
    """Build the locked legal definition set for a legal issue.

    Args:
        issue: one of 'verification', 'garnishment', 'default_judgment',
               'fcra', 'fdcpa', 'bad_faith', 'rule_60b', 'twombly_iqbal',
               'removal', 'quiet_title', 'hierarchy', 'service', 'discovery'

    Returns the locked definitions used VERBATIM by Beezle's drafter.
    These are NEVER LLM-generated â€” they come from a curated definition
    catalog. Use this when building an argument for a specific issue and
    you need the controlling terminology.
    """
    from pipeline.definition_engine import build_definition_set
    try:
        defs = build_definition_set(issue)
        return {
            "issue": issue,
            "count": len(defs),
            "definitions": [
                {
                    "term": getattr(d, "term", ""),
                    "definition": getattr(d, "definition", ""),
                    "source": getattr(d, "source", ""),
                }
                for d in defs
            ],
        }
    except Exception as e:
        return {"error": f"build_definitions failed: {e}", "issue": issue}


@mcp.tool
def build_logic_chain(
    rule_text: str,
    rule_authority: str,
    facts: list[str],
    issue: str,
    attack_vector: str = "application",
) -> dict:
    """Build a deterministic IF/AND/THEN logic chain.

    Args:
        rule_text: the controlling rule text (e.g. 'Pa.R.C.P. 1024 requires verification at filing')
        rule_authority: citation anchoring the rule (e.g. 'Pa.R.C.P. 1024')
        facts: list of fact strings establishing the conditions
        issue: legal issue bucket (e.g. 'verification', 'garnishment')
        attack_vector: 'application' (default), 'timing', 'scope', etc.

    Returns the deterministic chain (IF clause, AND clause, THEN clause)
    with citation anchoring. NEVER LLM-generated. Use this to scaffold an
    argument that the user (or Hermes) will then explain in prose.
    """
    from pipeline.if_then_engine import build_logic_chain as _build
    try:
        link = _build(
            rule_text=rule_text,
            rule_authority=rule_authority,
            facts=facts,
            issue=issue,
            attack_vector=attack_vector,
        )
        if link is None:
            return {"error": "logic chain could not be built", "issue": issue}
        return {
            "issue": issue,
            "attack_vector": attack_vector,
            "if_clause": getattr(link, "if_clause", ""),
            "and_clause": getattr(link, "and_clause", ""),
            "then_clause": getattr(link, "then_clause", ""),
            "rule_authority": rule_authority,
        }
    except Exception as e:
        return {"error": f"build_logic_chain failed: {e}", "trace": traceback.format_exc()[:500]}


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# RETRIEVAL TOOLS (ChromaDB + CourtListener)
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

_RETR_CTX = None


def _retr_ctx():
    """Cache the full-corpus retrieval context once (loaded in the long-running MCP process)."""
    global _RETR_CTX
    if _RETR_CTX is None:
        import sqlite3 as _sq, json as _js, turbovec as _tv
        from sentence_transformers import SentenceTransformer
        from pipeline.case_comprehension import NEW_INDEX, NEW_IDS, CHUNK_DB
        idx = _tv.TurboQuantIndex.load(str(NEW_INDEX))
        ids = _js.loads(NEW_IDS.read_text())
        con = _sq.connect(str(CHUNK_DB), check_same_thread=False)
        # device="cpu": decouple MCP corpus retrieval from the local-gpu's draft load so the
        # embedder never starves/hangs while m0 is drafting (root cause of "MCP unreachable"
        # blocks on the local-gpu swarm lane). No re-embed needed — model/dim unchanged.
        model = SentenceTransformer("BAAI/bge-base-en-v1.5", device="cpu")
        _RETR_CTX = (idx, ids, con, model)
    return _RETR_CTX


@mcp.tool
def search_chunks(query: str, n_results: int = 6, top_k: int = 0, k: int = 0, limit: int = 0) -> dict:
    """Search the FULL Beezle legal corpus (3M+ chunks) — semantic over the whole index + the recent
    re-embed delta + authoritative statute/rule/instruction text (FTS). Grounds Hermes exactly like m0.

    Args:
        query: natural-language search query
        n_results: number of results to return (default 6, max 50). top_k / k / limit are accepted aliases.

    Returns top-N chunks with text + source. Use for raw legal grounding: statutes, rules, cases,
    treatises, regulations, IRS forms — anything ingested. Cite the sources; never invent.
    """
    n = max(1, min(top_k or k or limit or n_results, 50))  # aliases: agents pass top_k/k/limit and burned a full LLM round per rejected call
    try:
        from pipeline.case_comprehension import _retrieve
        idx, ids, con, model = _retr_ctx()
        hits = _retrieve(idx, ids, con, model, query, k=n)
        import hashlib
        # chunk_id FIRST and stable (source + text hash): without it lanes cited result ordinals ("chunk 43-41") as ids
        hits = [{"chunk_id": f"{h.get('source','?')}#{hashlib.sha1((h.get('text') or '').encode()).hexdigest()[:10]}", **h} for h in hits]
        return {"query": query, "n_results": len(hits), "result": hits,
                "retrieval_mode": "full_corpus_hybrid",
                "cite_as": "[corpus:<chunk_id>] — copy chunk_id verbatim; quote only words present in that chunk's text"}
    except Exception as e:
        try:
            from pipeline.retriever import retrieve
            return {"query": query, "n_results": n, "result": retrieve(query, final_k=n),
                    "retrieval_mode": "fallback"}
        except Exception as e2:
            return {"error": f"search_chunks failed: {e}; fallback: {e2}"}


@mcp.tool
def read_graph(query: str, k: int = 5, limit: int = 0) -> dict:
    """Read Beezle's Obsidian knowledge graph BY MEANING — returns the most relevant accumulated, linked
    notes (prior grounded understanding) for a query, so reasoning builds on what Beezle already learned
    instead of starting cold. Cite the [[slug]] and verify against the law (search_chunks).
    """
    try:
        from pipeline.connected_notes import semantic_search_notes
        n = limit if limit else k          # agents commonly send `limit`; accept both
        hits = semantic_search_notes(query, max(1, min(n, 12)))
        return {"query": query, "notes": [
            {"slug": h["slug"], "title": h["title"], "score": round(h["score"], 3),
             "text": (h["text"] or "")[:1200]} for h in hits]}
    except Exception as e:
        return {"error": f"read_graph failed: {e}"}



@mcp.tool
def write_obsidian_note(
    subdir: str,
    filename: str,
    content: str,
    overwrite: bool = False,
) -> dict:
    """Write a markdown note to the Beezle obsidian_sync vault.

    Args:
        subdir: subdirectory under obsidian_sync/, e.g. 'learning/treasury',
                'definitions_learned', 'queries/irs'. Forward slashes only.
        filename: file name including .md extension, e.g. 'acceptance_for_value.md'.
        content: full markdown content. Caller provides frontmatter + body.
        overwrite: if False (default), refuse if file already exists.

    Returns:
        {"ok": bool, "path": str, "bytes_written": int} on success
        {"ok": false, "error": str, "path": str} on failure
    """
    from pathlib import Path
    VAULT = (_eroot() / "obsidian_sync")

    # Path safety
    if ".." in subdir or subdir.startswith("/") or subdir.startswith("\\"):
        return {"ok": False, "error": "subdir traversal blocked", "subdir": subdir}
    if ".." in filename or "/" in filename or "\\" in filename:
        return {"ok": False, "error": "filename must not contain path separators", "filename": filename}
    if not filename.endswith(".md"):
        return {"ok": False, "error": "filename must end with .md", "filename": filename}

    target = VAULT / subdir / filename

    # Ensure under vault
    try:
        target.resolve().relative_to(VAULT.resolve())
    except ValueError:
        return {"ok": False, "error": "resolved path escapes vault", "path": str(target)}

    target.parent.mkdir(parents=True, exist_ok=True)

    if target.exists() and not overwrite:
        return {
            "ok": False,
            "error": "file_exists",
            "path": str(target),
            "existing_size": target.stat().st_size,
            "hint": "pass overwrite=true to replace",
        }

    try:
        target.write_text(content, encoding="utf-8")
        return {
            "ok": True,
            "path": str(target),
            "bytes_written": len(content.encode("utf-8")),
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "path": str(target)}


@mcp.tool
def retrieve_with_expansion(query: str, jurisdiction: str = "PA", final_k: int = 8) -> dict:
    """Retrieve legal authority with query expansion + jurisdiction filtering.

    Higher-level than search_chunks: expands the query, retrieves from
    ChromaDB, filters by jurisdiction, and returns the top-K most relevant
    results assembled into a context block. Use this when you want
    Beezle's full retrieval pipeline rather than raw chunk search.
    """
    from pipeline.retriever import retrieve
    try:
        results = retrieve(query=query, jurisdiction=jurisdiction, final_k=final_k)
        if isinstance(results, list):
            return {"query": query, "jurisdiction": jurisdiction, "count": len(results), "results": results}
        return results
    except Exception as e:
        return {"error": f"retrieve failed: {e}"}


@mcp.tool
def search_case_law(query: str, court: str = "", n_results: int = 5) -> dict:
    """Search CourtListener for case law (live API call).

    Args:
        query: search query (e.g. 'verification at time of filing Pa.R.C.P. 1024')
        court: optional CourtListener court ID filter (e.g. 'pasup', 'ca3')
        n_results: number of results (default 5, max 20)

    Returns matching cases from CourtListener with citation, court, date,
    and snippet. Requires COURTLISTENER_API_KEY env var to be set.
    """
    from pipeline.authority_discovery import search_case_law as _search
    n = max(1, min(n_results, 20))
    try:
        results = _search(query=query, court=court, n_results=n)
        return {"query": query, "court_filter": court, "count": len(results), "results": results}
    except Exception as e:
        return {"error": f"search_case_law failed: {e}"}


@mcp.tool
def discover_authority(
    query: str,
    subject_areas: Optional[list[str]] = None,
    jurisdiction: str = "PA",
    include_case_law: bool = True,
) -> dict:
    """Combined authority discovery: ChromaDB + CourtListener.

    Args:
        query: search query
        subject_areas: optional list of subject area filters
        jurisdiction: 'PA', 'federal', 'CA3', etc. (default 'PA')
        include_case_law: whether to also hit CourtListener (default True)

    Returns combined authority results from local DB + live case law,
    deduped and ranked. This is what Beezle's main pipeline uses for
    authority discovery during a brief generation run.
    """
    from pipeline.authority_discovery import discover_authority as _discover
    try:
        return _discover(
            query=query,
            subject_areas=subject_areas or [],
            jurisdiction=jurisdiction,
            include_case_law=include_case_law,
        )
    except Exception as e:
        return {"error": f"discover_authority failed: {e}"}


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# DRAFTING TOOLS (idempotent â€” re-runnable, writes to output/reply_briefs/)
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

@mcp.tool
def draft_reply_brief(canonical_id: str, with_scorecard: bool = True, filing_type: str = "") -> dict:
    """Generate a filing-grade reply brief for a canonical case.

    Calls the hotfix7 deterministic drafter (reply_brief_drafter.draft_for_case).
    Output:
      - .md (markdown for review)
      - .docx (filing document)
      - scorecard exhibit .docx (separate Exhibit A)

    All three files are written to output/reply_briefs/ with timestamped
    filenames. Returns the absolute paths.

    The brief is 100% deterministic â€” no LLM calls. Caption, IF/THEN chains,
    per-citation rebuttals, definitions, and Section IV are all built from
    cached audit data + locked engines. Re-running with the same canonical_id
    is safe; it produces a new timestamped file.
    """
    from beezle.generation.drafter import draft_for_case
    try:
        md_path, docx_path, sc_path = draft_for_case(
            canonical_id=canonical_id,
            base_path=BASE_PATH,
            output_dir=REPLY_BRIEFS_DIR,
            with_scorecard=with_scorecard,
            filing_type=filing_type if filing_type else None,
        )
        return {
            "canonical_id": canonical_id,
            "filing_type": filing_type or "reply_brief",
            "status": "complete",
            "md_path": str(md_path),
            "docx_path": str(docx_path),
            "scorecard_path": str(sc_path) if sc_path else "",
        }
    except Exception as e:
        return {
            "error": f"draft_reply_brief failed: {e}",
            "trace": traceback.format_exc()[:1000],
            "canonical_id": canonical_id,
        }


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# OBSIDIAN BRIDGE (Layer 2 prep â€” read-only access to the legal vault)
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

@mcp.tool
def list_obsidian_notes(subdir: str = "") -> dict:
    """List markdown notes in the Beezle-managed Obsidian vault.

    Args:
        subdir: optional subdirectory under obsidian_sync/ (e.g. 'beezle_managed',
                'decisions', 'tool_usage'). Empty string lists the vault root.

    Returns a list of note paths. Use this to discover what's in the vault
    before reading specific notes.
    """
    base = OBSIDIAN_VAULT / subdir if subdir else OBSIDIAN_VAULT
    if not base.exists():
        return {"error": f"path not found: {base}"}
    try:
        notes = sorted(p.relative_to(OBSIDIAN_VAULT).as_posix() for p in base.rglob("*.md"))
        return {"vault_root": str(OBSIDIAN_VAULT), "subdir": subdir, "count": len(notes), "notes": notes[:200]}
    except Exception as e:
        return {"error": f"list_obsidian_notes failed: {e}"}


@mcp.tool
def read_obsidian_note(note_path: str) -> dict:
    """Read a single markdown note from the Obsidian vault.

    Args:
        note_path: path relative to obsidian_sync/, e.g. 'beezle_managed/case_xxx.md'

    Returns the full markdown content. SAFETY: only reads files inside the
    obsidian_sync/ vault â€” paths outside are rejected.
    """
    target = (OBSIDIAN_VAULT / note_path).resolve()
    try:
        target.relative_to(OBSIDIAN_VAULT.resolve())
    except ValueError:
        return {"error": "path escapes obsidian vault â€” refused"}
    if not target.exists():
        return {"error": f"note not found: {note_path}"}
    if not target.is_file() or target.suffix != ".md":
        return {"error": "not a markdown file"}
    try:
        content = target.read_text(encoding="utf-8")
        return {"path": note_path, "size": len(content), "content": content}
    except Exception as e:
        return {"error": f"read failed: {e}"}


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# SERVER LAUNCH
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•


@mcp.tool
def draft_document(
    intent: str,
    user_input: str,
    court: str = "federal_district",
    jurisdiction: str = "EDPA",
    documents: Optional[list[str]] = None,
    damages: str = "",
) -> dict:
    """Draft a legal document. Hermes calls this to generate complaints, motions, briefs, etc.
    
    Args:
        intent: Type of document (complaint, reply_brief, motion, appeal, scorecard, amended_complaint, cert_petition)
        user_input: Natural language description of what to draft
        court: Court type (federal_district, state_ccp, federal_appellate, scotus)
        jurisdiction: Specific jurisdiction (EDPA, PA_CP_PHILA, CA3, SCOTUS)
        documents: Optional list of file paths to uploaded documents
        damages: Optional damages amount requested
    
    Returns: dict with files list, status, classified_documents, and any questions if info is missing
    """
    import asyncio
    from core.workflow_router import route_draft

    request_data = {
        "intent": intent,
        "user_input": user_input,
        "court": court,
        "jurisdiction": jurisdiction,
        "documents": documents or [],
        "damages": damages,
    }

    # Run async route_draft synchronously
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                result = pool.submit(asyncio.run, route_draft(request_data)).result()
        else:
            result = asyncio.run(route_draft(request_data))
    except RuntimeError:
        result = asyncio.run(route_draft(request_data))

    return result


@mcp.tool
def query_legal_db(query: str, n_results: int = 5) -> dict:
    """Query the legal database for statutes, rules, case law, and procedural requirements.
    Use this to find: praecipe rules, motion requirements, filing procedures, statutory elements,
    court rules, CFR regulations, or any legal authority.
    
    Args:
        query: Natural language legal query (e.g. 'praecipe filing requirements Pennsylvania',
               'FOIA constructive denial elements', 'motion for default judgment federal court')
        n_results: Number of results to return (default 5, max 15)
    
    Returns: dict with results containing document text, source, and relevance
    """
    from pipeline.db_connector import LegalDB

    db = LegalDB()
    n = min(max(n_results, 1), 15)
    hits = db.query_text(query, n_results=n)

    results = []
    for h in hits:
        results.append({
            "source": h.get("metadata", {}).get("source", "unknown"),
            "text": h.get("document", "")[:500],
            "distance": h.get("distance", 0),
        })

    return {
        "query": query,
        "total_results": len(results),
        "results": results,
        "db_size": db.get_count(),
    }


@mcp.tool
def federal_register_search(term: str, agency: str = "", doc_type: str = "", n_results: int = 5) -> dict:
    """Search the FEDERAL REGISTER (official U.S. gov publication) for an agency's OWN published rules/
    notices — supporting authority. Cite results as [official]. Use it to: (1) supply the "your own policy"
    rung of an AGENCY defendant's proof-of-fact questionnaire (its published rule at X FR / X CFR that
    confers the duty); (2) attack agency action under 5 U.S.C. 552(a)(1) — a matter required to be published
    in the FR but not published cannot adversely affect a person — or a 5 U.S.C. 553 notice-and-comment
    defect; (3) defeat immunity — show delegated authority + its LIMITS (ultra vires acts fall outside
    official immunity) and APA reviewability (5 U.S.C. 702 waives sovereign immunity for non-monetary relief).
    Args: term (search words); agency (optional slug, e.g. 'housing-and-urban-development-department',
    'consumer-financial-protection-bureau'); doc_type (optional 'RULE'|'PRORULE'|'NOTICE'|'PRESDOCU');
    n_results (default 5, max 20). Returns {term,count,returned,results:[{type,agency,title,citation,
    publication_date,document_number,url,abstract}]}. Read-only; official source; nothing invented."""
    import urllib.request
    import urllib.parse
    n = min(max(int(n_results or 5), 1), 20)
    params = [("per_page", str(n)), ("order", "relevance"), ("conditions[term]", term)]
    for fld in ("title", "type", "abstract", "document_number", "publication_date", "citation",
                "html_url", "agency_names"):
        params.append(("fields[]", fld))
    if agency:
        params.append(("conditions[agencies][]", agency))
    if doc_type:
        params.append(("conditions[type][]", doc_type.upper()))
    url = "https://www.federalregister.gov/api/v1/documents.json?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Beezle-legal/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"error": f"federal_register fetch failed: {e}", "term": term, "results": []}
    out = []
    for x in (data.get("results") or []):
        out.append({
            "type": x.get("type", ""),
            "agency": (x.get("agency_names") or ["?"])[0] if x.get("agency_names") else "?",
            "title": x.get("title", ""),
            "citation": x.get("citation", ""),
            "publication_date": x.get("publication_date", ""),
            "document_number": x.get("document_number", ""),
            "url": x.get("html_url", ""),
            "abstract": (x.get("abstract") or "")[:600],
        })
    return {"term": term, "count": data.get("count", 0), "returned": len(out), "results": out}


@mcp.tool
def classify_uploaded_documents(file_paths: list[str]) -> dict:
    """Classify uploaded documents and assign exhibit letters.
    Reads each file, determines type (court order, motion, FOIA request, email, etc.),
    and assigns exhibit letters A, B, C...
    
    Args:
        file_paths: List of file paths to classify
    
    Returns: dict with classified documents including type, exhibit letter, and preview
    """
    from core.workflow_router import classify_documents

    classified = classify_documents(file_paths)
    return {
        "total": len(classified),
        "documents": classified,
    }


@mcp.tool  
def get_filing_requirements(court: str, document_type: str) -> dict:
    """Get filing requirements for a specific court and document type.
    Queries the legal database for rules about formatting, service, verification,
    page limits, deadlines, and other procedural requirements.
    
    Args:
        court: Court identifier (EDPA, PA_CP_PHILA, CA3, SCOTUS)
        document_type: Type of filing (complaint, motion, brief, praecipe, preliminary_objections)
    
    Returns: dict with applicable rules and requirements
    """
    from pipeline.db_connector import LegalDB

    db = LegalDB()

    # Build targeted queries
    queries = [
        f"{document_type} filing requirements {court}",
        f"{document_type} format rules federal court" if "EDPA" in court or "CA" in court else f"{document_type} format rules Pennsylvania",
        f"{document_type} service requirements",
        f"{document_type} verification requirement",
    ]

    all_results = []
    for q in queries:
        hits = db.query_text(q, n_results=3)
        for h in hits:
            all_results.append({
                "query": q,
                "source": h.get("metadata", {}).get("source", "unknown"),
                "text": h.get("document", "")[:400],
            })

    return {
        "court": court,
        "document_type": document_type,
        "requirements": all_results[:8],
        "note": "These are from the legal database. Verify against current court rules.",
    }



@mcp.tool
def discover_case_type(situation: str, n_results: int = 10) -> dict:
    """Discover the correct case type, statutes, and template by querying the legal database
    and Obsidian. Use this BEFORE drafting to find what laws apply to a situation.
    
    Do NOT rely on hardcoded case types. Always query the DB to discover:
    - Applicable statutes
    - Required elements  
    - Available templates
    - Filing requirements
    - Damages provisions
    
    Args:
        situation: Description of the legal situation (e.g. 'FOIA request denied by federal agency',
                   'insurance company charging premiums after policy cancelled')
        n_results: Number of DB results to examine
    
    Returns: dict with discovered case type, statutes, elements, templates, and recommended counts
    """
    from pipeline.db_connector import LegalDB
    from pathlib import Path as _Path
    import json as _json
    
    db = LegalDB()
    
    # Query DB for relevant statutes and rules
    statute_hits = db.query_text(f"{situation} statute elements requirements", n_results=n_results)
    template_hits = db.query_text(f"{situation} complaint template", n_results=3)
    
    # Check Obsidian templates
    obsidian_templates = []
    templates_dir = _Path(str(_eroot() / "obsidian_sync/templates"))
    if templates_dir.exists():
        for f in templates_dir.glob("*.md"):
            content = f.read_text(encoding="utf-8")[:500]
            if any(kw in content.lower() for kw in situation.lower().split()[:3]):
                obsidian_templates.append({
                    "file": f.name,
                    "preview": content[:200],
                })
    
    # Check legal_config for matching case types
    config_matches = []
    config_path = _Path(str(_eroot() / "dev/legal_config_updated.json"))
    if config_path.exists():
        config = _json.loads(config_path.read_text(encoding="utf-8"))
        for ct_name, ct_data in config.get("case_types", {}).items():
            keywords = ct_data.get("keywords", [])
            if any(kw in situation.lower() for kw in keywords):
                config_matches.append({
                    "case_type": ct_name,
                    "name": ct_data.get("name", ""),
                    "statutes": ct_data.get("statutes", []),
                    "counts": ct_data.get("counts", []),
                    "court": ct_data.get("court", ""),
                    "rules": ct_data.get("rules", []),
                })
    
    # Format results
    statutes_found = []
    for h in statute_hits[:n_results]:
        statutes_found.append({
            "source": h.get("metadata", {}).get("source", "unknown"),
            "text": h.get("document", "")[:300],
        })
    
    templates_found = []
    for h in template_hits[:3]:
        templates_found.append({
            "source": h.get("metadata", {}).get("source", "unknown"),
            "text": h.get("document", "")[:300],
        })
    
    return {
        "situation": situation,
        "config_matches": config_matches,
        "obsidian_templates": obsidian_templates,
        "statutes_from_db": statutes_found,
        "templates_from_db": templates_found,
        "db_size": db.get_count(),
        "instruction": "Use these results to determine the correct legal framework. Do NOT guess statutes â€” use what the DB returns.",
    }



# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# STATE-BASED PIPELINE TOOLS
# Each step reads state from disk, does ONE thing, saves state
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

@mcp.tool
def pipeline_intake(case_id: str, plaintiff_name: str, plaintiff_address: str,
                    defendant_names: str, case_description: str,
                    damages: str = "", court: str = "EDPA") -> dict:
    """STEP 1: Create case state on disk. V3 with defendant parsing."""
    from pipeline.intake_pipeline import pipeline_intake_v3
    return pipeline_intake_v3(case_id, plaintiff_name, plaintiff_address,
                              defendant_names, case_description, damages, court)
def pipeline_classify(case_id: str) -> dict:
    """STEP 2: Classify the case type using DB + config. Reads state from Step 1.
    Locks case type. Must pass before drafting.
    """
    from core.case_state import load_state, save_state
    
    state = load_state(case_id)
    if not state:
        return {"error": f"No state found for {case_id}. Run pipeline_intake first."}
    
    description = " ".join(state["facts"])
    
    # HARD OVERRIDES â€” these must match before keyword scoring
    dl = description.lower()
    if any(kw in dl for kw in ["foia", "freedom of information", "5 u.s.c. 552", "552a", "records request", "constructive denial"]):
        state["case_type"] = "foia"
        state["case_type_confidence"] = 1.0
        state["statutes"] = ["5 U.S.C. 552", "5 U.S.C. 552a", "28 U.S.C. 1361"]
        state["step"] = "classified"
        state["locked"] = True
        from core.case_state import save_state as _ss
        _ss(case_id, state)
        return {"status": "classified", "case_type": "foia", "confidence": 1.0, "next_step": "pipeline_draft"}
    
    # Use discover logic
    from pipeline.db_connector import LegalDB
    import json as _json
    from pathlib import Path as _P
    
    # Check config matches
    config_path = _P(str(_eroot() / "dev/legal_config_updated.json"))
    matches = []
    if config_path.exists():
        config = _json.loads(config_path.read_text(encoding="utf-8-sig"))
        for ct_name, ct_data in config.get("case_types", {}).items():
            keywords = ct_data.get("keywords", [])
            score = sum(1 for kw in keywords if kw.lower() in description.lower())
            if score > 0:
                matches.append((ct_name, score, ct_data))
    
    if matches:
        matches.sort(key=lambda x: x[1], reverse=True)
        best = matches[0]
        state["case_type"] = best[0]
        state["case_type_confidence"] = min(best[1] / 3.0, 1.0)
        state["statutes"] = best[2].get("statutes", [])
        state["step"] = "classified"
        state["locked"] = state["case_type_confidence"] >= 0.7
    else:
        state["case_type"] = "unknown"
        state["case_type_confidence"] = 0.0
        state["step"] = "classification_failed"
    
    save_state(case_id, state)
    
    if state["locked"]:
        return {"status": "classified", "case_type": state["case_type"], 
                "confidence": state["case_type_confidence"], "next_step": "pipeline_draft"}
    else:
        return {"status": "needs_confirmation", "case_type": state["case_type"],
                "confidence": state["case_type_confidence"],
                "question": f"Is this a {state['case_type']} case? Confirm to proceed."}


@mcp.tool
def pipeline_draft(case_id: str) -> dict:
    """STEP 3: Generate complaint .docx from state. V3 with FOIA builder."""
    from pipeline.draft_pipeline import pipeline_draft_v3
    return pipeline_draft_v3(case_id)
def _build_claims_for_type(case_type: str, state: dict) -> list:
    """Build claims from Obsidian (first) + DB + LLM structuring. Dynamic."""
    # CHECK OBSIDIAN FIRST for pre-structured templates
    from pathlib import Path as _P2
    obsidian_dir = _P2(str(_eroot() / "obsidian_sync/templates"))
    ct_file = obsidian_dir / f"{case_type}_case_type.md"
    if ct_file.exists():
        try:
            from core.obsidian_parser import parse_obsidian_counts
            obs_claims = parse_obsidian_counts(str(ct_file))
            if obs_claims and len(obs_claims) > 0:
                # Inject facts from state into each count
                for c in obs_claims:
                    c["facts"] = state.get("facts", [])
                return obs_claims
        except Exception:
            pass  # Fall through to DB + LLM
    import json as _json, requests
    from pipeline.db_connector import LegalDB
    from pathlib import Path as _P

    # 1. Load statutes for this case type from config
    config_path = _P(str(_eroot() / "dev/legal_config_updated.json"))
    statutes = state.get("statutes", [])
    count_names = []
    
    if config_path.exists():
        try:
            config = _json.loads(config_path.read_text(encoding="utf-8-sig"))
            ct_data = config.get("case_types", {}).get(case_type, {})
            if not statutes:
                statutes = ct_data.get("statutes", [])
            count_names = ct_data.get("counts", [])
        except Exception:
            pass

    if not statutes:
        return []

    # 2. Query DB for each statute â€” get duties, elements, requirements
    db = LegalDB()
    claims = []
    facts_text = " ".join(state.get("facts", []))[:1500]

    for i, statute in enumerate(statutes):
        # Query DB for this specific statute
        raw_chunks = db.query_text(f"{statute} duties elements requirements shall must", n_results=5)
        raw_text = "\n".join([h.get("text", h.get("document", ""))[:300] for h in raw_chunks])

        if not raw_text.strip():
            continue

        # 3. Small LLM call to structure DB results into claims JSON
        structured = _structure_one_count(
            statute=statute,
            raw_law=raw_text[:2000],
            facts=facts_text,
            count_number=i + 1,
        )
        if structured:
            claims.append(structured)

    return claims


def _structure_one_count(statute: str, raw_law: str, facts: str, count_number: int) -> dict:
    """Use LLM to structure raw DB text into one claim. Few-shot with concrete example."""
    import requests, json as _json

    example = '{"title":"VIOLATION","statute":"X","controlling_law":["statute: shall language"],"duties":[{"text":"Agency SHALL act","citation":"section"}],"elements":[{"desc":"element","if":"requirement","and":"fact"}],"facts":["fact1"],"breach_duty":"duty","breach_conduct":"conduct","decision_trigger":{"if":"duties","and":"elements","then":"Court SHALL act"},"relief":["relief1"]}'

    prompt = f"Given this law:\n{raw_law[:1200]}\n\nCase facts: {facts[:500]}\n\nOutput JSON for {statute} using this schema: {example}"

    try:
        r = requests.post("http://localhost:8000/v1/chat/completions", json={
            "model": os.environ.get("ENTROPY_MODEL", "local-model"),
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 4096,
            "temperature": 0.1,
        }, timeout=120)

        text = r.json()["choices"][0]["message"]["content"].strip()

        # Clean markdown fences
        if text.startswith("\u0060\u0060\u0060"):
            lines = text.split("\n")
            text = "\n".join(lines[1:])
        if text.endswith("\u0060\u0060\u0060"):
            text = text[:-3].strip()
        if text.startswith("json"):
            text = text[4:].strip()

        parsed = _json.loads(text)
        # Validate minimum quality
        if len(parsed.get("duties", [])) < 1 or len(parsed.get("elements", [])) < 1:
            raise ValueError("Insufficient structure")
        return parsed
    except Exception as e:
        return {
            "title": f"VIOLATION - {statute}",
            "statute": statute,
            "controlling_law": [statute],
            "duties": [{"text": f"Compliance with {statute}", "citation": statute}],
            "elements": [{"desc": "Statutory requirements met", "if": "Requirements apply", "and": "Facts establish violation"}],
            "facts": [facts[:200] if facts else "See complaint"],
            "breach_duty": statute,
            "breach_conduct": "Failed to comply with statutory requirements",
            "decision_trigger": {"if": "duties apply", "and": "elements satisfied", "then": f"Court SHALL enforce {statute}"},
            "relief": ["Appropriate relief under the statute"],
        }

@mcp.tool
def draft_complaint(case_id: str, plaintiff_name: str, plaintiff_address: str,
                    defendant_names: str, case_description: str,
                    damages: str = "", court: str = "EDPA") -> dict:
    """ONE-SHOT complaint drafter. Does intake + classify + draft in a single call.
    Use this instead of calling pipeline_intake/classify/draft separately."""
    import sys
    base_path = _eroot() if sys.platform == "win32" else Path(__file__).parent
    if str(base_path) not in sys.path:
        sys.path.insert(0, str(base_path))
    
    from pipeline.intake_pipeline import pipeline_intake_v3
    from pipeline.draft_pipeline import pipeline_draft_v3
    
    # Step 1: Intake
    r1 = pipeline_intake_v3(case_id, plaintiff_name, plaintiff_address,
                            defendant_names, case_description, damages, court)
    if r1.get("error"):
        return {"error": f"Intake failed: {r1['error']}"}
    
    # Step 2: Classify
    try:
        r2 = pipeline_classify(case_id)
    except Exception as e:
        return {"error": f"Classify failed: {e}"}
    
    # Step 3: Draft
    r3 = pipeline_draft_v3(case_id)
    if r3.get("error"):
        return {"error": f"Draft failed: {r3['error']}"}
    
    return {
        "status": "complaint_generated",
        "case_id": case_id,
        "case_type": r2.get("case_type", "unknown"),
        "file": r3.get("file", ""),
        "size": r3.get("size", 0),
        "defendants_count": r1.get("defendants_count", 0),
        "message": f"Complaint generated at {r3.get('file', '')}"
    }

# ---------------------------------------------------------------------------
# UNIFIED FILING COMPOSER (Session 10 — replaces fragmented drafting paths)
# ---------------------------------------------------------------------------

@mcp.tool
def compose_and_draft(
    case_id: str,
    filing_type: str,
    with_scorecard: bool = False,
    extra_context: str = "",
) -> dict:
    """Unified filing tool. Composes a filing using YAML templates + case audit data,
    renders to .docx with proper court caption.

    This replaces calling draft_reply_brief for non-reply-brief filings.
    The YAML template controls which argument blocks fire based on case defects.
    Scorecard only attaches when with_scorecard=True (stare decisis filings).

    Args:
        case_id: canonical case id (e.g. 'fed:2:26-cv-00123')
        filing_type: matches a YAML template in filing_types/ (e.g. 'motion_for_reconsideration')
        with_scorecard: attach scorecard exhibit (only for stare decisis filings)
        extra_context: additional instructions or facts for the filing (optional)

    Returns: dict with md_path, docx_path, scorecard_path, composition summary
    """
    import json as _json
    from datetime import datetime as _dt
    from pathlib import Path as _Path
    from dataclasses import asdict as _asdict

    try:
        # ---- Step 1: Compose using pipeline.filing_composer (was filing_composer_v2) ----
        import sys as _sys
        if str(BASE_PATH) not in _sys.path:
            _sys.path.insert(0, str(BASE_PATH))
        from pipeline.filing_composer import (
            compose_filing, render_composition_to_markdown,
            build_context, load_filing_type, recommend_filing_types,
        )

        composition = compose_filing(case_id, filing_type)

        if composition.errors:
            return {
                "error": f"compose_filing failed: {composition.errors}",
                "case_id": case_id,
                "filing_type": filing_type,
            }

        # ---- Step 2: Render to markdown ----
        md_text = render_composition_to_markdown(composition)
        # Inject extra_context from Hermes as a custom argument section
        if extra_context and extra_context.strip():
            md_text += chr(10) + "## CASE-SPECIFIC ARGUMENTS" + chr(10) + chr(10)
            md_text += extra_context.strip() + chr(10)


        if not md_text.strip():
            return {
                "error": "composition produced empty markdown — check block templates",
                "case_id": case_id,
                "filing_type": filing_type,
                "blocks_composed": len(composition.blocks),
                "blocks_skipped": len(composition.skipped_blocks),
                "skipped_details": composition.skipped_blocks,
            }

        # ---- Step 3: Write markdown ----
        ts = _dt.now().strftime("%Y%m%d_%H%M%S")
        slug = case_id.replace(":", "_").replace("/", "_")
        md_filename = f"{filing_type}_{slug}_{ts}.md"
        docx_filename = f"{filing_type}_{slug}_{ts}.docx"

        out_dir = REPLY_BRIEFS_DIR
        out_dir.mkdir(parents=True, exist_ok=True)

        md_path = out_dir / md_filename
        md_path.write_text(md_text, encoding="utf-8")

        # ---- Step 4: Convert to .docx with proper formatting ----
        try:
            # Court ID lookup: try registry first, default EDPA
            court_id = "EDPA"
            try:
                ctx = build_context(case_id)
                registry = ctx.get("registry", {}) if isinstance(ctx, dict) else {}
                court_id = registry.get("court_id") or registry.get("court") or "EDPA"
                # normalize legacy court names
                if "EASTERN DISTRICT" in str(court_id).upper():
                    court_id = "EDPA"
                elif "THIRD CIRCUIT" in str(court_id).upper() or "3D CIR" in str(court_id).upper():
                    court_id = "3D_CIR"
                elif "COMMON PLEAS" in str(court_id).upper() or "PHILADELPHIA" in str(court_id).upper():
                    court_id = "PA_CP_PHILA"
                elif "SUPREME COURT" in str(court_id).upper():
                    court_id = "SCOTUS"
            except Exception:
                court_id = "EDPA"

            # Pull ground truth if available (case-agnostic)
            try:
                import yaml as _yaml
                _slug = case_id.replace(":", "_").replace("/", "_").replace("fed_2_", "fed_")
                _gt_path = OBSIDIAN_VAULT / "cases" / _slug / "meta" / "CASE_GROUND_TRUTH.yaml"
                if _gt_path.exists():
                    _gt = _yaml.safe_load(_gt_path.read_text(encoding="utf-8")) or {}
                    _court_from_gt = (_gt.get("court") or {}).get("id")
                    if _court_from_gt:
                        court_id = _court_from_gt
                    print(f"compose_and_draft loaded ground truth for {case_id}: judge={_gt.get('judge',{}).get('name','?')}, court_id={court_id}")
            except Exception as _gt_err:
                print(f"ground truth load failed (non-fatal): {_gt_err}")

            docx_path = out_dir / docx_filename
            render_md_to_docx(md_text, str(docx_path), court_id=court_id)
            print(f"Rendered .docx via render_md_to_docx (court_id={court_id})")

        except Exception as docx_err:
            print(f"docx rendering failed: {docx_err}, falling back to md only")
            docx_path = md_path  # fallback

        # ---- Step 5: Optional scorecard ----
        sc_path = ""
        if with_scorecard:
            try:
                # Use existing draft_reply_brief scorecard generation
                from beezle.generation.drafter import draft_for_case
                _, _, sc = draft_for_case(
                    canonical_id=case_id,
                    base_path=BASE_PATH,
                    output_dir=out_dir,
                    with_scorecard=True,
                    filing_type=filing_type,
                )
                sc_path = str(sc) if sc else ""
            except Exception as sc_err:
                print(f"scorecard generation failed: {sc_err}")

        # ---- Step 6: Also save to case filings directory ----
        try:
            case_slug = slug.replace("fed_2_", "fed_")
            filings_dir = OBSIDIAN_VAULT / "cases" / case_slug / "filings"
            if filings_dir.exists():
                import shutil
                shutil.copy2(str(docx_path), str(filings_dir / docx_filename))
        except Exception:
            pass  # non-fatal

        return {
            "case_id": case_id,
            "filing_type": filing_type,
            "status": "complete",
            "md_path": str(md_path),
            "docx_path": str(docx_path),
            "scorecard_path": sc_path,
            "blocks_composed": len(composition.blocks),
            "blocks_skipped": len(composition.skipped_blocks),
            "skipped_details": [s["block"] for s in composition.skipped_blocks],
            "warnings": composition.warnings,
            "diagnosis": {
                "defect_total": composition.diagnosis_snapshot.get("defect_total", 0),
                "fatal_count": composition.diagnosis_snapshot.get("fatal_count", 0),
                "posture": composition.diagnosis_snapshot.get("posture", ""),
            },
        }
    except Exception as e:
        return {
            "error": f"compose_and_draft failed: {e}",
            "trace": traceback.format_exc()[:1000],
            "case_id": case_id,
            "filing_type": filing_type,
        }


@mcp.tool
def compose_recommend(case_id: str) -> dict:
    """Recommend filing types for a case based on audit defects.

    Returns ranked list of filing types with scores and reasons.
    Use this BEFORE compose_and_draft to determine what filings to produce.

    Args:
        case_id: canonical case id
    """
    try:
        import sys as _sys
        if str(BASE_PATH) not in _sys.path:
            _sys.path.insert(0, str(BASE_PATH))
        from pipeline.filing_composer import recommend_filing_types, build_context

        recommendations = recommend_filing_types(case_id)
        ctx = build_context(case_id)

        return {
            "case_id": case_id,
            "recommendations": recommendations,
            "context_summary": {
                "defect_total": ctx.get("defect_total", 0),
                "fatal_count": ctx.get("fatal_count", 0),
                "posture": ctx.get("posture", ""),
                "defect_categories": ctx.get("defect_categories", {}),
                "is_federal": ctx.get("is_federal", False),
            },
        }
    except Exception as e:
        return {"error": f"compose_recommend failed: {e}", "case_id": case_id}


@mcp.tool
def compose_diagnose(case_id: str) -> dict:
    """Show the full diagnosis context for a case — what the composer sees.

    Returns all defects, categories, cross-case signals, posture, and
    registry data. Use this to understand why certain blocks fire or skip.

    Args:
        case_id: canonical case id
    """
    try:
        import sys as _sys
        if str(BASE_PATH) not in _sys.path:
            _sys.path.insert(0, str(BASE_PATH))
        from pipeline.filing_composer import build_context
        ctx = build_context(case_id)
        return {"case_id": case_id, "context": ctx}
    except Exception as e:
        return {"error": f"compose_diagnose failed: {e}", "case_id": case_id}




@mcp.tool()
def case_brief(case_id: str, section: str = "summary") -> str:
    """Get case context for drafting. Call with different sections to avoid context overload.

    Sections:
      summary   - case posture, judge, parties, deadlines, what to file next
      timeline  - full docket history with ECF numbers and dates
      violations - scorecard with defendant-level breakdown and specific citations
      claims    - factual application (FCRA, FDCPA, UTPCPL claims with defendant names)
      strategy  - gate analysis, recommended filings, attack plan
      defects   - raw audit defects with source files and citations

    ALWAYS call summary first. Then call other sections as needed for each filing section.
    """
    import json
    from pathlib import Path as P
    ROOT = P(str(_eroot()))
    obs = ROOT / "obsidian_sync" / "cases" / "fed_26-cv-00123"

    if section == "summary":
        parts = []
        try:
            reg = json.loads((ROOT / "cases_index" / "case_registry.json").read_text(encoding="utf-8"))
            r = reg.get(case_id, {})
            parts.append("Judge: " + r.get("judge", "unknown"))
            parts.append("Parties: " + json.dumps(r.get("parties", [])))
            parts.append("Docket: " + str(r.get("docket_aliases", ["unknown"])[0]))
            parts.append("Status: " + r.get("status", "unknown"))
            notes = r.get("notes", [])
            if isinstance(notes, list):
                parts.append("Notes: " + " | ".join(str(n) for n in notes))
        except Exception:
            pass
        ks = obs / "meta" / "KANBAN_SUMMARY.md"
        if ks.exists():
            parts.append("")
            parts.append(ks.read_text(encoding="utf-8", errors="replace")[:3000])
        return "\n".join(parts)

    elif section == "timeline":
        da = ROOT / "obsidian_sync" / "cases" / "dockets" / "DOCKET_ANALYSIS_ALL_CASES.md"
        if da.exists():
            txt = da.read_text(encoding="utf-8", errors="replace")
            start = txt.find("CASE 1:")
            end = txt.find("## CASE 2:")
            if start > -1 and end > -1:
                return txt[start:end][:4000]
        dt = ROOT / "obsidian_sync" / "cases" / "dockets" / "2_26-cv-00123" / "docket.txt"
        if dt.exists():
            return dt.read_text(encoding="utf-8", errors="replace")[:4000]
        return "No docket timeline found"

    elif section == "violations":
        sc = obs / "scorecard_20260516.md"
        if sc.exists():
            return sc.read_text(encoding="utf-8", errors="replace")[:4000]
        return "No scorecard found"

    elif section == "claims":
        try:
            reg = json.loads((ROOT / "cases_index" / "case_registry.json").read_text(encoding="utf-8"))
            r = reg.get(case_id, {})
            fa = r.get("factual_application", {})
            ef = r.get("engine_facts", [])
            parts = ["## CLAIMS"]
            if isinstance(ef, list):
                parts.append("Engine facts: " + "; ".join(ef))
            if isinstance(fa, dict):
                for k, v in fa.items():
                    parts.append("")
                    parts.append("### " + k)
                    parts.append(str(v))
            return "\n".join(parts)
        except Exception as e:
            return "Claims error: " + str(e)

    elif section == "strategy":
        ar = obs / "analysis_report_20260516.md"
        if ar.exists():
            return ar.read_text(encoding="utf-8", errors="replace")[:4000]
        return "No strategy analysis found"

    elif section == "defects":
        try:
            from pipeline.filing_composer import build_context
            ctx = build_context(case_id)
            ds = ctx.get("defects", [])
            parts = ["Total: " + str(len(ds)) + ", Fatal: " + str(ctx.get("fatal_count", 0))]
            parts.append("Categories: " + json.dumps(ctx.get("defect_categories", {})))
            for i, d in enumerate(ds[:15]):
                if isinstance(d, dict):
                    parts.append("")
                    parts.append("[" + d.get("severity", "?") + "] " + d.get("category", "?"))
                    parts.append("  Citation: " + str(d.get("defect", ""))[:150])
                    parts.append("  Source: " + str(d.get("notes", ""))[:150])
            if len(ds) > 15:
                parts.append("")
                parts.append("... and " + str(len(ds) - 15) + " more defects")
            return "\n".join(parts)
        except Exception as e:
            return "Defects error: " + str(e)

    return "Unknown section: " + section + ". Use: summary, timeline, violations, claims, strategy, defects"

def main():
    parser = argparse.ArgumentParser(description="Beezle MCP server")
    parser.add_argument(
        "--transport",
        choices=["sse", "stdio", "http"],
        default="http",
        help="Transport mode (default: sse for Hermes Agent)",
    )
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind (SSE/HTTP only)")
    parser.add_argument("--port", type=int, default=8200, help="Port to bind (SSE/HTTP only)")
    args = parser.parse_args()

    print(f"[beezle-mcp] Starting Beezle MCP server")
    print(f"[beezle-mcp] BASE_PATH:    {BASE_PATH}")
    print(f"[beezle-mcp] AUDIT_DIR:    {AUDIT_DIR}")
    print(f"[beezle-mcp] OBSIDIAN:     {OBSIDIAN_VAULT}")
    print(f"[beezle-mcp] Transport:    {args.transport}")
    if args.transport in ("sse", "http"):
        print(f"[beezle-mcp] Bind:         {args.host}:{args.port}")
        if args.transport == "http":
            print(f"[beezle-mcp] Local URL:    http://localhost:{args.port}/mcp")
        else:
            print(f"[beezle-mcp] Local URL:    http://localhost:{args.port}/sse")
    print(f"[beezle-mcp] Tools registered: 16")
    print()

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport=args.transport, host=args.host, port=args.port, stateless_http=True)


# === BEEZLE PHASE B-2 PATCH (rebuilt Session 23 Item 6) ===
# Layer B Obsidian knowledge graph query tools.
# Adds 5 tools (obsidian_index_stats, trace_legal_authority, search_obsidian,
# list_authorities_by_jurisdiction, list_authorities_by_level) backed by the
# 58k-citation index at obsidian_sync/primary_sources/_index/ (built by
# pipeline.authority_resolver.build_index).
try:
    from obsidian_sync.mcp_tools_obsidian import register_obsidian_tools as _b2_register
    _b2_count = _b2_register(mcp)
    print(f"[beezle-mcp] Phase B-2 Obsidian tools registered: {_b2_count}")
except Exception as _b2_err:
    import sys as _b2_sys2, traceback as _b2_tb
    print(f"[beezle] WARNING: Phase B-2 Obsidian tools failed to load: {_b2_err}", file=_b2_sys2.stderr)
    _b2_tb.print_exc(file=_b2_sys2.stderr)
# === END BEEZLE PHASE B-2 PATCH ===


# --- Scenario Engine tools (Block 3) ---
try:
    from obsidian_sync.mcp_tools_scenario import register_scenario_tools as _reg_scenario_tools
    _scenario_count = _reg_scenario_tools(mcp)
except Exception as _scenario_err:
    import sys as _sys
    print(f"[beezle-mcp] WARNING: Scenario tools failed to load: {_scenario_err}", file=_sys.stderr)

# --- Duty Engine tools (Block 4) ---  # DUTY_V02_APPLIED
try:
    from obsidian_sync.mcp_tools_duty import register_duty_tools as _reg_duty_tools
    _duty_count = _reg_duty_tools(mcp)
except Exception as _duty_err:
    import sys as _sys
    print(f"[beezle-mcp] WARNING: Duty tools failed to load: {_duty_err}", file=_sys.stderr)
# --- /Duty Engine tools ---


# --- Block 9A skill ports (BLOCK_9A_MCP_REGISTRATION) ---
try:
    from regulatory.mcp_tools_block_9a import register_block_9a_tools as _reg_9a_tools
    _9a_count = _reg_9a_tools(mcp)
    from regulatory.mcp_tools_refinement import register_refinement_tools
    register_refinement_tools(mcp)
    print(f"[beezle-mcp] Block 9A registered: {_9a_count} tools")
except Exception as _9a_err:
    import sys as _sys
    print(f"[beezle-mcp] WARNING: Block 9A tools failed to load: {_9a_err}", file=_sys.stderr)
# --- /Block 9A ---

# --- Block 9B skill ports (BLOCK_9B_MCP_REGISTRATION) ---
try:
    from regulatory.mcp_tools_block_9b import register_block_9b_tools as _reg_9b_tools
    _9b_count = _reg_9b_tools(mcp)
    print(f"[beezle-mcp] Block 9B registered: {_9b_count} tools")
except Exception as _9b_err:
    import sys as _sys
    print(f"[beezle-mcp] WARNING: Block 9B tools failed to load: {_9b_err}", file=_sys.stderr)
# --- /Block 9B ---

# --- Phase 2A source fetch (PHASE_2A_MCP_SOURCE_FETCH) ---
try:
    from scrapers.mcp_tools_sources import register_source_fetch_tools as _reg_src_tools
    _src_count = _reg_src_tools(mcp)
    print(f"[beezle-mcp] Phase 2A registered: {_src_count} tools")
except Exception as _src_err:
    import sys as _sys
    print(f"[beezle-mcp] WARNING: Phase 2A source tools failed: {_src_err}", file=_sys.stderr)
# --- /Phase 2A ---

# --- Phase 7 Hermes autonomy (PHASE_7_HERMES_AUTONOMY) ---
try:
    from regulatory.mcp_tools_phase7 import register_phase7_tools as _reg_p7
    _p7_count = _reg_p7(mcp)
    print(f"[beezle-mcp] Phase 7 registered: {_p7_count} tools")
except Exception as _p7_err:
    import sys as _sys
    print(f"[beezle-mcp] WARNING: Phase 7 tools failed: {_p7_err}", file=_sys.stderr)
# --- /Phase 7 ---


# --- Document Lifecycle Orchestrator (LIFECYCLE_MCP_REGISTRATION) ---
try:
    from document_lifecycle import register_lifecycle_tools as _reg_lifecycle
    _lifecycle_count = _reg_lifecycle(mcp)
    print(f"[beezle-mcp] Lifecycle registered: {_lifecycle_count} tools")
except Exception as _lifecycle_err:
    import sys as _sys
    print(f"[beezle-mcp] WARNING: Lifecycle tools failed: {_lifecycle_err}", file=_sys.stderr)
# --- /Lifecycle ---

# --- Filing Queue (FILING_QUEUE_MCP_REGISTRATION) ---
try:
    from filing_queue import register_filing_queue_tools as _reg_filing
    _filing_count = _reg_filing(mcp)
    print(f"[beezle-mcp] Filing Queue registered: {_filing_count} tools")
except Exception as _filing_err:
    import sys as _sys
    print(f"[beezle-mcp] WARNING: Filing Queue tools failed: {_filing_err}", file=_sys.stderr)
# --- /Filing Queue ---

# --- Deadline Tracker (DEADLINE_TRACKER_MCP_REGISTRATION) ---
try:
    from deadline_tracker import register_deadline_tools as _reg_deadline
    _deadline_count = _reg_deadline(mcp)
    print(f"[beezle-mcp] Deadline Tracker registered: {_deadline_count} tools")
except Exception as _deadline_err:
    import sys as _sys
    print(f"[beezle-mcp] WARNING: Deadline tools failed: {_deadline_err}", file=_sys.stderr)
# --- /Deadline Tracker ---


# === FILING ATOMS (composable filing system) ===
try:
    from regulatory.mcp_tools_filing_atoms import register_filing_atom_tools as _reg_filing_atoms
    _filing_atoms_count = _reg_filing_atoms(mcp)
    print(f"[beezle-mcp] Filing atom tools registered: {_filing_atoms_count}")
except Exception as _filing_atoms_err:
    import sys as _sys
    print(f"[beezle-mcp] WARNING: Filing atom tools failed to load: {_filing_atoms_err}", file=_sys.stderr)
# === END FILING ATOMS ===


# === CHAIN PIPELINE TOOLS ===
# Wrappers exposing pipeline/chain_runner.py + pipeline/violation_classifier.py +
# pipeline/case_audit_engine.py as MCP tools. Added by patch_chain_mcp_wrappers.py.
# These let any Hermes agent invoke the 10-agent chain over an email scan,
# classify a backlog of violations, or audit a case's orders end-to-end.

@mcp.tool
def chain_run(
    input_path: Optional[str] = None,
    payload: Optional[dict] = None,
    case_id: Optional[str] = None,
    limit: int = 0,
    skip: Optional[list] = None,
    only: Optional[list] = None,
    write_vault: bool = False,
    window: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    dedupe: bool = False,
) -> dict:
    # CHAIN_RUN_VERSION = 2
    """Run the Beezle 10-agent chain over input data with agent-friendly extensions.

    Provide EITHER input_path (a JSON file or 'AUTO') OR payload (a single dict).
    At least one is required.

    Args:
        input_path: absolute path to a JSON file, OR the literal string 'AUTO'
                    to auto-resolve. AUTO tries: (1) latest email_scan_*.json
                    by mtime in output/email_scans/, (2) source of most recent
                    chain in logs/chains/ as fallback.
        payload: a single dict to feed directly into the chain (alternative to
                 input_path - use one or the other).
        case_id: optional case_id to bind every chain to. If omitted, each
                 item's own case_id/case_number/matched_case is used.
        limit: max items to process (0 = no limit). Applied AFTER window
               filtering. Default 0.
        skip: agent numbers to skip, e.g. [9].
        only: agent numbers to run exclusively (overrides skip).
        write_vault: if True, writes chain markdown to obsidian_sync/cases/.
                     Default False (fast for repeated calls).
        window: time window string like '1h', '6h', '24h', '7d'. When given
                with input_path='AUTO', considers ALL scan files whose mtime
                falls in the window (not just the latest one). Items from
                those files are concatenated. Ignored if since/until given.
        since: ISO timestamp lower bound on scan file mtime (overrides window).
        until: ISO timestamp upper bound on scan file mtime (overrides window).
        dedupe: if True, skip items whose message_id already has a chain
                output in obsidian_sync/cases/<slug>/chain_outputs/.
                Default False.

    Returns:
        dict with keys:
          - ok: bool, True if all items completed successfully
          - status: 'OK' | 'PARTIAL_DEGRADED' | 'PIPELINE_ERROR' | 'SYSTEM_FAILURE'
          - items_processed: int
          - items_ok: int
          - items_skipped_dedupe: int (count of items skipped due to dedupe=True)
          - source_files: list of scan file paths consumed
          - chains: list of {chain_id, case_id, ok, agent_count, packet_dir,
                             failed_agents}
          - error: str (only on fatal pre-flight failure)
    """
    try:
        from pipeline.chain_runner import (
            ChainRunner,
            _load_input,
            CASE_SLUG_MAP,
            PACKET_DIR,
            LOG_DIR,
        )
    except Exception as e:
        return {
            "ok": False,
            "status": "SYSTEM_FAILURE",
            "error": f"failed to import chain_runner: {e}",
            "trace": traceback.format_exc()[:500],
        }

    if not input_path and not payload:
        return {
            "ok": False, "status": "PIPELINE_ERROR",
            "error": "must provide either input_path or payload",
        }
    if input_path and payload:
        return {
            "ok": False, "status": "PIPELINE_ERROR",
            "error": "provide input_path OR payload, not both",
        }

    skip_list = list(skip) if skip else []
    only_list = list(only) if only else []

    # ------------------------------------------------------------------
    # Helper: parse window string like "24h" / "7d" / "30m" into seconds
    # ------------------------------------------------------------------
    def _parse_window_seconds(w):
        if not w or not isinstance(w, str):
            return None
        m = re.match(r"^\s*(\d+)\s*([smhd])\s*$", w.lower())
        if not m:
            return None
        n, unit = int(m.group(1)), m.group(2)
        return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]

    # ------------------------------------------------------------------
    # Helper: AUTO-resolve input_path
    # ------------------------------------------------------------------
    def _resolve_auto():
        scans_dir = BASE_PATH / "output" / "email_scans"
        if scans_dir.exists():
            scans = sorted(
                scans_dir.glob("email_scan_*.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if scans:
                return [scans[0]]
        # Fallback: source of most recent chain
        if LOG_DIR.exists():
            chains = sorted(
                LOG_DIR.glob("chain_*.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for cpath in chains[:5]:
                try:
                    cdata = json.loads(cpath.read_text(encoding="utf-8"))
                    src = (cdata.get("input_payload") or {}).get("_source_file")
                    if src and Path(src).exists():
                        return [Path(src)]
                except Exception:
                    continue
        return []

    # ------------------------------------------------------------------
    # Helper: collect scan files in a time window
    # ------------------------------------------------------------------
    def _scans_in_window():
        """Glob all email_scan_*.json by mtime within window/since/until."""
        scans_dir = BASE_PATH / "output" / "email_scans"
        if not scans_dir.exists():
            return []
        all_scans = list(scans_dir.glob("email_scan_*.json"))
        if not all_scans:
            return []

        # Compute bounds
        now = datetime.now()
        lo = None
        hi = None
        if since:
            try:
                lo = datetime.fromisoformat(since)
            except Exception:
                pass
        if until:
            try:
                hi = datetime.fromisoformat(until)
            except Exception:
                pass
        if lo is None and hi is None and window:
            secs = _parse_window_seconds(window)
            if secs:
                lo = datetime.fromtimestamp(now.timestamp() - secs)

        if lo is None and hi is None:
            return []

        out = []
        for p in all_scans:
            mt = datetime.fromtimestamp(p.stat().st_mtime)
            if lo and mt < lo:
                continue
            if hi and mt > hi:
                continue
            out.append(p)
        return sorted(out, key=lambda p: p.stat().st_mtime)

    # ------------------------------------------------------------------
    # Helper: dedupe by message_id against vault chain_outputs frontmatter
    # ------------------------------------------------------------------
    _seen_message_ids_cache = None

    def _seen_message_ids():
        nonlocal _seen_message_ids_cache
        if _seen_message_ids_cache is not None:
            return _seen_message_ids_cache
        seen = set()
        try:
            vault_cases = BASE_PATH / "obsidian_sync" / "cases"
            if not vault_cases.exists():
                _seen_message_ids_cache = seen
                return seen
            for slug in CASE_SLUG_MAP.values():
                co_dir = vault_cases / slug / "chain_outputs"
                if not co_dir.exists():
                    continue
                for md in co_dir.glob("chain_*.md"):
                    try:
                        head = md.read_text(encoding="utf-8")[:2000]
                        m = re.search(
                            r"source_email:\s*([^\n]+)", head
                        )
                        if m:
                            seen.add(m.group(1).strip())
                        # Also try message_id directly if present
                        m2 = re.search(
                            r"message_id:\s*([^\n]+)", head
                        )
                        if m2:
                            seen.add(m2.group(1).strip().strip('"'))
                    except Exception:
                        continue
        except Exception:
            pass
        _seen_message_ids_cache = seen
        return seen

    # ------------------------------------------------------------------
    # Build items list
    # ------------------------------------------------------------------
    source_files = []
    items = []
    skipped_dedupe = 0

    if payload:
        items = [payload]
    else:
        # Resolve input_path
        use_window = bool(window or since or until)

        if use_window:
            scans = _scans_in_window()
            if not scans:
                return {
                    "ok": True, "status": "OK",
                    "items_processed": 0, "items_ok": 0,
                    "items_skipped_dedupe": 0,
                    "source_files": [],
                    "chains": [],
                    "note": "no scan files in window",
                }
            source_files = [str(p) for p in scans]
            for sp in scans:
                try:
                    items.extend(_load_input(sp))
                except Exception as e:
                    return {
                        "ok": False, "status": "PIPELINE_ERROR",
                        "error": f"failed to load {sp.name}: {e}",
                    }
        elif input_path == "AUTO":
            scans = _resolve_auto()
            if not scans:
                return {
                    "ok": False, "status": "PIPELINE_ERROR",
                    "error": "AUTO resolution found no email_scan files and no fallback chain source",
                }
            source_files = [str(p) for p in scans]
            try:
                items = _load_input(scans[0])
            except Exception as e:
                return {
                    "ok": False, "status": "PIPELINE_ERROR",
                    "error": f"failed to load {scans[0].name}: {e}",
                }
        else:
            # Explicit path
            p = Path(input_path)
            if not p.exists():
                return {
                    "ok": False, "status": "PIPELINE_ERROR",
                    "error": f"input_path not found: {input_path}",
                }
            source_files = [str(p)]
            try:
                items = _load_input(p)
            except Exception as e:
                return {
                    "ok": False, "status": "PIPELINE_ERROR",
                    "error": f"failed to load {p.name}: {e}",
                }

        # Dedupe BEFORE limit (so limit cuts the deduped list)
        if dedupe and items:
            seen = _seen_message_ids()
            kept = []
            for it in items:
                if isinstance(it, dict):
                    mid = it.get("message_id") or ""
                    if mid and mid in seen:
                        skipped_dedupe += 1
                        continue
                kept.append(it)
            items = kept

        if limit and limit > 0:
            items = items[:limit]

    # ------------------------------------------------------------------
    # Snapshot packet dir to detect new packets
    # ------------------------------------------------------------------
    pre_packets = set()
    try:
        if PACKET_DIR.exists():
            pre_packets = {p.name for p in PACKET_DIR.iterdir() if p.is_dir()}
    except Exception:
        pass

    runner = ChainRunner(vault=write_vault, log_each=True)

    chains_out = []
    n_ok = 0

    # Trackers for status classification
    saw_system_failure = False
    saw_pipeline_failure = False
    saw_only_agent9_failure = True  # downgraded to False if any non-A9 fails
    any_failure = False

    SYSTEM_KEYWORDS = (
        "connection refused", "timeout", "timed out",
        "llama", "ECONNREFUSED", "no such host",
        "broken pipe", "connection reset",
    )

    for item in items:
        if case_id:
            item_case_id = case_id
        elif isinstance(item, dict):
            item_case_id = (
                item.get("case_id")
                or item.get("case_number")
                or item.get("matched_case")
            )
        else:
            item_case_id = None

        try:
            state = runner.run(
                item,
                case_id=item_case_id,
                skip=skip_list,
                only=only_list,
            )
        except Exception as e:
            err = str(e)
            chains_out.append({
                "chain_id": None,
                "case_id": item_case_id,
                "ok": False,
                "error": err,
                "failed_agents": [],
            })
            any_failure = True
            saw_only_agent9_failure = False
            if any(kw in err.lower() for kw in SYSTEM_KEYWORDS):
                saw_system_failure = True
            else:
                saw_pipeline_failure = True
            continue

        # Detect failed agents from state.results
        failed_agents = []
        try:
            for r in (state.results or []):
                r_ok = r.ok if hasattr(r, "ok") else (
                    r.get("ok") if isinstance(r, dict) else True
                )
                r_agent = r.agent if hasattr(r, "agent") else (
                    r.get("agent") if isinstance(r, dict) else None
                )
                r_err = r.error if hasattr(r, "error") else (
                    r.get("error") if isinstance(r, dict) else None
                )
                if not r_ok:
                    failed_agents.append({
                        "agent": r_agent,
                        "error": (str(r_err)[:300] if r_err else None),
                    })
        except Exception:
            pass

        # Status tracking from this chain
        if not state.ok or failed_agents:
            any_failure = True
            for fa in failed_agents:
                err_str = (fa.get("error") or "").lower()
                if any(kw in err_str for kw in SYSTEM_KEYWORDS):
                    saw_system_failure = True
                if fa.get("agent") != 9:
                    saw_only_agent9_failure = False
                if fa.get("agent") != 9 and not any(
                    kw in err_str for kw in SYSTEM_KEYWORDS
                ):
                    saw_pipeline_failure = True

        # Detect new packet for this chain
        new_packet_dir = None
        try:
            if PACKET_DIR.exists():
                for p in PACKET_DIR.iterdir():
                    if p.is_dir() and p.name not in pre_packets:
                        if state.chain_id and state.chain_id[-6:] in p.name:
                            new_packet_dir = str(p)
                            break
                pre_packets = {p.name for p in PACKET_DIR.iterdir() if p.is_dir()}
        except Exception:
            pass

        chains_out.append({
            "chain_id": state.chain_id,
            "case_id": state.case_id,
            "ok": bool(state.ok),
            "agent_count": len(state.results or []),
            "packet_dir": new_packet_dir,
            "failed_agents": failed_agents,
        })
        if state.ok and not failed_agents:
            n_ok += 1

    # ------------------------------------------------------------------
    # Classify overall status
    # ------------------------------------------------------------------
    if not any_failure and n_ok == len(items):
        status = "OK"
    elif saw_system_failure:
        status = "SYSTEM_FAILURE"
    elif any_failure and saw_only_agent9_failure and not saw_pipeline_failure:
        status = "PARTIAL_DEGRADED"
    else:
        status = "PIPELINE_ERROR"

    return {
        "ok": (status == "OK"),
        "status": status,
        "items_processed": len(items),
        "items_ok": n_ok,
        "items_skipped_dedupe": skipped_dedupe,
        "source_files": source_files,
        "chains": chains_out,
    }


@mcp.tool
def chain_status(chain_id: str) -> dict:
    """Read the JSON log of a previously-run chain.

    Args:
        chain_id: the full chain_id, e.g. 'chain_20260505_213126_dbd469'.
                  Must match the filename stem in logs/chains/.

    Returns:
        The full chain log dict (chain_id, case_id, started, finished, ok,
        and per-agent results), or {"error": ...} if not found.
    """
    try:
        from pipeline.chain_runner import LOG_DIR
    except Exception as e:
        return {"error": f"failed to import LOG_DIR: {e}"}

    path = LOG_DIR / f"{chain_id}.json"
    if not path.exists():
        return {
            "error": f"chain log not found: {chain_id}",
            "expected_path": str(path),
        }
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return {
            "error": f"failed to parse chain log: {e}",
            "trace": traceback.format_exc()[:300],
        }


@mcp.tool
def chain_list_recent(limit: int = 10) -> dict:
    """List recent chain runs by modification time.

    Args:
        limit: how many recent chains to return (default 10).

    Returns:
        dict with 'count' and 'chains' (list of {chain_id, mtime, size_bytes}).
        Most recent first.
    """
    try:
        from pipeline.chain_runner import LOG_DIR
    except Exception as e:
        return {"error": f"failed to import LOG_DIR: {e}"}

    if not LOG_DIR.exists():
        return {"count": 0, "chains": [], "note": f"{LOG_DIR} does not exist yet"}

    try:
        files = sorted(
            LOG_DIR.glob("chain_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        out = []
        for p in files[: max(1, limit)]:
            stat = p.stat()
            out.append({
                "chain_id": p.stem,
                "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "size_bytes": stat.st_size,
            })
        return {"count": len(out), "chains": out}
    except Exception as e:
        return {"error": f"failed to list chains: {e}"}


@mcp.tool
def violation_classify(
    input_path: str,
    output_path: Optional[str] = None,
    use_llm_fallback: bool = True,
    limit: int = 0,
    drop_noise: bool = True,
) -> dict:
    """Classify violations in a backlog of emails using deterministic rules
    plus optional LLM fallback for unknowns.

    Wraps pipeline.violation_classifier.classify_all(). Designed for processing
    the ~1,870 'unknown' violations from prior email scans.

    Args:
        input_path: path to email_scan JSON or similar.
        output_path: where to write classified results. Defaults to
                     <input_dirname>_classified.json next to input.
        use_llm_fallback: if True, calls LLM for any row not handled by
                          deterministic rules. False = rule-only (fast).
        limit: max rows to classify (0 = all).
        drop_noise: filter marketing/newsletter emails before classification.

    Returns:
        dict from classify_all (counts per category, classifier stats,
        total rows, output_path).
    """
    try:
        from pipeline.violation_classifier import classify_all
    except Exception as e:
        return {
            "error": f"failed to import violation_classifier: {e}",
            "trace": traceback.format_exc()[:500],
        }

    in_path = Path(input_path)
    if not in_path.exists():
        return {"error": f"input_path not found: {input_path}"}

    if output_path:
        out_path = Path(output_path)
    else:
        out_path = in_path.parent / f"{in_path.stem}_classified.json"

    try:
        result = classify_all(
            input_path=in_path,
            output_path=out_path,
            use_llm_fallback=use_llm_fallback,
            limit=limit,
            drop_noise=drop_noise,
        )
        if isinstance(result, dict):
            result.setdefault("output_path", str(out_path))
            return result
        return {"result": str(result), "output_path": str(out_path)}
    except Exception as e:
        return {
            "error": f"classify_all failed: {e}",
            "trace": traceback.format_exc()[:500],
        }


@mcp.tool
def case_audit_full(case_id: str) -> dict:
    """Run the full case audit engine over all judicial orders for a case.

    Wraps pipeline.case_audit_engine.audit_case(). For each document linked to
    the case in case_map.json, scans for judicial misconduct patterns and
    feeds each through Agent 10 (title_18_criminal) for immunity + criminal
    classification.

    NOTE: This is DIFFERENT from get_case_audit (defined earlier in this file),
    which is a static registry lookup. case_audit_full runs the live engine
    and may take several minutes depending on document count.

    Args:
        case_id: canonical case id, e.g. 'phila_ccp:260100000',
                 'fed:2:26-cv-00456', etc.

    Returns:
        dict with per-document reports, total_violations, total_void,
        total_voidable, and aggregated findings.
    """
    try:
        from pipeline.case_audit_engine import audit_case
    except Exception as e:
        return {
            "error": f"failed to import case_audit_engine: {e}",
            "trace": traceback.format_exc()[:500],
        }

    if not case_id or not isinstance(case_id, str):
        return {"error": "case_id must be a non-empty string"}

    try:
        return audit_case(case_id)
    except Exception as e:
        return {
            "error": f"audit_case failed: {e}",
            "trace": traceback.format_exc()[:500],
        }


# === END CHAIN PIPELINE TOOLS ===


# ============================================================================
# AGENT_MEMORY_TOOLS_VERSION = 1
# AGENT_MEMORY_OS_FIX_APPLIED
import os as _mem_os
# Durable memory for office agents, stored in obsidian_sync/agents/<slug>.md
# ============================================================================
import re as _mem_re
import tempfile as _mem_tempfile
from pathlib import Path as _MemPath
from typing import Optional as _MemOptional

_MEM_VAULT_ROOT = _MemPath(str(_eroot() / "obsidian_sync"))
_MEM_AGENTS_DIR = _MEM_VAULT_ROOT / "agents"
_MEM_SLUG_RE = _mem_re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
_MEM_LOG_CAP = 20  # keep last N lines in log sections
_MEM_DEFAULT_LOG_HEADER = "## Recent Activity"


def _mem_validate_slug(slug: str) -> str:
    s = (slug or "").strip().lower()
    if not _MEM_SLUG_RE.match(s):
        raise ValueError(
            f"agent_slug must match ^[a-z0-9][a-z0-9-]{{1,63}}$ (got: {slug!r})"
        )
    return s


def _mem_path_for(slug: str) -> _MemPath:
    s = _mem_validate_slug(slug)
    p = (_MEM_AGENTS_DIR / f"{s}.md").resolve()
    # Defense: ensure resolved path is still under _MEM_AGENTS_DIR
    agents_dir_resolved = _MEM_AGENTS_DIR.resolve()
    if agents_dir_resolved not in p.parents and p.parent != agents_dir_resolved:
        raise ValueError(f"resolved path escapes agents dir: {p}")
    return p


def _mem_split_frontmatter(text: str) -> tuple:
    """Parse YAML-ish frontmatter (subset) from a markdown file. Returns (fm, body)."""
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        return {}, text
    lines = text.splitlines(keepends=False)
    if len(lines) < 2 or lines[0].strip() != "---":
        return {}, text
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx is None:
        return {}, text
    fm_block = lines[1:end_idx]
    body = "\n".join(lines[end_idx + 1:])
    if body and not body.endswith("\n"):
        body += "\n"
    fm = {}
    for line in fm_block:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _mem_re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        # Strip surrounding quotes if present
        if len(val) >= 2:
            if (val[0] == '"' and val[-1] == '"') or (val[0] == "'" and val[-1] == "'"):
                val = val[1:-1]
        # Light type coercion: ints / bools / null
        low = val.lower()
        if low in ("null", "none", "~"):
            fm[key] = None
        elif low == "true":
            fm[key] = True
        elif low == "false":
            fm[key] = False
        else:
            try:
                fm[key] = int(val)
            except ValueError:
                fm[key] = val
    return fm, body


def _mem_format_frontmatter(fm: dict) -> str:
    if not fm:
        return ""
    out_lines = ["---"]
    for k, v in fm.items():
        if not _mem_re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", str(k)):
            continue  # silently skip invalid keys
        if v is None:
            out_lines.append(f"{k}: null")
        elif isinstance(v, bool):
            out_lines.append(f"{k}: {'true' if v else 'false'}")
        elif isinstance(v, int):
            out_lines.append(f"{k}: {v}")
        else:
            sval = str(v)
            # Quote if contains structural chars
            needs_quote = (
                ":" in sval
                or '"' in sval
                or "'" in sval
                or sval != sval.strip()
                or sval == ""
            )
            if needs_quote:
                # Minimal YAML subset: only quotes need escaping. Backslashes pass through.
                escaped = sval.replace('"', '\\"')
                out_lines.append(f'{k}: "{escaped}"')
            else:
                out_lines.append(f"{k}: {sval}")
    out_lines.append("---")
    return "\n".join(out_lines) + "\n"


def _mem_append_to_log_section(body: str, log_line: str, header: str = _MEM_DEFAULT_LOG_HEADER) -> str:
    """Prepend log_line to the first matching '## Recent ...' section, or create one.
    Caps the section at _MEM_LOG_CAP lines (most recent at top)."""
    line = log_line.rstrip("\n").strip()
    if not line:
        return body
    if not line.startswith("- "):
        line = f"- {line}"

    lines = body.splitlines() if body else []
    section_start = None
    for i, ln in enumerate(lines):
        if ln.startswith("## ") and "recent" in ln.lower():
            section_start = i
            break

    if section_start is None:
        # No section exists; create one at the end
        if lines and lines[-1].strip() != "":
            lines.append("")
        lines.append(header)
        lines.append("")
        lines.append(line)
        return "\n".join(lines) + "\n"

    # Find end of section (next H2 or EOF)
    section_end = len(lines)
    for j in range(section_start + 1, len(lines)):
        if lines[j].startswith("## "):
            section_end = j
            break

    existing = []
    for j in range(section_start + 1, section_end):
        if lines[j].lstrip().startswith("- "):
            existing.append(lines[j].rstrip())

    capped = [line] + existing[: _MEM_LOG_CAP - 1]

    head = lines[: section_start + 1]
    blank_after_header = [""]
    new_section = capped
    trailing_blank = [""] if section_end < len(lines) else []
    tail = lines[section_end:]

    rebuilt = head + blank_after_header + new_section + trailing_blank + tail
    result = "\n".join(rebuilt)
    if not result.endswith("\n"):
        result += "\n"
    return result


def _mem_atomic_write(path: _MemPath, content: str) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = _mem_tempfile.mkstemp(
        prefix=f".{path.name}.tmp-",
        dir=str(path.parent),
        text=False,
    )
    try:
        with _mem_os.fdopen(fd, "wb") as f:
            data = content.encode("utf-8")
            f.write(data)
        _mem_os.replace(tmp_path, path)
        return len(data)
    except Exception:
        try:
            _mem_os.unlink(tmp_path)
        except OSError:
            pass
        raise


@mcp.tool()
def memory_get(agent_slug: str) -> dict:
    """Read an office agent's durable memory note from the vault.

    Args:
        agent_slug: lowercase, hyphens only, e.g. "chain-runner", "case-auditor"

    Returns dict with: ok, slug, path, exists, frontmatter (dict), body (str).
    If the note doesn't exist yet, returns exists=False with empty fm/body."""
    try:
        slug = _mem_validate_slug(agent_slug)
        path = _mem_path_for(slug)
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    if not path.exists():
        return {
            "ok": True,
            "slug": slug,
            "path": str(path),
            "exists": False,
            "frontmatter": {},
            "body": "",
        }

    try:
        text = path.read_text(encoding="utf-8")
    except Exception as e:
        return {
            "ok": False,
            "slug": slug,
            "path": str(path),
            "error": f"read failed: {e}",
        }

    fm, body = _mem_split_frontmatter(text)
    return {
        "ok": True,
        "slug": slug,
        "path": str(path),
        "exists": True,
        "frontmatter": fm,
        "body": body,
    }


@mcp.tool()
def memory_set(
    agent_slug: str,
    frontmatter: _MemOptional[dict] = None,
    body: _MemOptional[str] = None,
    append_log: _MemOptional[str] = None,
) -> dict:
    """Write to an office agent's durable memory note in the vault.

    Args:
        agent_slug: lowercase, hyphens only, e.g. "chain-runner"
        frontmatter: dict of fields to merge into existing frontmatter.
            Pass {key: None} to delete a key.
        body: full body text to replace what's there. Mutually exclusive with append_log.
        append_log: a single log line to prepend to the "## Recent ..." section.
            Body parts outside that section are preserved. Auto-creates the
            section if missing. Caps at the most recent 20 entries.

    At least one of frontmatter / body / append_log must be provided.
    Returns: {ok, slug, path, bytes_written, frontmatter_merged}"""
    try:
        slug = _mem_validate_slug(agent_slug)
        path = _mem_path_for(slug)
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    if frontmatter is None and body is None and append_log is None:
        return {
            "ok": False,
            "error": "must provide at least one of frontmatter / body / append_log",
        }

    if body is not None and append_log is not None:
        return {
            "ok": False,
            "error": "body and append_log are mutually exclusive (use one)",
        }

    if path.exists():
        try:
            existing_text = path.read_text(encoding="utf-8")
        except Exception as e:
            return {"ok": False, "slug": slug, "path": str(path), "error": f"read failed: {e}"}
        existing_fm, existing_body = _mem_split_frontmatter(existing_text)
    else:
        existing_fm, existing_body = {}, ""

    # Merge frontmatter
    merged_fm = dict(existing_fm)
    if frontmatter is not None:
        for k, v in frontmatter.items():
            if v is None and k in merged_fm:
                del merged_fm[k]
            else:
                merged_fm[k] = v

    # Resolve body
    if body is not None:
        new_body = body if body.endswith("\n") else body + "\n"
    elif append_log is not None:
        new_body = _mem_append_to_log_section(existing_body, append_log)
    else:
        new_body = existing_body

    fm_block = _mem_format_frontmatter(merged_fm)
    final = fm_block + new_body
    if not final.endswith("\n"):
        final += "\n"

    try:
        bytes_written = _mem_atomic_write(path, final)
    except Exception as e:
        return {"ok": False, "slug": slug, "path": str(path), "error": f"write failed: {e}"}

    return {
        "ok": True,
        "slug": slug,
        "path": str(path),
        "bytes_written": bytes_written,
        "frontmatter_merged": merged_fm,
    }


# ============================================================================
# FILING_REQUIREMENTS_TOOL_VERSION = 1
# Lookup of required filing artifacts per (court, case_type, filing_type, plaintiff_type)
# ============================================================================
import json as _filing_json
import re as _filing_re
from pathlib import Path as _FilingPath
from typing import Optional as _FilingOptional

_FILING_REQ_PATH = _FilingPath(str(_eroot() / "filing_requirements.json"))
_FILING_VALID_PLAINTIFF_TYPES = {"natural_person", "entity"}


def _filing_load_db() -> dict:
    """Load the requirements JSON. Returns dict, or raises with clear error."""
    if not _FILING_REQ_PATH.exists():
        raise FileNotFoundError(
            f"filing_requirements.json not found at {_FILING_REQ_PATH}"
        )
    with open(_FILING_REQ_PATH, "r", encoding="utf-8") as f:
        return _filing_json.load(f)


def _filing_resolve_inheritance(db: dict, cell: dict, depth: int = 0) -> dict:
    """Resolve _inherits_from chain. Caller cell wins on overrides."""
    if depth > 5:
        return cell  # cycle protection
    inherits = cell.get("_inherits_from")
    if not inherits:
        return cell

    # Walk dotted path: e.g. "edpa.case_types.foia_privacy_act.complaint"
    path_parts = inherits.split(".")
    cursor = db.get("courts", {})
    for part in path_parts:
        if not isinstance(cursor, dict) or part not in cursor:
            return cell  # broken inheritance; return as-is
        cursor = cursor[part]

    parent = _filing_resolve_inheritance(db, cursor, depth + 1)

    # Merge: parent first, then cell overrides
    merged = dict(parent)
    overrides = cell.get("_overrides", {})

    # Special handling for *_add and *_remove keys in overrides
    for key, val in overrides.items():
        if key.endswith("_add") and isinstance(val, list):
            base_key = key[:-4]
            existing = list(merged.get(base_key, []))
            existing.extend(val)
            merged[base_key] = existing
        elif key.startswith("removed_") and isinstance(val, list):
            target_key = "required_for_natural_person"  # most common removal target
            existing = merged.get(target_key, [])
            merged[target_key] = [
                d for d in existing
                if (d.get("id") if isinstance(d, dict) else d) not in val
            ]
        else:
            merged[key] = val

    # Carry over the cell's own non-inheritance fields (excluding meta)
    for key, val in cell.items():
        if key in ("_inherits_from", "_overrides", "_status", "_doc"):
            continue
        if key not in merged or merged[key] != val:
            merged[key] = val

    return merged


def _filing_apply_natural_person_filter(
    docs: list, blocklist_exact: list, blocklist_aliases: list
) -> tuple:
    """Filter doc list. Returns (clean_docs, warnings)."""
    if not isinstance(docs, list):
        return docs, []

    clean = []
    warnings = []
    blocklist_aliases_lower = [a.lower() for a in blocklist_aliases]

    for d in docs:
        if isinstance(d, dict):
            doc_id = d.get("id", "")
            doc_notes = d.get("notes", "")
        else:
            doc_id = str(d)
            doc_notes = ""

        # Layer 1: exact ID match
        if doc_id in blocklist_exact:
            warnings.append({
                "doc_id": doc_id,
                "reason": "blocklisted for natural_person plaintiff (exact match)",
                "guard": "R10",
            })
            continue

        # Layer 2: alias check in id or notes
        combined_text = f"{doc_id} {doc_notes}".lower()
        alias_hit = next(
            (a for a in blocklist_aliases_lower if a in combined_text),
            None,
        )
        if alias_hit:
            warnings.append({
                "doc_id": doc_id,
                "reason": f"blocklisted for natural_person plaintiff (alias match: '{alias_hit}')",
                "guard": "R10",
            })
            continue

        clean.append(d)

    return clean, warnings


def _filing_apply_phrase_blacklist(text: str, phrase_blacklist: list) -> list:
    """Scan text for hallucinated phrases. Returns list of hits."""
    if not text or not phrase_blacklist:
        return []
    text_lower = text.lower()
    return [p for p in phrase_blacklist if p.lower() in text_lower]


@mcp.tool()
def filing_required_docs(
    court: str,
    case_type: str,
    filing_type: str,
    plaintiff_type: str = "natural_person",
) -> dict:
    """Look up the required filing artifacts for a (court, case_type, filing_type, plaintiff_type) cell.

    Args:
        court: e.g. "edpa", "phila_ccp", "pa_dobi"
        case_type: e.g. "foia_privacy_act", "fcra", "bad_faith", "contract"
        filing_type: e.g. "complaint", "amended_complaint", "motion", "response_to_motion"
        plaintiff_type: "natural_person" (default) or "entity"

    Returns dict with:
        ok: bool
        cell: { court, case_type, filing_type, plaintiff_type }
        status: "vetted" | "skeleton" | "missing"
        required: [...]
        optional: [...]
        post_clerk_artifacts: [...]
        global_rules_applicable: [...]
        warnings: [...]    (blocklist hits, hallucination flags)
        fee, response_deadline_days, service_clock_days, etc.

    On miss (cell not seeded), returns ok=False with status="missing".
    The agent should then route to Hermes web-fallback skill.
    """
    # Validate inputs
    if plaintiff_type not in _FILING_VALID_PLAINTIFF_TYPES:
        return {
            "ok": False,
            "error": f"plaintiff_type must be one of {_FILING_VALID_PLAINTIFF_TYPES}",
        }

    court = (court or "").strip().lower()
    case_type = (case_type or "").strip().lower()
    filing_type = (filing_type or "").strip().lower()

    if not (court and case_type and filing_type):
        return {
            "ok": False,
            "error": "court, case_type, and filing_type are all required",
        }

    # Load DB
    try:
        db = _filing_load_db()
    except Exception as e:
        return {"ok": False, "error": f"failed to load filing_requirements.json: {e}"}

    blocklist = db.get("blocklist_natural_person", {})
    blocklist_exact = blocklist.get("exact", [])
    blocklist_aliases = blocklist.get("aliases", [])
    phrase_blacklist = db.get("phrase_blacklist", {}).get("items", [])
    global_rules = db.get("global_rules", {})

    # Drill into courts -> case_types -> filing_types
    court_data = db.get("courts", {}).get(court)
    if not court_data:
        return {
            "ok": False,
            "status": "missing",
            "cell": {
                "court": court,
                "case_type": case_type,
                "filing_type": filing_type,
                "plaintiff_type": plaintiff_type,
            },
            "error": f"court '{court}' not seeded in filing_requirements.json",
            "available_courts": list(db.get("courts", {}).keys()),
            "next_step": "route to Hermes web-fallback skill to assemble requirements from authoritative sources",
        }

    case_data = court_data.get("case_types", {}).get(case_type)
    if not case_data:
        return {
            "ok": False,
            "status": "missing",
            "cell": {
                "court": court,
                "case_type": case_type,
                "filing_type": filing_type,
                "plaintiff_type": plaintiff_type,
            },
            "error": f"case_type '{case_type}' not seeded under court '{court}'",
            "available_case_types": list(court_data.get("case_types", {}).keys()),
            "next_step": "route to Hermes web-fallback skill",
        }

    cell = case_data.get(filing_type)
    if not cell:
        return {
            "ok": False,
            "status": "missing",
            "cell": {
                "court": court,
                "case_type": case_type,
                "filing_type": filing_type,
                "plaintiff_type": plaintiff_type,
            },
            "error": f"filing_type '{filing_type}' not seeded under {court}.{case_type}",
            "available_filing_types": list(case_data.keys()),
            "next_step": "route to Hermes web-fallback skill",
        }

    # Resolve inheritance
    cell = _filing_resolve_inheritance(db, cell)

    cell_status = cell.get("_status", "unknown")

    # Pick the right required list per plaintiff_type
    if plaintiff_type == "natural_person":
        required_raw = list(cell.get("required_for_natural_person", []))
        optional_raw = list(cell.get("optional_for_natural_person", []))
    else:  # entity
        required_raw = list(cell.get("required_for_natural_person", []))
        # Entities also get the "additional_for_entity" docs
        required_raw.extend(cell.get("additional_for_entity", []))
        optional_raw = list(cell.get("optional_for_natural_person", []))

    warnings = []

    # Apply natural_person blocklist (R10) only when plaintiff_type=natural_person
    if plaintiff_type == "natural_person":
        required_clean, req_warnings = _filing_apply_natural_person_filter(
            required_raw, blocklist_exact, blocklist_aliases
        )
        optional_clean, opt_warnings = _filing_apply_natural_person_filter(
            optional_raw, blocklist_exact, blocklist_aliases
        )
        warnings.extend(req_warnings)
        warnings.extend(opt_warnings)
    else:
        required_clean = required_raw
        optional_clean = optional_raw

    # Phrase blacklist scan over notes
    notes_concat = " ".join(
        (d.get("notes", "") if isinstance(d, dict) else "")
        for d in (required_clean + optional_clean)
    )
    phrase_hits = _filing_apply_phrase_blacklist(notes_concat, phrase_blacklist)
    if phrase_hits:
        warnings.append({
            "type": "phrase_hallucination_detected",
            "hits": phrase_hits,
            "guard": "R10_phrase_blacklist",
            "action": "review the cell's notes - these phrases should not appear",
        })

    # Determine which global rules apply
    applicable_rules = []
    for rule_id, rule in global_rules.items():
        if not isinstance(rule, dict):
            continue

        # Court applicability
        rule_courts = rule.get("applies_to_courts", [])
        court_applies = (
            not rule_courts
            or court in rule_courts
            or any(
                pattern.startswith("*") and pattern.endswith("*") and "federal" in pattern
                for pattern in rule_courts
            ) and "federal" in (court_data.get("name", "").lower() or "fed" in court)
        )

        # Filing applicability
        rule_filings = rule.get("applies_to_filings", [])
        filing_applies = not rule_filings or filing_type in rule_filings

        if court_applies and filing_applies:
            applicable_rules.append({
                "rule_id": rule_id,
                "rule": rule.get("rule", ""),
                "danger": rule.get("danger"),
            })

    return {
        "ok": True,
        "status": cell_status,
        "cell": {
            "court": court,
            "case_type": case_type,
            "filing_type": filing_type,
            "plaintiff_type": plaintiff_type,
        },
        "required": required_clean,
        "optional": optional_clean,
        "post_clerk_artifacts": cell.get("post_clerk_artifacts", []),
        "fee": cell.get("fee"),
        "fee_recovery_statutes": cell.get("fee_recovery_statutes", []),
        "response_deadline_days": cell.get("response_deadline_days"),
        "service_clock_days": cell.get("service_clock_days"),
        "service_protocol_ref": cell.get("service_protocol_ref"),
        "local_rule_refs": cell.get("local_rule_refs", []),
        "frcp_refs": cell.get("frcp_refs", []),
        "common_pro_se_errors_to_avoid": cell.get("common_pro_se_errors_to_avoid", []),
        "global_rules_applicable": applicable_rules,
        "warnings": warnings,
        "_meta": {
            "source": cell.get("_source", "unknown"),
            "last_verified": cell.get("_last_verified"),
        },
    }


@mcp.tool()
def filing_list_seeded_cells() -> dict:
    """List all (court, case_type, filing_type) cells currently seeded in filing_requirements.json.

    Useful for the agent to know what's covered before falling back to web lookup.
    """
    try:
        db = _filing_load_db()
    except Exception as e:
        return {"ok": False, "error": str(e)}

    cells = []
    for court_id, court_data in db.get("courts", {}).items():
        if not isinstance(court_data, dict):
            continue
        for case_id, case_data in court_data.get("case_types", {}).items():
            if not isinstance(case_data, dict):
                continue
            for filing_id, filing_data in case_data.items():
                if not isinstance(filing_data, dict):
                    continue
                cells.append({
                    "court": court_id,
                    "case_type": case_id,
                    "filing_type": filing_id,
                    "status": filing_data.get("_status", "unknown"),
                    "source": filing_data.get("_source", ""),
                })

    return {
        "ok": True,
        "count": len(cells),
        "cells": cells,
        "summary_by_status": {
            "vetted": len([c for c in cells if c["status"] == "vetted"]),
            "skeleton": len([c for c in cells if c["status"] == "skeleton"]),
            "other": len([c for c in cells if c["status"] not in ("vetted", "skeleton")]),
        },
    }








@mcp.tool()
def case_audit_summary(case_id: str, top_n: int = 10) -> dict:
    """Slim summary of case audit findings. Use INSTEAD of case_audit_full in chat for Qwen context safety.

    KNOWN ISSUE (2026-05-22): pipeline.case_audit_engine.audit_case currently mis-flattens scan
    schema keys (source_doc, role, violations_count, etc) as if they were legal violations.
    This wrapper instead delegates to case_brief(section='violations') which reads the
    hand-curated scorecard markdown — the actual source of truth for substantive defect data.
    Fix the audit engine separately."""
    try:
        brief_fn = case_brief.fn if hasattr(case_brief, "fn") else case_brief
        brief = brief_fn(case_id=case_id, section="violations")
    except Exception as e:
        return {"error": f"case_brief delegation failed: {e}", "case_id": case_id}

    # case_brief returns a string of scorecard markdown
    text = brief if isinstance(brief, str) else ""
    if not text and isinstance(brief, dict):
        for k in ("content", "text", "section_text", "violations", "body", "result"):
            v = brief.get(k)
            if isinstance(v, str) and v:
                text = v
                break

    import re as _re

    def _grab_int(pattern):
        m = _re.search(pattern, text, _re.IGNORECASE)
        return int(m.group(1)) if m else 0

    total = (_grab_int(r"Total Violations Found:\s*(\d+)")
             or _grab_int(r"total[^0-9]{0,30}(\d+)\s*violations")
             or _grab_int(r"(\d+)\s*violations\s+found")
             or 0)
    fatal = _grab_int(r"fatal[^0-9]{0,15}(\d+)") or _grab_int(r"(\d+)\s*fatal")
    serious = _grab_int(r"serious[^0-9]{0,15}(\d+)") or _grab_int(r"(\d+)\s*serious")

    # Per-defendant counts from "**Name**: N violations"
    defendants = {}
    for m in _re.finditer(r"\*\*([^*]+)\*\*:\s*(\d+)\s*violations", text):
        defendants[m.group(1).strip()] = int(m.group(2))

    # Category headers (### FCRA Violations, ### Due Process Violations, etc.)
    categories = _re.findall(r"###\s+([^\n]+?)\s+Violations", text)

    return {
        "case_id": case_id,
        "counts": {
            "total": total,
            "fatal": fatal,
            "serious": serious,
        },
        "by_defendant": defendants,
        "categories": categories,
        "brief_section_chars": len(text),
        "brief_preview": text[:1500] if text else "",
        "note": "Source: case_brief(section='violations'). For full audit (cron only): compose_diagnose. Raw audit engine is currently broken — see docstring.",
        "top_n_requested": top_n,
    }

@mcp.tool()
def case_ground_truth(case_id: str) -> dict:
    """Return the structured ground-truth YAML for a case. Generic, case-agnostic.
    Reads from cases/<slug>/meta/CASE_GROUND_TRUTH.yaml.
    Hermes MUST call this BEFORE drafting or inferring case posture.
    """
    import yaml
    from pathlib import Path
    slug = case_id.replace(":", "_").replace("/", "_").replace("fed_2_", "fed_")
    gt_path = OBSIDIAN_VAULT / "cases" / slug / "meta" / "CASE_GROUND_TRUTH.yaml"
    if not gt_path.exists():
        return {
            "error": "CASE_GROUND_TRUTH.yaml not found",
            "case_id": case_id,
            "expected_path": str(gt_path),
            "remedy": "Run _create_ground_truth_schema.py to scaffold, then populate fields.",
        }
    try:
        data = yaml.safe_load(gt_path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"error": f"yaml parse failed: {e}", "case_id": case_id}
    # Diagnostic: flag unpopulated required fields
    missing = []
    if not data.get("judge", {}).get("name"): missing.append("judge.name")
    if not data.get("latest_order", {}).get("dkt_no"): missing.append("latest_order.dkt_no")
    if not data.get("caption", {}).get("defendants"): missing.append("caption.defendants")
    data["_meta"] = {"populated": len(missing) == 0, "missing_required": missing}
    return data



@mcp.tool()
def compose_diagnose_summary(case_id: str) -> dict:
    """Lightweight version of compose_diagnose for chat agents.
    Returns counts and top items only (max ~2k tokens).
    Use this in interactive Hermes workflows. Cron/batch jobs keep using compose_diagnose."""
    try:
        full = compose_diagnose(case_id)
    except Exception as e:
        return {"error": f"compose_diagnose failed: {e}", "case_id": case_id}
    if not isinstance(full, dict):
        return {"error": "unexpected compose_diagnose return type", "case_id": case_id}
    summary = {
        "case_id": case_id,
        "posture": full.get("posture"),
        "counts": {
            "blocks_available":     len(full.get("blocks_available", []) or []),
            "blocks_triggered":     len(full.get("blocks_triggered", []) or []),
            "blocks_skipped":       len(full.get("blocks_skipped", []) or []),
            "defects_total":        full.get("defect_total") or (full.get("diagnosis") or {}).get("defect_total"),
            "fatal_count":          full.get("fatal_count")  or (full.get("diagnosis") or {}).get("fatal_count"),
            "recommended_filings":  len(full.get("recommended_filings", []) or []),
        },
        "top_recommended_filings": (full.get("recommended_filings", []) or [])[:5],
        "blocks_triggered_top":    (full.get("blocks_triggered", []) or [])[:10],
        "blocks_skipped_top":      (full.get("blocks_skipped", []) or [])[:5],
        "note": "Counts + top items only. For full data call compose_diagnose (cron-safe, ~80KB).",
    }
    return summary



# ============================================================================
# PHASE 4 TOOLS (block-stitcher, exhibit cover, exhibit package)
# Registered 2026-05-22.
# ============================================================================

@mcp.tool()
def build_motion_from_blocks(
    case_id: str,
    filing_type: str,
    court_id: str = "EDPA",
    extra_context: dict = None,
    body_md: str = None,
) -> dict:
    """Build a filing from obsidian template blocks using the filing-type YAML.

    Calls generation.md_to_docx.build_filing_from_blocks with the
    case_ground_truth loaded from CASE_GROUND_TRUTH.yaml.

    Args:
        case_id: e.g. "fed:2:26-cv-00123"
        filing_type: must match a file in obsidian_sync/templates/filing_types/
                     e.g. "motion_for_reconsideration", "amended_complaint"
        court_id: EDPA | 3D_CIR | SCOTUS | PA_CP_PHILA | PA_SUPER | PA_COMMWLTH | PA_SUPREME
        extra_context: optional dict merged into context (e.g. defect_categories)
        body_md: optional pre-rendered markdown body inserted after procedural_history

    Returns: {"ok": bool, "out_path": str, "blocks_used": [...], "blocks_skipped": [...]}
    """
    import yaml as _yaml
    from pathlib import Path as _Path
    from datetime import datetime as _dt
    try:
        from generation.md_to_docx import build_filing_from_blocks
    except Exception as e:
        return {"ok": False, "error": f"renderer import failed: {e}"}

    slug = case_id.replace(":", "_").replace("/", "_").replace("fed_2_", "fed_")
    gt_path = OBSIDIAN_VAULT / "cases" / slug / "meta" / "CASE_GROUND_TRUTH.yaml"
    if not gt_path.exists():
        return {"ok": False, "error": f"CASE_GROUND_TRUTH.yaml not found at {gt_path}",
                "case_id": case_id}
    try:
        gt = _yaml.safe_load(gt_path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        return {"ok": False, "error": f"YAML parse failed: {e}"}

    out_dir = OBSIDIAN_VAULT / "cases" / slug / "drafts"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = _dt.now().strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"{filing_type}_{ts}.docx"

    result = build_filing_from_blocks(
        filing_type=filing_type,
        case_ground_truth=gt,
        out_path=str(out_path),
        court_id=court_id,
        extra_context=extra_context or {},
        body_md=body_md,
    )
    result["case_id"] = case_id
    return result


@mcp.tool()
def generate_exhibit_cover(
    letter: str,
    title: str,
    date: str = "",
    case_id: str = "",
    court_id: str = "EDPA",
) -> dict:
    """Generate a single-page .docx exhibit cover sheet.

    Layout (centered): court caption | EXHIBIT <letter> | <title> | Dated: <date>

    Args:
        letter: A, B, C, ...
        title: e.g. "FOIA Request"
        date: ISO date string or human-readable
        case_id: optional, used to fetch caption from CASE_GROUND_TRUTH
        court_id: court rules for font + margins

    Returns: {"ok": bool, "out_path": str}
    """
    import yaml as _yaml
    from pathlib import Path as _Path
    from datetime import datetime as _dt
    from docx import Document as _Doc
    from docx.shared import Pt as _Pt, Inches as _In
    from docx.enum.text import WD_ALIGN_PARAGRAPH as _AL

    try:
        from generation.md_to_docx import COURT_RULES as _CR
    except Exception as e:
        return {"ok": False, "error": f"renderer import failed: {e}"}

    rules = _CR.get(court_id, _CR["EDPA"])

    caption_lines = rules["court_full"].split("\n")
    plaintiff_name = "PLAINTIFF"
    defendant_names = "et al., Defendants"
    if case_id:
        slug = case_id.replace(":", "_").replace("/", "_").replace("fed_2_", "fed_")
        gt_path = OBSIDIAN_VAULT / "cases" / slug / "meta" / "CASE_GROUND_TRUTH.yaml"
        if gt_path.exists():
            try:
                gt = _yaml.safe_load(gt_path.read_text(encoding="utf-8")) or {}
                cap = gt.get("caption") or {}
                plaintiff_name = (cap.get("plaintiff") or "PLAINTIFF").upper()
                dlist = cap.get("defendants") or []
                if isinstance(dlist, list) and dlist:
                    first = dlist[0]
                    name = first.get("name") if isinstance(first, dict) else str(first)
                    defendant_names = f"{name.upper()}, et al., Defendants" if name else defendant_names
            except Exception:
                pass

    doc = _Doc()
    for s in doc.sections:
        s.page_width = _In(8.5); s.page_height = _In(11)
        s.top_margin = _In(rules["margin_in"]); s.bottom_margin = _In(rules["margin_in"])
        s.left_margin = _In(rules["margin_in"]); s.right_margin = _In(rules["margin_in"])
    style = doc.styles["Normal"]
    style.font.name = rules["font"]
    style.font.size = _Pt(rules["size_pt"])

    for line in caption_lines:
        p = doc.add_paragraph()
        p.alignment = _AL.CENTER
        r = p.add_run(line); r.bold = True; r.font.size = _Pt(rules["size_pt"])
    doc.add_paragraph()
    p = doc.add_paragraph()
    p.alignment = _AL.CENTER
    r = p.add_run(f"{plaintiff_name}, Plaintiff"); r.font.size = _Pt(rules["size_pt"])
    p = doc.add_paragraph()
    p.alignment = _AL.CENTER
    r = p.add_run("v."); r.bold = True; r.font.size = _Pt(rules["size_pt"])
    p = doc.add_paragraph()
    p.alignment = _AL.CENTER
    r = p.add_run(defendant_names); r.font.size = _Pt(rules["size_pt"])

    for _ in range(6):
        doc.add_paragraph()

    p = doc.add_paragraph()
    p.alignment = _AL.CENTER
    r = p.add_run(f"EXHIBIT {letter.upper()}")
    r.bold = True; r.font.size = _Pt(36)
    doc.add_paragraph()
    p = doc.add_paragraph()
    p.alignment = _AL.CENTER
    r = p.add_run(title)
    r.bold = True; r.font.size = _Pt(18)
    if date:
        doc.add_paragraph()
        p = doc.add_paragraph()
        p.alignment = _AL.CENTER
        r = p.add_run(f"Dated: {date}")
        r.font.size = _Pt(rules["size_pt"] + 2)

    if case_id:
        slug = case_id.replace(":", "_").replace("/", "_").replace("fed_2_", "fed_")
        out_dir = OBSIDIAN_VAULT / "cases" / slug / "exhibits"
    else:
        out_dir = OBSIDIAN_VAULT / "tmp_exhibits"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = _dt.now().strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"exhibit_{letter.upper()}_cover_{ts}.docx"
    doc.save(str(out_path))
    return {"ok": True, "out_path": str(out_path), "letter": letter.upper(), "title": title}


@mcp.tool()
def build_exhibit_package(
    case_id: str,
    intake_dir: str = "",
) -> dict:
    """Build the combined exhibits PDF for a case using case.md exhibit entries.

    Pulls source files from intake_dir (e.g. C:/Users/malic/Documents/<case>/intake)
    plus the default unmatched_emails/ + case attachments folder.

    Args:
        case_id: e.g. "fed:2:26-cv-00123"
        intake_dir: full path to intake folder; overrides default search

    Returns: {"ok": bool, "exhibits_pdf": str, "files_matched": int, "total_pages": int}
    """
    from pathlib import Path as _Path
    try:
        from generation.exhibit_packager import build_filing_package
        from exhibit_resolver import resolve_case_exhibits
    except Exception as e:
        return {"ok": False, "error": f"package import failed: {e}"}

    slug = case_id.replace(":", "_").replace("/", "_").replace("fed_2_", "fed_")
    case_md = OBSIDIAN_VAULT / "cases" / slug / "case.md"
    if not case_md.exists():
        return {"ok": False, "error": f"case.md not found at {case_md}"}

    try:
        exhibits_map = resolve_case_exhibits(case_md)
    except Exception as e:
        return {"ok": False, "error": f"exhibit resolution failed: {e}"}

    uploaded_files = []
    if intake_dir:
        intake_p = _Path(intake_dir)
        if intake_p.exists():
            uploaded_files = [str(p) for p in intake_p.rglob("*") if p.is_file()]

    exhibits_list = []
    for letter, path in (exhibits_map or {}).items():
        exhibits_list.append({
            "letter": letter,
            "description": path.stem if hasattr(path, "stem") else str(path),
            "date": "",
        })

    out_dir = OBSIDIAN_VAULT / "cases" / slug / "exhibits"
    out_dir.mkdir(parents=True, exist_ok=True)
    result = build_filing_package(
        complaint_docx=str(case_md),
        exhibits=exhibits_list,
        uploaded_files=uploaded_files,
        output_dir=str(out_dir),
        case_id=slug,
    )
    return {
        "ok": True,
        "exhibits_pdf": result.get("exhibits_pdf"),
        "files_matched": result.get("files_matched", 0),
        "total_pages": result.get("total_pages", 0),
        "exhibits_count": len(exhibits_list),
    }

# ============================================================================
# END PHASE 4 TOOLS
# ============================================================================




# ============================================================
# Phase 2: resolve_citation — single tool, two modes
# ============================================================
@mcp.tool()
def resolve_citation(
    citation: str = "",
    namespace_hint: str = "",
    mode: str = "lookup",
    limit: int = 20,
    include_text: bool = False,
    text: str = "",
    source_chunk_id: str = "",
    treatment: str = "cited",
) -> dict:
    """Resolve citation to chunks (lookup) OR extract citations from chunk text (extract).

    Backed by beezle_structure.db (authority + case_index) — SQLite-first,
    Chroma metadata fallback for statutes/regs only. Cases route through
    case_index exclusively.

    LOOKUP mode (default):
        citation: e.g. "26 USC § 6330(b)(2)" or "Marbury v. Madison, 5 U.S. 137"
        namespace_hint: optional, e.g. "usc_t26" (parser wins on conflict)
        limit: max chunks (default 20)
        include_text: also fetch chunk text from Chroma (slower)
        Returns: {namespace, canonical, authority_id, chunk_ids, chunks,
                  source: "sqlite"|"chroma_meta"|"not_found", latency_ms}

    DIRECT_LOOKUP mode (mode="direct_lookup"):
        Phase 5.1 — deterministic SQLite-only lookup, no vector search.
        Faster (5-10ms vs 50-120ms), returns overruled flag + treatment summary.
        Best for: known citations, validity checks, citation normalization.
        Handles citation variants: "133 U.S. 333" ↔ "133 U. S. 333", etc.
        Returns: {mode, found, authority_id, canonical, kind, tier, chunk_ids,
                  overruled_flag, status_warning, status_message,
                  treatment_summary, latency_ms}

    EXTRACT mode (mode="extract"):
        text: raw text containing citations to extract
        source_chunk_id: Chroma chunk_id this text comes from (REQUIRED)
        treatment: edge treatment label (default "cited"; Phase 3 will add
                   followed/distinguished/overruled)
        Returns: {citations_found, edges_written, edges_skipped,
                  extracted_citations, latency_ms}

    Performance: p50<50ms, p99<120ms across ALL namespaces (lookup, no text).
    Idempotent: extract on same chunk N times writes edges only once.

    Hard guarantees:
        - USC query NEVER returns CFR chunks (and vice versa)
        - Case-namespace queries NEVER fall through to Chroma (case_index only)
        - Garbage input returns {source: "not_found", error: "unparseable"}
    """
    return resolve_citation_impl(
        citation=citation,
        namespace_hint=namespace_hint,
        mode=mode,
        limit=limit,
        include_text=include_text,
        text=text,
        source_chunk_id=source_chunk_id,
        treatment=treatment,
    )


# ─── Phase D: closed-loop full filing package ────────────────────────────────
# Merges compose_and_draft + _resolve_auto (status keywords) +
# recommend_filing_types (B5 posture/case_type scoring) + batching.
# One call → 1 or N filings, each with md + docx + optional scorecard.

# Status keyword → filing_type rules (ported from run_filings_all.py:194)
_PHASE_D_STATUS_RULES = [
    ("appellate",                          "appellate_reply_brief"),
    ("appeal",                             "appellate_reply_brief"),
    ("mandamus",                           "petition_for_writ_of_mandamus"),
    ("default",                            "motion_to_strike_state"),
    ("judgment_by_default",                "motion_to_strike_state"),
    ("enforc",                             "motion_to_strike_state"),
    ("garnish",                            "motion_for_garnishee_default"),
    ("dismiss",                            "motion_to_vacate"),
    ("mtd_pending",                        "opposition_to_motion_to_dismiss"),
    ("mtd_granted",                        "motion_to_vacate"),
    ("54b",                                "motion_to_certify_54b"),
    ("rule_54",                            "motion_to_certify_54b"),
    ("reconsider",                         "motion_for_reconsideration"),
]
_PHASE_D_DEFAULT_FILING = "motion_for_reconsideration"


def _phase_d_resolve_auto(case_id: str, registry_status: str = "") -> list[str]:
    """Pick filing_type(s) from status keywords. Always returns at least one.

    Specificity-first matching: rules are tried in descending keyword length so
    that `judgment_by_default` beats `default` and `appellate` beats `appeal`.
    Returns ALL matching filing types (deduplicated, ordered by specificity),
    skipping shorter keywords whose match span is already covered by a longer
    keyword's match. Falls back to _PHASE_D_DEFAULT_FILING if nothing matches.
    """
    status = (registry_status or "").lower()
    if not status:
        return [_PHASE_D_DEFAULT_FILING]

    # Sort rules by keyword length descending; original list order breaks ties
    indexed = list(enumerate(_PHASE_D_STATUS_RULES))
    ranked = sorted(indexed, key=lambda item: (-len(item[1][0]), item[0]))

    matched_filings: list[str] = []
    covered_spans: list[tuple[int, int]] = []  # (start, end) of consumed substrings

    def _is_covered(start: int, end: int) -> bool:
        return any(s <= start and end <= e for (s, e) in covered_spans)

    for _, (kw, ft) in ranked:
        idx = status.find(kw)
        if idx < 0:
            continue
        end = idx + len(kw)
        if _is_covered(idx, end):
            continue  # shorter keyword fully inside an already-matched span
        covered_spans.append((idx, end))
        if ft not in matched_filings:
            matched_filings.append(ft)

    if not matched_filings:
        return [_PHASE_D_DEFAULT_FILING]
    return matched_filings


def _phase_d_compose_one(case_id: str, filing_type: str,
                          with_scorecard: bool, extra_context: str,
                          out_dir, ts: str) -> dict:
    """Core single-filing compose+render+docx+scorecard. Returns dict per filing.

    Extracted from compose_and_draft body so it can be called in a loop
    without re-invoking the @mcp.tool wrapper (which would re-export it).
    """
    import sys as _sys
    from datetime import datetime as _dt
    from pathlib import Path as _Path

    if str(BASE_PATH) not in _sys.path:
        _sys.path.insert(0, str(BASE_PATH))
    from pipeline.filing_composer import (
        compose_filing, render_composition_to_markdown, build_context,
    )

    try:
        composition = compose_filing(case_id, filing_type)
    except Exception as e:
        return {
            "filing_type": filing_type,
            "status": "compose_error",
            "error": f"compose_filing raised: {e}",
        }

    if composition.errors:
        return {
            "filing_type": filing_type,
            "status": "compose_error",
            "error": f"compose_filing errors: {composition.errors}",
        }

    md_text = render_composition_to_markdown(composition)
    if extra_context and extra_context.strip():
        md_text += chr(10) + "## CASE-SPECIFIC ARGUMENTS" + chr(10) + chr(10)
        md_text += extra_context.strip() + chr(10)

    if not md_text.strip():
        return {
            "filing_type": filing_type,
            "status": "empty_render",
            "error": "composition produced empty markdown",
            "blocks_composed": len(composition.blocks),
            "blocks_skipped": len(composition.skipped_blocks),
        }

    slug = case_id.replace(":", "_").replace("/", "_")
    md_filename = f"{filing_type}_{slug}_{ts}.md"
    docx_filename = f"{filing_type}_{slug}_{ts}.docx"
    md_path = out_dir / md_filename
    md_path.write_text(md_text, encoding="utf-8")

    # docx render with court_id resolution (same logic as compose_and_draft)
    court_id = "EDPA"
    try:
        ctx = build_context(case_id)
        registry = ctx.get("registry", {}) if isinstance(ctx, dict) else {}
        court_id = registry.get("court_id") or registry.get("court") or "EDPA"
        court_id_upper = str(court_id).upper()
        if "EASTERN DISTRICT" in court_id_upper:
            court_id = "EDPA"
        elif "THIRD CIRCUIT" in court_id_upper or "3D CIR" in court_id_upper:
            court_id = "3D_CIR"
        elif "COMMON PLEAS" in court_id_upper or "PHILADELPHIA" in court_id_upper:
            court_id = "PA_CP_PHILA"
        elif "SUPREME COURT" in court_id_upper:
            court_id = "SCOTUS"
    except Exception:
        court_id = "EDPA"

    # ground-truth court override
    try:
        import yaml as _yaml
        _slug = case_id.replace(":", "_").replace("/", "_").replace("fed_2_", "fed_")
        _gt_path = OBSIDIAN_VAULT / "cases" / _slug / "meta" / "CASE_GROUND_TRUTH.yaml"
        if _gt_path.exists():
            _gt = _yaml.safe_load(_gt_path.read_text(encoding="utf-8")) or {}
            _court_from_gt = (_gt.get("court") or {}).get("id") if isinstance(_gt.get("court"), dict) else None
            if _court_from_gt:
                court_id = _court_from_gt
    except Exception:
        pass

    docx_path = out_dir / docx_filename
    docx_status = "ok"
    try:
        render_md_to_docx(md_text, str(docx_path), court_id=court_id)
    except Exception as docx_err:
        docx_status = f"docx_failed: {docx_err}"
        docx_path = md_path  # fallback

    # optional scorecard
    sc_path = ""
    if with_scorecard:
        try:
            from beezle.generation.drafter import draft_for_case
            _, _, sc = draft_for_case(
                canonical_id=case_id,
                base_path=BASE_PATH,
                output_dir=out_dir,
                with_scorecard=True,
                filing_type=filing_type,
            )
            sc_path = str(sc) if sc else ""
        except Exception as sc_err:
            sc_path = f"scorecard_failed: {sc_err}"

    # copy to case folder if exists
    try:
        case_slug = slug.replace("fed_2_", "fed_")
        filings_dir = OBSIDIAN_VAULT / "cases" / case_slug / "filings"
        if filings_dir.exists() and docx_path != md_path:
            shutil.copy2(str(docx_path), str(filings_dir / docx_filename))
    except Exception:
        pass

    return {
        "filing_type": filing_type,
        "status": "complete",
        "md_path": str(md_path),
        "docx_path": str(docx_path),
        "scorecard_path": sc_path,
        "court_id": court_id,
        "docx_status": docx_status,
        "blocks_composed": len(composition.blocks),
        "blocks_skipped": len(composition.skipped_blocks),
        "warnings": composition.warnings,
    }


@mcp.tool
def case_full_filing_package(
    case_id: str,
    mode: str = "auto",
    filing_type: str = "",
    top_n: int = 3,
    with_scorecard: bool = True,
    extra_context: str = "",
) -> dict:
    """Closed-loop full-package filing generator. One call → N filings.

    Modes:
      - "auto": resolve ONE filing_type from registry status keywords (default)
      - "all_recommended": use Phase B5 recommend_filing_types ranking, draft top_n
      - "explicit": use the filing_type arg verbatim (must be non-empty)

    For each resolved filing_type: compose → render md → render docx →
    optional scorecard → copy to case/filings/. All artifacts land in
    obsidian_sync/cases/<slug>/drafts/run_<ts>/ (Session 18 repoint).
    Final-filing copies still mirror to obsidian_sync/cases/<slug>/filings/.

    Args:
        case_id: canonical case id (e.g. 'fed:2:26-cv-00123')
        mode: 'auto' | 'all_recommended' | 'explicit'
        filing_type: only used when mode='explicit'
        top_n: how many filings to draft in 'all_recommended' mode (default 3)
        with_scorecard: attach stare decisis scorecard to each filing
        extra_context: appended as "## CASE-SPECIFIC ARGUMENTS" to each md

    Returns: {case_id, mode, posture, case_type, registry_status,
              filings_requested, filings_count, filings: [...], errors}
    """
    import sys as _sys
    import json as _json
    from datetime import datetime as _dt

    try:
        if str(BASE_PATH) not in _sys.path:
            _sys.path.insert(0, str(BASE_PATH))
        from pipeline.filing_composer import recommend_filing_types, build_context
        from pipeline.case_loader import load_case

        # Resolve context once
        case = load_case(case_id)
        if not case or not case.get("canonical_id"):
            return {
                "error": f"case_id {case_id!r} not found in registry",
                "case_id": case_id,
            }

        registry_status = case.get("status") or ""
        posture = case.get("posture") or ""
        case_type = case.get("case_type") or ""

        # Resolve which filing_type(s) to draft
        if mode == "explicit":
            if not filing_type:
                return {
                    "error": "mode='explicit' requires non-empty filing_type",
                    "case_id": case_id,
                }
            filings_requested = [filing_type]
        elif mode == "all_recommended":
            try:
                ranked = recommend_filing_types(case_id, top_n=top_n)
                filings_requested = [r["id"] for r in ranked if r.get("id")]
                if not filings_requested:
                    filings_requested = _phase_d_resolve_auto(case_id, registry_status)
            except Exception as e:
                return {
                    "error": f"recommend_filing_types failed: {e}",
                    "case_id": case_id,
                    "mode": mode,
                }
        elif mode == "auto":
            filings_requested = _phase_d_resolve_auto(case_id, registry_status)
        else:
            return {
                "error": f"unknown mode {mode!r} (use 'auto', 'all_recommended', or 'explicit')",
                "case_id": case_id,
            }

        # Deduplicate while preserving order
        seen = set()
        filings_requested = [f for f in filings_requested if not (f in seen or seen.add(f))]

        # Prep output dir + timestamp
        # Session 18: drafts land directly in obsidian_sync/cases/<slug>/drafts/<ts>/
        # (replaces legacy output/reply_briefs/). The mirror-to-filings/ step
        # inside _phase_d_compose_one remains for human-approved final filings.
        ts = _dt.now().strftime("%Y%m%d_%H%M%S")
        _case_slug = case_id.replace(":", "_").replace("/", "_").replace("fed_2_", "fed_")
        out_dir = OBSIDIAN_VAULT / "cases" / _case_slug / "drafts" / f"run_{ts}"
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            # fallback: if cases folder missing, use legacy path
            out_dir = REPLY_BRIEFS_DIR
            out_dir.mkdir(parents=True, exist_ok=True)

        # Loop: draft each filing
        filings_out = []
        errors = []
        for i, ft in enumerate(filings_requested):
            # bump timestamp per filing to avoid filename collision
            ft_ts = _dt.now().strftime("%Y%m%d_%H%M%S") + (f"_{i}" if i > 0 else "")
            result = _phase_d_compose_one(
                case_id=case_id,
                filing_type=ft,
                with_scorecard=with_scorecard,
                extra_context=extra_context,
                out_dir=out_dir,
                ts=ft_ts,
            )
            filings_out.append(result)
            if result.get("status") != "complete":
                errors.append({"filing_type": ft, "status": result.get("status"), "error": result.get("error")})

        complete_count = sum(1 for f in filings_out if f.get("status") == "complete")

        return {
            "case_id": case_id,
            "mode": mode,
            "posture": posture,
            "case_type": case_type,
            "registry_status": registry_status,
            "filings_requested": filings_requested,
            "filings_count": complete_count,
            "filings_count_total": len(filings_out),
            "filings": filings_out,
            "errors": errors,
        }

    except Exception as e:
        return {
            "error": f"case_full_filing_package failed: {e}",
            "trace": traceback.format_exc()[:1500],
            "case_id": case_id,
            "mode": mode,
        }
# ─── end Phase D ─────────────────────────────────────────────────────────────

# ─── Phase G (Session 19): case_reanalyze + approve_case_ground_truth ───────
@mcp.tool
def case_reanalyze(case_id: str) -> dict:
    """Re-read a case's intake corpus and propose an updated CASE_GROUND_TRUTH.yaml.

    Does NOT write GT. Stages a diff at pipeline/reanalysis_diffs/<diff_id>.json
    that the caller can review and pass to approve_case_ground_truth.

    Args:
        case_id: canonical id (e.g. 'fed:2:26-cv-00123', 'phila_ccp:260100000')

    Returns: {ok, diff_id, case_id, diff_path, has_changes, summary, preview_changes,
              preserved_fields}
    """
    import sys as _sys
    if str(BASE_PATH) not in _sys.path:
        _sys.path.insert(0, str(BASE_PATH))
    try:
        from pipeline.case_reanalysis import case_reanalyze as _impl
        return _impl(case_id)
    except Exception as e:
        return {
            "ok": False,
            "error": f"case_reanalyze failed: {e}",
            "trace": traceback.format_exc()[:1500],
            "case_id": case_id,
        }


@mcp.tool
def approve_case_ground_truth(case_id: str, diff_id: str) -> dict:
    """Accept a staged reanalysis diff: backup + write GT, then run drafter-check-all.

    Backs up the current YAML to <gt>.bak.pre_reanalyze_<ts>, writes the proposed
    YAML, then invokes case_full_filing_package(mode='all_recommended', top_n=5)
    to verify all filings still validate.

    Args:
        case_id: canonical id matching the diff's case_id
        diff_id: id returned by case_reanalyze

    Returns: {ok, case_id, diff_id, backup_path, gt_path, drafter_check, applied_to}
    """
    import sys as _sys
    if str(BASE_PATH) not in _sys.path:
        _sys.path.insert(0, str(BASE_PATH))
    try:
        from pipeline.case_reanalysis import approve_case_ground_truth as _impl
        return _impl(case_id, diff_id)
    except Exception as e:
        return {
            "ok": False,
            "error": f"approve_case_ground_truth failed: {e}",
            "trace": traceback.format_exc()[:1500],
            "case_id": case_id,
            "diff_id": diff_id,
        }
# ─── end Phase G ─────────────────────────────────────────────────────────────







# ─── Phase H (Session 20): obsidian_graph MCP tools ────────────────────────
# Native Obsidian wikilink graph queries. See pipeline/obsidian_graph.py for
# implementation. All tools are thin wrappers — they import the impl, call it,
# and return. Heavy lifting (graph build, caching) lives in the impl module.

def _import_obsidian_graph():
    """Lazy import + sys.path safety."""
    import sys as _sys
    if str(BASE_PATH) not in _sys.path:
        _sys.path.insert(0, str(BASE_PATH))
    from pipeline import obsidian_graph as _og
    return _og


@mcp.tool
def obsidian_backlinks(note_path: str, folder_scope: list = None) -> dict:
    """List notes that link TO note_path.

    Reads from cached wikilink index (rebuilds if >24h stale).

    Args:
        note_path: relative path under obsidian_sync/, e.g. 'Defendants/Equifax.md'
        folder_scope: optional list of top-level folders to restrict to.
                      Default excludes primary_sources, unmatched_emails, etc.

    Returns: {note_path, backlinks_count, backlinks: [paths], scope}
    """
    try:
        og = _import_obsidian_graph()
        return og.obsidian_backlinks(note_path, folder_scope=folder_scope)
    except Exception as e:
        return {"error": f"obsidian_backlinks failed: {e}",
                "trace": traceback.format_exc()[:1500]}


@mcp.tool
def obsidian_outgoing_links(note_path: str, folder_scope: list = None) -> dict:
    """List wikilinks FROM note_path with edge types and resolution status.

    Args:
        note_path: relative path under obsidian_sync/
        folder_scope: optional folder restriction (default excludes noise)

    Returns: {note_path, outgoing_count, resolved_count, unresolved_count,
              outgoing: [{target, edge_type, pin_cite, resolved_to}, ...]}
    """
    try:
        og = _import_obsidian_graph()
        return og.obsidian_outgoing_links(note_path, folder_scope=folder_scope)
    except Exception as e:
        return {"error": f"obsidian_outgoing_links failed: {e}",
                "trace": traceback.format_exc()[:1500]}


@mcp.tool
def obsidian_graph_walk(
    start: str,
    depth: int = 2,
    edge_types: list = None,
    folder_scope: list = None,
    max_nodes: int = 200,
) -> dict:
    """BFS walk from `start` note through resolved wikilinks.

    Args:
        start: relative note path (e.g. 'Defendants/Equifax.md')
        depth: hops outward (default 2)
        edge_types: filter to specific edge types (e.g. ['authority', 'implements'])
        folder_scope: optional folder restriction
        max_nodes: hard cap on visited nodes

    Returns: {start, depth, visited_count, edges_traversed_count, capped,
              by_depth: {depth: [paths]}, edges: [...]}
    """
    try:
        og = _import_obsidian_graph()
        return og.obsidian_graph_walk(
            start=start, depth=depth, edge_types=edge_types,
            folder_scope=folder_scope, max_nodes=max_nodes,
        )
    except Exception as e:
        return {"error": f"obsidian_graph_walk failed: {e}",
                "trace": traceback.format_exc()[:1500]}


@mcp.tool
def obsidian_search_by_tag(
    tag: str,
    frontmatter_only: bool = True,
    folder_scope: list = None,
    limit: int = 500,
) -> dict:
    """Find notes with a given tag.

    Args:
        tag: tag string (with or without #)
        frontmatter_only: True ignores inline #hashtags in body (default True)
        folder_scope: optional folder restriction
        limit: max matches to return

    Returns: {tag, scanned, match_count, matches: [{path, source, all_tags}]}
    """
    try:
        og = _import_obsidian_graph()
        return og.obsidian_search_by_tag(
            tag=tag, frontmatter_only=frontmatter_only,
            folder_scope=folder_scope, limit=limit,
        )
    except Exception as e:
        return {"error": f"obsidian_search_by_tag failed: {e}",
                "trace": traceback.format_exc()[:1500]}


@mcp.tool
def obsidian_search_by_frontmatter(
    field: str,
    value: str,
    folder_scope: list = None,
    limit: int = 500,
) -> dict:
    """Find notes where frontmatter field contains value (case-insensitive).

    Args:
        field: frontmatter field name (e.g. 'type', 'context', 'primary_tier')
        value: substring to match (case-insensitive)
        folder_scope: optional folder restriction
        limit: max matches

    Returns: {field, value, scanned, match_count, matches: [{path, field, value}]}
    """
    try:
        og = _import_obsidian_graph()
        return og.obsidian_search_by_frontmatter(
            field=field, value=value, folder_scope=folder_scope, limit=limit,
        )
    except Exception as e:
        return {"error": f"obsidian_search_by_frontmatter failed: {e}",
                "trace": traceback.format_exc()[:1500]}


@mcp.tool
def obsidian_list_orphans(folder_scope: list = None, limit: int = 100) -> dict:
    """Notes with zero outgoing AND zero incoming wikilinks in current scope.

    Returns: {scope, total_orphans, returned, orphans: [paths]}
    """
    try:
        og = _import_obsidian_graph()
        return og.obsidian_list_orphans(folder_scope=folder_scope, limit=limit)
    except Exception as e:
        return {"error": f"obsidian_list_orphans failed: {e}",
                "trace": traceback.format_exc()[:1500]}


@mcp.tool
def obsidian_graph_stats(folder_scope: list = None) -> dict:
    """Summary stats for the wikilink graph in current scope.

    Returns: {files_scanned, files_with_links, edges, unresolved_targets,
              orphans, duration_s, scope, built_at, cache_path}
    """
    try:
        og = _import_obsidian_graph()
        return og.obsidian_graph_stats(folder_scope=folder_scope)
    except Exception as e:
        return {"error": f"obsidian_graph_stats failed: {e}",
                "trace": traceback.format_exc()[:1500]}


@mcp.tool
def obsidian_propose_note(
    subdir: str,
    filename: str,
    content: str,
    frontmatter: dict = None,
    links_to: list = None,
) -> dict:
    """Stage a new note for human approval. Writes to _proposals/, NOT live vault.

    Args:
        subdir: target subdirectory under obsidian_sync/ (e.g. 'definitions_learned')
        filename: note filename (with or without .md)
        content: markdown body
        frontmatter: optional dict for YAML frontmatter
        links_to: optional list of wikilink targets to append as "## Linked"

    Returns: {ok, proposal_id, kind='note', target_path, staged_file,
              manifest_path, preview_first_200_chars}

    Use approve_obsidian_proposal(proposal_id) to apply.
    """
    try:
        og = _import_obsidian_graph()
        return og.obsidian_propose_note(
            subdir=subdir, filename=filename, content=content,
            frontmatter=frontmatter, links_to=links_to,
        )
    except Exception as e:
        return {"error": f"obsidian_propose_note failed: {e}",
                "trace": traceback.format_exc()[:1500]}


@mcp.tool
def obsidian_propose_link(
    from_note: str,
    to_note: str,
    edge_type: str,
    citation: str,
    scope: str = None,
) -> dict:
    """Stage adding a wikilink from from_note → to_note with edge metadata.

    Args:
        from_note: relative path of source note
        to_note: wikilink target (with or without .md)
        edge_type: one of 'cross_ref', 'authority', 'defined_by',
                   'implements', 'incorporates', 'enforced_by',
                   'interpreted_by', 'judicial_overlay', 'unknown'
        citation: pin cite (section, paragraph)
        scope: optional scope qualifier (e.g. 'subchapter II')

    When approved, link becomes: [[to_note|edge_type:citation]]
    appended under '## Linked Authorities' in from_note.

    Returns: {ok, proposal_id, kind='link', from_note, to_note, edge_type,
              link_text_to_be_added, manifest_path}
    """
    try:
        og = _import_obsidian_graph()
        return og.obsidian_propose_link(
            from_note=from_note, to_note=to_note,
            edge_type=edge_type, citation=citation, scope=scope,
        )
    except Exception as e:
        return {"error": f"obsidian_propose_link failed: {e}",
                "trace": traceback.format_exc()[:1500]}


@mcp.tool
def obsidian_propose_edit(
    note_path: str,
    set_frontmatter: dict = None,
    unset_frontmatter: list = None,
    body_replace: str = None,
    body_append: str = None,
    body_prepend: str = None,
    body_replace_pattern: dict = None,
) -> dict:
    """Stage an edit to an existing Obsidian note for human approval.

    Operations (at least one required):
      - set_frontmatter: dict of {key: value} to set/overwrite in YAML frontmatter
      - unset_frontmatter: list of keys to remove from frontmatter
      - body_replace: replace entire body (overrides other body ops)
      - body_append: append to body (after one blank line)
      - body_prepend: prepend to body (before existing content, after frontmatter)
      - body_replace_pattern: {"find": str, "replace": str} - one-time literal replace

    Use approve_obsidian_proposal(proposal_id) to apply.
    Returns {proposal_id, status, ...} or {status: "no_change"} for no-ops.
    """
    try:
        return og.obsidian_propose_edit(
            note_path=note_path,
            set_frontmatter=set_frontmatter,
            unset_frontmatter=unset_frontmatter,
            body_replace=body_replace,
            body_append=body_append,
            body_prepend=body_prepend,
            body_replace_pattern=body_replace_pattern,
        )
    except Exception as e:
        return {"error": f"obsidian_propose_edit failed: {e}",
                "trace": traceback.format_exc()[:1500]}


@mcp.tool
def approve_obsidian_proposal(proposal_id: str) -> dict:
    """Apply a staged Obsidian proposal to the live vault.

    For 'note' proposals: copies staged file into vault.
    For 'link' proposals: appends link under '## Linked Authorities' in from_note,
                          backs up original first.
    After success, moves proposal dir to _proposals/_applied/.

    Args:
        proposal_id: from obsidian_propose_note or obsidian_propose_link

    Returns: {ok, proposal_id, kind, applied_to OR modified, moved_to, ...}
    """
    try:
        og = _import_obsidian_graph()
        return og.approve_obsidian_proposal(proposal_id)
    except Exception as e:
        return {"error": f"approve_obsidian_proposal failed: {e}",
                "trace": traceback.format_exc()[:1500]}


# ─── Lane A (Session 23): Stage 4 Violation Chain Builder ──────────────────

@mcp.tool
def stage4_build_violation_chains(case_id: str) -> dict:
    """Build structured violation chains for one case from CASE_GROUND_TRUTH.yaml.

    Stage 4 violation-chain detector. GT-yaml-driven: consumes the
    human-approved CASE_GROUND_TRUTH.yaml and emits one chain per
    defendant linking claim → judicial error → controlling authority →
    remedy. Binding-authority labels (BINDING_SCOTUS, BINDING_GOVERNING_CIRCUIT,
    NON_PRECEDENTIAL, BINDING_STATE_STATUTE, etc.) assigned by regex classifier.

    Scanner findings from pipeline/judicial_findings_bridge are merged in as
    optional corroboration when available (currently 0 for all 4 cases).

    Args:
        case_id: e.g. 'fed:2:26-cv-00123', 'phila_ccp:260100000'

    Returns:
      {
        case_id, case_slug, case_type, court_id, governing_circuit,
        generated_at, ground_truth_provenance,
        chain_count, chains: [...],
        global_authority_index: {citation: binding_label},
        gt_yaml_path, scanner_findings_count,
      }

    Each chain:
      {
        chain_id, defendant, defendant_status, claim_taxonomy,
        judicial_error: {order_dkt, contradicts_dkt, narrative, error_type},
        controlling_authority: [{citation, binding_label, source}, ...],
        opposing_authority_used_by_court: [{citation, binding_label, defect, source}, ...],
        remedies: [{priority, motion_type, target_dkt, primary_authority}, ...],
        scanner_corroboration: [...],
      }
    """
    try:
        from pipeline.violation_chain_builder import build_violation_chains
        return build_violation_chains(case_id)
    except Exception as e:
        return {"error": f"stage4_build_violation_chains failed: {e}",
                "trace": traceback.format_exc()[:1500]}

# ─── end Lane A ────────────────────────────────────────────────────────────



# ─── Lane B (Session 24): Stage 5 Filing Schedule Engine ───────────────────

@mcp.tool
def stage5_compute_filing_deadlines(
    case_id: str,
    user_role: str = "plaintiff",
    write_appendix_file: bool = True,
) -> dict:
    """Compute live filing deadlines from CASE_GROUND_TRUTH.yaml anchors.

    Stage 5 temporal filing-schedule engine. Loads pipeline/temporal_rules.yaml
    (53 rules across federal trial/appellate/SCOTUS/Tax + PA all tiers) and
    evaluates each against the case's trigger anchors extracted from yaml.

    Day counting applies FRCP 6(a) / FRAP 26(a) / PA Rule 106 / literal
    (SCOTUS) regimes with weekend + federal/PA holiday rollover.

    Status classification:
      EXPIRED  — deadline has passed
      IMMINENT — ≤7 days remaining
      UPCOMING — ≤30 days
      OPEN     — >30 days or no fixed deadline

    Args:
        case_id: e.g. 'fed:2:26-cv-00123', 'phila_ccp:260100000'
        user_role: 'plaintiff', 'defendant', or 'both'. Filters rules by
                   party_role so plaintiff doesn't see Rule 12 answer
                   deadlines, defendant doesn't see Rule 1037 default
                   judgment deadlines, etc.
        write_appendix_file: if True (default), writes a markdown appendix
                             to obsidian_sync/cases/{slug}/chain_outputs/
                             named temporal_schedule_{YYYYMMDD}.md plus
                             temporal_schedule_latest.md.

    Returns:
      {
        case_id, user_role, computed_at,
        forum, anchors: {filed, latest_order, entry_of_judgment,
                         appeal_filed, appeal_decided, sources: {...}},
        deadlines: [
          {
            rule_id, citation, name, anchor_date, anchor_field, trigger,
            deadline, days_remaining, status, jurisdictional, waivable,
            party_role, notes,
          },
          ...
        ],
        summary: {expired, imminent, upcoming, open, total},
        critical: [...filtered list of jurisdictional+expired and imminent...],
        explicit_deadlines: [...from yaml deadlines[] field...],
        appendix_file_path: str or None,
        markdown_block: str (scorecard-ready),
      }
    """
    try:
        from datetime import date
        from pipeline.temporal_schedule import (
            compute_deadlines, get_case_anchors, format_deadlines_block,
        )

        today = date.today()
        anchors = get_case_anchors(case_id)
        if not anchors:
            return {"error": f"no case anchors found for {case_id}",
                    "case_id": case_id}

        deadlines = compute_deadlines(case_id, today=today, user_role=user_role)

        summary = {"expired": 0, "imminent": 0, "upcoming": 0, "open": 0,
                   "total": len(deadlines)}
        for d in deadlines:
            status = d.get("status", "OPEN").lower()
            if status in summary:
                summary[status] += 1

        critical = [
            d for d in deadlines
            if d.get("status") == "IMMINENT"
            or (d.get("status") == "EXPIRED" and d.get("jurisdictional"))
        ]

        # Format explicit_deadlines for JSON serialization
        explicit = []
        for ed in anchors.get("explicit_deadlines", []) or []:
            edt = ed.get("date")
            explicit.append({
                "date": edt.isoformat() if hasattr(edt, "isoformat") else str(edt),
                "label": ed.get("label"),
                "source": ed.get("source"),
                "days_remaining": (edt - today).days if hasattr(edt, "isoformat") else None,
            })

        markdown_block = format_deadlines_block(
            case_id, today=today, user_role=user_role, include_open=True,
        )

        appendix_path = None
        if write_appendix_file:
            from pipeline.scorecard_temporal_appendix import write_appendix
            wresult = write_appendix(case_id, user_role=user_role, today=today)
            appendix_path = wresult.get("latest_path") or wresult.get("dated_path")

        # Serialize anchors to JSON-safe shape
        anchors_out = {}
        for k in ("filed", "latest_order", "entry_of_judgment",
                  "appeal_filed", "appeal_decided"):
            v = anchors.get(k)
            anchors_out[k] = v.isoformat() if hasattr(v, "isoformat") else None
        anchors_out["sources"] = anchors.get("anchor_sources", {})
        anchors_out["forum"] = anchors.get("forum")

        return {
            "case_id": case_id,
            "user_role": user_role,
            "computed_at": today.isoformat(),
            "forum": anchors.get("forum"),
            "anchors": anchors_out,
            "deadlines": deadlines,
            "summary": summary,
            "critical_count": len(critical),
            "critical": critical,
            "explicit_deadlines": explicit,
            "appendix_file_path": appendix_path,
            "markdown_block": markdown_block,
        }
    except Exception as e:
        return {"error": f"stage5_compute_filing_deadlines failed: {e}",
                "trace": traceback.format_exc()[:1500]}

# ─── end Lane B ────────────────────────────────────────────────────────────



# ─── end Phase H ───────────────────────────────────────────────────────────


@mcp.tool
def statute_reaches_capacity(terms: list, actor_capacity: str) -> dict:
    """Definition-first: does a statute REACH a person in the capacity they actually acted? A regulatory
    statute binds a person 'only to the extent [they] act in [the regulated] capacity.' Terms like
    'motor vehicle' / 'driver' / 'transportation' are often defined COMMERCIALLY (for-hire / commerce), so
    a private (non-commercial) act may not be reached and a commercial-offense charge may not stand against
    it. Pass the statute's key `terms` (e.g. ['motor vehicle','driver']) and `actor_capacity` (e.g. 'private
    travel to a friend's house'). Returns per-term capacity (from the CORPUS definition, never common
    language), the regulated capacity, a verdict (APPLIES / DOES NOT APPLY / pull-the-statute's-definition),
    and the grounded proof. The specific charging statute's own definition controls."""
    try:
        from pipeline.statute_capacity import analyze
        if isinstance(terms, str):
            terms = [t.strip() for t in terms.split(",") if t.strip()]
        return analyze(terms, actor_capacity, pull=True)
    except Exception as e:
        import traceback
        return {"error": f"statute_reaches_capacity failed: {e}", "trace": traceback.format_exc()[:1200]}


# ─── Native Gmail access (agnostic, LIVE) — the agent queries the mailbox on demand, instead of the
#     deterministic nightly scraper whose cached ID list silently goes stale. Reuses the existing readonly
#     OAuth token (scrapers/email_scraper._get_gmail_service); no new login, no external package. ───
def _gmail_svc():
    from scrapers.email_scraper import _get_gmail_service
    return _get_gmail_service()


def _gmail_headers(msg) -> dict:
    return {x["name"].lower(): x["value"] for x in (msg.get("payload", {}).get("headers") or [])}


def _gmail_body_text(payload) -> str:
    import base64, re
    def walk(p, want):
        out = []
        if p.get("mimeType") == want and (p.get("body") or {}).get("data"):
            out.append(base64.urlsafe_b64decode(p["body"]["data"] + "===").decode("utf-8", "ignore"))
        for sub in (p.get("parts") or []):
            out.extend(walk(sub, want))
        return out
    txt = "\n".join(walk(payload, "text/plain"))
    if not txt.strip():
        html = "\n".join(walk(payload, "text/html"))
        txt = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"[ \t]+", " ", txt).strip()


@mcp.tool
def gmail_search(query: str, max_results: int = 15) -> dict:
    """Search the user's Gmail LIVE with any Gmail query string — e.g. 'from:planethomelending',
    'foreclosure newer_than:14d', '1099-C', 'subject:notice of appeal', 'has:attachment complaint'.
    Returns matching messages (id, from, date, subject, snippet), newest first; call gmail_read(id) for
    the full body. This is the agnostic, on-demand alternative to the nightly scraper: the mailbox is
    queried fresh every call, so it never goes stale."""
    try:
        svc = _gmail_svc()
        res = svc.users().messages().list(userId="me", q=query, maxResults=min(max_results, 50)).execute()
        out = []
        for m in (res.get("messages") or []):
            msg = svc.users().messages().get(userId="me", id=m["id"], format="metadata",
                                             metadataHeaders=["From", "Date", "Subject"]).execute()
            h = _gmail_headers(msg)
            out.append({"id": m["id"], "from": h.get("from", ""), "date": h.get("date", ""),
                        "subject": h.get("subject", ""), "snippet": (msg.get("snippet") or "")[:300]})
        return {"query": query, "count": len(out), "messages": out}
    except Exception as e:
        return {"error": f"gmail_search failed: {e}", "trace": traceback.format_exc()[:800]}


@mcp.tool
def gmail_read(message_id: str, max_chars: int = 12000) -> dict:
    """Read ONE Gmail message in full by id (obtained from gmail_search): headers + decoded plain-text
    body + attachment filenames. Live fetch."""
    try:
        svc = _gmail_svc()
        msg = svc.users().messages().get(userId="me", id=message_id, format="full").execute()
        h = _gmail_headers(msg)
        payload = msg.get("payload", {})
        atts = [p.get("filename") for p in (payload.get("parts") or []) if p.get("filename")]
        return {"id": message_id, "from": h.get("from", ""), "to": h.get("to", ""),
                "date": h.get("date", ""), "subject": h.get("subject", ""),
                "attachments": atts, "body": _gmail_body_text(payload)[:max_chars]}
    except Exception as e:
        return {"error": f"gmail_read failed: {e}", "trace": traceback.format_exc()[:800]}


@mcp.tool
def escalation_ladder(forum_or_actor: str) -> dict:
    """Grounded escalation ladder for a forum/regulator (FOIA, mortgage servicer, JCB, ODC, insurance,
    tax/IRS, debt, fair-housing, oversight-of-oversight, etc.). Returns the ordered levels, each with its
    statutory day-clock, cite, and on-expiry consequence — use to name the next enforcement step + deadline.
    Every deadline is corpus-confirmed. Accepts a forum key or free text (e.g. 'RESPA QWR', 'the JCB')."""
    try:
        from pipeline import escalation_ladders as E
        fk = E.forum_key(forum_or_actor) or forum_or_actor
        lad = E.ladder_for(fk)
        if not lad:
            return {"error": f"no grounded ladder for {forum_or_actor!r}", "known_forums": list(E.ladders().keys())}
        return {"forum_key": fk, "forum": lad.get("forum"),
                "applies_when": lad.get("applies_when"), "levels": lad.get("levels")}
    except Exception as e:
        return {"error": f"escalation_ladder failed: {e}"}


if __name__ == "__main__":
    main()


# === SESSION 11 NOTE ===
# To wire renderer into compose_and_draft, ensure inside that function you have:
#   from generation.md_to_docx import render_md_to_docx
#   court_id = case_data.get("court_id", "EDPA")
#   render_md_to_docx(composed_md, str(docx_path), court_id=court_id)
# Replace any prior docx_formatter._p() loop with this single call.
