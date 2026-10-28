"""m0 amendment gate — decide AMEND vs DECLINE via m0's LLM reasoning.

Closes the leak where an amended_complaint is generated *just because* a
judge's order (or seeded pending_motion) referenced amendment. m0 — the
LLM reasoner with the legal corpus, reached through pipeline.llm_gate
(the local-gpu model on :8000) — decides whether amending is the correct move,
or whether it should be DECLINED because:
  - the dismissal was WITH PREJUDICE (amendment futile/not permitted),
  - the "amendment" is a non-pleading amendment (e.g. Pa.R.C.P. 1033
    judgment-amount amendment), not a complaint amendment,
  - the correct move is to CHALLENGE the order (vacate / Rule 60(b) /
    appeal) and amending would waive that challenge,
  - there is no live leave to amend / the deadline has passed,
  - there are no surviving claims to amend.

Single compact decision call (signals + truncated order excerpt) — sized
for the local-gpu context limit, same truncation discipline as llm_gate.
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import json
from pathlib import Path

import yaml

ROOT = _eroot()  # R3: hardcoded WSL path, matches the pipeline
_CASES = ROOT / "obsidian_sync" / "cases"


def _gt_for(case_id: str) -> dict:
    """Resolve CASE_GROUND_TRUTH.yaml by its own case_id field."""
    try:
        for d in sorted(_CASES.glob("*/")):
            gt = d / "meta" / "CASE_GROUND_TRUTH.yaml"
            if not gt.exists():
                continue
            try:
                data = yaml.safe_load(gt.open(encoding="utf-8")) or {}
            except Exception:
                continue
            if data.get("case_id") == case_id:
                return data
    except Exception:
        pass
    return {}


def _load_audit(case_id: str) -> dict:
    """Load the case audit (everything the litigant already called out)."""
    slug = case_id.replace(":", "_")
    p = ROOT / "output" / "audit" / f"data_{slug}.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _exposed_record(audit: dict) -> dict:
    """Summarize what the litigant has ALREADY exposed across all filings —
    proof the litigant followed the rules and called everything out."""
    sc = (audit.get("scorecard") or {})
    proc = audit.get("procedural_violations") or []
    from collections import Counter
    cats = Counter()
    order_violations = []  # violations on judicial orders (incl. any amend order)
    for v in proc:
        cats[v.get("violation_type") or v.get("category") or "?"] += 1
        src = (str(v.get("source_doc") or v.get("source") or "")).lower()
        if any(k in src for k in ("order", "memorand", "amend")) or \
           "judicial" in (v.get("violation_type") or "").lower():
            order_violations.append({
                "type": v.get("violation_type") or v.get("category"),
                "source_doc": v.get("source_doc") or v.get("source"),
                "rule": v.get("rule_violated") or v.get("rule"),
            })
    return {
        "stare_decisis_violations": len((sc.get("violations") or [])),
        "procedural_violations_total": len(proc),
        "violation_categories": dict(cats),
        "violations_on_orders": order_violations[:25],
        "scorecard_fatal": sc.get("fatal_count"),
        "scorecard_serious": sc.get("serious_count"),
    }


def _gather_signals(gt: dict) -> dict:
    """Deterministic posture signals m0 reasons over."""
    sig = {
        "case_status": gt.get("status"),
        "appeal_number": gt.get("appeal_number"),
        "with_prejudice": [],
        "without_prejudice_leave": [],
        "pending_challenge_motions": [],
        "any_dismissal": False,
    }
    caption = gt.get("caption") or {}
    for d in (caption.get("defendants") or []):
        nm = d.get("name", "?")
        blob = " ".join(str(d.get(k) or "") for k in
                        ("status", "dismissed", "dismissed_basis", "notes")).lower()
        if "dismiss" in blob:
            sig["any_dismissal"] = True
        if "with prejudice" in blob or "with_prejudice" in blob:
            sig["with_prejudice"].append(nm)
        if "without prejudice" in blob or "leave to amend" in blob or "without_prejudice" in blob:
            sig["without_prejudice_leave"].append(nm)
    for pm in (gt.get("pending_motions") or []):
        mt = (pm.get("motion_type") or "").lower()
        if any(k in mt for k in ("vacate", "60b", "60(b)", "reconsider",
                                 "appeal", "mandamus", "strike")):
            sig["pending_challenge_motions"].append(mt)
    return sig


M0_SYSTEM = (
    "You are m0, Beezle's filing-decision reasoner, protecting a PRO SE / SUI JURIS "
    "litigant whose original claim and prior amendments are presumed CORRECT. A court "
    "order or posture references amending the complaint. Decide AMEND or DECLINE.\n\n"
    "CRITICAL — treat an order or 'leave to amend by [deadline]' as PRESUMPTIVELY "
    "ADVERSARIAL. Courts and opposing counsel routinely use an order to amend as a "
    "TACTICAL TRICK to:\n"
    "  - RESET THE CLOCK (restart deadlines, erasing the litigant's accrued timeliness);\n"
    "  - MOOT the litigant's prior, already-correct filings (a forced re-file wipes that "
    "work and waives accrued positions);\n"
    "  - Manufacture a 'with prejudice' trap if the litigant declines.\n"
    "If the litigant's operative pleading and prior amendments are already correct, "
    "COMPLYING HARMS the litigant. The correct move is to STAND ON the operative pleading "
    "and CHALLENGE the order (oppose / motion to vacate / Rule 60(b) / appeal / expose the "
    "order itself as the defect) — NOT to amend.\n\n"
    "DECLINE when ANY of these hold:\n"
    "- The operative/prior pleading is already correct and amending would reset the clock, "
    "moot prior filings, or waive accrued positions (the reset/moot trick).\n"
    "- The dismissal was WITH PREJUDICE (amendment futile).\n"
    "- The 'amendment' is a NON-pleading amendment (e.g. Pa.R.C.P. 1033 judgment amount), "
    "not a complaint amendment.\n"
    "- The correct move is to CHALLENGE the order rather than comply.\n"
    "- No surviving claims to amend / default-judgment enforcement posture.\n\n"
    "AMEND ONLY when: there is a GENUINE, curable defect in the litigant's OWN pleading "
    "that amending fixes, AND amending does NOT reset the clock, moot prior correct filings, "
    "or sacrifice any accrued position or stronger remedy. This is rare.\n\n"
    "You are given the litigant's EXPOSED RECORD: every violation already detected and "
    "called out across all filings (the audit). USE IT. If the record shows the litigant "
    "followed the rules and already exposed the defects, then an order directing amendment "
    "is itself IMPROPER — recognize it and POINT OUT the order's own violation and bias "
    "(ignoring exposed violations, sua sponte reset without basis, disparate pro se "
    "treatment, due-process / judicial-canon breach) so it can be added to the scorecard "
    "and challenged.\n\n"
    "When uncertain, DEFAULT TO DECLINE — protect the litigant's accrued position.\n\n"
    "Reason ONLY from the signals, exposed_record, and order text provided. Output STRICT JSON:\n"
    '{"decision": "AMEND" | "DECLINE", '
    '"reason": "<1-2 sentences; name the trick or controlling signal>", '
    '"scope": "<defendants/claims if partial, else all or n/a>", '
    '"order_violations": [{"violation": "<what the amend order itself does wrong>", '
    '"authority": "<rule / judicial canon / constitutional basis>", '
    '"bias_indicator": "<the bias it shows, or empty>"}], '
    '"challenge_instead": "<the move instead: oppose / vacate / 60(b) / appeal / expose order>"}'
)


def decide_amendment(case_id: str, order_text: str = "", *, gate=None) -> dict:
    """Return {decision: AMEND|DECLINE, reason, scope, source, signals}."""
    gt = _gt_for(case_id)
    sig = _gather_signals(gt)
    exposed = _exposed_record(_load_audit(case_id))  # everything already called out

    # m0 LLM reasoning over the FULL exposed record via the local-gpu channel.
    # (No deterministic short-circuit: m0 must always reason and point out the
    # order's own violation/bias, even in the clear cases.)
    if gate is None:
        try:
            from pipeline.llm_gate import get_gate
            gate = get_gate()
        except Exception as e:  # gate import failure -> protective DECLINE (don't take the bait)
            return {"decision": "DECLINE", "scope": "all", "source": "fallback",
                    "reason": f"m0 gate unavailable ({e}); DECLINE pending review — amend orders are presumptively reset/moot traps.",
                    "challenge_instead": "oppose/vacate; stand on operative pleading", "signals": sig}

    if not gate.is_available():
        return {"decision": "DECLINE", "scope": "all", "source": "fallback",
                "reason": "m0 (LLM) offline; DECLINE pending review — amend orders are presumptively reset/moot traps.",
                "challenge_instead": "oppose/vacate; stand on operative pleading", "signals": sig}

    user = json.dumps({"case_id": case_id, "signals": sig, "exposed_record": exposed,
                       "order_excerpt": (order_text or "")[:6000]}, default=str)
    try:
        raw = gate.llm.chat(
            messages=[{"role": "system", "content": M0_SYSTEM},
                      {"role": "user", "content": user}],
            thinking=True,
        )
        out = gate._parse_json(raw, "decide_amendment") or {}
        dec = str(out.get("decision") or "").upper()
        if dec not in ("AMEND", "DECLINE"):
            dec = "DECLINE"  # unparseable -> protective default
        return {"decision": dec, "scope": out.get("scope", "n/a"),
                "reason": out.get("reason", ""),
                "order_violations": out.get("order_violations", []),
                "challenge_instead": out.get("challenge_instead", ""),
                "source": "m0_llm", "signals": sig, "exposed": exposed}
    except Exception as e:
        return {"decision": "DECLINE", "scope": "all", "source": "error",
                "reason": f"m0 reasoning error ({e}); DECLINE pending review.",
                "challenge_instead": "oppose/vacate; stand on operative pleading", "signals": sig}


if __name__ == "__main__":
    import sys
    for cid in (sys.argv[1:] or ["fed:2:26-cv-00123", "fed:2:26-cv-00456",
                                 "fed:2:26-cv-00789", "phila_ccp:260100000"]):
        d = decide_amendment(cid)
        print(f"{cid:24} -> {d['decision']:8} [{d['source']}] {d['reason'][:90]}")
