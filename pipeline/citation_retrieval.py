"""
pipeline/citation_retrieval.py - citation-aware retrieval branch.

Problem: pure vector search ranks citation queries poorly because
"15 U.S.C. 1681s-2(b)" embeds similarly to any "<n> U.S.C. <n>" string.
The corpus already carries citation metadata (known_statutes,
citations_extracted) extracted at ingestion - this module USES it.

No re-ingestion. Read-only. Two-stage retrieval:
  1. Detect + normalize a citation in the query.
  2. Pull candidate chunks via ChromaDB metadata filter on the
     statute/title, then client-side match the normalized citation
     against known_statutes + citations_extracted.
  3. Caller falls back to vector search if this returns nothing.
"""
from __future__ import annotations
import re
from typing import Optional

# --- Citation parsing -------------------------------------------------------

# USC:  "15 U.S.C. 1681s-2(b)", "15 USC 1681s-2b", "15 U.S.C. 1681",
#        bare "1681s-2(b)" / "1681s-2"
# CFR:  "12 C.F.R. 1022.41", "12 CFR 1022.41", "12 C.F.R. 1022.137"
_USC_RE = re.compile(
    r"""(?:(\d{1,2})\s*U\.?\s*S\.?\s*C\.?\s*)?   # optional title: 15 U.S.C.
        (?:§|sec(?:tion)?\.?|s\.?)?\s*  # optional section mark
        (\d{3,4}[a-z]?(?:-\d+)?)                  # core section: 1681, 1681s-2
        (?:\(([a-z0-9]+)\))?                      # optional subsection: (b)
    """,
    re.IGNORECASE | re.VERBOSE,
)
_CFR_RE = re.compile(
    r"""(\d{1,2})\s*C\.?\s*F\.?\s*R\.?\s*
        (?:§|part|sec(?:tion)?\.?)?\s*
        (\d{1,4}\.\d{1,4})
    """,
    re.IGNORECASE | re.VERBOSE,
)


class ParsedCitation:
    """A normalized citation. kind is 'usc' or 'cfr'."""
    __slots__ = ("kind", "title", "section", "subsection", "raw")

    def __init__(self, kind, title, section, subsection, raw):
        self.kind = kind
        self.title = title          # "15", "12"
        self.section = section      # "1681s-2", "1681", "1022.41"
        self.subsection = subsection or ""   # "b"
        self.raw = raw

    @property
    def core_section_number(self) -> str:
        """The leading numeric portion: 1681s-2 -> 1681, 1022.41 -> 1022."""
        m = re.match(r"(\d+)", self.section)
        return m.group(1) if m else self.section

    def usc_string_variants(self) -> list[str]:
        """How this citation might appear in known_statutes /
        citations_extracted metadata. The corpus stores at section
        granularity, e.g. '15 U.S.C. § 1681'.

        If title is unknown, returns section-only fragments (e.g. "1681")
        that Stage 2 can substring-match against the metadata blob.
        """
        if self.kind != "usc":
            return []
        t = self.title or ""
        core = self.core_section_number
        full = self.section
        out = set()
        if t:
            for sec in {core, full}:
                out.add(f"{t} U.S.C. § {sec}")
                out.add(f"{t} U.S.C. {sec}")
                out.add(f"{t} USC {sec}")
        else:
            # Title-less: cannot do Stage 1 $in match, but still useful
            # as substring fragments for Stage 2.
            for sec in {core, full}:
                if sec:
                    out.add(sec)
        return sorted(out)

    def cfr_string_variants(self) -> list[str]:
        if self.kind != "cfr":
            return []
        t = self.title or ""
        sec = self.section
        return [
            f"{t} C.F.R. § {sec}",
            f"{t} C.F.R. {sec}",
            f"{t} CFR {sec}",
        ]

    def __repr__(self):
        sub = f"({self.subsection})" if self.subsection else ""
        return f"<ParsedCitation {self.kind} {self.title} {self.section}{sub}>"


def parse_citation(query: str) -> Optional[ParsedCitation]:
    """Return a ParsedCitation if the query looks like a statute/reg
    citation, else None. Only fires when the query is SHORT and
    citation-dominated - long natural-language queries return None so
    they go to normal vector search."""
    if not query:
        return None
    q = query.strip()
    # If the query is long prose, it is not a bare citation lookup.
    if len(q) > 60:
        return None

    m = _CFR_RE.search(q)
    if m:
        return ParsedCitation("cfr", m.group(1), m.group(2), "", q)

    m = _USC_RE.search(q)
    if m:
        title, section, subsection = m.group(1), m.group(2), m.group(3)
        # Require either an explicit title ("15 U.S.C.") OR a
        # section that is clearly a statute number (>= 3 digits).
        if not title and not re.match(r"\d{3,4}", section or ""):
            return None
        # Guard: if the query has many non-citation words, skip.
        word_count = len(re.findall(r"[A-Za-z]{3,}", q))
        if word_count > 6:
            return None
        # Bare-section query with no title: we cannot know the USC title,
        # but we can still match on the section number in metadata + text.
        # Mark title as "" and let citation_search handle title-less match.
        return ParsedCitation("usc", title or "", section, subsection, q)

    # Dedicated bare-section fallback: a query that is JUST a section
    # number like "1681s-2" or "1681s-2(b)" with nothing else.
    bare = re.fullmatch(r"\s*(\d{3,4}[a-z]?(?:-\d+)?)(?:\(([a-z0-9]+)\))?\s*", q, re.IGNORECASE)
    if bare:
        return ParsedCitation("usc", "", bare.group(1), bare.group(2) or "", q)

    return None


# --- Citation-filtered retrieval -------------------------------------------

def citation_search(db, citation: ParsedCitation, n_results: int = 10) -> list[dict]:
    """Retrieve chunks matching a parsed citation using metadata, not
    vector similarity. Returns hit dicts in the same shape as
    db.query_text(). Empty list if nothing matches (caller falls back)."""
    variants = (citation.usc_string_variants() if citation.kind == "usc"
                else citation.cfr_string_variants())
    if not variants:
        return []

    # Stage 1: ChromaDB metadata filter to pull a candidate pool.
    # known_statutes is pipe-delimited, so an exact $eq only catches
    # single-statute chunks; $in over FULL variants catches those.
    # Skip Stage 1 entirely when we only have section fragments
    # (title-less queries) - they cannot exact-match the metadata.
    db._connect()
    coll = db._collection
    pool_ids, pool_docs, pool_meta = [], [], []

    # Only $in if at least one variant looks like a full "<title> U.S.C. ..."
    # or "<title> C.F.R. ..." string. Bare section numbers are fragments.
    full_variants = [v for v in variants if (" U.S.C" in v or " USC" in v or " C.F.R" in v or " CFR" in v)]
    if full_variants:
        try:
            res = coll.get(
                where={"known_statutes": {"$in": full_variants}},
                include=["documents", "metadatas"],
                limit=max(n_results * 5, 50),
            )
            pool_ids = res.get("ids", []) or []
            pool_docs = res.get("documents", []) or []
            pool_meta = res.get("metadatas", []) or []
        except Exception:
            pass

    # Stage 2: also scan a broader pool for multi-statute / citations_extracted
    # hits. Pull chunks that have ANY citation, then client-side match.
    core = citation.core_section_number
    if len(pool_ids) < n_results:
        try:
            res2 = coll.get(
                where={"has_citation": "True"},
                include=["documents", "metadatas"],
                limit=20000,
            )
            ids2 = res2.get("ids", []) or []
            docs2 = res2.get("documents", []) or []
            meta2 = res2.get("metadatas", []) or []
            seen = set(pool_ids)
            for i, md in enumerate(meta2):
                ks = str(md.get("known_statutes", ""))
                ce = str(md.get("citations_extracted", ""))
                blob = ks + "|" + ce
                # match the core section number against the metadata
                if core and core in blob:
                    # tighten: require the title too if we have one
                    if citation.title and citation.kind == "usc":
                        if f"{citation.title} U.S.C" not in blob and                            f"{citation.title} USC" not in blob:
                            continue
                    if citation.title and citation.kind == "cfr":
                        if f"{citation.title} C.F.R" not in blob and                            f"{citation.title} CFR" not in blob:
                            continue
                    cid = ids2[i]
                    if cid in seen:
                        continue
                    seen.add(cid)
                    pool_ids.append(cid)
                    pool_docs.append(docs2[i] if i < len(docs2) else "")
                    pool_meta.append(md)
        except Exception:
            pass

    if not pool_ids:
        return []

    # Build hit dicts. Ranking priority (higher score = better):
    #   +4/+5  chunk TEXT contains the full section (and subsection)
    #   +3     chunk TEXT contains the core section number
    #   +2/+1  metadata names the section / subsection (weak signal)
    #   -2     demote form / template / letter chunk types
    # The actual statutory TEXT must outrank chunks merely TAGGED with
    # the parent statute - a generic FCRA fee table should never beat
    # the chunk that literally contains "1681s-2(b)".
    full_sec = citation.section
    core_sec = citation.core_section_number
    sub = citation.subsection
    hits = []
    for i, cid in enumerate(pool_ids):
        md = pool_meta[i] if i < len(pool_meta) else {}
        text = pool_docs[i] if i < len(pool_docs) else ""
        meta_blob = str(md.get("known_statutes", "")) + "|" + str(md.get("citations_extracted", ""))
        score = 0
        # text-based scoring (strong signal)
        if full_sec and full_sec in text:
            score += 4
            if sub and f"({sub})" in text:
                score += 1
        elif citation.kind == "usc" and core_sec and core_sec in text:
            # USC only: a bare core-section hit like "1681" in text is
            # reasonable evidence. For CFR we REQUIRE the full "1022.41"
            # to avoid matching "1022" incidentally.
            score += 3
        # metadata-based scoring (weak signal)
        if full_sec and full_sec in meta_blob:
            score += 2
        if sub and f"({sub})" in meta_blob:
            score += 1
        # ---- demote non-statute material ----
        src_l = str(md.get("source", "")).lower()
        # CFPB/agency guidance, warning letters, FAQs, advisories
        if any(k in src_l for k in (
            "warning_letter", "enforcement", "guidance",
            "advisory", "faq", "supervisory", "press_release",
            "consent_order",
        )):
            score -= 3
        # form/template tags (kept from the original rule)
        dt = str(md.get("doc_type_classified", ""))
        ct = str(md.get("chunk_type", ""))
        if dt in ("form", "letter_template") or "template" in ct:
            score -= 2
        # text-pattern: bracket placeholders mean a fill-in form letter
        # e.g. "[Company Name]", "[Address]", "[DATE]", "[Name]"
        import re as _re
        if _re.search(r"\[(Company Name|Address|DATE|Name|Date)\]", text):
            score -= 3
        # fee-table heuristic: chunk starts with a dollar amount or
        # contains "maximum allowable charge" - administrative schedule
        if text[:80].count("$") >= 2 or "maximum allowable charge" in text.lower():
            score -= 2

        # ---- promote primary-law sources ----
        if any(k in src_l for k in (
            "uscode", "u.s.code", "us_code", "publaw", "public_law",
            "code-title", "house-bills", "senate-bills",
            "statutes-at-large", "stat.",
        )):
            score += 3
        # also promote regulation chunks whose source clearly is a CFR part
        if "cfr-title" in src_l or "cfr_title" in src_l or "ecfr" in src_l:
            score += 2
        hits.append({
            "id": cid,
            "text": text,
            "distance": 1.0 / (1 + max(score, 0)),
            "_score": score,
            "source": md.get("source", "unknown"),
            "section": md.get("section", ""),
            "chunk_type": md.get("chunk_type", "general"),
            "citations": md.get("citations", ""),
            "doc_id": md.get("doc_id", ""),
            "has_definitions": md.get("has_defs", "False") == "True",
            "metadata": md,
            "retrieval_mode": "citation",
        })
    hits.sort(key=lambda h: (-h["_score"], h["distance"]))
    # If nothing scored above 0, the pool had no real textual match -
    # return empty so the caller falls back to vector search.
    if hits and hits[0]["_score"] <= 0:
        return []
    return hits[:n_results]
