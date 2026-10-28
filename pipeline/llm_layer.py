"""
pipeline/llm_layer.py â€” LLM integration via local llama.cpp server.

Model: local-model via llama-server Docker container
Endpoint: http://localhost:8000
API: llama.cpp native /completion (chat() now bypasses /v1/chat/completions)

ONLY used for:
  1. Structured fact extraction from documents
  2. Template-based legal document drafting

NEVER used for:
  - Legal conclusions
  - Rule creation
  - Memory storage

PATCH NOTE:
  The /v1/chat/completions endpoint with the local-model template
  routes model output into 'reasoning_content' instead of 'content', returning
  empty strings after 9-minute hangs. The chat() method has been rewritten to
  manually format ChatML and call /completion directly. Same input/output
  contract for callers, just bypasses the broken chat layer.
"""

import json
import re
import time
import requests
from typing import Optional

from core.logger import get_logger

log = get_logger("LLM")

# â”€â”€ Configuration â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

DEFAULT_LLM_URL = "http://127.0.0.1:8000"  # BLOCK_8_1_IPV4_LOCK
COMPLETION_ENDPOINT = "/completion"
CHAT_ENDPOINT = "/v1/chat/completions"
HEALTH_ENDPOINT = "/health"
SLOTS_ENDPOINT = "/slots"

DEFAULT_PARAMS = {
    "n_predict": 4096,
    "temperature": 0.1,
    "top_p": 0.9,
    "repeat_penalty": 1.15,
    "stop": ["<|user|>", "<|system|>", "[END]", "<|endoftext|>"],
}


class LlamaLLM:
    """Interface to local llama.cpp server."""

    def __init__(self, base_url: str = DEFAULT_LLM_URL, timeout: int = 300):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._last_elapsed_ms = 0

    def health_check(self) -> dict:
        """Check if llama-server is responsive."""
        try:
            r = requests.get(f"{self.base_url}{HEALTH_ENDPOINT}", timeout=5)
            if r.status_code == 200:
                return {"status": "ok", "data": r.json() if r.text else {}}
            return {"status": "error", "code": r.status_code}
        except requests.ConnectionError:
            return {"status": "offline", "error": "Cannot connect to llama-server"}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def check_slots(self) -> dict:
        """Check llama-server slot state for availability."""
        try:
            r = requests.get(f"{self.base_url}{SLOTS_ENDPOINT}", timeout=3)
            if r.status_code != 200:
                return {"available": True, "reason": f"slots endpoint returned {r.status_code}"}
            slots = r.json()
            if not slots:
                return {"available": True, "reason": "no slots reported"}
            busy = [s for s in slots if s.get("is_processing")]
            if not busy:
                return {"available": True, "reason": "idle"}
            if len(slots) > 1 and len(busy) < len(slots):
                return {"available": True, "reason": f"{len(busy)}/{len(slots)} busy, others free"}
            # Check for stuck slots (unlimited token budget)
            for s in busy:
                params = s.get("params", {})
                np = params.get("n_predict", 0)
                if np < 0:
                    return {"available": False, "reason": f"slot {s.get('id','?')} stuck (unlimited n_predict)"}
            return {"available": True, "reason": f"{len(busy)}/{len(slots)} busy but bounded"}
        except Exception as e:
            return {"available": True, "reason": f"slot check failed: {e}"}

    def complete(self, prompt: str, **kwargs) -> str:
        """Raw completion endpoint. Returns generated text."""
        params = {**DEFAULT_PARAMS, **kwargs}
        params["prompt"] = prompt

        t0 = time.time()
        log.info(f"Sending completion request ({len(prompt)} chars)")

        try:
            r = requests.post(
                f"{self.base_url}{COMPLETION_ENDPOINT}",
                json=params,
                timeout=self.timeout,
            )
            self._last_elapsed_ms = int((time.time() - t0) * 1000)

            if r.status_code != 200:
                log.error("complete", f"HTTP {r.status_code}", error=r.text[:200])
                raise RuntimeError(f"LLM error: {r.status_code} {r.text[:200]}")

            data = r.json()
            output = data.get("content", "").strip()
            log.stage("complete", f"Generated {len(output)} chars in {self._last_elapsed_ms}ms")
            return output

        except requests.ConnectionError:
            log.error("complete", "Connection failed", error="llama-server not running")
            raise RuntimeError("llama-server is not running. Start it with: docker start llama-server")
        except requests.Timeout:
            log.error("complete", "Timeout", error=f"Exceeded {self.timeout}s")
            raise RuntimeError(f"LLM request timed out after {self.timeout}s")

    def chat(self, messages: list[dict], thinking: bool = False, **kwargs) -> str:
        """Chat via /completion endpoint with manual ChatML formatting.

        PATCH: The /v1/chat/completions endpoint is broken with our patched
        Jinja template â€” model output ends up in 'reasoning_content' field
        instead of 'content', returning empty strings. The /completion endpoint
        with manually formatted ChatML works correctly. We bypass the broken
        chat layer entirely.

        Args:
            messages: Chat messages [{role, content}]
            thinking: If False (default), append empty <think></think> to skip
                      reasoning. If True, allow the model to think before
                      generating content (slower but higher quality drafts).
            **kwargs: Override default params (n_predict, temperature, etc.)
        """
        # Manually render ChatML format that the model expects
        prompt_parts = []
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            prompt_parts.append(f"<|im_start|>{role}\n{content}<|im_end|>\n")

        # Append assistant prompt
        if thinking:
            prompt_parts.append("<|im_start|>assistant\n<think>\n")
        else:
            # Empty think block tells model "reasoning done, emit content now"
            prompt_parts.append("<|im_start|>assistant\n<think>\n\n</think>\n\n")

        prompt = "".join(prompt_parts)

        params = {
            "prompt": prompt,
            "n_predict": kwargs.get("n_predict", DEFAULT_PARAMS["n_predict"]),
            "temperature": kwargs.get("temperature", DEFAULT_PARAMS["temperature"]),
            "top_p": kwargs.get("top_p", DEFAULT_PARAMS["top_p"]),
            "repeat_penalty": kwargs.get("repeat_penalty", DEFAULT_PARAMS["repeat_penalty"]),
            "stop": kwargs.get("stop", ["<|im_end|>", "<|im_start|>", "<|endoftext|>"]),
        }

        t0 = time.time()
        total_input = sum(len(m.get("content", "")) for m in messages)
        mode = "thinking" if thinking else "no-think"
        log.info(f"Sending chat request ({len(messages)} messages, {total_input} chars, {mode}) [via /completion]")

        try:
            r = requests.post(
                f"{self.base_url}{COMPLETION_ENDPOINT}",
                json=params,
                timeout=self.timeout,
            )
            self._last_elapsed_ms = int((time.time() - t0) * 1000)

            if r.status_code != 200:
                log.error("chat", f"HTTP {r.status_code}", error=r.text[:200])
                raise RuntimeError(f"LLM error: {r.status_code} {r.text[:200]}")

            data = r.json()
            output = data.get("content", "").strip()

            # Strip any leftover <think> blocks just in case
            output = re.sub(r'<think>.*?</think>', '', output, flags=re.DOTALL).strip()
            # Strip any leftover ChatML markers
            output = output.replace("<|im_end|>", "").replace("<|im_start|>", "").strip()

            log.stage("chat", f"Generated {len(output)} chars in {self._last_elapsed_ms}ms ({mode})")
            return output

        except requests.ConnectionError:
            log.error("chat", "Connection failed", error="llama-server not running")
            raise RuntimeError("llama-server is not running. Start it with: docker start llama-server")
        except requests.Timeout:
            log.error("chat", "Timeout", error=f"Exceeded {self.timeout}s")
            raise RuntimeError(f"LLM request timed out after {self.timeout}s")

    def extract_facts(self, document_text: str) -> list[str]:
        """Extract structured facts from a document using LLM.

        This is one of ONLY TWO permitted LLM uses.
        Returns a list of discrete factual statements.
        """
        system = """You are a legal fact extraction engine. Extract all legally relevant facts.
Return ONLY a JSON array of strings. Each fact must be:
- A single discrete factual statement
- Include dates, names, amounts, specific actions
- No legal conclusions or interpretations
- No opinions or characterizations
Return valid JSON only. No markdown, no explanation."""

        user = f"""Extract all legally relevant facts from this document:

{document_text[:10000]}"""

        output = ""
        try:
            output = self.chat([
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ], thinking=False)  # NO thinking for extraction â€” fast mode

            # Parse JSON array from response
            # Handle cases where LLM wraps in markdown
            cleaned = output.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[-1]
                if cleaned.endswith("```"):
                    cleaned = cleaned[:-3]
                cleaned = cleaned.strip()

            facts = json.loads(cleaned)
            if isinstance(facts, list):
                log.stage("extract_facts", f"LLM extracted {len(facts)} facts")
                return [str(f) for f in facts if f]
            else:
                log.warning("extract_facts", "LLM returned non-list, falling back")
                return []

        except json.JSONDecodeError as e:
            log.warning("extract_facts", f"JSON parse failed: {e}", error=output[:200] if output else "empty")
            # Fallback: split by newlines and clean
            lines = output.strip().split("\n")
            facts = [l.strip().lstrip("- â€¢*0123456789.").strip() for l in lines if len(l.strip()) > 10]
            return facts[:50]
        except Exception as e:
            log.error("extract_facts", "LLM fact extraction failed", error=str(e))
            return []

    def draft_document(self, system_prompt: str, user_prompt: str) -> str:
        """Generate a legal document draft using LLM + template.

        This is the SECOND of ONLY TWO permitted LLM uses.
        Returns the raw drafted text.
        """
        try:
            output = self.chat([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ], thinking=True, n_predict=4096)  # THINKING ON for drafting â€” quality mode

            log.stage("draft_document", f"Draft generated: {len(output)} chars")
            return output

        except Exception as e:
            log.error("draft_document", "Draft generation failed", error=str(e))
            raise


# â”€â”€ Singleton â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

_instance: Optional[LlamaLLM] = None

def get_llm(base_url: str = DEFAULT_LLM_URL) -> LlamaLLM:
    """Get or create the LLM singleton."""
    global _instance
    if _instance is None or _instance.base_url != base_url:
        _instance = LlamaLLM(base_url)
    return _instance


# -- Shared embedding singleton (GO step 6, 2026-07-08) ----------------------------------------
# The corpus embedder was constructed inline in 11+ modules (seconds + VRAM per load). One process,
# one model. Import: from pipeline.llm_layer import get_embedder
_EMBEDDER = None

def get_embedder():
    """Get or create the shared SentenceTransformer("BAAI/bge-base-en-v1.5") singleton.
    NEVER change the model name/dimension here — it must match the ingested corpus (config.yaml)."""
    global _EMBEDDER
    if _EMBEDDER is None:
        from sentence_transformers import SentenceTransformer
        _EMBEDDER = SentenceTransformer("BAAI/bge-base-en-v1.5")
    return _EMBEDDER
