# -*- coding: utf-8 -*-
"""escalation_ladders — grounded per-forum escalation LEVELS + day-clocks + cites (single source of truth).

Data = accountability_registry/escalation_ladders.yaml (18 forums, 60 levels; every statutory deadline
FTS-confirmed in the corpus, 3 operational SLAs web-cited). Consumed by:
  - accountability_tracker.sweep()   — auto-escalate a matter when a rung's day-clock runs out
  - motion_drafter_v2 / accountability_drafter — cite the correct forum + deadline + on_expiry when drafting
  - beezle_mcp_server.escalation_ladder — Hermes queries the next step deterministically (context-saver)

DETERMINISTIC: pure data + date math, no LLM. R2: no case/party data — forum/actor keys only.
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import datetime, functools
from pathlib import Path

_ROOT = next((p for p in (_eroot(), _eroot()) if (p / "pipeline").exists()),
             _eroot())
_YAML = _ROOT / "pipeline" / "accountability_registry" / "escalation_ladders.yaml"

_RESPONSE_EVENTS = ("acknowledged", "investigating", "action", "return:")  # a real response stops the clock


@functools.lru_cache(maxsize=1)
def _load() -> dict:
    import yaml
    return (yaml.safe_load(_YAML.read_text(encoding="utf-8")) or {}).get("ladders", {})


def ladders() -> dict:
    return _load()


def ladder_for(key: str) -> dict | None:
    return _load().get(key)


# ---- map an accountability-ledger actor / free-text forum name to a ladder key ----
_ACTOR_TO_FORUM = {
    "insurance_regulator": "insurance_pa", "opposing_counsel": "opposing_counsel",
    "tax_administration": "tax", "tax": "tax", "agency": "apa_review",
}
_FORUM_HINTS = [
    ("foia", ("foia", "freedom of information", "records request")),
    ("mortgage_servicing", ("servicer", "respa", "qwr", "notice of error", "mortgage servic")),
    ("fcra_dispute", ("fcra", "credit report", "reinvestigat", "1681")),
    ("fair_housing_hud", ("fair housing", "fheo", "discriminatory housing")),
    ("securities_sec", ("securities and exchange", "sec ", "securitiz", "whistleblow")),
    ("irs_internal", ("taxpayer advocate", "office of appeals", "collection due process", "cdp")),
    ("banking_regulators", ("comptroller", "occ", "fdic", "federal reserve", "ncua")),
    ("debt_collection", ("debt collect", "fdcpa", "1692")),
    ("insurance_pa", ("insurance", "1171", "bad faith", "commissioner")),
    ("judge_federal", ("third circuit", "judicial council", "judicial conduct and disability")),
    ("judge_state_pa", ("judicial conduct board", "court of judicial discipline", "jcb")),
    ("bar_oversight", ("disciplinary board", "disciplinary counsel", "odc")),
    ("opposing_counsel", ("opposing counsel", "rule 11", "sanction")),
    ("ftca", ("federal tort", "ftca", "2675", "sf-95")),
    ("apa_review", ("agency action", "administrative procedure", "706")),
    ("state_ag_consumer", ("attorney general", "consumer protection", "utpcpl")),
    ("oversight_of_oversight", ("king's bench", "cover", "refuses to act", "oversight")),
    ("tax", ("tigta", "1099-c", "cancellation of debt", "irs")),
]


def forum_key(actor_or_text: str) -> str | None:
    t = (actor_or_text or "").lower()
    if t in _ACTOR_TO_FORUM:
        return _ACTOR_TO_FORUM[t]
    for k, kws in _FORUM_HINTS:
        if any(w in t for w in kws):
            return k
    return None


def _working_days(a: datetime.date, b: datetime.date) -> int:
    if b <= a:
        return 0
    d, n = a, 0
    while d < b:
        d += datetime.timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def _level_of(matter: dict) -> dict | None:
    fk = matter.get("forum_key") or forum_key(matter.get("actor", ""))
    lad = _load().get(fk) if fk else None
    if not lad:
        return None
    i = matter.get("rung", 0)
    levels = lad.get("levels", [])
    return levels[i] if 0 <= i < len(levels) else None


def days_silent(matter: dict, today: datetime.date | None = None) -> int | None:
    """Days since the current rung's request was sent with NO response since. None if never sent/responded."""
    today = today or datetime.date.today()
    sent = matter.get("current_rung_sent")
    if not sent:
        return None
    sd = datetime.date.fromisoformat(sent)
    for h in matter.get("history", []):
        if h.get("date", "") >= sent and any(e in str(h.get("event", "")) for e in _RESPONSE_EVENTS) \
                and "no_response" not in str(h.get("event", "")):
            return None
    lvl = _level_of(matter)
    if not lvl:
        return (today - sd).days
    return _working_days(sd, today) if lvl.get("working") else (today - sd).days


def is_overdue(matter: dict, today: datetime.date | None = None) -> bool:
    today = today or datetime.date.today()
    lvl = _level_of(matter)
    ds = days_silent(matter, today)
    if ds is None or not lvl or lvl.get("deadline_days") is None:
        return False
    return ds >= lvl["deadline_days"]


def next_action(matter: dict, today: datetime.date | None = None) -> dict:
    """What this matter should do next: current rung, its clock, whether overdue, and the on_expiry consequence.
    Used by the ledger/report + m0/Hermes to name the next-best-action deterministically."""
    today = today or datetime.date.today()
    lvl = _level_of(matter)
    fk = matter.get("forum_key") or forum_key(matter.get("actor", ""))
    if not lvl:
        return {"forum_key": fk, "level": None, "note": "no grounded ladder for this actor/forum"}
    ds = days_silent(matter, today)
    return {
        "forum_key": fk, "level": lvl.get("level"), "cite": lvl.get("cite"),
        "deadline_days": lvl.get("deadline_days"), "working_days": bool(lvl.get("working")),
        "days_silent": ds, "overdue": is_overdue(matter, today),
        "on_expiry": lvl.get("on_expiry"),
    }


def grounding_block(text_or_forum: str) -> str:
    """m0/Hermes GROUNDING: the grounded escalation ladder for the forum this filing concerns — each level's
    statutory cite + day-clock + on_expiry consequence. Returns '' if no forum resolves (input-only, safe)."""
    fk = forum_key(text_or_forum) or (text_or_forum if text_or_forum in _load() else None)
    lad = _load().get(fk) if fk else None
    if not lad:
        return ""
    lines = [f"GROUNDED ESCALATION LADDER [{fk}] — {lad.get('forum', '')}:"]
    for i, lv in enumerate(lad.get("levels", []), 1):
        dd = lv.get("deadline_days")
        clock = f"{dd} {'working' if lv.get('working') else 'calendar'} days" if dd is not None else "no fixed complainant clock"
        lines.append(f"  {i}. {lv.get('level')} — {clock} [{lv.get('cite')}] -> on expiry: {lv.get('on_expiry')}")
    lines.append("Cite the forum + its GROUNDED statutory deadline + the on-expiry consequence; never invent a deadline.")
    return "\n".join(lines)


def sweep(ledger: dict, today: datetime.date | None = None) -> list[str]:
    """Auto-escalate any matter whose current rung's clock has run out with no response. Mirrors
    accountability_tracker.record(no_response) but fires from the CLOCK, not a manual call. Advances the
    ledger's own `chain`/`rung` (kept consistent with the tracker), stamps the grounded cite into history."""
    today = today or datetime.date.today()
    notes = []
    for m in ledger.get("matters", []):
        st = m.get("status", "")
        if st.startswith("resolved") or "exhausted" in st:
            continue
        if not is_overdue(m, today):
            continue
        lvl = _level_of(m) or {}
        chain = m.get("chain", [])
        cur = chain[m["rung"]] if m.get("rung", 0) < len(chain) else "(current rung)"
        ds = days_silent(m, today)
        m.setdefault("history", []).append({
            "date": today.isoformat(), "event": f"deadline expired: no_response after {ds}d",
            "forum": cur, "cite": lvl.get("cite"), "consequence": lvl.get("on_expiry")})
        if m.get("rung", 0) + 1 < len(chain):
            m["rung"] += 1
            m["current_rung_sent"] = None
            m["status"] = f"escalated -> {chain[m['rung']]}"
            m["history"].append({"date": today.isoformat(), "event": "auto-escalated (clock)", "forum": chain[m["rung"]]})
            notes.append(f"{m.get('key')}: {lvl.get('cite')} clock ran ({ds}d) -> escalated to {str(chain[m['rung']])[:40]}")
        else:
            m["status"] = "chain exhausted — all avenues pursued (clock)"
            m["ripe_for_next_forum"] = True
            notes.append(f"{m.get('key')}: {lvl.get('cite')} clock ran ({ds}d) -> {lvl.get('on_expiry')}")
    return notes


if __name__ == "__main__":
    import json, sys
    L = _load()
    if len(sys.argv) > 1:                       # `python -m pipeline.escalation_ladders <forum_or_actor>`
        fk = forum_key(sys.argv[1]) or sys.argv[1]
        print(json.dumps({fk: L.get(fk)}, indent=2, default=str))
    else:
        lv = sum(len(v["levels"]) for v in L.values())
        print(f"escalation_ladders: {len(L)} forums, {lv} levels")
        for k, v in L.items():
            clocks = [x["deadline_days"] for x in v["levels"] if x.get("deadline_days") is not None]
            print(f"  {k:22} {len(v['levels'])} levels, {len(clocks)} clocks -> {v['forum'][:60]}")
