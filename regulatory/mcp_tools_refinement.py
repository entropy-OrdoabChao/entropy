"""
regulatory/mcp_tools_refinement.py — MCP Registration for Refinement Engines

Registers new engines as Beezle MCP tools accessible to Hermes:
  beezle.analyze_burden      — Burden of proof analysis
  beezle.analyze_triggers    — Trigger condition extraction
  beezle.analyze_remedies    — Remedy mapping
  beezle.analyze_immunity    — Immunity waiver analysis
  beezle.analyze_actor_scope — Actor-scope mismatch detection
  beezle.score_consistency   — Multi-axis consistency scoring
  beezle.check_conformity    — Authority hierarchy validation
  beezle.run_refinement      — Trigger refinement pass

Usage:
    In beezle_mcp_server.py, add:
        from regulatory.mcp_tools_refinement import register_refinement_tools
        register_refinement_tools(mcp)
"""

import json
from core.logger import get_logger

log = get_logger("mcp_tools_refinement")


def register_refinement_tools(mcp):
    """Register all refinement engine tools with the MCP server."""

    # ─── Burden Engine ───────────────────────────────────────────────
    @mcp.tool()
    async def analyze_burden(text: str, citation: str = "") -> str:
        """Analyze burden of proof in statutory text. Returns holder, type, shifts, and mandatory remedy indicators."""
        from pipeline.burden_engine import analyze_burden as _analyze, format_burden_block
        result = _analyze(text, citation)
        return format_burden_block(result)

    # ─── Trigger Engine ──────────────────────────────────────────────
    @mcp.tool()
    async def analyze_triggers(text: str, citation: str = "") -> str:
        """Extract trigger conditions, deadlines, and mandatory duties from statutory text."""
        from pipeline.trigger_engine import extract_triggers, format_trigger_block
        result = extract_triggers(text, citation)
        return format_trigger_block(result)

    # ─── Remedy Engine ───────────────────────────────────────────────
    @mcp.tool()
    async def analyze_remedies(text: str, citation: str = "") -> str:
        """Map available remedies (damages, injunction, fees) and whether mandatory or discretionary."""
        from pipeline.remedy_engine import extract_remedies, format_remedy_block
        result = extract_remedies(text, citation)
        return format_remedy_block(result)

    # ─── Immunity Engine ─────────────────────────────────────────────
    @mcp.tool()
    async def analyze_immunity(defendant_type: str, conduct: str = "",
                                statutes: str = "") -> str:
        """Analyze immunity status for a government defendant. Returns waiver status, causes of action, and full reasoning chain."""
        from pipeline.immunity_engine import analyze_immunity as _analyze, format_immunity_report
        statute_list = [s.strip() for s in statutes.split(",") if s.strip()] if statutes else []
        result = _analyze(defendant_type, conduct, statute_list)
        return format_immunity_report(result)

    # ─── Actor-Scope Engine ──────────────────────────────────────────
    @mcp.tool()
    async def analyze_actor_scope(text: str, citation: str = "") -> str:
        """Detect who has duty vs who gets penalized. Flags mismatches where penalty targets differ from duty holders."""
        from pipeline.actor_scope_engine import analyze_actor_scope as _analyze, format_actor_scope_block
        result = _analyze(text, citation)
        return format_actor_scope_block(result)

    # ─── Consistency Scorer ──────────────────────────────────────────
    @mcp.tool()
    async def score_consistency(claim_json: str, engines_json: str = "{}") -> str:
        """Score a legal claim for consistency across all axes (0.0 to 1.0). Input: JSON claim and engine outputs."""
        from pipeline.consistency_scorer import score_consistency as _score, format_consistency_report
        try:
            claim = json.loads(claim_json)
            engines = json.loads(engines_json)
        except json.JSONDecodeError as e:
            return f"JSON parse error: {e}"
        result = _score(claim, engines)
        return format_consistency_report(result)

    # ─── Conformity Checker ──────────────────────────────────────────
    @mcp.tool()
    async def check_conformity(lower_text: str, lower_citation: str,
                                higher_text: str, higher_citation: str) -> str:
        """Check if a lower authority (regulation/statute) conforms to a higher authority (Constitution/statute). Detects contradictions and scope violations."""
        from pipeline.conformity_checker import check_conformity as _check, format_conformity_report
        result = _check(lower_text, lower_citation, higher_text, higher_citation)
        return format_conformity_report(result)

    # ─── Refinement Agent ────────────────────────────────────────────
    @mcp.tool()
    async def run_refinement_pass(pass_name: str = "", dry_run: bool = True,
                                   limit: int = 100) -> str:
        """Run a refinement pass on the Obsidian vault. Pass names: burden, trigger, remedy, actor_scope, conformity, construction, cross_ref, gap_detect. Default: dry_run=True (safe)."""
        from refinement_agent import run_refinement
        passes = [pass_name] if pass_name else None
        result = run_refinement(passes=passes, dry_run=dry_run, limit=limit)
        return json.dumps(result, indent=2)

    # ─── Statutory Construction ───────────────────────────────────────
    @mcp.tool()
    async def analyze_construction(text: str, citation: str = "") -> str:
        """Apply rules of statutory construction to a provision. Detects: means vs includes (closed/expansive), mutual exclusions between defined terms, expressio unius, ejusdem generis, shall vs may, surplusage, and last antecedent rule."""
        from pipeline.statutory_construction import apply_construction_rules, format_construction_analysis
        result = apply_construction_rules(text, citation)
        return format_construction_analysis(result)

    @mcp.tool()
    async def check_definition_scope(term: str, definition_text: str) -> str:
        """Classify a statutory definition as closed ('means') or expansive ('includes'). Returns scope type, what's excluded, and applicable construction rules."""
        from pipeline.statutory_construction import classify_definition_scope
        result = classify_definition_scope(term, definition_text)
        lines = [f"Term: '{result.term}'", f"Scope: {result.scope}"]
        if result.limited_to:
            lines.append(f"Limited to: {', '.join(result.limited_to[:10])}")
        if result.excludes:
            lines.append(f"Excludes: {', '.join(result.excludes)}")
        if result.cross_ref_to:
            lines.append(f"Cross-ref: {result.cross_ref_to}")
        lines.append(f"Rules: {', '.join(result.rules_applied)}")
        return "\n".join(lines)

    @mcp.tool()
    async def check_exclusion(term_a: str, term_b: str, statute_text: str) -> str:
        """Check if two terms in the same statute are mutually exclusive. Under the rule of exclusion, if something is defined as X it is excluded from Y and conversely."""
        from pipeline.statutory_construction import check_exclusion_rule
        result = check_exclusion_rule(term_a, term_b, statute_text)
        return f"Mutually exclusive: {result.mutually_exclusive}\n{result.explanation}"

    @mcp.tool()
    async def validate_definition_import(term: str, source_statute: str,
                                          target_statute: str, target_text: str) -> str:
        """Validate that a definition is NOT being illegally imported from one statute to another. Same word ≠ same meaning across statutes unless explicitly cross-referenced."""
        from pipeline.statutory_construction import validate_no_cross_statute_import
        result = validate_no_cross_statute_import(term, source_statute, target_statute, target_text)
        if result["valid"]:
            return f"VALID: {result.get('note', 'Definition usage is authorized.')}"
        return f"VIOLATION: {result['violation']}"

    log.info("Refinement engine MCP tools registered (12 tools)")
