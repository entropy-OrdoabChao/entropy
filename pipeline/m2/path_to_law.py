#!/usr/bin/env python3
"""path_to_law — hub-first retrieval for m2 cells (S-m2-path-to-law, 2026-11-09; flag M2_PATH_TO_LAW=1).

WHY: the hybrid axis retrieval hands every cell ~6 excerpts of which ~4 are noise at score 0.10–0.15 (customs CFR,
Pa. Bulletin lists, unrelated briefs). The Obsidian corpus graph (obsidian_sync/_graph/) already ties each rule /
term / topic HUB to the exact chunk ids that hold its text, definitions, interpreting opinions and commentary. This
module resolves the authorities a cell NAMES to those hubs and returns their chunk ids in depth order
(text → defines → interprets → commentary → applies) so the seat can pull 3 exact chunks instead of 6 guesses.

CONTRACT (skill obsidian-corpus-graph, PLAN §14):
  resolve(cell, forum=None) -> (hub_ids, chunk_ids, unresolved_links)
    cell: any dict carrying some of title / text / claim_text / proposition / finding / gap / authorities[].cite.
    hub_ids: hub names that resolved (e.g. 'Pa.R.C.P. 440', '15 USC 1681s-2', 'Am Jur 2d Parties').
    chunk_ids: deduped, ordered text→defines→interprets→commentary→applies; both id forms kept VERBATIM
               (bare 32-hex = chunk_text.db primary key; 'source#sha1[:10]' = search_chunks form).
    unresolved_links: canonical hub names the cell cites that have NO hub = the NOT-IN-CORPUS / ingest signal
               (append_unresolved() writes them to output/graph/unresolved_links.jsonl).
  fetch_chunks(chunk_ids) -> [{chunk_id, source, text}] via the get-by-id route the pipeline already uses
    (chunk_text.db `SELECT … WHERE chunk_id=?`); 'source#hash' ids are mapped once per source through a small side
    index (output/graph/chunk_hash_index.sqlite) — hash = sha1(text[:650])[:10]: beezle_mcp_server.search_chunks mints it over the 650-char
    window case_comprehension._search_one returns (verified 11-09 on 7/7 sample ids; the full-text form is indexed too).

INDEX RESOLUTION (never walks the vault): M2_GRAPH_INDEX=<path> | output/graph/graph.sqlite | output/graph/graph.json |
  obsidian_sync/_graph/_build/graph.json → else parse frontmatter of obsidian_sync/_graph/hubs/*/*.md + _graph/nodes/*/*.md
  (the generated admin tree only — small by design). Expected index shapes (another task builds them; both tolerated):
    graph.json : {"nodes": [ {"path": "...md", "name"|"id"|"title": ..., "kind": ..., "frontmatter"|"properties": {...}} | <props inline> ]}
    graph.sqlite: table nodes|notes with columns (path, name, kind, frontmatter|fm|props JSON)
  Any node whose frontmatter kind ∈ {rule, topic, term} is a hub; `kind: hub` + `hub_of: case` (hubs/cases/<Caption (cite)>.md, 11-10) is a
  'case' hub reachable by every entry of its `aliases` (reporter + parallel cites, caption) and carrying source + text_chunks (holding first);
  kind ∈ {text, defines, interprets, commentary, applies} is a
  corpus pointer node whose rules/topics/defines list the hubs it is tied to.

Zero LLM. Deterministic. No torch, no daemon, no local-gpu — safe to import anywhere.
"""
from __future__ import annotations
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes

import hashlib
import json
import os
import re
_WS = re.compile(r"\s+")   # hoisted: backslash inside an f-string expression is a SyntaxError on Python 3.11 (MCP server venv)
import sqlite3
import threading
import time
from pathlib import Path

ROOT = Path(os.environ.get("BEEZLE_BASE", str(_eroot())))
GRAPH_DIR = ROOT / "obsidian_sync" / "_graph"
OUT_DIR = ROOT / "output" / "graph"
UNRESOLVED_LOG = OUT_DIR / "unresolved_links.jsonl"
CELL_LOG = OUT_DIR / "path_to_law_cells.jsonl"
HASH_INDEX = OUT_DIR / "chunk_hash_index.sqlite"
CHUNK_DB = Path(os.environ.get("TURBOVEC_DIR", str(_eroot() / "corpus"))) / "chunk_text.db"

HUB_KINDS = {"rule", "topic", "term", "statute", "regulation", "case"}
# CASE hubs (corpus_graph_build --cases, 11-10): hubs/cases/<Caption (cite)>.md carry `kind: hub` + `hub_of: case`; the loader keys
# them as kind 'case'. Only hub_of=case is promoted — the bulk `kind: hub` rule/topic family hubs stay out, byte-identical to before.
_HUB_OF_AS_KIND = {"case"}
NODE_KIND_ORDER = ["text", "defines", "interprets", "commentary", "applies"]
EXCERPT_CAP = int(os.environ.get("M2_PTL_CAP", "3"))          # excerpts per cell from the hub path (text first)
MIN_RESOLVED = int(os.environ.get("M2_PTL_MIN", "2"))         # fewer resolved chunks than this → hybrid fallback
FIXED_PROMPT_TOK = 4060                                       # measured 11-09 (m2_cells_D2_mid: min prompt ≈ fixed part)
CHARS_PER_TOK = 3.4

_LOCK = threading.Lock()


# ----------------------------------------------------------------------------------------------- frontmatter (tiny YAML)
_LINK = re.compile(r"\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]")


def unlink(v: str) -> str:
    """'[[Pa.R.C.P. 440|alias]]' -> 'Pa.R.C.P. 440'; plain strings pass through."""
    m = _LINK.fullmatch((v or "").strip())
    return m.group(1).strip() if m else (v or "").strip().strip('"').strip("'")


def _yaml_scalar(s: str):
    s = s.strip()
    if not s:
        return ""
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        if not inner:
            return []
        try:
            return [str(x) for x in json.loads(s)]
        except Exception:
            return [x.strip().strip('"').strip("'") for x in re.split(r",(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)", inner) if x.strip()]
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    return s.strip('"').strip("'")


def parse_frontmatter(text: str) -> dict:
    """Minimal front-matter reader for the generated _graph notes: `key: scalar`, `key: [a, b]`, `key:` + `- item` blocks."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    fm, cur = {}, None
    for line in text[3:end].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith((" ", "\t")) and line.strip().startswith("- ") and cur:
            fm.setdefault(cur, [])
            if isinstance(fm[cur], list):
                fm[cur].append(_yaml_scalar(line.strip()[2:]))
            continue
        if line.strip().startswith("- ") and cur:
            fm.setdefault(cur, [])
            if isinstance(fm[cur], list):
                fm[cur].append(_yaml_scalar(line.strip()[2:]))
            continue
        k, sep, v = line.partition(":")
        if not sep:
            continue
        cur = k.strip()
        fm[cur] = _yaml_scalar(v) if v.strip() else []
    return fm


def _as_list(v) -> list:
    if v is None or v == "":
        return []
    return [str(x) for x in v] if isinstance(v, list) else [str(v)]


# ----------------------------------------------------------------------------------------------- hub index
def norm_key(name: str) -> str:
    """Alias key: lowercase, drop spaces/periods/§/commas, unify dashes, 'Fed. R. Civ. P. 19' -> 'fedrcivp19', '15 U.S.C. 1681s-2' -> '15usc1681s-2'."""
    s = unlink(name).lower().replace("–", "-").replace("—", "-")
    s = re.sub(r"\s*§+\s*", "", s)
    s = re.sub(r"[\s.,'’]", "", s)
    return s


# canonical-form aliases so 'Fed. R. Civ. P. 19', 'F.R.C.P. 19', 'FRCP 19' land on the same key
_ALIAS_RX = (
    (re.compile(r"^fedrcivp(\d.*)$"), r"frcp\1"), (re.compile(r"^frcivp(\d.*)$"), r"frcp\1"),
    (re.compile(r"^fedrappp(\d.*)$"), r"frap\1"), (re.compile(r"^fedrevid(\d.*)$"), r"fre\1"),
    (re.compile(r"^parcivp(\d.*)$"), r"parcp\1"), (re.compile(r"^231pacode(\d.*)$"), r"parcp\1"),
    (re.compile(r"^(\d+)usca(\d.*)$"), r"\1usc\2"), (re.compile(r"^(\d+)pacsa(\d.*)$"), r"\1pacs\2"),
    (re.compile(r"^amjur2d(.*)$"), r"amjur2d\1"), (re.compile(r"^(\d+[a-z]?)amjur2d(.*)$"), r"amjur2d\2"),
)


def alias_keys(name: str) -> list[str]:
    k = norm_key(name)
    out = [k]
    for rx, rep in _ALIAS_RX:
        k2 = rx.sub(rep, k)
        if k2 != k and k2 not in out:
            out.append(k2)
    return out


class HubIndex:
    """hubs: name -> {name, kind, path, family, text_chunks, props}; nodes: [{title, kind, rules, topics, defines, chunk_ids, status, warning}].
    Built from the sqlite/graph.json side index when present, else from the generated _graph/ frontmatter (never the whole vault)."""

    def __init__(self):
        self.hubs: dict[str, dict] = {}
        self.alias: dict[str, str] = {}
        self.nodes: list[dict] = []
        self.source = "none"
        self.loaded_at = 0.0
        self._stamp = None

    # ---- loading
    @staticmethod
    def _candidates() -> list[Path]:
        env = os.environ.get("M2_GRAPH_INDEX", "")
        c = [Path(env)] if env else []
        c += [OUT_DIR / "graph.sqlite", OUT_DIR / "graph.db", OUT_DIR / "graph.json", GRAPH_DIR / "_build" / "graph.json"]
        return c

    def _stamp_now(self):
        """Cheap change signal: mtimes of the index file or the hub/node dirs (no vault walk)."""
        st = []
        for p in self._candidates():
            if p.exists():
                st.append((str(p), int(p.stat().st_mtime)))
                break
        for d in (GRAPH_DIR / "hubs", GRAPH_DIR / "nodes"):
            if d.exists():
                st.append((str(d), int(d.stat().st_mtime)))
                for sub in d.iterdir():
                    if sub.is_dir():
                        st.append((str(sub), int(sub.stat().st_mtime)))
        return tuple(st)

    def load(self, force: bool = False) -> "HubIndex":
        with _LOCK:
            stamp = self._stamp_now()
            if not force and self._stamp == stamp and self.hubs:
                return self
            self.hubs, self.alias, self.nodes = {}, {}, []
            for p in self._candidates():
                if p.exists() and p.stat().st_size > 0:
                    try:
                        if p.suffix in (".sqlite", ".db"):
                            self._load_sqlite(p)
                        else:
                            self._load_json(p)
                        if self.hubs:
                            self.source = str(p)
                            break
                    except Exception as e:                           # a half-built index never blocks retrieval
                        print(f"[path_to_law] index {p} unreadable ({e!r}); falling back", flush=True)
                        self.hubs, self.alias, self.nodes = {}, {}, []
            if not self.hubs:
                self._load_frontmatter()
                self.source = f"frontmatter:{GRAPH_DIR}"
            self._stamp = stamp
            self.loaded_at = time.time()
            return self

    def _ingest(self, path: str, props: dict, name_hint: str = ""):
        kind = str(props.get("kind") or "").strip().lower()
        hub_of = str(props.get("hub_of") or "").strip().lower()
        if kind == "hub" and hub_of in _HUB_OF_AS_KIND:
            kind = hub_of                                                 # 'kind: hub / hub_of: case' → a 'case' hub
        name = unlink(name_hint or props.get("title") or Path(path).stem) if kind in HUB_KINDS else str(props.get("title") or name_hint or Path(path).stem)
        if kind in HUB_KINDS:
            hub_name = Path(path).stem if path else (name_hint or name)   # hub identity = note name (that is what [[links]] point at)
            h = {"name": hub_name, "kind": kind, "path": path, "family": unlink(str(props.get("family") or "")),
                 "text_chunks": _as_list(props.get("text_chunks")) + _as_list(props.get("chunk_ids")), "title": str(props.get("title") or ""),
                 "status": str(props.get("status") or ""), "props": props}
            if kind == "case":                                            # the corpus source key + cite the seat pins by
                h["source"] = str(props.get("source") or ""); h["cite"] = unlink(str(props.get("cite") or ""))
            self.hubs[hub_name] = h
            # every entry of `aliases` (reporter cites, parallel cites, caption) is an alias key, not just the first; case hubs add `cite`
            for a in {hub_name, h["title"], str(props.get("codified_at") or ""), h.get("cite", ""), *(_as_list(props.get("aliases")))}:
                if a:
                    for k in alias_keys(a):
                        self.alias.setdefault(k, hub_name)
        elif kind in NODE_KIND_ORDER or props.get("chunk_ids"):
            self.nodes.append({"title": name, "kind": kind or "interprets", "path": path,
                               "rules": [unlink(x) for x in _as_list(props.get("rules"))],
                               "topics": [unlink(x) for x in _as_list(props.get("topics"))],
                               "defines": [unlink(x) for x in _as_list(props.get("defines"))],
                               "chunk_ids": _as_list(props.get("chunk_ids")), "source": str(props.get("source") or ""),
                               "status": str(props.get("status") or ""), "warning": str(props.get("warning") or "")})

    def _load_json(self, p: Path):
        d = json.loads(p.read_text(encoding="utf-8"))
        nodes = d.get("nodes") if isinstance(d, dict) else d
        if isinstance(nodes, dict):
            nodes = [{"name": k, **(v if isinstance(v, dict) else {})} for k, v in nodes.items()]
        for n in nodes or []:
            if not isinstance(n, dict):
                continue
            props = n.get("frontmatter") or n.get("properties") or n.get("props") or n
            path = str(n.get("path") or n.get("file") or "")
            if path and not path.startswith(str(GRAPH_DIR)) and "_graph" not in path:
                continue                                              # hand-written notes are never hubs for the seat
            self._ingest(path, props, str(n.get("name") or n.get("id") or ""))

    def _load_sqlite(self, p: Path):
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            t = next((x for x in ("nodes", "notes") if x in tables), None)
            if not t:
                return
            cols = [r[1] for r in con.execute(f"PRAGMA table_info({t})")]
            fmcol = next((c for c in ("frontmatter", "fm", "props", "properties", "yaml") if c in cols), None)
            pathcol = next((c for c in ("path", "file", "rel_path") if c in cols), None)
            namecol = next((c for c in ("name", "id", "title") if c in cols), None)
            if not fmcol:
                return
            q = f"SELECT {pathcol or 'NULL'}, {namecol or 'NULL'}, {fmcol} FROM {t}"
            if pathcol:
                q += f" WHERE {pathcol} LIKE '%_graph/%'"
            for path, name, fm in con.execute(q):
                try:
                    props = json.loads(fm) if isinstance(fm, str) else (fm or {})
                except Exception:
                    continue
                self._ingest(str(path or ""), props, str(name or ""))
        finally:
            con.close()

    def _load_frontmatter(self):
        for d in (GRAPH_DIR / "hubs", GRAPH_DIR / "nodes"):
            if not d.exists():
                continue
            for sub in sorted(d.iterdir()):
                files = sorted(sub.glob("*.md")) if sub.is_dir() else ([sub] if sub.suffix == ".md" else [])
                for f in files:
                    if "Name clash" in f.name:
                        continue
                    try:
                        props = parse_frontmatter(f.read_text(encoding="utf-8", errors="replace"))
                    except OSError:
                        continue
                    if props:
                        self._ingest(str(f), props, f.stem)

    # ---- lookup
    def find(self, candidates: list[str]) -> dict | None:
        for c in candidates:
            if c in self.hubs:
                return self.hubs[c]
            for k in alias_keys(c):
                if k in self.alias:
                    return self.hubs[self.alias[k]]
        return None

    def members(self, hub_name: str) -> list[dict]:
        return [n for n in self.nodes if hub_name in n["rules"] or hub_name in n["topics"] or hub_name in n["defines"]]


_INDEX = HubIndex()


def index() -> HubIndex:
    return _INDEX.load()


# ----------------------------------------------------------------------------------------------- reference extraction
_SEC = r"(\d+[a-zA-Z]?(?:\.\d+)*(?:[-–]\d+[a-zA-Z]?)?)"
_REFS = (
    ("rule", re.compile(r"\bPa\.?\s*R\.?\s*(?:C\.?\s*P\.?|Civ\.?\s*P\.?)\s*(?:No\.?\s*)?" + _SEC), lambda m: [f"Pa.R.C.P. {m.group(1)}"]),
    ("rule", re.compile(r"\bPa\.?\s*R\.?\s*A\.?\s*P\.?\s*" + _SEC), lambda m: [f"Pa.R.A.P. {m.group(1)}"]),
    ("rule", re.compile(r"\bPa\.?\s*R\.?\s*E\.?\s*" + _SEC), lambda m: [f"Pa.R.E. {m.group(1)}"]),
    ("rule", re.compile(r"\b(?:Fed\.?\s*R\.?\s*Civ\.?\s*P\.?|F\.?\s*R\.?\s*C\.?\s*P\.?|FRCP)\s*" + _SEC), lambda m: [f"FRCP {m.group(1)}"]),
    ("rule", re.compile(r"\b(?:Fed\.?\s*R\.?\s*App\.?\s*P\.?|FRAP)\s*" + _SEC), lambda m: [f"FRAP {m.group(1)}"]),
    ("rule", re.compile(r"\b(?:Fed\.?\s*R\.?\s*Evid\.?|FRE)\s*" + _SEC), lambda m: [f"FRE {m.group(1)}"]),
    ("statute", re.compile(r"\b(\d{1,2})\s+U\.?\s*S\.?\s*C\.?(?:\s*A\.?)?\s*§{1,2}\s*" + _SEC), lambda m: [f"{m.group(1)} USC {m.group(2)}"]),
    ("regulation", re.compile(r"\b(\d{1,2})\s+C\.?\s*F\.?\s*R\.?\s*(?:§{1,2}|[Pp]art|pt\.)?\s*" + _SEC), lambda m: [f"{m.group(1)} CFR {m.group(2)}"]),
    ("statute", re.compile(r"\b(\d{1,2})\s+Pa\.?\s*C\.?\s*S\.?(?:\s*A\.?)?\s*§{1,2}\s*" + _SEC), lambda m: [f"{m.group(1)} Pa.C.S. {m.group(2)}"]),
    ("statute", re.compile(r"\b(\d{1,2})\s+P\.?\s*S\.?\s*§{1,2}\s*" + _SEC), lambda m: [f"{m.group(1)} P.S. {m.group(2)}"]),
    ("topic", re.compile(r"\b(?:\d+[A-Z]?\s+)?Am\.?\s*Jur\.?\s*2d\s+([A-Z][A-Za-z&',]*(?:\s+(?:and|of|the|&|[A-Z][A-Za-z&',]*)){0,5})"),
     lambda m: [f"Am Jur 2d {re.sub(r'[,]+$', '', m.group(1)).replace(' & ', ' and ').strip()}"]),
)
_BARE_RULE = re.compile(r"\bRule\s+" + _SEC)
_QUOTED_TERM = re.compile(r"[“\"']([a-z][a-z \-]{3,40}?)[”\"']")
# CASE hubs (11-10): a reporter cite ('399 A.2d 360', '484 Pa. 415', '123 F.3d 456') or a caption ('Danson v. Casey') names a case hub
# by one of its `aliases`. Candidates are tried in order; the caption drops leading words ('In Danson v. Casey' → 'Danson v. Casey').
_REPORTER = re.compile(r"\b(\d{1,4})\s+((?:A\.(?:\s?[23]d)?|Pa\.(?:\s?(?:Super|Cmwlth|Commw)\.?)?|Pa\.\s?D\.\s?&\s?C\.(?:\s?[2345]th|\s?[23]d)?|F\.(?:\s?(?:[23]d|4th))?|"
                       r"F\.\s?Supp\.(?:\s?[23]d)?|F\.\s?App'?x\.?|Fed\.\s?App'?x\.?|F\.\s?R\.\s?D\.|B\.\s?R\.|U\.\s?S\.|S\.\s?Ct\.|L\.\s?Ed\.(?:\s?2d)?|WL|"
                       r"N\.\s?J\.(?:\s?Super\.)?|N\.\s?E\.(?:\s?[23]d)?|N\.\s?W\.(?:\s?[23]d)?|S\.\s?E\.(?:\s?[23]d)?|S\.\s?W\.(?:\s?[23]d)?|So\.(?:\s?[23]d)?|P\.(?:\s?[23]d)?|Cal\.(?:\s?[2345]th)?|N\.\s?Y\.(?:\s?[23]d)?))\s+(\d{1,6})\b")
_CAPTION_WORD = r"[A-Z][A-Za-z0-9.'’&\-]*"
_CAPTION_RX = re.compile(r"\b((?:" + _CAPTION_WORD + r"\s+){0,4}" + _CAPTION_WORD + r")\s+v\.\s+((?:" + _CAPTION_WORD + r"\s+){0,4}" + _CAPTION_WORD + r")")
_FORUM_RULE_FAMILY = {"pa": "Pa.R.C.P.", "fed": "FRCP", "nj": "N.J. Ct. R."}


def _strip_sub(sec: str) -> str:
    """'1681s-2(b)' -> '1681s-2' ; '440(a)(1)' -> '440' (hubs are per rule/section; subsections are inside the text chunk)."""
    return re.split(r"\(", sec.replace("–", "-"))[0].rstrip(".")


def cell_text(cell: dict) -> str:
    parts = [str(cell.get(k) or "") for k in ("title", "text", "claim_text", "proposition", "finding", "gap", "fix")]
    for a in cell.get("authorities") or []:
        if isinstance(a, dict):
            parts.append(str(a.get("cite") or ""))
        elif isinstance(a, str):
            parts.append(a)
    return "\n".join(p for p in parts if p)


def extract_refs(text: str, forum: str | None = None) -> list[dict]:
    """Every rule / statute / regulation / treatise title / quoted term the text names → [{kind, raw, candidates:[hub names]}],
    deduped by first candidate, in text order. Bare 'Rule N' resolves by forum (pa → Pa.R.C.P., fed → FRCP); no forum → both tried."""
    out, seen = [], set()
    taken = []                                                      # spans already consumed by a family-qualified rule cite

    def _add(kind, raw, cands):
        cands = [_WS.sub( " ", c).strip() for c in cands if c]
        if not cands or cands[0].lower() in seen:
            return
        seen.add(cands[0].lower()); out.append({"kind": kind, "raw": raw, "candidates": cands})

    for kind, rx, mk in _REFS:
        for m in rx.finditer(text):
            taken.append(m.span())
            cands = mk(m)
            if kind in ("rule", "statute", "regulation"):
                cands = [re.sub(r"\s" + re.escape(c.split(" ")[-1]) + r"$", " " + _strip_sub(c.split(" ")[-1]), c) for c in cands]
            _add(kind, m.group(0), cands)
    for m in _BARE_RULE.finditer(text):
        if any(a <= m.start() < b for a, b in taken):
            continue
        n = _strip_sub(m.group(1))
        fams = [_FORUM_RULE_FAMILY[forum]] if forum in _FORUM_RULE_FAMILY else ["Pa.R.C.P.", "FRCP"]
        _add("rule", m.group(0), [f"{f} {n}" for f in fams])
    for m in _QUOTED_TERM.finditer(text):
        t = m.group(1).strip()
        if len(t.split()) <= 5:
            _add("term", m.group(0), [t])
    for m in _REPORTER.finditer(text):                              # CASE hubs: reporter cite → '399 A.2d 360' (alias of the case hub)
        _add("case", m.group(0), [f"{m.group(1)} {_WS.sub( ' ', m.group(2)).strip()} {m.group(3)}"])
    for m in _CAPTION_RX.finditer(text):                            # CASE hubs: caption → 'Danson v. Casey', then with leading words dropped
        left = _WS.sub( " ", m.group(1)).split(" ")
        cands = [f"{' '.join(left[i:])} v. {_WS.sub( ' ', m.group(2))}" for i in range(len(left))]
        _add("case", m.group(0), cands)
    return out


# ----------------------------------------------------------------------------------------------- resolution
def resolve_detail(cell: dict, forum: str | None = None) -> dict:
    """{hub_ids, chunk_ids, unresolved, refs, chunk_meta{cid: {kind, hub, node, status}}, index_source}."""
    idx = index()
    text = cell_text(cell)
    refs = extract_refs(text, forum)
    hubs, unresolved, meta = [], [], {}
    for r in refs:
        h = idx.find(r["candidates"])
        if h:
            if h["name"] not in hubs:
                hubs.append(h["name"])
            r["hub"] = h["name"]
        else:
            link = r["candidates"][0]
            if r["kind"] in ("term", "case"):
                continue                                            # a quoted phrase with no term hub is not an ingest signal; case hubs
                                                                    # are built on demand (--cases), so a cite with no hub is not one either
            if link not in unresolved:
                unresolved.append(link)
            r["hub"] = None
    ordered: list[str] = []

    def _take(cid, kind, hub, node, status=""):
        if cid and cid not in meta:
            meta[cid] = {"kind": kind, "hub": hub, "node": node, "status": status}
            ordered.append(cid)

    for kind in NODE_KIND_ORDER:
        if kind == "text":                                          # round-robin the hubs' own text so every cited rule gets its verbatim chunk before any hub's 2nd
            lists = [[(cid, hname) for cid in idx.hubs[hname]["text_chunks"]] for hname in hubs]
            for i in range(max((len(l) for l in lists), default=0)):
                for l in lists:
                    if i < len(l):
                        _take(l[i][0], "text", l[i][1], l[i][1], idx.hubs[l[i][1]].get("status", ""))
        for hname in hubs:
            h = idx.hubs[hname]
            for n in idx.members(hname):
                if n["kind"] != kind:
                    continue
                if n["status"] == "unverified" and n["warning"]:
                    continue                                        # mis-keyed / paraphrase stubs: never handed to a bot as law
                for cid in n["chunk_ids"]:
                    _take(cid, kind, hname, n["title"], n["status"])
    return {"hub_ids": hubs, "chunk_ids": ordered, "unresolved": unresolved, "refs": refs, "chunk_meta": meta,
            "index_source": idx.source}


def resolve(cell: dict, forum: str | None = None) -> tuple[list[str], list[str], list[str]]:
    d = resolve_detail(cell, forum)
    return d["hub_ids"], d["chunk_ids"], d["unresolved"]


# ----------------------------------------------------------------------------------------------- the ONE insertion (fold, 11-09)
_HOLDING = re.compile(r"\b(we hold|held:|we conclude|we therefore conclude|it is so ordered|judgment (?:is )?(?:affirmed|reversed|vacated)|"
                      r"(?:affirmed|reversed|vacated)(?: in part)?\.|order (?:is )?(?:affirmed|reversed|vacated)|we (?:affirm|reverse|vacate))", re.I)
_CAPTION = re.compile(r"\b(argued|decided|submitted|appeal from|before:|opinion by|syllabus|counsel for|for appellant|for appellee|"
                      r"petition for allowance|docket no|no\.\s*\d+\s+[a-z]+\s+20\d\d)\b", re.I)


def hub_rows_for_cite(con, cite: str, forum: str | None = None, cap: int = 12) -> list[tuple]:
    """Hub-first PIN lookup for ONE cite — the single place the corpus graph enters retrieval (user lock 11-09: one cite→chunk path).
    Consulted first by analysis_runner._exact_statute / _exact_rule_or_reporter (hence by pull_cites AND fetch_bot.step_pins) when
    M2_PATH_TO_LAW=1; returns [] → the existing pins logic runs unchanged. Rows are the pins shape (chunk_id, text, source), in
    NODE_KIND_ORDER (the unit hub's own text_chunks first); case sources get holding_rank() so a holding chunk outranks the caption."""
    try:
        det = resolve_detail({"text": cite}, forum=forum)
    except Exception:
        return []
    try:                                                              # LOOKUP 11-09 (retrieval-100): the unit's own text / the case's
        from pipeline.m2 import struct_pins as _SP                    # holding chunks by the ingest-time role key (struct_index.db);
        ids = _SP.chunk_ids_for(cite, det.get("refs") or [], cap=min(cap, 6))   # [] (reason in _SP.last_reason) → the hub path below, unchanged
    except Exception:
        ids = []
    if ids:
        rows = [(r["db_id"], r["text"], r["source"]) for r in fetch_chunks(ids)]
        a = getattr(_SP, "last_anchor", None)                         # unit begins mid-chunk (Rule 1028 at char ~1,400 of ord 12):
        if rows and a and (m := a.search(rows[0][1] or "")) and m.start() > 0:   # the bot's 1,200-char window must start at the heading
            rows[0] = (rows[0][0], rows[0][1][m.start():], rows[0][2])
        return rows
    if not det["chunk_ids"]:
        return []
    rows = [(r["db_id"], r["text"], r["source"]) for r in fetch_chunks(det["chunk_ids"][:cap])]
    return holding_rank(rows)


def holding_rank(rows: list[tuple]) -> list[tuple]:
    """Stable re-order for CASE sources only (reporter / docvault / usreports keys): chunks carrying a holding marker first, chunks that
    are only caption / procedural history last. Statute and rule sources pass through untouched. Never fabricates — order only."""
    def is_case(src):
        s = (src or "").lower()
        return any(k in s for k in ("usreports", "docvault_pull_", "_f3d_", "_f2d_", "_a2d_", "_a3d_", "courtlistener_opinion", "scotus_"))
    def key(r):
        t = r[1] or ""
        if not is_case(r[2]): return 1
        if _HOLDING.search(t): return 0
        return 2 if _CAPTION.search(t[:600]) and not _HOLDING.search(t) else 1
    return sorted(rows, key=key)


def gap_taxonomy(claim_text: str, pinned: list[dict], status: str, forum: str | None = None) -> str:
    """Deterministic per-cell GAP class (both A/B arms): no_cite_in_claim | not_in_corpus | wrong_source_pin | holding_missing | other;
    '' when the cell is not a GAP. wrong_source_pin = a pinned chunk's source is outside the cite's own registry unit sources (the
    73 P.S. § 201-2 → FCEUA-text class); holding_missing = every pinned chunk of a case cite is caption/procedural text."""
    if status not in ("GAP", "NOT_IN_CORPUS"):
        return ""
    refs = [r for r in extract_refs(claim_text, forum) if r["kind"] != "term"]
    try:
        from pipeline.m2 import analysis_runner as _AR
        cites = _AR.cites_in(claim_text)
    except Exception:
        cites = []
    if not refs and not cites:
        return "no_cite_in_claim"
    pins = [p for p in pinned if str(p.get("source", "")).startswith("cite:")]
    if not pins:
        return "not_in_corpus"
    idx = index()
    expected = set()
    for r in refs:
        h = idx.find(r["candidates"])
        if h:
            expected |= set(_as_list(h["props"].get("sources"))) | {m.get("source") for m in idx.members(h["name"]) if m.get("source")}
    srcs = {_pin_source(p) for p in pins}
    if expected and srcs and not (srcs & expected) and all(s for s in srcs):
        return "wrong_source_pin"
    case_pins = [p for p in pins if re.search(r"\bv\.\s", p["source"])]
    if case_pins and all(_CAPTION.search((p.get("text") or "")[:600]) and not _HOLDING.search(p.get("text") or "") for p in case_pins):
        return "holding_missing"
    return "other"


def _pin_source(p: dict) -> str:
    cid = re.search(r"chunk=([0-9a-f]+)", str(p.get("source", "")))
    if not cid:
        return ""
    row = _chunk_con().execute("SELECT source FROM chunk_text WHERE chunk_id=?", (cid.group(1),)).fetchone()
    return (row[0] if row else "") or ""


# ----------------------------------------------------------------------------------------------- chunk fetch (get-by-id)
_CON: dict = {"chunk": None, "hash": None}


def _chunk_con():
    if _CON["chunk"] is None:
        _CON["chunk"] = sqlite3.connect(f"file:{CHUNK_DB}?mode=ro", uri=True, check_same_thread=False, timeout=300)
    return _CON["chunk"]


def _hash_con():
    if _CON["hash"] is None:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(HASH_INDEX), check_same_thread=False, timeout=300)
        c.execute("CREATE TABLE IF NOT EXISTS hash_map(hid TEXT PRIMARY KEY, chunk_id TEXT, source TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS source_done(source TEXT PRIMARY KEY, n INTEGER, at REAL)")
        c.commit()
        _CON["hash"] = c
    return _CON["hash"]


def _index_source_hashes(source: str) -> int:
    """Map every chunk of one source to its search_chunks hash id ('source#sha1(text)[:10]') — once; idempotent."""
    hc, cc = _hash_con(), _chunk_con()
    if hc.execute("SELECT 1 FROM source_done WHERE source=?", (source,)).fetchone():
        return 0
    rows = cc.execute("SELECT chunk_id, text FROM chunk_text WHERE source=?", (source,)).fetchall()
    # search_chunks hashes the 650-char window case_comprehension._search_one returns (text[:650]), not the full chunk;
    # the FTS/authority leg may hand back the whole text — both forms are indexed so either id resolves.
    pairs = []
    for cid, t in rows:
        t = t or ""
        pairs.append((f"{source}#{hashlib.sha1(t[:650].encode()).hexdigest()[:10]}", cid, source))
        if len(t) > 650:
            pairs.append((f"{source}#{hashlib.sha1(t.encode()).hexdigest()[:10]}", cid, source))
    with _LOCK:
        hc.executemany("INSERT OR IGNORE INTO hash_map VALUES(?,?,?)", pairs)
        hc.execute("INSERT OR REPLACE INTO source_done VALUES(?,?,?)", (source, len(rows), time.time()))
        hc.commit()
    return len(rows)


def fetch_chunks(chunk_ids: list[str]) -> list[dict]:
    """[{chunk_id (as given), db_id, source, text}] in the given order; ids not found are dropped (caller counts them)."""
    out = []
    cc = _chunk_con()
    for cid in chunk_ids:
        row = None
        if "#" in cid:
            src = cid.rsplit("#", 1)[0]
            hc = _hash_con()
            r = hc.execute("SELECT chunk_id FROM hash_map WHERE hid=?", (cid,)).fetchone()
            if r is None:
                _index_source_hashes(src)
                r = hc.execute("SELECT chunk_id FROM hash_map WHERE hid=?", (cid,)).fetchone()
            if r:
                row = cc.execute("SELECT chunk_id, text, source FROM chunk_text WHERE chunk_id=?", (r[0],)).fetchone()
        else:
            row = cc.execute("SELECT chunk_id, text, source FROM chunk_text WHERE chunk_id=?", (cid,)).fetchone()
        if row and row[1]:
            out.append({"chunk_id": cid, "db_id": row[0], "source": row[2] or "?", "text": row[1]})
    return out


# ----------------------------------------------------------------------------------------------- logging / ingest signal
_SEEN_UNRESOLVED: set = set()


def append_unresolved(links: list[str], ref: str = "", forum: str | None = None) -> int:
    """output/graph/unresolved_links.jsonl — the ingest queue (skill: unresolved [[link]] = NOT-IN-CORPUS marker). One line per
    (link, ref) per process; `ref` names the cell (bot/claim) that needed it."""
    n = 0
    if not links:
        return 0
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with _LOCK, UNRESOLVED_LOG.open("a", encoding="utf-8") as f:
        for l in links:
            key = (l, ref)
            if key in _SEEN_UNRESOLVED:
                continue
            _SEEN_UNRESOLVED.add(key)
            f.write(json.dumps({"link": f"[[{l}]]", "hub": l, "ref": ref, "forum": forum, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                "consumer": "m2.path_to_law"}, ensure_ascii=False) + "\n")
            n += 1
    return n


def est_prompt_tokens(hits: list[dict]) -> int:
    return int(FIXED_PROMPT_TOK + sum(len(h.get("text") or "") for h in hits) / CHARS_PER_TOK)


def log_cell(bot: str, claim, mode: str, hits: list[dict], hubs: list[str], unresolved: list[str], extra: dict | None = None) -> dict:
    row = {"bot": bot, "claim": claim, "mode": mode, "excerpts": len(hits), "hub_excerpts": sum(1 for h in hits if h.get("mode") == "hub"),
           "prompt_tok_est": est_prompt_tokens(hits), "hubs": hubs, "unresolved": unresolved, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    if extra:
        row.update(extra)
    try:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        with _LOCK, CELL_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass
    print(f"[path_to_law] {bot:<18} claim {claim!s:>3} mode={mode:<6} ex={len(hits)} hub_ex={row['hub_excerpts']} tok≈{row['prompt_tok_est']} "
          f"hubs={hubs} unresolved={unresolved}", flush=True)
    return row


# ----------------------------------------------------------------------------------------------- CLI: hit-rate over a saved cell set
def hit_rate(cells_jsonl: str, forum: str | None = None, min_resolved: int = MIN_RESOLVED, verify_db: bool = True) -> dict:
    rows = [json.loads(l) for l in Path(cells_jsonl).read_text(encoding="utf-8").splitlines() if l.strip()]
    res, n_ok, n_hub_any, unresolved_all, hubs_all = [], 0, 0, {}, {}
    for r in rows:
        d = resolve_detail(r, forum)
        cids = d["chunk_ids"]
        n_found = len(fetch_chunks(cids[:EXCERPT_CAP * 2])) if (verify_db and cids) else len(cids)
        ok = n_found >= min_resolved
        n_ok += ok; n_hub_any += bool(d["hub_ids"])
        for u in d["unresolved"]:
            unresolved_all[u] = unresolved_all.get(u, 0) + 1
        for h in d["hub_ids"]:
            hubs_all[h] = hubs_all.get(h, 0) + 1
        res.append({"bot": r.get("bot"), "claim": r.get("claim"), "hubs": d["hub_ids"], "n_chunks": len(cids), "n_found": n_found,
                    "mode": "hub" if ok else ("mixed" if n_found else "hybrid"), "unresolved": d["unresolved"]})
    return {"cells": len(rows), "resolve_ge_min": n_ok, "any_hub": n_hub_any, "min_resolved": min_resolved, "index_source": index().source,
            "hubs_hit": hubs_all, "unresolved_top": sorted(unresolved_all.items(), key=lambda x: -x[1])[:25], "per_cell": res}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="path_to_law resolver — hit-rate over a saved m2 cell set (no model, no daemon)")
    ap.add_argument("cells", help="output/research/m2_cells_*.jsonl")
    ap.add_argument("--forum", default=None); ap.add_argument("--no-db", action="store_true", help="count chunk ids without verifying them in chunk_text.db")
    ap.add_argument("--json", default="", help="write the full report here")
    a = ap.parse_args()
    rep = hit_rate(a.cells, a.forum, verify_db=not a.no_db)
    print(json.dumps({k: v for k, v in rep.items() if k != "per_cell"}, indent=1, ensure_ascii=False))
    if a.json:
        Path(a.json).write_text(json.dumps(rep, indent=1, ensure_ascii=False), encoding="utf-8"); print(f"→ {a.json}")
