# Document Intake Schemas

Phase 3F document extraction schemas. Each schema defines the strict JSON
output format for one filing type.

## Files

- `core_schema.json` — Master schema. All filing-type schemas inherit
  required fields from this. Every extracted document must validate
  against this OR a more specific subtype schema.

- `order_v1.json` — Court orders. Stricter than core_schema:
  - filer_role MUST be "court"
  - filing_type MUST be "order"
  - claims, positions, definitions MUST be empty
  - rulings MUST have at least 1 item

## Versioning

When a schema changes:
1. Bump the version (e.g., `order_v1.json` → `order_v2.json`)
2. Update `EXTRACTOR_PROMPT_VERSION` in `intake.py`
3. Old cached entries become invalid (cache miss); new extractions run

## Validation

```python
from pipeline.document_intake.schema_validator import validate_against_schema

is_valid, errors = validate_against_schema(json_data, "order_v1")
```

## Adding a new filing type

1. Create `<filing_type>_v1.json` here
2. Create `../prompts/<filing_type>_v1.txt`
3. Add classifier pattern in `intake.py` if needed
4. Bump prompt version constant
