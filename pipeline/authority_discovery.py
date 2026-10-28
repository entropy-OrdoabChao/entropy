"""
pipeline/authority_discovery.py — Authority Discovery Module.

Expands queries into legal search terms, retrieves from:
  1. ChromaDB (local 2.2M chunk database) — statutes, regulations, rules
  2. CourtListener API — case law, judicial opinions

Returns citations and authority text — NO conclusions, NO interpretation.

Rules:
  - Returns citations only — no legal conclusions
  - ChromaDB for statutes/rules, CourtListener for case law
  - Queries expanded using legal term mapping
  - Results ranked by jurisdiction and binding level
"""

from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import os
import re
import json
import time
import urllib.request
import urllib.parse
from typing import Optional

from core.logger import get_logger
from pathlib import Path as _Path

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

log = get_logger("AUTHORITY")

# ── CourtListener Configuration ───────────────────────────────────────

CL_API_KEY = os.environ.get(
    "COURTLISTENER_API_KEY",
    "8e7b9e2c521ab2bb034a375d1c73702544339a00"
)
CL_BASE = "https://www.courtlistener.com/api/rest/v4"
CL_HEADERS = {"Authorization": f"Token {CL_API_KEY}"}
CL_TIMEOUT = 30

# ── Court ID Mapping ──────────────────────────────────────────────────

COURT_IDS = {
    "scotus": "scotus",
    "3rd_circuit": "ca3",
    "pa_supreme": "pa",
    "pa_superior": "pasuperct",
    "pa_commonwealth": "pacommwct",
    "ed_pa": "paed",
    "md_pa": "pamd",
    "wd_pa": "pawd",
}

# ── Query Expansion for Case Law ──────────────────────────────────────

CASE_LAW_EXPANSIONS = {
    "fcra": [
        "fair credit reporting act furnisher duties",
        "15 USC 1681 willfulness",
        "Safeco v Burr willfulness standard",
        "credit reporting dispute investigation",
    ],
    "fdcpa": [
        "fair debt collection practices act",
        "debt validation verification 1692g",
        "cease collection during verification period",
        "Heintz v Jenkins attorney debt collector",
    ],
    "due process": [
        "procedural due process notice hearing",
        "Mathews v Eldridge due process test",
        "void judgment lack jurisdiction",
    ],
    "verification": [
        "debt verification 30 day period",
        "1692g validation notice requirements",
        "verification vs validation distinction",
    ],
    "void judgment": [
        "void ab initio lack jurisdiction",
        "void judgment collateral attack",
        "Rule 60b void judgment relief",
    ],
    "twombly": [
        "Bell Atlantic v Twombly plausibility",
        "Ashcroft v Iqbal facial plausibility",
        "plausibility pleading standard",
    ],
}


# ══════════════════════════════════════════════════════════════════════
# COURTLISTENER API
# ══════════════════════════════════════════════════════════════════════

def _cl_api_get(url: str) -> Optional[dict]:
    """Make an authenticated GET request to CourtListener."""
    try:
        req = urllib.request.Request(url, headers=CL_HEADERS)
        with urllib.request.urlopen(req, timeout=CL_TIMEOUT) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log.warning("cl_api", f"CourtListener request failed", error=str(e))
        return None


def search_case_law(query: str, court: str = "", n_results: int = 5) -> list[dict]:
    """Search CourtListener for case law.

    Returns list of case dicts with: name, citation, court, date, snippet, id.
    NO holdings, NO conclusions — citations and text only.
    """
    encoded = urllib.parse.quote(query)
    url = f"{CL_BASE}/search/?q={encoded}&type=o&format=json&page_size={n_results}"
    if court:
        url += f"&court={court}"

    t0 = time.time()
    data = _cl_api_get(url)
    elapsed = int((time.time() - t0) * 1000)

    if not data:
        log.warning("search_case_law", f"No response for: {query[:50]}")
        return []

    total = data.get("count", 0)
    results = []

    for r in data.get("results", [])[:n_results]:
        # Clean snippet of HTML
        snippet = r.get("snippet", "")
        if snippet:
            snippet = re.sub(r"<[^>]+>", "", snippet).strip()

        case = {
            "name": r.get("caseName", "Unknown"),
            "citation": r.get("citation", []),
            "court": r.get("court", ""),
            "date_filed": r.get("dateFiled", ""),
            "id": r.get("id", ""),
            "snippet": snippet[:500],
            "source": "courtlistener",
        }
        results.append(case)

    log.stage("search_case_law",
              f"Found {total} total, returned {len(results)} for: {query[:50]} ({elapsed}ms)")
    return results


# BEEZLE_FN_UNUSED 2026-06-13: get_opinion_text (no static refs in pipeline/)
def get_opinion_text(opinion_id: str, max_chars: int = 5000) -> Optional[dict]:
    """Retrieve full opinion text from CourtListener.

    Returns dict with: id, text, cluster_url.
    """
    data = _cl_api_get(f"{CL_BASE}/opinions/{opinion_id}/?format=json")
    if not data:
        return None

    text = data.get("plain_text") or data.get("html_with_citations") or ""
    if "<" in text:
        text = re.sub(r"<[^>]+>", "", text)

    return {
        "id": opinion_id,
        "text": text[:max_chars],
        "full_length": len(text),
        "cluster": data.get("cluster", ""),
    }


# ══════════════════════════════════════════════════════════════════════
# AUTHORITY DISCOVERY (combined ChromaDB + CourtListener)
# ══════════════════════════════════════════════════════════════════════


# ======================================================================
# PHASE_3_INTEGRATION — 3-tier authority cascade
# ChromaDB → Obsidian primary_sources → Scraper fallback
# Origin-tagged, structured buckets, no flattening
# ======================================================================

_PRIMARY_SOURCES_DIR = (_BEEZLE_ROOT / "obsidian_sync" / "primary_sources")

THIN_MIN_RESULTS = 3
THIN_MIN_SCORE = 0.6  # Chroma distance threshold (lower = better match for cosine)


def is_thin(hits: list[dict], min_results: int = THIN_MIN_RESULTS,
            max_distance: float = THIN_MIN_SCORE) -> bool:
    """Determine if ChromaDB results are insufficient and need enrichment.

    'Thin' means: too few results OR all results have weak similarity.
    ChromaDB uses cosine distance: 0 = perfect match, 2 = opposite.
    So lower distance = better. Threshold 0.6 means 'reasonably relevant'.
    """
    if len(hits) < min_results:
        return True
    # All results are weak matches
    if hits and all(h.get("distance", 1.0) > max_distance for h in hits):
        return True
    return False


def search_primary_sources(query: str, max_results: int = 10) -> list[dict]:
    """Search Obsidian primary_sources by YAML frontmatter + filename + content.

    Returns list of dicts with: source_id, title, jurisdiction, type, path, origin, match_type.
    """
    if not _PRIMARY_SOURCES_DIR.exists():
        return []

    query_lower = query.lower()
    query_terms = [t for t in query_lower.split() if len(t) > 2]
    results = []

    for md_file in _PRIMARY_SOURCES_DIR.rglob("*.md"):
        try:
            text = md_file.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        # Extract YAML frontmatter
        fm = {}
        if text.startswith("---"):
            end = text.find("---", 3)
            if end > 0:
                for line in text[3:end].strip().splitlines():
                    if ":" in line:
                        k, _, v = line.partition(":")
                        fm[k.strip()] = v.strip()

        # Match scoring: filename > frontmatter title > frontmatter source_id > content
        score = 0
        match_type = ""
        fname = md_file.stem.lower()
        fm_title = (fm.get("title") or "").lower()
        fm_sid = (fm.get("source_id") or "").lower()

        for term in query_terms:
            if term in fname:
                score += 3
                match_type = "filename"
            if term in fm_title:
                score += 3
                match_type = match_type or "title"
            if term in fm_sid:
                score += 2
                match_type = match_type or "source_id"

        # Light content check only if no metadata match (expensive for large files)
        if score == 0 and len(text) < 500_000:
            content_lower = text[:50000].lower()  # first 50k chars only
            hits = sum(1 for t in query_terms if t in content_lower)
            if hits >= 2:
                score = hits
                match_type = "content"

        if score > 0:
            results.append({
                "source_id": fm.get("source_id", md_file.stem),
                "title": fm.get("title", md_file.stem),
                "jurisdiction": fm.get("jurisdiction", ""),
                "type": fm.get("type", ""),
                "section": fm.get("section", ""),
                "path": str(md_file),
                "sha1": fm.get("sha1", ""),
                "origin": "obsidian",
                "match_type": match_type,
                "match_score": score,
            })

    # Sort by score descending
    results.sort(key=lambda x: -x["match_score"])
    return results[:max_results]


def _scraper_fallback(query: str, subject_areas: list, jurisdiction: str) -> list[dict]:
    """Last-resort: call scrapers to fetch missing authority.

    Only called when ChromaDB is thin AND Obsidian has nothing.
    Returns list of fetched source dicts with origin='scraper'.
    """
    fetched = []

    # Determine which scraper to try based on query content
    ql = query.lower()

    # USC patterns: "15 USC", "28 U.S.C.", "42 usc"
    import re as _re
    usc_match = _re.search(r"(\d+)\s*(?:usc|u\.s\.c)", ql)
    if usc_match:
        title = int(usc_match.group(1))
        sec_match = _re.search(r"(?:section|sec\.?|[^a-z])(\d+[a-z]?(?:-\d+)?)", ql[usc_match.end():])
        section = sec_match.group(1) if sec_match else None
        try:
            from scrapers.fed_register import fetch_usc
            r = fetch_usc(title, section)
            if not r.get("error"):
                r["origin"] = "scraper"
                fetched.append(r)
        except Exception as e:
            log.warning("scraper_fallback", f"USC fetch failed: {e}")

    # CFR patterns: "12 CFR", "12 C.F.R."
    cfr_match = _re.search(r"(\d+)\s*(?:cfr|c\.f\.r)", ql)
    if cfr_match:
        title = int(cfr_match.group(1))
        part_match = _re.search(r"part\s*(\d+)", ql)
        part = part_match.group(1) if part_match else None
        try:
            from scrapers.fed_register import fetch_cfr
            r = fetch_cfr(title, part)
            if not r.get("error"):
                r["origin"] = "scraper"
                fetched.append(r)
        except Exception as e:
            log.warning("scraper_fallback", f"CFR fetch failed: {e}")

    # PA title patterns: "pa title 42", "42 pa.c.s."
    pa_match = _re.search(r"(?:pa\s*title|pa\.c\.s\.?)\s*(\d+)", ql)
    if pa_match:
        title = int(pa_match.group(1))
        try:
            from scrapers.pa_legis import fetch_title
            r = fetch_title(title)
            if not r.get("error"):
                r["origin"] = "scraper"
                fetched.append(r)
        except Exception as e:
            log.warning("scraper_fallback", f"PA legis fetch failed: {e}")

    # FRCP patterns: "frcp", "rule 12", "fed.r.civ.p"
    frcp_match = _re.search(r"(?:frcp|fed\.?\s*r\.?\s*civ\.?\s*p\.?)\s*(?:rule\s*)?(\d+)?", ql)
    if frcp_match:
        rule = frcp_match.group(1)
        try:
            from scrapers.fed_register import fetch_frcp
            r = fetch_frcp(rule)
            if not r.get("error"):
                r["origin"] = "scraper"
                fetched.append(r)
        except Exception as e:
            log.warning("scraper_fallback", f"FRCP fetch failed: {e}")

    # CourtListener for case law (if subject areas suggest it)
    if not fetched and subject_areas:
        try:
            from scrapers.courtlistener import search_and_save
            for area in subject_areas[:2]:
                cases = search_and_save(f"{area} {jurisdiction}", n=3)
                for c in cases:
                    c["origin"] = "scraper"
                    fetched.append(c)
        except Exception as e:
            log.warning("scraper_fallback", f"CL search failed: {e}")

    log.stage("scraper_fallback", f"Fetched {len(fetched)} sources")
    return fetched


# BEEZLE_FN_UNUSED 2026-06-13: discover_authority (no static refs in pipeline/)
def discover_authority(
    query: str,
    subject_areas: list[str] = None,
    jurisdiction: str = "PA",
    include_case_law: bool = True,
    max_cases: int = 5,
    max_db_results: int = 8,
) -> dict:
    """Run full authority discovery with 3-tier cascade.

    Tier 1: ChromaDB (fast, indexed, primary)
    Tier 2: Obsidian primary_sources (trusted, local, exact)
    Tier 3: Scraper fallback (last resort, external, slow)

    Returns structured buckets — NEVER flattened.
    Every result tagged with 'origin': 'chroma' | 'obsidian' | 'scraper'.
    """
    subject_areas = subject_areas or []
    t0 = time.time()

    result = {
        "db_results": [],       # Tier 1: ChromaDB
        "primary_sources": [],  # Tier 2: Obsidian
        "fetched": [],          # Tier 3: Scraper
        "case_law": [],         # CourtListener (always runs if requested)
        "all_citations": [],
        "context": "",
        "sources_count": 0,
        "source_of_truth": "chroma",  # which tier provided best results
        "thin": False,
    }

    # ── Tier 1: ChromaDB ──
    try:
        from pipeline.retriever import retrieve
        db_result = retrieve(query, n_results=20, jurisdiction=jurisdiction,
                             final_k=max_db_results)
        hits = db_result.get("hits", [])
        # Tag origin
        for h in hits:
            h["origin"] = "chroma"
        result["db_results"] = hits
        result["context"] = db_result.get("context", "")
        result["all_citations"] = db_result.get("citations", [])
        log.stage("discover", f"Tier 1 (Chroma): {len(hits)} results")
    except Exception as e:
        log.warning("discover", "Tier 1 (Chroma) failed", error=str(e))

    # ── Check if Chroma results are thin ──
    result["thin"] = is_thin(result["db_results"])

    if result["thin"]:
        log.stage("discover", "Chroma results THIN — checking Obsidian primary_sources")

        # ── Tier 2: Obsidian primary_sources ──
        obsidian_hits = search_primary_sources(query)
        result["primary_sources"] = obsidian_hits
        if obsidian_hits:
            result["source_of_truth"] = "obsidian"
            log.stage("discover", f"Tier 2 (Obsidian): {len(obsidian_hits)} matches")
        else:
            log.stage("discover", "Tier 2 (Obsidian): no matches")

        # ── Tier 3: Scraper fallback (only if STILL thin) ──
        if not obsidian_hits:
            log.stage("discover", "Both Chroma and Obsidian thin — triggering scraper fallback")
            fetched = _scraper_fallback(query, subject_areas, jurisdiction)
            result["fetched"] = fetched
            if fetched:
                result["source_of_truth"] = "scraper"
                log.stage("discover", f"Tier 3 (Scraper): {len(fetched)} sources fetched")
    else:
        log.stage("discover", "Chroma results sufficient — skipping Tier 2/3")

    # ── CourtListener case law (always runs if requested, independent of thinness) ──
    if include_case_law:
        case_queries = _build_case_queries(query, subject_areas)
        all_cases = {}

        for cq in case_queries[:3]:
            try:
                cases = search_case_law(cq, n_results=max_cases)
                for case in cases:
                    case["origin"] = "chroma"  # CL is a primary source via existing pipeline
                    case_key = case.get("name", "")
                    if case_key and case_key not in all_cases:
                        all_cases[case_key] = case
            except Exception as e:
                log.warning("discover", f"Case law search failed: {cq[:40]}", error=str(e))

        result["case_law"] = list(all_cases.values())[:max_cases]

        for case in result["case_law"]:
            cite = case.get("citation", [])
            name = case.get("name", "")
            cite_str = f"{name}, {cite[0]}" if isinstance(cite, list) and cite else name
            if cite_str and cite_str not in result["all_citations"]:
                result["all_citations"].append(cite_str)

        log.stage("discover", f"CourtListener: {len(result['case_law'])} cases")

    result["sources_count"] = (
        len(result["db_results"]) + len(result["primary_sources"]) +
        len(result["fetched"]) + len(result["case_law"])
    )
    elapsed = int((time.time() - t0) * 1000)
    log.stage("discover",
              f"Authority discovery complete: {result['sources_count']} sources, "
              f"truth={result['source_of_truth']}, thin={result['thin']} ({elapsed}ms)")

    return result



def _build_case_queries(query: str, subject_areas: list[str]) -> list[str]:
    """Build case law search queries from the main query and subject areas."""
    queries = [query]

    for area in subject_areas:
        area_lower = area.lower()
        if area_lower in CASE_LAW_EXPANSIONS:
            queries.extend(CASE_LAW_EXPANSIONS[area_lower][:2])

    return queries[:5]


# ══════════════════════════════════════════════════════════════════════
# FORMAT HELPERS
# ══════════════════════════════════════════════════════════════════════

# BEEZLE_FN_UNUSED 2026-06-13: format_authority_block (no static refs in pipeline/)
def format_authority_block(discovery_result: dict) -> str:
    """Format discovery results into a context block for LLM drafting."""
    parts = []

    # DB results (statutes, rules)
    if discovery_result.get("context"):
        parts.append("## STATUTES AND RULES\n" + discovery_result["context"])

    # Case law
    case_law = discovery_result.get("case_law", [])
    if case_law:
        case_parts = ["## CASE LAW"]
        for case in case_law:
            cite = case.get("citation", [])
            cite_str = cite[0] if isinstance(cite, list) and cite else "No citation"
            case_parts.append(
                f"\n**{case['name']}** ({cite_str})\n"
                f"Court: {case.get('court', '?')} | Filed: {case.get('date_filed', '?')}\n"
                f"{case.get('snippet', '')}"
            )
        parts.append("\n".join(case_parts))

    return "\n\n---\n\n".join(parts) if parts else "(no authority retrieved)"


# BEEZLE_FN_UNUSED 2026-06-13: format_citation_list (no static refs in pipeline/)
def format_citation_list(discovery_result: dict) -> list[str]:
    """Extract flat citation list from discovery results."""
    return discovery_result.get("all_citations", [])
