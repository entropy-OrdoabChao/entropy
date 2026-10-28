"""filing_sequencer — deterministic ENGINE that orders a case's generated motions
for filing and enforces the ANTI-MOOT rule.

Division of labor (R7: LLM=parsing, deterministic=logic):
  - m0 (pipeline/m0_appeal_gate) decides the appeal VEHICLE (LLM reasoning).
  - this engine decides the filing ORDER (pure deterministic logic) and writes the
    per-case FILING_INSTRUCTIONS.md.

ANTI-MOOT RULE (the thing this engine guarantees, for every case):
  A certify / interlocutory-appeal motion (28 U.S.C. § 1292(b) federal, 42 Pa.C.S.
  § 702(b) PA, Rule 54(b) partial-final) is the FORCING vehicle and is filed LAST in
  its court tier — after the merits motions are on file and pending. It asks the court
  to rule on every pending motion FIRST, then certify, so it PRESERVES the merits
  motions and cannot be used to certify-and-moot the case. A plain Notice of Appeal
  as of right is preferred over any certify motion the moment a final judgment exists.

Data-driven only (R2): classification keys off motion-type tokens in the stem, never
off a specific judge / party / case.
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
from pathlib import Path

ROOT = _eroot()
try:  # WSL
    if not ROOT.exists():
        ROOT = _eroot()
except Exception:
    ROOT = _eroot()

ANTI_MOOT_NOTE = (
    "Do NOT file the certify / interlocutory-appeal motion until the merits motions "
    "are on file and pending. It asks the court to rule on every pending motion FIRST, "
    "then certify — it preserves them, it does not replace them. Prefer a plain Notice "
    "of Appeal as of right over any certify motion once a final judgment exists."
)

# (category, rank-within-tier, human label). Lower rank files earlier.
# finality_certify is rank 90 so it always lands AFTER merits (10/15) in its tier.
_CATS = {
    "declined":         (-1, "DO NOT FILE — m0 declined this vehicle"),
    "recusal":          (0,  "Recusal / disqualification — decided before merits"),
    "ifp":              (0,  "In forma pauperis (Pa.R.C.P. 240) — file before paying"),
    "omnibus":          (10, "Consolidated trial motion (one fee, replaces many)"),
    "merits":           (10, "Merits motion"),
    "void_attack":      (10, "Void-judgment / fraud-on-court attack (any time)"),
    "costs":            (12, "Costs / sanctions / fee recovery"),
    "responsive":       (15, "Responsive — time to the opposing motion"),
    "supplemental":     (16, "Supplemental authority — as it arises"),
    "finality_certify": (90, "CERTIFY / interlocutory — FILE LAST (anti-moot)"),
    "notice_of_appeal": (95, "Notice of appeal — only after a final judgment"),
    # appellate tier
    "reinstate":        (10, "Reinstate appeal / recall mandate"),
    "appellate_brief":  (20, "Appellate brief"),
    "appellate_reply":  (25, "Appellate reply brief"),
    "rehearing":        (40, "Panel rehearing / rehearing en banc — if denied"),
    "mandamus":         (60, "Petition for writ of mandamus — if court refuses to act"),
    "scotus_cert":      (80, "Petition for certiorari — last resort"),
    # parallel
    "accountability":   (10, "Accountability complaint — parallel track, no court fee"),
}


def classify(stem: str) -> str:
    s = stem.lower()
    if "declined" in s:
        return "declined"
    if "recus" in s or "disqualif" in s:
        return "recusal"
    if "ifp" in s or "in_forma_pauperis" in s or "proceed_ifp" in s:
        return "ifp"
    if any(k in s for k in ("judicial_council", "section_1983", "_1983", "jcb_complaint",
                            "odc_complaint", "misconduct_complaint")):
        return "accountability"
    if "scotus" in s or "certiorari" in s:
        return "scotus_cert"
    if "mandamus" in s or "frap_21" in s:
        return "mandamus"
    if "rehearing" in s or "en_banc" in s or "frap_40" in s:
        return "rehearing"
    if "reinstate" in s or "recall_mandate" in s:
        return "reinstate"
    if "appellate_reply" in s or "reply_brief" in s:
        return "appellate_reply"
    if "appellate_brief" in s:
        return "appellate_brief"
    if any(k in s for k in ("1292b", "702b", "rule_54b", "certify", "interlocutory")):
        return "finality_certify"
    if "notice_of_appeal" in s:
        return "notice_of_appeal"
    if "opposition" in s:
        return "responsive"
    if "supplemental_authority" in s:
        return "supplemental"
    if "omnibus" in s:
        return "omnibus"
    if "costs" in s or "sanction" in s:
        return "costs"
    if any(k in s for k in ("60b4", "60d3", "fraud_on_court", "void")):
        return "void_attack"
    return "merits"


def court_of(stem: str, case_id: str) -> str:
    s = stem.lower()
    federal = case_id.startswith("fed")
    cat = classify(stem)
    if cat == "accountability":
        return "ACCOUNTABILITY"
    if not federal:  # PA
        if "superior_court" in s or cat in ("appellate_brief", "appellate_reply"):
            return "APPELLATE_PA_SUPERIOR"
        return "PA_CCP"                       # incl. 702(b) certify (filed in trial court)
    # federal
    if cat in ("reinstate", "appellate_brief", "appellate_reply", "rehearing",
               "mandamus", "scotus_cert") or "3rd_cir" in s or "3dcir" in s:
        return "APPELLATE_3DCIR"
    return "DISTRICT_EDPA"                     # incl. 1292(b)/54(b) certify, NOA


_TIER_ORDER = ["PA_CCP", "DISTRICT_EDPA", "APPELLATE_PA_SUPERIOR", "APPELLATE_3DCIR",
               "ACCOUNTABILITY"]
_TIER_LABEL = {
    "PA_CCP": "PA Court of Common Pleas (trial court)",
    "DISTRICT_EDPA": "U.S. District Court, E.D. Pa.",
    "APPELLATE_PA_SUPERIOR": "Superior Court of Pennsylvania (appeal)",
    "APPELLATE_3DCIR": "U.S. Court of Appeals for the Third Circuit",
    "ACCOUNTABILITY": "Accountability boards (parallel — no court fee)",
}


def sequence(case_id: str, stems: list[str]) -> dict:
    """Return {tier: [ {stem, category, rank, label}, ... ordered ]}."""
    by_tier: dict[str, list] = {}
    for stem in stems:
        cat = classify(stem)
        rank, label = _CATS[cat]
        tier = court_of(stem, case_id)
        # A notice of appeal IN an appellate tier is the entry vehicle (file first,
        # then the brief); in the district tier it is the reserved NOA (after finality).
        if cat == "notice_of_appeal" and tier.startswith("APPELLATE"):
            rank, label = 5, "Notice of appeal — entry vehicle (as of right), file first"
        by_tier.setdefault(tier, []).append(
            {"stem": stem, "category": cat, "rank": rank, "label": label})
    for tier in by_tier:
        # rank, then natural priority prefix (00_, 01_, ...), then name
        by_tier[tier].sort(key=lambda m: (m["rank"], m["stem"]))
    return {t: by_tier[t] for t in _TIER_ORDER if t in by_tier}


def _m0_vehicle(case_id: str) -> dict:
    try:
        from pipeline.m0_appeal_gate import decide_appeal_vehicle
        d = decide_appeal_vehicle(case_id)
        return {k: d.get(k) for k in ("primary_vehicle", "higher_court", "void_judgment",
                                      "hedge_vehicle", "controlling_questions", "reason")}
    except Exception:
        return {}


def render_instructions(case_id: str, stems: list[str], m0: dict | None = None) -> str:
    plan = sequence(case_id, stems)
    if m0 is None:
        m0 = _m0_vehicle(case_id)
    L = [f"# FILING INSTRUCTIONS — {case_id}", "",
         f"> **ANTI-MOOT RULE.** {ANTI_MOOT_NOTE}", ""]
    if m0.get("primary_vehicle"):
        L += ["## m0 appeal decision",
              f"- **Primary vehicle:** {m0.get('primary_vehicle')} → {m0.get('higher_court','')}",
              f"- **Void judgment:** {m0.get('void_judgment')}  ·  **Hedge:** {m0.get('hedge_vehicle')}"]
        cq = m0.get("controlling_questions") or []
        if cq:
            L.append("- **Controlling questions:** " + "; ".join(cq))
        if m0.get("reason"):
            L.append(f"- _m0: {m0['reason']}_")
        L.append("")
    n = 0
    for tier, items in plan.items():
        L.append(f"## {_TIER_LABEL[tier]}")
        declined = [m for m in items if m["category"] == "declined"]
        active = [m for m in items if m["category"] != "declined"]
        for m in active:
            n += 1
            flag = ""
            if m["category"] == "finality_certify":
                flag = "  ⟵ **FILE LAST (anti-moot)**"
            elif m["category"] in ("recusal", "ifp"):
                flag = "  ⟵ **file first in this tier**"
            L.append(f"{n}. `{m['stem']}` — {m['label']}{flag}")
        for m in declined:
            L.append(f"- ~~`{m['stem']}`~~ — {m['label']}")
        L.append("")
    L += ["## Attach", "- Scorecard exhibit to the merits motions.",
          "- One consolidated Notice of Change of Address covers every docket.", ""]
    return "\n".join(L)


def write_instructions(case_id: str, out_dir: Path, stems: list[str] | None = None,
                       m0: dict | None = None) -> Path:
    """Write FILING_INSTRUCTIONS.md into a case packet dir. If stems is None, scan
    out_dir for *.md / *_FILING.docx motion files."""
    out_dir = Path(out_dir)
    if stems is None:
        import re as _re
        seen = set()
        for p in out_dir.rglob("*"):
            if any(x in str(p) for x in ("_stale", "_archive", "legacy")):
                continue
            if p.suffix == ".md" and p.name != "FILING_INSTRUCTIONS.md" \
                    and not p.name.startswith("_"):
                stem = p.stem
            elif p.name.endswith("_FILING.docx"):
                stem = p.name.replace("_FILING.docx", "")
            else:
                continue
            # collapse generation variants: <stem>, <stem>_DRAFT, <stem>_FILING,
            # <stem>_20260613_1612 -> one logical motion
            stem = _re.sub(r"_(DRAFT|FILING)$", "", stem)
            stem = _re.sub(r"_\d{8}(_\d{6})?$", "", stem)
            seen.add(stem)
        stems = sorted(seen)
    text = render_instructions(case_id, stems, m0=m0)
    dest = out_dir / "FILING_INSTRUCTIONS.md"
    dest.write_text(text, encoding="utf-8")
    return dest


if __name__ == "__main__":
    import sys
    cid = sys.argv[1] if len(sys.argv) > 1 else "fed:2:26-cv-00123"
    stems = sys.argv[2:] or [
        "motion_for_recusal", "01_motion_for_reconsideration", "05_rule_60b4_motion",
        "07_rule_60d3_fraud_on_court", "motion_to_certify_entire_action_1292b",
        "09_notice_of_appeal_reserved", "motion_to_reinstate_appeal",
        "appellate_brief_3rd_cir", "frap_40_panel_rehearing",
        "00_judicial_council_complaint_28_usc_351", "00_amended_complaint_DECLINED_by_m0"]
    print(render_instructions(cid, stems, m0={}))
