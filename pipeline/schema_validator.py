#!/usr/bin/env python3
"""
schema_validator.py — Validates rendered legal-filing markdown against
schema_contract.yaml block contracts and court-level rules.

Standalone module + CLI. Lives at pipeline/schema_validator.py.

Usage:
    # Validate one markdown file:
    python3 -m pipeline.schema_validator validate /path/to/FILING.md \\
        --case-id fed:2:26-cv-00123 --filing-type motion_to_vacate \\
        [--court-id EDPA] [--judge "the-judge"] [--posture merits] [--log]

    # Baseline sweep across all canonical drafts in obsidian_sync/cases/:
    python3 -m pipeline.schema_validator baseline [--case fed_26-cv-00123] [--log] [--verbose]

    # Inspect contract for one block:
    python3 -m pipeline.schema_validator show-block sections/introduction
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import argparse
import json
import re
import sys
import yaml
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# -----------------------------------------------------------------------------
# Path resolution (WSL-aware per lessons_learned 2026-05-25 ROOT pattern)
# -----------------------------------------------------------------------------
_ROOT_WSL = _eroot()
_ROOT_WIN = _eroot()
ROOT = _ROOT_WSL if _ROOT_WSL.exists() else _ROOT_WIN

SCHEMA_PATH = ROOT / "obsidian_sync" / "templates" / "schema_contract.yaml"
CASES_DIR = ROOT / "obsidian_sync" / "cases"
REVIEW_DIR = ROOT / "_review"


# -----------------------------------------------------------------------------
# Slug ↔ case_id translation. Extend as new cases are filed.
# -----------------------------------------------------------------------------
SLUG_TO_CASE_ID = {
    "fed_26-cv-00123":     "fed:2:26-cv-00123",
    "fed_26-cv-00456":     "fed:2:26-cv-00456",
    "fed_26-cv-00789":     "fed:2:26-cv-00789",
    "phila_ccp_260100000": "phila_ccp:260100000",
}

DEFAULT_JUDGE_PER_CASE = {
    "fed:2:26-cv-00123":   "the-judge",
    "fed:2:26-cv-00456":   "the-judge",
    "fed:2:26-cv-00789":   "the-judge",
    "phila_ccp:260100000": "Hall",
}


def infer_court_id(case_id: str) -> str:
    if case_id.startswith("fed:"):       return "EDPA"
    if case_id.startswith("phila_ccp:"): return "PA_CP_PHILA"
    if case_id.startswith("3d_cir:"):    return "3D_CIR"
    if case_id.startswith("scotus:"):    return "SCOTUS"
    if case_id.startswith("pa_super:"):  return "PA_SUPER"
    return "EDPA"


def slug_to_case_id(slug: str) -> str:
    if slug in SLUG_TO_CASE_ID:
        return SLUG_TO_CASE_ID[slug]
    if slug.startswith("fed_"):
        return "fed:2:" + slug[4:]
    if slug.startswith("phila_ccp_"):
        return "phila_ccp:" + slug[10:]
    return slug


# -----------------------------------------------------------------------------
# H2 markdown header → block_id mapping. Best-effort; falls back to
# document-level validation when a section doesn't match.
# -----------------------------------------------------------------------------
HEADER_TO_BLOCK = {
    "I. INTRODUCTION":                   "sections/introduction",
    "INTRODUCTION":                      "sections/introduction",
    "FACTUAL STATEMENT":                 "sections/factual_statement_federal",
    "II. STATEMENT OF FACTS":            "sections/factual_statement_federal",
    "STATEMENT OF FACTS":                "sections/factual_statement_federal",
    "PROCEDURAL HISTORY":                "sections/procedural_history",
    "II. PROCEDURAL HISTORY":            "sections/procedural_history",
    "III. PROCEDURAL HISTORY":           "sections/procedural_history",
    "STANDARD OF REVIEW":                "sections/standard_of_review",
    "III. STANDARD OF REVIEW":           "sections/standard_of_review",
    "IV. STANDARD OF REVIEW":            "sections/standard_of_review",
    "PRAYER FOR RELIEF":                 "sections/prayer_for_relief",
    "CONCLUSION":                        "sections/conclusion",
    "CERTIFICATE OF SERVICE":            "boilerplate/certificate_of_service",
    "PROPOSED ORDER":                    "boilerplate/proposed_order",
    "[PROPOSED] ORDER":                  "boilerplate/proposed_order",
    "QUESTIONS PRESENTED":               "appellate/questions_presented",
    "QUESTIONS PRESENTED FOR REVIEW":    "appellate/questions_presented",
    "GROUNDS FOR THE WRIT":              "arguments/mandamus_grounds",
    "REASONS FOR GRANTING THE PETITION": "arguments/reasons_for_granting_petition",
    "STATEMENT OF JURISDICTION":         "appellate/statement_of_jurisdiction",
    "OPINIONS BELOW":                    "appellate/opinions_below",
    "STATUTES INVOLVED":                 "appellate/statutes_involved",
    "STATEMENT OF THE CASE":             "appellate/statement_of_the_case",
}


# -----------------------------------------------------------------------------
# Validator engine
# -----------------------------------------------------------------------------
class SchemaValidator:

    def __init__(self, schema_path: Path = SCHEMA_PATH):
        if not schema_path.exists():
            raise FileNotFoundError(f"schema not found: {schema_path}")
        with open(schema_path, encoding="utf-8") as f:
            self.schema = yaml.safe_load(f)
        self.known_judges     = self.schema.get("known_judges", [])
        self.court_rules      = self.schema.get("court_rules", {})
        self.block_contracts  = self.schema.get("block_contracts", {})
        self.validators_spec  = self.schema.get("validators", {})

    # -- public API ----------------------------------------------------------

    def validate_file(self, md_path: Path, ctx: Dict[str, Any]) -> Dict[str, Any]:
        content = md_path.read_text(encoding="utf-8")
        return self.validate_markdown(content, ctx, source_path=str(md_path))

    def validate_markdown(self, content: str, ctx: Dict[str, Any], source_path: str = "") -> Dict[str, Any]:
        ctx = self._augment_ctx(ctx)
        # FILING-ONLY: strip the review-copy "# APPENDIX A - Source Stub"
        # section (added by motion_drafter_v2.assemble_full_draft) and any
        # leftover code fences before validating. The appendix contains the
        # original stub for human review; v2's _strip_appendix removes it
        # before rendering the .docx. We mirror that behavior so the
        # validator only checks the content that will actually be filed.
        content = self._strip_appendix_and_code_fences(content)
        doc_gaps   = self._run_document_level(content, ctx)
        sections   = self._parse_sections(content)
        block_gaps: List[Dict] = []
        blocks_seen: List[str] = []
        for header, body in sections:
            block_id = self._map_header_to_block(header)
            if not block_id:
                continue
            blocks_seen.append(block_id)
            for g in self._validate_block(block_id, body, ctx):
                g["section_header"] = header
                block_gaps.append(g)
        all_gaps = doc_gaps + block_gaps
        return {
            "source_path":  source_path,
            "case_id":      ctx.get("case_id"),
            "filing_type":  ctx.get("filing_type"),
            "court_id":     ctx.get("court_id"),
            "brief_type":   ctx.get("brief_type"),
            "blocks_seen":  blocks_seen,
            "gaps":         all_gaps,
            "hard_gaps":    [g for g in all_gaps if g["severity"] == "hard"],
            "soft_gaps":    [g for g in all_gaps if g["severity"] == "soft"],
            "pass":         not any(g["severity"] == "hard" for g in all_gaps),
        }

    # -- context augmentation ------------------------------------------------

    def _augment_ctx(self, ctx: Dict[str, Any]) -> Dict[str, Any]:
        ctx = dict(ctx)
        case_id = ctx.get("case_id", "")
        ctx.setdefault("court_id", infer_court_id(case_id))
        if "judge_surname" not in ctx:
            judge = ctx.get("judge") or ctx.get("judge_name") or DEFAULT_JUDGE_PER_CASE.get(case_id)
            ctx["judge_surname"] = self._lookup_surname(judge) if judge else None
        ctx.setdefault("is_appellate", ctx["court_id"] in {
            "3D_CIR", "SCOTUS", "PA_SUPER", "PA_COMMWLTH", "PA_SUPREME"
        })
        ctx.setdefault("is_pro_se", True)
        # Normalize filing_type — strip leading digits, trailing case-id
        # suffix, trailing _DRAFT/_FILING markers — so exempt-list lookups
        # work regardless of how filing_type was passed (baseline scan
        # passes raw filename stem; v2 hook passes pre-stripped slug).
        ft = (ctx.get("filing_type") or "").lower()
        ft_norm = re.sub(r"^\d+_", "", ft).lstrip("_")
        for suf in ("_fed_", "_phila_ccp_", "_3d_cir_", "_scotus_", "_pa_super_"):
            if suf in ft_norm:
                ft_norm = ft_norm.split(suf)[0]
                break
        ft_norm = re.sub(r"_(draft|filing)$", "", ft_norm)
        ft_norm = re.sub(r"_\d+$", "", ft_norm)
        ctx["filing_type_normalized"] = ft_norm
        return ctx

    def _lookup_surname(self, judge_str: str) -> Optional[str]:
        if not judge_str:
            return None
        for j in self.known_judges:
            if j["surname"].lower() in judge_str.lower():
                return j["surname"]
        return judge_str.split()[-1] if judge_str else None

    # -- section parsing -----------------------------------------------------

    def _strip_appendix_and_code_fences(self, content: str) -> str:
        """Remove the APPENDIX A section (added by motion_drafter_v2's
        assemble_full_draft) and any leftover code-fenced regions.

        The appendix wraps the source stub inside ``` fences for human
        review only; v2's _strip_appendix removes it before docx render.
        Without this step, the validator would parse stub-template section
        headers (## I. INTRODUCTION, ## FACTUAL STATEMENT, ### Parties)
        from inside the fenced appendix as if they were real document
        sections, causing false-positive no_duplicate_block_renders and
        truncation_check failures.
        """
        # 1. Find "# APPENDIX A ..." heading (1 hash + space + APPENDIX);
        #    strip from the preceding "---" separator (if any) or from the
        #    heading itself onward.
        m = re.search(r"(\n---\s*\n)?\s*^#\s+APPENDIX\b.*$", content, re.MULTILINE)
        if m:
            content = content[:m.start()]
        # 2. Strip any remaining ``` ... ``` fence regions defensively.
        out_lines: List[str] = []
        in_fence = False
        for line in content.split("\n"):
            if re.match(r"^\s*```", line):
                in_fence = not in_fence
                continue
            if not in_fence:
                out_lines.append(line)
        return "\n".join(out_lines)

    def _parse_sections(self, content: str) -> List[Tuple[str, str]]:
        sections: List[Tuple[str, str]] = []
        current_header = ""
        current_body: List[str] = []
        for line in content.split("\n"):
            m = re.match(r"^##\s+(.+?)\s*$", line)
            if m:
                if current_header:
                    sections.append((current_header, "\n".join(current_body)))
                current_header = m.group(1).strip()
                current_body = []
            else:
                current_body.append(line)
        if current_header:
            sections.append((current_header, "\n".join(current_body)))
        return sections

    def _map_header_to_block(self, header: str) -> Optional[str]:
        header_clean = header.strip().rstrip(":")
        if header_clean in HEADER_TO_BLOCK:
            return HEADER_TO_BLOCK[header_clean]
        # Strip leading Roman numeral + period
        no_roman = re.sub(r"^[IVX]+\.\s+", "", header_clean).strip()
        return HEADER_TO_BLOCK.get(no_roman)

    # -- validation drivers --------------------------------------------------

    DOC_LEVEL_VALIDATORS = [
        "no_reasoning_leak", "no_filename_leak",
        "all_placeholders_resolved", "no_hardcoded_judge",
        "no_duplicate_block_renders", "motion_title_matches_filing_type",
        "consistent_docket_cite_form", "no_cross_jurisdiction_authority",
        "truncation_check",
    ]

    def _run_document_level(self, content: str, ctx: Dict) -> List[Dict]:
        gaps: List[Dict] = []
        for v_name in self.DOC_LEVEL_VALIDATORS:
            spec = self.validators_spec.get(v_name)
            if not spec:
                continue
            gap = self._dispatch(v_name, spec, content, ctx, contract=None)
            if gap:
                gaps.append({
                    "block_id":  "__document__",
                    "validator": v_name,
                    "severity":  spec.get("severity", "soft"),
                    "gap":       gap,
                })
        return gaps

    def _validate_block(self, block_id: str, content: str, ctx: Dict) -> List[Dict]:
        contract = self.block_contracts.get(block_id)
        if not contract:
            return [{
                "block_id":  block_id,
                "validator": "__contract_missing__",
                "severity":  "soft",
                "gap":       f"no contract defined for {block_id}",
            }]
        gaps: List[Dict] = []
        for v_name in contract.get("validators", []):
            if v_name in self.DOC_LEVEL_VALIDATORS:
                continue  # already run at doc level
            spec = self.validators_spec.get(v_name)
            if not spec:
                continue
            gap = self._dispatch(v_name, spec, content, ctx, contract=contract)
            if gap:
                gaps.append({
                    "block_id":  block_id,
                    "validator": v_name,
                    "severity":  spec.get("severity", "soft"),
                    "gap":       gap,
                })
        return gaps

    def _dispatch(self, v_name, spec, content, ctx, contract):
        method = getattr(self, f"_v_{v_name}", None)
        return method(spec, content, ctx, contract) if method else None

    # -- per-validator implementations ---------------------------------------

    def _v_non_empty(self, spec, content, ctx, contract):
        return None if content.strip() else spec.get("fail_message", "empty")

    def _v_no_reasoning_leak(self, spec, content, ctx, contract):
        for pat in spec.get("patterns_forbidden", []):
            try:
                m = re.search(pat, content, re.MULTILINE)
            except re.error:
                continue
            if m:
                snip = m.group(0)[:60].replace("\n", " ")
                return f"reasoning leak (matched: {snip!r})"
        return None

    def _v_no_filename_leak(self, spec, content, ctx, contract):
        for pat in spec.get("patterns_forbidden", []):
            try:
                m = re.search(pat, content)
            except re.error:
                continue
            if m:
                return f"filename leak (matched: {m.group(0)[:80]!r})"
        court_id = ctx.get("court_id")
        cross = spec.get("cross_court_forbidden", {}).get(court_id, [])
        for pat in cross:
            try:
                m = re.search(pat, content)
            except re.error:
                continue
            if m:
                return f"wrong-court docket form in {court_id}: {m.group(0)!r}"
        return None

    def _v_all_placeholders_resolved(self, spec, content, ctx, contract):
        text = content
        for pat in spec.get("patterns_excluded", []):
            try:
                text = re.sub(pat, "", text, flags=re.DOTALL)
            except re.error:
                continue
        for d in spec.get("directives_allowed", []):
            text = text.replace(d, "")
        for pat in spec.get("patterns_forbidden", []):
            try:
                m = re.search(pat, text)
            except re.error:
                continue
            if m:
                return f"unresolved placeholder/marker: {m.group(0)!r}"
        return None

    def _v_no_hardcoded_judge(self, spec, content, ctx, contract):
        expected = ctx.get("judge_surname")
        for j in self.known_judges:
            surname = j["surname"]
            if expected and surname.lower() == expected.lower():
                continue
            if re.search(r"\b" + re.escape(surname) + r"\b", content, re.IGNORECASE):
                return f"unexpected judge surname '{surname}' (expected: {expected})"
        return None

    def _v_judge_placeholder_resolved(self, spec, content, ctx, contract):
        for pat in spec.get("patterns_forbidden", []):
            try:
                if re.search(pat, content):
                    return spec.get("fail_message")
            except re.error:
                continue
        return None

    def _v_mixed_case_defendants(self, spec, content, ctx, contract):
        bad: List[str] = []
        for line in content.split("\n"):
            line = line.strip()
            if not line or line.startswith(("#", "---", "|", ">")):
                continue
            if line.startswith(("UNITED STATES", "IN THE", "FOR THE", "OF ",
                                "BY THE", "WHEREFORE", "Civil Action",
                                "AND NOW", "ORDERED")):
                continue
            if line in ("v.", "vs.", "Plaintiff,", "Defendants,", "Defendant,",
                        "Plaintiffs,", "Plaintiff", "Defendants", "Defendant"):
                continue
            if (re.match(r"^[A-Z0-9\s\.,;:()&/'-]{6,}$", line)
                and re.search(
                    r"\b(LLC|INC|CORP|N\.A|BANK|COMPANY|CO\.|LTD|LLP|GROUP|HOLDINGS|TRUST|SERVICES|ASSOCIATES)\b",
                    line)):
                bad.append(line)
        if bad:
            return f"ALL CAPS defendant name: {bad[0][:80]}"
        return None

    def _v_all_defendants_listed(self, spec, content, ctx, contract):
        expected = ctx.get("expected_defendants") or []
        missing = [d for d in expected if d.lower() not in content.lower()]
        if missing:
            return f"missing defendant(s): {', '.join(missing[:3])}"
        return None

    def _v_single_paragraph_prose(self, spec, content, ctx, contract):
        pat = spec.get("pattern_forbidden_first_chars", "")
        if not pat:
            return None
        for line in content.split("\n"):
            line_s = line.strip()
            if not line_s or line_s.startswith(("#", ">", "---")):
                continue
            if re.match(pat, line_s):
                return spec.get("fail_message")
        return None

    def _v_no_numbered_enumeration(self, spec, content, ctx, contract):
        pat = spec.get("pattern_forbidden")
        if pat and re.search(pat, content, re.MULTILINE):
            return spec.get("fail_message")
        return None

    def _v_numbered_paragraphs(self, spec, content, ctx, contract):
        pat = spec.get("pattern_required")
        if pat and not re.search(pat, content, re.MULTILINE):
            return spec.get("fail_message")
        return None

    def _v_numbered_list(self, spec, content, ctx, contract):
        pat = spec.get("pattern_required")
        if pat and not re.search(pat, content, re.MULTILINE):
            return spec.get("fail_message")
        return None

    def _v_record_cited_each_paragraph(self, spec, content, ctx, contract):
        court_id = ctx.get("court_id")
        court = self.court_rules.get(court_id, {})
        if "inherits_from" in court and "record_cite_forms" not in court:
            court = self.court_rules.get(court["inherits_from"], {})
        forms = court.get("record_cite_forms", [])
        if not forms:
            return None
        paras = re.findall(r"(?m)^\d+\.\s+.+?(?=^\d+\.\s|\Z)", content, re.DOTALL)
        bad: List[str] = []
        for p in paras:
            if not any(re.search(form, p) for form in forms):
                bad.append(p[:60].replace("\n", " "))
        if bad:
            return f"paragraph lacks {court_id} record cite: {bad[0]!r}"
        return None

    def _v_has_section_headers(self, spec, content, ctx, contract):
        pat = spec.get("pattern_required")
        if pat and not re.search(pat, content, re.MULTILINE):
            return spec.get("fail_message")
        return None

    def _v_cites_authority(self, spec, content, ctx, contract):
        pat = spec.get("pattern_required")
        if pat and not re.search(pat, content):
            return spec.get("fail_message")
        return None

    def _v_cites_jurisdictional_statute(self, spec, content, ctx, contract):
        pat = spec.get("pattern_required")
        if pat and not re.search(pat, content):
            return spec.get("fail_message")
        return None

    def _v_cites_required_authorities(self, spec, content, ctx, contract):
        if not contract:
            return None
        required = contract.get("output_constraints", {}).get("required_citations", [])
        missing = [c for c in required if c.split(",")[0].strip() not in content]
        if missing:
            return f"missing required citation(s): {missing[0][:60]}"
        return None

    def _v_ends_with_general_relief(self, spec, content, ctx, contract):
        pat = spec.get("pattern_required")
        if pat and not re.search(pat, content, re.IGNORECASE):
            return spec.get("fail_message")
        return None

    def _v_date_format_iso_or_long(self, spec, content, ctx, contract):
        pat = spec.get("pattern_required")
        if pat and not re.search(pat, content):
            return spec.get("fail_message")
        return None

    def _v_served_parties_match_caption(self, spec, content, ctx, contract):
        return None  # soft; future

    def _v_word_count_under_limit(self, spec, content, ctx, contract):
        court_id   = ctx.get("court_id")
        brief_type = ctx.get("brief_type")
        court = self.court_rules.get(court_id, {})
        if "inherits_from" in court and "word_limits" not in court:
            court = self.court_rules.get(court["inherits_from"], court)
        limits = court.get("word_limits", {})
        limit = limits.get(brief_type) if brief_type else None
        if not limit:
            return None
        words = len(content.split())
        if words > limit:
            return f"word count {words} exceeds {court_id} {brief_type} limit {limit}"
        return None

    def _v_page_count_under_limit(self, spec, content, ctx, contract):
        return None  # needs rendered docx; future

    def _v_element_lock_present(self, spec, content, ctx, contract):
        for pat in spec.get("pattern_required_subheaders", []):
            if not re.search(pat, content, re.MULTILINE):
                return f"missing element-lock sub-header pattern: {pat}"
        return None

    def _v_no_ungrounded_maxim(self, spec, content, ctx, contract):
        latin = re.search(
            r"\b(equity will not suffer|fiat justitia|ubi jus|de minimis|"
            r"in pari delicto|ex turpi causa|prima facie|res judicata|"
            r"stare decisis|ab initio|de novo|sub silentio|nunc pro tunc|"
            r"ipso facto|ex aequo)\b",
            content, re.IGNORECASE)
        if not latin:
            return None
        if not re.search(r"(Pomeroy|Bouvier|Black'?s|\d+ U\.S\.|F\.\d|Restatement)", content):
            return spec.get("fail_message")
        return None

    def _v_matches_motion_type(self, spec, content, ctx, contract):
        return None  # soft

    def _v_scoped_to_motion_type(self, spec, content, ctx, contract):
        return None  # soft; needs cross-reference into reasoning stack

    # === v3 additions (substantive defect catchers) =========================

    def _v_no_duplicate_block_renders(self, spec, content, ctx, contract):
        # Re-parse blocks_seen against permitted prefixes.
        sections = self._parse_sections(content)
        seen: Dict[str, int] = {}
        for header, _ in sections:
            block_id = self._map_header_to_block(header)
            if not block_id:
                continue
            seen[block_id] = seen.get(block_id, 0) + 1
        permitted = spec.get("permitted_block_prefixes", [])
        dupes = []
        for bid, count in seen.items():
            if count > 1 and not any(bid.startswith(p) for p in permitted):
                dupes.append(f"{bid} (x{count})")
        if dupes:
            return f"{spec.get('fail_message')} — {', '.join(dupes)}"
        return None

    def _v_motion_title_matches_filing_type(self, spec, content, ctx, contract):
        ft = ctx.get("filing_type") or ""
        if not ft:
            return None
        # Derive expected keywords from filing_type slug
        stopwords = set(spec.get("stopwords_dropped", []))
        tokens = [t for t in ft.lower().split("_") if t and t not in stopwords
                  and not t.isdigit() and not (len(t) <= 4 and t.startswith("frap"))]
        if not tokens:
            return None
        # Extract the title (first H1)
        m = re.search(r"(?m)^#\s+(.+?)\s*$", content)
        if not m:
            return None
        title = m.group(1).lower()
        # Check the most-distinctive token (longest non-stop word) appears in title
        # Use the longest token as the discriminator
        primary = max(tokens, key=len)
        if primary not in title and primary.replace("s", "") not in title:
            return f"title '{m.group(1)[:60]}' does not contain filing_type token '{primary}'"
        return None

    def _v_consistent_docket_cite_form(self, spec, content, ctx, contract):
        court_id = ctx.get("court_id")
        court = self.court_rules.get(court_id, {})
        if "inherits_from" in court and "record_cite_forms" not in court:
            court = self.court_rules.get(court["inherits_from"], court)
        forms = court.get("record_cite_forms", [])
        if not forms:
            return None
        # Count which docket-form patterns actually appear in the content.
        # We only care about the "docket entry" subset (not Exhibit/Order dated).
        docket_forms = [f for f in forms
                        if re.search(r"\\d", f)
                        and any(k in f for k in ("ECF", "Dkt", "Doc", "Control"))]
        used = []
        for f in docket_forms:
            if re.search(f, content):
                used.append(f)
        # Also catch "Dkt N" (no period) which is INVALID variant
        loose_dkt = re.findall(r"\bDkt\s+\d+\b(?!\.)", content)   # "Dkt 141" no period
        loose_ecf = re.findall(r"\bECF\s+\d+\b(?!\.)", content)
        if loose_dkt:
            used.append("Dkt <bare>")
        if loose_ecf:
            used.append("ECF <bare>")
        if len(used) > 1:
            return f"mixed docket cite forms in one document: {used}"
        return None

    def _v_no_cross_jurisdiction_authority(self, spec, content, ctx, contract):
        # Court-agnostic filing types (court: any in their filing_type YAML)
        # legitimately cite dual-jurisdiction parallel authority — skip.
        ft_norm = ctx.get("filing_type_normalized", "")
        exempt = spec.get("exempt_filing_types", [])
        if ft_norm in exempt:
            return None
        court_id = ctx.get("court_id")
        forbidden = spec.get("cross_jurisdiction_forbidden", {}).get(court_id, [])
        for pat in forbidden:
            try:
                m = re.search(pat, content)
            except re.error:
                continue
            if m:
                return f"cross-jurisdiction authority cited in {court_id}: {m.group(0)!r}"
        return None

    def _v_truncation_check(self, spec, content, ctx, contract):
        tail_n = spec.get("tail_line_count", 5)
        # Look at last N NON-EMPTY lines (skipping trailing whitespace)
        all_lines = content.rstrip().split("\n")
        non_empty = [ln for ln in all_lines if ln.strip()]
        tail_lines = non_empty[-tail_n:] if len(non_empty) > tail_n else non_empty
        tail = "\n".join(tail_lines)
        for pat in spec.get("patterns_forbidden_in_tail", []):
            try:
                m = re.search(pat, tail, re.MULTILINE)
            except re.error:
                continue
            if m:
                snip = m.group(0)[:60]
                return f"truncation indicator in tail: {snip!r}"
        return None

    # -- logging -------------------------------------------------------------

    def log_report(self, report: Dict[str, Any]) -> None:
        REVIEW_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().isoformat()
        gaps_path = REVIEW_DIR / "schema_gaps.jsonl"
        with open(gaps_path, "a", encoding="utf-8") as f:
            for gap in report["gaps"]:
                record = {
                    "ts":          ts,
                    "case_id":     report.get("case_id"),
                    "filing_type": report.get("filing_type"),
                    "court_id":    report.get("court_id"),
                    "brief_type":  report.get("brief_type"),
                    "source_path": report.get("source_path"),
                    **gap,
                }
                f.write(json.dumps(record) + "\n")
        pass_path = REVIEW_DIR / "draft_schema_pass.jsonl"
        with open(pass_path, "a", encoding="utf-8") as f:
            record = {
                "ts":              ts,
                "case_id":         report.get("case_id"),
                "filing_type":     report.get("filing_type"),
                "court_id":        report.get("court_id"),
                "source_path":     report.get("source_path"),
                "blocks_seen":     report["blocks_seen"],
                "hard_gap_count":  len(report["hard_gaps"]),
                "soft_gap_count":  len(report["soft_gaps"]),
                "pass":            report["pass"],
            }
            f.write(json.dumps(record) + "\n")


# -----------------------------------------------------------------------------
# CLI helpers
# -----------------------------------------------------------------------------
def _print_report(report: Dict[str, Any]) -> None:
    src = report.get("source_path") or "<inline>"
    print(f"source : {src}")
    print(f"case   : {report['case_id']}  filing: {report['filing_type']}  court: {report['court_id']}")
    print(f"blocks : {len(report['blocks_seen'])}  hard: {len(report['hard_gaps'])}  soft: {len(report['soft_gaps'])}  pass: {report['pass']}")
    if report["gaps"]:
        for g in report["gaps"]:
            sev   = g["severity"].upper()
            block = g.get("block_id", "?")
            print(f"  [{sev:4s}] {block:42s} {g['validator']:34s} {g['gap'][:120]}")


def cmd_validate(args) -> int:
    sv = SchemaValidator()
    ctx = {
        "case_id":      args.case_id,
        "filing_type":  args.filing_type,
        "court_id":     args.court_id,
        "posture":      args.posture,
        "judge_name":   args.judge,
        "brief_type":   args.brief_type,
    }
    report = sv.validate_file(Path(args.path), ctx)
    _print_report(report)
    if args.log:
        sv.log_report(report)
    return 0 if report["pass"] else 1


def cmd_baseline(args) -> int:
    sv = SchemaValidator()
    if not CASES_DIR.exists():
        print(f"ERROR: cases dir not found: {CASES_DIR}", file=sys.stderr)
        return 2
    case_dirs = sorted([p for p in CASES_DIR.iterdir() if p.is_dir()])
    if args.case:
        case_dirs = [p for p in case_dirs if p.name == args.case]

    totals = {"files": 0, "pass": 0, "fail": 0, "hard": 0, "soft": 0}
    per_validator: Dict[str, int] = {}

    for case_dir in case_dirs:
        case_id = slug_to_case_id(case_dir.name)
        drafts_dir = case_dir / "drafts"
        if not drafts_dir.exists():
            continue
        run_dirs = sorted([p for p in drafts_dir.iterdir() if p.is_dir()], reverse=True)
        if not run_dirs:
            continue
        latest = run_dirs[0]
        md_files = sorted(latest.glob("*FILING.md")) or sorted(latest.glob("*.md"))
        if not md_files:
            continue
        print(f"\n=== {case_id}  ({latest.name}) ===")
        for md in md_files:
            stem = re.sub(r"^\d+_", "", md.stem.replace("_FILING", "")).lower()
            ctx = {
                "case_id":     case_id,
                "filing_type": stem,
                "court_id":    infer_court_id(case_id),
            }
            report = sv.validate_file(md, ctx)
            totals["files"] += 1
            totals["hard"]  += len(report["hard_gaps"])
            totals["soft"]  += len(report["soft_gaps"])
            if report["pass"]:
                totals["pass"] += 1
            else:
                totals["fail"] += 1
            for g in report["gaps"]:
                key = f"{g['validator']}"
                per_validator[key] = per_validator.get(key, 0) + 1
            status = "PASS" if report["pass"] else "FAIL"
            print(f"  {md.name:62s}  blocks={len(report['blocks_seen']):2d}  hard={len(report['hard_gaps']):2d}  soft={len(report['soft_gaps']):2d}  {status}")
            if args.verbose and report["gaps"]:
                for g in report["gaps"]:
                    sev = g["severity"].upper()
                    print(f"     [{sev:4s}] {g.get('block_id','?'):38s} {g['validator']:32s} {g['gap'][:120]}")
            if args.log:
                sv.log_report(report)

    print(f"\n=== TOTALS ===")
    print(f"  files     : {totals['files']}")
    print(f"  pass      : {totals['pass']}")
    print(f"  fail      : {totals['fail']}")
    print(f"  hard gaps : {totals['hard']}")
    print(f"  soft gaps : {totals['soft']}")
    if per_validator:
        print(f"\n=== Top validators triggering gaps ===")
        for k, v in sorted(per_validator.items(), key=lambda x: -x[1])[:15]:
            print(f"  {v:4d}  {k}")
    return 0 if totals["fail"] == 0 else 1


def cmd_show_block(args) -> int:
    sv = SchemaValidator()
    contract = sv.block_contracts.get(args.block_id)
    if not contract:
        print(f"no contract for block: {args.block_id}", file=sys.stderr)
        return 1
    print(json.dumps(contract, indent=2, default=str))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="schema_validator")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_val = sub.add_parser("validate", help="Validate a single markdown file")
    p_val.add_argument("path")
    p_val.add_argument("--case-id", required=True)
    p_val.add_argument("--filing-type", required=True)
    p_val.add_argument("--court-id", default=None)
    p_val.add_argument("--posture", default=None)
    p_val.add_argument("--judge", default=None)
    p_val.add_argument("--brief-type", default=None)
    p_val.add_argument("--log", action="store_true")
    p_val.set_defaults(func=cmd_validate)

    p_base = sub.add_parser("baseline", help="Sweep across all canonical drafts")
    p_base.add_argument("--case", default=None, help="Restrict to one case slug")
    p_base.add_argument("--log", action="store_true")
    p_base.add_argument("--verbose", action="store_true")
    p_base.set_defaults(func=cmd_baseline)

    p_show = sub.add_parser("show-block", help="Print contract for a block_id")
    p_show.add_argument("block_id")
    p_show.set_defaults(func=cmd_show_block)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
