---
name: judicial-misconduct-tracker
description: Identify patterns of judicial error across cases — for building
  claims against judges. Track per-judge violation patterns and cross-case
  findings.
when_to_use: When you need to build a claim against a judge or when you need
  to show a pattern of judicial error across multiple cases.
---

# Judicial Misconduct Tracker

## Overview

Judges can be sued for actions taken in their judicial capacity only if they
acted in clear absence of jurisdiction or violated clearly established law.
This skill tracks patterns across cases to build those claims.

## Workflow

### Step 1: Query the corpus for the judge's name

    GET http://localhost:8100/search_text?query=JUDGE+NAME+judicial+misconduct&top_k=10

### Step 2: Query the corpus for the judge's decisions

    GET http://localhost:8100/search_text?query=JUDGE+NAME+order+decision&top_k=10

### Step 3: Cross-reference with Beezle MCP

- `mcp_beezle_case_audit_summary` — get audit findings for a case
- `mcp_beezle_case_audit_summary` — get flagged violations for a case
- `mcp_beezle_analyze_immunity` — analyze immunity status for a government
  defendant

### Step 4: Build the pattern

For each judge, document:
- Cases where the judge made errors
- Types of errors (procedural, substantive, bias)
- Whether the errors were corrected on appeal
- Whether the errors violated clearly established law

## Key patterns

### Clear absence of jurisdiction

- Judge acts in a case where they have no jurisdiction
- Judge issues orders beyond their authority
- Judge fails to recuse when required

### Violation of clearly established law

- Judge ignores binding precedent
- Judge misapplies a statute
- Judge violates a procedural rule

### Bias

- Judge shows bias against pro se litigants
- Judge shows bias against a particular party
- Judge makes rulings based on personal opinion rather than law

## Example: Judge {{JUDGE}} in EDPA

1. GET /search_text?query={{JUDGE}} EDPA judicial misconduct
2. GET /search_text?query={{JUDGE}} EDPA order decision
3. Cross-reference with Beezle MCP case audits
4. Document pattern: Judge {{JUDGE}} has made X errors in Y cases
5. Build claim: Judge {{JUDGE}} acted in clear absence of jurisdiction

## Pitfalls

- Don't assume a judge is acting improperly — verify the law
- Don't assume a judge is biased — verify the facts
- Don't assume a judge is beyond jurisdiction — verify the jurisdiction
- When in doubt, query the corpus directly rather than relying on MCP tool
  defaults
