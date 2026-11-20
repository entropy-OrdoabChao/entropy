#!/usr/bin/env python3
"""
Bundle 17 - case_definitions_page
For each case, generates a per-case definitions page that:
  - Lists every grounded term used in the case
  - Shows the authoritative definition from indexed sources, in priority:
      Black's Law 9th > Bouvier 1856 > CJS > USC > CFR > case law
  - Shows the user's actual usage contexts from their filings, side-by-side
  - Optional LLM check: flags usage that doesn't match the definition

Hand-holding output. Treats every term like a flashcard:
    TERM | AUTHORITATIVE DEFINITION | YOUR USAGE | MATCHES?

Inputs:
  <home>/obsidian_sync/_dynamic_term_grounding.yaml
  <home>/obsidian_sync/cases/<case>/_case_law_audit.md
  Live ChromaDB

Outputs per case:
  <home>/obsidian_sync/cases/<case>/_definitions.md

Run:
  python3 case_definitions_page.py                     # all 4 active cases
  python3 case_definitions_page.py --case fed:2:26-cv-00123
  python3 case_definitions_page.py --check-usage       # LLM consistency check
  python3 case_definitions_page.py --top-terms 100     # cap
"""
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import sys
import os
import re
import argparse
import yaml
import importlib
import hashlib
import json
import urllib.request
from pathlib import Path
from collections import Counter, defaultdict
from datetime import datetime

REPO = _eroot()
# 2026-11-10 (keyed rung): the vault root is env-overridable so a sandbox proof writes its OWN copies of the two vault files
# (_dynamic_term_grounding.yaml, cases/<case>/_definitions.md) — never the live vault. Default = live, unchanged.
VAULT = Path(os.environ.get('BEEZLE_VAULT', str(REPO / 'obsidian_sync')))
CASES_ROOT = VAULT / 'cases'
CACHE_DIR = REPO / '.cache' / 'case_law_audit_v2'
GROUNDING_YAML = VAULT / '_dynamic_term_grounding.yaml'

LLM_URL = 'http://localhost:8000/v1/chat/completions'

# Source priority for definitions (higher = more authoritative for
# definitional purposes)
SOURCE_PRIORITY = {
    'blacks_law_9th': 100,
    "blacks_law_9th": 100,
    "black's_law_9th": 100,
    'blacks_law_2nd': 95,
    "black's_law_2nd": 95,
    # 2026-11-10: the two Black's editions the corpus ALSO holds (blacks_law_8th 9,821 chunks, blacks_law_4th 9,953) were
    # unlisted and sorted at 10 (below CFR). Added additively BELOW the pre-existing 9th > 2nd order, above Bouvier.
    'blacks_law_8th': 94,
    'blacks_law_4th': 93,
    'bouvier_1856': 90,
    'bouvier_1856_law_dictionary': 90,
    'cjs_vol_40': 85,
    'cjs': 80,
    # statutory
    'us_code': 75,
    'uscode': 75,
    'usc': 75,
    # regulations
    'cfr': 70,
    # case law - lower for pure definitional purposes
    'usreports': 50,
    'us_reports': 50,
    'fsupp': 45,
    'f3d': 45,
    'f2d': 45,
}


def source_score(source):
    if not source:
        return 0
    sl = source.lower()
    for key, prio in SOURCE_PRIORITY.items():
        if key in sl:
            return prio
    return 10  # unknown sources go last


def _find_legaldb():
    for p in (str(REPO), str(REPO / 'pipeline')):
        if p not in sys.path:
            sys.path.insert(0, p)
    for name in [
        'pipeline.legal_db_client', 'legal_db_client',
        'pipeline.legal_db', 'legal_db',
        'pipeline.beezle_db', 'beezle_db',
        'pipeline.chromadb_client', 'chromadb_client',
        'pipeline.db_client', 'db_client',
    ]:
        try:
            mod = importlib.import_module(name)
            if hasattr(mod, 'LegalDB'):
                return mod.LegalDB
        except Exception:
            continue
    pipe = REPO / 'pipeline'
    if pipe.exists():
        for py_file in sorted(pipe.glob('*.py')):
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


# Skip dirs - same as Patch D
SKIP_DIR_NAMES = {
    '.git', '.obsidian', '.cache', '.backups', '.history', '.vscode',
    '.idea', '.svn', '.hg', 'node_modules', '__pycache__', '.audit_cache',
}


def gather_files(case_dir):
    files = []
    allowed_exts = {'.pdf', '.docx', '.txt', '.md'}
    for dirpath, dirnames, filenames in os.walk(str(case_dir),
                                                topdown=True):
        dirnames[:] = [d for d in dirnames
                       if d not in SKIP_DIR_NAMES
                       and not d.startswith('.')
                       and 'bundle' not in d.lower()
                       and 'packet' not in d.lower()]
        for fn in filenames:
            ext = os.path.splitext(fn.lower())[1]
            if ext not in allowed_exts:
                continue
            if fn.startswith('_'):
                continue
            if fn.lower().endswith('_draft.md'):
                continue
            files.append(Path(dirpath) / fn)
    return sorted(files)


def cache_key(path):
    try:
        s = path.stat()
        key = f'{path.name}|{s.st_size}|{int(s.st_mtime)}'
    except OSError:
        key = f'{path.name}|0|0'
    return hashlib.sha1(key.encode('utf-8')).hexdigest()


def read_file_cached(path):
    cp = CACHE_DIR / (cache_key(path) + '.txt')
    if cp.exists():
        try:
            return cp.read_text(encoding='utf-8', errors='ignore')
        except OSError:
            pass
    # Fall back to inline read
    suf = path.suffix.lower()
    try:
        if suf == '.pdf':
            from pypdf import PdfReader
            r = PdfReader(str(path))
            if len(r.pages) > 200:
                return ''
            return '\n'.join((p.extract_text() or '') for p in r.pages)
        if suf == '.docx':
            import docx
            d = docx.Document(str(path))
            return '\n'.join(p.text for p in d.paragraphs)
        if suf in ('.txt', '.md'):
            return path.read_text(encoding='utf-8', errors='ignore')
    except Exception:
        return ''
    return ''


def find_term_contexts(term, case_dir, max_contexts=5, context_chars=200):
    """Find all places in case files where `term` appears, return list of
    {file, snippet}."""
    contexts = []
    term_lower = term.lower()
    seen_files = set()
    for f in gather_files(case_dir):
        if len(contexts) >= max_contexts:
            break
        text = read_file_cached(f)
        if not text:
            continue
        tl = text.lower()
        idx = tl.find(term_lower)
        if idx < 0:
            continue
        rel = str(f.relative_to(case_dir))
        if rel in seen_files:
            continue
        seen_files.add(rel)
        ctx_start = max(0, idx - context_chars)
        ctx_end = min(len(text), idx + len(term) + context_chars)
        snippet = (text[ctx_start:ctx_end] or '').replace('\n', ' ').strip()
        snippet = re.sub(r'\s+', ' ', snippet)
        contexts.append({'file': rel, 'snippet': snippet[:500]})
    return contexts


def _normalize_db_result(result):
    docs, metas = [], []
    if isinstance(result, dict) and 'documents' in result:
        d = result.get('documents') or []
        m = result.get('metadatas') or []
        if d and isinstance(d[0], list):
            docs, metas = d[0], (m[0] if m else [])
        else:
            docs, metas = d, m
    return docs, metas


def fetch_definitions(term, db, n_results=20):
    """Query DB and return [{source, score, snippet}] sorted by source
    priority (Black's first, etc.)."""
    try:
        result = db.query_text(term, n_results=n_results)
    except Exception as e:
        return [], str(e)
    docs, metas = _normalize_db_result(result)
    out = []
    term_lower = term.lower()
    for doc, meta in zip(docs, metas):
        doc_l = (doc or '').lower()
        if term_lower not in doc_l:
            continue
        src = (meta or {}).get('source', 'unknown')
        idx = doc_l.find(term_lower)
        ctx_start = max(0, idx - 200)
        ctx_end = min(len(doc), idx + len(term) + 400)
        snippet = (doc[ctx_start:ctx_end] or '').replace('\n', ' ').strip()
        snippet = re.sub(r'\s+', ' ', snippet)
        out.append({
            'source': src,
            'score': source_score(src),
            'snippet': snippet[:600],
        })
    out.sort(key=lambda d: -d['score'])
    # dedupe by source - keep top 2 snippets per source
    by_src = defaultdict(list)
    for entry in out:
        if len(by_src[entry['source']]) < 2:
            by_src[entry['source']].append(entry)
    deduped = [e for entries in by_src.values() for e in entries]
    deduped.sort(key=lambda d: -d['score'])
    return deduped, None


def llm_check_usage(term, definition, user_context, timeout=30):
    """Ask LLM: does the user's usage of `term` match the `definition`?
    Returns: 'CONSISTENT', 'INCONSISTENT', or 'AMBIGUOUS' with brief reason.
    """
    payload = {
        'model': 'qwen-default',
        'messages': [{
            'role': 'system',
            'content': 'You are a precise legal terminology auditor. Answer in EXACTLY this JSON shape: {"verdict": "CONSISTENT|INCONSISTENT|AMBIGUOUS", "reason": "<one sentence>"}. No prose, no markdown.'
        }, {
            'role': 'user',
            'content': (
                f'TERM: {term}\n\n'
                f'AUTHORITATIVE DEFINITION:\n{definition}\n\n'
                f'USER\'S USAGE CONTEXT:\n{user_context}\n\n'
                f'Is the user\'s usage consistent with the authoritative '
                f'definition? Output the JSON.'
            )
        }],
        'temperature': 0.1,
        'max_tokens': 200,
    }
    try:
        req = urllib.request.Request(
            LLM_URL,
            data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
            method='POST')
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        content = data['choices'][0]['message']['content'].strip()
        # Strip possible markdown fence
        content = re.sub(r'^```(?:json)?\s*', '', content)
        content = re.sub(r'\s*```$', '', content)
        obj = json.loads(content)
        return obj.get('verdict', 'AMBIGUOUS'), obj.get('reason', '')
    except Exception as e:
        return 'ERROR', str(e)[:120]


def render_definitions_md(case_name, term_entries):
    L = []
    L.append(f'# Definitions — {case_name}')
    L.append('')
    L.append(f'**Generated:** {datetime.now().isoformat(timespec="seconds")}')
    L.append(f'**Terms covered:** {len(term_entries)}')
    L.append('')
    L.append('> For every term used in this case, this page shows the '
             'authoritative definition (Black\'s Law, Bouvier, CJS, U.S. '
             'Code, CFR, or case law in that priority) and the contexts '
             'where you used the term in your filings. Read the definition '
             'first, then your own usage. If they don\'t match, the term '
             'is being misapplied.')
    L.append('')

    for entry in term_entries:
        term = entry['term']
        L.append(f'## `{term}`')
        L.append('')
        L.append(f'**Used in your filings:** {entry["n_filings"]} '
                 f'file(s), {entry["total_occurrences"]} occurrence(s)')
        L.append('')

        if not entry.get('definitions'):
            L.append('**No definition found in indexed sources.**')
            L.append('')
            continue

        # Group by source
        L.append('### Authoritative definition')
        L.append('')
        for d in entry['definitions'][:5]:
            L.append(f'**Source: `{d["source"]}`**')
            L.append('')
            L.append(f'> {d["snippet"]}')
            L.append('')

        # User usage
        L.append('### Your usage in this case')
        L.append('')
        if not entry.get('usage_contexts'):
            L.append('_(no quoted occurrences found in case files)_')
        for u in entry['usage_contexts'][:5]:
            L.append(f'**`{u["file"]}`:**')
            L.append('')
            L.append(f'> {u["snippet"]}')
            L.append('')

        # LLM verdict if present
        if entry.get('llm_verdict'):
            v = entry['llm_verdict']
            mark = {'CONSISTENT': '✓', 'INCONSISTENT': '✗',
                    'AMBIGUOUS': '?', 'ERROR': '!'}.get(v['verdict'], '?')
            L.append(f'### {mark} Usage check: **{v["verdict"]}**')
            L.append('')
            if v.get('reason'):
                L.append(f'_{v["reason"]}_')
            L.append('')

        L.append('---')
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


# ============================================================================
# KEYED RUNG (2026-11-10) — the statute's OWN definitions section is the controlling definition (hierarchy level 3);
# Black's / Bouvier / Am Jur / C.J.S. are level 7. Before this block definition_lookup (regulatory/mcp_tools_block_9b.py)
# went semantic/dictionary-first and never read the struct index's definition-role rows, so 'producer' in 40 P.S. § 310.1
# returned Black's before § 601-A. Order here: (1) cite → source/unit through the ONE cite→chunk path
# (pipeline.m2.path_to_law.hub_rows_for_cite → struct_pins unit rung, then ps_crosswalk rung), definition-role chunks of
# that source, defined-term match in the chunk text (verbatim sentence, chunk_id, unit, heading, means/includes scope via
# pipeline.statutory_construction.classify_definition_scope); (2) dictionary / treatise headword rows of the same index
# in SOURCE_PRIORITY order, each labelled with its level and edition; (3) a GROUND+ line in the room grammar
# (scripts/rooms/room_prefetch.py:21 — 'GROUND+: <cite> | <chunk_id> | "<≤40 words verbatim>" | source=<source> [<family>]').
# Deterministic, read-only on the corpus DBs; NOT IN CORPUS is a counted rung, never a fabricated hit.
# ============================================================================
HIERARCHY_LEVEL = {'statute_own': 3, 'dictionary': 7, 'treatise': 7}
_EDITION = {
    'blacks_law_9th': "Black's Law Dictionary, 9th ed.",
    'blacks_law_8th': "Black's Law Dictionary, 8th ed.",
    'blacks_law_4th': "Black's Law Dictionary, 4th ed.",
    'blacks_law_2nd': "Black's Law Dictionary, 2d ed.",
    'bouvier_1856_law_dictionary': "Bouvier's Law Dictionary, 1856 ed.",
    'cjs_vol_40': 'C.J.S. vol. 40',
    'cjs_vol_36': 'C.J.S. vol. 36',
    'law_dictionary_glossary': 'law dictionary glossary (edition not in slug)',
    'banking_dictionary': 'banking dictionary (edition not in slug)',
}
_QL, _QR = '["“]', '["”]'
_GROUND_WORDS = 40                                   # room_prefetch.QUOTE_WORDS


def edition_label(source):
    """Edition from the corpus slug only — never a year the slug does not carry."""
    if source in _EDITION:
        return _EDITION[source]
    sl = (source or '').lower()
    if sl.startswith('amjur2d_') or sl.startswith('amjurppf_'):
        return f'Am. Jur. 2d ({source})'
    if sl.startswith('amjur'):
        return f'Am. Jur. ({source})'
    if 'corpus_20juris_20secundum' in sl or sl.startswith('cjs'):
        return f'C.J.S. ({source})'
    return source


def source_type_of(source):
    """definition_engine.DEFINITION_PRIORITY source_type for a corpus slug."""
    sl = (source or '').lower()
    if sl.startswith('blacks_law'):
        return 'blacks_law'
    if 'bouvier' in sl:
        return 'bouviers'
    if sl.startswith(('amjur', 'cjs')) or 'juris_20secundum' in sl:
        return 'amjur_cjs'
    if sl.startswith(('cfr', 'ecfr', 'pa_code', 'njac')):
        return 'regulatory'
    if sl.startswith(('usc', 'pa_', 'nj_', 'plaw', 'comps', 'law_')):
        return 'statutory'
    return 'unknown'


def _rung_level(source):
    st = source_type_of(source)
    if st in ('statutory', 'regulatory'):
        return HIERARCHY_LEVEL['statute_own']
    if st == 'amjur_cjs':
        return HIERARCHY_LEVEL['treatise']
    return HIERARCHY_LEVEL['dictionary']


def quote_words(text, n=_GROUND_WORDS):
    """room_prefetch.quote_words (scripts/rooms/room_prefetch.py:128) — first n words, whitespace-normalised, inner double
    quotes → ' so the room's quote regex keeps one span. Copied, not imported: that file is a script, not a package."""
    words = re.sub(r'\s+', ' ', (text or '')).strip().replace('"', "'").replace('“', "'").replace('”', "'").split(' ')
    return ' '.join(words[:n])


def ground_line(request, chunk_id, verbatim, source, family):
    """room_prefetch.py:229 grammar, minus the trailing <file:line> (a lookup has no input file)."""
    return f'GROUND+: {request} | {chunk_id} | "{quote_words(verbatim)}" | source={source} [{family or "?"}]'


_STOP_MARKERS = (
    r'\n\s*["“][A-Z][^"”\n]{0,80}\.["”]',                       # next PA LRB headword:  "Insurer." ...
    r'(?i)\bthe term ["“]',                                     # next USC defined term
    r'\s\(\d+\)\s+[A-Z][a-z]+(?: [a-z]+){0,4}(?:\.\s*—|\s+(?=[A-Z][a-z]))',   # next USC numbered paragraph heading: (2) Exclusions.— / (2) Exclusions Except
    r'\s\([a-z]\)\s+[A-Z][a-z]+(?: [a-z]+){0,6}(?:\.\s*—|\s+(?=[A-Z][a-z]))',  # next USC lettered subsection heading: (e) Investigative consumer report.—
    r'\s\([a-z]\)\s+The term',                                  # next USC lettered subsection opening with a definition
    r'\(Def\.\s',                                               # PA amendment note
    r'\n\s*\n',
)


def _verbatim_from(text, start, scan_from, cap=1500):
    end = min(len(text), start + cap)
    for rx in _STOP_MARKERS:
        m = re.compile(rx).search(text, scan_from, end)
        if m and m.start() > start:
            end = min(end, m.start())
    return re.sub(r'\s+', ' ', text[start:end]).strip()


def find_defining_sentence(term, text):
    """The defined-term match inside one chunk → {verbatim, headword, form, exact} or None.
    Forms: usc_term ('The term "X" means|includes'), pa_headword ('"X." Definition.' — PA LRB style, no verb),
    subsec_dash ('(d) X.—'), bare ('"X" means|includes'). Exact headword beats a headword that merely contains the term
    ("Insurance producer" for 'producer'); then the shorter headword; then form order."""
    t = re.escape(re.sub(r'\s+', ' ', term.strip()))
    hw = rf'(?P<hw>[^"”\n]{{0,40}}?\b{t}\b[^"”\n]{{0,40}}?)'
    forms = (
        ('usc_term', rf'(?i)\b(?:the\s+)?term\s+{_QL}{hw}{_QR}\s*(?P<verb>means|includes|shall include|does not include|has the (?:same )?meaning)'),
        ('pa_headword', rf'{_QL}{hw}\.{_QR}\s+(?=\S)'),
        ('subsec_dash', rf'(?im)^\s*(?:\([a-z0-9]+\)\s*)?{hw}\.\s*—'),
        ('bare', rf'(?i){_QL}?{hw}{_QR}?\s+(?P<verb>means|includes)\b'),
    )
    cands, seen = [], set()
    for order, (form, rx) in enumerate(forms):
        for m in re.finditer(rx, text):
            if m.start() in seen:
                continue
            seen.add(m.start())
            h = re.sub(r'\s+', ' ', m.group('hw')).strip()
            exact = h.lower() == term.strip().lower()
            cands.append((0 if exact else 1, len(h), order, m, h, form, exact))
    if not cands:
        return None
    cands.sort(key=lambda c: c[:3])
    _, _, _, m, h, form, exact = cands[0]
    scan_from = m.end()
    if form == 'subsec_dash':                                   # '(d) X.— (1) In general The term "X" means ...': scan past the verb
        v = re.compile(r'(?i)\b(means|includes)\b').search(text, m.end())
        scan_from = v.end() if v else m.end()
    return {'verbatim': _verbatim_from(text, m.start(), scan_from), 'headword': h, 'form': form, 'exact': exact}


def classify_scope(headword, verbatim, section_text=''):
    """means/includes verdict from pipeline.statutory_construction.classify_definition_scope (reused, not duplicated).
    PA LRB form ('"X." A person that ...', no verb) is 'undefined' to the classifier; when the section's own preamble says the
    words 'shall have the meanings given to them in this section' the definition IS the meaning → closed, basis quoted."""
    try:
        from pipeline.statutory_construction import classify_definition_scope
        r = classify_definition_scope(headword, verbatim)
        out = {'definition_scope': r.scope, 'scope_rules': list(r.rules_applied), 'limited_to': list(r.limited_to or [])[:10],
               'excludes': list(getattr(r, 'excludes', []) or [])[:10]}
    except Exception as e:                                      # the classifier is a dependency, not a gate
        out = {'definition_scope': 'undefined', 'scope_rules': [f'classifier_error:{type(e).__name__}'], 'limited_to': [], 'excludes': []}
    if out['definition_scope'] == 'undefined':
        m = re.search(r'(?i)shall have the meanings? (?:given|ascribed) to them in this (?:section|article|act|chapter|title)', section_text or '')
        if m:
            out['definition_scope'] = 'closed'
            out['scope_rules'].append('headword_form_meanings_given_in_section_closed')
            out['scope_basis'] = re.sub(r'\s+', ' ', (section_text or '')[max(0, m.start() - 80):m.end() + 40]).strip()
    return out


def _fetch_texts(chunk_ids):
    from pipeline.m2 import path_to_law as _PTL
    return {r['chunk_id']: r for r in _PTL.fetch_chunks(list(chunk_ids))}


def statute_own_rung(term, cite=None, source=None, forum=None):
    """Rung 1 (level 3). cite → source/unit by the one cite→chunk path; then the source's definition-role chunks."""
    from pipeline.m2 import struct_pins as _SP
    rung = {'rung': 'statute_own', 'level': HIERARCHY_LEVEL['statute_own'], 'status': 'NOT IN CORPUS', 'term': term,
            'cite': cite or '', 'source': source or '', 'unit': '', 'how': '', 'chunks_searched': 0, 'reason': ''}
    cited_rows, src, unit = [], source or '', ''
    if cite:
        try:
            from pipeline.m2 import path_to_law as _PTL
            cited_rows = _PTL.hub_rows_for_cite(None, cite, forum=forum)
        except Exception as e:
            rung['reason'] = f'ERROR hub_rows_for_cite: {type(e).__name__}: {str(e)[:120]}'; rung['status'] = 'ERROR'
            return rung
        rung['how'] = getattr(_SP, 'last_rung', '') or 'hub'
        if not cited_rows:
            rung['reason'] = f'cite not keyed: {getattr(_SP, "last_reason", "") or "no rows"}'
            return rung
        src = cited_rows[0][2]
        meta = _SP.chunk_meta(cited_rows[0][0])
        unit = meta.get('unit', '')
        rung['source'], rung['unit'], rung['family'] = src, unit, meta.get('family', '')
    if not src:
        rung['reason'] = 'no cite/source given — the statute-own rung needs the enacting statute (cite= or source=)'
        try:                                                     # defs_index statute headwords (kind=statute) — a keyed check, not a guess
            import sqlite3
            p = os.environ.get('BEEZLE_DEFS_INDEX_DB', str(_eroot() / "corpus/defs_index.db"))
            if os.path.exists(p):
                d = sqlite3.connect(f'file:{p}?mode=ro', uri=True)
                hits = d.execute("SELECT term, cite FROM headwords WHERE kind='statute' AND lower(term)=? LIMIT 5", (term.lower(),)).fetchall()
                rung['defs_index_statute_headwords'] = [list(h) for h in hits]
                rung['reason'] += f'; defs_index statute headwords for {term!r}: {len(hits)}'
        except Exception as e:
            rung['reason'] += f'; defs_index check errored: {type(e).__name__}'
        return rung
    rows = _SP.definition_rows(src, unit or None)
    if not rows and cited_rows:
        rows = [{'chunk_id': r[0], 'unit': unit, 'heading': '', 'role': 'text', 'family': rung.get('family', ''), 'cited_unit': True} for r in cited_rows]
    rung['chunks_searched'] = len(rows)
    if not rows:
        rung['reason'] = f'source {src} has no definition-role rows in the struct index'
        return rung
    texts = _fetch_texts([r['chunk_id'] for r in rows])
    section_text = ' '.join(texts[r['chunk_id']]['text'] for r in rows if r.get('cited_unit') and r['chunk_id'] in texts)
    best = None
    for r in rows:
        rec = texts.get(r['chunk_id'])
        if not rec:
            continue
        hit = find_defining_sentence(term, rec['text'])
        if not hit:
            continue
        key = (0 if hit['exact'] else 1, 0 if r.get('cited_unit') else 1, len(hit['headword']), r.get('ord') or 0)
        if best is None or key < best[0]:
            best = (key, r, rec, hit)
    if best is None:
        rung['reason'] = f'{term!r} is not a defined term in {src}' + (f' (unit {unit})' if unit else '') + f' — {len(rows)} definition chunks read'
        return rung
    _, r, rec, hit = best
    fam = r.get('family') or _SP.chunk_meta(r['chunk_id']).get('family', '')
    rung.update({'status': 'FOUND', 'chunk_id': r['chunk_id'], 'source': rec['source'], 'unit': r.get('unit') or unit, 'heading': r.get('heading', ''),
                 'family': fam, 'role': r.get('role', ''), 'headword': hit['headword'], 'form': hit['form'], 'exact_headword': hit['exact'],
                 'verbatim': hit['verbatim'], 'edition': rec['source'], 'cited_unit': bool(r.get('cited_unit'))})
    rung.update(classify_scope(hit['headword'], hit['verbatim'], section_text))
    rung['ground_line'] = ground_line(cite or term, r['chunk_id'], hit['verbatim'], rec['source'], fam)
    return rung


def _dict_entry(term, text):
    """The headword's own entry inside a dictionary chunk, best-scored → (score, verbatim) or None.
    score 0 = modern Black's entry 'demurrer (di-m…r), n. …' / 'demurrer, n.' (lower-case headword + pronunciation / part of speech);
    score 1 = classic entry 'DEMURRER. In pleading. …' (upper-case headword, period, capitalised or bracketed definition);
    score 2 = glossary 'Demurrer, 1.—…'. A running head ('DEMURRER.' + newline / numbered sense) is never an entry."""
    tt = re.sub(r'\s+', ' ', term.strip())
    tl, tu, tc = re.escape(tt.lower()), re.escape(tt.upper()), re.escape(tt[:1].upper() + tt[1:].lower())
    forms = (
        (0, rf'(?m)^[ \t]*{tl}\b[ \t]*(?:,?[ \t]*\([^)\n]{{1,60}}\)|,[ \t]*(?:n|vb|adj|adv)\.)'),
        (1, rf'(?m)^[ \t]*{tu}\.[ \t]+(?=[A-Z(\[])'),
        (2, rf'(?m)^[ \t]*{tc},?[ \t]+\d\.\s*[—-]'),
    )
    best = None
    for score, rx in forms:
        for m in re.compile(rx).finditer(text):
            end = min(len(text), m.start() + 1200)
            nxt = re.compile(r'\n[ \t]*(?:[A-Z]{3,}[A-Z ,\-\'’]*\.[ \t]|[a-z][a-z\-’\']+(?: [a-z\-’\']+){0,3},? \([^)\n]{1,60}\)[,.]? ?(?:n|vb|adj|adv)?\.?\s)').search(text, m.end() + 20, end)
            if nxt:
                end = nxt.start()
            v = re.sub(r'\s+', ' ', text[m.start():end]).strip()
            if len(v) < 40:                                     # chunk tail: the entry continues in the next chunk
                continue
            key = (score, -len(v))
            if best is None or key < best[0]:
                best = (key, v)
    return (best[0][0], best[1]) if best else None


def dictionary_rungs(term):
    """Rungs 2..n (level 7): the index's headword rows for `term` (+ the chunk after a heading row — header split), one rung per
    source, SOURCE_PRIORITY order (existing table), each labelled with level + edition. A source whose keyed chunks hold no
    entry line is reported as such — never upgraded."""
    from pipeline.m2 import struct_pins as _SP
    rows = _SP.headword_rows(term)
    by_src = defaultdict(list)
    for r in rows:
        if r.get('role') in ('text', 'definition'):
            by_src[r['source']].append(r)
    out = []
    for src in sorted(by_src, key=lambda s: (-source_score(s), s)):
        rs = sorted(by_src[src], key=lambda r: (0 if (r.get('heading') or '').upper() == term.upper() else 1, r.get('ord') or 0))[:12]
        texts = _fetch_texts([r['chunk_id'] for r in rs])
        fam = rs[0].get('family', '')
        rung = {'rung': 'treatise' if fam.startswith('treatise') else 'dictionary', 'level': HIERARCHY_LEVEL['treatise' if fam.startswith('treatise') else 'dictionary'],
                'status': 'KEYED_NO_ENTRY_TEXT', 'term': term, 'source': src, 'edition': edition_label(src), 'family': fam, 'priority': source_score(src),
                'keyed_chunk_ids': [r['chunk_id'] for r in rs]}
        best = None
        for r in rs:
            rec = texts.get(r['chunk_id'])
            if not rec:
                continue
            e = _dict_entry(term, rec['text'])
            if e and (best is None or (e[0], -len(e[1])) < (best[0][0], -len(best[0][1]))):
                best = (e, r)
        if best:
            (score, v), r = best
            rung.update({'status': 'FOUND', 'chunk_id': r['chunk_id'], 'unit': r.get('unit', ''), 'heading': r.get('heading', ''), 'headword': term.upper(),
                         'verbatim': v, 'form': ('modern_entry', 'classic_entry', 'glossary_entry')[score], 'exact_headword': True,
                         'continuation': bool(r.get('continuation'))})
            rung.update(classify_scope(term, v))
            rung['ground_line'] = ground_line(term, r['chunk_id'], v, src, fam)
        else:
            rung['reason'] = 'headword keyed at ingest but no entry line in the keyed chunk text (running head / OCR split)'
        out.append(rung)
    if not out:
        out.append({'rung': 'dictionary', 'level': HIERARCHY_LEVEL['dictionary'], 'status': 'NOT IN CORPUS', 'term': term,
                    'source': '', 'edition': '', 'reason': f'no dictionary/treatise HEADWORD row for {term!r} in families {list(_SP.DICT_FAMILIES)}',
                    'editions_searched': [edition_label(s) for s in _indexed_dictionary_sources()]})
    return out


def _indexed_dictionary_sources():
    """The dictionary / treatise sources the struct index actually holds (read-only; [] when the index is absent)."""
    from pipeline.m2 import struct_pins as _SP
    s = _SP._struct()
    if s is None:
        return []
    q = ','.join('?' * len(_SP.DICT_FAMILIES))
    return [r[0] for r in s.execute(f'SELECT DISTINCT source FROM chunk_struct WHERE family IN ({q}) ORDER BY source', _SP.DICT_FAMILIES)]


def keyed_definition_lookup(term, cite=None, source=None, case_id=None, write_back=False, forum=None):
    """definition_lookup's keyed tier. Returns the tool's existing shape (found/tier/term/definition/source/scope/source_type/
    _priority_rank) plus rungs (every rung, hierarchy order, each with level/edition/status) and ground_line."""
    term = (term or '').strip()
    rungs = [statute_own_rung(term, cite=cite, source=source, forum=forum)]
    rungs += dictionary_rungs(term)
    found = next((r for r in rungs if r.get('status') == 'FOUND'), None)
    rank = {'statutory': 1, 'regulatory': 2, 'case_law': 3, 'blacks_law': 4, 'bouviers': 5, 'amjur_cjs': 6}
    res = {'found': bool(found), 'tier': 'keyed', 'term': term, 'cite': cite or '', 'rungs': rungs, 'ground_line': ''}
    if found:
        st = source_type_of(found['source'])
        res.update({'definition': found['verbatim'], 'source': found['source'], 'scope': cite or source or 'general', 'source_type': st,
                    '_priority_rank': rank.get(st, 99), 'chunk_id': found['chunk_id'], 'unit': found.get('unit', ''), 'heading': found.get('heading', ''),
                    'family': found.get('family', ''), 'rung': found['rung'], 'level': found['level'], 'edition': found.get('edition', ''),
                    'headword': found.get('headword', ''), 'form': found.get('form', ''), 'definition_scope': found.get('definition_scope', ''),
                    'scope_rules': found.get('scope_rules', []), 'scope_basis': found.get('scope_basis', ''), 'ground_line': found.get('ground_line', '')})
    else:
        res['message'] = f'NOT IN CORPUS: {term!r} is not a keyed defined term in ' + (f'{cite} / ' if cite else '') + 'any indexed dictionary'
    if write_back and found:
        try:
            res['write_back'] = write_back_keyed(term, res, case_id=case_id)
        except Exception as e:
            res['write_back'] = {'error': f'{type(e).__name__}: {str(e)[:200]}'}
    return res


# ---- write-back (shape unchanged: terms.<term> in the grounding YAML gains a `keyed:` record; the case _definitions.md gains
# ---- the keyed definition at the top of the term's "Authoritative definition" list). Surgical text edits — the YAML is
# ---- ~105 MB and is never re-dumped.
def _keyed_record(res):
    return {'rung': res.get('rung'), 'level': res.get('level'), 'chunk_id': res.get('chunk_id'), 'source': res.get('source'),
            'unit': res.get('unit', ''), 'cite': res.get('cite', ''), 'edition': res.get('edition', ''), 'definition_scope': res.get('definition_scope', ''),
            'verbatim': res.get('definition', ''), 'ground_line': res.get('ground_line', ''), 'at': datetime.now().isoformat(timespec='seconds')}


def write_back_grounding_yaml(term, res, path=None):
    path = Path(path or GROUNDING_YAML)
    if not path.exists():
        return {'yaml': str(path), 'status': 'missing'}
    text = path.read_text(encoding='utf-8')
    block = yaml.safe_dump({'keyed': _keyed_record(res)}, sort_keys=False, allow_unicode=True, default_flow_style=False, width=4000)
    block = ''.join('    ' + ln + '\n' for ln in block.rstrip('\n').split('\n'))
    key = yaml.safe_dump({term: None}, allow_unicode=True).split(':')[0]      # YAML-quoted when the term needs it
    hdr = f'  {key}:\n'
    i = text.find('\n' + hdr)
    status = 'added_term'
    if i >= 0:
        i += 1
        j = re.compile(r'\n  (?! )').search(text, i + len(hdr))              # end of this term block = next 2-space key
        j = len(text) if j is None else j.start() + 1
        seg = text[i:j]
        if '\n    keyed:\n' in seg:                                         # replace the previous keyed record only
            a = seg.index('\n    keyed:\n') + 1
            b = re.compile(r'\n    (?! )').search(seg, a)
            b = len(seg) if b is None else b.start() + 1
            seg = seg[:a] + block + seg[b:]
            status = 'replaced_keyed'
        else:
            seg = hdr + block + seg[len(hdr):]
            status = 'added_keyed'
        text = text[:i] + seg + text[j:]
    else:
        if not text.endswith('\n'):
            text += '\n'
        text += hdr + '    grounded: true\n    n_sources: 1\n    sources:\n    - ' + str(res.get('source')) + '\n' + block
        m = re.search(r'(?m)^n_terms:\s*(\d+)\s*$', text)
        if m:
            text = text[:m.start(1)] + str(int(m.group(1)) + 1) + text[m.end(1):]
    path.write_text(text, encoding='utf-8')
    return {'yaml': str(path), 'status': status}


def write_back_definitions_md(term, res, case_id):
    cd = CASES_ROOT / case_id_to_folder(case_id)
    path = cd / '_definitions.md'
    if not path.exists():
        return {'md': str(path), 'status': 'missing'}
    text = path.read_text(encoding='utf-8')
    entry = (f'**Source: `{res.get("source")}`** — rung {res.get("rung")} (hierarchy level {res.get("level")}, {res.get("edition")}), '
             f'chunk `{res.get("chunk_id")}`' + (f', unit {res.get("unit")}' if res.get('unit') else '') +
             (f', cite {res.get("cite")}' if res.get('cite') else '') + f', scope {res.get("definition_scope") or "undefined"}\n\n'
             f'> {res.get("definition")}\n\n`{res.get("ground_line")}`\n\n')
    hdr = f'## `{term}`\n'
    i = text.find(hdr)
    if i >= 0:
        j = text.find('\n## ', i + len(hdr))
        j = len(text) if j < 0 else j + 1
        seg = text[i:j]
        seg = re.sub(r'\*\*Source: `[^`]*`\*\* — rung [^\n]*\n\n> [^\n]*\n\n`GROUND\+:[^\n]*`\n\n', '', seg)   # one keyed entry per term
        k = seg.find('### Authoritative definition\n\n')
        if k >= 0:
            k += len('### Authoritative definition\n\n')
            seg = seg[:k] + entry + seg[k:]
        else:
            seg = seg.replace('**No definition found in indexed sources.**\n\n', '### Authoritative definition\n\n' + entry, 1) \
                if '**No definition found in indexed sources.**' in seg else seg + '### Authoritative definition\n\n' + entry
        text = text[:i] + seg + text[j:]
        status = 'updated_term'
    else:
        text = text.rstrip('\n') + f'\n\n{hdr}\n**Used in your filings:** 0 file(s), 0 occurrence(s) _(keyed lookup; usage not counted)_\n\n' \
               f'### Authoritative definition\n\n{entry}---\n'
        m = re.search(r'(?m)^\*\*Terms covered:\*\* (\d+)$', text)
        if m:
            text = text[:m.start(1)] + str(int(m.group(1)) + 1) + text[m.end(1):]
        status = 'added_term'
    path.write_text(text, encoding='utf-8')
    return {'md': str(path), 'status': status}


def write_back_keyed(term, res, case_id=None):
    out = {'grounding': write_back_grounding_yaml(term, res)}
    if case_id:
        out['definitions_md'] = write_back_definitions_md(term, res, case_id)
    return out
# ============================================================================
# /KEYED RUNG
# ============================================================================


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--case', help='one case ID')
    ap.add_argument('--top-terms', type=int, default=80,
                    help='cap number of terms per case')
    ap.add_argument('--n-results', type=int, default=20,
                    help='DB results per term')
    ap.add_argument('--check-usage', action='store_true',
                    help='run LLM usage consistency check (slow)')
    args = ap.parse_args()

    if not GROUNDING_YAML.exists():
        print(f'[!] {GROUNDING_YAML} not found. Run '
              f'dynamic_term_grounding.py first.')
        sys.exit(1)

    grounding = yaml.safe_load(GROUNDING_YAML.read_text(encoding='utf-8'))
    terms_data = grounding.get('terms', {})
    grounded_terms = {t: d for t, d in terms_data.items()
                      if d.get('grounded')}
    print(f'Grounded terms from inventory: {len(grounded_terms)}')

    # Pick cases
    if args.case:
        case_dirs = [CASES_ROOT / case_id_to_folder(args.case)]
    else:
        case_dirs = [CASES_ROOT / d for d in (
            'fed_26-cv-00123', 'fed_26-cv-00456',
            'fed_26-cv-00789', 'phila_ccp_260100000') if
            (CASES_ROOT / d).exists()]

    LegalDB = _find_legaldb()
    db = LegalDB()

    for cd in case_dirs:
        if not cd.exists():
            print(f'  [!] skip {cd}')
            continue
        print(f'\n== {cd.name} ==')

        # For this case, gather which grounded terms actually appear in
        # ITS filings. We use in_filings from the inventory which already
        # tells us per-term file list, but cross-check against the case
        # directory.
        term_entries = []
        for term, gdata in grounded_terms.items():
            files_used = gdata.get('in_filings') or {}
            # Filter to files belonging to this case
            in_this_case = {}
            case_files = {str(f.relative_to(cd)) for f in gather_files(cd)}
            for f, n in files_used.items():
                # files_used keys are paths relative to case dir
                if f in case_files:
                    in_this_case[f] = n
            if not in_this_case:
                continue
            term_entries.append({
                'term': term,
                'n_filings': len(in_this_case),
                'total_occurrences': sum(in_this_case.values()),
                'files_used': in_this_case,
            })

        # Sort by usage frequency
        term_entries.sort(key=lambda e: -e['total_occurrences'])
        term_entries = term_entries[:args.top_terms]
        print(f'  {len(term_entries)} terms used in this case')

        # Fetch definitions and contexts
        for i, entry in enumerate(term_entries, 1):
            term = entry['term']
            defs, err = fetch_definitions(term, db, n_results=args.n_results)
            entry['definitions'] = defs
            entry['fetch_error'] = err
            entry['usage_contexts'] = find_term_contexts(term, cd)

            # Optional LLM check
            if args.check_usage and defs and entry['usage_contexts']:
                top_def = defs[0]['snippet']
                top_usage = entry['usage_contexts'][0]['snippet']
                verdict, reason = llm_check_usage(term, top_def, top_usage)
                entry['llm_verdict'] = {'verdict': verdict, 'reason': reason}

            if i <= 10 or i % 10 == 0:
                n_defs = len(defs)
                tag = (entry.get('llm_verdict', {}).get('verdict', '')
                       if args.check_usage else '')
                print(f'  [{i:3d}/{len(term_entries)}] {term[:50]:50s} '
                      f'{n_defs} defs   {tag}')

        # Render
        md = render_definitions_md(cd.name, term_entries)
        out = cd / '_definitions.md'
        out.write_text(md, encoding='utf-8')
        print(f'  [OK] {out}')


if __name__ == '__main__':
    main()
