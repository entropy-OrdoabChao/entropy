#!/usr/bin/env python3
"""foia_engine — the connector that makes FOIA a PIPELINE, not a memory task.

Flow (user doctrine 2026-09-17: input -> analysis(m0) -> draft/email; clocks decide, m0 comprehends):
  intake (cases/<slug>/intake + CASE_FACTS.md)
    -> draft_request(case, agency): m0 (local-gpu) drafts the FOIA request from the case record;
       staged to output/foia/<case>/<agency>_request_DRAFT.md + kanban reply card (NEVER auto-sent)
    -> sent(case, agency): user mailed/emailed it -> accountability_tracker.mark_sent starts the
       20-working-day clock (escalation_ladders 'foia' ladder, corpus-cited 5 USC 552(a)(6))
    -> tick(): sweep all foia matters; any expired rung = constructive denial ->
       kanban card + m0 drafts the NEXT instrument (appeal, then 552(a)(4)(B) complaint via
       the revived foia_builder path) -- each a bounded piece.

Organs reused (graphify-verified 2026-09-17): accountability_tracker (ledger/day-clocks),
escalation_ladders (grounded 'foia' ladder), kanban_bridge (working memory),
foia_builder.build_foia_complaint_args (complaint kwargs, was OUT-OF-FLOW -- re-wired here).
Deterministic = guardrail (clocks, rungs, paths); m0 = comprehension (request/appeal text).
No deterministic-only fallback: if m0 is unreachable the step stages a BLOCKED card, never a template.
"""
from __future__ import annotations
import json, sys, datetime, subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "output" / "foia"
LEDGER = ROOT / "output" / "audit" / "foia_engine.json"
M0_URL = "http://localhost:8000/v1/chat/completions"
M0_MODEL = "qwen3.8-27b"
TODAY = datetime.date.today().isoformat()


# ── ledger ───────────────────────────────────────────────────────────────────
def _load() -> dict:
    if LEDGER.exists():
        return json.loads(LEDGER.read_text())
    return {"matters": []}


def _save(d: dict) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(d, indent=2))


def _matter(d: dict, case: str, agency: str) -> dict | None:
    return next((m for m in d["matters"] if m["case"] == case and m["agency"] == agency), None)


# ── m0 (comprehension lane) ──────────────────────────────────────────────────
def _m0(prompt: str, max_tokens: int = 2800) -> str | None:
    import urllib.request
    try:
        req = urllib.request.Request(M0_URL, data=json.dumps({
            "model": M0_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens, "temperature": 0.2,
            "chat_template_kwargs": {"enable_thinking": False},
        }).encode(), headers={"Content-Type": "application/json"})
        out = json.load(urllib.request.urlopen(req, timeout=240))
        return out["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[foia] m0 unreachable ({type(e).__name__}) — staging BLOCKED card, no template fallback")
        return None


def _case_slice(case: str, limit: int = 6000) -> str:
    facts = ROOT / "obsidian_sync" / "cases" / case / "CASE_FACTS.md"
    return facts.read_text()[:limit] if facts.exists() else "(no CASE_FACTS.md)"


def _card(lane: str, case: str, title: str, due: str, body: str) -> None:
    subprocess.run([sys.executable, "-m", "pipeline.kanban_bridge", "card", lane, case,
                    title, "--due", due, "--body", body], cwd=ROOT, check=False)


# ── steps ────────────────────────────────────────────────────────────────────
LEVELS = ["request", "administrative_appeal", "district_court"]  # ogis optional, user elects

PROMPTS = {
    "request": ("Draft a FOIA request letter to {agency}. Requester: FILER NAME, "
                "123 Main St, Suite 100, Anytown, ST 00000, you@example.com. Ground every "
                "asserted fact ONLY in the case record below; describe records sought with reasonable "
                "specificity; cite 5 U.S.C. 552; request fee waiver/media terms only if the record "
                "supports it; ask for determination within 20 working days per 552(a)(6)(A)(i). "
                "Plain firm human voice, no AI-tells, no invented facts.\n\nCASE RECORD:\n{record}"),
    "administrative_appeal": ("Draft a FOIA administrative appeal to {agency}. The request below "
                "received no determination within 20 working days -> constructive denial, 5 U.S.C. "
                "552(a)(6)(C)(i) (deemed exhausted). Demand determination within 20 working days per "
                "552(a)(6)(A)(ii); preserve judicial review under 552(a)(4)(B). Quote only from the "
                "record.\n\nORIGINAL REQUEST + RECORD:\n{record}"),
}


def seed(case: str, agency: str) -> None:
    d = _load()
    if _matter(d, case, agency):
        print(f"exists: {case}/{agency}"); return
    d["matters"].append({"case": case, "agency": agency, "level": 0, "status": "seeded",
                         "sent": None, "history": [{"date": TODAY, "event": "seeded"}]})
    _save(d)
    print(f"seeded {case}/{agency} at level=request")


def draft(case: str, agency: str) -> None:
    d = _load()
    m = _matter(d, case, agency)
    if not m:
        seed(case, agency); d = _load(); m = _matter(d, case, agency)
    level = LEVELS[m["level"]]
    if level == "district_court":
        print("level=district_court -> use foia_builder complaint path (complaint subcommand)"); return
    text = _m0(PROMPTS[level].format(agency=agency, record=_case_slice(case)))
    dest = OUT / case
    dest.mkdir(parents=True, exist_ok=True)
    if text is None:
        _card("finding", case, f"FOIA {level} draft BLOCKED (m0 down) — {agency}", "2",
              "foia_engine could not reach m0; re-run draft when local-gpu lane is up.")
        return
    p = dest / f"{agency.lower().replace(' ', '_')}_{level}_DRAFT.md"
    p.write_text(text + "\n")
    m["status"] = f"{level}_drafted"
    m["history"].append({"date": TODAY, "event": f"{level} drafted", "path": str(p)})
    _save(d)
    _card("reply", case, f"FOIA {level} DRAFT ready — {agency}: review + send (skeptic gate first)",
          "3", f"Draft: {p}. Gate: skeptic + letter-humanizer, then send/mail; then: "
               f"python3 -m pipeline.foia_engine sent {case} '{agency}'")
    print(f"DRAFTED {level} -> {p}")


def sent(case: str, agency: str) -> None:
    d = _load()
    m = _matter(d, case, agency)
    if not m:
        print("unknown matter — seed/draft first"); return
    m["sent"] = TODAY
    m["status"] = f"{LEVELS[m['level']]}_sent"
    m["history"].append({"date": TODAY, "event": f"{LEVELS[m['level']]} sent — clock starts"})
    _save(d)
    print(f"clock started {case}/{agency} level={LEVELS[m['level']]} sent={TODAY}")


def tick() -> None:
    """The cron step: expire clocks -> escalate level -> card + next draft. Bounded: one level per tick."""
    from pipeline import escalation_ladders as E
    ladder = E.ladder_for("foia")["levels"]
    d = _load()
    for m in d["matters"]:
        if not m.get("sent") or m["level"] >= len(LEVELS) - 1:
            continue
        rung = next((r for r in ladder if r["level"] == LEVELS[m["level"]]), None)
        if not rung or rung["deadline_days"] is None:
            continue
        a = datetime.date.fromisoformat(m["sent"])
        days = (E._working_days(a, datetime.date.today()) if rung.get("working")
                else (datetime.date.today() - a).days)
        if days > rung["deadline_days"]:
            prev = LEVELS[m["level"]]
            m["level"] += 1
            m["sent"] = None
            m["status"] = f"escalated_from_{prev}"
            m["history"].append({"date": TODAY, "event":
                f"{prev} expired ({days}d > {rung['deadline_days']}) — {rung['on_expiry']}"})
            _save(d)
            nxt = LEVELS[m["level"]]
            _card("filing", m["case"],
                  f"FOIA {prev} EXPIRED ({m['agency']}): {rung['on_expiry']} -> draft {nxt}",
                  "5", f"cite: {rung['cite']} [{rung['provenance']}]. Auto-drafting {nxt} now.")
            if nxt != "district_court":
                draft(m["case"], m["agency"])
            print(f"ESCALATED {m['case']}/{m['agency']}: {prev} -> {nxt}")
    print("tick done")


def report() -> None:
    d = _load()
    if not d["matters"]:
        print("no foia matters — seed one: python3 -m pipeline.foia_engine seed <case> <agency>")
    for m in d["matters"]:
        print(f"  {m['case']} / {m['agency']:28} level={LEVELS[m['level']]:22} "
              f"status={m['status']} sent={m['sent']}")


if __name__ == "__main__":
    args = sys.argv[1:]
    cmds = {"seed": (seed, 2), "draft": (draft, 2), "sent": (sent, 2), "tick": (tick, 0), "report": (report, 0)}
    if not args or args[0] not in cmds:
        sys.exit("usage: foia_engine seed|draft|sent <case> <agency> | tick | report")
    fn, n = cmds[args[0]]
    fn(*args[1:1 + n])
