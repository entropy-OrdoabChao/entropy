#!/usr/bin/env python3
"""
factual_predicates — bridges findings DBs into the drafter grounding pipeline.

When a motion stub mentions Example Trust Co, Wells Fargo, State Farm, Hall,
the-judge, etc., this module queries the findings tables and returns an
"## ESTABLISHED FACTUAL PREDICATES" markdown block. Combined with the
existing AUTHORITATIVE DEFINITIONS block from doctrine_index, the LLM
drafter sees BOTH the legal framework AND the specific evidence.

Public API:
    get_factual_predicates(stub_text, top_k=3, db_path=None) -> str
        Returns markdown block or "" if no entities matched / DB empty.
    status() -> dict
        Diagnostic counts of available findings per source.
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional

ROOT = _eroot()
DB_PATH = ROOT / "output" / "audit" / "emma_intel.db"


# Canonical entities — every name the drafter might mention that should
# trigger a findings lookup. Patterns are case-insensitive substrings.
# Must stay aligned with relationship_graph's defendant list.
ENTITY_PATTERNS = [
    # Bank/custodian defendants
    ("Example Trust Co",  ["example trust co", "nti", "the example trust co company",
                          "example trust co investments"]),
    ("Wells Fargo",     ["wells fargo"]),
    ("U.S. Bank",       ["u.s. bank", "us bank", "u.s. bank trust",
                          "united states bank"]),
    ("BNY Mellon",      ["bny mellon", "bank of new york mellon"]),
    ("State Street",    ["state street bank"]),
    ("Computershare",   ["computershare"]),
    # Credit / consumer-finance defendants
    ("Equifax",         ["equifax"]),
    ("Experian",        ["experian"]),
    ("TransUnion",      ["transunion", "trans union"]),
    ("Avant",           ["avant"]),
    ("Example Card Issuer",     ["capital one"]),
    ("Amerisave",       ["amerisave"]),
    ("State Farm",      ["state farm"]),
    # Government / court entities
    ("First Judicial District",
                        ["first judicial district"]),
    ("City of Philadelphia",
                        ["city of philadelphia"]),
    ("Philadelphia Board of Pensions",
                        ["philadelphia board of pensions",
                         "phila. board of pensions",
                         "phila board of pensions"]),
    ("Commonwealth of Pennsylvania",
                        ["commonwealth of pennsylvania"]),
    # Specific judges: loaded from the case registry at runtime (no hard-coded names)
]


def _match_entities(stub_text: str) -> List[str]:
    """Return canonical entity names found in stub_text (deduplicated)."""
    if not stub_text:
        return []
    stub_lower = stub_text.lower()
    matched = []
    seen = set()
    for canonical, patterns in ENTITY_PATTERNS:
        for pat in patterns:
            if pat in stub_lower and canonical not in seen:
                matched.append(canonical)
                seen.add(canonical)
                break
    return matched


def _short_entity_key(canonical: str) -> str:
    """Strip honorifics for DB matching (e.g. 'Hon. [Judge]' -> 'Hall')."""
    s = canonical
    for prefix in ("Hon. ", "The "):
        if s.startswith(prefix):
            s = s[len(prefix):]
    # For judges, the surname is the most distinctive token
    if False:  # owner-specific judge shortcut removed; registry-driven
        return s.split()[-1]
    return s


def _fetch_emma_findings(conn, entity: str, limit: int) -> List[Dict]:
    """pdf_findings table — EMMA + uploaded regulatory PDFs."""
    key = _short_entity_key(entity)
    try:
        rows = conn.execute(
            "SELECT pdf_path, page, line, context, role_hint FROM pdf_findings "
            "WHERE entity LIKE ? OR context LIKE ? "
            "ORDER BY pdf_path, page, line LIMIT ?",
            (f"%{key}%", f"%{key}%", limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [{
        "source_kind": "EMMA",
        "source": Path(r[0]).name if r[0] else "(unknown)",
        "page": r[1],
        "line": r[2],
        "context": (r[3] or "").strip(),
        "role_hint": r[4] or "",
        "entity": entity,
    } for r in rows]


def _fetch_sec_findings(conn, entity: str, limit: int) -> List[Dict]:
    """sec_findings table — SEC EDGAR filings."""
    key = _short_entity_key(entity)
    try:
        rows = conn.execute(
            "SELECT filer_name, form_type, line, context, source_url FROM sec_findings "
            "WHERE entity LIKE ? OR context LIKE ? "
            "ORDER BY filer_name LIMIT ?",
            (f"%{key}%", f"%{key}%", limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [{
        "source_kind": "SEC",
        "source": r[0] or "(unknown filer)",
        "form_type": r[1] or "",
        "line": r[2],
        "context": (r[3] or "").strip(),
        "url": r[4] or "",
        "entity": entity,
    } for r in rows]


def _fetch_judicial_findings(conn, entity: str, limit: int) -> List[Dict]:
    """judicial_findings table — analyze-order outputs (canon violations)."""
    key = _short_entity_key(entity)
    try:
        rows = conn.execute(
            "SELECT case_id, judge, order_path, flag, severity, description, authority, evidence "
            "FROM judicial_findings "
            "WHERE evidence LIKE ? OR description LIKE ? OR judge LIKE ? "
            "ORDER BY CASE severity WHEN 'FATAL' THEN 0 WHEN 'SERIOUS' THEN 1 ELSE 2 END, "
            "ts DESC LIMIT ?",
            (f"%{key}%", f"%{key}%", f"%{key}%", limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [{
        "source_kind": "judicial",
        "case_id": r[0] or "",
        "judge": r[1] or "",
        "order_path": Path(r[2]).name if r[2] else "",
        "flag": r[3] or "",
        "severity": r[4] or "",
        "description": r[5] or "",
        "authority": r[6] or "",
        "entity": entity,
    } for r in rows]


def get_factual_predicates(stub_text: str, top_k: int = 3,
                            db_path: Optional[Path] = None) -> str:
    """Return the ESTABLISHED FACTUAL PREDICATES markdown block.

    Args:
        stub_text: motion stub text being drafted
        top_k:     max findings per (entity, source-kind)
        db_path:   override the default DB location (test only)

    Returns:
        Markdown block, or empty string if no entities matched.
    """
    db = db_path or DB_PATH
    if not db.exists():
        return ""
    matched = _match_entities(stub_text)
    if not matched:
        return ""

    conn = sqlite3.connect(str(db))
    by_entity: Dict[str, List[Dict]] = {}
    try:
        for entity in matched:
            entity_findings = []
            entity_findings.extend(_fetch_emma_findings(conn, entity, top_k))
            entity_findings.extend(_fetch_sec_findings(conn, entity, top_k))
            entity_findings.extend(_fetch_judicial_findings(conn, entity, top_k))
            if entity_findings:
                by_entity[entity] = entity_findings
    finally:
        conn.close()

    if not by_entity:
        return ""

    return _render(by_entity)


def _render(by_entity: Dict[str, List[Dict]]) -> str:
    """Render findings to markdown grounded block."""
    lines = [
        "## ESTABLISHED FACTUAL PREDICATES",
        "",
        "*The following are documented public-record facts available for "
        "direct citation. Each finding carries its own source and authority. "
        "When the prose argues financial nexus, conflict of interest, "
        "non-discretionary remedy, or canon-of-conduct violation, ground the "
        "assertion in one of these predicates and cite the source verbatim.*",
        "",
    ]
    for entity, findings in by_entity.items():
        lines.append(f"### Predicate facts — {entity}")
        lines.append("")
        for i, f in enumerate(findings, 1):
            kind = f["source_kind"]
            if kind == "EMMA":
                ctx = _truncate(f["context"], 420)
                role = f.get("role_hint", "")
                role_tag = f" ({role})" if role else ""
                lines.append(f"**{i}. EMMA / public-disclosure PDF{role_tag}:**")
                lines.append("")
                lines.append(f"> {ctx}")
                lines.append("")
                lines.append(f"*Source: `{f['source']}`, at p. {f['page']} "
                             f"(line {f['line']}). On file with the Municipal "
                             f"Securities Rulemaking Board (EMMA) or the "
                             f"respective issuer's public records.*")
                lines.append("")
            elif kind == "SEC":
                ctx = _truncate(f["context"], 420)
                form = f.get("form_type", "")
                form_tag = f" ({form})" if form else ""
                lines.append(f"**{i}. SEC EDGAR filing{form_tag}:**")
                lines.append("")
                lines.append(f"> {ctx}")
                lines.append("")
                src_url = f.get("url") or "SEC EDGAR (data.sec.gov)"
                lines.append(f"*Filer: {f['source']}. Source: {src_url}.*")
                lines.append("")
            elif kind == "judicial":
                lines.append(f"**{i}. Judicial order analysis — "
                             f"[{f['severity']}] {f['flag']}:**")
                lines.append("")
                lines.append(f"> {f['description']}")
                lines.append("")
                lines.append(f"*Order: `{f['order_path']}`; "
                             f"Judge: {f['judge']}; Case: {f['case_id']}.*")
                lines.append(f"*Authority: {f['authority']}.*")
                lines.append("")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _truncate(s: str, n: int) -> str:
    s = (s or "").replace("\n", " ").replace("\r", " ")
    s = " ".join(s.split())
    if len(s) > n:
        s = s[:n].rsplit(" ", 1)[0] + "..."
    return s


def status() -> Dict:
    """Diagnostic: counts of findings rows per source-kind."""
    out = {"db_path": str(DB_PATH), "db_exists": DB_PATH.exists()}
    if not DB_PATH.exists():
        return out
    conn = sqlite3.connect(str(DB_PATH))
    try:
        for tbl in ("pdf_findings", "sec_findings", "judicial_findings"):
            try:
                n = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                out[tbl] = n
            except sqlite3.OperationalError:
                out[tbl] = "(table not yet created)"
    finally:
        conn.close()
    return out


def _cli():
    import argparse
    import json
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    p = sub.add_parser("test", help="render predicates for a sample stub text")
    p.add_argument("--stub", required=True,
                   help="path to stub markdown OR raw text")
    p.add_argument("--top-k", type=int, default=3)
    args = ap.parse_args()
    if args.cmd == "status":
        print(json.dumps(status(), indent=2))
    elif args.cmd == "test":
        p = Path(args.stub)
        if p.exists():
            text = p.read_text(encoding="utf-8")
        else:
            text = args.stub
        out = get_factual_predicates(text, top_k=args.top_k)
        if out:
            print(out)
        else:
            print("(no factual predicates triggered for this stub)")
            print(f"  matched entities: {_match_entities(text)}")


if __name__ == "__main__":
    _cli()
