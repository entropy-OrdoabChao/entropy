# Document Intake Prompts

Phase 3F LLM extraction prompt templates. Each prompt corresponds to
exactly one schema version.

## Files

- `order_v1.txt` — Court order extraction prompt. Pairs with
  `../schemas/order_v1.json`.

## Template variables

Prompts use Python `.format()` substitution:

- `{schema_inline}` — Schema JSON inlined into the prompt
- `{doc_id}` — Document filename
- `{doc_path}` — Absolute path
- `{content_hash}` — SHA-256 of document text
- `{extractor_model}` — Model version pin
- `{extracted_at}` — ISO 8601 timestamp
- `{document_text}` — Full extracted document text

## Strict rules

Every prompt MUST:
1. Instruct LLM to output JSON only (no prose)
2. Include the schema verbatim
3. Forbid invented values
4. Require verbatim quotes for any text reproduction
5. Forbid guessing page numbers (use null instead)

## When to bump version

Bump the version when:
- Schema changes
- Extraction rules change
- New required fields added
- Behavior of existing fields changes

Old cached extractions become invalid automatically.
