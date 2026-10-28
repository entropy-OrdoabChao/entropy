"""
ECF_RESOLVER_V1 (2026-06-16)
Maps each case source document to its REAL ECF/docket number so the drafter
cites "(ECF No. N)" instead of guessing (the all-130 bug) or leaking the raw
filename. The number is recovered, in priority order, from:
  1. an 'ecf N' token in the filename                      (scanner temp names)
  2. the CM/ECF stamp in the PDF content header            ("...Document N Filed...")
  3. the per-case CASE_TIMELINE.json docket field           (canonical names)
NEVER fabricates: a doc with no recoverable number resolves to None, and the
drafter renders "(record)" for it (R7-safe). Result is cached at
output/audit/ecf_map_<slug>.json so the drafter run isn't slowed by re-parsing.
"""
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import json, re, subprocess
from pathlib import Path

ROOT = _eroot()
_HDR = re.compile(r'Document\s+(\d+)(?:-\d+)?\s+Filed', re.I)
_CASENO = re.compile(r'(\d:\d{2}-cv-\d{4,6})', re.I)
_FNECF = re.compile(r'ecf\s*(\d+)', re.I)
_EXT = re.compile(r'\.(pdf|docx?|md|txt)$', re.I)


def _case_no_from_id(case_id):
    """'fed:2:26-cv-00456' -> '2:26-cv-00456' (None for PA/state, no CM/ECF)."""
    m = _CASENO.search(str(case_id))
    return m.group(1).lower() if m else None


def _slug(case_id):
    return str(case_id).replace(':', '_').replace('/', '_')


def _win_to_wsl(p):
    p = str(p).replace('\\', '/')
    if re.match(r'^[A-Za-z]:/', p):
        p = '/mnt/' + p[0].lower() + p[2:]
    return p


def _stem(name):
    return _EXT.sub('', Path(str(name).replace('\\', '/')).name).strip().lower()


def _ocr_first_page(path):
    """OCR page 1 only (the CM/ECF stamp lives there) for image-only PDFs that
    pdftotext can't read. pdftoppm -> tesseract; both already in the WSL env."""
    import tempfile, os
    try:
        with tempfile.TemporaryDirectory() as td:
            base = os.path.join(td, 'pg')
            subprocess.run(['pdftoppm', '-png', '-f', '1', '-l', '1', '-r', '150',
                            str(path), base], capture_output=True, timeout=90)
            pngs = [os.path.join(td, f) for f in os.listdir(td) if f.endswith('.png')]
            if not pngs:
                return ''
            r = subprocess.run(['tesseract', pngs[0], '-', '--psm', '6'],
                               capture_output=True, text=True, timeout=90, errors='ignore')
            return r.stdout or ''
    except Exception:
        return ''


def _pdf_header(path, allow_ocr=True):
    """Extract (case_no, ecf_int) from the CM/ECF stamp on the first 2 pages.
    Falls back to OCR of page 1 for image-only PDFs. Either element may be None.
    case_no lets the caller reject docs that belong to a different case."""
    try:
        out = subprocess.run(
            ['pdftotext', '-f', '1', '-l', '2', str(path), '-'],
            capture_output=True, text=True, timeout=30, errors='ignore').stdout or ''
    except Exception:
        out = ''
    cm = _CASENO.search(out)
    dm = _HDR.search(out)
    if allow_ocr and not (cm and dm):
        o2 = _ocr_first_page(path)
        if o2:
            cm = cm or _CASENO.search(o2)
            dm = dm or _HDR.search(o2)
    return (cm.group(1).lower() if cm else None,
            int(dm.group(1)) if dm else None)


def _timeline_map(case_slug):
    m = {}
    tl = ROOT / 'obsidian_sync/cases' / case_slug / 'CASE_TIMELINE.json'
    if tl.exists():
        try:
            d = json.loads(tl.read_text(encoding='utf-8'))
        except Exception:
            return m
        for e in (d.get('entries', []) if isinstance(d, dict) else d):
            if not isinstance(e, dict):
                continue
            fn = str(e.get('filename') or '').strip()
            if not fn:
                continue
            st = _stem(fn)
            dk = e.get('docket')
            if isinstance(dk, int):
                m[st] = dk
            mm = _FNECF.search(fn)
            if mm:
                m[st] = int(mm.group(1))
    return m


def build_ecf_map(case_id, refresh=False, verbose=False):
    """stem(lower) -> ECF int for every source doc in the case audit."""
    slug = _slug(case_id)
    cache = ROOT / 'output/audit' / f'ecf_map_{slug}.json'
    if cache.exists() and not refresh:
        try:
            return json.loads(cache.read_text(encoding='utf-8'))
        except Exception:
            pass

    audit = ROOT / 'output/audit' / f'data_{slug}.json'
    data = json.loads(audit.read_text(encoding='utf-8'))

    # stem -> full WSL path (scorecard carries full Windows paths)
    paths = {}
    sc = data.get('scorecard') or {}
    for v in sc.get('violations', []):
        sf = v.get('source_file')
        if sf:
            wp = _win_to_wsl(sf)
            paths.setdefault(_stem(sf), wp)
    # EXPAND: header-extract every sibling PDF in the directories the case
    # actually references -- catches docs the audit names by temp/basename only.
    for wp in list(paths.values()):
        try:
            for f in Path(wp).parent.glob('*.pdf'):
                paths.setdefault(_stem(f.name), str(f))
        except Exception:
            pass
    # procedural_violations: basename + any _3f_ecf already enriched
    prebaked = {}
    for x in data.get('procedural_violations', []):
        src = x.get('source_doc') or x.get('file')
        ecf = x.get('_3f_ecf')
        if src and isinstance(ecf, int):
            prebaked[_stem(src)] = ecf

    tl = _timeline_map(slug)
    target = _case_no_from_id(case_id)   # e.g. '2:26-cv-00456' (None for PA)
    out = {}
    for st in set(list(paths) + list(prebaked) + list(tl)):
        # 1 PDF content header (authoritative; validates the case number)
        wp = paths.get(st)
        if wp and wp.lower().endswith('.pdf') and Path(wp).exists():
            # OCR off by default: 14min for +1 doc proved it's a data ceiling,
            # not a method gap. pdftotext header + case-validation is fast/reliable.
            cno, n = _pdf_header(wp, allow_ocr=False)
            if cno and target and cno != target:
                continue   # belongs to ANOTHER case -> never map (anti-contamination)
            if n and (not target or not cno or cno == target):
                out[st] = n
                if verbose:
                    print(f"  header ECF {n} <- {Path(wp).name}")
                continue
        # 2 filename 'ecf N' token (only when header gave nothing)
        mm = _FNECF.search(st)
        if mm:
            out[st] = int(mm.group(1)); continue
        # 3 pre-baked _3f_ecf
        if st in prebaked:
            out[st] = prebaked[st]; continue
        # 4 timeline docket
        if st in tl:
            out[st] = tl[st]

    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(out, indent=1), encoding='utf-8')
    return out


def resolve(filename, ecf_map):
    """Return ECF int for a filename, or None (never fabricate)."""
    if not filename:
        return None
    mm = _FNECF.search(str(filename))
    if mm:
        return int(mm.group(1))
    return ecf_map.get(_stem(filename))


# ── TIE_DOCKET: deterministic docket drop into CASE_TIMELINE.json ──────────────
_FILED_DATE = re.compile(r'\bFiled\s+(\d{1,2}/\d{1,2}/\d{2,4})', re.I)
_DKT_FN     = re.compile(r'\bdkt[_\- ]?(\d{1,4})\b', re.I)
_DOC_TITLE  = re.compile(
    r'^\s*((?:DEFENDANT|PLAINTIFF)\b[^\n]*?\b(?:MOTION|RESPONSE|BRIEF|OPPOSITION|MEMORANDUM|PETITION)\b[^\n]*'
    r'|MOTION\s+(?:FOR|TO)\s+[A-Z][^\n]{2,60}'
    r'|RESPONSE\s+IN\s+[A-Z][^\n]{2,50})$', re.I | re.M)


def _iso(mdy):
    m = re.match(r'(\d{1,2})/(\d{1,2})/(\d{2,4})', (mdy or '').strip())
    if not m:
        return ''
    mo, d, y = m.groups()
    y = int(y) + (2000 if int(y) < 100 else 0)
    return f'{y:04d}-{int(mo):02d}-{int(d):02d}'


def tie_docket(case_id, verbose=False):
    """DETERMINISTIC docket drop. Scan the case intake's OCR'd filings, recover each ECF# from the
    CM/ECF 'Document N Filed' header (or 'dkt_N' filename), and merge {docket,date_filed,type,filename}
    into CASE_TIMELINE.json so m0's DOCKET HIGHLIGHTS + ECF citations tie to the real docket. Incremental
    (existing entries preserved). Runs as the deterministic step right after the case-facts reasoning.
    Returns the list of ECF#s added. Never fabricates: a file with no recoverable number is skipped."""
    # case-FOLDER slug drops the district prefix (fed:2:26-cv-00123 -> fed_26-cv-00123), unlike _slug()
    # which keeps it for the audit/output files (data_fed_2_26-cv-00123.json).
    parts = str(case_id).split(':')
    if case_id.startswith('fed:'):
        slug = 'fed_' + parts[-1]
    elif case_id.startswith('phila_ccp:'):
        slug = 'phila_ccp_' + parts[-1]
    elif case_id.startswith('pa_super:'):
        slug = 'pa_super_' + parts[-1]
    else:
        slug = _slug(case_id)
    base = ROOT / 'obsidian_sync/cases' / slug / 'intake'
    tl_path = ROOT / 'obsidian_sync/cases' / slug / 'CASE_TIMELINE.json'
    if not base.exists():
        return []
    data = {'entries': []}
    if tl_path.exists():
        try:
            data = json.loads(tl_path.read_text(encoding='utf-8'))
        except Exception:
            data = {'entries': []}
    entries = data.get('entries', []) if isinstance(data, dict) else data
    have = {str(e.get('docket')) for e in entries if isinstance(e, dict) and e.get('docket') is not None}
    target = _case_no_from_id(case_id)

    files = sorted(set(list(base.glob('*.ocr.txt')) + list(base.glob('*.txt'))
                       + list((base / '_ocr').glob('*.txt'))))
    added = []
    for p in files:
        try:
            txt = p.read_text(encoding='utf-8', errors='ignore')
        except Exception:
            continue
        dm = _HDR.search(txt)
        ecf = int(dm.group(1)) if dm else None
        if ecf is None:
            fm = _DKT_FN.search(p.name)
            ecf = int(fm.group(1)) if fm else None
        if ecf is None or str(ecf) in have:
            continue
        cm = _CASENO.search(txt)
        if target and cm and cm.group(1).lower() != target:
            continue  # belongs to a different case
        fd = _FILED_DATE.search(txt)
        date = _iso(fd.group(1)) if fd else ''
        tm = _DOC_TITLE.search(txt)
        title = re.sub(r'\s+', ' ', tm.group(1)).strip()[:80] if tm else p.stem
        entries.append({'docket': str(ecf), 'date_filed': date, 'type': title, 'filename': p.name})
        have.add(str(ecf))
        added.append(ecf)
        if verbose:
            print(f"  tied ECF {ecf}: {title[:48]} | {date} | {p.name}")
    # FILER: REUSE the existing docket_index (ECF -> filer, parsed from the NEF 'filed by X' + docket OCR) —
    # do NOT re-extract. This binds each reply to the RIGHT movant; the real gap was that tie_docket never
    # carried filer into CASE_TIMELINE (docket_index's index only fed who_to_notify). Never overwrites a filer.
    filers_set = 0
    try:
        from pipeline.docket_index import build as _docket_build
        di = _docket_build(case_id) or {}
        for e in entries:
            if isinstance(e, dict) and not e.get('filer'):
                f = (di.get(str(e.get('docket'))) or {}).get('filer')
                if f:
                    e['filer'] = str(f).strip(' ,')[:60]
                    filers_set += 1
    except Exception:
        pass
    if added or filers_set:
        if isinstance(data, dict):
            data['entries'] = entries
        else:
            data = entries
        tl_path.write_text(json.dumps(data, indent=2, default=str), encoding='utf-8')
    return added


if __name__ == '__main__':
    import sys
    cid = sys.argv[1] if len(sys.argv) > 1 else 'fed:2:26-cv-00456'
    if '--tie-docket' in sys.argv:
        added = tie_docket(cid, verbose=True)
        print(f"\ntie_docket {cid}: +{len(added)} ECF#(s) into CASE_TIMELINE.json")
    else:
        emap = build_ecf_map(cid, refresh=True, verbose=True)
        print(f"\nECF map for {cid}: {len(emap)} docs resolved")
