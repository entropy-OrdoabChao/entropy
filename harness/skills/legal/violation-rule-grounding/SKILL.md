---
name: violation-rule-grounding
description: Ground audit violation categories in the corpus by finding the exact controlling legal rule for each.
version: 0.1.0
metadata:
  hermes:
    tags: Legal, Audit, Corpus, Authority
---

# Violation Rule Grounding

Given a violation category from the case audit (e.g. NON_BINDING_AS_CONTROLLING, PRIMA_FACIE_EVIDENCE_IGNORED, TWOMBLY_IQBAL_MISAPPLICATION), find the exact controlling legal rule in the corpus — not just any hit, but the authoritative passage that directly addresses that violation type.

## Step 1: Identify the violation category

From the audit output, extract the violation category label. If the user asks for a specific finding, note the category they reference.

## Step 2: Map to search queries

Use the BREACH_MAP from `pipeline/judicial_oath_breach.py` to understand what the violation means, then construct search queries for the corpus. Map keywords to corpus search terms:

- `NON_BINDING_AS_CONTROLLING` / `TRIAL_COURT_AS_APPELLATE` → search for rules about which court authority is binding (e.g., "lower court not controlling", "stare decisis lower court binding")
- `PRIMA_FACIE_EVIDENCE_IGNORED` → search for the controlling rule on accepting well-pleaded facts as true (e.g., "principle established that all well pleaded facts accepted true")
- `TWOMBLY_IQBAL_MISAPPLICATION` → search for the correct standard on motion to dismiss (e.g., "Twombly Iqbal plausibility standard")
- `CONCLUSORY` → search for rules against conclusory dismissals without element-by-element analysis
- `BIAS` / `PARTIAL` → search for impartiality requirements (e.g., "impartiality reasonably questioned")
- `DUE_PROCESS` / `NOTICE_INADEQUATE` → search for due process / notice requirements
- `VOID_JUDGMENT` / `RULE_60B4` → search for void judgment / jurisdiction requirements

## Step 3: Query the corpus

Use `search_text` endpoint (GET http://localhost:8100/search_text?query=QUERY) for each keyword mapping. Run multiple queries — the first one rarely returns the exact quote needed.

For each query:
1. Run the search via the corpus API
2. Check if the top hit directly addresses the violation type
3. If not, try the next mapped query
4. Keep going until you find a passage that EXACTLY addresses the violation

## Step 4: Extract the exact quote

Once you find the right passage, extract:
- The full quote (not just the excerpt)
- The source (case name, volume, reporter, page)
- The specific rule it establishes

## Pitfalls

- **Don't stop at the first hit** — the corpus is 35.6GB, so the top result may be tangentially related but not the controlling rule for this specific violation type
- **Don't summarize the rule** — the user needs the exact quote + citation, not your paraphrase
- **The BREACH_MAP is the source of truth** for what each violation category means — do not guess at the meaning
- **session_search is your cross-referencing tool** — when the user references a breach without restating it, use session_search to find what was discussed previously

## Verification

The skill worked when you can answer: "What is the exact controlling rule in the corpus for [violation category]?" with a direct quote and its source citation.
