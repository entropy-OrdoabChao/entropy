#!/usr/bin/env python3
"""organ_seat.py — run an m0/m1 comprehension organ as a SEAT (SOUL + lane), no GPU required.

An ORGAN is one bounded comprehension step the pipeline already defines (its PROMPT constant + the inputs it
assembles). A SEAT is that organ's prompt-contract as a SOUL.md plus the lane it runs on. The deterministic
guardrails around the organ (corpus pulls, cite_verifier, rule gates, JSON parsing) stay in the pipeline module;
only the comprehension call moves. That is the Beezle doctrine: deterministic = guardrail, LLM = comprehension.

Lanes (pick by env or --lane):  any OpenAI-compatible /v1/chat/completions endpoint —
    llama.cpp server on CPU (e.g. http://127.0.0.1:8000), Ollama (http://127.0.0.1:11434/v1), LM Studio
    (http://127.0.0.1:1234/v1), or a cloud key (OPENAI_BASE_URL + OPENAI_API_KEY). Nothing here assumes a GPU.

Usage:
  python3 -m harness.organ_seat list
  python3 -m harness.organ_seat run quality_review <draft.md> [--type complaint] [--court PA_CP_PHILA] [--lane URL] [--model ID]
  python3 -m harness.organ_seat soul quality_review          # print the SOUL the seat runs under

Output: the organ's own JSON verdict shape (so the pipeline, the parity harness, and the desk read it unchanged),
plus a sidecar <stem>.<organ>.seat.json with lane/model/latency for the parity ledger.
"""
from __future__ import annotations
import argparse, json, os, re, sys, time, urllib.request
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG))
from core.entropy_paths import entropy_root, package_root  # noqa: E402

SEATS_DIR = package_root() / "harness" / "organ_seats"

# ---------------------------------------------------------------- organ registry: name -> how the pipeline builds its prompt
# Each entry says which module holds the PROMPT and which function assembles the inputs. The seat reuses BOTH, so the
# seat sees exactly what m0 sees (same corpus excerpts, same court profile, same draft text). Parity is then a fair test.
ORGANS = {
    # call_fn = the module function that POSTs to the model; the seat swaps ONLY that. entry = the organ's own entry point.
    # kind: "draft" organs take a file path; "case" organs take a case_id and assemble their own record/corpus inputs.
    "quality_review": {
        "module": "pipeline.m0_quality_review", "prompt_const": "PROMPT", "call_fn": "_call_llm", "call_style": "prompt",
        "entry": "review", "kind": "draft", "verdict_key": "verdict",
        "describe": "Am Jur LITMUS: judges an assembled draft on 9 craft axes; PASS requires every axis >= 4.",
    },
    "posture_fit": {
        "module": "pipeline.m0_posture_fit", "prompt_const": "SYSTEM", "call_fn": "_m0", "call_style": "system_user",
        "entry": "posture_fit", "kind": "case", "verdict_key": "motions",
        "describe": "Per motion type: does the vehicle fit the procedural posture of the record? FILE / HOLD / DROP with why.",
    },
    "rebuttal_gate": {
        "module": "pipeline.m0_rebuttal_gate", "prompt_const": "M0_REBUT_SYSTEM", "call_fn": "_m0_json", "call_style": "system_user",
        "entry": "rebut_adverse", "kind": "case_text", "verdict_key": "grounds",
        "describe": "Gate on each rebuttal point: MOOT / SETTLED / RECORD / NOVEL with the controlling authority named.",
    },
    "appeal_gate": {
        "module": "pipeline.m0_appeal_gate", "prompt_const": "M0_APPEAL_SYSTEM", "call_fn": "_b70_json", "call_style": "system_user",
        "entry": "decide_appeal_vehicle", "kind": "case", "verdict_key": "primary_vehicle",
        "describe": "Is the order appealable now, by which vehicle, under which rule, with which deadline?",
    },
    "exposure_map": {
        "module": "pipeline.m0_exposure_map", "prompt_const": "PROMPT", "call_fn": "_llm", "call_style": "prompt",
        "entry": "map_exposure", "kind": "case_dir", "verdict_key": "items",
        "describe": "Full-spectrum violation map: duty-engine lane (deterministic) + LLM gap-fill, verified against the corpus, deduped.",
    },
    "claim_scan": {
        "module": "pipeline.m0_claim_scan", "prompt_const": "_PROMPT", "call_fn": "_call_llm", "call_style": "prompt",
        "entry": "discover", "kind": "email", "verdict_key": "duty",
        "describe": "One inbound email → is there a duty-breach? obligor / duty_type / breach / forum / why (or None).",
    },
}


def _lane():
    url = (os.environ.get("ENTROPY_LANE_URL") or os.environ.get("OPENAI_BASE_URL") or os.environ.get("BEEZLE_LLAMA_URL")
           or "http://127.0.0.1:8000").rstrip("/")
    if url.endswith("/chat/completions"):
        url = url[: -len("/chat/completions")]
    if not url.endswith("/v1"):
        url += "/v1"
    model = os.environ.get("ENTROPY_MODEL") or os.environ.get("BEEZLE_CF_MODEL") or "local-model"
    key = os.environ.get("OPENAI_API_KEY") or os.environ.get("ENTROPY_LANE_KEY") or ""
    return url, model, key


def chat(system: str, user: str, lane: str, model: str, key: str = "", max_tokens: int = 4000, temperature: float = 0.1,
         timeout: int = 900) -> str:
    body = {"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": temperature, "max_tokens": max_tokens, "stream": False,
            "chat_template_kwargs": {"enable_thinking": False}}
    req = urllib.request.Request(f"{lane}/chat/completions", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {key}"} if key else {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    msg = d["choices"][0]["message"]
    out = msg.get("content") or ""
    out = re.sub(r"<think>.*?</think>", "", out, flags=re.S).strip()
    if "{" not in out:  # some lanes put everything in reasoning_content; the JSON, if any, is at its tail
        out = (msg.get("reasoning_content") or msg.get("reasoning") or "")[-6000:]
    return out


def parse_json(txt: str) -> dict:
    """Same tolerant parse the organs use: first {...} block, repair a truncated tail."""
    i = txt.find("{\"verdict\"")
    if i < 0: i = txt.find("{\n")
    if i < 0: i = txt.find("{")
    raw = txt[i:] if i >= 0 else txt
    j = raw.rfind("}")
    raw = raw[: j + 1] if j > 0 else raw
    try:
        return json.loads(raw)
    except Exception:
        for tail in ("", "]}", "}]}", "\"}]}", "\"}]}}"):
            try:
                return json.loads(raw + tail)
            except Exception:
                continue
    return {"verdict": "UNPARSED", "raw": txt[:2000]}


def soul_for(organ: str, ftype: str = "filing") -> str:
    """The seat's SOUL = the organ's prompt contract with the input slots removed, plus the Beezle discipline block."""
    spec = ORGANS[organ]
    mod = __import__(spec["module"], fromlist=[spec["prompt_const"]])
    prompt = getattr(mod, spec["prompt_const"])
    contract = re.split(r"\n=== ", prompt)[0] if "\n=== " in prompt else prompt  # PROMPT has input sections; SYSTEM consts are pure contract
    contract = re.sub(r"\{ftype\}", ftype, contract)  # the only slot that lives in the contract half
    p = SEATS_DIR / organ / "SOUL.md"
    if p.exists():
        return p.read_text(encoding="utf-8")
    return (f"# SOUL — organ seat: {organ}\n\n{spec['describe']}\n\n## Contract (verbatim from the pipeline organ)\n\n{contract}\n\n"
            "## Discipline (locked)\n- Judge only what is on the page and in the excerpts given. Never invent facts, citations, or exhibits.\n"
            "- Corpus + written law decide; a gap is NOT IN CORPUS, never a guess.\n"
            "- Do NOT think out loud. Your reply starts with `{` and ends with `}` — the JSON object the contract names, nothing else.\n")


def run_via_organ(organ: str, args) -> tuple[dict, dict]:
    """Run the pipeline organ's OWN entry point (review()/gate()) with its model call redirected to the seat's lane.

    This is the seat definition: identical input assembly (docx/md read, locked-section elision, review window,
    court profile), identical prompt, identical JSON salvage — ONLY the lane and the SOUL prefix differ. No drift
    between what m0 sees and what the seat sees is possible, because it is the same code path.
    """
    spec = ORGANS[organ]
    mod = __import__(spec["module"], fromlist=["*"])
    lane, model, key = _lane()
    lane = (args.lane or lane).rstrip("/"); model = args.model or model
    soul = soul_for(organ, args.type)
    # the SOUL's extra rules (anything after the verbatim contract) ride as a system message; the organ prompt is the user turn
    extra = soul.split("## Discipline (locked)", 1)[-1] if "## Conclusory" in soul or "## Discipline" in soul else ""
    rules = "\n".join(part for part in soul.split("\n## ")[1:] if not part.startswith("Contract"))
    meta = {"calls": 0, "prompt_chars": 0, "seconds": 0.0}

    def _seat_prompt(prompt: str, timeout: int = 900) -> str:            # quality_review style: one prompt string -> raw text
        t0 = time.time(); meta["calls"] += 1; meta["prompt_chars"] += len(prompt) + len(rules)
        out = chat("## " + rules if rules else "Return only the JSON the task names.", prompt, lane, model, key,
                   max_tokens=args.max_tokens, timeout=max(timeout, 900))
        meta["seconds"] += time.time() - t0
        return out

    def _seat_system_user(system: str, user: str, *a, timeout: int = 900, **kw) -> dict:   # (system, user) -> parsed JSON
        t0 = time.time(); meta["calls"] += 1; meta["prompt_chars"] += len(system) + len(user) + len(rules)
        sysmsg = system + ("\n\n## " + rules if rules else "")
        out = chat(sysmsg, user, lane, model, key, max_tokens=args.max_tokens, timeout=max(timeout, 900))
        meta["seconds"] += time.time() - t0
        return parse_json(out)

    fn = spec["call_fn"]
    if not hasattr(mod, fn):
        raise SystemExit(f"{spec['module']} has no {fn} to redirect — organ not seat-able yet")
    setattr(mod, fn, _seat_prompt if spec["call_style"] == "prompt" else _seat_system_user)
    entry = getattr(mod, spec["entry"])
    if spec["kind"] == "draft":
        kwargs = {"filing_type": args.type}
        if args.court:
            kwargs["court"] = args.court
        data = entry(args.input, **kwargs)
    elif spec["kind"] == "case_text":                      # rebut_adverse(case_id, adverse_text)
        text = Path(args.text).read_text(encoding="utf-8", errors="replace") if args.text else ""
        if not text:
            raise SystemExit("rebuttal_gate needs --text <adverse order/brief file>")
        data = entry(args.input, text)
    elif spec["kind"] == "case_dir":                       # map_exposure(case_dir, stub)
        data = entry(Path(args.input), Path(args.text) if args.text else None)
    elif spec["kind"] == "email":                          # discover(subject, sender, attach, body) from a .json or .eml-ish text
        p = Path(args.input); raw = p.read_text(encoding="utf-8", errors="replace")
        if p.suffix == ".json":
            e = json.loads(raw); r = entry(e.get("subject", ""), e.get("sender") or e.get("from", ""), e.get("attach") or e.get("attachments", ""), e.get("body", ""))
        else:
            r = entry(p.stem, "", "", raw)
        data = r if isinstance(r, dict) else {"duty": None, "raw": r}
    elif organ == "posture_fit":                           # posture_fit(case_id, stems)
        stems = [x for x in (args.stems or "").split(",") if x] or ["motion_to_strike", "motion_to_compel", "motion_for_judgment_on_pleadings"]
        data = entry(args.input, stems)
    else:                                                  # decide_appeal_vehicle(case_id)
        data = entry(args.input)
    meta["lane"] = lane; meta["model"] = model; meta["seconds"] = round(meta["seconds"], 1)
    return data, meta


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    s = sub.add_parser("soul"); s.add_argument("organ")
    r = sub.add_parser("run"); r.add_argument("organ"); r.add_argument("input")
    r.add_argument("--type", default="complaint"); r.add_argument("--court", default="")
    r.add_argument("--lane", default=""); r.add_argument("--model", default=""); r.add_argument("--max-tokens", type=int, default=6000)
    r.add_argument("--out", default=""); r.add_argument("--text", default="", help="rebuttal_gate: adverse text file")
    r.add_argument("--stems", default="", help="posture_fit: comma list of motion stems")
    a = ap.parse_args()
    if a.cmd == "list":
        for k, v in ORGANS.items():
            print(f"{k:16} {v['module']:32} {v['describe']}")
        return 0
    if a.cmd == "soul":
        print(soul_for(a.organ)); return 0
    if a.organ not in ORGANS:
        print("unknown organ", a.organ, "— see `list`"); return 1
    data, meta = run_via_organ(a.organ, a)
    out = Path(a.out) if a.out else Path(a.input).with_suffix(f".{a.organ}.seat.json")
    side = {"organ": a.organ, **meta, "verdict": data.get(ORGANS[a.organ]["verdict_key"]), "result": data}
    out.write_text(json.dumps(side, indent=1), encoding="utf-8")
    print(json.dumps(data, indent=1)[:4000])
    print(f"\n[seat] {a.organ} on {meta['model']}@{meta['lane']} {meta['calls']} call(s) in {meta['seconds']}s -> {out}", file=sys.stderr)
    return 0 if data.get(ORGANS[a.organ]["verdict_key"]) in ("PASS", "FIT", "OK") else 2


if __name__ == "__main__":
    sys.exit(main())
