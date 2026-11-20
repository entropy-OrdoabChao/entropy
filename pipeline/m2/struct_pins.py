"""struct_pins — the LOOKUP key under path_to_law.hub_rows_for_cite (2026-11-09, retrieval-100).

Cite → the chunk ids that ARE the cited unit, read from the per-chunk role/unit table written at ingest time
(`pipeline/struct_index_build.py` → struct_index.db `chunk_struct`; recipe: output/graph/STRUCT_INDEX_HANDOFF.md).
Before it every pin reverse-engineered "which chunk is § 8371" from the text with regexes at query time and served
the TOC / enactment note / caption. Here the key is written once and looked up.

ONE cite→chunk path (user lock 11-09): this module is called ONLY from path_to_law.hub_rows_for_cite, which already
parses the cite with extract_refs(); it consumes those candidates ('42 Pa.C.S. 8371', 'FRCP 12', 'Pa.R.C.P. 1028',
'73 P.S. 201-2', '28 USC 1332') and resolves reporter cites through cite_registry. It does not re-parse cites.
Coverage is DATA: the family list is read from build_state, a cite whose family is unindexed returns [] with the
reason 'unindexed_family' so the miss is classed, never silent. Deterministic. No model. M2_STRUCT_PINS=0 disables.
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import os
import re
import sqlite3
from functools import lru_cache

STRUCT_DB = os.environ.get("M2_STRUCT_INDEX_DB", str(_eroot() / "corpus/struct_index.db"))
REGISTRY_DB = os.environ.get("M2_CITE_REGISTRY_DB", str(_eroot() / "corpus/cite_registry.db"))
ENABLED = os.environ.get("M2_STRUCT_PINS", "1") == "1"

# hub-candidate grammar (extract_refs output) → (family, title, unit). Title/unit are the chunk_struct columns.
_CAND = (
    (re.compile(r"^(\d{1,2})\s+Pa\.C\.S\.\s+(\d+(?:\.\d+)?)$", re.I),              lambda m: ("pa_cs", m.group(1), m.group(2))),
    (re.compile(r"^(\d{1,3})\s+P\.S\.\s+(\d+(?:[.-]\d+)*)$", re.I),                lambda m: ("pa_ps", m.group(1), m.group(2))),
    (re.compile(r"^(\d{1,2})\s+USC\s+(\d+[a-z]?(?:-\d+[a-z]?)?)$", re.I),           lambda m: (f"usc_t{m.group(1)}", m.group(1), m.group(2).lower())),
    (re.compile(r"^FRCP\s+(\d{1,4})$", re.I),                                       lambda m: ("frcp", "", m.group(1))),
    (re.compile(r"^Pa\.R\.C\.P\.\s+(\d{1,4}(?:\.\d+)?)$", re.I),                     lambda m: ("pa_rcp", "", m.group(1))),
    (re.compile(r"^Pa\.R\.A\.P\.\s+(\d{1,4})$", re.I),                              lambda m: ("pa_rap", "", m.group(1))),
    (re.compile(r"^(\d{1,2})\s+CFR\s+(\d+(?:\.\d+)?)$", re.I),                      lambda m: ("cfr", m.group(1), m.group(2))),
)
_REPORTER = re.compile(r"(\d{1,4})\s+(U\.\s?S\.|F\.\s?(?:2d|3d|4th)|A\.\s?(?:2d|3d)|Pa\.(?:\s?(?:Super|Cmwlth|Commw)\.)?)\s+(\d{1,4})\b", re.I)
last_anchor = None                                                    # regex of the unit heading inside the first chunk (window start), set by chunk_ids_for
last_reason = ""                                                      # why the last call returned [] ('' | unindexed_family | no_rows | no_key)
last_rung = ""                                                        # how the last hit was keyed: 'unit' (direct) | 'ps_crosswalk' (P.S. → act section, 11-10) | 'case'


@lru_cache(maxsize=1)
def _struct():
    return sqlite3.connect(f"file:{STRUCT_DB}?mode=ro", uri=True, check_same_thread=False) if os.path.exists(STRUCT_DB) else None


@lru_cache(maxsize=1)
def _registry():
    return sqlite3.connect(f"file:{REGISTRY_DB}?mode=ro", uri=True, check_same_thread=False) if os.path.exists(REGISTRY_DB) else None


@lru_cache(maxsize=1)
def families() -> frozenset:
    """Families the index actually holds (build_state) — coverage is data, not code."""
    s = _struct()
    if s is None:
        return frozenset()
    return frozenset(r[0] for r in s.execute("SELECT family FROM build_state WHERE done_at IS NOT NULL"))


def unit_key(candidates: list[str]):
    for c in candidates or []:
        for rx, fn in _CAND:
            m = rx.match(c.strip())
            if m:
                return fn(m)
    return None


def case_source(cite: str) -> str | None:
    """Reporter cite → source via cite_registry (reporter, vol, page); scotus_* slug keys preferred over volume scans."""
    reg = _registry(); m = _REPORTER.search(cite or "")
    if not reg or not m:
        return None
    rep = re.sub(r"\s+", "", m.group(2))
    r = reg.execute("SELECT source FROM cite_registry WHERE vol=? AND page=? AND replace(reporter,' ','')=? "
                    "ORDER BY (source LIKE 'scotus_%') DESC LIMIT 1", (int(m.group(1)), int(m.group(3)), rep)).fetchone()
    return r[0] if r else None


def unit_chunk_ids(family: str, title: str, unit: str, cap: int = 6) -> list[str]:
    """TEXT/DEFINITION chunks that carry the unit (T1 `unit` = unit at a chunk edge, `units` = every marker seen in it; a
    unit that begins mid-chunk — Rule 1028 inside ord 12 — is found through `units`), in reading order, never note / toc /
    other. Continuation: the following chunk of the same source is added only when it carries NO marker of its own
    (a long section spilling over); a chunk that opens a new unit is never appended."""
    s = _struct()
    rows = s.execute("SELECT chunk_id, source, ord FROM chunk_struct WHERE family=? AND title=? "
                     "AND (unit=? OR ','||COALESCE(units,'')||',' LIKE ?) AND role IN ('text','definition') ORDER BY ord",
                     (family, title, unit, f"%,{unit},%")).fetchall()
    out = [r[0] for r in rows[:cap]]
    while rows and len(out) < cap:
        _, src, o = rows[-1]
        nxt = s.execute("SELECT chunk_id, source, ord, COALESCE(units,'') FROM chunk_struct WHERE source=? AND ord=? "
                        "AND role IN ('text','definition')", (src, (o or 0) + 1)).fetchone()
        if not nxt or nxt[3].strip(",") or nxt[0] in out:
            break
        out.append(nxt[0]); rows = rows + [nxt[:3]]
    return out


def anchor_for(family: str, unit: str):
    """Regex for the unit's own heading inside a chunk — the window the bot reads must START here, not at char 0, when
    the unit begins mid-chunk (fetch_bot hands bots the first 1,200 chars of a pin)."""
    u = re.escape(unit)
    if family in ("frcp", "pa_rcp", "pa_rap"):
        return re.compile(rf"\bRule\s+{u}\.(?!\d)")
    if family == "pa_ps":
        sec = unit[1:] if re.match(r"^s\d", unit) else unit.split("-", 1)[-1]   # 's611-A' → 'Section 611-A.' (lettered acts, 11-10); '201-2' → 'Section 2.'
        return re.compile(rf"(?m)^\s*Section\s+{re.escape(sec)}\.|§\s*{u}\.")
    if family == "pa_cs":                                              # Pa.C.S. carries an explicit text marker <title>c<unit>s (h = heading/TOC row)
        return re.compile(rf"(?:§|&#167;)\s*{u}\.(?!\d)|\b\d{{1,2}}c{u}s\b")
    return re.compile(rf"(?:§|&#167;|Sec\.)\s*{u}\.(?!\d)")


def ps_crosswalk_pin(title: str, unit: str) -> tuple[str, str] | None:
    """P.S. cite → (source, unit) through the MINED crosswalk table (pipeline/ps_crosswalk_build.py; Pa. Code / Pa. Bulletin
    parentheticals 'section 611-A of the act (40 P.S. § 310.11)'). Only rows that resolved to exactly one source are used;
    ambiguous / unresolved rows are data, never a guess. None when the table is absent (older DB)."""
    s = _struct()
    try:
        r = s.execute("SELECT source, unit FROM ps_crosswalk WHERE ps_cite=? AND status IN ('resolved','resolved_by_sibling') "
                      "AND source<>'' AND unit<>''", (f"{title} P.S. § {unit}",)).fetchone()
    except sqlite3.OperationalError:                                   # no ps_crosswalk table in this DB
        return None
    return (r[0], r[1]) if r else None


def source_unit_chunk_ids(source: str, unit: str, cap: int = 6) -> list[str]:
    """unit_chunk_ids keyed by source instead of (family, title) — the crosswalk already named the source."""
    s = _struct()
    rows = s.execute("SELECT chunk_id, source, ord FROM chunk_struct WHERE source=? "
                     "AND (unit=? OR ','||COALESCE(units,'')||',' LIKE ?) AND role IN ('text','definition') ORDER BY ord",
                     (source, unit, f"%,{unit},%")).fetchall()
    out = [r[0] for r in rows[:cap]]
    while rows and len(out) < cap:
        _, src, o = rows[-1]
        nxt = s.execute("SELECT chunk_id, source, ord, COALESCE(units,'') FROM chunk_struct WHERE source=? AND ord=? "
                        "AND role IN ('text','definition')", (src, (o or 0) + 1)).fetchone()
        if not nxt or nxt[3].strip(",") or nxt[0] in out:
            break
        out.append(nxt[0]); rows = rows + [nxt[:3]]
    return out


def case_chunk_ids(source: str, cap: int = 6) -> list[str]:
    """Holding chunks first (ord), then body text; never caption / procedural / dissent."""
    s = _struct()
    hold = [r[0] for r in s.execute("SELECT chunk_id FROM chunk_struct WHERE source=? AND role='holding' ORDER BY ord", (source,))]
    text = [r[0] for r in s.execute("SELECT chunk_id FROM chunk_struct WHERE source=? AND role='text' ORDER BY ord", (source,))]
    return list(dict.fromkeys(hold + text))[:cap]


def chunk_ids_for(cite: str, refs: list[dict], cap: int = 6) -> list[str]:
    """The lookup. `refs` = path_to_law.extract_refs(cite) (already parsed); reporter cites resolve via the registry.
    [] with `last_reason` set when the key is unknown / its family unindexed / no rows."""
    global last_reason, last_anchor, last_rung
    last_reason = ""; last_anchor = None; last_rung = ""
    if not ENABLED or _struct() is None:
        last_reason = "disabled"; return []
    for r in refs or []:
        if r.get("kind") in ("statute", "rule", "regulation"):
            key = unit_key(r.get("candidates") or [])
            if not key:
                continue
            fam, title, unit = key
            if fam not in families():
                last_reason = f"unindexed_family:{fam}"; return []
            ids = unit_chunk_ids(fam, title, unit, cap)
            if ids:
                last_anchor = anchor_for(fam, unit); last_rung = "unit"
                return ids
            if fam == "pa_ps":                                          # 11-10: P.S. number absent from the act text → mined crosswalk
                pin = ps_crosswalk_pin(title, unit)
                if pin:
                    ids = source_unit_chunk_ids(pin[0], pin[1], cap)
                    if ids:
                        last_anchor = anchor_for(fam, pin[1]); last_rung = "ps_crosswalk"
                        return ids
                    last_reason = f"no_rows:ps_crosswalk/{pin[0]}/{pin[1]}"; return []
            last_reason = f"no_rows:{fam}/{title}/{unit}"
    src = case_source(cite)
    if src:
        ids = case_chunk_ids(src, cap)
        if ids:
            last_rung = "case"
            return ids
        last_reason = last_reason or f"unindexed_source:{src}"
    last_reason = last_reason or "no_key"
    return []


# ----------------------------------------------------------------------------------------------- definition keys (11-10)
# The DEFINITION rungs of case_definitions_page.keyed_definition_lookup read the same ingest-time key table: a statute's own
# `role='definition'` rows (+ the cited unit's text) and the dictionary/treatise headword rows (`unit`/`heading` = headword at
# ingest). Read-only; no cite parsing here — the cite→chunk step stays path_to_law.hub_rows_for_cite (user lock 11-09).
DICT_FAMILIES = ("dictionary_blacks", "dictionary_bouvier", "treatise_cjs", "treatise_amjur", "dictionary_other")


def chunk_meta(chunk_id: str) -> dict:
    """{source, family, title, unit, units, role, heading, ord} for one chunk id ({} when the id is not in the index)."""
    s = _struct()
    if s is None:
        return {}
    r = s.execute("SELECT source, family, title, unit, COALESCE(units,''), role, COALESCE(heading,''), ord FROM chunk_struct "
                  "WHERE chunk_id=?", (chunk_id,)).fetchone()
    return dict(zip(("source", "family", "title", "unit", "units", "role", "heading", "ord"), r)) if r else {}


def definition_rows(source: str, unit: str | None = None) -> list[dict]:
    """A source's own definition chunks: every `role='definition'` row, every row of a unit whose heading reads 'Definitions',
    and (when given) every text/definition chunk of `unit` — reading order, deduped. [] when the source is not indexed."""
    s = _struct()
    if s is None or not source:
        return []
    rows = s.execute("SELECT chunk_id, unit, COALESCE(heading,''), role, ord, family FROM chunk_struct WHERE source=? "
                     "AND (role='definition' OR heading LIKE '%efinition%') AND role IN ('text','definition') ORDER BY ord",
                     (source,)).fetchall()
    out, seen = [], set()
    if unit:
        for cid in source_unit_chunk_ids(source, unit, cap=12):
            if cid not in seen:
                m = chunk_meta(cid); seen.add(cid)
                out.append({"chunk_id": cid, "unit": m.get("unit", unit), "heading": m.get("heading", ""), "role": m.get("role", ""),
                            "ord": m.get("ord"), "family": m.get("family", ""), "cited_unit": True})
    for cid, u, h, role, o, fam in rows:
        if cid not in seen:
            seen.add(cid); out.append({"chunk_id": cid, "unit": u, "heading": h, "role": role, "ord": o, "family": fam, "cited_unit": False})
    return out


def headword_rows(term: str, families: tuple = DICT_FAMILIES, neighbors: bool = True) -> list[dict]:
    """Dictionary / treatise chunks keyed by HEADWORD (ingest wrote the headword into `unit` = headword at the chunk start and
    `heading` = headword that begins inside the chunk). Exact headword only (upper-cased); no LIKE, no guess. With `neighbors`
    the chunk that FOLLOWS a heading row (same source, ord+1) is appended as a continuation candidate: in the OCR'd 8th/9th
    the index keyed the page running head and the entry line itself sits in the next chunk (header split)."""
    s = _struct()
    if s is None or not term:
        return []
    hw = re.sub(r"\s+", " ", term).strip().upper()
    q = ",".join("?" * len(families))
    rows = s.execute(f"SELECT chunk_id, source, family, unit, COALESCE(heading,''), role, ord FROM chunk_struct WHERE family IN ({q}) "
                     "AND (UPPER(heading)=? OR UPPER(unit)=?) ORDER BY source, ord", tuple(families) + (hw, hw)).fetchall()
    out = [dict(zip(("chunk_id", "source", "family", "unit", "heading", "role", "ord"), r), continuation=False) for r in rows]
    if neighbors:
        seen = {r["chunk_id"] for r in out}
        for r in list(out):
            if (r["heading"] or "").upper() != hw or r["ord"] is None:
                continue
            n = s.execute("SELECT chunk_id, source, family, unit, COALESCE(heading,''), role, ord FROM chunk_struct WHERE source=? AND ord=? "
                          "AND role IN ('text','definition')", (r["source"], r["ord"] + 1)).fetchone()
            if n and n[0] not in seen:
                seen.add(n[0]); out.append(dict(zip(("chunk_id", "source", "family", "unit", "heading", "role", "ord"), n), continuation=True))
    return out
