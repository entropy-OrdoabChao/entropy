"""
pipeline/statutes_registry.py — Static registry of common statutes and rules.

DETERMINISTIC. NO LLM. NO INTERPRETATION.

Returns 1-line summaries for the most common statutes and procedural rules
that appear in Beezle's 4 active cases (Lippi state CCP, Equifax FCRA,
Amerisave Quiet Title, State Farm Bad Faith).

Used by:
    generation/scorecard_exhibit.py — Section 7 (Statutes & Rules Implicated)
    pipeline/authority_attack_agent.py — citation labeling

Phase 4 will replace this static dict with a dynamic Obsidian-backed lookup
that pulls from the structured rule extractor (Phase 4C).

The summaries are NOT legal interpretations — they are textbook-style
captions taken from the rule's own title or the statute's caption line.
"""

# Each entry: (citation_form, one_line_summary)
# Lookup is case-insensitive and tolerates minor formatting variations.

STATUTES_REGISTRY = {
    # ── Pa.R.C.P. (Pennsylvania Rules of Civil Procedure) ────────────
    "pa.r.c.p. 1024": "Verification — pleadings containing averments of fact must be verified at filing",
    "pa.r.c.p. 1028": "Preliminary objections — grounds and procedure",
    "pa.r.c.p. 1029": "Denials — form and effect; failure to deny constitutes admission",
    "pa.r.c.p. 1030": "Affirmative defenses — must be pleaded in new matter",
    "pa.r.c.p. 1037": "Default judgment — entry upon failure to plead",
    "pa.r.c.p. 208.2": "Motion practice — required form and contents",
    "pa.r.c.p. 208.3": "Motion practice — response time, briefing, and argument",
    "pa.r.c.p. 3145": "Garnishment — service of writ on garnishee",
    "pa.r.c.p. 3146": "Garnishment — answers and judgment against garnishee",
    "pa.r.c.p. 3148": "Garnishment — discovery against the garnishee",
    "pa.r.c.p. 1925": "Statement of errors — required after notice of appeal",
    "pa.r.c.p. 4014": "Discovery — requests for admission",
    "pa.r.c.p. 4019": "Discovery — sanctions for failure to comply",

    # ── Pa.C.S. (Pennsylvania Consolidated Statutes) ──────────────────
    "42 pa.c.s. 8371": "Bad faith insurance practices — interest, punitive damages, and fees recoverable",
    "42 pa.c.s. 5505": "Modification or rescission of orders — court retains authority for 30 days",
    "42 pa.c.s. 5524": "Two-year statute of limitations — personal injury, conversion, fraud, and related actions",
    "42 pa.c.s. 5525": "Four-year statute of limitations — contract and related actions",
    "42 pa.c.s. 8542": "Local agency liability — exceptions to governmental immunity",
    "42 pa.c.s. 8553": "Damage limitations — actions against local agencies",
    "75 pa.c.s. 1701": "Motor vehicle financial responsibility — definitions",
    "75 pa.c.s. 1797": "Motor vehicle insurance — peer review of medical treatment",

    # ── Federal consumer protection statutes ─────────────────────────
    "15 u.s.c. 1681": "Fair Credit Reporting Act (FCRA) — accuracy, dispute, and reinvestigation duties",
    "15 u.s.c. 1681e": "FCRA — reasonable procedures to assure maximum possible accuracy",
    "15 u.s.c. 1681i": "FCRA — reinvestigation duties upon consumer dispute",
    "15 u.s.c. 1681n": "FCRA — civil liability for willful noncompliance",
    "15 u.s.c. 1681o": "FCRA — civil liability for negligent noncompliance",
    "15 u.s.c. 1681s-2": "FCRA — duties of furnishers of information",
    "15 u.s.c. 1692": "Fair Debt Collection Practices Act (FDCPA) — purpose and definitions",
    "15 u.s.c. 1692e": "FDCPA — false, deceptive, or misleading representations prohibited",
    "15 u.s.c. 1692f": "FDCPA — unfair practices prohibited",
    "15 u.s.c. 1692g": "FDCPA — validation of debts and 30-day dispute rights",
    "15 u.s.c. 1692k": "FDCPA — civil liability and damages",
    "15 u.s.c. 1601": "Truth in Lending Act (TILA) — disclosure requirements",
    "15 u.s.c. 1640": "TILA — civil liability for disclosure violations",
    "12 u.s.c. 2601": "Real Estate Settlement Procedures Act (RESPA) — purpose",
    "12 u.s.c. 2605": "RESPA — servicer of loans, qualified written requests, and response duties",
    "12 u.s.c. 2607": "RESPA — prohibition against kickbacks and unearned fees",
    "12 u.s.c. 2609": "RESPA — escrow account limitations",

    # ── Federal Rules of Civil Procedure ──────────────────────────────
    "fed. r. civ. p. 8": "Pleading — general rules; short and plain statement; alternative claims",
    "fed. r. civ. p. 9": "Pleading special matters — fraud and mistake must be pleaded with particularity",
    "fed. r. civ. p. 9(b)": "Pleading special matters — fraud and mistake must be pleaded with particularity",
    "fed. r. civ. p. 12": "Defenses and objections — when and how presented",
    "fed. r. civ. p. 12(b)(6)": "Motion to dismiss for failure to state a claim upon which relief can be granted",
    "fed. r. civ. p. 12(c)": "Motion for judgment on the pleadings",
    "fed. r. civ. p. 15": "Amended and supplemental pleadings",
    "fed. r. civ. p. 56": "Summary judgment — standard and procedure",
    "fed. r. civ. p. 60(b)": "Relief from judgment — grounds including void judgment, fraud, and mistake",
    "fed. r. civ. p. 4": "Service of process — required form and timing",

    # ── Federal jurisdiction and procedure ────────────────────────────
    "28 u.s.c. 1331": "Federal question jurisdiction",
    "28 u.s.c. 1332": "Diversity jurisdiction — citizenship and amount in controversy",
    "28 u.s.c. 1441": "Removal of civil actions to federal court",
    "28 u.s.c. 1446": "Procedure for removal — timing and notice requirements",
    "28 u.s.c. 1447": "Procedure after removal — remand grounds",
    "28 u.s.c. 1651": "All Writs Act — federal courts may issue writs necessary in aid of jurisdiction",
    "28 u.s.c. 2201": "Declaratory Judgment Act",

    # ── Constitutional ────────────────────────────────────────────────
    "u.s. const. amend. v": "Fifth Amendment — due process of law",
    "u.s. const. amend. xiv": "Fourteenth Amendment — due process and equal protection",
    "pa. const. art. i 9": "Pennsylvania Constitution Art. I § 9 — rights of accused / due process",
    "pa. const. art. i 11": "Pennsylvania Constitution Art. I § 11 — open courts; remedies for injuries",

    # ── Twombly / Iqbal pleading standard (case law markers) ──────────
    "twombly": "Bell Atl. Corp. v. Twombly — federal pleading standard requires plausibility",
    "iqbal": "Ashcroft v. Iqbal — Twombly plausibility standard applies to all civil cases",
}


def _normalize_citation(cite: str) -> str:
    """Lowercase and strip extra whitespace for lookup."""
    if not cite:
        return ""
    s = str(cite).lower().strip()
    # Collapse whitespace
    s = " ".join(s.split())
    # Remove trailing periods
    s = s.rstrip(".").strip()
    return s


def lookup(citation: str) -> str:
    """Look up a 1-line summary for a citation.

    Returns the summary if found, or empty string if not in the registry.
    Tolerates minor formatting variations (case, spacing, trailing periods).
    """
    if not citation:
        return ""
    norm = _normalize_citation(citation)
    if norm in STATUTES_REGISTRY:
        return STATUTES_REGISTRY[norm]

    # Try without subsection
    base = norm.split("(")[0].strip()
    if base in STATUTES_REGISTRY:
        return STATUTES_REGISTRY[base]

    # Try matching the prefix (e.g., "15 u.s.c. 1681" matches "15 u.s.c. 1681 et seq")
    for key in STATUTES_REGISTRY:
        if norm.startswith(key) or key.startswith(norm):
            return STATUTES_REGISTRY[key]

    return ""


# BEEZLE_FN_UNUSED 2026-06-13: collect_statutes_from_violations (no static refs in pipeline/)
def collect_statutes_from_violations(violations: list) -> list:
    """Pull statute/rule citations from a violations list.

    Looks at the 'rule_violated' field of each violation and collects
    unique citations with their summaries.

    Returns:
        [{"citation": "Pa.R.C.P. 1024", "summary": "Verification — ..."}, ...]
        Sorted with rules first, then statutes, then federal rules.
    """
    seen = {}
    for v in violations or []:
        if not isinstance(v, dict):
            continue
        rule = v.get("rule_violated", "") or v.get("rule_authority", "") or ""
        if not rule:
            continue
        # Normalize the rule text: lowercase, strip section symbols
        rule_lower = rule.lower().replace("§", "").replace("  ", " ")
        matched = False
        # Try EVERY registry key as a substring of the rule text
        # Sort by length descending so longer (more specific) keys win
        for key in sorted(STATUTES_REGISTRY.keys(), key=len, reverse=True):
            # Normalize the key the same way
            key_norm = key.replace("§", "").replace("  ", " ")
            if key_norm in rule_lower:
                if key not in seen:
                    seen[key] = STATUTES_REGISTRY[key]
                matched = True
                break  # take the longest match only
        if not matched:
            # Add as-is if it looks like a citation but isn't in the registry
            if any(marker in rule_lower for marker in ("u.s.c", "pa.c.s", "pa.r.c.p", "fed. r.", "rule", "§")):
                short = rule[:80]
                if short not in seen:
                    seen[short] = "(rule cited; summary not in registry)"
    # Sort: rules first, then federal, then state
    def sort_key(item):
        k = item[0]
        if "pa.r.c.p" in k: return (0, k)
        if "fed. r." in k: return (1, k)
        if "pa.c.s" in k: return (2, k)
        if "u.s.c" in k: return (3, k)
        if "const" in k: return (4, k)
        return (5, k)
    return [
        {"citation": _proper_case(c), "summary": s}
        for c, s in sorted(seen.items(), key=sort_key)
    ]


def _proper_case(citation: str) -> str:
    """Convert lowercase citation back to proper case for display."""
    if not citation:
        return citation
    # Common abbreviation rewrites
    replacements = [
        ("pa.r.c.p.", "Pa.R.C.P."),
        ("pa.c.s.", "Pa.C.S."),
        ("u.s.c.", "U.S.C."),
        ("fed. r. civ. p.", "Fed. R. Civ. P."),
        ("u.s. const.", "U.S. Const."),
        ("pa. const.", "Pa. Const."),
        ("amend.", "Amend."),
        ("art.", "Art."),
    ]
    out = citation
    for low, high in replacements:
        if low in out.lower():
            # Case-insensitive replace
            idx = out.lower().find(low)
            out = out[:idx] + high + out[idx + len(low):]
    return out.strip()
