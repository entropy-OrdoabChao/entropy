#!/usr/bin/env python3
"""
Bundle 20 - cite_verifier
For every cite in every redrafted motion, verify against the indexed DB:
  1. Does the exact cite appear in the corpus?  → VERIFIED / UNVERIFIED
  2. If verified, does the corpus context match the motion's use?
     → in particular: federal cite in state case = FORUM_MISMATCH;
       criminal statute in civil filing = CIVIL_FORUM_MISMATCH

Catches LLM-fabricated cites and federal-template-bleeding-into-state-case
problems.

Outputs:
  <home>/obsidian_sync/cases/<case>/_cite_verification.md
  <home>/obsidian_sync/_cite_verification_master.md

Run:
  python3 cite_verifier.py
  python3 cite_verifier.py --case fed:2:26-cv-00789
"""
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import sys
import os
import re
import importlib
import argparse
from pathlib import Path
from collections import defaultdict
from datetime import datetime

REPO = _eroot()
CASES_ROOT = REPO / 'obsidian_sync' / 'cases'
PIPELINE_DIR = REPO / 'pipeline'
OUT_MASTER = REPO / 'obsidian_sync' / '_cite_verification_master.md'

ACTIVE_CASES = [
    'fed_26-cv-00123',
    'fed_26-cv-00456',
    'fed_26-cv-00789',
    'phila_ccp_260100000',
]

# Case forum mapping
FEDERAL_CASES = {'fed_26-cv-00123', 'fed_26-cv-00456', 'fed_26-cv-00789'}
STATE_CASES = {'phila_ccp_260100000'}

# Federal-only statutes - cite these in state court = forum mismatch
FEDERAL_ONLY_STATUTES = [
    (r'\b5\s+U\.S\.C\.', 'APA / federal admin law - federal agency only'),
    (r'\b28\s+U\.S\.C\.\s+§+\s*1361\b',
     'Federal mandamus statute - federal officers only'),
    (r'\b28\s+U\.S\.C\.\s+§+\s*1331\b',
     'Federal question jurisdiction - federal court only'),
    (r'\b28\s+U\.S\.C\.\s+§+\s*1332\b',
     'Diversity jurisdiction - federal court only'),
    (r'\b28\s+U\.S\.C\.\s+§+\s*1441\b',
     'Removal statute - federal court only'),
    (r'\b28\s+U\.S\.C\.\s+§+\s*1447\b',
     'Federal remand procedure'),
    (r'\b28\s+U\.S\.C\.\s+§+\s*351\b',
     'Federal Judicial Council complaint - federal judges only'),
    (r'\bFed\.\s*R\.\s*Civ\.\s*P\.', 'Federal Rules of Civil Procedure'),
    (r'\bFed\.\s*R\.\s*Evid\.', 'Federal Rules of Evidence'),
]

# Criminal statutes - private plaintiffs cannot invoke
CRIMINAL_STATUTES_PRIVATE_INVALID = [
    (r'\b18\s+U\.S\.C\.\s+§+\s*242\b',
     'Federal criminal civil-rights statute - private plaintiff cannot prosecute'),
    (r'\b18\s+U\.S\.C\.\s+§+\s*241\b',
     'Federal criminal conspiracy - private plaintiff cannot prosecute'),
    (r'\b18\s+U\.S\.C\.\s+§+\s*1001\b',
     'Federal false statements criminal statute'),
    (r'\b18\s+U\.S\.C\.\s+§+\s*1505\b',
     'Federal obstruction criminal statute'),
    (r'\b18\s+U\.S\.C\.\s+§+\s*1512\b',
     'Federal witness tampering criminal statute'),
]

# Cites that need procedural context (not jurisdictional)
PROCEDURALLY_MISPLACED = [
    (r'\b42\s+U\.S\.C\.\s+§+\s*1983\b',
     'Private right of action - not a jurisdictional ground in remand motion'),
]


# Citation extraction (same as filings_review)
def extract_citations(text):
    cites = set()
    for m in re.finditer(
            r'\b\d{1,4}\s+(?:U\.S\.|F\.\s?[2-4]?d|F\.\sSupp\.\s?[2-3]?d|S\.\s?Ct\.|Pa\.|A\.\s?[2-3]?d)\s+\d+',
            text):
        cites.add(re.sub(r'\s+', ' ', m.group(0)).strip())
    for m in re.finditer(
            r'\b\d{1,2}\s+U\.S\.C\.\s+§+\s*\d+[A-Za-z0-9\-\.]*', text):
        cites.add(re.sub(r'\s+', ' ', m.group(0)).strip())
    for m in re.finditer(
            r'\b\d{1,2}\s+C\.F\.R\.\s+§+\s*\d+[A-Za-z0-9\-\.]*', text):
        cites.add(re.sub(r'\s+', ' ', m.group(0)).strip())
    for m in re.finditer(
            r'\b\d{1,2}\s+Pa\.C\.S\.?\s+§+\s*\d+[A-Za-z0-9\-\.]*', text):
        cites.add(re.sub(r'\s+', ' ', m.group(0)).strip())
    for m in re.finditer(
            r'\b(?:Fed\.?\s*R\.\s*(?:Civ|Crim|Evid|App)\.\s*P?\.?\s*\d+(?:\([a-z0-9]+\))?)',
            text):
        cites.add(re.sub(r'\s+', ' ', m.group(0)).strip())
    for m in re.finditer(
            r'\bPa\.\s*R\.\s*(?:Civ|Crim|Evid|App)\.\s*P?\.?\s*\d+(?:\([a-z0-9]+\))?',
            text):
        cites.add(re.sub(r'\s+', ' ', m.group(0)).strip())
    return cites


def _find_legaldb():
    for p in (str(REPO), str(PIPELINE_DIR)):
        if p not in sys.path:
            sys.path.insert(0, p)
    for name in [
        'pipeline.legal_db_client', 'legal_db_client',
        'pipeline.legal_db', 'legal_db',
        'pipeline.beezle_db', 'beezle_db',
        'pipeline.chromadb_client', 'chromadb_client',
    ]:
        try:
            mod = importlib.import_module(name)
            if hasattr(mod, 'LegalDB'):
                return mod.LegalDB
        except Exception:
            continue
    if PIPELINE_DIR.exists():
        for py_file in sorted(PIPELINE_DIR.glob('*.py')):
            try:
                content = py_file.read_text(encoding='utf-8',
                                            errors='ignore')
            except OSError:
                continue
            if re.search(r'^class\s+LegalDB\b', content, re.MULTILINE):
                stem = py_file.stem
                for prefix in ('pipeline.', ''):
                    try:
                        mod = importlib.import_module(prefix + stem)
                        if hasattr(mod, 'LegalDB'):
                            return mod.LegalDB
                    except Exception:
                        continue
    raise ImportError('LegalDB class not found')


def _normalize(result):
    docs, metas = [], []
    # LegalDB.query_text returns a LIST OF DICTS ({'id','text','source'/'metadata'}).
    # The dict/Chroma shape below is kept for other callers, but the list shape is the
    # live one -- and because it was unhandled, every verification silently saw ZERO
    # documents and reported UNVERIFIED for cites that were in the corpus all along.
    if isinstance(result, list):
        for row in result:
            if isinstance(row, dict):
                docs.append(row.get('text') or row.get('document') or row.get('content') or '')
                meta = row.get('metadata') if isinstance(row.get('metadata'), dict) else {}
                meta = dict(meta)
                # PROVENANCE: row['source'] is the useless constant
                # 'chunk_db:chunk_text:fts'. The real corpus slug -- which encodes the
                # opinion's own reporter cite, e.g.
                # scotus_rancosky_v_washington_national_ins_co_170a3d364 -- lives in
                # 'doc_id'/'id'. Verification depends on it, so prefer those and fall
                # back to 'source' only when they are absent.
                slug = (row.get('doc_id') or row.get('id') or '')
                if not str(slug).strip() or str(slug).startswith('chunk_db:'):
                    slug = meta.get('source') or row.get('source') or ''
                meta['source'] = slug
                # keep any citation list the retriever already parsed
                if row.get('citations') and not meta.get('citations'):
                    meta['citations'] = row.get('citations')
                metas.append(meta)
            else:
                docs.append(str(row))
                metas.append({})
        return docs, metas
    if isinstance(result, dict) and 'documents' in result:
        d = result.get('documents') or []
        m = result.get('metadatas') or []
        if d and isinstance(d[0], list):
            docs, metas = d[0], (m[0] if m else [])
        else:
            docs, metas = d, m
    return docs, metas


def verify_cite(cite, db, cache, n_results=10):
    """Return {verified: bool, sources: [str], n_hits: int}."""
    if cite in cache:
        return cache[cite]
    try:
        result = db.query_text(cite, n_results=n_results)
    except Exception as e:
        out = {'verified': False, 'sources': [], 'error': str(e),
               'n_hits': 0}
        cache[cite] = out
        return out

    # Normalize the cite for matching - strip extra whitespace
    cite_norm = re.sub(r'\s+', ' ', cite).strip().lower()
    # Also try without § symbol (some sources use 'sec.')
    cite_alt = cite_norm.replace('§', '').replace('sec.', '')
    cite_alt = re.sub(r'\s+', ' ', cite_alt).strip()

    docs, metas = _normalize(result)
    sources = set()
    for doc, meta in zip(docs, metas):
        doc_l = (doc or '').lower()
        if cite_norm in doc_l or cite_alt in doc_l.replace('§', '').replace('sec.', ''):
            src = (meta or {}).get('source', 'unknown')
            sources.add(src)
    out = {
        'verified': len(sources) > 0,
        'sources': sorted(sources),
        'n_hits': len(sources),
    }
    cache[cite] = out
    return out


def detect_forum_mismatch(cite, case_folder):
    """Return list of mismatch reasons for the given cite + case forum."""
    flags = []
    in_state = case_folder in STATE_CASES
    in_federal = case_folder in FEDERAL_CASES

    if in_state:
        for pat, reason in FEDERAL_ONLY_STATUTES:
            if re.search(pat, cite):
                flags.append(('FEDERAL_IN_STATE', reason))

    # Criminal statutes - private plaintiffs in civil case
    for pat, reason in CRIMINAL_STATUTES_PRIVATE_INVALID:
        if re.search(pat, cite):
            flags.append(('CRIMINAL_BY_PRIVATE', reason))

    # Procedurally misplaced - check based on motion type later
    for pat, reason in PROCEDURALLY_MISPLACED:
        if re.search(pat, cite):
            flags.append(('PROCEDURAL_NOTE', reason))

    return flags


def render_case_report(case_name, per_motion_cites, cite_verification,
                        cite_flags):
    L = []
    L.append(f'# Cite Verification — {case_name}')
    L.append('')
    L.append(f'*Generated: {datetime.now().strftime("%Y-%m-%d %H:%M")}*')
    L.append('')

    all_cites = set()
    for cites in per_motion_cites.values():
        all_cites.update(cites)

    verified = [c for c in all_cites if cite_verification[c]['verified']]
    unverified = [c for c in all_cites
                  if not cite_verification[c]['verified']]
    flagged_cites = [c for c in all_cites if cite_flags.get(c)]

    L.append('## Summary')
    L.append('')
    L.append(f'- **Motions checked:** {len(per_motion_cites)}')
    L.append(f'- **Distinct cites:** {len(all_cites)}')
    L.append(f'- **Verified (found in DB):** {len(verified)}')
    L.append(f'- **Unverified (not in DB — likely fabricated):** '
             f'{len(unverified)}')
    L.append(f'- **Forum/context flagged:** {len(flagged_cites)}')
    L.append('')

    if unverified:
        L.append('## ⚠ UNVERIFIED cites — not in any indexed source')
        L.append('')
        L.append('These cites returned zero verbatim matches in the DB. '
                 'They may be hallucinated by the LLM. Verify each by hand '
                 'before filing.')
        L.append('')
        L.append('| Cite | In motions |')
        L.append('|---|---|')
        cite_to_motions = defaultdict(list)
        for mot, cites in per_motion_cites.items():
            for c in cites:
                cite_to_motions[c].append(mot)
        for c in sorted(unverified):
            ms = cite_to_motions[c]
            ms_str = ', '.join(f'`{m}`' for m in ms[:3])
            if len(ms) > 3:
                ms_str += f' (+{len(ms) - 3} more)'
            L.append(f'| `{c}` | {ms_str} |')
        L.append('')

    if flagged_cites:
        L.append('## ⚠ FORUM / CONTEXT MISMATCH')
        L.append('')
        L.append('These cites exist in the corpus but the way they are '
                 'used in this case may be procedurally wrong.')
        L.append('')
        for c in sorted(flagged_cites):
            L.append(f'### `{c}`')
            for kind, reason in cite_flags[c]:
                L.append(f'- **{kind}:** {reason}')
            ms = [m for m, cs in per_motion_cites.items() if c in cs]
            L.append(f'- **Appears in:** {", ".join(f"`{m}`" for m in ms)}')
            L.append('')

    L.append('## Verified cites')
    L.append('')
    L.append('| Cite | Sources | Motions |')
    L.append('|---|---|---:|')
    cite_to_motions = defaultdict(list)
    for mot, cites in per_motion_cites.items():
        for c in cites:
            cite_to_motions[c].append(mot)
    for c in sorted(verified):
        srcs = cite_verification[c]['sources'][:3]
        srcs_str = ', '.join(f'`{s}`' for s in srcs)
        if len(cite_verification[c]['sources']) > 3:
            srcs_str += f' (+{len(cite_verification[c]["sources"]) - 3})'
        L.append(f'| `{c}` | {srcs_str} | {len(cite_to_motions[c])} |')
    L.append('')

    L.append('## Next steps')
    L.append('')
    L.append('1. For every UNVERIFIED cite: verify the cite exists '
             '(Westlaw / Lexis / loc.gov) and delete or replace if it does '
             'not.')
    L.append('2. For every FORUM/CONTEXT flag: confirm the cite applies in '
             'this forum and procedural context, or replace with the '
             'correct state/civil analog.')
    L.append('3. After fixes, re-run `md_to_docx.py` to regenerate Word '
             'documents.')
    L.append('')
    return '\n'.join(L) + '\n'


def case_id_to_folder(case_id):
    parts = case_id.split(':')
    if len(parts) == 3:
        ct, _district, num = parts
        return f'{ct}_{num}'
    elif len(parts) == 2:
        return f'{parts[0]}_{parts[1]}'
    return case_id.replace(':', '_')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--case', help='one case ID')
    ap.add_argument('--path', nargs='+',
                    help='files/dirs to scan for WRONG_CITE (vault notes, SOULs, drafts)')
    ap.add_argument('--text', help='verify citations in a literal string')
    ap.add_argument('--glob', default='*.md')
    args = ap.parse_args()

    # WRONG_CITE gate mode: agnostic, any path, exit 1 when a wrong cite is found.
    if args.text:
        rep = scan_text(args.text)
        for x in rep['wrong']:
            print(f"  WRONG_CITE    {x['case'][:44]}: cited {x['cite']} "
                  f"-> corpus has {x['found_cite']}")
        for x in rep['gaps']:
            print(f"  NOT_IN_CORPUS {x['case'][:44]}: {x['cite']}")
        for x in rep['verified']:
            print(f"  ok            {x['case'][:44]}: {x['cite']}")
        sys.exit(1 if rep['wrong'] else (2 if rep['gaps'] else 0))
    if args.path:
        sys.exit(gate_scan(args.path, glob=args.glob))

    if args.case:
        case_folders = [case_id_to_folder(args.case)]
    else:
        case_folders = ACTIVE_CASES

    LegalDB = _find_legaldb()
    db = LegalDB()
    cite_cache = {}  # cite -> verification result (cross-case)

    master_rows = []
    for cf in case_folders:
        cd = CASES_ROOT / cf
        fr = cd / '_filing_ready'
        if not fr.exists():
            print(f'  [!] no {fr}')
            continue

        md_files = sorted(fr.glob('*.md'))
        if not md_files:
            continue

        print(f'\n== {cf} == {len(md_files)} motions')
        per_motion_cites = {}
        for mf in md_files:
            try:
                text = mf.read_text(encoding='utf-8', errors='ignore')
            except OSError:
                continue
            name = mf.stem.replace('_DRAFT', '')
            per_motion_cites[name] = extract_citations(text)

        # All cites in this case
        all_cites = set()
        for cs in per_motion_cites.values():
            all_cites.update(cs)
        print(f'  distinct cites: {len(all_cites)}')

        # Verify each cite
        cite_verification = {}
        for i, c in enumerate(sorted(all_cites), 1):
            v = verify_cite(c, db, cite_cache)
            cite_verification[c] = v
            if i % 10 == 0 or i == len(all_cites):
                n_unver = sum(1 for cc in cite_verification.values()
                              if not cc['verified'])
                print(f'    [{i:3d}/{len(all_cites)}] '
                      f'unverified so far: {n_unver}')

        # Flag forum/context mismatches
        cite_flags = {}
        for c in all_cites:
            flags = detect_forum_mismatch(c, cf)
            if flags:
                cite_flags[c] = flags

        # Write per-case report
        out = cd / '_cite_verification.md'
        out.write_text(
            render_case_report(cf, per_motion_cites, cite_verification,
                                cite_flags),
            encoding='utf-8')
        print(f'  [OK] {out}')
        n_unver = sum(1 for v in cite_verification.values()
                       if not v['verified'])
        print(f'    unverified: {n_unver}/{len(all_cites)}; '
              f'forum-flagged: {len(cite_flags)}')

        master_rows.append({
            'case': cf,
            'motions': len(md_files),
            'cites': len(all_cites),
            'unverified': n_unver,
            'flagged': len(cite_flags),
        })

    # Master
    L = ['# Cite Verification — Master', '',
         f'*Generated: {datetime.now().strftime("%Y-%m-%d %H:%M")}*', '']
    L.append('| Case | Motions | Cites | Unverified | Forum-flagged |')
    L.append('|---|---:|---:|---:|---:|')
    for r in master_rows:
        L.append(f'| `{r["case"]}` | {r["motions"]} | {r["cites"]} | '
                 f'**{r["unverified"]}** | **{r["flagged"]}** |')
    L.append('')
    L.append('Per-case detail at `cases/<case>/_cite_verification.md`')
    OUT_MASTER.write_text('\n'.join(L) + '\n', encoding='utf-8')
    print(f'\n[OK] master: {OUT_MASTER}')



# ===========================================================================
# WRONG_CITE detection + agnostic scanning  (added after the Rancosky defect)
# ===========================================================================
# Rancosky v. Washington National Ins. Co. was carried through the vault as
# "190 A.3d 854 (Pa. 2018)". The corpus has the opinion at "170 A.3d 364 (Pa. 2017)".
# The old path would have called that merely UNVERIFIED. It is not: it is a wrong cite
# for a case the corpus HAS, which is both provable and auto-correctable.

CASE_CITE_RE = re.compile(
    # Leading party may carry internal abbreviation dots ("CoreStates Bank, N.A."),
    # so the name must be allowed to start at a capitalized token and run through
    # comma-separated abbreviation segments. Breaking at the '.' in "N.A." produced
    # a TRUNCATED party name ("N.A. v. Cutillo") which then ingested the same
    # opinion under a SECOND source key -- duplicate chunks dilute retrieval.
    r"(?P<name>(?:[A-Z][\w'\u2019.&-]*[\s,]+)*[A-Z][\w'\u2019.&-]*"
    r"\s+v\.?\s+"
    # Party names carry '&', '.', and lowercase connectors: "Prudential Prop. & Cas.
    # Ins. Co.". An uppercase-only token class dropped Terletsky entirely, so a real
    # citation was never classified -- silence, not a finding. Allow those tokens.
    r"(?:[A-Z][\w'\u2019.&-]*|&|of|the|and)[\s,]*"
    r"(?:(?:[A-Z][\w'\u2019.&-]*|&|of|the|and)[\s,]*){0,9}?)"
    r",?\s*(?P<vol>\d{1,3})\s+"
    r"(?P<rep>A\.\s?[23]d|A\.|U\.S\.|S\.\s?Ct\.|F\.\s?[234]d|F\.\s?Supp\.(?:\s?[23]d)?|"
    r"Pa\.(?:\s?Super\.|\s?Cmwlth\.)?|L\.\s?Ed\.(?:\s?2d)?)"
    r"\s+(?P<pg>\d{1,4})"
    r"(?:\s*\((?P<paren>[^)]{2,40})\))?"
)


def _party_key(name):
    """First distinctive party word: 'Rancosky' from "Rancosky v. Washington Nat'l"."""
    head = re.split(r"\s+v\.?\s+", (name or "").strip())[0]
    words = [w for w in re.split(r"[^\w]+", head) if len(w) > 2]
    return words[-1] if words else head.strip()


# Deterministic store behind the retriever. Used to resolve opaque chunk identifiers
# back to their source key, because the retriever's metadata is inconsistent about it.
try:
    from pipeline.turbovec_ingest import CHUNK_DB as _CDB
    CHUNK_DB = str(_CDB)
except Exception:
    CHUNK_DB = str(_eroot() / "corpus/chunk_text.db")


def _cite_from_source_slug(slug):
    """Corpus source slugs embed the cite: ..._170a3d364 -> ('170','A.3d','364').

    STATE OFFICIAL REPORTERS MUST BE PARSED, not just the regional/federal ones.
    Without `pa`/`pasuper`/`pacommw` here, `scotus_cowden_v_aetna_..._389pa459`
    returned None, so Tier 1 found the slug in SQL, discarded it as unparseable,
    and reported NOT_IN_CORPUS for an opinion sitting in the corpus. PA practice
    cites the official reporter (389 Pa. 459) constantly, so this silently
    blanked a whole class of state authority.
    """
    m = re.search(
        r"_(\d{1,3})(a\d?d|us|sct|f\d?d|fsupp\d?d?|pasuper|pacommw|pa|nj|njsuper)(\d{1,4})(?:$|_)",
        (slug or "").lower())
    if not m:
        return None
    rep = {"a2d": "A.2d", "a3d": "A.3d", "ad": "A.", "us": "U.S.", "sct": "S. Ct.",
           "f2d": "F.2d", "f3d": "F.3d", "fd": "F.", "fsupp": "F. Supp.",
           "fsupp2d": "F. Supp. 2d", "fsupp3d": "F. Supp. 3d",
           "pa": "Pa.", "pasuper": "Pa. Super.", "pacommw": "Pa. Commw.",
           "nj": "N.J.", "njsuper": "N.J. Super."}.get(
               m.group(2), m.group(2))
    return (m.group(1), rep, m.group(3))


# Party tokens too generic to identify a case in a source key. "CoreStates Bank, N.A.
# v. Cutillo" reduces to "Bank", which matches thousands of unrelated keys.
_GENERIC_PARTY = {
    "bank", "banks", "insurance", "ins", "company", "companies", "corp", "corporation",
    "inc", "incorporated", "national", "assn", "association", "trust", "group", "financial",
    "finance", "holdings", "services", "service", "united", "states", "commonwealth",
    "county", "city", "state", "department", "dept", "board", "commission", "commissioner",
    "secretary", "director", "authority", "agency", "bureau", "office", "estate",
    "appellant", "appellee", "petitioner", "respondent", "plaintiff", "defendant",
    "mutual", "life", "casualty", "cas", "property", "prop", "general", "american",
    "first", "federal", "railroad", "railway", "brothers", "sons", "partners", "llc",
    "lllp", "llp", "company's", "the", "and", "of",
    # Place and jurisdiction names are never distinctive party tokens. "Pennsylvania"
    # as the SECOND party of "D'Ambrosio v. Pennsylvania Nat. Mut. Cas. Ins. Co."
    # token-matched scotus_giacco_v_pennsylvania_382us399 and produced a false
    # WRONG_CITE that blocked a filing and told the drafter to "correct" a good
    # Pa. Superior Court cite to an unrelated 1966 SCOTUS cite. A false WRONG_CITE
    # is strictly worse than NOT_IN_CORPUS: a gap warns, a wrong cite blocks and
    # misdirects. Keep this list in sync with any forum the corpus covers.
    "pennsylvania", "pennsylvanias", "penna", "pa", "philadelphia", "allegheny",
    "delaware", "jersey", "york", "maryland", "virginia", "ohio", "florida",
    "california", "texas", "illinois", "michigan", "georgia", "carolina",
    "columbia", "america", "usa", "us",
}


def _resolve_source(ident, _cache={}):
    """Map a retrieval identifier to its real `source` column value.
    The retriever is inconsistent about what it hands back: newly-ingested rows expose
    a chunk_id that STARTS with the source key (`scotus_x_729a2d1200_chunk_0`), while
    older corpus rows expose only an opaque hash (`0173a0e6ca8f...`). Parsing whatever
    arrives therefore yields no slug for older rows, and the verifier reported
    NOT_IN_CORPUS for cases the corpus demonstrably held -- a FALSE NEGATIVE, which is
    the one failure mode a gap-filling bot must not have (it triggers a redundant pull
    and, worse, makes a real authority look unusable).

    One deterministic sqlite lookup settles it. Cheap, cached, and indexed on chunk_id.
    """
    if not ident:
        return ""
    if ident in _cache:
        return _cache[ident]
    val = ""
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{CHUNK_DB}?mode=ro", uri=True, timeout=20)
        row = con.execute("SELECT source FROM chunk_text WHERE chunk_id=? LIMIT 1",
                          (ident,)).fetchone()
        if not row:
            # ...or the identifier IS a source key prefix (new-style chunk ids)
            row = con.execute("SELECT source FROM chunk_text WHERE chunk_id LIKE ? LIMIT 1",
                              (ident + "%",)).fetchone()
        con.close()
        val = (row[0] if row and row[0] else "") or ""
    except Exception:
        val = ""
    _cache[ident] = val
    return val


def verify_case_cite(name, vol, rep, pg, db=None, cache=None):
    """VERIFIED | WRONG_CITE | NOT_IN_CORPUS for one case citation.

    Provenance rule, learned the hard way: authority comes from the SOURCE SLUG of an
    opinion chunk (e.g. scotus_rancosky_v_washington_national_ins_co_170a3d364), never
    from body text. The contaminated vault note was itself ingested into the corpus, so
    "Rancosky" and the WRONG cite "190 A.3d 854" co-occur in real corpus text. Scoring
    by text co-occurrence therefore blessed the wrong cite and condemned the right one.
    A slug is written at ingest from the opinion's own reporter cite; prose is not.
    """
    if db is None:
        db = _find_legaldb()()
    cache = cache if cache is not None else {}
    _rep_clean = re.sub(r"\s+", " ", rep).strip()
    cite = f"{vol} {_rep_clean} {pg}"
    key = (name.strip().lower(), cite)
    if key in cache:
        return cache[key]

    party = _party_key(name)
    out = {"case": name.strip(), "cite": cite, "party": party,
           "status": "NOT_IN_CORPUS", "found_cite": "", "source": ""}

    # Collect every OPINION-SLUG cite for this party. Query on the case name plus
    # doctrinal words so the opinion itself ranks, not notes discussing it.
    slug_cites = {}          # "170 A.3d 364" -> source slug

    # TIER 1 -- deterministic sqlite on the source key.
    # Semantic retrieval sees new chunks ONLY after the next embed delta (hours),
    # while sqlite reaches them immediately. Without this tier the verifier could not
    # see the gap bot's OWN ingests and reported NOT_IN_CORPUS for opinions that had
    # just landed -- a false negative that triggers redundant pulls and makes a real
    # authority look unusable. Source keys are indexed and party-scoped, so this is cheap.
    #
    # Probe BOTH parties, and require the reporter-cite fragment in SQL. Two failures
    # forced this: (a) "CoreStates Bank, N.A. v. Cutillo" reduces to the generic party
    # key "Bank", which never reaches the Cutillo slug -- the SECOND party is usually the
    # distinctive token; (b) a bare LIKE on a common surname burns its LIMIT on unrelated
    # namesakes ("Mitchell" returned 8 cap_us Mitchells before the target), so filtering
    # must happen in SQL, not by truncation.
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{CHUNK_DB}?mode=ro", uri=True, timeout=20)
        frag = f"{vol}{_rep_clean.lower().replace('.', '').replace(' ', '')}{pg}"
        probes = set()
        # First party: prefer its LAST non-generic token. "_party_key" alone returns
        # "Corp" for "Exxon Mobil Corp.", which matched an unrelated Baxter Healthcare
        # case and produced a false WRONG_CITE -- worse than a miss, since it tells the
        # drafter to change a correct citation.
        m_first = re.split(r"\sv\.?\s", name.strip(), maxsplit=1, flags=re.I)[0]
        # {4,} silently skipped SHORT surnames: "Toy v. Metropolitan Life" yielded no
        # probe at all (Toy is 3 letters), so Tier 1 never looked for the Toy slug and
        # a present opinion read NOT_IN_CORPUS. Three-letter surnames are common
        # (Toy, Ray, Fox, Lee, Kim); accept {3,} for the FIRST party, whose surname is
        # checked against underscore tokens anyway, so a short token cannot run wild.
        for tok in reversed(re.findall(r"[A-Za-z']{3,}", m_first)):
            if tok.lower() not in _GENERIC_PARTY:
                probes.add(tok.lower())
                break
        if not probes:
            probes.add(party.lower())
        m_second = re.search(r"\sv\.?\s+(.+)$", name.strip(), re.I)
        if m_second:
            for tok in re.findall(r"[A-Za-z']{4,}", m_second.group(1)):
                if tok.lower() not in _GENERIC_PARTY:
                    probes.add(tok.lower())
                    break
        for pk in probes:
            # len < 3 only: a 3-char surname (Toy, Fox, Ray) is a legitimate probe and
            # is token-matched against underscore-split slug parts below, so it cannot
            # match by accident the way a substring would.
            if not pk or pk in _GENERIC_PARTY or len(pk) < 3:
                continue
            # Apostrophes become SEPARATORS at ingest: "D'Ambrosio" is keyed as
            # scotus_d_ambrosio_..., whose underscore tokens are {d, ambrosio}. A probe
            # of "d'ambrosio" therefore matched no token and a present opinion read
            # NOT_IN_CORPUS. Compare on the apostrophe-stripped tail as well.
            pk_parts = [p for p in re.split(r"[^a-z0-9]+", pk) if p]
            pk_alts = {pk, pk.replace("'", "")}
            if pk_parts:
                pk_alts.add(pk_parts[-1])
            # Try EVERY alt: which spelling reaches the slug depends on how the ingest
            # handled punctuation ("D'Ambrosio" -> scotus_d_ambrosio_...), so picking a
            # single "best" alt guesses wrong. "dambrosio" matches nothing while
            # "ambrosio" matches both the LIKE and the underscore token.
            for pk_sql in sorted(a for a in pk_alts if "'" not in a):
                for (src,) in con.execute(
                        "SELECT DISTINCT source FROM chunk_text "
                        "WHERE LOWER(source) LIKE ? AND LOWER(source) LIKE ? LIMIT 8",
                        (f"%{pk_sql}%", f"%{frag}%")):
                    if not (pk_alts & set((src or "").lower().split("_"))):
                        continue      # token match, not substring (see note below)
                    got = _cite_from_source_slug(src or "")
                    if got:
                        slug_cites[f"{got[0]} {got[1]} {got[2]}"] = src
                # also sweep the party alone, to surface a DIFFERENT cite for the same case
                for (src,) in con.execute(
                        "SELECT DISTINCT source FROM chunk_text "
                        "WHERE LOWER(source) LIKE ? AND source LIKE 'scotus_%' LIMIT 25",
                        (f"%{pk_sql}%",)):
                    # Source keys are underscore-delimited, so match on TOKENS, not
                    # substrings: "mobil" inside
                    # "scotus_mobile_j_kc_r_co_v_turnipseed_219us35" made an unrelated
                    # 1910 case masquerade as Exxon Mobil and produced a false
                    # WRONG_CITE that would have told the drafter to change a good cite.
                    if not (pk_alts & set((src or "").lower().split("_"))):
                        continue
                    got = _cite_from_source_slug(src or "")
                    if got:
                        slug_cites.setdefault(f"{got[0]} {got[1]} {got[2]}", src)
        con.close()
    except Exception as e:
        out.setdefault("error", f"sqlite_tier: {str(e)[:100]}")

    # TIER 1b -- VOLUME-KEYED families (legacy SCOTUS scans, cap_us).
    # `SCOTUS_vol_USREPORTS-263_PDFA` and `cap_us_263_413_<slug>` carry the volume in the
    # KEY, not a party-name slug, so the party-scoped sweep above cannot see them and the
    # verifier reported NOT_IN_CORPUS for opinions sitting in fully-ingested volumes
    # (Rooker, Feldman, Younger, Pennzoil all read absent while their volumes held
    # 400-600 chunks each). For these the volume+reporter IS the provenance: confirm the
    # volume exists, then require the case name in that volume's TEXT -- a legacy scan is
    # one blob per volume, so the name check is what distinguishes the case.
    # EXACT-KEY FIRST (class fix): a pleaded "549 U.S. 225" whose own opinion sits under a volume+page key
    # (cap_us_549_225_osborn_v_haley, docvault_pull_f3d_744_853) is VERIFIED even when another case sharing the
    # party name (Osborn v. Bank of U.S., 22 U.S. 738) surfaced in the slug sweep. Before this, the volume tier ran
    # only when the sweep found nothing, so a same-name older case turned a correct cite into a WRONG_CITE.
    try:
        import sqlite3
        _rk = re.sub(r"[^a-z0-9]", "", _rep_clean.lower())
        _pt = [t for t in re.split(r"[^a-z0-9]+", (party or "").lower()) if len(t) > 2]
        if _rk and _pt:
            con = sqlite3.connect(f"file:{CHUNK_DB}?mode=ro", uri=True, timeout=20)
            _pats = [f"cap_us_{vol}_{pg}_%"] if _rk == "us" else []
            _pats += [f"%{_rk}_{vol}_{pg}%", f"%{_rk}{vol}_{pg}%", f"%_{vol}{_rk}{pg}%"]
            _hit = None
            for _pat in _pats:
                for (srcx,) in con.execute("SELECT DISTINCT source FROM chunk_text WHERE LOWER(source) LIKE ? LIMIT 12",
                                           (_pat,)):
                    _toks = set((srcx or "").lower().replace("-", "_").split("_"))
                    _txt_ok = (_pt[-1] in _toks) or con.execute(
                        "SELECT 1 FROM chunk_text WHERE source=? AND LOWER(text) LIKE ? LIMIT 1",
                        (srcx, f"%{_pt[-1]}%")).fetchone()
                    if _txt_ok:
                        _hit = srcx; break
                if _hit: break
            con.close()
            if _hit:
                out.update(status="VERIFIED", source=_hit, found_cite=cite,
                           note="exact volume+page key; party confirmed in key or text")
                cache[key] = out
                return out
    except Exception as e:
        out.setdefault("error", f"exact_key_tier: {str(e)[:100]}")

    if not slug_cites and _rep_clean in ("U.S.", "U. S."):
        try:
            import sqlite3
            con = sqlite3.connect(f"file:{CHUNK_DB}?mode=ro", uri=True, timeout=20)
            legacy = f"SCOTUS_vol_USREPORTS-{vol}_PDFA"
            n_leg = con.execute("SELECT COUNT(*) FROM chunk_text WHERE source=?",
                                (legacy,)).fetchone()[0]
            cap_key = None
            row = con.execute(
                "SELECT source FROM chunk_text WHERE source LIKE ? AND LOWER(source) LIKE ? "
                "LIMIT 1", (f"cap_us_{vol}_%", f"%{party.lower()}%")).fetchone()
            if row:
                cap_key = row[0]
            if cap_key:
                out.update(status="VERIFIED", source=cap_key, found_cite=cite)
                con.close(); cache[key] = out; return out
            if n_leg:
                hit = con.execute(
                    "SELECT COUNT(*) FROM chunk_text WHERE source=? AND text LIKE ?",
                    (legacy, f"%{party}%")).fetchone()[0]
                con.close()
                if hit:
                    out.update(status="VERIFIED", source=legacy, found_cite=cite,
                               note="volume-keyed legacy scan; party confirmed in volume text")
                    cache[key] = out
                    return out
            else:
                con.close()
        except Exception as e:
            out.setdefault("error", f"volume_tier: {str(e)[:100]}")

    # TIER 1c -- NAME+YEAR-KEYED slugs (no reporter cite in the key).
    # Tier 1's SQL requires the reporter fragment ("600us122") INSIDE the source slug, so
    # a slug keyed by party and year -- scotus_mallory_v_norfolk_southern_2023 -- can never
    # satisfy it and the opinion reads NOT_IN_CORPUS no matter how completely it is ingested
    # (Mallory: 86 chunks, full opinion text, still absent). 14 named slugs in the corpus
    # have this shape (recent SCOTUS pulls keyed by year, plus NJ/early-US pulls).
    # Provenance rule still holds: authority comes from the OPINION'S OWN text, never from
    # prose discussing it. A reporter cite in the FIRST chunks of the opinion keyed to this
    # party is the opinion's own caption/syllabus line -- that is provenance. Restricting the
    # scan to that slug's own low-numbered chunks is what keeps a vault note out of it.
    if not slug_cites:
        try:
            import sqlite3
            con = sqlite3.connect(f"file:{CHUNK_DB}?mode=ro", uri=True, timeout=20)
            _cite_re = re.compile(
                rf"\b{re.escape(vol)}\s+{re.escape(_rep_clean)}\s+{re.escape(str(pg))}\b")
            for pk_sql in sorted(a for a in ({party.lower()} | set()) if a and len(a) >= 3):
                for (src,) in con.execute(
                        "SELECT DISTINCT source FROM chunk_text "
                        "WHERE LOWER(source) LIKE ? LIMIT 40", (f"%{pk_sql}%",)):
                    toks = set((src or "").lower().split("_"))
                    if pk_sql not in toks:
                        continue
                    if _cite_from_source_slug(src or ""):
                        continue          # cite-keyed slugs are Tier 1's business
                    # the opinion's own caption/syllabus: its first chunks and any cite header
                    for (txt,) in con.execute(
                            "SELECT text FROM chunk_text WHERE source=? AND ("
                            "chunk_id LIKE '%_cite_hdr' OR chunk_id LIKE '%_chunk_0' "
                            "OR chunk_id LIKE '%_chunk_1' OR chunk_id LIKE '%_chunk_2') "
                            "LIMIT 8", (src,)):
                        if _cite_re.search(txt or ""):
                            out.update(status="VERIFIED", source=src, found_cite=cite,
                                       note="name+year-keyed slug; cite confirmed in the "
                                            "opinion's own caption/first pages")
                            con.close(); cache[key] = out; return out
            con.close()
        except Exception as e:
            out.setdefault("error", f"nameyear_tier: {str(e)[:100]}")

    # TIER 2 -- semantic retrieval, for corpus rows whose slug the key sweep missed.
    for q in () if slug_cites else (f"{name} {cite}", f"{party} {_rep_clean}", party):
        try:
            rows = db.query_text(q, n_results=14)
        except Exception as e:
            out["error"] = str(e)[:160]
            break
        docs, metas = _normalize(rows)
        for doc, meta in zip(docs, metas):
            src = (meta or {}).get("source", "") or ""
            # The retriever may hand back an opaque chunk hash instead of the source
            # key; resolve it deterministically before judging provenance, or older
            # corpus rows read as absent.
            if not _cite_from_source_slug(src):
                resolved = _resolve_source(src)
                if resolved:
                    src = resolved
            # the slug must name THIS party to count as that party's opinion
            if party.lower() not in src.lower():
                continue
            got = _cite_from_source_slug(src)
            if got:
                slug_cites[f"{got[0]} {got[1]} {got[2]}"] = src
        if slug_cites:
            break

    if slug_cites:
        want = cite.replace(" ", "").lower()
        for found, src in slug_cites.items():
            if found.replace(" ", "").lower() == want:
                out.update(status="VERIFIED", source=src, found_cite=found)
                cache[key] = out
                return out
        # PARALLEL CITATIONS ARE NOT CONFLICTS. A Pennsylvania decision is published
        # in BOTH the official reporter and West's Atlantic: Stevenson v. Economy Bank
        # of Ambridge is 413 Pa. 442 AND 197 A.2d 721 -- one decision, two cites. The
        # corpus slug carries only one of them, so a pleaded "413 Pa. 442" against a
        # slug keyed "_197a2d721" read as a WRONG_CITE and blocked the filing, when the
        # pleaded cite was correct. Treat a same-case different-REPORTER hit as verified
        # (parallel), and reserve WRONG_CITE for a conflict WITHIN the same reporter --
        # which is the only case where one of the two numbers must actually be wrong.
        want_rep = _rep_clean.lower().replace(".", "").replace(" ", "")
        same_rep = []
        for found, src in slug_cites.items():
            fm = re.match(r"(\d{1,3})\s+(.+?)\s+(\d{1,4})$", found)
            frep = fm.group(2).lower().replace(".", "").replace(" ", "") if fm else ""
            if frep == want_rep:
                same_rep.append((found, src))
        if not same_rep:
            found, src = next(iter(slug_cites.items()))
            out.update(status="VERIFIED", source=src, found_cite=found,
                       note=f"parallel citation: pleaded {cite}; corpus carries {found} "
                            f"(same decision, different reporter)")
            cache[key] = out
            return out
        slug_cites = dict(same_rep)
        # The party's opinion is in corpus under a DIFFERENT cite -> Rancosky defect.
        # A case usually has SEVERAL slugs (Superior Court below, Supreme Court above:
        # Rancosky appears at both 130 A.3d 79 and 170 A.3d 364). Offer the HIGHEST
        # court's cite as the correction -- naming the intermediate decision would
        # replace one wrong citation with another.
        def _rank(item):
            found, src = item
            s = src.lower()
            lvl = 0
            if "scotus" in s or "_us_" in s or "sct" in s:
                lvl = 4
            elif "super" in s or "cmwlth" in s:
                lvl = 2
            elif re.search(r"\bpa\b|_pa_", s):
                lvl = 3
            # later volume of the same reporter = later decision = usually the appeal
            vol = int(re.match(r"(\d+)", found).group(1)) if re.match(r"(\d+)", found) else 0
            return (lvl, vol)
        ranked = sorted(slug_cites.items(), key=_rank, reverse=True)
        found, src = ranked[0]
        out.update(status="WRONG_CITE", found_cite=found, source=src,
                   alternatives=[c for c, _ in ranked[1:]])
        cache[key] = out
        return out

    cache[key] = out
    return out


def scan_text(text, db=None, cache=None):
    """Scan any text for case citations. Agnostic: no case list, no directory layout."""
    if db is None:
        db = _find_legaldb()()
    cache = cache if cache is not None else {}
    seen, results = set(), []
    for m in CASE_CITE_RE.finditer(text or ""):
        k = (m.group("name").strip(), m.group("vol"), m.group("pg"))
        if k in seen:
            continue
        seen.add(k)
        results.append(verify_case_cite(m.group("name"), m.group("vol"),
                                       m.group("rep"), m.group("pg"),
                                       db=db, cache=cache))
    return {
        "total": len(results),
        "wrong": [r for r in results if r["status"] == "WRONG_CITE"],
        "gaps": [r for r in results if r["status"] == "NOT_IN_CORPUS"],
        "verified": [r for r in results if r["status"] == "VERIFIED"],
        "results": results,
    }


def scan_file(path, db=None, cache=None):
    p = Path(path)
    rep = scan_text(p.read_text(encoding="utf-8", errors="replace"), db=db, cache=cache)
    rep["path"] = str(p)
    return rep


def scan_paths(paths, glob="*.md", db=None, cache=None):
    """Scan files and/or directories. Skips .bak. Returns only files carrying cites."""
    if db is None:
        db = _find_legaldb()()
    cache = cache if cache is not None else {}
    out = []
    for raw in paths:
        p = Path(raw)
        files = [p] if p.is_file() else sorted(p.rglob(glob))
        for f in files:
            if ".bak" in f.name:
                continue
            try:
                r = scan_file(f, db=db, cache=cache)
            except Exception:
                continue
            if r["total"]:
                out.append(r)
    return out


def gate_scan(paths, glob="*.md"):
    """GATE entry point. Non-zero when a WRONG_CITE exists: 1 = blocking, 2 = gaps."""
    reports = scan_paths(paths, glob=glob)
    wrong = [w for r in reports for w in r["wrong"]]
    gaps = [g for r in reports for g in r["gaps"]]
    for r in reports:
        if r["wrong"] or r["gaps"]:
            print(f"\n{r['path']}  [{r['total']} cites]")
            for x in r["wrong"]:
                print(f"  WRONG_CITE    {x['case'][:44]}: cited {x['cite']} "
                      f"-> corpus has {x['found_cite']}   ({x['source'][:44]})")
            for x in r["gaps"]:
                print(f"  NOT_IN_CORPUS {x['case'][:44]}: {x['cite']}")
    print(f"\nfiles {len(reports)} | WRONG {len(wrong)} | GAPS {len(gaps)} | "
          f"ok {sum(len(r['verified']) for r in reports)}")
    return 1 if wrong else (2 if gaps else 0)

if __name__ == '__main__':
    main()
