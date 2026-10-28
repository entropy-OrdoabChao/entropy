#!/usr/bin/env python3
"""m2 study runner — the ONE piece of code behind every m2 study/forum/record bot.

A bot is a FOLDER (pipeline/m2/bots/<group>/<name>/): JOB.yaml (axis, hierarchy, corpus families, output schema),
SOUL.md (the contract the model reads), CHECK.md (acceptance). Updating a bot = editing its folder. No code.

Runner contract (local-gpu production lane, concurrent):
  * lane = M2_LANE (default http://localhost:8000/v1, the local-gpu). Never the local-gpu unless M2_LANE says so.
  * unit of work = (bot × claim). Claims are the numbered points / count paragraphs of a draft; each claim is one
    bounded request (< ~6K prompt tokens) so decode never degrades at depth; N workers in flight → vLLM batches them.
  * retrieval = corpus service :8100 (/search semantic, /search_text FTS phrase), scoped by the bot's families.
  * GUARDRAIL (deterministic, not the model's opinion): every cite a bot returns must resolve — its quoted words
    found by FTS phrase in the corpus — else status NOT_IN_CORPUS. A bot cannot certify its own authority.
  * output = <draft>.m2/<bot>.json (one row per claim) + <draft>.m2/MATRIX.json (claim × axis × level) + MATRIX.md.

  python3 -m pipeline.m2.analysis_runner <draft.md> [--bots constitution,definition] [--claims 1-5] [--workers 6] [--dry]
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import argparse, json, os, re, sqlite3, sys, threading, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from pathlib import Path

ROOT = Path(os.environ.get("M2_ROOT", str(Path(__file__).resolve().parents[2])))
BOTS_DIR = Path(__file__).resolve().parent / "bots"
if not BOTS_DIR.is_dir():                                   # sandbox overlay carries only changed files: bots live under M2_ROOT
    BOTS_DIR = ROOT / "pipeline" / "m2" / "bots"
LANE = os.environ.get("M2_LANE", "http://localhost:8000/v1").rstrip("/")
MODEL = os.environ.get("M2_MODEL", "")
CORPUS = os.environ.get("M2_CORPUS", "http://localhost:8100").rstrip("/")
HIERARCHY = (Path(__file__).resolve().parent / "HIERARCHY.md")
if not HIERARCHY.is_file():                                 # sandbox overlay carries only changed files: HIERARCHY lives under M2_ROOT
    HIERARCHY = next((p / "pipeline" / "m2" / "HIERARCHY.md" for p in (ROOT, *Path(__file__).resolve().parents)
                      if (p / "pipeline" / "m2" / "HIERARCHY.md").is_file()), HIERARCHY)
    if not BOTS_DIR.is_dir(): BOTS_DIR = HIERARCHY.parent / "bots"
MAX_TOKENS = int(os.environ.get("M2_MAX_TOKENS", "1200"))   # a JSON cell is ~400-600 tok; 3000 let a loop run 169 s
# S-m2-merged2 (2026-10-24): ONE file = S-m2-retrieval-pipelined (per-cell retrieval→lane pipelining, main()) + S-m2-speed
# (opt-in levers: M2_PREFIX_ORDER / M2_ORDER / M2_BOT_CAPS) + S-m2-gap-cite-second-pass (run_cell: a GAP/NOT_IN_CORPUS/CONFLICT
# answer naming an unpinned cite is re-asked ONCE with that cite pulled through the pin path; default ON, M2_SECOND_PASS=0 opts out).
# CLASS RULE: the first two are scheduling/layout levers — with no opt-in env (and M2_SECOND_PASS=0, or any cell that is not
# GAP/NOT_IN_CORPUS/CONFLICT) every cell is byte-identical in content to the live runner; every promoted hardening (pin_version rows, plan_resume,
# forum_families, S-m2-refuse-fts abort, S-m2-cite-cache-corpus-version cache key, learn_back relink deferral) is kept.
# DEFECT: the two GREEN sandboxes were cut from the same live base and could not be promoted one after the other without
# the second overwriting the first (both rewrite run_cell + main()).
# ---- SPEED LEVERS (S-m2-speed, rebased onto S-m2-runner-hardening 2026-10-24; every one is OPT-IN, default = live behaviour byte-for-byte) ----
# CLASS RULE: a throughput lever may change prompt ORDER / token CAP / submission ORDER, never prompt CONTENT, never a cell's
# floor of output length, never the hardening semantics (pin_version rows, plan_resume, forum_families, learn_back relink deferral).
# DEFECT: run8 (102 cells) ran bot-major with a per-bot system prompt, so the ~3.5k-token unit material (HIERARCHY+schema,
# CLAIM, pinned authorities, graph notes) was re-prefilled 6× per unit; a global MAX_TOKENS let one looping bot run 169 s.
# M2_PREFIX_ORDER=1  shared-head prompt layout: the per-UNIT material (HIERARCHY+schema, CLAIM, PINNED authorities,
#                    PRIOR GROUNDED NOTES) is placed first in the system message and the bot SOUL last; the facts slice
#                    and the bot's own corpus excerpts stay in the user message. Same bytes of content, different order,
#                    so the 6 bots of one unit share a ~3.5k-token prefix the lane's prefix cache can hit (block 816).
# M2_ORDER=unit      submit cells unit-major (claim outer, bot inner) so those 6 cells run back-to-back on that prefix.
# M2_BOT_CAPS=<json> per-bot max_tokens {bot: cap} derived from an observed completion distribution (derive_bot_caps);
#                    a cell that hits a per-bot cap (finish_reason=length) is re-asked ONCE at MAX_TOKENS, so no cell
#                    can come back shorter than it would under the global cap — the cap only shortens runaway loops.
PREFIX_ORDER = os.environ.get("M2_PREFIX_ORDER", "0") == "1"
CELL_ORDER = os.environ.get("M2_ORDER", "bot")              # bot (live) | unit
BOT_CAPS_PATH = os.environ.get("M2_BOT_CAPS", "")
CAP_FLOOR = int(os.environ.get("M2_CAP_FLOOR", "1000"))      # never cap a bot below this (p95 of every bot observed so far < 1000)
CAP_MARGIN = float(os.environ.get("M2_CAP_MARGIN", "1.4"))   # cap = p95 × margin, rounded up to CAP_STEP, clamped [floor, MAX_TOKENS]
CAP_STEP = 50


def load_bot_caps(path: str = "") -> dict:
    """{bot: max_tokens} from M2_BOT_CAPS (JSON); {} = global MAX_TOKENS for every bot (live behaviour)."""
    p = path or BOT_CAPS_PATH
    if not p or not Path(p).is_file():
        return {}
    try:
        d = json.loads(Path(p).read_text(encoding="utf-8"))
        return {k: int(v) for k, v in (d.get("caps") if isinstance(d, dict) and "caps" in d else d).items()}
    except Exception:
        return {}


def derive_bot_caps(cells_jsonl: str, floor: int = CAP_FLOOR, margin: float = CAP_MARGIN, ceiling: int = MAX_TOKENS,
                    step: int = CAP_STEP) -> dict:
    """DATA, not a guess: per-bot cap = roundup(p95(completion_tokens of finish_reason=stop cells) × margin, step),
    clamped to [floor, ceiling]. Loop cells (finish_reason=length) are excluded from the distribution — they are what
    the cap is for. Returns {"caps": {...}, "evidence": {bot: {n, p95, max_stop, cap}}, "floor", "margin", "ceiling"}."""
    by: dict = {}
    for l in Path(cells_jsonl).read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(l)
        except Exception:
            continue
        if r.get("finish_reason") != "stop" or not r.get("completion_tokens"):
            continue
        by.setdefault(r["bot"], []).append(int(r["completion_tokens"]))
    caps, ev = {}, {}
    for b, xs in sorted(by.items()):
        xs.sort(); p95 = xs[min(len(xs) - 1, int(round(0.95 * (len(xs) - 1))))]
        cap = int(-(-(p95 * margin) // step) * step)
        cap = max(floor, min(ceiling, cap))
        caps[b] = cap; ev[b] = {"n": len(xs), "p95": p95, "max_stop": xs[-1], "cap": cap}
    return {"caps": caps, "evidence": ev, "floor": floor, "margin": margin, "ceiling": ceiling, "source": str(cells_jsonl)}


def order_cells(bots: list, claims: list, order: str = "") -> list:
    """The submission order of (bot, claim) cells. 'bot' (live): bot outer, claim inner — consecutive cells share the
    bot's system prompt. 'unit': claim outer, bot inner — the 6 bots of one unit run back-to-back on the unit's shared
    head (M2_PREFIX_ORDER=1). Same set of cells either way; only the order changes."""
    order = order or CELL_ORDER
    if order == "unit":
        return [(b, c) for c in claims for b in bots]
    return [(b, c) for b in bots for c in claims]


LEVELS = {"us_const": 1, "state_const": 2, "enacted_statute": 3, "court_rule": 4, "regulation": 5, "binding_case": 6, "treatise": 7}


# ------------------------------------------------------------------ bots (folders)
def _yaml(p: Path) -> dict:
    try:
        import yaml
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except ImportError:                                    # stdlib-only fallback: flat "key: value" + lists
        d, key = {}, None
        for l in p.read_text(encoding="utf-8").splitlines():
            if not l.strip() or l.lstrip().startswith("#"): continue
            if l.startswith("  - ") and key: d.setdefault(key, []).append(l[4:].strip().strip('"'))
            elif ":" in l and not l.startswith(" "):
                key, v = l.split(":", 1); key = key.strip(); v = v.strip().strip('"')
                d[key] = v if v else []
        return d


def load_bots(only: list[str] | None = None) -> list[dict]:
    bots = []
    for job in sorted(BOTS_DIR.glob("*/*/JOB.yaml")):
        d = _yaml(job); d["dir"] = job.parent; d["name"] = d.get("name") or job.parent.name; d["group"] = job.parent.parent.name
        d["soul"] = (job.parent / "SOUL.md").read_text(encoding="utf-8") if (job.parent / "SOUL.md").exists() else str(d.get("soul") or "")
        if str(d.get("unit") or "").strip().lower() == "run": continue   # S-m2-posture-axis: a run-level bot (posture) is never a per-claim cell
        if only and d["name"] not in only: continue
        if any(b["name"] == d["name"] for b in bots): continue   # analysis/ and study/ ship identical bots: run each ONCE
        bots.append(d)
    return bots


def load_posture_bot() -> dict | None:
    """S-m2-posture-axis: the ONE run-level bot (bots/posture/posture, `unit: run`). None when the folder is absent (live tree
    today) — main() then behaves exactly as before. SOUL.md when present, else JOB.yaml `soul:`."""
    for job in sorted(BOTS_DIR.glob("*/posture/JOB.yaml")):
        d = _yaml(job)
        if str(d.get("unit") or "").strip().lower() != "run":
            continue
        d["dir"] = job.parent; d["name"] = d.get("name") or job.parent.name; d["group"] = job.parent.parent.name
        d["soul"] = (job.parent / "SOUL.md").read_text(encoding="utf-8") if (job.parent / "SOUL.md").exists() else str(d.get("soul") or "")
        return d
    return None


# ------------------------------------------------------------------ claims (bounded units)
_NUM = re.compile(r"^\*\*(\d+)\.\s+(.+?)\*\*", re.M)            # rebuttal-matrix points: **12. Title.** body
_COUNT = re.compile(r"^(?:#+\s*)?((?:COUNT|CAUSE\s+OF\s+ACTION)\s+(?:[IVXLC]+|\d+)\b[^\n]*)$", re.M | re.I)  # m0 writes "### COUNT 1:" / "### Cause of Action 1:"
_PARA = re.compile(r"^(\d{1,3})\.\s+(?=[A-Z])", re.M)          # numbered pleading paragraphs
_HCOUNT = re.compile(r"^#{2,4}\s+(\d{1,2})\.\s+(\S[^\n]*)$", re.M)   # m0 count headings: "### 1. Bad Faith — 42 Pa.C.S. § 8371"


def split_claims(text: str) -> list[dict]:
    """Numbered bold points (rebuttal matrix) → else COUNT sections → else numbered paragraphs → else 1 claim.
    Claim ids must be UNIQUE: MATRIX cells and RESUME are keyed claim:bot (duplicate ids silently overwrite)."""
    out = _split_claims(text)
    if len({c["id"] for c in out}) != len(out):
        for i, c in enumerate(out): c["id"] = i + 1
    return out


def _split_claims(text: str) -> list[dict]:
    out = []
    ms = list(_NUM.finditer(text))
    if len(ms) >= 3:
        for i, m in enumerate(ms):
            end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
            out.append({"id": int(m.group(1)), "title": m.group(2).strip()[:160], "text": text[m.start():end].strip()[:6000]})
        return out
    ms = list(_COUNT.finditer(text))
    if len(ms) >= 1:
        for i, m in enumerate(ms):
            end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
            out.append({"id": i + 1, "title": m.group(1).strip()[:160], "text": text[m.start():end].strip()[:6000]})
        return out
    ms = list(_HCOUNT.finditer(text))                                 # numbered count HEADINGS beat element lists inside them
    if len(ms) >= 1:
        for i, m in enumerate(ms):
            end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
            out.append({"id": i + 1, "title": m.group(2).strip()[:160], "text": text[m.start():end].strip()[:6000]})
        return out
    ms = list(_PARA.finditer(text))
    if len(ms) >= 5:
        for i, m in enumerate(ms):
            end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
            out.append({"id": int(m.group(1)), "title": text[m.end():m.end() + 90].split("\n")[0], "text": text[m.start():end].strip()[:6000]})
        return out
    return [{"id": 1, "title": "whole draft", "text": text[:12000]}]


# ------------------------------------------------------------------ corpus
def _get(url: str, timeout=60):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def _post(url: str, body: dict, timeout=120):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# ---- retrieval cache: the corpus container embeds on CPU (6-15 s per fresh query); same cells re-run across configs
import hashlib, threading
CACHE_DIR = ROOT / "output" / "m2_cache"; CACHE_DIR.mkdir(parents=True, exist_ok=True)
_CACHE_LOCK = threading.Lock()


def _cached(kind: str, key_obj, fn):
    key = hashlib.sha1(json.dumps(key_obj, sort_keys=True).encode()).hexdigest()
    f = CACHE_DIR / f"{kind}_{key}.json"
    if f.exists():
        try: return json.loads(f.read_text(encoding="utf-8"))
        except Exception: pass
    val = fn()
    if val and not (isinstance(val, list) and val and "error" in val[0]):
        with _CACHE_LOCK: f.write_text(json.dumps(val), encoding="utf-8")
    return val


# QUERY COVERAGE (2026-10-20, S-amjur-gaps): fetch_cell prepends the claim title + text, so the old `queries[:4]` cap
# silently dropped every JOB.yaml query after the 2nd (procedure's Zerbst query, pleading_sufficiency's demurrer /
# appearance queries never ran). All queries up to MAX_Q now run, and selection is quota-first: each query's best
# in-family hit is kept before the score-ordered fill, so a doctrine query is never crowded out by the claim text.
MAX_Q = 12   # S-m2-cure: fetch_cell prepends 2 (title, text); procedure now carries 10 doctrine queries -> 12 must all run
# S-m2-retrieval-pipelined (2026-10-24): retrieval pool size for main(). The daemon embeds on CPU/GPU and serialises its
# encode calls, so more than ~4 concurrent fetch_cell() just queue at the socket and starve the daemon's own probe; keep <= 4.
RETRIEVAL_WORKERS = 4


def retrieve(queries: list[str], families: list[str], k: int = 6) -> list[dict]:
    k = max(k, min(len(queries), MAX_Q))
    return _cached("tv2", [queries[:MAX_Q], families, k, "holding", "quota"], lambda: _retrieve(queries, families, k))


# ---- SEMANTIC retrieval = the turbovec index m0 and the chat MCP use (main 3.05M + delta 2.85M = the whole corpus).
# CLASS FIX 2026-10-12: the container's POST /search is SQLite FTS5 keyword MATCH — m2 had never done semantic retrieval.
# A 300-char claim text as an FTS MATCH ran for minutes, the client timed out, SQLite kept grinding (1300% CPU, 0 conns).
# Opinion PART (side table chunk_part.db) is applied here: dissent/concurrence/headnote/syllabus never reach a bot as a
# "holding"; majority + untagged pass; non-opinion sources are untouched. Every excerpt header carries part=.
TV = Path(os.environ.get("TURBOVEC_DIR", str(_eroot() / "corpus")))
QPREFIX = "Represent this sentence for searching relevant passages: "
HOLDING_PARTS = {"majority", "unknown", None}
_TV = None
_TV_LOCK = threading.Lock()
# B-turbovec-daemon (2026-10-24): the embedder + both turbovec indices live in ONE resident process
# (pipeline/turbovec_daemon.py); this module is a torch-free socket client (pipeline/retrieval_client.py, 'no forks').
# No torch / sentence_transformers / turbovec import may appear in this file again — tests/regress_wiring.sh asserts it.
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))   # run-by-path from a sandbox overlay (M2_ROOT=repo) still finds pipeline.*
from pipeline import retrieval_client as RC   # noqa: E402
_RC_MODE = {"last": None, "warned": set()}


def _note_mode(resp: dict, where: str):
    """A hit list that did not come from the embedder (daemon down / degraded / FTS stand-in) is NOT semantically
    verified: say so once per mode on stderr so a bot run can be read for it; never fabricate a semantic verdict."""
    mode = (resp or {}).get("mode") if isinstance(resp, dict) else None
    _RC_MODE["last"] = mode
    if mode != "embed" and not _RC_MODE.get("abort"):               # S-m2-refuse-fts: first non-semantic answer arms the abort
        _RC_MODE["abort"] = mode or "none"
    if mode and mode != "embed" and mode not in _RC_MODE["warned"]:
        _RC_MODE["warned"].add(mode)
        print(f"[retrieval] {where}: mode={mode} daemon_mode={resp.get('daemon_mode')} — FTS stand-in, NOT semantically verified "
              f"({(resp.get('daemon_error') or resp.get('error') or '')[:120]})", file=sys.stderr, flush=True)
    return mode


# S-m2-refuse-fts (2026-10-24): order17 run5 ran 102 cells on mode=fts_fallback (daemon degraded for hours after one
# CPU-starved embed timeout) and stamped them "NOT semantically verified" — keyword stand-ins masquerading as analysis.
# CLASS RULE: a retrieval answer that did not come from the embedder is NOT analysis input; the runner refuses it.
RESTART_HINT = "restart the daemon (bash scripts/turbovec_daemon.sh restart)"
DEGRADED_MARKER = "RETRIEVAL_DEGRADED"
EXIT_RETRIEVAL_DEGRADED = 3


def _fts_allowed() -> bool:
    """M2_ALLOW_FTS=1 — explicit probe/test override only; logged loudly every time it is consulted and true."""
    if os.environ.get("M2_ALLOW_FTS") == "1":
        print("[m2] !!! M2_ALLOW_FTS=1: running on NON-SEMANTIC retrieval — cells are keyword stand-ins, NOT analysis. "
              "Probe/test use only; never ship these cells.", file=sys.stderr, flush=True)
        return True
    return False


def require_semantic_at_start() -> str:
    """Startup gate: RC.health().mode must be 'embed' or the runner exits 3 before any cell is written."""
    try:
        h = RC.health() or {}
    except Exception as e:                                           # RC.health never raises today; belt and braces
        h = {"mode": "down", "error": repr(e)}
    mode = h.get("mode") or "down"
    if mode != "embed" and not _fts_allowed():
        print(f"[m2] retrieval mode={mode}: not semantic — {RESTART_HINT}; refusing to run "
              f"({(h.get('degraded_reason') or h.get('error') or '')[:160]})", file=sys.stderr, flush=True)
        sys.exit(EXIT_RETRIEVAL_DEGRADED)
    return mode


def abort_retrieval_degraded(outdir: Path, where: str, cells_written: int = 0) -> int:
    """Mid-run gate: a retrieval answer came back non-semantic — leave the marker in M2_OUT and return exit code 3.
    Cells already on disk stay (they were produced on semantic hits); no new cell is launched after this point."""
    mode = _RC_MODE.get("abort") or _RC_MODE.get("last") or "unknown"
    try:
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / DEGRADED_MARKER).write_text(
            json.dumps({"mode": mode, "where": where, "cells_written_before_abort": cells_written,
                        "at": time.strftime("%Y-%m-%d %H:%M:%S"), "hint": RESTART_HINT}, indent=1) + "\n", encoding="utf-8")
    except Exception as e:
        print(f"[m2] could not write {DEGRADED_MARKER}: {e!r}", file=sys.stderr, flush=True)
    print(f"[m2] retrieval mode={mode} during {where}: not semantic — {RESTART_HINT}; stopping "
          f"({cells_written} semantic cells kept; marker {outdir / DEGRADED_MARKER})", file=sys.stderr, flush=True)
    return EXIT_RETRIEVAL_DEGRADED


def _tv():
    """Read-only chunk_text / chunk_part connections + a lock (shape kept for `_exact_statute`): no model, no index.
    Semantic search and embedding go through RC (the daemon)."""
    global _TV
    with _TV_LOCK:
        if _TV is None:
            con = sqlite3.connect(f"file:{TV / 'chunk_text.db'}?mode=ro", uri=True, check_same_thread=False, timeout=300)
            pdb = TV / "chunk_part.db"
            pcon = sqlite3.connect(f"file:{pdb}?mode=ro", uri=True, check_same_thread=False, timeout=300) if pdb.exists() else None
            _TV = (None, None, None, None, None, con, pcon, threading.Lock())
    return _TV


def _fam_prefixes(families: list[str]) -> list[str]:
    """FAMILIES.yaml expansion → daemon `source_prefix` list (case-insensitive startswith server-side; `=key` exact
    entries become their key as a prefix and `_fam_match` re-applies the exact semantics client-side)."""
    out = []
    for f in families or []:
        ent = _fam_registry().get((f or "").lower())
        if isinstance(ent, dict) and ent.get("absent"):
            continue
        if isinstance(ent, list):
            out += [x[1:] if x.startswith("=") else x for x in ent if x]
            continue
        m = re.fullmatch(r"amjur_(\d+[a-z]?)", (f or "").lower())
        out += list(("amjur2d_%s_" % m.group(1), "amjur_%s" % m.group(1)) if m else ((f or "").lower(),))
    return [p for p in dict.fromkeys(out) if p]


def _part_of(pcon, cid: str):
    if pcon is None or not cid: return None
    r = pcon.execute("SELECT part FROM chunk_part WHERE chunk_id=?", (cid,)).fetchone()
    return r[0] if r else None



# FAMILY SCOPE (2026-10-20, S-capacity-status): source keys are mixed-case and Am Jur 2d is keyed
# `amjur2d_<vol>_<topic>`, so a bare case-sensitive startswith dropped every Am Jur 2d volume for bots naming
# `amjur_`/`amjur_<vol>` and every upper-case U.S. Reports key for `usreports`. Match case-insensitively and
# resolve the known aliases; unknown families keep plain prefix semantics.
_FAMILIES_FILE = Path(__file__).resolve().parent / "FAMILIES.yaml"
_FAM_REG: dict | None = None


def _fam_registry() -> dict:
    """FAMILY REGISTRY (S-m2-family-scope): family -> real corpus source prefixes ('=key' exact). Data, not code."""
    global _FAM_REG
    if _FAM_REG is None:
        try:
            import yaml
            _FAM_REG = {str(k).lower(): v for k, v in (yaml.safe_load(_FAMILIES_FILE.read_text(encoding="utf-8")) or {}).items()}
        except Exception:
            _FAM_REG = {}
    return _FAM_REG


def _fam_match(source: str, fam: str) -> bool:
    s, f = (source or "").lower(), (fam or "").lower()
    if not f:
        return False
    ent = _fam_registry().get(f)
    if isinstance(ent, dict) and ent.get("absent"):
        return False
    if isinstance(ent, list):
        return any((s == x[1:].lower()) if x.startswith("=") else s.startswith(x.lower()) for x in ent)
    m = re.fullmatch(r"amjur_(\d+[a-z]?)", f)
    pre = ("amjur2d_%s_" % m.group(1), "amjur_%s" % m.group(1)) if m else (f,)
    return any(s.startswith(x) for x in pre)


# ------------------------------------------------------------------ forum filter (S-m2-forum-families, 2026-10-24)
# CLASS RULE: a bot's SEMANTIC families are forum-bound; exact-cite pins (pull_cites / _exact_*) are NEVER filtered.
# DEFECT: bot families are static, so a Philadelphia CCP run received frcp_rule_77 / nj_court_rule_4_46_2 semantic
# excerpts (run8 u13/u10/u17) — window wasted, federal standards invited into a state filing.
_FORUMS = ("fed", "pa", "nj")
_RUN_FORUM: set = {"any"}          # set by main(); {"any"} = no filtering (M2_FORUM=any / probe runs / library use)


def _fam_forum(fam: str) -> str:
    """Forum tag for a family: FAMILIES.yaml `_forum:` block first, else the name heuristic (pa_* → pa, nj* → nj,
    fr*/ca3*/edpa* → fed, else any). Returns one of fed | pa | nj | treatise | any."""
    f = (fam or "").lower()
    tags = _fam_registry().get("_forum") or {}
    t = str(tags.get(f) or "").lower() if isinstance(tags, dict) else ""
    if t in _FORUMS or t in ("treatise", "any"):
        return t
    if f.startswith(("pa_", "phila_", "cp-")): return "pa"
    if f.startswith(("nj", "njac")): return "nj"
    if f.startswith(("frcp", "frap", "fre", "ca3", "edpa")): return "fed"
    return "any"


def forum_families(families: list, forum=None) -> list:
    """Drop families whose forum tag is neither the run forum(s) nor any/treatise. forum: str|set|None (None = _RUN_FORUM;
    accepts a comma list — `fed,pa` keeps both, e.g. a removed/diversity matter under PA substantive law)."""
    run = forum if forum is not None else _RUN_FORUM
    run = {x.strip().lower() for x in (run.split(",") if isinstance(run, str) else run) if x and x.strip()}
    if not run or "any" in run:
        return list(families or [])
    return [f for f in (families or []) if _fam_forum(f) in run or _fam_forum(f) in ("any", "treatise")]


_FORUM_LINE = re.compile(r"court|forum|case_id|case_slug|docket|caption|jurisdiction", re.I)
_FORUM_RX = (
    ("pa",  re.compile(r"Court of Common Pleas|\bCCP\b|phila_ccp|Commonwealth Court|Pa\.\s*Super|Superior Court of Pennsylvania|Supreme Court of Pennsylvania|Pennsylvania (?:state )?courts?", re.I)),
    ("fed", re.compile(r"Third Circuit|3d Cir|Court of Appeals|District Court|E\.D\. Pa\.|\bEDPA\b|Eastern District|United States District|U\.S\. District|\bfed:|\bfed_|\bFederal\b", re.I)),
    ("nj",  re.compile(r"\bN\.J\.|New Jersey|\bnj_|\bnj:", re.I)),
    ("pa",  re.compile(r"\bPa\.", re.I)),
)


def infer_forum(facts: str, draft_path: str = "") -> str:
    """Infer the run forum from the facts file's caption/court line (first 60 lines: `case_id:` frontmatter, `**Court**:`,
    `Court of Common Pleas` …) then the draft/case path slug. 'Court of Common Pleas'/'Pa.' → pa; 'Third Circuit'/'E.D. Pa.'/
    'U.S.' → fed; 'N.J.' → nj; else 'any' (caller warns). Only lines naming a court/forum/docket are read so a reporter
    cite in the body ('504 U.S. 555') never decides it."""
    head = (facts or "").splitlines()[:60]
    for line in head:
        if not _FORUM_LINE.search(line):
            continue
        for tag, rx in _FORUM_RX:
            if rx.search(line):
                return tag
    p = (draft_path or "").lower()
    for tag, rx in (("pa", re.compile(r"phila_ccp|pa_cp|pa_super|/pa_")), ("fed", re.compile(r"/fed[_:]|ca3|edpa")), ("nj", re.compile(r"/nj_"))):
        if rx.search(p):
            return tag
    return "any"


def probe_families(sources_file: str) -> int:
    """Every family named by a bot resolves to >=1 corpus source, or is declared absent. Returns # unresolved."""
    srcs = [x for x in Path(sources_file).read_text(encoding="utf-8").split("\n") if x]
    bad = 0
    for f in sorted({f for b in load_bots() for f in (b.get("families") or [])}):
        ent = _fam_registry().get(f.lower()); n = sum(1 for x in srcs if _fam_match(x, f))
        tag = "ABSENT (declared)" if isinstance(ent, dict) and ent.get("absent") else ("ok" if n else "UNRESOLVED")
        bad += tag == "UNRESOLVED"; print(f"{n:7d}  {f:<20} {tag}")
    return bad


def _retrieve(queries: list[str], families: list[str], k: int = 6) -> list[dict]:
    """Semantic search over the WHOLE corpus (main + delta turbovec) THROUGH THE DAEMON (RC.semantic), part-filtered
    to holdings server-side, family-scoped when the bot names families (the daemon filters by source prefix inside a
    k*30 pool; `_fam_match` re-applies the exact FAMILIES.yaml semantics; fallback to unscoped so a thin family never
    returns nothing). Hit shape unchanged; each hit carries `mode` (embed | fts_fallback | client_fts) so a reader
    can tell a semantic hit from an FTS stand-in."""
    try:
        qs = [q for q in queries[:MAX_Q] if isinstance(q, str) and q.strip()]
        if not qs:
            return []
        prefixes = _fam_prefixes(families) if families else []
        resp = RC.semantic(qs, k, prefixes or None)
        mode = _note_mode(resp, "_retrieve")
        hits = RC.hits(resp)
        if hits and hits[0].get("error"):
            return [{"error": f"turbovec retrieval failed: {hits[0]['error']}"}]
        if families:
            hits = [h for h in hits if any(_fam_match(h.get("source") or "", f) for f in families)]
            if not hits:                                              # thin / absent family: unscoped pool (as before)
                resp = RC.semantic(qs, k, None)
                mode = _note_mode(resp, "_retrieve/unscoped"); hits = RC.hits(resp)
                if hits and hits[0].get("error"):
                    return [{"error": f"turbovec retrieval failed: {hits[0]['error']}"}]
        out = []
        for h in hits[:k]:
            t = _best_window(h.get("text") or "", qs, 1200)
            out.append({"chunk_id": h.get("chunk_id"), "source": h.get("source") or "?", "part": h.get("part") or "-",
                        "score": round(float(h.get("score") or 0.0), 3), "text": t, "recital": bool(_RECITAL.search(t)),
                        "mode": mode})
        out.sort(key=lambda h: -h["score"])
        return out
    except Exception as e:
        return [{"error": f"turbovec retrieval failed: {e}"}]


# S-m2-cite-leading-word (2026-10-24): the first party may run to 6 capitalised tokens ('Pennsylvania National Mutual Casualty
# Insurance Co.'); the old {0,3} made the match start mid-name ('Mutual Casualty Insurance Co.'). Leading words the longer run
# now swallows are removed by _trim_leading, not by shortening the run.
_CASE = re.compile(r"\b([A-Z][A-Za-z'’.&-]+(?:\s+[A-Z][A-Za-z'’.&-]+){0,5})\s+v\.\s+([A-Z][A-Za-z'’.&-]+(?:\s+[A-Z][A-Za-z'’.&-]+){0,3})")
# CLASS FIX (S-m2-cure, C3a): keep the USC section LETTER suffix (and the `1681s-2` dash form) — `[\d.]+` cut
# '15 U.S.C. § 1681e(b)' to '§ 1681', so the pin read usc_t15_s1681 (the short title) instead of usc_t15_s1681e.
_STAT = re.compile(r"\b(\d{1,2}\s+(?:U\.S\.C\.|Pa\.C\.S\.(?:A\.)?|P\.S\.|C\.F\.R\.)\s*§+\s*\d+(?:\.\d+)*[a-zA-Z]?(?:[-–]\d+[a-zA-Z]?)?(?:\([a-zA-Z0-9]{1,3}\))*)")
_NJ = re.compile(r"\bN\.J\.(?:S\.A?\.?|A\.C\.)\s*(?:§+\s*)?\d{1,2}[A-Z]?:\d+[A-Z]*-\d+(?:\.\d+)*[a-z]?")   # N.J.S.A. 17:29B-4 / N.J.A.C. 11:2-17.7
_RULE = re.compile(r"\b((?:Pa\.R\.C\.P\.|Pa\.R\.Civ\.P\.|Pa\.R\.A\.P\.|Fed\.\s*R\.\s*(?:Civ|App|Evid)\.\s*P\.|Rule|L\.R\.|Local Rule)\s*\d+(?:\.\d+)*(?:\([a-zA-Z0-9]{1,3}\))*)")


def _trim_party(p: str) -> str:
    """S-m2-cite-tail (2026-10-24) — CLASS RULE: a party name ends at a sentence period. The bot writes 'Neitzke v. Williams.
    However' / 'Stump v. Sparkman. The'; the greedy name regex swallowed the next sentence's first word, the pin path then
    searched for a case that does not exist and the cite was 'unresolved' while the opinion sat in cap_us_*. Abbreviations
    inside names ('Corp.', 'Inc.', 'Co.', 'U.S.') are kept: only a period followed by a capitalised ordinary word is a sentence end."""
    toks = p.split()
    for i, tok in enumerate(toks[:-1]):
        if tok.endswith(".") and tok.rstrip(".") not in _NAME_ABBR and toks[i + 1][:1].isupper() and len(toks[i + 1]) > 2:
            return " ".join(toks[: i + 1]).rstrip(".")
    return p


_NAME_ABBR = {"Corp", "Inc", "Co", "Ltd", "L.L.C", "LLC", "U.S", "Bros", "Ass'n", "Assn", "Dep't", "Dept", "Nat'l", "Int'l", "Mfg", "Ins", "Sav", "Bd", "Comm'n", "Pa", "St", "Mut", "Fed"}

# S-m2-cite-leading-word (2026-10-24): capitalised words that INTRODUCE a case name in running text and are never its first
# party — citation signals ('See', 'Cf.', 'Accord', 'But see', 'E.g.'), prepositions/articles a sentence opens with ('Under',
# 'In', 'The', 'While'), and the imperatives the bots write in `fix` ('Add', 'Pull', 'Cite'). Matched case-insensitively
# without the trailing period, and ONLY when the next token is itself capitalised (so 'In re X' / 'Ex parte Y' keep their
# lowercase marker and are left alone).
_LEAD_WORDS = {"see", "cf", "accord", "but", "also", "e.g", "eg", "citing", "quoting", "compare", "contra", "with", "under", "in",
               "the", "while", "add", "pull", "cite", "per", "and", "or", "at", "as", "to", "of", "by", "for", "from", "on",
               "that", "held", "following", "distinguishing", "applying", "generally", "although", "though", "because", "if",
               "when", "where", "whereas", "then", "thus", "here", "first", "second", "third", "finally", "further", "unlike"}


def _trim_leading(p: str) -> str:
    """S-m2-cite-leading-word (2026-10-24) — CLASS RULE (mirror of _trim_party): a party name STARTS after a sentence
    boundary or after the word that introduces the citation. _CASE takes a run of capitalised tokens before ' v. ', so when
    the bot writes 'Lance. Stump v. Sparkman' / 'Under Liteky v. United States' / 'Add Lomas v. Kravitz' / 'NOT IN CORPUS.
    Robinson v. Pa. Bd.' the run swallows the previous sentence's last word, the signal, or the imperative; the pin path then
    searched for a case that does not exist and the cite stayed 'unresolved' while the opinion sat in cap_us_*.
    Two trims, left to right: (1) everything up to and including the LAST token that ends a sentence — a token ending in '.'
    that is not a name abbreviation ('Corp.', 'Inc.', 'U.S.') and is followed by a capitalised word — is dropped ('Lance.',
    'CORPUS.'); (2) leading _LEAD_WORDS ('Under', 'The', 'While', 'See', 'Add', 'Pull') are dropped while the NEXT token is
    capitalised and at least one token remains. 'In re X' / 'Ex parte X' survive because 're'/'parte' are lowercase (the
    rule needs a capitalised successor); 'Commonwealth v. X' / 'United States v. X' / 'Pennsylvania National Mutual Casualty
    Insurance Co.' / 'Semtek Int'l Inc.' survive because none of their words is a lead word or a sentence end."""
    toks = p.split()
    if not toks:
        return p
    cut = 0
    for i, tok in enumerate(toks[:-1]):                                  # (1) the last sentence boundary inside the run
        if tok.endswith(".") and tok.rstrip(".") not in _NAME_ABBR and tok.rstrip(".").lower() not in _LEAD_WORDS \
                and toks[i + 1][:1].isupper() and len(tok.rstrip(".")) > 1:
            cut = i + 1
    toks = toks[cut:]
    while len(toks) > 1 and toks[0].rstrip(".,").lower() in _LEAD_WORDS and toks[1][:1].isupper():   # (2) signal / imperative / article
        toks = toks[1:]
    return " ".join(toks)


def cites_in(text: str) -> list[str]:
    """Every authority the claim NAMES — case names, statutes, rules — so the bot sees what the draft relies on."""
    out = []
    for m in _CASE.finditer(text): out.append(f"{_trim_leading(m.group(1))} v. {_trim_party(m.group(2))}")   # S-m2-cite-leading-word: both sides trimmed
    out += [m.group(1) for m in _STAT.finditer(text)] + [m.group(1) for m in _RULE.finditer(text)]
    out += [m.group(0) for m in _NJ.finditer(text)]
    seen, u = set(), []
    for c in out:
        k = re.sub(r"\s+", " ", c).strip()
        if k.lower() not in seen: seen.add(k.lower()); u.append(k)
    return u[:12]


def _pin_version() -> str:
    """CLASS FIX (2026-10-21): the cite cache keyed on the cite LIST only, so after a pin fix every run kept serving the
    pre-fix pulls (CA3 26-0000 issues v2: Rosenthal/Celotex/Anderson still 'NOT IN CORPUS' after the reporter pin was
    promoted). The key now carries a hash of the pin code itself — any change to these functions is a new cache."""
    import inspect
    src = "".join(inspect.getsource(f) for f in (cites_in, _cite_tokens, _pins, _exact_rule_or_reporter, _exact_statute,
                                                 _exact_named_rule, _exact_ps_alias, _heading_first, _trim_party, _trim_leading,
                                                 _pull_cites_tv, _src_keys, _keys_for_reporter, _keys_for_parties,
                                                 rule_family, rule_chunk_ok, _chunk_exists, _exact_pa_rule, _pa_rule_chunk_ok,
                                                 _cite_subsections, _cite_section_token, _context_rule_family, verify_quote,   # t_1972ad60 (2026-10-25): the subsection/token helpers and the C3c fallback are pin code
                                                 second_pass_cites, second_pass_pull,
                                                 adverse_identity, posture_pin_text, compute_deadline, deadline_days, guard_posture,
                                                 rule_verbatim_in_chunks, coerce_posture,   # S-m2-posture-axis: the posture pin text + guardrails are pin code too
                                                 resolve_adverse_doc, resolve_pleading, slice_pleading, record_docs_block, reclassify_record_gap,   # S-m2-record-docs-to-cells (2026-10-24): prompt code — promoting it busts the cache and RESUME re-runs the GAP cells
                                                 )) + _STAT.pattern + _REPORTER.pattern + _PS_ALIAS.pattern + _CASE.pattern   # S-m2-gap-cite-second-pass (2026-10-24): the second pass is pin code — promoting it busts the cite cache and RESUME re-runs the GAP cells; S-m2-cite-leading-word: _CASE widened
    # CLASS FIX (2026-10-24, S-m2-cite-cache-corpus-version): the key also carries the CORPUS version. Pin pulls made
    # before an ingest said "absent" and were served from cache after it (order17 run9: Pa.R.C.P. 1028(e) 'NOT IN CORPUS'
    # with the refilled Title 231 chapter sitting in chunk_text.db) — a stale cache is also a stale pin_version, so
    # RESUME re-runs those GAP cells. Any change to chunk_text.db (size or mtime) is a new cache.
    return hashlib.sha1((src + _corpus_version()).encode()).hexdigest()[:10]


def _corpus_version() -> str:
    """size+mtime of chunk_text.db — the cheapest honest 'has the corpus changed' signal (no table scan)."""
    try:
        st = (TV / "chunk_text.db").stat()
        return f"{st.st_size}:{int(st.st_mtime)}"
    except OSError:
        return "nodb"


def pull_cites(text: str, per: int = 2) -> list[dict]:
    return _cached("cites7tv", [cites_in(text), per, "holding", _pin_version()], lambda: _pull_cites_tv(text, per))


# CLASS FIX 2026-10-13 (Mnemosyne m2 BACKLOG: "cite-label→chunk map hands bots wrong text — EPA as Mitchell, table-of-cases
# as Hart"; litmus #2: Bruno chunk reciting the Brunos' brief handed over as the holding). Named cites are resolved
# SEMANTICALLY (turbovec, part-filtered) and then PINNED: a case hit must name the parties in its source slug or its
# opening text; a statute/rule hit must carry the section number. FTS is kept only to verify a quoted string.
_RECITAL = re.compile(r"\b(argu(?:es|ed|ing)|contend(?:s|ed)|assert(?:s|ed)|maintain(?:s|ed)|urge(?:s|d)|posit(?:s|ed)|"
                      r"submit(?:s|ted)|claim(?:s|ed) that|Brief (?:at|for|of)|Br\. (?:at|of)|Appell(?:ant|ee)'?s? (?:brief|position))", re.I)


_GENERIC = {"insurance", "company", "corp", "inc", "co", "state", "farm", "mutual", "casualty", "fire", "national", "united",
            "states", "county", "city", "board", "commonwealth", "pennsylvania", "washington", "american", "general", "group",
            "association", "trust", "bank", "services", "life", "indemnity", "surety", "society", "assurance", "see", "also", "cf"}
_FAMILY = {"pa.c.s": ("pacs", "pa_cs", "pa.c.s", "pa. c.s", "pennsylvania consolidated", "pa_"), "p.s.": ("ps_", "p.s.", "pa_73ps", "pa_"),
           "u.s.c": ("usc_", "u.s.c", "united states code"), "pa.r.c.p": ("parcp", "pa.r.c.p", "rule"), "c.f.r": ("cfr", "c.f.r")}


def _cite_tokens(c: str) -> tuple[str, list[str], tuple]:
    """('case', [distinct party tokens], ()) | ('section', ['8371'], family-markers). A hit must carry THESE to be this authority."""
    m = _CASE.search(c)
    if m:
        a = [w.lower().strip(".,'’()") for w in _trim_leading(m.group(1)).split()]; b = [w.lower().strip(".,'’()") for w in _trim_party(m.group(2)).split()]   # S-m2-cite-leading-word: pin tokens from the clean name
        pa = [w for w in a if len(w) > 3 and w not in _GENERIC]; pb = [w for w in b if len(w) > 3 and w not in _GENERIC]
        return "case", (pa[:1] + pb[:1]) or (a[:1] + b[:1]), ()
    nums = re.findall(r"\d+(?:\.\d+)?(?:\([a-z0-9]+\))*", c)
    fam = next((v for k, v in _FAMILY.items() if k in c.lower().replace(" ", "")), ())
    base = [re.match(r"\d+(?:\.\d+)?", nums[-1]).group(0)] if nums else []
    return "section", base, fam


# S-12 (t_1972ad60): plain family words for claims that name the family without the abbreviated 'Fed. R. Civ. P.' form.
# Federal entries carry the 'federal' discriminator so a PA claim never matches a federal family.
_FAMILY_WORDS = (
    ("civ",   ("fed. r. civ.", "fr.c.p.", "frcp", "federal rules of civil procedure", "federal rule of civil procedure")),
    ("app",   ("fed. r. app.", "frap", "federal rules of appellate procedure", "federal rules of appeal")),
    ("evid",  ("fed. r. evid.", "federal rules of evidence")),
    ("crim",  ("fed. r. crim.", "frcrp", "federal rules of criminal procedure")),
    ("bankr", ("fed. r. bankr.", "frbp", "federal rules of bankruptcy")),
    ("pa_civ", ("pa. r.c.p", "pa.r.c.p", "pennsylvania rules of civil procedure")),
    ("pa_app", ("pa. r.a.p", "pa.r.a.p", "pennsylvania rules of appellate procedure")),
)


def _context_rule_family(claim: str):
    """(fam, n_or_None) the rule family the claim text names — the full-cite regexes first (they carry the number), then
    plain family words. S-12: a bare 'Rule N' is pinned WITHIN the family the claim names, never a family-agnostic number
    match; None = the claim names no family (the pin stays loose but must still be a rule chunk)."""
    m = _FEDRULE.search(claim or "")
    if m:
        return m.group(1).lower(), m.group(2)
    m = _PARULE.search(claim or "")
    if m:
        return ("pa_app" if m.group(1).upper() == "A" else "pa_civ"), m.group(2)
    low = (claim or "").lower()
    for fam, words in _FAMILY_WORDS:
        if any(w in low for w in words):
            return fam, None
    return None


def _pins(kind: str, toks: list[str], fam: tuple, h: dict, cite: str = "", claim: str = "") -> bool:
    src = (h.get("source") or "").lower(); head = (h.get("text") or "")[:400].lower()
    if kind == "case":   # the distinctive party (first token) must appear in the source slug or the caption; when two
        # distinctive tokens are known, both in the caption OR the first in the slug (slugs abbreviate: "nat'l", "cas_co")
        if not toks: return False
        return (toks[0] in src) or all(t in head for t in toks)
    if cite and rule_family(cite):                                   # C3b: a federal rule pins by FAMILY, never by bare number
        return rule_chunk_ok(cite, h.get("source") or "", h.get("text") or "")
    # C3b/S-12 (t_1972ad60): a BARE 'Rule N' / 'Canon N' pins only a RULE chunk — never a treatise '§ N' heading, and, when
    # the claim names a family, only within that family. Class rule: the bare number never pins; the rule's own source
    # key, the text naming 'Rule N', or (PA codes) the 'N. Title' heading do.
    bm = _BARE_RULE.match((cite or "").strip())
    if bm:
        n, s, t = bm.group(1), (h.get("source") or "").lower(), (h.get("text") or "")
        if re.search(rf"(?:^|[_.-])(?:frcp|frap|fre|frcrp|frbp)[_.-]rule_{re.escape(n)}(?:[_.-]|$)", s):
            return True                                              # the rule's OWN source key, any federal family
        if re.search(rf"\bRule\s+{re.escape(n)}(?!\d|\.\d)", t):
            cf = _context_rule_family(claim)
            return True if cf is None else _family_rule_ok(cf[0], n, s, t)
        if re.search(rf"(?m)(?:^|\n)\s*{re.escape(n)}\.\s+[A-Z]", t):
            cf = _context_rule_family(claim)                         # PA codes print '2.11. Disqualification.' without the word 'Rule'
            return cf is None or cf[0] in ("pa_civ", "pa_app")
        return False
    ok = any(t in src.replace("_", " ") or t in head for t in toks)
    if fam: ok = ok and any(f in src or f in head for f in fam)
    return ok


_USC = re.compile(r"(\d{1,2})\s+U\.S\.C\.?\s*§+\s*(\d+[a-z]?(?:-\d+[a-z]?)?)", re.I)
_NJX = re.compile(r"N\.J\.(S\.A?\.?|A\.C\.)\s*(?:§+\s*)?(\d{1,2}[A-Z]?):(\d+[A-Z]*-\d+(?:\.\d+)*[a-z]?)", re.I)
_PACS = re.compile(r"(\d{1,2})\s+Pa\.\s*C\.S\.(?:A\.)?\s*§+\s*(\d+(?:\.\d+)?)", re.I)


_FEDRULE = re.compile(r"Fed\.\s*R\.\s*(Civ|App|Evid|Crim|Bankr)\.?\s*(?:P\.?)?\s*(\d{1,4})", re.I)
_FEDRULE_SRC = {"civ": ("frcp_rule_{n}", "courtlistener_frcp_rule_{n}"), "app": ("courtlistener_frap_rule_{n}", "frap_rule_{n}"),
                "evid": ("fre_rule_{n}", "courtlistener_fre_rule_{n}"), "crim": ("frcrp_rule_{n}", "courtlistener_frcrp_rule_{n}"),
                "bankr": ("frbp_rule_{n}", "courtlistener_frbp_rule_{n}")}
# PA class fix (sandbox _sandbox_m2_pa_reporter): Atlantic reporters (A.2d / A.3d) and the Pa. / Pa. Super. / Pa. Cmwlth.
# official reporters parse like U.S./F.3d. Corpus keys for PA opinions are mis-keyed `scotus_<names>_<V>a(2d|3d)<P>`
# (e.g. scotus_gregg_v_ameriprise_financial_245a3d637) — see _keys_for_reporter.
_REPORTER = re.compile(r"(\d{1,4})\s+(U\.\s?S\.|F\.\s?(?:2d|3d|4th)|A\.\s?(?:2d|3d)|Pa\.(?:\s?(?:Super|Cmwlth|Commw)\.)?)\s+(\d{1,4})\b", re.I)
# Pa.R.C.P. 1028(c)(2) / Pa.R.Civ.P. 1028 / Pa.R.A.P. 1925(a)(1) -> ('pa_civ'|'pa_app', '<n>')
_PARULE = re.compile(r"Pa\.\s*R\.\s*(C(?:iv)?|A)\.\s*P\.\s*(\d{1,4})", re.I)
# Pa.R.C.P. text lives (until re-ingest) in Pennsylvania Bulletin issue sources keyed <vol>-<issue> / <vol>_<issue>_cts;
# pa_code_t231_ch<n> is hollow (1 chunk = a web-page nav header). Pa.R.A.P. text IS under pa_code_t210_ch<chapter>.
_PA_BULLETIN_KEY = re.compile(r"^\d{2}[-_]\d{1,2}(?:_cts)?$")
_PARULE_FAM = {
    "pa_civ": {"prefixes": ("pa_code_t231_", "courtlistener_pa_code_t231_", "pa_rcp"), "title": ("civil procedure", "title 231")},
    "pa_app": {"prefixes": ("pa_code_t210_", "courtlistener_pa_code_t210_"), "title": ("appellate procedure", "title 210")},
}

# CLASS FIX (S-m2-cure, C3b): a federal RULE cite is pinned by rule FAMILY, never by the bare number. Without this,
# 'Fed. R. Civ. P. 77(d)(1)' (no frcp_rule_77 key in corpus) fell to the semantic path and `_pins('section', ['77'])`
# blessed a treatise heading '§ 77'. Family = own-key prefixes + the whole-rules compendium sources + a text test
# ('Rule <n>' AND the family's title words) for chunks whose source key does not carry the family.
_FEDRULE_FAM = {
    "civ":   {"prefixes": ("frcp_", "courtlistener_frcp_", "federal-rules-of-civil-procedure", "federal_rules_of_civil_procedure"),
              "title": ("civil procedure",)},
    "app":   {"prefixes": ("frap_", "courtlistener_frap_", "federal-rules-of-appellate-procedure", "federal_rules_of_appellate_procedure"),
              "title": ("appellate procedure",)},
    "evid":  {"prefixes": ("fre_", "courtlistener_fre_", "federal-rules-of-evidence", "federal_rules_of_evidence"),
              "title": ("rules of evidence",)},
    "crim":  {"prefixes": ("frcrp_", "courtlistener_frcrp_", "federal-rules-of-criminal-procedure", "federal_rules_of_criminal_procedure"),
              "title": ("criminal procedure",)},
    "bankr": {"prefixes": ("frbp_", "courtlistener_frbp_", "federal-rules-of-bankruptcy-procedure", "federal_rules_of_bankruptcy_procedure"),
              "title": ("bankruptcy procedure",)},
}


def rule_family(c: str):
    """'Fed. R. Civ. P. 77(d)(1)' -> ('civ', '77') ; 'Pa.R.C.P. 1028(c)(2)' -> ('pa_civ', '1028') ; 'Pa.R.A.P. 1925(a)' ->
    ('pa_app', '1925') ; not a rule cite -> None. Pure (no model, no db)."""
    m = _FEDRULE.search(c or "")
    if m:
        return (m.group(1).lower(), m.group(2))
    m = _PARULE.search(c or "")
    return (("pa_app" if m.group(1).upper() == "A" else "pa_civ"), m.group(2)) if m else None


def _pa_rule_chunk_ok(fam: str, n: str, source: str, text: str) -> bool:
    """Deterministic PA rule filter. pa_app: the chunk sits under pa_code_t210_ (its own chapter) and names 'Rule <n>'.
    pa_civ: own-key prefix (pa_code_t231_, hollow today but honoured for re-ingest) OR a Pennsylvania Bulletin issue
    source (<vol>-<issue> / <vol>_<issue>_cts), and the text names 'Rule <n>'."""
    s = (source or "").lower(); t = text or ""
    names_rule = re.search(rf"\bRule\s+{re.escape(n)}(?!\d|\.\d)", t) is not None
    if not names_rule:
        return False
    spec = _PARULE_FAM[fam]
    if any(s.startswith(p) for p in spec["prefixes"]):
        return True
    if fam == "pa_civ" and _PA_BULLETIN_KEY.match(s):
        return True
    return any(w in t.lower() for w in spec["title"])


def _family_rule_ok(fam: str, n: str, source: str, text: str) -> bool:
    """The C3b family filter for a KNOWN family + rule number (the S-12 extension, t_1972ad60: a bare 'Rule N' pinned
    within the family the claim names runs through here too). The rule's own source key (frcp_rule_77 /
    courtlistener_frcp_rule_77 / pa_code_t231_ch1033), or the compendium text names 'Rule <n>' AND the family's title
    words. A treatise '§ 77' heading fails both."""
    if fam in _PARULE_FAM:
        return _pa_rule_chunk_ok(fam, n, source, text)
    spec = _FEDRULE_FAM[fam]; s = (source or "").lower(); t = (text or "")
    names_rule = re.search(rf"\bRule\s+{re.escape(n)}(?!\d|\.\d)", t) is not None   # 'Rule 77.' heading yes, 'Rule 77.1' no
    if any(s.startswith(p) for p in spec["prefixes"]):
        return names_rule or re.search(rf"_rule_{re.escape(n)}(?:$|[_.-])", s) is not None   # own key, or a family chunk naming the rule (never a TOC line)
    return names_rule and any(w in t.lower() for w in spec["title"])


def rule_chunk_ok(c: str, source: str, text: str) -> bool:
    """Deterministic family filter for a rule cite whose family parses from the cite itself ('Fed. R. Civ. P. 77(d)(1)').
    A cite whose family does not parse (bare 'Rule N') is pinned by _pins, not here."""
    rf = rule_family(c)
    if not rf:
        return True
    return _family_rule_ok(*rf, source, text)


def _chunk_exists(con, cid) -> bool:
    """C3c: a chunk id is returned only when chunk_text holds it (orphan index ids, stale caches never reach a bot)."""
    if not cid:
        return False
    try:
        return con.execute("SELECT 1 FROM chunk_text WHERE chunk_id=? LIMIT 1", (cid,)).fetchone() is not None
    except Exception:
        return False


def _ro_con():
    """Read-only chunk_text.db connection WITHOUT loading turbovec/the embedder (used by the pin path and the probes)."""
    return sqlite3.connect(f"file:{TV / 'chunk_text.db'}?mode=ro", uri=True, check_same_thread=False, timeout=300)


def _src_range(con, prefix: str, extra: str = "", args: tuple = ()) -> list:
    """Index-friendly prefix scan on source (LIKE cannot use ix_ct_source)."""
    return con.execute(f"SELECT chunk_id, text, source FROM chunk_text WHERE source >= ? AND source < ?{extra} LIMIT 60",
                       (prefix, prefix + "~") + args).fetchall()


_SRC_KEYS: list | None = None


def _src_keys(con) -> list:
    """Every distinct corpus source key (~340k, ~0.3 s via ix_ct_source), loaded once per process."""
    global _SRC_KEYS
    if _SRC_KEYS is None:
        _SRC_KEYS = [r[0] for r in con.execute("SELECT DISTINCT source FROM chunk_text INDEXED BY ix_ct_source") if r[0]]
    return _SRC_KEYS


def _keys_for_reporter(con, vol: str, rep: str, page: str, toks: list | None = None) -> list:
    """The case's OWN source keys for a reporter triple, in every naming the corpus uses:
    scotus_<names>_<V>us<P> · cap_us_<V>_<P>_<names> · docvault_pull_f3d_<V>_<P> · <x>_f3d_<V>_<P>_<names>.
    Atlantic (A.2d / A.3d — PA opinions are mis-keyed with a scotus_ prefix): scotus_<names>_<V>a3d<P> ·
    docvault_pull_a2d_<V>_<P>. The key may be TRUNCATED after the volume (`_245a`, `_106a3d4`): the volume is required,
    the series and page are prefix-matched, and a truncated key is accepted only when the cite's party tokens confirm it."""
    if rep == "us":
        rx = re.compile(rf"(?:^|_){vol}us{page}$|^cap_us_{vol}_{page}_", re.I)
    elif rep in ("a2d", "a3d"):
        ser = rep[1:]                                                       # '2d' | '3d'
        pfx = "|".join(re.escape(page[:i]) for i in range(len(page), 0, -1))
        rx = re.compile(rf"(?:^|_){vol}a(?:{ser})?(?:{pfx})?$|(?:^|_){rep}_{vol}_{page}(?:_|$)", re.I)
        full = re.compile(rf"(?:^|_){vol}a{ser}{page}$|(?:^|_){rep}_{vol}_{page}(?:_|$)", re.I)
        ptoks = [t for t in (toks or []) if len(t) > 3][:2]
        out = []
        for k in _src_keys(con):
            if not rx.search(k):
                continue
            if full.search(k) or (ptoks and all(t in k.lower() for t in ptoks)):   # exact triple, or truncated + parties
                out.append(k)
        return out
    else:
        rx = re.compile(rf"(?:^|_){rep}_{vol}_{page}(?:_|$)", re.I)
    return [k for k in _src_keys(con) if rx.search(k)]


def _keys_for_parties(con, toks: list) -> list:
    """Name-only cite ('Celotex Corp. v. Catrett', no reporter in the text): source keys carrying BOTH distinctive party
    tokens. Accepted only when the match is narrow (<= 4 keys) — a common surname never pins."""
    toks = [t for t in toks if len(t) > 3][:2]
    if len(toks) < 2:
        return []
    ks = [k for k in _src_keys(con) if all(t in k.lower() for t in toks)]
    return ks if 0 < len(ks) <= 4 else []


def _rows_for_keys(con, keys: list) -> list:
    rows = []
    for k in keys[:3]:
        rows += con.execute("SELECT chunk_id, text, source FROM chunk_text WHERE source=? LIMIT 60", (k,)).fetchall()
    return rows


def _exact_rule_or_reporter(con, c: str, text: str = "") -> list:
    """GUARDRAIL (2026-10-21, CA3 26-0000 run): federal rules and reporter-cited cases are read from THEIR OWN source key.
    Semantic top-k + party-slug pin missed Fed. R. Civ. P. 4/55 (sources frcp_rule_N), Rosenthal v. Walker, 111 U.S. 185
    (volume source USREPORTS-111_PDFA, no party in slug) and Cortez, 617 F.3d 688 (docvault_pull_f3d_617_688) and told
    the bots 'NOT IN CORPUS' -> 261/288 false GAP cells. Class fix: rule number -> rule source; volume+page -> case source."""
    m = _FEDRULE.search(c)
    if m:
        n = m.group(2)
        rows = []
        for pat in _FEDRULE_SRC.get(m.group(1).lower(), ()):   # UNION of the rule's own keys: courtlistener_frap_rule_4 holds
            rows += con.execute("SELECT chunk_id, text, source FROM chunk_text WHERE source=?", (pat.format(n=n),)).fetchall()   # 12k chars of note
        if rows:                                                # fragments, frap_rule_4 the 112k rule text — first-hit-wins lost 4(a)(2)
            return rows
        # C3b: no own key (e.g. Rule 77 — only the whole-rules compendium is held) -> the compendium chunks that NAME the
        # rule, body lines first (a heading 'Rule 77. Conducting Business' / '(d) Serving Notice' beats a TOC dot-leader line)
        rows = []
        for pre in _FEDRULE_FAM[m.group(1).lower()]["prefixes"]:
            if pre.endswith("_"):           # own-key prefixes were tried above
                continue
            rows += _src_range(con, pre, " AND text LIKE ?", (f"%Rule {n}%",))
        rows = [r for r in rows if rule_chunk_ok(c, r[2], r[1])]
        head_rx = re.compile(rf"\bRule\s+{re.escape(n)}\.\s+[A-Z]")
        rows.sort(key=lambda r: (0 if head_rx.search(r[1] or "") and "....." not in (r[1] or "") else 1))
        return rows[:6]
    m = _PARULE.search(c)
    if m:
        return _exact_pa_rule(con, c, m)
    kind, toks, _ = _cite_tokens(c)
    ms = list(_REPORTER.finditer(c))
    if not ms and text:           # cites_in() yields the case NAME only ('Rosenthal v. Walker'); the reporter follows it in the text
        name = re.sub(r"^(?:see also|see|cf\.|accord|but see)\s+", "", c.strip(), flags=re.I)
        k = text.find(name)
        if k >= 0:
            ms = list(_REPORTER.finditer(text[k + len(name): k + len(name) + 80]))
    if not ms:
        return _rows_for_keys(con, _keys_for_parties(con, toks)) if kind == "case" else []
    # parallel cites ('650 Pa. 1, 245 A.3d 637'): the first reporter the corpus keys by wins; the others are only parsed
    for m in ms:
        vol, rep, page = m.group(1), re.sub(r"[\s.]", "", m.group(2)).lower(), m.group(3)
        own = _rows_for_keys(con, _keys_for_reporter(con, vol, rep, page, toks))
        if own:
            return own
    vol, rep, page = ms[0].group(1), re.sub(r"[\s.]", "", ms[0].group(2)).lower(), ms[0].group(3)
    if rep == "us":
        if kind != "case" or not toks:
            return []
        for src in (f"USREPORTS-{vol}_PDFA", f"SCOTUS_vol_USREPORTS-{vol}_PDFA", f"usreports-{vol}_pdfa"):
            rows = con.execute("SELECT chunk_id, text, source FROM chunk_text WHERE source=? AND text LIKE ? LIMIT 60",
                               (src, f"%{toks[0]}%")).fetchall()
            if rows:
                return rows
        return []
    if rep.startswith("pa"):      # Pa. / Pa. Super. / Pa. Cmwlth.: no corpus key family carries the official PA reporters
        return []
    return _src_range(con, f"docvault_pull_{rep}_{vol}_{page}")


_T231_KEYS: list[str] = []


def _t231_keys(con, n: str) -> list[str]:
    """Every Title 231 source key that can hold Rule <n>: the per-rule stub (pa_code_t231_ch1028, hollow nav header) AND
    the chapter key (pa_code_t231_ch1000 — chapters are 100-aligned: 240 -> ch200, 1028/1033 -> ch1000). CLASS FIX
    2026-10-24 (order17 run6: 87/100 GAP "Rule 240(j) NOT IN CORPUS" while ch200 held the full text): the old lookup
    read ONLY ch<n>, so any rule without a stub key fell to Pennsylvania Bulletin fragments. _rank still decides."""
    global _T231_KEYS
    if not _T231_KEYS:
        _T231_KEYS = [r[0] for r in con.execute(
            "SELECT DISTINCT source FROM chunk_text WHERE source LIKE 'pa_code_t231_ch%' OR source LIKE 'courtlistener_pa_code_t231_ch%'")]
    try:
        ni = int(n)
    except ValueError:
        return [k for k in _T231_KEYS if k.endswith(f"ch{n}")]
    out = []
    for k in _T231_KEYS:
        ks = k.rsplit("ch", 1)[1]
        if ks.isdigit() and (int(ks) == ni or (int(ks) % 100 == 0 and int(ks) <= ni < int(ks) + 100)):
            out.append(k)
    return out


def _best_window(t: str, terms, size: int) -> str:
    """The <size>-char window of a long chunk that carries the most query terms (and any '(x)' subsection named in the
    terms), instead of the chunk's first <size> chars. CLASS FIX 2026-10-24: rules chapters chunk at ~4.8k chars; head-
    truncation handed the bots headings only and they (honestly) reported fragments / NOT IN CORPUS."""
    t = t or ""
    if len(t) <= size:
        return t
    words = {w.lower() for w in re.findall(r"[A-Za-z]{4,}", " ".join(terms) if not isinstance(terms, str) else terms)}
    subs = set(re.findall(r"\((?:[a-z]|\d{1,2})\)", " ".join(terms) if not isinstance(terms, str) else terms))
    best, best_s = -1.0, 0
    step = max(200, size // 4)
    for s in range(0, len(t) - size + step, step):
        w = t[s:s + size].lower()
        score = sum(1.0 for x in words if x in w) + sum(2.0 for x in subs if re.search(rf"(?:^|\s){re.escape(x)}\s*\S", w))
        if score > best:
            best, best_s = score, s
    if best_s:
        nl = t.rfind("\n", max(0, best_s - 150), best_s + 1)   # open on a line boundary when one is near
        if nl >= 0: best_s = nl + 1
    return t[best_s:best_s + size]


def _exact_pa_rule(con, c: str, m) -> list:
    """PA rule cite -> the rule's own text chunks, deterministically.
    Pa.R.A.P. <n>: pa_code_t210_ch<chapter> (chapter = the rule number without its last two digits: 1925 -> ch19,
    341 -> ch3, 1311 -> ch13), chunks naming 'Rule <n>', heading lines first.
    Pa.R.C.P. <n>: pa_code_t231_ch<n> when it holds the text (hollow today: 1 nav-header chunk), else the Pennsylvania
    Bulletin issue chunks (FTS phrase 'Rule <n>', sources <vol>-<issue> / <vol>_<issue>_cts) that name the rule; chunks
    carrying the cited subsection marker and a 'Rule <n>.' heading rank first."""
    fam = "pa_app" if m.group(1).upper() == "A" else "pa_civ"
    n = m.group(2)
    head_rx = re.compile(rf"\bRule\s+{re.escape(n)}\.\s+[A-Z]")
    sub = re.search(rf"{re.escape(n)}\s*\(([a-z0-9]{{1,3}})\)", c)
    sub_rx = re.compile(rf"(?m)(?:^|\s)\({re.escape(sub.group(1))}\)\s*\S") if sub else None

    def _rank(rows):
        rows = [r for r in rows if _pa_rule_chunk_ok(fam, n, r[2], r[1])]
        name_rx = re.compile(rf"\bRule\s+{re.escape(n)}\b")

        def key(r):
            t = r[1] or ""; toc = "....." in t
            h = head_rx.search(t)
            return ((0 if h and h.start() < 120 and not toc else 1),              # the rule's own heading opens the chunk
                    (0 if h and not toc else 1),                                   # the heading is somewhere inside
                    (0 if sub_rx and sub_rx.search(t) else 1),                     # the cited subsection marker
                    -len(name_rx.findall(t)))                                      # how often the rule is named
        rows.sort(key=key)
        return rows[:6]

    if fam == "pa_app":
        ch = n[:-2] if len(n) > 2 else n
        rows = []
        for pre in ("pa_code_t210_", "courtlistener_pa_code_t210_"):
            rows += con.execute("SELECT chunk_id, text, source FROM chunk_text WHERE source=? AND text LIKE ? LIMIT 60",
                                (f"{pre}ch{ch}", f"%Rule {n}%")).fetchall()
        return _rank(rows)
    rows = []
    for key in _t231_keys(con, n):                                                # per-rule stub key AND the chapter key
        rows += con.execute("SELECT chunk_id, text, source FROM chunk_text WHERE source=? AND text LIKE ? LIMIT 60",
                            (key, f"%Rule {n}%")).fetchall()
    rows = _rank(rows)
    if rows:
        return rows
    try:                                                                         # Pennsylvania Bulletin issues (FTS phrase, ~0.1 s)
        ids = [r[0] for r in con.execute("SELECT source_id FROM chunk_text_fts WHERE chunk_text_fts MATCH ? LIMIT 400",
                                         (f'"Rule {n}"',))]
    except Exception:
        ids = []
    rows = []
    for cid in ids:
        r = con.execute("SELECT chunk_id, text, source FROM chunk_text WHERE chunk_id=?", (cid,)).fetchone()
        if r and _PA_BULLETIN_KEY.match(r[2] or ""):
            rows.append(r)
    return _rank(rows)


def _cite_subsections(c: str) -> list[str]:
    """Subsection markers after the section/rule number, in order: 'Pa.R.C.P. 240(j)(1)' -> ['j','1'];
    '15 U.S.C. § 1681e(b)' -> ['b']; '15 U.S.C. § 1681s-2(a)(1)(A)' -> ['a','1','A'] (t_1972ad60: letter+dash section
    form, uppercase subsection markers); 'Rule 12' -> []."""
    m = re.search(r"\d+[a-zA-Z]?(?:[-–]\d+[a-zA-Z]?)?((?:\s*\([a-zA-Z0-9]{1,3}\))+)\s*$", c.strip()) or re.search(r"\d+[a-zA-Z]?(?:[-–]\d+[a-zA-Z]?)?((?:\s*\([a-zA-Z0-9]{1,3}\))+)")
    return re.findall(r"\(([a-zA-Z0-9]{1,3})\)", m.group(1)) if m else []


# Named Pennsylvania codes that print their rules as '2.11. Disqualification.' (no 'Rule' word, no Pa.R.x.P. family).
# Key = words that appear near the cite in the claim text; value = (source key, how the rule number prints).
_NAMED_CODES = (
    (("judicial conduct", "cjc", "canon"), "pa_code_t207_ch33"),
    (("professional conduct", "rpc", "disciplinary"), "pa_code_t204_ch81"),
    (("judicial administration", "pa.r.j.a", "r.j.a."), "pa_code_t201_ch1"),
)
_BARE_RULE = re.compile(r"^(?:Rule|Canon)\s+(\d{1,4}(?:\.\d{1,2})?)((?:\s*\([A-Za-z0-9]{1,3}\))*)\s*$")


def _dedupe_rows(rows, cap: int = 12) -> list:
    seen, out = set(), []
    for r in rows:
        if r[0] not in seen:
            seen.add(r[0]); out.append(r)
    return out[:cap]                                                  # anchoring + the embedder rank the windows after this


def _exact_named_rule(con, c: str, claim_text: str) -> list:
    """A bare 'Rule <n>' (cites_in strips the family) is resolved from the CLAIM TEXT around it: 'Code of Judicial Conduct
    Rule 2.11(A)' -> pa_code_t207_ch33; a claim that speaks Pa.R.A.P. -> Title 210 chapter keys; default civil ->
    Title 231 stub+chapter keys. CLASS FIX 2026-10-24 (order17 run7: CJC 2.11/2.3, Rule 341(c), Rule 1925(a) all 0 rows
    while the text sat in corpus). Deterministic; the embedder still ranks the windows."""
    m = _BARE_RULE.match(c.strip())
    if not m:
        return []
    n = m.group(1)
    ctx = claim_text.lower()                                          # claim-WIDE: a unit is about one thing; the code's name
    for words, key in _NAMED_CODES:                                   # usually sits in the title ('Rules 2.6(A), 2.2' in the body)
        if any(w in ctx for w in words):
            rows = con.execute("SELECT chunk_id, text, source FROM chunk_text WHERE source=? AND (text LIKE ? OR text LIKE ?)",
                               (key, f"%{n}. %", f"%Rule {n}%")).fetchall()
            return _dedupe_rows(_heading_first(rows, n))
    low = claim_text.lower()
    if "pa.r.a.p" in low or "appellate procedure" in low or "pa. r.a.p" in low:
        try:                                                          # Pa.R.A.P. chapters are 100-aligned by the rule's
            ni = int(n.split(".")[0])                                 # leading digits: 341 -> ch3, 1925 -> ch19, 2111 -> ch21
            keys = [f"pa_code_t210_ch{ni // 100}", f"pa_code_t210_ch{n}"]
        except ValueError:
            keys = []
    elif re.search(r"fed\. ?r\.|federal rule", low):
        return []                                                     # federal bare rules keep the existing F.R. path
    else:
        keys = _t231_keys(con, n.split(".")[0])
    rows = []
    for k in keys:
        rows += con.execute("SELECT chunk_id, text, source FROM chunk_text WHERE source=? AND text LIKE ? LIMIT 60", (k, f"%Rule {n}%")).fetchall()
    return _dedupe_rows(_heading_first(rows, n))


def _heading_first(rows, n: str) -> list:
    """Chunks that OPEN with the rule's own heading ('Rule 1024. Verification.' / '2.11. Disqualification.') rank before
    chunks that merely mention the number (a 'Rule 1027 … see Rule 1024' source note is not Rule 1024). Deterministic."""
    head = re.compile(rf"(?:^|\n)\s*(?:Rule\s+)?{re.escape(n)}\.\s+[A-Z]")
    body = re.compile(rf"(?:Rule\s+|\n\s*){re.escape(n)}\.\s+[A-Z]")
    return sorted(rows, key=lambda r: (0 if head.search(r[1] or "") else 1 if body.search(r[1] or "") else 2))


# S-m2-ps-dash-alias (2026-10-24) — CLASS RULE: Purdon's dash-form numbers 'A-B' are <chapter prefix>-<act section>: 73 P.S.
# § 201-2 = UTPCPL § 2, § 201-9.2 = § 9.2 (the enacted text in corpus prints 'Section 2.' / 'Section 9.2.' and never '201-').
# Defect: the regex stopped at the dash, searched the act for '201' (0 hits) and declared the UTPCPL NOT IN CORPUS while
# pa_73ps_utpcpl (47 chunks) was sitting there — 7 order17 cells. Any title, any act: the act section is the part after the dash.
_PS_ALIAS = re.compile(r"(\d{1,3})\s*P\.S\.\s*§§?\s*([\d.]+(?:-[\d.]+)?)(?:[^()]*\((?:[^()]*?)§\s*([\d]+(?:-[A-Z])?)\))?")


def _exact_ps_alias(con, c: str, claim_text: str = "") -> list:
    """Purdon's (P.S.) numbers are a codifier's overlay; the ENACTED act in corpus (pa_<T>ps_<act>) prints 'Section 5.' /
    '611-A'. A cite may carry the act section in parentheses — '40 P.S. § 1171.5 (Act 1974-205, § 5)' — and the resolver
    searches every pa_<T>ps_* source for the P.S. number first, then the act section. Agnostic: any title, any act."""
    m = _PS_ALIAS.search(c)
    if not m:
        return []
    title, ps, act_sec = m.group(1), m.group(2), m.group(3)
    if "-" in ps and not act_sec:                                     # S-m2-ps-dash-alias: '201-9.2' -> act section '9.2'
        act_sec = ps.split("-", 1)[1]
    paren = ""
    if claim_text:                                                    # cites_in trims the cite; the drafter's parenthetical that
        i = claim_text.find(c)                                        # FOLLOWS it in the claim carries the act section:
        if i >= 0:                                                    # '40 P.S. § 1171.4 (… P.L.589, No.205, § 4)'
            pm = re.match(r"[^()]{0,6}\(([^()]{0,200})\)", claim_text[i + len(c):])
            if pm:
                paren = pm.group(1)
                sm = re.search(r"§\s*(\d+(?:-[A-Z])?)", paren)
                if sm and not act_sec:
                    act_sec = sm.group(1)
    keys = [r[0] for r in con.execute("SELECT DISTINCT source FROM chunk_text WHERE source LIKE ?", (f"pa_{title}ps_%",))]
    if len(keys) > 1:                                                 # several enacted acts share a P.S. title: pick by the
        norm = lambda s: re.sub(r"\s+", "", s or "")                   # act's OWN cite stamped on its chunks at ingest
        ids = [norm(x) for x in re.findall(r"(?:P\.L\.\s*\d+|No\.\s*\d+)", paren)]
        base = ps.split(".")[0]                                       # '1171.4' -> '1171' matches '§ 1171.1 et seq.'
        scored = []
        for k in keys:
            cites = norm(" ".join(r[0] or "" for r in con.execute("SELECT DISTINCT citations FROM chunk_text WHERE source=?", (k,))))
            s = sum(1 for x in ids if x in cites) + (2 if re.search(rf"§+{re.escape(base)}\.", cites) else 0)
            scored.append((s, k))
        top = max(s for s, _ in scored)
        if top > 0:
            keys = [k for s, k in scored if s == top]
    rows = []
    for k in keys:
        rows += con.execute("SELECT chunk_id, text, source FROM chunk_text WHERE source=? AND text LIKE ? LIMIT 40", (k, f"%{ps}%")).fetchall()
    if not rows and act_sec:
        pat = f"%Section {act_sec}.%" if "-" not in act_sec else f"%Section {act_sec}%"
        for k in keys:
            rows += con.execute("SELECT chunk_id, text, source FROM chunk_text WHERE source=? AND (text LIKE ? OR text LIKE ?) LIMIT 40", (k, pat, f"%{act_sec} %")).fetchall()
        subs = re.findall(r"\(([ivxlc]+|\d+|[a-z])\)", c[m.end(2):])          # '(4)(xxi)' -> the chunk that carries the sub-clause first
        if subs and rows:
            tag = f"({subs[-1]})"
            rows = sorted(rows, key=lambda r: 0 if tag in (r[1] or "") else 1)
    if not rows and claim_text:                                       # last anchor: the words the claim QUOTES from the act
        i = max(claim_text.find(c), 0)
        for q in re.findall(r"[\"“]([^\"”]{30,240})[\"”]", claim_text[i: i + 900]):
            words = re.findall(r"[A-Za-z]{3,}", q.split("…")[0])[:6]  # enacted text wraps mid-phrase: match the words
            if len(words) < 4:                                        # with wildcards between them, inside the act only
                continue
            needle = "%" + "%".join(words) + "%"
            for k in keys:
                rows += con.execute("SELECT chunk_id, text, source FROM chunk_text WHERE source=? AND text LIKE ? LIMIT 20", (k, needle)).fetchall()
            if rows:
                break
    if act_sec and rows:                                              # the section heading first; the TOC line last
        hd = re.compile(rf"Section {re.escape(act_sec)}\.\s*[A-Z][^\n]{{0,80}}(?:\.--|--)")
        rows.sort(key=lambda r: (0 if hd.search(r[1] or "") else 1))
    return _dedupe_rows(rows)


def _cite_section_token(c: str) -> str:
    """The section/rule token right before the first subsection marker: 'Pa.R.C.P. 240(j)(1)' -> '240';
    '15 U.S.C. § 1681e(b)' -> '1681e'; '15 U.S.C. § 1681s-2(a)(1)(A)' -> '1681s-2' (t_1972ad60: the dash form);
    'Pa.R.C.P. 1035.2(a)' -> '1035.2'."""
    m = re.search(r"(\d{1,4}(?:\.\d{1,2})?[a-z]?(?:[-–]\d+[a-z]?)?)\s*\(", c)
    return m.group(1) if m else ""


def _rule_region(t: str, tok: str) -> tuple[int, int, bool]:
    """[start, end, strong) of the cited rule's OWN text inside a chapter chunk: from its heading ('Rule 240. In Forma
    Pauperis.' / '§ 1681e. Compliance') to the next rule/section heading (strong=True). Chunks that start mid-rule carry
    no heading: the whole chunk is searched but marked weak (strong=False) so a heading-anchored chunk outranks it.
    Chapter chunks hold several rules and every rule has an '(a)'; without this scope the anchor lands in a neighbour
    (order17 run7: 1033(a) resolved to the 1034/1032 chunk)."""
    if not tok:
        return 0, len(t), False
    head = re.compile(rf"(?:^|\n|\.\s+|\)\s+)(?:(?:Rule|§|Sec\.?|Section)\s*)?{re.escape(tok)}\.\s+[A-Z]")
    m = head.search(t)
    if not m:
        return 0, len(t), False
    nxt = re.compile(r"(?:\n|\.\s+)(?:Rule|§|Sec\.?|Section)\s*\d{1,4}(?:\.\d{1,2})?[a-z]?\.\s+[A-Z]")
    n = nxt.search(t, m.end())
    return m.start(), (n.start() if n else len(t)), True


_XREF_WORDS = re.compile(r"\b(?:subdivisions?|subsections?|paragraphs?|clauses?|subparagraphs?|items?)\s*$", re.I)   # whole words only


def _next_sibling(s: str) -> str | None:
    if s.isdigit(): return str(int(s) + 1)
    if len(s) == 1 and s.isalpha() and s not in "z": return chr(ord(s) + 1)
    return None


def _subsection_anchors(t: str, subs: list[str], lead: int = 120, tok: str = "") -> list[tuple[int, bool]]:
    """(start, strong) of the window that OPENS on the cited subsection's OWN text, walking the hierarchy the way
    statutes print it: '(c)' then — inside (c), before '(d)' — '(2)' ('(c)(1) … (2) The court shall…' is 1028(c)(2);
    the markers are NOT contiguous). Each marker must sit at a line start / after whitespace, be followed by text or
    the next marker, and not be a cross-reference ('pursuant to subdivision (j)(1).'). Deterministic; [] when the
    cited subsection is not in this chunk."""
    if not subs or not t:
        return []
    lo, hi, strong = _rule_region(t, tok)
    if len(subs) > 1:                                                # contiguous print form first: '(j)(1) If, …'
        seq = r"\s*".join(rf"\({re.escape(s)}\)" for s in subs)
        for m in re.compile(rf"(?:^|\n|\s)({seq})\s*(?=[A-Z\"“‘'])").finditer(t, lo, hi):
            if not _XREF_WORDS.search(t[max(0, m.start() - 24): m.start()]):
                return [(max(0, m.start() - lead), strong)]
    pos, end, last = lo, hi, None                                    # else walk the hierarchy: '(c) … (1) … (2) The…'
    for s in subs:
        rx = re.compile(rf"(?:^|\n|\s)\({re.escape(s)}\)\s*(?=\(|[A-Z\"“‘'])")
        found = None
        for m in rx.finditer(t, pos, end):
            if _XREF_WORDS.search(t[max(0, m.start() - 24): m.start()]):
                continue
            found = m; break
        if not found:
            return []
        last, pos = found, found.end()
        sib = _next_sibling(s)
        if sib:
            n = re.compile(rf"(?:^|\n|\s)\({re.escape(sib)}\)\s*(?=\(|[A-Z\"“‘'])").search(t, pos, end)
            if n: end = n.start()
    return [(max(0, last.start() - lead), strong)]


def _exact_statute(c: str, claim_text: str, per: int, con=None) -> list[dict]:
    """GUARDRAIL (2026-10-19): a named statute is read from ITS OWN source key, not hoped for in a semantic top-k.
    '5 U.S.C. § 552(a)(6)(A)(i)' -> source usc_t5_s552 (20 chunks); '42 Pa.C.S. § 8371' -> pa_cs_t42 chunks naming
    § 8371. Within the source, chunks are ranked by the subsection markers and the claim's words (the bot reads them).
    `con` (S-m2-cure): a caller-supplied read-only chunk_text connection runs the pin path WITHOUT the embedder (probes,
    torch-free tests); the live runner passes none and uses the shared turbovec connection."""
    try:
        if con is None:
            _, _, _, _, _, con, _, lock = _tv()
        else:
            lock = threading.Lock()
        m = _USC.search(c); rows = []
        with lock:
            if m:
                for slug in dict.fromkeys([f"usc_t{m.group(1)}_s{m.group(2)}", f"usc_t{m.group(1)}_s{m.group(2).replace('-', '–')}"]):
                    rows = rows or con.execute("SELECT chunk_id, text, source FROM chunk_text WHERE source=?", (slug,)).fetchall()
            elif _NJX.search(c):
                m = _NJX.search(c)
                fam = "njac" if "A.C" in m.group(1).upper() else "nj_stat"
                rows = con.execute("SELECT chunk_id, text, source FROM chunk_text WHERE source=?",
                                   (f"{fam}_t{m.group(2).lower()}_s{m.group(3).lower()}",)).fetchall()
            else:
                m = _PACS.search(c)
                if m:
                    rows = con.execute("SELECT chunk_id, text, source FROM chunk_text WHERE source=? AND (text LIKE ? OR text LIKE ?)",
                                       (f"pa_cs_t{m.group(1)}", f"%§ {m.group(2)}%", f"%&#167; {m.group(2)}%")).fetchall()
            if not rows:
                rows = _exact_named_rule(con, c, claim_text)         # bare 'Rule 2.11' / 'Rule 341(c)' / 'Code of Judicial Conduct …'
            if not rows:
                rows = _exact_ps_alias(con, c, claim_text)            # '40 P.S. § 1171.5 (Act 1974-205, § 5)' -> the enacted act's own section
            if not rows:
                rows = _exact_rule_or_reporter(con, c, claim_text)
        if not rows: return []
        win, anchored, anch_w = [], [], []                            # statute chunks run 3-8k chars; a bot sees 1.4k:
        subs, tok = _cite_subsections(c), _cite_section_token(c)     # '240(j)(1)' -> ['j','1'], '240'; '12(d)' -> ['d'], '12'
        for cid, t, src in rows:                                     # slice into overlapping windows so the operative
            t = t or ""                                              # subsection is IN what the bot reads
            for s, strong in _subsection_anchors(t, subs, tok=tok):  # CLASS FIX 2026-10-24: the cited subsection's own
                anchored.append((cid, t[s:s + 1400], src))           # window FIRST, found by its marker — not hoped for
                anch_w.append(1.0 if strong else 0.5)                # heading-scoped anchor outranks a mid-rule chunk's
            for i in range(0, max(len(t) - 200, 1), 1100):           # by embedding rank (order17 run7: 240(j)(1) windows
                win.append((cid, t[i:i + 1400], src))                # came back as Rule 239.6 / 240(i) text, 85/102 GAP)
        rows = anchored + win
        n_anch = len(anchored)
        if len(rows) > max(per, 3):                                  # rank the source's own windows by MEANING vs the claim
            import numpy as np
            sub = re.search(r"\d\s*\(([a-zA-Z0-9]{1,3})\)", c)        # 'Fed. R. Civ. P. 55(a)' -> the '(a)' window first (t_1972ad60: uppercase markers too)
            subb = [1.0 if sub and re.search(rf"(?m)(?:^|\s)\({re.escape(sub.group(1))}\)\s+[A-Z]", t or "") else 0.0 for _, t, _ in rows]
            notes = [1.0 if re.search(r"(?i)\b(amendments|prior provisions|effective date of|short title|editorial notes|references in text|committee notes?)\b", (t or "")[:300]) else 0.0 for _, t, _ in rows]
            qvec = RC.embed([f"{c} {(claim_text or '')[:600]}"], is_query=True).get("vectors")
            cvec = []
            if qvec:                                                 # daemon embedder (batches <= 64); RC.embed never raises
                for i in range(0, len(rows), 64):
                    v = RC.embed([(t or "")[:1200] for _, t, _ in rows[i:i + 64]]).get("vectors")
                    if not v:
                        cvec = []; break
                    cvec += v
            if qvec and len(cvec) == len(rows):
                sims = np.asarray(cvec, dtype="float32") @ np.asarray(qvec[0], dtype="float32")
            else:                                                    # daemon down / degraded: subsection + notes only (deterministic)
                sims = np.zeros(len(rows))
            anch = np.array(anch_w + [0.0] * (len(rows) - n_anch))
            order = np.argsort(-(sims - 0.15 * np.array(notes) + 0.2 * np.array(subb) + 1.0 * anch), kind="stable")
            rows = [rows[i] for i in order]
        return [{"chunk_id": cid, "source": f"cite:{c} chunk={cid}", "part": None, "recital": False, "text": (t or "")[:1400]}
                for cid, t, _src in rows[:max(per, 3)]]
    except Exception:
        return []


def _pull_cites_tv(text: str, per: int = 2, con=None, retrieve_fn=None) -> list[dict]:
    """Named cites -> their own chunks. `con`/`retrieve_fn` (S-m2-cure) let a probe run this torch-free (read-only sqlite +
    an FTS stand-in for `_retrieve`); the live runner passes neither. C3c: no chunk id leaves here unless chunk_text holds it."""
    out = []
    own = con is None
    if own:
        con = _ro_con()
    retrieve_fn = retrieve_fn or (lambda qs, fams, k: _retrieve(qs, fams, k=k))
    try:
        for c in cites_in(text):
            exact = [e for e in _exact_statute(c, text, per, con=con) if _chunk_exists(con, e.get("chunk_id"))]
            if exact:
                out += exact; continue
            kind, toks, fam = _cite_tokens(c)
            lead = toks[0] if toks else c
            hits = [h for h in retrieve_fn([c, f"{c} held", f"{lead} opinion"], [], 12) if not h.get("error")]
            pinned = [h for h in hits if _pins(kind, toks, fam, h, cite=c, claim=text) and _chunk_exists(con, h.get("chunk_id"))][:per]
            for h in pinned:
                out.append({"chunk_id": h["chunk_id"], "source": f"cite:{c} chunk={h['chunk_id']}", "part": h.get("part"),
                            "recital": bool(_RECITAL.search(h.get("text") or "")), "text": _best_window(h.get("text") or "", [c, text[:400]], 1400)})
            if not pinned:
                out.append({"chunk_id": None, "source": f"cite:{c}", "text": f"(NOT IN CORPUS by semantic+pin: {c} — no chunk names this authority; say so, do not characterize its holding)"})
    finally:
        if own:
            con.close()
    return out


def _pull_cites(text: str, per: int = 2) -> list[dict]:
    """FTS the named authorities (phrase) → their chunks. These are what the bot must quote from."""
    out = []
    for c in cites_in(text):
        q = re.sub(r"\s*§+\s*", " ", c)                              # '42 Pa.C.S. § 8371' → '42 Pa.C.S. 8371' for FTS
        try:
            d = _get(f"{CORPUS}/search_text?query={urllib.parse.quote(q)}&phrase=true&top_k={per}", timeout=90)
        except Exception:
            continue
        for h in d.get("hits") or []:
            out.append({"chunk_id": h.get("chunk_id"), "source": f"cite:{c}", "part": h.get("part"), "text": (h.get("text") or "")[:1400]})
        if not d.get("hits"):
            out.append({"chunk_id": None, "source": f"cite:{c}", "text": f"(NOT IN CORPUS by phrase: {c})"})
    return out


def verify_quote(quote: str) -> dict:
    """GUARDRAIL: the quoted words must exist in the corpus (FTS phrase). Returns {ok, source, chunk_id}.
    S-m2-cure: checks chunk_text_fts in-process first (read-only sqlite, no HTTP, no model) and only returns a chunk id
    that chunk_text holds (C3c); the corpus service is the fallback when the local db is unavailable."""
    q = re.sub(r"\s+", " ", quote or "").strip().strip('"“”')
    if len(q) < 12:
        return {"ok": False, "why": "quote too short to verify"}
    phrase = " ".join(q.split()[:12])                        # first 12 words — enough to pin a chunk, robust to OCR tails
    try:
        con = _ro_con()
        try:
            fts = '"' + phrase.replace('"', '""') + '"'
            for (cid,) in con.execute("SELECT source_id FROM chunk_text_fts WHERE chunk_text_fts MATCH ? LIMIT 5", (fts,)):
                r = con.execute("SELECT source FROM chunk_text WHERE chunk_id=?", (cid,)).fetchone()
                if r:
                    return {"ok": True, "source": r[0], "chunk_id": cid}
            return {"ok": False, "why": "NOT IN CORPUS (phrase not found)"}
        finally:
            con.close()
    except Exception:
        pass                                                 # local db unavailable -> corpus service
    try:
        d = _get(f"{CORPUS}/search_text?query={urllib.parse.quote(phrase)}&phrase=true&top_k=1", timeout=60)
    except Exception as e:
        return {"ok": False, "why": f"fts error: {e}"}
    hits = d.get("hits") or []
    if not hits:
        return {"ok": False, "why": "NOT IN CORPUS (phrase not found)"}
    h = hits[0]
    cid = h.get("chunk_id") or h.get("id")
    # C3c (t_1972ad60): the service's index can run ahead of (or behind) the local chunk_text db — a fallback id must be
    # verified against local when local is reachable; an unverified id is dropped (fail-open: the quote may still be ok),
    # never asserted.
    if cid:
        try:
            c2 = _ro_con()
            try:
                if c2.execute("SELECT 1 FROM chunk_text WHERE chunk_id=? LIMIT 1", (cid,)).fetchone() is None:
                    cid = None
            finally:
                c2.close()
        except Exception:
            pass
    return {"ok": True, "source": h.get("source"), "chunk_id": cid}


# ------------------------------------------------------------------ lane
REP_PENALTY = float(os.environ.get("M2_REP_PENALTY", "1.05"))   # LOOP GUARD: greedy @C8 degenerates into repeat loops
                                                                  # (author's sweep flags LOOP at C8; slice run had a 169 s UNPARSED cell)
_LAST_USAGE: dict = {}
CLAIM_BUDGET = int(os.environ.get("M2_CLAIM_BUDGET", "2500"))   # PREFILL GUARD: an adverse-brief section runs 3-6k chars; 4 such
                                                                  # prompts at once crashed the local-gpu (gdn_attention OUT_OF_RESOURCES)
                                                                  # and wedged the local-gpu on 2026-10-13. Same budget as the facts slice.


def chat(system: str, user: str, max_tokens: int = MAX_TOKENS, timeout: int = 900) -> str:
    model = MODEL or _get(f"{LANE}/models", 10)["data"][0]["id"]
    body = {"model": model, "temperature": 0, "max_tokens": max_tokens,   # greedy: spec-decode acceptance ~2x at T=0 (HyperQwen #156)
            "repetition_penalty": REP_PENALTY,
            "chat_template_kwargs": {"enable_thinking": False}, "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    raw = _post(f"{LANE}/chat/completions", body, timeout=timeout)
    ch = raw["choices"][0]; msg = ch.get("message", {})
    u = raw.get("usage") or {}
    _LAST_USAGE[threading.get_ident()] = {"prompt_tokens": u.get("prompt_tokens"), "completion_tokens": u.get("completion_tokens"),
                                          "finish_reason": ch.get("finish_reason")}
    return (msg.get("content") or msg.get("reasoning_content") or "").strip()


def parse_json(s: str) -> dict:
    s = re.sub(r"<think>.*?</think>", "", s, flags=re.S)
    i = s.find("{"); j = s.rfind("}")
    if i < 0 or j < 0:
        return {"status": "UNPARSED", "raw": s[:1500]}
    body = s[i:j + 1]
    for cand in (body,
                 body.replace('\\\\"', '\\"'),                                        # model double-escaped quotes  \\" → \"
                 re.sub(r'(?<!\\)\\(?!["\\/bfnrtu])', r'\\\\', body),                   # stray backslashes
                 body.rstrip(", \n") + ("]}" if body.count("[") > body.count("]") else "}")):   # truncated tail → close it
        try:
            return json.loads(cand)
        except Exception:
            continue
    return {"status": "UNPARSED", "raw": s[:1500]}


# ------------------------------------------------------------------ one (bot × claim)
OUTPUT_SCHEMA = '''Reply with ONE JSON object and nothing else:
{"status": "GROUNDED" | "GAP" | "CONFLICT",
 "level": 1-7 (the HIGHEST hierarchy level your support rests on; see HIERARCHY),
 "finding": "one paragraph: what this claim needs on your axis and whether the draft has it",
 "proposition": "1-3 sentences a COURT reads, in filing voice: the rule from your verified authority and its application to this case. Written as the brief would state it (e.g. 'Wenk addressed an insurer's handling of a fire-loss claim; it does not speak to premiums collected on a policy after the insured vehicle was sold.'). NEVER mention the claim, the draft, the corpus, the excerpts, the opponent's characterization, or your axis — no 'the claim asserts', 'the corpus confirms', 'the draft correctly'. Empty when status is GAP with no authority.",
 "authorities": [{"cite": "...", "level": 1-7, "quote": "exact words from the corpus excerpt (12+ words)", "source": "chunk source key"}],
 "gap": "what is missing, or empty",
 "fix": "the exact sentence(s) to add/replace, or empty",
 "codification_only": true|false (true when the statute is cited by USC/Pa.C.S. and no enacted P.L./Stat. text was available)}
Rules: cite ONLY from the CORPUS EXCERPTS below. If the excerpts do not support the claim on your axis, say GAP and name
what is NOT IN CORPUS — never invent a cite, a quote, a docket number, a date or an amount.'''


def fetch_cell_parts(bot: dict, claim: dict) -> tuple[list[dict], list[dict]]:
    """(pinned, axis) — the named authorities of the claim (shared by every bot of the unit) and the bot's own
    family/query retrieval. fetch_cell() is their concatenation, unchanged; both halves are the same cached calls.
    S-m2-speed: the split lets build_prompt place the pinned half in the unit-shared head (M2_PREFIX_ORDER=1)."""
    fams = forum_families(bot.get("families") or [])                 # S-m2-forum-families: semantic families forum-filtered; pins never
    queries = [claim["title"], claim["text"][:300]] + [q.replace("{claim}", claim["title"]) for q in (bot.get("queries") or [])]
    return pull_cites(claim["text"]), retrieve(queries, fams)        # named authorities (unfiltered), the axis's families (filtered)


def fetch_cell(bot: dict, claim: dict) -> list[dict]:
    """Retrieval only (cached). S-m2-retrieval-pipelined (2026-10-24): main() runs this per cell in a small pool and submits the
    cell to the lane as soon as it returns (no whole-matrix barrier); the lane is still measured on model time per cell.
    S-m2-merged2: = fetch_cell_parts() concatenated (pinned + axis), the list main()'s prefetch hands to run_cell(ex=)."""
    pinned, axis = fetch_cell_parts(bot, claim)
    return pinned + axis                                               # named authorities FIRST (unfiltered), then the axis's families


_WORD = re.compile(r"[a-z][a-z0-9§\.\-]{3,}")
_STOP = set("that this with from have been were their there which shall under upon into such than then they them these those also only other about after before because where while would could should does each both more most must into over very when what".split())


def _terms(text: str) -> set:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP}


def slice_facts(facts: str, claim: dict, bot: dict, budget: int = 3000) -> str:
    """Per-claim CASE_FACTS slice, same byte budget as the old facts[:3000] (so speed is unchanged):
    anchor = the PARTIES block (always); then paragraphs ranked by term overlap with the claim (+ the bot's
    queries), highest first, until the budget is spent. Deterministic — no model picks the facts."""
    blocks, cur, head = [], [], ""
    for line in facts.splitlines():
        if line.startswith("#"):
            if cur: blocks.append((head, "\n".join(cur).strip())); cur = []
            head = line.strip("# ").strip()
        else:
            cur.append(line)
    if cur: blocks.append((head, "\n".join(cur).strip()))
    paras = [(h, p) for h, b in blocks for p in re.split(r"\n\s*\n", b) if p.strip()]
    anchor = next((f"## {h}\n{p}" for h, p in paras if "PARTIES" in h.upper()), "")
    anchor = anchor[: budget // 3]
    want = _terms(claim["title"] + " " + claim["text"]) | _terms(" ".join(bot.get("queries") or []))
    scored = []
    for h, p in paras:
        if "PARTIES" in h.upper() or "EXHIBIT REGISTER" in h.upper(): continue
        t = _terms(p); ov = len(t & want)
        if ov: scored.append((ov / (len(t) ** 0.5 + 1), h, p))
    scored.sort(key=lambda x: -x[0])
    out, used = [anchor] if anchor else [], len(anchor)
    for _, h, p in scored:
        piece = f"## {h}\n{p}"
        if used + len(piece) + 2 > budget: continue
        out.append(piece); used += len(piece) + 2
    return "\n\n".join(out)


# ------------------------------------------------------------------ S-m2-record-docs-to-cells (2026-10-24)
# CLASS RULE: RECORD documents (the adverse order, the complaint, exhibits) live in the CASE TREE and are cited [doc:<path>];
# they are never searched in the corpus, and NOT IN CORPUS applies to LAW only. DEFECT (order17 run9, 5 cells): "The text of the
# October 21, 2026 order is NOT IN CORPUS" / "The complaint text itself is NOT IN CORPUS" — the bot was handed the law corpus
# and looked there for the record. Fix: (a) the units header names the adverse paper -> its OCR text goes into EVERY cell;
# (b) a unit naming a pleading ('Complaint ¶¶ 12-14', 'Count V') gets that pleading's matching paragraphs/count; (c) the prompt
# block says the rule, and a 'not in corpus' answer about a record document is reclassified RECORD_SUPPLIED (never a GAP).
ADVERSE_DOC_CAP = 6000      # chars of the adverse paper per cell. An order is < 1k; a brief's first 6k (caption + relief) is
                            # what a cell needs to quote; anything longer is a law question the pins answer, not a record one.
PLEADING_CAP = 4000         # chars of pleading excerpt per cell (count section(s) + cited paragraphs), same prefill guard as CLAIM_BUDGET
PLEADING_SECTION_CAP = 2500 # one count section
PLEADING_PARA_CAP = 900     # one numbered paragraph
RECORD_HEAD = ("=== RECORD DOCUMENTS (from the case tree — NOT the corpus. Quote them as [doc:<path>]; never search the corpus for "
               "them and never say NOT IN CORPUS about them: NOT IN CORPUS applies to LAW only)")
RECORD_WORDS = ("order", "complaint", "motion", "docket", "exhibit", "pleading", "petition")
_RECORD: dict = {"adverse": None, "pleading": None}    # (path, text) | None — set once per run by prime_record_docs()
_CTRL_RX = re.compile(r"Control\s*No\.?\s*:?\s*(\d{6,10})", re.I)
_CASENO_RX = re.compile(r"(?:No\.?|Case\s*ID:?|docket)\s*:?\s*(\d{9,})", re.I)
_OCR_GLOBS = ("intake/_ocr/*.txt", "intake/*.txt", "intake/**/*.txt")


def _case_tree_files(case_root: Path, globs=_OCR_GLOBS, max_bytes: int = 400_000) -> list[Path]:
    out = []
    for g in globs:
        for p in sorted(case_root.glob(g)):
            if p.is_file() and p not in out and p.stat().st_size <= max_bytes and ".exhibit." not in p.name:
                out.append(p)
    return out


def resolve_adverse_doc(units_text: str, case_root: Path | None) -> tuple[str, str] | None:
    """The adverse paper the units file is about -> (path, text). M2_ADVERSE_DOC=<path> wins; else the units HEADER (first
    non-empty line) is read for its Control No. / case no. and intake/_ocr/*.txt is scanned: a file whose CONTENT carries the
    Control No. scores 4, 'order' in its name 2, the case no. in name or content 1; best score >= 2 wins, ties -> the
    shortest file (an order is one page; the complaint OCR also carries the case no.). None when nothing qualifies."""
    env = os.environ.get("M2_ADVERSE_DOC", "")
    if env and Path(env).is_file():
        return str(Path(env)), Path(env).read_text(encoding="utf-8", errors="replace")
    if not case_root or not Path(case_root).is_dir():
        return None
    head = next((l for l in (units_text or "").splitlines() if l.strip()), "")
    ctrls = set(_CTRL_RX.findall(head)); casenos = set(_CASENO_RX.findall(head))
    best = None
    for p in _case_tree_files(Path(case_root)):
        try:
            t = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        s = (4 if any(c in t for c in ctrls) else 0) + (2 if "order" in p.name.lower() else 0) \
            + (1 if any(n in p.name or n in t[:4000] for n in casenos) else 0)
        if s >= 2 and (best is None or (s, -len(t)) > (best[0], -len(best[2]))):
            best = (s, str(p), t)
    return (best[1], best[2]) if best else None


def resolve_pleading(case_root: Path | None) -> tuple[str, str] | None:
    """The as-filed complaint -> (path, text). M2_PLEADING_DOC=<path> wins; else intake/_ocr/*complaint*.txt ('asfiled'/'filed'
    in the name first), else the newest drafts/*complaint*FINAL*.md. None when the case tree holds no complaint text."""
    env = os.environ.get("M2_PLEADING_DOC", "")
    if env and Path(env).is_file():
        return str(Path(env)), Path(env).read_text(encoding="utf-8", errors="replace")
    if not case_root or not Path(case_root).is_dir():
        return None
    root = Path(case_root)
    ocr = [p for p in _case_tree_files(root) if "complaint" in p.name.lower()]
    ocr.sort(key=lambda p: (0 if re.search(r"as_?filed|_filed", p.name, re.I) else 1, -p.stat().st_size))
    cands = ocr or sorted((p for p in root.glob("drafts/*complaint*FINAL*.md") if p.is_file()), key=lambda p: -p.stat().st_mtime)
    if not cands:
        return None
    return str(cands[0]), cands[0].read_text(encoding="utf-8", errors="replace")


_ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8, "IX": 9, "X": 10, "XI": 11, "XII": 12}
_REF_COUNT = re.compile(r"\bCount\s+([IVX]{1,4}|\d{1,2})\b")
_REF_PARA = re.compile(r"(?:Complaint\s*)?¶¶?\s*(\d{1,3}(?:\s*[-–]\s*\d{1,3})?(?:\s*,\s*\d{1,3}(?:\s*[-–]\s*\d{1,3})?)*)")
_PLEAD_COUNT_HEAD = re.compile(r"(?m)^\s*(?:#+\s*)?COUNT\s+([IVX]{1,4}|\d{1,2})\b[^\n]*$")


def slice_pleading(text: str, claim_text: str, cap: int = PLEADING_CAP) -> str:
    """The pleading's OWN words for what the unit references: each 'Count N' -> that count's section (heading to the next
    COUNT heading, PLEADING_SECTION_CAP); each '¶ n' / '¶¶ a-b' -> the numbered paragraph(s) 'n. ...' (PLEADING_PARA_CAP).
    Deterministic, de-duplicated, in reference order, total <= cap. '' when the unit references nothing or nothing matches."""
    if not text or not claim_text:
        return ""
    want_counts, want_paras = [], []
    for m in _REF_COUNT.finditer(claim_text):
        n = _ROMAN.get(m.group(1).upper()) if not m.group(1).isdigit() else int(m.group(1))
        if n and n not in want_counts: want_counts.append(n)
    for m in _REF_PARA.finditer(claim_text):
        for part in re.split(r"\s*,\s*", m.group(1)):
            lo, _, hi = re.sub(r"\s", "", part).partition("-") if "-" in part else (part.strip(), "", "")
            lo = lo.replace("–", ""); hi = hi.replace("–", "")
            try:
                a, b = int(lo), int(hi or lo)
            except ValueError:
                continue
            for n in range(a, min(b, a + 30) + 1):
                if n not in want_paras: want_paras.append(n)
    if not want_counts and not want_paras:
        return ""
    pieces, used = [], 0
    heads = list(_PLEAD_COUNT_HEAD.finditer(text))
    for n in want_counts:
        for i, h in enumerate(heads):
            hn = _ROMAN.get(h.group(1).upper()) if not h.group(1).isdigit() else int(h.group(1))
            if hn != n: continue
            end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
            sec = text[h.start():end].strip()[:PLEADING_SECTION_CAP]
            if used + len(sec) > cap: break
            pieces.append(sec); used += len(sec); break
    for n in want_paras:
        m = re.search(rf"(?m)^\s*{n}\.\s+\S", text)
        if not m: continue
        nxt = re.compile(r"(?m)^\s*(?:\d{1,3}\.\s+\S|(?:#+\s*)?COUNT\s+(?:[IVX]{1,4}|\d{1,2})\b|WHEREFORE\b|PRAYER\b)").search(text, m.end())   # the paragraph ends at the NEXT numbered paragraph / count heading / prayer
        para = text[m.start():(nxt.start() if nxt else len(text))].strip()[:PLEADING_PARA_CAP]
        if any(para in p for p in pieces): continue                      # already inside a count section
        if used + len(para) > cap: break
        pieces.append(para); used += len(para)
    return "\n\n".join(pieces)


def record_docs_block(claim: dict, record: dict | None = None) -> str:
    """The RECORD DOCUMENTS block for one cell: the adverse paper (every cell, <= ADVERSE_DOC_CAP) + the pleading excerpt this
    unit references (when any). '' when the run has no record documents — the prompt is then byte-identical to the live one."""
    rec = _RECORD if record is None else record
    parts = []
    adv = rec.get("adverse")
    if adv:
        parts.append(f"[doc: {adv[0]}] ADVERSE PAPER (record, not corpus — quote it as [doc])\n{adv[1][:ADVERSE_DOC_CAP]}")
    pl = rec.get("pleading")
    if pl:
        sl = slice_pleading(pl[1], f"{claim.get('title', '')}\n{claim.get('text', '')}")
        if sl:
            parts.append(f"[doc: {pl[0]}] PLEADING EXCERPT (record, not corpus — quote it as [doc])\n{sl}")
    return (RECORD_HEAD + "\n" + "\n\n".join(parts)) if parts else ""


def prime_record_docs(units_text: str, case_root: Path | None) -> dict:
    """Once per run: resolve the adverse paper and the pleading for this case tree into _RECORD. M2_RECORD_DOCS=0 opts out."""
    _RECORD["adverse"] = _RECORD["pleading"] = None
    if os.environ.get("M2_RECORD_DOCS", "1") != "1":
        return _RECORD
    _RECORD["adverse"] = resolve_adverse_doc(units_text, case_root)
    _RECORD["pleading"] = resolve_pleading(case_root)
    a, p = _RECORD["adverse"], _RECORD["pleading"]
    print(f"[m2] record docs: adverse={a[0] + f' ({len(a[1])} chars)' if a else 'NONE (no Control No./order match under intake/)'}; "
          f"pleading={p[0] + f' ({len(p[1])} chars)' if p else 'NONE'} — cited [doc], never searched in the corpus", flush=True)
    return _RECORD


def reclassify_record_gap(out: dict, docs_supplied: bool) -> dict:
    """GUARDRAIL (deterministic): when a [doc] block WAS in the prompt and the answer's gap says a RECORD document is 'not in
    corpus' (the sentence carries both 'not in corpus' and one of RECORD_WORDS), status becomes RECORD_SUPPLIED with a note —
    a false GAP about the record never counts as a corpus absence. A LAW gap ('no rule requires stating reasons') is untouched."""
    if not docs_supplied or out.get("status") not in ("GAP", "NOT_IN_CORPUS"):
        return out
    if out.get("bot") == "posture" or "deadline_computed" in out:        # merged3: the posture cell (guard_posture schema — vehicle / deadline_computed, status in POSTURE_STATUSES) is never reclassified
        return out
    gap = str(out.get("gap") or "")
    for sent in re.split(r"(?<=[.;])\s+|\n", gap):
        s = sent.lower()
        if "not in corpus" in s and any(re.search(rf"\b{w}s?\b", s) for w in RECORD_WORDS):
            out["record_reclassified_from"] = out.get("status")
            out["status"] = "RECORD_SUPPLIED"
            out["record_note"] = ("the answer called a RECORD document 'not in corpus' while its [doc] text was in the prompt "
                                  f"(sentence: {sent.strip()[:200]}); record documents are cited [doc], never searched in the corpus")
            break
    return out


def _cn():
    """pipeline.connected_notes — the learning's home (Obsidian _hermes_learnings/notes + learnings.jsonl). Live tree."""
    try:
        import importlib, pipeline as _pkg
        live = str(Path(os.environ.get("M2_ROOT", str(ROOT))) / "pipeline")
        if live not in _pkg.__path__: _pkg.__path__.append(live)          # sandbox package shadows live: extend, don't replace
        return importlib.import_module("pipeline.connected_notes")
    except Exception:
        return None


_GRAPH: dict = {}      # claim id → prior-notes block, filled ONCE per run (24 reads, not 312; the note set is 27k files on /mnt/c)


def graph_notes(query: str, k: int = int(os.environ.get("M2_GRAPH_K", "6")), max_chars: int = int(os.environ.get("M2_GRAPH_CHARS", "4800"))) -> str:
    """READ: the accumulated GROUNDED notes nearest this claim (what m0's organs inject via graph_context). Semantic
    (cached note embeddings); keyword fallback is inside graph_context."""
    cn = _cn()
    if not cn or os.environ.get("M2_GRAPH", "1") != "1": return ""
    try:
        return cn.graph_context(query, k=k, max_chars=max_chars, semantic=True,
                                hops=int(os.environ.get("M2_GRAPH_HOPS", "2"))) or ""   # S-graph-first-hit: seeds + 2-hop [[links]]
    except Exception:
        return ""


def prime_graph(claims: list, bots: list) -> None:
    """Phase 0: one graph read per claim, shared by every bot's cell for that claim."""
    if os.environ.get("M2_GRAPH", "1") != "1": return
    t0 = time.time()
    for c in claims:
        _GRAPH[c["id"]] = graph_notes(f"{c['title']} {c['text'][:300]}")
    print(f"[m2] graph primed: {sum(1 for v in _GRAPH.values() if v)}/{len(claims)} claims have prior notes ({time.time()-t0:.0f}s)", flush=True)


def learn_back(rows: list, slug: str, relink_now: bool | None = None) -> int:
    """WRITE: each GROUNDED/CONFLICT cell with a verified authority becomes ONE atomic learning note
    (record_learning → learnings.jsonl + note; idempotent by content hash), tagged by bot/axis/case.
    S-m2-relink-offrun (2026-10-24) — CLASS RULE: the runner never walks the vault synchronously. DEFECT: end-to-end wall
    was dominated by connected_notes.relink (full walk of ~29k notes on /mnt/c, ~300 s) while retrieval+cells took 14 s.
    relink is left to the connected-notes cron (incremental) unless M2_RELINK=1 / relink_now=True asks for it."""
    cn = _cn()
    if not cn: return 0
    if relink_now is None:
        relink_now = os.environ.get("M2_RELINK", "0") == "1"
    n = 0
    touched = []     # S-relink-incremental: the notes THIS run wrote → relink(touched=...) links them (both directions) without a full walk
    for r in rows:
        if r.get("status") not in ("GROUNDED", "CONFLICT"): continue
        auths = [a for a in (r.get("authorities") or []) if str(a.get("verified")) == "True" and a.get("cite")]
        if not auths or not r.get("finding"): continue
        cites = "; ".join(f"{a['cite']} [corpus:{a.get('source','')}]" for a in auths[:3])
        text = f"{r['finding'].strip()} — authority: {cites}." + (f" CONFLICT: {r.get('gap')}" if r.get("status") == "CONFLICT" and r.get("gap") else "")
        try:
            p = cn.record_learning(text, tags=["m2", r["bot"], r.get("group", ""), slug, f"level{r.get('level', '')}", "verified"],
                                   source=f"m2/{r['bot']}/claim{r['claim']}", cite=auths[0]["cite"], relink_now=False); n += 1
            if p: touched.append(p)
        except Exception:
            continue
    if n and not relink_now:
        print(f"[m2] learn_back: {n} notes written, relink DEFERRED to the connected-notes cron (M2_RELINK=1 to relink now)", flush=True)
    try:
        if n and relink_now:
            print(f"[m2] learn_back: relink now (M2_RELINK=1) over {len(touched)} touched notes", flush=True)
            try:
                cn.relink(touched=touched)
            except TypeError:          # older connected_notes without the incremental signature
                cn.relink()
    except Exception:
        pass
    return n


def _excerpt_lines(ex: list[dict], start: int = 0) -> list[str]:
    """One block per hit, numbered from start+1 — the SAME bytes the live layout prints (header + text)."""
    return [f"[{start+i+1}] source={h.get('source')} chunk={h.get('chunk_id')} part={h.get('part') or '-'}"
            + (" RECITAL=yes (this passage recites a PARTY's argument — not the court's holding unless the sentence itself says 'we hold/we conclude')" if h.get("recital") else "")
            + f"\n{h.get('text')}" for i, h in enumerate(ex) if "text" in h]


GRAPH_HEAD = "=== PRIOR GROUNDED NOTES (Obsidian graph — verified earlier; cite their [[slug]], re-verify any quote against the corpus)"
PINNED_HEAD = "=== PINNED AUTHORITIES (named in the claim; shared by every axis of this unit — numbered with the corpus excerpts below)"


def build_prompt(bot: dict, claim: dict, facts: str, pinned: list[dict], axis: list[dict], gctx: str,
                 prefix_order: bool | None = None, docs: str = "") -> dict:
    """Assemble (system, user) for one cell. prefix_order=False reproduces the live layout byte-for-byte.
    prefix_order=True (M2_PREFIX_ORDER=1) emits the SAME content blocks in shared-head order:
      system = HIERARCHY+schema | CLAIM | PINNED authorities | PRIOR GROUNDED NOTES | SOUL      (unit-shared first, bot last)
      user   = CASE FACTS slice | the bot's CORPUS EXCERPTS (numbering continues after the pinned ones)
    Returns {"system", "user", "n_ex", "shared_head"} — shared_head is the system prefix before the SOUL (what the
    6 bots of one unit have in common; the prefix cache can only hit that).
    The families label is the forum-filtered set (S-m2-forum-families) in both layouts.
    docs (S-m2-record-docs-to-cells): the RECORD DOCUMENTS block (record_docs_block) — appended to the CASE FACTS block in the
    user message in both layouts; '' (no record documents) leaves every byte as it was."""
    if prefix_order is None: prefix_order = PREFIX_ORDER
    fams = forum_families(bot.get("families") or [])                 # S-m2-forum-families: the prompt label names the filtered set
    ex = pinned + axis
    seen = set(); ex = [h for h in ex if not (h.get("chunk_id") and (h["chunk_id"] in seen or seen.add(h["chunk_id"])))]
    n_pin = sum(1 for h in ex if any(h is p for p in pinned))          # pinned survivors of the dedupe (identity, same objects)
    uni = f"\n\n## HIERARCHY (grade every support by this ladder)\n{HIERARCHY.read_text(encoding='utf-8')}\n\n{OUTPUT_SCHEMA}"
    claim_blk = f"=== CLAIM {claim['id']}: {claim['title']}\n{claim['text'][:CLAIM_BUDGET]}"
    facts_blk = f"=== CASE FACTS (excerpt)\n{facts[:3000]}" + (f"\n\n{docs}" if docs else "")
    if not prefix_order:
        lines = _excerpt_lines(ex)
        excerpts = "\n\n".join(lines)
        if gctx: excerpts = (excerpts + "\n\n" if excerpts else "") + f"{GRAPH_HEAD}\n{gctx}"
        system = f"{bot['soul']}{uni}"
        if os.environ.get("M2_FACTS_FIRST") == "1":
            # opt-in (bench A/B 2026-10-10): the facts block is identical in every cell, so placing it in the per-bot
            # system prompt makes it part of the shared prefix the server caches; the claim + excerpts stay variable.
            system += f"\n\n{facts_blk}"
            user = f"{claim_blk}\n\n=== CORPUS EXCERPTS (families {fams or 'all'})\n{excerpts or '(no excerpts retrieved)'}"
        else:
            user = f"{claim_blk}\n\n{facts_blk}\n\n=== CORPUS EXCERPTS (families {fams or 'all'})\n{excerpts or '(no excerpts retrieved)'}"
        return {"system": system, "user": user, "n_ex": len(ex), "shared_head": ""}
    pin_lines = _excerpt_lines(ex[:n_pin]); axis_lines = _excerpt_lines(ex[n_pin:], start=n_pin)
    head = uni.lstrip("\n") + f"\n\n{claim_blk}"
    if pin_lines: head += f"\n\n{PINNED_HEAD}\n" + "\n\n".join(pin_lines)
    if gctx: head += f"\n\n{GRAPH_HEAD}\n{gctx}"
    system = f"{head}\n\n{bot['soul']}"
    user = f"{facts_blk}\n\n=== CORPUS EXCERPTS (families {fams or 'all'})\n" + ("\n\n".join(axis_lines) or "(no excerpts retrieved)")
    return {"system": system, "user": user, "n_ex": len(ex), "shared_head": head}


def run_cell(bot: dict, claim: dict, facts: str, dry: bool = False, ex: list | tuple | None = None, caps: dict | None = None) -> dict:
    """ex   (S-m2-retrieval-pipelined): the excerpts main()'s retrieval pool already fetched for THIS cell — either the fetch_cell()
            list (pinned + axis concatenated, the live order) or the fetch_cell_parts() tuple (pinned, axis) when M2_PREFIX_ORDER=1
            needs the split for the shared head. None = library caller: fetched here (same cached calls).
       caps (S-m2-speed): per-bot max_tokens; None = _BOT_CAPS (M2_BOT_CAPS), {} = global MAX_TOKENS (live).
    S-m2-merged2 (2026-10-24): a prefetched LIST carries no pinned/axis boundary, so it is treated as all-axis — correct for the default
    layout (build_prompt ignores the split when prefix_order is off) and never used with M2_PREFIX_ORDER=1 (main() prefetches parts)."""
    if os.environ.get("M2_FACTS_SLICE", "1") == "1":
        facts = slice_facts(facts, claim, bot)
    # S-m2-retrieval-pipelined (2026-10-24): main() hands in the excerpts its retrieval pool already fetched for THIS cell
    # (same fetch_cell output, same order) so the lane call starts the moment they arrive; library callers still get the fetch here.
    if ex is None:
        pinned, axis = fetch_cell_parts(bot, claim)
    elif isinstance(ex, tuple):
        pinned, axis = ex
    else:
        pinned, axis = [], list(ex)
    gctx = _GRAPH.get(claim["id"], "")                                   # the vault: what we already know, linked (primed per claim)
    docs = record_docs_block(claim)                                      # S-m2-record-docs-to-cells: adverse paper + pleading excerpt as [doc]; '' = live bytes
    pr = build_prompt(bot, claim, facts, pinned, axis, gctx, docs=docs)
    system, user = pr["system"], pr["user"]
    row = {"bot": bot["name"], "group": bot["group"], "claim": claim["id"], "title": claim["title"], "excerpts": pr["n_ex"],
           "pin_version": _pin_version()}       # S-m2-resume-stale (2026-10-24): every cell carries the pin/family code version it was produced under
    if docs:
        row["record_docs"] = [p[0] for p in (_RECORD.get("adverse"), _RECORD.get("pleading")) if p and f"[doc: {p[0]}]" in docs]
    if dry:
        row.update({"status": "DRY", "prompt_chars": len(system) + len(user), "shared_head_chars": len(pr["shared_head"])}); return row
    caps = _BOT_CAPS if caps is None else caps
    cap = int(caps.get(bot["name"], MAX_TOKENS)) if caps else MAX_TOKENS

    def ask(system: str, user: str) -> dict:
        """One lane ask (S-m2-gap-cite-second-pass factored the live body out so the retry runs the SAME path: NaN guard, cap retry, parse)."""
        try:
            raw = chat(system, user, max_tokens=cap)
            # NaN GUARD (lane correctness bug, prefix-cache + MTP on hybrid GDN: token 0 '!' = all-NaN logit row; fires on a
            # WARM cache after many cells share a bot prefix — the same prompt is clean at C1 on a fresh engine). A nonce
            # ahead of the system prompt defeats the poisoned cached block (fresh prefill). Recorded so the engine's rate is tracked.
            if raw.count("!") > 50 and raw.count("!") > len(raw) // 4:
                row["nan_first_attempt"] = True; row["usage_first_attempt"] = _LAST_USAGE.pop(threading.get_ident(), {})
                raw = chat(f"[cell {bot['name']}/{claim['id']} {time.time():.0f}]\n{system}", user, max_tokens=cap)
                if raw.count("!") > 50: row["status_note"] = "LANE_NAN_TWICE"
            # CAP RETRY (quality floor of the per-bot cap): a cell cut by a per-bot cap below the global one is re-asked once at
            # MAX_TOKENS, so its output is never shorter than the live runner's. Only a runaway loop pays twice (and it was
            # UNPARSED either way); every honest long cell lands exactly where it would have.
            if cap < MAX_TOKENS and (_LAST_USAGE.get(threading.get_ident()) or {}).get("finish_reason") == "length":
                row["cap_retry"] = True; row["usage_capped"] = _LAST_USAGE.pop(threading.get_ident(), {})
                raw = chat(system, user, max_tokens=MAX_TOKENS)
            return parse_json(raw)
        except Exception as e:
            return {"status": "LANE_ERROR", "raw": str(e)[:300]}

    def guard(out: dict) -> dict:
        # GUARDRAIL: verify every authority's quote; unverified → NOT_IN_CORPUS and it cannot count toward GROUNDED
        auths = out.get("authorities") or []
        verified = []
        for a in auths if isinstance(auths, list) else []:
            v = verify_quote(str(a.get("quote", "")))
            a["verified"] = v["ok"]; a["verify"] = v.get("source") or v.get("why")
            verified.append(a)
        out["authorities"] = verified
        if out.get("status") == "GROUNDED" and not any(a.get("verified") for a in verified):
            out["status"] = "NOT_IN_CORPUS"; out["guardrail"] = "GROUNDED claimed but no authority quote resolved in the corpus"
        return out

    t0 = time.time()
    out = ask(system, user)
    row["seconds"] = round(time.time() - t0, 1)
    row.update(_LAST_USAGE.pop(threading.get_ident(), {}))          # prompt/completion tokens + finish_reason per cell (diagnosable)
    row["max_tokens"] = cap
    out = guard(out)
    # S-m2-gap-cite-second-pass (2026-10-24) — CLASS RULE (user-locked): NOT-IN-CORPUS only after ALL families are actually
    # searched. DEFECT: pins are pulled from the CLAIM text only; when the bot's own GAP text NAMES an authority that was never
    # pinned (order17 run9 remedy u1: "Pa.R.C.P. 1028(e) ... NOT IN CORPUS" while pull_cites("Pa.R.C.P. 1028(e)") returns the rule
    # — 'right to plead over within twenty days'), the bot had no way to see it and the status was false (64/102 GAP). Fix: the
    # cites the ANSWER names minus the cell's pins go through the SAME pin path (deterministic, cached, forum-unfiltered, same
    # 1400-char window); if ANY resolves the cell is re-asked ONCE with them appended to the pinned block (same builder, same
    # dedupe, same guardrail). Never more than one retry per cell. Opt-out M2_SECOND_PASS=0.
    if second_pass_enabled() and out.get("status") in SECOND_PASS_STATUSES:
        want = second_pass_cites(out, claim["text"])
        sp: dict = {"cites": want, "resolved": [], "unresolved": list(want)}
        if want:
            extra, sp["resolved"], sp["unresolved"] = second_pass_pull(want)
            if sp["resolved"]:
                pr2 = build_prompt(bot, claim, facts, list(pinned) + extra, axis, gctx, docs=docs)
                sp["first_status"] = out.get("status"); sp["first_gap"] = str(out.get("gap") or "")[:400]
                t1 = time.time(); out = guard(ask(pr2["system"], pr2["user"]))
                sp["seconds"] = round(time.time() - t1, 1); sp["excerpts_added"] = pr2["n_ex"] - pr["n_ex"]
                row["seconds"] = round(time.time() - t0, 1); row["excerpts"] = pr2["n_ex"]
                row["usage_first_pass"] = {k: row.pop(k, None) for k in ("prompt_tokens", "completion_tokens", "finish_reason")}
                row.update(_LAST_USAGE.pop(threading.get_ident(), {}))
        row["second_pass"] = sp
    out = reclassify_record_gap(out, bool(docs))                         # S-m2-record-docs-to-cells: a record doc 'not in corpus' with its [doc] in the prompt is RECORD_SUPPLIED, never a GAP
    try:
        out["level"] = int(out.get("level") or 0) or None
    except Exception:
        out["level"] = None
    row.update(out); return row


# ------------------------------------------------------------------ S-m2-gap-cite-second-pass (2026-10-24)
SECOND_PASS_STATUSES = ("GAP", "NOT_IN_CORPUS", "CONFLICT")


def second_pass_enabled() -> bool:
    """M2_SECOND_PASS=0 opts out; default ON (read per call so a run can be toggled without re-import)."""
    return os.environ.get("M2_SECOND_PASS", "1") == "1"


def second_pass_cites(out: dict, claim_text: str, keys: tuple = ("gap", "finding", "proposition", "fix")) -> list[str]:
    """The authorities the ANSWER names (gap / finding / proposition / fix + each authority's cite) that were NOT already pinned
    for the cell — cites_in(claim) is exactly what pull_cites pinned, so those are never re-pulled. Same recogniser as the pins.
    `keys` (S-m2-posture-axis): the answer fields to read; default = the finding schema (live behaviour)."""
    parts = [str(out.get(k) or "") for k in keys]
    parts += [str(a.get("cite") or "") for a in (out.get("authorities") or []) if isinstance(a, dict)]
    have = {c.lower() for c in cites_in(claim_text)}
    return [c for c in cites_in("\n".join(parts)) if c.lower() not in have]


def second_pass_pull(cites: list[str]) -> tuple[list[dict], list[str], list[str]]:
    """(excerpts, resolved, unresolved) through pull_cites — the pin path itself (cached under _pin_version, forum-unfiltered,
    `per` hits per cite, 1400-char window). A cite is resolved when a hit for it carries a chunk_id; the unresolved ones keep
    their '(NOT IN CORPUS by semantic+pin ...)' marker so the retry sees them exactly as the claim's own unresolved pins."""
    ex = pull_cites("\n".join(cites))
    ok = {c.lower(): False for c in cites}
    for h in ex:
        src = str(h.get("source") or "")
        if src.startswith("cite:") and h.get("chunk_id"):
            k = src[5:].split(" chunk=")[0].strip().lower()
            if k in ok: ok[k] = True
    return ex, [c for c in cites if ok[c.lower()]], [c for c in cites if not ok[c.lower()]]


# ------------------------------------------------------------------ S-m2-posture-axis (2026-10-24): the movant's question
# DEFECT (order17 _bundle13, 17 units × 6 bots): every bot is violation-shaped ('what rule did the order break'), so WHAT VEHICLE /
# BY WHEN / UNDER WHAT STANDARD / WHAT MUST IT CONTAIN had no slot — unit 9 got 11 cells on 1925(a)/341(c) and 42 Pa.C.S. § 5505
# (in corpus, chunk 9d717bcf...) was a passing mention in one GAP cell. The old drafter decided the vehicle with a keyword table
# (chain_runner._infer_doc_type: 'DISMISS' in v -> motion_opposing_dismissal) — rejected on sight.
# CLASS RULE: deterministic = guardrail, LLM = comprehension. The model READS the forum's verbatim rule text (pinned through the
# same pin path every bot uses) and reasons the vehicle; the runner (a) verifies every authority quote, (b) NEVER takes the
# deadline date from the model — it is computed from the record's trigger date + the model's deadline_rule words by the repo's
# deadline computer (pipeline.temporal_schedule.add_days, PA Rule 106 / FRCP 6(a) counting), a mismatch is logged and the
# computed date wins, (c) demotes the cell to GAP when the quoted rule_verbatim is not in any pinned rule chunk shown.
# ONE cell per run (bot 'posture', claim 0), written to cells.jsonl and MATRIX.json['posture']; ON by default, M2_POSTURE=0 off;
# `--bots <list>` without 'posture' leaves it out (so every existing --bots run is byte-identical); M2_BOTS seeds --bots.
POSTURE_STATUSES = ("GROUNDED", "GAP", "CONFLICT")
POSTURE_SCHEMA = '''Reply with ONE JSON object and nothing else:
{"vehicle": "the paper the movant files next (e.g. 'motion for reconsideration under 42 Pa.C.S. § 5505', 'amended complaint as of right, Pa.R.C.P. 1028(e)')",
 "rule_verbatim": "the operative words of the rule that authorises that vehicle, copied EXACTLY from a corpus excerpt (12+ words)",
 "authorities": [{"cite": "...", "chunk_id": "the excerpt's chunk= id", "quote": "exact words from that excerpt (12+ words)"}],
 "trigger_event": "the event the rule counts from, in the rule's words (entry of the order / docketing / notice under Rule 236 / service)",
 "trigger_date": "YYYY-MM-DD" | null (the record date of that event, from the ADVERSE PAPER / POSTURE UPDATE shown; null if not fixed there),
 "deadline_rule": "the period in the rule's exact words (e.g. 'within 30 days after its entry')",
 "deadline_date": "YYYY-MM-DD" | null (your reading; the runner recomputes it deterministically and overrides),
 "standard": "the standard the forum applies to that vehicle, as stated in the excerpts (or 'NOT IN EXCERPTS')",
 "must_contain": ["each thing the rule / local rule text requires the paper to contain"],
 "alternatives_rejected": [{"vehicle": "...", "why": "the rule words that exclude or disfavour it at this posture", "cite": "..."}],
 "status": "GROUNDED" | "GAP" | "CONFLICT",
 "gap": "what is NOT IN CORPUS / not in the excerpts, or empty"}
Rules: cite ONLY from the CORPUS EXCERPTS below. Never invent a cite, a quote, a docket number, a date or an amount.'''
_ADV_DATE = re.compile(r"\b(entered|docketed|signed|filed|dated|served)\b(?:\s+on)?\s*:?\s*(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})", re.I)
_ADV_NO = re.compile(r"((?:Control|Civil Action|Case|Docket|CCP)\s*No\.?\s*[:#]?\s*[\w:\-]+|No\.\s*\d[\w:\-]*|ECF\s*(?:No\.?\s*)?\d+)", re.I)
_ADV_VERBATIM = re.compile(r"THE (?:ORDER|PAPER|MOTION|BRIEF) UNDER REVIEW[^\n]*?:\s*([^\n]+)", re.I)
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NUMWORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "fourteen": 14,
             "fifteen": 15, "twenty": 20, "twenty-one": 21, "thirty": 30, "forty-five": 45, "sixty": 60, "ninety": 90}


def _iso(d: str) -> str:
    """'10/22/2026' -> '2026-10-22'; ISO passes through. Pure."""
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", d.strip())
    return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}" if m else d.strip()


def adverse_identity(text: str) -> dict:
    """The adverse paper's identity from the draft's HEADER line (first non-empty line) — deterministic, record-only:
    {header, numbers:[...], dates:{entered|docketed|signed|filed|...: YYYY-MM-DD}, verbatim: the 'THE ORDER UNDER REVIEW ...:' sentence,
    trigger_date, trigger_event}. trigger = the ENTERED date when the header gives one, else DOCKETED, else FILED (never 'signed':
    signing is not entry). Nothing is inferred: a header without a date leaves trigger_date None (the cell says so)."""
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    header = lines[0] if lines else ""
    dates = {}
    for m in _ADV_DATE.finditer(header):
        dates.setdefault(m.group(1).lower(), _iso(m.group(2)))
    vm = _ADV_VERBATIM.search(text or "")
    verbatim = vm.group(1).strip() if vm else ""
    trig_ev, trig = None, None
    for k in ("entered", "docketed", "filed"):
        if dates.get(k):
            trig_ev, trig = k, dates[k]; break
    return {"header": header[:600], "numbers": [m.group(1).strip() for m in _ADV_NO.finditer(header)][:4], "dates": dates,
            "verbatim": verbatim[:800], "trigger_date": trig,
            "trigger_event": (f"{trig_ev} {trig} (ADVERSE_POINTS header)" if trig else "NOT IN HEADER (no entered/docketed/filed date)")}


def posture_facts(facts: str, max_chars: int = 2500) -> str:
    """The case's '## POSTURE UPDATE ...' section(s) from CASE_FACTS (latest first), capped. '' when none."""
    blocks, cur, head = [], [], None
    for line in (facts or "").splitlines():
        if line.startswith("#"):
            if head is not None and cur: blocks.append((head, "\n".join(cur).strip()))
            head = line.strip("# ").strip(); cur = []
        elif head is not None:
            cur.append(line)
    if head is not None and cur: blocks.append((head, "\n".join(cur).strip()))
    sel = [f"## {h}\n{b}" for h, b in blocks if "POSTURE UPDATE" in h.upper()]
    return "\n\n".join(reversed(sel))[:max_chars]


def deadline_days(rule_text: str) -> int | None:
    """'within 30 days after its entry' / 'twenty (20) days' / 'thirty days' -> 30 / 20 / 30; no day count -> None. Pure.
    Reads the FIRST period in the text (the rule's own period words the model was told to copy)."""
    t = (rule_text or "").lower()
    m = re.search(r"\b(\d{1,3})\b\s*(?:\)\s*)?(?:calendar\s+|court\s+|business\s+)?days?\b", t)
    if m:
        return int(m.group(1))
    m = re.search(r"\b(" + "|".join(sorted(_NUMWORDS, key=len, reverse=True)) + r")\b(?:\s*\(\d+\))?\s*(?:calendar\s+|court\s+|business\s+)?days?\b", t)
    return _NUMWORDS[m.group(1)] if m else None


def _counting_for(forum) -> str:
    f = {x.strip().lower() for x in (forum.split(",") if isinstance(forum, str) else (forum or []))}
    return "PA_106" if "pa" in f else "FRCP_6a"


def compute_deadline(trigger_date: str | None, deadline_rule: str, forum="pa") -> dict:
    """GUARDRAIL (never the model's date): trigger_date (YYYY-MM-DD, from the record) + the day count read from deadline_rule
    -> the repo's deadline computer, pipeline.temporal_schedule.add_days (PA: Rule 106 / 1 Pa.C.S. § 1908 counting — a last
    day on a weekend or legal holiday is omitted; fed: FRCP 6(a)). Returns {date|None, days, counting, why}."""
    days = deadline_days(deadline_rule)
    cnt = _counting_for(forum)
    if not trigger_date or not _ISO.match(str(trigger_date)):
        return {"date": None, "days": days, "counting": cnt, "why": "no record trigger date (header has no entered/docketed date)"}
    if days is None:
        return {"date": None, "days": None, "counting": cnt, "why": f"no day count in deadline_rule {str(deadline_rule)[:80]!r}"}
    try:
        from datetime import date as _date
        from pipeline import temporal_schedule as _ts
        y, mo, d = (int(x) for x in str(trigger_date).split("-"))
        return {"date": _ts.add_days(_date(y, mo, d), days, cnt).isoformat(), "days": days, "counting": cnt, "why": "computed"}
    except Exception as e:
        return {"date": None, "days": days, "counting": cnt, "why": f"deadline computer unavailable: {e!r}"[:200]}


def coerce_posture(out: dict) -> tuple[dict, list[str]]:
    """SCHEMA GUARD: every key of POSTURE_SCHEMA present with its type (strings, ISO-or-None dates, lists of the right shape),
    unknown keys dropped (except raw/guardrail notes), status in POSTURE_STATUSES else GAP. Returns (cell, violations)."""
    v: list[str] = []
    o = out if isinstance(out, dict) else {}
    cell: dict = {}
    for k in ("vehicle", "rule_verbatim", "trigger_event", "deadline_rule", "standard", "gap"):
        x = o.get(k)
        if x is None: x = ""
        if not isinstance(x, str): v.append(f"{k}: not a string"); x = json.dumps(x, ensure_ascii=False) if not isinstance(x, str) else x
        cell[k] = x.strip()
    for k in ("trigger_date", "deadline_date"):
        x = o.get(k)
        if x in (None, "", "null"): cell[k] = None
        elif isinstance(x, str) and _ISO.match(x.strip()): cell[k] = x.strip()
        else: v.append(f"{k}: not YYYY-MM-DD ({str(x)[:30]!r})"); cell[k] = None
    auths = []
    for a in (o.get("authorities") or []) if isinstance(o.get("authorities"), list) else []:
        if not isinstance(a, dict): v.append("authorities: item not an object"); continue
        auths.append({"cite": str(a.get("cite") or ""), "chunk_id": (str(a.get("chunk_id")) if a.get("chunk_id") not in (None, "") else None),
                      "quote": str(a.get("quote") or "")})
    if not isinstance(o.get("authorities"), list): v.append("authorities: not a list")
    cell["authorities"] = auths
    mc = o.get("must_contain")
    if not isinstance(mc, list): v.append("must_contain: not a list"); mc = [mc] if isinstance(mc, str) and mc else []
    cell["must_contain"] = [str(x) for x in mc if str(x).strip()]
    alts = []
    ar = o.get("alternatives_rejected")
    if not isinstance(ar, list): v.append("alternatives_rejected: not a list"); ar = []
    for a in ar:
        if isinstance(a, dict): alts.append({"vehicle": str(a.get("vehicle") or ""), "why": str(a.get("why") or ""), "cite": str(a.get("cite") or "")})
        else: v.append("alternatives_rejected: item not an object")
    cell["alternatives_rejected"] = alts
    st = str(o.get("status") or "").strip().upper()
    if st not in POSTURE_STATUSES:
        v.append(f"status: {st or 'missing'!r} not in {POSTURE_STATUSES}"); st = "GAP"
        cell["gap"] = (cell["gap"] + " " if cell["gap"] else "") + "schema: status missing/invalid"
    cell["status"] = st
    if o.get("status") == "UNPARSED" or "raw" in o: cell["raw"] = str(o.get("raw") or "")[:1500]
    return cell, v


def _norm_words(s: str) -> str:
    return re.sub(r"[^a-z0-9§ ]+", " ", re.sub(r"\s+", " ", (s or "").lower())).strip()


def rule_verbatim_in_chunks(rule_verbatim: str, excerpts: list[dict]) -> dict:
    """GUARDRAIL (c): the quoted rule words must sit in a pinned/shown chunk (whitespace/punctuation-normalised, first 10 words
    then the whole), else fall back to the corpus-wide phrase check. {ok, chunk_id|None, where}."""
    q = _norm_words(rule_verbatim)
    if len(q.split()) < 5:
        return {"ok": False, "chunk_id": None, "where": "rule_verbatim too short (< 5 words)"}
    heads = [" ".join(q.split()[:10]), q]
    for h in excerpts or []:
        t = _norm_words(h.get("text") or "")
        if t and any(x in t for x in heads):
            return {"ok": True, "chunk_id": h.get("chunk_id"), "where": "shown excerpt"}
    v = verify_quote(rule_verbatim)
    if v.get("ok"):
        return {"ok": True, "chunk_id": v.get("chunk_id"), "where": f"corpus phrase ({v.get('source')})"}
    return {"ok": False, "chunk_id": None, "where": "not in any pinned/shown chunk; corpus phrase check: " + str(v.get("why") or "no")}


def guard_posture(out: dict, excerpts: list[dict], identity: dict, forum="pa") -> dict:
    """The three deterministic guardrails over a parsed posture answer: schema (coerce_posture); (a) verify_quote on every authority
    — GROUNDED with none verified -> GAP; (c) rule_verbatim in a shown chunk or -> GAP naming what was missing; (b) trigger/deadline:
    the RECORD's trigger date (identity) overrides the model's, deadline_date = compute_deadline(...) always — the model's date is kept
    under `deadline_model` and any mismatch is logged. Pure over its inputs except verify_quote."""
    cell, viol = coerce_posture(out)
    if viol: cell["schema_violations"] = viol
    verified = []
    for a in cell["authorities"]:
        v = verify_quote(a.get("quote", ""))
        a["verified"] = bool(v.get("ok")); a["verify"] = v.get("source") or v.get("why")
        if v.get("ok") and not a.get("chunk_id"): a["chunk_id"] = v.get("chunk_id")
        verified.append(a)
    cell["authorities"] = verified
    notes = []
    if cell["status"] == "GROUNDED" and not any(a.get("verified") for a in verified):
        cell["status"] = "GAP"; notes.append("GROUNDED claimed but no authority quote resolved in the corpus")
    rv = rule_verbatim_in_chunks(cell["rule_verbatim"], excerpts)
    cell["rule_verbatim_chunk"] = rv.get("chunk_id")
    if not rv["ok"]:
        if cell["status"] != "CONFLICT": cell["status"] = "GAP"
        notes.append(f"rule_verbatim not found in any pinned rule chunk ({rv['where']}): {cell['rule_verbatim'][:120]!r}")
    # (b) deadline — the record's trigger wins; the date is computed, never read from the model
    rec_trig = identity.get("trigger_date")
    cell["trigger_model"] = cell.get("trigger_date")
    if rec_trig:
        if cell.get("trigger_date") and cell["trigger_date"] != rec_trig:
            notes.append(f"trigger_date: model {cell['trigger_date']} != record {rec_trig} ({identity.get('trigger_event')}); record wins")
        cell["trigger_date"] = rec_trig
        cell["trigger_event"] = (cell.get("trigger_event") or "").strip() or identity.get("trigger_event") or ""
        cell["trigger_record"] = identity.get("trigger_event")
    comp = compute_deadline(cell["trigger_date"], cell["deadline_rule"], forum)
    cell["deadline_model"] = cell.get("deadline_date")
    cell["deadline_computed"] = comp
    if cell["deadline_model"] and comp["date"] and cell["deadline_model"] != comp["date"]:
        notes.append(f"deadline_date: model {cell['deadline_model']} != computed {comp['date']} ({comp['days']} days from {cell['trigger_date']}, {comp['counting']}); computed wins")
    elif cell["deadline_model"] and not comp["date"]:
        notes.append(f"deadline_date: model gave {cell['deadline_model']} but it cannot be computed from the record ({comp['why']}); set to null")
    cell["deadline_date"] = comp["date"]
    if notes:
        cell["guardrail"] = "; ".join(notes)
        for n in notes: print(f"[m2] posture guardrail: {n}", file=sys.stderr, flush=True)
    if cell["status"] == "GAP" and notes and not cell.get("gap"):
        cell["gap"] = notes[0]
    return cell


def posture_pin_text(bot: dict, identity: dict, pfacts: str, forum="pa") -> str:
    """The text pull_cites pins for the posture cell: the adverse header + its verbatim disposition + the POSTURE UPDATE section +
    the forum's rule set from JOB.yaml forum_pins (data). One string, so cites_in dedupes and the cite cache keys it."""
    f = {x.strip().lower() for x in (forum.split(",") if isinstance(forum, str) else (forum or []))}
    pins = bot.get("forum_pins") or {}
    extra = [c for k in sorted(f) for c in (pins.get(k) or []) if isinstance(pins, dict)]
    return "\n".join([identity.get("header", ""), identity.get("verbatim", ""), pfacts, "FORUM RULE SET: " + "; ".join(extra)])


def fetch_posture(bot: dict, identity: dict, pfacts: str, forum="pa") -> tuple[list[dict], list[dict]]:
    """(pinned, axis) for the posture cell through the SAME cached calls every cell uses: pull_cites over posture_pin_text, retrieve
    over the adverse paper's own disposition words + the bot's JOB queries within its forum-filtered families."""
    fams = forum_families(bot.get("families") or [], forum)
    qs = [q for q in (identity.get("verbatim", ""), identity.get("header", "")[:300]) if q] + [q for q in (bot.get("queries") or [])]
    return pull_cites(posture_pin_text(bot, identity, pfacts, forum)), retrieve(qs, fams)


def build_posture_prompt(bot: dict, identity: dict, forum: str, pfacts: str, pinned: list[dict], axis: list[dict], docs: str = "") -> dict:
    """(system, user) for the posture cell: SOUL + HIERARCHY + POSTURE_SCHEMA | ADVERSE PAPER identity | FORUM | POSTURE UPDATE |
    [RECORD DOCUMENTS] | CORPUS EXCERPTS (pinned first, same _excerpt_lines bytes as every cell).
    docs (S-m2-record-docs-to-cells, merged3): the record_docs_block — the adverse paper's OCR text IS what the posture bot reasons
    about (vehicle / trigger / deadline follow the paper's own words), so it is appended after the POSTURE UPDATE block exactly as
    run_cell appends it to CASE FACTS; '' (no record documents) leaves every byte as the posture sandbox produced it."""
    ex = list(pinned) + list(axis)
    seen = set(); ex = [h for h in ex if not (h.get("chunk_id") and (h["chunk_id"] in seen or seen.add(h["chunk_id"])))]
    system = f"{bot['soul']}\n\n## HIERARCHY (grade every support by this ladder)\n{HIERARCHY.read_text(encoding='utf-8')}\n\n{POSTURE_SCHEMA}"
    adv = (f"=== ADVERSE PAPER (identity from the record header; dates are the record's, not yours)\n{identity.get('header','')}\n"
           f"numbers: {', '.join(identity.get('numbers') or []) or '-'}\ndates: {json.dumps(identity.get('dates') or {})}\n"
           f"trigger of record: {identity.get('trigger_event')}\n"
           + (f"disposition verbatim: {identity['verbatim']}" if identity.get("verbatim") else "disposition verbatim: (none quoted in the draft)"))
    user = f"{adv}\n\n=== FORUM: {forum}\n\n=== CASE FACTS — POSTURE UPDATE\n{pfacts or '(no POSTURE UPDATE section in CASE_FACTS)'}" \
           + (f"\n\n{docs}" if docs else "") + "\n\n" \
           f"=== CORPUS EXCERPTS (forum procedure families; pinned rule text first)\n" + ("\n\n".join(_excerpt_lines(ex)) or "(no excerpts retrieved)")
    return {"system": system, "user": user, "n_ex": len(ex), "excerpts": ex}


def posture_enabled(bots_arg: str = "") -> bool:
    """ON by default; M2_POSTURE=0 disables; an explicit --bots list without 'posture' leaves it out."""
    if os.environ.get("M2_POSTURE", "1") != "1":
        return False
    names = [x.strip() for x in (bots_arg or "").split(",") if x.strip()]
    return (not names) or ("posture" in names)


def run_posture(bot: dict, text: str, facts: str, forum: str = "pa", dry: bool = False) -> dict:
    """The ONE posture cell of a run (bot 'posture', claim 0): fetch (cached) -> lane -> guard_posture -> second pass like any cell
    (a GAP/CONFLICT naming an unpinned cite is re-asked ONCE with it pinned) -> row for cells.jsonl / MATRIX['posture']."""
    identity = adverse_identity(text); pfacts = posture_facts(facts)
    pinned, axis = fetch_posture(bot, identity, pfacts, forum)
    # S-m2-record-docs-to-cells (merged3): the adverse paper + any pleading the header references reach the posture cell as [doc]
    # blocks, same block run_cell builds; '' when the run has no record documents (posture sandbox bytes).
    docs = record_docs_block({"id": 0, "title": "posture", "text": f"{identity.get('header', '')}\n{identity.get('verbatim', '')}"})
    pr = build_posture_prompt(bot, identity, forum, pfacts, pinned, axis, docs=docs)
    row = {"bot": bot["name"], "group": bot.get("group", "posture"), "claim": 0, "title": "posture", "excerpts": pr["n_ex"],
           "pin_version": _pin_version(), "identity": identity, "forum": forum}
    if docs:
        row["record_docs"] = [p[0] for p in (_RECORD.get("adverse"), _RECORD.get("pleading")) if p and f"[doc: {p[0]}]" in docs]
    if dry:
        row.update({"status": "DRY", "prompt_chars": len(pr["system"]) + len(pr["user"])}); return row

    def ask(system: str, user: str) -> dict:
        try:
            raw = chat(system, user, max_tokens=max(MAX_TOKENS, 1400))
            if raw.count("!") > 50 and raw.count("!") > len(raw) // 4:          # NaN guard, as run_cell
                row["nan_first_attempt"] = True; raw = chat(f"[cell posture/0 {time.time():.0f}]\n{system}", user, max_tokens=max(MAX_TOKENS, 1400))
            return parse_json(raw)
        except Exception as e:
            return {"status": "LANE_ERROR", "raw": str(e)[:300]}

    t0 = time.time()
    out = ask(pr["system"], pr["user"])
    row.update(_LAST_USAGE.pop(threading.get_ident(), {}))
    if out.get("status") == "LANE_ERROR":
        row.update({"status": "LANE_ERROR", "raw": out.get("raw"), "seconds": round(time.time() - t0, 1)}); return row
    cell = guard_posture(out, pr["excerpts"], identity, forum)
    if second_pass_enabled() and cell.get("status") in SECOND_PASS_STATUSES:
        pin_text = posture_pin_text(bot, identity, pfacts, forum)
        want = second_pass_cites(cell, pin_text, keys=("gap", "standard", "vehicle", "deadline_rule"))
        want += [c for a in cell.get("alternatives_rejected") or [] for c in cites_in(str(a.get("cite") or "")) if c.lower() not in {w.lower() for w in want}
                 and c.lower() not in {x.lower() for x in cites_in(pin_text)}]
        sp: dict = {"cites": want, "resolved": [], "unresolved": list(want)}
        if want:
            extra, sp["resolved"], sp["unresolved"] = second_pass_pull(want)
            if sp["resolved"]:
                pr2 = build_posture_prompt(bot, identity, forum, pfacts, list(pinned) + extra, axis, docs=docs)
                sp["first_status"] = cell.get("status"); sp["first_gap"] = str(cell.get("gap") or "")[:400]
                t1 = time.time(); out2 = ask(pr2["system"], pr2["user"])
                if out2.get("status") != "LANE_ERROR":
                    cell = guard_posture(out2, pr2["excerpts"], identity, forum); row["excerpts"] = pr2["n_ex"]
                sp["seconds"] = round(time.time() - t1, 1); row.update(_LAST_USAGE.pop(threading.get_ident(), {}))
        row["second_pass"] = sp
    row["seconds"] = round(time.time() - t0, 1)
    row.update(cell); return row


def posture_line(row: dict) -> str:
    """'[m2] posture: vehicle=<v> | deadline <date> (<rule>) | standard: <...>' — the one line a reader scans for."""
    if row.get("status") in ("DRY", "LANE_ERROR"):
        return f"[m2] posture: {row.get('status')} {str(row.get('raw') or '')[:120]}"
    dl = row.get("deadline_date") or "null"
    cmp_ = row.get("deadline_computed") or {}
    tail = f" [{cmp_.get('days')}d from {row.get('trigger_date')} {cmp_.get('counting')}]" if cmp_.get("date") else f" [{cmp_.get('why', '')}]"
    return (f"[m2] posture: vehicle={row.get('vehicle') or '?'} | deadline {dl} ({row.get('deadline_rule') or '?'}){tail} | "
            f"standard: {str(row.get('standard') or '?')[:160]} | status {row.get('status')}"
            + (f" | guardrail: {row['guardrail'][:200]}" if row.get("guardrail") else ""))


# ------------------------------------------------------------------ matrix
def write_matrix(outdir: Path, rows: list[dict], bots: list[dict], claims: list[dict], posture: dict | None = None) -> Path:
    """posture (S-m2-posture-axis): the run's posture cell -> MATRIX.json['posture'] + a POSTURE block in MATRIX.md. None (live
    callers, M2_POSTURE=0, --bots without 'posture') = byte-identical output."""
    by = {(r["bot"], r["claim"]): r for r in rows if r.get("claim") != 0 or r.get("bot") != "posture"}
    rows = [r for r in rows if not (r.get("bot") == "posture" and r.get("claim") == 0)]
    names = [b["name"] for b in bots]
    m = {"claims": [c["id"] for c in claims], "bots": names, "cells": {}}
    lines = ["# m2 MATRIX — claim × axis (status / level)", "", "| claim | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
    for c in claims:
        cells = []
        for n in names:
            r = by.get((n, c["id"])) or {}
            st = r.get("status", "—"); lv = r.get("level")
            m["cells"][f"{c['id']}:{n}"] = {"status": st, "level": lv}
            cells.append(f"{st}{'/L' + str(lv) if lv else ''}")
        lines.append(f"| {c['id']} {c['title'][:40]} | " + " | ".join(cells) + " |")
    gaps = [r for r in rows if r.get("status") in ("GAP", "CONFLICT", "NOT_IN_CORPUS")]
    lines += ["", f"**{len(gaps)} cells need work** (GAP / CONFLICT / NOT_IN_CORPUS).", ""]
    rec = [r for r in rows if r.get("status") == "RECORD_SUPPLIED"]      # S-m2-record-docs-to-cells: counted apart, never a corpus absence
    if rec:
        lines += [f"**{len(rec)} cells RECORD_SUPPLIED** — the answer called a record document (order / complaint / exhibit) 'not in corpus' "
                  "while its [doc] text was in the prompt; not a corpus gap, re-asked when the pin/prompt code changes (stale, like GAP).", ""]
        lines += [f"- claim {r['claim']} × {r['bot']}: RECORD_SUPPLIED (was {r.get('record_reclassified_from')}) — {str(r.get('record_note') or '')[:220]}" for r in rec[:30]] + [""]
    for r in gaps[:60]:
        lines.append(f"- claim {r['claim']} × {r['bot']}: {r.get('status')} — {str(r.get('gap') or r.get('guardrail') or r.get('finding') or '')[:220]}")
    if posture is not None:
        keep = ("vehicle", "rule_verbatim", "authorities", "trigger_event", "trigger_date", "deadline_rule", "deadline_date", "deadline_computed",
                "deadline_model", "standard", "must_contain", "alternatives_rejected", "status", "gap", "guardrail", "rule_verbatim_chunk", "pin_version", "forum")
        m["posture"] = {k: posture.get(k) for k in keep if k in posture}
        alts = "; ".join("%s (%s)" % (a.get("vehicle"), a.get("why")) for a in (posture.get("alternatives_rejected") or [])) or "-"
        lines += ["", "## POSTURE (the movant's question — one cell per run; deadline computed by the runner, never the model)", "",
                  posture_line(posture).replace("[m2] posture: ", "- "), f"- must_contain: {'; '.join(posture.get('must_contain') or []) or '-'}",
                  f"- alternatives rejected: {alts}",
                  f"- rule_verbatim [corpus:{posture.get('rule_verbatim_chunk') or '-'}]: {str(posture.get('rule_verbatim') or '')[:300]}"]
    (outdir / "MATRIX.json").write_text(json.dumps(m, indent=1), encoding="utf-8")
    p = outdir / "MATRIX.md"; p.write_text("\n".join(lines), encoding="utf-8"); return p


_STALE_RERUN = ("GAP", "CONFLICT", "NOT_IN_CORPUS", "RECORD_SUPPLIED")   # statuses a pin/family/prompt fix can change; GROUNDED cells stay (RECORD_SUPPLIED: S-m2-record-docs-to-cells)


def plan_resume(cells_path: Path, current: str | None = None) -> tuple[set, list, int]:
    """RESUME plan over an existing cells.jsonl → (done keys, kept rows, stale count).
    S-m2-resume-stale (2026-10-24) — CLASS RULE: a cell is reusable only under the pin/family version that produced it.
    DEFECT: order17 run3 skipped 54 hollow-run GAP cells for claims 1-9 after S-m2-pa-reporter was promoted — the
    resume keyed on (bot, claim) alone. Now a GAP/CONFLICT/NOT_IN_CORPUS row whose `pin_version` != current is
    re-scheduled (a missing pin_version = pre-fix row = stale); GROUNDED rows are kept whatever their version."""
    cur = current if current is not None else _pin_version()
    done, rows, stale = set(), [], 0
    for l in cells_path.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(l)
        except Exception:
            continue
        st = r.get("status")
        if st in (None, "LANE_ERROR", "DRY"):
            continue
        if st in _STALE_RERUN and r.get("pin_version") != cur:
            stale += 1; continue
        done.add((r["bot"], str(r["claim"]))); rows.append(r)
    return done, rows, stale


def plan_cells(bots: list, claims: list, outdir: Path, resume: bool | None = None) -> tuple[list, set, list, int]:
    """S-m2-resume-retrieve-planned (2026-10-24) — CLASS RULE: the run plan (done / stale / missing) is computed BEFORE a single
    retrieval future is submitted, and only the cells that WILL run are retrieved. Returns (todo, done, kept_rows, stale):
    todo in order_cells() order minus the done keys; a fresh run (no cells.jsonl, or M2_RESUME=0) returns every cell unchanged.
    One function so main() cannot submit retrieval for a cell the plan then skips (a 52-cell re-run must retrieve 52, not 102)."""
    if resume is None:
        resume = os.environ.get("M2_RESUME", "1") == "1"
    rows: list = []; done: set = set(); stale = 0
    if resume and (outdir / "cells.jsonl").exists():
        done, rows, stale = plan_resume(outdir / "cells.jsonl")       # S-m2-resume-stale: stale GAP/CONFLICT rows re-scheduled
    todo = [(b, c) for b, c in order_cells(bots, claims) if (b["name"], str(c["id"])) not in done]   # S-m2-speed: M2_ORDER=unit → claim outer, bot inner (default 'bot' = live order)
    return todo, done, rows, stale


_BOT_CAPS: dict = load_bot_caps()      # per-bot max_tokens (M2_BOT_CAPS); {} = global MAX_TOKENS everywhere (live)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("draft", nargs="?", default=""); ap.add_argument("--probe-families", default="", metavar="SOURCES_TXT"); ap.add_argument("--bots", default=os.environ.get("M2_BOTS", "")); ap.add_argument("--claims", default="")
    ap.add_argument("--workers", type=int, default=int(os.environ.get("M2_WORKERS", "6"))); ap.add_argument("--dry", action="store_true")
    ap.add_argument("--facts", default="", help="CASE_FACTS.md (default: sibling cases/<slug>/CASE_FACTS.md)")
    ap.add_argument("--forum", default=os.environ.get("M2_FORUM", ""), help="fed | pa | nj | any (comma list ok, e.g. fed,pa); default: inferred from the facts caption/court line (S-m2-forum-families)")
    ap.add_argument("--derive-caps", default="", metavar="CELLS_JSONL", help="write per-bot max_tokens caps (p95×margin, floor, ceiling) from an observed cells.jsonl and exit")
    ap.add_argument("--caps-out", default="", metavar="JSON", help="where --derive-caps writes (default: <cells>.bot_caps.json)")
    a = ap.parse_args(argv)
    if a.derive_caps:
        d = derive_bot_caps(a.derive_caps); out = Path(a.caps_out or (a.derive_caps + ".bot_caps.json"))
        out.write_text(json.dumps(d, indent=1), encoding="utf-8")
        for b, e in d["evidence"].items(): print(f"  {b:<20} n={e['n']:>3} p95={e['p95']:>5} max_stop={e['max_stop']:>5} cap={e['cap']}")
        print(f"[m2] bot caps → {out} (floor {d['floor']}, margin {d['margin']}, ceiling {d['ceiling']})"); return 0
    if a.probe_families:
        return 1 if probe_families(a.probe_families) else 0
    draft = Path(a.draft); text = draft.read_text(encoding="utf-8", errors="replace")
    claims = split_claims(text)
    if a.claims:
        lo, _, hi = a.claims.partition("-"); lo, hi = int(lo), int(hi or lo)
        claims = [c for c in claims if lo <= c["id"] <= hi]
    _names = [x for x in a.bots.split(",") if x]
    _cell_names = [x for x in _names if x != "posture"]
    bots = load_bots(_cell_names) if _cell_names else ([] if _names else load_bots())   # --bots posture alone: no per-claim cell
    pbot = load_posture_bot() if posture_enabled(a.bots) else None          # S-m2-posture-axis: ONE run-level cell (bot 'posture', claim 0)
    if posture_enabled(a.bots) and pbot is None and "posture" in a.bots:
        print("[m2] posture: bot folder bots/posture/posture (unit: run) not found — skipped", flush=True)
    fp = Path(a.facts) if a.facts else next((p for p in draft.parents if (p / "CASE_FACTS.md").exists()), None)
    facts = (fp / "CASE_FACTS.md").read_text(encoding="utf-8", errors="replace") if fp and not a.facts else (Path(a.facts).read_text(encoding="utf-8", errors="replace") if a.facts else "")
    global _RUN_FORUM                                                  # S-m2-forum-families: one forum per run, pins never filtered
    forum = (a.forum or "").strip().lower() or infer_forum(facts, str(draft.resolve()) + " " + (str(fp) if fp else ""))
    _RUN_FORUM = {x.strip() for x in forum.split(",") if x.strip()} or {"any"}
    if forum == "any" and not a.forum:
        print("[m2] WARNING forum: could not infer the forum from the facts caption/court line — running UNFILTERED (pass --forum pa|fed|nj or M2_FORUM)", flush=True)
    else:
        print(f"[m2] forum: {','.join(sorted(_RUN_FORUM))} ({'explicit' if a.forum else 'inferred from facts'}); semantic families outside it dropped, exact-cite pins kept", flush=True)
    outdir = Path(os.environ["M2_OUT"]) if os.environ.get("M2_OUT") else draft.with_suffix(".m2")
    require_semantic_at_start()                                        # S-m2-refuse-fts: exit 3 BEFORE outdir / any cell exists
    outdir.mkdir(parents=True, exist_ok=True)
    print(f"[m2] {draft.name}: {len(claims)} claims × {len(bots)} bots = {len(claims)*len(bots)} cells; lane {LANE}; workers {a.workers}; facts {len(facts)} chars; "
          f"order {CELL_ORDER}; prefix_order {int(PREFIX_ORDER)}; bot caps {_BOT_CAPS or 'global ' + str(MAX_TOKENS)}")
    # S-m2-record-docs-to-cells: the case tree = the parent holding CASE_FACTS.md (or the --facts file's folder); its adverse paper
    # (named by the units header) and its as-filed complaint reach every cell as [doc] blocks — record, never searched in the corpus.
    case_root = (fp.parent if a.facts else fp) if fp else next((p for p in draft.parents if (p / "intake").is_dir()), None)
    prime_record_docs(text, case_root)
    # S-m2-resume-retrieve-planned: the plan (done / stale / missing) is fixed HERE, before any retrieval future exists; only the
    # cells in `todo` are retrieved. (RESUME: cells already on disk are not redone; a done cell is never re-retrieved.)
    todo, done, rows, stale = plan_cells(bots, claims, outdir)
    total = len(bots) * len(claims)
    if done or stale: print(f"[m2] resume: {len(done)} cells already done, {stale} stale (pin_version != {_pin_version()}) re-run, {total-len(done)} to run", flush=True)
    if len(todo) != total or done or stale:
        print(f"[m2] resume: retrieving {len(todo)} of {total} cells (done cells are never re-retrieved)", flush=True)
    run_pst = pbot is not None and ("posture", "0") not in done          # S-m2-posture-axis: RESUME keeps a non-stale posture row like any cell
    prow = next((r for r in rows if r.get("bot") == "posture" and r.get("claim") == 0), None)
    if pbot is not None: print(f"[m2] posture: {'resumed from cells.jsonl' if not run_pst else 'ON — one run-level cell (bot posture, claim 0), M2_POSTURE=0 disables'}", flush=True)
    # S-m2-retrieval-pipelined (2026-10-24) — CLASS RULE: a cell goes to the lane the moment ITS retrieval returns; the only
    # barrier is the first retrieval wave. DEFECT (output/research/m2_speed_profile_2026-10-24.md): a 102-cell run spent 519 s in
    # a retrieval phase that had to finish for EVERY cell before the FIRST model call (1737 s wall = 519 retrieval + ~1000 model
    # + ~220 learn_back/MATRIX). Scheduling only: fetch_cell / run_cell / prompt / cell content are untouched (cells byte-equal).
    #   * retrieval pool = RETRIEVAL_WORKERS (daemon is CPU/GPU-embed bound); lane pool = --workers (in-flight lane calls bounded).
    #   * first wave: the first RETRIEVAL_WORKERS retrievals all return before the first lane call — a non-embed answer anywhere
    #     in that wave aborts with ZERO cells exactly as the old phase-1 barrier did (regress_infra_guards B); a flip in a later
    #     retrieval stops new submissions, cancels the queue and keeps the semantic cells already written (the old mid-run rule).
    #   * cells.jsonl is appended as each cell returns (RESUME unchanged); per-bot json / learn_back / MATRIX run after the last.
    tf = t0 = time.time(); first_model_at = None; ret_done = 0; ret_last_at = None; degraded_midrun = False; where = ""
    buffered: list = []                                                # cells whose retrieval returned during the first wave
    wave = min(RETRIEVAL_WORKERS, len(todo))
    jl = None                                                          # opened at the FIRST cell write: an aborted first wave leaves no cells.jsonl (as the old barrier did)
    with ThreadPoolExecutor(max_workers=RETRIEVAL_WORKERS) as rex, ThreadPoolExecutor(max_workers=a.workers) as mex:
        # S-m2-merged2 (2026-10-24): the prefetch is fetch_cell() (the live list) by default; under M2_PREFIX_ORDER=1 it is
        # fetch_cell_parts() (the (pinned, axis) tuple) so build_prompt can place the pinned half in the unit-shared head. Same cached
        # calls, same bytes either way; run_cell(ex=) accepts both shapes. The retrieval pool submits in order_cells() order.
        _prefetch = fetch_cell_parts if PREFIX_ORDER else fetch_cell
        rfuts = {rex.submit(lambda b=b, c=c: (_prefetch(b, c), time.time())): (b, c) for b, c in todo}   # (excerpts, returned-at): the phase line reports the real return time, not when main() got to it
        if not a.dry: prime_graph(claims, bots)                         # phase 0: the graph, once per claim — overlaps the first retrievals
        mfuts: dict = {}

        def submit(b, c, ex):
            nonlocal first_model_at
            if first_model_at is None:
                first_model_at = time.time()
                print(f"[m2] pipelined: first model call at +{first_model_at - tf:.0f}s (was: after full retrieval phase)", flush=True)
            mfuts[mex.submit(run_cell, b, c, facts, a.dry, ex)] = (b["name"], c["id"])
            pending.add(next(reversed(mfuts)))

        def arm_abort(w):
            nonlocal degraded_midrun, where
            if degraded_midrun: return
            degraded_midrun = True; where = w
            for pf in list(rfuts) + list(mfuts):
                pf.cancel()                                            # queued cells never start; running ones finish and are kept
        pending = set(rfuts)
        pfut = None
        if run_pst:                                                    # S-m2-posture-axis: one run-level cell, fetched + asked in the lane pool;
            pfut = mex.submit(run_posture, pbot, text, facts, ",".join(sorted(_RUN_FORUM)), a.dry)   # its row is handled below like any cell
            pending.add(pfut)
        while pending:
            finished, pending = wait(pending, return_when=FIRST_COMPLETED)
            for f in finished:
                if f.cancelled(): continue                             # S-m2-refuse-fts: a queued cell cancelled after the abort
                if f in rfuts:                                         # ---- a retrieval returned
                    b, c = rfuts[f]; ex, ret_last_at = f.result(); ret_done += 1
                    if ret_done == len(rfuts): print(f"[m2] retrieval phase {ret_last_at-tf:.0f}s (cache {CACHE_DIR}; overlapped with the lane)", flush=True)
                    if _RC_MODE.get("abort") and not _fts_allowed():   # S-m2-refuse-fts: non-semantic answer at a cell's retrieval
                        arm_abort("retrieval phase" if first_model_at is None else "model phase"); continue
                    if degraded_midrun: continue
                    if ret_done < wave: buffered.append((b, c, ex)); continue
                    for bb, cc, ee in buffered: submit(bb, cc, ee)
                    buffered.clear(); submit(b, c, ex)
                else:                                                  # ---- a cell returned from the lane
                    if f is pfut:                                      # S-m2-posture-axis: the posture cell never takes the run down
                        try: r = f.result()
                        except Exception as e: r = {"bot": "posture", "group": "posture", "claim": 0, "title": "posture", "excerpts": 0, "status": "LANE_ERROR", "raw": repr(e)[:300], "pin_version": _pin_version()}
                        prow = r
                    else:
                        r = f.result()
                    rows.append(r)
                    if jl is None: jl = (outdir / "cells.jsonl").open("a", encoding="utf-8")   # append-as-you-go: survives a stopped run (RESUME)
                    jl.write(json.dumps(r) + "\n"); jl.flush()
                    print(f"  {r['bot']:<18} claim {r['claim']:>3}  {r.get('status','?'):<14} L{r.get('level') or '-'}  {r.get('seconds','')}s  ex={r['excerpts']}", flush=True)
                    if _RC_MODE.get("abort") and not _fts_allowed():   # S-m2-refuse-fts: stop launching new cells
                        arm_abort("model phase")
    if degraded_midrun:
        if jl: jl.close()
        return abort_retrieval_degraded(outdir, where, len(rows) - len(done))
    for b in bots:
        (outdir / f"{b['name']}.json").write_text(json.dumps([r for r in rows if r["bot"] == b["name"]], indent=1), encoding="utf-8")
    if prow is not None:                                               # S-m2-posture-axis: its own json + the one scan line
        (outdir / "posture.json").write_text(json.dumps([prow], indent=1), encoding="utf-8")
        print(posture_line(prow), flush=True)
    if jl: jl.close()
    model_wall = time.time() - (first_model_at or t0); new = len(rows) - len(done)   # from the FIRST lane call (pipelined: retrieval overlaps it); MEASUREMENT FIX: the lane phase alone (run8: 'wall' 1737 s hid 720 s of learn_back/relink)
    print(f"[m2] model phase {model_wall:.0f}s for {new} new cells ({new/max(model_wall,1)*60:.1f} cells/min; model-time sum {sum(r.get('seconds', 0) for r in rows):.0f}s; "
          f"in-flight avg {sum(r.get('seconds', 0) for r in rows)/max(model_wall,1):.2f}; finish=length {sum(1 for r in rows if r.get('finish_reason') == 'length')}; cap retries {sum(1 for r in rows if r.get('cap_retry'))})", flush=True)
    if os.environ.get("M2_LEARN", "1") == "1":
        tl = time.time()
        print(f"[m2] learning: {learn_back(rows, (fp.name if fp else draft.parent.parent.name))} verified findings → Obsidian graph (connected_notes) ({time.time()-tl:.0f}s)", flush=True)
    else:
        print("[m2] learning: SKIPPED (M2_LEARN=0 — timing run; no learnings.jsonl/notes written, no relink)", flush=True)   # S-m2-relink-offrun
    mp = write_matrix(outdir, rows, bots, claims, posture=prow)
    wall = time.time() - t0; mt = sum(r.get("seconds", 0) for r in rows)
    print(f"[m2] done {len(rows)} cells in {wall:.0f}s wall ({len(rows)/max(wall,1)*60:.1f} cells/min; model-time sum {mt:.0f}s; "
          f"{sum(1 for r in rows for a in (r.get('authorities') or []) if a.get('verified'))} verified cites; "
          f"{sum(1 for r in rows if r.get('status') == 'RECORD_SUPPLIED')} RECORD_SUPPLIED (record doc called 'not in corpus' with its [doc] in the prompt — not corpus gaps)) → {mp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
