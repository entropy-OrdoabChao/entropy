#!/usr/bin/env python3
"""
pipeline/definitions_index.py

DEFINITIONS INDEX SEARCHER — a headword-keyed index of legal definitions, so pulling the controlling
LEGAL meaning of a term is an EXACT lookup, not a fuzzy semantic guess (which missed terms that were in
the corpus and sometimes returned the wrong-context sense, e.g. bank vs equitable subrogation).

Built ONCE from the corpus's definitional sources, each parsed by its own format:
  - pa_legal_defs / definitions_learned : structured "## Term / ## Definition / Citation" (clean)
  - bouvier_1856 / blacks_*              : dictionary entries "HEADWORD. definition..." (headword = leading caps)
  - USC/statute chunks                   : "the term 'X' means ..." / "'X' means ..."
Stored in ~/turbovec/defs_index.db table `headwords(term, definition, cite, source, kind)`,
term lowercased for O(1) lookup. lookup(term) returns ranked entries (statute/structured > dictionary).

Rebuild: `python -m pipeline.definitions_index --build`  |  Query: `... --lookup garnishee`
Never raises on lookup; safe. The parse is conservative — a messy/empty entry is dropped, not stored.
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import sqlite3
import re
from pathlib import Path
from typing import List, Dict, Any

DB = str(_eroot() / "corpus/defs_index.db")          # already holds `defs` (dictionary chunks) + `defs_fts`
CORPUS = str(_eroot() / "corpus/chunk_text.db")
_LEARNED = (_eroot() / "obsidian_sync/definitions_learned")

# Ranking: curated/case-relevant first; then the general LEGAL-meaning dictionary (always contextually
# safe for a term of art); statute LAST so it only fills terms the dictionary doesn't carry (the
# statute-specific terms — reinvestigation, furnisher, consumer report — which is exactly where we want it,
# without letting a narrow single-title statutory def override a term's general legal meaning).
_TIER = {"structured": 0, "rule": 0, "dictionary": 1, "statute": 2}
# words too common/ambiguous to tag as terms of art (avoid noise); short function words
_STOP = {"the", "and", "for", "that", "with", "any", "all", "such", "this", "term", "law", "act",
         "person" if False else "", "court", "case", "rule", "state", "united", "section"}
_STOP.discard("")


def _clean(s: str) -> str:
    s = " ".join((s or "").split())
    # kill obvious OCR garble tokens
    return s


def _looks_ocr_garbage(s: str) -> bool:
    if not s or len(s) < 25:
        return True
    low = s.lower()
    if re.search(r"gations|exhausting|conusant|romise the|frb_chicago", low):
        return True
    # too many 1-2 char tokens => OCR noise
    toks = s.split()
    short = sum(1 for t in toks if len(t) <= 2)
    return short > len(toks) * 0.45


def _iter_source_chunks(con, like_patterns, limit=None):
    clause = " OR ".join("lower(source) LIKE ?" for _ in like_patterns)
    q = f"SELECT text, source FROM chunk_text WHERE {clause}"
    if limit:
        q += f" LIMIT {limit}"
    yield from con.execute(q, tuple(p + "%" for p in like_patterns))


# ---- per-source parsers: yield (term, definition, cite, kind) ----
def _parse_structured(text: str, source: str):
    """pa_legal_defs / definitions_learned: '## Term' + '## Definition' (+ Citation)."""
    mt = re.search(r"(?ims)^#+\s*Term\s*\n(.+?)(?=\n#|\Z)", text)
    md = re.search(r"(?ims)^#+\s*Definition\s*\n(.+?)(?=\n#|\Z)", text)
    mc = re.search(r"\*\*Citation:\*\*\s*(.+)", text) or re.search(r"\*\*Cite\*\*:\s*(.+)", text)
    if not (mt and md):
        return
    term = re.split(r"\s+and\s+|\s+—\s+|\s*\(", mt.group(1).strip(), 1)[0].strip()
    term = re.sub(r"[^A-Za-z0-9 §.-]", "", term).strip().lower()
    defn = _clean(re.split(r"(?<=[.;])\s", " ".join(md.group(1).split()), 1)[0])[:280]
    cite = _clean(mc.group(1))[:90] if mc else source
    if term and 1 <= len(term.split()) <= 4 and not _looks_ocr_garbage(defn):
        yield term, defn, cite, ("statute" if "statute" in text.lower()[:200] else "structured"), 5


def _def_quality(defn: str, head: str) -> int:
    """Score a candidate definition. <=0 => reject (cross-reference / OCR noise / wrong boundary).
    We do NOT reward definitional lead-ins (that mis-ranked wrong-boundary fragments that happened to
    open 'One'/'The'); we REJECT the tells of a see-also pointer or captured-neighbor-headword."""
    if not defn or len(defn) < 42 or _looks_ocr_garbage(defn):
        return -1
    low = defn.lower()
    # cross-reference / see-also pointer, dictionary pronunciation, or compound-term list fragment
    if re.search(r"\bsee\s+[A-Z]{3,}", defn) or low.startswith(("see ", "id.", "supra", "infra")):
        return -1
    if re.search(r"\([a-z]{2,}[- ][a-z]", defn) or ">>schwa<<" in low or "<<" in defn:
        return -1
    first3 = defn.split()[:3]
    if any(re.fullmatch(r"[A-Z]{3,}[.,;:]?", w) for w in first3):   # captured the NEXT headword
        return -1
    if re.search(r"[a-z]-[a-z]+\s+(?:doctrine|fund|contract|rule|clause)\.", low[:60]):  # 'void-for-vagueness doctrine.'
        return -1
    if sum(1 for w in defn.split() if w[:1].isupper()) > len(defn.split()) * 0.6:  # too many Caps => cite/heading soup
        return -1
    score = 2
    if len(defn) >= 90:                       # a fuller, self-contained definition
        score += 1
    if re.search(r"\b(means|signifies|denotes|is the|is a|is an|consists|defined as)\b", low[:90]):
        score += 1
    return score


def _parse_dictionary(text: str, source: str):
    """Bouvier/Black's/Am.Jur.: 'HEADWORD. Definition sentence.' — headword = leading CAPS run; definition
    is the FIRST self-contained sentence (starts capital, no internal period, ends at a period/semicolon).
    Single-sentence capture keeps the definition from bleeding into the next entry; the gate drops the rest."""
    t = " ".join(text.split())
    for m in re.finditer(r"(?:(?<=\s)|^)([A-Z]{3,}(?:[ '&-][A-Z]{2,}){0,3})\.\s+([A-Z(].{40,300}?\.)", t):
        head = re.sub(r"[^a-z '&-]", "", m.group(1).strip().lower()).strip()
        defn = _clean(m.group(2))
        if not head or not (1 <= len(head.split()) <= 4) or head in _STOP:
            continue
        q = _def_quality(defn, head)
        if q <= 0:
            continue
        cite = ("Bouvier's Law Dictionary (1856)" if "bouvier" in source
                else "Black's Law Dictionary" if "black" in source else "American Jurisprudence"
                if "amjur" in source else source)
        yield head, defn[:300], cite, "dictionary", q


def _parse_statute(text: str, source: str):
    """USC only (CFR excluded — CFR defines terms narrowly for a single regulation, which produced
    wrong-context senses). Matches the statutory-definition idiom: 'The term "X" means ...'."""
    t = " ".join(text.split())
    # require the "term ... means" idiom (a real statutory definition), not a bare quoted word
    for m in re.finditer(r'[Tt]he term[s]?\s+["“\'`]?([A-Za-z][A-Za-z \-]{2,40}?)["”\'`]?\s+means\s+(.{40,300}?[.;])', t):
        term = m.group(1).strip().lower()
        defn = _clean(m.group(2))
        cite = _usc_cite(source, t, m.start())
        if 1 <= len(term.split()) <= 4 and term not in _STOP and not _looks_ocr_garbage(defn):
            yield term, defn[:300], cite, "statute", 5


def _usc_cite(source: str, t: str, pos: int):
    """Turn 'USCODE-2023-title15' + a nearby section marker into a readable USC cite."""
    mtitle = re.search(r"title[-_]?(\d+)", source, re.I)
    title = mtitle.group(1) if mtitle else "?"
    msec = re.search(r"§+\s*(\d[\w.\-]*)", t[max(0, pos - 400):pos + 40])
    sec = f" § {msec.group(1)}" if msec else ""
    return f"{title} U.S.C.{sec}".strip()


def _pacs_cite_from_source(source: str):
    """PA Consolidated Statutes cite straight from the source name: 'pa_cons_stat_t75_s1702' -> '75 Pa.C.S.
    § 1702'; 't75_s1103_1' -> '75 Pa.C.S. § 1103.1'. Returns None for the flat 'pa_consolidated_statutes'
    scheme (which lacks title/section in the name). Reliable — no text-marker guessing."""
    m = re.search(r"pa_cons_stat_t(\d+)_s(\d+[a-zA-Z]?(?:_\d+)*)", source)
    if not m:
        return None
    return f"{m.group(1)} Pa.C.S. § {m.group(2).replace('_', '.')}"


def _parse_pa_statute(text: str, source: str):
    """PA Consolidated Statutes statutory definitions. PA's definitions-section idiom is the quoted term
    followed by a period inside the quote, then the definition sentence(s):  "Serious injury." A personal
    injury resulting in death... — distinct from USC's 'the term "X" means'. Cite from the source name
    (scheme-1 'pa_cons_stat_tNN_sMMMM'), so 75 Pa.C.S. § 1702 (MVFRL) resolves with the correct Pa.C.S. cite."""
    cite = _pacs_cite_from_source(source)
    if not cite:
        return  # flat scheme has no reliable section cite — skip (scheme-1 carries the same defs)
    t = " ".join(text.split())
    for m in re.finditer(r'["“]([A-Z][^"”]{2,55}?)\.["”]\s+([^"”]{40,400}?)(?=["“][A-Z]|$)', t):
        term = m.group(1).strip().lower()
        defn = _clean(m.group(2))
        if 1 <= len(term.split()) <= 6 and term not in _STOP and not _looks_ocr_garbage(defn):
            yield term, defn[:300], cite, "statute", 5


def build_index(verbose: bool = True) -> int:
    con = sqlite3.connect(CORPUS)
    idx = sqlite3.connect(DB)
    idx.execute("DROP TABLE IF EXISTS headwords")
    idx.execute("CREATE TABLE headwords (term TEXT, definition TEXT, cite TEXT, kind TEXT, score INT)")
    idx.execute("CREATE INDEX ix_hw_term ON headwords(term)")
    seen, rows = set(), []

    def _add(gen):
        for term, defn, cite, kind, score in gen:
            key = (term, defn[:60])
            if key in seen:
                continue
            seen.add(key)
            rows.append((term, defn, cite, kind, score))

    # 1) learned (clean, structured)
    for f in (_LEARNED.glob("*.md") if _LEARNED.exists() else []):
        try:
            _add(_parse_structured(f.read_text(encoding="utf-8", errors="ignore"), "definitions_learned"))
        except Exception:
            pass
    # 2) pa_legal_defs (structured)
    for text, src in _iter_source_chunks(con, ["pa_legal_defs"]):
        _add(_parse_structured(text, src))
    # 3) dictionaries (headword form)
    for text, src in _iter_source_chunks(con, ["bouvier", "black", "amjur", "banking_dict"]):
        _add(_parse_dictionary(text, src))
    # 4) statutory definitions — USC ONLY (CFR excluded: too context-narrow, produced wrong-context senses)
    for text, src in _iter_source_chunks(con, ["uscode", "usc-", "usc_"]):
        _add(_parse_statute(text, src))
    # 5) PA Consolidated Statutes statutory definitions (scheme-1 sources carry title/section -> Pa.C.S. cite).
    #    Fixes the gap where 75 Pa.C.S. § 1702 (MVFRL) / 42 Pa.C.S. / 13 Pa.C.S. (UCC) defs were absent.
    for text, src in _iter_source_chunks(con, ["pa_cons_stat_t"]):
        _add(_parse_pa_statute(text, src))

    idx.executemany("INSERT INTO headwords (term, definition, cite, kind, score) VALUES (?,?,?,?,?)", rows)
    idx.commit()
    n_terms = idx.execute("SELECT COUNT(DISTINCT term) FROM headwords").fetchone()[0]
    if verbose:
        print(f"definitions index built: {len(rows)} entries, {n_terms} distinct headwords -> {DB}:headwords")
    con.close(); idx.close()
    return len(rows)


def lookup(term: str, limit: int = 4) -> List[Dict[str, Any]]:
    """Exact headword lookup (then a normalized/singular fallback). Ranked statute/structured before
    dictionary. Returns [{term, definition, cite, kind}]. [] if not indexed."""
    t = (term or "").strip().lower()
    if not t:
        return []
    try:
        con = sqlite3.connect(DB)
        rows = con.execute("SELECT term, definition, cite, kind, score FROM headwords WHERE term=?", (t,)).fetchall()
        if not rows:  # singular/inflection fallback
            base = re.sub(r"(?:ies|es|s|ing|ed|ion|ions)$", "", t)
            rows = con.execute("SELECT term, definition, cite, kind, score FROM headwords WHERE term=?",
                               (base,)).fetchall()
        con.close()
        # rank: authoritative kind first (structured/statute), then highest def-quality score
        rows.sort(key=lambda r: (_TIER.get(r[3], 5), -(r[4] or 0), -len(r[1] or "")))
        return [{"term": r[0], "definition": r[1], "cite": r[2], "kind": r[3], "score": r[4]} for r in rows[:limit]]
    except Exception:
        return []


if __name__ == "__main__":
    import sys
    if "--build" in sys.argv:
        build_index()
    elif "--lookup" in sys.argv:
        q = sys.argv[sys.argv.index("--lookup") + 1]
        for e in lookup(q):
            print(f"  [{e['kind']}] {e['term']} — {e['definition'][:150]}  ({e['cite'][:40]})")
    else:
        print("usage: --build | --lookup <term>")
