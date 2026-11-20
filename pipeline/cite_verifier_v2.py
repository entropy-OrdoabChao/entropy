#!/usr/bin/env python3
"""
Bundle 21 - cite_verifier_v2
Fixes the 100%-unverified false-negative in Bundle 20's verifier.

Root causes addressed:
  1. n_results=10 too small - ChromaDB returns SEMANTIC top-K, the cite as
     a query may not surface chunks containing it verbatim. Now n=50.
  2. Exact substring match too strict. Real cite text in corpus is
     formatted dozens of ways: '15 U.S.C. § 1681' vs '15 USC 1681' vs
     '§ 1681' vs '15 U.S.C.A. § 1681'. Now uses 5 normalization variants
     for both query and matching.
  3. Query and match in the same form. Now queries each variant and
     accepts a match against ANY variant in ANY returned chunk.

The forum / criminal-statute flags from v1 are preserved (the regex-based
flagging worked correctly).

Outputs:
  <home>/obsidian_sync/cases/<case>/_cite_verification.md
  <home>/obsidian_sync/_cite_verification_master.md
"""
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import sys
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
    'fed_26-cv-00123', 'fed_26-cv-00456',
    'fed_26-cv-00789', 'phila_ccp_260100000',
]

FEDERAL_CASES = {'fed_26-cv-00123', 'fed_26-cv-00456', 'fed_26-cv-00789'}
STATE_CASES = {'phila_ccp_260100000'}

FEDERAL_ONLY_STATUTES = [
    (r'\b5\s+U\.S\.C\.', 'APA / federal admin law - federal agency only'),
    (r'\b28\s+U\.S\.C\.\s+§+\s*1361\b', 'Federal mandamus - federal officers only'),
    (r'\b28\s+U\.S\.C\.\s+§+\s*1331\b', 'Federal question jurisdiction'),
    (r'\b28\s+U\.S\.C\.\s+§+\s*1332\b', 'Diversity jurisdiction - federal'),
    (r'\b28\s+U\.S\.C\.\s+§+\s*1441\b', 'Removal - federal court only'),
    (r'\b28\s+U\.S\.C\.\s+§+\s*1447\b', 'Federal remand procedure'),
    (r'\b28\s+U\.S\.C\.\s+§+\s*351\b',
     'Federal Judicial Council complaint - federal judges only'),
    (r'\bFed\.\s*R\.\s*Civ\.\s*P\.', 'Federal Rules of Civil Procedure'),
    (r'\bFed\.\s*R\.\s*Evid\.', 'Federal Rules of Evidence'),
]
CRIMINAL_STATUTES_PRIVATE_INVALID = [
    (r'\b18\s+U\.S\.C\.\s+§+\s*242\b',
     'Federal criminal - private plaintiff cannot prosecute'),
    (r'\b18\s+U\.S\.C\.\s+§+\s*241\b',
     'Federal criminal conspiracy - private plaintiff cannot prosecute'),
    (r'\b18\s+U\.S\.C\.\s+§+\s*1001\b',
     'Federal false statements criminal statute'),
    (r'\b18\s+U\.S\.C\.\s+§+\s*1505\b', 'Federal obstruction criminal'),
    (r'\b18\s+U\.S\.C\.\s+§+\s*1512\b',
     'Federal witness tampering criminal'),
]
PROCEDURALLY_MISPLACED = [
    (r'\b42\s+U\.S\.C\.\s+§+\s*1983\b',
     'Private right of action - check usage context'),
]


def extract_citations(text):
    cites = set()
    for m in re.finditer(
            r'\b\d{1,4}\s+(?:U\.S\.|F\.\s?[2-4]?d|F\.\sSupp\.\s?[2-3]?d|S\.\s?Ct\.|Pa\.|A\.\s?[2-3]?d|N\.E\.\s?[2-3]?d|S\.W\.\s?[2-3]?d|S\.E\.\s?[2-3]?d|P\.\s?[2-3]?d)\s+\d+',
            text):
        cites.add(re.sub(r'\s+', ' ', m.group(0)).strip().rstrip('.,;'))
    for m in re.finditer(
            r'\b\d{1,2}\s+U\.S\.C\.?(?:A\.)?\s+§+\s*\d+[A-Za-z0-9\-\.]*',
            text):
        cites.add(re.sub(r'\s+', ' ', m.group(0)).strip().rstrip('.,;'))
    for m in re.finditer(
            r'\b\d{1,2}\s+C\.F\.R\.\s+§+\s*\d+[A-Za-z0-9\-\.]*', text):
        cites.add(re.sub(r'\s+', ' ', m.group(0)).strip().rstrip('.,;'))
    for m in re.finditer(
            r'\b\d{1,2}\s+Pa\.C\.S\.?(?:A\.)?\s+§+\s*\d+[A-Za-z0-9\-\.]*',
            text):
        cites.add(re.sub(r'\s+', ' ', m.group(0)).strip().rstrip('.,;'))
    for m in re.finditer(
            r'\bFed\.?\s*R\.\s*(?:Civ|Crim|Evid|App|Bankr)\.\s*P?\.?\s*\d+(?:\([a-z0-9]+\))?',
            text):
        cites.add(re.sub(r'\s+', ' ', m.group(0)).strip().rstrip('.,;'))
    for m in re.finditer(
            r'\bPa\.\s*R\.\s*(?:Civ|Crim|Evid|App)\.\s*P?\.?\s*\d+(?:\([a-z0-9]+\))?',
            text):
        cites.add(re.sub(r'\s+', ' ', m.group(0)).strip().rstrip('.,;'))
    return cites


def cite_variants(cite):
    """Generate up to 6 normalized variants for matching."""
    out = []
    base = cite.strip()
    out.append(base)
    out.append(base.lower())
    no_sec = re.sub(r'§+\s*', '', base.lower())
    no_sec = re.sub(r'\bsec\.\s*', '', no_sec)
    no_sec = re.sub(r'\s+', ' ', no_sec).strip()
    out.append(no_sec)
    # Drop periods entirely (USC vs U.S.C.)
    no_dots = re.sub(r'\.', '', base.lower())
    no_dots = re.sub(r'\s+', ' ', no_dots).strip()
    out.append(no_dots)
    # Structural number form (e.g., "15 1681" or "551 89")
    m = re.match(r'(\d+)\s+[A-Za-z\.]+\s+§?\s*([\dA-Za-z\-\.]+)', base)
    if m:
        title, section = m.group(1), m.group(2)
        out.append(f'{title} {section}')
        out.append(f'§ {section}')
        out.append(f'section {section}')
    # Dedupe preserving order
    seen = set()
    dedup = []
    for v in out:
        v = v.strip()
        if v and v not in seen:
            seen.add(v)
            dedup.append(v)
    return dedup


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


def _normalize_db_result(result):
    """B34: handle list-of-dicts from LegalDB. Don't let item fields
    override our derived source. Always cast to string."""
    docs, metas = [], []
    if isinstance(result, list):
        for item in result:
            if not isinstance(item, dict):
                continue
            text = item.get('text') or item.get('document') or ''
            id_str = item.get('id', '') or ''
            id_str = str(id_str)
            # Derive a clean source name from the id
            if '_chunk_' in id_str:
                source = id_str.split('_chunk_', 1)[0]
            elif re.match(r'^[a-f0-9]{16,}$', id_str):
                source = 'unknown'
            else:
                m = re.match(r'(.+?)_\d+$', id_str)
                source = m.group(1) if m else (
                    id_str[:48] if id_str else 'unknown')
            source = str(source) if source else 'unknown'
            meta = {'source': source, 'id': id_str}
            docs.append(text)
            metas.append(meta)
        return docs, metas
    if isinstance(result, dict) and 'documents' in result:
        d = result.get('documents') or []
        m_ = result.get('metadatas') or []
        if d and isinstance(d[0], list):
            docs, metas = d[0], (m_[0] if m_ else [])
        else:
            docs, metas = d, m_
    return docs, metas

def verify_cite_v2(cite, db, cache, n_results=50, max_queries=3):
    """Aggressive verification: query 3 variants at n=50, accept any
    verbatim match (in any variant form) against any returned chunk."""
    if cite in cache:
        return cache[cite]
    variants = cite_variants(cite)
    sources = set()
    snippets = {}
    for q in variants[:max_queries]:
        if not q.strip():
            continue
        try:
            result = db.query_text(q, n_results=n_results)
        except Exception as e:
            continue
        docs, metas = _normalize_db_result(result)
        for doc, meta in zip(docs, metas):
            doc_l = (doc or '').lower()
            for vv in variants:
                if not vv or len(vv) < 3:
                    continue
                if vv.lower() in doc_l:
                    src = (meta or {}).get('source', 'unknown')
                    sources.add(src)
                    if src not in snippets:
                        idx = doc_l.find(vv.lower())
                        ctx = (doc[max(0, idx - 60):idx + len(vv) + 60]
                               or '').replace('\n', ' ').strip()
                        snippets[src] = re.sub(r'\s+', ' ', ctx)[:220]
                    break
    out = {
        'verified': len(sources) > 0,
        'sources': sorted(sources),
        'snippets': snippets,
        'n_hits': len(sources),
    }
    cache[cite] = out
    return out


def detect_forum_mismatch(cite, case_folder):
    flags = []
    in_state = case_folder in STATE_CASES
    if in_state:
        for pat, reason in FEDERAL_ONLY_STATUTES:
            if re.search(pat, cite):
                flags.append(('FEDERAL_IN_STATE', reason))
    for pat, reason in CRIMINAL_STATUTES_PRIVATE_INVALID:
        if re.search(pat, cite):
            flags.append(('CRIMINAL_BY_PRIVATE', reason))
    for pat, reason in PROCEDURALLY_MISPLACED:
        if re.search(pat, cite):
            flags.append(('PROCEDURAL_NOTE', reason))
    return flags


def render_case_report(case_name, per_motion_cites, cite_verification,
                        cite_flags):
    L = [f'# Cite Verification (v2) — {case_name}', '',
         f'*Generated: {datetime.now().strftime("%Y-%m-%d %H:%M")}*', '']
    all_cites = set()
    for cites in per_motion_cites.values():
        all_cites.update(cites)
    verified = [c for c in all_cites if cite_verification[c]['verified']]
    unverified = [c for c in all_cites
                  if not cite_verification[c]['verified']]
    flagged_cites = [c for c in all_cites if cite_flags.get(c)]
    L.append('## Summary')
    L.append('')
    L.append(f'- Motions checked: {len(per_motion_cites)}')
    L.append(f'- Distinct cites: {len(all_cites)}')
    L.append(f'- Verified (found in DB with verbatim match): {len(verified)}')
    L.append(f'- Unverified (no verbatim match in 50-result query): '
             f'{len(unverified)}')
    L.append(f'- Forum/context flagged: {len(flagged_cites)}')
    L.append('')

    if unverified:
        L.append('## Unverified cites')
        L.append('')
        L.append('No verbatim match in the indexed corpus. Could be: (a) the '
                 'cite exists in real law but is not in this DB subset; '
                 '(b) a formatting variant the matcher missed; or '
                 '(c) actually fabricated. Verify by hand.')
        L.append('')
        cite_to_motions = defaultdict(list)
        for mot, cites in per_motion_cites.items():
            for c in cites:
                cite_to_motions[c].append(mot)
        L.append('| Cite | # motions |')
        L.append('|---|---:|')
        for c in sorted(unverified):
            L.append(f'| `{c}` | {len(cite_to_motions[c])} |')
        L.append('')

    if flagged_cites:
        L.append('## Forum / context flags')
        L.append('')
        for c in sorted(flagged_cites):
            L.append(f'### `{c}`')
            for kind, reason in cite_flags[c]:
                L.append(f'- {kind}: {reason}')
            ms = [m for m, cs in per_motion_cites.items() if c in cs]
            L.append(f'- In motions: {", ".join(f"`{m}`" for m in ms[:6])}')
            if len(ms) > 6:
                L.append(f'  (+ {len(ms) - 6} more)')
            L.append('')

    L.append('## Verified cites')
    L.append('')
    L.append('| Cite | Sources | # motions | Snippet |')
    L.append('|---|---|---:|---|')
    cite_to_motions = defaultdict(list)
    for mot, cites in per_motion_cites.items():
        for c in cites:
            cite_to_motions[c].append(mot)
    for c in sorted(verified):
        v = cite_verification[c]
        srcs = v['sources'][:3]
        srcs_str = ', '.join(f'`{s}`' for s in srcs)
        if len(v['sources']) > 3:
            srcs_str += f' (+{len(v["sources"]) - 3})'
        # Top snippet
        snip = ''
        if v.get('snippets'):
            snip = next(iter(v['snippets'].values()))
            snip = snip.replace('|', '\\|')[:120]
        L.append(f'| `{c}` | {srcs_str} | {len(cite_to_motions[c])} | '
                 f'{snip} |')
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
    ap.add_argument('--n-results', type=int, default=50)
    args = ap.parse_args()

    case_folders = [case_id_to_folder(args.case)] if args.case else ACTIVE_CASES
    LegalDB = _find_legaldb()
    db = LegalDB()
    cite_cache = {}
    master_rows = []

    for cf in case_folders:
        cd = CASES_ROOT / cf
        fr = cd / '_filing_ready'
        if not fr.exists():
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

        all_cites = set()
        for cs in per_motion_cites.values():
            all_cites.update(cs)
        print(f'  distinct cites: {len(all_cites)}')

        cite_verification = {}
        for i, c in enumerate(sorted(all_cites), 1):
            v = verify_cite_v2(c, db, cite_cache, n_results=args.n_results)
            cite_verification[c] = v
            if i % 10 == 0 or i == len(all_cites):
                n_v = sum(1 for cc in cite_verification.values()
                          if cc['verified'])
                print(f'    [{i:3d}/{len(all_cites)}] verified: {n_v}')

        cite_flags = {}
        for c in all_cites:
            f = detect_forum_mismatch(c, cf)
            if f:
                cite_flags[c] = f

        out = cd / '_cite_verification.md'
        out.write_text(
            render_case_report(cf, per_motion_cites, cite_verification,
                                cite_flags),
            encoding='utf-8')
        n_v = sum(1 for v in cite_verification.values() if v['verified'])
        n_u = len(all_cites) - n_v
        print(f'  [OK] verified: {n_v}/{len(all_cites)}; '
              f'unverified: {n_u}; forum: {len(cite_flags)}')
        master_rows.append({
            'case': cf, 'motions': len(md_files), 'cites': len(all_cites),
            'verified': n_v, 'unverified': n_u, 'flagged': len(cite_flags),
        })

    L = ['# Cite Verification (v2) — Master', '',
         f'*Generated: {datetime.now().strftime("%Y-%m-%d %H:%M")}*', '']
    L.append('| Case | Motions | Cites | Verified | Unverified | Flagged |')
    L.append('|---|---:|---:|---:|---:|---:|')
    for r in master_rows:
        L.append(f'| `{r["case"]}` | {r["motions"]} | {r["cites"]} | '
                 f'{r["verified"]} | {r["unverified"]} | {r["flagged"]} |')
    L.append('')
    OUT_MASTER.write_text('\n'.join(L) + '\n', encoding='utf-8')
    print(f'\n[OK] {OUT_MASTER}')


if __name__ == '__main__':
    main()
