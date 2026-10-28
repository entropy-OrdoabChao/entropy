"""
pipeline/violation_chain_builder.py
====================================
Stage 4 — Violation Chain Detector (Lane A, Session 22).

Consumes the human-approved CASE_GROUND_TRUTH.yaml for a case and emits
structured violation chains: defendant → claim → judicial error → controlling
authority → remedy. Binding-authority labels are assigned by a regex
classifier (BINDING_SCOTUS / BINDING_GOVERNING_CIRCUIT / NON_PRECEDENTIAL / ...).

This is GT-driven by design. The yaml IS the source of truth for the chain.
Scanner output from pipeline/judicial_findings_bridge can be merged in as
optional corroboration but is not load-bearing.

PUBLIC API
----------
build_violation_chains(case_id: str) -> dict

Returns:
  {
    case_id, case_slug, case_type, generated_at,
    ground_truth_provenance, chain_count, chains, global_authority_index,
    gt_yaml_path,
  }

Each chain shape:
  {
    chain_id, defendant, claim_taxonomy, judicial_error,
    controlling_authority, opposing_authority_used_by_court,
    remedies, scanner_corroboration,
  }
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import re
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Any, Optional

import yaml

log = logging.getLogger("violation_chain_builder")

ROOT = _eroot()
VAULT_CASES = ROOT / "obsidian_sync" / "cases"

# ── Court → governing circuit map ─────────────────────────────────────
COURT_TO_CIRCUIT = {
    "EDPA":   "3d Cir.",
    "WDPA":   "3d Cir.",
    "MDPA":   "3d Cir.",
    "DNJ":    "3d Cir.",
    "DDE":    "3d Cir.",
    "PA_CCP": None,  # state court — no federal circuit governance
    "CA3":    "3d Cir.",
}

# ── Binding-label classifier patterns ─────────────────────────────────
_RE_SCOTUS_USREPORTS    = re.compile(r"\b\d+\s+U\.S\.\s+\d+", re.I)
_RE_FED_RULE            = re.compile(r"Fed\.\s*R\.\s*(Civ\.|App\.|Crim\.|Evid\.)\s*P\.|FRCP|F\.R\.A\.P\.|F\.R\.E\.", re.I)
_RE_FRAP = re.compile(r"\b(FRAP|F\.R\.A\.P\.)\b", re.I)
_RE_LAR = re.compile(r"(L\.A\.R\.|\bLAR\b|Cir\.\s*R\.|Local Rule|Local Civ\.\s*R\.|Circuit Rule)", re.I)
_RE_CANON = re.compile(r"\bCanon\s+\d|Code of Conduct", re.I)
_RE_USC                 = re.compile(r"\b\d+\s+U\.S\.C\.\s*§", re.I)
_RE_STATE_RULE          = re.compile(r"Pa\.R\.C\.P\.|N\.J\.R\.", re.I)
_RE_STATE_STATUTE       = re.compile(r"\b\d+\s+Pa\.\s*C\.S\.|N\.J\.S\.A\.|\bP\.S\.\s*§", re.I)
_RE_NONPRECEDENTIAL     = re.compile(r"F\.\s*App'x|Fed\.\s*Appx", re.I)
_RE_F3D                 = re.compile(r"\b\d+\s+F\.\s*(3d|4th)\s+\d+", re.I)
_RE_FSUPP               = re.compile(r"\bF\.\s*Supp\.\s*(2d|3d)?\s*\d+", re.I)
_RE_CIRCUIT_PAREN       = re.compile(r"\(([^)]*Cir\.[^)]*)\)", re.I)


def _looks_like_citation(s: str) -> bool:
    """True if s plausibly contains a legal citation.

    Filters out issue labels ('lack of diversity / removal defects'),
    bookkeeping notes ('Pruden non-prec', 'Westinghouse misapplication'),
    and bare docket refs ('3d Cir. Appeal 26-0001').

    Recognition: any of:
      - statutory/rule patterns (§, U.S.C., Pa.C.S., FRCP, Fed. R.)
      - case patterns (v. or vs. or v )
      - reporter patterns (U.S., F.3d, F.4th, F. App'x, F. Supp.)
    """
    s = s.strip()
    if not s:
        return False
    # Statute or rule markers
    if re.search(
        r"§|U\.S\.C\.|Pa\.\s*C\.S\.|N\.J\.S\.A\.|Pa\.R\.C\.P\.|"
        r"Fed\.\s*R\.|FRCP|FRAP|F\.R\.A\.P\.|F\.R\.E\.|F\.R\.B\.P\.|"
        r"L\.A\.R\.|LAR|Local Rule|Local Civ\.\s*R\.|"
        r"Cir\.\s*R\.|Circuit Rule|"
        r"Canon\s+\d|Code of Conduct",
        s, re.I,
    ):
        return True
    # Case caption ("X v. Y" / "X vs. Y")
    if re.search(r"\bvs?\.?\s+[A-Z]", s):
        return True
    # Reporter
    if re.search(r"\b\d+\s+(U\.S\.|F\.\s*(3d|4th|2d|Supp)|F\.\s*App'x|Fed\.\s*Appx)", s, re.I):
        return True
    return False


def _classify_binding_label(citation: str, governing_circuit: Optional[str]) -> str:
    """Classify a citation as binding/persuasive/non-precedential.

    governing_circuit is e.g. '3d Cir.' for EDPA; None for state courts.
    """
    c = citation.strip()
    # Order matters: NON_PRECEDENTIAL must beat F.3d match
    if _RE_NONPRECEDENTIAL.search(c):
        return "NON_PRECEDENTIAL"
    if _RE_SCOTUS_USREPORTS.search(c):
        return "BINDING_SCOTUS"
    if _RE_FED_RULE.search(c):
        return "BINDING_FED_RULE"
    if _RE_FRAP.search(c):
        return "BINDING_FED_RULE"
    if _RE_LAR.search(c):
        # Local appellate rules are binding only in their circuit. If the
        # citation contains the governing-circuit prefix (e.g. "3d Cir." in
        # "3d Cir. L.A.R. 107.2"), label as binding. Compare in lowercase
        # with periods normalized so "3d Cir." matches "3d cir."
        if governing_circuit:
            gc = governing_circuit.lower().rstrip(".").strip()
            cl = c.lower()
            if gc and gc in cl:
                return "BINDING_GOVERNING_CIRCUIT"
        return "BINDING_FED_RULE"  # Fall-through: still a rule
    if _RE_CANON.search(c):
        return "BINDING_FED_RULE"
    if _RE_USC.search(c):
        return "BINDING_STATUTE"
    if _RE_STATE_RULE.search(c):
        return "BINDING_STATE_RULE"
    if _RE_STATE_STATUTE.search(c):
        return "BINDING_STATE_STATUTE"
    if _RE_F3D.search(c):
        # Check circuit
        m = _RE_CIRCUIT_PAREN.search(c)
        if m and governing_circuit and governing_circuit.lower() in m.group(1).lower():
            return "BINDING_GOVERNING_CIRCUIT"
        return "PERSUASIVE_OTHER_CIRCUIT"
    if _RE_FSUPP.search(c):
        return "PERSUASIVE_DISTRICT"
    return "UNKNOWN"


def _classify_error_type(contradiction: Optional[dict],
                         non_binding_citations: List[dict],
                         service_facts: Optional[dict]) -> str:
    """Coarse judge-error category based on what GT contains."""
    if contradiction:
        return "factual_contradiction"
    if non_binding_citations:
        return "non_binding_misapplication"
    if service_facts and service_facts.get("the_judge_s_error"):
        return "doctrinal_conflation"
    return "unspecified"


def _gt_yaml_path_for(case_id: str) -> Optional[Path]:
    """Resolve which CASE_GROUND_TRUTH.yaml belongs to this case_id.

    case_id format: 'fed:2:26-cv-00123' or 'phila_ccp:260100000'.
    """
    # Try each case folder; the yaml's own case_id field is the source of truth.
    for case_dir in sorted(VAULT_CASES.glob("*/")):
        gt = case_dir / "meta" / "CASE_GROUND_TRUTH.yaml"
        if not gt.exists():
            continue
        try:
            with gt.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if data and data.get("case_id") == case_id:
                return gt
        except Exception:
            continue
    return None



def _defendant_name_matches(caption_name: str, affected_name: str) -> bool:
    """Fuzzy defendant name match for cross-yaml-section linkage.

    Handles common variants between caption.defendants[].name and
    pending_motions[].defendants_affected[] / procedural_defects[].defendants_affected[]:
      - Case differences ('Amerisave' vs 'AmeriSave')
      - Corporate-suffix variants ('Planet Home Lending' vs 'Planet Home Lending, LLC')
      - Punctuation/whitespace differences

    Strategy:
      1. Lowercase + strip + collapse whitespace + strip common suffixes
      2. Exact match on normalized form → True
      3. Substring (either direction) on normalized form → True
      4. First two name tokens (e.g. "amerisave mortgage") substring match → True
    """
    if not caption_name or not affected_name:
        return False
    import re as _re

    def _norm(s: str) -> str:
        s = (s or "").lower().strip()
        s = _re.sub(r"\s+", " ", s)
        # Strip common corporate suffixes and trailing punctuation
        s = _re.sub(r"[,.]+\s*(llc|l\.l\.c\.|inc|inc\.|corporation|corp|corp\.|co|co\.|company|n\.a\.|na|ltd|ltd\.|p\.c\.|pc)\s*$", "", s)
        s = _re.sub(r"\s+(llc|inc|corporation|corp|company|n\.a\.|na|ltd|p\.c\.|pc)\s*$", "", s)
        s = s.rstrip(",.; ")
        return s.strip()

    a = _norm(caption_name)
    b = _norm(affected_name)
    if not a or not b:
        return False
    if a == b:
        return True
    if a in b or b in a:
        return True
    # Two-token prefix match: "amerisave mortgage" matches both forms
    a_toks = a.split()
    b_toks = b.split()
    if len(a_toks) >= 2 and len(b_toks) >= 2:
        if a_toks[:2] == b_toks[:2]:
            return True
    return False


_CITE_TOKEN_PATTERNS = [
    # Reporters (volume + reporter + page)
    re.compile(r"\b(\d+\s+U\.\s*S\.\s+\d+)\b"),
    re.compile(r"\b(\d+\s+S\.\s*Ct\.\s+\d+)\b"),
    re.compile(r"\b(\d+\s+L\.\s*Ed\.\s*\d*d?\s+\d+)\b"),
    re.compile(r"\b(\d+\s+F\.\s*4th\s+\d+)\b"),
    re.compile(r"\b(\d+\s+F\.\s*3d\s+\d+)\b"),
    re.compile(r"\b(\d+\s+F\.\s*2d\s+\d+)\b"),
    re.compile(r"\b(\d+\s+F\.\s*App'?x\.?\s+\d+)\b"),
    re.compile(r"\b(\d+\s+F\.\s*Supp\.?\s*\d?d?\s+\d+)\b"),
    # Statutes
    re.compile(r"\b(\d{1,2}\s*U\.S\.C\.\s*§?\s*\d+(?:\(\w+\))*)", re.I),
    re.compile(r"\b(\d{1,2}\s*C\.F\.R\.\s*§?\s*\d+(?:\.\d+)?)", re.I),
    re.compile(r"\b(\d+\s*Pa\.C\.S\.\s*§?\s*\d+)", re.I),
    # Federal rules
    re.compile(r"\b(Fed\.\s*R\.\s*Civ\.\s*P\.\s*\d+(?:\(\w+\))*)", re.I),
    re.compile(r"\b(Fed\.\s*R\.\s*App\.\s*P\.\s*\d+(?:\(\w+\))*)", re.I),
    re.compile(r"\b(Fed\.\s*R\.\s*Evid\.\s*\d+)", re.I),
    re.compile(r"\b(FRCP\s*\d+(?:\(\w+\))*)", re.I),
    re.compile(r"\b(FRAP\s*\d+(?:\(\w+\))*)", re.I),
    re.compile(r"\b(L\.A\.R\.\s*\d+(?:\.\d+)?)", re.I),
    # Pennsylvania
    re.compile(r"\b(Pa\.\s*R\.\s*C\.\s*P\.\s*\d+)", re.I),
    re.compile(r"\b(Pa\.\s*R\.\s*A\.\s*P\.\s*\d+)", re.I),
    re.compile(r"\b(\d+\s+Pa\.\s*Code(?:\s*§?\s*\d+(?:\.\d+)?)?)", re.I),
]


def _extract_citation_tokens(citation_str):
    """Extract canonical citation tokens from a verbose GT string.

    Input: "Westinghouse Electric Corp., 90 F.3d 696 (3d Cir. 1996)"
    Output: ["90 F.3d 696"]
    """
    tokens = []
    seen = set()
    for rx in _CITE_TOKEN_PATTERNS:
        for m in rx.finditer(citation_str):
            t = re.sub(r"\s+", " ", m.group(1)).strip()
            if t and t not in seen:
                seen.add(t)
                tokens.append(t)
    return tokens


def _resolve_authority(citation, jurisdiction=None):
    """Look up a citation in the primary_sources index.

    For verbose GT strings, extract canonical citation tokens first
    (e.g. "Smith v. Jones, 90 F.3d 696 (3d Cir. 1996)" → "90 F.3d 696"),
    then query each token. Returns deduped list of matching rule nodes.
    Failures are non-fatal.
    """
    try:
        from pipeline.authority_resolver import resolve_citation
    except FileNotFoundError:
        return []
    except Exception as e:
        log.debug(f"_resolve_authority import failed: {e}")
        return []

    tokens = _extract_citation_tokens(citation) or [citation]
    seen_paths = set()
    out = []
    for tok in tokens:
        try:
            hits = resolve_citation(tok, jurisdiction=jurisdiction, limit=5)
        except Exception as e:
            log.debug(f"_resolve_authority({tok!r}) failed: {e}")
            continue
        for h in hits:
            p = h.get("path")
            if p and p not in seen_paths:
                seen_paths.add(p)
                h["matched_token"] = tok
                out.append(h)
                if len(out) >= 5:
                    return out
    return out


def _accountability_paths(binding_label, citation, error_type):
    """Map a binding-authority violation to concrete accountability complaint paths.

    Returns list of {body, path, basis, form, trigger} entries identifying where a
    misconduct or regulatory complaint can be lodged for this violation.
    """
    paths = []
    cite_low = (citation or "").lower()
    if binding_label in ("BINDING_SCOTUS", "BINDING_GOVERNING_CIRCUIT", "BINDING_FED_RULE"):
        paths.append({
            "body": "Judicial Council of the Third Circuit",
            "path": "judicial_misconduct_complaint",
            "basis": "28 U.S.C. § 351 (judicial misconduct or disability)",
            "form": "JC Form (3d Cir.)",
            "trigger": f"Judge disregarded binding authority: {citation}",
        })
    if binding_label in ("BINDING_STATE_RULE", "BINDING_STATE_STATUTE"):
        paths.append({
            "body": "Pennsylvania Judicial Conduct Board",
            "path": "pa_judicial_conduct_complaint",
            "basis": "Pa. Const. Art. V § 18(b); 42 Pa.C.S. § 3331",
            "form": "JCB Complaint Form",
            "trigger": f"Judge disregarded binding state authority: {citation}",
        })
    if "15 u.s.c" in cite_low and ("1681" in cite_low or "1692" in cite_low):
        paths.append({
            "body": "Consumer Financial Protection Bureau (CFPB)",
            "path": "cfpb_complaint",
            "basis": "12 U.S.C. § 5481 et seq.",
            "form": "https://www.consumerfinance.gov/complaint/",
            "trigger": f"FCRA/FDCPA violation: {citation}",
        })
        paths.append({
            "body": "Federal Trade Commission (FTC)",
            "path": "ftc_complaint",
            "basis": "15 U.S.C. § 45 (FTC Act)",
            "form": "https://reportfraud.ftc.gov",
            "trigger": f"Consumer protection violation: {citation}",
        })
    if "12 u.s.c" in cite_low and "2605" in cite_low:
        paths.append({
            "body": "Consumer Financial Protection Bureau (CFPB)",
            "path": "cfpb_complaint_respa",
            "basis": "12 U.S.C. § 2605 (RESPA QWR servicer duty)",
            "form": "https://www.consumerfinance.gov/complaint/",
            "trigger": f"RESPA QWR violation: {citation}",
        })
    if "15 u.s.c" in cite_low and "1635" in cite_low:
        paths.append({
            "body": "Consumer Financial Protection Bureau (CFPB)",
            "path": "cfpb_complaint_tila",
            "basis": "15 U.S.C. § 1635 (TILA rescission)",
            "form": "https://www.consumerfinance.gov/complaint/",
            "trigger": f"TILA violation: {citation}",
        })
    if "pa.r.c.p" in cite_low or "pa. r.c.p" in cite_low or "pa.c.s" in cite_low:
        paths.append({
            "body": "Pennsylvania Disciplinary Board (Office of Disciplinary Counsel)",
            "path": "pa_disciplinary_complaint",
            "basis": "Pa.R.D.E. 207",
            "form": "https://www.padisciplinaryboard.org/for-the-public/file-a-complaint",
            "trigger": f"Pa. rule violation by counsel: {citation}",
        })
    return paths




# ── Source-doc → defendant resolver (Session 23) ──────────────────────────
# Maps a scanner finding's source_doc filename (e.g. "ecf 122.pdf") to the
# defendant(s) it pertains to, by reading the yaml's defendants[] fields:
#   - defendants[].ecf_answer: "ECF 14, 69"      → ECF 14, ECF 69 → that defendant
#   - defendants[].dismissed:  "ECF 122 (09/19/2025)" → ECF 122 → that defendant
#   - defendants[].notes:      may reference ECF/Dkt numbers too
# Falls back to: include for all defendants (current behavior) only if no
# specific mapping is found, AND the source_doc doesn't look like a numbered
# judge order (e.g. "ecf 41.pdf"). Generic plaintiff memoranda always go to all.

_ECF_DKT_RE = re.compile(r"(?:ecf|dkt|docket)\s*(?:no\.?\s*)?(\d+)", re.IGNORECASE)
_NUMBERED_ORDER_RE = re.compile(r"(?:^|[^a-z0-9])(?:ecf|dkt)\s*\d+", re.IGNORECASE)


def _build_source_doc_defendant_map(defendants: List[dict]) -> Dict[str, List[str]]:
    """Build {ecf_number_str: [defendant_name, ...]} from yaml defendants[].

    Returns mapping like {"122": ["Example Card Issuer (USA), N.A."], "14": ["TransUnion LLC"]}
    Also handles comma-separated lists like "ECF 14, 69" by re-scanning after each
    primary match for trailing bare numbers.
    """
    # Regex variants:
    #   ECF_PRIMARY: "ECF 14" or "Dkt 122" (anchored with keyword)
    #   ECF_TRAILING: bare numbers separated by commas/and/&: ", 69" or "and 70"
    primary_re = re.compile(r"(?:ecf|dkt|docket)\s*(?:no\.?\s*)?(\d+)((?:\s*[,&]\s*(?:and\s+)?\d+)*)", re.IGNORECASE)
    trailing_re = re.compile(r"\d+")

    mapping: Dict[str, List[str]] = {}
    for d in defendants or []:
        if not isinstance(d, dict):
            continue
        name = d.get("name")
        if not name:
            continue
        text_blob = " ".join(str(d.get(k, "")) for k in ("ecf_answer", "dismissed", "notes"))
        for m in primary_re.finditer(text_blob):
            # Primary number
            nums = [m.group(1)]
            # Any trailing numbers in the comma/and-separated tail
            tail = m.group(2) or ""
            nums.extend(trailing_re.findall(tail))
            for num in nums:
                mapping.setdefault(num, [])
                if name not in mapping[num]:
                    mapping[num].append(name)
    return mapping


def _extract_ecf_from_filename(filename: str) -> Optional[str]:
    """Extract the first ECF/Dkt number from a filename. Returns None if none found."""
    if not filename:
        return None
    m = _ECF_DKT_RE.search(filename)
    return m.group(1) if m else None


def _filter_scanner_findings_for_defendant(
    findings: List[dict],
    defendant_name: str,
    source_map: Dict[str, List[str]],
) -> List[dict]:
    """Filter scanner findings to those relevant to this defendant.

    A finding is relevant if:
      (a) Its source_doc has an ECF/Dkt number AND that number maps to this defendant
      (b) Its source_doc is a generic Plaintiff filing (no ECF number) — included for all defendants
      (c) Its source_doc has an ECF number NOT in any mapping — included for all defendants
          (e.g. judge orders affecting the case as a whole like prior_restraint orders)
    A finding is EXCLUDED if its source_doc has an ECF number mapped to OTHER defendants.
    """
    filtered = []
    for f in findings:
        sd = f.get("source_doc", "") or ""
        ecf_num = _extract_ecf_from_filename(sd)
        if ecf_num is None:
            # No ECF number in filename — generic doc, include for all
            filtered.append(f)
            continue
        if ecf_num not in source_map:
            # ECF number not yaml-mapped to anyone — case-wide finding
            filtered.append(f)
            continue
        # ECF mapped to specific defendants — include only if THIS defendant is in the list
        if defendant_name in source_map[ecf_num]:
            # Tag the finding so consumer knows it's per-defendant attributed
            f2 = dict(f)
            f2["attributed_via"] = f"ecf_{ecf_num}_in_yaml"
            filtered.append(f2)
    return filtered


def _build_chain_for_defendant(
    defendant: dict,
    gt: dict,
    governing_circuit: Optional[str],
    chain_idx: int,
    case_slug: str,
    scanner_findings: List[dict],
) -> dict:
    """Build a single violation chain for one defendant."""
    latest_order = gt.get("latest_order", {}) or {}
    contradictions = latest_order.get("contradictions") or []
    non_binding = latest_order.get("non_binding_citations_used") or []
    service_facts = gt.get("service_facts") or {}
    pending_motions = gt.get("pending_motions") or []

    # Which contradiction (if any) involves this defendant?
    # The yaml describes contradictions narratively. We match by defendant name.
    dname = (defendant.get("name") or "").lower()
    matching_contradiction = None
    for c in contradictions:
        desc = (c.get("description") or "").lower()
        # crude: check if any name token (first word) appears
        # e.g. "Experian" appears in description
        first_token = dname.split()[0] if dname.split() else ""
        if first_token and first_token in desc:
            matching_contradiction = c
            break

    # Service facts apply only to the defendant they name
    matching_service = None
    # service_facts may be a list of events (new schema) or a single dict (legacy).
    # Normalize: pick the event matching this defendant, else first item, else {}.
    _sf = service_facts
    if isinstance(_sf, list):
        _matched = None
        _def_name_lower = (defendant.get("name") or "").lower()
        for _ev in _sf:
            if not isinstance(_ev, dict):
                continue
            _ev_def = (_ev.get("defendant") or "").lower()
            if _ev_def and _ev_def in _def_name_lower:
                _matched = _ev
                break
        if _matched is None:
            _matched = next((e for e in _sf if isinstance(e, dict)), {})
        _sf = _matched
    elif not isinstance(_sf, dict):
        _sf = {}
    sf_defendant = (_sf.get("defendant") or "").lower()
    if sf_defendant and (sf_defendant == dname or
                        (dname.split() and dname.split()[0] in sf_defendant)):
        matching_service = service_facts

    # Controlling authority — drawn from service_facts if matched, else from
    # pending_motions whose defendants_affected include this defendant
    controlling: List[Dict[str, str]] = []
    if matching_service:
        for cite in matching_service.get("controlling_authority", []) or []:
            controlling.append({
                "citation": cite,
                "binding_label": _classify_binding_label(cite, governing_circuit),
                "source": "service_facts.controlling_authority",
            })

    # Pending motions affecting this defendant
    remedies: List[dict] = []
    dname_canonical = defendant.get("name", "")
    for pm in pending_motions:
        affected = pm.get("defendants_affected") or []
        # Fuzzy match: handles "Amerisave" vs "AmeriSave Mortgage Corporation" vs "AmeriSave Mortgage Corporation, LLC", etc.
        if any(_defendant_name_matches(dname_canonical, a) for a in affected):
            remedies.append({
                "priority": pm.get("priority"),
                "motion_type": pm.get("motion_type"),
                "target_dkt": pm.get("target_dkt"),
                "primary_authority": pm.get("primary_authority"),
            })
            # Also harvest authorities from primary_authority field
            pa = pm.get("primary_authority") or ""
            # split on semicolons (matches yaml convention)
            for piece in re.split(r";\s*", pa):
                piece = piece.strip()
                if not piece:
                    continue
                # Skip docket references; those aren't legal authorities
                if re.match(r"^Dkt\b", piece, re.I):
                    continue
                # Skip non-citation bookkeeping / issue labels
                if not _looks_like_citation(piece):
                    continue
                if not any(c.get("citation") == piece for c in controlling):
                    controlling.append({
                        "citation": piece,
                        "binding_label": _classify_binding_label(piece, governing_circuit),
                        "source": f"pending_motion.priority_{pm.get('priority')}",
                    })

    # Opposing authority — non-binding citations the court used
    opposing: List[Dict[str, str]] = []
    for nb in non_binding:
        cite_full = f"{nb.get('case', '')}, {nb.get('reporter', '')}".strip(", ")
        opposing.append({
            "citation": cite_full,
            "binding_label": _classify_binding_label(cite_full, governing_circuit),
            "defect": nb.get("reason_non_binding", ""),
            "source": "latest_order.non_binding_citations_used",
        })

    # Scanner corroboration — findings whose source_doc is for this case
    # (judicial_findings_bridge today returns 0 on this case, but reserve the slot)
    # ── Per-defendant scanner_corroboration filter (Session 23) ──
    # Read all defendants[] from yaml to build ECF→defendant map, then filter.
    _all_defs = (gt.get('caption') or {}).get('defendants') or []
    _src_map = _build_source_doc_defendant_map(_all_defs)
    corroborating = _filter_scanner_findings_for_defendant(
        scanner_findings, defendant.get('name', ''), _src_map
    )

    judicial_error: Dict[str, Any] = {}
    if matching_contradiction:
        judicial_error = {
            "order_dkt": matching_contradiction.get("dkt_ref"),
            "contradicts_dkt": matching_contradiction.get("contradicts_dkt"),
            "narrative": matching_contradiction.get("description"),
        }
    elif matching_service and matching_service.get("the_judge_s_error"):
        judicial_error = {
            "narrative": matching_service["the_judge_s_error"],
        }
    judicial_error["error_type"] = _classify_error_type(
        matching_contradiction, non_binding, matching_service,
    )

    # ── Enrich each authority with obsidian_node_refs + accountability_paths ──
    for _ca in controlling:
        _cite = _ca.get("citation")
        _label = _ca.get("binding_label", "")
        if _cite:
            _ca["obsidian_node_refs"] = _resolve_authority(_cite)
            _ca["accountability_paths"] = _accountability_paths(_label, _cite, judicial_error)
    for _ca in opposing:
        _cite = _ca.get("citation")
        if _cite:
            _ca["obsidian_node_refs"] = _resolve_authority(_cite)

    return {
        "chain_id": f"{case_slug}_chain_{chain_idx}",
        "defendant": defendant.get("name"),
        "defendant_status": defendant.get("status"),
        "claim_taxonomy": defendant.get("claims") or [],
        "judicial_error": judicial_error,
        "controlling_authority": controlling,
        "opposing_authority_used_by_court": opposing,
        "remedies": remedies,
        "scanner_corroboration": corroborating,
    }


def build_violation_chains(case_id: str) -> dict:
    """Build structured violation chains for one case.

    Returns a dict suitable for direct MCP/JSON serialization.
    """
    gt_path = _gt_yaml_path_for(case_id)
    if gt_path is None:
        return {
            "error": f"CASE_GROUND_TRUTH.yaml not found for case_id={case_id!r}",
            "case_id": case_id,
            "chains": [],
            "chain_count": 0,
        }

    try:
        with gt_path.open("r", encoding="utf-8") as f:
            gt = yaml.safe_load(f) or {}
    except Exception as e:
        return {
            "error": f"Failed to parse {gt_path}: {e}",
            "case_id": case_id,
            "chains": [],
            "chain_count": 0,
        }

    case_slug = gt.get("case_slug", "")
    case_type = gt.get("case_type", "")
    court_id = (gt.get("court") or {}).get("id", "")
    governing_circuit = COURT_TO_CIRCUIT.get(court_id)

    defendants = (gt.get("caption") or {}).get("defendants") or []

    # Optional scanner corroboration (non-blocking)
    scanner_findings: List[dict] = []
    try:
        from pipeline.judicial_findings_bridge import collect_judicial_findings
        scanner_findings = collect_judicial_findings(case_id) or []
    except Exception as e:
        log.warning(f"judicial_findings_bridge failed: {e}")

    chains = []
    for i, d in enumerate(defendants, start=1):
        if not isinstance(d, dict):
            continue
        chains.append(_build_chain_for_defendant(
            d, gt, governing_circuit, i, case_slug, scanner_findings,
        ))

    # v2026-05-30: append synthetic judicial_conduct chain (aggregates case-wide)
    try:
        _jc = build_judicial_conduct_chain(case_id, scanner_findings, case_slug)
        if _jc is not None:
            _jc_kinds = JUDICIAL_CONDUCT_KINDS
            # Strip dup'd judicial findings from per-defendant chains
            for _c in chains:
                if isinstance(_c, dict) and _c.get('chain_type') != 'judicial_conduct':
                    _orig = _c.get('scanner_corroboration') or []
                    _kept = []
                    for _f in _orig:
                        if not isinstance(_f, dict):
                            _kept.append(_f)
                            continue
                        _vt = (_f.get('violation_type') or _f.get('kind')
                               or _f.get('error_type'))
                        if _vt not in _jc_kinds:
                            _kept.append(_f)
                    _c['scanner_corroboration'] = _kept
            chains.append(_jc)
    except Exception as _e:
        import logging as _logging
        _logging.getLogger('violation_chain_builder').warning(
            f'judicial_conduct hook failed: {_e}')

    # Build global de-duped authority index (rich: includes obsidian + accountability)
    authority_index: Dict[str, str] = {}
    authority_index_rich: Dict[str, Dict[str, Any]] = {}
    accountability_summary: Dict[str, List[Dict[str, str]]] = {}
    for ch in chains:
        for ca in ch.get("controlling_authority", []) + ch.get("opposing_authority_used_by_court", []):
            cite = ca.get("citation")
            label = ca.get("binding_label")
            if cite and label:
                # Prefer NON_PRECEDENTIAL over later BINDING_* claims about the same string
                if cite not in authority_index:
                    authority_index[cite] = label
                if cite not in authority_index_rich:
                    authority_index_rich[cite] = {
                        "binding_label": label,
                        "obsidian_node_refs": ca.get("obsidian_node_refs", []),
                        "accountability_paths": ca.get("accountability_paths", []),
                    }
            for ap in ca.get("accountability_paths", []) or []:
                body = ap.get("body", "unknown")
                if body not in accountability_summary:
                    accountability_summary[body] = []
                key = (ap.get("path"), ap.get("basis"))
                if not any((p.get("path"), p.get("basis")) == key for p in accountability_summary[body]):
                    accountability_summary[body].append(ap)

    return {
        "case_id": case_id,
        "case_slug": case_slug,
        "case_type": case_type,
        "court_id": court_id,
        "governing_circuit": governing_circuit,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ground_truth_provenance": gt.get("_provenance") or {},
        "chain_count": len(chains),
        "chains": chains,
        "global_authority_index": authority_index,
        "global_authority_index_rich": authority_index_rich,
        "accountability_summary": accountability_summary,
        "gt_yaml_path": str(gt_path),
        "scanner_findings_count": len(scanner_findings),
    }


if __name__ == "__main__":
    import sys
    case_id = sys.argv[1] if len(sys.argv) > 1 else "fed:2:26-cv-00123"
    out = build_violation_chains(case_id)
    print(json.dumps(out, indent=2, default=str))


# ─────────────────────────────────────────────────────────────────────────
# v2026-05-30: synthetic judicial_conduct chain
# ─────────────────────────────────────────────────────────────────────────
JUDICIAL_CONDUCT_KINDS = {
    "PRIOR_RESTRAINT_FILING_BAN", "DO_NOT_FILE_LANGUAGE",
    "ACCESS_TO_COURTS_DENIAL", "NO_REASONS_STATED",
    "DENIED_WITHOUT_REASONS", "FRAUD_ON_COURT_JUDICIAL",
    "REMAND_DENIED", "IMPROPER_REMOVAL_FACT",
    "DEPRIVATION_RIGHTS_COLOR_OF_LAW",
}


def build_judicial_conduct_chain(case_id, all_findings, case_slug=None):
    """Synthetic chain aggregating case-wide judicial conduct findings."""
    if not all_findings:
        return None
    relevant = []
    seen_keys = set()
    for f in all_findings:
        if not isinstance(f, dict):
            continue
        kind = (f.get("violation_type") or f.get("kind")
                or f.get("error_type"))
        if kind not in JUDICIAL_CONDUCT_KINDS:
            continue
        # Dedup by (source_doc, violation_type, quote-prefix)
        key = (f.get("source_doc"), kind, str(f.get("quote",""))[:80])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        relevant.append(f)
    if not relevant:
        return None
    if not case_slug:
        parts = case_id.split(":")
        case_slug = parts[-1] if parts else case_id
    return {
        "chain_id": f"{case_slug}_judicial_conduct",
        "chain_type": "judicial_conduct",
        "defendant": "Court (judicial conduct)",
        "target": "court",
        "case_id": case_id,
        "scanner_corroboration": relevant,
        "finding_count": len(relevant),
        "kinds_aggregated": sorted({
            (f.get("violation_type") or f.get("kind") or f.get("error_type"))
            for f in relevant
        }),
        "remedies": [
            {"motion_type": "rule_60d3_fraud_on_court",
             "primary_authority": "FRCP 60(d)(3); Hazel-Atlas; Herring 424 F.3d 384"},
            {"motion_type": "section_1983_judicial_misconduct_complaint",
             "primary_authority": "42 U.S.C. § 1983 (against non-judicial officials acting under color)"},
            {"motion_type": "judicial_council_complaint_28_usc_351",
             "primary_authority": "28 U.S.C. § 351"},
        ],
        "primary_authorities": [
            "FRCP 60(d)(3)",
            "Hazel-Atlas Glass Co. v. Hartford-Empire Co., 322 U.S. 238 (1944)",
            "Herring v. United States, 424 F.3d 384 (3d Cir. 2005)",
            "Chambers v. NASCO, Inc., 501 U.S. 32 (1991)",
            "28 U.S.C. § 351",
        ],
        "note": "Synthetic chain. Aggregates case-wide judicial conduct findings (deduplicated).",
    }
