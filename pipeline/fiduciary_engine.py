"""
fiduciary_engine.py — marks when an actor is a fiduciary and which fiduciary
duty a detected violation breaches.

The litigant's adversaries are all fiduciaries to the tribunal/parties:
  - judge   -> personal impartiality + due-process duties      (duty_maps/judge.yaml)
  - court   -> institutional even-handed-administration duties (duty_maps/court.yaml)
  - opposing counsel -> officer-of-the-court / candor duties    (duty_maps/opposing_counsel.yaml)

DB + obsidian backed: every duty's authority is a citation key verified present
in the corpus (:8100) and resolved to full text by pipeline.retriever; the LLM
never invents the law (R7). Architecture: obsidian_sync/system/fiduciary_engine.md.

Public surface:
  is_fiduciary(role) -> bool
  fiduciary_profile(role) -> dict | None     (entity types, basis, all duties)
  match_breaches(role, *signals) -> list     (duties the signals breach)
"""
from __future__ import annotations
import re
from functools import lru_cache

try:
    from pipeline.duty_engine import load_duty_map
except Exception:  # pragma: no cover - importable standalone
    load_duty_map = None

# attribution roles -> fiduciary entity duty maps
ROLE_TO_ENTITIES = {
    "judicial_output": ("judge", "court"),
    "judge": ("judge", "court"),
    "court": ("court", "judge"),
    "opposing_party_filing": ("opposing_counsel",),
    "opposing_counsel": ("opposing_counsel",),
}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")


def is_fiduciary(role: str) -> bool:
    return role in ROLE_TO_ENTITIES


@lru_cache(maxsize=8)
def _duties(entity_type: str) -> tuple:
    if load_duty_map is None:
        return tuple()
    try:
        dm = load_duty_map(entity_type) or {}
    except Exception:
        return tuple()
    out = []
    for d in dm.get("duties", []):
        out.append({
            "entity": entity_type,
            "id": d.get("id"),
            "duty": d.get("name"),
            "class": d.get("class"),
            "source": d.get("source_title") or d.get("source"),
            "source_key": d.get("source"),
            "description": (d.get("description") or "").strip(),
            "triggers": tuple(_norm(t) for t in d.get("triggering_actions", [])),
            "remedy": tuple(d.get("violation_remedy", [])),
        })
    return tuple(out)


def fiduciary_profile(role: str) -> dict | None:
    """Full fiduciary profile for an actor role, or None if not a fiduciary."""
    ents = ROLE_TO_ENTITIES.get(role)
    if not ents:
        return None
    duties = []
    for et in ents:
        duties.extend(_duties(et))
    return {"is_fiduciary": True, "entities": list(ents), "duties": duties}


def match_breaches(role: str, *signals: str) -> list:
    """Return the fiduciary duties that the given violation signals breach.
    Signals are violation types/categories (any case/format). Best-effort:
    matches a duty when a normalized signal hits one of its triggering_actions."""
    ents = ROLE_TO_ENTITIES.get(role)
    if not ents:
        return []
    sigs = [_norm(s) for s in signals if s]
    if not sigs:
        return []
    seen, matched = set(), []
    for et in ents:
        for d in _duties(et):
            hit = any(
                s == t or (len(s) > 4 and len(t) > 4 and (s in t or t in s))
                for s in sigs for t in d["triggers"]
            )
            if hit and d["id"] not in seen:
                seen.add(d["id"])
                matched.append({
                    "duty": d["duty"], "source": d["source"],
                    "remedy": list(d["remedy"]), "entity": d["entity"],
                })
    return matched


if __name__ == "__main__":
    import json
    for role in ("judicial_output", "opposing_counsel", "pro_se_plaintiff_filing"):
        prof = fiduciary_profile(role)
        print(f"\n{role}: fiduciary={is_fiduciary(role)} duties={len(prof['duties']) if prof else 0}")
    print("\nbreach match (judge, bias_indicator):")
    print(json.dumps(match_breaches("judicial_output", "bias_indicator"), indent=1))
    print("\nbreach match (opposing, Non-Binding Authority Presented as Controlling):")
    print(json.dumps(match_breaches("opposing_counsel", "Non-Binding Authority Presented as Controlling"), indent=1))
