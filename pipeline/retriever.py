"""
pipeline/retriever.py - ChromaDB retrieval with legal-term-of-art query expansion.

No LLM. Deterministic. Rebuilt 2026-05-10 to use pipeline.db_connector and
return structured dicts matching what definition_lookup Tier 4 expects.
"""
from __future__ import annotations
from pipeline.db_connector import get_db
from pipeline.citation_retrieval import parse_citation, citation_search

# Legal terms-of-art that semantic search fails on. Maps surface form to
# citation-anchored expansion queries. Lower-case keys; substring match.
EXPANSIONS = {
    # UCC Article 3 - negotiable instruments
    "holder in due course": ["UCC 3-302 holder in due course", "section 3-302 negotiable instrument holder", "good faith without notice"],
    "negotiable instrument": ["UCC 3-104 negotiable instrument", "section 3-104 unconditional promise order pay"],
    "accommodation party": ["UCC 3-419 accommodation party", "section 3-419 surety"],
    "presentment": ["UCC 3-501 presentment", "section 3-501 demand payment"],
    "dishonor": ["UCC 3-502 dishonor", "section 3-502 notice of dishonor"],
    # UCC Article 9 - secured transactions
    "perfection": ["UCC 9-308 perfection", "section 9-308 perfected security interest"],
    "security interest": ["UCC 9-203 security interest", "section 9-203 attachment"],
    # Federal civil procedure
    "real party in interest": ["FRCP 17 real party in interest", "Rule 17 prosecuted in the name"],
    "standing": ["Article III standing", "injury in fact causation redressability", "Lujan v Defenders of Wildlife"],
    "subject matter jurisdiction": ["FRCP 12(b)(1) subject matter jurisdiction", "Article III federal court power"],
    "judicial immunity": ["absolute judicial immunity", "Stump v Sparkman judicial acts", "Mireles v Waco judicial immunity"],
    "qualified immunity": ["qualified immunity Harlow v Fitzgerald", "clearly established right"],
    # Treasury / federal reserve
    "lawful money": ["31 USC 5103 legal tender", "Federal Reserve notes United States coins"],
    "federal reserve note": ["12 USC 411 Federal Reserve notes", "Federal Reserve Act section 16"],
    "hjr-192": ["House Joint Resolution 192 1933", "31 USC 5118 gold clauses", "Public Law 73-10"],
    "hjr 192": ["House Joint Resolution 192 1933", "31 USC 5118 gold clauses"],
    "a4v": ["acceptance for value", "UCC 3-419 accommodation"],
    "acceptance for value": ["acceptance for value A4V", "UCC 3-410 acceptance"],
    # Civil rights
    "42 usc 1983": ["42 USC 1983 civil action deprivation rights", "section 1983 color of law"],
    "section 1983": ["42 USC 1983 civil action deprivation rights"],
    "color of law": ["42 USC 1983 under color of state law", "Monroe v Pape color of law"],
    "monell": ["Monell v Department of Social Services municipal liability", "official policy custom"],
    # Contract
    "consideration": ["contract consideration bargained for exchange", "Restatement Second Contracts section 71"],
    "promissory estoppel": ["Restatement Second Contracts section 90 promissory estoppel", "detrimental reliance"],
    # FCRA
    "fcra": ["15 USC 1681 Fair Credit Reporting Act", "section 1681 consumer reporting agency"],
    "reinvestigation": ["15 USC 1681i reinvestigation", "section 1681i dispute procedure"],
    # Due process
    "due process": ["14th Amendment due process", "Mathews v Eldridge balancing test", "procedural due process"],
    "void judgment": ["void judgment subject matter jurisdiction", "FRCP 60(b)(4) void"],
}

def expand_query(query: str) -> list[str]:
    """Expand a query with citation-anchored variants. Always includes the
    original query first, then up to 4 expansions (5 total max)."""
    queries = [query]
    q_lower = (query or "").lower().strip()
    if not q_lower:
        return queries
    for key, expansions in EXPANSIONS.items():
        if key in q_lower:
            for ex in expansions:
                if ex not in queries:
                    queries.append(ex)
    return queries[:5]

def retrieve(query: str, jurisdiction: str = "federal", final_k: int = 10,
             collection: str | None = None, n_results: int | None = None,
             max_chars: int | None = None):
    """Retrieve relevant chunks from DB with deterministic query expansion.

    Returns a list of dict hits matching what pipeline.db_connector.query_text
    returns (id, text, distance, source, section, chunk_type, metadata, etc.),
    deduplicated across expansion queries and sorted by distance.

    jurisdiction is accepted but currently unused (metadata is inconsistent
    in the corpus so jurisdiction filtering would lose authoritative hits).
    """
    db = get_db()

    # Citation-first branch: if the query is a bare statute/reg citation,
    # use metadata-filtered retrieval (the corpus already carries
    # known_statutes / citations_extracted). Falls through to vector
    # search if no citation match is found.
    _cite = parse_citation(query)
    if _cite is not None:
        try:
            _cite_hits = citation_search(db, _cite, n_results=final_k)
        except Exception:
            _cite_hits = []
        if _cite_hits:
            return _cite_hits[:final_k]

    queries = expand_query(query)
    per_query_k = max(final_k, 10)
    seen_ids = set()
    all_hits = []
    for q in queries:
        try:
            hits = db.query_text(query_text=q, n_results=per_query_k) or []
        except Exception:
            hits = []
        for h in hits:
            hid = h.get("id") or ""
            if hid and hid not in seen_ids:
                seen_ids.add(hid)
                all_hits.append(h)
    all_hits.sort(key=lambda x: x.get("distance", 999))
    return all_hits[:final_k]
