"""
pipeline.document_intake.extractor
====================================

LLM extraction layer. Calls LlamaLLM client, parses JSON output, repairs
common LLM quirks (markdown fences, prose, trailing commas).

Determinism guarantees:
  - temperature=0 (greedy decoding)
  - top_p=1.0
  - thinking=False (no reasoning tokens, just extraction)
  - Tight stop tokens
  - Schema validation hard-gates output

If LLM output cannot be repaired into valid JSON matching the schema,
returns None. Fail-closed by design.
"""

from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

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

PROMPTS_DIR = Path(__file__).parent / "prompts"
SCHEMAS_DIR = Path(__file__).parent / "schemas"

_prompt_cache: dict[str, str] = {}
_schema_cache: dict[str, str] = {}


def load_prompt_template(filing_type: str, version: str = "v1") -> Optional[str]:
    """Load a versioned prompt template. Cached after first load."""
    key = f"{filing_type}_{version}"
    if key in _prompt_cache:
        return _prompt_cache[key]

    path = PROMPTS_DIR / f"{filing_type}_{version}.txt"
    if not path.exists():
        return None

    text = path.read_text(encoding="utf-8")
    _prompt_cache[key] = text
    return text


def load_schema_inline(schema_name: str) -> Optional[str]:
    """Load schema JSON as a string (for inlining into prompts). Cached."""
    if schema_name in _schema_cache:
        return _schema_cache[schema_name]

    path = SCHEMAS_DIR / f"{schema_name}.json"
    if not path.exists():
        return None

    # Re-serialize the JSON to normalize indentation
    data = json.loads(path.read_text(encoding="utf-8"))
    text = json.dumps(data, indent=2)
    _schema_cache[schema_name] = text
    return text


def repair_and_parse_json(raw: str) -> tuple[Optional[dict], list[str]]:
    """
    Try to extract a clean JSON object from LLM output.

    Handles common LLM output quirks:
      - Markdown code fences (```json ... ```)
      - Prose before or after the JSON
      - Trailing commas in objects/arrays

    Returns:
        (parsed_dict | None, list_of_repairs_applied)
    """
    if not raw or not isinstance(raw, str):
        return None, ["empty_input"]

    repairs: list[str] = []
    text = raw.strip()

    # Try direct parse first
    try:
        return json.loads(text), repairs
    except json.JSONDecodeError:
        pass

    # Strip markdown code fences
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*\n", "", text)
        text = re.sub(r"\n```\s*$", "", text)
        repairs.append("stripped_markdown_fences")

    # Strip prose before/after the JSON object
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace >= 0 and last_brace > first_brace:
        if first_brace > 0 or last_brace < len(text) - 1:
            text = text[first_brace:last_brace + 1]
            repairs.append("stripped_surrounding_prose")

    # Try parse again
    try:
        return json.loads(text), repairs
    except json.JSONDecodeError:
        pass

    # Remove trailing commas before } or ]
    cleaned = re.sub(r",(\s*[}\]])", r"\1", text)
    if cleaned != text:
        repairs.append("removed_trailing_commas")
        text = cleaned

    try:
        return json.loads(text), repairs
    except json.JSONDecodeError as e:
        repairs.append(f"unparseable: {e}")
        return None, repairs


def extract_with_llm(
    document_text: str,
    doc_path: Path,
    filing_type: str,
    content_hash: str,
    extractor_model: str,
    prompt_version: str = "v1",
) -> Optional[dict]:
    """
    Run LLM extraction on a document.

    Args:
        document_text: full text of the document
        doc_path: path to source document
        filing_type: e.g. "order" — determines which prompt + schema to use
        content_hash: SHA-256 of document text (used in metadata)
        extractor_model: model version pin (used in metadata)
        prompt_version: which version of the prompt template to use

    Returns:
        Parsed JSON dict (NOT yet validated against schema — caller validates)
        None if LLM call failed, output unparseable, or filing_type unsupported
    """
    # Load template. CORE_FALLBACK_V1: no type-specific prompt -> generic core_v1.
    _core_fallback = False
    template = load_prompt_template(filing_type, prompt_version)
    if template is None:
        template = load_prompt_template("core", prompt_version)
        _core_fallback = True
    if template is None:
        return None

    # Inline the schema into the template
    if _core_fallback:
        schema_inline = load_schema_inline("core_schema")
    else:
        schema_inline = load_schema_inline(f"{filing_type}_{prompt_version}")
        if schema_inline is None:
            schema_inline = load_schema_inline("core_schema")
    if schema_inline is None:
        return None

    # HYBRID_ECF_ANCHOR_V1: a deterministic CM/ECF header scan (pdftotext, no LLM)
    # gives the model a VERIFIED hint to confirm or correct. The own-vs-cited
    # reasoning lives in the prompt; this just anchors it. Empty for non-PDFs or
    # image/garbled headers -> the model is told to read the stamp itself.
    anchor_hint = "deterministic header scan unavailable for this file type"
    try:
        if str(doc_path).lower().endswith(".pdf"):
            from pipeline.ecf_resolver import _pdf_header
            _cno, _ecf = _pdf_header(doc_path, allow_ocr=False)
            if _ecf is not None:
                anchor_hint = (f"a header scan read: case {_cno or '?'}, "
                               f"Document {_ecf}. Confirm against the stamp text.")
            else:
                anchor_hint = ("no 'Document N' stamp found by the deterministic "
                               "scan (image or garbled header) — read the stamp "
                               "yourself if it is visible in the text, else null.")
    except Exception:
        pass

    # Substitute template variables
    prompt = template.format(
        schema_inline=schema_inline,
        doc_id=doc_path.name,
        doc_path=str(doc_path),
        content_hash=content_hash,
        extractor_model=extractor_model,
        extracted_at=datetime.now().isoformat(timespec="seconds"),
        document_text=document_text,
        anchor_hint=anchor_hint,
    )

    # Call LLM via the existing client
    try:
        from pipeline.llm_layer import get_llm
        llm = get_llm()
    except Exception as e:
        return None

    # chat() with temperature=0 for determinism, no thinking tokens
    try:
        raw_output = llm.chat(
            messages=[{"role": "user", "content": prompt}],
            thinking=False,
            temperature=0.0,
            top_p=1.0,
            n_predict=4096,
            stop=["<|im_end|>", "<|im_start|>", "<|endoftext|>"],
        )
    except Exception as e:
        return None

    if not raw_output:
        return None

    # Parse + repair
    parsed, repairs = repair_and_parse_json(raw_output)
    if parsed is None:
        # Log the failure for debugging
        _log_extraction_failure(doc_path, filing_type, raw_output, repairs)
        return None

    # Defensive sanitization — strip JSON Schema artifacts the LLM may echo
    # E.g. "$schema": "http://json-schema.org/draft-07/schema#" sometimes appears
    # as a top-level field if the LLM mirrors the schema metadata.
    for k in list(parsed.keys()):
        if k.startswith("$"):
            parsed.pop(k, None)

    # Inject metadata fields the LLM shouldn't fabricate
    parsed["doc_id"] = doc_path.name
    parsed["doc_path"] = str(doc_path)
    parsed["content_hash"] = content_hash
    parsed["extractor_model"] = extractor_model
    parsed["extractor_prompt_version"] = f"{filing_type}_{prompt_version}"
    if "extracted_at" not in parsed or not parsed["extracted_at"]:
        parsed["extracted_at"] = datetime.now().isoformat(timespec="seconds")

    return parsed


def _log_extraction_failure(doc_path: Path, filing_type: str,
                             raw_output: str, repairs: list[str]) -> None:
    """Log unparseable LLM output for debugging."""
    log_path = (_BEEZLE_ROOT / "output" / "audit" / "extraction_failures.log")
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"\n{'=' * 70}\n")
            f.write(f"timestamp: {datetime.now().isoformat()}\n")
            f.write(f"doc: {doc_path.name}\n")
            f.write(f"filing_type: {filing_type}\n")
            f.write(f"repairs_attempted: {repairs}\n")
            f.write(f"raw_output (first 2000 chars):\n")
            f.write(raw_output[:2000])
            f.write(f"\n{'=' * 70}\n")
    except Exception:
        pass


def _log_low_confidence(doc_path: Path, filing_type: str, json_output: dict) -> None:
    """Log extractions where confidence is 'low'."""
    log_path = (_BEEZLE_ROOT / "output" / "audit" / "low_confidence_extractions.log")
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            ts = datetime.now().isoformat(timespec="seconds")
            notes = json_output.get("extraction_notes", {})
            unparseable = notes.get("fields_unparseable", [])
            warnings = notes.get("warnings", [])
            f.write(f"{ts}  {doc_path.name}  filing_type={filing_type}  "
                    f"unparseable={unparseable}  warnings={warnings}\n")
    except Exception:
        pass


# Filing types that have wired prompts + schemas
SUPPORTED_FOR_EXTRACTION = {"order"}  # Step 4 expands this set


def is_extraction_supported(filing_type: str) -> bool:
    """Check if a filing type has a wired prompt + schema."""
    return filing_type in SUPPORTED_FOR_EXTRACTION
