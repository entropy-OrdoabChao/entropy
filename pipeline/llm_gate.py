"""
pipeline/llm_gate.py — LLM Access Control Gate.

CRITICAL: This is the ONLY place LLM calls happen.
Beezle calls this module, never the LLM directly.

The gate enforces:
  1. LLM is a TOOL, not a brain
  2. Each call has a defined role (extraction, parsing, drafting)
  3. System prompt locks the LLM into a constrained role
  4. JSON output is requested where possible
  5. No "decision" calls — those are deterministic in Python

Permitted LLM uses (and ONLY these):
  - SURFACE_SCAN: identify document type, domain, structure
  - FACT_EXTRACTION: pull discrete factual statements
  - ARGUMENT_EXTRACTION: parse opposing brief arguments
  - DRAFT_FILL: fill a template with case-specific content
  - CASE_CONTEXT: extract caption, parties, court info

Forbidden uses (handled deterministically elsewhere):
  - Intent classification (regex)
  - Document type selection (procedural engine)
  - Legal conclusions (DB rules)
  - Pipeline routing (Beezle)
"""

import json
import re
from typing import Optional

from core.logger import get_logger
from pipeline.llm_layer import get_llm

log = get_logger("LLM_GATE")


# ══════════════════════════════════════════════════════════════════════
# ROLE-LOCKED SYSTEM PROMPTS
# ══════════════════════════════════════════════════════════════════════

ROLE_PROMPT_BASE = """You are a structured legal processing engine.
Your role is limited and controlled.

You MUST:
- Extract structured information only
- Return JSON when requested
- Follow instructions exactly
- Mark unknown information as "unknown"

You MUST NOT:
- Determine legal outcomes
- Decide procedural steps
- Select document types
- Make assumptions beyond the provided text
- Add explanations or commentary unless requested

You are a tool inside a deterministic system. You do not control the system."""


SURFACE_SCAN_PROMPT = ROLE_PROMPT_BASE + """

CURRENT TASK: Surface scan a legal document.

Return ONLY this JSON structure (no markdown, no preamble):
{
  "document_type": "complaint|motion|opposition|reply|order|notice|letter|credit_report|contract|discovery|exhibit|unknown",
  "domain": "civil_litigation|consumer_finance|family|criminal|administrative|unknown",
  "procedural_role": "initiating|responsive|reply|sur_reply|order|notice|unknown",
  "structure_detected": ["list", "of", "section", "headers", "found"],
  "possible_laws": ["FCRA", "FDCPA", "Pa.R.C.P. 3146", "etc"],
  "key_terms": ["verification", "garnishee", "etc"],
  "is_court_filing": true,
  "has_caption": true,
  "has_signature_block": true
}

NO interpretation. NO conclusions. Only what is on the page."""


FACT_EXTRACTION_PROMPT = ROLE_PROMPT_BASE + """

CURRENT TASK: Extract discrete facts from a document.

Return ONLY a JSON array of strings. Each fact must:
- Be a single discrete factual statement
- Include dates, names, amounts where present
- Use exact language from the document where possible
- NOT contain legal conclusions
- NOT contain interpretations

Example format (illustrative — use the actual document's facts, not these):
["On [date], [Party A] [specific action with detail].",
 "On [date], [Party B] [response or counter-action].",
 "[Quantitative fact: dollar amount, time period, count]."]

Return valid JSON array only. No markdown, no explanation."""


ARGUMENT_EXTRACTION_PROMPT = ROLE_PROMPT_BASE + """

CURRENT TASK: Extract opposing arguments from a brief or motion.

Return ONLY this JSON structure (no markdown, no preamble):
{
  "opposing_party": "name of party making arguments",
  "document_type": "opposition|motion|reply|complaint",
  "main_arguments": [
    {
      "claim": "Their assertion (verbatim or close paraphrase)",
      "their_authority": ["case or rule they cite"],
      "their_reasoning": "Why they say it"
    }
  ],
  "admissions": ["facts they admit"],
  "denials": ["facts they deny"],
  "procedural_demands": ["what they want the court to do"],
  "weaknesses": ["any internal contradictions, missing authority, conclusory statements"]
}

Extract verbatim or close paraphrase. NO interpretation. NO opinions."""


CASE_CONTEXT_PROMPT = ROLE_PROMPT_BASE + """

CURRENT TASK: Extract case caption and procedural information from a court filing.

Return ONLY this JSON structure (no markdown, no preamble):
{
  "court": "exact court name as written",
  "case_no": "case number",
  "plaintiff": "plaintiff name",
  "defendant": "defendant name(s)",
  "garnishee": "garnishee name if any",
  "other_parties": ["any other parties"],
  "filing_date": "date filed if shown",
  "judge": "judge name if shown",
  "opposing_counsel": "opposing counsel name and firm if shown",
  "current_filing_title": "exact title of the document",
  "case_caption_text": "the full caption block as it appears"
}

Mark unknown fields as "unknown". Use exact language from the document."""


DRAFT_FILL_PROMPT = ROLE_PROMPT_BASE + """

CURRENT TASK: Fill a legal document template with case-specific content.

You will receive:
1. A template with structural sections
2. Case context (real parties, court, dates)
3. Facts (discrete statements)
4. Authority (statutes and cases retrieved from database)
5. Conflict map (rule vs opposing argument)

Rules:
- Use ONLY facts and authority provided
- Use real party names from case context (NEVER use [PLACEHOLDER])
- Quote statutes and rules verbatim
- For each conflict, apply the rule to the fact deterministically
- NO hedging language ("may", "might", "possibly", "arguably")
- NO conclusions beyond what authority supports
- If authority is missing for a point, write "[AUTHORITY NEEDED: <topic>]"

Output: the completed document as plain text. No markdown headers (###).
Use plain section headers like "I. INTRODUCTION" or "ARGUMENT".
NO scorecard. NO debug output. NO commentary."""


# ══════════════════════════════════════════════════════════════════════
# THE GATE
# ══════════════════════════════════════════════════════════════════════

class LLMGate:
    """The single point of LLM access. All LLM calls flow through here."""

    def __init__(self):
        self.llm = get_llm()
        self._call_count = 0
        self._call_log = []

    def is_available(self) -> bool:
        """Check if LLM is reachable."""
        try:
            health = self.llm.health_check()
            return health.get("status") == "ok"
        except Exception:
            return False

    def surface_scan(self, document_text: str) -> Optional[dict]:
        """LLM Role 1: Identify what kind of document this is.
        Used in Phase 3.5 document intelligence layer.
        """
        self._call_count += 1
        log.stage("surface_scan", f"LLM call #{self._call_count} (no thinking)")
        try:
            result = self.llm.chat(
                messages=[
                    {"role": "system", "content": SURFACE_SCAN_PROMPT},
                    {"role": "user", "content": document_text[:8000]},
                ],
                thinking=False,
            )
            return self._parse_json(result, "surface_scan")
        except Exception as e:
            log.error("surface_scan", "LLM call failed", error=str(e))
            return None

    def extract_facts(self, document_text: str) -> list[str]:
        """LLM Role 2: Pull discrete facts from a document."""
        self._call_count += 1
        log.stage("extract_facts", f"LLM call #{self._call_count} (no thinking)")
        try:
            result = self.llm.chat(
                messages=[
                    {"role": "system", "content": FACT_EXTRACTION_PROMPT},
                    {"role": "user", "content": document_text[:10000]},
                ],
                thinking=False,
            )
            parsed = self._parse_json(result, "extract_facts")
            if isinstance(parsed, list):
                return [str(f) for f in parsed if f]
            return []
        except Exception as e:
            log.error("extract_facts", "LLM call failed", error=str(e))
            return []

    def extract_arguments(self, opposition_text: str) -> Optional[dict]:
        """LLM Role 3: Parse opposing arguments from a brief.
        Used in Conflict Engine.
        """
        self._call_count += 1
        log.stage("extract_arguments", f"LLM call #{self._call_count} (no thinking)")
        try:
            result = self.llm.chat(
                messages=[
                    {"role": "system", "content": ARGUMENT_EXTRACTION_PROMPT},
                    {"role": "user", "content": opposition_text[:12000]},
                ],
                thinking=False,
            )
            return self._parse_json(result, "extract_arguments")
        except Exception as e:
            log.error("extract_arguments", "LLM call failed", error=str(e))
            return None

    def extract_case_context(self, document_text: str) -> Optional[dict]:
        """LLM Role 4: Pull caption, parties, court from a filing."""
        self._call_count += 1
        log.stage("extract_case_context", f"LLM call #{self._call_count} (no thinking)")
        try:
            # Caption is usually in first 3000 chars
            result = self.llm.chat(
                messages=[
                    {"role": "system", "content": CASE_CONTEXT_PROMPT},
                    {"role": "user", "content": document_text[:4000]},
                ],
                thinking=False,
            )
            return self._parse_json(result, "extract_case_context")
        except Exception as e:
            log.error("extract_case_context", "LLM call failed", error=str(e))
            return None

    def fill_draft(self, template: str, case_context: dict, facts: list,
                   authority: str, conflicts: list = None,
                   opposing_arguments: dict = None) -> str:
        """LLM Role 5: Fill a template with case-specific content.

        This is the ONLY drafting LLM call. Thinking is enabled here
        because draft quality matters more than speed.
        """
        self._call_count += 1
        log.stage("fill_draft", f"LLM call #{self._call_count} (thinking ON)")

        user_content = self._build_draft_input(
            template, case_context, facts, authority, conflicts, opposing_arguments
        )

        try:
            result = self.llm.chat(
                messages=[
                    {"role": "system", "content": DRAFT_FILL_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                thinking=True,  # Quality mode for drafting
                n_predict=4000,
            )
            return result.strip()
        except Exception as e:
            log.error("fill_draft", "LLM call failed", error=str(e))
            return ""

    def get_stats(self) -> dict:
        """Return call statistics for debugging."""
        return {
            "total_calls": self._call_count,
            "available": self.is_available(),
        }

    # ══════════════════════════════════════════════════════════════════
    # INTERNAL HELPERS
    # ══════════════════════════════════════════════════════════════════

    def _parse_json(self, raw: str, context: str):
        """Parse JSON from LLM output, handling markdown wrapping."""
        if not raw:
            return None
        cleaned = raw.strip()

        # Strip markdown code fences
        if cleaned.startswith("```"):
            cleaned = re.sub(r'^```(?:json)?\s*\n?', '', cleaned)
            cleaned = re.sub(r'\n?```\s*$', '', cleaned)
            cleaned = cleaned.strip()

        # Strip <think> tags if any leaked through
        cleaned = re.sub(r'<think>.*?</think>', '', cleaned, flags=re.DOTALL).strip()

        # Try to find JSON object/array if there's extra text
        if not cleaned.startswith(('{', '[')):
            obj_match = re.search(r'\{.*\}', cleaned, re.DOTALL)
            arr_match = re.search(r'\[.*\]', cleaned, re.DOTALL)
            if obj_match:
                cleaned = obj_match.group(0)
            elif arr_match:
                cleaned = arr_match.group(0)

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as e:
            log.warning(context, f"JSON parse failed: {e}", error=cleaned[:200])
            return None

    def _build_draft_input(self, template, case_context, facts, authority,
                           conflicts, opposing_arguments) -> str:
        """Build the user message for drafting.

        Trimmed to keep total input under ~12000 chars to fit in reasonable
        generation time on local hardware.
        """
        sections = []

        # Case context (real names, court, etc.) — compact
        if case_context:
            ctx_compact = {
                k: v for k, v in case_context.items()
                if k in ("court", "case_no", "plaintiff", "defendant", "garnishee",
                         "opposing_counsel", "current_filing_title")
                and v and v != "unknown"
            }
            sections.append("## CASE CONTEXT\n" + json.dumps(ctx_compact, indent=2))

        # Facts (top 20 only)
        if facts:
            facts_subset = facts[:20]
            facts_str = "\n".join(f"{i+1}. {f}" for i, f in enumerate(facts_subset))
            sections.append(f"## FACTS\n{facts_str}")

        # Authority from DB (trim to 4000 chars max)
        if authority:
            auth_trimmed = authority[:4000]
            if len(authority) > 4000:
                auth_trimmed += "\n[... truncated]"
            sections.append(f"## CONTROLLING AUTHORITY\n{auth_trimmed}")

        # Conflict map (the killer feature) — top 6 conflicts
        if conflicts:
            conflict_str = ""
            for i, c in enumerate(conflicts[:6], 1):
                cd = c if isinstance(c, dict) else c.to_dict()
                conflict_str += f"\n### Conflict {i}: {cd.get('issue', '')}\n"
                conflict_str += f"- Their position: {cd.get('their_position', '')[:300]}\n"
                rule_text = cd.get('rule_text', '')[:300]
                if rule_text:
                    conflict_str += f"- Controlling rule: {rule_text}\n"
                conflict_str += f"- Application: {cd.get('application', '')[:200]}\n"
                conflict_str += f"- Result: {cd.get('result', '')}\n"
            sections.append(f"## CONFLICTS TO ADDRESS{conflict_str}")

        # NOTE: opposing_arguments is now NOT duplicated since conflicts already include them
        # This was causing duplicate ~6KB content in the prompt

        # The template last (trim to 2000 chars)
        if template:
            tmpl_trimmed = template[:2000]
            sections.append(f"## TEMPLATE\n{tmpl_trimmed}")

        result = "\n\n".join(sections)
        log.stage("build_draft_input", f"Built draft input: {len(result)} chars")
        return result


# ── Singleton ─────────────────────────────────────────────────────────

_gate: Optional[LLMGate] = None

def get_gate() -> LLMGate:
    global _gate
    if _gate is None:
        _gate = LLMGate()
    return _gate
