#!/usr/bin/env python3
"""
cite_authority_check.py - external authority lookup

For each unverified federal cite, look up the actual case via:
  - Justia.com (SCOTUS cases by X U.S. Y reporter)
  - CourtListener.com REST API (federal circuit/district cases)
  - Cornell LII as fallback for SCOTUS

For each match, classify:
  - Case name + year + court
  - Binding status given the user's forum
  - Age warnings (pre-Twombly, pre-Iqbal for pleading cites)
  - Treatment warnings if findable

Outputs obsidian_sync/_authority_report.md with full per-cite analysis.

Caches lookups in ~/.beezle_cache/authority/ so subsequent runs are fast.
"""
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import re
import json
import time
import sys
import urllib.request
import urllib.parse
import urllib.error
import argparse
from pathlib import Path
from datetime import datetime

REPO = _eroot()
SYNC = REPO / 'obsidian_sync'
CACHE_DIR = Path.home() / '.beezle_cache' / 'authority'

USER_AGENT = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

# Case-folder -> forum info for binding analysis
FORUMS = {
    'fed_26-cv-00123': {
        'court': 'E.D. Pa.', 'circuit': 'Third', 'circuit_num': '3',
        'is_federal': True,
    },
    'fed_26-cv-00456': {
        'court': 'E.D. Pa.', 'circuit': 'Third', 'circuit_num': '3',
        'is_federal': True,
    },
    'fed_26-cv-00789': {
        'court': 'E.D. Pa.', 'circuit': 'Third', 'circuit_num': '3',
        'is_federal': True,
    },
    'phila_ccp_260100000': {
        'court': 'Philadelphia CCP', 'circuit': 'PA',
        'is_federal': False,
    },
}

# Year markers for age-based warnings
TWOMBLY_YEAR = 2007  # Pleading standard before/after
IQBAL_YEAR = 2009




# === B37: CourtListener token + external_cases cache ===
import os as _os
EXTERNAL_CASES_DIR = Path.home() / '.beezle_cache' / 'external_cases'

def _load_cl_token():
    """First match wins."""
    for p in [
        Path.home() / '.beezle_cache' / 'courtlistener.token',
        Path.home() / '.courtlistener_token',
        (_eroot() / ".courtlistener.token"),
    ]:
        if p.exists():
            try:
                t = p.read_text(encoding='utf-8').strip()
                if t:
                    return t
            except Exception:
                pass
    return (_os.environ.get('COURTLISTENER_TOKEN') or '').strip() or None

_CL_TOKEN = _load_cl_token()

def _save_external_case(cite, data):
    """Persist a verified case to local cache."""
    EXTERNAL_CASES_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[^a-z0-9]+', '_', cite.lower()).strip('_')
    p = EXTERNAL_CASES_DIR / f'{safe}.json'
    try:
        record = dict(data)
        record['ingested_at'] = datetime.now().isoformat()
        record['original_cite'] = cite
        p.write_text(json.dumps(record, indent=2), encoding='utf-8')
    except Exception:
        pass

# CL rate limit state - shared across calls
_CL_LAST_CALL = [0.0]
_CL_MIN_INTERVAL = 12.5  # seconds (5 req/min)


def _cache_path(key):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[^a-z0-9]+', '_', key.lower())
    return CACHE_DIR / f'{safe}.json'


def _load_cache(key):
    p = _cache_path(key)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding='utf-8'))
        except Exception:
            return None
    return None


def _save_cache(key, data):
    p = _cache_path(key)
    try:
        p.write_text(json.dumps(data, indent=2), encoding='utf-8')
    except Exception:
        pass


def parse_cite(cite_str):
    """Classify a cite into a lookup strategy."""
    s = cite_str.strip()
    # X U.S. Y - SCOTUS
    m = re.match(r'^(\d+)\s+U\.?\s*S\.?\s+(\d+)$', s)
    if m:
        return {'kind': 'scotus', 'volume': int(m.group(1)),
                'page': int(m.group(2))}
    # X S. Ct. Y - alt SCOTUS reporter
    m = re.match(r'^(\d+)\s+S\.?\s*Ct\.?\s+(\d+)$', s)
    if m:
        return {'kind': 'scotus_alt', 'volume': int(m.group(1)),
                'page': int(m.group(2))}
    # X F.3d Y / F.2d Y / F. Y
    m = re.match(r'^(\d+)\s+F\.?\s*(2d|3d|4th)?\s+(\d+)$', s)
    if m:
        return {'kind': 'federal_reporter',
                'volume': int(m.group(1)),
                'series': m.group(2) or '1st',
                'page': int(m.group(3))}
    # X F. Supp. (2d|3d) Y
    m = re.match(r'^(\d+)\s+F\.?\s*Supp\.?\s*(2d|3d)?\s+(\d+)$', s,
                 re.IGNORECASE)
    if m:
        return {'kind': 'federal_supplement',
                'volume': int(m.group(1)),
                'series': m.group(2) or '1st',
                'page': int(m.group(3))}
    # 28 U.S.C. § X - DB-verifiable (we already check these locally)
    if re.match(r'^\d+\s+U\.?S\.?C\.?', s):
        return {'kind': 'usc'}
    if re.match(r'^\d+\s+C\.?F\.?R\.?', s):
        return {'kind': 'cfr'}
    # Federal Rules
    if re.match(r'^(Fed\.\s*R\.|F\.R\.|FRCP|FRE|FRAP|Rule\s)',
                s, re.IGNORECASE):
        return {'kind': 'federal_rule'}
    # PA state cites
    if re.search(r'(Pa\.|Penn\.|Pa\.C\.S\.|Pa\.R\.C\.P\.)', s):
        return {'kind': 'pa_state'}
    return {'kind': 'unknown'}


def lookup_justia_scotus(volume, page):
    """Scrape Justia for SCOTUS case at X U.S. Y. B36: cleaner parsing."""
    cache_key = f'justia_scotus_{volume}_{page}'
    cached = _load_cache(cache_key)
    if cached is not None:
        cached['from_cache'] = True
        return cached

    url = f'https://supreme.justia.com/cases/federal/us/{volume}/{page}/'
    result = {
        'cite': f'{volume} U.S. {page}',
        'kind': 'scotus',
        'lookup_source': 'justia',
        'url': url,
        'found': False,
    }

    try:
        req = urllib.request.Request(
            url, headers={'User-Agent': USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as resp:
            html = resp.read().decode('utf-8', errors='ignore')
    except urllib.error.HTTPError as e:
        if e.code == 404:
            result['note'] = 'not_in_justia_404'
        else:
            result['note'] = f'http_{e.code}'
        _save_cache(cache_key, result)
        return result
    except Exception as e:
        result['note'] = f'error: {type(e).__name__}: {e!s}'[:160]
        return result

    # === B36 FIX 1: Justia title uses '|' separator ===
    # Format: "Case Name | V U.S. P (YYYY) | Justia US Supreme Court Center"
    case_name = ''
    m = re.search(r'<title>([^<]+)</title>', html)
    if m:
        title = m.group(1).strip()
        # Try separators in order of likelihood
        for sep in (' | ', ' :: ', ' \u2013 ', ' - '):
            if sep in title:
                case_name = title.split(sep)[0].strip()
                break
        if not case_name:
            case_name = title  # fall back to full title

    # Strip trailing cite if it slipped through (e.g. "Foo v. Bar 550 U.S. 544")
    case_name = re.sub(r'\s+\d+\s+U\.?\s*S\.?\s+\d+\s*\(?\d{0,4}\)?\s*$',
                       '', case_name).strip()
    # Strip ", Inc." duplicates and trailing whitespace
    case_name = case_name.rstrip(',').strip()

    # === B36 FIX 2: prefer 'Decided' year over 'Argued' year ===
    year = ''
    for pat in [
        r'Decided[^<>]{0,80}?,\s*(\d{4})',     # "Decided May 21, 2007"
        r'Decided\s+[A-Z][a-z]+\s+\d+,?\s*(\d{4})',  # "Decided May 21 2007"
        r'<meta\s+property="article:published_time"\s+content="(\d{4})',
        r'Argued[^<>]{0,80}?,\s*(\d{4})',
        r'Argued\s+[A-Z][a-z]+\s+\d+,?\s*(\d{4})',
    ]:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            year = m.group(1)
            break

    if case_name and case_name not in ('Justia', 'US Supreme Court Center'):
        result['found'] = True
        result['case_name'] = case_name
        result['year'] = year or '?'
        result['court'] = 'SCOTUS'
        # B37: also persist to external_cases
        _save_external_case(f'{volume} U.S. {page}', result)

    _save_cache(cache_key, result)
    return result

def lookup_courtlistener(reporter, volume, page):
    """B38: use /citation-lookup/ POST endpoint instead of /search/.
    The search endpoint requires paid-tier auth; citation-lookup works
    on free-tier accounts with the 5/min rate limit.
    REGRESSION FIX 2026-07-06: CourtListener retired API v3 for new tokens
    ("you don't have permission to access V3 ... Please use V4 instead" -> HTTP 403).
    Endpoint bumped v3 -> v4; the citation-lookup response shape is unchanged."""
    cite_str = f'{volume} {reporter} {page}'
    cache_key = f'cl_{cite_str.replace(" ", "_").replace(".", "")}'

    cached = _load_cache(cache_key)
    # B38: only honor cache hits that were successful; retry failures
    if cached is not None and cached.get('found') is True:
        cached['from_cache'] = True
        return cached

    # Enforce 5/min rate limit (12.5s between requests)
    elapsed = time.time() - _CL_LAST_CALL[0]
    if elapsed < _CL_MIN_INTERVAL:
        wait = _CL_MIN_INTERVAL - elapsed
        print(f'   [rate-limit] waiting {wait:.1f}s for CL...')
        time.sleep(wait)
    _CL_LAST_CALL[0] = time.time()

    url = 'https://www.courtlistener.com/api/rest/v4/citation-lookup/'
    body = urllib.parse.urlencode({'text': cite_str}).encode('utf-8')
    headers = {
        'User-Agent': USER_AGENT,
        'Accept': 'application/json',
        'Content-Type': 'application/x-www-form-urlencoded',
    }
    if _CL_TOKEN:
        headers['Authorization'] = f'Token {_CL_TOKEN}'

    result = {
        'cite': cite_str,
        'kind': 'fed_lower',
        'lookup_source': 'courtlistener_citation_lookup',
        'url': url,
        'found': False,
    }

    try:
        req = urllib.request.Request(
            url, data=body, headers=headers, method='POST')
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        result['note'] = f'http_{e.code}'
        # Capture diagnostic body for visibility
        try:
            err_body = e.read().decode('utf-8', errors='ignore')[:200]
            if err_body:
                result['note'] += f' body={err_body!r}'
        except Exception:
            pass
        if e.code == 401:
            result['note'] += '_check_token'
        elif e.code == 429:
            result['note'] += '_rate_limited_wait'
        # B38: don't cache transient failures
        return result
    except Exception as e:
        result['note'] = f'error: {type(e).__name__}: {e!s}'[:200]
        return result

    # Response is a list of per-citation lookups
    if isinstance(data, list) and data:
        first_lookup = data[0]
        status = first_lookup.get('status')
        clusters = first_lookup.get('clusters') or []
        if clusters:
            c = clusters[0]
            result['found'] = True
            result['case_name'] = (c.get('case_name_short')
                                   or c.get('case_name')
                                   or c.get('case_name_full') or '?')
            date_filed = c.get('date_filed') or ''
            result['year'] = date_filed[:4] if date_filed else '?'
            # Court from reporter (CL response doesn't always include
            # docket court in this endpoint without follow-up)
            if reporter == 'U.S.':
                result['court'] = 'SCOTUS'
            elif 'F.' in reporter and 'Supp' not in reporter:
                result['court'] = 'U.S. Court of Appeals'
            elif 'Supp' in reporter:
                result['court'] = 'U.S. District Court'
            else:
                result['court'] = '?'
            absolute_url = c.get('absolute_url', '')
            if absolute_url:
                result['absolute_url'] = (
                    f'https://www.courtlistener.com{absolute_url}')
            _save_external_case(cite_str, result)
            _save_cache(cache_key, result)
            return result
        elif status:
            result['note'] = f'cl_status_{status}'
            if status == 404:
                # Definitively not in CL - cache this
                _save_cache(cache_key, result)
        else:
            result['note'] = 'cl_no_clusters'
    else:
        result['note'] = 'cl_empty_response'

    return result

def binding_analysis(case_court, case_year, forum_info, motion_name,
                     cite_str=''):
    """Given the case's court and the user's forum, return binding status.
    B36: skip PRE_TWOMBLY for Twombly + Iqbal themselves."""
    flags = []
    if not case_court:
        return ('UNKNOWN_COURT', flags)
    cc = (case_court or '').lower()

    is_scotus = 'scotus' in cc or 'supreme' in cc
    is_3rd_cir = '3rd' in cc or 'third circuit' in cc or 'ca3' in cc
    is_other_cir = 'circuit' in cc and not is_3rd_cir
    is_dist = 'district' in cc or 'd.' in cc

    if forum_info.get('is_federal'):
        if is_scotus:
            status = 'BINDING_SCOTUS'
        elif is_3rd_cir:
            status = 'BINDING_3D_CIR'
        elif is_other_cir:
            status = 'PERSUASIVE_OTHER_CIR'
        elif is_dist:
            status = 'PERSUASIVE_DISTRICT'
        else:
            status = 'PERSUASIVE'
    else:
        if is_scotus:
            status = 'BINDING_FED_QUESTIONS_ONLY'
        elif 'pa.' in cc or 'pennsylvania' in cc:
            status = 'BINDING_PA' if 'supreme' in cc else 'PERSUASIVE_PA'
        else:
            status = 'PERSUASIVE_FED'

    # B36 sentinels: skip self-flagging
    SELF_PLEADING_CITES = {
        '550 U.S. 544',  # Twombly
        '556 U.S. 662',  # Iqbal
    }

    try:
        y = int(case_year)
        if (cite_str not in SELF_PLEADING_CITES and
            ('motion_to_dismiss' in motion_name.lower() or
             'opposition' in motion_name.lower()) and
                y < TWOMBLY_YEAR):
            flags.append(f'PRE_TWOMBLY({y})')
        if y < 1950:
            flags.append('OLD_PRE_1950')
    except (TypeError, ValueError):
        pass

    return (status, flags)

def parse_verification_report(case_folder):
    """Parse per-case verification report, extract all cites with status."""
    path = SYNC / 'cases' / case_folder / '_draft_verification.md'
    if not path.exists():
        return []
    text = path.read_text(encoding='utf-8')
    cites = []
    current_motion = None
    for line in text.splitlines():
        m = re.match(r'### `(.+?)`', line)
        if m:
            current_motion = m.group(1)
            continue
        m = re.match(r'\| `([^`]+)` \|\s*(\S+)\s*(?:PA)?\s*\|', line)
        if m and current_motion:
            cite = m.group(1)
            status_char = m.group(2)
            status_map = {
                '✅': 'verified',
                '⚠️': 'unverified',
                '🟡': 'pa_gap',
                '🚫': 'flagged',
            }
            status = status_map.get(status_char, 'unknown')
            cites.append({
                'cite': cite,
                'motion': current_motion,
                'status': status,
            })
    return cites


# === B39: Google Scholar fallback ===
_GS_LAST_CALL = [0.0]
_GS_MIN_INTERVAL = 5.0  # 5s between Scholar queries to avoid captcha


def lookup_google_scholar(cite_str):
    """B39: fallback when CL refuses (e.g. new-user 403).
    Searches scholar.google.com/scholar with the quoted cite and parses
    the first result. May trigger captcha if used aggressively."""
    cache_key = f'gs_{cite_str.replace(" ", "_").replace(".", "")}'
    cached = _load_cache(cache_key)
    if cached is not None and cached.get('found') is True:
        cached['from_cache'] = True
        return cached

    # Rate limit
    elapsed = time.time() - _GS_LAST_CALL[0]
    if elapsed < _GS_MIN_INTERVAL:
        wait = _GS_MIN_INTERVAL - elapsed
        print(f'   [rate-limit] waiting {wait:.1f}s for Scholar...')
        time.sleep(wait)
    _GS_LAST_CALL[0] = time.time()

    quoted = f'"{cite_str}"'
    params = {'q': quoted, 'hl': 'en', 'as_sdt': '2006'}
    url = ('https://scholar.google.com/scholar?'
           + urllib.parse.urlencode(params))

    result = {
        'cite': cite_str,
        'lookup_source': 'google_scholar',
        'url': url,
        'found': False,
    }

    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': USER_AGENT,
            'Accept': 'text/html,application/xhtml+xml',
            'Accept-Language': 'en-US,en;q=0.9',
        })
        with urllib.request.urlopen(req, timeout=25) as resp:
            html = resp.read().decode('utf-8', errors='ignore')
    except urllib.error.HTTPError as e:
        result['note'] = f'http_{e.code}'
        if e.code in (429, 503):
            result['note'] += '_scholar_rate_limited'
        return result
    except Exception as e:
        result['note'] = f'error: {type(e).__name__}: {e!s}'[:160]
        return result

    # Captcha detection
    if ('/sorry/index' in html or 'unusual traffic' in html.lower()
            or 'recaptcha' in html.lower()):
        result['note'] = 'scholar_captcha_blocked'
        return result

    # Parse first result. gs_rt = result title, gs_a = author/citation line
    title_m = re.search(
        r'<h3 class="gs_rt"[^>]*>.*?<a[^>]*>(.*?)</a>',
        html, re.DOTALL)
    if not title_m:
        # No results
        result['note'] = 'scholar_no_results'
        _save_cache(cache_key, result)
        return result

    case_name = re.sub(r'<[^>]+>', '', title_m.group(1)).strip()
    case_name = re.sub(r'^\[[^\]]+\]\s*', '', case_name).strip()
    # Strip leading citation prefix some results have
    case_name = re.sub(r'^\d+\s+\w[\w.\s]+\d+\s*[-:]\s*', '',
                       case_name).strip()

    court = ''
    year = ''
    gs_a_m = re.search(r'<div class="gs_a">(.*?)</div>', html, re.DOTALL)
    if gs_a_m:
        gs_a_text = re.sub(r'<[^>]+>', '', gs_a_m.group(1)).strip()
        # Typical: "CITE - Court of Appeals, 3rd Circuit, 2008 - source"
        ym = re.search(r'\b(19[5-9]\d|20[0-2]\d)\b', gs_a_text)
        if ym:
            year = ym.group(1)
        parts = [p.strip() for p in gs_a_text.split(' - ')]
        if len(parts) >= 2:
            court_part = parts[1]
            court = re.sub(r',\s*\d{4}\s*$', '', court_part).strip()

    if case_name and len(case_name) > 2:
        result['found'] = True
        result['case_name'] = case_name
        result['court'] = court or 'Scholar-parsed'
        result['year'] = year or '?'
        _save_external_case(cite_str, result)

    _save_cache(cache_key, result)
    return result


def process_case(case_folder, forum_info, limit=None,
                 only_unverified=True, quiet=False):
    """B39: federal_reporter/supplement now falls back to Scholar
    when CL refuses (e.g. new-user 403)."""
    cites = parse_verification_report(case_folder)
    if only_unverified:
        cites = [c for c in cites if c['status'] == 'unverified']
    if limit:
        cites = cites[:limit]
    results = []
    for i, c in enumerate(cites, 1):
        parsed = parse_cite(c['cite'])
        kind = parsed.get('kind')

        if kind == 'scotus':
            lookup = lookup_justia_scotus(parsed['volume'], parsed['page'])
            if not lookup.get('from_cache'):
                time.sleep(0.6)
        elif kind == 'federal_reporter':
            rep = {'1st': 'F.', '2d': 'F.2d',
                   '3d': 'F.3d', '4th': 'F.4th'}.get(
                parsed['series'], 'F.')
            lookup = lookup_courtlistener(rep, parsed['volume'],
                                          parsed['page'])
            # B39: Scholar fallback when CL refuses
            if (not lookup.get('found') and
                ('403' in str(lookup.get('note', '')) or
                 'new user' in str(lookup.get('note', '')).lower())):
                if not quiet:
                    print(f'   [cl refused, trying scholar for '
                          f'{c["cite"]}]')
                scholar = lookup_google_scholar(c['cite'])
                if scholar.get('found'):
                    cl_note = lookup.get('note', '')[:60]
                    scholar['cl_fallback_note'] = (
                        f'CL refused ({cl_note}); Scholar resolved')
                    lookup = scholar
        elif kind == 'federal_supplement':
            rep = {'1st': 'F. Supp.', '2d': 'F. Supp. 2d',
                   '3d': 'F. Supp. 3d'}.get(parsed['series'],
                                            'F. Supp.')
            lookup = lookup_courtlistener(rep, parsed['volume'],
                                          parsed['page'])
            if (not lookup.get('found') and
                ('403' in str(lookup.get('note', '')) or
                 'new user' in str(lookup.get('note', '')).lower())):
                if not quiet:
                    print(f'   [cl refused, trying scholar for '
                          f'{c["cite"]}]')
                scholar = lookup_google_scholar(c['cite'])
                if scholar.get('found'):
                    cl_note = lookup.get('note', '')[:60]
                    scholar['cl_fallback_note'] = (
                        f'CL refused ({cl_note}); Scholar resolved')
                    lookup = scholar
        elif kind in ('usc', 'cfr', 'federal_rule'):
            lookup = {'kind': kind, 'found': None,
                      'note': 'verifiable_via_db_not_external',
                      'cite': c['cite']}
        elif kind == 'pa_state':
            lookup = {'kind': kind, 'found': None,
                      'note': 'pa_state_manual_check_required',
                      'cite': c['cite']}
        else:
            lookup = {'kind': 'unknown', 'found': None,
                      'note': 'unrecognized_cite_format',
                      'cite': c['cite']}

        binding, flags = ('N/A', [])
        if lookup.get('found'):
            binding, flags = binding_analysis(
                lookup.get('court', ''), lookup.get('year', ''),
                forum_info, c['motion'], cite_str=c['cite'])

        merged = {
            'case_folder': case_folder,
            'motion': c['motion'],
            'original_cite': c['cite'],
            'verifier_status': c['status'],
            'parsed_kind': kind,
            'external_found': lookup.get('found'),
            'external_case_name': lookup.get('case_name', ''),
            'external_year': lookup.get('year', ''),
            'external_court': lookup.get('court', ''),
            'external_source': lookup.get('lookup_source', ''),
            'external_note': lookup.get('note', ''),
            'binding_status': binding,
            'binding_flags': flags,
        }
        results.append(merged)
        if not quiet:
            sym = ('✅' if merged['external_found']
                   else ('❌' if merged['external_found'] is False
                         else '·'))
            name = (merged['external_case_name'] or
                    merged['external_note'] or '')[:50]
            src = merged['external_source'][:8] if merged['external_source']                 else ''
            print(f'   {sym} {c["cite"]:22s} {kind:18s} '
                  f'{src:9s} {name}')
    return results

def render_report(all_results, out_path):
    """Write a markdown authority report."""
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write('# Cite Authority Report\n\n')
        f.write(f'_Generated: {datetime.now().strftime("%Y-%m-%d %H:%M")}_\n\n')
        f.write('External lookup of unverified federal cites via public ')
        f.write('sources (Justia for SCOTUS, CourtListener for circuit/district). ')
        f.write('Confirms cite is a real published case AND determines binding ')
        f.write('status given each case\'s forum.\n\n')
        f.write('## Legend\n\n')
        f.write('- **External found** ✅: cite resolves to a real published case\n')
        f.write('- **External found** ❌: cite does not appear in external sources '
                '(likely fabricated or wrong format)\n')
        f.write('- **External found** ·: not externally checkable (USC, CFR, '
                'rules, PA-state cites)\n')
        f.write('- **Binding** `BINDING_SCOTUS`: SCOTUS, binding everywhere\n')
        f.write('- **Binding** `BINDING_3D_CIR`: Third Circuit, binding in E.D. Pa.\n')
        f.write('- **Binding** `PERSUASIVE_OTHER_CIR`: another federal circuit, '
                'persuasive only\n')
        f.write('- **Binding** `PERSUASIVE_DISTRICT`: district court, persuasive only\n')
        f.write('- **Flags** `PRE_TWOMBLY(YYYY)`: pleading-related case decided before '
                f'{TWOMBLY_YEAR}; check Twombly/Iqbal preemption\n\n')

        # Group by case
        by_case = {}
        for r in all_results:
            by_case.setdefault(r['case_folder'], []).append(r)

        # Summary table
        f.write('## Summary by case\n\n')
        f.write('| Case | Cites checked | External ✅ | External ❌ | Skipped |\n')
        f.write('|---|---:|---:|---:|---:|\n')
        for cf, rs in by_case.items():
            checked = len(rs)
            found = sum(1 for r in rs if r['external_found'] is True)
            not_found = sum(1 for r in rs if r['external_found'] is False)
            skipped = sum(1 for r in rs if r['external_found'] is None)
            f.write(f'| `{cf}` | {checked} | {found} | {not_found} | '
                    f'{skipped} |\n')
        f.write('\n')

        # Detailed per-case
        for cf, rs in by_case.items():
            f.write(f'\n## `{cf}`\n\n')
            f.write('| Motion | Cite | ✓ | Case name | Year | Court | '
                    'Binding | Flags |\n')
            f.write('|---|---|---|---|---|---|---|---|\n')
            for r in sorted(rs, key=lambda x: (x['motion'], x['original_cite'])):
                sym = ('✅' if r['external_found'] is True
                       else ('❌' if r['external_found'] is False else '·'))
                name = (r['external_case_name']
                        or (r['external_note'][:40] if r['external_note']
                            else ''))
                if len(name) > 45:
                    name = name[:42] + '...'
                mot = r['motion']
                if len(mot) > 50:
                    mot = mot[:47] + '...'
                f.write(f"| {mot} | `{r['original_cite']}` | {sym} | "
                        f"{name} | {r['external_year']} | "
                        f"{r['external_court'][:25]} | "
                        f"{r['binding_status']} | "
                        f"{', '.join(r['binding_flags'])} |\n")

            # Per-case hallucination warning if any
            hallu = [r for r in rs if r['external_found'] is False]
            if hallu:
                f.write('\n### ⚠️ Possible fabrications in this case\n\n')
                f.write('The following cites did not resolve via external '
                        'sources. Manually verify on Google Scholar before '
                        'filing:\n\n')
                for r in hallu:
                    f.write(f"- `{r['original_cite']}` in "
                            f"`{r['motion']}`\n")

        f.write('\n---\n')
        f.write('Generated by `cite_authority_check.py`. Cached lookups in '
                '`~/.beezle_cache/authority/`. Delete cache to force re-fetch.\n')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--case', help='single case folder')
    ap.add_argument('--limit', type=int, default=None,
                    help='limit cites per case (for testing)')
    ap.add_argument('--all-cites', action='store_true',
                    help='check ALL cites not just unverified ones')
    args = ap.parse_args()

    cases = [args.case] if args.case else list(FORUMS.keys())
    print('=' * 71)
    print(' Cite Authority Check (external lookup)')
    print('=' * 71)
    print(f' Cache dir: {CACHE_DIR}')
    print(f' Cases: {cases}')
    print()

    all_results = []
    for cf in cases:
        forum = FORUMS.get(cf, {'is_federal': True, 'circuit': 'Third'})
        print(f'== {cf} ({forum.get("court", "?")}, '
              f'{forum.get("circuit", "?")} Cir.)')
        rs = process_case(cf, forum, limit=args.limit,
                          only_unverified=not args.all_cites)
        all_results.extend(rs)
        print(f'   {len(rs)} cites processed')
        print()

    out = SYNC / '_authority_report.md'
    render_report(all_results, out)

    found = sum(1 for r in all_results if r['external_found'] is True)
    not_found = sum(1 for r in all_results if r['external_found'] is False)
    skipped = sum(1 for r in all_results if r['external_found'] is None)
    print(f'[OK] Report -> {out}')
    print(f'     Total: {len(all_results)} cites')
    print(f'     ✅ Externally found: {found}')
    print(f'     ❌ Not found (likely fabricated): {not_found}')
    print(f'     · Skipped (USC/CFR/rules/PA): {skipped}')


if __name__ == '__main__':
    main()
