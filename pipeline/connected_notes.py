"""connected_notes — every learning becomes a SMALL, ATOMIC, LINKED Obsidian note. Nothing sits alone.
Zettelkasten for Beezle/Hermes: one idea per note, auto-[[wikilinked]] to related notes by shared
concepts, with a Map-of-Content (MOC) hub so even a brand-new note has a home.

DETERMINISTIC (no LLM) -> runs as a `--no-agent` cron (zero load on the single GPU; fixes the
timing-out agent-based lessons-learned-logger). It only splits, tags, links and indexes text that
ALREADY exists — it never invents content.

Durability: this is the Obsidian FALLBACK for the assistant's compacting memory. Learnings are written
both to the assistant memory dir (working store, can be compacted) AND here (append-only, never lost).

Sources ingested (each entry -> one atomic note):
  - obsidian_sync/00_system/lessons_learned.md         (Hermes/system lessons)
  - obsidian_sync/_hermes_learnings/*.md               (record_learning writeback)
  - obsidian_sync/definitions_learned/LEARNING_LOG*.md (definition-scan learnings)
  - output/learnings.jsonl                             (anything appended by code: {text,tags,source})
Output: obsidian_sync/_hermes_learnings/notes/<slug>.md  + _MOC.md index.

CLI:  python -m pipeline.connected_notes            # ingest + relink + MOC
      python -m pipeline.connected_notes --relink   # just recompute links across existing notes
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import sys, re, json, hashlib, os
from pathlib import Path

ROOT = next((p for p in (_eroot(), _eroot())
             if (p / "obsidian_sync").exists()), _eroot())
OBS = ROOT / "obsidian_sync"
NOTES = Path(os.environ["CN_NOTES"]) if os.environ.get("CN_NOTES") else OBS / "_hermes_learnings" / "notes"
JSONL = ROOT / "output" / "learnings.jsonl"
# S-relink-incremental: cached concept index {note path: {"m": mtime, "c": [concepts], "s": [semantic nbrs], "r": [related]}}
# — refreshed by mtime (one stat per note, one READ per changed note) so relink(touched=[...]) never reads the whole vault.
CONCEPT_IDX = (Path(os.environ["CN_CONCEPT_INDEX"]) if os.environ.get("CN_CONCEPT_INDEX")
               else ROOT / "output" / "audit" / "notes_concepts.json")
# V4: Obsidian sync leaves duplicate clones like "note (# Edit conflict 2026-07-03 d2ijrwC #).md" and
# "note (conflicted copy).md". They are NOT canonical notes — linking/ingesting them double-counts a note
# and pollutes the graph. Exclude them everywhere the linker enumerates notes.
_CONFLICT_CLONE = re.compile(r"\(#?\s*Edit conflict\b|conflicted copy|\bconflicted\b", re.I)
def _is_conflict_clone(p) -> bool:
    return bool(_CONFLICT_CLONE.search(getattr(p, "name", str(p))))
def _md_notes(d):
    """Canonical *.md notes in dir d, minus _MOC.md and Obsidian edit-conflict clones (V4)."""
    return [n for n in (sorted(d.glob("*.md")) if d.exists() else [])
            if n.name != "_MOC.md" and not _is_conflict_clone(n)]
# Slugs the adversarial deposit-verifier REFUTED (unconfirmable against the corpus). write_note() skips
# them so a quarantined finding is never regenerated on the next relink/ingest — the quarantine sticks.
REFUTED_FILE = ROOT / "output" / "refuted_slugs.txt"
_REFUTED_CACHE = None
def _refuted_slugs() -> set:
    global _REFUTED_CACHE
    if _REFUTED_CACHE is None:
        try:
            _REFUTED_CACHE = {x.strip() for x in REFUTED_FILE.read_text(encoding="utf-8").splitlines() if x.strip()}
        except Exception:
            _REFUTED_CACHE = set()
    return _REFUTED_CACHE

# concept vocabulary — shared concepts become the [[links]] between notes. Add terms freely; a note
# links to every other note that shares >=1 concept, so the graph self-assembles.
CONCEPTS = ["fcra", "fdcpa", "tila", "ecoa", "fcba", "utpcpl", "bad faith", "8371", "1692", "1681",
            "recusal", "stare decisis", "binding", "erie", "removal", "diversity", "venue",
            "judicial", "disciplinary", "odc", "sanctions", "verification", "service", "twombly",
            "default", "garnish", "insurance", "dental", "medical", "debt", "credit", "notice",
            "regulatory", "cron", "llm", "context", "gpu", "prompt injection", "m0", "audit",
            "scorecard", "umbrella", "definition", "intake", "docket", "ecf", "obsidian", "chromadb"]
STOP = set("the a an and or of to in for on with is are be this that it as at by from your you we i".split())


def _slug(text: str) -> str:
    h = hashlib.sha1(text.strip().lower().encode("utf-8")).hexdigest()[:8]
    head = re.sub(r"[^a-z0-9]+", "-", text.strip().lower())[:48].strip("-")
    return f"{head or 'note'}-{h}"


def _concepts_in(text: str) -> list:
    low = text.lower()
    found = [c for c in CONCEPTS if c in low]
    # plus salient capitalized/§-cited tokens as ad-hoc concepts
    for m in re.findall(r"§\s?\d+[\w.\-]*|\b\d{3,4}[a-z]?\b", text):
        if m.lower() not in found:
            found.append(m.lower())
    return found[:8]


def _atomic_entries(raw: str) -> list:
    """Split a source doc into atomic learnings: by markdown heading or by bullet/`---` separators."""
    raw = raw.strip()
    if not raw:
        return []
    # prefer heading/`---` sections; fall back to top-level bullets
    chunks = re.split(r"\n(?=#{1,3}\s)|\n-{3,}\n", raw)
    out = []
    for c in chunks:
        c = c.strip()
        if len(c) < 25:
            continue
        if len(c) > 1200:                                  # a long section -> split to bullets
            for b in re.split(r"\n(?=[-*]\s)", c):
                b = b.strip()
                if 25 <= len(b) <= 1200:
                    out.append(b)
        else:
            out.append(c)
    return out



# Corpus pointer = the followable coordinate that lets an LLM walking the graph PULL the full text from the
# DB instead of the note duplicating it. Prefer an explicit "cite" field; else extract cites/chunk-ids from
# the text. This is the "matrix" half of the note: tiny, machine-readable, context-cheap.
_CITE_PATS = [
    r"\b\d+\s+U\.?\s?S\.?\s?C\.?\s+\u00a7*\s*[0-9]+[\w().\-]*",
    r"\b\d+\s+C\.?\s?F\.?\s?R\.?\s+\u00a7*\s*[0-9.]+[\w().\-]*",
    r"\bPa\.?\s*R\.?\s*C\.?\s*P\.?\s*[0-9.]+[\w()]*",
    r"\bU\.?C\.?C\.?\s*\u00a7?\s*\d+-\d+[\w()]*",
    r"\bchunks?\s+[0-9a-f][0-9a-f\-]{5,}",
    r"\b[A-Z][A-Za-z.'\-]+\s+v\.?\s+[A-Z][A-Za-z.'\-]+",
]
def _corpus_pointer(text: str, cite: str = "") -> str:
    if cite and cite.strip():
        return cite.strip().replace('"', "'")[:200]
    found = []
    for pat in _CITE_PATS:
        for m in re.findall(pat, text):
            m = m.strip()
            if m and m not in found:
                found.append(m)
        if len(found) >= 4:
            break
    return "; ".join(found[:4]).replace('"', "'")


def write_note(text: str, tags=None, source: str = "", cite: str = "") -> Path | None:
    """Write ONE atomic note (idempotent by content hash). Frontmatter + body; links added in relink()."""
    text = text.strip()
    if len(text) < 25:
        return None
    NOTES.mkdir(parents=True, exist_ok=True)
    slug = _slug(text)
    if slug in _refuted_slugs():
        return None   # quarantined by the adversarial verifier — never (re)generate its note
    p = NOTES / f"{slug}.md"
    if p.exists():
        return p
    concepts = sorted(set((tags or []) + _concepts_in(text)))
    title = re.sub(r"^#+\s*", "", text.splitlines()[0])[:80]
    ptr = _corpus_pointer(text, cite)
    fm = ["---", f"id: {slug}", f"title: \"{title}\"",
          f"concepts: [{', '.join(concepts)}]", f"cite: \"{ptr}\"", f"source: {source}", "type: learning", "---", ""]
    p.write_text("\n".join(fm) + text + "\n\n## Related\n", encoding="utf-8")
    return p


def _read_concepts(p: Path) -> list:
    m = re.search(r"^concepts:\s*\[(.*)\]", p.read_text(encoding="utf-8"), re.M)
    return [c.strip() for c in (m.group(1).split(",") if m else []) if c.strip()]



def _sem_cache_load(notes, embed_only=None):
    """Shared by the full and incremental semantic passes: load the slug→vector cache, embed what is missing
    (all missing notes, capped by CN_MAX_NEW_PER_RUN; or ONLY `embed_only` when given), persist, and return
    (emb_slugs, vecs) in note order. Raises on any failure (callers fall back to concept-only)."""
    import numpy as np
    _EMB_MODEL = _emb_model()                                 # daemon-backed; raises -> {} (concept-only relink)
    slugs = [p.stem for p in notes]
    cache = {}
    if _EMB_CACHE.exists():
        d = np.load(_EMB_CACHE, allow_pickle=True)
        for s, v in zip(list(d["slugs"]), d["vecs"]):
            cache[s] = v
    pool = notes if embed_only is None else [p for p in embed_only if p in set(notes)]
    new = [p for p in pool if p.stem not in cache]            # slug = sha1(content) -> changed note = new slug
    # BOUNDED per run: embed at most CN_MAX_NEW_PER_RUN new notes so one invocation fits the 120s cron
    # window; the remainder keep their existing links and get embedded on the next tick (cache persists).
    _cap = int(os.environ.get("CN_MAX_NEW_PER_RUN", "50"))
    if embed_only is None and len(new) > _cap:
        new = new[:_cap]
    if new:
        nv = _EMB_MODEL.encode([_emb_text(p) for p in new], normalize_embeddings=True).astype("float32")
        for p, v in zip(new, nv):
            cache[p.stem] = v
    cache = {s: cache[s] for s in slugs if s in cache}        # drop removed; keep only embedded current notes
    emb = [s for s in slugs if s in cache]                    # embedded slugs only; un-embedded wait for next run
    if not emb:
        return [], None
    vecs = np.array([cache[s] for s in emb], dtype="float32")
    _tmp = _EMB_CACHE.with_name(f"{_EMB_CACHE.stem}.{os.getpid()}.tmp.npz")   # atomic: the loop and drafts share this file
    np.savez(_tmp, slugs=np.array(emb), vecs=vecs)
    os.replace(str(_tmp), str(_EMB_CACHE))
    return emb, vecs


def _sem_row_picks(row, i, emb, k, threshold):
    order = row.argsort()[::-1]
    picks = []
    for j in order:
        jj = int(j)
        if jj == i:
            continue
        if row[jj] < threshold:
            break
        picks.append(emb[jj])
        if len(picks) >= k:
            break
    return set(picks)


def _semantic_neighbors(notes, k: int = 8, threshold: float = 0.58):
    """Per-note top-k cosine-similar neighbours via BAAI/bge (incremental, cached by slug=content-hash).
    This is what links a concept ACROSS authorities — TFM 'bill of exchange' to the Title 31 / UCC / OCC
    notes that mean the same thing even with different wording. Returns {slug: set(neighbour_slugs)};
    {} on any failure so relink() still works concept-only."""
    try:
        emb, vecs = _sem_cache_load(notes)
        if vecs is None:
            return {}
        sims = vecs @ vecs.T                                      # full pairwise cosine (BLAS, fast)
        return {s: _sem_row_picks(sims[i], i, emb, k, threshold) for i, s in enumerate(emb)}
    except Exception:
        return {}


def _semantic_rows(notes, touched, k: int = 8, threshold: float = 0.58):
    """Incremental twin of _semantic_neighbors: embed ONLY the touched notes, then recompute the neighbour set
    for the touched notes AND for every note whose similarity to a touched note clears the threshold (the only
    notes whose top-k can have changed). Returns ({slug: set}, ok) — ok=False means no semantic layer (daemon down)."""
    try:
        emb, vecs = _sem_cache_load(notes, embed_only=touched)
        if vecs is None:
            return {}, False
        pos = {s: i for i, s in enumerate(emb)}
        tidx = [pos[p.stem] for p in touched if p.stem in pos]
        if not tidx:
            return {}, True
        S = vecs @ vecs[tidx].T                                   # N × |touched|
        cand = set(tidx) | {int(i) for i in (S >= threshold).any(axis=1).nonzero()[0]}
        out = {}
        for i in sorted(cand):
            out[emb[i]] = _sem_row_picks(vecs @ vecs[i], i, emb, k, threshold)
        return out, True
    except Exception:
        return {}, False


def _load_idx() -> dict:
    try:
        return json.loads(CONCEPT_IDX.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_idx(idx: dict) -> None:
    try:
        CONCEPT_IDX.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONCEPT_IDX.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")
        os.replace(str(tmp), str(CONCEPT_IDX))
    except Exception:
        pass


def _concept_map(notes, idx: dict) -> dict:
    """{note: set(concepts)} for ALL notes from the cached index: one os.stat per note, one READ only for a note
    whose mtime changed (or that is new). Index entries for removed notes are dropped."""
    keep = {}
    out = {}
    for n in notes:
        key = str(n)
        try:
            m = os.stat(n).st_mtime
        except OSError:
            continue
        e = idx.get(key)
        if not e or e.get("m") != m:
            e = {"m": m, "c": _read_concepts(n)}
        keep[key] = e
        out[n] = set(e["c"])
    idx.clear(); idx.update(keep)
    return out


def _write_related(n: Path, related: list, idx: dict) -> None:
    body = n.read_text(encoding="utf-8")
    body = body.split("\n## Related\n")[0]
    links = ("\n".join(f"- [[{r}]]" for r in related[:16]) if related else "- [[_MOC]]")
    n.write_text(body + "\n## Related\n" + links + "\n", encoding="utf-8")
    e = idx.setdefault(str(n), {})
    try:
        e["m"] = os.stat(n).st_mtime      # the rewrite changed mtime, not the frontmatter: keep the index warm
    except OSError:
        pass
    e["r"] = related


def relink(touched=None) -> int:
    """Recompute [[links]] by shared concepts + semantic neighbours. Every note links to its neighbours;
    a note with no neighbour links to [[_MOC]] so NOTHING sits alone. Returns note count.
    touched=None (cron path): FULL walk — every note's '## Related' block is rewritten.
    touched=[Path,...] (a run's new notes): concepts for ALL notes come from the cached index (stat-only unless a
    note changed); '## Related' is rewritten ONLY for the touched notes, the existing notes they newly link to
    (reverse edges: shared concept, or semantic neighbourhood that changed) — and the MOC is rebuilt from the
    in-memory concept map (one write). Byte-identical to the full walk for every rewritten block."""
    notes = _md_notes(NOTES)   # V4: excludes _MOC.md + edit-conflict clones
    idx = _load_idx()
    cmap = _concept_map(notes, idx)
    if touched is None:
        sem = _semantic_neighbors(notes)   # {slug: set(slug)} — cross-authority same-meaning links (may be {})
        for n in notes:
            mine = cmap[n]
            related = {o.stem for o in notes if o != n and (mine & cmap[o])}  # shared-concept neighbours
            related |= sem.get(n.stem, set())                                 # + semantic neighbours
            related = sorted(related)
            _write_related(n, related, idx)
            idx[str(n)]["s"] = sorted(sem.get(n.stem, set()))
        _write_moc(notes, cmap)
        _save_idx(idx)
        return len(notes)
    # ── incremental ──
    tset = {Path(p).resolve() for p in touched}
    T = [n for n in notes if n.resolve() in tset]
    if not T:
        _save_idx(idx)
        return len(notes)
    sem, sem_ok = _semantic_rows(notes, T)
    Tset = set(T)
    tcon = set().union(*(cmap[t] for t in T))
    rewrite = set(T)
    rewrite |= {n for n in notes if n not in rewrite and (cmap[n] & tcon)}      # reverse concept edges
    rewrite |= {n for n in notes if n.stem in sem}                              # semantic neighbourhoods that moved
    for n in rewrite:
        e = idx.get(str(n), {})
        mine = cmap[n]
        related = {o.stem for o in notes if o != n and (mine & cmap[o])}
        if n.stem in sem:
            s = sem[n.stem]
        elif sem_ok and "s" in e:
            s = set(e["s"])
        else:
            s = set()
        related |= s
        related = sorted(related)
        if n not in Tset and e.get("r") is not None and e["r"][:16] == related[:16] and bool(e["r"]) == bool(related):
            continue                                                            # block unchanged: no read, no write
        _write_related(n, related, idx)
        idx[str(n)]["s"] = sorted(s)
    _write_moc(notes, cmap)
    _save_idx(idx)
    return len(notes)


# ── graph READ (deterministic, no LLM) — lets m0 / Hermes pull accumulated, grounded understanding
#    out of the Obsidian link graph and into their reasoning, instead of starting cold each time. ──
def _all_notes() -> list:
    return _md_notes(NOTES)   # V4: excludes _MOC.md + edit-conflict clones


def _note_title(p: Path) -> str:
    m = re.search(r'^title:\s*"?(.*?)"?\s*$', p.read_text(encoding="utf-8", errors="ignore"), re.M)
    return (m.group(1).strip() if m else p.stem)


def search_notes(query: str, k: int = 6) -> list:
    """READ the graph: the notes most relevant to a query, scored by shared concepts + keyword hits.
    Returns [{slug, title, score, text}]. For m0/Hermes to reason ON the accumulated knowledge."""
    qconcepts = set(_concepts_in(query))
    qwords = {w for w in re.findall(r"[a-z0-9§.\-]{3,}", query.lower()) if w not in STOP}
    scored = []
    for p in _all_notes():
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        body = txt.split("---", 2)[-1]
        bl = body.lower()
        score = 3 * len(qconcepts & set(_read_concepts(p))) + sum(1 for w in qwords if w in bl)
        if score:
            snip = re.sub(r"\n## Related[\s\S]*$", "", body).strip()
            scored.append((score, p.stem, _note_title(p), snip))
    scored.sort(key=lambda x: -x[0])
    return [{"slug": s, "title": t, "score": sc, "text": body} for sc, s, t, body in scored[:k]]


def related(slug_or_concept: str, k: int = 8) -> list:
    """Notes sharing >=1 concept with the given note (by slug) or a bare concept."""
    p0 = NOTES / f"{slug_or_concept}.md"
    mine = set(_read_concepts(p0)) if p0.exists() else {slug_or_concept.lower()}
    return [p.stem for p in _all_notes() if p.stem != slug_or_concept and (mine & set(_read_concepts(p)))][:k]


# ── semantic graph READ (depth) — embeds notes (cached) so the link-read matches by MEANING, not just
#    keywords; falls back to the keyword search if the embedding model isn't available. ──
_EMB_MODEL = None


class _DaemonEncoder:
    """B-turbovec-daemon (2026-10-24): bge-base vectors come from the resident turbovec daemon (pipeline.retrieval_client.embed,
    <= 64 texts per request) — this process never imports torch / sentence_transformers. Same surface as
    SentenceTransformer.encode(normalize_embeddings=True) for the three call sites; raises RuntimeError when the daemon
    cannot embed (down / degraded / FTS-only) so each caller's existing try/except takes its keyword fallback
    (never a fabricated similarity). The query prefix is applied by the daemon when is_query=True."""
    QPREFIX = "Represent this sentence for searching relevant passages: "

    def encode(self, texts, normalize_embeddings=True, batch_size=64, **_):
        import numpy as np
        from pipeline import retrieval_client as RC
        texts = list(texts)
        out = []
        for i in range(0, len(texts), 64):
            batch = texts[i:i + 64]
            is_q = all(t.startswith(self.QPREFIX) for t in batch)
            r = RC.embed([t[len(self.QPREFIX):] if is_q else t for t in batch], is_query=is_q)
            v = r.get("vectors")
            if not v:
                raise RuntimeError(f"turbovec daemon embed unavailable: {r.get('error')}")
            out += v
        return np.asarray(out, dtype="float32")


def _emb_model():
    global _EMB_MODEL
    if _EMB_MODEL is None:
        _EMB_MODEL = _DaemonEncoder()
    return _EMB_MODEL
_EMB_CACHE = (Path(os.environ["CN_EMB_CACHE"]) if os.environ.get("CN_EMB_CACHE") else NOTES.parent / ".note_embeddings.npz")


def _emb_text(p: Path) -> str:
    t = p.read_text(encoding="utf-8", errors="ignore")
    body = re.sub(r"\n## Related[\s\S]*$", "", t.split("---", 2)[-1]).strip()
    return (_note_title(p) + " | " + " ".join(_read_concepts(p)) + " | " + body)[:1500]


def semantic_search_notes(query: str, k: int = 6) -> list:
    """READ the graph by MEANING. Embeds each note (cached to .note_embeddings.npz, refreshed when the
    note set changes) and cosine-matches the query. Falls back to keyword search_notes on any failure."""
    try:
        import numpy as np
        notes = _all_notes()
        slugs = [p.stem for p in notes]
        _EMB_MODEL = _emb_model()                                 # daemon-backed; raises -> keyword search_notes
        vecs, cached = None, []
        if _EMB_CACHE.exists():
            try:
                d = np.load(_EMB_CACHE, allow_pickle=True)
                cached = [str(s) for s in d["slugs"]]; vecs = d["vecs"]
            except Exception:                                     # corrupt/half-written .npz -> rebuild, don't slow-fallback
                cached, vecs = [], None
        # INCREMENTAL_EMBED_V1 (2026-07-12): embed ONLY notes not already cached.
        # The learning loop writes notes constantly, so `cached != slugs` was almost
        # always true and this re-embedded the ENTIRE graph (~87s) on EVERY call —
        # called 8x per draft (canon+restoration x4 sections) it added ~700s/draft and
        # was mistaken for a GPU/drafter fault. Reuse cached vectors, embed the delta,
        # realign to current slug order. Identical vectors + search; just no wasted work.
        cmap = {str(s): vecs[i] for i, s in enumerate(cached)} if vecs is not None else {}
        missing = [p for p in notes if p.stem not in cmap]
        if missing:
            mv = _EMB_MODEL.encode([_emb_text(p) for p in missing],
                                   normalize_embeddings=True).astype("float32")
            for p, v in zip(missing, mv):
                cmap[p.stem] = v
        vecs = np.array([cmap[s] for s in slugs], dtype="float32") if slugs else None
        if vecs is not None and (missing or cached != slugs):     # persist only when it changed
            # ATOMIC write: the learning loop AND drafts both call this; a plain np.savez to
            # the shared path races and corrupts the .npz (Bad CRC-32 -> 100s keyword fallback,
            # 8x/draft). Write a per-process temp (name ends in .npz so savez won't re-append)
            # then os.replace -- atomic on the same filesystem.
            import os as _os
            _tmp = _EMB_CACHE.with_name(f"{_EMB_CACHE.stem}.{_os.getpid()}.tmp.npz")
            np.savez(_tmp, slugs=np.array(slugs), vecs=vecs)
            _os.replace(str(_tmp), str(_EMB_CACHE))
        if vecs is None:
            return search_notes(query, k)
        qv = _EMB_MODEL.encode(["Represent this sentence for searching relevant passages: " + query],
                               normalize_embeddings=True).astype("float32")[0]
        sims = vecs @ qv
        out = []
        for i in sims.argsort()[::-1][:k]:
            p = notes[int(i)]
            body = re.sub(r"\n## Related[\s\S]*$", "", p.read_text(encoding="utf-8", errors="ignore")
                          .split("---", 2)[-1]).strip()
            out.append({"slug": slugs[int(i)], "title": _note_title(p), "score": float(sims[int(i)]), "text": body})
        return out
    except Exception:
        return search_notes(query, k)


def graph_walk(seeds: list, hops: int = 2, max_nodes: int = 40) -> list:
    """[(slug, hop, via)] — seeds (hop 0) then their vault neighbours by hop. Traversal is the EXISTING walker
    `pipeline.obsidian_graph.obsidian_graph_walk` (wikilink index, typed edges, in+out links, folder scope); this is only
    the adapter from note slugs to vault paths and back. No private BFS lives here (existing-implementation-first)."""
    from pipeline import obsidian_graph as og
    try:
        rel = NOTES.relative_to(og.VAULT).as_posix()
    except ValueError:
        rel = None
    top = rel.split("/")[0] if rel else None   # obsidian_graph scopes by TOP folder only
    order, seen = [(s, 0, None) for s in seeds], set(seeds)
    if rel is None:
        return order
    per = max(4, max_nodes // max(len(seeds), 1))
    for seed in seeds:
        try:
            w = og.obsidian_graph_walk(f"{rel}/{seed}.md", depth=hops, folder_scope=[top], max_nodes=per * 3)
        except Exception:
            continue
        via = {}
        for e in w.get("edges_traversed", []):
            d = e.get("dst") or e.get("to") or e.get("resolved") or ""
            via.setdefault(d, e.get("src") or e.get("from") or seed)
        for d in sorted(int(k) for k in w.get("by_depth", {}) if int(k) > 0):
            for path in w["by_depth"][str(d)]:
                slug = path.rsplit("/", 1)[-1]; slug = slug[:-3] if slug.endswith(".md") else slug
                if slug in seen or not path.startswith(rel + "/"):
                    continue
                seen.add(slug)
                v = via.get(path, seed); v = v.rsplit("/", 1)[-1]; v = v[:-3] if v.endswith(".md") else v
                order.append((slug, d, v))
        if len(order) >= max_nodes:
            break
    return order[:max_nodes]


def graph_context(query: str, k: int = 5, max_chars: int = 4000, semantic: bool = True, hops: int | None = None,
                  max_nodes: int = 40) -> str:
    """A compact block of the most relevant accumulated notes — inject into an m0/Hermes/m2 prompt so the
    reasoning builds on prior GROUNDED understanding from the Obsidian graph (cited by [[slug]]).
    Semantic (by meaning) seeds, THEN the vault's own [[links]] walked `hops` deep (default 2; env GRAPH_HOPS).
    S-graph-first-hit (2026-10-27): before this the read was the k nearest notes and zero link traversal — the room saw
    the first things found, never the connections. Linked notes are emitted hop by hop, each tagged `via [[seed]]`,
    until max_chars; hop-0 text is fuller (900 chars) than hop-1/2 (420) so depth costs breadth, not the seeds."""
    if hops is None:
        try:
            hops = int(os.environ.get("GRAPH_HOPS", "2"))
        except ValueError:
            hops = 2
    out, used = [], 0
    hits = semantic_search_notes(query, k) if semantic else search_notes(query, k)
    by_slug = {h["slug"]: h for h in hits}
    refuted = _refuted_slugs()
    walk = graph_walk([h["slug"] for h in hits], hops=hops, max_nodes=max_nodes)
    # rank each hop's candidates by query-term overlap (title + first 1200 chars) — the walk finds the connections,
    # the query decides which connections are emitted first; seeds keep their semantic order
    qt = {t for t in re.findall(r"[a-z0-9()§.]{3,}", query.lower())}
    def _score(slug):
        try:
            raw = (NOTES / f"{slug}.md").read_text(encoding="utf-8", errors="ignore")[:1400].lower()
        except OSError:
            return -1
        return sum(1 for t in qt if t in raw)
    walk = sorted(walk, key=lambda x: (x[1], 0 if x[1] == 0 else -_score(x[0])))
    # budget: seeds may take at most 55% of max_chars when linked notes exist, so depth is never starved by breadth
    seed_cap = int(max_chars * 0.55) if any(h > 0 for _, h, _ in walk) else max_chars
    for slug, hop, via in walk:
        if slug in refuted:
            continue
        if hop == 0 and used >= seed_cap:
            continue
        if slug in by_slug:
            h = by_slug[slug]; title, text = h["title"], h["text"]
        else:
            p = NOTES / f"{slug}.md"
            try:
                raw = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            title = _note_title(p); text = raw.split("\n## Related\n")[0]
            text = re.sub(r"^---.*?---\s*", "", text, count=1, flags=re.S)
        lim = 900 if hop == 0 else 420
        tag = f"[[{slug}]] {title}" if hop == 0 else f"[[{slug}]] {title}  (hop {hop} via [[{via}]])"
        seg = f"{tag}\n{text.strip()[:lim]}"
        if used + len(seg) > max_chars:
            if hop == 0:
                continue
            break
        out.append(seg); used += len(seg)
    return "\n\n".join(out)


def _write_moc(notes, cmap):
    """Map-of-Content hub grouped by concept — the home every note can reach."""
    by_concept = {}
    for n in notes:
        for c in cmap[n]:
            by_concept.setdefault(c, []).append(n.stem)
    L = ["---", "id: _MOC", "title: \"Learnings — Map of Content\"", "type: moc", "---", "",
         "# Connected Learnings — Map of Content", "", f"{len(notes)} atomic notes, auto-linked by concept.", ""]
    for c in sorted(by_concept, key=lambda x: -len(by_concept[x])):
        L.append(f"## {c}  ({len(by_concept[c])})")
        L += [f"- [[{s}]]" for s in sorted(by_concept[c])[:40]]
        L.append("")
    (NOTES / "_MOC.md").write_text("\n".join(L), encoding="utf-8")


def _learning_md(base, pattern):
    # Pull substantive learning markdown, skipping method/tracking files (_*, *frontier*, *pipeline*).
    d = OBS / base
    if not d.exists():
        return []
    out = []
    for p in sorted(d.rglob(pattern)):
        nm = p.name.lower()
        if nm.startswith("_") or "frontier" in nm or "pipeline" in nm or "waterfall" in nm:
            continue
        if _is_conflict_clone(p):   # V4: skip Obsidian edit-conflict / conflicted-copy clones
            continue
        out.append(p)
    return out


SOURCES = [
    OBS / "00_system" / "lessons_learned.md",
    *( (p for p in (OBS / "_hermes_learnings").glob("2*.md") if not _is_conflict_clone(p))
       if (OBS / "_hermes_learnings").exists() else []),
    *( (p for p in (OBS / "definitions_learned").glob("LEARNING_LOG*.md") if not _is_conflict_clone(p))
       if (OBS / "definitions_learned").exists() else []),
    # Research + comprehension OUTPUT — previously written blind (not in the linked graph). Now ingested
    # so every job's findings become linked atomic notes. Tracking files (_*, *frontier*, *pipeline*) skipped.
    *_learning_md("studies", "*.md"),
    *_learning_md("research", "*.md"),
    *_learning_md("legal/comprehension", "*_comprehension.md"),
    *_learning_md("legal/comprehension", "*_financial_flow.md"),
]


def ingest() -> int:
    n = 0
    for src in SOURCES:
        if src.exists():
            for entry in _atomic_entries(src.read_text(encoding="utf-8", errors="ignore")):
                if write_note(entry, source=src.name):
                    n += 1
    if JSONL.exists():
        for line in JSONL.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                d = json.loads(line)
                if write_note(d.get("text", ""), tags=d.get("tags"), source=d.get("source", "jsonl"), cite=d.get("cite", "")):
                    n += 1
            except Exception:
                pass
    return n


def _coerce_learning_text(text) -> str:
    """Workers (small models) sometimes emit a dict {'title':..,'summary':..} instead of a string. Flatten
    it to readable text so the append AND the note-write both take strings. THE bug this kills: a dict text
    appended fine (json.dumps) but write_note() then THREW, the collector's except skipped the archive move,
    and the same file re-appended every tick — 334/400 recent learnings were duplicates of 4 findings."""
    if isinstance(text, str):
        return text
    if isinstance(text, dict):
        parts = []
        for k in ("title", "summary", "finding", "text", "definition", "gap_description"):
            v = text.get(k)
            if isinstance(v, str) and v.strip():
                parts.append(v.strip())
        for k, v in text.items():
            if k in ("title", "summary", "finding", "text", "definition", "gap_description"):
                continue
            if isinstance(v, str) and v.strip():
                parts.append(f"{k}: {v.strip()}")
            elif isinstance(v, (list, tuple)):
                parts.append(f"{k}: " + "; ".join(str(x) for x in v))
        return "\n".join(parts) if parts else json.dumps(text, ensure_ascii=False)
    return str(text)


def record_learning(text: str, tags=None, source: str = "session", cite: str = "", relink_now: bool = True):
    """Public hook — append a learning to the durable JSONL (so it survives memory compaction) and
    write its atomic note immediately. Call this from anywhere a learning is produced. The JSONL is the
    source of truth; a note-write failure must never strand the caller (ingest() regenerates notes).
    relink_now=False defers the graph relink (a FULL walk of ~80k notes — minutes) so batch callers like
    the keeper collector can ingest hundreds of files fast and relink once at the end."""
    text = _coerce_learning_text(text)
    JSONL.parent.mkdir(parents=True, exist_ok=True)
    import fcntl
    with open(JSONL, "a", encoding="utf-8") as f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)   # serialize concurrent appends from BOTH GPU lanes (no clobber)
        except Exception:
            pass
        f.write(json.dumps({"text": text, "tags": tags or [], "cite": cite, "source": source}) + "\n")
        f.flush()
    try:
        p = write_note(text, tags=tags, source=source, cite=cite)
        if relink_now:
            relink(touched=[p] if p else None)   # S-relink-incremental: one note → link it both ways, no full walk
        return p
    except Exception as e:
        print(f"record_learning: appended OK, note-write failed (non-fatal): {e}")
        return None


if __name__ == "__main__":
    if "--relink" in sys.argv:
        print(f"relinked {relink()} notes")
    else:
        added = ingest()
        total = relink()
        print(f"ingested {added} new atomic note(s); {total} total, all linked. -> {NOTES}")
        print(f"MOC -> {NOTES / '_MOC.md'}")
