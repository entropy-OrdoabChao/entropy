"""
regulatory/mcp_tools_phase7.py — Phase 7: Hermes Autonomy MCP tools.

PHASE_7_HERMES_AUTONOMY

Exposes:
  beezle.run_case(case_id, mode, options)  — single entry point for all legal work
  beezle.challenge_opponent(case_id)       — instant counter-strategy from audit

Hermes calls these. Nothing else for legal work.
Hermes = router + presenter. Beezle = only legal brain.
"""
from __future__ import annotations


def register_phase7_tools(mcp) -> int:
    count = 0

    @mcp.tool
    def run_case(
        case_id: str,
        mode: str = "strategy",
        options: dict = None,
    ) -> dict:
        """Single entry point for all legal work. Hermes calls this, nothing else.

        case_id: canonical case ID (e.g. 'fed:2:26-cv-00123')
        mode: 'strategy' | 'draft' | 'bundle'
          - strategy: returns strategy + prioritized actions only
          - draft: strategy + generates draft motion/brief
          - bundle: strategy + draft + finalized filing package

        options: optional overrides
          - posture_override: 'enforcement' | 'merits' | 'defensive' | 'appellate'
          - include_case_law: bool (default True)
          - max_theories: int

        Returns structured output with posture, gates, tiered actions, authority summary.
        """
        options = options or {}

        from pipeline.reasoning_orchestrator import build_strategy, save_strategy

        # Build strategy (deterministic — no LLM)
        strat = build_strategy(
            case_id,
            include_case_law=options.get("include_case_law", True),
        )

        if "error" in strat:
            return {"ok": False, "error": strat["error"]}

        save_strategy(strat)

        # Build output contract
        pri = strat.get("prioritized", {})
        result = {
            "ok": True,
            "case_id": case_id,
            "caption": strat.get("caption", case_id),
            "posture": strat.get("posture", "unknown"),
            "gates": strat.get("gates", {}),
            "theories": strat.get("theories", []),
            "actions": {
                "primary": pri.get("primary", []),
                "secondary": pri.get("secondary", []),
                "reserve": pri.get("reserve", []),
            },
            "defects": strat.get("audit_attacks", {}),
            "defense_exposure": strat.get("defense_sim", {}),
            "strategy_summary": strat.get("strategy_summary", ""),
            "strategy_path": str(strat.get("_saved_json", "")),
            "mode": mode,
        }

        if mode == "strategy":
            return result

        # Draft mode: generate motion documents
        if mode in ("draft", "bundle"):
            try:
                from generation.baseline_memorandum import build_memo
                from pipeline.phase4_runner import theories_to_authorities, theories_to_duties

                theories_raw = strat.get("_theories_raw", [])
                if not theories_raw:
                    # Reconstruct from scenario decompose
                    from pipeline.case_loader import load_case
                    from pipeline.scenario_engine import scenario_decompose
                    case = load_case(case_id)
                    if case:
                        decomp = scenario_decompose(
                            case.get("facts", []),
                            context={"target_court": case.get("court", "EDPA")},
                            max_depth=1, min_confidence="low"
                        )
                        theories_raw = decomp.get("theories", [])

                auths = theories_to_authorities(theories_raw)
                duties = theories_to_duties(theories_raw)

                memo = build_memo(
                    case_id=case_id,
                    caption=strat.get("caption", case_id),
                    controlling_authorities=auths,
                    mandatory_duties=duties,
                    write_markdown=True,
                    write_docx=True,
                )
                result["draft"] = {
                    "markdown_path": memo.get("markdown_path", ""),
                    "docx_path": memo.get("docx_path", ""),
                    "violation_ids": memo.get("violation_ids", []),
                }
            except Exception as e:
                result["draft"] = {"error": str(e)}

        if mode == "bundle":
            # Bundle: include violation scorecard
            try:
                from pipeline.procedural_attack_builder import load_and_scan_audit
                is_fed = "fed" in case_id
                audit = load_and_scan_audit(case_id, "federal" if is_fed else "state_pa")
                result["audit_detail"] = {
                    "total": audit.get("total", 0),
                    "fatal": audit.get("fatal_count", 0),
                    "top_5_defects": [
                        {
                            "severity": d["severity"],
                            "category": d["category"],
                            "defect": d["defect"][:150],
                            "motion": d["recommended_motion"],
                        }
                        for d in audit.get("defects", [])[:5]
                    ],
                }
            except Exception as e:
                result["audit_detail"] = {"error": str(e)}

        return result
    count += 1

    @mcp.tool
    def challenge_opponent(
        case_id: str,
        court_type: str = "federal",
    ) -> dict:
        """Instant counter-strategy from audit data.

        Scans opposing filings for procedural/authority defects and returns
        prioritized motions to file.

        case_id: canonical case ID
        court_type: 'federal' | 'state_pa'

        Returns {defects, motions_to_file, authority_validity_summary}.
        """
        from pipeline.procedural_attack_builder import load_and_scan_audit

        audit = load_and_scan_audit(case_id, court_type)

        if audit.get("error"):
            return {"ok": False, "error": audit["error"]}

        # Consolidate defects into distinct motions
        motion_groups = {}
        for d in audit.get("defects", []):
            motion = d.get("recommended_motion", "Review")
            if motion not in motion_groups:
                motion_groups[motion] = {
                    "motion": motion,
                    "count": 0,
                    "fatal": 0,
                    "serious": 0,
                    "authorities": set(),
                    "sample_defects": [],
                }
            mg = motion_groups[motion]
            mg["count"] += 1
            if d.get("severity") == "FATAL":
                mg["fatal"] += 1
            elif d.get("severity") == "SERIOUS":
                mg["serious"] += 1
            for a in d.get("authority", []):
                mg["authorities"].add(a)
            if len(mg["sample_defects"]) < 3:
                mg["sample_defects"].append(d.get("defect", "")[:150])

        # Sort by fatal count descending
        motions_list = sorted(motion_groups.values(), key=lambda x: (-x["fatal"], -x["count"]))
        for m in motions_list:
            m["authorities"] = list(m["authorities"])

        return {
            "ok": True,
            "case_id": case_id,
            "total_defects": audit.get("total", 0),
            "fatal_defects": audit.get("fatal_count", 0),
            "motions_to_file": motions_list,
            "summary": audit.get("summary", ""),
        }
    count += 1

    return count