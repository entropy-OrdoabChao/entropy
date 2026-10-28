"""
pipeline/exhibit_resolver.py

Generic exhibit resolver for any case. For each exhibit entry in a case.md,
finds the actual email stub or attachment file that backs it, by searching
unmatched_emails/ and the case's own attachments folder using the entry's
match_keywords + from_pattern + date_pattern.

Usage from drafter / exhibit_packager:

    from pipeline.exhibit_resolver import resolve_case_exhibits
    resolved = resolve_case_exhibits(case_md_path)
    # returns: {"A": Path("..."), "B": Path("..."), ...}

Schema for case.md exhibit entries (new schema, backward compatible):

    exhibits:
      - letter: A
        description: "FOIA Request"
        match_keywords: ["FOIA Request", "446-5514079"]
        from_pattern: "(?i)<your-email-or-surname>"   # optional
        date_pattern: "2026-01-08"             # optional
        prefer: "agency"                       # optional: agency | plaintiff | any (default any)
        # Legacy field still honored if present:
        file_pattern: "Gmail_-_FOIA_Request"

The resolver tries in order:
1. file_pattern (exact substring match in case attachments folder)
2. match_keywords (subject/body match in unmatched_emails)
3. fallback to None and let exhibit_packager log warning

Designed to be deterministic and idempotent — same case.md always yields
same resolution. No LLM, no Hermes.
"""
from __future__ import annotations
from core import identity as _ident
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# === BEEZLE_ROOT_SHIM (WSL-safe; auto-injected) ===
import os as _os_broot
from pathlib import Path as _Path_broot
def _resolve_beezle_root():
    _e = _os_broot.environ.get("BEEZLE_ROOT")
    if _e:
        return _Path_broot(_e)
    _d = _Path_broot(__file__).resolve().parent
    for _ in range(5):
        if (_d / "pipeline").exists() or (_d / "obsidian_sync").exists():
            return _d
        _d = _d.parent
    return _Path_broot(str(_eroot()))
_BEEZLE_ROOT = _resolve_beezle_root()
# === END BEEZLE_ROOT_SHIM ===

ROOT = _BEEZLE_ROOT
EMAILS_DIR = ROOT / "obsidian_sync" / "unmatched_emails"


@dataclass
class ExhibitEntry:
    letter: str
    description: str = ""
    match_keywords: list = field(default_factory=list)
    from_pattern: Optional[str] = None
    date_pattern: Optional[str] = None
    prefer: str = "any"  # agency | plaintiff | any
    file_pattern: Optional[str] = None  # legacy

    @property
    def is_legacy(self) -> bool:
        return self.file_pattern is not None and not self.match_keywords


@dataclass
class ResolvedExhibit:
    letter: str
    description: str
    source_path: Optional[Path]  # path to email stub or attachment
    method: str  # how it was resolved: file_pattern | keyword | from | date | none
    confidence: float  # 0.0 - 1.0


# ---- case.md exhibit-block parser ----
def parse_exhibit_block(case_md_path: Path) -> list[ExhibitEntry]:
    """Read exhibits: block from case.md, return list of ExhibitEntry."""
    if not case_md_path.exists():
        return []
    text = case_md_path.read_text(encoding="utf-8", errors="replace")

    # Find the exhibits: block (YAML frontmatter section)
    m = re.search(
        r"^(\s*exhibits:\s*\n)(.*?)(?=^\S|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if not m:
        return []

    body = m.group(2)
    entry_re = re.compile(r"(  - letter:.*?)(?=\n  - letter:|\Z)", re.DOTALL)
    raw_entries = entry_re.findall(body)

    out = []
    for raw in raw_entries:
        entry = ExhibitEntry(letter="?")
        for line in raw.split("\n"):
            line = line.strip()
            if line.startswith("- letter:"):
                entry.letter = line.split(":", 1)[1].strip()
            elif line.startswith("description:"):
                entry.description = line.split(":", 1)[1].strip().strip('"')
            elif line.startswith("file_pattern:"):
                val = line.split(":", 1)[1].strip()
                # Strip inline comment "# blah" and "null"
                val = val.split("#")[0].strip()
                if val and val.lower() != "null":
                    entry.file_pattern = val
            elif line.startswith("from_pattern:"):
                val = line.split(":", 1)[1].strip().strip('"')
                if val and val.lower() != "null":
                    entry.from_pattern = val
            elif line.startswith("date_pattern:"):
                val = line.split(":", 1)[1].strip().strip('"')
                if val and val.lower() != "null":
                    entry.date_pattern = val
            elif line.startswith("prefer:"):
                entry.prefer = line.split(":", 1)[1].strip()
            elif line.startswith("match_keywords:"):
                # Inline list:  match_keywords: ["foo", "bar"]
                val = line.split(":", 1)[1].strip()
                if val.startswith("["):
                    inner = val.strip("[]")
                    entry.match_keywords = [
                        x.strip().strip('"').strip("'")
                        for x in inner.split(",")
                        if x.strip()
                    ]
            elif line.startswith("- "):
                # Multi-line list item under match_keywords:
                kw = line[2:].strip().strip('"').strip("'")
                if kw:
                    entry.match_keywords.append(kw)
        out.append(entry)
    return out


# ---- email stub scanner ----
def _stub_text(path: Path) -> tuple[str, str, str]:
    """Return (subject, from_field, body) from an email stub."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ("", "", "")
    subj = ""
    from_field = ""
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            fm = parts[1]
            sm = re.search(r"^subject:\s*(.+)$", fm, re.MULTILINE)
            if sm:
                subj = sm.group(1).strip().strip('"')
            fm_m = re.search(r"^from:\s*(.+)$", fm, re.MULTILINE)
            if fm_m:
                from_field = fm_m.group(1).strip().strip('"')
            body = parts[2]
        else:
            body = text
    else:
        body = text
    return (subj, from_field, body)


def _filename_date(path: Path) -> str:
    """Email stubs are named YYYY-MM-DD_..."""
    m = re.match(r"(\d{4}-\d{2}-\d{2})_", path.name)
    return m.group(1) if m else ""


# ---- resolver core ----
def _try_file_pattern(entry: ExhibitEntry, case_dir: Path) -> Optional[Path]:
    """Legacy: substring match in case attachments folder."""
    if not entry.file_pattern:
        return None
    pat = entry.file_pattern.lower()
    for p in case_dir.rglob("*"):
        if p.is_file() and pat in p.name.lower():
            return p
    return None


def _try_keyword_match(entry: ExhibitEntry,
                       emails_dir: Path = EMAILS_DIR) -> tuple[Optional[Path], float, str]:
    """Search unmatched_emails for a stub matching keywords + optional from/date."""
    if not entry.match_keywords:
        return (None, 0.0, "no_keywords")

    # If date_pattern is set, narrow to stubs with that date prefix
    candidates = []
    if entry.date_pattern:
        for p in emails_dir.glob(f"{entry.date_pattern}*.md"):
            candidates.append(p)
    if not candidates:
        candidates = list(emails_dir.glob("*.md"))

    best_path = None
    best_score = 0.0
    best_method = "none"

    for p in candidates:
        subj, frm, body = _stub_text(p)
        haystack = f"{p.name} {subj} {frm}".lower()
        body_lower = body.lower()

        # Score: keyword hits in subject/filename are high, body lower
        kw_subject_hits = sum(
            1 for kw in entry.match_keywords if kw.lower() in haystack
        )
        kw_body_hits = sum(
            1 for kw in entry.match_keywords if kw.lower() in body_lower
        )

        if kw_subject_hits == 0 and kw_body_hits == 0:
            continue

        score = kw_subject_hits * 1.0 + kw_body_hits * 0.3

        # Boost if from_pattern matches
        if entry.from_pattern:
            if re.search(entry.from_pattern, frm + " " + p.name, re.I):
                score += 0.5
            else:
                # If from_pattern set and doesn't match, penalize hard
                score -= 1.0

        # Boost if date in filename matches
        if entry.date_pattern:
            if _filename_date(p) == entry.date_pattern:
                score += 0.3

        # prefer: plaintiff vs agency
        is_from_plaintiff = bool(re.search(
            r"|".join(map(re.escape, _ident.name_tokens() + [_ident.email()])), frm + " " + p.name, re.I
        ))
        if entry.prefer == "plaintiff" and is_from_plaintiff:
            score += 0.5
        elif entry.prefer == "agency" and not is_from_plaintiff:
            score += 0.5

        if score > best_score:
            best_score = score
            best_path = p
            best_method = "keyword"

    confidence = min(1.0, best_score / max(1, len(entry.match_keywords)))
    return (best_path, confidence, best_method)


def resolve_exhibit(entry: ExhibitEntry, case_dir: Path,
                    emails_dir: Path = EMAILS_DIR) -> ResolvedExhibit:
    """Resolve one exhibit entry to a real file path."""
    # 1. Try file_pattern first (legacy support, deterministic)
    p = _try_file_pattern(entry, case_dir)
    if p:
        return ResolvedExhibit(
            letter=entry.letter,
            description=entry.description,
            source_path=p,
            method="file_pattern",
            confidence=1.0,
        )

    # 2. Try match_keywords against unmatched_emails
    p, conf, method = _try_keyword_match(entry, emails_dir)
    if p:
        return ResolvedExhibit(
            letter=entry.letter,
            description=entry.description,
            source_path=p,
            method=method,
            confidence=conf,
        )

    # 3. Try file_pattern as keyword if it's a non-empty string
    if entry.file_pattern:
        # Synthesize keywords from file_pattern and try match
        synth = ExhibitEntry(
            letter=entry.letter,
            description=entry.description,
            match_keywords=[entry.file_pattern.replace("_", " ")],
        )
        p, conf, method = _try_keyword_match(synth, emails_dir)
        if p:
            return ResolvedExhibit(
                letter=entry.letter,
                description=entry.description,
                source_path=p,
                method=f"synth_{method}",
                confidence=conf * 0.7,
            )

    return ResolvedExhibit(
        letter=entry.letter,
        description=entry.description,
        source_path=None,
        method="none",
        confidence=0.0,
    )


def resolve_case_exhibits(case_md_path: Path,
                          emails_dir: Path = EMAILS_DIR) -> dict:
    """Resolve all exhibits for a case. Returns {letter: ResolvedExhibit}."""
    entries = parse_exhibit_block(case_md_path)
    case_dir = case_md_path.parent
    result = {}
    for entry in entries:
        result[entry.letter] = resolve_exhibit(entry, case_dir, emails_dir)
    return result


# ---- CLI for testing ----
def main():
    import sys
    if len(sys.argv) < 2:
        print("Usage: python exhibit_resolver.py <case.md path>")
        return 1
    case_md = Path(sys.argv[1])
    if not case_md.exists():
        print(f"[FAIL] not found: {case_md}")
        return 1
    print(f"=== Resolving {case_md.name} ===\n")
    resolved = resolve_case_exhibits(case_md)
    for letter in sorted(resolved.keys()):
        r = resolved[letter]
        if r.source_path:
            rel = r.source_path.name
            print(f"  {letter}: [{r.method:10s} conf={r.confidence:.2f}]")
            print(f"     desc: {r.description}")
            print(f"     file: {rel}")
        else:
            print(f"  {letter}: [UNRESOLVED]")
            print(f"     desc: {r.description}")
        print()

    unresolved = [l for l, r in resolved.items() if r.source_path is None]
    if unresolved:
        print(f"[WARN] {len(unresolved)} unresolved: {', '.join(unresolved)}")
        return 1
    print(f"[OK] all {len(resolved)} exhibits resolved")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
