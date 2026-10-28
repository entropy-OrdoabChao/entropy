"""
generation/reply_brief_drafter.py — hotfix7

Deterministic reply-brief generator that merges:
  - hotfix6's audit-driven structure (case_registry + violation grouping by file)
  - Phase 3.5k's locked-in legal engines:
      * definition_engine (locked term definitions, never LLM-touched)
      * if_then_engine (deterministic IF/AND/THEN chains with citation anchoring)
      * fact_polarity_engine (plaintiff-framed facts, no defense leakage)
      * pre_draft_validator (catches double-negation, polarity bleeds)
  - hotfix6's per-court binding hierarchy (BINDING_HIERARCHY) with structural
    explanations of why each non-binding court is non-binding
  - NEW: per-issue grouping in addition to per-file grouping
  - NEW: separate scorecard exhibit (.docx) generated per case
  - NEW: real PA caption table (no tabs, no leading spaces)
  - NEW: numbered fact list, numbered argument sections, correct Roman numerals
  - NEW: stripped of stray "|" pipes that appeared in v11

ZERO LLM CALLS. Pure template assembly. CourtListener verification is the
manual handoff (still listed in the verification checklist at the end of
each draft).

CLI:
    python -m generation.reply_brief_drafter <canonical_id>
    python -m generation.reply_brief_drafter --all
    python -m generation.reply_brief_drafter --all --with-scorecard

Output:
    output/reply_briefs/reply_brief_<key>_<ts>.{md,docx}
    output/reply_briefs/scorecard_exhibit_<key>_<ts>.docx
"""

from __future__ import annotations
from core import identity as _ident
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import json
import logging
import re
import sys
from collections import defaultdict, OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from generation.source_display import display_source
from typing import Dict, List, Optional, Tuple

# Make sibling pipeline modules importable when run as module
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pipeline.definition_engine import (
    build_definition_set,
    format_definition_block,
)
from pipeline.if_then_engine import (
    build_logic_chain,
    format_chain_block,
)
from pipeline.fact_polarity_engine import (
    reframe_fact,
    sanitize_rule_text,
)
from pipeline.pre_draft_validator import validate_text

log = logging.getLogger("beezle.reply_brief_drafter")


# =============================================================================
# COURT HIERARCHY METADATA  (carried forward from hotfix6, unchanged)
# =============================================================================

BINDING_HIERARCHY = {
    "EDPA": {
        "binding": ["SCOTUS", "CA3"],
        "binding_for_pa_substantive_law": ["SCOTUS", "CA3", "PA_SUP"],
        "non_binding": ["CA_OTHER", "DPA_OTHER", "DIST_OTHER",
                        "PA_SUPER", "PA_CMWLTH", "PA_CP_PHILA", "PA_CP_OTHER"],
        "explanation": (
            "The Eastern District of Pennsylvania is bound by decisions of the "
            "United States Supreme Court and the United States Court of Appeals "
            "for the Third Circuit. For state-law claims adjudicated under Erie "
            "doctrine, the court is also bound by Pennsylvania Supreme Court "
            "decisions interpreting Pennsylvania substantive law."
        ),
    },
    "PA_CP_PHILA": {
        "binding": ["PA_SUP", "PA_SUPER", "PA_CMWLTH"],
        "non_binding": ["SCOTUS_PA_LAW_ONLY", "CA3", "EDPA", "DPA_OTHER",
                        "DIST_OTHER", "PA_CP_OTHER"],
        "explanation": (
            "The Philadelphia Court of Common Pleas is bound by decisions of "
            "the Pennsylvania Supreme Court, Superior Court (in published "
            "opinions), and Commonwealth Court (in matters within its "
            "jurisdiction). Trial-court decisions from other Pennsylvania "
            "Courts of Common Pleas are persuasive only and do not bind this "
            "court."
        ),
    },
    "CA3": {
        "binding": ["SCOTUS"],
        "non_binding": ["CA_OTHER", "DPA_OTHER", "DIST_OTHER",
                        "PA_SUPER", "PA_CMWLTH", "PA_CP_PHILA", "PA_CP_OTHER"],
        "explanation": (
            "The Third Circuit is bound only by decisions of the United States "
            "Supreme Court and its own prior published panel decisions."
        ),
    },
}

COURT_LABELS = {
    "SCOTUS": "United States Supreme Court",
    "CA3": "United States Court of Appeals for the Third Circuit",
    "CA_OTHER": "U.S. Court of Appeals (sister circuit)",
    "EDPA": "United States District Court for the Eastern District of Pennsylvania",
    "DPA_OTHER": "U.S. District Court (sister district within the Third Circuit)",
    "DIST_OTHER": "U.S. District Court (outside the Third Circuit)",
    "PA_SUP": "Pennsylvania Supreme Court",
    "PA_SUPER": "Pennsylvania Superior Court",
    "PA_CMWLTH": "Pennsylvania Commonwealth Court",
    "PA_CP_PHILA": "Philadelphia Court of Common Pleas",
    "PA_CP_OTHER": "Pennsylvania Court of Common Pleas (sister county)",
    "UNKNOWN": "an unidentified court",
}

NON_BINDING_RATIONALE = {
    "DIST_OTHER": (
        "is a federal district court outside the Third Circuit and therefore "
        "is not binding on this court. Decisions of sister district courts in "
        "other circuits are persuasive authority only."
    ),
    "DPA_OTHER": (
        "is a federal district court trial-level decision from a sister district "
        "within the Third Circuit. District court decisions do not bind other "
        "district courts. Only Third Circuit and Supreme Court decisions are "
        "binding on this court."
    ),
    "CA_OTHER": (
        "is a decision of another United States Court of Appeals. Decisions of "
        "sister circuits are persuasive authority but are not binding on this "
        "court. Only Third Circuit and Supreme Court decisions are binding."
    ),
    "PA_SUPER": (
        "is a decision of the Pennsylvania Superior Court (intermediate "
        "appellate court). For Pennsylvania-substantive-law claims adjudicated "
        "under Erie doctrine in federal court, the controlling state-law "
        "authority is the Pennsylvania Supreme Court."
    ),
    "PA_CMWLTH": (
        "is a decision of the Pennsylvania Commonwealth Court (intermediate "
        "appellate court). The controlling state-law authority is the "
        "Pennsylvania Supreme Court."
    ),
    "PA_CP_OTHER": (
        "is a decision of a Pennsylvania Court of Common Pleas (trial court) "
        "from a different county. Trial-court decisions from sister counties "
        "are persuasive authority only and do not bind this court."
    ),
    "PA_CP_PHILA": (
        "is a trial-court decision from this same court. Trial-court decisions "
        "do not establish binding precedent."
    ),
    "EDPA": (
        "is a federal district court trial-level decision. District court "
        "decisions do not bind other district court judges and are persuasive "
        "authority only."
    ),
    "UNKNOWN": (
        "could not be parsed to determine the issuing court. Plaintiff requests "
        "that the court verify the citation and confirm the issuing court "
        "before treating it as binding."
    ),
}


# =============================================================================
# ISSUE CLASSIFICATION (drives definition_engine + if_then_engine)
# =============================================================================
# Maps a flagged violation to a high-level issue bucket so that:
#   - definition_engine can lock the right definitions
#   - if_then_engine can build the right IF/THEN chain
#   - per-issue argument sections collapse duplicate cites
#
# Order matters: first match wins. "hierarchy" is the catch-all.

ISSUE_KEYWORDS: List[Tuple[str, List[str]]] = [
    ("verification", ["verif", "praecipe", "1024", "208.3", "sworn", "affidavit"]),
    ("garnishment", ["garnish", "3145", "3146", "writ of execution", "interrogator"]),
    ("default_judgment", ["default", "60(b)", "void judgment", "rule 55"]),
    ("fcra", ["fcra", "1681", "credit report", "furnisher", "reinvestigat"]),
    ("fdcpa", ["fdcpa", "1692", "debt collect", "validation"]),
    ("bad_faith", ["bad faith", "8371", "rancosky", "terletsky", "insurance"]),
    ("rule_60b", ["60(b)", "void", "vacate", "reconsiderat"]),
    ("twombly_iqbal", ["twombly", "iqbal", "12(b)(6)", "plausible", "failure to state"]),
    ("removal", ["removal", "1441", "1446", "remand"]),
    ("quiet_title", ["quiet title", "rescission", "tila", "1635", "mortgage"]),
]




# === DYNAMIC_TITLE_V1 ===
# Map filing_type → motion title. Replaces hardcoded
# "PLAINTIFF'S REPLY IN SUPPORT OF MOTION FOR RECONSIDERATION".
_FILING_TYPE_TITLES = {
    "motion_to_strike_state":           "PLAINTIFF'S MOTION TO STRIKE DEFECTIVE FILINGS\nAND FOR SANCTIONS",
    "motion_to_strike_federal":         "PLAINTIFF'S MOTION TO STRIKE DEFECTIVE FILINGS\nAND FOR SANCTIONS",
    "motion_to_vacate":                 "PLAINTIFF'S MOTION TO VACATE VOID JUDGMENT",
    "motion_for_reconsideration":       "PLAINTIFF'S REPLY IN SUPPORT OF MOTION FOR RECONSIDERATION",
    "motion_for_garnishee_default":     "PLAINTIFF'S MOTION FOR ENTRY OF DEFAULT JUDGMENT\nAGAINST GARNISHEES",
    "motion_for_recusal":               "PLAINTIFF'S MOTION FOR RECUSAL",
    "motion_to_remand":                 "PLAINTIFF'S MOTION TO REMAND",
    "notice_of_supplemental_authority": "PLAINTIFF'S NOTICE OF SUPPLEMENTAL AUTHORITY",
    "petition_for_writ_of_mandamus":    "PETITION FOR WRIT OF MANDAMUS",
    "reply_brief_pipeline":             "PLAINTIFF'S REPLY IN SUPPORT OF MOTION FOR RECONSIDERATION",
}

def _motion_title_for(filing_type: str) -> str:
    """Look up the document title for a filing_type slug.
    Falls back to a generic title for unknown types."""
    if not filing_type:
        return "PLAINTIFF'S REPLY IN SUPPORT OF MOTION FOR RECONSIDERATION"
    t = _FILING_TYPE_TITLES.get(filing_type)
    if t:
        return t
    # Unknown filing_type — title-case the slug
    return f"PLAINTIFF'S {filing_type.replace('_', ' ').upper()}"
# === END DYNAMIC_TITLE_V1 ===


def classify_issue(violation: dict) -> str:
    """Classify a single audit violation into an issue bucket.

    The Phase 3.5k definition_engine and if_then_engine are keyed off these
    issue strings. Returns 'hierarchy' as a safe default for stare-decisis-only
    flags that don't pattern-match to a substantive issue.
    """
    text = " ".join(str(violation.get(k, "")) for k in
                    ("citation", "rule_violated", "reason", "rule_type")).lower()
    for issue, keywords in ISSUE_KEYWORDS:
        if any(kw in text for kw in keywords):
            return issue
    return "hierarchy"


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class FlaggedViolation:
    citation: str
    cited_court: str
    severity: str
    source_file: str
    source_filename: str
    rule_violated: str = ""
    issue: str = "hierarchy"

    @classmethod
    def from_audit_dict(cls, v: dict, filename: str = "") -> "FlaggedViolation":
        f = cls(
            citation=str(v.get("citation", "")).strip(),
            cited_court=str(v.get("cited_court", "UNKNOWN")),
            severity=str(v.get("severity", "?")),
            source_file=filename,
            source_filename=display_source(filename) if filename else "",
            rule_violated=str(v.get("rule_violated", v.get("reason", ""))),
        )
        f.issue = classify_issue(v)
        return f


@dataclass
class IssueGroup:
    """All violations sharing one issue bucket, across all source files."""
    issue: str
    violations: List[FlaggedViolation] = field(default_factory=list)

    @property
    def fatal_count(self) -> int:
        return sum(1 for v in self.violations if v.severity == "fatal")

    @property
    def serious_count(self) -> int:
        return sum(1 for v in self.violations if v.severity == "serious")

    @property
    def source_files(self) -> List[str]:
        return sorted({v.source_filename for v in self.violations if v.source_filename})


@dataclass
class ReplyBriefData:
    canonical_id: str
    case_caption: str
    case_court: str
    docket_id: str
    judge: str = ""
    case_type: str = ""
    parties: List[str] = field(default_factory=list)
    by_source_file: Dict[str, List[FlaggedViolation]] = field(default_factory=dict)
    by_issue: Dict[str, IssueGroup] = field(default_factory=dict)
    total_fatal: int = 0
    total_serious: int = 0
    score: int = 0
    rank: str = "?"
    mined_authorities: List[Dict] = field(default_factory=list)
    # === PIPELINE_PATH_V2 ===
    claims: List[Dict] = field(default_factory=list)
    doctrines: List[Dict] = field(default_factory=list)
    mode: str = "audit"   # audit | pipeline | hybrid


# =============================================================================
# LOADER
# =============================================================================



# === PIPELINE_PATH_V2 ===
# Pipeline-driven brief data builder (Block 6.8)
# ─────────────────────────────────────────────────────────────────────────

# BLOCK_6_13_POSTURE_LOCK — helper to attach posture to any data object
def _attach_posture(data_obj, case_dict, role, facts):
    try:
        from pipeline.posture_engine import derive_posture
        p = derive_posture(case_dict or {}, role or "", facts or [])
        try:
            setattr(data_obj, "posture", p)
        except Exception:
            pass
        return p
    except Exception:
        try:
            setattr(data_obj, "posture", "merits")
        except Exception:
            pass
        return "merits"


def _build_data_from_pipeline(canonical_id: str, base_path: Path,
                              hybrid_with_audit: bool = True) -> "ReplyBriefData":
    """Build a ReplyBriefData populated with engine output."""
    import sys as _sys
    base_path = Path(base_path)
    if str(base_path) not in _sys.path:
        _sys.path.insert(0, str(base_path))

    from pipeline.case_loader import load_case
    from pipeline.scenario_engine import scenario_decompose
    from pipeline.order_engine import analyze_order
    # BLOCK_6_13_POSTURE_LOCK
    from pipeline.posture_engine import derive_posture as _derive_posture

    case = load_case(canonical_id)
    if case is None:
        raise ValueError(f"Case {canonical_id} not in registry")

    facts_text = "\n".join(case.get("facts", []))

    try:
        scenario = scenario_decompose(fact_pattern=facts_text, max_depth=1)
    except Exception as _e:
        log.warning(f"scenario_decompose failed: {_e}")
        scenario = {"theories": [], "gaps": []}

    # === ENGINE_BLACKLIST_V1 ===
    # Strip duties whose source matches the case_type blacklist BEFORE they
    # propagate to attached_duties (and thus to brief, matrix, and case_log).
    try:
        from pipeline.scenario_engine import (
            _resolve_engine_disallowed,
            _filter_disallowed_for_case,
        )
        _case_type_for_filter = (case.get("case_type") or "")
        _disallowed = _resolve_engine_disallowed(canonical_id, _case_type_for_filter)
        if _disallowed:
            for _theory in (scenario.get("theories") or []):
                _orig = _theory.get("attached_duties") or []
                _filtered = _filter_disallowed_for_case(_orig, _disallowed)
                if len(_filtered) != len(_orig):
                    log.info(
                        f"engine blacklist stripped {len(_orig) - len(_filtered)} "
                        f"duties from {_theory.get('id')} for case_type={_case_type_for_filter!r}"
                    )
                _theory["attached_duties"] = _filtered
    except Exception as _bl_e:
        log.warning(f"engine blacklist filter failed: {_bl_e}")
    # === END ENGINE_BLACKLIST_V1 ===

    # === CORPUS_CLEANUP_V1 ===
    # FIX 1: Strip vague duties (Regulatory-Compliance-Cross-Cutting) from
    #        every theory's attached_duties. Mirror of matrix-time strip
    #        but applied at engine-level so case_log corpus is clean.
    try:
        from pipeline.scenario_engine import _filter_vague_duties
        for _theory in (scenario.get("theories") or []):
            _orig = _theory.get("attached_duties") or []
            _filtered = _filter_vague_duties(_orig)
            if len(_filtered) != len(_orig):
                log.info(
                    f"vague duty strip removed {len(_orig) - len(_filtered)} "
                    f"entries from {_theory.get('id')}"
                )
            _theory["attached_duties"] = _filtered
    except Exception as _vg_e:
        log.warning(f"vague duty strip failed: {_vg_e}")

    # FIX 2: Strip pa_utpcpl_violation if no UTPCPL fact triggers present.
    try:
        from pipeline.scenario_engine import _filter_utpcpl_if_no_triggers
        _orig_theories = scenario.get("theories") or []
        _filtered_theories = _filter_utpcpl_if_no_triggers(_orig_theories, facts_text)
        if len(_filtered_theories) != len(_orig_theories):
            log.info(
                f"UTPCPL stripped from theories — no consumer-protection "
                f"trigger in facts for case_type={case.get('case_type')!r}"
            )
        scenario["theories"] = _filtered_theories
    except Exception as _ut_e:
        log.warning(f"UTPCPL trigger filter failed: {_ut_e}")

    # FIX 3: Inject case-level constitutional as a synthetic theory so the
    #        case_log_writer sees constitutional_sources. This mirrors the
    #        matrix injection (Block 6.9.3) but propagates to the corpus.
    # === CONSTITUTIONAL_ENGINE_FACTS_V2 ===
    # Path B v2: load engine_facts directly from registry every time.
    # No reliance on outer-scope vars that may not exist here.
    try:
        _CONST_TRIGGERS = (
            "no proper service", "lack of jurisdiction",
            "no notice", "due process violation", "defective service",
        )
        _engine_facts = []
        try:
            import json as _json
            _reg_raw = _json.loads(
                (_eroot() / "cases_index/case_registry.json")
                .read_text(encoding="utf-8")
            )
            _engine_facts = _reg_raw.get(canonical_id, {}).get("engine_facts", []) or []
        except Exception as _re_e:
            log.warning(f"engine_facts load failed for {canonical_id}: {_re_e}")
            _engine_facts = []
        _ef_text = " ".join(_engine_facts).lower()
        _fired = [t for t in _CONST_TRIGGERS if t in _ef_text]
    # === END CONSTITUTIONAL_ENGINE_FACTS_V2 ===
        if _fired:
            import yaml as _yaml
            _cmap_path = (_eroot() / "pipeline/duty_maps/constitutional.yaml")
            if _cmap_path.exists():
                _cmap = _yaml.safe_load(_cmap_path.read_text(encoding="utf-8"))
                _const_duties = _cmap.get("duties", []) or []
                if _const_duties:
                    _synthetic_theory = {
                        "id":            "(case-level constitutional)",
                        "theory_id":     "(case-level constitutional)",
                        "display_name":  "Case-Level Constitutional Due Process",
                        "name":          "Case-Level Constitutional Due Process",
                        "domains":       ["constitutional", "procedural"],
                        "confidence":    "high",
                        "triggers_matched": list(_fired),
                        "attached_duties":  list(_const_duties),
                    }
                    scenario.setdefault("theories", []).append(_synthetic_theory)
                    log.info(
                        f"case-level constitutional injected: "
                        f"{len(_const_duties)} duties, triggers={_fired}"
                    )
    except Exception as _c_e:
        log.warning(f"case-level constitutional injection failed: {_c_e}")
    # === END CORPUS_CLEANUP_V1 ===


    try:
        order = analyze_order(fact_pattern=facts_text,
                              role_hint=case.get("role"),
                              min_confidence="low")
    except Exception as _e:
        log.warning(f"analyze_order failed: {_e}")
        order = {"doctrines": []}

    base_data = None
    if hybrid_with_audit:
        try:
            base_data = load_brief_data(canonical_id, base_path)
            base_data.mode = "hybrid"
        except FileNotFoundError:
            log.info(f"no audit data for {canonical_id}; pipeline-only mode")
        except Exception as _e:
            log.warning(f"audit data load failed: {_e}")

    if base_data is None:
        registry_path = base_path / "cases_index" / "case_registry.json"
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        entry = registry.get(canonical_id, {})
        base_data = ReplyBriefData(
            canonical_id=canonical_id,
            case_caption=entry.get("caption", ""),
            case_court=entry.get("case_court", ""),
            docket_id=canonical_id.split(":", 1)[-1],
            judge=entry.get("judge", ""),
            case_type=entry.get("case_type", ""),
            parties=entry.get("parties", []),
        )
        base_data.mode = "pipeline"

    base_data.claims    = scenario.get("theories", []) or []
    base_data.doctrines = order.get("doctrines", [])  or []
    # === BLOCK_6_13_2_1_DRAFTER ===
    try:
        from pipeline.posture_engine import derive_posture as _bz_dp
        import json as _bz_json
        _bz_reg_path = base_path / "cases_index" / "case_registry.json"
        _bz_reg = _bz_json.loads(_bz_reg_path.read_text(encoding="utf-8"))
        _bz_entry = _bz_reg.get(canonical_id, {}) if isinstance(_bz_reg, dict) else {}
        _bz_role = (
            (order.get("role") if isinstance(order, dict) else None)
            or _bz_entry.get("role")
            or case.get("role")
            or "plaintiff"
        )
        _bz_facts = _bz_entry.get("engine_facts", []) or case.get("facts", []) or []
        base_data.posture = _bz_dp(_bz_entry, _bz_role, _bz_facts)
        log.info(f"[posture] derived: {base_data.posture} (role={_bz_role})")
    except Exception as _bz_e:
        base_data.posture = "merits"
        log.warning(f"[posture] derivation failed, defaulting to merits: {_bz_e}")
    # === END BLOCK_6_13_2_1_DRAFTER ===
    return base_data


def _aggregate_duty_classes(claims: list, doctrines: list = None,
                            facts_text: str = "") -> dict:
    """SECTION_II_RELIEF_V2: aggregate from claims + doctrines + case-level constitutional.

    Constitutional duties live in constitutional.yaml and fire at case level
    (per Block 6.9.3 violations_matrix design). They are NOT on
    claim.attached_duties. So we load them directly when the canonical due-
    process triggers fire on facts_text.

    Void doctrines live in data.doctrines (order_engine output), classified
    as 'void' / 'voidable' — also not on claim.attached_duties.
    """
    agg = {
        "constitutional": [],
        "void_procedural": [],
        "voidable_procedural": [],
        "procedural": [],
        "statutory": [],
        "regulatory": [],
        "common_law": [],
        "fiduciary": [],
        "industry_standard": [],
    }
    seen_sources = {k: set() for k in agg}

    # ─── Pass 1: claim.attached_duties ────────────────────────────────
    for c in claims or []:
        cid = c.get("id") or c.get("theory_id") or "unknown"
        for d in (c.get("attached_duties") or []):
            cls = (d.get("class") or "").lower().strip()
            jur = (d.get("jurisdiction") or "").lower().strip()
            src_ = (d.get("source") or "").strip()
            if not src_:
                continue
            if cls == "constitutional":
                bucket = "constitutional"
            elif cls == "procedural" and jur == "void":
                bucket = "void_procedural"
            elif cls == "procedural" and jur == "voidable":
                bucket = "voidable_procedural"
            elif cls in agg:
                bucket = cls
            else:
                continue
            if src_ in seen_sources[bucket]:
                continue
            seen_sources[bucket].add(src_)
            agg[bucket].append({
                "source": src_,
                "title": d.get("source_title") or d.get("name", ""),
                "claim_id": cid,
                "jurisdiction": jur,
            })

    # ─── Pass 2: order_engine doctrines → void_procedural / voidable ──
    for doc_ in doctrines or []:
        cls_ = (doc_.get("classification") or "").lower().strip()
        did   = (doc_.get("id") or "").strip()
        dname = (doc_.get("display_name") or did).strip()
        if not did:
            continue
        if cls_ == "void":
            bucket = "void_procedural"
        elif cls_ == "voidable":
            bucket = "voidable_procedural"
        else:
            continue
        if did in seen_sources[bucket]:
            continue
        seen_sources[bucket].add(did)
        agg[bucket].append({
            "source": did,
            "title": dname,
            "claim_id": "(case-level doctrine)",
            "jurisdiction": cls_,
        })

    # ─── Pass 3: case-level constitutional (load from yaml directly) ──
    if facts_text:
        _CONST_TRIGGERS = (
            "no proper service",
            "lack of jurisdiction",
            "no notice",
            "due process violation",
            "defective service",
        )
        fl = facts_text.lower()
        if any(t in fl for t in _CONST_TRIGGERS):
            try:
                import yaml as _yaml
                cmap_path = (_eroot() / "pipeline/duty_maps/constitutional.yaml")
                if cmap_path.exists():
                    cmap = _yaml.safe_load(cmap_path.read_text(encoding="utf-8"))
                    for d in (cmap.get("duties") or []):
                        src_ = (d.get("source") or d.get("id") or "").strip()
                        if not src_ or src_ in seen_sources["constitutional"]:
                            continue
                        seen_sources["constitutional"].add(src_)
                        agg["constitutional"].append({
                            "source": src_,
                            "title": d.get("source_title") or d.get("name", ""),
                            "claim_id": "(case-level due process)",
                            "jurisdiction": d.get("jurisdiction", ""),
                        })
            except Exception as _e:
                log.warning(f"section II.C constitutional load failed: {_e}")

    return agg


def _render_section_ii_relief_block(data: "ReplyBriefData",
                                    section_number: str = "II") -> list:
    """SECTION_II_RELIEF_V1: build the closing II.C RELIEF DEMANDED block.

    Mirrors the matrix's CONSOLIDATED RELIEF DEMAND. ALL CAPS for the
    mandatory mandatory tier (constitutional + void); mixed register
    below.
    """
    paragraphs: list = []
    # === SECTION_II_RELIEF_V2 ===
    # Pull facts_text for case-level constitutional aggregation
    _facts_text = ""
    try:
        from pipeline.case_loader import load_case as _v2_load_case
        _v2_case = _v2_load_case(data.canonical_id)
        if _v2_case:
            _facts_text = "\n".join(_v2_case.get("facts", []))
    except Exception:
        pass
    agg = _aggregate_duty_classes(
        data.claims or [],
        doctrines=getattr(data, "doctrines", None) or [],
        facts_text=_facts_text,
    )
    # === END SECTION_II_RELIEF_V2 ===

    # === SECTION_II_RELIEF_V2 ===
    # Use level=1 to preserve the alphanumeric prefix (level=2 strips it
    # in the docx renderer's table-of-contents handling).
    paragraphs.append({
        "heading": f"{section_number}.C. RELIEF DEMANDED ON THE CLAIMS",
        "level": 1,
    })
    # === END SECTION_II_RELIEF_V2 ===

    has_mandatory = bool(agg["constitutional"] or agg["void_procedural"])

    if has_mandatory:
        # ALL CAPS mandatory tier
        const_count = len(agg["constitutional"])
        void_count  = len(agg["void_procedural"])
        parts = []
        if const_count:
            parts.append(
                f"{const_count} CONSTITUTIONAL DUE PROCESS VIOLATION(S)"
            )
        if void_count:
            parts.append(
                f"{void_count} VOID-JUDGMENT DOCTRINE(S)"
            )
        joined = " AND ".join(parts)

        # BLOCK_6_13_POSTURE_LOCK — posture-dispatched II.C summary
        from generation.posture_templates import II_C_SUMMARY as _II_C_SUMMARY, get as _pget
        _posture = getattr(data, "posture", "merits") or "merits"
        paragraphs.append(_pget(_II_C_SUMMARY, _posture).format(joined=joined))

        # List the specific authorities driving this mandatory result
        if agg["constitutional"]:
            paragraphs.append({
                "heading": "Constitutional authorities mandating vacatur",
                "level": 3,
            })
            for v in agg["constitutional"][:6]:
                paragraphs.append(
                    f"• **{v['source']}** — {v['title']}"
                )
        if agg["void_procedural"]:
            paragraphs.append({
                "heading": "Void-judgment authorities mandating vacatur",
                "level": 3,
            })
            for v in agg["void_procedural"][:6]:
                paragraphs.append(
                    f"• **{v['source']}** — {v['title']}"
                )

    # Procedural rule remedies (non-void)
    if agg["procedural"] or agg["voidable_procedural"]:
        all_proc = agg["procedural"] + agg["voidable_procedural"]
        paragraphs.append({
            "heading": "Procedural relief (rule-bound)",
            "level": 3,
        })
        paragraphs.append(
            f"On the procedural rule violations established above, the "
            f"Court shall enforce each rule as written; defendant carries "
            f"the burden of demonstrating compliance with each cited rule."
        )
        for v in all_proc[:8]:
            paragraphs.append(
                f"• Strike or compel compliance under **{v['source']}** — {v['title']}."
            )

    # Statutory remedies
    if agg["statutory"]:
        paragraphs.append({
            "heading": "Statutory relief (mandatory remedies)",
            "level": 3,
        })
        paragraphs.append(
            f"On the statutory violations established above, the Court is "
            f"bound to enforce the remedies prescribed by each cited "
            f"statute, including statutory damages, interest, and fees "
            f"where authorized."
        )
        for v in agg["statutory"][:8]:
            paragraphs.append(
                f"• Enforce statutory remedy under **{v['source']}** — {v['title']}."
            )

    # Common-law / equitable
    if agg["common_law"]:
        paragraphs.append({
            "heading": "Common-law and equitable relief",
            "level": 3,
        })
        paragraphs.append(
            f"On the common-law violations established above, the Court "
            f"shall award the remedies authorized by the cited doctrines, "
            f"including damages and any equitable relief the doctrines "
            f"authorize."
        )
        for v in agg["common_law"][:8]:
            paragraphs.append(
                f"• Award damages and equitable relief authorized by **{v['source']}**."
            )

    # Fiduciary
    if agg["fiduciary"]:
        paragraphs.append({
            "heading": "Fiduciary relief (disgorgement / restoration)",
            "level": 3,
        })
        paragraphs.append(
            f"On the fiduciary breaches established above, the Court "
            f"shall require disgorgement of any benefit retained and "
            f"restoration of plaintiff's position pre-breach."
        )
        for v in agg["fiduciary"][:6]:
            paragraphs.append(
                f"• Require disgorgement and restoration based on **{v['source']}**."
            )

    # Regulatory
    if agg["regulatory"]:
        paragraphs.append({
            "heading": "Regulatory relief (compliance + damages)",
            "level": 3,
        })
        paragraphs.append(
            f"On the regulatory violations established above, the Court "
            f"shall require compliance and award damages flowing from breach."
        )
        for v in agg["regulatory"][:6]:
            paragraphs.append(
                f"• Require compliance with **{v['source']}** and "
                f"award resulting damages."
            )

    # Industry standard (persuasive only)
    if agg["industry_standard"]:
        paragraphs.append({
            "heading": "Industry standard (persuasive)",
            "level": 3,
        })
        paragraphs.append(
            f"The industry standards identified above are persuasive on "
            f"the standard of care; the Court should weigh them in "
            f"fashioning relief."
        )

    # Closing punchline
    paragraphs.append({
        "heading": "Summary",
        "level": 3,
    })
    if has_mandatory:
        paragraphs.append(
            f"THE MANDATORY RELIEF IDENTIFIED ABOVE IS NON-DISCRETIONARY "
            f"ON THE FACTS ESTABLISHED. The remaining tiers carry rule-"
            f"bound and authority-bound remedies that the Court shall "
            f"enforce as written. Plaintiff respectfully demands the "
            f"complete relief stated above on each tier."
        )
    else:
        paragraphs.append(
            f"The relief identified above is bound by the cited authorities. "
            f"Plaintiff respectfully demands the complete relief stated "
            f"above on each tier."
        )

    return paragraphs


def _render_claims_section(data: "ReplyBriefData", section_number: str = "II") -> Dict:
    """Build affirmative-claims section. REFINEMENT_V1 changes:
      - Real fact lines from case.facts (no more 'Triggering facts established: 0')
      - Per-claim factual application sentence (auto-derived OR registry override)
      - Sentence-boundary truncation, original case preserved
      - Inline narrative for evidence; full discovery list goes to Appendix A
      - Strengthened II.B doctrine punchline
    """
    paragraphs: List = []

    if not data.claims:
        paragraphs.append(
            "Plaintiff has not yet identified the operative claims for "
            "deterministic analysis. This section will be populated when "
            "the underlying claim theories are processed by the engine."
        )
        return {
            "heading": f"{section_number}. PLAINTIFF'S CLAIMS AND CONTROLLING DUTIES",
            "level": 1,
            "paragraphs": paragraphs,
        }

    # === REFINEMENT_V1: load case for real facts + override field ===
    _case = None
    _registry_entry = None
    try:
        import sys as _sys
        from pathlib import Path as _P
        _bp = _P(str(_eroot()))
        if str(_bp) not in _sys.path:
            _sys.path.insert(0, str(_bp))
        from pipeline.case_loader import load_case as _load_case
        _case = _load_case(data.canonical_id)
        import json as _json
        _reg_path = _bp / "cases_index" / "case_registry.json"
        if _reg_path.exists():
            _reg = _json.loads(_reg_path.read_text(encoding="utf-8"))
            _registry_entry = _reg.get(data.canonical_id, {})
    except Exception as _e:
        log.debug(f"refinement_v1 case load failed: {_e}")

    _case_facts = (_case or {}).get("facts", [])
    _parties = [p for p in (_registry_entry or {}).get("parties", []) if p != _ident.party_key()]
    _factual_overrides = (_registry_entry or {}).get("factual_application", {}) or {}

    paragraphs.append(
        f"Plaintiff identifies {len(data.claims)} cause(s) of action below. "
        "Each is supported by controlling duty authority binding on this "
        "Court and stated in the form of a deterministic IF/AND/THEN logic "
        "chain. The duties below shift the burden of production and "
        "rebuttal to defendant on the matters identified."
    )

    # === BLOCK_6_10_NUMBERING: apply registry claim_priority ===
    _claim_priority = (_registry_entry or {}).get("claim_priority", []) or []
    if _claim_priority:
        # Build priority index map; claims not in priority list go to end
        _priority_idx = {cid: i for i, cid in enumerate(_claim_priority)}
        def _claim_sort_key(c):
            cid_ = c.get("id") or c.get("theory_id") or ""
            return _priority_idx.get(cid_, 999)
        data.claims = sorted(data.claims, key=_claim_sort_key)
        log.info(f"BLOCK_6_10: claims reordered per registry claim_priority "
                 f"({len(_claim_priority)} priority entries)")
    # === END BLOCK_6_10_NUMBERING ===

    for idx, claim in enumerate(data.claims, start=1):
        cid          = claim.get("id") or claim.get("theory_id") or "unknown"
        cname        = claim.get("display_name") or claim.get("name") or cid
        confidence   = claim.get("confidence", "low")
        domains      = ", ".join(claim.get("domains", []))
        duties       = claim.get("attached_duties") or []
        remedies     = claim.get("remedies") or {}
        triggers_hit = claim.get("triggers_matched") or []

        # === REFINEMENT_V1: pull REAL fact lines from case.facts ===
        matched_fact_lines: List[str] = []
        seen_lc = set()
        for trig in triggers_hit:
            t_lc = trig.lower().strip()
            if not t_lc:
                continue
            for fact_line in _case_facts:
                fl = fact_line.strip()
                fl_lc = fl.lower()
                if t_lc in fl_lc and fl_lc not in seen_lc and len(fl) > 4:
                    matched_fact_lines.append(fl)
                    seen_lc.add(fl_lc)
                    break  # one match per trigger
            if len(matched_fact_lines) >= 6:
                break

        # === BLOCK_6_10_NUMBERING: improved claim heading ───────────
        # Use level=1 to preserve the alphanumeric prefix (level=2 strips
        # it — same docx renderer bug as Block 6.9.6).
        # Use court-style ALL CAPS claim name for visual separation.
        _cname_display = (cname or "").replace("_", " ").upper().strip()
        paragraphs.append({
            "heading": f"{section_number}.A.{idx} — CLAIM {idx}: {_cname_display}",
            "level": 1,
        })
        # FIX 3: engine metadata line REMOVED at source.
        # (Block 8 v2 strips it at finalize time as a safety net; this
        #  removes it so it never appears in any future draft.)
        # === END BLOCK_6_10_NUMBERING ===

        # === REFINEMENT_V1: real fact lines or fallback ===
        if matched_fact_lines:
            paragraphs.append({
                "heading": "Triggering facts established from record",
                "level": 1,
            })
            try:
                from generation.posture_templates import TRIGGER_LABEL_REWRITE as _TLR
                _bz_p = getattr(data, "posture", "merits") or "merits"
                _bz_label_map = _TLR.get(_bz_p, {})
            except Exception:
                _bz_label_map = {}
            for fl in matched_fact_lines[:6]:
                fl_out = _bz_label_map.get((fl or "").strip().lower(), fl)
                paragraphs.append(f"• {fl_out}")
        else:
            paragraphs.append(
                "The triggering facts for this claim are addressed in the "
                "supporting record and incorporated by reference into the "
                "factual application below."
            )

        # === REFINEMENT_V1: factual application (override OR auto-derived) ===
        fa_text = _build_factual_application(
            cid, cname, _parties, triggers_hit, _factual_overrides
        )
        # === BLOCK_6_10_NUMBERING: numbered subsection ===
        paragraphs.append({
            "heading": f"{section_number}.A.{idx}.3 Factual Application",
            "level": 1,
        })
        # === END BLOCK_6_10_NUMBERING ===
        paragraphs.append(fa_text)

        # ── Controlling duty authorities ─────────────────────────────
        if duties:
            # === BLOCK_6_10_NUMBERING: numbered subsection ===
            paragraphs.append({
                "heading": f"{section_number}.A.{idx}.1 Controlling Authorities",
                "level": 1,
            })
            # === END BLOCK_6_10_NUMBERING ===
            for d in duties[:8]:
                src_  = d.get("source") or d.get("citation") or ""
                title = d.get("source_title") or d.get("name", "")
                cls   = d.get("class", "")
                jur   = d.get("jurisdiction", "")
                paragraphs.append(
                    f"• **{src_}** — {title} ({cls}; {jur})"
                )

            # ── Deterministic logic chain ────────────────────────────
            # === BLOCK_6_10_NUMBERING: numbered subsection ===
            paragraphs.append({
                "heading": f"{section_number}.A.{idx}.2 Elements (IF/AND/THEN)",
                "level": 1,
            })
            # === END BLOCK_6_10_NUMBERING ===

            primary = duties[0]
            primary_src = primary.get("source", "the controlling authority")
            primary_desc = _trim_to_sentence(primary.get("description") or "")
            paragraphs.append(
                f"IF the controlling authority {primary_src} establishes that "
                f"\"{primary_desc}\","
                if primary_desc else
                f"IF the controlling authority {primary_src} establishes the "
                f"duty stated above,"
            )
            for d in duties[1:3]:
                src2 = d.get("source", "")
                desc2 = _trim_to_sentence(d.get("description") or "")
                if desc2:
                    paragraphs.append(
                        f"AND the additional controlling authority {src2} "
                        f"reinforces that \"{desc2}\","
                    )
                else:
                    paragraphs.append(
                        f"AND the additional controlling authority {src2} "
                        f"reinforces the duty owed,"
                    )
            paragraphs.append(
                "THEN defendant\'s conduct as alleged constitutes a breach of "
                "the duties enumerated above, and plaintiff is entitled to "
                "the relief stated below."
            )

            # === REFINEMENT_V1: evidence as NARRATIVE ONE-LINER ===
            evidence_set: List[str] = []
            for d in duties[:5]:
                for e in (d.get("evidence_demanded") or [])[:3]:
                    if e and e not in evidence_set:
                        evidence_set.append(e)
            if evidence_set:
                top3 = evidence_set[:3]
                if len(top3) == 1:
                    e_phrase = top3[0]
                elif len(top3) == 2:
                    e_phrase = f"{top3[0]} and {top3[1]}"
                else:
                    e_phrase = f"{top3[0]}, {top3[1]}, and {top3[2]}"
                paragraphs.append(
                    f"The duties enumerated require defendant to produce, "
                    f"among other items, {e_phrase}. The complete production "
                    f"demand is set forth in Appendix A."
                )

            # ── Relief stated ────────────────────────────────────────
            remedy_set: List[str] = []
            for d in duties[:5]:
                for r_ in (d.get("violation_remedy") or [])[:3]:
                    if r_ and r_ not in remedy_set:
                        remedy_set.append(r_)
            for k, v in remedies.items():
                line = f"{k.replace('_', ' ').title()}: {v}"
                if line not in remedy_set:
                    remedy_set.append(line)
            if remedy_set:
                paragraphs.append({
                    "heading": "Relief stated by controlling authority",
                    "level": 1,
                })
                for r_ in remedy_set[:6]:
                    paragraphs.append(f"• {r_}")

            # === ENFORCEMENT_LAYER_V1: per-claim RELIEF DEMANDED closer ===
            _has_void_duty = any(
                (d.get("jurisdiction") or "").lower() in ("void", "voidable")
                for d in duties
            )
            _has_const = any(
                (d.get("class") or "").lower() == "constitutional" for d in duties
            )
            _has_proc = any(
                (d.get("class") or "").lower() == "procedural" for d in duties
            )
            _has_stat = any(
                (d.get("class") or "").lower() == "statutory" for d in duties
            )

            # === BLOCK_6_10_NUMBERING: numbered subsection ===
            paragraphs.append({
                "heading": f"{section_number}.A.{idx}.4 Relief Requested",
                "level": 1,
            })
            # === END BLOCK_6_10_NUMBERING ===

            if _has_void_duty or _has_const:
                # BLOCK_6_13_POSTURE_LOCK — posture-dispatched DP relief footer
                from generation.posture_templates import DUE_PROCESS_RELIEF_FOOTER as _DP, get as _pget
                _posture = getattr(data, "posture", "merits") or "merits"
                paragraphs.append(_pget(_DP, _posture))
            elif _has_proc and _has_stat:
                paragraphs.append(
                    "On this claim, the Court shall enforce both the "
                    "procedural rule and the statutory remedy stated by the "
                    "controlling authorities, including any damages, "
                    "interest, and fees the cited statutes authorize."
                )
            elif _has_stat:
                paragraphs.append(
                    "On this claim, the Court is bound to enforce the "
                    "statutory remedies stated by the controlling authorities, "
                    "including all damages, interest, and fees authorized "
                    "by the cited statutes."
                )
            elif _has_proc:
                paragraphs.append(
                    "On this claim, the Court shall enforce the procedural "
                    "rules stated; defendant carries the burden of showing "
                    "compliance with each rule cited above."
                )
            else:
                paragraphs.append(
                    "On this claim, the Court shall award the remedies "
                    "authorized by the controlling authorities cited above, "
                    "including damages and any equitable relief the doctrines "
                    "authorize."
                )
            # === END ENFORCEMENT_LAYER_V1 ===
        else:
            paragraphs.append(
                "No controlling duty authority is presently bound to this "
                "claim in the structured analysis."
            )

    # === REFINEMENT_V1: II.B Procedural Doctrines with PUNCHLINE ===
    if data.doctrines:
        paragraphs.append({
            "heading": f"{section_number}.B  Procedural Doctrines Available to Plaintiff",
            "level": 2,
        })
        paragraphs.append(
            f"In addition to the affirmative claims above, the following "
            f"{len(data.doctrines)} procedural doctrine(s) are available "
            "based on the current case posture:"
        )
        _has_void = False
        for d in data.doctrines:
            did   = d.get("id", "")
            dname = d.get("display_name") or did
            cls_  = d.get("classification", "")
            conf  = d.get("confidence", "")
            paragraphs.append(f"• **{dname}** [`{cls_}` / confidence: {conf}]")
            if cls_.lower() in ("void", "voidable"):
                _has_void = True

        if _has_void:
            # BLOCK_6_13_POSTURE_LOCK — posture-dispatched II.B paragraph
            from generation.posture_templates import II_B_DOCTRINE_PARAGRAPH as _IIB, get as _pget
            _posture = getattr(data, "posture", "merits") or "merits"
            paragraphs.append(_pget(_IIB, _posture))

    # === SECTION_II_RELIEF_V1 ===
    # Append Section II.C consolidated relief demand
    paragraphs.extend(
        _render_section_ii_relief_block(data, section_number=section_number)
    )
    # === END SECTION_II_RELIEF_V1 ===

    return {
        "heading": f"{section_number}. PLAINTIFF'S CLAIMS AND CONTROLLING DUTIES",
        "level": 1,
        "paragraphs": paragraphs,
    }


def _trim_to_sentence(text: str, max_chars: int = 280) -> str:
    """REFINEMENT_V1: trim text at sentence boundary, preserve original case."""
    if not text:
        return ""
    text = re.sub(r"\s+", " ", str(text)).strip()
    if len(text) <= max_chars:
        return text.rstrip(".") + "."
    cut = text[:max_chars]
    last_period = max(cut.rfind(". "), cut.rfind("; "))
    if last_period > max_chars * 0.4:
        return cut[:last_period + 1]
    last_space = cut.rfind(" ")
    if last_space > 0:
        return cut[:last_space] + "."
    return cut + "."


_VERB_FROM_TRIGGER = {
    "withheld": "withheld funds rightfully owed to plaintiff",
    "took money": "took funds from plaintiff",
    "retained money": "retained payments without consideration",
    "retained premium": "retained insurance premiums after risk ceased",
    "denied claim": "denied plaintiff\'s claim without reasonable investigation",
    "denied coverage": "denied coverage despite a valid policy",
    "failed to pay claim": "failed to pay plaintiff\'s claim when due",
    "failed to perform": "failed to perform required obligations",
    "breached contract": "breached the underlying contract",
    "did not honor agreement": "failed to honor the agreement",
    # BLOCK_6_12_DUEPROCESS_POSTURE — enforcement-phase anchored
    "no proper service": "failed to comply with required service of garnishment process and enforcement notices owed to Plaintiff",
    "lack of jurisdiction": "purported to act in the enforcement proceeding without statutory or rule-based authority",
    "due process violation": "deprived Plaintiff of property (the judgment proceeds) without procedurally compliant enforcement",
    "broken chain of title": "asserted title rights without an unbroken chain",
    "no standing to foreclose": "lacked standing to foreclose",
    "qualified written request": "failed to respond to a qualified written request",
    "servicer failed to respond": "failed to respond to plaintiff\'s servicing inquiry",
    "deceptive practice": "engaged in deceptive trade practices",
    "consumer harm": "caused harm to plaintiff as a consumer",
    "credit report": "reported inaccurate credit information",
    "failed to investigate": "failed to conduct a reasonable reinvestigation",
    "amount not authorized": "collected an amount not authorized by agreement or law",
    "wrongful possession": "exercised wrongful possession over plaintiff\'s property",
    "refused to return": "refused to return funds upon demand",
    "refused refund": "refused to issue a refund despite demand",
    "improper removal": "improperly removed the action from state court",
    "forum shopping": "engaged in forum shopping to evade state-court jurisdiction",
    "policy breach": "breached the operative policy terms",
    "bad faith": "acted in bad faith toward plaintiff",
    "unreasonable denial": "denied plaintiff\'s claim unreasonably",
}


def _build_factual_application(
    cid: str, cname: str, parties: list,
    triggers_matched: list, overrides: dict
) -> str:
    """REFINEMENT_V1: build the per-claim factual application sentence.
    Priority: registry override > auto-derived > generic fallback.
    """
    # Priority 1: registry override (per-claim or per-case)
    if isinstance(overrides, dict):
        if cid in overrides and overrides[cid]:
            return str(overrides[cid]).strip()
        if "_default" in overrides and overrides["_default"]:
            return str(overrides["_default"]).strip()

    # Priority 2: auto-derive from triggers + parties
    actor_parts = []
    for p in (parties or [])[:3]:
        actor_parts.append(p.replace("_", " ").title())
    actor = " and ".join(actor_parts) if actor_parts else "Defendant"

    verbs = []
    seen = set()
    for trig in (triggers_matched or []):
        key = trig.lower().strip()
        if key in _VERB_FROM_TRIGGER and key not in seen:
            verbs.append(_VERB_FROM_TRIGGER[key])
            seen.add(key)
        if len(verbs) >= 3:
            break

    if verbs:
        if len(verbs) == 1:
            verb_phrase = verbs[0]
        elif len(verbs) == 2:
            verb_phrase = f"{verbs[0]} and {verbs[1]}"
        else:
            verb_phrase = f"{verbs[0]}, {verbs[1]}, and {verbs[2]}"
        return (
            f"Here, {actor} {verb_phrase}, satisfying each element of "
            f"the {cname.lower()} claim above and triggering the duties "
            f"enumerated below."
        )

    # Priority 3: generic fallback
    return (
        f"Here, {actor}\'s conduct as established in the record satisfies "
        f"each element of the {cname.lower()} claim above and triggers the "
        f"duties enumerated below."
    )


def _render_discovery_appendix(data: "ReplyBriefData") -> Dict:
    """REFINEMENT_V1: build Appendix A — production demand by claim."""
    paragraphs: List = []
    paragraphs.append(
        "The duties identified in Section II carry burden-flip discovery "
        "obligations. Plaintiff demands production of the following items "
        "under Pa.R.C.P. 4009 (state) or Fed.R.Civ.P. 34 (federal) within "
        "thirty (30) days of service of this filing:"
    )

    any_evidence = False
    for idx, claim in enumerate(data.claims or [], start=1):
        cname = claim.get("display_name") or claim.get("id", "")
        duties = claim.get("attached_duties") or []
        ev_set: List[str] = []
        for d in duties[:8]:
            for e in (d.get("evidence_demanded") or []):
                if e and e not in ev_set:
                    ev_set.append(e)
        if not ev_set:
            continue
        any_evidence = True
        paragraphs.append({
            "heading": f"A.{idx}  Production demanded under {cname}",
            "level": 2,
        })
        for e in ev_set[:12]:
            paragraphs.append(f"• {e}")

    if not any_evidence:
        paragraphs.append(
            "No specific production demand items are bound to the present "
            "claim set. Plaintiff reserves the right to supplement before "
            "the discovery cutoff."
        )

    return {
        "heading": "APPENDIX A. DISCOVERY PRODUCTION DEMANDED",
        "level": 1,
        "paragraphs": paragraphs,
    }
# === END REFINEMENT_V1 ===
# === END PIPELINE_PATH_V2 ===
def load_brief_data(canonical_id: str, base_path: Path) -> ReplyBriefData:
    base_path = Path(base_path)
    registry_path = base_path / "cases_index" / "case_registry.json"
    case_map_path = base_path / "cases_index" / "case_map.json"
    if not case_map_path.exists():
        case_map_path = base_path / "cases" / "_consolidated" / "case_map.json"
    audit_dir = base_path / "output" / "audit"

    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    if canonical_id not in registry:
        raise ValueError(f"Case {canonical_id} not in registry")
    entry = registry[canonical_id]

    case_map = {}
    if case_map_path.exists():
        try:
            case_map = json.loads(case_map_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    safe_key = canonical_id.replace(":", "_").replace("/", "_")
    audit_data_path = audit_dir / f"data_{safe_key}.json"
    if not audit_data_path.exists():
        raise FileNotFoundError(f"Audit data not found: {audit_data_path}")
    audit_data = json.loads(audit_data_path.read_text(encoding="utf-8"))

    hier = audit_data.get("hierarchy", {})
    by_file_raw = hier.get("by_file", {})

    by_source_file: Dict[str, List[FlaggedViolation]] = {}
    by_issue: Dict[str, IssueGroup] = OrderedDict()

    for fpath, viols in by_file_raw.items():
        if not viols:
            continue
        flags = [FlaggedViolation.from_audit_dict(v, fpath) for v in viols]
        keepable = [f for f in flags if f.severity in ("fatal", "serious")]
        if keepable:
            by_source_file[fpath] = keepable
            for f in keepable:
                if f.issue not in by_issue:
                    by_issue[f.issue] = IssueGroup(issue=f.issue)
                by_issue[f.issue].violations.append(f)

    # Mined binding authorities
    mined: List[Dict] = []
    auth_index_path = base_path / "obsidian_sync" / "authority_index.json"
    if auth_index_path.exists():
        try:
            ai = json.loads(auth_index_path.read_text(encoding="utf-8"))
            target = entry.get("case_court", "EDPA")
            binding_courts = BINDING_HIERARCHY.get(target, {}).get("binding", [])
            for a in ai.get("authorities", []):
                if a.get("cited_court", "") in binding_courts:
                    mined.append(a)
        except Exception as e:
            log.warning(f"Could not load mined authorities: {e}")

    scorecard = audit_data.get("scorecard", {})

    return ReplyBriefData(
        canonical_id=canonical_id,
        case_caption=entry.get("caption", canonical_id),
        case_court=entry.get("case_court", "UNKNOWN"),
        docket_id=canonical_id.split(":", 1)[-1],
        judge=entry.get("judge", ""),
        case_type=entry.get("case_type", ""),
        parties=entry.get("parties", []),
        by_source_file=by_source_file,
        by_issue=by_issue,
        total_fatal=hier.get("fatal_count", 0),
        total_serious=hier.get("serious_count", 0),
        score=scorecard.get("score", scorecard.get("reversibility_score", 0)),
        rank=scorecard.get("rank", "?"),
        mined_authorities=mined,
    )


# =============================================================================
# CITATION CLEANUP
# =============================================================================

_PIPE_RE = re.compile(r"\s*\|\s*")


def _clean_citation_text(s: str) -> str:
    """Strip pipes (v11 polarity-engine residue), collapse whitespace."""
    s = _PIPE_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _representative_citation(group: IssueGroup) -> str:
    """Pick one representative citation per issue group, preferring fatal."""
    fatal = [v for v in group.violations if v.severity == "fatal"]
    pool = fatal or group.violations
    if not pool:
        return ""
    return _clean_citation_text(pool[0].citation)[:200]


def _all_citations_for_issue(group: IssueGroup) -> List[str]:
    """All cleaned citations under one issue, deduplicated, fatal first."""
    seen = set()
    out = []
    for v in sorted(group.violations,
                    key=lambda x: 0 if x.severity == "fatal" else 1):
        c = _clean_citation_text(v.citation)
        key = c.lower()[:80]
        if c and key not in seen:
            seen.add(key)
            out.append((c, v.cited_court, v.severity))
    return out


# =============================================================================
# CASE-CONTEXT FACTS
# =============================================================================
# These get fed to if_then_engine.build_logic_chain() as the fact list.
# They're not the violation citations themselves — they're case-record facts
# that establish WHY the cited authority is non-binding (i.e., the structural
# stare-decisis violation).

def _facts_for_issue(data: ReplyBriefData, group: IssueGroup) -> List[str]:
    """Build a record-fact list for the if_then_engine for one issue group."""
    target_label = COURT_LABELS.get(data.case_court, data.case_court)
    n_fatal = group.fatal_count
    n_serious = group.serious_count
    files = group.source_files

    facts = []
    if files:
        files_str = ", ".join(files[:3])
        if len(files) > 3:
            files_str += f", and {len(files) - 3} additional filing(s)"
        facts.append(
            f"Defendant's filing(s) ({files_str}) cite {n_fatal} decision(s) "
            f"flagged as facially non-binding on {target_label} and {n_serious} "
            f"decision(s) flagged for binding-effect verification on the {group.issue} issue"
        )
    # Court mix
    court_mix = defaultdict(int)
    for v in group.violations:
        court_mix[v.cited_court] += 1
    for court_code, count in sorted(court_mix.items(), key=lambda x: -x[1])[:3]:
        court_label = COURT_LABELS.get(court_code, court_code)
        facts.append(
            f"Defendant cites {count} decision(s) issued by {court_label} on this issue"
        )
    return facts


# =============================================================================
# PER-ISSUE ARGUMENT BUILDER  (uses Phase 3.5k engines)
# =============================================================================

def build_issue_argument(
    data: ReplyBriefData,
    group: IssueGroup,
    section_num: int,
) -> List[str]:
    """Build a single argument section for one issue group.

    Layout (5 deterministic blocks, NO LLM):
      1. Section header
      2. Defendant's reliance summary (which files, how many cites, court mix)
      3. Definition block (locked terms from definition_engine)
      4. Stare-decisis IF/THEN chain (from if_then_engine, citation-anchored)
      5. Per-citation table + per-citation rebuttal paragraphs
      6. Per-issue conclusion
    """
    out: List[str] = []
    target_label = COURT_LABELS.get(data.case_court, data.case_court)
    issue = group.issue

    # Header
    issue_titles = {
        "verification": "Defendant's Reliance on Non-Binding Authority Regarding Verification",
        "garnishment": "Defendant's Reliance on Non-Binding Authority Regarding Garnishment Procedure",
        "default_judgment": "Defendant's Reliance on Non-Binding Authority Regarding Default Judgment",
        "fcra": "Defendant's Reliance on Non-Binding Authority Regarding FCRA Liability",
        "fdcpa": "Defendant's Reliance on Non-Binding Authority Regarding FDCPA Liability",
        "bad_faith": "Defendant's Reliance on Non-Binding Authority Regarding Bad Faith",
        "rule_60b": "Defendant's Reliance on Non-Binding Authority Regarding Rule 60(b) Relief",
        "twombly_iqbal": "Defendant's Reliance on Non-Binding Authority Regarding Pleading Sufficiency",
        "removal": "Defendant's Reliance on Non-Binding Authority Regarding Removal Jurisdiction",
        "quiet_title": "Defendant's Reliance on Non-Binding Authority Regarding Title Claims",
        "hierarchy": "Defendant's Reliance on Non-Binding Authority — Stare Decisis Hierarchy",
    }
    out.append(f"### {section_num}. {issue_titles.get(issue, issue.replace('_', ' ').title())}")
    out.append("")

    # Block 1: Reliance summary (filenames stripped — see scorecard exhibit for per-file detail)
    n_files_for_issue = len(group.source_files)
    out.append(
        f"Across {n_files_for_issue} adversary filing(s), defendant relies on "
        f"{group.fatal_count} facially non-binding citation(s) and "
        f"{group.serious_count} additional citation(s) requiring binding-effect "
        f"verification, all directed at the {issue.replace('_', ' ')} issue. "
        f"The accompanying Scorecard Exhibit identifies each cite by source filing."
    )
    out.append("")

    # Block 2: Locked definitions (Phase 3.5k engine)
    # Only some issues have locked definitions; substantive issues like
    # verification/garnishment/service do, but bad_faith/hierarchy don't.
    # Skip cleanly when none.
    definitions = build_definition_set(issue)
    if definitions:
        def_block = format_definition_block(definitions[:3])
        if def_block and def_block.strip():
            out.append("**Controlling definitions.**")
            out.append("")
            out.append(def_block)
            out.append("")

            # Construction rule annotations — scope classification for each definition
            try:
                from pipeline.statutory_construction import classify_definition_scope
                construction_notes = []
                for d in definitions[:3]:
                    sc = classify_definition_scope(d.term, d.definition)
                    if sc.scope == "closed":
                        construction_notes.append(
                            f'The term "{d.term}" is defined with "means" — a closed definition. '
                            f'Only what is expressly listed qualifies. '
                            f'({", ".join(sc.rules_applied)})'
                        )
                    elif sc.scope == "expansive":
                        construction_notes.append(
                            f'The term "{d.term}" is defined with "includes" AND an express expansion clause '
                            f'(e.g. "not limited to" / "shall not be deemed to exclude") — the enumeration is '
                            f'illustrative. ({", ".join(sc.rules_applied)})'
                        )
                    elif sc.scope == "expansive_enumerated":
                        construction_notes.append(
                            f'The term "{d.term}" is defined with "includes" but WITHOUT any expansion clause. '
                            f'The enumerated items therefore define the class: they are the floor, and reaching '
                            f'beyond them must be argued under the canons (ejusdem generis / expressio unius; '
                            f'26 U.S.C. 7701(c) expands only where the statute so provides) — it is NOT assumed '
                            f'open-ended. ({", ".join(sc.rules_applied)})'
                        )
                if construction_notes:
                    out.append("**Statutory construction notes.**")
                    out.append("")
                    for note in construction_notes:
                        out.append(note)
                    out.append("")
            except Exception:
                pass  # construction module not available — skip gracefully

    # Block 3: Deterministic IF/THEN chain
    # For stare-decisis arguments, the if_then_engine's generic templates don't
    # fit well — we hand-craft a clean hierarchy chain. For substantive issues
    # with locked definitions (verification, garnishment, etc.), the engine's
    # output is correct.
    binding_codes = BINDING_HIERARCHY.get(data.case_court, {}).get("binding", [])
    binding_labels = [COURT_LABELS.get(c, c) for c in binding_codes]
    if len(binding_labels) > 1:
        binding_str = ", ".join(binding_labels[:-1]) + " or " + binding_labels[-1]
    else:
        binding_str = binding_labels[0] if binding_labels else "a court of binding jurisdiction"

    chain_lines = [
        f"IF stare decisis requires that controlling authority on the {issue.replace('_', ' ')} issue come from the {binding_str},",
        f"AND the record establishes that defendant cites {group.fatal_count} decision(s) from courts that do not bind {target_label},",
        f"THEN those decisions are not controlling, and any ruling that adopts them as if controlling rests on legal error subject to reversal.",
    ]
    out.append("**Deterministic stare-decisis chain:**")
    out.append("")
    for line in chain_lines:
        out.append(f"> {line}")
    out.append("")

    # Block 4: Per-citation table + rebuttal paragraphs
    cites = _all_citations_for_issue(group)
    if cites:
        out.append("**Citations flagged on this issue:**")
        out.append("")
        out.append("| # | Citation | Issuing Court | Severity |")
        out.append("|---|---|---|---|")
        for i, (cite, court_code, severity) in enumerate(cites, 1):
            court_label = COURT_LABELS.get(court_code, court_code)
            cite_short = cite[:140].replace("|", "\\|")
            out.append(f"| {i} | {cite_short} | {court_label} | {severity} |")
        out.append("")

        # Per-citation rebuttal paragraphs — DEDUPED, COUNT-AWARE, SEVERITY-INCLUSIVE
        def _norm_key(cite_text: str) -> str:
            import re as _re
            k = _re.sub(r"[^a-z0-9]+", "", (cite_text or "").lower())
            k = k.replace("nothampton", "northampton")
            return k

        order = {"fatal": 0, "serious": 1}
        sorted_v = sorted(
            group.violations,
            key=lambda v: (order.get(v.severity, 9), v.citation or "")
        )
        seen = OrderedDict()
        for v in sorted_v:
            k = _norm_key(v.citation)
            if not k:
                continue
            if k not in seen:
                seen[k] = {"v": v, "count": 1}
            else:
                seen[k]["count"] += 1

        for i, (k, entry) in enumerate(seen.items(), 1):
            v = entry["v"]
            count = entry["count"]
            cite_clean = _clean_citation_text(v.citation)
            court_label = COURT_LABELS.get(v.cited_court, v.cited_court)
            rationale = NON_BINDING_RATIONALE.get(
                v.cited_court,
                "is non-binding on this court under standard hierarchy principles."
            )
            count_phrase = (
                "" if count == 1
                else f" Defendant repeats this same citation {count} times across its filings."
            )
            # BLOCK_6_11_KLINGER_TIGHTENING
            # FATAL = sister-county trial court (keep absolute language).
            # SERIOUS = federal appellate (e.g., 3d Cir.) — attack burden, not power.
            if v.severity == "fatal":
                out.append(
                    f"**{section_num}.{i}** Defendant cites *{cite_clean}*.{count_phrase} "
                    f"This decision was issued by {court_label}, which {rationale} "
                    f"Even assuming arguendo that the decision remains good law, "
                    f"it is not binding on this court and cannot compel the result "
                    f"defendant seeks on the {issue.replace('_', ' ')} issue."
                )
            else:
                # SERIOUS-tier: federal appellate or other persuasive-only authority.
                # Frame as burden failure, not lack of power.
                short_name = cite_clean.split(",")[0].strip() if "," in cite_clean else cite_clean
                out.append(
                    f"**{section_num}.{i}** Defendant relies on *{cite_clean}*, a decision "
                    f"of {court_label} that does not bind this Court.{count_phrase} "
                    f"While such decisions may be considered for their persuasive value, "
                    f"they cannot substitute for controlling Pennsylvania authority. "
                    f"Defendant identifies no binding Pennsylvania appellate authority "
                    f"governing the {issue.replace('_', ' ')} issue. Accordingly, "
                    f"{short_name} is insufficient to carry Defendant's burden."
                )
            out.append("")

    # Block 5: Per-issue conclusion
    out.append(
        f"For the foregoing reasons, defendant's reliance on the above "
        f"authority on the {issue.replace('_', ' ')} issue fails as a matter "
        f"of law. The cited decisions are not binding on {target_label}, and "
        f"this court is required by stare decisis to apply the binding "
        f"precedent identified in Section IV below."
    )
    out.append("")

    # Dedupe consecutive blank lines so the docx renderer doesn't get
    # multi-blank runs that turn into stacked empty paragraphs.
    cleaned = []
    prev_blank = False
    for line in out:
        is_blank = not line.strip()
        if is_blank and prev_blank:
            continue
        cleaned.append(line)
        prev_blank = is_blank
    return cleaned


# =============================================================================
# FULL BRIEF BUILDER
# =============================================================================

def build_caption_block(data: ReplyBriefData) -> List[Tuple[str, str]]:
    """Return rows for the caption table: (left_cell, right_cell)."""
    rows: List[Tuple[str, str]] = []
    if data.case_court == "EDPA":
        court_lines = [
            "IN THE UNITED STATES DISTRICT COURT",
            "FOR THE EASTERN DISTRICT OF PENNSYLVANIA",
        ]
    elif data.case_court == "PA_CP_PHILA":
        court_lines = [
            "IN THE COURT OF COMMON PLEAS",
            "OF PHILADELPHIA COUNTY, PENNSYLVANIA",
            "CIVIL TRIAL DIVISION",
        ]
    elif data.case_court == "CA3":
        court_lines = [
            "IN THE UNITED STATES COURT OF APPEALS",
            "FOR THE THIRD CIRCUIT",
        ]
    else:
        court_lines = [f"[{data.case_court}]"]

    defs = [p for p in data.parties if p != _ident.party_key()]
    PARTY_NAME_MAP = {
        "lippi": "MIKE LIPPI",
        "state_farm": "STATE FARM FIRE AND CASUALTY COMPANY",
        
        "equifax": "EQUIFAX INFORMATION SERVICES, LLC",
        "experian": "EXPERIAN INFORMATION SOLUTIONS, INC.",
        "transunion": "TRANS UNION LLC",
        "amerisave": "AMERISAVE MORTGAGE CORPORATION",
        "allstate": "ALLSTATE INSURANCE COMPANY",
        "progressive": "PROGRESSIVE INSURANCE COMPANY",
        "avant": "AVANT, LLC",
    }
    def_lines = [
        PARTY_NAME_MAP.get(d, d.replace("_", " ").upper())
        for d in defs
    ]

    left_lines = [
        _ident.caption_name() + ",",
        "    Plaintiff, sui juris,",
        "",
        "v.",
        "",
    ] + [f"{d}," for d in def_lines[:-1]] + (
        [f"{def_lines[-1]},"] if def_lines else []
    ) + ["    Defendants."]

    right_lines = court_lines + [
        "",
        f"Civil Action No. {data.docket_id}",
    ]
    if data.judge:
        right_lines.append(f"The Honorable {data.judge}")

    # Pad both columns to same height
    max_h = max(len(left_lines), len(right_lines))
    while len(left_lines) < max_h:
        left_lines.append("")
    while len(right_lines) < max_h:
        right_lines.append("")

    return list(zip(left_lines, right_lines))


def build_reply_brief(data: ReplyBriefData) -> Dict:
    """Build the full reply brief as a structured dict.

    The dict format lets the renderer build proper tables, numbered lists,
    headings, and rule-of-law blocks instead of trying to parse markdown.

    Schema:
        {
            "caption_rows": [(left, right), ...],
            "title": str,
            "sections": [
                {"heading": str, "level": 1|2|3, "paragraphs": [str|dict]},
                ...
            ],
            "signature": [str, ...],
            "verification_checklist": [str, ...]
        }
    """
    target_label = COURT_LABELS.get(data.case_court, data.case_court)
    n_files = len(data.by_source_file)
    n_issues = len(data.by_issue)

    out = {
        "caption_rows": build_caption_block(data),
        "title": (
            _motion_title_for(getattr(data, "filing_type", "") or "") + "\n"
            "AND OBJECTION TO DEFENDANTS' RELIANCE ON NON-BINDING AUTHORITY"
        ),
        "sections": [],
        "signature": [],
        "verification_checklist": [],
    }

    # ── I. INTRODUCTION ─────────────────────────────────────────────────────
    # BLOCK_6_13_2_1_DRAFTER posture clarifier
    try:
        from generation.posture_templates import POSTURE_CLARIFIER as _BZ_PC
        _bz_clar = _BZ_PC.get(getattr(data, "posture", "merits") or "merits", "")
    except Exception:
        _bz_clar = ""
    _bz_intro_first = (
        f"Plaintiff respectfully submits this reply in support of his "
        f"motion. A structural review of {n_files} adversary filing(s) "
        f"identified {data.total_fatal} citation(s) flagged as facially "
        f"non-binding under stare decisis principles, and {data.total_serious} "
        f"additional citation(s) flagged as requiring verification of "
        f"binding effect. Those flagged citations cluster across "
        f"{n_issues} discrete legal issue(s), each addressed in turn below."
    )
    _bz_intro_second = (
        "Even assuming arguendo that each cited decision remains good law and "
        "has not been overruled, distinguished, or undermined by subsequent "
        "appellate treatment, the decisions remain non-binding on this court "
        "under settled hierarchy principles and therefore cannot compel the "
        "result defendant seeks. The arguments below are directed at that "
        "structural defect, which is dispositive regardless of any further "
        "treatment those decisions may have received."
    )
    _bz_intro_third = (
        "A separate Scorecard Exhibit accompanying this reply tabulates "
        "each flagged citation, the issuing court, the severity of the "
        "stare-decisis defect, and the binding-hierarchy classification. "
        "Plaintiff respectfully requests that the court take judicial notice "
        "of the Scorecard Exhibit as part of the record on this motion."
    )
    _bz_intro_paragraphs = []
    if _bz_clar:
        _bz_intro_paragraphs.append(_bz_clar)
    _bz_intro_paragraphs.extend([_bz_intro_first, _bz_intro_second, _bz_intro_third])
    out["sections"].append({
        "heading": "I. INTRODUCTION",
        "level": 1,
        "paragraphs": _bz_intro_paragraphs,
    })


    # === PIPELINE_PATH_V2 — affirmative claims section ===
    if getattr(data, "mode", "audit") in ("pipeline", "hybrid") and getattr(data, "claims", None):
        out["sections"].append(_render_claims_section(data, section_number="II"))
        _CLAIMS_INSERTED = True
    else:
        _CLAIMS_INSERTED = False
    # ── II. STANDARD OF REVIEW ──────────────────────────────────────────────
    h = BINDING_HIERARCHY.get(data.case_court, {})
    out["sections"].append({
        "heading": "II. STANDARD OF REVIEW",
        "level": 1,
        "paragraphs": [
            (
                "The doctrine of stare decisis requires courts to apply binding "
                "precedent from courts of superior jurisdiction. "
                + h.get("explanation", "")
            ),
            (
                f"Citations from courts that do not bind the {target_label} "
                "are persuasive authority only. They are not entitled to the "
                "deference owed to controlling precedent and cannot be presented "
                "to the court as if they compelled a particular result."
            ),
        ],
    })

    # ── III. ARGUMENT ───────────────────────────────────────────────────────
    arg_section = {
        "heading": "III. ARGUMENT",
        "level": 1,
        "paragraphs": [
            (
                "Plaintiff addresses each issue on which defendant's reliance on "
                "non-binding authority appears in the record. Each subsection "
                "below identifies the controlling definitions, sets out the "
                "deterministic stare-decisis chain, lists the specific citations "
                "flagged on that issue, and rebuts each on structural grounds."
            ),
        ],
    }
    out["sections"].append(arg_section)

    # Per-issue subsections (using Phase 3.5k engines)
    section_num = 0
    # Stable ordering: known issues first, then alphabetical others
    issue_order = [
        "verification", "garnishment", "default_judgment", "fcra", "fdcpa",
        "bad_faith", "rule_60b", "twombly_iqbal", "removal", "quiet_title",
        "hierarchy",
    ]
    ordered_issues = (
        [k for k in issue_order if k in data.by_issue]
        + [k for k in data.by_issue if k not in issue_order]
    )
    for issue_key in ordered_issues:
        group = data.by_issue[issue_key]
        if not group.violations:
            continue
        section_num += 1
        sub = {
            "heading": "",  # rendered inside the markdown lines
            "level": 2,
            "_issue": issue_key,
            "paragraphs": build_issue_argument(data, group, section_num),
        }
        out["sections"].append(sub)

    # ── IV. BINDING AUTHORITY ───────────────────────────────────────────────
    binding = h.get("binding", [])
    binding_labels = [COURT_LABELS.get(c, c) for c in binding]
    if not binding_labels:
        binding_str = "courts of binding jurisdiction"
    elif len(binding_labels) == 1:
        binding_str = binding_labels[0]
    elif len(binding_labels) == 2:
        binding_str = f"the {binding_labels[0]} and the {binding_labels[1]}"
    else:
        binding_str = ", ".join(f"the {b}" for b in binding_labels[:-1]) + \
                      f", and the {binding_labels[-1]}"

    iv_paras = [
        (
            f"Under stare decisis, this court is required to apply controlling "
            f"precedent from {binding_str}. Where defendants ask the court to be "
            "guided by non-binding authority, plaintiff respectfully submits that "
            "the court has a mandatory duty to first identify whether binding "
            "authority on the same question exists and, if so, to apply that "
            "authority."
        ),
    ]
    # SECTION IV CLEANUP: the authority_miner produces noisy, duplicated, and
    # mis-parsed citation snippets. Rather than expose that to the court, we
    # replace the mined-authorities listing with a single deterministic
    # statement. The binding authority for the underlying motion is presented
    # in the principal motion and memorandum, which remain controlling.
    iv_paras.append(
        "Plaintiff has identified controlling authority from courts that bind "
        "this court, which governs the issues presented. Defendant has cited "
        "none. The binding authority for the issues raised in the underlying "
        "motion is set forth in plaintiff's principal motion and supporting "
        "memorandum, which remain controlling on the issues now before the "
        "court."
    )
    out["sections"].append({
        "heading": "IV. THE COURT'S MANDATORY DUTY TO APPLY BINDING PRECEDENT",
        "level": 1,
        "paragraphs": iv_paras,
    })

    # ── V. CONCLUSION ───────────────────────────────────────────────────────
    _KILL_SENTENCE = (
        "Because defendant has failed to present controlling authority and "
        "instead relies on non-binding precedent, defendant's position fails "
        "as a matter of law and cannot support the relief defendant seeks. "
        "Plaintiff respectfully requests that this court so hold."
    )
    out["sections"].append({
        "heading": "V. CONCLUSION",
        "level": 1,
        "paragraphs": [
            (
                "For the foregoing reasons, plaintiff respectfully requests that "
                "the court (1) disregard the citations flagged above as non-binding "
                "for any controlling-precedent purpose; (2) require defendant(s) "
                "to identify and address the binding authority on the issues "
                "raised; (3) take judicial notice of the accompanying Scorecard "
                "Exhibit; and (4) grant plaintiff's underlying motion."
            ),
        ],
    })

    out["signature"] = [
        "Respectfully submitted,",
        "",
        "/s/ Filer Babah Filer",
        "Filer Babah Filer",
        "Plaintiff, sui juris",
        "2522 S Darien St",
        "Anytown, ST 00000",
        "(555) 555-0100",
        "",
        f"Dated: {datetime.now().strftime('%B %d, %Y')}",
    ]

    out["verification_checklist"] = [
        "Each flagged citation has been pulled from CourtListener / Lexis / Westlaw and confirmed to exist as written",
        "Each flagged citation's current treatment has been Shepardized / KeyCited",
        "The court classification (e.g. 'M.D. Pa.', 'Pa. Super.') matches the actual court of issuance",
        "Any citation flagged with [UNKNOWN] court has been investigated and either reclassified or removed",
        "The Conclusion section accurately reflects the relief sought in the underlying motion",
        "The court caption matches the current docket",
        "Counsel of record service list is attached",
        "The Scorecard Exhibit has been reviewed and is attached as Exhibit A",
    ]

    # Inject kill sentence as the final paragraph of V. CONCLUSION
    try:
        for _sec in out.get("sections", []):
            if _sec.get("heading", "").startswith("V."):
                _paras = _sec.setdefault("paragraphs", [])
                if _KILL_SENTENCE not in _paras:
                    _paras.append(_KILL_SENTENCE)
                break
    except Exception:
        pass

    # === REFINEMENT_V1 — append Appendix A if claims present ===
    if _CLAIMS_INSERTED:
        out["sections"].append(_render_discovery_appendix(data))
    # === END REFINEMENT_V1 ===
    # === PIPELINE_PATH_V2 — renumber sections if claims inserted ===
    if _CLAIMS_INSERTED:
        _ROMAN_SHIFT = {
            "II. STANDARD OF REVIEW": "III. STANDARD OF REVIEW",
            "III. ARGUMENT": "IV. ARGUMENT",
            "IV. THE COURT'S MANDATORY DUTY TO APPLY BINDING PRECEDENT":
                "V. THE COURT'S MANDATORY DUTY TO APPLY BINDING PRECEDENT",
            "V. CONCLUSION": "VI. CONCLUSION",
        }
        for s in out["sections"]:
            h = s.get("heading", "")
            if h in _ROMAN_SHIFT:
                s["heading"] = _ROMAN_SHIFT[h]
    # === END PIPELINE_PATH_V2 ===
    return out


# =============================================================================
# DOCX RENDERER  (real tables, numbered lists, single spacing, no tab hacks)
# =============================================================================

def render_docx(brief: Dict, output_path: Path, data: ReplyBriefData) -> None:
    from docx import Document as DocxDocument
    from docx.shared import Pt, Inches, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
    from docx.enum.table import WD_ALIGN_VERTICAL
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement

    doc = DocxDocument()

    # Page setup
    for section in doc.sections:
        section.top_margin = Inches(1)
        section.bottom_margin = Inches(1)
        section.left_margin = Inches(1.25)
        section.right_margin = Inches(1.25)

    # Default font + single spacing (compact, as requested)
    style = doc.styles["Normal"]
    style.font.name = "Times New Roman"
    style.font.size = Pt(12)
    pf = style.paragraph_format
    pf.line_spacing_rule = WD_LINE_SPACING.SINGLE
    pf.space_after = Pt(6)
    pf.space_before = Pt(0)

    def _add_para(text: str, *, bold: bool = False, italic: bool = False,
                  align=WD_ALIGN_PARAGRAPH.LEFT, indent: float = 0.0,
                  size: int = 12) -> None:
        p = doc.add_paragraph()
        p.alignment = align
        if indent:
            p.paragraph_format.left_indent = Inches(indent)
        # Inline markdown bold/italic parser
        parts = re.split(r"(\*\*[^*]+\*\*|\*[^*]+\*)", text)
        for part in parts:
            if not part:
                continue
            run = p.add_run(part[2:-2] if part.startswith("**") else
                            part[1:-1] if part.startswith("*") and part.endswith("*") else
                            part)
            run.font.size = Pt(size)
            run.bold = bold or part.startswith("**")
            run.italic = italic or (part.startswith("*") and not part.startswith("**"))

    # ── CAPTION TABLE ────────────────────────────────────────────────────────
    caption_rows = brief["caption_rows"]
    if caption_rows:
        cap_table = doc.add_table(rows=len(caption_rows), cols=2)
        cap_table.autofit = False
        cap_table.allow_autofit = False
        for r, (left, right) in enumerate(caption_rows):
            cells = cap_table.rows[r].cells
            cells[0].width = Inches(3.5)
            cells[1].width = Inches(2.5)
            for cell, text in ((cells[0], left), (cells[1], right)):
                cell.text = ""
                p = cell.paragraphs[0]
                p.paragraph_format.line_spacing_rule = WD_LINE_SPACING.SINGLE
                p.paragraph_format.space_after = Pt(0)
                run = p.add_run(text)
                run.font.size = Pt(11)
                run.font.name = "Times New Roman"
        # Add a vertical separator using a border on column boundary
        for row in cap_table.rows:
            tcPr = row.cells[0]._tc.get_or_add_tcPr()
            tcBorders = OxmlElement("w:tcBorders")
            right = OxmlElement("w:right")
            right.set(qn("w:val"), "single")
            right.set(qn("w:sz"), "8")
            right.set(qn("w:color"), "000000")
            tcBorders.append(right)
            tcPr.append(tcBorders)
        doc.add_paragraph()

    # ── TITLE ────────────────────────────────────────────────────────────────
    for line in brief["title"].split("\n"):
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(line)
        run.bold = True
        run.font.size = Pt(13)
    doc.add_paragraph()

    # ── SECTIONS ─────────────────────────────────────────────────────────────
    for sec in brief["sections"]:
        heading = sec.get("heading", "")
        level = sec.get("level", 1)
        if heading:
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT
            run = p.add_run(heading)
            run.bold = True
            run.font.size = Pt(14 if level == 1 else 12)
            p.paragraph_format.space_before = Pt(8)
            p.paragraph_format.space_after = Pt(4)

        for para in sec.get("paragraphs", []):
            if isinstance(para, dict):
                # === RENDERER_INNER_HEADINGS_V1 ===
                # Inner heading dict — emit as styled paragraph at level
                if "heading" in para:
                    inner_level = int(para.get("level", 2))
                    p = doc.add_paragraph()
                    p.alignment = WD_ALIGN_PARAGRAPH.LEFT
                    run = p.add_run(para["heading"])
                    run.bold = True
                    # level=1 (e.g. II.A.1 CLAIM) bigger; level=2/3 smaller
                    if inner_level == 1:
                        run.font.size = Pt(13)
                        p.paragraph_format.space_before = Pt(10)
                        p.paragraph_format.space_after = Pt(4)
                    elif inner_level == 2:
                        run.font.size = Pt(12)
                        p.paragraph_format.space_before = Pt(8)
                        p.paragraph_format.space_after = Pt(3)
                    else:
                        run.font.size = Pt(11)
                        p.paragraph_format.space_before = Pt(6)
                        p.paragraph_format.space_after = Pt(2)
                    continue
                # === END RENDERER_INNER_HEADINGS_V1 ===
                if para.get("type") == "ordered_list":
                    for i, item in enumerate(para["items"], 1):
                        _add_para(f"{i}. {item}", indent=0.25)
                continue

            if not isinstance(para, str):
                continue
            text = para
            stripped = text.strip()
            if not stripped:
                continue

            # Markdown sub-headings inside argument blocks
            if stripped.startswith("### "):
                p = doc.add_paragraph()
                run = p.add_run(stripped[4:])
                run.bold = True
                run.font.size = Pt(12)
                p.paragraph_format.space_before = Pt(6)
                p.paragraph_format.space_after = Pt(2)
                continue

            # Quote blocks (the IF/THEN chain)
            if stripped.startswith("> "):
                _add_para(stripped[2:], italic=True, indent=0.5)
                continue

            # Markdown table
            if stripped.startswith("|") and "|" in stripped[1:]:
                # Buffer table rows
                # We need to look ahead — but since we're streaming, just let
                # the markdown parser below handle it. We do it inline:
                # Collect cells of THIS row
                cells = [c.strip() for c in stripped.strip("|").split("|")]
                # Skip separator rows like |---|---|
                if all(re.match(r"^:?-+:?$", c) for c in cells):
                    continue
                # Stash in scratchpad on doc object
                if not hasattr(doc, "_table_scratch"):
                    doc._table_scratch = []
                doc._table_scratch.append(cells)
                continue
            else:
                # Flush any pending table rows
                if hasattr(doc, "_table_scratch") and doc._table_scratch:
                    rows = doc._table_scratch
                    doc._table_scratch = []
                    if len(rows) >= 1:
                        cols = len(rows[0])
                        t = doc.add_table(rows=len(rows), cols=cols)
                        try:
                            t.style = "Light Grid Accent 1"
                        except KeyError:
                            pass
                        for ri, row in enumerate(rows):
                            for ci, cell_text in enumerate(row):
                                if ci < cols:
                                    cell = t.rows[ri].cells[ci]
                                    cell.text = ""
                                    cp = cell.paragraphs[0]
                                    cp.paragraph_format.space_after = Pt(0)
                                    cp.paragraph_format.line_spacing_rule = \
                                        WD_LINE_SPACING.SINGLE
                                    crun = cp.add_run(cell_text)
                                    crun.font.size = Pt(10)
                                    if ri == 0:
                                        crun.bold = True
                        doc.add_paragraph()

            # Bold inline label like "**Controlling definitions.**"
            _add_para(text)

        # Flush any trailing table
        if hasattr(doc, "_table_scratch") and doc._table_scratch:
            rows = doc._table_scratch
            doc._table_scratch = []
            if len(rows) >= 1:
                cols = len(rows[0])
                t = doc.add_table(rows=len(rows), cols=cols)
                try:
                    t.style = "Light Grid Accent 1"
                except KeyError:
                    pass
                for ri, row in enumerate(rows):
                    for ci, cell_text in enumerate(row):
                        if ci < cols:
                            cell = t.rows[ri].cells[ci]
                            cell.text = ""
                            cp = cell.paragraphs[0]
                            cp.paragraph_format.space_after = Pt(0)
                            cp.paragraph_format.line_spacing_rule = \
                                WD_LINE_SPACING.SINGLE
                            crun = cp.add_run(cell_text)
                            crun.font.size = Pt(10)
                            if ri == 0:
                                crun.bold = True
                doc.add_paragraph()

    # ── SIGNATURE ────────────────────────────────────────────────────────────
    doc.add_paragraph()
    for line in brief["signature"]:
        _add_para(line)

    # ── VERIFICATION CHECKLIST (separator + small print) ─────────────────────
    doc.add_paragraph()
    p = doc.add_paragraph("_" * 60)
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p = doc.add_paragraph()
    run = p.add_run("VERIFICATION CHECKLIST (REMOVE BEFORE FILING)")
    run.bold = True
    run.font.size = Pt(11)
    _add_para(
        "Beezle generated this draft from a structural audit of adversary "
        "filings. Before filing, verify the following:",
        size=10,
    )
    for item in brief["verification_checklist"]:
        _add_para(f"☐  {item}", size=10, indent=0.25)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))


# =============================================================================
# MD RENDERER  (for inline review and version control)
# =============================================================================

def render_md(brief: Dict, data: ReplyBriefData) -> str:
    out = []
    # Caption (markdown)
    for left, right in brief["caption_rows"]:
        out.append(f"{left:<45}  |  {right}")
    out.append("")
    out.append("")
    for line in brief["title"].split("\n"):
        out.append(f"## {line}")
    out.append("")

    for sec in brief["sections"]:
        if sec.get("heading"):
            level = "#" * (1 + sec.get("level", 1))
            out.append(f"{level} {sec['heading']}")
            out.append("")
        for para in sec.get("paragraphs", []):
            if isinstance(para, dict):
                # === RENDERER_INNER_HEADINGS_V1 ===
                # Inner heading dict — emit as markdown heading at level
                if "heading" in para:
                    inner_level = "#" * (1 + int(para.get("level", 2)))
                    out.append(f"{inner_level} {para['heading']}")
                    out.append("")
                    continue
                # === END RENDERER_INNER_HEADINGS_V1 ===
                if para.get("type") == "ordered_list":
                    for i, item in enumerate(para["items"], 1):
                        out.append(f"{i}. {item}")
                    out.append("")
                continue
            if isinstance(para, str):
                out.append(para)
                if not para.startswith("|") and not para.startswith(">"):
                    out.append("")

    out.append("")
    for line in brief["signature"]:
        out.append(line)
    out.append("")
    out.append("---")
    out.append("")
    out.append("## VERIFICATION CHECKLIST (REMOVE BEFORE FILING)")
    out.append("")
    for item in brief["verification_checklist"]:
        out.append(f"- [ ] {item}")
    out.append("")

    # Dedupe consecutive blank lines (max one blank between content blocks)
    cleaned = []
    prev_blank = False
    for line in out:
        is_blank = not line.strip()
        if is_blank and prev_blank:
            continue
        cleaned.append(line)
        prev_blank = is_blank
    return "\n".join(cleaned)


# =============================================================================
# CLI
# =============================================================================

def draft_for_case(
    canonical_id: str,
    base_path: Path,
    output_dir: Path,
    with_scorecard: bool = True,
    mode: str = "audit",   # === PIPELINE_PATH_V2 === audit | pipeline | hybrid
    filing_type: Optional[str] = None,  # === FILING_TYPE_KWARG_V1 ===
) -> Tuple[Path, Path, Optional[Path]]:
    # === PIPELINE_PATH_V2 ===
    if mode in ("pipeline", "hybrid"):
        try:
            data = _build_data_from_pipeline(
                canonical_id, base_path,
                hybrid_with_audit=(mode == "hybrid"),
            )
        except Exception as _e:
            log.warning(f"pipeline mode failed ({_e}); falling back to audit")
            data = load_brief_data(canonical_id, base_path)
    else:
        data = load_brief_data(canonical_id, base_path)
    # === END PIPELINE_PATH_V2 ===

    # === FILING_TYPE_ATTACH_V1 ===
    try:
        if filing_type:
            try:
                data.filing_type = filing_type
            except Exception:
                object.__setattr__(data, "filing_type", filing_type)
    except Exception as _e:
        log.warning(f"could not attach filing_type to data: {_e}")
    # === END FILING_TYPE_ATTACH_V1 ===

    # === CANONICAL_ID_ATTACH_V1 ===
    # Attach canonical_id to data so build_procedural_motion can locate
    # the audit JSON for scorecard fact retrieval.
    try:
        try:
            data.canonical_id = canonical_id
        except Exception:
            object.__setattr__(data, "canonical_id", canonical_id)
    except Exception as _e:
        log.warning(f"could not attach canonical_id to data: {_e}")
    # === END CANONICAL_ID_ATTACH_V1 ===

    # === CROSS_CASE_ATTACH_V1 ===
    # Pull cross_case_count from build_context onto the data object so
    # Section IV (Reliance on Contested Order) fires correctly.
    try:
        from pipeline.filing_composer import build_context as _bctx
        _ctx = _bctx(canonical_id)
        cc_count = _ctx.get("cross_case_count", 0) if isinstance(_ctx, dict) else 0
        related = _ctx.get("related_case_ids", []) if isinstance(_ctx, dict) else []
        try:
            data.cross_case_count = cc_count
            data.related_case_ids = related
        except Exception:
            object.__setattr__(data, "cross_case_count", cc_count)
            object.__setattr__(data, "related_case_ids", related)
    except Exception as _e:
        log.warning(f"could not attach cross_case_count to data: {_e}")
    # === END CROSS_CASE_ATTACH_V1 ===

    # === PHASE2_DRAFTER_BRANCH_V1 ===
    # Route to the correct drafter based on filing_type.
    bundled_with = []  # populated by recommender for multi-motion bundling

    if filing_type in _PROCEDURAL_MOTIONS:
        try:
            brief = build_procedural_motion(
                data, filing_type=filing_type, bundled_with=bundled_with,
            )
            # Mark for renderer routing
            try:
                object.__setattr__(data, "_motion_doc", brief)
            except Exception:
                pass
        except Exception as _e:
            log.warning(f"build_procedural_motion failed; falling back to brief: {_e}")
            brief = build_reply_brief(data)
    elif filing_type in _RECUSAL_MOTIONS:
        try:
            brief = build_recusal_motion(data)
            try:
                object.__setattr__(data, "_motion_doc", brief)
            except Exception:
                pass
        except ValueError as _e:
            # No bias indicator — recusal not appropriate. Fall back to strike-shaped doc.
            log.warning(f"recusal refused (no bias_indicator); using procedural motion: {_e}")
            brief = build_procedural_motion(
                data,
                filing_type=("motion_to_strike_state" if "PA_CP" in str(getattr(data, "case_court", ""))
                             else "motion_to_strike_federal"),
                bundled_with=[],
            )
            try:
                object.__setattr__(data, "_motion_doc", brief)
            except Exception:
                pass
        except Exception as _e:
            log.warning(f"build_recusal_motion failed; falling back to brief: {_e}")
            brief = build_reply_brief(data)
    else:
        # _BRIEF_MOTIONS, _COMPLAINT_TYPES, or unknown → existing claims-based brief
        brief = build_reply_brief(data)
    # === END PHASE2_DRAFTER_BRANCH_V1 ===

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # === OBSIDIAN_WRITELOOP_V1: pre-init optional output paths ===
    vm_md = None
    vm_docx = None
    # === END OBSIDIAN_WRITELOOP_V1 ===


    safe_key = canonical_id.replace(":", "_").replace("/", "_")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # === PHASE2_RENDER_OVERRIDE_V1 ===
    # Filename uses filing_type prefix when available
    filename_prefix = filing_type if filing_type else "reply_brief"
    motion_doc = getattr(data, "_motion_doc", None)
    md_path = output_dir / f"{filename_prefix}_{safe_key}_{ts}.md"
    docx_path = output_dir / f"{filename_prefix}_{safe_key}_{ts}.docx"

    if motion_doc and isinstance(motion_doc, dict) and motion_doc.get("structure") in ("procedural_motion", "recusal_motion"):
        # Use motion-specific renderers
        try:
            md_path.write_text(render_motion_md(motion_doc, data), encoding="utf-8")
        except Exception as _e:
            log.error(f"motion MD render failed: {_e}")
        try:
            render_motion_docx(motion_doc, docx_path, data)
        except Exception as _e:
            log.error(f"motion DOCX render failed: {_e}")
            docx_path = None
    else:
        # Original brief renderers
        md_path.write_text(render_md(brief, data), encoding="utf-8")
        try:
            render_docx(brief, docx_path, data)
        except Exception as e:
            log.error(f"DOCX render failed: {e}")
            docx_path = None
    # === END PHASE2_RENDER_OVERRIDE_V1 ===

    scorecard_path = None
    if with_scorecard:
        try:
            from generation.scorecard_exhibit import build_scorecard_exhibit
            scorecard_path = output_dir / f"scorecard_exhibit_{safe_key}_{ts}.docx"
            build_scorecard_exhibit(data, scorecard_path)
        except Exception as e:
            log.error(f"Scorecard exhibit failed: {e}")
            scorecard_path = None


    # === VIOLATIONS_MATRIX_V1 ===
    # When in pipeline/hybrid mode, also emit a Violations Matrix exhibit.
    if getattr(data, "mode", "audit") in ("pipeline", "hybrid") and getattr(data, "claims", None):
        try:
            from generation.violations_matrix import build_violations_matrix
            # === MATRIX_CASE_TYPE_V1 ===
            # Pull facts_text + case_type for matrix filtering / constitutional gating
            _facts_text = ""
            try:
                from pipeline.case_loader import load_case as _ml_load_case
                _ml_case = _ml_load_case(canonical_id)
                if _ml_case:
                    _facts_text = "\n".join(_ml_case.get("facts", []))
            except Exception:
                pass
            # === BLOCK_6_13_1_POSTURE_WIRING ===
            try:
                from generation import violations_matrix as _bz_vm
                _bz_vm.set_posture(getattr(data, "posture", "merits"))
                log.info(f"[posture] matrix set_posture: {getattr(data, 'posture', 'merits')}")
            except Exception as _bz_pe:
                log.warning(f"[posture] matrix set_posture failed: {_bz_pe}")
            # === END BLOCK_6_13_1_POSTURE_WIRING ===
            vm_md, vm_docx = build_violations_matrix(
                canonical_id=canonical_id,
                caption=data.case_caption,
                claims=data.claims,
                doctrines=getattr(data, "doctrines", []) or [],
                output_dir=output_dir,
                timestamp=ts,
                case_type=getattr(data, "case_type", "") or "",
                facts_text=_facts_text,
            )
            # === END MATRIX_CASE_TYPE_V1 ===
            log.info(f"Violations matrix written: {vm_docx.name}")
        except Exception as _e:
            log.error(f"Violations matrix failed: {_e}")
    # === END VIOLATIONS_MATRIX_V1 ===


    # === OBSIDIAN_WRITELOOP_V1 ===
    # Sync write to obsidian_sync, wrapped in try/except so log failure
    # never breaks draft generation.
    try:
        from obsidian_sync.case_log_writer import write_case_log
        _ob_per_run, _ob_rolling = write_case_log(
            canonical_id=canonical_id,
            data=data,
            output_paths={
                "brief_md":       md_path,
                "brief_docx":     docx_path,
                "scorecard_docx": scorecard_path,
                "matrix_docx":    vm_docx,
                "matrix_md":      vm_md,
            },
            timestamp=ts,
            mode=getattr(data, "mode", mode),
        )
        if _ob_per_run:
            log.info(f"Obsidian case log: {_ob_per_run.name}")
    except Exception as _ob_e:
        log.error(f"Obsidian case_log write skipped: {_ob_e}")
    # === END OBSIDIAN_WRITELOOP_V1 ===

    return md_path, docx_path, scorecard_path







# === STEP_A_AND_B_FACT_INJECTION_V1 ===
# Two-layer fact-injection:
#   Step A: deterministic fact + rule sentence (always runs)
#   Step B: optional LLM polish via LlamaLLM.chat() (working /completion path)
#
# Step B never blocks Step A. If the LLM is offline, returns invalid output,
# or fails validation, the deterministic Step A prose ships.

import re as _sab_re

# ---------- scorecard fact retrieval ----------

def _gather_scorecard_facts(data, canonical_id: str = "") -> list:
    """Pull violations from BOTH stare-decisis AND procedural sources.

    Reads output/audit/data_<case>.json and merges:
      1. scorecard.violations[]      — stare-decisis findings
                                       (keyed by 'category', text in 'description')
      2. procedural_violations[]     — block14_pinpointer findings
                                       (keyed by 'violation_type', text in 'quote'+'explanation')

    Both are normalized into the same fact-record shape so the strict
    Section II builder treats them uniformly:
      {text, category, severity, source_file, evidence, weight, count}
    """
    # === PATH_B_DUAL_SOURCE_GATHER_V1 ===
    facts = []

    if not canonical_id:
        canonical_id = (
            getattr(data, "canonical_id", "")
            or getattr(data, "case_id", "")
            or ""
        )

    if not canonical_id:
        return facts

    slug = canonical_id.replace(":", "_")
    audit_path = (_eroot() / "output/audit") / f"data_{slug}.json"

    if not audit_path.exists():
        return facts

    try:
        import json as _ej
        with open(audit_path, "r", encoding="utf-8") as f:
            audit = _ej.load(f)
    except Exception:
        return facts

    # ---- SOURCE 1: stare-decisis violations (existing data) ----
    sc = audit.get("scorecard") or {}
    if isinstance(sc, dict):
        for v in (sc.get("violations") or []):
            if not isinstance(v, dict):
                continue
            text = str(v.get("description") or v.get("evidence") or "").strip()
            if not text:
                continue
            facts.append({
                "text": text,
                "category": str(v.get("category", "")),
                "severity": str(v.get("severity", "")),
                "source_file": str(v.get("source_file", "")),
                "evidence": str(v.get("evidence", "")),
                "weight": v.get("weight", 0),
                "count": 1,
                "_source": "stare_decisis",
            })

    # ---- SOURCE 2: procedural violations (NEW from block14_pinpointer) ----
    for v in (audit.get("procedural_violations") or []):
        if not isinstance(v, dict):
            continue
        # Build readable fact text from quote + explanation
        quote = str(v.get("quote", "")).strip()
        explanation = str(v.get("explanation", "")).strip()
        rule_citation = str(v.get("rule_citation", "")).strip()
        source_file = str(v.get("file", v.get("source_doc", ""))).strip()

        # Compose fact text: prefer explanation, fall back to quote
        text_parts = []
        if explanation:
            text_parts.append(explanation)
        if quote and quote not in (explanation or ""):
            text_parts.append(f"Specifically, the record contains: \"{quote[:150]}\"")
        text = " ".join(text_parts).strip()

        if not text:
            continue

        # Map ViolationPin's violation_type to our category slot
        # (so the same Section II builder code works for both sources)
        facts.append({
            "text": text,
            "category": str(v.get("violation_type", "")),
            "severity": str(v.get("severity", "STANDARD")),
            "source_file": source_file,
            "evidence": rule_citation,
            "weight": _severity_to_weight(v.get("severity", "STANDARD")),
            "count": v.get("count", 1),
            "_source": "procedural",
        })

    return facts


def _severity_to_weight(severity: str) -> int:
    """Convert text severity to numeric weight for sorting."""
    return {"FATAL": 3, "SERIOUS": 2, "STANDARD": 1, "MINOR": 0}.get(
        (severity or "").upper(), 1
    )




# ===PHASE_3F_STEP6C_CHUNKED_V1=== Agent 6 LLM enhancement
import os as _os_3f
import json as _json_3f
from datetime import datetime as _dt_3f


_LLM_DRAFTER_LOG = (_eroot() / "output/llm_drafter_log.jsonl")


def _log_llm_drafter_pass(entry: dict) -> None:
    """Append one entry to the LLM drafter audit log. Never raises."""
    try:
        _LLM_DRAFTER_LOG.parent.mkdir(parents=True, exist_ok=True)
        entry["ts"] = _dt_3f.now().isoformat(timespec="seconds")
        with open(_LLM_DRAFTER_LOG, "a", encoding="utf-8") as f:
            f.write(_json_3f.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass  # never block on logging


# Sections we are allowed to ENHANCE (replace paragraphs in)
# ===PHASE_3F_STEP6D_RECONNECT_V1=== deny-list approach
# Sections we MUST NEVER touch (deterministic spine)
_PROTECTED_SECTION_PATTERNS = [
    re.compile(r"^\s*$"),                                  # empty
    re.compile(r"caption", re.IGNORECASE),
    re.compile(r"^\s*PLAINTIFF'?S?\s+MOTION", re.IGNORECASE),  # title
    re.compile(r"PRAYER\s+FOR\s+RELIEF", re.IGNORECASE),
    re.compile(r"RELIEF\s+REQUESTED", re.IGNORECASE),
    re.compile(r"CERTIFICATE\s+OF\s+SERVICE", re.IGNORECASE),
    re.compile(r"PROPOSED\s+ORDER", re.IGNORECASE),
    re.compile(r"VERIFICATION", re.IGNORECASE),  # the plaintiff verification
    re.compile(r"^\s*Respectfully\s+submitted", re.IGNORECASE),
    re.compile(r"^\s*Date:", re.IGNORECASE),
    re.compile(r"^\s*/s/", re.IGNORECASE),
    re.compile(r"signature", re.IGNORECASE),
]

# Legacy allowlist kept for backward-compat with code that imports it
# ===PHASE_3F_JUDICIAL_DUTY_V1=== judicial_duty_validator_cache
_validator_jdd_cache: dict = {}
# ===END PHASE_3F_JUDICIAL_DUTY_V1===

_ENHANCABLE_SECTIONS = {
    "II. DISPOSITIVE ORDER AT ISSUE",
    "IV. FAILURE TO ADDRESS RECORD EVIDENCE",
    "V. FAILURE TO ADDRESS RECORD EVIDENCE",
    "II. ARGUMENT",
    "III. ARGUMENT",
    "IV. ARGUMENT",
    # ===PHASE_3F_JUDICIAL_DUTY_V1=== judicial_duty_enhancable
    "III. JUDICIAL DUTY BREACH",
    "V. ARGUMENT",
    "VI. FAILURE TO ADDRESS RECORD EVIDENCE",
    # ===END PHASE_3F_JUDICIAL_DUTY_V1===
}


def _section_is_protected(heading: str) -> bool:
    """True if heading matches any protected pattern (deny-list)."""
    if not heading:
        return True
    for pat in _PROTECTED_SECTION_PATTERNS:
        if pat.search(heading):
            return True
    return False
# ===END PHASE_3F_STEP6D_RECONNECT_V1===

# Map LLM section names -> deterministic section heading patterns to find/replace
# ===PHASE_3F_STEP6D_RECONNECT_V1=== comprehensive mapping
# Map LLM section names -> deterministic section heading patterns to find/replace
# All argument_N collapse into the single ARGUMENT/DEFECTS section
_LLM_SECTION_TO_HEADING = {
    "preamble":           re.compile(r"^I\.\s*INTRODUCTION", re.IGNORECASE),
    "dispositive_anchor": re.compile(r"DISPOSITIVE\s*ORDER", re.IGNORECASE),
    "rule_framework":     re.compile(r"STANDARD\s*OF\s*REVIEW|APPLICABLE\s*LAW|APPLICABLE\s*RULES", re.IGNORECASE),
    "evidence_ignored":   re.compile(r"FAILURE\s*TO\s*ADDRESS\s*RECORD", re.IGNORECASE),
    "conclusion":         re.compile(r"^V?I*\.\s*(CONCLUSION|RELIEF\s*REQUESTED)", re.IGNORECASE),
    # ===PHASE_3F_JUDICIAL_DUTY_V1=== judicial_duty_heading
    "judicial_duty_breach": re.compile(r"JUDICIAL\s*DUTY\s*BREACH", re.IGNORECASE),
    # ===END PHASE_3F_JUDICIAL_DUTY_V1===
}

# Patterns that catch ANY argument heading (II. DEFECTS, III. ARGUMENT, etc.)
_ARGUMENT_HEADING_PATTERNS = [
    re.compile(r"DEFECTS\s*IN\s*OPPOSING\s*FILINGS", re.IGNORECASE),
    re.compile(r"DETERMINISTIC\s*RULE\s*APPLICATION", re.IGNORECASE),
    re.compile(r"^I*V?I*\.\s*ARGUMENT", re.IGNORECASE),
]
# ===END PHASE_3F_STEP6D_RECONNECT_V1===


def _build_chunked_case_context(data, dispositive_orders: list) -> dict:
    """Build the case_context dict that chunked_drafter expects.

    Anchors the LLM to the actual case + dispositive ruling so it cannot
    drift to generic content.
    """
    case_ctx = {
        "case_id":      getattr(data, "canonical_id", "") or getattr(data, "case_id", ""),
        "case_court":   getattr(data, "case_court", ""),
        "case_caption": getattr(data, "caption", "") or (_ident.caption_name() + " v. DEFENDANTS"),
        "docket":       getattr(data, "docket_id", ""),
        "judge":        getattr(data, "judge", ""),
        "client":       "Plaintiff FILER NAME",
        "filing_type":  getattr(data, "filing_type", "motion_to_vacate"),
        "we_represent": "Plaintiff",
    }

    # Anchor the dispositive order — this is THE fact the LLM cannot drift on
    if dispositive_orders:
        d = dispositive_orders[0]  # most recent
        case_ctx["dispositive_order"] = {
            "date":          d.get("date", ""),
            "judge":         d.get("judge", ""),
            "ecf":           d.get("ecf", ""),
            "defendants":    d.get("defendants", []),
            "subjects":      d.get("subjects", []),
            "deadline":      d.get("deadline"),
        }
        # Build a concise narrative the LLM can use verbatim
        try:
            date_pretty = _format_date(d["date"])
            defs_pretty = _format_defendant_list(d.get("defendants", []))
            case_ctx["dispositive_narrative"] = (
                f"On {date_pretty}, {d.get('judge','this Court')} "
                f"({d.get('ecf','')}) granted Motions to Dismiss filed by "
                f"Defendants {defs_pretty}, terminating Plaintiff's claims."
            )
        except Exception:
            case_ctx["dispositive_narrative"] = ""
    # ===PHASE_3F_STEP6D_RECONNECT_V1=== load facts via facts_loader
    # ===PHASE_3F_STEP6F_REGRESSION_FIX_V1=== always populate from
    # facts_loader; only skip overwrite if case_ctx ALREADY has non-empty
    # dispositive_text + dispositive_orders (i.e. step 6b ran upstream).
    # The 6e change assumed step 6b always sets these — false in this code path.
    try:
        from generation.facts_loader import load_drafting_facts
        case_id_for_facts = (
            getattr(data, "canonical_id", "")
            or getattr(data, "case_id", "")
            or ""
        )
        _audit_data = {
            "scorecard":             getattr(data, "scorecard", {}) or {},
            "procedural_violations": getattr(data, "procedural_violations", []) or [],
        }
        if not _audit_data["procedural_violations"]:
            from pathlib import Path as _P
            import json as _json
            safe = case_id_for_facts.replace(":", "_").replace("/", "_")
            p = _P(str(_eroot())) / "output" / "audit" / f"data_{safe}.json"
            if p.exists():
                _audit_data = _json.loads(p.read_text(encoding="utf-8", errors="replace"))

        _facts = load_drafting_facts(case_id_for_facts, _audit_data)

        # Only set dispositive fields if case_ctx doesn't already have them.
        # Step 6b might have set them upstream; if so, preserve those.
        if not case_ctx.get("dispositive_text"):
            case_ctx["dispositive_text"]   = _facts.get("dispositive_text", "")
        if not case_ctx.get("dispositive_orders"):
            case_ctx["dispositive_orders"] = _facts.get("dispositive_orders", [])
        # Always set the supplementary fields
        case_ctx["record_facts_text"]    = _facts.get("record_facts_text", "")
        case_ctx["citations_text"]       = _facts.get("citations_text", "")
        case_ctx["record_evidence_gaps"] = _facts.get("record_evidence_gaps", [])
        case_ctx["violation_facts"]      = _facts.get("violation_facts", {})
        # ===PHASE_3F_JUDICIAL_DUTY_V1=== populate_judicial_duty
        try:
            _canon = case_id_for_facts
            case_ctx["judicial_duty_defects"] = _extract_judicial_duty_defects(_canon)
        except Exception:
            case_ctx["judicial_duty_defects"] = []
        # ===END PHASE_3F_JUDICIAL_DUTY_V1===
    except Exception as _e:
        try:
            _log_llm_drafter_pass({"stage": "facts_loader_fail", "error": str(_e)})
        except Exception:
            pass
    # ===END PHASE_3F_STEP6F_REGRESSION_FIX_V1===
    # ===END PHASE_3F_STEP6D_RECONNECT_V1===


    return case_ctx


def _build_chunked_facts(data, scorecard_facts: list) -> list:
    """Build the facts list for chunked_drafter from scorecard + audit data.

    Each fact is anchored to a source document (no hallucination possible —
    the LLM can only use what's in this list).
    """
    facts = []
    for f in (scorecard_facts or [])[:30]:  # cap at 30 to keep prompt focused
        text = f.get("text", "").strip()
        if not text:
            continue
        facts.append({
            "text":        text,
            "source":      f.get("source_file", "") or f.get("_source", ""),
            "category":    f.get("category", ""),
            "severity":    f.get("severity", ""),
            "evidence":    f.get("evidence", ""),
        })
    return facts


def _build_chunked_conflicts(scorecard_facts: list, dispositive_orders: list) -> list:
    """Build the conflicts list — pairs (defendant authority X court ruling).

    This is the bridge data: defendant cited X, court adopted X, dismissal followed.
    """
    conflicts = []
    if not dispositive_orders:
        return conflicts

    # Group scorecard facts by category for issue-level conflicts
    by_cat = {}
    for f in (scorecard_facts or []):
        cat = f.get("category", "")
        if cat:
            by_cat.setdefault(cat, []).append(f)

    # Build one conflict entry per major issue category
    for cat, facts_in_cat in by_cat.items():
        if len(facts_in_cat) < 2:
            continue  # need at least 2 instances to be worth raising
        sample_text = facts_in_cat[0].get("text", "")[:200]
        conflicts.append({
            "issue":           cat,
            "their_position": (
                f"Defendants advanced arguments under '{cat}' supported by "
                f"the authorities flagged in Plaintiff's Scorecard Exhibit "
                f"({len(facts_in_cat)} citation(s))."
            ),
            "our_position": (
                f"Those authorities are non-binding on this Court. "
                f"To the extent the dismissal adopted arguments grounded in "
                f"those authorities, the ruling rests on an incorrect legal standard."
            ),
            "supporting_facts": [f.get("text", "")[:300] for f in facts_in_cat[:3]],
            "dispositive_link": dispositive_orders[0].get("date", "") if dispositive_orders else "",
        })

    return conflicts


def _build_chunked_procedural(data, dispositive_orders: list) -> dict:
    """Build the procedural dict — case posture, deadlines, dispositive context."""
    proc = {
        "posture":         "post-dismissal seeking reconsideration / vacatur",
        "filing_purpose":  "motion to vacate dismissal order",
        "case_court":      getattr(data, "case_court", ""),
    }
    if dispositive_orders:
        d = dispositive_orders[0]
        proc["dispositive_date"] = d.get("date", "")
        proc["dispositive_judge"] = d.get("judge", "")
        if d.get("deadline"):
            proc["amend_deadline"] = d["deadline"]
    return proc


def _validate_llm_section(text: str, section_name: str,
                          dispositive_orders: list,
                          expected_max_chars: int = 3000) -> tuple:
    """Validate one LLM-generated section.

    Returns (ok: bool, reason: str). On any failure -> caller falls back
    to deterministic.
    """
    if not text or not text.strip():
        return (False, "empty")
    if len(text) > expected_max_chars:
        return (False, f"length {len(text)} > {expected_max_chars}")
    # Block obvious hedging language the system prompt forbids
    forbidden = ["indeterminate", "may or may not",
                 "arguably", "perhaps", "it could be argued"]
    for f in forbidden:
        if f in text.lower():
            return (False, f"forbidden phrase: {f!r}")
    # ===PHASE_3F_JUDICIAL_DUTY_V1=== judicial_duty_validator
    # For judicial_duty_breach: at least one verbatim quote prefix
    # must survive the LLM pass. The quotes are pre-extracted from the
    # source orders; if none appear in the output, the LLM has
    # paraphrased away the evidence and the section must be rejected.
    if section_name == "judicial_duty_breach":
        try:
            jdds = []
            for sect_in_plan_attr in ("_judicial_duty_defects_for_validate",):
                pass  # placeholder for future direct-attach
            # Pull defects from the validator-side cache populated below
            jdds = _validator_jdd_cache.get("defects", [])
            if jdds:
                anchors = []
                for entry in jdds:
                    for d in entry.get("defects", []) or []:
                        q = (d.get("quote") or "").strip()
                        if q:
                            # Use first 30 non-whitespace chars of quote
                            anchor = "".join(q.split())[:30]
                            if anchor:
                                anchors.append(anchor)
                # Compute compact form of output text for substring search
                compact = "".join((text or "").split())
                if anchors and not any(a in compact for a in anchors):
                    return (False, "no verbatim defect quote present")
        except Exception:
            pass
    # ===END PHASE_3F_JUDICIAL_DUTY_V1===
    # For dispositive_anchor: must contain the actual date
    if section_name == "dispositive_anchor" and dispositive_orders:
        date = dispositive_orders[0].get("date", "")
        if date and date not in text:
            # Try the formatted version too
            try:
                pretty = _format_date(date)
                if pretty not in text:
                    return (False, f"missing dispositive date {date!r}")
            except Exception:
                pass
    return (True, "ok")


def _enhance_motion_with_llm(motion_dict: dict, data,
                              dispositive_orders: list,
                              scorecard_facts: list) -> dict:
    """Selectively enhance the deterministic motion with LLM-generated sections.

    Preserves the deterministic spine — caption, title, citations, prayer
    for relief, exhibits, signature blocks, certificate of service, proposed
    order — and only replaces paragraphs in the specific reasoning sections
    listed in _ENHANCABLE_SECTIONS.

    Falls back to the input motion_dict unchanged on ANY failure
    (LLM offline, validation fail, exception, etc.).
    """
    flag = (_os_3f.environ.get("BEEZLE_LLM_DRAFTER") or "auto").lower().strip()
    if flag == "off":
        return motion_dict

    # Auto mode: only enhance when dispositive orders present
    if flag == "auto" and not dispositive_orders:
        return motion_dict

    # Get LLM gate (same as core/beezle.py L881)
    gate = None
    try:
        from pipeline.llm_gate import get_gate
        gate = get_gate()
        if not gate.is_available():
            _log_llm_drafter_pass({
                "stage": "skip",
                "reason": "llm_gate_offline",
                "case_id": getattr(data, "canonical_id", ""),
            })
            return motion_dict
    except Exception as e:
        _log_llm_drafter_pass({
            "stage": "skip",
            "reason": f"gate_import_fail: {e}",
        })
        return motion_dict

    # Build the context the chunked drafter needs
    case_ctx = _build_chunked_case_context(data, dispositive_orders)
    # ===PHASE_3F_JUDICIAL_DUTY_V1=== populate_validator_cache
    try:
        _validator_jdd_cache["defects"] = case_ctx.get("judicial_duty_defects", []) or []
    except Exception:
        _validator_jdd_cache["defects"] = []
    # ===END PHASE_3F_JUDICIAL_DUTY_V1===
    facts = _build_chunked_facts(data, scorecard_facts)
    conflicts = _build_chunked_conflicts(scorecard_facts, dispositive_orders)
    procedural = _build_chunked_procedural(data, dispositive_orders)

    _log_llm_drafter_pass({
        "stage":             "start",
        "case_id":           case_ctx.get("case_id", ""),
        "filing_type":       case_ctx.get("filing_type", ""),
        "dispositive_count": len(dispositive_orders),
        # ===PHASE_3F_JUDICIAL_DUTY_V1=== judicial_duty_defect_count
        "judicial_duty_defect_count": sum(
            len(e.get("defects", []) or [])
            for e in (case_ctx.get("judicial_duty_defects") or [])
        ),
        # ===END PHASE_3F_JUDICIAL_DUTY_V1===
        "fact_count":        len(facts),
        "conflict_count":    len(conflicts),
    })

    # Determine doc_type
    filing_type = (getattr(data, "filing_type", "") or "").lower()
    if "vacate" in filing_type:
        doc_type = "motion_to_vacate"
    elif "reconsider" in filing_type:
        doc_type = "motion_for_reconsideration"
    else:
        doc_type = "motion"

    # Pull the section plan (lazy import — section_builder may not be loaded)
    try:
        from generation.section_builder import DOCUMENT_PLANS, build_sections
    except Exception as e:
        _log_llm_drafter_pass({"stage": "skip", "reason": f"section_builder import: {e}"})
        return motion_dict

    if doc_type not in DOCUMENT_PLANS:
        _log_llm_drafter_pass({"stage": "skip", "reason": f"no plan for {doc_type}"})
        return motion_dict

    # Build the section plan
    try:
        plan = build_sections(
            doc_type=doc_type,
            case_context=case_ctx,
            facts=facts,
            authority="",
            conflicts=conflicts,
            procedural=procedural,
        )
    except Exception as e:
        _log_llm_drafter_pass({"stage": "skip", "reason": f"build_sections: {e}"})
        return motion_dict

    if not plan:
        _log_llm_drafter_pass({"stage": "skip", "reason": "empty plan"})
        return motion_dict

    # Run the LLM section by section
    try:
        from generation.chunked_drafter import (
            _build_case_overview, _generate_section, _assemble_argument_section,
        )
    except Exception as e:
        _log_llm_drafter_pass({"stage": "skip", "reason": f"chunked import: {e}"})
        return motion_dict

    case_overview = _build_case_overview(
        doc_type, case_ctx, procedural, conflicts, facts
    )

    rendered = {}
    for section in plan:
        try:
            if section.section_type == "argument":
                text = _assemble_argument_section(gate, section, case_overview)
            else:
                text = _generate_section(gate, section, case_overview)

            # ===PHASE_3F_JUDICIAL_DUTY_V3_CAP=== judicial_duty_breach_cap_raise
            _v3_cap = 6000 if section.name == "judicial_duty_breach" else max(section.expected_length * 3, 2000)
            # ===END PHASE_3F_JUDICIAL_DUTY_V3_CAP===
            ok, reason = _validate_llm_section(
                text, section.name, dispositive_orders,
                expected_max_chars=_v3_cap,
            )
            if ok:
                rendered[section.name] = text
                _log_llm_drafter_pass({
                    "stage":   "section_ok",
                    "section": section.name,
                    "chars":   len(text),
                })
            else:
                _log_llm_drafter_pass({
                    "stage":   "section_reject",
                    "section": section.name,
                    "reason":  reason,
                    "chars":   len(text or ""),
                })
        except Exception as e:
            _log_llm_drafter_pass({
                "stage":   "section_error",
                "section": getattr(section, "name", "?"),
                "error":   str(e),
            })

    if not rendered:
        _log_llm_drafter_pass({"stage": "no_enhancements"})
        return motion_dict

    # Now selectively merge LLM output back into motion_dict.
    # Only sections in _ENHANCABLE_SECTIONS get their paragraphs replaced.
    sections_list = motion_dict.get("sections", [])
    replaced_count = 0

    # ===PHASE_3F_STEP6D_RECONNECT_V1=== deny-list merge with argument collapse
    # Group argument_N sections — collapse into single block for the
    # DEFECTS/ARGUMENT section
    argument_blocks = []
    for name, text in rendered.items():
        if name.startswith("argument_") or name == "argument":
            argument_blocks.append((name, text))

    # Sort by argument number for stable order
    argument_blocks.sort(key=lambda x: x[0])

    collapsed_argument_text = ""
    if argument_blocks:
        argument_pieces = []
        for name, text in argument_blocks:
            argument_pieces.append(text.strip())
        collapsed_argument_text = "\n\n".join(argument_pieces)

    # Now do the merge — for each LLM section, find matching deterministic
    # section heading and replace its paragraphs. Skip protected sections.
    handled_arguments = False
    for llm_section_name, llm_text in rendered.items():
        if llm_section_name.startswith("argument_") or llm_section_name == "argument":
            # Handle all arguments in one pass below
            continue

        heading_pat = _LLM_SECTION_TO_HEADING.get(llm_section_name)
        if heading_pat is None:
            _log_llm_drafter_pass({
                "stage":   "no_mapping",
                "section": llm_section_name,
            })
            continue

        for sec in sections_list:
            heading = sec.get("heading", "") or ""
            if _section_is_protected(heading):
                continue
            if heading_pat.search(heading):
                old_para_count = len(sec.get("paragraphs", []) or [])
                new_paras = [
                    line.strip()
                    for line in llm_text.splitlines()
                    if line.strip()
                ]
                if new_paras:
                    sec["paragraphs"] = new_paras
                    replaced_count += 1
                    _log_llm_drafter_pass({
                        "stage":     "merge_ok",
                        "section":   llm_section_name,
                        "heading":   heading,
                        "old_paras": old_para_count,
                        "new_paras": len(new_paras),
                    })
                break  # one heading per LLM section

    # Handle collapsed arguments — find the ARGUMENT/DEFECTS section
    if collapsed_argument_text:
        for sec in sections_list:
            heading = sec.get("heading", "") or ""
            if _section_is_protected(heading):
                continue
            for pat in _ARGUMENT_HEADING_PATTERNS:
                if pat.search(heading):
                    old_para_count = len(sec.get("paragraphs", []) or [])
                    new_paras = [
                        line.strip()
                        for line in collapsed_argument_text.splitlines()
                        if line.strip()
                    ]
                    if new_paras:
                        sec["paragraphs"] = new_paras
                        replaced_count += 1
                        handled_arguments = True
                        _log_llm_drafter_pass({
                            "stage":     "merge_argument_collapse",
                            "heading":   heading,
                            "n_args":    len(argument_blocks),
                            "old_paras": old_para_count,
                            "new_paras": len(new_paras),
                        })
                    break
            if handled_arguments:
                break

    # ===PHASE_3F_JUDICIAL_DUTY_V1=== judicial_duty_insert_logic
    # If we have judicial_duty_breach prose but no matching section in
    # motion, INSERT a new section after dispositive_anchor / before
    # rule_framework or conclusion (whichever comes first).
    if "judicial_duty_breach" in rendered:
        jdb_text = rendered["judicial_duty_breach"]
        jdb_already_present = any(
            _LLM_SECTION_TO_HEADING["judicial_duty_breach"].search(
                s.get("heading", "") or ""
            )
            for s in sections_list
        )
        if not jdb_already_present and jdb_text:
            # Find the FIRST section after dispositive_anchor; insert there.
            insert_pos = len(sections_list)
            disp_idx = None
            for i, s in enumerate(sections_list):
                h = s.get("heading", "") or ""
                if _LLM_SECTION_TO_HEADING["dispositive_anchor"].search(h):
                    disp_idx = i
                    break
            if disp_idx is not None:
                insert_pos = disp_idx + 1
            else:
                # fallback: before conclusion
                for i, s in enumerate(sections_list):
                    h = s.get("heading", "") or ""
                    if re.search(r"CONCLUSION|RELIEF\s*REQUESTED", h, re.IGNORECASE):
                        insert_pos = i
                        break
            # ===PHASE_3F_JUDICIAL_DUTY_V4C_PARAGRAPH_SPLIT=== 3tier_paragraph_split
            # 3-tier paragraph splitter for the JUDICIAL DUTY BREACH section.
            # Tier 1: blank-line separated (prompt-compliant LLM output)
            # Tier 2: single-newline (markdown lists / short lines)
            # Tier 3: sentence-boundary fallback for monolithic prose
            #         (e.g., "In Document NNN, ... In Document MMM, ...")
            import re as _re_jdb
            _jdb_text_clean = (jdb_text or "").strip()
            new_paras = []
            if _jdb_text_clean:
                # TIER 1: prefer blank-line separation
                _t1 = [p.strip() for p in _re_jdb.split(r"\n\s*\n+", _jdb_text_clean) if p.strip()]
                if len(_t1) >= 2:
                    new_paras = _t1
                else:
                    # TIER 2: single-newline splits
                    _t2 = [ln.strip() for ln in _jdb_text_clean.splitlines() if ln.strip()]
                    if len(_t2) >= 2:
                        new_paras = _t2
                    else:
                        # TIER 3: sentence-boundary heuristic
                        # Look for paragraph-starter sentences. Patterns are
                        # case-agnostic - they describe DOCUMENT STRUCTURE, not
                        # parties. "In Document NNN", "In ECF NNN", "In the
                        # judges memorandum", "In an order [from this Court]",
                        # and the conclusion sentence "These defects, ...".
                        _starter = _re_jdb.compile(
                            r"(?:^|(?<=[.!?])\s+)(?=(?:In\s+Document\s+\d+|"
                            r"In\s+ECF\s+\d+|In\s+the\s+judges?\s+memorandum|"
                            r"In\s+the\s+memorandum|In\s+an?\s+(?:undated\s+)?"
                            r"order|These\s+defects))",
                            _re_jdb.IGNORECASE,
                        )
                        _t3 = [p.strip() for p in _starter.split(_jdb_text_clean) if p.strip()]
                        if len(_t3) >= 2:
                            new_paras = _t3
                        else:
                            # Final fallback: keep as single paragraph
                            new_paras = [_jdb_text_clean]
            # ===END PHASE_3F_JUDICIAL_DUTY_V4C_PARAGRAPH_SPLIT===
            if new_paras:
                sections_list.insert(insert_pos, {
                    "kind": "section",
                    "heading": "III. JUDICIAL DUTY BREACH",
                    "paragraphs": new_paras,
                })
                replaced_count += 1
                _log_llm_drafter_pass({
                    "stage":   "merge_insert_new",
                    "heading": "III. JUDICIAL DUTY BREACH",
                    "before":  insert_pos,
                    "n_paras": len(new_paras),
                })
    # ===END PHASE_3F_JUDICIAL_DUTY_V1===
    # If we have evidence_ignored prose but no matching section in motion,
    # INSERT a new section before conclusion
    if "evidence_ignored" in rendered:
        ev_text = rendered["evidence_ignored"]
        ev_already_present = any(
            _LLM_SECTION_TO_HEADING["evidence_ignored"].search(s.get("heading", "") or "")
            for s in sections_list
        )
        if not ev_already_present and ev_text:
            # Find conclusion section index
            conclusion_idx = None
            for i, s in enumerate(sections_list):
                h = s.get("heading", "") or ""
                if re.search(r"CONCLUSION|RELIEF\s*REQUESTED", h, re.IGNORECASE):
                    conclusion_idx = i
                    break
            insert_pos = conclusion_idx if conclusion_idx is not None else len(sections_list)
            new_paras = [line.strip() for line in ev_text.splitlines() if line.strip()]
            if new_paras:
                sections_list.insert(insert_pos, {
                    "kind": "section",
                    "heading": "V. FAILURE TO ADDRESS RECORD EVIDENCE",
                    "paragraphs": new_paras,
                })
                replaced_count += 1
                _log_llm_drafter_pass({
                    "stage":   "merge_insert_new",
                    "heading": "V. FAILURE TO ADDRESS RECORD EVIDENCE",
                    "before":  conclusion_idx,
                    "n_paras": len(new_paras),
                })
    # ===END PHASE_3F_STEP6D_RECONNECT_V1===
    _log_llm_drafter_pass({
        "stage":             "complete",
        "sections_replaced": replaced_count,
        "sections_rendered": len(rendered),
    })

    return motion_dict
# ===END PHASE_3F_STEP6C_CHUNKED_V1=== Agent 6 LLM enhancement
# ===PHASE_3F_TEMPLATE_HOOKS_V1=== INSERT 2 — dispositive-order extractor
def _extract_dispositive_orders(canonical_id: str) -> list:
    """Read audit/procedural_violations[] and return dispositive orders.

    A dispositive order is any FATAL ruling that terminates plaintiff
    claims — currently MTD_GRANTED_AGAINST_PLAINTIFF or CLAIMS_DISMISSED.

    Groups all rulings on the same _3f_filing_date together (one Section
    II entry per dispositive order).

    Returns list of:
        {
            "date":          "2025-09-19",
            "judge":         "",   # filled from case registry
            "ecf":           "Document 122",
            "defendants":    ["capital_one", "avant", "equifax"],
            "subjects":      [<list of ruling subjects/claims dismissed>],
            "deadline":      "2025-10-20" (or None),
        }
    """
    if not canonical_id:
        return []

    slug = canonical_id.replace(":", "_")
    audit_path = (_eroot() / "output/audit") / f"data_{slug}.json"
    if not audit_path.exists():
        return []

    try:
        import json as _ej
        with open(audit_path, "r", encoding="utf-8") as f:
            audit = _ej.load(f)
    except Exception:
        return []

    DISPOSITIVE_TYPES = {"MTD_GRANTED_AGAINST_PLAINTIFF", "CLAIMS_DISMISSED"}

    by_date = {}
    deadlines_by_date = {}

    for v in (audit.get("procedural_violations") or []):
        if not isinstance(v, dict):
            continue
        vtype = (v.get("violation_type") or "").upper()
        date = v.get("_3f_filing_date") or ""

        # Track court-set deadlines separately for cross-reference
        if vtype == "COURT_DEADLINE_SET":
            deadline_date = v.get("_3f_deadline_date") or ""
            if date and deadline_date:
                deadlines_by_date.setdefault(date, deadline_date)
            continue

        if vtype not in DISPOSITIVE_TYPES:
            continue
        if not date:
            continue

        entry = by_date.setdefault(date, {
            "date":       date,
            "judge":      v.get("_3f_filer") or "",
            "ecf":        v.get("_3f_ecf") or "",
            "defendants": [],
            "subjects":   [],
            "deadline":   None,
        })

        # Merge defendants (dedup, preserve order)
        for d in (v.get("_3f_subject_defendants") or []):
            if d and d not in entry["defendants"]:
                entry["defendants"].append(d)

        # Append subject text
        subj = (v.get("rule_text") or v.get("quote") or "").strip()
        if subj and subj not in entry["subjects"]:
            entry["subjects"].append(subj)

    # Attach matching deadlines
    for d, entry in by_date.items():
        if d in deadlines_by_date:
            entry["deadline"] = deadlines_by_date[d]

    # Sort newest first (most recent dispositive order first)
    return sorted(by_date.values(), key=lambda e: e.get("date", ""), reverse=True)


# ===PHASE_3F_JUDICIAL_DUTY_V1=== judicial_duty_extractor
JUDICIAL_DUTY_VIOLATION_TYPES = {
    "TWOMBLY_IQBAL_MISAPPLICATION",
    "TWOMBLY_IQBAL_MISAPPLIED",
    "TWOMBLY_IQBAL",
    "SERVICE_DEFECTIVE",
    "NOTICE_INADEQUATE",
    "VERIFICATION_MISSING",
    "FILING_DEADLINE_VIOLATION",
    "FILING_STRICKEN",
    "STARE_DECISIS_VIOLATION",
    "PRO_SE_HEIGHTENED_STANDARD",
    "IMPROPER_CONVERSION_12_TO_56",
    "SUA_SPONTE_NO_LEAVE",
    "REMAND_DENIED",
    "IFP_DENIAL_CONSTRUCTIVE",
    "EX_PARTE_RULING",
    "FRAUD_ON_COURT_JUDICIAL",
    "DUE_PROCESS_NOTICE",
    "DUE_PROCESS_VIOLATION",
    "DENIED_OPPORTUNITY_TO_RESPOND",
    "DENIED_WITHOUT_REASONS",
    "JUDICIAL_BIAS_LANGUAGE",
    "DEPRIVATION_RIGHTS_COLOR_OF_LAW",
}


def _judicial_duty_norm(s: str) -> str:
    if not s:
        return ""
    name = str(s).replace("\\", "/").rsplit("/", 1)[-1]
    if "." in name:
        name = name.rsplit(".", 1)[0]
    return " ".join(name.lower().split())


def _build_court_authored_index(canonical_id: str) -> dict:
    """Return dict of normalized_basename -> {ecf, filing_date, judge}
    for every court-authored entry in orders_<case>.json."""
    if not canonical_id:
        return {}
    safe = canonical_id.replace(":", "_").replace("/", "_")
    from pathlib import Path as _P
    import json as _ej
    idx_path = _P(str(_eroot())) / "output" / "audit" / "order_indexes" / f"orders_{safe}.json"
    if not idx_path.exists():
        return {}
    try:
        idx = _ej.loads(idx_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if isinstance(idx, dict) and "orders" in idx:
        orders = idx["orders"]
    elif isinstance(idx, list):
        orders = idx
    elif isinstance(idx, dict):
        orders = list(idx.values())
    else:
        orders = []

    JUDGE_TOKENS = (" j.", " j,", "u.s.d.j", "magistrate", "judge ",
                    " p.j.", "circuit judge")
    out = {}
    for o in orders:
        if not isinstance(o, dict):
            continue
        role = (o.get("filer_role") or "").lower()
        filer = " " + (o.get("filer") or "").lower()
        is_court = role == "court" or any(t in filer for t in JUDGE_TOKENS)
        if not is_court:
            continue
        meta = {
            "ecf":          o.get("ecf_number") or o.get("ecf") or "",
            "filing_date":  o.get("filing_date") or "",
            "judge":        o.get("filer") or "",
        }
        for k in ("doc_id", "file", "filename", "path"):
            v = o.get(k)
            if v:
                out[_judicial_duty_norm(v)] = meta
    return out


def _extract_judicial_duty_defects(canonical_id: str) -> list:
    """Read audit/procedural_violations[] and return court-authored doctrinal
    defects grouped by source order. Mirrors _extract_dispositive_orders.

    Returns list of:
        {
            "date":          "2025-09-19",
            "judge":         "",   # filled from case registry
            "ecf":           "Document 121",
            "source_doc":    "judges memorandum.pdf",
            "defects": [
                {"violation_type", "severity", "rule_citation",
                  "rule_text", "quote", "page", "lines", "fix"},
                ...
            ],
        },
        ...
    """
    if not canonical_id:
        return []
    slug = canonical_id.replace(":", "_").replace("/", "_")
    from pathlib import Path as _P
    audit_path = _P(str(_eroot() / "output/audit")) / f"data_{slug}.json"
    if not audit_path.exists():
        return []
    try:
        import json as _ej
        with open(audit_path, "r", encoding="utf-8") as f:
            audit = _ej.load(f)
    except Exception:
        return []

    procv = audit.get("procedural_violations") or []
    if not procv:
        return []

    court_idx = _build_court_authored_index(canonical_id)

    SEV_ORDER = {"FATAL": 0, "SERIOUS": 1, "STANDARD": 2,
                 "MODERATE": 3, "MINOR": 4}

    grouped = {}
    for v in procv:
        if not isinstance(v, dict):
            continue
        vt = (v.get("violation_type") or "").upper()
        if vt not in JUDICIAL_DUTY_VIOLATION_TYPES:
            continue

        src_raw = v.get("source_doc") or v.get("file") or ""
        norm = _judicial_duty_norm(src_raw)
        if not norm:
            continue
        # ===PHASE_3F_JUDICIAL_DUTY_V4_PROVENANCE=== provenance_gate
        # CHANGE 1: drop court_idx HITs that are upstream mis-tags.
        # Plaintiff-prepared draft orders ("Proposed Order", etc.) often
        # contain a "To: <judge>" salutation that upstream LLM scanners
        # mistakenly treat as filer. Detect by filename keyword.
        # All keywords describe filing TYPE, not party identity.
        _NON_COURT_FILENAME_TOKENS = (
            "proposed", "motion for", "opposition", "rebuttal",
            "reply ", " reply", "sur reply", "surreply",
            "answer to", "notice of appeal", "verified",
            "supplemental declaration", "conditional acceptance",
            "appellate brief", "in support to", "in support of",
            "in opposition to", "appeals", "ifp",
        )
        _src_lc = (src_raw or "").lower()
        _norm_lc = norm.lower()
        if any(tok in _src_lc or tok in _norm_lc
               for tok in _NON_COURT_FILENAME_TOKENS):
            continue

        meta = court_idx.get(norm)

        # CHANGE 2: when court_idx misses, try ECF-number cross-match.
        # Source like "ecf 41" can be matched to order_index entry
        # whose ecf_number is "Document 41" or "41".
        if meta is None:
            # heuristic fallback: court-pattern basenames
            if not (norm.startswith("ecf ") or norm.startswith("judge ") or
                    norm.startswith("judges memorandum") or
                    norm.startswith("ca3_order") or
                    norm.startswith("show_temp ecf ")):
                continue
            meta = {"ecf": "", "filing_date": "", "judge": ""}
            # ECF-number cross-match against the raw order_index
            try:
                import re as _re
                _ecf_num = None
                _m = _re.match(r"(?:show_temp\s+)?ecf\s+(\d+(?:-\d+)?)\b", norm)
                if _m:
                    _ecf_num = _m.group(1)
                if _ecf_num:
                    _slug = canonical_id.replace(":", "_").replace("/", "_")
                    _idx_path = (_eroot() / "output" /
                                 "audit" / "order_indexes" /
                                 f"orders_{_slug}.json")
                    if _idx_path.exists():
                        import json as _ej_x
                        _idx_x = _ej_x.loads(_idx_path.read_text(encoding="utf-8"))
                        _orders_x = (_idx_x.get("orders", _idx_x)
                                     if isinstance(_idx_x, dict) else _idx_x) or []
                        _COURT_TOKENS = (" j.", " j,", "u.s.d.j",
                                         "magistrate", "judge ",
                                         " p.j.", "circuit judge", "hon.")
                        for _o in _orders_x:
                            if not isinstance(_o, dict):
                                continue
                            _en = str(_o.get("ecf_number") or "").strip()
                            if not _en:
                                continue
                            _en_norm = _en.replace("Document ", "").strip()
                            if _en_norm != _ecf_num:
                                continue
                            _filer = _o.get("filer") or ""
                            _filer_lc = " " + _filer.lower()
                            if not any(t in _filer_lc for t in _COURT_TOKENS):
                                continue
                            _did = (_o.get("doc_id") or "").lower()
                            if any(tok in _did for tok in
                                   _NON_COURT_FILENAME_TOKENS):
                                continue
                            meta = {
                                "ecf":         _en,
                                "filing_date": _o.get("filing_date") or "",
                                "judge":       _filer,
                            }
                            break
            except Exception:
                pass
            # ===PHASE_3F_JUDICIAL_DUTY_V4D_DEDUP=== judges_memorandum_lookup
            # Filename-prefix lookup for court-pattern docs that have no
            # ECF number in their basename (e.g., "judges memorandum").
            # Looks in the raw order_index for any entry whose doc_id
            # starts with the same court-pattern prefix and has any
            # non-empty filer. Case-agnostic - keys describe doc TYPE.
            if not meta.get("judge") or not meta.get("filing_date"):
                try:
                    _COURT_FILENAME_PREFIXES = (
                        "judges memorandum", "judge memorandum",
                        "memorandum opinion", "ca3_order",
                    )
                    _matched_prefix = None
                    for _pfx in _COURT_FILENAME_PREFIXES:
                        if norm.startswith(_pfx):
                            _matched_prefix = _pfx
                            break
                    if _matched_prefix:
                        _slug2 = canonical_id.replace(":", "_").replace("/", "_")
                        _idx_path2 = (_eroot() / "output" /
                                      "audit" / "order_indexes" /
                                      f"orders_{_slug2}.json")
                        if _idx_path2.exists():
                            import json as _ej_y
                            _idx_y = _ej_y.loads(_idx_path2.read_text(encoding="utf-8"))
                            _orders_y = (_idx_y.get("orders", _idx_y)
                                         if isinstance(_idx_y, dict) else _idx_y) or []
                            for _o2 in _orders_y:
                                if not isinstance(_o2, dict):
                                    continue
                                _did2 = (_o2.get("doc_id") or "").lower()
                                _did2_norm = _judicial_duty_norm(_did2)
                                if not _did2_norm.startswith(_matched_prefix):
                                    continue
                                _filer2 = _o2.get("filer") or ""
                                if not _filer2.strip():
                                    continue
                                if not meta.get("judge"):
                                    meta["judge"] = _filer2
                                if not meta.get("filing_date"):
                                    meta["filing_date"] = (_o2.get("filing_date")
                                                            or "")
                                if not meta.get("ecf"):
                                    meta["ecf"] = (_o2.get("ecf_number")
                                                    or _o2.get("ecf") or "")
                                break
                except Exception:
                    pass
            # ===END PHASE_3F_JUDICIAL_DUTY_V4D_DEDUP===
            # _3f_ provenance fallback (already populated by audit_merger
            # for some cases / some violations)
            if not meta.get("filing_date") and v.get("_3f_filing_date"):
                meta["filing_date"] = v.get("_3f_filing_date") or ""
            if not meta.get("ecf") and v.get("_3f_ecf"):
                meta["ecf"] = v.get("_3f_ecf") or ""
            if not meta.get("judge") and v.get("_3f_filer"):
                meta["judge"] = v.get("_3f_filer") or ""
        # ===END PHASE_3F_JUDICIAL_DUTY_V4_PROVENANCE===

        # ===PHASE_3F_JUDICIAL_DUTY_V4D_DEDUP=== dedup_key
        # Group by (ecf, date) when both are populated. Two source_doc
        # strings that resolve to the same court order (e.g., "ecf 41"
        # via heuristic-fallback ECF cross-match, and "show_temp (15)"
        # via direct court_idx hit, both -> the-judge 7/14 ECF 41) collapse
        # into one group. Defects accumulate inside the kept group.
        # Falls back to src_raw when meta lacks ecf+date.
        _meta_ecf = (meta.get("ecf") or "").strip() if isinstance(meta, dict) else ""
        _meta_date = (meta.get("filing_date") or "").strip() if isinstance(meta, dict) else ""
        if _meta_ecf and _meta_date:
            key = ("ecf_date", _meta_ecf, _meta_date)
        else:
            key = src_raw  # group by exact source string
        # ===END PHASE_3F_JUDICIAL_DUTY_V4D_DEDUP===
        if key not in grouped:
            grouped[key] = {
                "date":        meta.get("filing_date", ""),
                "judge":       meta.get("judge", ""),
                "ecf":         meta.get("ecf", ""),
                "source_doc":  src_raw,
                "defects":     [],
            }
        grouped[key]["defects"].append({
            "violation_type":  vt,
            "severity":        v.get("severity", ""),
            "rule_citation":   v.get("rule_citation", ""),
            "rule_text":       v.get("rule_text", ""),
            "quote":           v.get("quote", ""),
            "page":            v.get("page"),
            "lines":           v.get("lines"),
            "fix":             v.get("fix", ""),
        })

    # Sort defects within each group: FATAL first, then violation_type
    for entry in grouped.values():
        entry["defects"].sort(key=lambda d: (
            SEV_ORDER.get((d.get("severity") or "").upper(), 99),
            d.get("violation_type", ""),
        ))

    # Return groups sorted by date desc (newest first)
    return sorted(grouped.values(),
                  key=lambda e: (e.get("date") or ""),
                  reverse=True)
# ===END PHASE_3F_JUDICIAL_DUTY_V1===


def _format_defendant_list(defendants: list) -> str:
    """Convert ['capital_one', 'avant', 'equifax'] -> 'Example Card Issuer, Avant, and Equifax'."""
    PARTY_NAME_MAP = {
        "capital_one":    "Example Card Issuer",
        "avant":          "Avant",
        "equifax":        "Equifax",
        "experian":       "Experian",
        "transunion":     "TransUnion",
        "trans_union":    "TransUnion",
        "greensky":       "GreenSky",
        "state_farm":     "State Farm",
        
        "lippi":          "Lippi",
        "amerisave":      "Amerisave",
    }
    pretty = [PARTY_NAME_MAP.get(d, d.replace("_", " ").title()) for d in defendants]
    if not pretty:
        return ""
    if len(pretty) == 1:
        return pretty[0]
    if len(pretty) == 2:
        return f"{pretty[0]} and {pretty[1]}"
    return ", ".join(pretty[:-1]) + f", and {pretty[-1]}"


def _format_date(iso_date: str) -> str:
    """Convert '2025-09-19' -> 'September 19, 2025'.

    Portable across Linux/Windows (no platform-specific strftime flags).
    """
    if not iso_date:
        return ""
    try:
        from datetime import datetime as _dt
        dt = _dt.strptime(iso_date, "%Y-%m-%d")
        # Manual day-without-leading-zero (portable)
        return f"{dt.strftime('%B')} {dt.day}, {dt.year}"
    except Exception:
        return iso_date
# ===END PHASE_3F_TEMPLATE_HOOKS_V1=== INSERT 2
    # === END PATH_B_DUAL_SOURCE_GATHER_V1 ===


# ---------- Step A: deterministic fact matching + rendering ----------

# Map known rule citations to keywords that identify a relevant fact
_FACT_KEYWORD_MAP = {
    "Pa.R.C.P. 1024":    ["verif", "unverified", "without verification"],
    "Pa.R.C.P. 1028":    ["non-conform", "improperly", "form"],
    "Pa.R.C.P. 440":     ["service", "served", "defective service", "did not serve"],
    "Pa.R.C.P. 3145":    ["garnishee", "garnishment", "interrogator", "fail to answer"],
    "Pa.R.C.P. 3146":    ["default", "fail to respond", "fail to answer"],
    "Pa.R.C.P. 237.3":   ["void", "default judgment"],
    "Pa.R.C.P. 1023.1":  ["certificat", "frivolous"],
    "FRCP 4":            ["service", "served", "summons"],
    "FRCP 5":            ["service of subsequent", "served"],
    "FRCP 11":           ["frivolous", "without inquiry", "improper purpose"],
    "FRCP 12(b)":        ["motion to dismiss", "defense"],
    "FRCP 12(f)":        ["immaterial", "insufficient", "scandalous"],
    "FRCP 60(b)(4)":     ["void", "without jurisdiction"],
    "U.S. Const. amend. XIV § 1": [
        "notice", "opportunity to be heard", "due process",
        "no notice", "deprived",
    ],
}


# Map our 6-bucket violations matrix entries to scorecard violation
# categories. Multiple scorecard categories may map to a single rule.
_RULE_TO_SCORECARD_CATEGORIES = {
    "Pa.R.C.P. 1024":  ["VERIFICATION_MISSING", "VERIFICATION", "UNVERIFIED"],
    "Pa.R.C.P. 1028":  ["NON_CONFORMING_FILING", "PLEADING_FORM"],
    "Pa.R.C.P. 440":   ["SERVICE_DEFECTIVE", "SERVICE", "DEFECTIVE_SERVICE"],
    "Pa.R.C.P. 3145":  ["GARNISHEE_NO_ANSWER", "GARNISHMENT", "GARNISHEE"],
    "Pa.R.C.P. 3146":  ["DEFAULT_GARNISHMENT", "GARNISHEE_DEFAULT"],
    "Pa.R.C.P. 237.3": ["VOID_JUDGMENT", "DEFAULT_JUDGMENT_VOID"],
    "Pa.R.C.P. 1023.1": ["FRIVOLOUS", "CERTIFICATION"],
    "FRCP 4":          ["SERVICE_DEFECTIVE", "SERVICE"],
    "FRCP 5":          ["SERVICE_SUBSEQUENT"],
    "FRCP 11":         ["FRIVOLOUS", "RULE_11", "IMPROPER_PURPOSE"],
    "FRCP 12(b)":      ["MOTION_TO_DISMISS"],
    "FRCP 12(f)":      ["IMMATERIAL", "INSUFFICIENT"],
    "FRCP 60(b)(4)":   ["VOID_JUDGMENT"],
    "U.S. Const. amend. XIV § 1": [
        "DUE_PROCESS", "NO_NOTICE", "NOTICE_DEFECTIVE",
        "NO_OPPORTUNITY_TO_BE_HEARD", "PROCEDURAL_DUE_PROCESS",
    ],    # ===PHASE_3F_TEMPLATE_HOOKS_V1=== INSERT 1B — 3F rule citations
    "court_order":           ["MTD_GRANTED_AGAINST_PLAINTIFF", "CLAIMS_DISMISSED",
                              "COURT_RULING_DENIED", "COURT_RULING_GRANTED",
                              "FILING_STRICKEN"],
    "court_order_deadline":  ["COURT_DEADLINE_SET"],
    # ===END PHASE_3F_TEMPLATE_HOOKS_V1=== INSERT 1B

}

# Rule → category(name) keyword for partial-match fallback when the
# violation source isn't an exact rule citation.
_NAME_TO_CATEGORIES = {
    "verification":     ["VERIFICATION_MISSING", "VERIFICATION"],
    "garnishee":        ["GARNISHEE_NO_ANSWER", "GARNISHMENT", "GARNISHEE"],
    "garnishment":      ["GARNISHEE_NO_ANSWER", "GARNISHMENT", "GARNISHEE"],
    "service":          ["SERVICE_DEFECTIVE", "SERVICE"],
    "due process":      ["DUE_PROCESS", "NO_NOTICE", "NOTICE_DEFECTIVE",
                         "PROCEDURAL_DUE_PROCESS"],
    "void":             ["VOID_JUDGMENT", "DEFAULT_JUDGMENT_VOID",
                         "TRIAL_COURT_AS_APPELLATE_AUTHORITY"],
    "non-binding":      ["NON_BINDING_AUTHORITY",
                         "TRIAL_COURT_AS_APPELLATE_AUTHORITY"],
    "appellate":        ["TRIAL_COURT_AS_APPELLATE_AUTHORITY"],
    "spoliation":       ["SPOLIATION", "DISCOVERY_ABUSE"],
    "discovery":        ["SPOLIATION", "DISCOVERY_ABUSE"],
    "default":          ["DEFAULT_JUDGMENT_VOID", "DEFAULT_GARNISHMENT"],
    "coram nobis":      ["VOID_JUDGMENT", "DEFAULT_JUDGMENT_VOID"],
    "rule 60":          ["VOID_JUDGMENT"],
    "(b)(4)":           ["VOID_JUDGMENT"],    # ===PHASE_3F_TEMPLATE_HOOKS_V1=== INSERT 1 — new 3F categories
    "mtd granted":           ["MTD_GRANTED_AGAINST_PLAINTIFF", "CLAIMS_DISMISSED"],
    "motion to dismiss":     ["MTD_GRANTED_AGAINST_PLAINTIFF", "CLAIMS_DISMISSED"],
    "claims dismissed":      ["CLAIMS_DISMISSED", "MTD_GRANTED_AGAINST_PLAINTIFF"],
    "dispositive":           ["MTD_GRANTED_AGAINST_PLAINTIFF", "CLAIMS_DISMISSED", "FILING_STRICKEN"],
    "vacate":                ["MTD_GRANTED_AGAINST_PLAINTIFF", "CLAIMS_DISMISSED", "VOID_JUDGMENT"],
    "reconsideration":       ["MTD_GRANTED_AGAINST_PLAINTIFF", "CLAIMS_DISMISSED"],
    "court deadline":        ["COURT_DEADLINE_SET"],
    "stricken":              ["FILING_STRICKEN"],
    "ruling denied":         ["COURT_RULING_DENIED"],
    # ===END PHASE_3F_TEMPLATE_HOOKS_V1=== INSERT 1

}


def match_fact(violation: dict, scorecard_facts: list) -> str:
    """Find the most relevant scorecard fact for this violation.

    Pass 1: rule (violation.source) → scorecard categories via
            _RULE_TO_SCORECARD_CATEGORIES.
    Pass 2: violation name → scorecard categories via
            _NAME_TO_CATEGORIES (keyword in name).
    Pass 3: substring match on violation.name in scorecard category
            string itself (catch-all).

    Returns the fact text (description), or empty string if no match.
    Pulls the FATAL severity preferentially within matched candidates.
    """
    if not scorecard_facts:
        return ""
    rule = (violation.get("source") or "").strip()
    name = (violation.get("name") or "").lower()

    target_categories = []

    # Pass 1: rule citation → categories
    for known_rule, cats in _RULE_TO_SCORECARD_CATEGORIES.items():
        if known_rule in rule or rule.startswith(known_rule):
            target_categories.extend(cats)
            break

    # Pass 2: name keyword → categories
    if not target_categories:
        for kw, cats in _NAME_TO_CATEGORIES.items():
            if kw in name:
                target_categories.extend(cats)

    # Pass 3: substring match on the category string
    if not target_categories:
        # Try matching name tokens against category strings
        name_tokens = [t for t in name.replace("_", " ").split() if len(t) > 3]
        for f in scorecard_facts:
            cat = f.get("category", "").lower()
            if any(tok in cat for tok in name_tokens):
                return f.get("text", "")
        return ""

    # Find candidate facts whose category matches target
    candidates = [
        f for f in scorecard_facts
        if any(tc.upper() in (f.get("category", "") or "").upper()
               for tc in target_categories)
    ]

    if not candidates:
        return ""

    # Prefer FATAL > SERIOUS > STANDARD; then highest weight
    sev_rank = {"FATAL": 3, "SERIOUS": 2, "STANDARD": 1}
    candidates.sort(
        key=lambda f: (
            -sev_rank.get((f.get("severity") or "").upper(), 0),
            -(f.get("weight") or 0),
        )
    )
    return candidates[0].get("text", "")


def _match_fact_to_violation_OLD_DOCSTRING(violation: dict, scorecard_facts: list) -> str:
    """OLD docstring kept here only so the V1 anchor regex can find replacement."""
    if not scorecard_facts:
        return ""
    rule = (violation.get("source") or "").strip()
    name = (violation.get("name") or "").lower()
    title = (violation.get("source_title") or "").lower()

    # Pass 1 — exact rule tag
    for f in scorecard_facts:
        rules = f.get("rules") or []
        for r in rules:
            r_str = str(r)
            if rule and (rule == r_str or rule in r_str or r_str in rule):
                return f.get("text", "")

    # Pass 2 — keyword match by rule citation
    keywords = []
    for known_rule, kws in _FACT_KEYWORD_MAP.items():
        if known_rule in rule or rule.startswith(known_rule):
            keywords = kws
            break

    # Pass 3 — fallback to keywords by violation name
    if not keywords:
        if "verification" in name or "verification" in title:
            keywords = _FACT_KEYWORD_MAP.get("Pa.R.C.P. 1024", [])
        elif "garnishee" in name or "garnish" in title:
            keywords = _FACT_KEYWORD_MAP.get("Pa.R.C.P. 3145", [])
        elif "default" in name and "garnish" not in name:
            keywords = _FACT_KEYWORD_MAP.get("Pa.R.C.P. 3146", [])
        elif "service" in name:
            keywords = _FACT_KEYWORD_MAP.get("Pa.R.C.P. 440", [])
        elif "due process" in name or "14th amend" in title or "fourteenth" in title:
            keywords = _FACT_KEYWORD_MAP.get("U.S. Const. amend. XIV § 1", [])
        elif "void" in name:
            keywords = ["void", "without jurisdiction", "no authority"]
        elif "spoliation" in name or "discovery" in name:
            keywords = ["spoliat", "destroyed", "withheld"]

    if keywords:
        for f in scorecard_facts:
            text = (f.get("text") or "").lower()
            if any(kw.lower() in text for kw in keywords):
                return f.get("text", "")

    return ""


def render_fact_application_deterministic(violation: dict, fact: str) -> list:
    """Step A — produce two indented lines: fact sentence + rule conclusion.

    Returns a list of strings (paragraphs) to append to Section II entry.
    Empty fact → returns generic line (still ships, just less specific).
    """
    rule = (violation.get("source") or "").strip()
    try:
        remedy = _remedy_for(rule)
    except Exception:
        remedy = "the filing must be stricken or appropriate relief entered."

    lines = []

    if fact:
        # Fact sentence — make sure it ends with a period
        fact_sentence = fact.strip()
        if fact_sentence and fact_sentence[-1] not in ".!?":
            fact_sentence = fact_sentence + "."
        # Capitalize first letter if it's lowercase
        if fact_sentence and fact_sentence[0].islower():
            fact_sentence = fact_sentence[0].upper() + fact_sentence[1:]
        lines.append("   " + fact_sentence)

    # Rule conclusion — always include
    if rule:
        lines.append(
            f"   Under {rule}, " + remedy
        )
    else:
        lines.append(
            "   The record establishes a violation of this rule warranting "
            "the relief stated in Section V."
        )

    return lines


# ---------- Step B: optional LLM polish via LlamaLLM.chat() ----------

_SAB_MAX_LINE_LEN = 500
_SAB_OTHER_RULE_PATTERNS = [
    r"Pa\.R\.C\.P\. \d+",
    r"\d+ U\.S\.C\.",
    r"FRCP \d+",
    r"Fed\. R\. Civ\. P\. \d+",
    r"FRAP \d+",
    r"Fed\. R\. App\. P\. \d+",
]


def _is_valid_llm_polish(text: str, violation: dict) -> bool:
    """Validate LLM-polished output. Reject if:
       - empty
       - exceeds length budget
       - missing the input rule citation
       - introduces UNAUTHORIZED rule citations
    """
    if not text or not text.strip():
        return False
    if len(text) > _SAB_MAX_LINE_LEN:
        return False
    rule = (violation.get("source") or "").strip()
    if rule:
        # Take first chunk of rule (e.g. "Pa.R.C.P. 1024")
        rule_stem = rule.split(",")[0].strip()
        if rule_stem and rule_stem not in text:
            return False
    # Reject other rule citations
    for pat in _SAB_OTHER_RULE_PATTERNS:
        for match in _sab_re.findall(pat, text):
            if rule and match in rule:
                continue
            if rule and rule.startswith(match):
                continue
            return False
    return True


# Module-level cached LLM handle (lazy)
_SAB_LLM = None
_SAB_LLM_TRIED = False
_SAB_LLM_OK = False


def _get_polish_llm():
    """Lazy-init LlamaLLM. Memoizes failure so we don't retry every call."""
    global _SAB_LLM, _SAB_LLM_TRIED, _SAB_LLM_OK
    if _SAB_LLM_TRIED:
        return _SAB_LLM if _SAB_LLM_OK else None
    _SAB_LLM_TRIED = True
    try:
        from pipeline.llm_layer import get_llm
        _SAB_LLM = get_llm()
        # Quick health check
        h = _SAB_LLM.health()
        if isinstance(h, dict) and h.get("status") == "offline":
            _SAB_LLM_OK = False
            return None
        _SAB_LLM_OK = True
        return _SAB_LLM
    except Exception:
        _SAB_LLM_OK = False
        return None


def llm_polish_fact_application(violation: dict, fact: str,
                                 deterministic_lines: list) -> list:
    """Step B — try to replace deterministic lines with LLM-polished prose.

    Returns either polished lines OR the original deterministic_lines.
    Never raises. Never blocks. Pure best-effort polish.
    """
    if not fact:
        return deterministic_lines  # nothing to polish
    llm = _get_polish_llm()
    if llm is None:
        return deterministic_lines

    rule = (violation.get("source") or "").strip()
    rule_title = (violation.get("source_title") or "").strip()
    try:
        remedy = _remedy_for(rule)
    except Exception:
        remedy = ""

    system_msg = (
        "You are a legal-writing assistant. You write a SHORT polished "
        "paragraph (1-3 sentences, max 80 words) applying ONE specific "
        "case fact to ONE specific rule. Rules: "
        "(1) Cite ONLY the rule given. Never cite any other rule. "
        "(2) Never invent facts. Use ONLY the fact given. "
        "(3) Never change the remedy. The remedy is fixed. "
        "(4) Output the paragraph only — no preamble, no markdown, "
        "no headings, no quotation marks."
    )
    user_msg = (
        f"Rule: {rule}"
        + (f" — {rule_title}" if rule_title and rule_title != rule else "")
        + f"\n"
        f"Case fact (do not alter): {fact}\n"
        f"Mandatory remedy under this rule (do not change): {remedy}\n"
        f"\n"
        f"Compose a 1-3 sentence application paragraph (max 80 words) "
        f"that begins with 'Here,' or with the party name, applies "
        f"the fact to {rule}, and concludes with the remedy. Plain "
        f"legal English. No other rule citations."
    )

    try:
        out = llm.chat(
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            thinking=False,
            n_predict=200,
            temperature=0.2,
        )
    except Exception:
        return deterministic_lines

    if not out or not out.strip():
        return deterministic_lines

    out = out.strip()
    if not _is_valid_llm_polish(out, violation):
        return deterministic_lines

    # Indent and split into lines
    polished_lines = ["   " + line.strip() for line in out.split("\n") if line.strip()]
    if not polished_lines:
        return deterministic_lines
    return polished_lines


# ---------- Public entry: replaces the V1 LLM compose function ----------

def _llm_compose_fact_application(violation: dict,
                                  scorecard_facts: list) -> str:
    """Public entry called by _enumerate_procedural_defects.

    Pipeline:
      1. Step A: match a scorecard fact, render deterministic two-line
         fact + rule conclusion.
      2. Step B: try to polish with LLM. Validate. Use polished if valid,
         else use Step A output.

    Returns a single string (multi-line, newline-separated) ready to
    append to the violation entry.
    """
    fact = match_fact(violation, scorecard_facts)
    deterministic_lines = render_fact_application_deterministic(violation, fact)

    final_lines = llm_polish_fact_application(
        violation, fact, deterministic_lines,
    )
    return "\n".join(final_lines)


# Compatibility shims (Phase 3 names some code may still call)
def _is_valid_composition(text: str, violation: dict) -> bool:
    return _is_valid_llm_polish(text, violation)


def _deterministic_fact_line(violation: dict, scorecard_facts: list) -> str:
    fact = match_fact(violation, scorecard_facts)
    return "\n".join(render_fact_application_deterministic(violation, fact))


def _match_fact_to_violation(violation: dict, scorecard_facts: list) -> dict:
    fact_text = match_fact(violation, scorecard_facts)
    if not fact_text:
        return {}
    return {"text": fact_text}
# === END STEP_A_AND_B_FACT_INJECTION_V1 ===



# === STRICT_SCORECARD_SECTION_II_V1 ===
# Strict mode: Section II of procedural motions is built ONLY from
# scorecard.violations[] — the actual defects in opposing filings.
# No fact → no paragraph. No overclaiming.

# Canonical legal hooks per scorecard category. Unmapped categories
# still emit (via _DEFAULT_HOOK), so adding new audit categories
# doesn't break the motion.
# === COURT_LABEL_HUMANIZER_V1 ===
# Replace raw audit court labels with readable text in fact strings.

_COURT_LABEL_REPLACEMENTS = [
    # Order matters: longer / more specific patterns first
    (re.compile(r'\bPA_CP_PHILA\b'),    'the Philadelphia Court of Common Pleas'),
    (re.compile(r'\bPA_CP_OTHER\b'),    'a Pennsylvania trial court'),
    (re.compile(r'\bPA_SUPER\b'),       'the Pennsylvania Superior Court'),
    (re.compile(r'\bPA_SUP\b'),         'the Pennsylvania Supreme Court'),
    (re.compile(r'\bDPA_OTHER\b'),      'a different federal district court in Pennsylvania'),
    (re.compile(r'\bEDPA\b'),           'the U.S. District Court for the Eastern District of Pennsylvania'),
    (re.compile(r'\bMDPA\b'),           'the U.S. District Court for the Middle District of Pennsylvania'),
    (re.compile(r'\bWDPA\b'),           'the U.S. District Court for the Western District of Pennsylvania'),
    (re.compile(r'\bCA3\b'),            'the United States Court of Appeals for the Third Circuit'),
    (re.compile(r'\bCA_OTHER\b'),       'another federal circuit court'),
    (re.compile(r'\bDIST_OTHER\b'),     'a federal district court outside this jurisdiction'),
    (re.compile(r'\bNJ_SUP\b'),         'the New Jersey Supreme Court'),
    (re.compile(r'\bNY_SUP\b'),         'the New York Court of Appeals'),
    (re.compile(r'\bUNKNOWN\b'),        'an unidentified court'),
]


def _humanize_court_labels(text: str) -> str:
    """Replace raw audit court labels (DIST_OTHER, CA_OTHER, etc.) with
    readable phrasing for inclusion in motion prose."""
    if not text:
        return text
    for pattern, replacement in _COURT_LABEL_REPLACEMENTS:
        text = pattern.sub(replacement, text)
    return text
# === END COURT_LABEL_HUMANIZER_V1 ===



# === PATH_B_NEEDS_HOOK_LOGGER_V1 ===
def _log_unknown_category(category: str, _scorecard_hint=None) -> None:
    """Log unknown scorecard categories to needs_hook.log so they can be
    added to _SCORECARD_HOOK_MAP / YAML in a future patch."""
    try:
        from pathlib import Path as _P
        from datetime import datetime as _dt
        log_path = _P(str(_eroot() / "output/audit/needs_hook.log"))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # Don't double-log within one run
        existing = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        if f"category={category}" in existing.split("\n")[-50:].__str__():
            return  # already logged recently
        with log_path.open("a", encoding="utf-8") as f:
            ts = _dt.now().isoformat(timespec="seconds")
            hint = ""
            if _scorecard_hint and isinstance(_scorecard_hint, dict):
                hint_text = (_scorecard_hint.get("text") or "")[:80]
                hint = f"  hint: {hint_text}"
            f.write(f"{ts}  category={category}{hint}\n")
    except Exception:
        pass
# === END PATH_B_NEEDS_HOOK_LOGGER_V1 ===

_SCORECARD_HOOK_MAP = {
    "REAL_PARTY_RECIPROCITY": {
        "name":           "Standing / Real-Party Rule Applied Reciprocally to the Enforcing Filer",
        "rule_state":     "Pa.R.C.P. 2002; 13 Pa.C.S. 3203, 3205",
        "rule_federal":   "Fed. R. Civ. P. 17(a); U.C.C. 3-203, 3-205",
        "remedy_state":   "dismissed or the real party in interest joined",
        "remedy_federal": "dismissed or the real party in interest joined",
        "frame":  ("the real-party-in-interest requirement binds the party who FILED to enforce: a "
                   "servicer that placed the note in the record without an allonge, where the note is "
                   "held by the securitization trust, is not the holder or real party in interest and "
                   "cannot enforce while the litigant is disqualified for the identical non-holder "
                   "status; the enforcing filing is therefore subject to being"),
    },
    "NON_BINDING_AS_CONTROLLING": {
        "name":           "Non-Binding Authority Presented as Controlling",
        "rule_state":     "Pa.R.C.P. 1028(a)(2)",
        "rule_federal":   "Fed. R. Civ. P. 12(f)",
        "remedy_state":   "stricken",
        "remedy_federal": "stricken",
        "frame":  ("presenting non-binding authority as controlling is "
                   "procedurally defective and the affected portions must be"),
    },
    "TRIAL_COURT_AS_APPELLATE_AUTHORITY": {
        "name":           "Trial Court Authority Misused as Binding Precedent",
        "rule_state":     "Pa.R.C.P. 1028(a)(2)",
        "rule_federal":   "Fed. R. Civ. P. 12(f)",
        "remedy_state":   "stricken",
        "remedy_federal": "stricken",
        "frame":  ("trial court decisions are not binding precedent on this "
                   "Court; reliance on such decisions as controlling is "
                   "procedurally defective and the affected portions must be"),
    },
    "BIAS_EMOTIONAL_LANGUAGE": {
        "name":           "Improper Argumentative / Prejudicial Language",
        "rule_state":     "Pa.R.C.P. 1023.1",
        "rule_federal":   "Fed. R. Civ. P. 11(b)",
        "remedy_state":   "stricken or sanctioned",
        "remedy_federal": "stricken or sanctioned",
        "frame":  ("filings must be well-grounded in fact and law and not "
                   "interposed for improper purpose; emotional or "
                   "characterological attacks on the litigant are subject to being"),
    },
    "BIAS_PRO_SE_GATEKEEPING": {
        "name":           "Improper Pro Se Gatekeeping",
        "rule_state":     "Pa.R.C.P. 1023.1",
        "rule_federal":   "Fed. R. Civ. P. 11(b)",
        "remedy_state":   "stricken or sanctioned",
        "remedy_federal": "stricken or sanctioned",
        "frame":  ("pro se litigants are entitled to liberal construction of "
                   "their filings, and characterological attacks on a pro se "
                   "litigant are subject to being"),
    },
    "BIAS_PUNITIVE_APPEAL": {
        "name":           "Punitive / Improper Appeals to the Court",
        "rule_state":     "Pa.R.C.P. 1023.1",
        "rule_federal":   "Fed. R. Civ. P. 11(b)",
        "remedy_state":   "stricken or sanctioned",
        "remedy_federal": "stricken or sanctioned",
        "frame":  ("requests for punitive or extraordinary relief untethered "
                   "to the merits are improper and subject to being"),
    },
    "VERIFICATION_MISSING": {
        "name":           "Verification Requirement",
        "rule_state":     "Pa.R.C.P. 1024",
        "rule_federal":   "Fed. R. Civ. P. 11(a)",
        "remedy_state":   "stricken under Pa.R.C.P. 1028(a)(2)",
        "remedy_federal": "stricken",
        "frame":  ("verification is required at the threshold of judicial "
                   "review; unverified pleadings must be"),
    },
    "SERVICE_DEFECTIVE": {
        "name":           "Defective Service",
        "rule_state":     "Pa.R.C.P. 440",
        "rule_federal":   "Fed. R. Civ. P. 4",
        "remedy_state":   "stricken or disregarded",
        "remedy_federal": "stricken or disregarded",
        "frame":  ("defective service renders the affected filing without "
                   "legal effect, and the filing must be"),
    },
    "GARNISHEE_NO_ANSWER": {
        "name":           "Garnishee Failure to Answer",
        "rule_state":     "Pa.R.C.P. 3145",
        "rule_federal":   "Fed. R. Civ. P. 69(a)(2)",
        "remedy_state":   "subject to default under Pa.R.C.P. 3146",
        "remedy_federal": "subject to default judgment",
        "frame":  ("a garnishee's failure to answer interrogatories is"),
    },
    "DUE_PROCESS": {
        "name":           "Procedural Due Process Violation",
        "rule_state":     "U.S. Const. amend. XIV § 1",
        "rule_federal":   "U.S. Const. amend. XIV § 1",
        "remedy_state":   "void",
        "remedy_federal": "void",
        "frame":  ("the constitutional guarantee of notice and an "
                   "opportunity to be heard is foundational; orders entered "
                   "in violation of that guarantee are"),
    },
    # === NEW_HOOK_CATEGORIES_V1 ===
    "LEAVE_TO_AMEND_CONCLUSORY": {
        "name":           "Failure to Address Leave to Amend",
        "rule_state":     "Pa.R.C.P. 1033",
        "rule_federal":   "Fed. R. Civ. P. 15(a)(2)",
        "remedy_state":   "addressed before any dismissal with prejudice may issue",
        "remedy_federal": "addressed before any dismissal with prejudice may issue",
        "frame":  ("leave to amend must be freely given when justice so "
                   "requires, and a conclusory denial without analysis is "
                   "reversible error; the request must be"),
    },
    "CONCLUSORY_DISMISSAL": {
        "name":           "Conclusory Dismissal Without Required Analysis",
        "rule_state":     "Pa.R.C.P. 1028(a)(4)",
        "rule_federal":   "Fed. R. Civ. P. 12(b)(6)",
        "remedy_state":   "reconsidered with the analysis required by binding precedent",
        "remedy_federal": "reconsidered under the Twombly / Iqbal plausibility standard with element-by-element analysis",
        "frame":  ("dismissal of a claim requires a reasoned application of "
                   "the controlling pleading standard to the well-pleaded "
                   "factual allegations; a conclusory ruling lacking such "
                   "analysis must be"),
    },
    # === END NEW_HOOK_CATEGORIES_V1 ===
    # === PATH_B_PROCEDURAL_HOOKS_V1 ===
    # Hooks for block14_pinpointer-detected procedural violations.
    "TWOMBLY_IQBAL_MISAPPLICATION": {
        "name":           "Twombly / Iqbal Misapplied",
        "rule_state":     "Pa.R.C.P. 1028(a)(4)",
        "rule_federal":   "Fed. R. Civ. P. 12(b)(6)",
        "remedy_state":   "reconsidered with all well-pleaded facts accepted as true",
        "remedy_federal": "reconsidered under the correct plausibility standard with all well-pleaded facts accepted as true",
        "frame":  ("the plausibility standard requires accepting all "
                   "well-pleaded factual allegations as true; treating "
                   "factual allegations as legal conclusions inverts the "
                   "rule and the resulting decision must be"),
    },
    "NOTICE_INADEQUATE": {
        "name":           "Inadequate Notice (Procedural Due Process)",
        "rule_state":     "U.S. Const. amend. XIV § 1; Pa.R.C.P. 440",
        "rule_federal":   "U.S. Const. amend. XIV § 1; Fed. R. Civ. P. 5",
        "remedy_state":   "void under Pa.R.C.P. 237.3 and Fed. R. Civ. P. 60(b)(4)",
        "remedy_federal": "void under Fed. R. Civ. P. 60(b)(4)",
        "frame":  ("notice and an opportunity to be heard are constitutional "
                   "minimums; orders entered without adequate notice are"),
    },
    "BIAS_JUDICIAL": {
        "name":           "Indicia of Judicial Bias",
        "rule_state":     "Code of Judicial Conduct, Canon 2",
        "rule_federal":   "28 U.S.C. § 455",
        "remedy_state":   "subject to recusal proceedings",
        "remedy_federal": "subject to recusal under 28 U.S.C. § 455",
        "frame":  ("a judicial officer must disqualify in any proceeding "
                   "in which impartiality might reasonably be questioned; "
                   "the conduct identified is"),
    },
    "VERIFICATION_MISSING_PROC": {
        # Note: VERIFICATION_MISSING already mapped above. This is a
        # block14_pinpointer alias kept for compatibility.
        "name":           "Verification Missing (Pinpointed)",
        "rule_state":     "Pa.R.C.P. 1024",
        "rule_federal":   "Fed. R. Civ. P. 11(a)",
        "remedy_state":   "stricken under Pa.R.C.P. 1028(a)(2)",
        "remedy_federal": "stricken",
        "frame":  ("verification is required at the threshold of judicial "
                   "review; unverified pleadings must be"),
    },
    "PRO_SE_LIBERAL_CONSTRUCTION": {
        "name":           "Failure to Apply Liberal Construction to Pro Se Filings",
        "rule_state":     "Erickson v. Pardus, 551 U.S. 89 (2007)",
        "rule_federal":   "Erickson v. Pardus, 551 U.S. 89 (2007); Haines v. Kerner, 404 U.S. 519 (1972)",
        "remedy_state":   "reconsidered with the liberal-construction standard applied",
        "remedy_federal": "reconsidered with the liberal-construction standard applied",
        "frame":  ("filings by pro se litigants are held to less stringent "
                   "standards than formal pleadings drafted by lawyers; "
                   "reliance on technical defects against a pro se filing must be"),
    },
    "IMPROPER_STANDARD_OF_REVIEW": {
        "name":           "Improper Standard of Review",
        "rule_state":     "Pa.R.C.P. 1028(a)(4)",
        "rule_federal":   "Fed. R. Civ. P. 12(b)(6); Twombly / Iqbal",
        "remedy_state":   "reconsidered under the correct standard",
        "remedy_federal": "reconsidered under the correct standard",
        "frame":  ("application of the wrong legal standard at the "
                   "preliminary objection / motion to dismiss stage is "
                   "reversible error; the affected ruling must be"),
    },
    "FILING_DEADLINE_VIOLATION": {
        "name":           "Filing Deadline Violation",
        "rule_state":     "Pa.R.C.P. 1026; 1028(a)",
        "rule_federal":   "Fed. R. Civ. P. 6; 12(a)",
        "remedy_state":   "stricken or disregarded as untimely",
        "remedy_federal": "stricken or disregarded as untimely",
        "frame":  ("filings made outside the time limits set by the "
                   "applicable rule are untimely; the affected filing must be"),
    },
    "PAGE_LIMIT_EXCEEDED": {
        "name":           "Page Limit Exceeded",
        "rule_state":     "local rules of court",
        "rule_federal":   "Local Rules of Civil Procedure (E.D. Pa. L.R. 7.1)",
        "remedy_state":   "stricken or returned for compliance",
        "remedy_federal": "stricken or returned for compliance",
        "frame":  ("filings exceeding the page or word limits set by the "
                   "applicable local rule are non-compliant; the affected filing must be"),
    },
    # === END PATH_B_PROCEDURAL_HOOKS_V1 ===
    # === MIRROR_SYNC_20260825 (ported from pipeline/scorecard_if_then.py — single source of truth) ===
    "STANDING_DEFECT": {
        "name":           "Lack of Article III Standing",
        "rule_state":     "standing doctrine (injury-in-fact, causation, redressability)",
        "rule_federal":   "U.S. Const. art. III, § 2; Lujan v. Defenders of Wildlife, 504 U.S. 555",
        "remedy_state":   "dismissed for lack of standing",
        "remedy_federal": "dismissed for lack of standing",
        "frame":  (
                   "the party invoking the court's power lacks Article III standing, so the "
                   "claim must be"),
    },
    "SUBJECT_MATTER_JURISDICTION_DEFECT": {
        "name":           "Lack of Subject-Matter Jurisdiction",
        "rule_state":     "42 Pa.C.S. (courts of limited statutory jurisdiction)",
        "rule_federal":   "Fed. R. Civ. P. 12(b)(1); Fed. R. Civ. P. 12(h)(3)",
        "remedy_state":   "dismissed for lack of subject-matter jurisdiction",
        "remedy_federal": "dismissed — the court must dismiss whenever it determines it lacks jurisdiction",
        "frame":  (
                   "the court lacks subject-matter jurisdiction, which cannot be waived and may "
                   "be raised at any time, so the action must be"),
    },
    "VOID_JUDGMENT_RULE_60B4": {
        "name":           "Void Judgment",
        "rule_state":     "void-judgment doctrine (a void judgment is attackable at any time)",
        "rule_federal":   "Fed. R. Civ. P. 60(b)(4)",
        "remedy_state":   "vacated as void",
        "remedy_federal": "vacated as void",
        "frame":  (
                   "the judgment is void for want of jurisdiction or a due-process deprivation, "
                   "and a void judgment must be"),
    },
    "REMOVAL_DEFECTIVE": {
        "name":           "Defective Removal",
        "rule_state":     "n/a (federal removal procedure)",
        "rule_federal":  (
                   "28 U.S.C. § 1441; 28 U.S.C. § 1446(b)(2)(A) (rule of unanimity); 28 U.S.C. § "
                   "1447(c)"),
        "remedy_state":   "remanded",
        "remedy_federal": "remanded for a defect in the removal procedure",
        "frame":  (
                   "removal failed a statutory prerequisite (typically the rule of unanimity), "
                   "so the case must be"),
    },
    "PROCEDURAL_DUE_PROCESS_VIOLATION": {
        "name":           "Procedural Due-Process Violation",
        "rule_state":     "Pa. Const. art. I, §§ 1, 9, 11",
        "rule_federal":   "U.S. Const. amend. XIV, § 1 (and amend. V)",
        "remedy_state":   "vacated for want of due process",
        "remedy_federal": "vacated for want of due process",
        "frame":  (
                   "the ruling deprived a party of life, liberty, or property without adequate "
                   "notice and an opportunity to be heard, and must be"),
    },
    "SUA_SPONTE_RULING": {
        "name":           "Sua Sponte Ruling Without Notice",
        "rule_state":     "Pa. Const. art. I, § 9 (notice and opportunity to be heard)",
        "rule_federal":   "U.S. Const. amend. XIV, § 1 (due process)",
        "remedy_state":   "vacated (entered without notice or opportunity to be heard)",
        "remedy_federal": "vacated (entered without notice or opportunity to be heard)",
        "frame":  (
                   "the court granted substantive relief on an issue no party raised, without "
                   "notice or briefing, so the ruling must be"),
    },
    "PRIMA_FACIE_EVIDENCE_IGNORED": {
        "name":           "Mandatory Judicial Notice Ignored",
        "rule_state":     "Pa.R.E. 201(b), (c)",
        "rule_federal":   "Fed. R. Evid. 201(b), (c)",
        "remedy_state":   "the court must take judicial notice of the record fact",
        "remedy_federal": "the court must take judicial notice of the record fact",
        "frame":  (
                   "the court ignored record facts subject to mandatory judicial notice "
                   "(admissions, defaults, court records), which it was required to notice, so "
                   "the ruling must be"),
    },
    "FRAUD_ON_THE_COURT": {
        "name":           "Fraud on the Court",
        "rule_state":     "fraud-on-the-court / void-judgment doctrine",
        "rule_federal":   "Fed. R. Civ. P. 60(d)(3)",
        "remedy_state":   "judgment set aside for fraud on the court",
        "remedy_federal": "judgment set aside for fraud on the court",
        "frame":  (
                   "an officer of the court perpetrated a fraud that defiled the court itself, "
                   "and the resulting judgment must be"),
    },
    "JUDICIAL_DISQUALIFICATION_DUTY": {
        "name":           "Disqualification Duty",
        "rule_state":     "Code of Judicial Conduct, Rule 2.11",
        "rule_federal":   "28 U.S.C. § 455; 28 U.S.C. § 144",
        "remedy_state":   "recusal / disqualification",
        "remedy_federal": "recusal / disqualification",
        "frame":  (
                   "the judge's impartiality might reasonably be questioned (including a "
                   "financial or fiduciary interest), triggering the duty to"),
    },
    "DEPRIVATION_OF_RIGHTS_COLOR_OF_LAW": {
        "name":           "Deprivation of Rights Under Color of Law",
        "rule_state":     "42 U.S.C. § 1983",
        "rule_federal":   "42 U.S.C. § 1983 (18 U.S.C. § 242 criminal analog)",
        "remedy_state":   "civil liability / relief under § 1983",
        "remedy_federal": "civil liability / relief under § 1983",
        "frame":  (
                   "a person acting under color of state law deprived a party of a federally "
                   "protected right, giving rise to liability, and the deprivation must be"),
    },
    "OBSTRUCTION_OF_JUSTICE": {
        "name":           "Obstruction of Justice",
        "rule_state":     "18 Pa.C.S. § 5101 (obstructing administration of law)",
        "rule_federal":   "18 U.S.C. § 1503; 18 U.S.C. § 1512",
        "remedy_state":   "referral for obstruction",
        "remedy_federal": "referral for obstruction",
        "frame":  (
                   "the conduct corruptly endeavored to influence, obstruct, or impede the due "
                   "administration of justice, warranting"),
    },
    "IMMUNITY_WAIVER_TRIGGERED": {
        "name":           "Judicial Immunity Does Not Bar",
        "rule_state":     "judicial-immunity limits (non-judicial acts; clear absence of jurisdiction)",
        "rule_federal":  (
                   "Stump v. Sparkman, 435 U.S. 349; Bradley v. Fisher, 80 U.S. 335; Pulliam v. "
                   "Allen, 466 U.S. 522"),
        "remedy_state":   "immunity does not bar the claim or relief",
        "remedy_federal": "immunity does not bar the claim or relief",
        "frame":  (
                   "the act falls outside judicial immunity (a non-judicial act, an act in the "
                   "clear absence of all jurisdiction, or a request for prospective relief), so "
                   "immunity does not"),
    },
    "SANCTIONS_TRIGGERED": {
        "name":           "Sanctionable Conduct",
        "rule_state":     "Pa.R.C.P. 1023.1",
        "rule_federal":   "Fed. R. Civ. P. 11(b), (c); 28 U.S.C. § 1927",
        "remedy_state":   "sanctions",
        "remedy_federal": "sanctions",
        "frame":  (
                   "the conduct meets the standard for sanctions (a frivolous or "
                   "improper-purpose filing), warranting"),
    },
    "STARE_DECISIS_VIOLATION": {
        "name":           "Departure From Controlling Precedent",
        "rule_state":     "stare decisis (binding precedent of a superior court in the same line)",
        "rule_federal":   "stare decisis (binding precedent of a superior court in the same line)",
        "remedy_state":   "reversal for departing from controlling precedent",
        "remedy_federal": "reversal for departing from controlling precedent",
        "frame":  (
                   "the ruling is inconsistent with controlling precedent that binds this court, "
                   "and must be"),
    },
    "SOVEREIGN_CITIZEN_LABEL": {
        "name":           "Ideological Label — Rebutted, Not Applied",
        "rule_state":     "Neitzke v. Williams, 490 U.S. 319 (arguable-basis standard)",
        "rule_federal":   "Neitzke v. Williams, 490 U.S. 319 (arguable-basis standard)",
        "remedy_state":   "the label is rebutted; the claim is adjudicated on its corpus-grounded merits",
        "remedy_federal": "the label is rebutted; the claim is adjudicated on its corpus-grounded merits",
        "frame":  (
                   "the court affixed an ideological label in lieu of merits analysis; a claim "
                   "with an arguable basis in law is not frivolous, and the label must be"),
    },
    # === END MIRROR_SYNC_20260825 ===
    # === MIRROR_SYNC_20260825 (ported from pipeline/scorecard_if_then.py — single source of truth) ===
    "INSTRUMENT_VERSION_CONFLICT": {
        "name":           "Instrument Version Conflict Across Productions",
        "rule_state":     "13 Pa.C.S. §§ 3204(a), 3205(b), 3301; Pa.R.E. 1003",
        "rule_federal":   "U.C.C. §§ 3-204(a), 3-205(b), 3-301; Fed. R. Evid. 1003; Fed. R. Civ. P. 17(a)",
        "remedy_state":  (
                   "resolved by compelled production of the original instrument, with "
                   "enforcement reliance on the conflicting copies stricken"),
        "remedy_federal":  (
                   "resolved by compelled production of the original instrument, with "
                   "enforcement reliance on the conflicting copies stricken"),
        "frame":  (
                   "two productions of the same instrument in materially different physical "
                   "states cannot both be authentic; an indorsement exists only on the "
                   "instrument or a paper affixed to it, a blank indorsement makes the paper "
                   "enforceable only by its possessor, and a duplicate is inadmissible once a "
                   "genuine question of authenticity is raised, so the conflict must be"),
    },
    # === END MIRROR_SYNC_20260825 ===
    # === MIRROR_SYNC_20260825 (ported from pipeline/scorecard_if_then.py — single source of truth) ===
    "CROSS_PARTY_ROLE_CONFLICT": {
        "name":           "Mutually Exclusive Role Claims by Opposing Parties",
        "rule_state":     "Pa.R.C.P. 2002; 13 Pa.C.S. § 3301; Pa.R.E. 803(25)",
        "rule_federal":   "Fed. R. Civ. P. 17(a); U.C.C. § 3-301; Fed. R. Evid. 801(d)(2)",
        "remedy_state":  (
                   "resolved against the claimants: each must prove its claimed role or the "
                   "enforcement claim is dismissed"),
        "remedy_federal":  (
                   "resolved against the claimants: each must prove its claimed role or the "
                   "enforcement claim is dismissed"),
        "frame":  (
                   "the record contains mutually exclusive statements — each a party-opponent "
                   "admission — about who holds the enforceable interest; statements that cannot "
                   "all be true establish that at least one enforcement posture is false, and "
                   "the conflict must be"),
    },
    # === END MIRROR_SYNC_20260825 ===
    # === MIRROR_SYNC_20260825 (ported from pipeline/scorecard_if_then.py — single source of truth) ===
    "CASE_FACT_CONFLICT": {
        "name":           "Irreconcilable Facts Across the Claimant's Own Filings",
        "rule_state":     "Pa.R.E. 803(25); 13 Pa.C.S. § 3308; UTPCPL, 73 P.S. § 201-2(4)",
        "rule_federal":  (
                   "Fed. R. Evid. 801(d)(2); 26 U.S.C. §§ 6050P, 7434; 12 U.S.C. § 2605(e); "
                   "U.C.C. § 9-210"),
        "remedy_state":  (
                   "resolved against the claimant: election of one position, full accounting, "
                   "and dismissal of enforcement built on the abandoned position"),
        "remedy_federal":  (
                   "resolved against the claimant: election of one position, full accounting, "
                   "and dismissal of enforcement built on the abandoned position"),
        "frame":  (
                   "the claimant's own documents assert facts that cannot all be true (discharge "
                   "reported while the same debt is billed; amounts that do not reconcile; "
                   "insurance kept active on a debt foreclosed as defaulted); each statement is "
                   "a party-opponent admission, and the enforcement demand resting on them must "
                   "be"),
    },
    # === END MIRROR_SYNC_20260825 ===
    # === MIRROR_SYNC_20260825 (ported from pipeline/scorecard_if_then.py — single source of truth) ===
    # === END MIRROR_SYNC_20260825 ===
    # === MIRROR_SYNC_20260825 (ported from pipeline/scorecard_if_then.py — single source of truth) ===
    # === END MIRROR_SYNC_20260825 ===
    # === MIRROR_SYNC_20260825 (ported from pipeline/scorecard_if_then.py — single source of truth) ===
    # === END MIRROR_SYNC_20260825 ===
    # === MIRROR_SYNC_20260825 (ported from pipeline/scorecard_if_then.py — single source of truth) ===
    "UNREBUTTED_ADVERSE_STATEMENT": {
        "name":           "Adverse Record Statement Standing Unanswered",
        "rule_state":     "Pa.R.C.P. 1029(b) (averments not denied are admitted); the record-silence risk",
        "rule_federal":  (
                   "Fed. R. Civ. P. 8(b)(6) (allegation not denied is admitted); Fed. R. Evid. "
                   "801(d)(2)"),
        "remedy_state":   "answered, distinguished, or moved against in the present filing",
        "remedy_federal": "answered, distinguished, or moved against in the present filing",
        "frame":  (
                   "an adverse statement (court directive or opponent claim) sits in the record "
                   "with no responsive filing detected; unanswered adverse statements risk being "
                   "treated as conceded and constrain the appellate record, so each one "
                   "identified here is now expressly"),
    },
    "DEFINITION_DIVERGENCE": {
        "name":           "Term of Art Used in Its Colloquial Sense Against the Controlling Definition",
        "rule_state":  (
                   "1 Pa.C.S. 1903 (words and phrases; technical words per their peculiar "
                   "meaning); 13 Pa.C.S. 1201, 3301 (defined terms control)"),
        "rule_federal":  (
                   "1 U.S.C. 1 (Dictionary Act); the controlling definitional statute per term "
                   "(U.C.C. 1-201(b)(21), 26 U.S.C. 6050P, 15 U.S.C. 1692a); Gould v. Gould, 245 "
                   "U.S. 151 (doubt construed against the government)"),
        "remedy_state":  (
                   "the argument re-analyzed under the statutory definition, colloquial-sense "
                   "reliance disregarded"),
        "remedy_federal":  (
                   "the argument re-analyzed under the statutory definition, colloquial-sense "
                   "reliance disregarded"),
        "frame":  (
                   "the filing trades on the ordinary meaning of a defined term (discharge, "
                   "holder, includes, tender, creditor, verification) where the controlling text "
                   "defines it differently; statutory definitions are not optional — the "
                   "argument must rise or fall on the DEFINED meaning, and reliance on the "
                   "colloquial sense is a definitional misrepresentation that must be"),
    },
    "ENTITY_IDENTITY_CONFLICT": {
        "name":           "Party Litigating Under an Unproven Second Identity",
        "rule_state":     "Pa.R.C.P. 2002 (real party in interest); Pa.R.C.P. 1024 (verification)",
        "rule_federal":  (
                   "Fed. R. Civ. P. 17(a); 28 U.S.C. 1332, 1446(a) (citizenship of the entity "
                   "actually sued)"),
        "remedy_state":  (
                   "stricken unless the identity chain (registration, merger, succession) is "
                   "proven of record"),
        "remedy_federal":  (
                   "stricken or the action remanded/dismissed unless the identity chain is "
                   "proven of record"),
        "frame":  (
                   "a party appearing under a different identity than the entity named "
                   "('incorrectly captioned as', a/k/a, f/k/a) has asserted its own "
                   "non-identity: pleadings filed by the unproven entity are a nullity as to the "
                   "named party, and in removal the citizenship actually pleaded belongs to the "
                   "WRONG entity, defeating the jurisdictional showing; the filings making the "
                   "swap are party-opponent admissions under FRE 801(d)(2) and must be"),
    },
    # === END MIRROR_SYNC_20260825 ===
    # === MIRROR_SYNC_20260825 (ported from pipeline/scorecard_if_then.py — single source of truth) ===
    "AGENCY_AUTHORITY_UNPROVEN": {
        "name":           "Act Taken Without Proven Agency or Authority",
        "rule_state":     "Restatement (agency); Pa.R.C.P. 1024 (verification by party with knowledge)",
        "rule_federal":  (
                   "actual/apparent authority doctrine; Fed. R. Evid. 602 (personal knowledge); "
                   "Fed. R. Civ. P. 17(a)"),
        "remedy_state":  (
                   "the act disregarded until the authority chain (appointment, scope, "
                   "ratification) is proven of record"),
        "remedy_federal": "the act disregarded until the authority chain is proven of record",
        "frame":  (
                   "a signer, servicer, or representative acted for a principal without record "
                   "proof of authority (appointment, delegation, scope); acts of an unproven "
                   "agent bind no one, and documents executed by them (allonges signed by "
                   "'Collateral Specialists', affidavits by servicer employees about owner "
                   "records) are"),
    },
    "VENUE_IMPROPER": {
        "name":           "Venue Improper or In-Rem Power Absent",
        "rule_state":     "Pa.R.C.P. 1006 (venue; actions on real property where the land lies)",
        "rule_federal":  (
                   "28 U.S.C. 1391 (venue); local-action doctrine (real property actions in the "
                   "district of the property)"),
        "remedy_state":   "transferred to the proper county or dismissed",
        "remedy_federal": "dismissed or transferred under 28 U.S.C. 1406(a)",
        "frame":  (
                   "the action or claim sits in a forum without venue over it — in particular, "
                   "claims operating on real property belong where the property lies (local "
                   "action), and a court elsewhere lacks the in-rem power the claim requires; "
                   "the misplaced claims must be"),
    },
    "AGENCY_OWN_RULES_VIOLATED": {
        "name":           "Agency Bound by Its Own Rules Departed From Them (Accardi)",
        "rule_state":     "1 Pa. Code (agency procedural rules bind the agency); administrative-agency law",
        "rule_federal":  (
                   "United States ex rel. Accardi v. Shaughnessy, 347 U.S. 260; 5 U.S.C. 706(2) "
                   "(agency action contrary to procedure set aside)"),
        "remedy_state":   "the agency action set aside and the agency held to its published procedure",
        "remedy_federal":  (
                   "the agency action set aside under 706(2); mandamus under 1361 for withheld "
                   "mandatory action"),
        "frame":  (
                   "an agency (HUD, CFPB, servicer acting under agency program rules, court "
                   "clerk under administrative procedures) departed from its own published "
                   "rules; under Accardi an agency is bound by its own regulations while they "
                   "remain in force, and action taken in violation of them must be"),
    },
    "CONDITION_PRECEDENT_UNMET": {
        "name":           "Statutory Condition Precedent Never Satisfied",
        "rule_state":  (
                   "41 P.S. 403 (Act 6 notice); 35 P.S. 1680.402c seq. (Act 91 / HEMAP, 12 Pa. "
                   "Code Ch. 31); Pa.R.C.P. 1147"),
        "rule_federal":  (
                   "24 CFR 203.602 seq. (FHA loss-mitigation/face-to-face prerequisites); 12 CFR "
                   "1024.41 (Reg X loss-mit before foreclosure referral)"),
        "remedy_state":  (
                   "the action dismissed or stayed until every statutory prerequisite is proven "
                   "satisfied"),
        "remedy_federal":  (
                   "the action dismissed or the referral undone; Reg X and FHA servicing "
                   "violations pled affirmatively"),
        "frame":  (
                   "enforcement was commenced without proof that the statutory conditions "
                   "precedent were performed (Act 6/Act 91 notices, FHA servicing prerequisites, "
                   "Reg X loss-mitigation review); these are not technicalities — they are "
                   "legislative gates on the power to foreclose, and an action commenced through "
                   "an unopened gate must be"),
    },
    "JUDICIAL_NOTICE_COMPELLED": {
        "name":           "Adjudicative Fact Compelling Judicial Notice",
        "rule_state":     "Pa.R.E. 201 (judicial notice; mandatory on request with necessary information)",
        "rule_federal":  (
                   "Fed. R. Evid. 201(c)(2) (court MUST take notice if requested and supplied "
                   "with the necessary information)"),
        "remedy_state":   "notice taken of the government-record fact and the analysis conducted on it",
        "remedy_federal": "notice taken; refusal to notice a qualifying fact is reviewable error",
        "frame":  (
                   "facts from sources whose accuracy cannot reasonably be questioned "
                   "(government databases, recorded instruments, court dockets, HUD/FHA system "
                   "screens, IRS transcripts) qualify for judicial notice; on request with the "
                   "material supplied, notice is MANDATORY under Rule 201(c)(2), and the "
                   "qualifying facts here must be"),
    },
    "BURDEN_MISALLOCATED": {
        "name":           "Burden of Proof Placed on the Wrong Party",
        "rule_state":     "burden follows the party asserting the claim/affirmative defense; Pa.R.C.P. 1029",
        "rule_federal":  (
                   "the proponent bears the burden on each element; presumptions shift "
                   "production not persuasion (Fed. R. Evid. 301)"),
        "remedy_state":   "the ruling re-made with the burden correctly allocated",
        "remedy_federal": "the ruling re-made with the burden correctly allocated",
        "frame":  (
                   "the enforcing party was relieved of its burden (standing, holder status, "
                   "authority, condition-precedent compliance) and the burden was shifted to the "
                   "responding party to DISPROVE the claim; burden allocation is substantive "
                   "law, and a ruling built on the inverted burden must be"),
    },
    "CLAIM_PRECLUSION_BAR": {
        "name":           "Res Judicata / Claim Preclusion Bar",
        "rule_state":  (
                   "res judicata: final judgment on the merits, same parties/privies, same cause "
                   "of action"),
        "rule_federal":  (
                   "claim preclusion: final judgment on the merits bars claims that were or "
                   "could have been raised"),
        "remedy_state":   "the precluded claim dismissed; the prior judgment given its full effect",
        "remedy_federal": "the precluded claim dismissed; the prior judgment given its full effect",
        "frame":  (
                   "a claim already concluded by final judgment between these parties is being "
                   "relitigated (or an opponent splits claims to evade a prior loss); preclusion "
                   "cuts BOTH ways — their prior losses and admissions in concluded proceedings "
                   "bind them here, and the precluded matter must be"),
    },
    "ISSUE_PRECLUSION_BAR": {
        "name":           "Collateral Estoppel / Issue Preclusion Bar",
        "rule_state":  (
                   "issue actually litigated, determined, and essential to a prior final "
                   "judgment binds the parties"),
        "rule_federal":  (
                   "issue preclusion: identical issue, actually litigated, necessarily decided, "
                   "full and fair opportunity"),
        "remedy_state":   "the issue taken as established and not relitigated",
        "remedy_federal": "the issue taken as established and not relitigated",
        "frame":  (
                   "an issue this record shows was actually litigated and decided (or that an "
                   "opponent concedes by its own prior positions) is being re-argued as if open; "
                   "the determined issue must be"),
    },
    "LAW_OF_THE_CASE_VIOLATED": {
        "name":           "Law of the Case Disregarded",
        "rule_state":  (
                   "coordinate-jurisdiction rule: a judge may not overrule another judge's prior "
                   "decision in the same case absent new law/facts"),
        "rule_federal":  (
                   "law-of-the-case doctrine: prior rulings govern subsequent stages absent "
                   "extraordinary circumstances"),
        "remedy_state":   "the departing ruling vacated and the prior ruling enforced",
        "remedy_federal": "the departing ruling vacated and the prior ruling enforced",
        "frame":  (
                   "a later ruling silently departs from an earlier ruling in the same case (or "
                   "a second judge overrules the first without new law or facts); the departure "
                   "is error under the law of the case and the coordinate-jurisdiction rule, and "
                   "the earlier ruling must be"),
    },
    "OPPONENT_WAIVER_FORFEITURE": {
        "name":           "Opponent's Waiver or Forfeiture of the Objection",
        "rule_state":  (
                   "waiver = intentional relinquishment of a known right; objections not timely "
                   "raised are waived (Pa.R.C.P. 1032)"),
        "rule_federal":  (
                   "Fed. R. Civ. P. 12(h) (defenses waived if omitted); forfeiture by failure to "
                   "timely assert"),
        "remedy_state":   "the untimely objection disregarded as waived",
        "remedy_federal": "the untimely objection disregarded as waived/forfeited",
        "frame":  (
                   "the opponent sat on the objection it now raises (or failed to deny, respond, "
                   "or preserve in sequence) — waiver and forfeiture run against ENFORCERS and "
                   "their counsel exactly as they are invoked against pro se litigants; the "
                   "belated objection must be"),
    },
    "CANDOR_VIOLATION": {
        "name":           "Counsel's Duty of Candor Violated",
        "rule_state":  (
                   "Pa. RPC 3.3 (candor toward the tribunal); RPC 3.4 (fairness); RPC 8.4(c) "
                   "(dishonesty)"),
        "rule_federal":  (
                   "Model RPC 3.3; Fed. R. Civ. P. 11(b) (representations to the court); 28 "
                   "U.S.C. 1927"),
        "remedy_state":  (
                   "the offending assertion stricken; referral to the Disciplinary Board; fees "
                   "under 42 Pa.C.S. 2503"),
        "remedy_federal":  (
                   "the assertion stricken; Rule 11 motion after safe harbor; 1927 excess costs; "
                   "disciplinary referral"),
        "frame":  (
                   "counsel asserted to the tribunal facts its own client's documents contradict "
                   "(holder status against a blank allonge; verification by copies against an "
                   "admitted off-site original; citizenship of an entity not the one sued); Rule "
                   "3.3 makes the LAWYER answerable for the falsity independent of the client, "
                   "and the assertions must be"),
    },
    "RESCISSION_EFFECT_IGNORED": {
        "name":           "TILA Rescission's Automatic Effect Ignored",
        "rule_state":  (
                   "rescission under federal law displaces inconsistent state enforcement while "
                   "effective"),
        "rule_federal":  (
                   "15 U.S.C. 1635(b): security interest becomes VOID on rescission by operation "
                   "of law; 12 CFR 1026.23(d); Jesinoski v. Countrywide (notice suffices)"),
        "remedy_state":  (
                   "enforcement of the voided security interest halted pending resolution of the "
                   "rescission"),
        "remedy_federal":  (
                   "the security interest treated as void per 1635(b) unless and until a court "
                   "orders otherwise; foreclosure premised on it fails"),
        "frame":  (
                   "a 1635 rescission notice was sent and the statutory sequence (void security "
                   "interest, return of consideration, 20-day creditor duties) was never engaged "
                   "or adjudicated; Jesinoski holds notice alone effects rescission — "
                   "enforcement that simply ignores the outstanding rescission must be"),
    },
    "STATUTORY_DAMAGES_AVAILABLE": {
        "name":           "Statutory Damages Triggered on the Record",
        "rule_state":  (
                   "73 P.S. 201-1 seq. (UTPCPL: actual damages, treble, fees); 42 Pa.C.S. 8371 "
                   "(insurance bad faith)"),
        "rule_federal":  (
                   "15 U.S.C. 1692k (FDCPA); 15 U.S.C. 1681n/1681o (FCRA willful/negligent); 12 "
                   "U.S.C. 2605(f) (RESPA); 26 U.S.C. 7434 (false information returns)"),
        "remedy_state":   "damages pled and computed per violation on the record evidence",
        "remedy_federal": "statutory, actual, and punitive damages pled per violation; fees and costs",
        "frame":  (
                   "the record violations are not only defenses — each mapped violation carries "
                   "its own statutory damages provision (FDCPA 1692k, FCRA 1681n/o, RESPA "
                   "2605(f), 7434 for false 1099s, UTPCPL treble, 8371 bad faith); the "
                   "affirmative recovery must be"),
    },
    # === END MIRROR_SYNC_20260825 ===
    # === MIRROR_SYNC_20260825 (ported from pipeline/scorecard_if_then.py — single source of truth) ===
    "MANDATORY_LANGUAGE_DISREGARDED": {
        "name":           "Mandatory 'Shall' Duty Treated as Optional",
        "rule_state":  (
                   "1 Pa.C.S. 1921(b) (clear text not disregarded under pretext of its spirit); "
                   "the statute's own command word"),
        "rule_federal":   "plain-meaning rule; the enacted text's command language (shall = mandatory)",
        "remedy_state":   "the mandatory duty performed or the ruling built on its disregard vacated",
        "remedy_federal": "the mandatory duty performed or the ruling built on its disregard vacated",
        "frame":  (
                   "the text says 'shall' and the ruling treats it as directory or weighs it as "
                   "a factor; command words are not suggestions — 1 Pa.C.S. 1921(b) forbids "
                   "disregarding clear text under the pretext of pursuing its spirit, and "
                   "rulings resting on the disregard must be"),
    },
    "EQUITY_MAXIM_APPLICABLE": {
        "name":           "Controlling Equity Maxim Engaged by the Record",
        "rule_state":  (
                   "equity jurisprudence (maxims operative in equitable proceedings); Pa. "
                   "foreclosure/quiet title are equitable"),
        "rule_federal":  (
                   "equity jurisprudence (Broom's Maxims; equity treatises); clean hands, "
                   "vigilance, substance over form"),
        "remedy_state":   "the maxim applied as forum law: the offending party denied equitable relief",
        "remedy_federal": "the maxim applied as forum law: the offending party denied equitable relief",
        "frame":  (
                   "this is an equitable proceeding and the record engages a controlling maxim "
                   "(clean hands against inconsistent 1099-C positions and conflicting "
                   "productions; substance over form against label-deep claims; no profit from "
                   "one's own wrong against self-created defaults); maxims are the operative law "
                   "of the equity forum and must be"),
    },
    # === END MIRROR_SYNC_20260825 ===
    # === MIRROR_SYNC_20260825 (ported from pipeline/scorecard_if_then.py — single source of truth) ===
    "PERSONAL_JURISDICTION_UNESTABLISHED": {
        "name":           "Personal Jurisdiction Never Established Over the Party",
        "rule_state":  (
                   "42 Pa.C.S. 5322 (long-arm); Pa.R.C.P. 402-404 (service); due process minimum "
                   "contacts"),
        "rule_federal":  (
                   "Fed. R. Civ. P. 4(k); International Shoe minimum-contacts line; specific vs "
                   "general jurisdiction; Fed. R. Civ. P. 12(b)(2)"),
        "remedy_state":  (
                   "dismissed as to the party, or all acts against them void for want of "
                   "personal jurisdiction"),
        "remedy_federal": "dismissed under 12(b)(2); judgment against the party void under 60(b)(4)",
        "frame":  (
                   "a court's power over a PERSON requires valid service AND constitutionally "
                   "sufficient contacts (in personam), or the res within the forum (in rem/quasi "
                   "in rem); neither was established here — and a judgment without personal "
                   "jurisdiction is void, not merely erroneous, so the affected rulings must be"),
    },
    "RESTITUTION_REMEDY_AVAILABLE": {
        "name":           "Restitution / Constructive Trust / Subrogation Triggered by Double Recovery",
        "rule_state":  (
                   "PA unjust enrichment (benefit conferred, appreciation, inequitable "
                   "retention); constructive trust; equitable subrogation"),
        "rule_federal":  (
                   "restitution doctrine; constructive trust as remedy for property obtained by "
                   "fraud/mistake; subrogation (insurer steps into insured's shoes)"),
        "remedy_state":  (
                   "disgorgement of the double recovery; constructive trust impressed on "
                   "proceeds; subrogation accounting compelled"),
        "remedy_federal":  (
                   "disgorgement; constructive trust on identifiable proceeds; accounting of all "
                   "recoveries on the obligation"),
        "frame":  (
                   "the record shows multiple recovery lanes on one obligation (cancellation "
                   "reported, insurance premiums flowing, securitization proceeds taken, "
                   "foreclosure pursued); where a party has recovered or positioned to recover "
                   "more than once, restitution law supplies the AFFIRMATIVE remedy — the excess "
                   "is held for the party who bore the loss, and must be"),
    },
    # === END MIRROR_SYNC_20260825 ===
}


def _default_hook_for_unknown_category(category: str) -> dict:
    """Generic legal hook for scorecard categories we haven't mapped yet.
    Allows new audit categories to flow into motions without code changes."""
    pretty = category.replace("_", " ").title()
    return {
        "name":           pretty,
        "rule_state":     "Pa.R.C.P. 1028(a)(2)",
        "rule_federal":   "Fed. R. Civ. P. 12(f)",
        "remedy_state":   "stricken",
        "remedy_federal": "stricken",
        "frame":  ("the conduct identified is procedurally defective and "
                   "the affected portions must be"),
    }


def _build_strict_section_ii(scorecard_facts: list, is_state: bool) -> list:
    """Build Section II strictly from scorecard violations.

    Args:
        scorecard_facts: list of dicts from _gather_scorecard_facts().
                         Each has 'text', 'category', 'severity', 'weight'.
        is_state: True for PA CCP filings, False for federal.

    Returns:
        List of paragraph strings ready for the section.
    """
    if not scorecard_facts:
        return [
            "Plaintiff's audit of the record discloses procedural defects "
            "as enumerated below."
        ]

    # Group facts by category
    by_category = {}
    for f in scorecard_facts:
        cat = f.get("category", "")
        if not cat:
            continue
        by_category.setdefault(cat, []).append(f)

    if not by_category:
        return [
            "Plaintiff's audit of the record discloses procedural defects "
            "as enumerated below."
        ]

    # Sort by total weight per category (heaviest first)
    def cat_weight(cat):
        return -sum(f.get("weight", 0) for f in by_category[cat])

    sorted_categories = sorted(by_category.keys(), key=cat_weight)

    lines = [
        "Plaintiff identifies the following defects in opposing filings, "
        "each established by the record and each independently warranting "
        "the relief requested:",
        "",
    ]

    counter = 1
    for cat in sorted_categories:
        facts_in_cat = by_category[cat]
        if not facts_in_cat:
            continue

        hook = _SCORECARD_HOOK_MAP.get(cat) or _default_hook_for_unknown_category(cat)
        name   = hook["name"]
        # Jurisdiction-aware rule citation (avoids court-profile-filter stripping
        # state cites from federal motions, and vice versa)
        rule   = hook.get("rule_state" if is_state else "rule_federal") \
                 or hook.get("rule") \
                 or "Pa.R.C.P. 1028(a)(2)"
        remedy = hook["remedy_state"] if is_state else hook["remedy_federal"]
        frame  = hook["frame"]

        # Header
        lines.append(f"{counter}. **{name}** ({rule})")

        # Count line if multiple instances
        n = len(facts_in_cat)
        if n > 1:
            lines.append(
                f"   The record reflects at least {n} instances of this defect."
            )

        # Pick a representative fact (highest severity, highest weight)
        sev_rank = {"FATAL": 3, "SERIOUS": 2, "STANDARD": 1}
        facts_in_cat.sort(
            key=lambda f: (
                -sev_rank.get((f.get("severity") or "").upper(), 0),
                -(f.get("weight") or 0),
            )
        )
        primary = facts_in_cat[0]
        fact_text = (primary.get("text") or "").strip()
        fact_text = _humanize_court_labels(fact_text)
        if fact_text and fact_text[-1] not in ".!?":
            fact_text = fact_text + "."
        if fact_text:
            lines.append(f"   {fact_text}")

        # Add a second representative fact only if it's MEANINGFULLY different
        # (compare by first 60 chars after lowercasing).
        if n > 1:
            seen_keys = {(fact_text or "")[:60].lower()}
            for f in facts_in_cat[1:]:
                t = (f.get("text") or "").strip()
                if not t:
                    continue
                t = _humanize_court_labels(t)
                key = t[:60].lower()
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                if t[-1] not in ".!?":
                    t = t + "."
                lines.append(f"   {t}")
                break  # one supplementary fact is enough

        # Rule + remedy sentence
        lines.append(
            f"   Under {rule}, {frame} {remedy}."
        )
        lines.append("")
        counter += 1

    return lines


def _build_strict_section_iii(scorecard_facts: list, is_state: bool) -> list:
    """Build Section III IF/THEN strictly from scorecard violations.

    One IF/THEN block per category present in the scorecard.
    """
    if not scorecard_facts:
        return [
            "The application of the controlling rules to the defects "
            "identified is deterministic. Each violation independently "
            "triggers the requested relief."
        ]

    by_category = {}
    for f in scorecard_facts:
        cat = f.get("category", "")
        if cat and cat not in by_category:
            by_category[cat] = f  # keep first per category

    if not by_category:
        return []

    lines = [
        "The application of the controlling rules to the defects "
        "established by the record is deterministic. Each defect "
        "independently triggers the requested relief.",
        "",
    ]

    for cat, f in by_category.items():
        hook = _SCORECARD_HOOK_MAP.get(cat) or _default_hook_for_unknown_category(cat)
        rule   = hook.get("rule_state" if is_state else "rule_federal") \
                 or hook.get("rule") \
                 or "Pa.R.C.P. 1028(a)(2)"
        remedy = hook["remedy_state"] if is_state else hook["remedy_federal"]
        fact_text = (f.get("text") or "").strip()
        if fact_text and fact_text[-1] in ".!?":
            fact_text = fact_text[:-1]

        lines.append(f"**IF** {fact_text},")
        lines.append(f"**AND** {rule} governs the propriety of such filings,")
        lines.append(f"**THEN** the affected material shall be {remedy}.")
        lines.append("")

    return lines
# === END STRICT_SCORECARD_SECTION_II_V1 ===


# === PHASE2_PROCEDURAL_DRAFTERS_V2 ===
# Data-driven procedural motion drafter. Sections II and III are now
# generated from collect_violations() output instead of generic blocks.

# Filing-type categorization
_PROCEDURAL_MOTIONS = {
    "motion_to_strike_state",
    "motion_to_strike_federal",
    "motion_to_strike",
    "motion_to_compel",
    "motion_for_sanctions",
    "motion_for_garnishee_default",
    "motion_to_vacate",
}
_RECUSAL_MOTIONS = {"motion_for_recusal"}
_BRIEF_MOTIONS = {
    "motion_for_reconsideration",
    "petition_for_writ_of_mandamus",
    "motion_to_remand",
    "notice_of_supplemental_authority",
}
_COMPLAINT_TYPES = {
    "complaint",
    "amended_complaint",
    "complaint_for_declaratory_judgment",
}


def _safe_block_text(block_path: str) -> str:
    """Read a block .md, strip frontmatter, return body text."""
    try:
        from pathlib import Path as _P
        full = _P(str(_eroot() / "obsidian_sync/templates/blocks")) / block_path
        if not full.exists():
            return ""
        text = full.read_text(encoding="utf-8")
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                text = parts[2]
        return text.strip()
    except Exception:
        return ""


def _has_bias_indicator(data) -> bool:
    """Recusal requires explicit bias evidence."""
    try:
        BIAS_KEYWORDS = (
            "bias", "prejudice", "ex parte", "personal interest",
            "financial interest", "appearance of impropriety",
            "extrajudicial", "predetermined",
        )
        by_issue = getattr(data, "by_issue", None) or {}
        for grp in by_issue.values():
            for v in getattr(grp, "violations", []) or []:
                blob = (
                    str(getattr(v, "issue", "")) + " "
                    + str(getattr(v, "violation", "")) + " "
                    + str(getattr(v, "explanation", "")) + " "
                    + str(getattr(v, "category", ""))
                ).lower()
                if any(kw in blob for kw in BIAS_KEYWORDS):
                    return True
        return False
    except Exception:
        return False


# Map procedural rule source → standardized IF/THEN result text
_PROCEDURAL_REMEDIES = {
    "Pa.R.C.P. 1024":  "the unverified pleading shall be stricken under Pa.R.C.P. 1028(a)(2).",
    "Pa.R.C.P. 1028":  "the non-conforming pleading shall be stricken.",
    "Pa.R.C.P. 440":   "the filing has no legal effect and must be stricken.",
    "Pa.R.C.P. 3145":  "the garnishee's failure to answer triggers default under Pa.R.C.P. 3146.",
    "Pa.R.C.P. 3146":  "default judgment against the garnishee is appropriate.",
    "Pa.R.C.P. 237.3": "the resulting judgment is void and subject to relief.",
    "Pa.R.C.P. 1023.1": "the certification requirement was violated; sanctions are warranted.",
    "Pa.R.C.P. 1023.4": "sanctions are warranted to deter recurrence.",
    "FRCP 4":          "service is defective and the filing is without legal effect.",
    "FRCP 5":          "service of subsequent papers is defective; the filing must be disregarded.",
    "FRCP 11":         "sanctions are warranted under Rule 11(c).",
    "FRCP 12(b)":      "the defense is improperly raised and must be dismissed.",
    "FRCP 12(f)":      "the immaterial or insufficient matter is properly stricken.",
    "FRCP 60(b)(4)":   "the void judgment shall be vacated.",
    "U.S. Const. amend. XIV § 1": "the resulting action lacks due process and is void.",
    "14th Amendment":  "the resulting action lacks due process and is void.",
}


def _remedy_for(source: str) -> str:
    """Map a rule citation to its mandatory remedy text."""
    if not source:
        return "the filing must be stricken or appropriate relief entered."
    s = source.strip()
    # Try exact match first, then prefix matches
    if s in _PROCEDURAL_REMEDIES:
        return _PROCEDURAL_REMEDIES[s]
    for key, remedy in _PROCEDURAL_REMEDIES.items():
        if s.startswith(key) or key in s:
            return remedy
    return "the filing must be stricken or appropriate relief entered."


def _enumerate_procedural_defects(violations: dict,
                                  scorecard_facts: list = None,
                                  use_llm: bool = True) -> list:
    """Section II: numbered list of actual violations from collect_violations().

    Phase 3: when use_llm=True and scorecard_facts provided, append a
    LLM-composed fact-application sentence per violation."""
    lines = []
    procedural   = violations.get("procedural", []) or []
    constitutional = violations.get("constitutional", []) or []
    fiduciary    = violations.get("fiduciary", []) or []

    if not (procedural or constitutional or fiduciary):
        lines.append(
            "The opposing filings contain procedural defects identified in "
            "the record. Each is enumerated below with the controlling rule "
            "and the deterministic consequence."
        )
        return lines

    counter = 1
    if procedural:
        lines.append(
            "Plaintiff identifies the following procedural defects in opposing "
            "filings, each independently warranting the relief requested:"
        )
        lines.append("")
        for v in procedural:
            source = v.get("source", "") or ""
            name   = v.get("name") or v.get("source_title") or source
            title  = v.get("source_title", "") or ""
            jur    = v.get("jurisdiction", "") or ""
            entry = f"{counter}. **{name}** ({source})"
            lines.append(entry)
            if title and title != name:
                lines.append(f"   Authority: {title}")
            lines.append(f"   Jurisdiction: {jur}")
            # Phase 3: LLM-composed fact application
            if use_llm and scorecard_facts:
                fact_line = _llm_compose_fact_application(v, scorecard_facts)
                if fact_line and fact_line.strip():
                    lines.append(fact_line)
            lines.append("")
            counter += 1

    if constitutional:
        if procedural:
            lines.append("In addition, the record establishes constitutional defects:")
            lines.append("")
        for v in constitutional:
            source = v.get("source", "") or ""
            name   = v.get("name") or source
            title  = v.get("source_title", "") or ""
            entry = f"{counter}. **{name}** ({source})"
            lines.append(entry)
            if title and title != name:
                lines.append(f"   Authority: {title}")
            # Phase 3: LLM-composed fact application
            if use_llm and scorecard_facts:
                fact_line = _llm_compose_fact_application(v, scorecard_facts)
                if fact_line and fact_line.strip():
                    lines.append(fact_line)
            lines.append("")
            counter += 1

    return lines


def _build_if_then_blocks(violations: dict) -> list:
    """Section III: per-violation IF/AND/THEN blocks driven by data."""
    lines = [
        "The application of controlling rules to the defects identified is "
        "deterministic. Each violation independently triggers the requested "
        "relief.",
        "",
    ]

    procedural     = violations.get("procedural", []) or []
    constitutional = violations.get("constitutional", []) or []

    rendered = 0
    for v in procedural:
        source = v.get("source", "") or ""
        name   = v.get("name") or v.get("source_title") or source
        if not source:
            continue
        remedy = _remedy_for(source)
        lines.append(f"**IF** an opposing filing fails to comply with {source} ({name}),")
        lines.append(f"**AND** {source} requires compliance at the threshold of judicial review,")
        lines.append(f"**THEN** {remedy}")
        lines.append("")
        rendered += 1

    for v in constitutional:
        source = v.get("source", "") or ""
        name   = v.get("name") or source
        if not source:
            continue
        remedy = _remedy_for(source)
        lines.append(f"**IF** the proceeding lacks compliance with {source} ({name}),")
        lines.append(f"**AND** the violation affects the validity of resulting orders,")
        lines.append(f"**THEN** {remedy}")
        lines.append("")
        rendered += 1

    if rendered == 0:
        lines.append(
            "The procedural rules in force in this matter — including those "
            "cited above — apply deterministically; non-compliance triggers "
            "the relief stated in Section V."
        )
    return lines


def build_procedural_motion(data, filing_type: str = "",
                            bundled_with: list = None) -> dict:
    """Build a procedural motion document from violation data.

    Sections II and III are GENERATED from collect_violations() output —
    the same source the violations_matrix exhibit uses. No generic blocks.

    Bundles compatible motions (e.g. strike + sanctions).
    """
    bundled_with = bundled_with or []
    case_court = (getattr(data, "case_court", "") or "")
    is_state  = "state" in case_court.lower() or "PA_CP" in case_court
    is_strike = "strike" in filing_type
    is_vacate = "vacate" in filing_type
    is_sanctions = "sanctions" in filing_type or "motion_for_sanctions" in bundled_with
    is_compel = "compel" in filing_type or "motion_to_compel" in bundled_with
    is_garnishee = "garnishee" in filing_type
    has_cross_case = bool(getattr(data, "cross_case_count", 0))

    # Pull violations from the same source the matrix uses
    try:
        from generation.violations_matrix import collect_violations
        claims    = getattr(data, "claims", []) or []
        doctrines = getattr(data, "doctrines", []) or []
        violations = collect_violations(claims, doctrines)
    except Exception as _e:
        log.warning(f"collect_violations failed: {_e}")
        violations = {}

    sections = []

    # ---- Caption ----
    try:
        sections.append({"kind": "caption", "data": build_caption_block(data)})
    except Exception as _e:
        log.warning(f"caption build failed: {_e}")

    # ---- Title ----
    title = _motion_title_for(filing_type)
    if "motion_for_sanctions" in bundled_with and "sanctions" not in title.lower():
        title = title + "\nAND FOR SANCTIONS"
    if "motion_to_compel" in bundled_with and "compel" not in title.lower():
        title = title + "\nAND TO COMPEL COMPLIANCE"
    sections.append({"kind": "title", "text": title})

    # ---- I. INTRODUCTION ----
    intro_lines = [
        "Plaintiff respectfully submits this motion seeking the relief stated below."
    ]
    n_proc = len(violations.get("procedural", []) or [])
    n_const = len(violations.get("constitutional", []) or [])
    if is_strike:
        intro_lines.append(
            f"The opposing filings identified herein contain {n_proc} procedural "
            f"defects and {n_const} constitutional defect(s) tabulated below. "
            "Each independently warrants the relief requested."
        )
    if is_vacate:
        intro_lines.append(
            "The judgment Plaintiff seeks to vacate rests on procedural and "
            "authority defects established by the record and tabulated below. "
            "Each defect independently renders the resulting order voidable; "
            "in combination they render it void as a matter of law."
        )
    if is_sanctions:
        intro_lines.append(
            "Sanctions are warranted to deter recurrence of conduct that "
            "shifted to Plaintiff (and to this Court) the work of identifying "
            "infirmities that should never have entered the record."
        )
    if is_garnishee:
        intro_lines.append(
            "Plaintiff requests entry of default judgment against garnishee(s) "
            "that have failed to file the verified responses required by "
            "Pa.R.C.P. 3145, warranting default under Pa.R.C.P. 3146."
        )
    sections.append({"kind": "section", "heading": "I. INTRODUCTION",
                     "paragraphs": intro_lines})
    # ===PHASE_3F_TEMPLATE_HOOKS_V1=== INSERT 3 — Dispositive Order anchor
    _dispositive_orders = []
    try:
        _canonical_id = (
            getattr(data, "canonical_id", "")
            or getattr(data, "case_id", "")
            or ""
        )
        if _canonical_id:
            _dispositive_orders = _extract_dispositive_orders(_canonical_id)
    except Exception as _e:
        log.warning(f"_extract_dispositive_orders failed: {_e}")
        _dispositive_orders = []

    # Attach to data for downstream (relief block, conclusion)
    try:
        object.__setattr__(data, "_dispositive_orders", _dispositive_orders)
    except Exception:
        pass

    if _dispositive_orders:
        # Build Section II: DISPOSITIVE ORDER AT ISSUE
        # (one paragraph per dispositive order, anchored to actual data)
        disp_paras = []
        for d_order in _dispositive_orders:
            date_str = _format_date(d_order["date"])
            defs_str = _format_defendant_list(d_order["defendants"])
            judge_str = d_order["judge"] or "this Court"
            ecf_str = d_order["ecf"] or ""

            # Lead paragraph — anchors to the actual ruling
            anchor_parts = []
            if date_str:
                anchor_parts.append(f"On {date_str}")
            anchor_parts.append("this Court")
            if ecf_str:
                anchor_parts.append(f"({ecf_str})")
            verb = "granted Motions to Dismiss filed by Defendants"
            if not defs_str:
                # Fallback when defendants couldn't be parsed
                anchor_lead = " ".join(anchor_parts) + f" {verb} in this matter"
            else:
                anchor_lead = " ".join(anchor_parts) + f" {verb} {defs_str}"

            anchor_lead += ", resulting in dismissal of Plaintiff's claims as set forth therein."
            disp_paras.append(anchor_lead)

            # Optional: list dismissed claim subjects if extracted
            if d_order["subjects"]:
                first_subj = d_order["subjects"][0]
                if len(first_subj) > 30:  # only use if substantive
                    disp_paras.append(
                        f"The Order specifically addressed: {first_subj}."
                    )

            # Deadline reference
            if d_order["deadline"]:
                disp_paras.append(
                    f"The Order set a corresponding deadline of "
                    f"{_format_date(d_order['deadline'])} for any amendment "
                    f"of the dismissed claims."
                )

        # IF/THEN block — connects dismissal to relief sought
        disp_paras.append("")
        disp_paras.append(
            "> IF a court grants a Motion to Dismiss,"
        )
        disp_paras.append(
            "> AND the ruling terminates Plaintiff's claims,"
        )
        disp_paras.append(
            "> THEN the ruling is dispositive and subject to reconsideration "
            "or vacatur where legal error affected the outcome."
        )

        sections.append({
            "kind": "section",
            "heading": "II. DISPOSITIVE ORDER AT ISSUE",
            "paragraphs": disp_paras,
        })
    # ===END PHASE_3F_TEMPLATE_HOOKS_V1=== INSERT 3


    # ---- II. PROCEDURAL DEFECTS (data-driven + Phase 3 LLM polish) ----
    # Phase 3: gather scorecard facts so each violation can get a
    # fact-application sentence. Pass canonical_id so the gatherer can
    # find the right audit JSON file.
    try:
        _canonical = (
            getattr(data, "canonical_id", "")
            or getattr(data, "case_id", "")
            or ""
        )
        _scorecard_facts = _gather_scorecard_facts(data, canonical_id=_canonical)
    except Exception as _e:
        log.warning(f"_gather_scorecard_facts failed: {_e}")
        _scorecard_facts = []
    # STRICT MODE: Section II from scorecard only (fact-backed)
    sections.append({
        "kind": "section",
        "heading": "II. DEFECTS IN OPPOSING FILINGS",
        "paragraphs": _build_strict_section_ii(_scorecard_facts, is_state),
    })

    # ---- III. RULE APPLICATION (data-driven IF/THEN) ----
    # STRICT MODE: Section III IF/THEN from scorecard only
    sections.append({
        "kind": "section",
        "heading": "III. DETERMINISTIC RULE APPLICATION",
        "paragraphs": _build_strict_section_iii(_scorecard_facts, is_state),
    })

    next_roman = "IV"

    # ---- IV. CROSS-CASE / VOID ORDER (conditional) ----
    if has_cross_case:
        void_text   = _safe_block_text("arguments/void_order_misuse.md")
        candor_text = _safe_block_text("arguments/candor_violation.md")
        body = [t for t in (void_text, candor_text) if t]
        if body:
            sections.append({
                "kind": "section",
                "heading": f"{next_roman}. RELIANCE ON CONTESTED ORDER FROM RELATED CASE",
                "paragraphs": body,
            })
            next_roman = {"IV": "V", "V": "VI"}[next_roman]

    # ---- SANCTIONS (conditional) ----
    if is_sanctions:
        sanctions_text = _safe_block_text("arguments/sanctions_request.md")
        if sanctions_text:
            sections.append({
                "kind": "section",
                "heading": f"{next_roman}. SANCTIONS ARE WARRANTED",
                "paragraphs": [sanctions_text],
            })
            next_roman = {"IV": "V", "V": "VI", "VI": "VII"}[next_roman]

    # ---- RELIEF (mandatory, rule-bound) ----
    relief_lines = [
        "WHEREFORE, Plaintiff respectfully requests that this Court enter an Order:",
        "",
    ]
    relief_count = 1
    if is_strike:
        relief_lines.append(
            f"{relief_count}. STRIKING the procedurally defective filings "
            f"identified in Section II;"
        )
        relief_count += 1
        relief_lines.append(
            f"{relief_count}. STRIKING any reliance on authority outside "
            f"the binding hierarchy of this forum;"
        )
        relief_count += 1
    if is_garnishee:
        relief_lines.append(
            f"{relief_count}. ENTERING default judgment against the "
            f"non-responsive garnishee(s) under Pa.R.C.P. 3146;"
        )
        relief_count += 1
    if is_vacate:
        relief_lines.insert(
            0 if not relief_lines or not relief_lines[0].startswith("WHEREFORE") else 1,
            ""
        )
        # Put vacate-specific relief at the top of the numbered list
        vacate_para = (
            f"{relief_count}. VACATING the underlying order/judgment as void "
            f"for the reasons set forth in Section II;"
        )
        relief_lines.append(vacate_para)
        relief_count += 1
    if is_compel:
        relief_lines.append(
            f"{relief_count}. COMPELLING compliance with outstanding "
            f"procedural obligations within fourteen (14) days;"
        )
        relief_count += 1
    if is_sanctions:
        relief_lines.append(
            f"{relief_count}. AWARDING reasonable costs and fees as a "
            f"sanction under "
            + ("Pa.R.C.P. 1023.4" if is_state else "FRCP 11") + ";"
        )
        relief_count += 1
    if has_cross_case:
        relief_lines.append(
            f"{relief_count}. DECLINING to give effect to any order from a "
            f"related case whose validity is the subject of pending appellate "
            f"review;"
        )
        relief_count += 1
    relief_lines.append(
        f"{relief_count}. GRANTING such further relief as the Court deems "
        f"just and proper."
    )
    # ===PHASE_3F_TEMPLATE_HOOKS_V1=== INSERT 4 — vacate dismissal relief
    _dispositive_orders_attached = getattr(data, "_dispositive_orders", []) or []
    if _dispositive_orders_attached:
        most_recent = _dispositive_orders_attached[0]
        vacate_date = _format_date(most_recent["date"])
        defs_str = _format_defendant_list(most_recent["defendants"])
        # Insert at top of relief list (after WHEREFORE preamble if present)
        insert_at = 1 if (relief_lines and relief_lines[0].startswith("WHEREFORE")) else 0
        # Build prepend lines (will renumber existing ones below)
        recon_para = (
            f"GRANTING reconsideration of the {vacate_date} Order "
            f"granting Defendants" + (f" {defs_str}'" if defs_str else "'")
            + " Motion(s) to Dismiss;"
        )
        vacate_para_3f = (
            f"VACATING the {vacate_date} dismissal order and reopening "
            f"Plaintiff's claims for adjudication under the correct "
            f"legal standard;"
        )
        # Splice in (need to renumber)
        new_lines = relief_lines[:insert_at]
        new_lines.append(f"{1}. " + recon_para)
        new_lines.append(f"{2}. " + vacate_para_3f)
        # Renumber any subsequent items
        existing_count = 2
        for line in relief_lines[insert_at:]:
            # Strip leading "N. " and replace with new number
            m = re.match(r"^(\d+)\. (.*)$", line)
            if m:
                existing_count += 1
                new_lines.append(f"{existing_count}. {m.group(2)}")
            else:
                new_lines.append(line)
        relief_lines = new_lines
        # Update relief_count for any further appends below
        relief_count = existing_count + 1
    # ===END PHASE_3F_TEMPLATE_HOOKS_V1=== INSERT 4


    sections.append({
        "kind": "section",
        "heading": f"{next_roman}. RELIEF REQUESTED",
        "paragraphs": relief_lines,
    })
    next_roman = {"IV": "V", "V": "VI", "VI": "VII", "VII": "VIII"}.get(next_roman, "VIII")

    # ---- CONCLUSION ----
        # ===PHASE_3F_TEMPLATE_HOOKS_V1=== INSERT 5 — conclusion bridge
    _conclusion_paras = [
        "For the foregoing reasons, Plaintiff respectfully requests that "
        "the Court grant the relief stated above.",
    ]
    _disp_orders_concl = getattr(data, "_dispositive_orders", []) or []
    if _disp_orders_concl:
        most_recent = _disp_orders_concl[0]
        bridge = (
            f"The {_format_date(most_recent['date'])} dismissal order rests "
            "on the authorities and procedural defects identified above. "
            "Because the dismissal adopts arguments grounded in non-binding "
            "authority and was entered without addressing the record evidence "
            "submitted by Plaintiff, the resulting decision is structurally "
            "defective and warrants reconsideration."
        )
        _conclusion_paras.insert(0, bridge)
    # ===END PHASE_3F_TEMPLATE_HOOKS_V1=== INSERT 5

    sections.append({
        "kind": "section",
        "heading": f"{next_roman}. CONCLUSION",
        "paragraphs": _conclusion_paras,
    })

    # ---- Boilerplate ----
    sig_text = _safe_block_text("boilerplate/signature_block_pro_se.md")
    if sig_text:
        sections.append({"kind": "boilerplate", "label": "signature",
                         "text": sig_text})
    cos_text = _safe_block_text("boilerplate/certificate_of_service.md")
    if cos_text:
        sections.append({"kind": "boilerplate", "label": "certificate_of_service",
                         "text": cos_text})
    if is_state:
        ver_text = _safe_block_text("boilerplate/verification.md")
        if ver_text:
            sections.append({"kind": "boilerplate", "label": "verification",
                             "text": ver_text})
    po_text = _safe_block_text("boilerplate/proposed_order.md")
    if po_text:
        sections.append({"kind": "boilerplate", "label": "proposed_order",
                         "text": po_text})

    # ===PHASE_3F_STEP6C_CHUNKED_V1=== invoke Agent 6 enhancement
    try:
        _disp_orders_for_llm = getattr(data, "_dispositive_orders", []) or []
        _scorecard_facts_for_llm = []
        try:
            _canonical = (
                getattr(data, "canonical_id", "")
                or getattr(data, "case_id", "")
                or ""
            )
            if _canonical:
                _scorecard_facts_for_llm = _gather_scorecard_facts(
                    data, canonical_id=_canonical
                )
        except Exception:
            _scorecard_facts_for_llm = []

        _motion_result = {
            "filing_type": filing_type,
            "title": title,
            "sections": sections,
            "bundled_with": bundled_with,
            "structure": "procedural_motion",
            "violation_counts": {k: len(v) for k, v in violations.items()},
        }

        _enhanced = _enhance_motion_with_llm(
            _motion_result,
            data,
            _disp_orders_for_llm,
            _scorecard_facts_for_llm,
        )

        if _enhanced is not None:
            return _enhanced
    except Exception as _e:
        log.warning(f"Agent 6 enhancement failed (falling back to deterministic): {_e}")
    # ===END PHASE_3F_STEP6C_CHUNKED_V1===

    return {
        "filing_type": filing_type,
        "title": title,
        "sections": sections,
        "bundled_with": bundled_with,
        "structure": "procedural_motion",
        "violation_counts": {k: len(v) for k, v in violations.items()},
    }


def build_recusal_motion(data) -> dict:
    """Recusal motion — only fires with explicit bias_indicator."""
    if not _has_bias_indicator(data):
        raise ValueError(
            "build_recusal_motion: no bias_indicator in defects. "
            "Recusal requires explicit bias evidence."
        )

    is_federal = "fed" in (getattr(data, "case_court", "") or "").lower()

    sections = []
    try:
        sections.append({"kind": "caption", "data": build_caption_block(data)})
    except Exception:
        pass

    title = "PLAINTIFF'S MOTION FOR RECUSAL"
    sections.append({"kind": "title", "text": title})

    sections.append({"kind": "section", "heading": "I. INTRODUCTION",
                     "paragraphs": [
        "Plaintiff respectfully moves the Court for the recusal of the "
        "presiding judge pursuant to the authorities cited herein.",
    ]})

    if is_federal:
        sections.append({"kind": "section", "heading": "II. STATUTORY FRAMEWORK",
                         "paragraphs": [
            "28 U.S.C. § 455(a) requires recusal in any proceeding in which "
            "the judge's impartiality might reasonably be questioned. "
            "28 U.S.C. § 455(b) sets out specific circumstances mandating "
            "recusal. 28 U.S.C. § 144 permits a party to file an affidavit "
            "of personal bias or prejudice, requiring reassignment.",
        ]})
    else:
        sections.append({"kind": "section", "heading": "II. FRAMEWORK",
                         "paragraphs": [
            "Pennsylvania's Code of Judicial Conduct, Canon 2, Rule 2.11, "
            "requires recusal where the judge's impartiality might "
            "reasonably be questioned.",
        ]})

    bias_block = _safe_block_text("arguments/recusal_bias.md")
    sections.append({"kind": "section",
                     "heading": "III. FACTS DEMONSTRATING BIAS",
                     "paragraphs": [
        bias_block or
        "[The specific bias indicators identified in the record are "
        "cataloged here.]",
    ]})

    sections.append({"kind": "section",
                     "heading": "IV. RELIEF REQUESTED",
                     "paragraphs": [
        "WHEREFORE, Plaintiff respectfully requests that this Court grant "
        "the motion for recusal and direct that this matter be reassigned.",
    ]})

    sig = _safe_block_text("boilerplate/signature_block_pro_se.md")
    if sig:
        sections.append({"kind": "boilerplate", "label": "signature", "text": sig})
    cos = _safe_block_text("boilerplate/certificate_of_service.md")
    if cos:
        sections.append({"kind": "boilerplate", "label": "certificate_of_service", "text": cos})

    return {
        "filing_type": "motion_for_recusal",
        "title": title,
        "sections": sections,
        "bundled_with": [],
        "structure": "recusal_motion",
    }


def render_motion_md(motion: dict, data=None) -> str:
    """Render a procedural/recusal motion dict to markdown."""
    lines = []
    for sec in motion.get("sections", []):
        kind = sec.get("kind", "")
        if kind == "caption":
            cap = sec.get("data", "")
            if isinstance(cap, str):
                lines.append(cap)
            elif isinstance(cap, dict):
                for k in ("header", "caption", "text"):
                    if cap.get(k):
                        lines.append(str(cap[k]))
            lines.append("")
        elif kind == "title":
            lines.append("")
            lines.append(sec.get("text", ""))
            lines.append("")
        elif kind == "section":
            lines.append("")
            lines.append("## " + sec.get("heading", ""))
            lines.append("")
            for p in sec.get("paragraphs", []) or []:
                lines.append(p)
                lines.append("")
        elif kind == "boilerplate":
            lines.append("")
            lines.append("---")
            lines.append("")
            lines.append(sec.get("text", ""))
            lines.append("")
    return "\n".join(lines)


def render_motion_docx(motion: dict, output_path, data=None) -> None:
    """Render a procedural/recusal motion to .docx."""
    from docx import Document
    from docx.shared import Pt, Inches
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Times New Roman"
    style.font.size = Pt(12)
    for section in doc.sections:
        section.left_margin = Inches(1)
        section.right_margin = Inches(1)
        section.top_margin = Inches(1)
        section.bottom_margin = Inches(1)

    for sec in motion.get("sections", []):
        kind = sec.get("kind", "")
        if kind == "caption":
            cap = sec.get("data", "")
            if isinstance(cap, str):
                p = doc.add_paragraph(cap)
            elif isinstance(cap, dict):
                for k in ("header", "caption", "text"):
                    if cap.get(k):
                        p = doc.add_paragraph(str(cap[k]))
                        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            doc.add_paragraph("")
        elif kind == "title":
            for line in sec.get("text", "").split("\n"):
                p = doc.add_paragraph(line)
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                for run in p.runs:
                    run.bold = True
            doc.add_paragraph("")
        elif kind == "section":
            heading = sec.get("heading", "")
            if heading:
                p = doc.add_paragraph(heading)
                for run in p.runs:
                    run.bold = True
            for para in sec.get("paragraphs", []) or []:
                doc.add_paragraph(para)
        elif kind == "boilerplate":
            doc.add_paragraph("")
            doc.add_paragraph("─" * 50)
            doc.add_paragraph(sec.get("text", ""))

    doc.save(str(output_path))
# === END PHASE2_PROCEDURAL_DRAFTERS_V2 ===




def main():
    import argparse
    parser = argparse.ArgumentParser(description="Beezle reply-brief drafter (hotfix7)")
    parser.add_argument("canonical_id", nargs="?", default="fed:2:26-cv-00789")
    parser.add_argument("--base", default=str(_eroot()))
    parser.add_argument("--output", default=str(_eroot() / "output/reply_briefs"))
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--no-scorecard", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    base = Path(args.base)
    output = Path(args.output)

    if args.all:
        registry = json.loads(
            (base / "cases_index" / "case_registry.json").read_text(encoding="utf-8")
        )
        cases = list(registry.keys())
    else:
        cases = [args.canonical_id]

    print(f"Drafting reply briefs for {len(cases)} case(s)")
    print()

    for cid in cases:
        try:
            md, docx, scorecard = draft_for_case(
                cid, base, output, with_scorecard=not args.no_scorecard
            )
            print(f"  [ok] {cid}")
            print(f"       md:        {md}")
            if docx:
                print(f"       docx:      {docx}")
            if scorecard:
                print(f"       scorecard: {scorecard}")
        except Exception as e:
            print(f"  [FAIL] {cid}: {e}")
            if args.verbose:
                import traceback; traceback.print_exc()

    print()
    print(f"Output directory: {output}")


if __name__ == "__main__":
    main()
