"""
pipeline/reasoning_orchestrator.py — Block 10: Thin Reasoning Layer.

BLOCK_10_REASONING_ORCHESTRATOR

Wires ALL pipeline components into a single deterministic strategy builder.
NO LLM. Pure rule evaluation + component orchestration.

Flow:
  load_case → scenario_decompose → jurisdiction_check → four_gates →
  authority_discovery → authority_validity → defense_simulator →
  procedural_attack (audit) → decide() → strategy_summary

decide() encodes IF/THEN rules:
  - default_judgment + FFC=true → enforce (writ/garnishment)
  - default_judgment + FFC=false → vacate (60(b)(4))
  - removal_defective → remand
  - insurer + judgment → trigger bad faith + indemnity
  - non-binding as controlling → strike + reconsideration
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

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

log = get_logger("reasoning")


# ======================================================================
# DECIDE() — Deterministic action rules
# ======================================================================

DECISION_RULES = [
    {
        "id": "R001",
        "name": "enforce_default_judgment",
        "condition": lambda ctx: (
            "default" in (ctx.get("status") or "").lower() and
            ctx.get("ffc_applies", True) and
            ctx.get("jurisdiction_pass", False)
        ),
        "action": "Enforce judgment — file writ of execution / garnishment",
        "authority": ["Pa.R.C.P. 3101 et seq.", "FRCP 69"],
        "priority": 1,
    },
    {
        "id": "R002",
        "name": "vacate_void_judgment",
        "condition": lambda ctx: (
            # VACATE_TRIGGER_V2: a void ORDER or judgment is vacatable under
            # 60(b)(4). Fire when the by-the-book void determination found a
            # ground (ffc_applies False) and the court has entered an order.
            not ctx.get("ffc_applies", True) and
            (ctx.get("has_judgment", False) or ctx.get("has_dispositive_order", False))
        ),
        "action": "Vacate void judgment — Motion under FRCP 60(b)(4) or Pa.R.C.P. 3051",
        "authority": ["FRCP 60(b)(4)", "U.S. Const. Amend. XIV", "42 Pa.C.S. 5505"],
        "priority": 1,
    },
    {
        "id": "R003",
        "name": "remand_defective_removal",
        "condition": lambda ctx: (
            "removed" in (ctx.get("status") or "").lower() and
            ctx.get("removal_defective", False)
        ),
        "action": "File Motion to Remand — removal procedurally defective",
        "authority": ["28 U.S.C. 1447(c)", "28 U.S.C. 1446"],
        "priority": 1,
    },
    {
        "id": "R004",
        "name": "bad_faith_insurance",
        "condition": lambda ctx: (
            ctx.get("case_type") in ("bad_faith", "insurance") and
            ctx.get("has_judgment", False) and
            any("insur" in d.lower() for d in ctx.get("defendants", []))
        ),
        "action": "Trigger bad faith + indemnity demand under 42 Pa.C.S. 8371",
        "authority": ["42 Pa.C.S. 8371", "Terletsky v. Prudential, 649 A.2d 680 (Pa. Super. 1994)"],
        "priority": 2,
    },
    {
        "id": "R005",
        "name": "strike_non_binding",
        "condition": lambda ctx: ctx.get("non_binding_count", 0) > 0,
        "action": "File Motion to Strike non-binding authority cited as controlling",
        "authority": ["FRCP 12(f)", "3d Cir. R. 28.3"],
        "priority": 3,
    },
    {
        "id": "R006",
        "name": "reconsider_trial_as_appellate",
        "condition": lambda ctx: ctx.get("trial_as_appellate_count", 0) > 0,
        "action": "File Motion for Reconsideration — trial court exceeded authority",
        "authority": ["28 U.S.C. 1651 (All Writs)", "FRCP 60(b)(4)"],
        "priority": 2,
    },
    {
        "id": "R007",
        "name": "challenge_filing_restriction",
        "condition": lambda ctx: ctx.get("filing_restriction_detected", False),
        "action": "Challenge filing restriction — First Amendment Petition Clause + Open Courts",
        "authority": [
            "U.S. Const. Amend. I (Petition Clause)",
            "Pa. Const. Art. I Sec. 11 (Open Courts)",
            "In re Oliver, 682 F.2d 443 (3d Cir. 1982)",
        ],
        "priority": 1,
    },
    {
        "id": "R008",
        "name": "garnishee_default",
        "condition": lambda ctx: (
            "garnishee" in " ".join(ctx.get("facts", [])).lower() and
            "default" in " ".join(ctx.get("facts", [])).lower()
        ),
        "action": "Enter judgment against garnishee in default — Pa.R.C.P. 3146(a)",
        "authority": ["Pa.R.C.P. 3146(a)", "Pa.R.C.P. 3111"],
        "priority": 2,
    },
    {
        "id": "R010",
        "name": "certify_54b",
        "condition": lambda ctx: (
            # FRCP 54(b): a partial dispositive order disposing of fewer than all
            # claims/parties, where appellate review is sought of that partial
            # disposition. Fires when there is a dispositive order AND an appeal
            # is pending/awaiting whole-case certification.
            ctx.get("has_dispositive_order", False) and
            ctx.get("appeal_pending", False)
        ),
        "action": "File Motion for Entry of Final Judgment under FRCP 54(b) — certify partial disposition for appeal",
        "authority": [
            "Fed. R. Civ. P. 54(b)",
            "Curtiss-Wright Corp. v. General Electric Co., 446 U.S. 1 (1980)",
            "Sears, Roebuck & Co. v. Mackey, 351 U.S. 427 (1956)",
        ],
        "priority": 1,
    },
    {
        "id": "R011",
        "name": "interlocutory_1292b",
        "condition": lambda ctx: (
            # 28 USC 1292(b): controlling question of law, substantial ground for
            # difference of opinion, immediate appeal materially advances the
            # litigation. Alternative appellate vehicle where 54(b) does not fit.
            ctx.get("has_dispositive_order", False) and
            ctx.get("appeal_pending", False)
        ),
        "action": "File Motion to Certify Order for Interlocutory Appeal under 28 U.S.C. 1292(b)",
        "authority": [
            "28 U.S.C. 1292(b)",
            "Katz v. Carte Blanche Corp., 496 F.2d 747 (3d Cir. 1974) (en banc)",
        ],
        "priority": 2,
    },
    {
        "id": "R012",
        "name": "writ_of_mandamus",
        "condition": lambda ctx: (
            # FRAP 21 / All Writs Act: extraordinary relief where the court has a
            # clear duty to act and there is no other adequate means. Fires where a
            # void/due-process ground exists AND the trial court has refused or
            # failed to correct it (no adequate alternative remedy).
            bool(ctx.get("void_grounds")) and
            (ctx.get("court_refused_to_act", False) or ctx.get("trial_as_appellate_count", 0) > 0)
        ),
        "action": "File Petition for Writ of Mandamus (FRAP 21) — compel correction of jurisdictional/due-process defect",
        "authority": [
            "Fed. R. App. P. 21",
            "28 U.S.C. 1651 (All Writs Act)",
            "Cheney v. U.S. Dist. Court, 542 U.S. 367 (2004)",
            "In re School Asbestos Litig., 977 F.2d 764 (3d Cir. 1992)",
        ],
        "priority": 2,
    },
    {
        "id": "R013",
        "name": "entry_of_default",
        "condition": lambda ctx: ctx.get("default_available", False),
        "action": "File Request/Motion for Entry of Default — defendant failed to plead or otherwise defend (FRCP 55(a))",
        "authority": ["Fed. R. Civ. P. 55(a)"],
        "priority": 1,
    },
    {
        "id": "R014",
        "name": "default_judgment",
        "condition": lambda ctx: ctx.get("default_available", False),
        "action": "File Motion for Default Judgment — entry of default obtained (FRCP 55(b))",
        "authority": ["Fed. R. Civ. P. 55(b)(1)", "Fed. R. Civ. P. 55(b)(2)"],
        "priority": 1,
    },
    {
        "id": "R015",
        "name": "compel_discovery",
        "condition": lambda ctx: ctx.get("discovery_unanswered", False),
        "action": "File Motion to Compel Discovery — opposing party failed to respond (FRCP 37)",
        "authority": ["Fed. R. Civ. P. 37(a)", "Fed. R. Civ. P. 26"],
        "priority": 2,
    },
    {
        "id": "R016",
        "name": "sanctions",
        "condition": lambda ctx: ctx.get("frivolous_or_abuse", False),
        "action": "File Motion for Sanctions — frivolous filing or discovery abuse (FRCP 11 / 28 U.S.C. 1927)",
        "authority": ["Fed. R. Civ. P. 11(c)", "28 U.S.C. 1927", "Fed. R. Civ. P. 37(b)"],
        "priority": 3,
    },
    {
        "id": "R017",
        "name": "disqualify_recuse",
        "condition": lambda ctx: ctx.get("bias_detected", False),
        "action": "File Motion to Disqualify/Recuse — appearance of partiality (28 U.S.C. 455 / 144)",
        "authority": [
            "28 U.S.C. 455(a)",
            "28 U.S.C. 144",
            "Liljeberg v. Health Servs. Acquisition Corp., 486 U.S. 847 (1988)",
        ],
        "priority": 2,
    },
    {
        "id": "R018",
        "name": "summary_judgment",
        "condition": lambda ctx: ctx.get("no_genuine_dispute", False),
        "action": "File Motion for Summary Judgment — no genuine dispute of material fact (FRCP 56)",
        "authority": [
            "Fed. R. Civ. P. 56",
            "Celotex Corp. v. Catrett, 477 U.S. 317 (1986)",
            "Anderson v. Liberty Lobby, Inc., 477 U.S. 242 (1986)",
        ],
        "priority": 2,
    },
    {
        "id": "R009",
        "name": "four_gates_fail",
        "condition": lambda ctx: ctx.get("gates_overall") == "FAIL",
        "action": "HOLD — claim fails viability gate, do not file until resolved",
        "authority": [],
        "priority": 0,
    },
    # === APPELLATE_RULES_V1: fire only when the case is on appeal. These match
    # the appellate stub set (reinstate/recall/FRAP40/cert/60d3). posture is
    # injected into ctx as ctx["posture"]; build_strategy sets it. ===
    {
        "id": "R020",
        "name": "reinstate_appeal",
        "condition": lambda ctx: ctx.get("posture") == "appellate",
        "action": "File Motion to Reinstate Appeal — restore dismissed/defaulted appeal",
        "authority": ["FRAP 3", "FRAP 4", "3d Cir. L.A.R. 107.2"],
        "priority": 1,
    },
    {
        "id": "R021",
        "name": "recall_mandate",
        "condition": lambda ctx: ctx.get("posture") == "appellate",
        "action": "File Motion to Recall Mandate — exceptional circumstances / fraud on court",
        "authority": ["Calderon v. Thompson, 523 U.S. 538 (1998)", "FRAP 41"],
        "priority": 2,
    },
    {
        "id": "R022",
        "name": "frap_40_panel",
        "condition": lambda ctx: ctx.get("posture") == "appellate",
        "action": "File Petition for Panel Rehearing under FRAP 40",
        "authority": ["FRAP 40", "3d Cir. L.A.R. 35"],
        "priority": 2,
    },
    {
        "id": "R023",
        "name": "frap_40_en_banc",
        "condition": lambda ctx: ctx.get("posture") == "appellate",
        "action": "File Petition for Rehearing En Banc under FRAP 35",
        "authority": ["FRAP 35", "3d Cir. L.A.R. 35.1"],
        "priority": 3,
    },
    {
        "id": "R024",
        "name": "cert_petition",
        "condition": lambda ctx: ctx.get("posture") == "appellate",
        "action": "File Petition for Writ of Certiorari to the Supreme Court",
        "authority": ["28 U.S.C. 1254(1)", "Sup. Ct. R. 10"],
        "priority": 3,
    },
    {
        "id": "R025",
        "name": "rule_60d3_fraud",
        "condition": lambda ctx: (
            ctx.get("posture") == "appellate" and not ctx.get("ffc_applies", True)
        ),
        "action": "File Independent Action for Fraud on the Court under FRCP 60(d)(3)",
        "authority": ["FRCP 60(d)(3)", "Hazel-Atlas Glass Co. v. Hartford-Empire Co., 322 U.S. 238 (1944)"],
        "priority": 2,
    },

]


def decide(context: dict) -> list[dict]:
    """Evaluate all decision rules against case context.
    Returns list of triggered actions, sorted by priority (1=highest).
    """
    triggered = []
    for rule in DECISION_RULES:
        try:
            if rule["condition"](context):
                triggered.append({
                    "rule_id": rule["id"],
                    "name": rule["name"],
                    "action": rule["action"],
                    "authority": rule["authority"],
                    "priority": rule["priority"],
                })
        except Exception as e:
            log.warning("decide", f"Rule {rule['id']} eval failed: {e}")
    triggered.sort(key=lambda x: x["priority"])
    return triggered


# ======================================================================
# BUILD STRATEGY — the main orchestrator
# ======================================================================
# MOTION PRIORITIZATION
# ======================================================================

def prioritize(actions, audit_defects=None):
    """Sort actions into PRIMARY / SECONDARY / RESERVE tiers."""
    primary, secondary, reserve = [], [], []
    seen = set()
    for a in actions:
        mk = a.get("name", "")
        if mk in seen:
            continue
        seen.add(mk)
        pri = a.get("priority", 5)
        if pri <= 1:
            primary.append(a)
        elif pri == 2:
            secondary.append(a)
        else:
            reserve.append(a)
    defect_counts = {}
    if audit_defects:
        for d in audit_defects:
            cat = d.get("category", "OTHER")
            defect_counts[cat] = defect_counts.get(cat, 0) + 1
    return {"primary": primary, "secondary": secondary, "reserve": reserve,
            "total_actions": len(primary) + len(secondary) + len(reserve),
            "defect_consolidation": defect_counts}


# ======================================================================
# POSTURE LOCK
# ======================================================================

POSTURE_ALLOWED = {
    # Trial-court active litigation (motions to dismiss, summary judgment, discovery)
    "merits": {"blocked": []},
    # Post-judgment enforcement (default judgments, garnishment, contempt)
    "enforcement": {"blocked": ["vacate_void_judgment", "remand_defective_removal"]},
    # Defensive posture (responding to opponent's dispositive motion)
    "defensive": {"blocked": ["enforce_default_judgment", "garnishee_default"]},
    # Case on appeal — trial-court merits motions blocked, appellate vehicles allowed
    "appellate": {"blocked": ["enforce_default_judgment", "garnishee_default",
                              "summary_judgment_merits", "summary_judgment",
                              "certify_54b", "entry_of_default", "default_judgment",
                              "compel_discovery", "interlocutory_1292b",
                              "remand_defective_removal", "bad_faith_insurance",
                              "garnishee_default", "challenge_filing_restriction",
                              "strike_non_binding",           # FRCP 12(f): pleadings-only, no appellate analog
                              "reconsider_trial_as_appellate"]},  # trial reconsideration superseded once on appeal
                              # NOTE: disqualify_recuse stays — 28 U.S.C. 455 reaches appellate judges
    # Original-jurisdiction mandamus pending in appellate court
    "mandamus": {"blocked": ["enforce_default_judgment", "garnishee_default"]},
}

def detect_posture(case):
    """Determine case posture from registry status string.

    Buckets (specific → general):
      - appellate    : on appeal at any level
      - mandamus     : original-jurisdiction mandamus pending (not collateral)
      - enforcement  : post-judgment collection / default / garnishment
      - defensive    : responding to opponent's dispositive motion
      - merits       : trial-level active litigation (default)
    """
    status = (case.get("status") or "").lower()
    # Order matters — check specific buckets first
    if "appellate" in status or "appeal" in status:
        return "appellate"
    # Pure mandamus (not "merits_mandamus_pending" which is a merits case with collateral mandamus)
    if "mandamus" in status and not status.startswith("merits"):
        return "mandamus"
    if any(k in status for k in ["default", "judgment", "enforc", "garnish"]):
        return "enforcement"
    if any(k in status for k in ["defending", "opposing_mtd", "responding"]):
        return "defensive"
    return "merits"
def apply_posture_lock(actions, posture):
    """Filter actions that contradict current posture."""
    blocked = set(POSTURE_ALLOWED.get(posture, {}).get("blocked", []))
    filtered = [a for a in actions if a.get("name") not in blocked]
    nb = len(actions) - len(filtered)
    if nb > 0:
        log.stage("posture_lock", f"Posture={posture}: blocked {nb} contradictory actions")
    return filtered


# ======================================================================

def _compute_default_timeline(case_id: str) -> dict:
    """Run the docket-driven default timeline (FRCP 81(c)). Returns {} if no
    parsed docket. Agnostic: reads the case's docket + defendant list."""
    try:
        import json as _json
        from pipeline.default_timeline import analyze_defaults
        from pipeline.case_loader import load_case as _lc
        dk = (_eroot() / "_docket_parsed.json")
        if not dk.exists():
            return {}
        docket = _json.loads(dk.read_text(encoding="utf-8"))
        _case = _lc(case_id) or {}
        parties = _case.get("parties", []) or []
        # extract defendant name fragments (skip plaintiff)
        defs = [str(p).split()[0].lower() for p in parties
                if "filer" not in str(p).lower()] or \
               ["transunion","greensky","experian","equifax","capital","avant"]
        amd = (_case.get("latest_order") or {}).get("amended_complaint_date")
        return analyze_defaults(docket, defs, amendment_date=amd)
    except Exception as _e:
        log.info(f"default timeline skipped: {_e}")
        return {}


def build_strategy(case_id: str, include_case_law: bool = True) -> dict:
    """Build complete litigation strategy for a case.

    Orchestrates: load_case → scenario → jurisdiction → four_gates →
    authority_discovery → authority_validity → defense_sim → procedural_attack → decide()

    Returns structured strategy dict with all components + recommended actions.
    """
    t0 = time.time()
    from pipeline.case_loader import load_case
    from pipeline.scenario_engine import scenario_decompose

    # ── Load case ──
    case = load_case(case_id)
    if not case:
        return {"error": f"Case not found: {case_id}"}

    facts = case.get("facts", [])
    court = case.get("court", "EDPA" if "fed" in case_id else "PA_CCP")
    status = case.get("status", "")
    case_type = case.get("case_type", "")
    defendants = case.get("parties", [])
    is_federal = "fed" in case_id or "EDPA" in court

    strategy = {
        "case_id": case_id,
        "caption": case.get("caption", case_id),
        "court": court,
        "status": status,
        "case_type": case_type,
    }

    # ── Step 1: Scenario Decompose ──
    log.stage("strategy", f"Step 1: scenario decompose ({len(facts)} facts)")
    try:
        decomp = scenario_decompose(facts, context={"target_court": court}, max_depth=1, min_confidence="low")
        theories = decomp.get("theories", [])
        strategy["theories"] = [{
            "name": t.get("display_name", t.get("name")),
            "confidence": t.get("confidence"),
            "matched": len(t.get("matched_elements", [])),
            "total": len(t.get("core_elements", [])),
            "seed_nodes": t.get("seed_nodes", []),
        } for t in theories]
    except Exception as e:
        log.warning("strategy", f"Scenario decompose failed: {e}")
        theories = []
        strategy["theories"] = []

    # ── Step 2: Jurisdiction Check ──
    log.stage("strategy", "Step 2: jurisdiction check")
    try:
        from pipeline.jurisdiction_check import run as run_jurisdiction
        j = run_jurisdiction(
            court_type="federal" if is_federal else "state",
            claim_arises_under="federal_law" if is_federal else "state_law",
            defendant_in_forum_state=True,
            was_removed="removed" in status.lower(),
        )
        strategy["jurisdiction"] = {
            "overall": j["overall"],
            "subject_matter": j["subject_matter"],
            "personal": j["personal"],
            "removal": j["removal"],
            "blocking": j["blocking_issues"],
        }
    except Exception as e:
        log.warning("strategy", f"Jurisdiction check failed: {e}")
        strategy["jurisdiction"] = {"overall": "UNCLEAR", "error": str(e)}

    # ── Step 3: Four Gates ──
    log.stage("strategy", "Step 3: four gates")
    try:
        from pipeline.four_gates import run as run_gates
        matched_count = sum(len(t.get("matched_elements", [])) for t in theories)
        total_count = sum(len(t.get("core_elements", [])) for t in theories)
        top_theory = theories[0] if theories else {}

        gates = run_gates(
            jurisdiction_inputs={
                "court_type": "federal" if is_federal else "state",
                "claim_arises_under": "federal_law" if is_federal else "state_law",
                "defendant_in_forum_state": True,
                "was_removed": "removed" in status.lower(),
            },
            standing_inputs={
                "injury_in_fact": True,
                "injury_concrete_particularized": True,
                "causation_traceable": True,
                "redressable_by_court": True,
                "injury_description": case_type,
            },
            cause_inputs={
                "statute_or_common_law": top_theory.get("display_name", case_type),
                "elements_identified": total_count > 0,
                "elements_count": total_count,
                "private_right_of_action": True,
            },
            plausibility_inputs={
                "elements_satisfied": matched_count,
                "elements_total": total_count,
                "facts_specific": True,
                "threadbare_recitals_only": False,
            },
        )
        strategy["gates"] = {
            "overall": gates["overall"],
            "failed_at": gates.get("failed_at"),
            "summary": gates["summary"],
        }
    except Exception as e:
        log.warning("strategy", f"Four gates failed: {e}")
        strategy["gates"] = {"overall": "UNCLEAR", "error": str(e)}

    # ── Step 4: Authority Validity (from audit) ──
    log.stage("strategy", "Step 4: authority validity from audit")
    try:
        from pipeline.procedural_attack_builder import load_and_scan_audit
        audit_attacks = load_and_scan_audit(case_id, "federal" if is_federal else "state_pa")
        strategy["audit_attacks"] = {
            "total": audit_attacks.get("total", 0),
            "fatal": audit_attacks.get("fatal_count", 0),
            "by_category": audit_attacks.get("by_category", {}),
            "by_severity": audit_attacks.get("by_severity", {}),
            "summary": audit_attacks.get("summary", ""),
        }
        # Extract counts for decide()
        non_binding = sum(1 for d in audit_attacks.get("defects", []) if d.get("category") == "AUTHORITY")
        trial_appellate = sum(1 for d in audit_attacks.get("defects", []) if "exceeded" in d.get("defect", "").lower())
    except Exception as e:
        log.warning("strategy", f"Audit attack scan failed: {e}")
        strategy["audit_attacks"] = {"total": 0, "error": str(e)}
        non_binding = 0
        trial_appellate = 0

    # ── Step 5: Defense Simulator ──
    log.stage("strategy", "Step 5: defense simulator")
    try:
        from pipeline.defense_simulator import simulate
        from pipeline.phase4_runner import theories_to_elements, theories_to_authorities

        elements = theories_to_elements(theories)
        authority_cites = [a["citation"] for a in theories_to_authorities(theories)]

        sim = simulate(
            elements=elements,
            authorities=authority_cites,
            facts=facts[:15],
            target_jurisdiction="EDPA" if is_federal else "PA_CCP",
        )
        strategy["defense_sim"] = {
            "total": sim["total"],
            "blocking": sim["blocking_count"],
            "ready": sim["ready_for_filing"],
            "by_strength": sim["by_strength"],
        }
    except Exception as e:
        log.warning("strategy", f"Defense sim failed: {e}")
        strategy["defense_sim"] = {"total": 0, "error": str(e)}

    # ── Step 5B: LEARNING_LOOP — pattern insights from past cases ──
    log.stage("strategy", "Step 5B: learning loop")
    try:
        from obsidian_sync.feedback_loop import get_pre_draft_insights
        insights = get_pre_draft_insights(
            intent="strategy",
            doc_type=case_type,
            violations=[],
            subject_areas=[t.get("name", "") for t in theories[:5]],
            facts=facts[:15],
        )
        strategy["learning_loop"] = {
            "warnings": insights.get("warnings", []),
            "suggested_authority": insights.get("suggested_authority", []),
            "weakness_alerts": insights.get("weakness_alerts", []),
            "pattern_count": insights.get("pattern_count", 0),
            "confidence": insights.get("confidence", 0.0),
        }
        if insights.get("warnings"):
            log.stage("strategy", f"Learning loop: {len(insights['warnings'])} warnings from past cases")
    except Exception as e:
        strategy["learning_loop"] = {"error": str(e)}
        log.warning("strategy", f"Learning loop failed (non-fatal): {e}")

    # ── Step 6: Decide ──
    log.stage("strategy", "Step 6: decide()")
    # VOID_DETERMINATION_V1: derive ffc by-the-book (Espinosa) from this case's
    # computed verdicts, instead of assuming judgments are valid.
    try:
        from pipeline.void_determination import ffc_applies as _void_ffc, determine_void_grounds as _void_grounds
        _jur = strategy.get("jurisdiction") or {}
        _gates = strategy.get("gates") or {}
        # standing fails only if four_gates failed AT the standing gate
        _standing = "FAIL" if str(_gates.get("failed_at") or "").lower() == "standing" else "PASS"
        _defects = audit_attacks.get("defects", []) if isinstance(audit_attacks, dict) else []
        _ffc = _void_ffc(_jur, _standing, _defects)
        _vg = _void_grounds(_jur, _standing, _defects)
        strategy["void_grounds"] = _vg
        if _vg:
            log.stage("strategy", f"VOID grounds: {[g['ground'] for g in _vg]} -> ffc_applies={_ffc}")
    except Exception as _ve:
        log.warning("strategy", f"void determination failed (non-fatal, default ffc=True): {_ve}")
        _ffc = True
        strategy["void_grounds"] = []
    posture = detect_posture(case)  # APPELLATE_RULES_V1: compute before decide so rules can branch
    decision_context = {
        "posture": posture,
        "status": status,
        "case_type": case_type,
        "court": court,
        "defendants": defendants,
        "facts": facts,
        "has_judgment": "judgment" in status.lower() or "default" in status.lower(),
        "has_dispositive_order": bool((case.get("latest_order") or {}).get("dkt_no"))
                                 or bool(strategy.get("void_grounds")),
        "appeal_pending": bool(case.get("appeals")),
        "void_grounds": strategy.get("void_grounds", []),
        "court_refused_to_act": any(
            ("refus" in f.lower() or "denied" in f.lower() or "exceeded" in f.lower())
            for f in facts
        ),
        "default_available": any(
            ("default" in f.lower() or "failed to plead" in f.lower()
             or "failure to plead" in f.lower() or "failed to defend" in f.lower())
            for f in facts
        ),
        "discovery_unanswered": any(
            ("compel" in f.lower() or "discovery" in f.lower() or "interrogator" in f.lower()
             or "failed to respond" in f.lower())
            for f in facts
        ),
        "frivolous_or_abuse": any(
            ("frivolous" in f.lower() or "sanction" in f.lower() or "bad faith" in f.lower())
            for f in facts
        ),
        "bias_detected": any(
            (str(d.get("category","")).upper() == "JURISDICTION" and "bias" in str(d.get("defect","")).lower())
            or "bias" in str(d.get("defect","")).lower()
            for d in (audit_attacks.get("defects", []) if isinstance(audit_attacks, dict) else [])
        ),
        "no_genuine_dispute": False,  # set post-hoc from default below
        # DEFAULT_TIMELINE_V1: by-the-book FRCP 81(c) default determination
        **(lambda: (
            (lambda _dt_res: {
                "default_available": bool(_dt_res.get("defaulted")),
                "default_detail": _dt_res,
            })(_compute_default_timeline(case_id))
        ))(),
        "ffc_applies": _ffc,  # VOID_DETERMINATION_V1: by-the-book Espinosa test
        "jurisdiction_pass": strategy.get("jurisdiction", {}).get("overall") == "PASS",
        "removal_defective": strategy.get("jurisdiction", {}).get("removal") == "FAIL",
        "gates_overall": strategy.get("gates", {}).get("overall"),
        "non_binding_count": non_binding,
        "trial_as_appellate_count": trial_appellate,
        "filing_restriction_detected": any("restrict" in f.lower() or "no longer file" in f.lower() for f in facts),
    }
    raw_actions = decide(decision_context)
    strategy["posture"] = posture
    locked = apply_posture_lock(raw_actions, posture)
    ad = audit_attacks.get("defects", []) if isinstance(audit_attacks, dict) else []
    pri = prioritize(locked, ad)
    strategy["prioritized"] = pri
    strategy["recommended_actions"] = locked
    log.stage("strategy", f"Triggered {len(raw_actions)}, posture={posture}, P={len(pri['primary'])}/S={len(pri['secondary'])}/R={len(pri['reserve'])}")

    # ── Step 7: Strategy Summary (1-pager) ──
    strategy["strategy_summary"] = _build_summary(strategy)

    elapsed = int((time.time() - t0) * 1000)
    strategy["elapsed_ms"] = elapsed
    log.stage("strategy", f"Strategy complete: {len(locked)} actions, posture={posture}, {elapsed}ms")

    return strategy


def _build_summary(s: dict) -> str:
    """Generate 1-page strategy summary text."""
    lines = [
        f"# STRATEGY SUMMARY — {s.get('caption', s['case_id'])}",
        f"Court: {s.get('court', '?')} | Status: {s.get('status', '?')} | Type: {s.get('case_type', '?')}",
        "",
        "## JURISDICTION",
        f"  Overall: {s.get('jurisdiction', {}).get('overall', '?')}",
    ]
    blocking = s.get("jurisdiction", {}).get("blocking", [])
    if blocking:
        for b in blocking:
            lines.append(f"  BLOCKED: {b}")

    lines.append("")
    lines.append("## VIABILITY (Four Gates)")
    lines.append(f"  Overall: {s.get('gates', {}).get('overall', '?')}")
    lines.append(f"  {s.get('gates', {}).get('summary', '')}")

    lines.append("")
    lines.append("## THEORIES")
    for t in s.get("theories", [])[:5]:
        lines.append(f"  - {t['name']} (conf={t['confidence']}, elements={t['matched']}/{t['total']})")

    lines.append("")
    lines.append("## OPPOSING FILING DEFECTS")
    aa = s.get("audit_attacks", {})
    lines.append(f"  Total: {aa.get('total', 0)} | Fatal: {aa.get('fatal', 0)}")
    lines.append(f"  {aa.get('summary', '')}")

    lines.append("")
    lines.append("## DEFENSE EXPOSURE")
    ds = s.get("defense_sim", {})
    lines.append(f"  Attacks: {ds.get('total', 0)} | Blocking: {ds.get('blocking', 0)} | Ready: {ds.get('ready', '?')}")

    lines.append("")
    lines.append(f"## POSTURE: {s.get('posture', '?').upper()}")
    pri = s.get("prioritized", {})
    if pri.get("primary"):
        lines.append("")
        lines.append("## PRIMARY ACTIONS (file now)")
        for a in pri["primary"]:
            lines.append(f"  >> {a['action']}")
            if a.get("authority"):
                lines.append(f"     Authority: {', '.join(a['authority'])}")
    if pri.get("secondary"):
        lines.append("")
        lines.append("## SECONDARY ACTIONS (support / backup)")
        for a in pri["secondary"]:
            lines.append(f"  > {a['action']}")
            if a.get("authority"):
                lines.append(f"    Authority: {', '.join(a['authority'])}")
    if pri.get("reserve"):
        lines.append("")
        lines.append("## RESERVE (hold for appeal)")
        for a in pri["reserve"]:
            lines.append(f"  - {a['action']}")

    return "\n".join(lines)


# BEEZLE_FN_UNUSED 2026-06-13: save_strategy (no static refs in pipeline/)
def save_strategy(strategy: dict) -> Path:
    """Save strategy to output/ as JSON + markdown summary."""
    safe = strategy["case_id"].replace(":", "_").replace("/", "_")
    out_dir = (_BEEZLE_ROOT / "output")
    out_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_dir / f"strategy_{safe}.json"
    json_path.write_text(json.dumps(strategy, indent=2, default=str), encoding="utf-8")

    md_path = out_dir / f"strategy_{safe}.md"
    md_path.write_text(strategy.get("strategy_summary", ""), encoding="utf-8")

    log.info(f"saved strategy: {json_path}, {md_path}")
    return json_path