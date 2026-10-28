"""
PHASE_4_CASE_VALIDATION (DYNAMIC)
No hardcoded case configs. Everything derived from:
  - case_loader.load_case() -> facts, expected_claims, audit, status
  - scenario_engine.scenario_decompose() -> theories with seed_nodes, elements, duties, remedies
  - authority_discovery.discover_authority() -> enriched authorities

Runs baseline_memorandum + defense_simulator + procedural_scan for all registered cases.
"""
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import json
import re
from pathlib import Path
from pipeline.case_loader import load_all_cases
from pipeline.scenario_engine import scenario_decompose
from generation.baseline_memorandum import build_memo
from pipeline.defense_simulator import simulate
from pipeline.procedural_attack_builder import scan
from core.logger import get_logger

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

log = get_logger("phase4")


def seed_node_to_citation(node: str) -> str:
    """Convert theory_map seed_node format to proper citation.
    '15-USC-1681n' -> '15 U.S.C. 1681n'
    '73-PS-201-2'  -> '73 P.S. 201-2'
    'Pa-RCP-1024'  -> 'Pa.R.C.P. 1024'
    """
    n = node.strip()
    # Federal: NN-USC-XXXX
    m = re.match(r"^(\d+)-USC-(.+)$", n, re.I)
    if m:
        return f"{m.group(1)} U.S.C. {m.group(2).replace('-', '')}"
    # PA Statutes: NN-PS-XXX or 42-PaCS-XXXX
    m = re.match(r"^(\d+)-PS-(.+)$", n, re.I)
    if m:
        return f"{m.group(1)} P.S. {m.group(2)}"
    m = re.match(r"^(\d+)-PaCS-(.+)$", n, re.I)
    if m:
        return f"{m.group(1)} Pa.C.S. {m.group(2)}"
    # Pa Rules: Pa-RCP-XXXX
    m = re.match(r"^Pa-RCP-(.+)$", n, re.I)
    if m:
        return f"Pa.R.C.P. {m.group(1)}"
    # Fallback: replace dashes with spaces
    return n.replace("-", " ")


def theories_to_authorities(theories: list[dict]) -> list[dict]:
    """Extract controlling authorities from scenario theories.
    Each theory has seed_nodes, remedies, venue — all contain citations.
    Returns list of {name, citation, proposition} for baseline_memorandum.
    """
    seen = set()
    auths = []
    for t in theories:
        theory_name = t.get("display_name", t.get("name", "?"))
        for node in t.get("seed_nodes", []):
            cite = seed_node_to_citation(node)
            if cite not in seen:
                seen.add(cite)
                auths.append({
                    "name": f"{cite} ({theory_name})",
                    "citation": cite,
                    "proposition": f"Governing statute for {theory_name}",
                })
        # Venue basis often has a good citation
        venue = t.get("venue", {})
        for court_type, v_info in venue.items():
            basis = v_info.get("basis", "")
            if basis and basis not in seen:
                seen.add(basis)
                auths.append({
                    "name": f"Venue: {basis}",
                    "citation": basis,
                    "proposition": f"Jurisdictional basis for {theory_name}",
                })
    return auths


def theories_to_duties(theories: list[dict]) -> list[dict]:
    """Extract mandatory duties from attached_duties on each theory.
    Returns list of {duty, authority, consequence} for baseline_memorandum.
    """
    seen = set()
    duties = []
    for t in theories:
        for d in t.get("attached_duties", []):
            did = d.get("id", "")
            if did in seen:
                continue
            seen.add(did)
            duties.append({
                "duty": d.get("name", "unnamed duty"),
                "authority": d.get("source", d.get("source_title", "unspecified")),
                "consequence": d.get("consequence", "violation recorded"),
            })
        # Also create duties from remedies (each remedy implies a duty breach consequence)
        for remedy_key, remedy_val in t.get("remedies", {}).items():
            duty_text = f"{t.get('display_name', '?')}: {remedy_key}"
            if duty_text not in seen:
                seen.add(duty_text)
                # Extract the citation from remedy value if present
                remedy_str = str(remedy_val)
                cite_match = re.search(r"(\d+ (?:USC|U\.S\.C\.) \S+)", remedy_str, re.I)
                duties.append({
                    "duty": f"Comply with {remedy_key} requirements",
                    "authority": cite_match.group(1) if cite_match else t.get("seed_nodes", [""])[0],
                    "consequence": remedy_str[:150],
                })
    return duties


def theories_to_elements(theories: list[dict]) -> list[dict]:
    """Convert theory elements to defense_simulator input format.
    Uses matched_elements (satisfied=True) and missing_elements (satisfied=False).
    """
    elements = []
    for t in theories:
        theory_name = t.get("display_name", t.get("name", "?"))
        for el in t.get("matched_elements", []):
            elements.append({
                "name": f"{theory_name}: {el}",
                "satisfied": True,
                "fact_basis": f"Matched from case facts for {theory_name}",
                "evidence": t.get("triggers_hit", []),
            })
        for el in t.get("missing_elements", []):
            elements.append({
                "name": f"{theory_name}: {el}",
                "satisfied": False,
                "fact_basis": f"Not matched in available facts for {theory_name}",
                "evidence": [],
            })
    return elements


def case_to_filing_meta(case: dict) -> dict:
    """Derive filing metadata from case data for procedural_scan."""
    status = (case.get("status") or "").lower()
    court = case.get("court", "")
    case_type = case.get("case_type", "")

    is_federal = "fed" in case.get("canonical_id", "") or "EDPA" in court
    court_type = "federal" if is_federal else "state_pa"

    # Determine if verification required (PA state complaints/motions)
    requires_verification = not is_federal  # PA practice requires verification

    # Check removal
    was_removed = "removed" in status

    return {
        "court_type": court_type,
        "doc_type": "complaint",  # most filings start as complaints
        "requires_verification": requires_verification,
        "verified": True,  # assume our filings are verified (we're checking opposing)
        "certificate_of_service": True,
        "signed_by_authorized": True,
        "was_removed": was_removed,
        "removal_within_30_days": True,  # assume unless proven otherwise
    }


def run_phase4():
    cases = load_all_cases()
    print(f"Phase 4 (Dynamic): Validating {len(cases)} cases")
    print(f"{'='*70}\n")

    summary = {}

    for case in cases:
        cid = case["canonical_id"]
        cap = case.get("caption", cid)
        facts = case.get("facts", [])
        court = case.get("court", "EDPA" if "fed" in cid else "PA_CCP")

        print(f"{'='*70}")
        print(f"CASE: {cap}")
        print(f"  ID: {cid} | Court: {court}")
        print(f"  Facts: {len(facts)} | Claims: {case.get('expected_claims', [])}")
        print(f"{'='*70}")

        case_summary = {"case_id": cid, "caption": cap}

        # ── Step 0: Scenario Decompose (dynamic theory discovery) ──
        print("\n  [0] SCENARIO DECOMPOSE")
        try:
            decomp = scenario_decompose(facts, context={"target_court": court}, max_depth=1, min_confidence="low")
            theories = decomp.get("theories", [])
            print(f"      theories: {len(theories)}")
            for t in theories[:5]:
                print(f"        - {t.get('display_name', t.get('name', '?'))} (conf={t.get('confidence', '?')}, "
                      f"elements={len(t.get('matched_elements', []))}/{len(t.get('core_elements', []))}, "
                      f"seed_nodes={len(t.get('seed_nodes', []))})")
            case_summary["theories"] = len(theories)
        except Exception as e:
            print(f"      ERROR: {e}")
            theories = []
            case_summary["theories"] = f"FAIL: {e}"

        # ── Step 1: Baseline Memorandum (from theories) ──
        print("\n  [1] BASELINE MEMORANDUM")
        try:
            auths = theories_to_authorities(theories)
            duties = theories_to_duties(theories)
            print(f"      authorities derived: {len(auths)}")
            print(f"      duties derived: {len(duties)}")

            memo_result = build_memo(
                case_id=cid,
                caption=cap,
                controlling_authorities=auths,
                mandatory_duties=duties,
                write_markdown=True,
                write_docx=False,
            )
            print(f"      violation_ids: {memo_result['violation_ids']}")
            print(f"      markdown: {memo_result.get('markdown_path', 'N/A')}")
            case_summary["baseline_memo"] = "OK"
            case_summary["violation_ids"] = memo_result["violation_ids"]
            case_summary["authorities_count"] = len(auths)
            case_summary["duties_count"] = len(duties)
        except Exception as e:
            print(f"      ERROR: {e}")
            case_summary["baseline_memo"] = f"FAIL: {e}"

        # ── Step 2: Defense Simulator (from theory elements) ──
        print("\n  [2] DEFENSE SIMULATOR")
        try:
            elements = theories_to_elements(theories)
            authority_cites = [a["citation"] for a in theories_to_authorities(theories)]
            print(f"      elements derived: {len(elements)} (satisfied={sum(1 for e in elements if e['satisfied'])})")

            sim_result = simulate(
                elements=elements,
                authorities=authority_cites,
                facts=facts[:15],
                logic_chain=[],
                target_jurisdiction="EDPA" if "fed" in cid else "PA_CCP",
            )
            print(f"      total attacks: {sim_result['total']}")
            print(f"      blocking: {sim_result['blocking_count']}")
            print(f"      ready_for_filing: {sim_result['ready_for_filing']}")
            print(f"      by_strength: {sim_result['by_strength']}")
            case_summary["defense_sim"] = {
                "total": sim_result["total"],
                "blocking": sim_result["blocking_count"],
                "ready": sim_result["ready_for_filing"],
                "by_strength": sim_result["by_strength"],
            }
        except Exception as e:
            print(f"      ERROR: {e}")
            case_summary["defense_sim"] = f"FAIL: {e}"

        # ── Step 3: Procedural Scan (from case metadata) ──
        print("\n  [3] PROCEDURAL SCAN")
        try:
            filing_meta = case_to_filing_meta(case)
            print(f"      filing_meta: court={filing_meta['court_type']}, removed={filing_meta.get('was_removed',False)}")

            scan_result = scan(filing_meta)
            print(f"      total defects: {scan_result['total']}")
            print(f"      fatal: {scan_result['fatal_count']}")
            print(f"      summary: {scan_result['summary']}")
            for d in scan_result["defects"][:3]:
                print(f"        [{d['severity']}] {d['category']}: {d['defect']}")
            case_summary["procedural"] = {
                "total": scan_result["total"],
                "fatal": scan_result["fatal_count"],
            }
        except Exception as e:
            print(f"      ERROR: {e}")
            case_summary["procedural"] = f"FAIL: {e}"

        summary[cid] = case_summary
        print()

    # ── Final Summary ──
    print(f"\n{'='*70}")
    print("PHASE 4 SUMMARY (DYNAMIC)")
    print(f"{'='*70}")
    for cid, s in summary.items():
        print(f"\n  {s['caption']} ({cid})")
        print(f"    Theories:      {s.get('theories', '?')}")
        print(f"    Authorities:   {s.get('authorities_count', '?')}")
        print(f"    Duties:        {s.get('duties_count', '?')}")
        print(f"    Baseline memo: {s.get('baseline_memo', '?')}")
        ds = s.get("defense_sim", {})
        if isinstance(ds, dict):
            print(f"    Defense sim:   {ds.get('total',0)} attacks, {ds.get('blocking',0)} blocking, ready={ds.get('ready','?')}")
        else:
            print(f"    Defense sim:   {ds}")
        ps = s.get("procedural", {})
        if isinstance(ps, dict):
            print(f"    Procedural:    {ps.get('total',0)} defects, {ps.get('fatal',0)} fatal")
        else:
            print(f"    Procedural:    {ps}")

    out = (_BEEZLE_ROOT / "output" / "phase4_validation_dynamic.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print(f"\n  Saved: {out}")


if __name__ == "__main__":
    run_phase4()