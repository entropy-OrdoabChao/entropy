"""
draft_leak_gate.py — write-time placeholder/leak detection for the
motion drafter pipeline. Canonical patterns ported from
_audit_overnight_drafts_v1.py so the live drafter catches the same
leaks the post-hoc audit detects.

Usage:
    from pipeline.draft_leak_gate import gate_draft, case_slug_from_case_id

    case_slug = case_slug_from_case_id(case_id)  # "fed:2:26-cv-00789" -> "fed_26-cv-00789"
    clean, flags, summary = gate_draft(draft_text, case_slug,
                                       draft_path=out_path)
    if not clean:
        # log/deprioritize/retry — do not silently swallow

The gate is INFORMATIONAL: it does not modify the draft and does not
block the write. It produces a sidecar JSONL log
(obsidian_sync/_review/draft_leaks.jsonl) plus a structured return
value the caller can act on.
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple

_ROOT = _eroot()
LEAK_LOG = _ROOT / 'obsidian_sync/_review/draft_leaks.jsonl'

# Canonical patterns ported from _audit_overnight_drafts_v1.py PROBES dict.
# Case-sensitive, re.MULTILINE.
PATTERNS: Dict[str, str] = {
    # Bracketed placeholders the LLM emits when it can't fill a slot
    'placeholder_record':           r'\[record\]',
    'placeholder_citation':         r'\[citation\]',
    'placeholder_date':             r'\[date\]',
    'placeholder_exhibit':          r'\[exhibit\]',
    'placeholder_source':           r'\[source\]',
    'placeholder_case_name':        r'\[case[_ ]name\]',
    # Plus-joined italic placeholders ("See *Complaint + status*")
    'placeholder_complaint_status': r'Complaint \+ status',
    'italic_placeholder_pattern':   r'See \*[A-Z][a-z]+(?: \+ [a-z]+)+',
    # Generic developer placeholders
    'placeholder_todo':             r'\b(?:TODO|FIXME|XXX|PLACEHOLDER|\[TBD\])\b',
    # Citation misuse
    'tashire_miscite':              r'Tashire.*complete diversity|complete diversity.*Tashire',
    # Structural artifacts
    'empty_table_row':              r'^\| *(?:\| *)+\| *$',
    'missing_subsection':           r'^[A-Z]\.\s*$',
}

# Hits in HARD_LEAKS make a draft "unclean" by default. The other patterns
# (e.g. ellipsis_truncation, structural_repeats) are tracked separately by
# the scorer; this gate focuses on artifacts that are objectively wrong.
HARD_LEAKS = frozenset({
    'placeholder_record',
    'placeholder_citation',
    'placeholder_date',
    'placeholder_exhibit',
    'placeholder_source',
    'placeholder_case_name',
    'placeholder_complaint_status',
    'placeholder_todo',
    'italic_placeholder_pattern',
    'tashire_miscite',
})

# Court mixups — special-cased because they depend on case_slug
COURT_MIXUPS = ('ecf_in_pa_ccp', 'control_no_in_federal')


def case_slug_from_case_id(case_id: str) -> str:
    """Convert "phila_ccp:260100000" -> "phila_ccp_260100000",
    "fed:2:26-cv-00789" -> "fed_26-cv-00789" to match drafts/ slugs."""
    s = (case_id or '').replace(':', '_')
    # collapse "2_25-cv-..." -> "25-cv-..." (federal cases drop the 2:)
    s = re.sub(r'_2_(\d{2}-cv-\d+)', r'_\1', s)
    return s


def scan_text(text: str, case_slug: str = '') -> Dict[str, int]:
    """Run all canonical probes against `text`. Returns count per probe.
    case_slug controls court-mixup checks (ecf_in_pa_ccp etc.).
    """
    counts: Dict[str, int] = {}
    for name, pat in PATTERNS.items():
        try:
            counts[name] = len(re.findall(pat, text, flags=re.MULTILINE))
        except re.error:
            counts[name] = 0

    # Court mixups — depend on case_slug
    ecf_count = len(re.findall(r'\bECF\s+No\.\s*\d+', text))
    control_count = len(re.findall(r'\bControl\s+No\.\s*\d+', text))
    is_pa_state = case_slug.startswith('phila_ccp')
    is_federal  = case_slug.startswith('fed_')
    counts['ecf_in_pa_ccp']        = ecf_count if is_pa_state else 0
    counts['control_no_in_federal'] = control_count if is_federal else 0

    return counts


def gate_draft(text: str,
               case_slug: str,
               draft_path: Optional[Path] = None,
               log_to_file: bool = True
               ) -> Tuple[bool, Dict[str, int], str]:
    """Scan `text`, log a record, return (clean, flag_counts, summary).

    clean = no hard leaks AND no court mixups.
    summary = short human-readable line for stdout.
    """
    counts = scan_text(text, case_slug)
    leaks = {k: v for k, v in counts.items() if v > 0}
    hard_hits = {k: v for k, v in leaks.items()
                 if k in HARD_LEAKS or k in COURT_MIXUPS}
    clean = not bool(hard_hits)

    if hard_hits:
        summary = 'LEAK_FOUND: ' + ', '.join(
            f'{k}={v}' for k, v in sorted(hard_hits.items()))
    elif leaks:
        summary = 'soft_only: ' + ', '.join(
            f'{k}={v}' for k, v in sorted(leaks.items()))
    else:
        summary = 'clean'

    if log_to_file:
        try:
            LEAK_LOG.parent.mkdir(parents=True, exist_ok=True)
            rec = {
                'ts':         datetime.now().isoformat(timespec='seconds'),
                'case_slug':  case_slug,
                'draft':      str(draft_path) if draft_path else None,
                'clean':      clean,
                'hard_hits':  sorted(hard_hits.keys()),
                'flags':      leaks,
            }
            with open(LEAK_LOG, 'a', encoding='utf-8') as f:
                f.write(json.dumps(rec) + '\n')
        except Exception:
            # Logging failure must NEVER break drafting
            pass

    return clean, counts, summary


# Convenience: CLI scan
def _cli():
    import argparse, sys as _sys
    p = argparse.ArgumentParser()
    p.add_argument('draft')
    p.add_argument('--case-slug', default='')
    p.add_argument('--no-log', action='store_true')
    a = p.parse_args()
    path = Path(a.draft)
    if not path.exists():
        print(f'ERROR: not found: {path}', file=_sys.stderr)
        return 2
    txt = path.read_text(encoding='utf-8', errors='replace')
    cs = a.case_slug
    if not cs:
        for part in path.parts:
            if part.startswith(('fed_', 'phila_ccp')):
                cs = part
                break
    clean, counts, summary = gate_draft(txt, cs, draft_path=path,
                                        log_to_file=not a.no_log)
    print(f'case_slug: {cs}')
    print(f'clean:     {clean}')
    print(f'summary:   {summary}')
    print('counts:')
    for k, v in sorted(counts.items()):
        marker = ' ⚠' if v > 0 and k in HARD_LEAKS else ''
        print(f'  {k:30s} {v}{marker}')
    return 0 if clean else 1


if __name__ == '__main__':
    import sys as _sys
    _sys.exit(_cli())
