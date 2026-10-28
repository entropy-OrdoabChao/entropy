#!/usr/bin/env python3
"""case_state.py — the case-management engine (Entropy copy).

One command compiles, for EVERY case, a single CASE_STATE.md answering:
posture | asset/claim | last court event | what we filed | what's mailed/pending |
open deadlines | next action. Sources (all local, no website access):
  - vault/cases/<slug>/CASE_FACTS.md      (facts base)
  - vault/cases/<slug>/CASE_TIMELINE.json (events)
  - output/audit/mailing_tracker.json             (what actually mailed)
  - efiling/letterstream_outbox/**                (.ALREADY_FILED / pending jobs)
  - vault/emails_full/*                   (court acceptance/notice emails)
Also writes vault/CASES_MOC.md — the one-screen map of contents that
boot-warm sessions read FIRST so chat never needs case clarification.
"""
import json, re, time
from pathlib import Path

from pipeline import paths as _P
ROOT = _P.HOME
CASES = _P.CASES
TRACKER = _P.TRACKER
OUTBOX = _P.OUTBOX
EMAILS = _P.EMAILS

# registry: slug -> (case id, court, one-line what-it-is), data-driven from cases_index/case_registry.json (R2: no hardcoded cases)
def _registry():
    try:
        reg = json.loads(_P.REGISTRY.read_text(encoding='utf-8'))
    except Exception:
        return {}
    out = {}
    for cid, e in reg.items():
        if not isinstance(e, dict):
            continue
        slug = e.get('slug') or cid.replace(':', '_')
        what = (e.get('case_type') or '').replace('_', ' ')
        if e.get('status'):
            what = f"{what} — {str(e['status']).replace('_', ' ')}" if what else str(e['status']).replace('_', ' ')
        out[slug] = (cid.split(':', 1)[-1], e.get('court_name') or e.get('case_court') or '?', what or 'no summary yet')
    return out
REGISTRY = _registry()


def read_json(p):
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def timeline_tail(slug, n=5):
    t = read_json(CASES / slug / 'CASE_TIMELINE.json')
    if not t:
        return []
    entries = t.get('entries', t if isinstance(t, list) else [])
    for e in entries:
        if isinstance(e, dict) and not e.get('date') and e.get('date_filed'):
            e['date'] = e['date_filed']
        if isinstance(e, dict) and not e.get('description'):
            e['description'] = e.get('filename') or ''
    dated = [e for e in entries if isinstance(e, dict) and e.get('date')]
    dated.sort(key=lambda e: str(e.get('date')))
    return dated[-n:]


def tracker_entries(case_id):
    mt = read_json(TRACKER) or {}
    txt = json.dumps(mt)
    out = []
    def walk(node, path=''):
        if isinstance(node, dict):
            s = json.dumps(node)
            if case_id in s and ('ls_job' in node or 'status' in node or 'history' in node):
                out.append(node)
                return
            for k, v in node.items():
                walk(v, path + '/' + str(k))
        elif isinstance(node, list):
            for v in node:
                walk(v, path)
    walk(mt)
    return out[:12]


def outbox_state(slug_hints):
    rows = []
    if not OUTBOX.exists():
        return rows
    for job in OUTBOX.glob('*/*/job.json'):
        d = job.parent
        txt = job.read_text(errors='replace')
        if not any(h.lower() in (str(d).lower() + txt.lower()) for h in slug_hints):
            continue
        state = 'MAILED' if (d / '.ALREADY_FILED').exists() else (
                'DO-NOT-MAIL' if (d / '.DO_NOT_MAIL').exists() else 'PENDING')
        rows.append((d.parent.name + '/' + d.name, state))
    return rows


def build_state(slug, meta):
    case_id, court, what = meta
    lines = [f"# CASE STATE — {slug}",
             f"*compiled {time.strftime('%Y-%m-%d %H:%M')} by pipeline.case_state — do not hand-edit*", '',
             f"**Case:** {case_id} | **Court:** {court}",
             f"**What it is:** {what}", '']
    tl = timeline_tail(slug)
    if tl:
        lines.append('## Last events (timeline)')
        for e in tl:
            lines.append(f"- {e.get('date')}: [{e.get('type','')}] {str(e.get('description',''))[:180]}")
        lines.append('')
    mails = tracker_entries(case_id)
    if mails:
        lines.append('## Mailings (tracker)')
        for m in mails:
            doc = m.get('document', '?')
            st = m.get('status', '?')
            job = m.get('ls_job', '')
            lines.append(f"- {doc}: **{st}**{' (LS ' + job + ')' if job else ''}")
        lines.append('')
    ob = outbox_state([slug, case_id, slug.split('_')[0]])
    if ob:
        lines.append('## Outbox')
        for name, st in ob:
            lines.append(f"- {name}: {st}")
        lines.append('')
    facts = CASES / slug / 'CASE_FACTS.md'
    if facts.exists():
        lines.append(f"**Full facts:** [[cases/{slug}/CASE_FACTS]]")
    lines.append('')
    return '\n'.join(lines)


def main():
    moc = ["# CASES — MAP OF CONTENTS (auto-compiled)",
           f"*compiled {time.strftime('%Y-%m-%d %H:%M')} — pipeline.case_state; boot-warm sessions read THIS first*", '']
    for slug, meta in REGISTRY.items():
        d = CASES / slug
        if not d.exists():
            continue
        state = build_state(slug, meta)
        (d / 'CASE_STATE.md').write_text(state, encoding='utf-8')
        case_id, court, what = meta
        moc.append(f"## [[cases/{slug}/CASE_STATE|{case_id}]] — {court}")
        moc.append(f"{what}")
        tl = timeline_tail(slug, 1)
        if tl:
            moc.append(f"*Last: {tl[-1].get('date')} — {str(tl[-1].get('description',''))[:120]}*")
        moc.append('')
    (_P.VAULT / 'CASES_MOC.md').write_text('\n'.join(moc), encoding='utf-8')
    print(f"compiled {len(REGISTRY)} case states + CASES_MOC.md")


if __name__ == '__main__':
    main()
