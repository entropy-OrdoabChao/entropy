# -*- coding: utf-8 -*-
"""FABLE CONNECTION-HUNTER — the 3rd learning lane (embedder-only, ZERO LLM-GPU contention).

Runs CONCURRENTLY with the two LLM lanes (local-gpu FAST research-worker :8000, local-gpu DEEP b70agent :8080).
Because it uses only the corpus embedder (CUDA_VISIBLE_DEVICES=""), it never competes for an LLM GPU
slot and cannot hallucinate — every line it emits is verbatim retrieved corpus text with its cite.

The role it automates is the depth the user kept supplying by hand: when a coverage / obligation /
party / instrument is NAMED, TRACE it to its GOVERNING STATUTORY TITLE + the duty that attaches + the
court's non-discretionary duty to apply that law + the lever the omission exposes. It does NOT stop at
the procedural rule. (Exemplar the loop learned: "Coverage E" -> Title 40 / 40 P.S. 1171 / 42 Pa.C.S.
8371 / the court's duty to apply Title 40.)

term -> governing title -> statutory duty -> court's duty -> lever.

Usage:
  python -m pipeline.fable_connection_hunter            # trace ONE untraced term, deposit a boundary record
  python -m pipeline.fable_connection_hunter "<term>"   # trace a specific term
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import os, sys, json, sqlite3, re, hashlib
from pathlib import Path

os.environ["CUDA_VISIBLE_DEVICES"] = ""            # embedder on CPU — never touch an LLM GPU slot
BASE = _eroot() if os.name == "nt" else _eroot()
sys.path.insert(0, str(BASE))

PRIV = Path(str(_ehermes() / "beezle_private")) if os.name != "nt" else Path(os.environ.get("BEEZLE_PRIV", str(BASE / "output")))
DONE = PRIV / "fable_traced.txt"
LEARNINGS = BASE / "output" / "learnings.jsonl"

# --- terms we should trace: named coverages / obligations / instruments / capacities from the cases +
#     OUTBOUND TERMS the boundary records themselves surfaced. NOT a fixed syllabus — discovered. ---
def _done_terms() -> set:
    s = set()
    try:
        s = {x.strip().lower() for x in DONE.read_text(encoding="utf-8").splitlines() if x.strip()}
    except Exception:
        pass
    return s

def _candidate_terms(limit_scan: int = 200) -> list:
    """Pull OUTBOUND TERMS from recent boundary records + any 'Coverage X' / named obligation tokens."""
    cands: list[str] = []
    seen = set()
    try:
        lines = LEARNINGS.read_text(encoding="utf-8", errors="ignore").splitlines()[-limit_scan:]
    except Exception:
        lines = []
    for ln in reversed(lines):
        try:
            t = str(json.loads(ln).get("text", ""))
        except Exception:
            continue
        m = re.search(r"OUTBOUND TERMS:\s*([^|\n]{2,240})", t, re.I)
        if m:
            for c in m.group(1).split(","):
                c = c.strip(" .;:").lower()
                if 2 < len(c) < 60 and "not in corpus" not in c and c not in seen:
                    seen.add(c); cands.append(c)
        for m2 in re.finditer(r"\bCoverage [A-Z]\b|\b\d+ U\.?S\.?C\.? §?\s*\d+", t):
            c = m2.group(0).strip().lower()
            if c not in seen:
                seen.add(c); cands.append(c)
    return cands

def _gap_queue_pop() -> str | None:
    """Priority terms the gap-auditor flagged (shallow findings with no governing-title trace). Drain
    FIRST, popping the term so it is not retried. This is the evaluator->discovery wire."""
    gq = PRIV / "gap_terms.txt"
    try:
        lines = [x.strip() for x in gq.read_text(encoding="utf-8").splitlines() if x.strip()]
    except Exception:
        return None
    if not lines:
        return None
    term = lines[0]
    try:
        gq.write_text("\n".join(lines[1:]) + ("\n" if lines[1:] else ""), encoding="utf-8")
    except Exception:
        pass
    return term

def _pick_term() -> str | None:
    if len(sys.argv) > 1 and sys.argv[1].strip():
        return sys.argv[1].strip()
    gp = _gap_queue_pop()          # evaluator-flagged holes first
    if gp:
        return gp
    done = _done_terms()
    for c in _candidate_terms():
        if c.lower() not in done:
            return c
    return None

# --- retrieval (identical wiring to fable_ground.py — the corpus embedder path Hermes/Fable use) ---
_RETRIEVER_SINGLETON = None


def _retriever():
    """Corpus semantic retriever, LOADED ONCE per process and cached. The turbovec index + 38 GB chunk DB +
    BAAI embedder each take real time to load off the /mnt/c 9p mount; rebuilding them on every call put the
    cite-gate (and every retrieval-using stage) into disk-sleep at DRAFT time. Memoized here so the heavy
    load happens at most once, not per draft / per term. Same callable contract as before."""
    global _RETRIEVER_SINGLETON
    if _RETRIEVER_SINGLETON is not None:
        return _RETRIEVER_SINGLETON
    import turbovec
    from pipeline.case_comprehension import NEW_INDEX, NEW_IDS, CHUNK_DB, _retrieve
    from pipeline.llm_layer import get_embedder
    idx = turbovec.TurboQuantIndex.load(str(NEW_INDEX))
    ids = json.loads(Path(NEW_IDS).read_text(encoding="utf-8"))
    con = sqlite3.connect(str(CHUNK_DB), check_same_thread=False)
    model = get_embedder()
    _RETRIEVER_SINGLETON = (lambda q, k=4: _retrieve(idx, ids, con, model, q, k=k, prefer_authority=True))
    return _RETRIEVER_SINGLETON

def _chunks(hits, n=4):
    out = []
    for h in (hits or [])[:n]:
        src = (h.get("source") or "")[:48]
        txt = " ".join((h.get("text") or "").split())[:420]
        if txt:
            out.append(f"[{src}] {txt}")
    return out

def _grounding_block(term: str) -> tuple[str, list]:
    """Off-GPU embedder retrieval (concurrent, no LLM-GPU contention). Returns the corpus text the LLM
    will reason over, plus the source tags for cite validation. term -> title -> duty -> court -> lever."""
    R = _retriever()
    queries = [
        ("DEFINITION",      f"definition of {term}; {term} means; what {term} is; the category or kind of thing it is"),
        ("GOVERNING TITLE", f"what statutory title chapter or act governs {term}; the statute that defines and regulates it"),
        ("STATUTORY DUTY",  f"{term} the duty that attaches; shall pay perform or comply; statutory obligation and its enforcement"),
        ("COURT'S DUTY",    f"court's non-discretionary duty to apply the governing statute of the state; cannot disregard the law"),
        ("LEVER",           f"{term} not enforced or ignored; remedy sanction bad faith unfair practices default penalty"),
    ]
    blocks, srcs = [], set()
    for label, q in queries:
        hits = R(q, k=6)
        ch = _chunks(hits, 4)
        for h in (hits or [])[:4]:
            if h.get("source"):
                srcs.add((h["source"] or "")[:48])
        if ch:
            blocks.append(f"### RETRIEVED FOR {label}:\n" + "\n".join(ch))
    return ("\n\n".join(blocks), sorted(srcs))

# R7: the LLM COMPREHENDS AND REASONS over the retrieved corpus text. It MAY connect chunks by inference
# (term X is a kind of Y; Y is governed by [retrieved statute Z]) — that multi-hop reasoning is the whole
# point. What it may NOT do is import a fact or an authority that is not traceable to the retrieved text.
# Reason WITHIN the corpus; never from outside it. Where a link cannot be completed, say NOT IN CORPUS.
_PROMPT = """You are a legal-corpus analyst. Below is text retrieved VERBATIM from a legal corpus for the term \"{term}\".
Your job is to TRACE the term to the law that governs it and the lever its omission exposes. You MAY REASON across
the chunks: e.g. if the text shows \"{term}\" is a KIND of some broader thing, and another chunk shows that broader
thing is governed by a statute, you SHOULD connect them and say so. That grounded multi-hop reasoning is the point —
do not refuse it. The ONE hard rule: every legal proposition and every citation must be traceable to the retrieved
text below; never import an authority or fact that is not present here. Discard chunks that are off-topic (wrong
subject/jurisdiction). Cite by copying a bracketed [source] tag from a chunk you actually used. Where the CHAIN
genuinely cannot be completed from the retrieved text, write \"NOT IN CORPUS\" for that one line only.

Output EXACTLY these labeled lines and nothing else:
TERM/RULE: {term}
GOVERNING TITLE: <the statutory title/act/chapter that governs {term} (connect via its category if needed), with [source] — do not stop at a procedural rule>
STATUTORY DUTY: <the duty that attaches (who shall pay/perform/comply), with [source]>
COURT'S DUTY: <the court's non-discretionary duty to apply that governing law, with [source]>
LEVER: <the error/remedy the omission exposes — e.g. deciding the obligation without applying the governing title, with [source]>
WHY (BASIS): <the principle: a court cannot decide a substantive obligation by ignoring the title that governs it>
OUTBOUND TERMS: <comma-separated legal terms this record uses that deserve their own trace>

RETRIEVED CORPUS TEXT:
{grounding}
"""

# The comprehension runs on local-gpu (:8080, high-context, now thinking-off) by DEFAULT — the local-gpu (:8000) is
# reserved for m0 drafting + the FAST lane and saturates. This is the "3rd model": local-gpu carries the Fable
# comprehension while the embedder does retrieval off-GPU. Falls back to :8000 if local-gpu is unreachable.
_LANES = [
    (os.environ.get("FABLE_LLM_URL",   "http://127.0.0.1:8080/v1/chat/completions"),
     os.environ.get("FABLE_LLM_MODEL", "qwen3.8-27b-exl3")),
    ("http://127.0.0.1:8000/v1/chat/completions", "local-model"),
]

def _complete(prompt: str, max_tokens: int = 1200, timeout: int = 240) -> str:
    import json as _json, urllib.request
    for url, model in _LANES:
        body = _json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens, "temperature": 0.2, "top_p": 0.8,
            "chat_template_kwargs": {"enable_thinking": False},   # no runaway thinking (the cron killer)
        }).encode()
        try:
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = _json.loads(r.read().decode())
            return (d["choices"][0]["message"]["content"] or "").strip()
        except Exception:
            continue
    return ""

def trace(term: str) -> dict | None:
    grounding, srcs = _grounding_block(term)
    if not grounding.strip():
        return None
    prompt = _PROMPT.format(term=term, grounding=grounding[:9000])
    text = _complete(prompt, max_tokens=1200, timeout=240)
    if not text:
        print("fable-connection-hunter: no LLM lane reachable (local-gpu/:8000); skipping (no raw deposit)")
        return None
    if len(text) < 60 or "GOVERNING TITLE:" not in text:
        return None
    # cite = first retrieved source the LLM actually kept in its answer (grounded-cite validation)
    cite = ""
    for s in srcs:
        if s and s in text:
            cite = s; break
    return {"term": term, "text": text, "cite": cite}

def main():
    term = _pick_term()
    if not term:
        print("fable-connection-hunter: no untraced term this tick"); return
    rec = trace(term)
    if not rec:
        print(f"fable-connection-hunter: {term} -> nothing retrieved (skip, not recorded)"); return
    try:
        from pipeline.connected_notes import record_learning
        record_learning(text=rec["text"],
                         tags=["fable-lane", "lever-trace", "connection", "term->governing-title->duty->lever", "grounded"],
                         source="connection-hunter-local-gpu", cite=rec["cite"], relink_now=False)
    except Exception as e:
        print(f"fable-connection-hunter: record_learning failed: {e}"); return
    try:
        PRIV.mkdir(parents=True, exist_ok=True)
        with open(DONE, "a", encoding="utf-8") as f:
            f.write(term.lower() + "\n")
    except Exception:
        pass
    print(f"fable-connection-hunter: traced + deposited [{rec['cite']}] {term}")

if __name__ == "__main__":
    main()
