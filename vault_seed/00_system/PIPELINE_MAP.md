# Beezle Drafting Pipeline Map (refreshed 2026-07-08)

> Supersedes the 2026-05-26 map, which described the retired dual-drafter era (beezle/generation
> drafter + case_e2e_v2 filing_composer). Since 2026-06-16 there is ONE drafter. The prior map's
> filing-class taxonomy (still correct doctrine) is preserved below.

## The single drafter — m0

`pipeline/motion_drafter_v2.py` ("m0") is the only drafting engine. Everything else feeds it or
consumes its output. **m0 is fragile and load-bearing: do not modify it** — orchestration
improvements go AROUND it (see the 2026-07-08 GO run, output/pipeline_hardening_efficiency_report.md).

```
email (docket_email_fetch / consumer_claim_scan)
  → intake  cases/<slug>/intake/  →  OCR sidecars intake/_ocr/  (scripts/ocr_intake.py,
                                      swept by the keeper every ~15 min — incremental)
  → ANALYSIS (deterministic, single source of truth)
      audit_writer.build_audit_data → output/audit/data_<safe_case_id>.json
        · skip_if_fresh: audit newer than newest intake file ⇒ no re-scan (reply flows)
      scorecard_orchestrator.build_scorecard_for_case (honors the fresh audit; refresh="force"
        for an unconditional re-scan)
      case_comprehension.run — per-question forensic loop; TWO full-agent lanes
        (local-gpu :8080 262k + manifest-fast local-gpu :8000 131k), both with corpus tools;
        definition-first depth blocks + term-journey seeds into the learning cycle
  → m0 DRAFT   motion_drafter_v2.process_stub (stub → 4 section LLM calls → assemble →
        cite-verify → schema/leak gates → definitions injection → docx). Median 42s/draft.
        LLAMA_URL env var selects the endpoint per subprocess (:8000 default).
  → PRE-FILE GATE (universal, non-blocking)  pre_file_review.gate_draft:
        Schedule-A injection if absent + adversarial review + authority-jurisdiction check;
        wired into m0-external producers (accountability_drafter, new_claim_drafter);
        writes <draft>.prefile.json
  → filing_status / consolidation / mailing (mailing_consolidator, LetterStream ledger)
    or CCP e-filing (pipeline/ccp_efiler.py + efiling/efile_run.py — proven live)
```

Deprecated (do not route new work through): `generation/reply_brief_drafter.py`,
`pipeline/filing_composer.py` (still used to render STUBS only), `pipeline/filing_pipeline.py`,
`beezle/generation/*` (v2 port, separate track), `case_e2e_v2/*`.

## Filing class taxonomy (unchanged doctrine)

> **Guiding principle:** Quality is non-negotiable. Brevity is opportunistic.

Three documents, three distinct jobs:

| Document | Attack vector | What court cannot ignore |
|---|---|---|
| Motion | Structural / argumentative | The relief requested + the rule basis |
| Scorecard Exhibit A | Rules + binding precedent | Each rule-violation row (mechanical, judicially-noticeable) |
| Reply brief / walkthrough | Per-citation | Each named cite (silence = waiver on appeal) |

- Scorecard makes the RULES side bulletproof — rule-mechanical tables ("Rule X requires Y. Record
  shows Z. Action: ..."), jurisdiction-branched via `scorecard_if_then._rule_for_jurisdiction`
  (state rules never leak into federal filings and vice versa; unmapped categories log to
  output/audit/needs_hook.log).
- Reply brief / walkthrough makes the CITATIONS side bulletproof — each non-binding cite named and
  isolated.
- The Motion is the spine pointing at both; the receipts live in the exhibits.
