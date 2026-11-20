#!/usr/bin/env python3
"""Deterministic BULK builder for the Beezle corpus graph (PLAN_obsidian_corpus_graph §7/§9-14, item G2'/G6').

Reads  : ~/turbovec/cite_registry.db  rule_registry(family,title,source)   (66,592 rows / 170 (family,title) hubs)
         ~/turbovec/chunk_text.db     chunk census (GROUP BY source, indexed) + bounded text reads
         pipeline/m2/FAMILIES.yaml               family key for a source slug (longest-prefix; '=' exact)
Writes : ext4 mirror FIRST  -> ~/beezle_graph_mirror/_graph/{hubs,nodes,_build}   (env GRAPH_MIRROR overrides)
         then `rsync -a` of hubs/ + nodes/ ONLY into obsidian_sync/_graph/ (9p: never per-file writes in a loop, never walk the vault)
         side index: _build/graph.db (sqlite) + _build/graph.json + obsidian_sync/_index/wikilink_index_<scope-hash('_graph')>.json
Rules  : zero LLM — every property is a regex/lookup over registry + census + bounded chunk text. Hand-written notes are never
         touched (only _graph/hubs + _graph/nodes are written). Idempotent: content-compare before write, `created` preserved from
         the existing file, so a second run writes 0 files. An unresolved [[link]] is the NOT-IN-CORPUS / not-in-registry marker.
Verify : python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" pipeline/corpus_graph_build.py   (py_compile crashes on WSL2)
Run    : env -u PYTHONPATH /usr/bin/python3 -m pipeline.corpus_graph_build --build [--no-nodes] [--mirror-to-vault]
         graph_emit(['law_constitution'])   # G6' hook: regenerate hubs/nodes for just those slugs (same code path, scoped)
         --cases slug[,slug…]               # explicit case hubs (hubs/cases/) — cases are NEVER built in bulk (hundreds of thousands)
Struct : ~/turbovec/struct_index.db chunk_struct(chunk_id, source, family, title, unit, units, role, heading, ord, ord_mode, score)
         (env STRUCT_DB overrides; read-only). Used for (A) case hubs — a SCOPED source whose struct family is `case_*` gets
         hubs/cases/<Caption (cite)>.md (caption/cite/court/year from cite_registry.cite_registry, else the caption chunk text;
         chunk ids grouped by role; `applies` edges to rule hubs via cites_in) — and (B) the `## Sections` list on Am Jur topic
         hubs (`§ n — heading (k chunks) → first text chunk id`, toc rows excluded, capped at TOPIC_SECTION_LINES).
"""
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import json, re, os, sys, time, sqlite3, hashlib, subprocess, argparse
from pathlib import Path

ROOT = _eroot()
VAULT = ROOT / "obsidian_sync"
VAULT_G = VAULT / "_graph"
VAULT_INDEX = VAULT / "_index"
MIRROR = Path(os.environ.get("GRAPH_MIRROR", str(Path.home() / "beezle_graph_mirror/_graph")))
TV = (_eroot() / "corpus")
REG_DB = TV / "cite_registry.db"
CHUNK_DB = TV / "chunk_text.db"
STRUCT_DB = Path(os.environ.get("STRUCT_DB", str(TV / "struct_index.db")))
FAMILIES = ROOT / "pipeline" / "m2" / "FAMILIES.yaml"
TODAY = time.strftime("%Y-%m-%d")

HUB_SCAN_CHUNKS = 300      # bounded text scan per hub for cross_ref / operative counts
NODE_CHUNK_IDS = 12        # chunk ids kept per pointer stub (count is always full)
NODE_TEXT_CHARS = 1500     # first-chunk text read per node (heading + operative regex)
TOPIC_SECTION_LINES = 400  # (B) cap on the `## Sections` list of a topic hub; the rest stays in chunk_struct
CASE_HUB_CHUNK_IDS = 40    # (A) chunk ids listed inline on a case hub; the full set is chunk_struct WHERE source=…
CASE_ROLE_ORDER = ["caption", "syllabus", "procedural", "holding", "text", "opinion", "concurrence", "dissent", "other"]
# reporter preference when a source carries several cites and none matches the slug tail (official reporter first)
REPORTER_PREF = ["U.S.", "Pa.", "N.J.", "F.4th", "F.3d", "F.2d", "F.", "F. Supp. 3d", "F. Supp. 2d", "F. Supp.", "A.3d", "A.2d", "A.", "S. Ct.", "L. Ed. 2d", "L. Ed."]
COURT_JURIS = {"SCOTUS": "US", "CA": "US", "OTHER_CIRCUIT": "US", "OTHER_DISTRICT": "US", "PA": "PA", "NJ": "NJ"}

# ----------------------------------------------------------------------------- registry family -> hub naming / axes
# (family in rule_registry) -> (hub-name template, jurisdiction, authority_level, FAMILIES fallback key)
FAM_SPEC = {
    "U.S.C.":   ("{t} USC",        "US", "statute",      "usc_"),
    "C.F.R.":   ("{t} CFR",        "US", "regulation",   "cfr_"),
    "Fed. Reg.": ("{t} Fed. Reg.", "US", "regulation",   "fed_reg"),
    "Pa. Code": ("{t} Pa. Code",   "PA", "regulation",   "pa_code_"),
    "Pa. Bull.": ("{t} Pa. Bull.", "PA", "regulation",   "pa_bulletin"),
    "P.S.":     ("{t} P.S.",       "PA", "statute",      "pa_{t}ps_"),
    "Pa.R.C.P.": ("Pa.R.C.P. {t}", "PA", "rule",         "pa_rcp"),
}
# constitutions: not in rule_registry (no RULE_PATS entry) — fixed source map (both-case twins are in the census)
CONST_HUBS = {
    "US Const":   {"sources": ["law_constitution", "LAW_Constitution"], "jurisdiction": "US", "family": "us_const",
                   "title": "Constitution of the United States", "node_dir": "us_const"},
    "Pa. Const.": {"sources": ["the_constitution_of_pennsylvania", "The_Constitution_of_Pennsylvania"], "jurisdiction": "PA",
                   "family": "pa_const", "title": "Constitution of the Commonwealth of Pennsylvania", "node_dir": "pa_const"},
}
PAB_SECTION = {"not": "Notices", "rr": "Rules and Regulations", "prm": "Proposed Rulemaking", "cts": "The Courts",
               "sop": "Statements of Policy", "gv": "The Governor", "ga": "The General Assembly", "dgs": "DGS (State Contracts)"}
# PAB_SECTION values are confirmed from the chunk text's own running heads (`# RULES AND REGULATIONS`, `# STATEMENTS OF POLICY`)
# where present; a slice whose first chunk carries a different head gets THAT head instead (see node_title).

# ----------------------------------------------------------------------------- cite regex (hub-level, title granularity)
CITE_RX = [
    (re.compile(r"\b(\d{1,2})\s*U\.?\s?S\.?\s?C\.?(?:A\.)?\s*(?:§|[Ss]ec)"), "{n} USC"),
    (re.compile(r"\b[Ss]ections?\s+[\w–\-]+(?:\([a-zA-Z0-9]+\))*(?:\s*(?:,|and|through|to)\s*[\w–\-]+(?:\([a-zA-Z0-9]+\))*)*\s+of\s+[Tt]itle\s+(\d{1,2})\b"), "{n} USC"),   # U.S.C. internal form
    (re.compile(r"\b(\d{1,2})\s*C\.?\s?F\.?\s?R\.?\s*(?:§|[Pp]art|\d)"), "{n} CFR"),
    (re.compile(r"\b(\d{1,3})\s*Pa\.?\s?Code"), "{n} Pa. Code"),
    (re.compile(r"\b(\d{1,2})\s*Pa\.?\s?B(?:ull)?\.\s*\d"), "{n} Pa. Bull."),
    (re.compile(r"\b(\d{1,2})\s*Pa\.?\s?C\.?\s?S\.?(?:A\.)?\s*§"), "{n} Pa.C.S."),
    (re.compile(r"\b(\d{1,2})\s*P\.\s?S\.\s*§"), "{n} P.S."),
    (re.compile(r"\b(\d{1,3})\s*(?:Fed\.\s?Reg\.|FR)\s+\d"), "{n} Fed. Reg."),
    (re.compile(r"Pa\.\s?R\.\s?C(?:iv)?\.\s?P\.\s?(?:No\.\s?)?(\d{2,4}(?:\.\d+)?)"), "Pa.R.C.P. {n}"),
    (re.compile(r"Fed\.\s?R\.\s?Civ\.\s?P\.\s?(\d{1,2})\b"), "FRCP {n}"),
    (re.compile(r"\b(U)\.?\s?S\.?\s?Const\."), "US Const"),
    (re.compile(r"\b(Pa)\.\s?Const\."), "Pa. Const."),
]
RE_SHALL = re.compile(r"\bshall\b", re.I)
RE_MUST = re.compile(r"\bmust\b", re.I)
RE_VOID = re.compile(r"\bvoid\b", re.I)
RE_MAY = re.compile(r"\bmay\b", re.I)
RE_DEADLINE = re.compile(r"\b(within|not later than)\s+(\w+(?:\s*\(\d+\))?)\s+days\b", re.I)
RE_USC_HEAD = re.compile(r"^\s*§\s*([\w–.\-]+)\.\s+([^.\n]{3,140})")
RE_PACODE_HEAD = re.compile(r"TITLE\s+(\d+)\s+([A-Z][A-Z ,&'\-]{3,80}?)\s+(?:PART|CHAPTER|Chapter|Subpart)")
RE_RUNHEAD = re.compile(r"^#\s*([A-Z][A-Z &]{4,40})\b")
RE_AMJUR_SEC = re.compile(r"§\s*(\d+[A-Z]?)\.\s+([^.\n]{3,120})\.")
RE_US_AMEND = re.compile(r"\bAmendment\s+([IVXL]+)\.")
RE_US_ART = re.compile(r"\bArticle\.?\s+([IVX]+)\.")
RE_PA_ART = re.compile(r"\bARTICLE\s+([IVX]+)\.?\s+([A-Z][A-Z ,]{3,60}?)(?:\s+(?:Sec\.|§|A\.)|$)")


def operative(text):
    """node level (one chunk): sample-build convention, strongest verb wins (void > shall > must > may)."""
    if RE_VOID.search(text): return "void"
    if RE_SHALL.search(text): return "shall"
    if RE_MUST.search(text): return "must"
    if RE_MAY.search(text): return "may"
    return ""

def operative_counts(text):
    """hub level (hundreds of chunks): the DOMINANT verb, with counts — a title with 638 `shall` and 2 `void` is a `shall` hub."""
    c = {"shall": len(RE_SHALL.findall(text)), "must": len(RE_MUST.findall(text)), "void": len(RE_VOID.findall(text)), "may": len(RE_MAY.findall(text))}
    order = ["shall", "must", "void", "may"]
    top = max(order, key=lambda k: (c[k], -order.index(k)))
    return (top if c[top] else ""), c


def cites_in(text, self_name):
    out = {}
    for rx, tpl in CITE_RX:
        for m in rx.finditer(text):
            name = tpl.format(n=m.group(1)) if "{n}" in tpl else tpl
            if name != self_name:
                out[name] = out.get(name, 0) + 1
    return out


# ----------------------------------------------------------------------------- yaml helpers (hand-rolled, deterministic)
def yq(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'

def links(vals):
    return "[" + ", ".join(yq(f"[[{v}]]") for v in vals) + "]"

def strs(vals):
    return "[" + ", ".join(yq(v) for v in vals) + "]"

SAFE = re.compile(r'[\\/:*?"<>|#^\[\]]')
def safe_name(s):
    return SAFE.sub("~", s).strip().rstrip(".")[:150]


# ----------------------------------------------------------------------------- families (same semantics as analysis_runner._fam_match)
_FAMS = None
def load_families():
    global _FAMS
    if _FAMS is None:
        try:
            import yaml
            d = yaml.safe_load(FAMILIES.read_text(encoding="utf-8")) or {}
        except Exception:
            d = {}
        _FAMS = {k: v for k, v in d.items() if k != "_forum" and isinstance(v, list)}
    return _FAMS

def family_for(source, fallback):
    s = source.lower(); best, best_len = None, -1
    for key, prefixes in load_families().items():
        for p in prefixes:
            p = str(p)
            if p.startswith("="):
                if s == p[1:].lower() and len(p) > best_len: best, best_len = key, len(p)
            elif s.startswith(p.lower()) and len(p) > best_len:
                best, best_len = key, len(p)
    return best or fallback


# ----------------------------------------------------------------------------- data access
def census():
    con = sqlite3.connect(f"file:{CHUNK_DB}?mode=ro", uri=True); con.execute("PRAGMA busy_timeout=120000")
    rows = con.execute("SELECT source, count(*) FROM chunk_text WHERE source IS NOT NULL AND source != '' GROUP BY source").fetchall()
    con.close()
    return dict(rows)

def registry_rows():
    r = sqlite3.connect(f"file:{REG_DB}?mode=ro", uri=True)
    rows = r.execute("SELECT family, title, source FROM rule_registry ORDER BY family, title, source").fetchall()
    r.close()
    return rows

def chunk_con():
    con = sqlite3.connect(f"file:{CHUNK_DB}?mode=ro", uri=True, timeout=300); con.execute("PRAGMA busy_timeout=120000")
    return con

def node_rows(con, source):
    """first NODE_CHUNK_IDS chunk ids (deterministic order) + first-chunk text head."""
    rows = con.execute("SELECT chunk_id, substr(text, 1, ?) FROM chunk_text WHERE source=? ORDER BY chunk_id LIMIT ?",
                       (NODE_TEXT_CHARS, source, NODE_CHUNK_IDS)).fetchall()
    return [r[0] for r in rows], (rows[0][1] or "") if rows else ""

def hub_scan_text(con, sources, budget=HUB_SCAN_CHUNKS):
    """bounded: spread `budget` chunks over the hub's sources (sorted), >=1 per source until the budget is spent."""
    srcs = sorted(sources)
    if not srcs: return "", 0
    per = max(1, budget // len(srcs)); parts = []; n = 0
    for s in srcs:
        if n >= budget: break
        rows = con.execute("SELECT substr(text, 1, 2500) FROM chunk_text WHERE source=? ORDER BY chunk_id LIMIT ?", (s, per)).fetchall()
        parts += [r[0] or "" for r in rows]; n += len(rows)
    return "\n".join(parts), n


# ----------------------------------------------------------------------------- struct index (chunk_struct) — read-only
def struct_con():
    """read-only handle on struct_index.db; None when the db is absent (every struct feature then degrades to 'not indexed')."""
    if not STRUCT_DB.exists(): return None
    con = sqlite3.connect(f"file:{STRUCT_DB}?mode=ro", uri=True, timeout=120); con.execute("PRAGMA busy_timeout=120000")
    return con

def struct_rows(scon, source):
    """[(chunk_id, family, title, unit, role, heading, ord)] for one source, in struct order (ix_cs_source)."""
    if scon is None: return []
    return scon.execute("SELECT chunk_id, family, title, unit, role, heading, ord FROM chunk_struct WHERE source=? ORDER BY ord, chunk_id",
                        (source,)).fetchall()

def struct_family(scon, source):
    if scon is None: return ""
    r = scon.execute("SELECT family FROM chunk_struct WHERE source=? LIMIT 1", (source,)).fetchone()
    return r[0] if r else ""

def is_case_family(fam):
    return bool(fam) and fam.startswith("case_")

def case_cites(source):
    """cite_registry.cite_registry rows for a source: [(reporter, vol, page, caption, court_code, year)]."""
    r = sqlite3.connect(f"file:{REG_DB}?mode=ro", uri=True)
    try:
        rows = r.execute("SELECT reporter, vol, page, caption, court_code, year FROM cite_registry WHERE source=? ORDER BY reporter, vol, page", (source,)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    r.close()
    return rows

RE_CAPS_RUN = re.compile(r"\b((?:[A-Z][A-Z'’&.\-]{2,}|OF|THE|AND|&)(?:\s+(?:[A-Z][A-Z'’&.\-]{2,}|OF|THE|AND|&))*)\b")
RE_CITE_HEAD = re.compile(r"^\s*(\d{1,4})\s+([A-Z][A-Za-z.\s]{0,14}?\d?[a-z]{0,2}\.?)\s+(\d{1,4})\s*$", re.M)
RE_DECIDED = re.compile(r"\b(?:Decided|Filed|Argued|Submitted|Opinion filed)\b[^\n]{0,60}?\b(1[6-9]\d\d|20\d\d)\b")
RE_COURT_LINE = re.compile(r"^(Supreme Court of [A-Z][\w ]+|Superior Court of [A-Z][\w ]+|Commonwealth Court of [A-Z][\w ]+|United States (?:Supreme Court|Court of Appeals[^\n]{0,40}|District Court[^\n]{0,60}))\.?\s*$", re.M)

def _side_short(side):
    """one caption side -> short party name: first ALL-CAPS run (reporter style 'William DANSON ... Appellants' -> 'Danson'),
    else the side with et al./role words stripped."""
    side = re.sub(r"\bet\s+al\.?", "", side).strip(" ,;")
    m = RE_CAPS_RUN.search(side)
    if m and re.search(r"[A-Z]{3,}", m.group(1)):
        words = m.group(1).split()
        return " ".join(w.lower() if w in ("OF", "THE", "AND") else w.title() for w in words)
    return re.split(r",", side, 1)[0].strip()[:60]

def short_caption(caption):
    if not caption: return ""
    parts = re.split(r"\s+v\.?\s+", caption, 1, flags=re.I)
    if len(parts) == 2:
        return f"{_side_short(parts[0])} v. {_side_short(parts[1])}"
    return _side_short(caption)

def slug_caption(source):
    """fallback when no registry row: scotus_danson_v_casey_399a2d360 -> 'Danson v. Casey'; cap_nj_100_108_in_re_thomson -> 'In Re Thomson'."""
    s = re.sub(r"^(?:scotus|cap_[a-z0-9]+|cap|nj|pa|ca3|cl|courtlistener)_", "", source)
    s = re.sub(r"_\d+[a-z0-9]*\d$", "", s)          # trailing cite slug 399a2d360
    s = re.sub(r"^\d+_\d+_", "", s)                 # cap vol_page prefix
    return re.sub(r"\b[Vv]\b\.?", "v.", s.replace("_", " ")).title().replace(" V. ", " v. ").replace("'S", "'s")

def case_meta(source, cites, head):
    """caption / primary cite / aliases / court / jurisdiction / year, with provenance per field (registry | text | slug | NOT IN DATA)."""
    prov = {}
    tail = re.sub(r"[^a-z0-9]", "", source.rsplit("_", 1)[-1].lower())
    forms = [(f"{v} {rep} {p}", rep, cap, cc, yr) for rep, v, p, cap, cc, yr in cites]
    primary = None
    for f in forms:
        if re.sub(r"[^a-z0-9]", "", f[0].lower()) == tail: primary = f; break
    if primary is None and forms:
        primary = sorted(forms, key=lambda f: (REPORTER_PREF.index(f[1]) if f[1] in REPORTER_PREF else 99, f[0]))[0]
    caption = short_caption(primary[2]) if primary and primary[2] else ""
    if caption: prov["caption"] = "registry"
    cite = primary[0] if primary else ""
    if cite: prov["cite"] = "registry"
    court_code = (primary[3] if primary else "") or ""
    year = (primary[4] if primary else "") or ""
    if year: prov["year"] = "registry"
    # text fallbacks from the caption chunk (first chunk head)
    if not cite:
        m = RE_CITE_HEAD.search(head or "")
        if m: cite = f"{m.group(1)} {m.group(2).strip()} {m.group(3)}"; prov["cite"] = "text"
    if not caption:
        line = next((l for l in (head or "").splitlines() if re.search(r"\sv\.?\s", l) and len(l) < 600), "")
        if line: caption = short_caption(line); prov["caption"] = "text"
    if not caption:
        caption = slug_caption(source); prov["caption"] = "slug"
    if not year:
        m = RE_DECIDED.search(head or "")
        if m: year = m.group(1); prov["year"] = "text"
    mc = RE_COURT_LINE.search(head or "")
    court = mc.group(1).strip() if mc else ""
    if court: prov["court"] = "text"
    elif court_code: court = court_code; prov["court"] = "registry"
    juris = ""
    for k, v in COURT_JURIS.items():
        if court_code.startswith(k): juris = v; break
    if not juris and court:
        juris = "PA" if "Pennsylvania" in court else "NJ" if "New Jersey" in court else "US" if "United States" in court else ""
    aliases = sorted({f[0] for f in forms} | ({cite} if cite else set()))
    return {"caption": caption, "cite": cite, "aliases": aliases, "court": court, "court_code": court_code, "jurisdiction": juris,
            "year": year, "prov": prov}


# ----------------------------------------------------------------------------- naming (slug -> alias / human title)
def alias_for(fam, title, source):
    s = source
    # ingest twins (upper-case re-keys, `_N` duplicate loads) share the canonical key's text: alias only the canonical lower-case key
    if fam in ("C.F.R.", "Fed. Reg.", "Pa. Code") and (s != s.lower() or re.search(r"_\d+$", s)):
        return ""
    if fam == "U.S.C.":
        m = re.match(r"^usc_t(\d+)_s(.+)$", s)
        if m: return f"{m.group(1)} USC {m.group(2).replace('–', '-')}"
    elif fam == "C.F.R.":
        m = re.match(r"(?i)^(?:courtlistener_)?cfr_(\d+)_part_(\w+)$", s)
        if m: return f"{m.group(1)} CFR part {m.group(2)}"
        m = re.match(r"(?i)^cfr-(\d{4})-title(\d+)-vol(\d+)", s)
        if m: return f"{m.group(2)} CFR ({m.group(1)} ed. vol. {m.group(3)})"
        m = re.match(r"(?i)^ecfr-title(\d+)", s)
        if m: return f"{m.group(1)} CFR (eCFR)"
    elif fam == "Pa. Code":
        m = re.match(r"^(\d{3})_(\d{4})([a-z]?)(?:_\d+)?$", s)
        if m: return f"{int(m.group(1))} Pa. Code ch. {int(m.group(2))}{m.group(3)}"
        m = re.match(r"^pa_code_t(\d+)_ch(\w+)$", s)
        if m: return f"{m.group(1)} Pa. Code ch. {m.group(2)}"
    elif fam == "Pa. Bull.":
        m = re.match(r"^(\d{1,2})-(\d{1,4})(?:_p(\d+))?$", s)
        if m: return f"{int(m.group(1))} Pa.B. No. {int(m.group(2))}" + (f" pt. {m.group(3)}" if m.group(3) else "")
        m = re.match(r"^(\d{1,2})_(\d{1,4})_([a-z]+)(\d*)$", s)
        if m:
            sec = PAB_SECTION.get(m.group(3), m.group(3))
            return f"{int(m.group(1))} Pa.B. No. {int(m.group(2))} ({sec}{(' ' + m.group(4)) if m.group(4) else ''})"
    elif fam == "Fed. Reg.":
        m = re.match(r"(?i)^fr-(\d{4}-\d\d-\d\d)", s)
        if m: return f"{title} FR ({m.group(1)})"
    elif fam == "Pa.R.C.P.":
        return f"Pa.R.C.P. {title}"
    return ""

def node_title(fam, title, source, head):
    if fam == "U.S.C.":
        m = RE_USC_HEAD.match(head)
        if m: return f"§ {m.group(1)}. {m.group(2).strip()}"[:160]
    if fam == "Pa. Bull.":
        m = RE_RUNHEAD.match(head)
        if m:
            a = alias_for(fam, title, source)
            return (a.split(" (")[0] + f" ({m.group(1).title()})") if a else m.group(1).title()
    if fam == "P.S.":
        m = re.match(r"^pa_(\d+)ps_(.+)$", source)
        if m: return f"{m.group(1)} P.S. — {m.group(2).replace('_', ' ')}"
    return alias_for(fam, title, source) or source


# ----------------------------------------------------------------------------- file writing (idempotent)
_CREATED_RX = re.compile(r"^created:\s*(\S+)\s*$", re.M)
class Writer:
    def __init__(self, root: Path):
        self.root = root; self.written = 0; self.unchanged = 0; self.bytes = 0; self.paths = []
    def write(self, rel: str, text: str):
        p = self.root / rel
        old = None
        if p.exists():
            old = p.read_text(encoding="utf-8")
            m = _CREATED_RX.search(old)
            if m: text = _CREATED_RX.sub(f"created: {m.group(1)}", text, count=1)
        self.paths.append(rel); self.bytes += len(text.encode("utf-8"))
        if old == text:
            self.unchanged += 1; return False
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n"); self.written += 1; return True


# ----------------------------------------------------------------------------- hub / node renderers
def hub_name(fam, title):
    return FAM_SPEC[fam][0].format(t=title)

def hub_rel(name):
    return f"hubs/rules/{safe_name(name)}.md"

def node_rel(fam_key, title, source):
    # case-twin slugs (CFR-…/cfr-…, LAW_Constitution/law_constitution) are distinct on ext4 but ONE file on the NTFS vault —
    # mixed-case sources get a deterministic hash suffix so the mirror and the vault hold the same file set.
    nm = safe_name(source) if source == source.lower() else f"{safe_name(source)} ~{hashlib.md5(source.encode()).hexdigest()[:6]}"
    return f"nodes/{fam_key}/{safe_name(str(title))}/{nm}.md"

def render_rule_hub(fam, title, sources, cens, con, hub_names, edges):
    name = hub_name(fam, title); tpl, juris, level, fkey_tpl = FAM_SPEC[fam]
    fam_key = family_for(sources[0], fkey_tpl.format(t=title)) if sources else fkey_tpl.format(t=title)
    chunk_n = sum(cens.get(s, 0) for s in sources)
    text, scanned = hub_scan_text(con, sources)
    xr = cites_in(text, name)
    op, counts = operative_counts(text)
    dl = RE_DEADLINE.search(text)
    heading = ""
    if fam == "Pa. Code":
        m = RE_PACODE_HEAD.search(text)
        if m and m.group(1) == str(title): heading = m.group(2).strip().title()
    xr_sorted = sorted(xr.items(), key=lambda kv: (-kv[1], kv[0]))
    xr_names = [k for k, _ in xr_sorted]
    for k, n in xr_sorted:
        edges.append((hub_rel(name), k, "cross_ref", k in hub_names, n))
    edges.append((hub_rel(name), fam_key, "family", False, 1))
    fm = ["---", "kind: hub", "hub_of: rule", f"title: {yq(f'{fam} Title {title}' + (f' — {heading}' if heading else ''))}",
          f"cite: {yq(name)}", f"family: {yq('[[' + fam_key + ']]')}", f"jurisdiction: {links([juris])}", "court: []",
          f"authority_level: {links([level])}", f"rules: {links([name])}", f"registry_family: {yq(fam)}", f"registry_title: {yq(str(title))}",
          f"sources_n: {len(sources)}", f"chunks_n: {chunk_n}", f"sources_sample: {strs(sources[:25])}",
          f"scan_chunks: {scanned}", f"mandatory: {'true' if op in ('shall', 'must', 'void') else 'false'}"]
    if op: fm.append(f"operative: {op}")
    if dl: fm.append(f"deadline: {yq(dl.group(0))}")
    fm += [f"cross_ref: {links(xr_names[:40])}", "status: good_law", f"created: {TODAY}", "---"]
    body = [f"# {name}", "",
            f"**{fam} Title {title}**" + (f" — {heading}" if heading else "") + f" · registry sources: **{len(sources)}** · chunks in corpus: **{chunk_n}** "
            f"(census chunk_text.db). Every pointer stub under `_graph/nodes/{fam_key}/{safe_name(str(title))}/` carries `rules: [[{name}]]`.", "",
            "## Tied corpus nodes (Base)", "", "![[rules.base]]", "",
            f"## Verbatim-text canon (regex over {scanned} scanned chunks, spread across this hub's sources — not the whole title)", "",
            f"- operative: `{op or 'none detected'}` → mandatory: **{'true' if op in ('shall', 'must', 'void') else 'false'}** "
            f"(shall {counts['shall']} · must {counts['must']} · void {counts['void']} · may {counts['may']})",
            f"- deadline: `{dl.group(0) if dl else 'none in scanned text'}`", "",
            "## Typed edges", "",
            "- cross_ref (cite regex over the scanned text; count = occurrences): " +
            (", ".join(f"[[{k}]] ({n})" for k, n in xr_sorted[:40]) if xr_sorted else "—"),
            "- requires / defined_by / exception: *(not regex-safe at title granularity — left empty, not guessed)*", "",
            "## Sources (first 25 of " + str(len(sources)) + "; full list = side index `_build/graph.db` table hub_sources)", ""]
    body += [f"- `{s}` ({cens.get(s, 0)} chunks)" for s in sources[:25]]
    body += ["", "## NOT IN CORPUS", "",
             "Any `[[link]]` above that does not resolve is the marker (a cited title with no registry hub — e.g. Pa.C.S. — or a family",
             "FAMILIES.yaml does not name). Do not create a target by hand; legal-corpus-ingest reads the unresolved-link list.", ""]
    return hub_rel(name), "\n".join(fm + [""] + body), fam_key


def render_node(fam, title, source, fam_key, cens, con, hub, edges):
    ids, head = node_rows(con, source)
    n = cens.get(source, 0)
    tpl, juris, level, _ = FAM_SPEC[fam]
    alias = alias_for(fam, title, source)
    ttl = node_title(fam, title, source, head)
    op = operative(head)
    dl = RE_DEADLINE.search(head)
    fm = ["---", "kind: text", f"title: {yq(ttl)}"]
    if alias: fm.append(f"aliases: {strs([alias])}")
    fm += [f"family: {yq('[[' + fam_key + ']]')}", f"jurisdiction: {links([juris])}", f"authority_level: {links([level])}",
           f"rules: {links([hub])}", f"source: {yq(source)}", f"chunk_n: {n}", f"chunk_ids: {strs(ids)}",
           f"mandatory: {'true' if op in ('shall', 'must', 'void') else 'false'}"]
    if op: fm.append(f"operative: {op}")
    if dl: fm.append(f"deadline: {yq(dl.group(0))}")
    fm += ["status: good_law", f"created: {TODAY}", "---"]
    first = ids[0] if ids else "—"
    body = [f"# {alias or ttl}", "",
            f"> Pointer stub — `[corpus:{first}]`" + (f" (+{n - 1} more)" if n > 1 else "") + ". Fetch by id; text is not stored here.", "",
            "## Tied to", "", f"- [[{hub}]]", ""]
    rel = node_rel(fam_key, title, source)
    edges.append((rel, hub, "rules", True, 1))
    return rel, "\n".join(fm + [""] + body), alias


def topic_title(source):
    """amjur2d_<vol>_<topic> -> ('Am Jur 2d Topic', vol); amjur2d_<topic> (no vol) -> ('Am Jur 2d Topic', ''); volume blobs -> 'Am Jur 2d vol. N'."""
    m = re.match(r"^amjur2d_([0-9]+[A-Za-z]?|1stPleading)_(.+)$", source, re.I)
    if m:
        vol, slug = m.group(1), m.group(2)
        if re.match(r"^volume_", slug, re.I): return f"Am Jur 2d vol. {vol}", vol
        return "Am Jur 2d " + slug.replace("_", " ").title().replace("'S", "'s"), vol
    m = re.match(r"^amjur2d_(.+)$", source, re.I)
    return ("Am Jur 2d " + (m.group(1) if m else source).replace("_", " ").title().replace("'S", "'s")), ""

def topic_sections(scon, sources):
    """(B) chunk_struct -> [(sort_key, unit, heading, k_text_chunks, first_text_chunk_id, also_headings)] for a topic's sources.
    toc rows never count; heading = first text-row heading in struct order (running heads like 'CONFLICT OF LAWS 16 Am Jur 2d'
    skipped), else the toc heading tagged [toc]. A volume blob holds several titles whose § numbers restart — the other
    headings for the same § number are kept as `also:` rather than silently merged."""
    units = {}
    for s in sorted(sources):
        for cid, fam, title, unit, role, heading, ordv in struct_rows(scon, s):
            if not unit: continue
            u = re.sub(r"^§\s*", "", str(unit)).strip()
            d = units.setdefault(u, {"text": [], "toc": [], "first": None})
            h = (heading or "").strip().rstrip(".’»\"”'").strip()
            if re.search(r"\d+\s*Am\s*Jur\s*2d", h) or (h.isupper() and len(h) > 3): h = ""
            if role == "text":
                d["text"].append(h)
                if d["first"] is None: d["first"] = cid
            elif role == "toc" and h: d["toc"].append(h)
    out = []
    for u, d in units.items():
        if not d["text"]: continue
        heads = []
        for h in d["text"]:
            if h and h not in heads: heads.append(h)
        heading = heads[0] if heads else (f"{d['toc'][0]} [toc]" if d["toc"] else "")
        m = re.match(r"^(\d+)(.*)$", u)
        key = (int(m.group(1)), m.group(2)) if m else (10**9, u)
        out.append((key, u, heading, len(d["text"]), d["first"], heads[1:3]))
    out.sort(key=lambda r: r[0])
    return out


def render_topic_hub(name, vol, sources, cens, con, hub_names, edges, scon=None):
    ttl = name
    fam_key = family_for(sources[0], "amjur_")
    text, scanned = hub_scan_text(con, sources)
    secs = sorted({(int(re.sub(r"\D", "", a) or 0), a, b.strip()) for a, b in RE_AMJUR_SEC.findall(text)})[:60]
    sidx = topic_sections(scon, sources)
    xr = cites_in(text, name)
    xr_sorted = sorted(xr.items(), key=lambda kv: (-kv[1], kv[0]))
    for k, n in xr_sorted: edges.append((f"hubs/topics/{safe_name(name)}.md", k, "commentary", k in hub_names, n))
    edges.append((f"hubs/topics/{safe_name(name)}.md", fam_key, "family", False, 1))
    chunks_n = sum(cens.get(s, 0) for s in sources)
    fm = ["---", "kind: hub", "hub_of: topic", f"title: {yq(ttl)}", f"family: {yq('[[' + fam_key + ']]')}", f"volume: {yq(vol)}",
          f"authority_level: {links(['treatise'])}", f"source: {strs(sources)}", f"chunks_n: {chunks_n}", f"scan_chunks: {scanned}",
          f"sections_seen: {len(secs)}", f"sections_indexed: {len(sidx)}", f"cross_ref: {links([k for k, _ in xr_sorted[:40]])}", "status: in_corpus", f"created: {TODAY}", "---"]
    body = [f"# {ttl}", "", f"Am Jur 2d{(' vol. ' + vol) if vol else ''} — corpus source{'s' if len(sources) > 1 else ''} " + ", ".join(f"`{s}`" for s in sources) +
            f" ({chunks_n} chunks). Treatise TEXT never enters the vault;",
            "this hub is the category door: nodes carrying `topics: [[" + ttl + "]]` group below.", "",
            "## Nodes on this topic (Base)", "", "![[topics.base]]", "",
            f"## Sections ({len(sidx)} indexed in struct_index.db chunk_struct; `§ n — heading (text chunks) → first text chunk id`; toc rows excluded)", ""]
    if sidx:
        body += [f"- § {u} — {h or '*(no heading in data)*'} ({k} chunk{'s' if k != 1 else ''}) → `{first}`" + (f" · also: {'; '.join(also)}" if also else "")
                 for _, u, h, k, first, also in sidx[:TOPIC_SECTION_LINES]]
        if len(sidx) > TOPIC_SECTION_LINES:
            body.append(f"- … {len(sidx) - TOPIC_SECTION_LINES} more sections — `SELECT unit, heading, chunk_id FROM chunk_struct WHERE source IN ({', '.join(repr(s) for s in sources)}) AND role='text'`")
    else:
        body.append("- *(source not in struct_index.db — run struct_index_build for its family)*")
    body += ["", f"## Sections seen (regex `§ n. heading.` over {scanned} scanned chunks — skeleton only, see PLAN §10)", ""]
    body += [f"- § {a}. {b}" for _, a, b in secs] or ["- *(no section headings in the scanned sample)*"]
    body += ["", "## Commentary edges (cite regex over the scanned text)", "",
             (", ".join(f"[[{k}]] ({n})" for k, n in xr_sorted[:40]) if xr_sorted else "—"), ""]
    return f"hubs/topics/{safe_name(name)}.md", "\n".join(fm + [""] + body)


def case_hub_name(meta):
    return f"{meta['caption']} ({meta['cite']})" if meta["cite"] else meta["caption"]

def render_case_hub(source, cens, con, scon, hub_names, edges):
    """(A) one hub per case SOURCE: hubs/cases/<Caption (cite)>.md. Deterministic: registry row + caption chunk regex + chunk_struct roles.
    Edges: `applies` (case → every rule hub the cite regex finds in its text; resolved iff the hub exists) + `family`."""
    srows = struct_rows(scon, source)
    fam = srows[0][1] if srows else struct_family(scon, source)
    ids_all, head = node_rows(con, source)
    cites = case_cites(source)
    meta = case_meta(source, cites, head)
    name = case_hub_name(meta); rel = f"hubs/cases/{safe_name(name)}.md"
    fam_key = family_for(source, fam or "case")
    n = cens.get(source, 0)
    by_role = {}
    for cid, _f, _t, _u, role, heading, ordv in srows:
        by_role.setdefault(role or "other", []).append((ordv, cid, heading or ""))
    roles = sorted(by_role, key=lambda r: (CASE_ROLE_ORDER.index(r) if r in CASE_ROLE_ORDER else 99, r))
    text, scanned = hub_scan_text(con, [source])
    xr = cites_in(text, name); xr_sorted = sorted(xr.items(), key=lambda kv: (-kv[1], kv[0]))
    for k, cnt in xr_sorted: edges.append((rel, k, "applies", k in hub_names, cnt))
    edges.append((rel, fam_key, "family", False, 1))
    nid = "NOT IN DATA"
    fm = ["---", "kind: hub", "hub_of: case", f"title: {yq(meta['caption'])}", f"cite: {yq(meta['cite'] or nid)}"]
    if meta["aliases"] or meta["caption"]:
        fm.append(f"aliases: {strs(sorted(set(meta['aliases']) | {meta['caption']}))}")
    fm += [f"court: {yq(meta['court'] or nid)}", f"court_code: {yq(meta['court_code'] or nid)}",
           f"jurisdiction: {links([meta['jurisdiction']]) if meta['jurisdiction'] else '[]'}", f"year: {meta['year'] or yq(nid)}",
           f"authority_level: {links(['case'])}", f"family: {yq('[[' + fam_key + ']]')}", f"struct_family: {yq(fam or nid)}",
           f"source: {yq(source)}", f"chunks_n: {n}", f"chunks_indexed: {len(srows)}", f"roles: {strs(roles)}",
           f"reporters: {strs(sorted(set(f'{v} {rep} {p}' for rep, v, p, _c, _cc, _y in cites)))}",
           f"applies: {links([k for k, _ in xr_sorted[:40]])}", f"provenance: {yq(', '.join(f'{k}={v}' for k, v in sorted(meta['prov'].items())) or nid)}",
           "status: in_corpus", f"created: {TODAY}", "---"]
    body = [f"# {name}", "",
            f"**{meta['caption']}**, {meta['cite'] or nid}" + (f" ({meta['court']}" + (f" {meta['year']}" if meta['year'] else "") + ")" if meta['court'] else "") +
            f" · corpus source `{source}` · {n} chunks (census) · {len(srows)} indexed in chunk_struct. Opinion TEXT is not stored here — fetch by chunk id.", "",
            "## Chunks by role (struct_index.db chunk_struct; first " + str(CASE_HUB_CHUNK_IDS) + f" of {len(srows)} inline — the full set is `SELECT chunk_id, role FROM chunk_struct WHERE source='{source}' ORDER BY ord`; side index `_build/graph.db` hub_sources)", ""]
    budget = CASE_HUB_CHUNK_IDS
    for role in roles:
        items = sorted(by_role[role])
        body.append(f"### {role} ({len(items)})")
        for ordv, cid, heading in items[:budget]:
            body.append(f"- `{cid}`" + (f" — {heading[:120].strip()}…" if heading else ""))
        if len(items) > budget: body.append(f"- … +{len(items) - budget} more `{role}` chunks")
        budget = max(0, budget - min(len(items), budget))
        body.append("")
    if not srows:
        body += [f"- *(source not in struct_index.db — roles unknown; {n} chunks in chunk_text; first id `{ids_all[0] if ids_all else '—'}`)*", ""]
    body += ["## Typed edges", "",
             "- applies (cite regex over the opinion text; count = occurrences): " + (", ".join(f"[[{k}]] ({c})" for k, c in xr_sorted[:40]) if xr_sorted else "—"),
             "- holding / overrules / distinguishes: *(not regex-safe — left empty, not guessed)*", "",
             "## Provenance", "",
             "- " + (", ".join(f"{k}: {v}" for k, v in sorted(meta["prov"].items())) or "no registry row, no caption regex hit — caption from slug") +
             "; fields marked `NOT IN DATA` have no source in cite_registry or the caption chunk.", "",
             "## NOT IN CORPUS", "", "Unresolved `[[link]]` above = a cited rule with no hub; legal-corpus-ingest reads the unresolved-link list.", ""]
    # props for notes.fm (G0c shape) so graph.json hands the m2 seat (path_to_law.HubIndex._ingest) kind/aliases/text_chunks without a vault read;
    # text_chunks = holding chunks first, then the rest in role order (capped) — the seat's holding_rank() already prefers holdings for cases.
    ordered = [cid for role in roles for _o, cid, _h in sorted(by_role[role])]
    holding = [cid for _o, cid, _h in sorted(by_role.get("holding", []))]
    props = {"kind": "hub", "hub_of": "case", "title": meta["caption"], "cite": meta["cite"], "aliases": sorted(set(meta["aliases"]) | {meta["caption"]}),
             "court": meta["court"], "court_code": meta["court_code"], "jurisdiction": meta["jurisdiction"], "year": meta["year"], "family": fam_key,
             "struct_family": fam, "source": source, "chunks_n": n, "roles": roles, "applies": [k for k, _ in xr_sorted[:40]], "status": "in_corpus",
             "text_chunks": (holding + [c for c in ordered if c not in holding])[:CASE_HUB_CHUNK_IDS]}
    return rel, "\n".join(fm + [""] + body), name, fam_key, props


def render_const(name, spec, cens, con, hub_names, edges):
    srcs = [s for s in spec["sources"] if cens.get(s)]
    rows = []
    for s in srcs:
        rows += con.execute("SELECT chunk_id, text FROM chunk_text WHERE source=? ORDER BY chunk_id", (s,)).fetchall()
    text = "\n".join(t or "" for _, t in rows)
    # segment: article / amendment headings -> nodes with the chunk ids where the heading occurs
    units = {}
    if name == "US Const":
        for cid, t in rows:
            for m in RE_US_AMEND.finditer(t or ""): units.setdefault(("Amendment", m.group(1)), set()).add(cid)
            for m in RE_US_ART.finditer(t or ""): units.setdefault(("Article", m.group(1)), set()).add(cid)
    else:
        for cid, t in rows:
            for m in RE_PA_ART.finditer(t or ""): units.setdefault(("Article", m.group(1)), set()).add(cid)
    xr = cites_in(text, name); xr_sorted = sorted(xr.items(), key=lambda kv: (-kv[1], kv[0]))
    rel = hub_rel(name)
    for k, n in xr_sorted: edges.append((rel, k, "cross_ref", k in hub_names, n))
    edges.append((rel, spec["family"], "family", False, 1))
    op, _ = operative_counts(text)
    fm = ["---", "kind: hub", "hub_of: rule", f"title: {yq(spec['title'])}", f"cite: {yq(name)}", f"family: {yq('[[' + spec['family'] + ']]')}",
          f"jurisdiction: {links([spec['jurisdiction']])}", "court: []", f"authority_level: {links(['constitution'])}", f"rules: {links([name])}",
          f"sources_n: {len(srcs)}", f"chunks_n: {sum(cens.get(s, 0) for s in srcs)}", f"sources_sample: {strs(srcs)}", f"scan_chunks: {len(rows)}",
          f"units_n: {len(units)}", f"mandatory: {'true' if op in ('shall', 'must', 'void') else 'false'}"]
    if op: fm.append(f"operative: {op}")
    fm += [f"cross_ref: {links([k for k, _ in xr_sorted[:40]])}", "status: good_law", f"created: {TODAY}", "---"]
    roman = lambda r: {"I": 1, "V": 5, "X": 10, "L": 50}
    def rv(s):
        v, prev = 0, 0
        for ch in reversed(s):
            x = {"I": 1, "V": 5, "X": 10, "L": 50}[ch]; v += -x if x < prev else x; prev = max(prev, x)
        return v
    ukeys = sorted(units, key=lambda k: (k[0], rv(k[1])))
    body = [f"# {name}", "", f"**{spec['title']}** · sources {', '.join('`' + s + '`' for s in srcs)} · {len(rows)} chunks (all scanned).", "",
            "## Tied corpus nodes (Base)", "", "![[rules.base]]", "",
            f"## Units segmented by regex ({len(units)}; a unit node = the chunk ids where its heading occurs — OCR two-column scramble means a heading can sit in >1 chunk)", ""]
    body += [f"- [[{name} {k[0]} {k[1]}]] ({len(units[k])} chunk{'s' if len(units[k]) != 1 else ''})" for k in ukeys] or ["- *(no headings matched)*"]
    body += ["", "## Typed edges", "", "- cross_ref: " + (", ".join(f"[[{k}]] ({n})" for k, n in xr_sorted[:40]) if xr_sorted else "—"), "",
             "## NOT IN CORPUS", "", "Unresolved `[[link]]` = marker; legal-corpus-ingest reads the unresolved-link list.", ""]
    files = [(rel, "\n".join(fm + [""] + body))]
    for k in ukeys:
        uname = f"{name} {k[0]} {k[1]}"; ids = sorted(units[k])
        nfm = ["---", "kind: text", f"title: {yq(uname)}", f"family: {yq('[[' + spec['family'] + ']]')}", f"jurisdiction: {links([spec['jurisdiction']])}",
               f"authority_level: {links(['constitution'])}", f"rules: {links([name])}", f"unit: {yq(k[0])}", f"number: {yq(k[1])}",
               f"source: {strs(srcs)}", f"chunk_n: {len(ids)}", f"chunk_ids: {strs(ids)}", "status: good_law", f"created: {TODAY}", "---"]
        nbody = [f"# {uname}", "", f"> Pointer stub — `[corpus:{ids[0]}]`" + (f" (+{len(ids) - 1} more)" if len(ids) > 1 else "") + ". Heading match only; fetch by id.", "",
                 "## Tied to", "", f"- [[{name}]]", ""]
        nrel = f"nodes/{spec['node_dir']}/{safe_name(uname)}.md"
        edges.append((nrel, name, "rules", True, 1))
        files.append((nrel, "\n".join(nfm + [""] + nbody)))
    return files


# ----------------------------------------------------------------------------- side index
def open_index(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript("""
    CREATE TABLE IF NOT EXISTS notes(rel TEXT PRIMARY KEY, name TEXT, kind TEXT, hub_of TEXT, family TEXT, source TEXT, chunk_n INTEGER, alias TEXT, bytes INTEGER);
    CREATE TABLE IF NOT EXISTS edges(src TEXT, dst TEXT, type TEXT, resolved INTEGER, n INTEGER, PRIMARY KEY(src, dst, type));
    CREATE TABLE IF NOT EXISTS hub_sources(hub TEXT, source TEXT, chunk_n INTEGER, node_rel TEXT, PRIMARY KEY(hub, source));
    CREATE INDEX IF NOT EXISTS ix_edges_dst ON edges(dst);
    CREATE INDEX IF NOT EXISTS ix_notes_alias ON notes(alias);
    CREATE INDEX IF NOT EXISTS ix_hs_source ON hub_sources(source);
    CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
    """)
    # G0c (11-09): unit hubs (pipeline.corpus_graph_units) store their frontmatter as JSON so graph.json can hand the m2 seat
    # (path_to_law.HubIndex._load_json) kind/aliases/text_chunks without any vault read. NULL for bulk notes.
    if "fm" not in {r[1] for r in db.execute("PRAGMA table_info(notes)")}:
        db.execute("ALTER TABLE notes ADD COLUMN fm TEXT")
    return db

def note_meta(rel, text):
    fm = text.split("---", 2)[1] if text.startswith("---") else ""
    def g(k):
        m = re.search(rf"^{k}:\s*(.*)$", fm, re.M)
        return (m.group(1) if m else "").strip().strip('"')
    name = Path(rel).stem
    fam = re.sub(r'^\["?\[\[|\]\]"?\]$', "", g("family"))
    src = g("source").strip("[]").split(",")[0].strip().strip('"')
    cn = g("chunks_n") or g("chunk_n") or "0"
    al = g("aliases").strip("[]").strip()
    al = al.split('", "')[0].strip().strip('"')     # first alias only (notes.alias is a single key; the full list rides in notes.fm when present)
    return (rel, name, g("kind"), g("hub_of"), fam, src, int(cn) if cn.isdigit() else 0, al, len(text.encode("utf-8")))

def dump_graph_json(db, out: Path, scope_json: Path | None):
    notes = db.execute("SELECT rel, name, kind, hub_of, family, source, chunk_n, alias, fm FROM notes ORDER BY rel").fetchall()
    edges = db.execute("SELECT src, dst, type, resolved, n FROM edges ORDER BY src, dst").fetchall()
    names = {n[1]: n[0] for n in notes}
    for n in notes:
        if n[7]: names.setdefault(n[7], n[0])
    def _node(n):
        d = dict(zip(("rel", "name", "kind", "hub_of", "family", "source", "chunk_n", "alias"), n[:8])); d["path"] = f"_graph/{n[0]}"
        if n[8]:
            try: d["frontmatter"] = json.loads(n[8])
            except Exception: pass
        return d
    g = {"built": TODAY, "scope": "_graph", "nodes": [_node(n) for n in notes],
         "edges": [{"src": s, "dst": d, "type": t, "resolved_to": names.get(d), "n": n} for s, d, t, r, n in edges]}
    out.write_text(json.dumps(g, ensure_ascii=False), encoding="utf-8")
    if scope_json is not None:
        # cache-compatible with pipeline.obsidian_graph.get_wikilink_index(folder_scope=['_graph']) — built WITHOUT walking the vault
        outgoing, incoming = {}, {}
        for n in notes: outgoing[f"_graph/{n[0]}"] = []
        unresolved = 0
        for s, d, t, r, n in edges:
            rt = names.get(d); rel = f"_graph/{s}"; rts = f"_graph/{rt}" if rt else None
            outgoing.setdefault(rel, []).append({"target": d, "edge_type": t if t in ("cross_ref", "applies", "commentary") else "unknown", "pin_cite": None, "resolved_to": rts})
            if rts: incoming.setdefault(rts, []).append(rel)
            else: unresolved += 1
        orphans = [k for k, v in outgoing.items() if not v and not incoming.get(k)]
        payload = {"scope": ["_graph"], "built_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()), "vault_root": str(VAULT),
                   "stats": {"files_scanned": len(notes), "files_with_links": sum(1 for v in outgoing.values() if v), "edges": len(edges),
                             "unresolved_targets": unresolved, "orphans": len(orphans), "duration_s": 0.0, "builder": "pipeline.corpus_graph_build (scoped, no vault walk)"},
                   "outgoing": outgoing, "incoming": incoming, "orphans": orphans}
        scope_json.parent.mkdir(parents=True, exist_ok=True)
        scope_json.write_text(json.dumps(payload), encoding="utf-8")
    return len(notes), len(edges)

def scope_hash(folder_scope):
    return hashlib.md5(",".join(sorted(folder_scope)).encode()).hexdigest()[:12]


# ----------------------------------------------------------------------------- build core (full or scoped)
def build(sources_filter=None, nodes=True, mirror_to_vault=False, verbose=True):
    """sources_filter=None → full build. Otherwise an iterable of source slugs: rebuild the hubs those slugs belong to (hub content
    needs ALL of the hub's registry sources) and the pointer nodes for just those slugs."""
    t0 = time.time()
    cens = census(); t_census = time.time() - t0
    reg = registry_rows()
    hubs = {}
    for fam, title, src in reg:
        if fam in FAM_SPEC: hubs.setdefault((fam, title), []).append(src)
    amjur_sources = sorted(s for s in cens if s.lower().startswith("amjur2d_"))
    hub_names = {hub_name(f, t) for f, t in hubs} | set(CONST_HUBS) | {"FRCP 19", "Pa.R.C.P. 440", "15 USC 1681s-2"}   # sample hubs exist in the vault
    topic_names, topics = {}, {}      # source -> title ; title -> (vol, [sources])  (one hub per TITLE: twins / section slices share it)
    for s in amjur_sources:
        ttl, vol = topic_title(s)
        topic_names[s] = ttl
        tv = topics.setdefault(ttl, [vol, []]); tv[1].append(s)
        if not tv[0]: tv[0] = vol
    hub_names |= set(topics)

    sel_hubs, sel_nodes, sel_topics, sel_const, sel_cases = set(hubs), None, set(topics), set(CONST_HUBS), set()
    scon = struct_con()
    if sources_filter is not None:
        sf = set(sources_filter); sel_hubs, sel_nodes, sel_topics, sel_const = set(), set(), set(), set()
        src2hub = {}
        for (f, t), ss in hubs.items():
            for s in ss: src2hub.setdefault(s, []).append((f, t))
        for s in sf:
            for ht in src2hub.get(s, []): sel_hubs.add(ht); sel_nodes.add(s)
            if s in topic_names: sel_topics.add(topic_names[s])
            for cn, spec in CONST_HUBS.items():
                if s in spec["sources"]: sel_const.add(cn)
            # (A) a scoped source outside registry/amjur/const whose struct family is case_* gets a case hub — SCOPED ONLY, never bulk
            if s not in src2hub and s not in topic_names and s in cens and is_case_family(struct_family(scon, s)):
                sel_cases.add(s)
    W = Writer(MIRROR); con = chunk_con(); edges = []; idx_rows = []; hub_src_rows = []; fm_rows = []
    rep = {"hubs_by_family": {}, "nodes": 0, "topic_hubs": 0, "const_hubs": 0, "const_nodes": 0, "case_hubs": 0, "aliases_dup": 0}
    seen_alias = {}
    for (fam, title) in sorted(sel_hubs, key=lambda k: (k[0], len(k[1]), k[1])):
        srcs = sorted(hubs[(fam, title)])
        rel, text, fam_key = render_rule_hub(fam, title, srcs, cens, con, hub_names, edges)
        W.write(rel, text); idx_rows.append(note_meta(rel, text))
        rep["hubs_by_family"][fam] = rep["hubs_by_family"].get(fam, 0) + 1
        hn = hub_name(fam, title)
        for s in srcs:
            nrel = node_rel(fam_key, title, s) if nodes else None
            hub_src_rows.append((hn, s, cens.get(s, 0), nrel))
        if nodes:
            for s in srcs:
                if sel_nodes is not None and s not in sel_nodes: continue
                nrel, ntext, alias = render_node(fam, title, s, fam_key, cens, con, hn, edges)
                W.write(nrel, ntext); idx_rows.append(note_meta(nrel, ntext)); rep["nodes"] += 1
                if alias:
                    if alias in seen_alias: rep["aliases_dup"] += 1
                    seen_alias[alias] = nrel
        if verbose and rep["nodes"] and rep["nodes"] % 10000 < len(srcs):
            print(f"  … {rep['nodes']} nodes, {sum(rep['hubs_by_family'].values())} hubs, {round(time.time() - t0)}s", flush=True)
    for ttl in sorted(sel_topics):
        vol, tsrcs = topics[ttl]
        rel, text = render_topic_hub(ttl, vol, sorted(tsrcs), cens, con, hub_names, edges, scon)
        W.write(rel, text); idx_rows.append(note_meta(rel, text)); rep["topic_hubs"] += 1
        for s in tsrcs: hub_src_rows.append((ttl, s, cens.get(s, 0), None))
    for s in sorted(sel_cases):
        rel, text, cname, _fk, props = render_case_hub(s, cens, con, scon, hub_names, edges)
        W.write(rel, text); idx_rows.append(note_meta(rel, text)); rep["case_hubs"] += 1
        hub_src_rows.append((cname, s, cens.get(s, 0), None)); fm_rows.append((json.dumps(props, ensure_ascii=False), rel))
    rep["case_sources"] = sorted(sel_cases)
    for cn in sorted(sel_const):
        files = render_const(cn, CONST_HUBS[cn], cens, con, hub_names, edges)
        for i, (rel, text) in enumerate(files):
            W.write(rel, text); idx_rows.append(note_meta(rel, text))
            if i == 0: rep["const_hubs"] += 1
            else: rep["const_nodes"] += 1
        for s in CONST_HUBS[cn]["sources"]:
            if cens.get(s): hub_src_rows.append((cn, s, cens.get(s, 0), None))
    con.close()
    if scon is not None: scon.close()
    # side index (upsert) + graph.json + scoped wikilink cache
    db = open_index(MIRROR / "_build" / "graph.db")
    db.executemany("INSERT OR REPLACE INTO notes(rel, name, kind, hub_of, family, source, chunk_n, alias, bytes) VALUES(?,?,?,?,?,?,?,?,?)", idx_rows)
    db.executemany("INSERT OR REPLACE INTO edges VALUES(?,?,?,?,?)", [(s, d, t, int(r), n) for s, d, t, r, n in edges])
    db.executemany("INSERT OR REPLACE INTO hub_sources VALUES(?,?,?,?)", hub_src_rows)
    if fm_rows: db.executemany("UPDATE notes SET fm=? WHERE rel=?", fm_rows)   # case hubs: props → graph.json frontmatter (aliases reach the seat)
    db.execute("INSERT OR REPLACE INTO meta VALUES('built', ?)", (TODAY,)); db.commit()
    n_notes, n_edges = dump_graph_json(db, MIRROR / "_build" / "graph.json", MIRROR / "_build" / f"wikilink_index_{scope_hash(['_graph'])}.json")
    # unresolved audit from the index (whole graph, not just this run)
    unres = db.execute("SELECT dst, type, sum(n) FROM edges WHERE resolved=0 GROUP BY dst, type ORDER BY sum(n) DESC LIMIT 20").fetchall()
    db.close()
    rep.update({"files_in_scope": len(W.paths), "files_written": W.written, "files_unchanged": W.unchanged, "bytes_in_scope": W.bytes,
                "index_notes": n_notes, "index_edges": n_edges, "unresolved_top20": [{"target": d, "type": t, "n": n} for d, t, n in unres],
                "census_s": round(t_census, 1), "wall_s": round(time.time() - t0, 1), "mirror": str(MIRROR), "scoped": sources_filter is not None})
    if mirror_to_vault:
        rep["vault_sync"] = sync_to_vault(W.paths if sources_filter is not None else None)
    (MIRROR / "_build" / ("emit_report.json" if sources_filter is not None else "build_report.json")).write_text(json.dumps(rep, indent=1, ensure_ascii=False), encoding="utf-8")
    return rep


def sync_to_vault(rel_paths=None):
    """Push hubs/ + nodes/ (or just rel_paths) from the ext4 mirror into obsidian_sync/_graph/ — never --delete, never per-file loops.
    Default lane = ONE PAX tar (UTF-8 path headers: en-dash slugs survive) copied over 9p, extracted by Windows-native tar.exe at NTFS
    speed (67K files ≈ 35 s; rsync over 9p measured 40 files/s ≈ 28 min). CP437 mangling (`–` → `ΓÇô`) happens with ustar/GNU
    format — PAX_FORMAT is load-bearing. Falls back to rsync when tar.exe is absent. Also drops the scoped wikilink cache into
    obsidian_sync/_index/ (one file)."""
    import tarfile
    t0 = time.time(); VAULT_G.mkdir(parents=True, exist_ok=True)
    if rel_paths is None:
        rel_paths = []
        for d in ("hubs", "nodes"):
            for root, _, fs in os.walk(MIRROR / d):
                rel_paths += [os.path.relpath(os.path.join(root, f), MIRROR) for f in fs]
        rel_paths.sort()
    out: dict = {"files": len(rel_paths)}
    tar_exe = Path("/mnt/c/Windows/System32/tar.exe")
    if tar_exe.exists() and rel_paths:
        tpath = VAULT_G / "_build" / "_sync_payload.tar"; tpath.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(tpath, "w", format=tarfile.PAX_FORMAT, encoding="utf-8") as t:
            for rel in rel_paths: t.add(MIRROR / rel, arcname=rel, recursive=False)
        win = lambda p: "C:" + str(p).replace("/mnt/c", "", 1).replace("/", "\\")
        r = subprocess.run([str(tar_exe), "-xf", win(tpath), "-C", win(VAULT_G)], capture_output=True, text=True)
        tpath.unlink(missing_ok=True)
        out.update({"lane": "pax-tar+tar.exe", "rc": r.returncode, "err": r.stderr[-300:]})
    else:
        lst = MIRROR / "_build" / "emit_files.txt"; lst.write_text("\n".join(rel_paths) + "\n", encoding="utf-8")
        r = subprocess.run(["rsync", "-a", "--info=stats1", f"--files-from={lst}", f"{MIRROR}/", f"{VAULT_G}/"], capture_output=True, text=True)
        out.update({"lane": "rsync", "rc": r.returncode, "err": r.stderr[-300:],
                    "stats": [l for l in r.stdout.splitlines() if l.startswith(("Number of", "Total transferred"))]})
    src = MIRROR / "_build" / f"wikilink_index_{scope_hash(['_graph'])}.json"
    if src.exists():
        VAULT_INDEX.mkdir(parents=True, exist_ok=True)
        subprocess.run(["rsync", "-a", str(src), str(VAULT_INDEX / src.name)], capture_output=True)
    for extra in ("graph.json", "build_report.json", "emit_report.json"):
        if (MIRROR / "_build" / extra).exists():
            subprocess.run(["rsync", "-a", str(MIRROR / "_build" / extra), str(VAULT_G / "_build" / extra)], capture_output=True)
    out["wall_s"] = round(time.time() - t0, 1)
    return out


def graph_emit(source_slugs, mirror_to_vault=True):
    """G6' hook (PLAN §14): after an ingest, regenerate the _graph hubs/nodes for just these source slugs, idempotently, and sync them.
    Call sites (flag-gated GRAPH_EMIT=1): official_source_puller.ingest_source, case_fetch.ingest_case, corpus_gap_bot.fill_gap → graph_emit([slug]).
    A slug whose chunk_struct family is case_* (and is in no registry hub) yields hubs/cases/<Caption (cite)>.md — the ONLY way case hubs are made."""
    return build(sources_filter=list(source_slugs), nodes=True, mirror_to_vault=mirror_to_vault, verbose=False)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true", help="full build on the ext4 mirror (rule/topic/const hubs + nodes; NEVER case hubs)")
    ap.add_argument("--no-nodes", action="store_true", help="registry-level hubs only (size-first fallback)")
    ap.add_argument("--mirror-to-vault", action="store_true", help="rsync hubs/ + nodes/ into obsidian_sync/_graph/ after the build")
    ap.add_argument("--emit", nargs="+", metavar="SLUG", help="graph_emit for these source slugs")
    ap.add_argument("--cases", metavar="SLUG[,SLUG…]", help="explicit case hubs for these case source slugs (scoped emit; same code path as --emit)")
    ap.add_argument("--sync-only", action="store_true")
    a = ap.parse_args()
    if a.build:
        print(json.dumps(build(nodes=not a.no_nodes, mirror_to_vault=a.mirror_to_vault), indent=1, ensure_ascii=False))
    if a.emit:
        print(json.dumps(graph_emit(a.emit, mirror_to_vault=a.mirror_to_vault), indent=1, ensure_ascii=False))
    if a.cases:
        print(json.dumps(graph_emit([s.strip() for s in a.cases.split(",") if s.strip()], mirror_to_vault=a.mirror_to_vault), indent=1, ensure_ascii=False))
    if a.sync_only:
        print(json.dumps(sync_to_vault(), indent=1))
