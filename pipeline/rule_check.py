"""pipeline/rule_check.py — check an ADVERSE filing against the components the rule it was filed under requires.

Deterministic. The requirements come from `m1_style_profiles.json` (written by `rule_requirements` from the court's own rules
in the library; data, never code). This module only asks, for each mandatory component: is it in the filing? It never
derives a requirement of its own. A court/kind with no contract in the profiles renders every component as
"not in your library" (verdict n_a) — a gap, not a verdict.

    python -m pipeline.rule_check --court PA_CP_PHILA --kind movant_preliminary_objections \
        --filing cases/<slug>/intake/<their_filing>.txt --slug <slug> [--control 26010000] [--profiles path.json]

Writes cases/<slug>/rule_check.json in the vault_contract shape:
    filings[control, title, filed_under[], contract_source, components[component, rule, requires, found, verdict, defect_ref]]
and appends one MANDATORY_COMPONENT_MISSING finding per missing component to the same file under `findings`.
"""
from __future__ import annotations
import argparse, json, re, sys
from datetime import datetime
from pathlib import Path

try:
    from pipeline import paths as _P
    _HOME = _P.HOME
except Exception:  # running from the live Beezle tree for a sandbox test
    _HOME = Path(".")

# How a component's heading is recognised in a filing. Small, additive, and only about WORDS on the page — never a
# requirement. A heading not listed here is matched on its own significant words (2+ of them on one line).
_ALIASES = {
    "BRIEF OR MEMORANDUM OF LAW": ["memorandum of law", "brief in support", "memorandum in support", "brief"],
    "EXHIBITS": ["exhibit", "attached hereto", "attached as", "appendix"],
    "PROPOSED ORDER": ["proposed order", "it is hereby ordered", "by the court"],
    "CERTIFICATE OF SERVICE": ["certificate of service"],
    "VERIFICATION": ["verification", "verified"],
    "NOTICE TO PLEAD": ["notice to plead"],
    "CERTIFICATE OF COMPLIANCE": ["certificate of compliance"],
}
_STOP = {"OF", "OR", "THE", "AND", "A", "AN", "TO", "IN", "FOR", "BY", "WITH"}


def _lines(text: str) -> list[str]:
    return [re.sub(r"\s+", " ", l).strip().lower() for l in text.splitlines() if l.strip()]


def _find(heading: str, lines: list[str], text_l: str) -> tuple[bool, str]:
    """(present?, evidence). Evidence names the line that matched, or the count of a keyword."""
    for alias in _ALIASES.get(heading.upper(), []):
        for i, l in enumerate(lines):
            if alias in l:
                return True, f"line {i+1}: \"{l[:90]}\""
    words = [w for w in re.findall(r"[A-Za-z]+", heading.upper()) if w not in _STOP and len(w) > 2]
    if len(words) >= 2:
        for i, l in enumerate(lines):
            if sum(1 for w in words if w.lower() in l) >= 2:
                return True, f"line {i+1}: \"{l[:90]}\""
    elif words:
        n = text_l.count(words[0].lower())
        if n:
            return True, f"'{words[0].lower()}' appears {n}×"
    return False, "no heading or keyword for this component anywhere in the filing"


def check(filing_text: str, court: str, kind: str, profiles: dict, *, control: str = "", title: str = "",
          filed_under: list[str] | None = None) -> dict:
    lines = _lines(filing_text); text_l = filing_text.lower()
    contract = ((profiles.get(court) or {}).get(kind)) or {}
    comps = list(contract.get("mandatory_sections") or [])
    # structural items from the contract's section_order that are not already mandatory_sections
    named = {c.get("heading", "").upper() for c in comps}
    for so in contract.get("section_order") or []:
        h = re.sub(r"\s*\(.*\)\s*$", "", so).strip().upper()
        if h in ("CERTIFICATE OF SERVICE", "VERIFICATION", "NOTICE TO PLEAD") and h not in named:
            comps.append({"heading": h, "rule": "section_order", "requires": h.title()})
    rows, findings = [], []
    if not contract:
        rows.append({"component": "(all)", "rule": "", "requires": "", "found": "", "verdict": "n_a",
                     "note": f"no contract for {court}/{kind} in the library's rule profiles — run rule_requirements first"})
    for c in comps:
        present, ev = _find(c.get("heading", ""), lines, text_l)
        verdict = "present" if present else "missing"
        row = {"component": c.get("heading", ""), "rule": c.get("rule", ""), "requires": c.get("requires", ""),
               "found": ev, "verdict": verdict, "defect_ref": ""}
        if not present:
            fid = f"MCM-{re.sub(r'[^A-Z0-9]+', '_', c.get('heading', '').upper()).strip('_')}"
            row["defect_ref"] = fid
            findings.append({"id": fid, "violation_type": "MANDATORY_COMPONENT_MISSING", "severity": "critical",
                             "component": c.get("heading", ""), "rule": c.get("rule", ""), "requires": c.get("requires", ""),
                             "evidence": ev, "consequence": "", "consequence_source": "NOT IN LIBRARY until grounded"})
        rows.append(row)
    return {"control": control, "title": title or kind, "court": court, "kind": kind, "filed_under": filed_under or [],
            "contract_source": (contract.get("_authority") or "") if contract else "not in your library",
            "checked_at": datetime.now().isoformat(timespec="seconds"), "components": rows, "findings": findings}


def write(slug: str, result: dict, cases_dir: Path | None = None) -> Path:
    base = cases_dir or (_HOME / "obsidian_sync" / "cases")
    p = base / slug / "rule_check.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {"slug": slug, "filings": [], "findings": []}
    if p.exists():
        try: data = json.loads(p.read_text(encoding="utf-8"))
        except Exception: pass
    data["filings"] = [f for f in data.get("filings", []) if not (f.get("control") and f.get("control") == result.get("control"))]
    data["filings"].append({k: v for k, v in result.items() if k != "findings"})
    data["findings"] = [f for f in data.get("findings", []) if f.get("control") != result.get("control")] + \
                       [{**f, "control": result.get("control")} for f in result["findings"]]
    p.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    return p


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--court", required=True); ap.add_argument("--kind", required=True)
    ap.add_argument("--filing", required=True, help="text of the adverse filing (.txt/.md; OCR output)")
    ap.add_argument("--slug", required=True); ap.add_argument("--control", default=""); ap.add_argument("--title", default="")
    ap.add_argument("--filed-under", default="", help="comma-separated rules the movant invoked")
    ap.add_argument("--profiles", default="", help="m1_style_profiles.json (default: HOME/output/audit/)")
    ap.add_argument("--cases-dir", default="", help="override cases dir (sandbox)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    prof_path = Path(a.profiles) if a.profiles else (_HOME / "output" / "audit" / "m1_style_profiles.json")
    profiles = json.loads(prof_path.read_text(encoding="utf-8")) if prof_path.exists() else {}
    text = Path(a.filing).read_text(encoding="utf-8", errors="ignore")
    res = check(text, a.court, a.kind, profiles, control=a.control, title=a.title,
                filed_under=[x.strip() for x in a.filed_under.split(",") if x.strip()])
    for r in res["components"]:
        print(f"  {r['verdict']:8s} {r['component']:34s} {r['rule']:16s} {r['found'][:70]}")
    print(f"  findings: {len(res['findings'])}  contract: {res['contract_source'][:80]}")
    if not a.dry_run:
        print("  wrote", write(a.slug, res, Path(a.cases_dir) if a.cases_dir else None))
    return res


if __name__ == "__main__":
    main()
