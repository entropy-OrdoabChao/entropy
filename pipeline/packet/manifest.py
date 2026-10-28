#!/usr/bin/env python3
"""manifest.py — read the exhibit manifest from a stub's '## NEW EVIDENCE BLOCK' (the rows pipeline.m2.cells_to_stub
writes and pipeline.m1.parse_stub reads):

    ### <L> — <title> _(record)_
    **Element:** ...
    **Source:** ... (Gmail ids 180b966388aad826, 180b9738d5c6e0c8) [doc:motion_EXHIBIT_D_....txt]
    **Quote:**
    > ...

-> [{letter, title, element, source, doc (str as written), path (Path resolved against the stub dir), gmail_ids [..]}]
A row without a [doc:] is skipped and reported (an exhibit is a record DOCUMENT; no file = no exhibit).
Also exported: default_stub(m_dir) = <M>/EXHIBITS.md if present, else <M>/motion_to_vacate.md.
"""
import re
from pathlib import Path

_ROW = re.compile(r"^###\s+([A-Z]\d?|E\d+)\s+[—-]\s+(.+?)\s*(?:_\((.*?)\)_)?\s*$(.*?)(?=^###\s+(?:E\d+|[A-Z]\d?)\s+[—-]|\Z)", re.M | re.S)
_DOC = re.compile(r"\[\s*doc\s*:\s*([^\]\n]+?)\s*\]", re.I)
_GMAIL = re.compile(r"\b([0-9a-f]{16})\b")


def default_stub(m_dir) -> Path:
    m = Path(m_dir)
    for name in ("EXHIBITS.md", "motion_to_vacate.md"):
        if (m / name).is_file():
            return m / name
    return m / "EXHIBITS.md"


def read_manifest(stub_path, bases=None, verbose=True) -> list:
    stub_path = Path(stub_path)
    t = stub_path.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"^##\s*NEW EVIDENCE BLOCK[^\n]*\n(.*?)(?=^##\s|\Z)", t, re.S | re.M)
    sec = m.group(1) if m else t
    bases = [Path(b) for b in (bases or [])] + [stub_path.resolve().parent]
    rows = []
    for em in _ROW.finditer(sec):
        tag, title, _sev, body = em.groups()
        if re.fullmatch(r"E\d+", tag):
            if verbose:
                print(f"  [manifest] skip ### {tag} (unit-prose tag, not a record document)")
            continue
        src = re.search(r"\*\*Source:\*\*\s*(.+)", body)
        src = src.group(1).strip() if src else ""
        elem = re.search(r"\*\*Element:\*\*\s*(.+)", body)
        docs = _DOC.findall(src + " " + body)
        if not docs:
            if verbose:
                print(f"  [manifest] skip ### {tag} — {title.strip()[:60]}: no [doc:]")
            continue
        doc = docs[0].split(";")[0].strip().strip("`'\"")
        path = None
        for b in bases:
            if (b / doc).is_file():
                path = b / doc
                break
        if path is None and Path(doc).is_absolute() and Path(doc).is_file():
            path = Path(doc)
        rows.append({"letter": tag, "title": re.sub(r"\s+", " ", title).strip(),
                     "element": elem.group(1).strip() if elem else "", "source": src,
                     "doc": doc, "path": path if path is not None else bases[-1] / doc,
                     "exists": path is not None,
                     "gmail_ids": [g for g in _GMAIL.findall(src) if not re.fullmatch(r"\d{16}", g)]})
    return rows


if __name__ == "__main__":
    import sys
    for r in read_manifest(sys.argv[1]):
        print(f"{r['letter']:>2} | {r['doc']} | exists={r['exists']} | gmail={r['gmail_ids']} | {r['title'][:60]}")
