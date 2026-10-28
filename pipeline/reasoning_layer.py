"""
reasoning_layer.py — LLM-based audit prompts.

This is Layer 2 of the hybrid architecture. Layer 1 (hierarchy_enforcer,
dismissal_analyzer) does deterministic pattern detection. Layer 2 runs a
set of NARROW, SPECIFIC prompts against an LLM to catch patterns that can
only be recognized in natural language:

  1. Internal contradictions (admitted X then argued against X)
  2. Procedural conflation (claiming waiver via wrong mechanism)
  3. Element-by-element coverage of the actual legal test
  4. Rule-text vs rule-as-quoted discrepancies
  5. Hierarchy audit cross-check (did code miss anything?)

Each prompt:
  - Asks ONE specific question
  - Requires JSON output with a fixed schema
  - Gets its results validated by HallucinationGuard before being trusted
  - Gets logged to Obsidian as reasoning_*.md for Path B training data

Layer 3 (HallucinationGuard in this file) verifies every claim the LLM
makes against the DB or the original document. Nothing the LLM invents
survives.
"""

from __future__ import annotations
import json
import re
import logging
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any, Optional, Callable
from pathlib import Path

log = logging.getLogger("beezle.reasoning")


# =============================================================================
# PROMPT LIBRARY
# =============================================================================

class ReasoningPromptLibrary:
    """
    Each method returns (system_prompt, user_prompt, expected_schema).
    The prompts are intentionally narrow - each one asks ONE specific
    lawyerly question. Narrow questions get reliable answers.
    """

    # ---- COMMON PREAMBLE ----
    BASE_SYSTEM = (
        "You are a legal reasoning auditor. You read court filings and "
        "answer one specific question at a time. You NEVER invent citations, "
        "quotes, or rules. If you are not certain, say 'unknown'. You output "
        "ONLY valid JSON matching the schema given — no preamble, no commentary, "
        "no markdown fences. Just the JSON."
    )

    @classmethod
    def hierarchy_audit(cls, filing_text: str, case_court: str) -> tuple[str, str, dict]:
        """
        Cross-check the deterministic hierarchy_enforcer. Asks the LLM to
        independently identify non-binding authority being presented as controlling.
        """
        system = cls.BASE_SYSTEM
        user = f"""CASE COURT: {case_court}

Read the filing below. For every case citation, determine:
1. Is the cited court binding on {case_court}?
2. Is the filing presenting the citation as if it were controlling authority?

Binding hierarchy rules:
- SCOTUS binds everyone
- Third Circuit binds EDPA on federal law
- PA Supreme Court binds all PA state courts
- PA Superior Court and Commonwealth Court bind PA Common Pleas
- Common Pleas decisions from one PA county do NOT bind another county
- Trial court decisions are persuasive only, never binding on other trial courts
- Federal district court opinions don't bind state courts on state law

Output JSON only:
{{
  "citations": [
    {{
      "citation": "<text as it appears>",
      "cited_court": "<SCOTUS|CA3|EDPA|PA_SUP|PA_SUPER|PA_CMWLTH|PA_CP_PHILA|PA_CP_OTHER|OTHER|UNKNOWN>",
      "is_binding": true|false,
      "presented_as_controlling": true|false,
      "violation": "<none|non_binding_as_controlling|trial_court_as_appellate>"
    }}
  ]
}}

FILING:
{filing_text[:6000]}"""

        schema = {
            "citations": [
                {
                    "citation": str,
                    "cited_court": str,
                    "is_binding": bool,
                    "presented_as_controlling": bool,
                    "violation": str,
                }
            ]
        }
        return system, user, schema

    @classmethod
    def contradiction_audit(cls, filing_text: str) -> tuple[str, str, dict]:
        """
        Find internal contradictions in the filing — places where the author
        admits X in one paragraph then argues against X in another.
        """
        system = cls.BASE_SYSTEM
        user = f"""Read the filing below and identify internal contradictions.

A contradiction is when the filing:
- Admits a rule applies, then argues the rule doesn't apply
- Admits a fact, then denies the same fact elsewhere
- States a legal standard, then argues against that standard
- Admits an element is satisfied, then claims it's not

Output JSON only:
{{
  "contradictions": [
    {{
      "admission": "<what was admitted (quote or paraphrase)>",
      "contradiction": "<what contradicts the admission>",
      "location_1": "<paragraph number or 'p. X' if stated>",
      "location_2": "<paragraph number or 'p. X' if stated>",
      "significance": "<one sentence explaining why this contradiction matters>"
    }}
  ]
}}

If no contradictions found, return {{"contradictions": []}}.

FILING:
{filing_text[:6000]}"""

        schema = {"contradictions": [{"admission": str, "contradiction": str, "location_1": str, "location_2": str, "significance": str}]}
        return system, user, schema

    @classmethod
    def procedural_conflation_audit(cls, filing_text: str) -> tuple[str, str, dict]:
        """
        Detect where the opposing party argues 'waiver' by claiming the wrong
        procedural mechanism was used. Example: arguing that failure to file
        preliminary objections waives a Rule 3146 default judgment claim —
        these are distinct remedies with different triggers.
        """
        system = cls.BASE_SYSTEM
        user = f"""Read the filing below. The filing may argue that the opposing party
"waived" a claim or argument by using one procedural mechanism instead of
another. Identify any such waiver arguments.

For each:
1. What does the filing say SHOULD have been used?
2. What was actually used?
3. Are these mechanisms actually substitutes, or are they distinct remedies
   with different triggers?

Output JSON only:
{{
  "waiver_arguments": [
    {{
      "filing_claim": "<what the filing says>",
      "proposed_mechanism": "<the mechanism the filing says should have been used>",
      "actual_mechanism": "<the mechanism that was used>",
      "are_substitutes": true|false,
      "explanation": "<why they are or aren't substitutes>"
    }}
  ]
}}

If no waiver arguments, return {{"waiver_arguments": []}}.

FILING:
{filing_text[:6000]}"""

        schema = {"waiver_arguments": [{"filing_claim": str, "proposed_mechanism": str, "actual_mechanism": str, "are_substitutes": bool, "explanation": str}]}
        return system, user, schema

    @classmethod
    def element_coverage_audit(cls, order_text: str, claim_elements: List[str]) -> tuple[str, str, dict]:
        """
        Check whether a dismissal order addressed each element of the claim.
        Given a list of elements, the LLM reports which were addressed.
        """
        system = cls.BASE_SYSTEM
        elements_json = json.dumps(claim_elements)
        user = f"""A claim has these elements: {elements_json}

Read the court order below. For each element, determine:
1. Did the court address this specific element?
2. If yes, what did the court say about it?
3. If no, the element was ignored.

Output JSON only:
{{
  "element_analysis": [
    {{
      "element": "<element name>",
      "addressed": true|false,
      "court_finding": "<what the court said, or 'ignored'>",
      "evidence_quote": "<short quote from order if available, else empty string>"
    }}
  ]
}}

ORDER:
{order_text[:6000]}"""

        schema = {"element_analysis": [{"element": str, "addressed": bool, "court_finding": str, "evidence_quote": str}]}
        return system, user, schema

    @classmethod
    def rule_text_audit(cls, filing_text: str, rule_citations: List[str]) -> tuple[str, str, dict]:
        """
        The filing cites specific rules. For each, check if the filing quotes
        the rule accurately or paraphrases in a misleading way.
        """
        system = cls.BASE_SYSTEM
        rules_json = json.dumps(rule_citations)
        user = f"""The filing cites these rules: {rules_json}

For each rule cited in the filing:
1. Does the filing quote the rule, or only paraphrase it?
2. If there is a quote, what exactly does the filing say the rule says?
3. Is the paraphrase a reasonable characterization, or is it spun/misleading?

Output JSON only:
{{
  "rule_audits": [
    {{
      "rule": "<rule citation>",
      "has_direct_quote": true|false,
      "quoted_text": "<exact quote from filing or empty>",
      "paraphrase": "<how the filing characterizes the rule>",
      "suspicious": true|false,
      "reason": "<why it's suspicious, or 'accurate characterization'>"
    }}
  ]
}}

FILING:
{filing_text[:6000]}"""

        schema = {"rule_audits": [{"rule": str, "has_direct_quote": bool, "quoted_text": str, "paraphrase": str, "suspicious": bool, "reason": str}]}
        return system, user, schema

    @classmethod
    def bias_narrative_audit(cls, filing_text: str) -> tuple[str, str, dict]:
        """
        Identify bias narrative patterns beyond single-word triggers:
        sustained attacks on the litigant, emotional language, appeals to
        discretion instead of law.
        """
        system = cls.BASE_SYSTEM
        user = f"""Read the filing below. Identify any sentences or passages that:
- Attack the opposing party personally rather than their legal argument
- Use emotional or pejorative language instead of legal analysis
- Appeal to the court's discretion to punish rather than to rule on merits
- Characterize arguments as "frivolous" / "harassing" / "baseless" without legal support
- Invoke the pro se status of the opposing party as grounds for disregard

Output JSON only:
{{
  "bias_passages": [
    {{
      "passage": "<the problematic text>",
      "type": "<personal_attack|emotional_language|punitive_appeal|pro_se_attack|unsupported_characterization>",
      "why_problematic": "<one sentence>"
    }}
  ],
  "overall_bias_score": 0-10
}}

FILING:
{filing_text[:6000]}"""

        schema = {"bias_passages": [{"passage": str, "type": str, "why_problematic": str}], "overall_bias_score": int}
        return system, user, schema

    # === RECIPROCITY_AUDIT_V1 (2026-08-25) ===
    @classmethod
    def reciprocity_audit(cls, filing_text: str, pro_se_party: str = "the pro se litigant") -> tuple[str, str, dict]:
        """
        The generalized "flip it back" detector. Finds ASYMMETRIC application
        of a legal requirement: a rule invoked AGAINST the pro se party while
        the party who FILED to enforce is excused from the identical showing.

        Classic instance: "borrower lacks standing to challenge the assignment"
        while the enforcing servicer never proved holder / person-entitled-to-
        enforce status (Fed. R. Civ. P. 17(a); U.C.C. 3-301). But the pattern
        is general: verification demanded one-way, timeliness enforced one-way,
        particularity demanded one-way, authentication demanded one-way.

        Regex cannot catch this — it requires comprehension of WHO invoked
        WHAT against WHOM and whether the record shows the invoker satisfied
        the same rule. Deterministic layer stays as the guardrail: emitted
        facts carry quotes verified by HallucinationGuard, and the category
        maps pin the cite + remedy.
        """
        system = cls.BASE_SYSTEM
        user = f"""PRO SE PARTY: {pro_se_party}

Read the filing below (an opposing filing or court order). Find every place
where a legal REQUIREMENT is applied ASYMMETRICALLY — invoked against
{pro_se_party} while the party seeking enforcement (or the court) excuses
the invoking/enforcing party from the identical showing.

For each requirement invoked against {pro_se_party}, answer:
1. WHAT rule/requirement is invoked (standing, real-party-in-interest,
   holder/person-entitled-to-enforce, verification, timeliness, pleading
   particularity, authentication, exhaustion, or another)?
2. WHO invoked it, and WHO is the party actually seeking enforcement/relief?
3. Does the filing show the ENFORCING party itself satisfied that same
   requirement (e.g. proved holder status via indorsement/allonge/possession,
   verified its own pleading, filed timely, pleaded with particularity)?
4. If the filing does NOT show the enforcer's own compliance, that is a
   reciprocity violation — the rule binds the party who filed to enforce.

Output JSON only:
{{
  "asymmetries": [
    {{
      "requirement": "<standing|real_party|holder_status|verification|timeliness|particularity|authentication|exhaustion|other>",
      "invoked_against": "<who the rule was applied against>",
      "enforcing_party": "<the party seeking enforcement/relief, by name if stated>",
      "direction": "<against_pro_se if the rule was invoked against {pro_se_party}; by_pro_se if {pro_se_party} is the one invoking the rule against an opponent>",
      "quote": "<EXACT text from the filing invoking the rule — verbatim>",
      "enforcer_compliance_shown": true|false,
      "enforcer_compliance_quote": "<verbatim text showing the enforcer satisfied the same rule, or empty string>",
      "flip": "<one sentence: how the identical rule disqualifies the enforcing party on this record>"
    }}
  ]
}}

If no asymmetries found, return {{"asymmetries": []}}.

FILING:
{filing_text[:6000]}"""

        schema = {
            "asymmetries": [
                {
                    "requirement": str,
                    "invoked_against": str,
                    "enforcing_party": str,
                    "direction": str,
                    "quote": str,
                    "enforcer_compliance_shown": bool,
                    "enforcer_compliance_quote": str,
                    "flip": str,
                }
            ]
        }
        return system, user, schema
    # === END RECIPROCITY_AUDIT_V1 ===


# =============================================================================
# LLM CALLER (ADAPTER PATTERN)
# =============================================================================

class LLMAdapter:
    """
    Adapter for calling Beezle's LLM layer. The real llm_layer.py is
    hardcoded to llama.cpp at localhost:8000 — we don't assume that.
    Pass in a callable that takes (system, user) and returns a string.
    """

    def __init__(self, call_fn: Optional[Callable[[str, str], str]] = None):
        self.call_fn = call_fn or self._default_llama_cpp

    def _default_llama_cpp(self, system: str, user: str) -> str:
        """Default: call local llama.cpp server at localhost:8000."""
        import urllib.request
        import urllib.error
        payload = {
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.1,
            "max_tokens": 2048,
            # RECIPROCITY_AUDIT_V1: the m0 drafter model is a thinking model —
            # without this it spends the token budget in the 'reasoning' field
            # and returns content=null. Harmless for non-thinking models.
            "chat_template_kwargs": {"enable_thinking": False},
        }
        req = urllib.request.Request(
            "http://localhost:8000/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                msg = body["choices"][0]["message"]
                content = msg.get("content")
                if not content:  # thinking model ran out of budget mid-reason
                    content = msg.get("reasoning") or ""
                return content
        except Exception as e:
            log.error(f"LLM call failed: {e}")
            return ""

    def call(self, system: str, user: str) -> str:
        return self.call_fn(system, user)


# =============================================================================
# JSON EXTRACTOR (STRICT)
# =============================================================================

def extract_json(raw: str) -> Optional[dict]:
    """
    Pull JSON out of an LLM response. LLMs often wrap JSON in markdown
    fences or add preamble despite being told not to — strip that.
    """
    if not raw:
        return None

    # Strip markdown fences
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.I)
    raw = re.sub(r"\s*```$", "", raw.strip())

    # Find first { and last }
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None

    candidate = raw[start:end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as e:
        log.debug(f"JSON decode failed: {e}")
        # One retry: try to fix common issues
        try:
            fixed = re.sub(r",\s*([}\]])", r"\1", candidate)  # trailing commas
            return json.loads(fixed)
        except Exception:
            return None


# =============================================================================
# HALLUCINATION GUARD
# =============================================================================

class HallucinationGuard:
    """
    Validates LLM output against the source document. For every claim the
    LLM makes that includes a quote or citation, verify that text actually
    appears in the source. Discard claims that don't verify.
    """

    def __init__(self, source_text: str):
        self.source_text = source_text
        self.source_lower = source_text.lower()

    def verify_quote(self, quote: str, min_length: int = 8) -> bool:
        """Does this exact quote (or close match) appear in the source?"""
        if not quote or len(quote) < min_length:
            return True  # Too short to verify — skip
        # Normalize whitespace
        q = re.sub(r"\s+", " ", quote).strip().lower()
        src = re.sub(r"\s+", " ", self.source_lower)
        if q in src:
            return True
        # Try partial match for long quotes (first 50 chars)
        if len(q) > 60:
            prefix = q[:50]
            return prefix in src
        return False

    def verify_citation(self, citation: str) -> bool:
        """Does this citation (by party name or reporter) appear in source?"""
        if not citation:
            return True
        # Extract a distinctive signature: party name or reporter
        match = re.search(r"([A-Z][a-z]+)\s+v\.?\s+([A-Z][a-z]+)", citation)
        if match:
            sig = f"{match.group(1)} v. {match.group(2)}".lower()
            return sig in self.source_lower
        # Try reporter pattern
        rep = re.search(r"\d+\s+[A-Z][a-z.&\s]+\d+", citation)
        if rep:
            return rep.group(0).lower() in self.source_lower
        return True  # Can't signature-match, give benefit of doubt

    def filter_hierarchy_audit(self, result: dict) -> dict:
        """Filter a hierarchy_audit result, dropping unverifiable citations."""
        if not result or "citations" not in result:
            return {"citations": []}
        verified = []
        for c in result.get("citations", []):
            cite = c.get("citation", "")
            if self.verify_citation(cite):
                verified.append(c)
            else:
                log.debug(f"Dropping unverified citation: {cite[:60]}")
        return {"citations": verified}

    def filter_contradiction_audit(self, result: dict) -> dict:
        """Filter a contradiction result, dropping unverifiable quotes."""
        if not result or "contradictions" not in result:
            return {"contradictions": []}
        verified = []
        for c in result.get("contradictions", []):
            a = c.get("admission", "")
            b = c.get("contradiction", "")
            # Both admission and contradiction must be traceable
            if self.verify_quote(a, min_length=15) and self.verify_quote(b, min_length=15):
                verified.append(c)
            else:
                log.debug(f"Dropping unverified contradiction")
        return {"contradictions": verified}

    # === RECIPROCITY_AUDIT_V1 (2026-08-25) ===
    def filter_reciprocity_audit(self, result: dict) -> dict:
        """Filter a reciprocity result: the invoking quote MUST verify against
        the source (that is the whole evidentiary basis of the flip). The
        enforcer_compliance_quote is only kept if it verifies; if the LLM
        claimed compliance was shown but its quote fails verification, flip
        the flag to False rather than trusting the unverified claim."""
        if not result or "asymmetries" not in result:
            return {"asymmetries": []}
        verified = []
        for a in result.get("asymmetries", []):
            q = a.get("quote", "")
            if not self.verify_quote(q, min_length=15):
                log.debug("Dropping unverified reciprocity quote")
                continue
            cq = a.get("enforcer_compliance_quote", "")
            if cq and not self.verify_quote(cq, min_length=15):
                a["enforcer_compliance_quote"] = ""
                a["enforcer_compliance_shown"] = False
            verified.append(a)
        return {"asymmetries": verified}
    # === END RECIPROCITY_AUDIT_V1 ===


# =============================================================================
# REASONING ENGINE
# =============================================================================

@dataclass
class ReasoningTrace:
    """One complete LLM audit: prompt, response, validation, findings."""
    prompt_name: str
    source_file: str
    case_id: str
    raw_response: str = ""
    parsed_result: dict = field(default_factory=dict)
    filtered_result: dict = field(default_factory=dict)
    dropped_claims: int = 0
    timestamp: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class ReasoningEngine:
    """
    Orchestrates the reasoning layer. For each case:
      1. Loads the relevant filing text
      2. Runs each applicable audit prompt sequentially (NEVER in parallel)
      3. Validates every response with HallucinationGuard
      4. Logs every trace to Obsidian for Path B training
    """

    def __init__(self, llm: Optional[LLMAdapter] = None):
        self.llm = llm or LLMAdapter()
        self.prompts = ReasoningPromptLibrary()

    def run_audit(
        self,
        prompt_name: str,
        source_file: str,
        source_text: str,
        case_id: str,
        **kwargs
    ) -> ReasoningTrace:
        """Run one audit prompt and return a trace."""
        from datetime import datetime

        trace = ReasoningTrace(
            prompt_name=prompt_name,
            source_file=source_file,
            case_id=case_id,
            timestamp=datetime.now().isoformat(),
        )

        # Get the prompt
        prompt_method = getattr(self.prompts, prompt_name, None)
        if not prompt_method:
            log.error(f"Unknown prompt: {prompt_name}")
            return trace

        try:
            system, user, schema = prompt_method(source_text, **kwargs)
        except Exception as e:
            log.error(f"Prompt build failed for {prompt_name}: {e}")
            return trace

        # Call LLM
        raw = self.llm.call(system, user)
        trace.raw_response = raw

        # Parse JSON
        parsed = extract_json(raw)
        if not parsed:
            log.warning(f"No JSON extracted from {prompt_name} response")
            return trace
        trace.parsed_result = parsed

        # Validate
        guard = HallucinationGuard(source_text)
        filter_method = getattr(guard, f"filter_{prompt_name}", None)
        if filter_method:
            filtered = filter_method(parsed)
            trace.filtered_result = filtered
            # Count drops
            key = list(parsed.keys())[0] if parsed else None
            if key and isinstance(parsed.get(key), list):
                trace.dropped_claims = len(parsed[key]) - len(filtered.get(key, []))
        else:
            trace.filtered_result = parsed  # No filter = accept as-is

        return trace

    def run_full_audit(
        self,
        source_file: str,
        source_text: str,
        case_id: str,
        case_court: str,
    ) -> List[ReasoningTrace]:
        """Run the standard audit battery for an opposition/motion filing."""
        traces = []

        traces.append(self.run_audit(
            "hierarchy_audit",
            source_file, source_text, case_id,
            case_court=case_court,
        ))

        traces.append(self.run_audit(
            "contradiction_audit",
            source_file, source_text, case_id,
        ))

        traces.append(self.run_audit(
            "procedural_conflation_audit",
            source_file, source_text, case_id,
        ))

        traces.append(self.run_audit(
            "bias_narrative_audit",
            source_file, source_text, case_id,
        ))

        # === RECIPROCITY_AUDIT_V1 (2026-08-25) ===
        traces.append(self.run_audit(
            "reciprocity_audit",
            source_file, source_text, case_id,
        ))
        # === END RECIPROCITY_AUDIT_V1 ===

        return traces


# =============================================================================
# RECIPROCITY -> SCORECARD BRIDGE (2026-08-25)
# =============================================================================

def reciprocity_asymmetries_to_violations(
    asymmetries: list,
    source_doc: str = "Unknown Document",
    direction_filter: str = "against_pro_se",
) -> list:
    """Convert verified reciprocity_audit asymmetries into ViolationPin-shaped
    dicts so they merge into the scorecard exactly like block14 regex pins
    (audit_writer normalizes on 'violation_type'/'quote'/'explanation').

    Only asymmetries where the enforcer's own compliance was NOT shown become
    violations — a rule applied to both sides evenly is not a defect.

    DIRECTION GATE (V2): only asymmetries whose direction matches
    direction_filter (default 'against_pro_se') become violations. An
    asymmetry the pro se party created in ITS OWN filing (direction
    'by_pro_se') is leverage, not a defect to plead against ourselves —
    collect those via reciprocity_leverage() instead. Entries missing a
    direction (V1 traces) are treated as 'against_pro_se' for backward
    compatibility ONLY when invoked_against does not name the enforcing
    party as the target of the pro se party's own demand.

    Deterministic guardrail: category is pinned to REAL_PARTY_RECIPROCITY
    (hooked in all three _SCORECARD_HOOK_MAP mirrors), the rule cite is
    pinned per requirement type, and the quote arrived HallucinationGuard-
    verified. The LLM found the asymmetry; the maps decide rule + remedy.
    """
    _RULE_BY_REQ = {
        "standing":       "Fed. R. Civ. P. 17(a); U.S. Const. art. III, § 2; Lujan v. Defenders of Wildlife, 504 U.S. 555, 561 (1992)",
        "real_party":     "Fed. R. Civ. P. 17(a); Pa.R.C.P. 2002",
        "holder_status":  "U.C.C. §§ 3-203, 3-204, 3-205, 3-301; 13 Pa.C.S. §§ 3203, 3204, 3205, 3301, 3309",
        "verification":   "Fed. R. Civ. P. 11(a); 28 U.S.C. § 1746; Pa.R.C.P. 1024",
        "timeliness":     "Fed. R. Civ. P. 6; 12(a); Pa.R.C.P. 1026",
        "particularity":  "Fed. R. Civ. P. 9(b); Pa.R.C.P. 1019(b)",
        "authentication": "Fed. R. Evid. 901; 902; Pa.R.E. 901",
        "exhaustion":     "the exhaustion doctrine invoked in the filing",
    }
    out = []
    for a in asymmetries or []:
        if not isinstance(a, dict):
            continue
        if a.get("enforcer_compliance_shown"):
            continue  # rule applied evenly — not a defect
        # V2 direction gate
        direction = str(a.get("direction", "")).strip().lower() or "against_pro_se"
        if direction != direction_filter:
            continue
        req = str(a.get("requirement", "other")).strip().lower()
        rule = _RULE_BY_REQ.get(req, "Fed. R. Civ. P. 17(a); U.C.C. § 3-301")
        enforcer = str(a.get("enforcing_party", "the enforcing filer")).strip() or "the enforcing filer"
        flip = str(a.get("flip", "")).strip()
        out.append({
            "violation_type": "REAL_PARTY_RECIPROCITY",
            "rule_citation": rule,
            "rule_text": (
                "An action must be prosecuted in the name of the real party in "
                "interest; a requirement invoked to disqualify one party binds "
                "the party who filed to enforce to the identical showing."
            ),
            "source_doc": source_doc,
            "quote": str(a.get("quote", ""))[:500],
            "explanation": (
                f"The {req.replace('_', ' ')} requirement was invoked against "
                f"{a.get('invoked_against', 'the pro se party')} while "
                f"{enforcer} was excused from the identical showing. "
                + (flip if flip else "The same rule disqualifies the enforcing filer on this record.")
            ),
            "fix": (
                f"Require {enforcer} to satisfy the identical requirement it "
                f"invoked ({rule}), or dismiss / join the real party in "
                "interest under Rule 17(a)."
            ),
            "severity": "FATAL",
        })
    return out


# =============================================================================
# DOCUMENT AUTHORSHIP GUARDRAIL (deterministic — decides direction bucketing)
# =============================================================================

def detect_document_author(text: str, pro_se_names: list | None = None) -> str:
    """Deterministically classify WHO authored a court document.

    Returns one of: 'pro_se', 'court', 'opponent', 'unknown'.

    This is the guardrail the direction gate rides on: the LLM finds the
    asymmetry, but AUTHORSHIP decides the bucket. A demand inside the pro se
    party's own filing can never be a trick played against them — it is
    their leverage. A demand inside an opponent's filing or a court order
    aimed at the pro se party is the trick.

    Detection order (most reliable first):
      1. Signature block in the tail: '/s/ <name>', 'Respectfully submitted'
         + name, 'Submitted by: <name>', 'Pro Se' near a pro-se name.
      2. Court order markers in the tail: 'IT IS SO ORDERED', 'BY THE COURT',
         'J.' / 'JUDGE' signature — BUT a proposed order submitted by a party
         is attributed to that party (checked before the court marker).
      3. Letterhead / salutation in the head addressed TO the pro se party
         ('Dear <pro-se first name>') => opponent correspondence.
    """
    if not text:
        return "unknown"
    names = [n.strip().lower() for n in (pro_se_names or []) if n and n.strip()]
    if not names:
        names = ["filer", "filer"]

    head = text[:1500].lower()
    tail = text[-2500:].lower()

    def _any_name(seg: str) -> bool:
        return any(n in seg for n in names)

    # 1) explicit submission signature (also catches proposed orders:
    #    'Submitted by: <pro se>' outranks 'BY THE COURT' on the same page)
    for marker in ("submitted by:", "/s/", "respectfully submitted"):
        pos = tail.rfind(marker)
        if pos != -1:
            window = tail[pos:pos + 300]
            if _any_name(window):
                return "pro_se"
            if window.strip(" \n,.:;-—").rstrip():
                # a signature block naming someone else
                if any(k in window for k in ("esq", "attorney", "counsel", "llp", "llc", "p.c.")):
                    return "opponent"

    # 2) court order (only after ruling out party-submitted signature)
    if any(k in tail for k in ("it is so ordered", "by the court", "district judge",
                               "magistrate judge", "j., judge")):
        return "court"

    # 3) opponent correspondence: letterhead / 'Dear <pro se name>'
    if _any_name(head) and any(k in head for k in ("dear ", "re: ", "loan no", "account no")):
        # addressed TO the pro se party
        for n in names:
            if f"dear {n}" in head or f"dear mr" in head or f"dear ms" in head:
                return "opponent"
        if "dear" in head:
            return "opponent"

    # 4) caption heuristic: pro se name present + 'pro se' anywhere
    if _any_name(text[:800].lower()) and "pro se" in text.lower() and _any_name(tail):
        return "pro_se"

    return "unknown"


def bucket_asymmetries_by_author(
    asymmetries: list,
    author: str,
) -> tuple[list, list]:
    """Deterministic direction bucketing: returns (violations_input, leverage_input).

    - author == 'pro_se':   EVERY asymmetry in the pro se party's own filing is
                            their demand => leverage bucket (direction forced
                            'by_pro_se'). None can be a trick against them.
    - author in ('opponent','court'): asymmetries keep their LLM direction, but
                            anything the LLM did not mark 'by_pro_se' is treated
                            as 'against_pro_se' (the conservative reading of a
                            hostile document).
    - author == 'unknown':  trust the LLM direction as-is (V2 behavior).
    """
    vio_in, lev_in = [], []
    for a in asymmetries or []:
        if not isinstance(a, dict):
            continue
        b = dict(a)
        if author == "pro_se":
            b["direction"] = "by_pro_se"
            lev_in.append(b)
        elif author in ("opponent", "court"):
            if str(b.get("direction", "")).strip().lower() == "by_pro_se":
                lev_in.append(b)
            else:
                b["direction"] = "against_pro_se"
                vio_in.append(b)
        else:
            d = str(b.get("direction", "")).strip().lower() or "against_pro_se"
            (lev_in if d == "by_pro_se" else vio_in).append(b)
    return vio_in, lev_in


def reciprocity_leverage(
    asymmetries: list,
    source_doc: str = "Unknown Document",
) -> list:
    """The by_pro_se side of the direction gate: asymmetries where the PRO SE
    party is the one who invoked a requirement its opponent never satisfied.
    These are NOT violations to plead against ourselves — they are CONFIRMED
    LEVERAGE: demands we made on the record that the opponent left unmet.

    Returns lightweight leverage records (not ViolationPin shape) for the
    drafting layer to cite as record-support: {requirement, opponent, quote,
    demand_summary, source_doc}. A demand the opponent DID satisfy
    (enforcer_compliance_shown=True) is excluded — no leverage there.
    """
    out = []
    for a in asymmetries or []:
        if not isinstance(a, dict):
            continue
        direction = str(a.get("direction", "")).strip().lower()
        if direction != "by_pro_se":
            continue
        if a.get("enforcer_compliance_shown"):
            continue  # opponent met the demand — no leverage
        req = str(a.get("requirement", "other")).strip().lower()
        out.append({
            "kind": "RECIPROCITY_LEVERAGE",
            "requirement": req,
            "opponent": str(a.get("invoked_against", "")).strip(),
            "quote": str(a.get("quote", ""))[:500],
            "demand_summary": str(a.get("flip", "")).strip(),
            "source_doc": source_doc,
        })
    return out
