"""turbovec_ingest — embed corpus chunks that lack vectors and ADD them to a turbovec index (online
add, no rebuild). SAFE: writes a NEW index file (new_chunks_b4.tv) + sidecar id map; never touches the
existing chunk_vectors_768.npy / chunk_index_b4.idx (no corruption). Makes new content semantic-searchable.

  python -m pipeline.turbovec_ingest                      # drain the whole backlog, checkpointed
  python -m pipeline.turbovec_ingest --max-minutes 75     # stop cleanly after a time budget
  python -m pipeline.turbovec_ingest --limit 50000        # stop after N chunks
  python -m pipeline.turbovec_ingest --status             # coverage report only, embeds nothing
  python -m pipeline.turbovec_ingest --search "..."       # semantic search the new index

CHECKPOINTING (why this file is batched)
  The embedder is driven by scripts/rechunk_backfill_tick.sh under `timeout 5400`. The original
  version loaded every missing chunk into RAM, called model.encode() ONCE on all of them, and wrote
  the index + id map only after that single call returned. With a 2.2M-chunk backlog the timeout
  always fired mid-encode, so every run persisted NOTHING and the backlog never drained.
  Work is now flushed every --batch chunks, so a kill costs at most one batch.

CRASH ORDERING (do not swap these two writes)
  The id map is POSITIONAL: new_chunk_ids.json[i] names the chunk at index position i. The index is
  written BEFORE the id map on purpose. If we die in between, the index holds vectors the map does
  not cover; search skips positions >= len(ids) (see search()), so those are merely unreachable.
  The next run re-embeds the same chunks in the same scan order and the map lands on the FIRST copy,
  which still aligns. Writing the map first would instead leave ids pointing at vectors that do not
  exist yet -- a silent text/vector mismatch. Wasted space is recoverable; a desync is not.
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import sys, json, time, sqlite3
from pathlib import Path
import numpy as np

TV = (_eroot() / "corpus")
CHUNK_DB = TV / "chunk_text.db"
EXIST_IDS = TV / "chunk_ids.json"               # ids already vectorized (the 3.05M baseline)
NEW_INDEX = TV / "new_chunks_b4.tv"
NEW_IDS = TV / "new_chunk_ids.json"
REPORT = (_eroot() / "output/audit/turbovec_coverage.json")
DIM, BITS = 768, 4
QPREFIX = "Represent this sentence for searching relevant passages: "
BATCH_DEFAULT = 25_000
_MODEL = None


def _model(device: str | None = None):
    global _MODEL
    if _MODEL is None:
        from sentence_transformers import SentenceTransformer
        _MODEL = SentenceTransformer("BAAI/bge-base-en-v1.5",
                                     **({"device": device} if device else {}))
    return _MODEL


def _already_vectorized() -> set:
    s = set(json.loads(EXIST_IDS.read_text())) if EXIST_IDS.exists() else set()
    if NEW_IDS.exists():
        s |= set(json.loads(NEW_IDS.read_text()))     # resume: skip ones already added
    return s


def _live_chunk_total() -> int:
    con = sqlite3.connect(f"file:{CHUNK_DB}?mode=ro", uri=True)
    try:
        return con.execute("SELECT count(*) FROM chunk_text").fetchone()[0]
    finally:
        con.close()


def status(write: bool = True) -> dict:
    """Coverage report. Embeds nothing, mutates no index."""
    base = set(json.loads(EXIST_IDS.read_text())) if EXIST_IDS.exists() else set()
    new = set(json.loads(NEW_IDS.read_text())) if NEW_IDS.exists() else set()
    emb = base | new
    total = _live_chunk_total()
    con = sqlite3.connect(f"file:{CHUNK_DB}?mode=ro", uri=True)
    live_emb = 0
    try:
        for (cid,) in con.execute("SELECT chunk_id FROM chunk_text"):
            if cid in emb:
                live_emb += 1
    finally:
        con.close()
    rep = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "chunks_total": total,
        "embedded": live_emb,
        "missing": total - live_emb,
        "coverage_pct": round(100.0 * live_emb / total, 2) if total else 0.0,
        "base_ids": len(base),
        "delta_ids": len(new),
        "orphaned_vectors": len(emb) - live_emb,   # ids in the maps with no live chunk (re-key fallout)
    }
    if write:
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps(rep, indent=1))
    return rep


def ingest(limit: int | None = None, max_minutes: float | None = None,
           batch: int = BATCH_DEFAULT, device: str | None = None,
           source_prefixes: list | None = None) -> dict:
    """Embed not-yet-vectorized chunks in durable batches.

    limit        stop after this many chunks this run (None = drain everything)
    max_minutes  stop cleanly after this wall-clock budget (None = no cap)
    batch        chunks per checkpoint; each flush costs one full rewrite of NEW_INDEX
    source_prefixes  CLASS RULE (2026-10-24, S-download-implies-ingest): a pull is not "in corpus" until its
                 chunks are in the SEMANTIC index (m2 refuses FTS). Without a filter this scan walks the whole
                 table in rowid order and the newest pull (last rows) is never reached before the time budget —
                 the 871 refilled 231 Pa. Code chunks stayed invisible and m2 reported Pa.R.C.P. 1028(e) NOT IN
                 CORPUS. A pull passes its own source keys/prefixes here (uses ix_ct_source) and is embedded first.
    """
    import turbovec
    t0 = time.time()
    deadline = (t0 + max_minutes * 60) if max_minutes else None
    have = _already_vectorized()
    total = _live_chunk_total()
    print(f"already vectorized: {len(have):,} | live chunks: {total:,}", flush=True)

    idx = turbovec.TurboQuantIndex.load(str(NEW_INDEX)) if NEW_INDEX.exists() \
        else turbovec.TurboQuantIndex(dim=DIM, bit_width=BITS)
    id_map = json.loads(NEW_IDS.read_text()) if NEW_IDS.exists() else []
    added = 0
    pend_ids: list = []
    pend_txt: list = []

    def flush() -> None:
        """Encode + persist one batch. Index first, id map second (see module docstring)."""
        nonlocal added, pend_ids, pend_txt, id_map
        if not pend_ids:
            return
        vecs = _model(device).encode(pend_txt, batch_size=128, normalize_embeddings=True,
                                    show_progress_bar=False).astype("float32")
        idx.add(vecs)                                   # ONLINE add — no rebuild, no train
        idx.write(str(NEW_INDEX))                       # (1) vectors durable
        id_map = id_map + pend_ids
        tmp = NEW_IDS.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(id_map))
        tmp.replace(NEW_IDS)                            # (2) map durable, atomic rename
        added += len(pend_ids)
        pend_ids, pend_txt = [], []
        print(f"  checkpoint: +{len(vecs):,} (run total {added:,}, index {len(id_map):,}) "
              f"{time.time() - t0:.0f}s", flush=True)

    con = sqlite3.connect(f"file:{CHUNK_DB}?mode=ro", uri=True)
    stopped = "complete"
    try:
        if source_prefixes:
            where = " OR ".join("source LIKE ?" for _ in source_prefixes)
            cur = con.execute(f"SELECT chunk_id, text FROM chunk_text WHERE {where}",
                              [p.rstrip("%") + "%" for p in source_prefixes])
        else:
            cur = con.execute("SELECT chunk_id, text FROM chunk_text")
        for cid, txt in cur:
            if cid in have or not txt:
                continue
            pend_ids.append(cid)
            pend_txt.append(txt)
            if len(pend_ids) >= batch:
                flush()
                if limit is not None and added >= limit:
                    stopped = "limit"
                    break
                if deadline and time.time() >= deadline:
                    stopped = "time_budget"
                    break
        else:
            flush()                                      # tail batch; scan ran to completion
    finally:
        con.close()
    if stopped != "complete" and pend_ids:
        flush()

    rep = status()
    print(f"added {added:,} vectors -> {NEW_INDEX} | stop={stopped} | "
          f"coverage {rep['coverage_pct']}% ({rep['missing']:,} still missing)", flush=True)
    return {"added": added, "stopped": stopped, **rep}


def search(q: str, k: int = 5):
    import turbovec
    idx = turbovec.TurboQuantIndex.load(str(NEW_INDEX))
    ids = json.loads(NEW_IDS.read_text())
    qv = _model().encode([QPREFIX + q], normalize_embeddings=True).astype("float32")
    scores, pos = idx.search(qv, k)
    con = sqlite3.connect(str(CHUNK_DB))
    for s, p in zip(np.atleast_2d(scores)[0], np.atleast_2d(pos)[0]):
        if p < 0 or p >= len(ids):
            continue
        row = con.execute("SELECT text, source FROM chunk_text WHERE chunk_id=?", (ids[int(p)],)).fetchone()
        if row:
            print(f"[{float(s):.3f}] {(row[1] or '')[:22]:22} {(row[0] or '')[:170]}")


def _arg(name: str, cast=str, default=None):
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return cast(sys.argv[i + 1])
    return default


if __name__ == "__main__":
    if "--search" in sys.argv:
        search(sys.argv[sys.argv.index("--search") + 1])
    elif "--status" in sys.argv:
        print(json.dumps(status(), indent=1))
    else:
        prefixes = [s for s in (_arg("--source-prefix") or "").split(",") if s]
        if _arg("--sources-from"):                      # a pull manifest: {source_key: ...} or [source_key, ...]
            m = json.loads(Path(_arg("--sources-from")).read_text())
            prefixes += list(m.keys() if isinstance(m, dict) else m)
        ingest(limit=_arg("--limit", int), max_minutes=_arg("--max-minutes", float),
               batch=_arg("--batch", int, BATCH_DEFAULT), device=_arg("--device"),
               source_prefixes=prefixes or None)
