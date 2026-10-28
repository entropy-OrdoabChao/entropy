"""
BLOCK_9A_MCP_REGISTRATION
Registers Block 9A skill ports as Beezle MCP tools.

Tools registered:
  beezle.violation_log         — log a judicial violation to persistent tracker
  beezle.violation_report      — report by judge / case / all
  beezle.jurisdiction_check    — federal Q / diversity / personal / removal
  beezle.four_gates            — jurisdiction → standing → cause → plausibility
  beezle.exhibit_add           — catalog evidence file as exhibit
  beezle.exhibit_list          — list all exhibits for a case
  beezle.exhibit_index         — generate markdown exhibit index
"""
from __future__ import annotations
from typing import Optional


def register_block_9a_tools(mcp) -> int:
    """Register all Block 9A tools. Returns count registered."""
    count = 0

    # ─── violation_tracker ────────────────────────────────────────
    @mcp.tool
    def violation_log(
        judge: str,
        case: str,
        violation: str,
        authority: str = "unspecified",
        severity: str = "MODERATE",
        source: str = "manual",
    ) -> dict:
        """Log a judicial violation to the persistent tracker.

        severity: MINOR | MODERATE | SERIOUS | EGREGIOUS
        Returns the logged entry with assigned id and timestamp.
        Persists to obsidian_sync/violation_tracker.json (queryable from Obsidian).
        """
        from regulatory.violation_tracker import log_violation
        return log_violation(judge, case, violation, authority, severity, source)
    count += 1

    @mcp.tool
    def violation_report(
        judge: Optional[str] = None,
        case: Optional[str] = None,
    ) -> dict:
        """Report violations from the persistent tracker.

        - If judge given: violations by that judge
        - If case given: violations in that case
        - If neither: aggregate report (counts by judge, by case)
        """
        from regulatory.violation_tracker import report_judge, report_case, report_all
        if judge:
            return report_judge(judge)
        if case:
            return report_case(case)
        return report_all()
    count += 1

    # ─── jurisdiction_check ────────────────────────────────────────
    @mcp.tool
    def jurisdiction_check(
        court_type: str = "federal",
        claim_arises_under: Optional[str] = None,
        diversity: Optional[bool] = None,
        amount_in_controversy: Optional[float] = None,
        has_federal_anchor_claim: bool = False,
        defendant_in_forum_state: bool = False,
        minimum_contacts: bool = False,
        long_arm_applies: bool = False,
        consent_or_waiver: bool = False,
        served_properly: bool = True,
        was_removed: bool = False,
        removal_within_30_days: bool = True,
        all_defendants_consented: bool = True,
        forum_defendant_rule_violated: bool = False,
    ) -> dict:
        """Check subject matter, personal, and removal jurisdiction.

        court_type: 'federal' | 'state'
        claim_arises_under: 'federal_law' | 'state_law' | 'constitution' | None
        Returns structured PASS/FAIL/UNCLEAR per check + overall + cited authority.
        """
        from pipeline.jurisdiction_check import run
        return run(
            court_type=court_type,
            claim_arises_under=claim_arises_under,
            diversity=diversity,
            amount_in_controversy=amount_in_controversy,
            has_federal_anchor_claim=has_federal_anchor_claim,
            defendant_in_forum_state=defendant_in_forum_state,
            minimum_contacts=minimum_contacts,
            long_arm_applies=long_arm_applies,
            consent_or_waiver=consent_or_waiver,
            served_properly=served_properly,
            was_removed=was_removed,
            removal_within_30_days=removal_within_30_days,
            all_defendants_consented=all_defendants_consented,
            forum_defendant_rule_violated=forum_defendant_rule_violated,
        )
    count += 1

    # ─── four_gates ────────────────────────────────────────────────
    @mcp.tool
    def four_gates(
        jurisdiction_inputs: dict,
        standing_inputs: dict,
        cause_inputs: dict,
        plausibility_inputs: dict,
        short_circuit: bool = True,
    ) -> dict:
        """Run sequential four-gate viability test.

        Required input shapes:
          jurisdiction_inputs: {court_type, claim_arises_under, ...}  (see jurisdiction_check)
          standing_inputs: {injury_in_fact, injury_concrete_particularized, causation_traceable,
                            redressable_by_court, injury_description}
          cause_inputs: {statute_or_common_law, elements_identified, elements_count, private_right_of_action}
          plausibility_inputs: {elements_satisfied, elements_total, facts_specific, threadbare_recitals_only}

        Returns {overall, failed_at, summary, gates:[{name,verdict,reason,authority,sub_checks}]}.
        """
        from pipeline.four_gates import run
        return run(jurisdiction_inputs, standing_inputs, cause_inputs, plausibility_inputs, short_circuit)
    count += 1

    # ─── exhibit_manager ───────────────────────────────────────────
    @mcp.tool
    def exhibit_add(
        case_id: str,
        source_path: str,
        title: Optional[str] = None,
        description: str = "",
    ) -> dict:
        """Catalog an evidence file as an exhibit.

        Auto-labels (A, B, C, ...), extracts text excerpt + page count,
        deduplicates by SHA-1, persists to cases/<case_id>/exhibits.json.
        """
        from generation.exhibit_manager import add_exhibit
        return add_exhibit(case_id, source_path, title, description)
    count += 1

    @mcp.tool
    def exhibit_list(case_id: str) -> list:
        """List all exhibits cataloged for a case."""
        from generation.exhibit_manager import list_exhibits
        return list_exhibits(case_id)
    count += 1

    @mcp.tool
    def exhibit_index(case_id: str) -> str:
        """Generate markdown exhibit index for a case (suitable for prepending to a brief)."""
        from generation.exhibit_manager import build_index_markdown
        return build_index_markdown(case_id)
    count += 1

    return count