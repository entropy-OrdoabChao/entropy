#!/usr/bin/env python3
"""struct_index_build.py — T1 STRUCTURAL INDEX for the turbovec corpus (2026-11-09).

Why: chunks in chunk_text.db carry no structural metadata, so a cite to HELD law can pin a caption / TOC / note chunk
instead of its own verbatim text (D2 evidence: Hart v. O'Malley, Wiernik 736 A.2d 616 pinned CAPTION chunks;
'73 P.S. § 201-2(3)' routed to Dog Law). This builder writes ONE row per chunk with family / title / unit / role / ord:

  ~/turbovec/struct_index.db
    chunk_struct(chunk_id PK, source, family, title, unit, units, role, heading, ord, ord_mode, score)
    build_state(family PK, n_sources, n_chunks, started, done_at, secs)
    ix_cs_source(source) · ix_cs_fam_unit(family, title, unit) · ix_cs_role(source, role)

  role ∈ {text, note, definition, holding, syllabus, caption, procedural, table, toc, other}
  unit  = § / Rule / part number of the FIRST heading in the chunk, or the unit carried forward from the previous chunk
          (continuation) when the source's chunk order is recoverable; `units` = every heading number in the chunk.
  ord   = position of the chunk in its source: `_chunk_N` / `_cN` suffix, else sha1(f"{src}:{n}")[:16] / md5(f"{src}_{n}")
          brute force (the two id conventions the ingest lanes use), else sorted-by-id (ord_mode='sorted' = NOT reliable).

EXTENDS (does not duplicate) cite_registry.db rule_units (unit → heading-chunk ids, built by pipeline.cite_registry
--build-units) and chunk_part.db (SCOTUS part tagging). Zero LLM. Bounded per family, resumable (build_state), idempotent
(INSERT OR REPLACE by chunk_id). Read-only on chunk_text.db.

Run : env -u PYTHONPATH /usr/bin/python3 -m pipeline.struct_index_build --families priority [--minutes 60] [--force]
      env -u PYTHONPATH /usr/bin/python3 -m pipeline.struct_index_build --families coverage --only-missing   # sandbox coverage top-up
      STRUCT_DB=/path/to/copy.db … (env STRUCT_DB overrides the target DB; the live path is never written from the sandbox)
      env -u PYTHONPATH /usr/bin/python3 -m pipeline.struct_index_build --report
Verify: python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" pipeline/struct_index_build.py

OVERRIDE LAYER (sandbox 2026-11-10, orphan_exclusion_spec.md §last): family_of() stays PURE (slug -> FAMILIES table). Two DATA
tables in struct_index.db, written by struct_family_apply.rebuild_override_tables() from struct_family_audit, are consulted by
the BUILD path only (load_overrides / resolve_family):
  source_family_override(source PK, family, kind, title, confidence, origin, ts)  — used ONLY when family_of() returns
      'unclassified' (deterministic table first = the guardrail; the LLM verdict fills only the hole it was asked about)
  source_chunk_override(chunk_id PK, source, ord, method)                        — orphan re-keys: the orphan build emits
      those chunks under the recovered source instead of re-filing them as orphans
Override families are buildable (--families coverage / all append them); kind_of() falls back to the override kind.
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import argparse, hashlib, json, os, re, sqlite3, sys, time
from pathlib import Path

TV = Path(os.environ.get("TURBOVEC_DIR", str(_eroot() / "corpus")))
CHUNK_DB = TV / "chunk_text.db"
STRUCT_DB = Path(os.environ.get("STRUCT_DB", str(TV / "struct_index.db")))
PART_DB = TV / "chunk_part.db"

# ---------------------------------------------------------------------------------------------------------------------
# FAMILIES: name -> (sqlite range prefix, kind, title_regex_or_None). Range scan on ix_ct_source (prefix >= p AND < p+'\uffff').
# kind: statute | rule | reg | case | local_rule. Priority order = the order below (user: 231 / Pa.C.S. / P.S. / USC 15,28 /
# FRCP / PA cases / courtlistener first).
# ---------------------------------------------------------------------------------------------------------------------
FAMILIES: list[tuple[str, str, str, str | None]] = [
    # SANDBOX 2026-11-10 (coverage build): matching is CASE-INSENSITIVE on lower(source); prefix "re:<regex>" = regex family;
    # title regex is applied to lower(source) and every non-empty group is joined with '-'. First match wins; the LAST row is the
    # catch-all `unclassified`; NULL / 'unknown' sources are family `orphan` (handled in family_of before the table).
    ("pa_rcp",        "pa_code_t231_", "rule",    None),
    ("pa_rcp_hand",   "pa_rcp_",       "rule",    None),
    ("pa_rap",        "pa_code_t210_", "rule",    None),
    ("pa_cs",         "pa_cs_t",       "statute", r"^pa_cs_t(\d+)"),
    ("pa_ps",         "pa_",           "statute", r"^pa_(\d+)ps_"),          # filtered by regex below
    ("frcp",          "frcp_",         "rule",    None),
    ("frap",          "frap_",         "rule",    None),
    ("fre",           "fre_",          "rule",    None),
    ("usc_t15",       "usc_t15_",      "statute", r"^usc_t(\d+)_"),
    ("usc_t28",       "usc_t28_",      "statute", r"^usc_t(\d+)_"),
    ("case_scotus_slug", "scotus_",    "case",    None),                      # PA + federal cases keyed scotus_<slug>_<cite>
    ("case_cap_pa",   "cap_pa",        "case",    None),
    ("case_docvault", "docvault_pull_","case",    None),
    ("case_ca3",      "ca3_",          "case",    None),
    ("phila_cr",      "phila_cr_",     "local_rule", None),
    ("phila_civ",     "phila_civ",     "local_rule", None),
    ("cp_trial",      "cp-trial",      "local_rule", None),
    ("edpa_lr",       "edpa_lr_",      "local_rule", None),
    ("pa_code_other", "pa_code_t",     "reg",     r"^pa_code_t(\d+)_"),      # minus 231/210 (filtered)
    ("cfr_hand",      "cfr_",          "reg",     r"^cfr_(\d+)_"),
    ("courtlistener_other", "courtlistener_", "case", None),                 # minus plaw/cfr/pa/frcp/fre/frap (filtered)
    ("usc_rest",      "usc_t",         "statute", r"^usc_t(\d+)_"),          # minus t15/t28
    ("pa_code_pacb",  r"re:^\d{3}_\d{4}", "reg",  r"^(\d{3})_\d{4}"),        # NNN_NNNN pacodeandbulletin keys
    ("case_cap_us",   "cap_us_",       "case",    None),
    ("case_cap_other","cap_",          "case",    None),                     # minus us_/pa/nj_ (filtered)
    ("case_nj_cap",   "cap_nj_",       "case",    None),
    ("case_usreports","scotus_vol_",   "case",    None),
    ("cfr_govinfo",   "cfr-",          "reg",     r"^cfr-\d+-title(\d+)"),   # CFR-2024-title12-vol8 AND cfr-2024-title40-vol20(_1) (case variants)
    ("ecfr",          "ecfr-",         "reg",     r"^ecfr-title(\d+)"),      # ECFR-title12 AND ecfr-title10(_1)
    # ---- coverage families (sandbox 2026-11-10): codified families that were missing from the index ------------------------
    ("uscode_govinfo","uscode-",       "statute", r"^uscode-(?:\d{4}|lii)-title(\d+)"),   # USCODE-2023-title26 / uscode-… / USCODE-LII-title28 / …-chapNN-secNNNN
    ("usc_title",     "usc-title",     "statute", r"^usc-title(\d+)"),       # USC-title26
    ("courtlistener_plaw", "courtlistener_plaw_", "statute", r"^courtlistener_plaw_(\d+)_(\d+)"),
    ("courtlistener_cfr",  "courtlistener_cfr_",  "reg",     r"^courtlistener_cfr_(\d+)_part_"),
    ("courtlistener_rules","re:^courtlistener_(?:frcp|fre|frap|pa_rap|pa_code_t231|pa_code_t210)", "rule", r"^courtlistener_pa_code_t(\d+)_"),
    ("courtlistener_pa_code", "courtlistener_pa_code", "reg", r"^courtlistener_pa_code_t(?:itle_)?(\d+)"),
    ("plaw",          "plaw",          "statute", r"^plaw[-_](\d+)(?:publ|_)(\d+)"),   # plaw_109_304 / plaw-104publ59 / PLAW-116publ283
    ("comps",         "comps-",        "statute", r"^comps-(\d+)"),          # COMPS-16716 / comps-10008(_1)  govinfo compiled statutes
    ("njac",          "njac_",         "reg",     r"^njac_t(\d+[a-z]?)_s"),  # njac_t5_s70-3.2  -> N.J.A.C. 5:70-3.2
    ("nj_stat",       "nj_stat_",      "statute", r"^nj_stat_t(\d+[a-z]?)_s"),  # nj_stat_t34_s11-56a -> N.J.S.A. 34:11-56a
    ("nj_court_rule", "nj_court_rule_","rule",    r"^nj_court_rule_(\d+)_"), # nj_court_rule_1_10_1 -> R. 1:10-1
    ("nj_other",      "nj_",           "statute", None),                     # nj_const etc.
    ("pa_cons_stat",  "pa_cons_stat_t","statute", r"^pa_cons_stat_t(\d+)_s"),  # pa_cons_stat_t75_s1718 -> 75 Pa.C.S. § 1718
    ("pa_consolidated","pa_consolidated_statutes", "statute", None),         # one 14K-chunk dump, § headings inline
    ("treatise_amjur", r"re:^(?:amjur|american_jurisprudence)", "treatise", r"^amjur2d_(\d+[a-z]?)_(.+)$"),   # amjur2d_16_conflict_of_laws -> title 16-conflict_of_laws
    ("treatise_cjs",  r"re:^(?:cjs_vol|\d+-corpus_20juris)", "treatise", r"^cjs_vol_?(\d+)"),
    ("treatise_encyc","encyc_",        "treatise", None),
    ("dictionary_blacks", "blacks_law_", "dictionary", r"^blacks_law_(\d+(?:st|nd|rd|th))"),
    ("dictionary_bouvier","bouvier_",  "dictionary", None),
    ("dictionary_other",  "law_dictionary", "dictionary", None),
    ("case_usreports_pdfa", "usreports-", "case", r"^usreports-(\d+)_pdfa"), # USREPORTS-463_PDFA + usreports-463_pdfa: same family, canonical lowercase
    ("case_usreports_bv",    r"re:^\d{3}bv(?:_\d+)?$", "case", r"^(\d{3})bv"),    # 552bv / 586BV_1 bound volumes
    ("case_usreports_pp",   r"re:^\d{3}us\dpp", "case", r"^(\d{3})us(\d)pp"),  # 590US2PP_web_8 preliminary prints
    ("pa_bulletin",   r"re:^\d{2}[-_]\d{1,2}(?:_p\d|_[a-z][a-z\-]*)?$", "document", r"^(\d{2})[-_]\d{1,2}"),  # 36-34 / 39_29 / 26_11_p2 / 26_10_not|rr|cts|prm|dgs|sop|ga|gv = Pa. Bulletin vol-issue(-section)
    ("scotus_journal","jnl",           "document", r"^jnl(\d+)"),
    ("trf",           "trf-",          "document", None),                    # thrift financial report dataset (non-law)
    ("irs",           "irs",           "document", None),
    ("irs_pubs",      r"re:^p\d+[a-z]*$", "document", r"^p(\d+)"),
    ("frb",           "frb_",          "document", None),
    ("fara",          "fara",          "document", None),
    ("senate_manual", "sman-",         "document", r"^sman-(\d+)"),
    ("sec_forms",     r"re:^sec\d",    "document", None),
    ("ussgl",         r"re:^(?:part\d|p\d-combined)", "document", None),
    ("numeric_doc",   r"re:^\d{6,}_\d{3}$", "document", None),
    ("pa_other",      "pa_",           "document", None),                    # pa_dor_pit_guide, pa_legal_defs, … (non-codified PA material)
    ("unclassified",  "re:",           "document", None),                    # CATCH-ALL — every non-null source lands here at worst
]
PRIORITY = [f[0] for f in FAMILIES[:22]]
FAM_INDEX = {f[0]: f for f in FAMILIES}
COVERAGE_FAMILIES = [f[0] for f in FAMILIES[FAMILIES.index(FAM_INDEX["uscode_govinfo"]):]] + ["orphan"]
ORPHAN = "orphan"                                                             # NULL / 'unknown' source
KIND_OF = {f[0]: f[2] for f in FAMILIES}; KIND_OF[ORPHAN] = "document"
VOLUME_CASE_FAMILIES = {"case_usreports", "case_usreports_pdfa", "case_usreports_bv", "case_usreports_pp"}   # many opinions per source

_SKIP = {                                                                     # applied to lower(source)
    "pa_ps": lambda s: not re.match(r"^pa_\d+ps_", s),
    "pa_code_other": lambda s: re.match(r"^pa_code_t(231|210)_", s) is not None,
    "courtlistener_other": lambda s: re.match(r"^courtlistener_(plaw|cfr|pa|frcp|fre|frap)", s) is not None,
    "usc_rest": lambda s: re.match(r"^usc_t(15|28)_", s) is not None,
    "case_cap_other": lambda s: re.match(r"^cap_(us_|pa|nj_)", s) is not None,
    "case_scotus_slug": lambda s: s.startswith("scotus_vol_"),               # case-insensitive match would swallow SCOTUS_vol_*
}


def is_orphan_source(source) -> bool:
    return source is None or source == "" or source == "unknown"


def family_of(source: str | None) -> tuple[str, str]:
    """-> (family, title) for a source slug by the FAMILIES table (first match wins, honouring the skip filters).
    Case-insensitive (lower(source)); NULL/'unknown' -> ('orphan', ''); anything unmatched -> ('unclassified', '')."""
    if is_orphan_source(source): return ORPHAN, ""
    low = source.lower()
    for fam, prefix, _kind, trx in FAMILIES:
        if prefix.startswith("re:"):
            if prefix != "re:" and not re.match(prefix[3:], low): continue
        elif not low.startswith(prefix): continue
        if fam in _SKIP and _SKIP[fam](low): continue
        title = ""
        if trx:
            m = re.match(trx, low)
            title = "-".join(g for g in m.groups() if g) if m else ""
        return fam, title
    return "unclassified", ""


def kind_of(fam: str) -> str:
    return KIND_OF.get(fam) or _OVERRIDE_KINDS.get(fam, "")


# ---------------------------------------------------------------------------------------------------------------------
# Override layer (DATA, not code): consulted by the build path only when family_of() returns 'unclassified'.
# ---------------------------------------------------------------------------------------------------------------------
_OVERRIDES: dict[str, tuple[str, str]] = {}          # source -> (family, title)
_OVERRIDE_KINDS: dict[str, str] = {}                 # override family -> builder kind
_CHUNK_OVERRIDES: dict[str, tuple[str, int | None]] = {}   # chunk_id -> (recovered source, ord or None)
_OVERRIDES_LOADED = False


def _table_exists(con, name: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def load_overrides(con_struct) -> dict:
    """Load source_family_override + source_chunk_override once per process (empty dicts when the tables are absent, e.g. the
    live DB before promotion — the build then behaves exactly as before). Returns counts."""
    global _OVERRIDES_LOADED
    _OVERRIDES.clear(); _OVERRIDE_KINDS.clear(); _CHUNK_OVERRIDES.clear()
    if _table_exists(con_struct, "source_family_override"):
        for src, fam, kind, title in con_struct.execute("SELECT source, family, kind, title FROM source_family_override"):
            if not fam or fam in ("unclassified", ORPHAN): continue
            _OVERRIDES[src] = (fam, title or "")
            if fam not in KIND_OF: _OVERRIDE_KINDS.setdefault(fam, kind or "document")
    if _table_exists(con_struct, "source_chunk_override"):
        for cid, src, o in con_struct.execute("SELECT chunk_id, source, ord FROM source_chunk_override"):
            if src and not is_orphan_source(src): _CHUNK_OVERRIDES[cid] = (src, o)
    _OVERRIDES_LOADED = True
    return {"source_overrides": len(_OVERRIDES), "override_families": len(_OVERRIDE_KINDS), "chunk_overrides": len(_CHUNK_OVERRIDES)}


def override_families() -> list[str]:
    """Distinct override families that are NOT in FAMILIES (buildable like any coverage family; sorted for determinism)."""
    return sorted(_OVERRIDE_KINDS)


_RESOLVE_CACHE: dict[str, tuple[str, str]] = {}


def resolve_family(source: str | None) -> tuple[str, str]:
    """BUILD-PATH resolver: family_of() first (pure, deterministic); the override is consulted ONLY when that returns
    'unclassified'. Cached per process (340K sources x ~60 families per build pass otherwise)."""
    if is_orphan_source(source): return ORPHAN, ""
    hit = _RESOLVE_CACHE.get(source)
    if hit is not None: return hit
    fam, title = family_of(source)
    if fam == "unclassified":
        ov = _OVERRIDES.get(source)
        if ov is not None: fam, title = ov
    _RESOLVE_CACHE[source] = (fam, title)
    return fam, title


# ---------------------------------------------------------------------------------------------------------------------
# Heading / role regexes (shared with cite_registry G0c where the shapes overlap — kept here so the index is self-contained)
# ---------------------------------------------------------------------------------------------------------------------
RX_SEC = re.compile(r"(?:§|&#167;|&sect;)\s*(\d{1,5}[A-Za-z]?(?:[.-]\d+)*)\.\s{1,3}([A-Z\"“(][^.\n]{2,140}?)\.(?=\s|$)")
RX_SEC_LOOSE = re.compile(r"^\s*(?:§|&#167;|&sect;|Sec(?:tion)?\.?)\s*(\d{1,5}[A-Za-z]?(?:[.-]\d+)*)\.?\s+([A-Z][^\n]{2,140})$", re.M)
RX_RULE = re.compile(r"\bRule\s+(\d{1,4}(?:\.\d+)?)\.\s+([A-Z\[\"“][^.\n]{2,120}?)\.(?=\s|$)")
RX_PS_HEAD = re.compile(r"Section\s+(\d+(?:\.\d+)?(?:-[A-Z](?:\.\d+)?)?)\.\s{1,3}([A-Z][^.]{2,140}?)\.--")   # 11-10: lettered acts (Section 611-A. / 1102-C.4.)
# PA LETTERED ACTS (sandbox 2026-11-10, class defect): unconsolidated acts are amended with lettered articles/sections
# (Article VI-A, 'Section 611-A. Prohibited acts.' — 40 P.S. §§ 310.1–310.99; same pattern in 72/35/53/24 P.S.). Those
# sections print in the NEWLINE form ('Section 611-A. Prohibited acts.\n body' — no '.--'), so RX_PS_HEAD never saw them and
# every Article VI-A chunk carried the last plain unit (s663) with an empty heading. The extended parser below is applied
# ONLY to sources that carry at least one lettered heading (ps_context()['lettered']); every other P.S. source keeps its
# unit / heading labels byte-identical (promotion safety — the plain newline-form sections in those sources are a
# separate backlog item, counted in output/research/ps_lettered_affected.json).
_PS_NUM = r"\d+(?:\.\d+)?(?:-[A-Z](?:\.\d+)?)?"
RX_PS_LETTERED = re.compile(r"Section\s+\d+(?:\.\d+)?-[A-Z](?:\.\d+)?\.\s")                            # source-level gate
RX_PS_HEAD_NL = re.compile(rf"(?m)(?:^[ \t\x0c]*|\)[ \t]+)Section\s+({_PS_NUM})\.\s{{1,3}}([A-Z][^.\n]{{2,140}}?(?:\n[ \t]*[A-Za-z][^.\n]{{1,140}}?)?)\.(?=\s|$)")   # 'Section 611-A. Prohibited acts.' at a line start or after an enactment note ')'
RX_PS_TOC_L = re.compile(rf"^\s*Section\s+({_PS_NUM})\.\s+([A-Z][^.]{{2,140}}?)\.\s*$", re.M)          # TOC line, lettered numbers allowed
RX_PS_TOC_LINE = re.compile(rf"(?m)^[ \t\x0c]*Section\s+{_PS_NUM}\.\s+[A-Z][^\n]*$")                      # any 'Section n.' TOC-shaped line (no '.--' on it)
RX_PS_TAIL = re.compile(rf"Section\s+({_PS_NUM})\.\s*$")                                              # heading number at the chunk tail (title in the next chunk)
RX_PS_ARTICLE = re.compile(r"(?m)^[ \t\x0c]*ARTICLE\s+([IVXLC]+(?:-[A-Z])?)\.?[ \t]*$")                 # 'ARTICLE VI-A' heading line
RX_PACS_MARK = re.compile(r"\b(\d{1,2})c(\d{1,5}(?:\.\d+)?)([shv])\b")          # Pa.C.S. inline markers 42c8371s / h / v
RX_USC_SEC = re.compile(r"^\s*§\s?(\d{1,5}[a-z]?(?:[-–]\d+[a-z]?)?)\.\s+([A-Z][^\n]{2,160})", re.M)
RX_CFR_SEC = re.compile(r"§\s?(\d{1,4}\.\d{1,5}[a-z]?)\s+([A-Z][^.\n]{2,140})\.")

NOTE_RX = re.compile(r"(?m)^\s*(Official Note|Advisory Committee(?:'s)? Notes?|Explanatory Comment|Comment\b|Notes? of (?:Advisory|Decisions)|"
                     r"Amendments?\b|HISTORICAL(?: AND STATUTORY)? NOTES?|Editorial Notes|Source\b|Cross References|Enactment\.|"
                     r"Amendment Notes?|References in Text|Effective Date|Statutory Notes|Short Title|Codification|Prior Provisions|"
                     r"Transfer of Functions|Notes of Decisions|Legislative History|Authority:|\[Reserved\])")
DEF_RX = re.compile(r"(?i)(the following words and phrases when used in this|as used in this (?:section|chapter|part|act|subchapter|title)|"
                    r"shall have the meanings?|for (?:the )?purposes of this (?:section|chapter|part|act|subchapter|title),? the term|"
                    r"the term [\"“][^\"”]{1,60}[\"”] (?:means|includes)|[\"“][A-Z][^\"”]{1,60}[\"”]\.?\s+(?:means|includes|shall mean))")
TOC_LINE_RX = re.compile(r"(?m)^\s*(?:Rule|Sec\.?|§)?\s*\d{1,5}(?:\.\d+)?\.\s+[A-Z][^\n]{2,100}\.?\s*$")
TABLE_RX = re.compile(r"(?m)^(?:[^\n|]*\|){3,}[^\n]*$")

HOLD_RX = re.compile(r"(?i)\b(?:we|the court|this court)\s+(?:therefore\s+|thus\s+|accordingly\s+|now\s+)?(?:hold|holds|held|conclude|concludes)\b|"
                     r"\bHeld[:,]|\bwe (?:therefore|accordingly|thus) (?:affirm|reverse|vacate|remand|dismiss|quash|grant|deny)\b|"
                     r"\b(?:order|judgment|decree|decision)s? (?:of the [a-z ]{3,40} )?(?:is|are) (?:hereby )?(?:affirmed|reversed|vacated|quashed)\b|"
                     r"\b(?:affirmed|reversed|vacated) in part\b|\bwe (?:find|determine|are satisfied) that\b|\bit is (?:so )?ordered\b|"
                     r"\bfor (?:the foregoing|these) reasons,? we\b|\bwe (?:agree|disagree) with\b|\bthe rule (?:is|has been) that\b|"
                     r"\bwe (?:decline|reject|adopt|overrule|disapprove)\b|\bmotion (?:is|will be) (?:granted|denied)\b")
DISPO_RX = re.compile(r"(?i)\b(?:order|judgment|decree)s?\s+(?:affirmed|reversed|vacated|quashed)|\b(?:affirmed|reversed|remanded|vacated)\.\s*$|"
                      r"\bjurisdiction relinquished\b|\bcase remanded\b|\bso ordered\b")
CAPTION_RX = re.compile(r"(?i)\b(?:appellants?|appellees?|petitioners?|respondents?)\b.{0,40}\bv\.|\bv\.\s.{0,120}\b(?:appellants?|appellees?)\b|"
                        r"\b(?:Superior|Supreme|Commonwealth|District|Circuit) Court of\b|\bArgued\b.{0,30}\b(?:Decided|Filed)\b|\bNo\.\s?\d{1,5}\s?(?:[A-Z]{2,4}\s?\d{4}|[A-Z]\.D\.)|"
                        r"\bOPINION BY\b|\bBefore:?\s+[A-Z]|\bCIVIL ACTION NO\b|\bSyllabus\b|\bcertiorari to\b|\bAttorneys? and Law Firms\b|\bWest Headnotes\b|\bCounsel\b.{0,60}\bfor\b")
PROC_RX = re.compile(r"(?i)\b(?:procedural history|factual and procedural|facts and procedural|this appeal followed|filed (?:a|an|its|their|this) (?:complaint|appeal|motion|petition|answer|preliminary objections)|"
                     r"the trial court (?:granted|denied|sustained|overruled|entered|dismissed)|appellants? (?:filed|raise|argue|contend|timely)|"
                     r"(?:our|the) (?:standard|scope) of review|we review\b|on appeal,? (?:appellant|plaintiff|defendant)|the court below|"
                     r"the case was (?:tried|removed|transferred)|a jury (?:returned|found)|post-trial motions|notice of appeal)\b")
OPINION_SPLIT_RX = re.compile(r"(?i)\b(?:J\.|Justice|Judge),?\s+(?:dissenting|concurring)|\b(?:dissenting|concurring) opinion\b|\bI (?:respectfully )?dissent\b")

RX_CHUNK_SUFFIX = re.compile(r"_(?:chunk_|c)(\d+)$")


def derive_ord(source: str, ids: list[str]) -> tuple[dict[str, int], str]:
    """Recover the chunk order of one source. -> ({chunk_id: ord}, mode) mode ∈ suffix | sha1_colon | md5_us | md5_colon | sha1_us | sorted."""
    out = {}
    if all(RX_CHUNK_SUFFIX.search(c) for c in ids):
        for c in ids: out[c] = int(RX_CHUNK_SUFFIX.search(c).group(1))
        return out, "suffix"
    idset = set(ids); n = len(ids); L = len(ids[0]) if ids else 0
    if L in (16, 32, 40) and all(len(c) == L for c in ids):
        for mode, fn in (("sha1_colon", lambda i: hashlib.sha1(f"{source}:{i}".encode()).hexdigest()),
                         ("md5_us", lambda i: hashlib.md5(f"{source}_{i}".encode()).hexdigest()),
                         ("md5_colon", lambda i: hashlib.md5(f"{source}:{i}".encode()).hexdigest()),
                         ("sha1_us", lambda i: hashlib.sha1(f"{source}_{i}".encode()).hexdigest())):
            hits = {}
            for i in range(n * 2 + 8):
                h = fn(i)[:L]
                if h in idset: hits[h] = i
                if len(hits) == n: break
            if len(hits) >= max(1, int(n * 0.9)):
                for c in ids:
                    if c not in hits: hits[c] = n * 3 + ids.index(c)      # stragglers after the recovered run
                return hits, mode
    for i, c in enumerate(sorted(ids)): out[c] = i
    return out, "sorted"


def _first_heading(kind: str, fam: str, text: str) -> tuple[list[tuple[str, str, int]], int]:
    """-> ([(unit, heading, pos)...], toc_lines) over the chunk for statute/rule/reg kinds."""
    heads = []
    if fam == "pa_cs":
        for m in RX_PACS_MARK.finditer(text):
            if m.group(3) == "s":
                tail = text[m.end():m.end() + 260]
                hm = RX_SEC.search(tail)
                heads.append((m.group(2), hm.group(2).strip() if hm and hm.group(1) == m.group(2) else "", m.start()))
        if not heads:
            for m in RX_SEC.finditer(text): heads.append((m.group(1), m.group(2).strip(), m.start()))
    elif fam == "pa_ps":
        for m in RX_PS_HEAD.finditer(text): heads.append((m.group(1), re.sub(r"\s+", " ", m.group(2)).strip(), m.start()))
    elif fam in ("plaw", "comps", "courtlistener_plaw"):                      # Public Laws / compiled acts: `SEC. 435. [42 U.S.C. 11389] TITLE`
        for m in RX_SEC_CAPS.finditer(text): heads.append((m.group(1), _ws(m.group(2)), m.start()))
    elif fam in ("uscode_govinfo", "usc_title", "pa_consolidated", "pa_cons_stat"):   # 2-column OCR dumps: `§6050R. Returns …` inline
        for m in RX_USC_SEC.finditer(text): heads.append((m.group(1).replace("–", "-"), m.group(2).strip(), m.start()))
        if not heads:
            for m in RX_USC_INLINE.finditer(text): heads.append((m.group(1).replace("–", "-"), _ws(m.group(2)), m.start()))
    elif kind == "rule" or fam in ("phila_cr", "phila_civ", "cp_trial", "edpa_lr"):
        for m in RX_RULE.finditer(text): heads.append((m.group(1), m.group(2).strip(), m.start()))
        if not heads:
            for m in re.finditer(r"(?m)^\s*(?:Local )?Rule\s+\*?(\d{1,4}(?:\.\d+){0,2})\.?\s+([A-Z][^\n]{2,120})$", text): heads.append((m.group(1), m.group(2).strip(), m.start()))
    elif fam.startswith("usc"):
        for m in RX_USC_SEC.finditer(text): heads.append((m.group(1).replace("–", "-"), m.group(2).strip(), m.start()))
        if not heads:
            for m in RX_SEC.finditer(text): heads.append((m.group(1), m.group(2).strip(), m.start()))
    elif kind == "reg":
        for m in RX_CFR_SEC.finditer(text): heads.append((m.group(1), m.group(2).strip(), m.start()))
        if not heads:
            for m in RX_SEC.finditer(text): heads.append((m.group(1), m.group(2).strip(), m.start()))
    else:
        for m in RX_SEC.finditer(text): heads.append((m.group(1), m.group(2).strip(), m.start()))
    heads = [h for h in heads if not h[1].startswith(("Source", "Official Note", "See ", "The provisions", "Explanatory", "Note", "Comment"))]
    toc_lines = len(TOC_LINE_RX.findall(text))
    return heads, toc_lines


try:
    from pipeline.cite_registry import PS_UNIT_TPL, RX_PS_TOC, RX_PS_START      # P.S. section → codified unit map (G0c), shared
except Exception:                                                                 # pragma: no cover — standalone fallback
    PS_UNIT_TPL = {"pa_73ps_utpcpl": "201-{n}", "pa_40ps_uipa": "1171.{n}", "pa_73ps_fceua": "2270.{n}"}
    RX_PS_TOC = re.compile(r"^\s*Section\s+(\d+(?:\.\d+)?)\.\s+([A-Z][^.]{2,140}?)\.\s*$", re.M)
    RX_PS_START = re.compile(r"^\s*([A-Z][^.]{2,140}?)\.--")
_ws = lambda h: re.sub(r"\s+", " ", h).strip()


def slug_unit(fam: str, source: str) -> str:
    """Unit carried by the SLUG itself (one source per rule/section): frcp_rule_12 → 12, usc_t28_s1332 → 1332, pa_rcp_1531_x → 1531,
    cfr_12_1002_regb → 1002. '' when the slug is a chapter/title container.
    Coverage families (sandbox 2026-11-10, matched on lower(source)):
      cfr-2010-title48-vol2-sec52-232-25 → 52.232-25 (title 48 ⇒ 48 CFR 52.232-25) · uscode-2020-title12-chap16-sec1829b → 1829b
      njac_t5_s70-3.2 → 5:70-3.2 · nj_stat_t54a_s4-12b → 54A:4-12b · nj_court_rule_1_10_1 → 1:10-1 · pa_cons_stat_t75_s1718 → 1718
      courtlistener_frcp_rule_54 → 54 · pa_bulletin 36-34 → 36-34 (vol-issue) · plaw / comps / usc-title / cfr vol containers → ''"""
    m = re.match(r"^(?:frcp|frap|fre)_rule_(\d+(?:_\d+)?)$", source)
    if m: return m.group(1).replace("_", ".")
    m = re.match(r"^usc_t\d+_s(.+)$", source)
    if m: return m.group(1).replace("–", "-")
    m = re.match(r"^pa_rcp_(\d{3,4})_[a-z]", source)
    if m: return m.group(1)
    m = re.match(r"^cfr_\d+_(\d+[a-z]?)_", source)
    if m: return m.group(1)
    m = re.match(r"^(?:phila_cr|edpa_lr)_(\d+(?:_\d+)*)_", source)
    if m: return m.group(1).replace("_", ".")
    low = source.lower()
    m = re.match(r"^cfr-\d{4}-title\d+-vol\d+-sec(\d+)-(\d+[a-z]?)(?:-(\d+[a-z]?))?(?:_\d+)?$", low)
    if m: return f"{m.group(1)}.{m.group(2)}" + (f"-{m.group(3)}" if m.group(3) else "")
    m = re.match(r"^uscode-(?:\d{4}|lii)-title\d+(?:-[a-z0-9]+)*-sec(\d+[a-z]?(?:-\d+[a-z]?)?)(?:_\d+)?$", low)
    if m: return m.group(1)
    m = re.match(r"^njac_t(\d+)([a-z]?)_s(.+)$", low)
    if m: return f"{m.group(1)}{m.group(2).upper()}:{m.group(3)}"
    m = re.match(r"^nj_stat_t(\d+)([a-z]?)_s(.+)$", low)
    if m: return f"{m.group(1)}{m.group(2).upper()}:{m.group(3)}"
    m = re.match(r"^nj_court_rule_(\d+)_(\d+[a-z]?)(?:_(\d+[a-z]?))?$", low)
    if m: return f"{m.group(1)}:{m.group(2)}" + (f"-{m.group(3)}" if m.group(3) else "")
    m = re.match(r"^pa_cons_stat_t\d+_s(\d+)(?:_(\d+))?$", low)
    if m: return m.group(1) + (f".{m.group(2)}" if m.group(2) else "")
    m = re.match(r"^courtlistener_(?:frcp|fre|frap)_rule_(\d+(?:_\d+)?)$", low)
    if m: return m.group(1).replace("_", ".")
    m = re.match(r"^(\d{2})[-_](\d{1,2})(?:_p\d|_[a-z][a-z\-]*)?$", low)
    if m and fam == "pa_bulletin": return f"{m.group(1)}-{m.group(2)}"
    return ""


# ---------------------------------------------------------------------------------------------------------------------
# Coverage kinds (sandbox 2026-11-10): treatise (Am Jur 2d / CJS), dictionary (Black's / Bouvier), document (non-codified)
# ---------------------------------------------------------------------------------------------------------------------
RX_TREATISE_SEC = re.compile(r"(?m)^\s*§\s*(\d{1,4}[A-Za-z]?(?:\.\d+)?)\b\.?\s*([A-Z][^\n]{2,140})?")
RX_TREATISE_CIT = re.compile(r"§\s*(\d{1,4}[A-Za-z]?(?:\.\d+)?)\b")
RX_SEC_CAPS = re.compile(r"(?m)(?:^|[\s“\"‘'`])SEC(?:TION)?\.\s*(\d{1,5}[A-Za-z]?)\.\s+(?:\[[^\]]{1,60}\]\s*)?([A-Z][^\n.]{2,120})")
RX_USC_INLINE = re.compile(r"§\s?(\d{1,5}[A-Za-z]?(?:[-–]\d+[a-z]?)?)\.\s+([A-Z][^\n.]{2,120})")
RX_HEADWORD = re.compile(r"(?m)^([A-Z][A-Z'\-]{1,30}(?: [A-Z][A-Z'\-]+){0,6})\.\s")
RX_BOUVIER_ID = re.compile(r"^bouvier_([A-Z][A-Z'\-]*(?:_[A-Z][A-Z'\-]*)*?)_\d")


def _doc_role(text: str) -> tuple[str, float]:
    lines = [l for l in text.splitlines() if l.strip()]
    avg_len = (sum(len(l) for l in lines) / len(lines)) if lines else 0
    if len(TOC_LINE_RX.findall(text)) >= 6 and avg_len < 70: return "toc", 0.8
    if len(TABLE_RX.findall(text)) >= 4: return "table", 0.7
    return "text", 0.5


def classify_treatise(text: str, citations: str | None, carry_unit: str) -> tuple[str, str, str, str, float]:
    """Am Jur 2d / CJS: unit = `§ n` of the chunk (per-chunk `citations` column when the ingest kept it, else the first
    line-start `§ n` heading in the text), carried forward across continuation chunks. -> (unit, units, role, heading, score)."""
    heads = [(m.group(1), _ws(m.group(2)) if m.group(2) else "", m.start()) for m in RX_TREATISE_SEC.finditer(text)]
    units = [u for u, _h, _p in heads]
    cit_unit = ""
    if citations:
        mc = RX_TREATISE_CIT.search(citations)
        if mc: cit_unit = mc.group(1)
    lines = [l for l in text.splitlines() if l.strip()]
    avg_len = (sum(len(l) for l in lines) / len(lines)) if lines else 0
    if len(heads) >= 6 and avg_len < 80 and not cit_unit:
        return (heads[0][0] if heads else carry_unit), ",".join(dict.fromkeys(units)), "toc", "", 0.8
    if heads and len(text.strip()) < 120:                                       # heading-only stub (TOC line split into its own chunk)
        return (cit_unit or heads[0][0]), ",".join(dict.fromkeys(([cit_unit] if cit_unit else []) + units)), "toc", heads[0][1], 0.7
    if cit_unit:
        order = [cit_unit] + [u for u in units if u != cit_unit]
        heading = next((h for u, h, _p in heads if u == cit_unit), "")
        return cit_unit, ",".join(dict.fromkeys(order)), "text", heading, 1.0 if heading else 0.9
    if heads:
        unit, heading, hpos = heads[0]
        lead = carry_unit if (carry_unit and hpos > 150) else ""
        return (lead or unit), ",".join(dict.fromkeys(([lead] if lead else []) + units)), "text", heading, 0.9
    if not carry_unit:
        return "", "", "other", "", 0.3
    return carry_unit, "", "text", "", 0.6


def classify_dictionary(text: str, chunk_id: str, carry_unit: str) -> tuple[str, str, str, str, float]:
    """Black's / Bouvier: unit = the ALL-CAPS headword that opens an entry (line-start `HEADWORD. `), carried forward; Bouvier ids
    carry the headword (bouvier_ABANDONMENT_0_…). role = definition when the chunk opens an entry, text for continuations."""
    mid = RX_BOUVIER_ID.match(chunk_id or "")
    heads = [(m.group(1).strip(), m.start()) for m in RX_HEADWORD.finditer(text)
             if re.search(r"[A-Z]{3,}", m.group(1)) and not re.match(r"^(?:SEC|SECTION|ART|CHAPTER|PART|TITLE|NOTE|SEE|THE|AND)$", m.group(1))]
    units = [h for h, _p in heads]
    if mid:
        hw = mid.group(1).replace("_", " ")
        return hw, ",".join(dict.fromkeys([hw] + units)), "definition", hw, 1.0
    if heads:
        unit, hpos = heads[0]
        lead = carry_unit if (carry_unit and hpos > 120) else ""
        return (lead or unit), ",".join(dict.fromkeys(([lead] if lead else []) + units)), "definition", unit, 0.9
    if not carry_unit:
        return "", "", "other", "", 0.3
    return carry_unit, "", "text", "", 0.6


def classify_document(text: str, slug_u: str) -> tuple[str, str, str, str, float]:
    """Non-codified material (datasets, manuals, journals, Pa. Bulletin issues): unit = slug unit when the slug carries one, role from
    shape (toc / table / text)."""
    role, score = _doc_role(text)
    return slug_u, slug_u, role, "", score


ORPHAN_HINT_RX = [
    ("usreports", re.compile(r"\b\d{1,3}\s+U\.\s?S\.\s+\d+\b|\bSyllabus\b|\bcertiorari\b|\bJUSTICE [A-Z]+\b")),
    ("cfr", re.compile(r"\b\d{1,2}\s+C\.?F\.?R\.?\s+(?:§|part|Part)\s?\d|§\s?\d{1,4}\.\d{1,5}\b")),
    ("usc", re.compile(r"\b\d{1,2}\s+U\.\s?S\.\s?C\.\s+§?\s?\d+|\bsection \d{1,5}[a-z]? of this title\b")),
    ("public_law", re.compile(r"\bPub(?:lic)?\.?\s+L(?:aw)?\.?\s+(?:No\.\s*)?\d{2,3}[-–]\d+|\bSEC\.\s*\d{1,4}\.\s+[A-Z]|\d+\s+Stat\.\s+\d+")),
    ("case_opinion", re.compile(r"\bv\.\s+[A-Z]|\bappellants?\b|\bappellees?\b|\bpetitioners?\b|\bdistrict court\b|\bwe hold\b", re.I)),
    ("amjur", re.compile(r"\bAm\.?\s?Jur\.?\s?2d\b|\bC\.J\.S\.\b")),
]


def orphan_hint(text: str) -> str:
    """Content-only provenance hint for NULL-source chunks (no slug to key on): usreports | cfr | usc | public_law | case_opinion | amjur | none."""
    for name, rx in ORPHAN_HINT_RX:
        if rx.search(text): return name
    return "none"


def ps_context(rows: list[tuple[str, str]], source: str) -> dict:
    """P.S. acts: TOC map lower(title) -> section n (so a heading split at a chunk boundary — chunk opens with `Title.--` — still
    resolves), plus the codification template for the act. `lettered` (11-10) = the source carries lettered sections
    (Section 611-A.) and gets the extended parser in classify_law; plain-numbered TOC titles keep priority in the map."""
    toc = {}
    for _cid, t in rows:
        for m in RX_PS_TOC.finditer(t or ""): toc.setdefault(_ws(m.group(2)).lower(), m.group(1))
    lettered = any(RX_PS_LETTERED.search(t or "") for _cid, t in rows)
    if lettered:
        for _cid, t in rows:
            for m in RX_PS_TOC_L.finditer(t or ""): toc.setdefault(_ws(m.group(2)).lower(), m.group(1))
    return {"toc": toc, "tpl": PS_UNIT_TPL.get(source, "s{n}"), "lettered": lettered, "pending": ""}


def _ps_lettered_heads(text: str, heads: list[tuple[str, str, int]], ctx: dict) -> tuple[list[tuple[str, str, int]], bool]:
    """Extended P.S. heading pass for lettered sources (11-10). Adds the NEWLINE-form headings ('Section 611-A. Prohibited acts.'
    at a line start / after an enactment note), resolves a heading whose number closed the previous chunk (ctx['pending'] →
    chunk opens with 'Title.--' or 'Title.'), and reports whether the chunk is a TOC page (>= 6 TOC-shaped 'Section n.' lines,
    short lines). Units are raw act numbers here (template applied by the caller). Deterministic; order = text position."""
    out = list(heads)
    seen = {u for u, _h, _p in out}
    for m in RX_PS_HEAD_NL.finditer(text):
        u, h = m.group(1), _ws(m.group(2))
        if u in seen or h.startswith(("Section ", "Sections ", "Article ", "ARTICLE ")): continue
        seen.add(u); out.append((u, h, m.start()))
    out.sort(key=lambda h: h[2])
    pend = ctx.get("pending") or ""
    if pend:
        ms = RX_PS_START.match(text) or re.match(r"^\s*([A-Z][^.\n]{2,140}?)\.(?=\s|$)", text)
        if ms and pend not in seen:
            out.insert(0, (pend, _ws(ms.group(1)), 0)); seen.add(pend)
    lines = [l for l in text.splitlines() if l.strip()]
    avg_len = (sum(len(l) for l in lines) / len(lines)) if lines else 0
    toc_n = sum(1 for l in lines if RX_PS_TOC_LINE.match(l) and ".--" not in l)
    is_toc = toc_n >= 6 and avg_len < 70 and toc_n >= 2 * sum(1 for l in lines if ".--" in l)
    return out, is_toc


def classify_law(kind: str, fam: str, text: str, carry_unit: str, ctx: dict | None = None) -> tuple[str, str, str, str, float]:
    """-> (unit, units, role, heading, score) for a statute / rule / reg chunk."""
    ctx = ctx or {}
    heads, toc_lines = _first_heading(kind, fam, text)
    su = ctx.get("slug_unit", "")
    if fam == "pa_ps":
        tpl = ctx.get("tpl", "s{n}")
        ps_toc = False
        if ctx.get("lettered"):                                                 # 11-10: lettered acts — extended heading pass (gated per source)
            heads, ps_toc = _ps_lettered_heads(text, heads, ctx)
        heads = [(tpl.format(n=u), h, p) for u, h, p in heads]
        ms = RX_PS_START.match(text)
        if ms and _ws(ms.group(1)).lower() in ctx.get("toc", {}) and not (heads and heads[0][2] == 0):
            heads.insert(0, (tpl.format(n=ctx["toc"][_ws(ms.group(1)).lower()]), _ws(ms.group(1)), 0))
        if ps_toc:                                                              # TOC page of a lettered act: never a text anchor
            return (heads[0][0] if heads else carry_unit), ",".join(dict.fromkeys(u for u, _h, _p in heads)), "toc", "", 0.9
        if ctx.get("lettered") and heads and carry_unit:                        # a lettered ARTICLE heading before the first section
            am = RX_PS_ARTICLE.search(text, 0, heads[0][2])                     # heading opens a new article: nothing to carry across it
            if am and "-" in am.group(1): carry_unit = ""
    if su:                                                                      # slug-keyed source: the slug's unit owns every chunk
        own = [h for h in heads if h[0] == su]
        heads = own + [h for h in heads if h[0] != su]
        if not own: heads = [(su, "", 0)] + heads
    lead = carry_unit if (carry_unit and heads and heads[0][2] > 150 and not su) else ""
    units = ",".join(dict.fromkeys(([lead] if lead else []) + [u for u, _h, _p in heads]))
    nm = NOTE_RX.search(text); note_pos = nm.start() if nm else None
    lines = [l for l in text.splitlines() if l.strip()]
    avg_len = (sum(len(l) for l in lines) / len(lines)) if lines else 0
    if toc_lines >= 6 and (not heads or (toc_lines >= len(heads) * 2 and fam != "pa_cs")) and avg_len < 70:
        return (heads[0][0] if heads else carry_unit), units, "toc", "", 0.9
    if len(TABLE_RX.findall(text)) >= 4 and fam != "pa_cs":
        return (heads[0][0] if heads else carry_unit), units, "table", "", 0.7
    if heads:
        unit, heading, hpos = heads[0]
        role = "text"
        if note_pos is not None and note_pos < hpos and hpos > 400:
            role = "text"                                                  # heading inside the chunk still makes it the text anchor
        if heading.lower().startswith(("definition", "definitions", "words and phrases")):
            role = "definition"                                            # heading chunk: only the heading decides (body idiom is noise here)
        return unit, units, role, heading, 1.0
    # continuation chunk (no heading): note if a note marker opens it, definition if the definition idiom dominates, else text
    if note_pos is not None and note_pos < 300:
        return carry_unit, units, "note", "", 0.8
    if len(DEF_RX.findall(text)) >= 2:
        return carry_unit, units, "definition", "", 0.8
    if not carry_unit:
        return "", units, "other", "", 0.3
    return carry_unit, units, "text", "", 0.6


def classify_case(text: str, ord_i: int, n: int, part: str | None, in_dissent: bool) -> tuple[str, str, float, bool]:
    """-> (role, heading, score, in_dissent) for a case chunk. Dissent/concurrence chunks are 'other' (never a holding)."""
    head = text[:500]
    strict = re.compile(r"(?m)^\s*(?:[A-Z][A-Za-z.' ]{2,40},\s+)?(?:J\.|JJ\.|C\.\s?J\.|Justice [A-Z][a-z]+|JUSTICE [A-Z]+|Judge [A-Z][a-z]+),?\s+(?:dissenting|concurring)")
    if part in ("dissent", "concurrence"):
        return "other", "", 0.9, in_dissent                                 # chunk_part is authoritative per chunk; never sticky
    if strict.search(text[:300]) or re.search(r"(?im)^\s*(?:dissenting|concurring) opinion\b", text[:300]):
        return "other", "", 0.8, True                                        # formal separate-opinion header → sticky to end of source
    if in_dissent:
        return "other", "", 0.7, True
    if part == "syllabus" or re.search(r"(?m)^\s*Syllabus\s*$", head):
        return "syllabus", "", 0.9, in_dissent
    if part == "headnote":
        return "caption", "", 0.7, in_dissent
    holds = list(HOLD_RX.finditer(text))
    disp = DISPO_RX.search(text[-700:]) if n and ord_i >= max(1, int(n * 0.6)) else None
    cap = len(CAPTION_RX.findall(head))
    if holds:
        m = holds[0]; s = text.rfind(".", 0, m.start()); s = 0 if s < 0 else s + 1
        snippet = re.sub(r"\s+", " ", text[s:m.start() + 220]).strip()[:200]
        return "holding", snippet, min(1.0, 0.6 + 0.1 * len(holds)), in_dissent
    if disp:
        return "holding", re.sub(r"\s+", " ", text[-220:]).strip()[:200], 0.6, in_dissent
    if ord_i <= 1 and cap >= 1 or cap >= 3:
        return "caption", "", 0.8, in_dissent
    if PROC_RX.search(text) and len(PROC_RX.findall(text)) >= 2:
        return "procedural", "", 0.6, in_dissent
    return "text", "", 0.5, in_dissent


# ---------------------------------------------------------------------------------------------------------------------
def open_struct(path: Path = STRUCT_DB) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), timeout=120)
    con.execute("PRAGMA journal_mode=WAL"); con.execute("PRAGMA synchronous=NORMAL")
    con.execute("""CREATE TABLE IF NOT EXISTS chunk_struct(chunk_id TEXT PRIMARY KEY, source TEXT, family TEXT, title TEXT, unit TEXT, units TEXT,
                   role TEXT, heading TEXT, ord INTEGER, ord_mode TEXT, score REAL)""")
    con.execute("CREATE INDEX IF NOT EXISTS ix_cs_source ON chunk_struct(source)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_cs_fam_unit ON chunk_struct(family, title, unit)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_cs_role ON chunk_struct(source, role)")
    con.execute("CREATE TABLE IF NOT EXISTS build_state(family TEXT PRIMARY KEY, n_sources INTEGER, n_chunks INTEGER, started TEXT, done_at TEXT, secs REAL, last_source TEXT)")
    return con


def _part_map(pcon, source):
    if pcon is None: return {}
    return dict(pcon.execute("SELECT chunk_id, part FROM chunk_part WHERE source=?", (source,)).fetchall())


def index_source(source: str, rows: list[tuple], fam: str, title: str, pcon=None) -> list[tuple]:
    """rows = [(chunk_id, text[, citations])] for ONE source -> chunk_struct rows. Kinds: statute/rule/reg (classify_law),
    case (classify_case), treatise / dictionary / document (coverage kinds, sandbox 2026-11-10)."""
    kind = kind_of(fam)
    ids = [r[0] for r in rows]
    ordm, mode = derive_ord(source, ids) if not is_orphan_source(source) else ({c: i for i, c in enumerate(sorted(ids))}, "sorted")
    order = sorted(rows, key=lambda r: ordm[r[0]])
    out = []
    if kind == "case":
        parts = _part_map(pcon, source) if pcon is not None else {}
        in_d = False; n = len(order)
        sticky = fam not in VOLUME_CASE_FAMILIES and mode != "sorted"          # a bound volume holds many opinions: a dissent header never sticks to the end
        for r in order:
            cid, t = r[0], r[1]
            role, heading, score, in_d = classify_case(t or "", ordm[cid], n, parts.get(cid), in_d if sticky else False)
            out.append((cid, source, fam, title, "", "", role, heading, ordm[cid], mode, score))
    elif kind == "treatise":
        carry = ""
        for r in order:
            cid, t = r[0], r[1]; cit = r[2] if len(r) > 2 else None
            unit, units, role, heading, score = classify_treatise(t or "", cit, carry if mode != "sorted" else "")
            if role == "text" and unit: carry = (units.split(",")[-1] if units else unit)
            out.append((cid, source, fam, title, unit, units, role, heading, ordm[cid], mode, score))
    elif kind == "dictionary":
        carry = ""
        for r in order:
            cid, t = r[0], r[1]
            unit, units, role, heading, score = classify_dictionary(t or "", cid, carry if mode != "sorted" else "")
            if role in ("definition", "text") and unit: carry = (units.split(",")[-1] if units else unit)
            out.append((cid, source, fam, title, unit, units, role, heading, ordm[cid], mode, score))
    elif kind == "document":
        su = slug_unit(fam, source) if not is_orphan_source(source) else ""
        for r in order:
            cid, t = r[0], r[1]
            if fam == ORPHAN:
                out.append((cid, source, fam, "", "", "", "other", orphan_hint(t or ""), ordm[cid], mode, 0.2))
            elif fam == "unclassified":
                out.append((cid, source, fam, "", "", "", "other", "", ordm[cid], mode, 0.2))
            else:
                unit, units, role, heading, score = classify_document(t or "", su)
                if fam == "pa_bulletin":                                        # section file tag (not / rr / cts / prm / dgs / sop / ga / gv / p2)
                    mt = re.match(r"^\d{2}[-_]\d{1,2}_([a-z][a-z0-9\-]*)$", source.lower()); heading = mt.group(1) if mt else ""
                out.append((cid, source, fam, title, unit, units, role, heading, ordm[cid], mode, score))
    else:
        carry = ""
        ctx = {"slug_unit": slug_unit(fam, source)}
        if fam == "pa_ps": ctx.update(ps_context([(r[0], r[1]) for r in rows], source))
        for r in order:
            cid, t = r[0], r[1]
            unit, units, role, heading, score = classify_law(kind, fam, t or "", carry if mode != "sorted" else "", ctx)
            if role in ("text", "definition") and unit: carry = (units.split(",")[-1] if units else unit)
            out.append((cid, source, fam, title, unit, units, role, heading, ordm[cid], mode, score))
            if fam == "pa_ps" and ctx.get("lettered"):                          # 11-10: a heading number closing this chunk titles the next one
                mt = RX_PS_TAIL.search((t or "")[-80:]) if mode != "sorted" else None
                ctx["pending"] = mt.group(1) if mt else ""
    return out


def index_slug(source: str, struct_db: Path | None = None, chunk_db: Path | None = None) -> dict:
    """Ingest-lane entry point (one slug, idempotent): read the slug's chunks from chunk_text, classify via index_source, upsert into
    chunk_struct. Call sites (flag-gated GRAPH_EMIT=1, BEFORE graph_emit so a case hub sees roles): official_source_puller.ingest_source,
    case_fetch.ingest_case, corpus_gap_bot.fill_gap. Writes STRUCT_DB (env override) only; never deletes."""
    t0 = time.time()
    cc = sqlite3.connect(f"file:{chunk_db or CHUNK_DB}?mode=ro", uri=True); cc.execute("PRAGMA busy_timeout=120000")
    rows = cc.execute("SELECT chunk_id, text, citations FROM chunk_text WHERE source=?", (source,)).fetchall(); cc.close()
    if not rows:
        return {"source": source, "family": None, "chunks": 0, "secs": round(time.time() - t0, 1), "detail": "no chunk_text rows"}
    cs = open_struct(struct_db or STRUCT_DB)
    if not _OVERRIDES_LOADED: load_overrides(cs)
    fam, title = resolve_family(source)                                        # family_of first; override only fills 'unclassified'
    pcon = sqlite3.connect(f"file:{PART_DB}?mode=ro", uri=True) if PART_DB.exists() else None
    out = index_source(source, rows, fam, title, pcon)
    if pcon is not None: pcon.close()
    cs.executemany("INSERT OR REPLACE INTO chunk_struct VALUES(?,?,?,?,?,?,?,?,?,?,?)", out); cs.commit(); cs.close()
    return {"source": source, "family": fam, "kind": kind_of(fam), "chunks": len(out), "secs": round(time.time() - t0, 1)}


_CENSUS: list[tuple] | None = None


def source_census(con_chunk) -> list[tuple]:
    """[(source, n_chunks)] for the whole corpus — one index-only GROUP BY on ix_ct_source (~0.3 s), cached per process."""
    global _CENSUS
    if _CENSUS is None:
        _CENSUS = con_chunk.execute("SELECT source, count(*) FROM chunk_text GROUP BY source").fetchall()
    return _CENSUS


def _orphan_rows(con_chunk, source, batch=20000, start=""):
    """Keyset-paginated rows for the NULL / 'unknown' source (229K / 17.5K chunks) so one family never holds the whole set in RAM.
    start = last chunk_id already written (resume after a timeout)."""
    last = start or ""
    while True:
        if source is None:
            rows = con_chunk.execute("SELECT chunk_id, text FROM chunk_text WHERE source IS NULL AND chunk_id > ? ORDER BY chunk_id LIMIT ?", (last, batch)).fetchall()
        else:
            rows = con_chunk.execute("SELECT chunk_id, text FROM chunk_text WHERE source = ? AND chunk_id > ? ORDER BY chunk_id LIMIT ?", (source, last, batch)).fetchall()
        if not rows: return
        yield rows
        last = rows[-1][0]


def _index_orphan_batch(src, rows: list[tuple], fam: str, pcon=None) -> list[tuple]:
    """One orphan page: chunks with a source_chunk_override row are emitted under their RECOVERED source (family by
    resolve_family, ord from the override when known, ord_mode='recovered'); the rest stay family 'orphan'."""
    plain = [r for r in rows if r[0] not in _CHUNK_OVERRIDES]
    out = index_source(src, plain, fam, "", pcon) if plain else []
    by_src: dict[str, list[tuple]] = {}
    for r in rows:
        ov = _CHUNK_OVERRIDES.get(r[0])
        if ov is not None: by_src.setdefault(ov[0], []).append(r)
    for rsrc, rrows in by_src.items():
        rfam, rtitle = resolve_family(rsrc)
        for t in index_source(rsrc, rrows, rfam, rtitle, pcon):
            o = _CHUNK_OVERRIDES[t[0]][1]
            out.append(t[:8] + (o if o is not None else t[8], "recovered", t[10]))
    return out


def build_family(fam: str, con_struct, con_chunk, pcon=None, verbose=True, deadline: float | None = None, force=False, only_missing=False) -> dict:
    """Per-source build. Sources come from the full source census filtered by family_of (case-insensitive; no prefix range scan), fetched
    one source at a time. only_missing=True (sandbox coverage build) indexes only sources with no chunk_struct row yet and ignores
    build_state.done_at, so a family already 'done' under a narrower matcher can be topped up without rebuilding it."""
    st = con_struct.execute("SELECT done_at, last_source FROM build_state WHERE family=?", (fam,)).fetchone()
    if st and st[0] and not force and not only_missing:
        return {"family": fam, "skipped": "done", "done_at": st[0]}
    if not _OVERRIDES_LOADED: load_overrides(con_struct)
    last = st[1] if (st and not force and not only_missing) else None
    t0 = time.time(); started = time.strftime("%Y-%m-%d %H:%M:%S")
    if fam == ORPHAN:
        srcs = [s for s, _n in source_census(con_chunk) if is_orphan_source(s)]
    else:
        srcs = sorted(s for s, _n in source_census(con_chunk) if not is_orphan_source(s) and resolve_family(s)[0] == fam and (not last or s > last))
    if only_missing:
        have = set(s for (s,) in con_struct.execute("SELECT DISTINCT source FROM chunk_struct"))   # any family: never re-index a covered source
        srcs = [s for s in srcs if s not in have or is_orphan_source(s)]           # orphan sources resume per chunk_id (see _orphan_rows)
    prev = con_struct.execute("SELECT n_sources, n_chunks FROM build_state WHERE family=?", (fam,)).fetchone() if (last or only_missing) else None
    base_s = prev[0] if prev else 0; base_c = prev[1] if prev else 0
    n_src = n_ch = 0; stopped = None; cur_src = last
    for src in srcs:
        if deadline and time.time() > deadline:
            stopped = src; break
        if is_orphan_source(src):
            done_to = con_struct.execute("SELECT max(chunk_id) FROM chunk_struct WHERE family=? AND source IS ?", (fam, src)).fetchone()[0] or ""
            for rows in _orphan_rows(con_chunk, src, start=done_to):
                if deadline and time.time() > deadline:
                    stopped = src or "<NULL>"; break
                out = _index_orphan_batch(src, rows, fam, pcon)
                con_struct.executemany("INSERT OR REPLACE INTO chunk_struct VALUES(?,?,?,?,?,?,?,?,?,?,?)", out)
                n_ch += len(out); con_struct.commit()
                if verbose: print(f"  [{fam}] source={src!r} {n_ch} chunks, {round(time.time() - t0)}s", flush=True)
            if stopped: break
            n_src += 1; cur_src = src or "<NULL>"
            continue
        rows = con_chunk.execute("SELECT chunk_id, text, citations FROM chunk_text WHERE source=?", (src,)).fetchall()
        if not rows: continue
        out = index_source(src, rows, fam, resolve_family(src)[1], pcon)
        con_struct.executemany("INSERT OR REPLACE INTO chunk_struct VALUES(?,?,?,?,?,?,?,?,?,?,?)", out)
        n_src += 1; n_ch += len(out); cur_src = src
        if n_src % 500 == 0:
            con_struct.execute("INSERT OR REPLACE INTO build_state VALUES(?,?,?,?,?,?,?)", (fam, base_s + n_src, base_c + n_ch, started, None, round(time.time() - t0, 1), cur_src)); con_struct.commit()
            if verbose: print(f"  [{fam}] {n_src}/{len(srcs)} sources / {n_ch} chunks, {round(time.time() - t0)}s", flush=True)
    tot_s = base_s + n_src; tot_c = base_c + n_ch
    con_struct.execute("INSERT OR REPLACE INTO build_state VALUES(?,?,?,?,?,?,?)",
                       (fam, tot_s, tot_c, started, None if stopped else time.strftime("%Y-%m-%d %H:%M:%S"), round(time.time() - t0, 1), cur_src))
    con_struct.commit()
    rep = {"family": fam, "sources": tot_s, "chunks": tot_c, "new_sources": n_src, "new_chunks": n_ch, "secs": round(time.time() - t0, 1), "stopped_at": stopped}
    if verbose: print(json.dumps(rep), flush=True)
    return rep


def report(con_struct=None, census: dict | None = None) -> dict:
    con_struct = con_struct or open_struct()
    out = {"db": str(STRUCT_DB), "families": {}}
    roles = {}
    for fam, role, n in con_struct.execute("SELECT family, role, count(*) FROM chunk_struct GROUP BY family, role"):
        roles.setdefault(fam, {})[role] = n
    units = {fam: (n, u) for fam, n, u in con_struct.execute("SELECT family, count(*), sum(unit != '') FROM chunk_struct GROUP BY family")}
    for fam, ns, nc, started, done, secs, last in con_struct.execute("SELECT * FROM build_state ORDER BY family"):
        d = {"sources": ns, "chunks": nc, "done": bool(done), "secs": secs, "roles": roles.get(fam, {})}
        if fam in units:
            d["rows"] = units[fam][0]; d["unit_rate_pct"] = round(100.0 * (units[fam][1] or 0) / units[fam][0], 1) if units[fam][0] else None
        if census:
            tot = sum(v for s, v in census.items() if resolve_family(None if s == "<NULL>" else s)[0] == fam)
            d["corpus_chunks"] = tot; d["coverage_pct"] = round(100.0 * nc / tot, 1) if tot else None
        out["families"][fam] = d
    out["rows_total"] = con_struct.execute("SELECT count(*) FROM chunk_struct").fetchone()[0]
    out["not_indexed"] = [f[0] for f in FAMILIES if f[0] not in out["families"]]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--families", nargs="*", default=["priority"], help="family names, or 'priority' / 'all' / 'coverage'")
    ap.add_argument("--minutes", type=float, default=80.0, help="wall-clock self-stop (resumable)")
    ap.add_argument("--force", action="store_true", help="rebuild families already marked done")
    ap.add_argument("--only-missing", action="store_true", help="index only sources with no chunk_struct row yet (coverage top-up)")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--census", default=str(_ehermes() / "profiles/main/cache/scratch/source_census.json"))
    ap.add_argument("--rebuild-sources", nargs="*", default=None, metavar="SOURCE",
                    help="re-index ONLY these sources in place (index_slug: INSERT OR REPLACE by chunk_id — row count unchanged); use with STRUCT_DB=<sandbox copy>")
    a = ap.parse_args()
    cs = open_struct()
    ov = load_overrides(cs)                                                     # DATA layer; empty on a DB without the tables
    print("overrides:", json.dumps(ov), flush=True)
    if a.rebuild_sources is not None:                                           # 11-10: bounded per-source rebuild (lettered-act parser fix)
        cs.close()
        for src in a.rebuild_sources:
            print(json.dumps(index_slug(src)), flush=True)
        return
    if a.report:
        cen = json.load(open(a.census)) if os.path.exists(a.census) else None
        print(json.dumps(report(cs, cen), indent=1)); return
    extra = override_families()                                                 # NEW families learned via the audit are buildable
    fams = (PRIORITY if a.families == ["priority"] else
            [f[0] for f in FAMILIES] + extra + [ORPHAN] if a.families == ["all"] else
            COVERAGE_FAMILIES[:-1] + extra + [ORPHAN] if a.families == ["coverage"] else a.families)
    cc = sqlite3.connect(f"file:{CHUNK_DB}?mode=ro", uri=True); cc.execute("PRAGMA busy_timeout=120000")
    pcon = sqlite3.connect(f"file:{PART_DB}?mode=ro", uri=True) if PART_DB.exists() else None
    deadline = time.time() + a.minutes * 60
    for fam in fams:
        if time.time() > deadline: print(f"deadline reached before {fam}"); break
        build_family(fam, cs, cc, pcon, deadline=deadline, force=a.force, only_missing=a.only_missing)
    print(json.dumps(report(cs), indent=1))


if __name__ == "__main__":
    main()
