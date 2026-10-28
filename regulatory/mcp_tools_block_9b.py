"""
BLOCK_9B_MCP_REGISTRATION
Registers Block 9B skill ports as Beezle MCP tools.

Tools registered:
  beezle.baseline_memo          - generate standing legal notice (markdown + docx)
  beezle.defense_simulate       - adversarial element/authority/fact/logic attack
  beezle.procedural_scan        - scan filing for procedural defects
  beezle.definition_lookup      - priority-ranked definition lookup
  beezle.definition_validate    - validate a definition dict against banned behaviors
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
from typing import Optional


def register_block_9b_tools(mcp) -> int:
    """Register all Block 9B tools. Returns count registered."""
    count = 0

    # --- baseline_memorandum ---
    @mcp.tool
    def baseline_memo(
        case_id: str,
        caption: str = "",
        controlling_authorities: Optional[list[dict]] = None,
        mandatory_duties: Optional[list[dict]] = None,
        definitions: Optional[list[dict]] = None,
        court_rules: Optional[list[dict]] = None,
        write_markdown: bool = True,
        write_docx: bool = True,
    ) -> dict:
        """Generate a Standing Legal Notice / Memorandum of Law.

        controlling_authorities: [{name, citation, proposition, binding_on?}]
        mandatory_duties: [{duty, authority, consequence}]
        definitions: [{term, definition, source, scope?}]
        court_rules: [{rule, citation, application}]

        Returns {case_id, duties_count, markdown_path?, docx_path?, violation_ids}.
        """
        from generation.baseline_memorandum import build_memo
        return build_memo(case_id, caption, controlling_authorities,
                         mandatory_duties, definitions, court_rules,
                         write_markdown, write_docx)
    count += 1

    # --- defense_simulator ---
    @mcp.tool
    def defense_simulate(
        elements: Optional[list[dict]] = None,
        authorities: Optional[list[str]] = None,
        facts: Optional[list[str]] = None,
        logic_chain: Optional[list[dict]] = None,
        target_jurisdiction: str = "",
    ) -> dict:
        """Simulate strongest possible defense attacks against your argument.

        elements: [{name, satisfied:bool, fact_basis, evidence:[]}]
        authorities: ["citation1", ...]
        facts: ["fact statement", ...]
        logic_chain: [{if_clause, then_clause, authority}]
        target_jurisdiction: e.g. "EDPA"

        Returns {ready_for_filing, total, by_strength, blocking_count, summary, attacks}.
        """
        from pipeline.defense_simulator import simulate
        return simulate(elements, authorities, facts, logic_chain, target_jurisdiction)
    count += 1

    # --- procedural_attack_builder ---
    @mcp.tool
    def procedural_scan(filing_meta: dict) -> dict:
        """Scan a filing for procedural defects across 4 categories.

        filing_meta keys (all optional):
          court_type, doc_type, verified, requires_verification,
          signed_by_authorized, certificate_of_service, served_by,
          service_method, proof_of_service_filed, substituted_service,
          cover_sheet_filed, within_page_limit, filed_after_deadline,
          days_late, premature, subject_matter_jurisdiction_lacking,
          personal_jurisdiction_lacking

        Returns {total, fatal_count, by_category, by_severity, summary, defects}.
        """
        from pipeline.procedural_attack_builder import scan
        return scan(filing_meta)
    count += 1

    # --- definition_engine (new priority functions) ---
    @mcp.tool
    def definition_lookup(
        term: str,
        statute_scope: Optional[str] = None,
        candidates: Optional[list[dict]] = None,
    ) -> dict:
        """Look up a legal term using priority-ranked sources.
        Tiered fallback when no candidates passed:
          1. explicit candidates from caller (existing behavior)
          2. CORE_DEFINITIONS (litigation terms catalog)
          3. definitions_learned/ cache (prior cron-built records)
          4. ChromaDB retrieval (statutes, dicts, case law - all corpora)
        Tier 4 RULE: a chunk is admitted ONLY if it contains legal-structure
        markers (USC, CFR, UCC, Rule, v., Stat., HJR, etc.). Term match alone
        is NOT enough - that would admit blog text and non-authority content.
        Classification (source_type) is metadata only, not a gate.
        Returns the highest-priority valid definition or extracted candidates.
        """
        import re as _re
        from pipeline.definition_engine import lookup_with_priority
        # Tier 1: caller passed candidates
        if candidates:
            result = lookup_with_priority(term, statute_scope, candidates)
            if result:
                return {"found": True, "tier": "explicit_candidates", **result}
            return {"found": False, "tier": "explicit_candidates", "term": term,
                    "message": f"No valid definition for '{term}' among candidates",
                    "candidates_received": len(candidates)}
        # Tier 2: CORE_DEFINITIONS
        result = lookup_with_priority(term, statute_scope, None)
        if result:
            return {"found": True, "tier": "core_definitions", **result}
        # Tier 3: definitions_learned cache
        try:
            from pathlib import Path
            learned_dir = (_eroot() / "obsidian_sync/definitions_learned")
            if learned_dir.exists():
                term_slug = _re.sub(r"[^a-zA-Z0-9]+", "_", term.lower()).strip("_")
                for f in learned_dir.glob("*.md"):
                    if f.name.startswith("_"):
                        continue
                    if term_slug and term_slug in f.name.lower():
                        text = f.read_text(encoding="utf-8", errors="ignore")
                        return {
                            "found": True,
                            "tier": "definitions_learned_cache",
                            "term": term,
                            "definition": text[:2000],
                            "source": f.name,
                            "source_type": "cached",
                            "cache_path": str(f),
                        }
        except Exception:
            pass
        # Tier 4: ChromaDB via the same code path search_chunks uses
        # (proven to work in this MCP process). Falls back to direct db_connector
        # if anything goes wrong.
        try:
            _hits = None
            # Path A: use retriever.retrieve() with legal-term-of-art query expansion
            try:
                from pipeline.retriever import retrieve as _expand_retrieve
                _hits = _expand_retrieve(term, jurisdiction="federal", final_k=15)
            except Exception as _e_path_a:
                _hits = None
            # Path B: fall back to raw query_text if expansion path errors
            if not _hits:
                try:
                    from pipeline.db_connector import get_db as _get_db_b
                    _db_b = _get_db_b()
                    _hits = _db_b.query_text(query_text=term, n_results=15)
                except Exception:
                    _hits = []
            _hits = _hits or []
            # _hits is a list of dicts with keys: id, text, distance, source,
            # section, chunk_type, citations, doc_id, has_definitions, metadata
            ids_list = [h.get("id", "") for h in (_hits or [])]
            docs_list = [h.get("text", "") for h in (_hits or [])]
            metas_list = [h.get("metadata", {}) or {} for h in (_hits or [])]
            _extras = {
                h.get("id", ""): {
                    "source": h.get("source", ""),
                    "section": h.get("section", ""),
                    "chunk_type": h.get("chunk_type", ""),
                    "distance": h.get("distance"),
                }
                for h in (_hits or [])
            }
            LEGAL_MARKERS = [
                r"\bU\.?S\.?C\.?\s*\u00a7?\s*\d+",
                r"\bC\.?F\.?R\.?\s*\u00a7?\s*\d+",
                r"\bUCC\s*\u00a7?\s*\d+-\d+",
                r"\b\d+\s+Stat\.\s+\d+",
                r"\bRule\s+\d+",
                r"\bF\.?R\.?C\.?P\.?\s*\d+",
                r"\bF\.?R\.?E\.?\s*\d+",
                r"\bPa\.?\s*R\.?\s*C\.?\s*P\.?\s*\d+",
                r"\b[A-Z][a-z]+\s+v\.\s+[A-Z]",
                r"\bH\.?J\.?\s*Res\.?\s*\d+",
                r"\u00a7\s*\d+",
                r"\u00a7\s*\d+-\d+",
                r"\b\d+-\d+\b",
                r"\bRestatement",
                r"\bSection\s+\d+",
            ]
            marker_re = _re.compile("|".join(LEGAL_MARKERS), _re.IGNORECASE)
            extracted = []
            term_lower = (term or "").lower()
            AUTHORITY_TYPES = {"statutory", "regulatory", "case_law", "blacks_law", "bouviers"}
            for i, cid in enumerate(ids_list):
                doc = docs_list[i] if i < len(docs_list) else ""
                meta = metas_list[i] if i < len(metas_list) else {}
                if not doc:
                    continue
                has_legal_marker = bool(marker_re.search(doc))
                has_term = term_lower and (term_lower in doc.lower())
                # Soft labeling
                cid_l = (cid or "").lower()
                if isinstance(meta, dict) and meta.get("source_type"):
                    source_type = meta["source_type"]
                elif cid_l.startswith("blacks_law_"):
                    source_type = "blacks_law"
                elif cid_l.startswith("ucc_") or "_ucc_" in cid_l:
                    source_type = "statutory"
                elif "usc" in cid_l:
                    source_type = "statutory"
                elif "cfr" in cid_l:
                    source_type = "regulatory"
                elif "bouvier" in cid_l:
                    source_type = "bouviers"
                else:
                    source_type = "unknown"
                # ADMIT POLICY (other AI's correction):
                # Legal marker required. Term-only is allowed ONLY if metadata
                # source_type identifies an authority class.
                if not has_legal_marker:
                    if not (has_term and source_type in AUTHORITY_TYPES):
                        continue
                _xtra = _extras.get(cid, {})
                source_label = (
                    (meta.get("source") if isinstance(meta, dict) else None)
                    or _xtra.get("source")
                    or _xtra.get("section")
                    or f"chunk:{cid}"
                )
                if not isinstance(meta, dict) or not meta.get("source_type"):
                    _ct = _xtra.get("chunk_type", "")
                    if _ct and _ct.lower() in AUTHORITY_TYPES:
                        source_type = _ct.lower()
                extracted.append({
                    "term": term,
                    "definition": doc[:1500],
                    "source": source_label,
                    "scope": statute_scope or "general",
                    "source_type": source_type,
                    "chunk_id": cid,
                    "has_legal_marker": has_legal_marker,
                    "has_term": has_term,
                    "distance": _extras.get(cid, {}).get("distance"),
                })
            # Pre-sort: statutory/regulatory first, then everything else
            _order = {"statutory": 0, "regulatory": 1, "blacks_law": 2, "case_law": 3, "bouviers": 4, "unknown": 9}
            extracted.sort(key=lambda c: _order.get(c.get("source_type", "unknown"), 9))
            if extracted:
                ranked = lookup_with_priority(term, statute_scope, extracted)
                if ranked:
                    return {
                        "found": True,
                        "tier": "chromadb_ranked",
                        "candidates_extracted": len(extracted),
                        **ranked,
                    }
                return {
                    "found": False,
                    "tier": "chromadb_extracted_unranked",
                    "term": term,
                    "candidates_extracted": len(extracted),
                    "candidates": extracted[:8],
                    "message": f"DB returned {len(extracted)} candidates with legal-text markers but none ranked as valid",
                }
        except Exception as _e_db:
            return {"found": False, "tier": "db_error", "term": term,
                    "error": str(_e_db),
                    "message": f"DB fallback errored: {_e_db}"}
        return {"found": False, "tier": "exhausted", "term": term,
                "message": f"No definition for '{term}' in any tier"}
    count += 1

    @mcp.tool
    def definition_validate(definition: dict) -> dict:
        """Validate a definition dict against banned behaviors.

        definition: {term, definition, source, scope}
        Returns {valid:bool, required_fields_missing:[], violations:[]}.
        """
        from pipeline.definition_engine import validate_definition
        return validate_definition(definition)
    count += 1

    return count