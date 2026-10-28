"""
posture_engine.py — BLOCK_6_13_POSTURE_LOCK

Deterministically derive case posture for due-process rendering.
NO LLM. Pure dispatch over registry fields + role + facts.

Postures:
  - enforcing_judgment : plaintiff is enforcing a judgment (garnishment, execution)
  - attacking_judgment : party is collaterally attacking an adverse judgment
  - merits             : pre-judgment phase (default)
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
from typing import Iterable

ENFORCING_TRIGGERS = (
    "garnishee", "garnishment", "writ of execution", "sheriff sale", "sheriff's sale",
    "interrogatories in attachment", "default judgment without proper service",  # legacy fact label
    "agreement existed",  # contract enforcement context
)

ATTACKING_TRIGGERS = (
    "vacate judgment", "set aside judgment", "set aside default",
    "frcp 60(b)(4)", "rule 60(b)(4)", "collateral attack",
    "judgment void", "void the judgment",
)

ENFORCING_STATUSES = (
    "judgment_by_default_active_motions_to_strike",
    "judgment_in_plaintiff_favor_active_enforcement",
    "active_garnishment",
)

ATTACKING_STATUSES = (
    "collateral_attack_pending",
    "rule_60b_motion_pending",
    "default_judgment_against_us_active",
)


# BEEZLE_FN_UNUSED 2026-06-13: derive_posture (no static refs in pipeline/)
def derive_posture(case: dict, role: str = "", facts: Iterable[str] = ()) -> str:
    """Return one of: enforcing_judgment | attacking_judgment | merits."""
    status = (case.get("status") or "").lower()
    judgment_status = (case.get("judgment_status") or "").lower()

    if judgment_status in ("plaintiff_favor_enforcing", "enforcing"):
        return "enforcing_judgment"
    if judgment_status in ("attacking_adverse", "collateral_attack"):
        return "attacking_judgment"

    if status in ENFORCING_STATUSES:
        return "enforcing_judgment"
    if status in ATTACKING_STATUSES:
        return "attacking_judgment"

    fact_blob = " ".join(str(f).lower() for f in (facts or []))
    enforcing_hits = sum(1 for t in ENFORCING_TRIGGERS if t in fact_blob)
    attacking_hits = sum(1 for t in ATTACKING_TRIGGERS if t in fact_blob)

    if enforcing_hits and (role or "").lower() == "plaintiff" and enforcing_hits >= attacking_hits:
        return "enforcing_judgment"
    if attacking_hits and attacking_hits > enforcing_hits:
        return "attacking_judgment"

    return "merits"


# ============================================================================
# POSTURE_GATE_V1 (2026-06-17) — per-party motion gate.
#
# m0 selects motions by violation type; it does not know that once you HOLD a
# judgment against a party you are at ENFORCEMENT, where merits motions (strike,
# compel, entry-of-default, default-judgment, reconsideration) against that party
# are moot — or worse, reopen what you already won. This gate suppresses them.
#
# General + data-driven (R2): posture comes from registry `judgment_status` and a
# party class derived from the chain's own role/name tokens — never a hardcoded
# judge / party / case. Works for every case, old and future.
# ============================================================================
import json as _json
from pathlib import Path as _Path

_REGISTRY = next(
    (p / "cases_index" / "case_registry.json"
     for p in (_Path(str(_eroot())), _Path(str(_eroot())))
     if (p / "cases_index" / "case_registry.json").exists()),
    _Path(str(_eroot())) / "cases_index" / "case_registry.json")

# enforcement / order-attack are ALWAYS allowed (they are the work of enforcing or
# of undoing a court's erroneous order); checked before any suppression.
_ENFORCEMENT_TOKENS = ("writ", "execution", "garnish", "interrogator", "sanction",
                       "cost", "contempt", "reissue", "judgment_against_garnishee")
_ORDER_ATTACK_TOKENS = ("vacate", "recus", "disqualif", "fraud_on_court", "void",
                        "clarification_writ", "_hall_order", "mandamus")
# DEFENSIVE / responsive filings — ALWAYS allowed regardless of posture or default.
# A hostile court will grant an opponent's motion if it sits unopposed, so a response
# to anything the opposing side files is never suppressed (even from a defaulted party).
_RESPONSIVE_TOKENS = ("opposition", "response_to", "answer_to_motion", "reply_in_support",
                      "objection_to")
# merits / relitigation motions — moot against a party you already hold judgment over.
_MERITS_TOKENS = ("strike", "compel", "entry_of_default", "default_judgment",
                  "reconsideration", "amended_complaint",
                  "summary_judgment", "preliminary_objection", "counterclaim")
# things that make no sense against a garnishee (it is not a defendant on the merits).
_NA_FOR_GARNISHEE = ("amended_complaint", "answer", "counterclaim", "preliminary_objection")


def _load_case(case_id: str) -> dict:
    try:
        data = _json.loads(_REGISTRY.read_text(encoding="utf-8"))
    except Exception:
        return {}
    cases = data if isinstance(data, list) else data.get("cases", data)
    if isinstance(cases, dict):
        for k, v in cases.items():
            if isinstance(v, dict) and (k == case_id or case_id in k):
                return v
        return {}
    for c in (cases or []):
        if isinstance(c, dict) and (c.get("canonical_id") == case_id
                                    or case_id in _json.dumps(c)):
            return c
    return {}


def party_class(chain: dict) -> str:
    """garnishee | court | defendant — derived from the chain's own tokens (R2)."""
    blob = " ".join(str(chain.get(k, "")) for k in
                    ("defendant", "role", "notes", "slug", "name")).lower()
    if "garnish" in blob:                                   # garnishee / garnishment
        return "garnishee"
    if any(w in blob for w in ("court", "judic", "judge", "conduct", "tribunal")):
        return "court"
    return "defendant"


def motion_allowed(case, chain: dict, motion_type: str) -> tuple[bool, str]:
    """Return (allowed, reason_if_suppressed). `case` may be a case_id str or a case dict."""
    case_obj = _load_case(case) if isinstance(case, str) else (case or {})
    mt = (motion_type or "").lower()
    posture = derive_posture(case_obj)
    klass = party_class(chain)

    # Always allow enforcement, order-attack, and DEFENSIVE responses. A response to an
    # opponent's motion is never suppressed — an unopposed motion gets granted.
    if any(t in mt for t in _ENFORCEMENT_TOKENS):
        return True, ""
    if any(t in mt for t in _ORDER_ATTACK_TOKENS):
        return True, ""
    if any(t in mt for t in _RESPONSIVE_TOKENS):
        return True, ""

    if posture == "enforcing_judgment":
        party = chain.get("defendant") or chain.get("name") or "this party"
        if klass == "defendant" and any(t in mt for t in _MERITS_TOKENS):
            return False, (f"enforcing_judgment vs defendant '{party}': judgment already "
                           f"obtained — merits motion '{motion_type}' is moot at enforcement "
                           f"(would relitigate / risk reopening a win)")
        if klass == "garnishee" and any(t in mt for t in _NA_FOR_GARNISHEE):
            return False, (f"garnishee enforcement vs '{party}': '{motion_type}' is a merits "
                           f"vehicle inapplicable to a garnishee (use compel-verified-answer / "
                           f"sanctions / judgment-against-garnishee instead)")
    return True, ""


def audit_packet(case_id: str, motions_dir) -> list[dict]:
    """For OLD already-generated packets: report which per-chain motions the gate
    would suppress. motions_dir = .../drafts/_bundle11_packet/motions."""
    motions_dir = _Path(motions_dir)
    out = []
    for ch_dir in sorted(motions_dir.glob("chain_*")):
        if ch_dir.name == "chain_99_m0_selected":
            continue
        # reconstruct a minimal chain dict from the folder name
        defendant = ch_dir.name.split("_", 2)[-1].replace("_", " ")
        chain = {"defendant": defendant, "slug": ch_dir.name}
        for f in ch_dir.glob("*_FILING.docx"):
            mt = f.name.replace("_FILING.docx", "")
            mt = mt.split("_", 1)[-1] if mt[:2].isdigit() else mt  # strip NN_ prefix
            ok, why = motion_allowed(case_id, chain, mt)
            if not ok:
                out.append({"chain": ch_dir.name, "file": f.name, "reason": why})
    return out
