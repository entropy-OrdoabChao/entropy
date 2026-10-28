#!/usr/bin/env python3
"""pa_title_puller — COMPLETE Pa.C.S. backfill, title by title, from the OFFICIAL
General Assembly site. Born 2026-09-19 from the user's class correction: "I want all
the rules pulled — this is how we end up with title 20 missing a section." Never
section-picks: each title is fetched WHOLE and keyed pa_cs_t<N> so coverage is
auditable per title (the old pa_consolidated_statutes monolith is left in place;
dedupe favors the new per-title keys at retrieval because they're fresher + labeled).

Resumable: state in output/plaw_backfill/pa_titles_state.json (done/failed per title).
Self-stopping: --minutes N (default 25). Checkpoint after EVERY title.
Verify: COUNT + per-title distinctive probe (first section heading text).

Official route: https://www.legis.state.pa.us/cfdocs/legis/LI/consCheck.cfm?txtType=HTM&ttl=<N>
(whole-title HTML). Politeness: 3s between titles.
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import json, re, sys, time, sqlite3, hashlib, urllib.request
from pathlib import Path

ROOT = _eroot()
DB = str(_eroot() / "corpus/chunk_text.db")
STATE = ROOT / 'output' / 'plaw_backfill' / 'pa_titles_state.json'
RAW = ROOT / 'corpus_sources' / 'pa_statutes'
# Pa.C.S. titles actually enacted (skip reserved). Source: official title list.
TITLES = [1,2,3,4,5,7,8,9,10,11,12,13,14,15,16,17,18,20,22,23,24,25,26,27,30,32,34,
          35,37,38,40,42,44,45,46,49,51,53,54,57,58,61,62,64,66,67,68,71,74,75,79]

UA = {'User-Agent': 'Mozilla/5.0 (research; you@example.com)'}


def _load():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"done": {}, "failed": {}}


def _save(st):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=1))


def _fetch(title: int) -> str | None:
    """Whole title = TOC page + every chapter page it links (the proven per-chapter route;
    consCheck.cfm returns EMPTY — dead route, don't regress to it)."""
    base = f"https://www.legis.state.pa.us/WU01/LI/LI/CT/HTM/{title:02d}/"
    try:
        req = urllib.request.Request(base + f"{title:02d}.HTM", headers=UA)
        toc = urllib.request.urlopen(req, timeout=60).read().decode('utf-8', 'ignore')
    except Exception as e:
        print(f"  t{title} TOC error: {type(e).__name__}: {str(e)[:80]}")
        return None
    chapters = sorted(set(re.findall(r'href="?(\d\d\.\d+\.\.?HTM)"?', toc, re.I)))
    if not chapters:
        chapters = sorted(set(re.findall(r'(\d\d\.\d+\.\.HTM)', toc)))
    parts = [_strip(toc)]
    for ch in chapters:
        try:
            req = urllib.request.Request(base + ch, headers=UA)
            html = urllib.request.urlopen(req, timeout=60).read().decode('utf-8', 'ignore')
            parts.append(_strip(html))
            time.sleep(0.7)
        except Exception as e:
            print(f"  t{title}/{ch}: {type(e).__name__}")
    text = '\n\n'.join(parts)
    return text if len(text) > 15000 else None


def _strip(html: str) -> str:
    text = re.sub(r'<script.*?</script>|<style.*?</style>', ' ', html, flags=re.S | re.I)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'&nbsp;?', ' ', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n\s*\n+', '\n\n', text)
    return text


def _chunk(text: str):
    out, i = [], 0
    while i < len(text):
        piece = text[i:i + 1500]
        if len(piece) == 1500:
            cut = piece.rfind('. ')
            if cut > 900:
                piece = piece[:cut + 1]
        out.append(piece)
        i += len(piece)
    return out


def _ingest(title: int, text: str) -> int:
    key = f"pa_cs_t{title}"
    db = sqlite3.connect(DB)
    c = db.cursor()
    c.execute("DELETE FROM chunk_text WHERE source=?", (key,))
    n = 0
    for piece in _chunk(text):
        cid = hashlib.sha1(f"{key}:{n}".encode()).hexdigest()[:16]
        c.execute("INSERT INTO chunk_text (chunk_id,text,source,jurisdiction,citations) VALUES (?,?,?,?,?)",
                  (cid, piece, key, 'pa_enacted', f'{title} Pa.C.S.'))
        n += 1
    db.commit()
    # probe: the title's own heading appears
    hit = c.execute("SELECT COUNT(*) FROM chunk_text WHERE source=? AND (text LIKE '%Pa.C.S.%' OR text LIKE '%§%' OR text LIKE '%Section%')", (key,)).fetchone()[0]
    db.close()
    return n if hit else -n


def main():
    minutes = 25.0
    for i, a in enumerate(sys.argv):
        if a == '--minutes' and i + 1 < len(sys.argv):
            minutes = float(sys.argv[i + 1])
    t0 = time.monotonic()
    st = _load()
    RAW.mkdir(parents=True, exist_ok=True)
    for title in TITLES:
        if str(title) in st['done']:
            continue
        if (time.monotonic() - t0) > minutes * 60:
            print(f"SELF-STOP at {minutes}min; resume rerun continues")
            break
        print(f"pulling Pa.C.S. title {title}...")
        text = _fetch(title)
        if not text:
            st['failed'][str(title)] = st['failed'].get(str(title), 0) + 1
            _save(st)
            time.sleep(3)
            continue
        (RAW / f"pa_cs_t{title}.txt").write_text(text, encoding='utf-8')
        n = _ingest(title, text)
        st['done'][str(title)] = {'chunks': abs(n), 'probe': n > 0, 'chars': len(text)}
        st['failed'].pop(str(title), None)
        _save(st)
        print(f"  t{title}: {abs(n)} chunks probe={'OK' if n>0 else 'MISS'}")
        time.sleep(3)
    done = len(st['done'])
    print(f"STATE: {done}/{len(TITLES)} titles done; failed pending: {list(st['failed'])}")


if __name__ == '__main__':
    main()
