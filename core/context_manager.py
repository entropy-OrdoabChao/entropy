"""
core/context_manager.py — Controls all data flow to LLM.

Rules:
- NEVER accumulate context across steps
- Context is rebuilt per step from scratch
- Only required data is passed forward
- Priority: Facts > Relevant Law > Required Authority > Patterns
- Enforces token budget limits
"""

from typing import Optional
from core.logger import get_logger

log = get_logger("CONTEXT_MGR")

# Max characters sent to LLM per call (roughly ~3k tokens)
MAX_CONTEXT_CHARS = 12000
MAX_FACTS_CHARS = 3000
MAX_LAW_CHARS = 5000
MAX_AUTHORITY_CHARS = 3000
MAX_PATTERN_CHARS = 1000


class ContextManager:
    """Builds minimal, controlled context for each LLM call.

    Stateless — does NOT store anything between calls.
    Each build_context() call produces a fresh context package.
    """

    def build_context(
        self,
        facts: str = "",
        relevant_law: list[dict] = None,
        authorities: list[dict] = None,
        patterns: list[str] = None,
        task_instruction: str = "",
    ) -> dict:
        """Build a minimal context package for one LLM call.

        Args:
            facts: Extracted case facts (highest priority)
            relevant_law: Retrieved statutes/rules from DB
            authorities: Retrieved case law citations
            patterns: Obsidian patterns (lowest priority, optional)
            task_instruction: What the LLM should do with this context

        Returns:
            dict with 'system_prompt', 'user_content', 'token_estimate'
        """
        relevant_law = relevant_law or []
        authorities = authorities or []
        patterns = patterns or []

        # Truncate each section to its budget
        facts_block = self._truncate(facts, MAX_FACTS_CHARS, "facts")
        law_block = self._build_law_block(relevant_law)
        auth_block = self._build_authority_block(authorities)
        pattern_block = self._build_pattern_block(patterns)

        # Assemble user content with priority ordering
        sections = []
        if facts_block:
            sections.append(f"## CASE FACTS\n{facts_block}")
        if law_block:
            sections.append(f"## GOVERNING LAW\n{law_block}")
        if auth_block:
            sections.append(f"## CONTROLLING AUTHORITY\n{auth_block}")
        if pattern_block:
            sections.append(f"## RELEVANT PATTERNS\n{pattern_block}")

        user_content = "\n\n".join(sections)

        # Final truncation if still over budget
        if len(user_content) > MAX_CONTEXT_CHARS:
            user_content = user_content[:MAX_CONTEXT_CHARS] + "\n\n[CONTEXT TRUNCATED]"
            log.warning("build_context", "Context exceeded budget, truncated",
                     error=f"{len(user_content)} chars")

        # Add task instruction
        if task_instruction:
            user_content = f"## TASK\n{task_instruction}\n\n{user_content}"

        token_estimate = len(user_content) // 4  # rough estimate

        log.stage("build_context",
                  f"Built context: {len(facts_block)}F + {len(law_block)}L + "
                  f"{len(auth_block)}A + {len(pattern_block)}P = ~{token_estimate} tokens")

        return {
            "user_content": user_content,
            "token_estimate": token_estimate,
            "sections_included": {
                "facts": bool(facts_block),
                "law": bool(law_block),
                "authority": bool(auth_block),
                "patterns": bool(pattern_block),
            },
        }

    def build_extraction_context(self, document_text: str) -> dict:
        """Build minimal context for fact extraction (LLM call).

        Only sends the document text + extraction instruction.
        No law, no authority, no patterns.
        """
        truncated = self._truncate(document_text, MAX_CONTEXT_CHARS, "document")

        instruction = """Extract all legally relevant facts from this document.
Return ONLY a JSON array of fact strings. Each fact must be:
- A single discrete factual statement
- Include dates, names, amounts, actions where present
- No legal conclusions or interpretations
- No opinions or characterizations

Example format:
["On 2024-01-15, Defendant XYZ Bank sent a notice to Plaintiff.",
 "The notice did not include the required verification language under 15 USC 1692g.",
 "Plaintiff disputed the debt in writing on 2024-01-20."]"""

        return {
            "user_content": f"{instruction}\n\n## DOCUMENT\n{truncated}",
            "token_estimate": len(truncated) // 4,
            "sections_included": {"document": True},
        }

    def build_draft_context(
        self,
        doc_type: str,
        facts: str,
        law_block: str,
        authority_block: str,
        scorecard: Optional[dict] = None,
    ) -> dict:
        """Build context specifically for document drafting.

        Includes scorecard binding if present.
        """
        sections = [f"## DOCUMENT TYPE\n{doc_type}"]

        if scorecard:
            sc_text = (
                f"Score: {scorecard.get('score', 'N/A')}\n"
                f"Determination: {scorecard.get('determination', 'N/A')}\n"
                f"Draft Language: {scorecard.get('draft_language', 'N/A')}\n"
                f"Authority: {scorecard.get('controlling_authority', 'N/A')}"
            )
            sections.append(f"## SCORECARD (BINDING)\n{sc_text}")

        sections.append(f"## FACTS\n{self._truncate(facts, MAX_FACTS_CHARS, 'facts')}")
        sections.append(f"## LAW\n{self._truncate(law_block, MAX_LAW_CHARS, 'law')}")
        sections.append(f"## AUTHORITY\n{self._truncate(authority_block, MAX_AUTHORITY_CHARS, 'authority')}")

        user_content = "\n\n".join(sections)

        if len(user_content) > MAX_CONTEXT_CHARS:
            user_content = user_content[:MAX_CONTEXT_CHARS] + "\n\n[CONTEXT TRUNCATED]"

        return {
            "user_content": user_content,
            "token_estimate": len(user_content) // 4,
            "sections_included": {
                "doc_type": True,
                "scorecard": bool(scorecard),
                "facts": True,
                "law": True,
                "authority": True,
            },
        }

    # ══════════════════════════════════════════════════════════════════════
    # INTERNAL HELPERS
    # ══════════════════════════════════════════════════════════════════════

    def _truncate(self, text: str, max_chars: int, label: str) -> str:
        if not text:
            return ""
        if len(text) <= max_chars:
            return text.strip()
        log.warning("truncate", f"{label} truncated from {len(text)} to {max_chars} chars")
        return text[:max_chars].strip() + f"\n[...{label} truncated...]"

    def _build_law_block(self, laws: list[dict]) -> str:
        if not laws:
            return ""
        lines = []
        char_count = 0
        for law in laws:
            entry = (
                f"### {law.get('citation', 'Unknown')}\n"
                f"{law.get('text', '').strip()}\n"
            )
            if char_count + len(entry) > MAX_LAW_CHARS:
                lines.append("[...additional law truncated...]")
                break
            lines.append(entry)
            char_count += len(entry)
        return "\n".join(lines)

    def _build_authority_block(self, authorities: list[dict]) -> str:
        if not authorities:
            return ""
        lines = []
        char_count = 0
        for auth in authorities:
            entry = (
                f"- **{auth.get('citation', 'Unknown')}** "
                f"({auth.get('jurisdiction', '?')}, {auth.get('binding_level', '?')})\n"
                f"  {auth.get('holding', '').strip()}\n"
            )
            if char_count + len(entry) > MAX_AUTHORITY_CHARS:
                lines.append("[...additional authority truncated...]")
                break
            lines.append(entry)
            char_count += len(entry)
        return "\n".join(lines)

    def _build_pattern_block(self, patterns: list[str]) -> str:
        if not patterns:
            return ""
        text = "\n".join(f"- {p}" for p in patterns)
        return self._truncate(text, MAX_PATTERN_CHARS, "patterns")
