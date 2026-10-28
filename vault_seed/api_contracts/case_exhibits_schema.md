---
type: spec
schema: case_exhibits
last_updated: 2026-05-03
tags:
  - beezle
  - schema
  - exhibits
  - v2
---

# Case Exhibit Map Schema

How to define exhibit-to-file mappings for a complaint, so the exhibit packager bundles the right PDFs without fuzzy filename guessing.

## Where files live

Each case gets a folder:

```
obsidian_sync/cases/<case_id>/
    <case_id>_case.md       ← exhibit map + defendants + dates (this schema)
```

Recommended `case_id` formats:
- `{{CASE_SLUG}}_446-5514079-703`
- `{{CASE_SLUG}}_2026-0417`
- `{{CASE_SLUG}}_{{CASE_SLUG}}_2026`
- `fcra_credit_denial_2026`

## Required YAML blocks inside the .md file

The `exhibit_packager.py` extracts these via simple regex over the markdown content. Plain YAML inside fenced code blocks works.

### exhibits

Required. Maps slot letters to source files.

```yaml
exhibits:
  - letter: A
    description: FOIA Request
    date: 2026-01-08
    file_pattern: Gmail_-_FOIA_Request___FHA_Case_446-5514079-703
```

- `letter` — A through H (or further if more exhibits)
- `description` — used for the cover page title
- `date` — used for the cover page date
- `file_pattern` — unique substring guaranteed to appear in only one file's name (case-insensitive). The packager substring-matches this against filenames in `output/email_scans/attachments/` after stripping the content-hash prefix.

### defendants (optional)

Used by `build_structured_defendants` to override default detection.

```yaml
defendants:
  - name: Yolonda D. Hatton
    role: official
    title: Government Information Specialist
    department: HUD FOIA Office
    capacity: her official capacity
```

### tracking_numbers (optional)

Used by the narrative builder for fact paragraphs.

```yaml
tracking_numbers:
  request: 26-FI-HQ-00985
  appeal: 26-A-C-00014
```

### dates (optional)

Used for the dated Statement of Facts section.

```yaml
dates:
  request_submitted: 2026-01-08
  acknowledged: 2026-01-13
  deadline: 2026-02-06
  appeal_filed: 2026-02-07
  appeal_acknowledged: 2026-02-11
  appeal_determination: 2026-03-06
```

## How the packager finds the right file

For each exhibit slot's `file_pattern`:

1. Get all PDFs in `output/email_scans/attachments/` matching `*.pdf`
2. Strip the content-hash prefix (e.g., `1595f9c8_`) from the filename
3. Substring-match `file_pattern` against the stripped filename, case-insensitive
4. First match wins; the file is marked "used" so it can't be assigned to another slot

If a slot has no match, the packager logs `[EXHIBIT-WARN]` and leaves that slot empty. The cover page is still produced.

## Fallback when no map exists

If no `_case.md` exists in `obsidian_sync/cases/`, the packager falls back to legacy fuzzy keyword patterns. Those patterns are tuned for HUD FOIA. They will NOT work well for non-FOIA cases. Always create a case map before running a non-HUD complaint.

## Why this exists (v2 lessons)

We discovered that the fuzzy pattern matcher was assigning random files to exhibit slots when patterns missed. In one bad run, Exhibit B was a Third Circuit dismissal order from a different case, and Exhibit A was the FOIA request bundled with the wrong attachments.

The root cause: filename-based heuristics over a directory containing 120+ PDFs from MULTIPLE cases. Pattern matching is brittle when the haystack is large and contains adversarial content (other cases' files).

The fix: **pin the exact file per slot in Obsidian.** No guessing. If the file doesn't match, the warning surfaces it. Each new case explicitly declares its exhibit list.

## Adding a new case

1. Create `obsidian_sync/cases/<case_id>/<case_id>_case.md` with at minimum the `exhibits:` block
2. Make sure each `file_pattern` is unique enough to identify exactly one file
3. Run the complaint pipeline — packager will pick up the map automatically
4. Watch for `[EXHIBIT]` log lines showing the map loaded and slot fill rate
5. Watch for `[EXHIBIT-WARN]` lines indicating misses

## Changelog

- **2026-05-03** — Schema created. First case wired up: `{{CASE_SLUG}}_446-5514079-703`. Replaces fuzzy fallback as the primary mechanism.
