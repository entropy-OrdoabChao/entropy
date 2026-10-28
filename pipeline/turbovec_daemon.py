#!/usr/bin/env python3
"""turbovec_daemon.py — ONE resident retrieval process for the whole repo (B-turbovec-daemon).

Loads torch + bge-base + the two turbovec indices ONCE and serves every m2 bot / relay-room / m1 process over a
local socket, so no pipeline process imports torch again (2026-10-23: dxg bridge wedged, every torch import hung
in D state for 6 h).  Clients use pipeline/retrieval_client.py (torch-free; FTS-only sqlite fallback when down).

PROTOCOL  newline-delimited JSON, one request per line, one response line.  Unix socket (default
/tmp/turbovec.sock, env TURBOVEC_SOCK) and/or TCP (env TURBOVEC_TCP=127.0.0.1:18031).
  {"op":"health"}
  {"op":"semantic","query":"...", "k":6, "source_prefix":"usc_t15"|["a","b"]|null}
  {"op":"fts_phrase","phrase":"...", "source_prefix":null, "limit":40}
  {"op":"chunk","chunk_id":"..."}
Responses: {"ok":true, ...payload} or {"ok":false,"error":"..."}; semantic/fts carry "mode" =
  embed | fts_fallback (daemon degraded or TURBOVEC_NO_EMBED=1).

GPU/EMBED GUARD  the embedder runs in a worker thread; main thread waits EMBED_TIMEOUT_S (60).  On timeout the
daemon marks itself DEGRADED (answers FTS-only, logs output/audit/turbovec_daemon.log) and never spawns nvidia-smi.
S-turbovec-degraded-recovery (2026-10-24): while degraded a background prober runs ONE tiny embed under the same
timeout every TURBOVEC_PROBE_INTERVAL_S (60) and flips mode back to 'embed' on the first in-time probe (CPU
starvation is transient); a load failure stays degraded until an operator restart.  CUDA is hidden (CUDA_VISIBLE_DEVICES='')
unless TURBOVEC_DEVICE=cuda — the m2 embedder is CPU by design (never competes with a lane).  Batches are capped
(MAX_BATCH queries, 400 chars each).
ENV  TURBOVEC_NO_EMBED=1 (FTS-only, torch never imported) · TURBOVEC_DIR · TURBOVEC_SOCK · TURBOVEC_TCP ·
     TURBOVEC_LOG · TURBOVEC_EMBED_TIMEOUT_S · TURBOVEC_LOAD_TIMEOUT_S · TURBOVEC_DEVICE
"""
from __future__ import annotations
import os, sys, json, time, socket, signal, threading, socketserver, traceback
from pathlib import Path

if os.environ.get("TURBOVEC_DEVICE", "cpu") != "cuda":
    os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")          # S-cpu-jobs-hide-cuda: torch must not probe dxg
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pipeline import turbovec_fts as F                           # noqa: E402  (torch-free)

SOCK = os.environ.get("TURBOVEC_SOCK", "/tmp/turbovec.sock")
TCP = os.environ.get("TURBOVEC_TCP", "")                          # "127.0.0.1:18031" to also listen on TCP
NO_EMBED = os.environ.get("TURBOVEC_NO_EMBED", "") == "1"
LOG = Path(os.environ.get("TURBOVEC_LOG", str(ROOT / "output" / "audit" / "turbovec_daemon.log")))
EMBED_TIMEOUT_S = float(os.environ.get("TURBOVEC_EMBED_TIMEOUT_S", "60"))
LOAD_TIMEOUT_S = float(os.environ.get("TURBOVEC_LOAD_TIMEOUT_S", "900"))
MAX_BATCH = int(os.environ.get("TURBOVEC_MAX_BATCH", "12"))      # = analysis_runner.MAX_Q (12 queries per cell)
MAX_K = 200
QPREFIX = "Represent this sentence for searching relevant passages: "
WARM_QUERY = "all defendants who have been properly joined and served must join in or consent to the removal"
T0 = time.time()

_STATE = {"mode": "fts_only" if NO_EMBED else "loading", "embed_loaded": False, "requests": 0, "errors": 0,
          "last_embed_ms": None, "warm_ms": None, "degraded_reason": None, "pid": os.getpid(),
          "degrade_events": 0, "recover_events": 0, "last_probe_at": None, "probe_failures": 0}   # S-turbovec-degraded-recovery
_EMB = {}                     # idx, ids, dlt, dids, model, np
_EMB_LOCK = threading.Lock()  # one embed/search at a time (index + model are not thread-safe)
_LOG_LOCK = threading.Lock()
PROBE_INTERVAL_S = float(os.environ.get("TURBOVEC_PROBE_INTERVAL_S", "60"))
_PROBE = {"thread": None, "lock": threading.Lock(), "stop": threading.Event()}


def log(msg: str):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{_STATE['mode']}] {msg}"
    with _LOG_LOCK:
        try:
            LOG.parent.mkdir(parents=True, exist_ok=True)
            with LOG.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
    print(line, flush=True)


def degrade(reason: str):
    if _STATE["mode"] != "degraded":
        _STATE["mode"] = "degraded"
        _STATE["degraded_reason"] = reason
        _STATE["degrade_events"] += 1
        log(f"DEGRADED: {reason} — answering FTS-only; probing the embedder every {PROBE_INTERVAL_S:.0f}s to recover")
        _start_prober()


# ---------------------------------------------------------------- S-turbovec-degraded-recovery (2026-10-24)
# CLASS RULE: a timeout caused by CPU starvation (load 23/16 cores from six OCR jobs) is transient; a sticky 'degraded'
# turned one slow embed into hours of FTS-only answers. The daemon now re-probes the embedder under the SAME timeout
# every PROBE_INTERVAL_S and clears degraded on the first probe that finishes in time. A load failure (model never
# loaded) is not transient: the prober leaves that alone (restart = operator decision, as before).
def _probe_embed_once(timeout_s: float = None) -> bool:
    """One tiny embed under the production timeout. True = finished in time without error."""
    timeout_s = EMBED_TIMEOUT_S if timeout_s is None else timeout_s
    if not _EMB.get("model"):
        return False
    box = {}

    def work():
        try:
            with _EMB_LOCK:
                _EMB["model"].encode([QPREFIX + WARM_QUERY[:400]], normalize_embeddings=True)
                box["ok"] = True
        except Exception as e:
            box["err"] = repr(e)
    t0 = time.time()
    th = threading.Thread(target=work, name="tv-probe", daemon=True); th.start(); th.join(timeout_s)
    _STATE["last_probe_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    if th.is_alive() or "err" in box:
        return False
    _STATE["last_embed_ms"] = int((time.time() - t0) * 1000)
    return True


def recover(note: str = ""):
    """degraded → embed: clear the reason, count the event, log 'recovered'."""
    if _STATE["mode"] == "degraded":
        _STATE["mode"] = "embed"
        _STATE["degraded_reason"] = None
        _STATE["recover_events"] += 1
        _STATE["probe_failures"] = 0
        log(f"recovered: embedder answered a probe in {_STATE['last_embed_ms']} ms {note}".rstrip())


def _prober_loop(interval_s: float, probe_fn):
    while not _PROBE["stop"].is_set():
        if _STATE["mode"] != "degraded":
            break                                                     # nothing to recover; thread exits, next degrade restarts it
        if not _STATE["embed_loaded"]:
            break                                                     # load failure: not transient; operator restart
        if _PROBE["stop"].wait(interval_s):
            break
        if _STATE["mode"] != "degraded":
            break
        try:
            ok = probe_fn()
        except Exception as e:
            ok = False; log(f"probe raised {e!r}")
        if ok:
            recover(f"(after {_STATE['probe_failures']} failed probes)")
            break
        _STATE["probe_failures"] += 1
        log(f"probe #{_STATE['probe_failures']} still over {EMBED_TIMEOUT_S:.0f}s — staying degraded")
    with _PROBE["lock"]:
        if _PROBE["thread"] is threading.current_thread():
            _PROBE["thread"] = None


def _start_prober(interval_s: float = None, probe_fn=None):
    """Start (or reuse) the single background prober. Idempotent: a live prober is kept."""
    interval_s = PROBE_INTERVAL_S if interval_s is None else interval_s
    probe_fn = probe_fn or _probe_embed_once
    with _PROBE["lock"]:
        th = _PROBE["thread"]
        if th is not None and th.is_alive():
            return th
        _PROBE["stop"].clear()
        th = threading.Thread(target=_prober_loop, args=(interval_s, probe_fn), name="tv-prober", daemon=True)
        _PROBE["thread"] = th
        th.start()
        return th


# ---------------------------------------------------------------- embedder (loaded once, in a worker thread)
def _load_embedder():
    try:
        import numpy as np
        import turbovec
        from sentence_transformers import SentenceTransformer
        tv = F.TV_DIR
        idx = turbovec.TurboQuantIndex.load(str(tv / "chunk_index_b4.idx")); ids = json.loads((tv / "chunk_ids.json").read_text())
        dlt = turbovec.TurboQuantIndex.load(str(tv / "new_chunks_b4.tv")); dids = json.loads((tv / "new_chunk_ids.json").read_text())
        model = SentenceTransformer("BAAI/bge-base-en-v1.5", device=os.environ.get("TURBOVEC_DEVICE", "cpu"))
        _EMB.update(idx=idx, ids=ids, dlt=dlt, dids=dids, model=model, np=np)
        t1 = time.time()
        # WARM-UP (2026-10-24): the first real search pages the quantised index in (~29 s cold); do it HERE, before
        # health reports mode=embed, so the first client call never eats it (client timeout is 60 s).
        warm = _embed_search([WARM_QUERY], 6 * 30)
        for _, cid, _ in warm[:3]:
            F.chunk(cid, timeout_s=10)
        _STATE["embed_loaded"] = True
        _STATE["warm_ms"] = int((time.time() - t1) * 1000)
        if _STATE["mode"] == "loading":
            _STATE["mode"] = "embed"
        elif _STATE["mode"] == "degraded":                            # load-watch degraded us but the load finished: probe back
            _start_prober()
        log(f"embedder loaded in {t1 - T0:.1f}s, warm-up {_STATE['warm_ms']} ms ({len(warm)} cands) (main={len(ids)} delta={len(dids)})")
    except Exception as e:
        degrade(f"embedder load failed: {e!r}")


def _start_loader():
    th = threading.Thread(target=_load_embedder, name="tv-loader", daemon=True)
    th.start()

    def watch():
        th.join(LOAD_TIMEOUT_S)
        if th.is_alive():
            degrade(f"embedder load exceeded {LOAD_TIMEOUT_S:.0f}s (dxg wedge?)")
    threading.Thread(target=watch, name="tv-load-watch", daemon=True).start()


def _embed_search(queries: list[str], pool: int):
    """Runs under _EMB_LOCK in a worker thread; returns [(score, chunk_id, qi)] or raises."""
    np, model = _EMB["np"], _EMB["model"]
    qv = model.encode([QPREFIX + q[:400] for q in queries], normalize_embeddings=True).astype("float32")
    cand = []
    for index, idlist in ((_EMB["idx"], _EMB["ids"]), (_EMB["dlt"], _EMB["dids"])):
        sc, pos = index.search(qv, pool)
        for qi in range(len(queries)):
            for s_, p_ in zip(np.atleast_2d(sc)[qi], np.atleast_2d(pos)[qi]):
                p_ = int(p_)
                if 0 <= p_ < len(idlist):
                    cand.append((float(s_), idlist[p_], qi))
    return cand


def semantic(query, k: int = 6, source_prefix=None) -> dict:
    queries = [q for q in (query if isinstance(query, list) else [query]) if isinstance(q, str) and q.strip()][:MAX_BATCH]
    if not queries:
        return {"error": "query required"}
    k = max(1, min(int(k or 6), MAX_K))
    if _STATE["mode"] != "embed":
        r = F.fts_semantic_fallback(" ".join(queries), k, source_prefix)
        r["daemon_mode"] = _STATE["mode"]
        return r
    pool = k * (30 if source_prefix else 4)
    box = {}

    def work():
        try:
            with _EMB_LOCK:
                box["cand"] = _embed_search(queries, pool)
        except Exception as e:
            box["err"] = repr(e)
    t0 = time.time()
    th = threading.Thread(target=work, name="tv-embed", daemon=True)
    th.start(); th.join(EMBED_TIMEOUT_S)
    if th.is_alive():
        degrade(f"embed call exceeded {EMBED_TIMEOUT_S:.0f}s for {queries[0][:60]!r}")
        r = F.fts_semantic_fallback(" ".join(queries), k, source_prefix); r["daemon_mode"] = "degraded"; return r
    _STATE["last_embed_ms"] = int((time.time() - t0) * 1000)
    if "err" in box:
        _STATE["errors"] += 1
        r = F.fts_semantic_fallback(" ".join(queries), k, source_prefix); r["daemon_mode"] = "embed_error"; r["embed_error"] = box["err"]; return r
    cand = sorted(box["cand"], key=lambda t: -t[0])
    qhits, seen_id, seen_txt, out = {}, set(), set(), []
    for s_, cid, qi in cand:
        qhits.setdefault(cid, set()).add(qi)
    for s_, cid, qi in cand:
        if cid in seen_id:
            continue
        seen_id.add(cid)
        c = F.chunk(cid, timeout_s=10)
        if c.get("error") or not c.get("text"):
            continue
        if not F._prefix_ok(c["source"], source_prefix):
            continue
        part = c.get("part")
        if part not in F.HOLDING_PARTS:
            continue
        key = c["text"][:80]
        if key in seen_txt:
            continue
        seen_txt.add(key)
        out.append({"chunk_id": cid, "source": c["source"], "part": part or "-", "score": round(s_, 3), "text": c["text"][:1200],
                    "q": sorted(qhits.get(cid, ()))})
        if len(out) >= k * 3:
            break
    pick, used = [], set()
    for qi in range(len(queries)):                                  # quota: each query's best hit first
        h = next((h for h in out if qi in h["q"] and h["chunk_id"] not in used), None)
        if h is not None and len(pick) < k:
            pick.append(h); used.add(h["chunk_id"])
    pick += [h for h in out if h["chunk_id"] not in used][:max(0, k - len(pick))]
    pick.sort(key=lambda h: -h["score"])
    return {"hits": pick, "mode": "embed", "daemon_mode": "embed", "embed_ms": _STATE["last_embed_ms"]}


MAX_EMBED_TEXTS = int(os.environ.get("TURBOVEC_MAX_EMBED_TEXTS", "64"))


def embed(texts, is_query: bool = False) -> dict:
    """Raw bge vectors (normalised) for callers that re-rank locally (analysis_runner._pull_cites_tv :470-472,
    m0_authority_spine._family_scan :763).  Batch capped at MAX_EMBED_TEXTS x 1200 chars; same watchdog as semantic."""
    texts = [t for t in (texts if isinstance(texts, list) else [texts]) if isinstance(t, str)]
    if not texts:
        return {"error": "texts required"}
    if len(texts) > MAX_EMBED_TEXTS:
        return {"error": f"batch too large ({len(texts)} > {MAX_EMBED_TEXTS}); chunk the request"}
    if _STATE["mode"] != "embed":
        return {"error": f"embedder unavailable (daemon mode={_STATE['mode']})", "daemon_mode": _STATE["mode"], "vectors": None}
    box = {}

    def work():
        try:
            with _EMB_LOCK:
                v = _EMB["model"].encode([(QPREFIX if is_query else "") + t[:1200] for t in texts], normalize_embeddings=True)
                box["v"] = [[round(float(x), 6) for x in row] for row in v]
        except Exception as e:
            box["err"] = repr(e)
    t0 = time.time()
    th = threading.Thread(target=work, name="tv-embed-raw", daemon=True); th.start(); th.join(EMBED_TIMEOUT_S)
    if th.is_alive():
        degrade(f"embed(texts={len(texts)}) exceeded {EMBED_TIMEOUT_S:.0f}s")
        return {"error": "embed timed out; daemon degraded", "daemon_mode": "degraded", "vectors": None}
    _STATE["last_embed_ms"] = int((time.time() - t0) * 1000)
    if "err" in box:
        _STATE["errors"] += 1
        return {"error": f"embed failed: {box['err']}", "vectors": None}
    return {"vectors": box["v"], "dim": len(box["v"][0]), "n": len(box["v"]), "embed_ms": _STATE["last_embed_ms"]}


def health() -> dict:
    return {"ok": True, "mode": _STATE["mode"], "embed_loaded": _STATE["embed_loaded"], "degraded_reason": _STATE["degraded_reason"],
            "degrade_events": _STATE["degrade_events"], "recover_events": _STATE["recover_events"],
            "probe_failures": _STATE["probe_failures"], "last_probe_at": _STATE["last_probe_at"], "probe_interval_s": PROBE_INTERVAL_S,
            "uptime_s": round(time.time() - T0, 1), "requests": _STATE["requests"], "errors": _STATE["errors"],
            "last_embed_ms": _STATE["last_embed_ms"], "warm_ms": _STATE["warm_ms"], "pid": _STATE["pid"], "sock": SOCK, "tcp": TCP or None,
            "chunk_db": str(F.CHUNK_DB), "no_embed": NO_EMBED}


def dispatch(req: dict) -> dict:
    op = req.get("op")
    if op == "health":
        return health()
    if op == "semantic":
        return semantic(req.get("query"), req.get("k", 6), req.get("source_prefix"))
    if op == "fts_phrase":
        return F.fts_phrase(req.get("phrase", ""), req.get("source_prefix"), int(req.get("limit", 40)))
    if op == "chunk":
        return F.chunk(req.get("chunk_id", ""))
    if op == "embed":
        return embed(req.get("texts"), bool(req.get("is_query", False)))
    if op == "sleep":                                                # test hook: client timeout behaviour
        time.sleep(float(req.get("s", 1))); return {"slept": req.get("s", 1)}
    return {"error": f"unknown op {op!r}"}


class Handler(socketserver.StreamRequestHandler):
    timeout = 60

    def handle(self):
        try:
            line = self.rfile.readline(1_000_000)
            if not line:
                return
            _STATE["requests"] += 1
            try:
                req = json.loads(line.decode("utf-8", "replace"))
                resp = dispatch(req if isinstance(req, dict) else {})
                if "error" in resp and len(resp) <= 3:
                    resp.setdefault("ok", False)
                else:
                    resp.setdefault("ok", True)
            except Exception as e:
                _STATE["errors"] += 1
                resp = {"ok": False, "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-800:]}
            self.wfile.write((json.dumps(resp, default=str) + "\n").encode("utf-8"))
        except Exception as e:
            log(f"handler error: {e!r}")


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True; allow_reuse_address = True


class TcpServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True; allow_reuse_address = True


def main():
    servers = []
    try:
        if os.path.exists(SOCK):
            os.unlink(SOCK)
        us = UnixServer(SOCK, Handler); os.chmod(SOCK, 0o666); servers.append(us)
        _STATE["sock_ino"] = os.stat(SOCK).st_ino       # ownership: a superseded daemon must not unlink its successor's socket
    except Exception as e:
        log(f"unix socket {SOCK} unavailable: {e!r}")
    if TCP:
        host, _, port = TCP.rpartition(":")
        servers.append(TcpServer((host or "127.0.0.1", int(port)), Handler))
    if not servers:
        log("no listener could be opened"); sys.exit(2)
    log(f"turbovec daemon up pid={os.getpid()} sock={SOCK} tcp={TCP or '-'} no_embed={NO_EMBED} py={sys.executable}")
    if not NO_EMBED:
        _start_loader()

    def stop(*_):
        log("stopping")
        for s in servers:
            threading.Thread(target=s.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)
    ths = [threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True) for s in servers]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    for s in servers:
        s.server_close()
    try:                                                 # 2026-10-24: an old daemon's SIGTERM removed the NEW daemon's socket
        if os.path.exists(SOCK) and os.stat(SOCK).st_ino == _STATE.get("sock_ino"):
            os.unlink(SOCK)
    except OSError:
        pass
    log("stopped")


if __name__ == "__main__":
    main()
