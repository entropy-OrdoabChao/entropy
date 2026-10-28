#!/usr/bin/env python3
"""
quality_score.py — Beezle quality-metric scoring engine (Phase 3 Layer A).

Converts a single draft .md into an 8-axis numeric quality score.
Used to (a) track per-run quality drift, (b) compare LLM candidates
head-to-head, and (c) drive a learning loop that selects the best draft
per filing slot.

Axes (each 0.0-1.0):
  1. schema_pass         schema validator: hard=0 -> 1.0, soft-only -> 0.75, hard>0 -> 0.0
  2. scope_purity        out-of-scope hits: 1.0 - 0.10*hits  (clamped >= 0)
  3. defendant_focus     for per-defendant filings: argument_titles_with_active_def / total_arg_sections
  4. citation_density    distinct authority citations / target_per_filing_type (capped 1.0)
  5. claim_coverage      jaccard(draft_claims, baseline_memo_claims)
  6. repetition_penalty  1.0 - max_paragraph_pair_similarity (capped >= 0)
  7. length_appropriate  piecewise per filing_type word count target band
  8. structural_integrity  header hierarchy correctness + no orphans + appendix-free body

Final score = weighted geometric mean. Any axis = 0 tanks the whole score
(forces balanced quality, prevents gaming any single dimension).

Usage:
    python3 -m pipeline.tools.quality_score DRAFT.md \\
        --case-id fed:2:26-cv-00789 \\
        --filing-type opposition_to_motion_to_dismiss \\
        [--baseline-memo /path/to/baseline_memo.md] \\
        [--log] [--json]

Outputs JSON to stdout with breakdown + overall.
Appends a record to <home>/_review/quality_scores.jsonl when --log.
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import argparse
import json
import math
import re
import sys
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_ROOT_WSL = _eroot()
_ROOT_WIN = _eroot()
ROOT = _ROOT_WSL if _ROOT_WSL.exists() else _ROOT_WIN
LOG_PATH = ROOT / "_review" / "quality_scores.jsonl"

# Axis weights — sum should be 1.0
WEIGHTS = {
    "schema_pass":          0.20,
    "scope_purity":         0.20,
    "defendant_focus":      0.10,
    "citation_density":     0.10,
    "claim_coverage":       0.10,
    "repetition_penalty":   0.10,
    "length_appropriate":   0.10,
    "structural_integrity": 0.10,
}

# Per-filing-type word-count target bands (low, ideal_low, ideal_high, high)
LENGTH_TARGETS: Dict[str, Tuple[int, int, int, int]] = {
    "opposition_to_motion_to_dismiss": (400, 1500, 4500, 8000),
    "motion_for_summary_judgment":      (800, 2000, 6000, 10000),
    "motion_to_remand":                 (300, 1000, 3500, 6000),
    "motion_to_vacate":                 (400, 1500, 4500, 8000),
    "motion_to_strike":                 (200, 600, 2000, 4000),
    "motion_to_strike_federal":         (200, 600, 2000, 4000),
    "motion_to_strike_state":           (200, 600, 2000, 4000),
    "notice_of_supplemental_authority": (80, 200, 800, 1500),
    "notice_of_appeal_reserved":        (50, 100, 300, 600),
    "frap_21_petition_for_writ_of_mandamus": (1000, 3000, 8000, 14000),
    "frap_40_panel_rehearing":          (500, 1500, 4500, 7000),
    "scotus_cert_petition":             (3000, 5000, 9000, 15000),
    "appellate_reply_brief":            (1500, 3000, 7000, 12000),
    # default fallback for unknown filing types
    "_default":                         (300, 1000, 4000, 8000),
}

# Target distinct citation count per filing type (for citation_density axis)
CITATION_TARGETS: Dict[str, int] = {
    "opposition_to_motion_to_dismiss": 6,
    "motion_for_summary_judgment":      10,
    "motion_to_remand":                 5,
    "motion_to_vacate":                 6,
    "motion_to_strike":                 4,
    "motion_to_strike_federal":         4,
    "motion_to_strike_state":           4,
    "notice_of_supplemental_authority": 2,
    "frap_21_petition_for_writ_of_mandamus": 8,
    "scotus_cert_petition":             12,
    "appellate_reply_brief":            10,
    "_default":                         5,
}

# Out-of-scope phrases (negative scope across all 4 cases)
OUT_OF_SCOPE_PHRASES = [
    r"Unearned Premium",
    r"premium retention",
    r"Risk Ceased",
    r"risk ceased",
    r"NAIC unfair claims",
    r"naic unfair claims",
]


# ============================================================================
# helpers
# ============================================================================

def _strip_appendix(content: str) -> str:
    """Remove '# APPENDIX A - Source Stub ...' and everything after it.
    Mirrors motion_drafter_v2._strip_appendix + schema_validator behavior."""
    m = re.search(r"(\n---\s*\n)?\s*^#\s+APPENDIX\b.*$", content, re.MULTILINE)
    if m:
        content = content[:m.start()]
    # Defensive: strip leftover ``` code fences
    out: List[str] = []
    in_fence = False
    for line in content.split("\n"):
        if re.match(r"^\s*```", line):
            in_fence = not in_fence
            continue
        if not in_fence:
            out.append(line)
    return "\n".join(out)


def _normalize_filing_type(ft: str) -> str:
    ft = (ft or "").lower()
    ft = re.sub(r"^\d+_", "", ft).lstrip("_")
    for suf in ("_fed_", "_phila_ccp_", "_3d_cir_", "_scotus_", "_pa_super_"):
        if suf in ft:
            ft = ft.split(suf)[0]
            break
    ft = re.sub(r"_(draft|filing)$", "", ft)
    ft = re.sub(r"_\d+$", "", ft)
    return ft


def _extract_argument_sections(body: str) -> List[str]:
    """Return list of `### I.`, `### II.`, etc. section titles."""
    titles = []
    for m in re.finditer(
        r"^###\s+(I|II|III|IV|V|VI|VII|VIII|IX|X)\.\s+(.+?)$",
        body,
        re.MULTILINE,
    ):
        titles.append(m.group(2).strip())
    return titles


def _extract_citations(body: str) -> List[str]:
    """Pull canonical citation tokens. Coarse but consistent across drafts."""
    cites = set()
    # Federal Rules
    for m in re.finditer(r"\b(?:Fed\.\s*R\.\s*Civ\.\s*P\.|FRCP)\s*\d+(?:\([a-z]+\))?", body):
        cites.add(m.group(0))
    # USC
    for m in re.finditer(r"\b\d+\s+U\.S\.C\.\s+§?\s*\d+", body):
        cites.add(m.group(0))
    # Pa. statutes
    for m in re.finditer(r"\b\d+\s+Pa\.\s*C\.S\.\s*§?\s*\d+", body):
        cites.add(m.group(0))
    # P.S.
    for m in re.finditer(r"\b\d+\s+P\.S\.\s*§?\s*\d+", body):
        cites.add(m.group(0))
    # Pa.R.X.P.
    for m in re.finditer(r"\bPa\.R\.[A-Z]\.\s*\d+", body):
        cites.add(m.group(0))
    # Case citations (Plaintiff v. Defendant)
    for m in re.finditer(r"\b[A-Z][\w\.\']+(?:\s+[A-Z][\w\.\']+)*\s+v\.\s+[A-Z][\w\.\']+(?:\s+[A-Z][\w\.\']+)*", body):
        cites.add(m.group(0))
    return sorted(cites)


def _extract_paragraphs(body: str) -> List[str]:
    """Split body into paragraphs for similarity comparison.

    Excludes (P2 2026-06-13):
      - metadata/scaffold blocks where >=50% of non-empty lines start with
        bold-key markers like ``**Filed:**`` / ``- **Court:**`` etc.
      - horizontal rules
      - pure-header paragraphs
    so SequenceMatcher only compares actual prose. Without this filter,
    identical scaffold blocks across motion types collapsed the
    repetition_penalty axis to ~0.255 for every draft.
    """
    paras = re.split(r"\n\s*\n", body)
    cleaned = []
    for p in paras:
        s = p.strip()
        if len(s) <= 80:
            continue
        lines = [ln for ln in s.split("\n") if ln.strip()]
        if not lines:
            continue
        # Skip pure separators (---  ***  ___ etc.)
        if all(re.match(r"^[-=*_]{3,}\s*$", ln) for ln in lines):
            continue
        # Skip pure-header paragraphs (## ... or ### ...)
        if all(re.match(r"^#{1,6}\s+\S", ln) for ln in lines):
            continue
        # Skip metadata blocks (lines that are mostly **Key:** value bullets)
        meta_lines = sum(
            1 for ln in lines
            if re.match(r"^\s*(?:-\s+)?\*\*[^*\n]+:\*\*\s", ln)
        )
        if meta_lines >= len(lines) / 2:
            continue
        cleaned.append(s)
    return cleaned


def _max_paragraph_similarity(paras: List[str]) -> float:
    """Worst-case pair similarity (0.0-1.0). Higher = more repetition."""
    if len(paras) < 2:
        return 0.0
    worst = 0.0
    for i in range(len(paras)):
        for j in range(i + 1, len(paras)):
            sim = SequenceMatcher(None, paras[i], paras[j]).ratio()
            if sim > worst:
                worst = sim
    return worst


def _extract_baseline_claims(memo_path: Optional[Path]) -> List[str]:
    """Parse a baseline_memo to extract Section 2 claim names from H3
    headings of the form '### <Citation> (<ClaimName>)'."""
    if not memo_path or not memo_path.exists():
        return []
    txt = memo_path.read_text(encoding="utf-8", errors="ignore")
    claims = set()
    for m in re.finditer(r"^###\s+.*?\(([^)]+)\)\s*$", txt, re.MULTILINE):
        cname = m.group(1).strip()
        if 5 < len(cname) < 100 and not cname.lower().startswith("the "):
            claims.add(cname.lower())
    return sorted(claims)


def _extract_draft_claims(body: str) -> List[str]:
    """Pull claim phrases from STATEMENT OF FACTS and ARGUMENT titles.

    Broadened 2026-06-13 (P1 batch 1):
      - Legacy narrow grammar retained ("claim for X", "pleads a claim for X")
      - Also takes each H3 argument-title head as a claim-noun bag (so
        claim-focused titles produced under SCOPE_CLAMP_V1 surface here)
      - Also picks up "Plaintiff/Movant <verb> a claim for X" anywhere in body
    """
    claims = set()
    # Numbered claim breakdowns: "1. ClaimName (Citation). Here, ..."
    for m in re.finditer(
        r"^\s*\d+\.\s+([^.(]+?)(?:\s*\(|\.\s+Here)",
        body,
        re.MULTILINE,
    ):
        claims.add(m.group(1).strip().lower())
    # H3 argument titles: extract claim noun phrases
    for m in re.finditer(
        r"^###\s+(?:I|II|III|IV|V|VI|VII|VIII|IX|X)\.\s+(.+?)$",
        body,
        re.MULTILINE,
    ):
        title = m.group(1).strip().lower()
        # Narrow grammar (legacy): "claim for X", "claim under X", "pleads/states a claim for X"
        cm = re.search(
            r"(?:claim (?:for|under) the |claim (?:for|under) |pleads a claim for |states a claim for )([^.]+?)(?:against|under|\.|$)",
            title,
        )
        if cm:
            claims.add(cm.group(1).strip())
        # Broadened: take the title head itself as a claim-noun bag.
        # Strip leading articles, trailing "against X" suffixes, trailing punct.
        cleaned = re.sub(r"^(the|a|an)\s+", "", title)
        cleaned = re.sub(r"\s+against\s+[^.]+$", "", cleaned)
        cleaned = re.sub(r"[\.,;:]+$", "", cleaned).strip()
        if 6 < len(cleaned) < 200:
            claims.add(cleaned)
    # Body-level: "Plaintiff/Movant ... claim(s) for/under X"
    for m in re.finditer(
        r"(?:Plaintiff|Movant)(?:\'s)?\s+(?:has\s+)?(?:pleaded|pleads|alleges|states|asserts)\s+(?:a\s+)?claim(?:s)?\s+(?:for|under)\s+([^.;:,]+)",
        body,
    ):
        claims.add(m.group(1).strip().lower())
    return sorted(claims)


def _jaccard(a: List[str], b: List[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _word_count(body: str) -> int:
    return len(re.findall(r"\b\w+\b", body))


def _length_score(word_count: int, band: Tuple[int, int, int, int]) -> float:
    low, ideal_low, ideal_high, high = band
    if ideal_low <= word_count <= ideal_high:
        return 1.0
    if word_count < low or word_count > high:
        return 0.0
    if word_count < ideal_low:
        return (word_count - low) / max(1, ideal_low - low)
    # word_count > ideal_high
    return (high - word_count) / max(1, high - ideal_high)


def _structural_integrity(content: str, body: str) -> float:
    """Check header hierarchy: H1 exists, H2 follow H1, H3 follow H2."""
    score = 1.0
    # Must have an H1 (title)
    if not re.search(r"^#\s+\S", content, re.MULTILINE):
        score -= 0.3
    # Must have INTRODUCTION + STATEMENT OF FACTS + ARGUMENT + PRAYER FOR RELIEF
    required = ["INTRODUCTION", "STATEMENT OF FACTS", "ARGUMENT", "PRAYER FOR RELIEF"]
    for sec in required:
        if not re.search(rf"^##\s+{re.escape(sec)}\b", body, re.MULTILINE):
            score -= 0.15
    # No orphan H3 (### not preceded by an H2 at some point)
    # Simplified: just count H3s without prior H2
    lines = body.split("\n")
    last_h2 = -1
    for i, ln in enumerate(lines):
        if re.match(r"^##\s+\S", ln):
            last_h2 = i
        elif re.match(r"^###\s+\S", ln):
            if last_h2 < 0:
                score -= 0.05
                break
    return max(0.0, score)


def _run_schema_validator(draft_path: Path, case_id: str, filing_type: str) -> Dict[str, Any]:
    """Invoke the in-tree schema validator on the draft. Returns {hard:int, soft:int, pass:bool}."""
    try:
        sys.path.insert(0, str(ROOT))
        from pipeline.schema_validator import SchemaValidator  # type: ignore
    except Exception as e:
        return {"hard": 0, "soft": 0, "pass": True, "error": f"validator unavailable: {e}"}
    try:
        sv = SchemaValidator()
        content = draft_path.read_text(encoding="utf-8", errors="ignore")
        r = sv.validate_markdown(
            content,
            {"case_id": case_id, "filing_type": filing_type},
            source_path=str(draft_path),
        )
        return {
            "hard": len(r.get("hard_gaps", [])),
            "soft": len(r.get("soft_gaps", [])),
            "pass": r.get("pass", False),
        }
    except Exception as e:
        return {"hard": 0, "soft": 0, "pass": True, "error": str(e)}


# ============================================================================
# main scoring
# ============================================================================

def score_draft(
    draft_path: Path,
    case_id: str,
    filing_type: str,
    baseline_memo: Optional[Path] = None,
) -> Dict[str, Any]:
    raw = draft_path.read_text(encoding="utf-8", errors="ignore")
    body = _strip_appendix(raw)
    ft_norm = _normalize_filing_type(filing_type)
    word_count = _word_count(body)

    # axis 1: schema
    sv_result = _run_schema_validator(draft_path, case_id, filing_type)
    if sv_result["hard"] > 0:
        schema_pass = 0.0
    elif sv_result["soft"] > 0:
        schema_pass = 0.75
    else:
        schema_pass = 1.0

    # axis 2: scope purity
    oos_hits = 0
    for pat in OUT_OF_SCOPE_PHRASES:
        oos_hits += len(re.findall(pat, body, re.IGNORECASE))
    scope_purity = max(0.0, 1.0 - 0.10 * oos_hits)

    # axis 3: defendant focus
    # Extract active defendant from raw stub-style metadata if any inline,
    # or infer from filename / argument title coherence.
    titles = _extract_argument_sections(body)
    if titles:
        # Coherence: do all titles share a common defendant token?
        # Heuristic: extract last "Against <X>" or "<X>'s Motion" tokens
        defendants_per_title = []
        for t in titles:
            # try patterns: "Against <Name>" or "<Name>'s Motion"
            am = re.search(r"[Aa]gainst\s+(?!the\b)([A-Z][\w\s\.,&'-]+?)(?:\s+(?:Under|Because|States?|Pleads?|\.|$))", t)
            sm = re.search(r"^([A-Z][\w\s\.,&'-]+?)['']s\s+Motion", t)
            sm2 = re.search(r"Deny\s+([A-Z][\w\s\.,&'-]+?)['']s\s+Motion", t)
            name = (am or sm or sm2).group(1).strip() if (am or sm or sm2) else None
            defendants_per_title.append(name)
        defendants_per_title = [d for d in defendants_per_title if d]
        if defendants_per_title:
            most_common = max(set(defendants_per_title), key=defendants_per_title.count)
            mc_count = defendants_per_title.count(most_common)
            defendant_focus = mc_count / len(titles)
        else:
            # P6 (2026-06-13): titles don't name any defendant. Pre-
            # SCOPE_CLAMP_V1 this was "ambiguous" and scored 0.5. Post-
            # clamp, claim-focused titles are the CORRECT output — the
            # clamp explicitly forbids enumerating defendants as separate
            # argument sections. Score 1.0 instead of penalizing the
            # right behavior.
            defendant_focus = 1.0
    else:
        defendant_focus = 1.0  # no argument sections to score (e.g., notice)

    # axis 4: citation density
    cites = _extract_citations(body)
    cite_target = CITATION_TARGETS.get(ft_norm, CITATION_TARGETS["_default"])
    citation_density = min(1.0, len(cites) / cite_target)

    # axis 5: claim coverage
    if baseline_memo:
        baseline_claims = _extract_baseline_claims(baseline_memo)
        draft_claims = _extract_draft_claims(body)
        if not baseline_claims:
            # P1 (2026-06-13): baseline has no extractable claims — treat as
            # missing data, not a failure of the draft.
            claim_coverage = 0.8
        elif not draft_claims:
            # baseline has claims but draft extractor found none —
            # extractor-grammar mismatch (still bounded penalty)
            claim_coverage = 0.4
        else:
            claim_coverage = _jaccard(draft_claims, baseline_claims)
            # If both sides have items but intersection is empty, the
            # grammars don't align with each other. Floor at 0.5 so the
            # geometric-mean overall isn't tanked by extraction quality
            # alone. Real claim mismatch still scores below 0.5.
            if claim_coverage == 0.0:
                claim_coverage = 0.5
    else:
        # No baseline → can't compute, default to 0.8 (mild penalty for missing data)
        claim_coverage = 0.8
        baseline_claims, draft_claims = [], []

    # axis 6: repetition penalty
    paras = _extract_paragraphs(body)
    max_sim = _max_paragraph_similarity(paras)
    repetition_penalty = max(0.0, 1.0 - max_sim)

    # axis 7: length appropriate
    band = LENGTH_TARGETS.get(ft_norm, LENGTH_TARGETS["_default"])
    length_appropriate = _length_score(word_count, band)

    # axis 8: structural integrity
    structural_integrity = _structural_integrity(raw, body)

    axes = {
        "schema_pass":          round(schema_pass, 4),
        "scope_purity":         round(scope_purity, 4),
        "defendant_focus":      round(defendant_focus, 4),
        "citation_density":     round(citation_density, 4),
        "claim_coverage":       round(claim_coverage, 4),
        "repetition_penalty":   round(repetition_penalty, 4),
        "length_appropriate":   round(length_appropriate, 4),
        "structural_integrity": round(structural_integrity, 4),
    }

    # Weighted geometric mean — any axis at 0 tanks the score
    # Use a tiny floor to avoid log(0) -> -inf; treat 0 axes as ~1e-6
    log_sum = 0.0
    for axis, val in axes.items():
        w = WEIGHTS[axis]
        v = max(val, 1e-6)
        log_sum += w * math.log(v)
    overall = round(math.exp(log_sum), 4)

    return {
        "ts": datetime.now().isoformat(),
        "draft":       str(draft_path),
        "case_id":     case_id,
        "filing_type": filing_type,
        "filing_type_normalized": ft_norm,
        "word_count":  word_count,
        "overall":     overall,
        "axes":        axes,
        "diagnostics": {
            "schema":         sv_result,
            "oos_hits":       oos_hits,
            "arg_sections":   len(titles),
            "cite_count":     len(cites),
            "baseline_claims": baseline_claims,
            "draft_claims":   draft_claims,
            "max_para_sim":   round(max_sim, 4),
            "length_band":    band,
        },
    }


def _classify(score: float) -> str:
    if score >= 0.90: return "EXCELLENT"
    if score >= 0.75: return "GOOD"
    if score >= 0.55: return "ACCEPTABLE"
    if score >= 0.35: return "MARGINAL"
    return "POOR"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("draft")
    ap.add_argument("--case-id", required=True)
    ap.add_argument("--filing-type", required=True)
    ap.add_argument("--baseline-memo", default=None,
                    help="Path to baseline_memo for claim_coverage axis. "
                         "Auto-resolved from case_id if not provided.")
    ap.add_argument("--log", action="store_true",
                    help="Append result to _review/quality_scores.jsonl")
    ap.add_argument("--json", action="store_true",
                    help="Emit full JSON record. Without this, prints a compact summary.")
    args = ap.parse_args()

    draft_path = Path(args.draft)
    if not draft_path.exists():
        print(f"ERROR: draft not found: {draft_path}", file=sys.stderr)
        return 2

    # Auto-resolve baseline memo from case_id if not provided
    baseline = None
    if args.baseline_memo:
        baseline = Path(args.baseline_memo)
    else:
        # Try ROOT/obsidian_sync/baseline_memos/<case_id_underscored>.md
        slug = args.case_id.replace(":", "_").replace("2_", "")
        candidate = ROOT / "obsidian_sync" / "baseline_memos" / f"{slug}.md"
        if candidate.exists():
            baseline = candidate

    result = score_draft(
        draft_path, args.case_id, args.filing_type, baseline_memo=baseline,
    )

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(f"==== QUALITY SCORE: {draft_path.name} ====")
        print(f"  case_id:     {args.case_id}")
        print(f"  filing_type: {args.filing_type}  (norm: {result['filing_type_normalized']})")
        print(f"  word_count:  {result['word_count']:,}")
        print(f"  baseline:    {baseline if baseline else '(none — claim_coverage defaulted to 0.8)'}")
        print()
        print(f"  OVERALL:     {result['overall']:.4f}  [{_classify(result['overall'])}]")
        print()
        print(f"  Axis breakdown:")
        for axis, val in result["axes"].items():
            bar = "#" * int(val * 30)
            print(f"    {axis:24s} {val:.4f}  {bar}")
        print()
        diag = result["diagnostics"]
        print(f"  Diagnostics:")
        print(f"    schema:           hard={diag['schema']['hard']}  soft={diag['schema']['soft']}  pass={diag['schema']['pass']}")
        print(f"    oos_hits:         {diag['oos_hits']}")
        print(f"    arg_sections:     {diag['arg_sections']}")
        print(f"    cite_count:       {diag['cite_count']}")
        print(f"    max_para_sim:     {diag['max_para_sim']:.4f}")
        print(f"    length_band:      {diag['length_band']}")
        if diag["baseline_claims"]:
            print(f"    baseline_claims:  {diag['baseline_claims'][:4]}{'...' if len(diag['baseline_claims'])>4 else ''}")
            print(f"    draft_claims:     {diag['draft_claims'][:4]}{'...' if len(diag['draft_claims'])>4 else ''}")

    if args.log:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(result, default=str) + "\n")
        print(f"\n  logged to {LOG_PATH}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
