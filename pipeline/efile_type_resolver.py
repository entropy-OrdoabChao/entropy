#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pipeline/efile_type_resolver.py — LLM-driven FJD filing-type resolver (no hardcoded CATEGORY_DOC).

The FJD e-filing wizard presents a Category dropdown, and selecting a category populates a Document dropdown.
Hardcoding (filing_type -> code) per document is untenable — there are hundreds of document types. Instead the
e-filer READS the LIVE dropdown options and this module has the LLM pick the option whose label best matches
the filing being submitted. Generalizes to ANY filing; the map extends itself.

pick_option(filing_desc, options) -> the chosen option VALUE (or None). options = [(value, label), ...] read
straight from the live <select>. Deterministic prompt, temperature 0; falls back to a lexical best-match when
the LLM is unreachable so filing never hard-stops on the resolver alone.
"""
from __future__ import annotations
import os, re, json, urllib.request

LLAMA = os.environ.get("BEEZLE_LLAMA_URL", "http://localhost:8000/v1/chat/completions")
# default = the local-gpu LEGAL lane (user-locked), matching case_facts_refresh / m0_exposure_map /
# accountability_map. The old :8000 default was dead on this box, so EVERY pick_option() call
# raised URLError and fell through to the word-count fallback — that is how E-File #2609070813
# got filed as PRSUB. Same class of failure as the 2026-08-31 CASE_FACTS night run.
MODEL = os.environ.get("BEEZLE_CF_MODEL", "qwen3.8-27b")

_SERVED_MODEL = None


def _resolve_model() -> str:
    """Return a model name this lane ACTUALLY serves (pattern from case_facts_refresh).

    A stale MODEL name is indistinguishable here from "no option matched": posting an
    unserved name returns HTTP 404, the exception path runs, and a word-count picks the
    court filing code. The old hardcoded 'qwen36-local-gpu' is served by NOTHING on this box.
    """
    global _SERVED_MODEL
    if _SERVED_MODEL is not None:
        return _SERVED_MODEL
    served = []
    try:
        base = LLAMA.split("/v1/")[0]
        with urllib.request.urlopen(f"{base}/v1/models", timeout=20) as r:
            served = [m.get("id") for m in (json.loads(
                r.read().decode("utf-8")).get("data") or []) if m.get("id")]
    except Exception as e:
        print(f"  [efile_type_resolver] WARNING: cannot list models at {LLAMA}: "
              f"{type(e).__name__}: {str(e)[:70]} — using configured {MODEL!r}")
        _SERVED_MODEL = MODEL
        return _SERVED_MODEL
    if MODEL in served:
        _SERVED_MODEL = MODEL
    elif served:
        _SERVED_MODEL = served[0]
        print(f"  [efile_type_resolver] configured {MODEL!r} is NOT served by this lane "
              f"(serves {served}); using {_SERVED_MODEL!r}. Set BEEZLE_CF_MODEL to silence.")
    else:
        _SERVED_MODEL = MODEL
    return _SERVED_MODEL


MIN_LEXICAL_SCORE = int(os.environ.get("EFILE_MIN_LEXICAL_SCORE", "2"))

# Why this module refuses instead of guessing (2026-09-29, E-File #2609070813):
# a "Praecipe To Transmit The Record On Appeal" was submitted to the FJD as M/PRSUB =
# "Praecipe To Supplement/Attach Documents To Record" — a different instrument — and the
# OJR rejected it. Cause: the word "transmit" appears in ZERO of the 303 live document
# labels, so the old lexical fallback produced a FIVE-WAY tie at score 2 (PRSUB, PRSAP,
# PRAAP, CRTRR, APPRS) and returned whichever sat first in menu order, because the scan
# used a strict `>`. Worse, an explicit LLM refusal ({"value":""}) fell through to that
# same lexical guess.
#
# Two rules follow, and they are the doctrine, not a special case for praecipes:
#   1. A wrong code is not a lesser failure than no code. It puts a mislabeled instrument
#      on a court docket. The resolver REFUSES; the caller HALTS.
#   2. Word overlap is a GUARDRAIL, never a decider. Picking a filing code by counting
#      shared words is a keyword table impersonating comprehension. When the comprehension
#      layer (the LLM) is unreachable, this module refuses and logs the lexical guess as an
#      operator SUGGESTION — it does not file under it.
LAST_REFUSAL: str = ""


def _set_refusal(msg: str) -> None:
    global LAST_REFUSAL
    LAST_REFUSAL = msg
    print(f"  [efile_type_resolver] REFUSED: {msg}")


def _lexical_suggest(filing_desc: str, options, min_score: int | None = None):
    """ADVISORY ONLY — never decides a filing code. Returns (value, score, n_tied) for the
    option sharing the most significant (>=4 char) words with the filing description, or
    (None, score, n_tied) when the count does not discriminate (below `min_score`, or tied).
    Callers surface this to the operator; they must not submit on it."""
    min_score = MIN_LEXICAL_SCORE if min_score is None else min_score
    fw = set(re.findall(r"[a-z]{4,}", (filing_desc or "").lower()))
    scored = [(len(fw & set(re.findall(r"[a-z]{4,}", (lab or "").lower()))), val)
              for val, lab in options]
    if not scored:
        return None, 0, 0
    top = max(s for s, _ in scored)
    winners = [v for s, v in scored if s == top]
    if top < min_score or len(winners) != 1:
        return None, top, len(winners)
    return winners[0], top, 1


def _llm(prompt: str, max_tokens: int = 160) -> str:
    body = json.dumps({"model": _resolve_model(),
                       "messages": [{"role": "user", "content": prompt}],
                       "max_tokens": max_tokens, "temperature": 0.0,
                       "chat_template_kwargs": {"enable_thinking": False}}).encode()
    r = urllib.request.urlopen(urllib.request.Request(
        LLAMA, data=body, headers={"Content-Type": "application/json"}), timeout=90)
    return json.loads(r.read().decode())["choices"][0]["message"].get("content", "")


def _verify_pick(filing_desc: str, label: str, kind: str):
    """Second comprehension pass: does `label` perform the SAME procedural act as the filing?

    The pick pass optimizes for "closest available", which on a menu with no true match
    yields a confident near-miss (9/29: a praecipe directing the clerk to TRANSMIT the
    record was matched to "Certified Record Filed In A Statutory Appeal" — same subject,
    different act, different actor). Asking the question in ISOLATION — no option list to
    be relatively-best within — turns it from a ranking into a judgment.
    Returns (ok: bool, reason: str).
    """
    out = _llm(
        f"A pro se litigant wants to e-file this document in the Philadelphia Court of Common Pleas:\n"
        f'  FILING: "{filing_desc}"\n\n'
        f"The court's {kind} menu option chosen for it is:\n  OPTION: \"{label}\"\n\n"
        f"Question: does that option denote the SAME procedural act by the SAME actor as the filing?\n"
        f"Answer NO if the option:\n"
        f"  - names a different act on the same subject (supplementing a record vs. directing its transmission;\n"
        f"    a record already filed by a clerk vs. a praecipe ordering a clerk to transmit one;\n"
        f"    striking an appeal vs. withdrawing one);\n"
        f"  - is filed BY a different actor (the clerk, the court, the opposing party) than this filing;\n"
        f"  - is the thing being responded TO rather than the response (e.g. 'Preliminary Objections'\n"
        f"    chosen for an ANSWER to preliminary objections).\n"
        f"Answer YES only if a Pennsylvania prothonotary would docket this filing under that exact option.\n"
        f'Return STRICT JSON only: {{"same_act": true|false, "why": "<12 words>"}}',
        max_tokens=120)
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        return False, f"verifier returned no JSON ({out[:60]!r})"
    try:
        d = json.loads(m.group(0))
    except Exception:
        return False, f"verifier JSON unparseable ({m.group(0)[:60]!r})"
    return bool(d.get("same_act")), str(d.get("why", ""))[:120]


def pick_option(filing_desc: str, options, kind: str = "category"):
    """LLM picks the FJD dropdown VALUE whose label matches `filing_desc`. options=[(value,label)].
    Returns the value string, or None when NO option corresponds to the instrument. A None is a
    HARD STOP for the caller — never a cue to submit under a near-miss code (see the PRSUB note
    above). `LAST_REFUSAL` carries the reason for the log/operator."""
    global LAST_REFUSAL
    LAST_REFUSAL = ""
    options = [(v, (l or "").strip()) for v, l in options if v and v.strip() and (l or "").strip()]
    if not options:
        _set_refusal(f"{kind}: the live dropdown returned no usable options")
        return None
    labels = {v: l for v, l in options}
    listing = "\n".join(f'{i+1}. value="{v}" | {l}' for i, (v, l) in enumerate(options))
    prompt = (
        f"A pro se litigant is e-filing this document in the Philadelphia Court of Common Pleas:\n"
        f'  FILING: "{filing_desc}"\n\n'
        f"The court's {kind} dropdown offers these options:\n{listing}\n\n"
        f"Pick the ONE option whose LEGAL FUNCTION matches the filing — the {kind} a Pennsylvania court clerk "
        f"would choose. Reason about what the document procedurally IS, NOT shared words: the court's option "
        f"names often differ from a filing's title. For example, a Pa.R.A.P. 1925(b) 'Concise Statement of "
        f"Errors Complained of on Appeal' is filed under 'STATEMENT OF MATTERS COMPLAINED OF ON APPEAL' (not "
        f"'briefs'); an opposition/answer goes under 'RESPONSES' or 'ANSWERS'; a preliminary objection under "
        f"'PRELIMINARY OBJECTIONS'. Match the procedural role. "
        f"A NEAR MISS IS A WRONG ANSWER: an option that touches the same subject matter by a DIFFERENT "
        f"procedural act, or that is filed by a DIFFERENT ACTOR (the clerk or the court rather than this "
        f"litigant), does NOT match — filing under it puts a mislabeled instrument on the docket. "
        f"Some instruments have no code in this menu at all; \"none\" is the correct answer for those, and "
        f"is ALWAYS better than the closest available option. "
        f'Return STRICT JSON only: {{"value":"<the exact value= string>","label":"<its label>"}}. '
        f"If truly none fits, return {{\"value\":\"\"}}.")
    try:
        content = _llm(prompt, max_tokens=120)
        m = re.search(r"\{.*\}", content, re.S)
        val = (json.loads(m.group(0)).get("value") if m else "") or ""
        if val not in labels:
            # LLM returned a label or near-miss string -> map back to a real option value
            hit = next((v for v, l in options
                        if val and (val.lower() == l.lower() or val.lower() in l.lower())), None)
            if not val.strip():
                # EXPLICIT REFUSAL by the comprehension layer. Honor it. Falling through to a
                # lexical count here is what shipped M/PRSUB: it overrides the only organ that
                # actually read the labels with a word-overlap tally.
                _set_refusal(f"{kind}: the model read all {len(options)} live options and found NONE "
                             f"that corresponds to {filing_desc!r}")
                return None
            if not hit:
                _set_refusal(f"{kind}: model returned {val!r}, not a live option value or label")
                return None
            val = hit
        ok, why = _verify_pick(filing_desc, labels[val], kind)
        if not ok:
            _set_refusal(f"{kind}: pick {val} ({labels[val]!r}) FAILED the same-act check for "
                         f"{filing_desc!r} — {why}")
            return None
        return val
    except Exception as e:
        # Comprehension layer unreachable. A word-count must not choose a court filing code
        # (rule 2 above), so REFUSE — and hand the operator the lexical suggestion to check.
        sug, score, tied = _lexical_suggest(filing_desc, options)
        hint = (f"closest by wording: {sug!r} (score {score})" if sug
                else f"not even a lexical lead (best score {score}, {tied} tied)")
        _set_refusal(f"{kind}: comprehension layer unreachable "
                     f"({type(e).__name__}: {str(e)[:90]}) — refusing to pick a filing code by "
                     f"word count; {hint}. Bring BEEZLE_LLAMA_URL up and re-run.")
        return None


if __name__ == "__main__":
    demo = [("NS", "Notice of Appeal to Superior Court"), ("MI", "Miscellaneous"),
            ("ST", "Statement of Matters / Concise Statement of Errors (Pa.R.A.P. 1925(b))")]
    print("category ->", pick_option("Concise Statement of Errors Complained of on Appeal, Pa.R.A.P. 1925(b)", demo))
