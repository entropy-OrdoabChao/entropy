"""
BLOCK_9B_HERMES_SKILLS_PORT_2_OF_2
Procedural Attack Builder — defect scanner. Ported from hermes procedural-attack-builder skill.

Scans opposing filings / court actions for procedural defects across 4 categories:
  1. SERVICE        — improper service, wrong address, untimely
  2. FILING         — verification missing, no certificate of service, format defects
  3. TIMING         — late, missed deadline, premature
  4. STANDING/JURISDICTION — defects in those areas

Each defect → recommended motion + cited authority.

NO LLM. Pure rule lookup. Existing pipeline/procedural_engine.py (posture/role
detection) is untouched.
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
from dataclasses import dataclass, field, asdict
from typing import Optional, Literal
from datetime import datetime, date as _date
from pathlib import Path

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

log = get_logger("procedural_attack")

DefectCategory = Literal["SERVICE", "FILING", "TIMING", "JURISDICTION"]
Severity = Literal["MINOR", "MODERATE", "SERIOUS", "FATAL"]


@dataclass
class ProceduralDefect:
    category: DefectCategory
    defect: str               # what's wrong
    severity: Severity
    authority: list[str]      # rules violated
    recommended_motion: str   # specific motion to file
    motion_authority: list[str]
    notes: str = ""


@dataclass
class ScanResult:
    defects: list[ProceduralDefect] = field(default_factory=list)
    by_category: dict = field(default_factory=dict)
    by_severity: dict = field(default_factory=dict)
    fatal_count: int = 0
    summary: str = ""


# ──────────── Scanners ────────────

def scan_service(filing_meta: dict) -> list[ProceduralDefect]:
    """
    filing_meta keys (all optional):
      service_method, served_by, service_address, served_on, service_date,
      proof_of_service_filed, substituted_service, court_type ('federal'|'state_pa')
    """
    defects: list[ProceduralDefect] = []
    court = (filing_meta.get("court_type") or "federal").lower()

    method = (filing_meta.get("service_method") or "").lower()
    if method in ("email", "text", "fax", "verbal") and not filing_meta.get("consent_to_electronic_service"):
        defects.append(ProceduralDefect(
            category="SERVICE",
            defect=f"Service by {method} without consent",
            severity="SERIOUS",
            authority=["FRCP 5(b)(2)(E)"] if court == "federal" else ["Pa.R.C.P. 440(d)"],
            recommended_motion="Motion to Quash Service / Motion to Dismiss under FRCP 12(b)(5)",
            motion_authority=["FRCP 12(b)(5)"] if court == "federal" else ["Pa.R.C.P. 1028(a)(1)"],
        ))

    served_by = (filing_meta.get("served_by") or "").lower()
    if served_by and "party" in served_by:
        defects.append(ProceduralDefect(
            category="SERVICE",
            defect="Service made by a party (not authorized non-party)",
            severity="FATAL",
            authority=["FRCP 4(c)(2)"] if court == "federal" else ["Pa.R.C.P. 400"],
            recommended_motion="Motion to Quash Service",
            motion_authority=["FRCP 12(b)(5)"] if court == "federal" else ["Pa.R.C.P. 1028(a)(1)"],
        ))

    if filing_meta.get("proof_of_service_filed") is False:
        defects.append(ProceduralDefect(
            category="SERVICE",
            defect="No proof of service filed",
            severity="MODERATE",
            authority=["FRCP 4(l)"] if court == "federal" else ["Pa.R.C.P. 405"],
            recommended_motion="Motion to Compel Filing of Proof of Service or Quash",
            motion_authority=["FRCP 12(b)(5)"] if court == "federal" else ["Pa.R.C.P. 1028(a)(1)"],
        ))

    if filing_meta.get("substituted_service") and not filing_meta.get("substituted_service_authorized"):
        defects.append(ProceduralDefect(
            category="SERVICE",
            defect="Substituted service used without prior court authorization",
            severity="SERIOUS",
            authority=["Pa.R.C.P. 430"] if court != "federal" else ["FRCP 4(e)"],
            recommended_motion="Motion to Quash Service",
            motion_authority=["FRCP 12(b)(5)"] if court == "federal" else ["Pa.R.C.P. 1028(a)(1)"],
        ))

    return defects


def scan_filing(filing_meta: dict) -> list[ProceduralDefect]:
    """
    filing_meta keys (all optional):
      doc_type, verified, signed_by_authorized, certificate_of_service,
      proposed_order_attached (when required), within_page_limit,
      cover_sheet_filed (for complaints), court_type, requires_verification (bool)
    """
    defects: list[ProceduralDefect] = []
    court = (filing_meta.get("court_type") or "federal").lower()
    doc_type = (filing_meta.get("doc_type") or "").lower()

    # Verification (PA-specific for many doc types)
    if filing_meta.get("requires_verification") and filing_meta.get("verified") is False:
        defects.append(ProceduralDefect(
            category="FILING",
            defect=f"{doc_type or 'Pleading'} requires verification but is unverified",
            severity="FATAL",
            authority=["Pa.R.C.P. 1024"] if court != "federal" else ["FRCP 11"],
            recommended_motion="Motion to Strike Unverified Pleading",
            motion_authority=["Pa.R.C.P. 1028(a)(2)"] if court != "federal" else ["FRCP 12(f)"],
            notes="Unverified pleading where verification is required is a nullity in PA practice.",
        ))

    if filing_meta.get("signed_by_authorized") is False:
        defects.append(ProceduralDefect(
            category="FILING",
            defect="Filing not signed by authorized person",
            severity="SERIOUS",
            authority=["FRCP 11(a)"] if court == "federal" else ["Pa.R.C.P. 1023.1"],
            recommended_motion="Motion to Strike Unsigned Filing",
            motion_authority=["FRCP 11(a)"] if court == "federal" else ["Pa.R.C.P. 1028(a)(2)"],
        ))

    if filing_meta.get("certificate_of_service") is False:
        defects.append(ProceduralDefect(
            category="FILING",
            defect="No certificate of service",
            severity="MODERATE",
            authority=["FRCP 5(d)(1)(B)"] if court == "federal" else ["Pa.R.C.P. 440(d)"],
            recommended_motion="Motion to Strike for Lack of Service Certificate",
            motion_authority=["Local rules / inherent authority"],
        ))

    if "motion" in doc_type and filing_meta.get("proposed_order_attached") is False:
        defects.append(ProceduralDefect(
            category="FILING",
            defect="Motion filed without proposed order (when required)",
            severity="MINOR",
            authority=["Local rules vary"],
            recommended_motion="Object on next response / request court strike",
            motion_authority=["Inherent authority"],
            notes="Severity depends on local rule. Confirm requirement in jurisdiction.",
        ))

    if filing_meta.get("within_page_limit") is False:
        defects.append(ProceduralDefect(
            category="FILING",
            defect="Filing exceeds page limit without leave",
            severity="MODERATE",
            authority=["Local rules"],
            recommended_motion="Motion to Strike Excess Pages",
            motion_authority=["Local rules / inherent authority"],
        ))

    if doc_type == "complaint" and filing_meta.get("cover_sheet_filed") is False:
        defects.append(ProceduralDefect(
            category="FILING",
            defect="Complaint filed without civil cover sheet",
            severity="MINOR",
            authority=["Local rules"],
            recommended_motion="Object / request to cure",
            motion_authority=["Inherent authority"],
        ))

    return defects


def _parse_date(d) -> Optional[_date]:
    if isinstance(d, _date): return d
    if isinstance(d, str):
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%B %d, %Y"):
            try:
                return datetime.strptime(d, fmt).date()
            except ValueError:
                continue
    return None


def scan_timing(filing_meta: dict) -> list[ProceduralDefect]:
    """
    filing_meta keys (all optional):
      filed_date, deadline, response_deadline, filed_after_deadline (bool override),
      premature (bool), days_late (int)
    """
    defects: list[ProceduralDefect] = []

    filed = _parse_date(filing_meta.get("filed_date"))
    deadline = _parse_date(filing_meta.get("deadline"))
    days_late = filing_meta.get("days_late")

    if filing_meta.get("filed_after_deadline") or (filed and deadline and filed > deadline):
        late_by = (filed - deadline).days if filed and deadline else (days_late or "?")
        defects.append(ProceduralDefect(
            category="TIMING",
            defect=f"Filing late by {late_by} day(s)",
            severity="SERIOUS",
            authority=["Court scheduling order / applicable rule"],
            recommended_motion="Motion to Strike as Untimely / Motion to Bar",
            motion_authority=["FRCP 6(b)"] if (filing_meta.get("court_type") or "federal").lower() == "federal" else ["Pa.R.C.P. 248"],
        ))

    if filing_meta.get("premature"):
        defects.append(ProceduralDefect(
            category="TIMING",
            defect="Filing premature — submitted before triggering event / required notice period",
            severity="MODERATE",
            authority=["Applicable rule / scheduling order"],
            recommended_motion="Motion to Strike as Premature",
            motion_authority=["Inherent authority"],
        ))

    return defects


def scan_jurisdiction(filing_meta: dict) -> list[ProceduralDefect]:
    """Light wrapper — heavy lifting is in pipeline.jurisdiction_check."""
    defects: list[ProceduralDefect] = []
    if filing_meta.get("subject_matter_jurisdiction_lacking"):
        defects.append(ProceduralDefect(
            category="JURISDICTION",
            defect="Court lacks subject matter jurisdiction",
            severity="FATAL",
            authority=["28 U.S.C. § 1331", "28 U.S.C. § 1332"],
            recommended_motion="Motion to Dismiss for Lack of SMJ",
            motion_authority=["FRCP 12(b)(1)"],
            notes="Cannot be waived. Court must dismiss sua sponte if discovered.",
        ))
    if filing_meta.get("personal_jurisdiction_lacking"):
        defects.append(ProceduralDefect(
            category="JURISDICTION",
            defect="Court lacks personal jurisdiction over defendant",
            severity="FATAL",
            authority=["International Shoe v. Washington, 326 U.S. 310 (1945)"],
            recommended_motion="Motion to Dismiss for Lack of Personal Jurisdiction",
            motion_authority=["FRCP 12(b)(2)"],
            notes="Must be raised in first responsive pleading or waived.",
        ))
    return defects


def scan(filing_meta: dict) -> dict:
    """Run all defect scanners. Returns ScanResult as dict."""
    result = ScanResult()
    result.defects.extend(scan_service(filing_meta))
    result.defects.extend(scan_filing(filing_meta))
    result.defects.extend(scan_timing(filing_meta))
    result.defects.extend(scan_jurisdiction(filing_meta))

    by_cat: dict[str, int] = {}
    by_sev: dict[str, int] = {}
    fatal = 0
    for d in result.defects:
        by_cat[d.category] = by_cat.get(d.category, 0) + 1
        by_sev[d.severity] = by_sev.get(d.severity, 0) + 1
        if d.severity == "FATAL":
            fatal += 1
    result.by_category = by_cat
    result.by_severity = by_sev
    result.fatal_count = fatal

    if not result.defects:
        result.summary = "No procedural defects detected."
    else:
        result.summary = (
            f"{len(result.defects)} defect(s): "
            f"FATAL={fatal}, SERIOUS={by_sev.get('SERIOUS',0)}, "
            f"MODERATE={by_sev.get('MODERATE',0)}, MINOR={by_sev.get('MINOR',0)}"
        )

    log.info(f"procedural_scan: {len(result.defects)} defects, fatal={fatal}")
    return {
        "total": len(result.defects),
        "fatal_count": fatal,
        "by_category": result.by_category,
        "by_severity": result.by_severity,
        "summary": result.summary,
        "defects": [asdict(d) for d in result.defects],
    }


# ======================================================================
# PHASE_4B_AUDIT_ATTACK — Convert existing audit violations into
# procedural attacks with recommended motions.
#
# Two modes:
#   ATTACK mode: scan THEIR filings via audit data → motions to file
#   SELF-CHECK mode: scan OUR draft before filing → compliance issues
# ======================================================================

# Violation type → recommended motion mapping
VIOLATION_MOTION_MAP = {
    "NON_BINDING_AS_CONTROLLING": {
        "motion": "Motion to Strike Non-Binding Authority / Motion for Reconsideration",
        "basis": "Authority cited is not binding in this jurisdiction; opposing party presented persuasive authority as controlling",
        "authority_fed": ["FRCP 12(f)", "3d Cir. R. 28.3"],
        "authority_pa": ["Pa.R.C.P. 1028(a)(2)"],
        "severity": "SERIOUS",
        "category": "AUTHORITY",
    },
    "TRIAL_COURT_AS_APPELLATE_AUTHORITY": {
        "motion": "Motion for Reconsideration / Petition for Mandamus",
        "basis": "Trial court exceeded its authority by acting as appellate court; exceeded scope of review",
        "authority_fed": ["28 U.S.C. 1651 (All Writs)", "FRCP 60(b)(4) (void order)"],
        "authority_pa": ["42 Pa.C.S. 5505", "Pa.R.A.P. 1551"],
        "severity": "FATAL",
        "category": "JURISDICTION",
    },
    "OVERRULED_AUTHORITY": {
        "motion": "Motion to Strike Overruled Authority",
        "basis": "Cited authority has been overruled and no longer states the law",
        "authority_fed": ["FRCP 12(f)"],
        "authority_pa": ["Pa.R.C.P. 1028(a)(2)"],
        "severity": "SERIOUS",
        "category": "AUTHORITY",
    },
    "STARE_DECISIS_VIOLATION": {
        "motion": "Motion for Reconsideration Citing Binding Precedent",
        "basis": "Court's order conflicts with binding precedent from superior court",
        "authority_fed": ["Stare decisis doctrine", "FRCP 60(b)"],
        "authority_pa": ["42 Pa.C.S. 5505"],
        "severity": "FATAL",
        "category": "AUTHORITY",
    },
    "BIAS_INDICATOR": {
        "motion": "Motion for Recusal / Motion to Disqualify",
        "basis": "Judicial conduct indicates bias or appearance of impropriety",
        "authority_fed": ["28 U.S.C. 455(a)", "28 U.S.C. 144"],
        "authority_pa": ["Pa. Code of Judicial Conduct Canon 2"],
        "severity": "FATAL",
        "category": "JURISDICTION",
    },
    "DUE_PROCESS_VIOLATION": {
        "motion": "Motion to Vacate for Due Process Violation",
        "basis": "Order entered without adequate notice or opportunity to be heard",
        "authority_fed": ["U.S. Const. Amend. V", "U.S. Const. Amend. XIV", "FRCP 60(b)(4)"],
        "authority_pa": ["Pa. Const. Art. I Sec. 9", "42 Pa.C.S. 5505"],
        "severity": "FATAL",
        "category": "JURISDICTION",
    },
    "FILING_RESTRICTION": {
        "motion": "Motion to Vacate Filing Restriction / Petition for Mandamus",
        "basis": "Court cannot restrict pro se litigant right to file motions absent formal vexatious litigant finding with due process",
        "authority_fed": ["U.S. Const. Amend. I (Petition Clause)", "28 U.S.C. 1651", "In re Oliver, 682 F.2d 443 (3d Cir. 1982)"],
        "authority_pa": ["Pa. Const. Art. I Sec. 11 (Open Courts)", "Pa. Const. Art. I Sec. 9"],
        "severity": "FATAL",
        "category": "JURISDICTION",
    },
}


def scan_audit_violations(audit_data: dict, court_type: str = "federal") -> dict:
    """Convert audit violations into procedural attacks.

    audit_data: the full audit dict from output/audit/data_*.json
    court_type: 'federal' | 'state_pa'

    Returns same shape as scan() — {total, fatal_count, by_category, by_severity, summary, defects}
    """
    defects = []

    # Source 1: hierarchy violations (detailed)
    hierarchy_violations = []
    if isinstance(audit_data.get("hierarchy"), dict):
        hierarchy_violations = audit_data["hierarchy"].get("all", [])

    # Source 2: scorecard violations (may have extras)
    scorecard_violations = []
    if isinstance(audit_data.get("scorecard"), dict):
        scorecard_violations = audit_data["scorecard"].get("violations", [])

    # Deduplicate by (violation_type, citation, source_file)
    seen = set()

    for v in hierarchy_violations:
        vtype = v.get("violation_type", "UNKNOWN")
        cite = v.get("citation", "")
        src = v.get("source_file", "")
        key = (vtype, cite, src)
        if key in seen:
            continue
        seen.add(key)

        mapping = VIOLATION_MOTION_MAP.get(vtype)
        if not mapping:
            # Unknown violation type — still record it
            defects.append(ProceduralDefect(
                category="FILING",
                defect=v.get("explanation", f"Violation: {vtype}"),
                severity="MODERATE",
                authority=[cite] if cite else [],
                recommended_motion=v.get("fix", "Review and challenge"),
                motion_authority=["Court's inherent authority"],
                notes=f"Source: {src}",
            ))
            continue

        auth_key = "authority_fed" if court_type == "federal" else "authority_pa"
        defects.append(ProceduralDefect(
            category=mapping["category"],
            defect=f"{mapping['basis']} — cited: {cite}" if cite else mapping["basis"],
            severity=mapping["severity"],
            authority=mapping.get(auth_key, []),
            recommended_motion=mapping["motion"],
            motion_authority=mapping.get(auth_key, []),
            notes=f"Source: {src} | Fix: {v.get('fix', '')}",
        ))

    # Also check dismissal entries for reversibility signals
    for d in audit_data.get("dismissal", []):
        summary = d.get("summary", "")
        src = d.get("source", "")
        if "BIAS" in summary.upper():
            bias_key = ("BIAS", "", src)
            if bias_key not in seen:
                seen.add(bias_key)
                mapping = VIOLATION_MOTION_MAP["BIAS_INDICATOR"]
                auth_key = "authority_fed" if court_type == "federal" else "authority_pa"
                defects.append(ProceduralDefect(
                    category="JURISDICTION",
                    defect=f"Bias indicator detected in {Path(src).name if src else 'filing'}",
                    severity="FATAL",
                    authority=mapping.get(auth_key, []),
                    recommended_motion=mapping["motion"],
                    motion_authority=mapping.get(auth_key, []),
                    notes=f"Source: {src} | Summary: {summary}",
                ))

    # Build result in same format as scan()
    by_cat = {}
    by_sev = {}
    fatal = 0
    for d in defects:
        by_cat[d.category] = by_cat.get(d.category, 0) + 1
        by_sev[d.severity] = by_sev.get(d.severity, 0) + 1
        if d.severity == "FATAL":
            fatal += 1

    total = len(defects)
    if not defects:
        summary_text = "No attackable violations found in opposing filings."
    else:
        summary_text = (
            f"{total} attackable defect(s) in opposing filings: "
            f"FATAL={fatal}, SERIOUS={by_sev.get('SERIOUS',0)}, "
            f"MODERATE={by_sev.get('MODERATE',0)}, MINOR={by_sev.get('MINOR',0)}"
        )

    log.info(f"audit_attack: {total} defects from {len(hierarchy_violations)} violations, fatal={fatal}")
    return {
        "mode": "ATTACK",
        "total": total,
        "fatal_count": fatal,
        "by_category": by_cat,
        "by_severity": by_sev,
        "summary": summary_text,
        "defects": [asdict(d) for d in defects],
    }


def verify_court_findings(ground_truth: dict, court_type: str = "federal") -> list:
    """Agnostic finding-verifier. Reads structured fact-blocks documenting a
    court-asserted procedural failure the record refutes; emits a defect per
    refuted finding. Works for any case whose ground truth carries such a block."""
    out = []
    sf = (ground_truth or {}).get("service_facts")
    if isinstance(sf, dict) and sf.get("the_judge_s_error"):
        out.append(ProceduralDefect(
            category="DEFECTIVE_SERVICE",
            defect=("Court's failure-of-service finding is contradicted by the "
                    "record: " + str(sf.get("the_judge_s_error", ""))[:400]),
            severity="FATAL",
            authority=list(sf.get("controlling_authority", []) or []),
            recommended_motion="Motion to Vacate void order — service established of record",
            motion_authority=["Fed. R. Civ. P. 60(b)(4)",
                              "United Student Aid Funds, Inc. v. Espinosa, 559 U.S. 260 (2010)"],
            notes=("FINDING_VERIFIER_V1: service finding refuted by service_facts "
                   "(proof of service + good-address corroboration + mailbox presumption)."),
        ))
    return out


def load_and_scan_audit(case_id: str, court_type: str = "federal") -> dict:
    """Convenience: load audit data for a case and run attack scan."""
    import json as _json
    safe = case_id.replace(":", "_").replace("/", "_")
    _audit_wsl = Path(f"<home>/output/audit/data_{safe}.json")
    _audit_win = Path(str(_BEEZLE_ROOT / "output" / "audit" / f"data_{safe}.json"))
    audit_path = _audit_wsl if _audit_wsl.parent.exists() else _audit_win
    if not audit_path.exists():
        return {"error": f"No audit data found at {audit_path}", "total": 0, "defects": []}
    audit_data = _json.loads(audit_path.read_text(encoding="utf-8"))
    result = scan_audit_violations(audit_data, court_type)
    # FINDING_VERIFIER_V1: merge record-refuted court findings (e.g. service)
    try:
        import yaml as _yaml
        slug = case_id.split(":", 1)[1].replace(":", "-") if ":" in case_id else case_id
        slug = "fed_" + slug.split("-cv-")[0].split(":")[-1] + "-cv-" + slug.split("-cv-")[1] if "-cv-" in slug else slug
        gt_path = Path(f"<home>/obsidian_sync/cases/{slug}/meta/CASE_GROUND_TRUTH.yaml")
        if not gt_path.exists():
            # fallback: scan for any matching case folder
            import glob as _glob
            cand = _glob.glob(f"<home>/obsidian_sync/cases/*/meta/CASE_GROUND_TRUTH.yaml")
            for c in cand:
                gt = _yaml.safe_load(Path(c).read_text(encoding="utf-8")) or {}
                if str(gt.get("case_id","")) == case_id:
                    gt_path = Path(c); break
        if gt_path.exists():
            gt = _yaml.safe_load(gt_path.read_text(encoding="utf-8")) or {}
            extra = verify_court_findings(gt, court_type)
            if extra:
                from dataclasses import asdict as _asdict
                result.setdefault("defects", []).extend(_asdict(d) for d in extra)
                result["total"] = len(result["defects"])
                for d in extra:
                    result.setdefault("by_category", {})[d.category] = result.get("by_category", {}).get(d.category, 0) + 1
                    if d.severity == "FATAL":
                        result["fatal_count"] = result.get("fatal_count", 0) + 1
    except Exception as _e:
        log.info(f"finding_verifier skipped: {_e}")
    return result
# ======================================================================
# /PHASE_4B_AUDIT_ATTACK
# ======================================================================
