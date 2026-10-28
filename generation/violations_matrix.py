"""
violations_matrix.py — Block 6.9
Generates a Violations Matrix exhibit (.docx + .md) classifying every
duty in the engine output by class:
  - Statutory Violations
  - Common Law Violations
  - Regulatory Violations
  - Procedural Violations (from order_engine doctrines)

Pulls verbatim from claim.attached_duties[].class + .source + .source_title.
ZERO LLM. Deterministic. Cites only what's already in the duty maps.

Drop into: <home>\\generation\\violations_matrix.py
Called from drafter when mode in ("pipeline", "hybrid").
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import logging
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple, Optional

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

log = logging.getLogger("violations_matrix")

# ─── Class → Section heading + introductory language ─────────────────────
CLASS_SECTIONS = {
    "statutory":         ("STATUTORY VIOLATIONS",
                          "Defendant's conduct violates the following statutory duties:"),
    "regulatory":        ("REGULATORY VIOLATIONS",
                          "Defendant's conduct violates the following regulatory duties:"),
    "industry_standard": ("INDUSTRY STANDARD VIOLATIONS",
                          "Defendant's conduct departs from the following industry standards:"),
    "fiduciary":         ("FIDUCIARY VIOLATIONS",
                          "Defendant breached the following fiduciary duties:"),
    "common_law":        ("COMMON LAW VIOLATIONS",
                          "Defendant's conduct violates the following common-law duties:"),
    "procedural":        ("PROCEDURAL VIOLATIONS",
                          "Defendant's conduct violates the following rules of procedure:"),
    "constitutional":    ("CONSTITUTIONAL VIOLATIONS",
                          "Defendant's conduct violates the following procedural due process protections:"),
}
CLASS_ORDER = ["statutory", "regulatory", "fiduciary", "common_law",
               "industry_standard", "procedural", "constitutional"]


# === MATRIX_REFINEMENT_V1 ===
# Per-case_type domain whitelist. Duties whose `applies_to_domains` does NOT
# intersect with this whitelist are STRIPPED before matrix rendering.
# Override per-case via registry["allowed_domains"] = ["domain1", "domain2"].
CASE_TYPE_ALLOWED_DOMAINS = {
    "contracts_other": [
        "contract", "tort", "equity", "property", "quasi_contract",
        "consumer_protection", "procedural", "constitutional",
    ],
    "fraud_fcra": [
        "consumer_finance", "statutory", "consumer_protection",
        "procedural", "constitutional",
    ],
    "quiet_title": [
        "real_property", "contract", "equity", "consumer_finance",
        "procedural", "constitutional",
    ],
    "bad_faith": [
        "insurance", "contract", "statutory", "consumer_protection",
        "consumer_finance", "procedural", "constitutional",
    ],
    "respa": [
        "real_property", "consumer_finance", "contract",
        "procedural", "constitutional",
    ],
    "tila": [
        "consumer_finance", "contract",
        "procedural", "constitutional",
    ],
}

# Per case_type tier assignment.
# TIER 1 = case-winning core
# TIER 2 = supporting statutory/regulatory
# TIER 3 = supplemental / adjacent
CASE_TYPE_TIERS = {
    "contracts_other": {
        1: ["common_law"],                            # contracts/torts core
        2: ["statutory", "procedural", "constitutional"],
        3: ["regulatory", "fiduciary", "industry_standard"],
    },
    "fraud_fcra": {
        1: ["statutory"],                             # FCRA/FDCPA core
        2: ["regulatory", "procedural", "constitutional"],
        3: ["common_law", "fiduciary", "industry_standard"],
    },
    "quiet_title": {
        1: ["common_law", "statutory"],               # title doctrine + RESPA
        2: ["regulatory", "procedural", "constitutional"],
        3: ["fiduciary", "industry_standard"],
    },
    "bad_faith": {
        1: ["statutory"],                             # 42 Pa.C.S. 8371 core
        2: ["common_law", "fiduciary", "regulatory", "procedural", "constitutional"],
        3: ["industry_standard"],
    },
}

# Vague filler entries to STRIP before matrix render.
_VAGUE_DROPLIST = {
    "Regulatory-Compliance-Cross-Cutting",
    # add more here as patterns emerge
}

# Constitutional triggers — fact text MUST contain at least one of these
# for the constitutional section to render. Narrow by design.
_CONSTITUTIONAL_TRIGGERS = (
    "no proper service",
    "lack of jurisdiction",
    "no notice",
    "due process violation",
    "defective service",
)


# === MATRIX_REFINEMENT_V2 ===
# Source-prefix blacklist per case_type. Strips duties whose source begins
# with any listed prefix. Use to surgically prevent cross-case bleed when
# domain filtering is too coarse (e.g. consumer_protection overlap pulls
# FCRA into a contract case via UTPCPL).
CASE_TYPE_DISALLOWED_SOURCES = {
    "contracts_other": [
        "15-USC-1681",   # FCRA
        "15-USC-1692",   # FDCPA
        "12-USC-2605",   # RESPA QWR
        "15-USC-1601",   # TILA
    ],
    "fraud_fcra": [
        # in FCRA cases, allow everything FCRA-related; strip mortgage/insurance
        "12-USC-2605",
        "12-CFR-1024",
    ],
    "quiet_title": [
        "15-USC-1681",   # FCRA
        "42-Pa-CS-8371", # PA bad faith
    ],
    "bad_faith": [
        "15-USC-1681",   # FCRA stays out of bad-faith insurance cases
        "12-USC-2605",   # RESPA stays out
    ],
}


def _strip_disallowed_sources(by_class, case_type):
    """Remove violations whose source starts with any blacklisted prefix."""
    blacklist = CASE_TYPE_DISALLOWED_SOURCES.get(case_type, [])
    if not blacklist:
        return by_class
    out = {}
    dropped = []
    for cls, items in by_class.items():
        kept = []
        for v in items:
            src_ = (v.get("source") or "").strip()
            if any(src_.startswith(prefix) for prefix in blacklist):
                dropped.append(src_)
                continue
            kept.append(v)
        if kept:
            out[cls] = kept
    if dropped:
        log.info(f"blacklisted {len(dropped)} sources for case_type={case_type!r}: {dropped}")
    return out


def _inject_case_level_constitutional(by_class, facts_text):
    """If due-process triggers fire, load constitutional duty_map directly
    and add all duties as a synthetic case-level violation set. Bypasses
    claim-domain matching (which is wrong for constitutional — applies to
    case posture, not specific claims).
    """
    if not _has_constitutional_triggers(facts_text):
        return by_class

    # Find which triggers actually fired
    fl = facts_text.lower() if facts_text else ""
    fired = [t for t in _CONSTITUTIONAL_TRIGGERS if t in fl]

    # Load constitutional duty_map
    try:
        import yaml as _yaml
        from pathlib import Path as _P
        cmap_path = _P(str((_BEEZLE_ROOT / "pipeline" / "duty_maps" / "constitutional.yaml")))
        if not cmap_path.exists():
            log.warning("constitutional.yaml not found; skipping case-level injection")
            return by_class
        cmap = _yaml.safe_load(cmap_path.read_text(encoding="utf-8"))
    except Exception as _e:
        log.warning(f"constitutional.yaml load failed: {_e}")
        return by_class

    cduties = cmap.get("duties", []) or []
    if not cduties:
        return by_class

    # Build case-level constitutional entries
    const_entries = []
    for d in cduties:
        const_entries.append({
            "source": d.get("source") or d.get("id", ""),
            "source_title": d.get("source_title") or d.get("name", ""),
            "name": d.get("name", ""),
            "jurisdiction": d.get("jurisdiction", ""),
            "claim_ids": [f"(case-level due process: {', '.join(fired)})"],
            "regulator": d.get("regulator", ""),
        })

    by_class.setdefault("constitutional", []).extend(const_entries)
    log.info(f"case-level constitutional injection: {len(const_entries)} duties added (triggers fired: {fired})")
    return by_class
# === END MATRIX_REFINEMENT_V2 ===


def _has_constitutional_triggers(fact_text: str) -> bool:
    """True iff fact_text contains at least one canonical due-process trigger."""
    if not fact_text:
        return False
    fl = fact_text.lower()
    return any(t in fl for t in _CONSTITUTIONAL_TRIGGERS)


# === ENFORCEMENT_LAYER_V1 ===
# Per-violation enforcement directive language. Tiered register:
# ALL CAPS only for void/constitutional outcomes; lowercase shall/must elsewhere.

def _enforcement_for_violation(v: dict, cls: str) -> str:
    """Return the per-violation `>>> RELIEF:` directive sentence."""
    src_   = (v.get("source") or "").strip()
    jur    = (v.get("jurisdiction") or "").lower().strip()
    title  = (v.get("source_title") or v.get("name") or src_).strip()

    # BLOCK_6_13_POSTURE_LOCK — posture-dispatched void/voidable relief
    if jur in ("void",):
        from generation.posture_templates import MATRIX_VOID_RELIEF as _MVR, get as _pget
        _p = globals().get("_BEEZLE_POSTURE", "merits")
        return _pget(_MVR, _p)
    if jur in ("voidable",):
        return (">>> RELIEF MANDATED: The cited authority establishes "
                "voidability on the grounds stated; the Court shall act "
                "accordingly upon proper motion.")

    # CONSTITUTIONAL — ALL CAPS, non-discretionary
    if cls == "constitutional":
        # BLOCK_6_13_POSTURE_LOCK — posture-dispatched constitutional relief
        from generation.posture_templates import MATRIX_CONST_RELIEF as _MCR, get as _pget
        _p = globals().get("_BEEZLE_POSTURE", "merits")
        return _pget(_MCR, _p)

    # PROCEDURAL (non-void) — "shall strike" / required action
    if cls == "procedural":
        return (f">>> RELIEF: The Court shall strike the defective filing "
                f"and enforce the rule as written under {src_}.")

    # STATUTORY — bound to remedy
    if cls == "statutory":
        return (f">>> RELIEF: The Court is bound to enforce the remedy "
                f"prescribed by {src_}, including statutory damages, "
                f"interest, and fees where authorized.")

    # REGULATORY — shall enforce
    if cls == "regulatory":
        return (f">>> RELIEF: The regulatory duty under {src_} is bound on "
                f"defendant; the Court shall require compliance and award "
                f"any damages or equitable relief flowing from breach.")

    # FIDUCIARY — disgorgement / restoration
    if cls == "fiduciary":
        return (f">>> RELIEF: Defendant breached a fiduciary duty under "
                f"{title}; the Court shall require disgorgement of any "
                f"benefit retained and restoration of plaintiff's position.")

    # COMMON LAW — relief stated
    if cls == "common_law":
        return (f">>> RELIEF: The Court shall award the remedies stated by "
                f"{src_}, including damages and any equitable relief that "
                f"the doctrine authorizes.")

    # INDUSTRY STANDARD — persuasive
    if cls == "industry_standard":
        return (f">>> RELIEF: The industry standard under {src_} is "
                f"persuasive on the standard of care; the Court should "
                f"weigh it accordingly in fashioning relief.")

    # default fallback
    return (f">>> RELIEF: The Court shall enforce the duty stated in "
            f"{src_} consistent with applicable law.")


def _section_relief_summary(cls: str, count: int) -> str:
    """One-line consolidated relief statement at the END of each tier section."""
    if cls == "constitutional":
        # BLOCK_6_13_POSTURE_LOCK — posture-dispatched section summary
        from generation.posture_templates import MATRIX_CONST_SECTION_SUMMARY as _MCSS, get as _pget
        _p = globals().get("_BEEZLE_POSTURE", "merits")
        return _pget(_MCSS, _p).format(count=count)
    if cls == "procedural":
        return (f">>> SECTION RELIEF SUMMARY: {count} procedural rule "
                f"violation(s) established. The Court shall enforce each "
                f"rule as written; defendant carries the burden of "
                f"showing compliance.")
    if cls == "statutory":
        return (f">>> SECTION RELIEF SUMMARY: {count} statutory violation(s) "
                f"established. Each carries a mandatory remedy under the "
                f"cited statute; the Court shall enforce all such remedies.")
    if cls == "regulatory":
        return (f">>> SECTION RELIEF SUMMARY: {count} regulatory violation(s) "
                f"established. The Court shall require compliance and "
                f"award damages flowing from breach.")
    if cls == "fiduciary":
        return (f">>> SECTION RELIEF SUMMARY: {count} fiduciary breach(es) "
                f"established. The Court shall require disgorgement and "
                f"restoration on each.")
    if cls == "common_law":
        return (f">>> SECTION RELIEF SUMMARY: {count} common-law violation(s) "
                f"established. The Court shall award the remedies authorized "
                f"by the cited doctrines.")
    return (f">>> SECTION RELIEF SUMMARY: {count} violation(s) "
            f"established in this section.")


def _consolidated_relief_demand(by_class: dict) -> list:
    """Build the bottom-of-matrix CONSOLIDATED RELIEF DEMAND (list of strings)."""
    out = []
    out.append("CONSOLIDATED RELIEF DEMANDED")
    out.append("")
    out.append(
        "Based on the violations established in the tiered sections above, "
        "plaintiff demands the following relief, organized by mandatory "
        "vs. discretionary character:"
    )
    out.append("")

    # Mandatory (void/constitutional)
    void_count = 0
    const_count = len(by_class.get("constitutional") or [])
    for v in by_class.get("procedural") or []:
        if (v.get("jurisdiction") or "").lower() == "void":
            void_count += 1

    # BLOCK_6_13_2_POSTURE_FINAL_POLISH — posture mandatory-relief fallback
    _bz_posture_for_mr = globals().get("_BEEZLE_POSTURE", "merits")
    if const_count or void_count:
        out.append("MANDATORY RELIEF (NO DISCRETION):")
        if not (const_count or void_count) is False:
            try:
                from generation.posture_templates import MATRIX_MANDATORY_FALLBACK as _BZ_MMF
                _bz_fb = _BZ_MMF.get(_bz_posture_for_mr)
                if _bz_fb and const_count == 0 and void_count == 0:
                    out.append(_bz_fb)
            except Exception:
                pass
        # BLOCK_6_13_POSTURE_LOCK — posture-dispatched consolidated vacatur
        from generation.posture_templates import (
            MATRIX_CONSOLIDATED_VACATUR as _MCV,
            MATRIX_CONSOLIDATED_VOID as _MCVD,
            get as _pget,
        )
        _p = globals().get("_BEEZLE_POSTURE", "merits")
        if const_count:
            _line = _pget(_MCV, _p)
            if _line:
                out.append(_line.format(count=const_count))
        if void_count:
            _line = _pget(_MCVD, _p)
            if _line:
                out.append(_line.format(count=void_count))
        out.append("")

    # Procedural rule remedies
    proc_rules = [
        v for v in (by_class.get("procedural") or [])
        if (v.get("jurisdiction") or "").lower() not in ("void", "voidable")
    ]
    if proc_rules:
        out.append("PROCEDURAL RELIEF (rule-bound):")
        for v in proc_rules:
            src_ = v.get("source", "")
            out.append(f"  - Strike or compel compliance under {src_}.")
        out.append("")

    # Statutory remedies
    stat = by_class.get("statutory") or []
    if stat:
        out.append("STATUTORY RELIEF (mandatory remedies):")
        for v in stat:
            src_ = v.get("source", "")
            out.append(f"  - Enforce statutory remedy under {src_}, "
                       f"including damages, interest, and fees where authorized.")
        out.append("")

    # Common-law / equitable
    cl = by_class.get("common_law") or []
    if cl:
        out.append("COMMON-LAW / EQUITABLE RELIEF:")
        for v in cl:
            src_ = v.get("source", "")
            out.append(f"  - Award damages and equitable relief authorized by {src_}.")
        out.append("")

    # Fiduciary
    fid = by_class.get("fiduciary") or []
    if fid:
        out.append("FIDUCIARY RELIEF (disgorgement / restoration):")
        for v in fid:
            src_ = v.get("source", "")
            out.append(f"  - Require disgorgement and restoration based on breach of {src_}.")
        out.append("")

    # Regulatory
    reg = by_class.get("regulatory") or []
    if reg:
        out.append("REGULATORY RELIEF (compliance + damages):")
        for v in reg:
            src_ = v.get("source", "")
            out.append(f"  - Require compliance with {src_} and award resulting damages.")
        out.append("")

    # === BLOCK_6_13_2_POSTURE_FINAL_POLISH — posture-dispatched closing line ===
    from generation.posture_templates import MATRIX_CLOSING_LINE as _BZ_MCL, get as _bz_get
    _bz_p = globals().get("_BEEZLE_POSTURE", "merits")
    out.append(_bz_get(_BZ_MCL, _bz_p))
    return out
# === END ENFORCEMENT_LAYER_V1 ===


def _resolve_allowed_domains(canonical_id: str, case_type: str) -> set:
    """Resolve allowed_domains per case. Registry override wins over default."""
    try:
        import json as _json
        from pathlib import Path as _P
        reg_path = _P(str((_BEEZLE_ROOT / "cases_index" / "case_registry.json")))
        if reg_path.exists():
            reg = _json.loads(reg_path.read_text(encoding="utf-8"))
            entry = reg.get(canonical_id, {})
            override = entry.get("allowed_domains")
            if isinstance(override, list) and override:
                return {d.lower() for d in override}
    except Exception:
        pass
    defaults = CASE_TYPE_ALLOWED_DOMAINS.get(case_type, [])
    if not defaults:
        # fall back to allowing everything if case_type unknown
        return set()  # empty = no filter applied
    return {d.lower() for d in defaults}


def _filter_by_case_domains(by_class: Dict[str, List[Dict]],
                            claims: List[Dict],
                            allowed: set) -> Dict[str, List[Dict]]:
    """Drop violations whose claim's domains do NOT intersect allowed.
    If allowed is empty (unknown case_type) returns by_class unchanged.
    """
    if not allowed:
        return by_class

    # Build claim_id -> domains lookup
    claim_domains = {}
    for c in claims or []:
        cid = c.get("id") or c.get("theory_id")
        if cid:
            claim_domains[cid] = {d.lower() for d in (c.get("domains") or [])}

    filtered: Dict[str, List[Dict]] = {}
    for cls, items in by_class.items():
        kept = []
        for v in items:
            # Check the claims this violation came from
            asserting = v.get("claim_ids") or []
            if not asserting:
                # No claim association — keep (e.g., pure procedural doctrines)
                kept.append(v)
                continue
            # Keep if ANY asserting claim has a domain in allowed
            keep = False
            for cid in asserting:
                cd = claim_domains.get(cid, set())
                if cd & allowed:
                    keep = True
                    break
            if keep:
                kept.append(v)
        if kept:
            filtered[cls] = kept
    return filtered


def _strip_vague(by_class: Dict[str, List[Dict]]) -> Dict[str, List[Dict]]:
    """Remove entries whose source matches _VAGUE_DROPLIST."""
    out: Dict[str, List[Dict]] = {}
    for cls, items in by_class.items():
        kept = [v for v in items if v.get("source") not in _VAGUE_DROPLIST]
        if kept:
            out[cls] = kept
    return out


def _tier_for_class(case_type: str, cls: str) -> int:
    """Return tier (1/2/3) for a class given case_type. Default = 2."""
    tiers = CASE_TYPE_TIERS.get(case_type, {})
    for tier in (1, 2, 3):
        if cls in tiers.get(tier, []):
            return tier
    return 2  # default mid-tier


def _section_heading_with_tier(case_type: str, cls: str, base_heading: str) -> str:
    """Prefix base_heading with TIER N — based on case_type."""
    tier = _tier_for_class(case_type, cls)
    return f"TIER {tier} — {base_heading}"
# === END MATRIX_REFINEMENT_V1 ===


def collect_violations(claims: List[Dict],
                       doctrines: List[Dict]) -> Dict[str, List[Dict]]:
    """Return {class_key: [violation_dict, ...]} aggregated across all claims.

    Each violation_dict:
        {
          "source": "42-Pa-CS-8371",
          "source_title": "PA Bad Faith Insurance Statute",
          "name": "Bad Faith Prohibition (PA)",
          "jurisdiction": "pa",
          "claim_ids": ["bad_faith_insurance_pa", "breach_of_insurance_contract"],
          "regulator": "PA_insurance_department",
        }
    """
    by_class: Dict[str, Dict[str, Dict]] = defaultdict(dict)

    for claim in claims or []:
        cid = claim.get("id") or claim.get("theory_id") or "unknown"
        for d in claim.get("attached_duties") or []:
            cls = (d.get("class") or "").lower().strip()
            if not cls:
                continue
            src = d.get("source") or d.get("citation") or d.get("id") or ""
            if not src:
                continue
            existing = by_class[cls].get(src)
            if existing:
                if cid not in existing["claim_ids"]:
                    existing["claim_ids"].append(cid)
            else:
                by_class[cls][src] = {
                    "source": src,
                    "source_title": d.get("source_title") or d.get("name", ""),
                    "name": d.get("name", ""),
                    "jurisdiction": d.get("jurisdiction", ""),
                    "claim_ids": [cid],
                    "regulator": d.get("regulator", ""),
                }

    # Procedural doctrines as their own pseudo-class
    if doctrines:
        for doc in doctrines:
            did = doc.get("id", "")
            if not did:
                continue
            existing = by_class["procedural"].get(did) if "procedural" in by_class else None
            entry = {
                "source": did,
                "source_title": doc.get("display_name") or did,
                "name": doc.get("display_name", ""),
                "jurisdiction": doc.get("classification", ""),
                "claim_ids": [],
                "regulator": "",
            }
            by_class["procedural"][did] = entry

    # Convert inner dicts → lists, sorted by source
    return {
        cls: sorted(entries.values(), key=lambda x: x["source"])
        for cls, entries in by_class.items()
    }


def render_md(canonical_id: str, caption: str,
              by_class: Dict[str, List[Dict]]) -> str:
    """Render Violations Matrix as Markdown."""
    out = []
    out.append(f"# VIOLATIONS MATRIX")
    out.append("")
    out.append(f"**Case:** {caption}")
    out.append(f"**Docket:** {canonical_id}")
    out.append(f"**Generated:** {datetime.utcnow().isoformat()}Z")
    out.append("")

    total = sum(len(v) for v in by_class.values())
    out.append(f"**Total distinct violations identified:** {total}")
    out.append("")

    # Stats by class
    for cls in CLASS_ORDER:
        if cls in by_class and by_class[cls]:
            heading, _ = CLASS_SECTIONS.get(cls, (cls.upper(), ""))
            out.append(f"- **{heading}**: {len(by_class[cls])}")
    if "procedural" in by_class and by_class["procedural"]:
        out.append(f"- **PROCEDURAL ATTACK VECTORS**: {len(by_class['procedural'])}")
    out.append("")

    # === MATRIX_REFINEMENT_V1: tier-prefixed headings ===
    case_type = getattr(render_md, "_active_case_type", "")  # threaded via attr
    for cls in CLASS_ORDER:
        items = by_class.get(cls) or []
        if not items:
            continue
        base_heading, intro = CLASS_SECTIONS[cls]
        heading = _section_heading_with_tier(case_type, cls, base_heading)
        out.append(f"## {heading}")
        out.append("")
        out.append(intro)
        out.append("")
        for v in items:
            out.append(f"- **{v['source']}** — {v['source_title']}")
            if v["name"] and v["name"] != v["source_title"]:
                out.append(f"  - Duty: {v['name']}")
            if v["jurisdiction"]:
                out.append(f"  - Jurisdiction: {v['jurisdiction']}")
            if v["regulator"]:
                out.append(f"  - Regulator: {v['regulator']}")
            if v["claim_ids"]:
                out.append(f"  - Asserted in claim(s): "
                           f"{', '.join(v['claim_ids'])}")
            # === ENFORCEMENT_LAYER_V1 ===
            out.append(f"  - {_enforcement_for_violation(v, cls)}")
        # === ENFORCEMENT_LAYER_V1: section relief summary ===
        out.append("")
        out.append(_section_relief_summary(cls, len(items)))
        out.append("")

    # Procedural section
    if by_class.get("procedural"):
        out.append("## PROCEDURAL ATTACK VECTORS")
        out.append("")
        out.append("The following procedural doctrines support attack on the "
                   "prior order or judgment:")
        out.append("")
        for v in by_class["procedural"]:
            out.append(f"- **{v['source']}** — {v['source_title']} "
                       f"[{v['jurisdiction']}]")
        out.append("")

    # === ENFORCEMENT_LAYER_V1: consolidated relief demand ===
    out.append("---")
    out.append("")
    for line in _consolidated_relief_demand(by_class):
        if not line:
            out.append("")
        elif line.startswith(("MANDATORY", "PROCEDURAL", "STATUTORY",
                              "COMMON-LAW", "FIDUCIARY", "REGULATORY",
                              "CONSOLIDATED")):
            out.append(f"### {line}")
        else:
            out.append(line)
    out.append("")
    out.append("---")
    out.append("")
    out.append("**CRIMINAL EXPOSURE NOTE:** This matrix excludes Title 18 (federal "
               "criminal) statutes by design. Criminal exposure is reserved for "
               "separate referral packages (DOJ, state AG, regulator complaints) "
               "and is not actionable in civil filings. See Beezle Block X "
               "(Criminal Exposure Engine, future).")
    out.append("")
    out.append("*Generated deterministically by Beezle violations_matrix engine. "
               "Verify each citation in CourtListener / Lexis / Westlaw before "
               "filing. Each duty is bound to its source via duty_maps; no LLM "
               "interpretation was performed.*")
    return "\n".join(out)


def render_docx(canonical_id: str, caption: str,
                by_class: Dict[str, List[Dict]],
                output_path: Path) -> Path:
    """Render Violations Matrix as .docx."""
    from docx import Document as DocxDocument
    from docx.shared import Pt, Inches, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING

    doc = DocxDocument()
    for section in doc.sections:
        section.top_margin = Inches(1)
        section.bottom_margin = Inches(1)
        section.left_margin = Inches(1.25)
        section.right_margin = Inches(1.25)

    style = doc.styles["Normal"]
    style.font.name = "Times New Roman"
    style.font.size = Pt(12)
    style.paragraph_format.line_spacing_rule = WD_LINE_SPACING.SINGLE
    style.paragraph_format.space_after = Pt(6)

    # Title
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run("VIOLATIONS MATRIX")
    r.bold = True
    r.font.size = Pt(16)

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run(caption)
    r.font.size = Pt(12)
    r.italic = True

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run(f"Docket: {canonical_id}    |    "
                  f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    r.font.size = Pt(10)

    doc.add_paragraph()

    # Summary
    total = sum(len(v) for v in by_class.values())
    p = doc.add_paragraph()
    r = p.add_run(f"Total distinct violations identified: {total}")
    r.bold = True
    r.font.size = Pt(12)

    for cls in CLASS_ORDER:
        if cls in by_class and by_class[cls]:
            heading, _ = CLASS_SECTIONS.get(cls, (cls.upper(), ""))
            p = doc.add_paragraph(style="List Bullet")
            p.add_run(f"{heading}: ").bold = True
            p.add_run(f"{len(by_class[cls])}")

    if "procedural" in by_class and by_class["procedural"]:
        p = doc.add_paragraph(style="List Bullet")
        p.add_run("PROCEDURAL ATTACK VECTORS: ").bold = True
        p.add_run(f"{len(by_class['procedural'])}")

    doc.add_paragraph()

    # === MATRIX_REFINEMENT_V1: tier-prefixed headings in DOCX ===
    case_type = getattr(render_docx, "_active_case_type", "")
    for cls in CLASS_ORDER:
        items = by_class.get(cls) or []
        if not items:
            continue
        base_heading, intro = CLASS_SECTIONS[cls]
        heading = _section_heading_with_tier(case_type, cls, base_heading)

        # Section heading
        p = doc.add_paragraph()
        r = p.add_run(heading)
        r.bold = True
        r.font.size = Pt(14)

        p = doc.add_paragraph()
        p.add_run(intro).italic = True

        for v in items:
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Inches(0.25)
            r = p.add_run(f"\u2022 {v['source']}")
            r.bold = True
            p.add_run(f" \u2014 {v['source_title']}")

            if v["name"] and v["name"] != v["source_title"]:
                p2 = doc.add_paragraph()
                p2.paragraph_format.left_indent = Inches(0.5)
                p2.add_run(f"Duty: {v['name']}").italic = True
            if v["jurisdiction"]:
                p2 = doc.add_paragraph()
                p2.paragraph_format.left_indent = Inches(0.5)
                p2.add_run(f"Jurisdiction: {v['jurisdiction']}").italic = True
            if v["regulator"]:
                p2 = doc.add_paragraph()
                p2.paragraph_format.left_indent = Inches(0.5)
                p2.add_run(f"Regulator: {v['regulator']}").italic = True
            if v["claim_ids"]:
                p2 = doc.add_paragraph()
                p2.paragraph_format.left_indent = Inches(0.5)
                p2.add_run(f"Asserted in claim(s): "
                           f"{', '.join(v['claim_ids'])}").italic = True
            # === ENFORCEMENT_LAYER_V1 ===
            p_relief = doc.add_paragraph()
            p_relief.paragraph_format.left_indent = Inches(0.5)
            r_relief = p_relief.add_run(_enforcement_for_violation(v, cls))
            r_relief.bold = True
            r_relief.font.size = Pt(11)
        # === ENFORCEMENT_LAYER_V1: section relief summary ===
        p_sec = doc.add_paragraph()
        p_sec.paragraph_format.left_indent = Inches(0.0)
        r_sec = p_sec.add_run(_section_relief_summary(cls, len(items)))
        r_sec.bold = True
        r_sec.font.size = Pt(11)
        doc.add_paragraph()

    # Procedural
    if by_class.get("procedural"):
        p = doc.add_paragraph()
        r = p.add_run("PROCEDURAL ATTACK VECTORS")
        r.bold = True
        r.font.size = Pt(14)
        p = doc.add_paragraph()
        p.add_run("The following procedural doctrines support attack on "
                  "the prior order or judgment:").italic = True
        for v in by_class["procedural"]:
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Inches(0.25)
            r = p.add_run(f"\u2022 {v['source']}")
            r.bold = True
            p.add_run(f" \u2014 {v['source_title']}")
            if v["jurisdiction"]:
                p.add_run(f"  [{v['jurisdiction']}]").italic = True

    # Footer disclaimer
    doc.add_paragraph()
    # === ENFORCEMENT_LAYER_V1: consolidated relief demand (docx) ===
    doc.add_paragraph()
    for line in _consolidated_relief_demand(by_class):
        if not line:
            continue
        p_cr = doc.add_paragraph()
        if line.startswith(("CONSOLIDATED",)):
            p_cr.alignment = WD_ALIGN_PARAGRAPH.CENTER
            r_cr = p_cr.add_run(line)
            r_cr.bold = True
            r_cr.font.size = Pt(14)
        elif line.startswith(("MANDATORY", "PROCEDURAL", "STATUTORY",
                              "COMMON-LAW", "FIDUCIARY", "REGULATORY")):
            r_cr = p_cr.add_run(line)
            r_cr.bold = True
            r_cr.font.size = Pt(12)
        else:
            r_cr = p_cr.add_run(line)
            r_cr.font.size = Pt(11)
    doc.add_paragraph()
    # === END ENFORCEMENT_LAYER_V1 ===

    # Criminal exclusion note
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run(
        "CRIMINAL EXPOSURE NOTE: This matrix excludes Title 18 (federal criminal) "
        "statutes by design. Criminal exposure is reserved for separate referral "
        "packages (DOJ, state AG, regulator complaints) and is not actionable in "
        "civil filings."
    )
    r.bold = True
    r.font.size = Pt(9)

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run(
        "Generated deterministically by Beezle violations_matrix engine. "
        "Verify each citation in CourtListener / Lexis / Westlaw before filing. "
        "Each duty is bound to its source via duty_maps; no LLM interpretation "
        "was performed."
    )
    r.italic = True
    r.font.size = Pt(9)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))
    log.info(f"violations matrix written: {output_path}")
    return output_path


def build_violations_matrix(canonical_id: str, caption: str,
                            claims: List[Dict],
                            doctrines: List[Dict],
                            output_dir: Path,
                            timestamp: str,
                            case_type: str = "",
                            facts_text: str = "") -> Tuple[Path, Path]:
    """Build both .md and .docx Violations Matrix exhibits.
    Returns (md_path, docx_path).

    case_type: drives allowed_domains filter + tier ranking
    facts_text: drives constitutional section conditional gating
    """
    by_class = collect_violations(claims, doctrines)

    # === MATRIX_REFINEMENT_V1: filter, drop vague, gate constitutional ===
    allowed = _resolve_allowed_domains(canonical_id, case_type)
    by_class = _filter_by_case_domains(by_class, claims, allowed)
    by_class = _strip_vague(by_class)
    # === MATRIX_REFINEMENT_V2: source blacklist + case-level constitutional ===
    by_class = _strip_disallowed_sources(by_class, case_type)
    # Drop any constitutional that came in via claim-matching (will be re-added)
    by_class.pop("constitutional", None)
    by_class = _inject_case_level_constitutional(by_class, facts_text)
    # === END MATRIX_REFINEMENT_V2 ===
    # === END MATRIX_REFINEMENT_V1 ===

    output_dir = Path(output_dir)
    safe_key = canonical_id.replace(":", "_").replace("/", "_")

    # === MATRIX_REFINEMENT_V1: thread case_type to renderers via attr ===
    render_md._active_case_type = case_type
    render_docx._active_case_type = case_type

    md_path = output_dir / f"violations_matrix_{safe_key}_{timestamp}.md"
    md_path.write_text(render_md(canonical_id, caption, by_class),
                       encoding="utf-8")

    docx_path = output_dir / f"violations_matrix_{safe_key}_{timestamp}.docx"
    render_docx(canonical_id, caption, by_class, docx_path)

    log.info(f"violations matrix complete: {md_path.name} + {docx_path.name}")
    return md_path, docx_path


# === BLOCK_6_13_POSTURE_LOCK — module-level posture setter ===
_BEEZLE_POSTURE = "merits"

def set_posture(posture: str) -> None:
    """Called by drafter/finalizer before rendering matrix sections."""
    global _BEEZLE_POSTURE
    _BEEZLE_POSTURE = posture or "merits"
# === END BLOCK_6_13_POSTURE_LOCK ===
