"""Deterministic card gate — THE single quote verifier for the room pipeline (cells_to_ground.reverify delegates here).
A quoted authority on a CARD is VERIFIED only when the FULL quoted sentence (whitespace/quote-mark normalised, every
elided segment) is a substring of ONE corpus chunk whose source belongs to the cited authority's family
(usc_t<T>_s<sec><letter> / amjur2d_<vol> / federal rule text carrying 'Rule <n>' / reporter slug or both party names).
A match of only a leading/trailing/middle window is status 'partial' and NEVER counts as verified (S-07).
[doc] quotes (record facts) are checked against the room's input files only.
Backend: sqlite FTS5 over chunk_text.db (read-only, no torch, no HTTP). CARD_GATE_HTTP=1 adds the corpus HTTP search
as an extra candidate finder (candidates are still full-text checked here).
usage: card_gate.py <cards.md> [input_dir]   (rewrites in place, prints a tally)"""
from pathlib import Path
from core.entropy_paths import entropy_root as _eroot, package_root as _epkg, hermes_home as _ehermes
import re, sys, json, glob, sqlite3, os, urllib.request, urllib.parse, importlib.util

DB = os.environ.get("CHUNK_DB", "file:~/turbovec/chunk_text.db?mode=ro")
CORPUS = "http://127.0.0.1:8100/search_text"
Q = re.compile(r'[“"]([^”"]{4,400})[”"]')
_con = None


def _db():
    global _con
    if _con is None:
        _con = sqlite3.connect(DB, uri=True, timeout=90)
        _con.execute("PRAGMA busy_timeout=90000")
    return _con


def norm(s):
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"').replace("—", "-").replace("–", "-").replace("\u00a0", " ")
    return re.sub(r"\s+", " ", s).strip().lower()


def segments(quote):
    """an elided quote ("A ... B") must match segment by segment; each segment ≥ 4 words is checked, all must hold."""
    segs = [x.strip(" ,;:") for x in re.split(r"\s*(?:\.\.\.|…|\[\.\.\.\]|\[…\])\s*", quote) if x.strip()]
    segs = [x for x in segs if len(x.split()) >= 4] or segs
    return segs


def _in_text(q, text):
    """FULL-segment substring match, tolerant of bracketed alterations and punctuation differences."""
    if q in text: return True
    strip = lambda x: re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", "", re.sub(r"\[[^\]]*\]", "", x))).strip()
    qs, ts = strip(q), strip(text)
    return len(qs) > 20 and qs in ts


# ---------------------------------------------------------------- authority family (class rule, no per-case list)
_USC = re.compile(r"(\d+)\s*U\.?S\.?C\.?A?\.?\s*§+\s*(\d+[a-z]?(?:-\d+[a-z]?)?)")
_RULE = re.compile(r"Fed\. ?R\. ?(Civ|App|Crim|Evid)\. ?P?\.? ?(\d+)")
_AMJUR = re.compile(r"(\d+[A-Z]?) Am\. ?Jur\. ?2d")
_REP = re.compile(r"(\d+)\s+(U\.S\.|F\.\d?d|F\. ?Supp\.? ?\d?d?|S\. ?Ct\.|A\.\d?d|Pa\.)\s+(\d+)")
_RULEFAM = {"Civ": r"federal.?rules.?of.?civil|frcp|\d+bv$", "App": r"federal.?rules.?of.?appellate|frap|\d+bv$",
            "Crim": r"federal.?rules.?of.?criminal|frcrmp", "Evid": r"federal.?rules.?of.?evidence|fre\b"}


def family_ok(cite, src, cid, text):
    """Does chunk (src, cid, text) belong to the authority family of `cite`?  Returns the match kind, '' on mismatch,
    or 'family-unparsed' when the cite names no family this gate knows (forum/local rules, treatises other than Am Jur)."""
    cite = cite or ""; S, C, T = (src or "").lower(), (cid or "").lower(), text or ""
    m = _USC.search(cite)
    if m:
        key = f"usc_t{m.group(1)}_s{m.group(2).lower()}"
        rx = rf"(^|[^a-z0-9]){re.escape(key)}([^a-z0-9]|$)"
        return "usc-key" if re.search(rx, S) or re.search(rx, C) else ""
    m = _RULE.search(cite)
    if m:
        if not re.search(_RULEFAM[m.group(1)], S): return ""
        return "rule+number" if re.search(rf"\bRule {m.group(2)}\b", T) else ""
    m = _AMJUR.search(cite)
    if m: return "amjur-volume" if f"amjur2d_{m.group(1).lower()}" in S else ""
    m = _REP.search(cite)
    if m:
        v, pg = m.group(1), m.group(3)
        if re.search(rf"(?<!\d){v}[_-]?(us|f\d?d|fsupp\d?|sct|a\d?d|pa)?[_-]?{pg}(?!\d)", S): return "reporter-slug"
        parties = re.match(r"\s*(?:In re |Ex parte )?([A-Z][\w'.-]+)[^,]*?\bv\.\s+([A-Z][\w'.-]+)", cite)
        if parties and all(re.search(rf"\b{re.escape(p)}\b", T) for p in parties.groups()): return "both-parties"
        return ""
    return "family-unparsed"


# ---------------------------------------------------------------- candidates + verdict
def _fts(phrase, limit=40):
    toks = re.sub(r"[^\w\s§]", " ", phrase).split()
    if not toks: return []
    try:
        return [r[0] for r in _db().execute("SELECT source_id FROM chunk_text_fts WHERE chunk_text_fts MATCH ? LIMIT ?",
                                            ('"' + " ".join(toks) + '"', limit)).fetchall()]
    except Exception:
        return []


def _chunk(cid):
    try: r = _db().execute("SELECT source, text FROM chunk_text WHERE chunk_id=?", (cid,)).fetchone()
    except Exception: r = None
    return (r[0], r[1]) if r else (None, None)


def _http_candidates(q, k=20):
    try:
        url = CORPUS + "?" + urllib.parse.urlencode({"query": q, "top_k": k})
        return [(h.get("chunk_id") or h.get("source"), h.get("source"), h.get("text", "")) for h in json.load(urllib.request.urlopen(url, timeout=60)).get("hits", [])]
    except Exception:
        return []


def _candidates(seg):
    """(cid, source, text) candidates: full-phrase FTS first, then lead-8-words FTS, then optional HTTP."""
    seen, out = set(), []
    for phrase, lim in ((seg, 40), (" ".join(seg.split()[:8]), 60)):
        for cid in _fts(phrase, lim):
            if cid in seen: continue
            src, txt = _chunk(cid)
            if txt is not None: seen.add(cid); out.append((cid, src, txt))
    if os.environ.get("CARD_GATE_HTTP") == "1":
        for cid, src, txt in _http_candidates(seg):
            if cid and cid not in seen: seen.add(cid); out.append((cid, src, txt))
    return out


def _windows(seg):
    w = seg.split()
    wins = [" ".join(w[:18]), " ".join(w[-18:])]
    if len(seg) > 60: wins.append(seg[len(seg)//2 - 30: len(seg)//2 + 30])
    return [x for x in wins if len(x.split()) >= 4]


def verify_phrase(quote, cite=""):
    """-> {"status": verified|partial|unverified, "chunk_id", "source", "how"}.
    verified : every segment of the quote is a full substring of a chunk in the cited family (same chunk preferred).
    partial  : only a window (first/last 18 words or a 60-char middle) of some segment was found — NOT verified.
    unverified: nothing found, or found only in a chunk of another authority family (how says which)."""
    segs = [norm(s) for s in segments(quote)] or [norm(quote)]
    hits, wrong = [], None
    for seg in segs:
        full = None
        cands = _candidates(seg)
        for cid, src, txt in cands:
            if not _in_text(seg, norm(txt)): continue
            fam = family_ok(cite, src, cid, txt)
            if fam: full = (cid, src, fam); break
            wrong = wrong or (cid, src)
        if full:
            hits.append(full); continue
        # no full in-family match: a window may be there — reported as partial, never verified
        for win in _windows(seg):
            pool = cands or [(c, s, t) for c in _fts(win, 40) for (s, t) in [_chunk(c)] if t is not None]
            for cid, src, txt in pool:
                if win in norm(txt) and family_ok(cite, src, cid, txt):
                    return {"status": "partial", "chunk_id": cid, "source": src, "how": "window only — full sentence not in chunk"}
        if wrong: return {"status": "unverified", "chunk_id": wrong[0], "source": wrong[1], "how": "phrase found, but in another authority's chunk"}
        return {"status": "unverified", "chunk_id": None, "source": None, "how": "no exact corpus phrase"}
    cids = {h[0] for h in hits}
    note = hits[0][2] + ("" if len(hits) == 1 else ("; elided quote, all segments in one chunk" if len(cids) == 1 else "; segments in different chunks — check context"))
    return {"status": "verified", "chunk_id": hits[0][0], "source": hits[0][1], "how": note}


def in_corpus(quote, cite=""):
    """back-compat: (found, where) — found only when verify_phrase says verified."""
    v = verify_phrase(quote, cite)
    return (True, v["chunk_id"]) if v["status"] == "verified" else (False, None)


def in_docs(quote, docs):
    segs = [norm(x) for x in segments(quote)]
    for name, text in docs.items():
        if all(_in_text(q, text) for q in segs): return name
    return None


# ---------------------------------------------------------------- S-cite-drift-normalize (2026-10-24, run 42): the ONE cite pinner
# pipeline/m2/cite_normalize.py (sibling tree first, live tree second). A case name that resolves to exactly one pinned corpus
# source gets the pin's reporter cite; 0 -> FETCH; >1 -> exact match or FETCH. The gate ANNOTATES the card line
# ('GATE: cite normalized OLD -> NEW [source]' / 'GATE: cite FETCH ...'); cards_to_cells rewrites the cell. CITE_NORMALIZE=off disables.
_CN = None
_RESOLVER = None


def cite_normalizer():
    """-> (module, Resolver) | (None, None)."""
    global _CN, _RESOLVER
    if _RESOLVER is not None or _CN is False:
        return (_CN or None), _RESOLVER
    if os.environ.get("CITE_NORMALIZE", "").lower() in ("off", "0", "none"):
        _CN = False; return None, None
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(os.path.dirname(os.path.dirname(here)), "pipeline", "m2", "cite_normalize.py"),
                 str(_eroot() / "pipeline/m2/cite_normalize.py")):
        if os.path.exists(cand):
            try:
                spec = importlib.util.spec_from_file_location("cite_normalize", cand); mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod); R = mod.Resolver(); R.keys(); _CN, _RESOLVER = mod, R
                return _CN, _RESOLVER
            except Exception as e:
                print(f"card_gate: cite normalizer unavailable ({e!r}) -- cites NOT pinned", file=sys.stderr)
                break
    _CN = False
    return None, None


def normalize_note(cite):
    """-> (new_cite, 'GATE: cite normalized OLD -> NEW [source]' | 'GATE: cite FETCH (...)' | '')."""
    cn, R = cite_normalizer()
    if not cn or not cite or " v. " not in cite: return cite, ""
    try:
        new, res = cn.normalize_cite(cite, R)
    except Exception as e:
        return cite, f"GATE: cite normalizer error ({str(e)[:60]})"
    if res["action"] == "normalized": return new, f"GATE: cite normalized {res['old']} -> {res['new']} [{res['source']}]"
    if res["action"] == "fetch": return cite, f"GATE: cite FETCH ({res['why']})"
    return cite, ""


def cite_of(auth):
    """the citation part of an authority field: text before the first quote mark / em-dash."""
    return re.split(r"\s+[—-]\s+|[“\"]", auth or "", 1)[0].strip().strip("[]")


def gate(path, input_dir=None):
    docs = {}
    if input_dir:
        for f in glob.glob(input_dir + "/*.md") + glob.glob(input_dir + "/*.txt"):
            docs[f.split("/")[-1]] = norm(open(f, errors="ignore").read())
    out = []; tally = {"cards": 0, "quoted": 0, "doc": 0, "corpus": 0, "partial": 0, "forced_open": 0, "unquoted_authority": 0}
    for line in open(path, errors="ignore").read().splitlines():
        if not line.startswith("CARD:"):
            out.append(line); continue
        tally["cards"] += 1
        parts = [p.strip() for p in line.split(" | ")]
        parts = [p for p in parts if not p.startswith("GATE:")]          # re-gating: drop stale notes
        # tolerant layout: axis | finding | [where] | [authority] | STATUS [| PROPOSITION: …] — bots drop optional fields
        si = next((i for i in range(len(parts) - 1, 1, -1) if re.match(r"(SUSTAINED|OPEN|REJECTED|NOT IN CORPUS)\b", parts[i].upper())), None)
        if si is None:
            out.append(line); continue
        auth = parts[si - 1] if si >= 3 else ""; status = parts[si]
        parts_status_idx = si
        if "NOT IN CORPUS" in auth or auth in ("—", "-", ""):
            out.append(" | ".join(parts)); continue
        allq = Q.findall(auth) + [q for q in Q.findall(line) if q not in Q.findall(auth)]   # authority-field quotes first
        quotes = [q for q in allq if len(q.split()) >= 4]
        short = [q for q in allq if len(q.split()) < 4]   # 1-3 word quotes (a typo, a term) are checked against the inputs only
        if not quotes and short:
            d = next((in_docs(q, docs) for q in short if in_docs(q, docs)), None)
            if d: tally["doc"] += 1; out.append(" | ".join(parts) + f" | GATE: short quote verified ({d})"); continue
        if not quotes:
            tally["unquoted_authority"] += 1
            if status.upper().startswith("SUSTAINED"):
                parts[parts_status_idx] = "OPEN"; tally["forced_open"] += 1; out.append(" | ".join(parts) + " | GATE: authority carries no quote"); continue
            out.append(" | ".join(parts)); continue
        tally["quoted"] += 1
        cite = cite_of(auth)
        ok = False; where = None; best = None
        for qt in quotes:
            d = in_docs(qt, docs)
            if d: ok = True; where = d; tally["doc"] += 1; break
            v = verify_phrase(qt, cite)
            if v["status"] == "verified": ok = True; where = v["chunk_id"]; tally["corpus"] += 1; break
            if best is None or (v["status"] == "partial" and best["status"] != "partial"): best = v
        _, cn_note = normalize_note(cite)
        cn_note = f" | {cn_note}" if cn_note else ""
        if ok:
            out.append(" | ".join(parts) + f" | GATE: quote verified ({where})" + cn_note)
        else:
            parts[parts_status_idx] = "OPEN"; tally["forced_open"] += 1
            if best and best["status"] == "partial":
                tally["partial"] += 1
                out.append(" | ".join(parts) + f" | GATE: quote NOT verified — partial match only ({best['chunk_id']}); full sentence not in chunk" + cn_note)
            elif best and best.get("chunk_id"):
                out.append(" | ".join(parts) + f" | GATE: quote NOT verified — found only in another authority's chunk ({best['chunk_id']})" + cn_note)
            else:
                out.append(" | ".join(parts) + " | GATE: quote NOT found in corpus or inputs" + cn_note)
    open(path, "w").write("\n".join(out) + "\n")
    return tally


if __name__ == "__main__":
    print(json.dumps(gate(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)))
