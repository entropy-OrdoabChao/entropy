"""
legal_validity_check.py — M0 LEGAL_VALIDITY_CHECK + M1 JURISDICTION_IMPACT_CHECK

Tiered rule lookup and validation. Routes claims to correct procedural
vehicles. Does NOT filter or delete claims.

Tiers:
  1. obsidian_sync/patterns/legal_validity_rules.md  (hot cache)
  2. Beezle MCP: search_chunks, definition_lookup, audit_citation
  3. discover_authority (Beezle + CourtListener)
  4. Web fallback (CourtListener API, Justia, Scholar)
  5. Write-back to Tier 1 for newly-validated rules
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import json
import logging
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("legal_validity_check")

ROOT = _eroot()
TIER1_PATH = ROOT / "obsidian_sync" / "patterns" / "legal_validity_rules.md"


# =============================================================================
# Data shapes
# =============================================================================

@dataclass
class Claim:
    """One claim asserted in a motion section."""
    claim_id: str
    grievance: str              # what the user is complaining about
    asserted_rule: str          # e.g. "FRCP 60(b)(4)"
    intended_use: str           # what the claim says the rule does
    requested_relief: str       # e.g. "vacate"
    court: str                  # e.g. "EDPA"
    facts: List[str] = field(default_factory=list)
    docket_refs: List[str] = field(default_factory=list)


@dataclass
class ValidationResult:
    claim_id: str
    asserted_rule: str
    validity: str               # "PASS" | "FAIL" | "ESCALATED"
    error: Optional[str] = None
    reroute_to_rule: Optional[str] = None
    rewrite_template: Optional[str] = None
    keep_claim: bool = True     # claims are NEVER deleted
    rule_text: Optional[str] = None
    source: Optional[str] = None  # which tier supplied the rule
    notes: List[str] = field(default_factory=list)


# =============================================================================
# Tier 1 cache loader
# =============================================================================

def parse_cached_rules(path: Path = TIER1_PATH) -> Dict[str, Dict[str, Any]]:
    """Parse legal_validity_rules.md into a dict keyed by rule_id."""
    if not path.exists():
        log.warning("Tier 1 cache not found: %s", path)
        return {}
    text = path.read_text(encoding="utf-8", errors="replace")
    rules: Dict[str, Dict[str, Any]] = {}

    # Each rule starts with "## <rule_id> — <title>"
    sections = re.split(r"\n## (?=\S)", "\n" + text)
    for sec in sections[1:]:
        lines = sec.strip().split("\n")
        if not lines:
            continue
        header = lines[0]
        # rule_id is the leading text before " — " or " - "
        m = re.match(r"^([^—\-]+?)(?:\s*[—\-]\s*(.*))?$", header)
        if not m:
            continue
        rule_id = m.group(1).strip()
        title = (m.group(2) or "").strip()
        body = "\n".join(lines[1:])

        # Pull fields like "- **scope**: ..."
        fields_found: Dict[str, Any] = {"_title": title}
        for fld in ("scope", "standard", "relief", "court_authority",
                    "misuses", "reroute_targets", "validated_by",
                    "last_validated", "proper_use", "M1_trigger",
                    "state_law_for_EDPA", "due_process_backstop",
                    "alternative_to", "time"):
            pattern = rf"\*\*{fld}\*\*\s*:\s*(.+?)(?=\n- \*\*|\n##|\Z)"
            mm = re.search(pattern, body, re.S)
            if mm:
                val = mm.group(1).strip()
                fields_found[fld] = val
        if not fields_found.get("scope"):
            continue
        # Must look like a citation: contains digits OR keywords like Rule/USC/FRCP/FRE/FRCrP
        looks_like_rule = (
            re.search(r"\d", rule_id) or
            re.search(r"\b(rule|usc|u\.s\.c|frcp|frcrp|fre|pa\.r\.c\.p|edpa)\b", rule_id, re.I)
        )
        if not looks_like_rule:
            continue
        rules[rule_id] = fields_found
    return rules



_RULE_NORMALIZATION_V2 = True
_CFR_NAMESPACE_V1 = True

def _normalize_rule_id(rule_id: str) -> str:
    """Normalize a citation string to the canonical key used in legal_validity_rules.md cache.
    
    Handles common variants:
      "Rule 54(b)" -> "FRCP 54(b)"
      "Fed. R. Civ. P. 12" -> "FRCP 12"  
      "Fed.R.Civ.P. 12(b)(6)" -> "FRCP 12(b)(6)"
      "FRCP 12" -> "FRCP 12"  (already canonical)
      "28 USC 1927" -> "28 U.S.C. § 1927"
      "28 U.S.C. 1927" -> "28 U.S.C. § 1927"
      "15 USC 1681n" -> "15 U.S.C. § 1681n"
      "L.R. 7.1(g)" -> "EDPA Local Rule 7.1(g)"
      "Local Rule 7.1(g)" -> "EDPA Local Rule 7.1(g)"
    """
    if not rule_id:
        return ""
    
    rid = str(rule_id).strip()
    
    # Strip trailing punctuation
    rid = rid.rstrip(".,;:")
    
    # Federal Rules of Civil Procedure
    # Match anything like: "Rule 54(b)", "Fed. R. Civ. P. 12", "FRCP 12(b)(6)", etc.
    frcp_patterns = [
        (r"^Fed\.?\s*R\.?\s*Civ\.?\s*P\.?\s*(\d+(?:\([a-z0-9]+\))*)", r"FRCP \1"),
        (r"^Federal\s+Rules?\s+of\s+Civil\s+Procedure\s+(\d+(?:\([a-z0-9]+\))*)", r"FRCP \1"),
        (r"^Federal\s+Rule\s+(\d+(?:\([a-z0-9]+\))*)", r"FRCP \1"),
        (r"^F\.?R\.?C\.?P\.?\s*(\d+(?:\([a-z0-9]+\))*)", r"FRCP \1"),
        (r"^Rule\s+(\d+(?:\([a-z0-9]+\))*)", r"FRCP \1"),  # bare "Rule X" -> FRCP X
        (r"^FRCP\s+(\d+(?:\([a-z0-9]+\))*)", r"FRCP \1"),  # already canonical, normalize spacing
    ]
    for pat, repl in frcp_patterns:
        m = re.match(pat, rid, re.IGNORECASE)
        if m:
            return re.sub(pat, repl, rid, count=1, flags=re.IGNORECASE)
    
    # FRE (Federal Rules of Evidence)
    if re.match(r"^Fed\.?\s*R\.?\s*Evid", rid, re.IGNORECASE) or re.match(r"^FRE\s", rid, re.IGNORECASE):
        m = re.search(r"(\d+(?:\([a-z0-9]+\))*)", rid)
        if m: return f"FRE {m.group(1)}"
    
    # USC: "28 USC 1927", "28 U.S.C. 1927", "28 U.S.C. § 1927"

    # CFR: "12 CFR 1022.53" or "12 C.F.R. § 1022.53"
    cfr_match = re.match(
        r"^(\d+)\s*C\.?\s*F\.?\s*R\.?\s*(?:\u00a7|sec\.?|section)?\s*(\d+(?:\.\d+)?(?:\([a-z0-9]+\))*)",
        rid, re.IGNORECASE
    )
    if cfr_match:
        title, section = cfr_match.group(1), cfr_match.group(2)
        return f"{title} C.F.R. \u00a7 {section}"
    
    usc_match = re.match(
        r"^(\d+)\s*U\.?\s*S\.?\s*C\.?\s*(?:\u00a7|sec\.?|section)?\s*(\d+[a-z]?(?:-\d+[a-z]?)?(?:\([a-z0-9]+\))*)",
        rid, re.IGNORECASE
    )
    if usc_match:
        title, section = usc_match.group(1), usc_match.group(2)
        return f"{title} U.S.C. \u00a7 {section}"
    
    # EDPA Local Rules
    lr_match = re.match(
        r"^(?:E\.?D\.?\s*Pa\.?\s+)?(?:L\.?R\.?|Local\s+Rule|Loc\.?\s*R\.?)\s*(\d+\.\d+(?:\([a-z]\))?)",
        rid, re.IGNORECASE
    )
    if lr_match:
        return f"EDPA Local Rule {lr_match.group(1)}"
    
    # Pa.R.C.P.
    parcp_match = re.match(r"^Pa\.?\s*R\.?\s*C\.?\s*P\.?\s*(\d+(?:\([a-z0-9]+\))*)", rid, re.IGNORECASE)
    if parcp_match:
        return f"Pa.R.C.P. {parcp_match.group(1)}"
    
    # No transformation matched; return cleaned version
    return rid


class LegalValidityChecker:
    """
    M0 router: validates each claim against rule-as-written.
    Reroutes failed claims to correct procedural vehicles.
    Never deletes claims.
    """

    def __init__(self, mcp_session=None):
        self.cache = parse_cached_rules()
        self.mcp = mcp_session
        log.info("LegalValidityChecker loaded %d cached rules", len(self.cache))

    def _get_rule(self, rule_id: str) -> Optional[Dict[str, Any]]:
        """Normalized lookup: USC/U.S.C./? punctuation differences are unified."""
        norm = _normalize_rule_id(rule_id)
        # Build normalized index lazily
        if not hasattr(self, "_norm_index"):
            self._norm_index = {_normalize_rule_id(k): k for k in self.cache}
        # 1. Exact normalized match
        if norm in self._norm_index:
            return self.cache[self._norm_index[norm]]
        # 2. Substring match: norm contained in any normalized key (most specific match wins)
        candidates = [(k, orig) for k, orig in self._norm_index.items()
                      if norm in k or k in norm]
        if candidates:
            # Prefer the most specific (longest) cache key
            candidates.sort(key=lambda x: -len(x[0]))
            return self.cache[candidates[0][1]]
        # 3. Tier 2-5 MCP lookup
        if self.mcp:
            rule = lookup_via_mcp(rule_id, self.mcp)
            if rule:
                return rule
        return None

    def validate_claim(self, claim: Claim) -> ValidationResult:
        rule = self._get_rule(claim.asserted_rule)
        # Write-back: if rule came from Tier 2-5 (MCP/web), append to Tier 1 cache.
        if rule is not None and rule.get("_source", "").startswith("tier"):
            try:
                self.writeback_new_rule(claim.asserted_rule, rule)
            except Exception as _e:
                log.debug("writeback skipped: %s", _e)
        if rule is None:
            return ValidationResult(
                claim_id=claim.claim_id,
                asserted_rule=claim.asserted_rule,
                validity="FAIL",
                error=f"Rule '{claim.asserted_rule}' not found in Tier 1-5 lookup",
                keep_claim=True,
                notes=[
                    "Rule not located. Possible: misspelled, very obscure, or "
                    "needs Tier 4 web lookup. Claim preserved for manual review."
                ],
            )

        scope = (rule.get("scope") or "").lower()
        misuses = (rule.get("misuses") or "").lower()
        court_auth = (rule.get("court_authority") or "").lower()
        relief = (rule.get("relief") or "").lower()
        intended = claim.intended_use.lower()
        req_relief = claim.requested_relief.lower()

        notes = [f"Rule found in cache: {claim.asserted_rule}"]
        if rule.get("_source"):
            notes.append(f"Source: {rule['_source']}")

        # Check 1: scope match
        scope_keywords = self._extract_keywords(scope)
        intended_keywords = self._extract_keywords(intended)
        if scope_keywords and not (scope_keywords & intended_keywords):
            return ValidationResult(
                claim_id=claim.claim_id,
                asserted_rule=claim.asserted_rule,
                validity="FAIL",
                error=f"Scope mismatch: rule covers {list(scope_keywords)[:3]}, claim asserts {list(intended_keywords)[:3]}",
                keep_claim=True,
                rule_text=rule.get("scope"),
                source=rule.get("_source", "tier1_cache"),
                notes=notes + [f"Scope mismatch detected — rule does not cover this claim's intended use"],
            )

        # Check 2: misuse pattern match (the strong reject signal)
        if misuses:
            for misuse_line in misuses.split("\n"):
                misuse_line = misuse_line.strip().lstrip("-* ").lower()
                if not misuse_line:
                    continue
                # If intended_use matches a known misuse → FAIL
                if self._matches_pattern(intended, misuse_line):
                    return ValidationResult(
                        claim_id=claim.claim_id,
                        asserted_rule=claim.asserted_rule,
                        validity="FAIL",
                        error=f"Misuse detected: {misuse_line[:200]}",
                        reroute_to_rule=self._extract_reroute(rule),
                        rewrite_template=self._build_rewrite(claim, rule),
                        keep_claim=True,
                        rule_text=rule.get("scope"),
                        source=rule.get("_source", "tier1_cache"),
                        notes=notes + [f"Matched misuse pattern: {misuse_line[:120]}"],
                    )

        # Check 3: court_authority gate
        if court_auth and "appellate" in court_auth.lower() and "appellate" not in claim.court.lower():
            # Rule requires appellate court but claim is in non-appellate court
            if "only" in court_auth.lower():
                return ValidationResult(
                    claim_id=claim.claim_id,
                    asserted_rule=claim.asserted_rule,
                    validity="FAIL",
                    error=f"Court authority mismatch: rule requires appellate court, claim in {claim.court}",
                    reroute_to_rule=self._extract_reroute(rule),
                    rewrite_template=self._build_rewrite(claim, rule),
                    keep_claim=True,
                    source=rule.get("_source", "tier1_cache"),
                    notes=notes + [f"Court authority: {court_auth[:200]}"],
                )

        # Check 4: relief match (looser — just warn if doesn't match)
        if relief and req_relief:
            if not (self._matches_pattern(req_relief, relief) or
                    self._matches_pattern(relief, req_relief)):
                notes.append(f"Relief shape may not match: rule grants {relief[:80]}, claim seeks {req_relief[:80]}")

        return ValidationResult(
            claim_id=claim.claim_id,
            asserted_rule=claim.asserted_rule,
            validity="PASS",
            rule_text=rule.get("scope"),
            source=rule.get("_source", "tier1_cache"),
            notes=notes,
            keep_claim=True,
        )

    def jurisdiction_impact_check(self, claim: Claim) -> Optional[str]:
        """
        M1: data-driven jurisdiction-impact router.
        Loads (phrase, rule) pairs from obsidian_sync/patterns/jurisdiction_impact_triggers.md.
        Anyone may edit that file to add or remove triggers — no code change needed.
        """
        text = (claim.grievance + " " + claim.intended_use + " " +
                " ".join(claim.facts)).lower()
        triggers = self._load_m1_triggers()
        for phrase, rule in triggers:
            if phrase in text:
                return rule
        return None

    _M1_TRIGGER_PATH = ROOT / "obsidian_sync" / "patterns" / "jurisdiction_impact_triggers.md"
    _m1_triggers_cache = None
    _m1_triggers_mtime = 0.0

    def _load_m1_triggers(self):
        """Load M1 triggers from sidecar file. Reloads if file changes on disk."""
        path = self._M1_TRIGGER_PATH
        if not path.exists():
            return []
        try:
            mtime = path.stat().st_mtime
            if (LegalValidityChecker._m1_triggers_cache is not None and
                    mtime == LegalValidityChecker._m1_triggers_mtime):
                return LegalValidityChecker._m1_triggers_cache
            text = path.read_text(encoding="utf-8", errors="replace")
            triggers = []
            for line in text.split("\n"):
                m = re.match(r'^\s*-\s*"([^"]+)"\s*->\s*(.+?)\s*$', line)
                if m:
                    phrase = m.group(1).strip().lower()
                    rule = m.group(2).strip()
                    if phrase and rule:
                        triggers.append((phrase, rule))
            LegalValidityChecker._m1_triggers_cache = triggers
            LegalValidityChecker._m1_triggers_mtime = mtime
            log.info("M1 loaded %d triggers from %s", len(triggers), path.name)
            return triggers
        except Exception as e:
            log.warning("M1 trigger load failed: %s", e)
            return []

    # ---- helpers --------------------------------------------------------

    def _extract_keywords(self, s: str, n: int = 12) -> set:
        words = re.findall(r"\b[a-z]{4,}\b", s.lower())
        stop = {"that", "this", "with", "from", "have", "been", "will",
                "must", "rule", "court", "claim", "case", "when", "where",
                "which", "they", "their", "them", "what", "such", "other",
                "into", "than", "then", "order", "judgment", "relief"}
        return set(w for w in words if w not in stop)

    def _matches_pattern(self, text: str, pattern: str) -> bool:
        """
        Tight matching: pattern is a MISUSE description like
            '"judgment is clearly erroneous" ? erroneous != void'
        Extract the quoted misuse fragment first; require that fragment
        to appear (or near-appear) in text. Without a quoted fragment,
        require strong keyword overlap (>=60%) AND no contradictory keywords.
        """
        text_l = text.lower()
        pat_l = pattern.lower()

        # 1. Extract quoted misuse fragment if present
        quoted = re.findall(r'"([^"]+)"', pattern)
        if quoted:
            # Check ANY quoted fragment appears in text
            for q in quoted:
                q_clean = q.lower().strip()
                if q_clean and (q_clean in text_l or
                                self._token_overlap_ratio(q_clean, text_l) >= 0.7):
                    return True
            return False  # Had quotes but none matched

        # 2. No quoted fragments - need strong overlap
        # Strip the explanation portion (after arrows)
        before_arrow = pat_l.split("\u2192")[0].split("->")[0].strip()
        if not before_arrow:
            before_arrow = pat_l

        # Substring check first
        if before_arrow in text_l or text_l in before_arrow:
            return True

        # Keyword overlap with high threshold
        text_kw = self._extract_keywords(text_l)
        pat_kw = self._extract_keywords(before_arrow)
        if not pat_kw:
            return False
        overlap = len(text_kw & pat_kw)
        # Need at least 60% of pattern keywords matched
        return overlap >= max(2, int(0.6 * len(pat_kw)))

    def _token_overlap_ratio(self, a: str, b: str) -> float:
        """Fraction of a's tokens that appear in b."""
        a_tokens = set(re.findall(r"\b[a-z]{3,}\b", a.lower()))
        b_tokens = set(re.findall(r"\b[a-z]{3,}\b", b.lower()))
        if not a_tokens:
            return 0.0
        return len(a_tokens & b_tokens) / len(a_tokens)

    def _extract_reroute(self, rule: Dict[str, Any]) -> Optional[str]:
        targets = rule.get("reroute_targets") or ""
        # Try to pick the first rule-id-looking token
        m = re.search(r"(FRCP\s+\d+\([a-z]\)(\(\d+\))?|FRCP\s+\d+|"
                       r"L\.R\.\s+[\d\.]+\([a-z]\)|28\s*U\.S\.C\.\s*§?\s*\d+(\([a-z]\))?|"
                       r"appeal)", targets, re.I)
        return m.group(0) if m else (targets[:100] or None)

    def _build_rewrite(self, claim: Claim, rule: Dict[str, Any]) -> str:
        reroute = self._extract_reroute(rule) or "appropriate procedural vehicle"
        return (
            f"Original grievance preserved: {claim.grievance}\n"
            f"Original rule (rejected): {claim.asserted_rule}\n"
            f"Rejection reason: see validity report\n"
            f"Reroute target: {reroute}\n"
            f"Rewrite under {reroute} keeping all factual content. "
            f"Adjust legal theory to match {reroute}'s scope/standard/relief."
        )

    # ---- write-back -----------------------------------------------------

    def writeback_new_rule(self, rule_id: str, fields: Dict[str, Any]) -> bool:
        """Append a new validated rule to Tier 1 cache."""
        try:
            existing = self.cache.get(rule_id)
            if existing:
                log.info("Rule %s already in cache, skipping write-back", rule_id)
                return False
            entry = [f"\n## {rule_id} — {fields.get('_title', '')}"]
            for f in ("scope", "standard", "relief", "court_authority",
                      "misuses", "reroute_targets"):
                if f in fields and fields[f]:
                    entry.append(f"\n- **{f}**: {fields[f]}")
            entry.append(f"\n- **validated_by**: {fields.get('_source', 'mcp')}")
            entry.append(f"\n- **last_validated**: {datetime.now().date().isoformat()}")
            with open(TIER1_PATH, "a", encoding="utf-8") as fh:
                fh.write("\n".join(entry) + "\n")
            self.cache[rule_id] = fields
            return True
        except Exception as e:
            log.error("writeback failed for %s: %s", rule_id, e)
            return False


# =============================================================================
# Convenience: process a list of claims
# =============================================================================

# === Tier 2-5 MCP lookup ===
# Called when Tier 1 cache misses. The mcp_session is a live MCP ClientSession.
# Returns a rule dict matching the cache schema, or None if nothing found.
# On hit, the calling code writes the rule back to the Tier 1 cache.

def lookup_via_mcp(rule_id, mcp_session):
    """Resolve an unknown rule via Beezle MCP. Returns rule dict or None.
    
    Sequence:
      Tier 2: query_legal_db (structured statute/rule data)
      Tier 3: search_chunks (semantic search in legal corpus)
      Tier 4: search_case_law (live CourtListener, only for case citations)
    
    If MCP itself is unreachable or any tier raises, we degrade gracefully.
    Result schema matches Tier 1 cache: {scope, misuses, court_authority, 
    relief, rule_text, _source}.
    """
    if mcp_session is None:
        return None
    
    import asyncio
    
    async def _try_tier(tool_name, args):
        try:
            result = await mcp_session.call_tool(tool_name, args)
            if hasattr(result, "content"):
                for c in result.content:
                    if hasattr(c, "text") and c.text:
                        return c.text
            return None
        except Exception as _e:
            log.debug("MCP tool %s failed: %s", tool_name, _e)
            return None
    
    async def _resolve():
        # --- Tier 2: query_legal_db ---
        # Best for: FRCP rules, USC statutes, regulations, procedural rules
        text = await _try_tier("query_legal_db", {"query": rule_id})
        if text and len(text) > 50:
            return {
                "scope": "",
                "misuses": "",
                "court_authority": "",
                "relief": "",
                "rule_text": text[:2000],
                "_source": "tier2_query_legal_db",
            }
        
        # --- Tier 3: search_chunks (semantic) ---
        text = await _try_tier("search_chunks", {
            "query": f"{rule_id} text rule statute",
            "n_results": 3,
        })
        if text and len(text) > 50:
            return {
                "scope": "",
                "misuses": "",
                "court_authority": "",
                "relief": "",
                "rule_text": text[:2000],
                "_source": "tier3_chroma",
            }
        
        # --- Tier 4: search_case_law (only if rule looks like a case citation) ---
        # heuristic: case citations contain "v." or "F.3d" or "U.S."
        if re.search(r"\bv\.|F\.\s*\dd|U\.S\.|F\.\s*App'x", rule_id):
            text = await _try_tier("search_case_law", {"query": rule_id})
            if text and len(text) > 50:
                return {
                    "scope": "",
                    "misuses": "",
                    "court_authority": "",
                    "relief": "",
                    "rule_text": text[:2000],
                    "_source": "tier4_courtlistener",
                }
        
        return None
    
    # Run the async resolution. Caller is in async context (validate_claims 
    # is sync but called from async via _validate_with_mcp). We need to 
    # invoke without spawning a new loop.
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # We are inside a running loop -> use asyncio task on existing loop.
            # This requires us to actually run the coroutine. Since sync code 
            # can't await from within a running loop, we use threading.
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                future = ex.submit(asyncio.run, _resolve())
                return future.result(timeout=30)
        else:
            return loop.run_until_complete(_resolve())
    except Exception as _e:
        log.debug("lookup_via_mcp resolution failed: %s", _e)
        return None


def validate_claims(claims: List[Claim], mcp_session=None) -> List[ValidationResult]:
    checker = LegalValidityChecker(mcp_session=mcp_session)
    results = []
    for claim in claims:
        # First M1 escalation check
        escalated = checker.jurisdiction_impact_check(claim)
        if escalated and claim.asserted_rule != escalated:
            # Re-create claim with escalated rule, mark as escalated
            results.append(ValidationResult(
                claim_id=claim.claim_id,
                asserted_rule=claim.asserted_rule,
                validity="ESCALATED",
                reroute_to_rule=escalated,
                rewrite_template=(
                    f"M1 escalation: claim involves {escalated[:80]}; "
                    f"reroute and re-validate under {escalated}."
                ),
                keep_claim=True,
                notes=[f"M1 detected jurisdictional impact → {escalated}"],
            ))
            # Also run validity against the escalated rule
            esc_claim = Claim(
                claim_id=claim.claim_id + "_escalated",
                grievance=claim.grievance,
                asserted_rule=escalated,
                intended_use=claim.intended_use,
                requested_relief=claim.requested_relief,
                court=claim.court,
                facts=claim.facts,
                docket_refs=claim.docket_refs,
            )
            results.append(checker.validate_claim(esc_claim))
        else:
            results.append(checker.validate_claim(claim))
    return results


if __name__ == "__main__":
    # Smoke test with the the-judge claims
    logging.basicConfig(level=logging.INFO)
    test_claims = [
        Claim("G1_capital_one", "court dismissed ignoring payment evidence",
              "FRCP 60(b)(4)", "order is void because court ignored evidence",
              "vacate", "EDPA"),
        Claim("G3_experian_service", "court said no service but Dkt 54 shows it",
              "FRCP 60(b)(4)", "void for lack of personal jurisdiction",
              "vacate", "EDPA",
              facts=["Dkt 54 contains proof of service"]),
        Claim("G_strike", "defendants cited non-binding authority",
              "FRCP 12(d)", "strike non-binding authority", "strike", "EDPA"),
        Claim("G_mandamus", "judge denying review of void orders",
              "28 USC 1651", "mandamus to compel certification", "mandamus", "EDPA"),
    ]
    results = validate_claims(test_claims)
    print(f"\n=== {len(results)} validation results ===\n")
    for r in results:
        print(f"[{r.validity}] {r.claim_id}  ({r.asserted_rule})")
        if r.error:
            print(f"  error: {r.error}")
        if r.reroute_to_rule:
            print(f"  reroute → {r.reroute_to_rule}")
        for n in r.notes[:3]:
            print(f"  note: {n}")
        print()
