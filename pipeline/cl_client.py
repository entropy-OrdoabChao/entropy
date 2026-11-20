#!/usr/bin/env python3
"""pipeline/cl_client.py — THE single CourtListener client for every Beezle process.

WHY (item cl_client, backlog 'ROOM — topic-pull burned 24.5 min on CourtListener HTTP 429'):
CourtListener allows ~5 requests/min on this key and the key is shared by EVERY process
(topic_pull, fetch_bot's CL rung via case_fetch, caselaw_verify, cite_authority_check,
authority_discovery, ingest_case, docvault_pull_runner) plus any browser window the user has
open on the same account.  Eight call sites each ran their OWN private pacing/retry loop, so
they raced each other into 429s and then slept through 3-attempt back-off ladders — r4's
topic-pull burned 24.5 min that way.  A per-process rate limiter cannot fix a per-ACCOUNT
quota.  This module is the class fix:

  (a) ONE token bucket persisted on disk (output/audit/cl_bucket.json, fcntl-locked) so the
      pacing is shared by every process.  Default 5 req/min; env CL_RPM overrides.
      A caller RESERVES its slot under the lock, then sleeps outside the lock -> N processes
      queue into distinct slots instead of all blocking on one holder.
  (b) Retry-After is honored exactly ONCE, and only when the wait is <= CL_MAX_WAIT_S (60 s).
      A second 429 is TERMINAL for that call: {"ok": False, "reason": "cl_rate_limited",
      "retry_after": n}.  There is NO retry loop anywhere -- the caller logs one line and
      moves on with zero results.  A room must never wait on CourtListener.
  (c) 24 h on-disk response cache at output/cache/courtlistener/<sha>.json keyed by
      sha256(path + sorted params).  HIT is logged and costs no bucket slot.
  (d) One receipt line per call appended to output/audit/cl_calls.log:
      ts  caller  path  status  cache=HIT|MISS  wait=<s>  [retry_after=<s>]
  (e) The API token comes from the SAME places the call sites already use (first hit wins):
      env COURTLISTENER_API_KEY, env COURTLISTENER_TOKEN,
      ~/.beezle_cache/courtlistener.token, ~/.courtlistener_token,
      <home>/.courtlistener.token, then config.yaml.
      The token is NEVER printed, logged, cached or echoed in a result dict.

API
  get(path, params=None, caller=None, timeout=..., method="GET", data=None) -> dict
  search(q, **kw) -> dict          # /search/ (type=o by default)
  opinion(opinion_id) -> dict      # /opinions/<id>/
  cluster(cluster_id) -> dict      # /clusters/<id>/
  citation_lookup(text) -> dict    # POST /citation-lookup/  (free-tier endpoint)

`path` may be a bare path ("/search/"), a path with a query string, or a FULL
courtlistener.com URL -- the query string is parsed into params either way, so a call site
that already builds a URL converts with a one-line change.

Result shape (never raises):
  ok    : {"ok": True,  "status": 200, "data": <parsed json>, "cache": "HIT"|"MISS",
           "wait_s": <float>, "path": <path>}
  429   : {"ok": False, "reason": "cl_rate_limited", "retry_after": <int>, "status": 429, ...}
  other : {"ok": False, "reason": "http_<code>"|"no_token"|"error: <Type>", "status": <int|0>, ...}

Test seams (monkeypatch these; no network, no clock):
  HTTP   -- callable(method, url, headers, body, timeout) -> (status, bytes, headers_dict)
  _now   -- time.time
  _sleep -- time.sleep
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import fcntl
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------- configuration
ROOT = Path(os.environ.get("BEEZLE_ROOT", str(_eroot())))
BUCKET_PATH = Path(os.environ.get("CL_BUCKET_PATH", str(ROOT / "output" / "audit" / "cl_bucket.json")))
CALLS_LOG = Path(os.environ.get("CL_CALLS_LOG", str(ROOT / "output" / "audit" / "cl_calls.log")))
CACHE_DIR = Path(os.environ.get("CL_CACHE_DIR", str(ROOT / "output" / "cache" / "courtlistener")))

CL_HOST = "www.courtlistener.com"
CL_API = "/api/rest/v4"
BASE = f"https://{CL_HOST}{CL_API}"
UA = os.environ.get("CL_USER_AGENT", "beezle-legal-research (pro se litigation research)")

DEFAULT_RPM = 5
CACHE_TTL_S = int(os.environ.get("CL_CACHE_TTL_S", str(24 * 3600)))
MAX_WAIT_S = float(os.environ.get("CL_MAX_WAIT_S", "60"))      # a Retry-After above this is terminal at once
DEFAULT_TIMEOUT = float(os.environ.get("CL_TIMEOUT_S", "60"))
CACHE_ENABLED = os.environ.get("CL_CACHE", "1") not in ("0", "no", "false")

# test seams
HTTP = None          # callable(method, url, headers, body, timeout) -> (status, bytes, headers)
_now = time.time
_sleep = time.sleep


def rpm() -> int:
    try:
        v = int(float(os.environ.get("CL_RPM", DEFAULT_RPM)))
    except Exception:
        v = DEFAULT_RPM
    return max(1, v)


# ---------------------------------------------------------------- token (never printed)
_TOKEN_CACHE: list = []


def _token_from_config() -> str:
    for cfg in (ROOT / "config.yaml",):
        try:
            t = cfg.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        m = re.search(r'COURTLISTENER(?:_API)?_(?:KEY|TOKEN)["\s:=]+([A-Za-z0-9]{20,})', t)
        if m:
            return m.group(1)
        # the historical case_fetch._cl_key heuristic: a 40-char key near the courtlistener block
        m = re.search(r"courtlistener[^\n]*\n(?:.*\n){0,6}?.*?([A-Za-z0-9]{40})", t, re.I)
        if m:
            return m.group(1)
    return ""


def token() -> str:
    """Resolve the CourtListener API token. NEVER print or log the return value."""
    if _TOKEN_CACHE:
        return _TOKEN_CACHE[0]
    tok = ""
    for var in ("COURTLISTENER_API_KEY", "COURTLISTENER_TOKEN"):
        tok = (os.environ.get(var) or "").strip()
        if tok:
            break
    if not tok:
        for p in (Path.home() / ".beezle_cache" / "courtlistener.token",
                  Path.home() / ".courtlistener_token",
                  ROOT / ".courtlistener.token"):
            try:
                if p.exists():
                    tok = p.read_text(encoding="utf-8", errors="ignore").strip()
                    if tok:
                        break
            except Exception:
                pass
    if not tok:
        tok = _token_from_config().strip()
    _TOKEN_CACHE.append(tok)
    return tok


def token_present() -> bool:
    return bool(token())


# ---------------------------------------------------------------- shared on-disk token bucket
def _bucket_state(fh) -> dict:
    try:
        fh.seek(0)
        return json.loads(fh.read() or "{}") or {}
    except Exception:
        return {}


def _bucket_write(fh, st: dict) -> None:
    fh.seek(0)
    fh.truncate()
    fh.write(json.dumps(st, sort_keys=True))
    fh.flush()
    try:
        os.fsync(fh.fileno())
    except Exception:
        pass


def _with_bucket(fn):
    """Run fn(state) under an exclusive fcntl lock on BUCKET_PATH; persist what it returns."""
    try:
        BUCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    try:
        fh = open(BUCKET_PATH, "a+", encoding="utf-8")
    except Exception:
        return fn({})[0]       # unwritable bucket must never break a call; pacing degrades, nothing else
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        st = _bucket_state(fh)
        out, st = fn(st)
        _bucket_write(fh, st)
        return out
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        fh.close()


def reserve(caller: str = "") -> float:
    """Reserve the next CL slot. Returns the seconds this caller must wait (it does NOT sleep).

    Slots are handed out strictly 60/CL_RPM apart across EVERY process, because next_at lives
    in the locked bucket file.  Reserving under the lock and sleeping outside it is what keeps
    two concurrent processes from both waking into the same slot.
    """
    interval = 60.0 / rpm()

    def _res(st):
        now = _now()
        next_at = max(float(st.get("next_at") or 0.0), now)
        st = {
            "next_at": next_at + interval,
            "rpm": rpm(),
            "interval_s": round(interval, 3),
            "last_reserved_at": round(now, 3),
            "last_caller": caller or "?",
            "reservations": int(st.get("reservations") or 0) + 1,
        }
        return max(0.0, next_at - now), st

    return float(_with_bucket(_res))


def penalize(seconds: float, caller: str = "") -> None:
    """A 429 came back: push the shared bucket forward so every process backs off, not just us."""
    def _pen(st):
        now = _now()
        st = dict(st)
        st["next_at"] = max(float(st.get("next_at") or 0.0), now + max(0.0, float(seconds)))
        st["last_429_at"] = round(now, 3)
        st["last_429_caller"] = caller or "?"
        st["penalties"] = int(st.get("penalties") or 0) + 1
        st.setdefault("rpm", rpm())
        return None, st

    _with_bucket(_pen)


def bucket_snapshot() -> dict:
    def _snap(st):
        return dict(st), st
    return _with_bucket(_snap) or {}


# ---------------------------------------------------------------- 24 h response cache
def cache_key(path: str, params: dict | None) -> str:
    items = sorted((str(k), str(v)) for k, v in (params or {}).items())
    raw = path + "|" + "&".join(f"{k}={v}" for k, v in items)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cache_read(key: str):
    if not CACHE_ENABLED:
        return None
    p = CACHE_DIR / f"{key}.json"
    try:
        if not p.exists():
            return None
        rec = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if (_now() - float(rec.get("ts") or 0)) > CACHE_TTL_S:
        return None
    return rec


def _cache_write(key: str, path: str, status: int, data) -> None:
    if not CACHE_ENABLED:
        return
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_DIR / f".{key}.tmp"
        tmp.write_text(json.dumps({"ts": _now(), "path": path, "status": status, "data": data}),
                       encoding="utf-8")
        tmp.replace(CACHE_DIR / f"{key}.json")
    except Exception:
        pass


# ---------------------------------------------------------------- receipts
def receipt(caller: str, path: str, status, cache: str, wait_s: float, extra: str = "") -> str:
    line = (f"{time.strftime('%Y-%m-%dT%H:%M:%S')}\t{caller or '?'}\t{path}\t{status}\t"
            f"cache={cache}\twait={wait_s:.1f}s" + (f"\t{extra}" if extra else ""))
    try:
        CALLS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(CALLS_LOG, "a", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            fh.write(line + "\n")
            fh.flush()
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    return line


# ---------------------------------------------------------------- caller attribution
def _caller() -> str:
    try:
        import inspect
        here = os.path.abspath(__file__)
        for fr in inspect.stack()[1:]:
            fn = os.path.abspath(fr.filename)
            if fn != here:
                return f"{os.path.basename(fn).rsplit('.', 1)[0]}.{fr.function}"
    except Exception:
        pass
    return "?"


# ---------------------------------------------------------------- HTTP
def _split(path: str, params: dict | None) -> tuple[str, dict]:
    """Accept a bare path, a path+query, or a full CL URL. -> (api path, merged params)."""
    merged = dict(params or {})
    if path.startswith("http://") or path.startswith("https://"):
        u = urllib.parse.urlsplit(path)
        p, q = u.path, u.query
    elif "?" in path:
        p, q = path.split("?", 1)
    else:
        p, q = path, ""
    for k, v in urllib.parse.parse_qsl(q, keep_blank_values=True):
        merged.setdefault(k, v)
    if not p.startswith("/api/"):
        p = CL_API + "/" + p.lstrip("/")
    return p, merged


def _do_http(method: str, url: str, headers: dict, body, timeout: float):
    if HTTP is not None:                      # test seam
        return HTTP(method, url, headers, body, timeout)
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        try:
            payload = e.read()
        except Exception:
            payload = b""
        return e.code, payload, dict(getattr(e, "headers", {}) or {})


def _retry_after(headers: dict, default: float = 60.0) -> float:
    for k, v in (headers or {}).items():
        if str(k).lower() == "retry-after":
            try:
                return max(0.0, float(str(v).strip()))
            except Exception:
                return default
    return default


def get(path: str, params: dict | None = None, caller: str | None = None,
        timeout: float | None = None, method: str = "GET", data: dict | None = None) -> dict:
    """One CourtListener call. Bucketed, cached, receipted. NEVER raises, NEVER retries twice."""
    who = caller or _caller()
    p, qs = _split(path, params)
    key = cache_key(p, dict(qs, **{f"_b_{k}": v for k, v in (data or {}).items()}))

    hit = _cache_read(key)
    if hit is not None:
        receipt(who, p, hit.get("status", 200), "HIT", 0.0)
        return {"ok": True, "status": hit.get("status", 200), "data": hit.get("data"),
                "cache": "HIT", "wait_s": 0.0, "path": p}

    tok = token()
    if not tok:
        receipt(who, p, 0, "MISS", 0.0, "no_token")
        return {"ok": False, "reason": "no_token", "status": 0, "cache": "MISS",
                "wait_s": 0.0, "path": p,
                "note": "NOT CONFIGURED: no CourtListener token in env/token files/config.yaml"}

    url = f"https://{CL_HOST}{p}"
    if qs:
        url += "?" + urllib.parse.urlencode(qs)
    headers = {"Authorization": f"Token {tok}", "User-Agent": UA, "Accept": "application/json"}
    body = None
    if data:
        body = urllib.parse.urlencode(data).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        method = "POST"

    tmo = DEFAULT_TIMEOUT if timeout is None else float(timeout)
    waited = reserve(who)
    if waited > MAX_WAIT_S:
        # The shared bucket was penalized by a long Retry-After (CL daily quota: 18,785 s seen 2026-11-14).
        # Sleeping here would park the caller for hours; a room never waits on CL -> terminal at once.
        receipt(who, p, 0, "SKIP", 0.0, f"cl_rate_limited bucket_wait={int(waited)}s (> {int(MAX_WAIT_S)}s, no wait)")
        return {"ok": False, "reason": "cl_rate_limited", "retry_after": int(waited), "status": 0,
                "path": p, "params": params or {}}
    if waited > 0:
        _sleep(waited)

    status, payload, rhdrs = _do_http(method, url, headers, body, tmo)

    # ---- 429: ONE wait (<= MAX_WAIT_S), then terminal. No loop, ever.
    if status == 429:
        ra = _retry_after(rhdrs)
        penalize(ra, who)
        if ra > MAX_WAIT_S:
            receipt(who, p, 429, "MISS", waited, f"cl_rate_limited retry_after={int(ra)} (> {int(MAX_WAIT_S)}s, no wait)")
            return {"ok": False, "reason": "cl_rate_limited", "retry_after": int(ra), "status": 429,
                    "cache": "MISS", "wait_s": waited, "path": p}
        _sleep(ra)
        waited += ra
        status, payload, rhdrs = _do_http(method, url, headers, body, tmo)
        if status == 429:
            ra2 = _retry_after(rhdrs)
            penalize(ra2, who)
            receipt(who, p, 429, "MISS", waited, f"cl_rate_limited TERMINAL retry_after={int(ra2)}")
            return {"ok": False, "reason": "cl_rate_limited", "retry_after": int(ra2), "status": 429,
                    "cache": "MISS", "wait_s": waited, "path": p}

    if status != 200:
        snippet = ""
        try:
            snippet = (payload or b"").decode("utf-8", "ignore")[:160]
        except Exception:
            pass
        receipt(who, p, status, "MISS", waited, f"http_{status}")
        return {"ok": False, "reason": f"http_{status}", "status": status, "cache": "MISS",
                "wait_s": waited, "path": p, "body": snippet}

    try:
        parsed = json.loads((payload or b"").decode("utf-8", "ignore") if isinstance(payload, bytes) else payload)
    except Exception as e:
        receipt(who, p, status, "MISS", waited, f"bad_json:{type(e).__name__}")
        return {"ok": False, "reason": f"error: {type(e).__name__}", "status": status,
                "cache": "MISS", "wait_s": waited, "path": p}

    _cache_write(key, p, status, parsed)
    receipt(who, p, status, "MISS", waited)
    return {"ok": True, "status": status, "data": parsed, "cache": "MISS",
            "wait_s": waited, "path": p}


# ---------------------------------------------------------------- endpoint helpers
def search(q: str, caller: str | None = None, **kw) -> dict:
    params = {"q": q, "type": kw.pop("type", "o")}
    for k, v in kw.items():
        if k in ("timeout", "caller"):
            continue
        if v in (None, "", []):
            continue
        params[k] = "+".join(str(x) for x in v) if isinstance(v, (list, tuple)) else v
    return get("/search/", params, caller=caller or _caller(), timeout=kw.get("timeout"))


def opinion(opinion_id, caller: str | None = None, **kw) -> dict:
    oid = str(opinion_id).rstrip("/").split("/")[-1]
    return get(f"/opinions/{oid}/", {"format": "json"}, caller=caller or _caller(),
               timeout=kw.get("timeout"))


def cluster(cluster_id, caller: str | None = None, **kw) -> dict:
    cid = str(cluster_id).rstrip("/").split("/")[-1]
    return get(f"/clusters/{cid}/", caller=caller or _caller(), timeout=kw.get("timeout"))


def citation_lookup(text: str, caller: str | None = None, **kw) -> dict:
    """POST /citation-lookup/ — the free-tier endpoint cite_authority_check uses."""
    return get("/citation-lookup/", None, caller=caller or _caller(),
               timeout=kw.get("timeout"), method="POST", data={"text": text})


def raw_text(path: str, params: dict | None = None, caller: str | None = None, **kw) -> str:
    """Compatibility shim for sites that json.loads() a text body: '' on any failure."""
    r = get(path, params, caller=caller or _caller(), timeout=kw.get("timeout"))
    return json.dumps(r.get("data")) if r.get("ok") else ""


if __name__ == "__main__":
    import sys
    if not token_present():
        print("NOT CONFIGURED: no CourtListener token found")
        sys.exit(2)
    r = search(sys.argv[1] if len(sys.argv) > 1 else "fair credit reporting act", court="ca3", timeout=30)
    print(json.dumps({k: v for k, v in r.items() if k != "data"}, indent=1))
    if r.get("ok"):
        print("count:", (r["data"] or {}).get("count"))
    print("bucket:", json.dumps(bucket_snapshot()))
